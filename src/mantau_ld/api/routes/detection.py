"""Per-camera detection settings.

Any household member may read them; owners and admins change them. A change
is stored as a new version and delivered to the camera's agent with the
`apply_detection_settings` command; the agent's success result records which
version it is running.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from mantau_core.contracts import CommandType, DetectionSettings
from pydantic import BaseModel

from ...control_auth import authenticated_user
from ...store.identity_repo import UserPrincipal
from ...store.detection_settings_repo import SettingsConflict
from ..deps import get_cameras_repo, get_control_repo
from ...store.cameras_repo import CamerasRepo
from ...store.control_repo import ControlRepo

router = APIRouter(tags=["detection-settings"])


class DetectionSettingsOut(BaseModel):
    camera_id: str
    settings: DetectionSettings
    applied_version: int | None
    customized: bool
    command_id: str | None = None
    recordings_supported: bool = False
    # The camera's agent applies the `stream` section; older agents ignore it.
    stream_supported: bool = False


@router.get("/cameras/{camera_id}/detection-settings", response_model=DetectionSettingsOut)
async def get_settings(camera_id: str, request: Request,
                       cameras: CamerasRepo = Depends(get_cameras_repo),
                       principal: UserPrincipal = Depends(authenticated_user)):
    camera = await cameras.get_for_household(principal.household_id, camera_id)
    if camera is None:
        raise HTTPException(404, "resource_not_found")
    stored = await request.app.state.detection_settings_repo.get(principal.household_id, camera_id)
    return DetectionSettingsOut(camera_id=camera_id, settings=stored.settings,
                                applied_version=stored.applied_version, customized=stored.stored,
                                recordings_supported=await request.app.state.agent_recordings_repo.supported(camera.agent_id),
                                stream_supported=await request.app.state.agents_repo.stream_settings_supported(camera.agent_id))


@router.put("/cameras/{camera_id}/detection-settings", response_model=DetectionSettingsOut)
async def put_settings(camera_id: str, body: DetectionSettings, request: Request,
                       cameras: CamerasRepo = Depends(get_cameras_repo),
                       control: ControlRepo = Depends(get_control_repo),
                       principal: UserPrincipal = Depends(authenticated_user)):
    camera = await cameras.get_for_household(principal.household_id, camera_id)
    if camera is None:
        raise HTTPException(404, "resource_not_found")
    if principal.role not in ("owner", "admin"):
        raise HTTPException(403, "forbidden")
    repo = request.app.state.detection_settings_repo
    supports_recordings = await request.app.state.agent_recordings_repo.supported(camera.agent_id)
    supports_stream = await request.app.state.agents_repo.stream_settings_supported(camera.agent_id)
    previous = await repo.get(principal.household_id, camera_id)
    if body.recordings != previous.settings.recordings and not supports_recordings:
        raise HTTPException(409, "agent_recordings_upgrade_required")
    if "stream" not in body.model_fields_set:
        # An app that predates stream settings must not reset them to defaults.
        body = body.model_copy(update={"stream": previous.settings.stream})
    elif body.stream != previous.settings.stream and not supports_stream:
        raise HTTPException(409, "agent_stream_upgrade_required")
    try:
        async with request.app.state.db.transaction():
            stored = await repo.save(principal.household_id, camera_id, body, principal.user_id)
            command_id = None
            if camera.agent_id:
                delivered = stored.model_dump(mode="json")
                if not supports_recordings:
                    delivered.pop("recordings", None)
                if not supports_stream:
                    delivered.pop("stream", None)
                command = await control.queue(
                    agent_id=camera.agent_id, household_id=principal.household_id,
                    requested_by_user_id=principal.user_id,
                    command_type=CommandType.APPLY_DETECTION_SETTINGS,
                    payload={"camera_id": camera_id, "settings": delivered},
                    idempotency_key=f"detection-settings-{camera_id}-{stored.version}-{uuid.uuid4().hex}",
                    ttl_s=request.app.state.settings.command_ttl_s,
                )
                command_id = command.command_id
    except SettingsConflict as exc:
        raise HTTPException(409, "settings_version_conflict") from exc
    current = await repo.get(principal.household_id, camera_id)
    return DetectionSettingsOut(camera_id=camera_id, settings=stored,
                                applied_version=current.applied_version, customized=True,
                                command_id=command_id, recordings_supported=supports_recordings,
                                stream_supported=supports_stream)
