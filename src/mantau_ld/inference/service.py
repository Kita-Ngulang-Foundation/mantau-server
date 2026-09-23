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
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from mantau_core.activity import ActivityEngine, Posture, default_rules
from mantau_core.contracts import DetectionSettings
from mantau_core.contracts import (
    FallEvent, InferenceCapability, InferenceConfirmation, InferenceResult,
)

from ..config import Settings

log = logging.getLogger(__name__)

# (camera_id, config, clock) -> a PerceivingDetector
DetectorFactory = Callable[[str, dict, Callable[[], datetime]], object]
# JPEG bytes -> BGR image, or None when the bytes are not a decodable image
FrameDecoder = Callable[[bytes], "np.ndarray | None"]

CONFIRM_CONFIG = {"motion": {"enabled": False}}


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
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Prolonged position / nocturnal movement / bathroom duration on this stream,
    # with the camera's saved settings (the agent does not run them in CLOUD).
    activity: ActivityEngine = field(default_factory=lambda: ActivityEngine(default_rules()))


class InferenceService:
    def __init__(self, settings: Settings, *, factory: DetectorFactory | None = None,
                 decoder: FrameDecoder | None = None) -> None:
        self.settings = settings
        self._factory = factory or default_detector_factory
        self._decode = decoder or default_decoder
        self._sessions: dict[tuple[str, str, str], _Session] = {}
        self._sessions_lock = asyncio.Lock()
        self._cpu = asyncio.Semaphore(max(1, settings.inference_workers))
        self._answered: OrderedDict[tuple[str, str], tuple[float, InferenceResult]] = OrderedDict()
        self._in_flight: dict[tuple[str, str], asyncio.Future] = {}
        self.available = False
        self.reason: str | None = "Server inference has not started"

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        if not self.settings.inference_enabled:
            self.available, self.reason = False, "Server inference is disabled"
            return
        try:
            probe = await asyncio.to_thread(
                self._factory, "probe", {}, lambda: datetime.now(timezone.utc))
            await asyncio.to_thread(probe.close)
        except Exception as exc:  # noqa: BLE001 -- report the type only, never paths/secrets
            self.available = False
            self.reason = f"Detector unavailable on this server ({type(exc).__name__})"
            log.warning("server inference unavailable: %s", type(exc).__name__)
            return
        self.available, self.reason = True, None

    async def close(self) -> None:
        async with self._sessions_lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            await self._close_session(session)

    def capability(self) -> InferenceCapability:
        return InferenceCapability(
            available=self.available, detector="mediapipe" if self.available else None,
            max_frame_bytes=self.settings.inference_max_frame_bytes,
            max_frame_age_s=self.settings.inference_max_frame_age_s,
            max_fps=self.settings.inference_max_fps, reason=self.reason,
        )

    # -- idempotency -----------------------------------------------------------
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
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._in_flight[key] = future
        try:
            result = await work()
            self.remember(agent_id, result)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # consumed here; waiters re-raise their own copy
            raise
        finally:
            self._in_flight.pop(key, None)

    # -- inference -------------------------------------------------------------
    async def infer(self, *, agent_id: str, camera_id: str, session_id: str, frame_id: str,
                    ts_ms: int, captured_at: datetime, event_ids: tuple[str, ...],
                    jpeg: bytes, settings: DetectionSettings | None = None) -> InferenceResult:
        if not self.available:
            raise InferenceUnavailable(self.reason or "Server inference unavailable")
        started = time.perf_counter()
        session = await self._session(agent_id, camera_id, session_id)
        async with session.lock:
            # perf_counter: monotonic() ticks every ~15 ms on Windows.
            arrival = time.perf_counter()
            min_interval = 1.0 / self.settings.inference_max_fps
            # Half the advertised interval: tolerate jitter, refuse floods.
            if session.last_arrival and arrival - session.last_arrival < min_interval / 2:
                raise RateLimited()
            session.last_arrival = arrival
            session.last_seen = time.monotonic()
            image = await asyncio.to_thread(self._decode, jpeg)
            if image is None:
                raise UndecodableFrame()
            session.captured_at = captured_at
            if event_ids:
                return await self._confirm(session, camera_id, session_id, frame_id, ts_ms,
                                           event_ids, image, started)
            if session.last_ts is not None and ts_ms <= session.last_ts:
                return InferenceResult(frame_id=frame_id, session_id=session_id,
                                       processed=False, reason="out_of_order",
                                       server_ms=_ms(started))
            session.last_ts = ts_ms
            if session.stream is None:
                session.stream = await asyncio.to_thread(
                    self._factory, camera_id, {}, lambda: session.captured_at)
            async with self._cpu:
                perception = await asyncio.to_thread(session.stream.perceive, image, ts_ms)
            events = [_server_event(event) for event in perception.events]
            if perception.observation is not None:
                if settings is not None:
                    session.activity.apply_settings(settings)
                events += [_server_event(event)
                           for event in session.activity.update(perception.observation)]
        people = (len(perception.observation.people)
                  if perception.observation is not None else None)
        return InferenceResult(frame_id=frame_id, session_id=session_id, processed=True,
                               events=events, people=people, server_ms=_ms(started))

    async def _confirm(self, session: _Session, camera_id: str, session_id: str,
                       frame_id: str, ts_ms: int, event_ids: tuple[str, ...], image,
                       started: float) -> InferenceResult:
        if session.last_confirm_ts is not None and ts_ms <= session.last_confirm_ts:
            return InferenceResult(frame_id=frame_id, session_id=session_id, processed=False,
                                   reason="out_of_order", server_ms=_ms(started))
        session.last_confirm_ts = ts_ms
        if session.confirm is None:
            session.confirm = await asyncio.to_thread(
                self._factory, camera_id, CONFIRM_CONFIG, lambda: session.captured_at)
        async with self._cpu:
            perception = await asyncio.to_thread(session.confirm.perceive, image, ts_ms)
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
        return InferenceResult(frame_id=frame_id, session_id=session_id, processed=True,
                               confirmations=confirmations, people=len(people),
                               server_ms=_ms(started))

    # -- sessions ----------------------------------------------------------------
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
                session = self._sessions[key] = _Session()
            session.last_seen = now
        for old in expired:
            await self._close_session(old)
        return session

    async def _close_session(self, session: _Session) -> None:
        async with session.lock:
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
    return event.model_copy(update={"signals": {**event.signals, "server_inference": 1.0}})


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
