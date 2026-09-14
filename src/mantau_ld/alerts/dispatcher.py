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

from mantau_core.contracts import FallEvent
from mantau_core.notify import Fanout
from mantau_core.telemetry import LatencyTrace, Stage

from ..store.cameras_repo import CamerasRepo
from ..store.events_repo import EventsRepo


class AlertDispatcher:
    def __init__(self, events_repo: EventsRepo, cameras_repo: CamerasRepo, fanout: Fanout) -> None:
        self.events_repo = events_repo
        self.cameras_repo = cameras_repo
        self.fanout = fanout
        # Short-lived, in-process -- matches mantau_core's own weekend-scope
        # choice for AckService and mantau-backend-rtsp's dispatcher.
        self.traces: dict[str, LatencyTrace] = {}

    async def dispatch(self, event: FallEvent) -> None:
        trace = LatencyTrace(event.event_id)
        trace.stamp(Stage.CAPTURED, at=event.occurred_at.timestamp())
        trace.stamp(Stage.DETECTED, at=event.occurred_at.timestamp())
        self.traces[event.event_id] = trace

        await self.events_repo.insert(event)
        camera_name = await self.cameras_repo.name_for(event.camera_id)
        await self.fanout.send(event, camera_name=camera_name, trace=trace)

    def get_trace(self, event_id: str) -> LatencyTrace | None:
        return self.traces.get(event_id)
