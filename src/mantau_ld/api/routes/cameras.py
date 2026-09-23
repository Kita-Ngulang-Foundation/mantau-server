"""Camera name registration -- deliberately thin. See
`store/cameras_repo.py`'s docstring for why this server holds no RTSP URL or
credentials: the agent, not this server, ever talks to the physical camera.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ...control_auth import authenticated_user
from ...store.cameras_repo import CamerasRepo
from ...store.identity_repo import UserPrincipal
from ..deps import get_cameras_repo

router = APIRouter(prefix="/cameras", tags=["cameras"])


_URL_WITH_USERINFO = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s]*@", re.IGNORECASE)


class CameraCreate(BaseModel):
    camera_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    agent_id: str | None = None

    @field_validator("name")
    @classmethod
    def name_is_not_a_credential_url(cls, value: str) -> str:
        # A pasted rtsp://user:pass@host would otherwise be stored and shown
        # to every household member.
        if _URL_WITH_USERINFO.search(value):
            raise ValueError("Camera name must not contain a URL with credentials")
        return value


class CameraOut(BaseModel):
    camera_id: str
    name: str
    agent_id: str | None


@router.post("", response_model=CameraOut, status_code=201)
async def create_camera(
    body: CameraCreate,
    repo: CamerasRepo = Depends(get_cameras_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> CameraOut:
    try:
        info = await repo.create(
            body.camera_id, body.name,
            household_id=principal.household_id, agent_id=body.agent_id,
        )
    except LookupError as exc:
        raise HTTPException(404, "resource_not_found") from exc
    return CameraOut(camera_id=info.camera_id, name=info.name, agent_id=info.agent_id)


@router.get("", response_model=list[CameraOut])
async def list_cameras(
    repo: CamerasRepo = Depends(get_cameras_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> list[CameraOut]:
    cameras = await repo.list_for_household(principal.household_id)
    return [CameraOut(camera_id=c.camera_id, name=c.name, agent_id=c.agent_id) for c in cameras]


@router.get("/{camera_id}", response_model=CameraOut)
async def get_camera(
    camera_id: str,
    repo: CamerasRepo = Depends(get_cameras_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> CameraOut:
    info = await repo.get_for_household(principal.household_id, camera_id)
    if info is None:
        raise HTTPException(404, "resource_not_found")
    return CameraOut(camera_id=info.camera_id, name=info.name, agent_id=info.agent_id)


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(
    camera_id: str,
    repo: CamerasRepo = Depends(get_cameras_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> None:
    if not await repo.delete(principal.household_id, camera_id):
        raise HTTPException(404, "resource_not_found")
