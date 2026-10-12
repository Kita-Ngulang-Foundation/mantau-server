"""Server-side fall detection for agents that upload frames instead.

One detector per (agent, camera, session): the fall rules are temporal, so a
stream's frames must reach the same tracking state in order. Sessions expire
after `inference_session_idle_s` without a frame, and the number of live
sessions is capped (each holds a pose model). HYBRID confirmations use a
separate per-session detector with the motion gate off, because they arrive
one frame per event: the question there is only "is someone lying down in
this frame?".

Frames are decoded in memory and dropped after the detector returns; nothing
here writes a frame anywhere. Results of frame ids already answered are kept
for `inference_idempotency_ttl_s` so a retried upload is answered without
running the detector twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from mantau_core.activity import ActivityEngine, Posture, Perception, default_rules
from mantau_core.activity.geometry import zone_at
from mantau_core.contracts import DetectionSettings, EventKind, ZoneKind
from mantau_core.contracts import (
    FallEvent, InferenceCapability, InferenceConfirmation, InferenceResult,
)

from ..config import Settings, build_info

log = logging.getLogger(__name__)

# (camera_id, config, clock) -> a PerceivingDetector
DetectorFactory = Callable[[str, dict, Callable[[], datetime]], object]
# JPEG bytes -> BGR image, or None when the bytes are not a decodable image
FrameDecoder = Callable[[bytes], "np.ndarray | None"]
# Finished per-minute rollups (dicts keyed like camera_minutes) go to this sink.
MinuteSink = Callable[[list[dict]], Awaitable[None]]

CONFIRM_CONFIG = {"motion": {"enabled": False}}
MINUTE_FLUSH_S = 10.0
# Counters per (session, UTC minute); names match the camera_minutes columns.
MINUTE_COUNTERS = (
    "frames_received", "frames_processed", "rate_limited", "out_of_order", "undecodable",
    "confirmations", "frames_people_0", "frames_people_1", "frames_people_multi",
    "cov_no_person_s", "cov_one_person_s", "cov_multi_person_s", "cov_low_confidence_s",
    "cov_outage_s", "events_emitted", "hidden_low_confidence", "hidden_excluded",
)


def default_detector_factory(camera_id: str, config: dict, clock) -> object:
    from mantau_core.detection.mediapipe_adapter import MediapipeDetector
    return MediapipeDetector(camera_id, config, clock=clock)


def default_decoder(jpeg: bytes):
    import cv2
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    return image if image is not None and image.size else None


class InferenceUnavailable(Exception):
    """The detector is not installed or failed to load; reason is safe to show."""


class CapacityExceeded(Exception):
    """Every session slot is busy with a recently active stream."""


class RateLimited(Exception):
    """A session is sending faster than the advertised max_fps."""


class UndecodableFrame(Exception):
    """The body is not a decodable image."""


@dataclass
class _Session:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stream: object | None = None
    confirm: object | None = None
    last_ts: int | None = None
    last_confirm_ts: int | None = None
    last_seen: float = field(default_factory=time.monotonic)
    last_arrival: float = 0.0
    stream_config: dict | None = None
    last_frame_monotonic: float = 0.0
    failed: bool = False
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Prolonged position / nocturnal movement / bathroom duration on this stream,
    # with the camera's saved settings (the agent does not run them in CLOUD).
    activity: ActivityEngine = field(default_factory=lambda: ActivityEngine(default_rules()))
    agent_id: str = ""
    camera_id: str = ""
    household_id: str | None = None
    policy: DetectionSettings | None = None
    # UTC minute start -> counters, flushed to the minute sink.
    minutes: dict[float, dict] = field(default_factory=dict)


class InferenceService:
    def __init__(self, settings: Settings, *, factory: DetectorFactory | None = None,
                 decoder: FrameDecoder | None = None, timer_events=None,
                 minute_sink: MinuteSink | None = None) -> None:
        self.settings = settings
        self._factory = factory or default_detector_factory
        self._decode = decoder or default_decoder
        self._sessions: dict[tuple[str, str, str], _Session] = {}
        self._sessions_lock = asyncio.Lock()
        self._cpu = asyncio.Semaphore(max(1, settings.inference_workers))
        self._answered: OrderedDict[tuple[str, str], tuple[float, InferenceResult]] = OrderedDict()
        self._in_flight: dict[tuple[str, str], asyncio.Future] = {}
        self._computed: dict[tuple[str, str], tuple[str, InferenceResult | None]] = {}
        self.available = False
        self.reason: str | None = "Server inference has not started"
        self._timer_events = timer_events
        self._timer_task = None
        self._timer_pending = []
        self._minute_sink = minute_sink
        self._minute_flush_due = 0.0
        self._background: set[asyncio.Task] = set()

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        if not self.settings.inference_enabled:
            self.available, self.reason = False, "Server inference is disabled"
            return
        try:
            probe = await asyncio.to_thread(
                self._factory, "probe", _detector_config(DetectionSettings()), lambda: datetime.now(timezone.utc))
            await asyncio.to_thread(probe.close)
        except Exception as exc:  # noqa: BLE001 -- report the type only, never paths/secrets
            self.available = False
            self.reason = f"Detector unavailable on this server ({type(exc).__name__})"
            log.warning("server inference unavailable: %s", type(exc).__name__)
            return
        self.available, self.reason = True, None
        if self._timer_events is not None:
            self._timer_task = asyncio.create_task(self._timers(), name="fall-confirmation-timers")

    async def close(self) -> None:
        if self._timer_task is not None:
            self._timer_task.cancel()
            await asyncio.gather(self._timer_task, return_exceptions=True)
            self._timer_task = None
        await asyncio.gather(*list(self._in_flight.values()), return_exceptions=True)
        async with self._sessions_lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            await self._close_session(session)
        await asyncio.gather(*list(self._background), return_exceptions=True)

    def _spawn(self, coroutine) -> None:
        """Run a telemetry write without holding up the caller. Failures are
        already logged inside the write; the task only needs to be kept alive."""
        task = asyncio.create_task(coroutine, name="minute-rollup-write")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def capability(self) -> InferenceCapability:
        return InferenceCapability(
            available=self.available, detector="mediapipe" if self.available else None,
            max_frame_bytes=self.settings.inference_max_frame_bytes,
            max_frame_age_s=self.settings.inference_max_frame_age_s,
            max_fps=self.settings.inference_max_fps, reason=self.reason,
        )

    # -- idempotency -----------------------------------------------------------
    def pending_computation(self, agent_id, frame_id):
        return self._computed.get((agent_id, frame_id))

    def computation(self, agent_id, frame_id, request_hash):
        key = (agent_id, frame_id)
        existing = self._computed.get(key)
        if existing is not None:
            if existing[0] != request_hash:
                raise ValueError('frame_id_conflict')
            return existing[1]
        if len(self._computed) >= 256:
            raise CapacityExceeded()
        self._computed[key] = (request_hash, None)
        return None

    def retain_computation(self, agent_id, frame_id, request_hash, result):
        self._computed[(agent_id, frame_id)] = (request_hash, result)

    def complete_computation(self, agent_id, frame_id):
        self._computed.pop((agent_id, frame_id), None)

    def answered(self, agent_id: str, frame_id: str) -> InferenceResult | None:
        self._expire_answers()
        hit = self._answered.get((agent_id, frame_id))
        return hit[1].model_copy(update={"duplicate": True}) if hit else None

    def remember(self, agent_id: str, result: InferenceResult) -> None:
        self._answered[(agent_id, result.frame_id)] = (time.monotonic(), result)
        self._expire_answers()

    def _expire_answers(self) -> None:
        cutoff = time.monotonic() - self.settings.inference_idempotency_ttl_s
        while self._answered and next(iter(self._answered.values()))[0] < cutoff:
            self._answered.popitem(last=False)
        while len(self._answered) > 4096:
            self._answered.popitem(last=False)

    async def once(self, agent_id: str, frame_id: str,
                   work: Callable[[], Awaitable[InferenceResult]]) -> InferenceResult:
        """Run `work` at most once per (agent, frame id), even for concurrent
        retries; later calls get the first answer marked `duplicate`."""
        key = (agent_id, frame_id)
        cached = self.answered(agent_id, frame_id)
        if cached is not None:
            return cached
        pending = self._in_flight.get(key)
        if pending is not None:
            result = await asyncio.shield(pending)
            return result.model_copy(update={"duplicate": True})
        async def execute():
            try:
                result = await work()
                self.remember(agent_id, result)
                return result
            finally:
                self._in_flight.pop(key, None)
        task = asyncio.create_task(execute(), name='persist-inference-result')
        self._in_flight[key] = task
        task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
        # A disconnected HTTP caller must not cancel computed safety events.
        return await asyncio.shield(task)

    # -- inference -------------------------------------------------------------
    async def infer(self, *, agent_id: str, camera_id: str, session_id: str, frame_id: str,
                    ts_ms: int, captured_at: datetime, event_ids: tuple[str, ...],
                    jpeg: bytes, settings: DetectionSettings | None = None,
                    household_id: str | None = None) -> InferenceResult:
        if not self.available:
            raise InferenceUnavailable(self.reason or "Server inference unavailable")
        # Wall time at arrival, before any waiting: it places the frame in its UTC minute.
        arrived = time.time()
        started = time.perf_counter()
        session = await self._session(agent_id, camera_id, session_id)
        if household_id is not None:
            session.household_id = household_id
        async with session.lock:
            _count(session, arrived, frames_received=1,
                   lag_ms=arrived * 1000 - captured_at.timestamp() * 1000)
            if session.failed:
                raise InferenceUnavailable("The camera detector failed; retry a fresh frame")
            # perf_counter: monotonic() ticks every ~15 ms on Windows.
            arrival = time.perf_counter()
            min_interval = 1.0 / self.settings.inference_max_fps
            # Half the advertised interval: tolerate jitter, refuse floods.
            if session.last_arrival and arrival - session.last_arrival < min_interval / 2:
                _count(session, arrived, rate_limited=1)
                raise RateLimited()
            session.last_arrival = arrival
            session.last_seen = time.monotonic()
            image = await asyncio.to_thread(self._decode, jpeg)
            if image is None:
                _count(session, arrived, undecodable=1)
                raise UndecodableFrame()
            session.captured_at = captured_at
            if event_ids:
                return await self._confirm(session, camera_id, session_id, frame_id, ts_ms,
                                           event_ids, image, started, arrived)
            if session.last_ts is not None and ts_ms <= session.last_ts:
                _count(session, arrived, out_of_order=1)
                return InferenceResult(frame_id=frame_id, session_id=session_id,
                                       processed=False, reason="out_of_order",
                                       server_ms=_ms(started))
            session.last_ts = ts_ms
            policy = settings or DetectionSettings()
            session.policy = policy
            session.last_frame_monotonic = time.perf_counter()
            perception = await self._perceive(session, camera_id, image, ts_ms, policy=policy)
            events = []
            for event in perception.events:
                hidden = _hidden_reason(event, policy, perception.observation)
                if hidden is None:
                    events.append(_server_event(event))
                else:
                    _count(session, arrived, **_hidden_delta(hidden))
            if perception.observation is not None:
                if settings is not None:
                    session.activity.apply_settings(settings)
                events += [_server_event(event)
                           for event in session.activity.update(perception.observation)]
                _count(session, arrived, **_coverage_delta(session.activity.drain_coverage()))
        people = (len(perception.observation.people)
                  if perception.observation is not None else None)
        ms = _ms(started)
        _count(session, arrived, frames_processed=1, events_emitted=len(events), ms=ms,
               **_people_delta(perception.observation))
        return InferenceResult(frame_id=frame_id, session_id=session_id, processed=True,
                               events=events, people=people, server_ms=ms)

    async def _confirm(self, session: _Session, camera_id: str, session_id: str,
                       frame_id: str, ts_ms: int, event_ids: tuple[str, ...], image,
                       started: float, arrived: float) -> InferenceResult:
        if session.last_confirm_ts is not None and ts_ms <= session.last_confirm_ts:
            _count(session, arrived, out_of_order=1)
            return InferenceResult(frame_id=frame_id, session_id=session_id, processed=False,
                                   reason="out_of_order", server_ms=_ms(started))
        session.last_confirm_ts = ts_ms
        perception = await self._perceive(session, camera_id, image, ts_ms, confirm=True)
        people = perception.observation.people if perception.observation is not None else ()
        lying = [p for p in people if p.posture is Posture.LYING]
        if lying:
            confirmed, confidence, reason = True, max(p.confidence for p in lying), None
        elif people:
            confirmed, reason = False, "nobody_lying"
            confidence = max(p.confidence for p in people)
        else:
            confirmed, confidence, reason = False, 0.0, "no_person"
        confirmations = [InferenceConfirmation(event_id=event_id, confirmed=confirmed,
                                               confidence=round(float(confidence), 3),
                                               reason=reason)
                         for event_id in event_ids]
        ms = _ms(started)
        _count(session, arrived, frames_processed=1, confirmations=1, ms=ms,
               **_people_delta(perception.observation))
        return InferenceResult(frame_id=frame_id, session_id=session_id, processed=True,
                               confirmations=confirmations, people=len(people),
                               server_ms=ms)

    async def _perceive(self, session: _Session, camera_id: str, image, ts_ms: int,
                        *, confirm: bool = False, policy: DetectionSettings | None = None):
        """Discard broken temporal state so the next fresh frame can recover."""
        attribute = "confirm" if confirm else "stream"
        try:
            detector = getattr(session, attribute)
            cancelled = []
            config = CONFIRM_CONFIG if confirm else _detector_config(policy or DetectionSettings())
            if not confirm and detector is not None and session.stream_config != config:
                # Apply threshold and model changes to a new temporal session.
                if hasattr(detector, "cancel_candidates"):
                    cancelled = detector.cancel_candidates(ts_ms)
                await asyncio.to_thread(detector.close)
                detector = None
            if detector is None:
                detector = await asyncio.to_thread(
                    self._factory, camera_id, config,
                    lambda: session.captured_at)
                setattr(session, attribute, detector)
                if not confirm:
                    session.stream_config = config
            async with self._cpu:
                result = await asyncio.to_thread(detector.perceive, image, ts_ms)
                return Perception(events=cancelled + result.events, observation=result.observation)
        except Exception as exc:  # noqa: BLE001 -- expose only a safe error type
            session.failed = True
            async with self._sessions_lock:
                for key, current in list(self._sessions.items()):
                    if current is session:
                        self._sessions.pop(key)
            # The caller holds session.lock; taking it again would deadlock.
            await self._close_detectors(session)
            # The session is no longer swept by tick(), so its minutes go now. Rows are
            # popped here; the sink write runs after the caller releases session.lock.
            self._spawn(self._write_minutes(self._take_minutes(session, include_open=True)))
            log.warning("camera inference failed (%s)", type(exc).__name__)
            raise InferenceUnavailable(
                f"Camera inference unavailable ({type(exc).__name__}); retry a fresh frame") from exc

    # -- sessions ----------------------------------------------------------------
    async def _timers(self):
        while True:
            await asyncio.sleep(0.1)
            try:
                await self.tick()
            except Exception as exc:
                log.error("fall timer persistence failed (%s)", type(exc).__name__)

    async def tick(self):
        """Expire confirmations without relying on another upload.

        Keep events in a retry buffer until the same durable dispatcher commits.
        Stream time advances from the last received frame using a monotonic
        server clock; wall-clock changes cannot shorten confirmation.
        """
        for key, session in list(self._sessions.items()):
            async with session.lock:
                if session.stream is None or session.last_ts is None or not hasattr(session.stream, "advance"):
                    continue
                elapsed = time.perf_counter() - session.last_frame_monotonic
                # Do not call a live 7 fps track missing between normal frames.
                if elapsed < 0.3:
                    continue
                ts_ms = session.last_ts + int(elapsed * 1000)
                pending = await asyncio.to_thread(session.stream.advance, ts_ms)
                if pending:
                    self._timer_pending.append((key[0], key[1], [_server_event(e) for e in pending]))
        try:
            while self._timer_pending and self._timer_events is not None:
                agent_id, camera_id, events = self._timer_pending[0]
                await self._timer_events(agent_id, camera_id, events)
                self._timer_pending.pop(0)
        finally:
            # Runs even when a timer event raised (the exception still propagates), so a
            # poison timer event cannot starve the minute rollup.
            if self._minute_sink is not None and time.monotonic() >= self._minute_flush_due:
                self._minute_flush_due = time.monotonic() + MINUTE_FLUSH_S
                try:
                    await self._flush_minutes()
                except Exception as exc:  # noqa: BLE001 -- telemetry only
                    log.warning("minute rollup flush failed (%s)", type(exc).__name__)

    async def _session(self, agent_id: str, camera_id: str, session_id: str) -> _Session:
        key = (agent_id, camera_id, session_id)
        expired: list[_Session] = []
        async with self._sessions_lock:
            now = time.monotonic()
            for other_key, other in list(self._sessions.items()):
                if now - other.last_seen > self.settings.inference_session_idle_s:
                    expired.append(self._sessions.pop(other_key))
            session = self._sessions.get(key)
            if session is None:
                if len(self._sessions) >= self.settings.inference_max_sessions:
                    raise CapacityExceeded()
                session = self._sessions[key] = _Session(agent_id=agent_id, camera_id=camera_id)
            session.last_seen = now
        for old in expired:
            await self._close_session(old)
        return session

    async def _close_session(self, session: _Session) -> None:
        async with session.lock:
            await self._close_detectors(session)
        # In the background: an expired session closes on a new frame's path.
        # close() gathers these, so shutdown still writes them.
        self._spawn(self._write_minutes(self._take_minutes(session, include_open=True)))

    async def _flush_minutes(self, *, include_open: bool = False) -> None:
        rows: list[dict] = []
        for session in list(self._sessions.values()):
            rows += self._take_minutes(session, include_open=include_open)
        await self._write_minutes(rows)

    def _take_minutes(self, session: _Session, *, include_open: bool) -> list[dict]:
        """Pop finished minute buckets as camera_minutes rows. Synchronous, so no
        await can run between the pop and the row build. Counting looks the
        bucket up again on every frame, so a late frame just opens a new one."""
        current = math.floor(time.time() / 60) * 60
        if self._minute_sink is None:
            session.minutes.clear()
            return []
        rows: list[dict] = []
        for minute in sorted(session.minutes):
            if minute >= current and not include_open:
                continue
            bucket = session.minutes.pop(minute)
            if session.household_id is None:
                continue
            try:
                processed = bucket["frames_processed"]
                rows.append({
                    "household_id": session.household_id, "agent_id": session.agent_id,
                    "camera_id": session.camera_id, "minute": minute,
                    **{name: bucket[name] for name in MINUTE_COUNTERS},
                    "server_ms_mean": round(bucket["server_ms_sum"] / processed, 2) if processed else None,
                    "server_ms_max": bucket["server_ms_max"] if processed else None,
                    "capture_lag_ms_min": bucket["capture_lag_ms_min"],
                    "server_build": build_info(self.settings)["server"],
                    # Confirm-only sessions never ran the stream detector: no config to name.
                    "config_hash": config_hash(session.policy) if session.policy is not None else None,
                })
            except Exception as exc:  # noqa: BLE001 -- drop this row, keep the rest
                log.warning("minute rollup row dropped (%s)", type(exc).__name__)
        return rows

    async def _write_minutes(self, rows: list[dict]) -> None:
        if not rows or self._minute_sink is None:
            return
        try:
            await self._minute_sink(rows)
        except Exception as exc:  # noqa: BLE001 -- a failed rollup must not fail inference
            log.warning("minute rollup write dropped (%s)", type(exc).__name__)

    async def _close_detectors(self, session: _Session) -> None:
        for detector in (session.stream, session.confirm):
            if detector is not None:
                try:
                    await asyncio.to_thread(detector.close)
                except Exception:  # noqa: BLE001 -- closing must not fail a request
                    log.warning("closing an inference detector failed")
        session.stream = session.confirm = None

    @property
    def session_count(self) -> int:
        return len(self._sessions)


def _server_event(event: FallEvent) -> FallEvent:
    """Mark server-detected events so history can tell them apart."""
    return event.model_copy(update={"signals": {**event.signals, "server_inference": 1.0,
        "server_detected_at_ms": datetime.now(timezone.utc).timestamp() * 1000}})


def _allowed_event(event: FallEvent, settings: DetectionSettings, observation) -> bool:
    return _hidden_reason(event, settings, observation) is None


def _hidden_reason(event: FallEvent, settings: DetectionSettings, observation) -> str | None:
    """None when the event is shown; otherwise why it is hidden: 'disabled',
    'low_confidence' or 'excluded'. Also sets `alert_suppressed` on candidates."""
    if event.kind is not EventKind.FALL:
        return None
    candidate = event.signals.get("research_candidate") == 1
    # Research has its own loose/high thresholds; the legacy confidence
    # slider must not silently suppress a valid Tier B confirmation.
    if candidate and not settings.fall.enabled:
        event.signals["alert_suppressed"] = 1.0
    if not candidate and not settings.fall.enabled:
        return "disabled"
    if not candidate and event.confidence < settings.fall.min_confidence:
        return "low_confidence"
    excluded = settings.zones_of(ZoneKind.EXCLUDED)
    if not excluded:
        return None
    person = next((p for p in observation.people if p.track_id == event.track_id), None) \
        if observation is not None else None
    # Unknown geometry cannot establish that this fall is outside an exclusion.
    if candidate and all(k in event.signals for k in ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2")):
        x = (event.signals["bbox_x1"] + event.signals["bbox_x2"]) / 2
        y = event.signals["bbox_y2"]
        if zone_at(x, y, excluded) is not None:
            event.signals["alert_suppressed"] = 1.0
        return None  # All candidates remain available for camera tuning.
    if person is not None and zone_at(*person.anchor, excluded) is None:
        return None
    return "excluded"


def _count(session: _Session, at: float, *, lag_ms: float | None = None,
           ms: float | None = None, **deltas: float) -> None:
    """Add to the UTC-minute bucket holding wall time `at`. Bookkeeping only:
    a failure here is logged and never reaches the frame."""
    try:
        minute = math.floor(at / 60) * 60
        bucket = session.minutes.get(minute)
        if bucket is None:
            bucket = session.minutes[minute] = {
                **dict.fromkeys(MINUTE_COUNTERS, 0), "server_ms_sum": 0.0,
                "server_ms_max": 0.0, "capture_lag_ms_min": None}
        for name, value in deltas.items():
            bucket[name] += value
        if lag_ms is not None:
            current = bucket["capture_lag_ms_min"]
            bucket["capture_lag_ms_min"] = lag_ms if current is None else min(current, lag_ms)
        if ms is not None:
            bucket["server_ms_sum"] += ms
            bucket["server_ms_max"] = max(bucket["server_ms_max"], ms)
    except Exception:  # noqa: BLE001 -- counting must never fail a frame
        log.warning("minute counter skipped")


def _people_delta(observation) -> dict[str, float]:
    if observation is None:
        return {}
    count = len(observation.people)
    return {"frames_people_multi" if count >= 2 else f"frames_people_{count}": 1}


def _hidden_delta(reason: str) -> dict[str, float]:
    # 'disabled' is deliberately not counted (team decision).
    column = {"low_confidence": "hidden_low_confidence", "excluded": "hidden_excluded"}.get(reason)
    return {column: 1} if column else {}


def _coverage_delta(totals: dict[str, float]) -> dict[str, float]:
    return {f"cov_{state}_s": seconds for state, seconds in totals.items()}


def _detector_config(settings: DetectionSettings) -> dict:
    fall = settings.fall.model_dump()
    return {"fall_model": fall.pop("model"), "fall_confirmation": fall,
            "zones": [zone.model_dump(mode="json") for zone in settings.zones]}


def config_hash(settings: DetectionSettings) -> str:
    """Short fingerprint of the detector config. Zones are left out: the hash
    names the model and thresholds, not the camera's geometry."""
    config = {key: value for key, value in _detector_config(settings).items() if key != "zones"}
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
