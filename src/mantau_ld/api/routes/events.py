"""List/inspect events, acknowledge one (closes the latency trace's ACKED
stage), and human triage (dismiss/confirm) matching the app's FallStatus.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mantau_core.contracts import FallEvent
from mantau_core.notify.delivery import AckService
from mantau_core.telemetry import Stage
from pydantic import BaseModel

from ...alerts.dispatcher import AlertDispatcher
from ...store.events_repo import VALID_STATUSES, EventsRepo
from ..deps import get_ack_service, get_dispatcher, get_events_repo

router = APIRouter(prefix="/events", tags=["events"])


class EventOut(BaseModel):
    event_id: str
    camera_id: str
    kind: str
    severity: str
    occurred_at: str
    confidence: float
    status: str

    @classmethod
    def from_event(cls, event: FallEvent, status: str) -> "EventOut":
        return cls(
            event_id=event.event_id, camera_id=event.camera_id, kind=event.kind.value,
            severity=event.severity.value, occurred_at=event.occurred_at.isoformat(),
            confidence=event.confidence, status=status,
        )


async def _out(event: FallEvent, repo: EventsRepo) -> EventOut:
    status = await repo.get_status(event.event_id) or "needs_review"
    return EventOut.from_event(event, status)


@router.get("", response_model=list[EventOut])
async def list_events(limit: int = 100, repo: EventsRepo = Depends(get_events_repo)) -> list[EventOut]:
    return [await _out(e, repo) for e in await repo.list_all(limit=limit)]


@router.get("/{event_id}", response_model=EventOut)
async def get_event(event_id: str, repo: EventsRepo = Depends(get_events_repo)) -> EventOut:
    event = await repo.get(event_id)
    if event is None:
        raise HTTPException(404, f"event {event_id!r} not found")
    return await _out(event, repo)


class AckRequest(BaseModel):
    member_id: str


class AckResponse(BaseModel):
    event_id: str
    first_ack: bool
    latency_to_ack_s: float | None


@router.post("/{event_id}/ack", response_model=AckResponse)
async def ack_event(
    event_id: str,
    body: AckRequest,
    repo: EventsRepo = Depends(get_events_repo),
    ack_service: AckService = Depends(get_ack_service),
    dispatcher: AlertDispatcher = Depends(get_dispatcher),
) -> AckResponse:
    if await repo.get(event_id) is None:
        raise HTTPException(404, f"event {event_id!r} not found")
    first = ack_service.ack(event_id, member_id=body.member_id)
    trace = dispatcher.get_trace(event_id)
    if trace is not None:
        trace.stamp(Stage.ACKED)
    latency = trace.elapsed(end=Stage.ACKED) if trace is not None else None
    return AckResponse(event_id=event_id, first_ack=first, latency_to_ack_s=latency)


class LatencyOut(BaseModel):
    event_id: str
    summary: dict[str, float | None]      # LatencyTrace.to_summary() -- seconds since captured
    within_budget_delivered: bool | None   # None if not measurable yet


@router.get("/{event_id}/latency", response_model=LatencyOut)
async def get_latency(
    event_id: str,
    repo: EventsRepo = Depends(get_events_repo),
    dispatcher: AlertDispatcher = Depends(get_dispatcher),
) -> LatencyOut:
    """The per-stage breakdown behind the project's actual claim: under 5
    seconds, captured to delivered. Mirrors mantau-backend-rtsp's identical
    route -- `mantau-testbed`'s comparison harness reads this from both
    backends to build one table instead of trusting two different partial
    views."""
    if await repo.get(event_id) is None:
        raise HTTPException(404, f"event {event_id!r} not found")
    trace = dispatcher.get_trace(event_id)
    if trace is None:
        return LatencyOut(event_id=event_id, summary={}, within_budget_delivered=None)
    return LatencyOut(event_id=event_id, summary=trace.to_summary(),
                      within_budget_delivered=trace.within_budget(5.0))


class StatusRequest(BaseModel):
    status: str  # "dismissed" | "confirmed" | "needs_review"


@router.post("/{event_id}/status", response_model=EventOut)
async def set_status(
    event_id: str, body: StatusRequest, repo: EventsRepo = Depends(get_events_repo)
) -> EventOut:
    event = await repo.get(event_id)
    if event is None:
        raise HTTPException(404, f"event {event_id!r} not found")
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"invalid status, must be one of {sorted(VALID_STATUSES)}")
    await repo.set_status(event_id, body.status)
    return EventOut.from_event(event, body.status)
