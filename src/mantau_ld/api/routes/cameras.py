"""Camera name registration -- deliberately thin. See
`store/cameras_repo.py`'s docstring for why this server holds no RTSP URL or
credentials: the agent, not this server, ever talks to the physical camera.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...store.cameras_repo import CamerasRepo
from ..deps import get_cameras_repo

router = APIRouter(prefix="/cameras", tags=["cameras"])


class CameraCreate(BaseModel):
    camera_id: str
    name: str
    agent_id: str | None = None


class CameraOut(BaseModel):
    camera_id: str
    name: str
    agent_id: str | None


@router.post("", response_model=CameraOut, status_code=201)
async def create_camera(body: CameraCreate, repo: CamerasRepo = Depends(get_cameras_repo)) -> CameraOut:
    info = await repo.create(body.camera_id, body.name, agent_id=body.agent_id)
    return CameraOut(camera_id=info.camera_id, name=info.name, agent_id=info.agent_id)


@router.get("", response_model=list[CameraOut])
async def list_cameras(repo: CamerasRepo = Depends(get_cameras_repo)) -> list[CameraOut]:
    return [CameraOut(camera_id=c.camera_id, name=c.name, agent_id=c.agent_id) for c in await repo.list_all()]


@router.get("/{camera_id}", response_model=CameraOut)
async def get_camera(camera_id: str, repo: CamerasRepo = Depends(get_cameras_repo)) -> CameraOut:
    info = await repo.get(camera_id)
    if info is None:
        raise HTTPException(404, f"camera {camera_id!r} not found")
    return CameraOut(camera_id=info.camera_id, name=info.name, agent_id=info.agent_id)


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(camera_id: str, repo: CamerasRepo = Depends(get_cameras_repo)) -> None:
    if not await repo.delete(camera_id):
        raise HTTPException(404, f"camera {camera_id!r} not found")
