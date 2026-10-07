"""Event clips.

Upload: the agent that produced the event, signed like live frames with
HMAC-SHA256(secret, "<event_id>." + body). Bathroom-duration events are never
recorded -- that zone is private by design -- and uploads for them are
refused. Download: any member of the event's household.
"""

from __future__ import annotations

import hashlib
import hmac
import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from mantau_core.contracts import EventKind, CommandType
from ...recording_relay import RelayBusy, RelayResponse

from ...control_auth import authenticated_user
from ...store.agents_repo import AgentsRepo
from ...store.events_repo import EventsRepo
from ...store.recordings_repo import RecordingsCapacityExceeded
from ...store.identity_repo import UserPrincipal
from ..deps import get_agents_repo, get_events_repo
from ..limits import read_limited

router = APIRouter(tags=["recordings"])

_ALLOWED_TYPES = {"video/mp4"}


@router.post("/events/{event_id}/recording", status_code=204)
async def upload_recording(
    event_id: str, request: Request,
    x_mantau_agent: str = Header(...),
    x_mantau_signature: str = Header(...),
    agents: AgentsRepo = Depends(get_agents_repo),
    events: EventsRepo = Depends(get_events_repo),
    transfer_id: str | None = None,
) -> Response:
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in _ALLOWED_TYPES:
        raise HTTPException(415, "unsupported_media_type")
    settings = request.app.state.settings
    agent = await agents.get(x_mantau_agent)
    if agent is None or agent.revoked_at is not None or agent.household_id is None:
        raise HTTPException(401, "unauthorized")
    if transfer_id is not None:
        pending = request.app.state.recording_relay.pending.get(transfer_id)
        if pending is None or pending.future.done() or (pending.agent_id,pending.household_id,pending.event_id) != (agent.agent_id,agent.household_id,event_id):
            raise HTTPException(410, "recording_transfer_expired")
    body = await read_limited(request, settings.recording_max_bytes)
    expected = hmac.new(agent.secret.encode("utf-8"), event_id.encode("utf-8") + b"." + body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_mantau_signature):
        raise HTTPException(401, "unauthorized")
    record = await events.record(agent.household_id, event_id)
    if record is None or await events.agent_for(event_id) != agent.agent_id:
        raise HTTPException(404, "resource_not_found")
    if record.event.kind is EventKind.BATHROOM_DURATION:
        raise HTTPException(403, "recording_not_allowed")
    policy = await request.app.state.detection_settings_repo.get(agent.household_id, record.event.camera_id)
    if transfer_id is not None:
        if not record.recording_permitted:
            raise HTTPException(404, "resource_not_found")
        if not request.app.state.recording_relay.accept(
                transfer_id, agent.household_id, agent.agent_id, event_id, body):
            raise HTTPException(410, "recording_transfer_expired")
        return Response(status_code=204)
    if await request.app.state.agent_recordings_repo.supported(agent.agent_id):
        raise HTTPException(409, "recording_transfer_required")
    if not policy.settings.recordings.enabled:
        raise HTTPException(403, "recording_disabled")
    # Legacy clients remain readable during rollout; updated agents use the one-use relay above.
    repo = request.app.state.recordings_repo
    try:
        await repo.save(household_id=agent.household_id, event_id=event_id,
                        camera_id=record.event.camera_id, body=body, content_type=content_type)
    except RecordingsCapacityExceeded as exc:
        raise HTTPException(507, 'recording_capacity') from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(503, 'recording_storage_unavailable') from exc
    await repo.prune(settings.recording_retention_days)
    return Response(status_code=204)


@router.get("/events/{event_id}/recording")
async def download_recording(event_id: str, request: Request,
                             principal: UserPrincipal = Depends(authenticated_user)):
    event = await request.app.state.events_repo.record(principal.household_id, event_id)
    if event is None or not event.recording_permitted or event.event.kind is EventKind.BATHROOM_DURATION:
        raise HTTPException(404, "resource_not_found")
    recording = await request.app.state.recordings_repo.get(principal.household_id, event_id)
    if recording is not None:
        return FileResponse(recording.path, media_type=recording.content_type,
                            headers={"Cache-Control": "private, no-store"})
    available = await request.app.state.agent_recordings_repo.get(principal.household_id, event_id)
    if available is None:
        raise HTTPException(404, "resource_not_found")
    relay = request.app.state.recording_relay
    try:
        transfer = relay.open(principal.household_id, available['agent_id'], event_id, available['size_bytes'])
    except RelayBusy as exc:
        raise HTTPException(503, "recording_transfer_busy", headers={"Retry-After": "5"}) from exc
    handed_off = False
    try:
        await request.app.state.control_repo.queue(
            agent_id=available['agent_id'], household_id=principal.household_id,
            requested_by_user_id=principal.user_id, command_type=CommandType.UPLOAD_RECORDING,
            payload={"event_id": event_id, "transfer_id": transfer.transfer_id},
            idempotency_key='clip-transfer-'+transfer.transfer_id, ttl_s=60)
        body = await relay.receive(transfer)
        # Membership/camera removal during a delayed transfer takes effect before serving bytes.
        current = await authenticated_user(request)
        event = await request.app.state.events_repo.record(current.household_id, event_id)
        if current.household_id != principal.household_id or event is None or not event.recording_permitted:
            raise HTTPException(404, "resource_not_found")
        response = RelayResponse(body, relay, transfer)
        handed_off = True
        return response
    except asyncio.TimeoutError as exc:
        raise HTTPException(503, "recording_agent_unavailable", headers={"Retry-After": "5"}) from exc
    finally:
        if not handed_off:
            relay.close(transfer)
