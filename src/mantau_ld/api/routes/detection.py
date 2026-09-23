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


@router.get("/cameras/{camera_id}/detection-settings", response_model=DetectionSettingsOut)
async def get_settings(camera_id: str, request: Request,
                       cameras: CamerasRepo = Depends(get_cameras_repo),
                       principal: UserPrincipal = Depends(authenticated_user)):
    if await cameras.get_for_household(principal.household_id, camera_id) is None:
        raise HTTPException(404, "resource_not_found")
    stored = await request.app.state.detection_settings_repo.get(principal.household_id, camera_id)
    return DetectionSettingsOut(camera_id=camera_id, settings=stored.settings,
                                applied_version=stored.applied_version, customized=stored.stored)


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
    stored = await repo.save(principal.household_id, camera_id, body, principal.user_id)
    command_id = None
    if camera.agent_id:
        command = await control.queue(
            agent_id=camera.agent_id, household_id=principal.household_id,
            requested_by_user_id=principal.user_id,
            command_type=CommandType.APPLY_DETECTION_SETTINGS,
            payload={"camera_id": camera_id, "settings": stored.model_dump(mode="json")},
            idempotency_key=f"detection-settings-{camera_id}-{stored.version}-{uuid.uuid4().hex}",
            ttl_s=request.app.state.settings.command_ttl_s,
        )
        command_id = command.command_id
    current = await repo.get(principal.household_id, camera_id)
    return DetectionSettingsOut(camera_id=camera_id, settings=stored,
                                applied_version=current.applied_version, customized=True,
                                command_id=command_id)
