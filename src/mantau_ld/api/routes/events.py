"""Event history for the household, acknowledgement, and human triage
(needs_review / dismissed / confirmed, matching the app's FallStatus).

Who acknowledged or reviewed an event is always the authenticated user; the
request body can never name someone else.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from mantau_core.notify.delivery import AckService
from mantau_core.telemetry import Stage
from pydantic import BaseModel

from ...alerts.dispatcher import AlertDispatcher
from ...control_auth import authenticated_user
from ...store.events_repo import VALID_STATUSES, EventRecord, EventsRepo
from ...store.identity_repo import UserPrincipal
from ..deps import get_ack_service, get_dispatcher, get_events_repo

router = APIRouter(prefix="/events", tags=["events"])


def _ts(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


class EventOut(BaseModel):
    event_id: str
    camera_id: str
    camera_name: str | None = None
    kind: str
    severity: str
    occurred_at: str
    confidence: float
    status: str
    signals: dict[str, float] = {}
    created_at: float | None = None      # pagination cursor for `before`
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    has_recording: bool = False

    @classmethod
    def from_record(cls, record: EventRecord) -> "EventOut":
        event = record.event
        return cls(
            event_id=event.event_id, camera_id=event.camera_id,
            camera_name=record.camera_name, kind=event.kind.value,
            severity=event.severity.value, occurred_at=event.occurred_at.isoformat(),
            confidence=event.confidence, status=record.status, signals=event.signals,
            created_at=record.created_at,
            acknowledged_at=_ts(record.acknowledged_at), acknowledged_by=record.acknowledged_by,
            reviewed_at=_ts(record.reviewed_at), reviewed_by=record.reviewed_by,
            has_recording=record.has_recording,
        )


async def _record(repo: EventsRepo, principal: UserPrincipal, event_id: str) -> EventRecord:
    record = await repo.record(principal.household_id, event_id)
    if record is None:
        raise HTTPException(404, "resource_not_found")
    return record


@router.get("", response_model=list[EventOut])
async def list_events(
    limit: int = Query(50, ge=1, le=200),
    before: float | None = Query(None, description="created_at of the last event already shown"),
    kind: str | None = Query(None, pattern="^(fall|stillness|nocturnal_movement|bathroom_duration)$"),
    repo: EventsRepo = Depends(get_events_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> list[EventOut]:
    records = await repo.records(principal.household_id, limit=limit, before=before, kind=kind)
    return [EventOut.from_record(r) for r in records]


@router.get("/{event_id}", response_model=EventOut)
async def get_event(
    event_id: str,
    repo: EventsRepo = Depends(get_events_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> EventOut:
    return EventOut.from_record(await _record(repo, principal, event_id))


class AckRequest(BaseModel):
    # Accepted for older clients and ignored: the acknowledging user is the
    # authenticated caller.
    member_id: str | None = None


class AckResponse(BaseModel):
    event_id: str
    first_ack: bool
    latency_to_ack_s: float | None


@router.post("/{event_id}/ack", response_model=AckResponse)
async def ack_event(
    event_id: str,
    body: AckRequest | None = None,
    repo: EventsRepo = Depends(get_events_repo),
    ack_service: AckService = Depends(get_ack_service),
    dispatcher: AlertDispatcher = Depends(get_dispatcher),
    principal: UserPrincipal = Depends(authenticated_user),
) -> AckResponse:
    await _record(repo, principal, event_id)
    first = await repo.acknowledge(principal.household_id, event_id, principal.user_id)
    ack_service.ack(event_id, member_id=principal.user_id)
    trace = dispatcher.get_trace(event_id)
    if trace is not None and first:
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
    principal: UserPrincipal = Depends(authenticated_user),
) -> LatencyOut:
    """Per-stage breakdown behind the claim: under 5 seconds, captured to
    delivered."""
    await _record(repo, principal, event_id)
    trace = dispatcher.get_trace(event_id)
    if trace is None:
        return LatencyOut(event_id=event_id, summary={}, within_budget_delivered=None)
    return LatencyOut(event_id=event_id, summary=trace.to_summary(),
                      within_budget_delivered=trace.within_budget(5.0))


class StatusRequest(BaseModel):
    status: str  # "dismissed" | "confirmed" | "needs_review"


@router.post("/{event_id}/status", response_model=EventOut)
async def set_status(
    event_id: str, body: StatusRequest,
    repo: EventsRepo = Depends(get_events_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> EventOut:
    await _record(repo, principal, event_id)
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, "invalid_status")
    await repo.review(principal.household_id, event_id, body.status, principal.user_id)
    return EventOut.from_record(await _record(repo, principal, event_id))
