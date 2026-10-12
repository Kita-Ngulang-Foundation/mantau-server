"""FallEvent -> persisted + fanned out, with a latency trace attached.

`captured_at` here comes from the event's own `occurred_at` (the agent's
wall-clock time when it detected the fall) rather than a separately
transmitted capture timestamp -- the envelope doesn't carry frame-level
timing, only the already-detected event. This means the CAPTURED stage of
the trace actually reads closer to "agent-side detection time," a few
milliseconds after the real frame capture, not the frame capture itself.
Close enough for the <5s budget; worth knowing about if a comparison against
mantau-backend-rtsp's trace (whose CAPTURED stage IS the frame timestamp)
is ever read down to the millisecond.
"""

from __future__ import annotations

import logging
import time

from mantau_core.contracts import FallEvent
from mantau_core.notify import Fanout
from mantau_core.telemetry import LatencyTrace, Stage

from ..store.cameras_repo import CamerasRepo
from ..store.events_repo import EventsRepo
from ..store.telemetry_repo import TelemetryRepo
from .outbox import PushOutbox

log = logging.getLogger(__name__)


class AlertDispatcher:
    def __init__(self, events_repo: EventsRepo, cameras_repo: CamerasRepo, fanout: Fanout,
                 telemetry: TelemetryRepo | None = None) -> None:
        self.events_repo = events_repo
        self.cameras_repo = cameras_repo
        self.fanout = fanout
        # Short-lived, in-process -- matches mantau_core's own weekend-scope
        # choice for AckService and mantau-backend-rtsp's dispatcher.
        self.traces: dict[str, LatencyTrace] = {}
        self.telemetry = telemetry
        self.outbox = PushOutbox(events_repo, cameras_repo, fanout)
        self.outbox.trace_for = self.get_trace
        self.outbox.telemetry = telemetry

    async def dispatch(self, event: FallEvent, *, household_id: str, agent_id: str, deliver: bool = True) -> None:
        candidate = event.signals.get("research_candidate") == 1
        # Pending/recovered candidates are history entries, not alert traces.
        if candidate and event.signals.get("candidate_outcome") != 1:
            if await self.outbox.enqueue(event, household_id, agent_id):
                await self._stamp(event, household_id, agent_id, candidate_at=time.time())
            return
        captured_at = event.occurred_at.timestamp()
        detected_at = (event.signals.get("server_detected_at_ms", captured_at * 1000) / 1000)
        detected_at = detected_at if candidate else captured_at
        if event.event_id not in self.traces:
            trace = LatencyTrace(event.event_id)
            trace.stamp(Stage.CAPTURED, at=captured_at)
            trace.stamp(Stage.DETECTED, at=detected_at)
            self.traces[event.event_id] = trace

        if await self.outbox.enqueue(event, household_id, agent_id):
            stamps = {"captured_at": captured_at, "detected_at": detected_at,
                      "queued_at": self.outbox.last_queued_at}
            if candidate:
                # A confirmed candidate: when it was first logged, and when confirmed.
                now = time.time()
                stamps.update(candidate_at=now, confirmed_at=now)
            await self._stamp(event, household_id, agent_id, **stamps)
        if deliver:
            await self.outbox.deliver_pending()
        if len(self.traces) > 1000:
            self.traces.pop(next(iter(self.traces)))

    async def _stamp(self, event: FallEvent, household_id: str, agent_id: str, **stamps) -> None:
        """Persist pipeline times for one event. Runs inside the caller's
        transaction through savepoints, so a failure never rolls back the event."""
        if self.telemetry is None:
            return
        try:
            stamps = {name: value for name, value in stamps.items() if value is not None}
            stamps["build_json"] = await self.telemetry.build_json_for(
                household_id, event.camera_id, agent_id)
            await self.telemetry.stamp_event(event.event_id, household_id, **stamps)
        except Exception as exc:  # noqa: BLE001 -- telemetry never blocks an alert
            log.warning("event telemetry dropped (%s)", type(exc).__name__)

    def get_trace(self, event_id: str) -> LatencyTrace | None:
        return self.traces.get(event_id)
