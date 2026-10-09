"""Event clips, relayed once from the agent that keeps them.

Clips stay on the agent (and on the family's phone). The server never stores
them: a household member's download queues an UPLOAD_RECORDING command, the
agent uploads the clip with that one-use transfer id, signed like live frames
with HMAC-SHA256(secret, "<event_id>." + body), and the bytes are passed
through memory to the waiting download. An upload without a transfer id
(agents older than local clip retention) is refused with 409
`recording_transfer_required`. Bathroom-duration events are never recorded --
that zone is private by design.
"""

from __future__ import annotations

import hashlib
import hmac
import asyncio
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from mantau_core.contracts import EventKind, CommandType
from ...recording_relay import RelayBusy, RelayResponse

from ...control_auth import authenticated_user, authenticated_agent
from ...store.agents_repo import AgentsRepo
from ...store.events_repo import EventsRepo
from ...store.identity_repo import UserPrincipal
from ..deps import get_agents_repo, get_events_repo
from ..limits import read_limited

router = APIRouter(tags=["recordings"])

_ALLOWED_TYPES = {"video/mp4"}


class LegacyClipCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ids: list[Annotated[str, Field(min_length=1, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]] = Field(max_length=5)

    @field_validator("event_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate event identifiers")
        return values


@router.post("/agent-control/recordings/legacy-check")
async def legacy_clip_check(body: LegacyClipCheck, request: Request,
                            agent=Depends(authenticated_agent)):
    """Metadata only. Never infer a legacy file's ownership from its filename."""
    if agent.household_id is None:
        raise HTTPException(401, "unauthorized")
    approved = []
    events = request.app.state.events_repo
    async with request.app.state.db.transaction():
        for event_id in body.event_ids:
            record = await events.record(agent.household_id, event_id)
            if (record is None or not record.recording_permitted
                    or record.event.kind is EventKind.BATHROOM_DURATION
                    or await events.agent_for(event_id) != agent.agent_id):
                continue
            camera = await request.app.state.cameras_repo.get_for_household(
                agent.household_id, record.event.camera_id)
            if camera is None or camera.agent_id != agent.agent_id:
                continue
            approved.append({
                "event_id": event_id, "camera_id": record.event.camera_id,
                "kind": record.event.kind.value,
                "occurred_at_ms": int(record.event.occurred_at.timestamp() * 1000),
            })
    return {"recordings": approved}


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
    if transfer_id is not None:
        if not record.recording_permitted:
            raise HTTPException(404, "resource_not_found")
        if not request.app.state.recording_relay.accept(
                transfer_id, agent.household_id, agent.agent_id, event_id, body):
            raise HTTPException(410, "recording_transfer_expired")
        return Response(status_code=204)
    # No server-side storage: every clip goes through the one-use relay above.
    raise HTTPException(409, "recording_transfer_required")


@router.get("/events/{event_id}/recording")
async def download_recording(event_id: str, request: Request,
                             principal: UserPrincipal = Depends(authenticated_user)):
    event = await request.app.state.events_repo.record(principal.household_id, event_id)
    if event is None or not event.recording_permitted or event.event.kind is EventKind.BATHROOM_DURATION:
        raise HTTPException(404, "resource_not_found")
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
