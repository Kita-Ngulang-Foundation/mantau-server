"""Event clips.

Upload: the agent that produced the event, signed like live frames with
HMAC-SHA256(secret, "<event_id>." + body). Bathroom-duration events are never
recorded -- that zone is private by design -- and uploads for them are
refused. Download: any member of the event's household.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from mantau_core.contracts import EventKind

from ...control_auth import authenticated_user
from ...store.agents_repo import AgentsRepo
from ...store.events_repo import EventsRepo
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
) -> Response:
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in _ALLOWED_TYPES:
        raise HTTPException(415, "unsupported_media_type")
    settings = request.app.state.settings
    body = await read_limited(request, settings.recording_max_bytes)
    agent = await agents.get(x_mantau_agent)
    if agent is None or agent.revoked_at is not None or agent.household_id is None:
        raise HTTPException(401, "unauthorized")
    expected = hmac.new(agent.secret.encode("utf-8"), event_id.encode("utf-8") + b"." + body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_mantau_signature):
        raise HTTPException(401, "unauthorized")
    record = await events.record(agent.household_id, event_id)
    if record is None or await events.agent_for(event_id) != agent.agent_id:
        raise HTTPException(404, "resource_not_found")
    if record.event.kind is EventKind.BATHROOM_DURATION:
        raise HTTPException(403, "recording_not_allowed")
    repo = request.app.state.recordings_repo
    await repo.save(household_id=agent.household_id, event_id=event_id,
                    camera_id=record.event.camera_id, body=body, content_type=content_type)
    await repo.prune(settings.recording_retention_days)
    return Response(status_code=204)


@router.get("/events/{event_id}/recording")
async def download_recording(event_id: str, request: Request,
                             principal: UserPrincipal = Depends(authenticated_user)):
    recording = await request.app.state.recordings_repo.get(principal.household_id, event_id)
    if recording is None:
        raise HTTPException(404, "resource_not_found")
    return FileResponse(recording.path, media_type=recording.content_type,
                        headers={"Cache-Control": "private, no-store"})
