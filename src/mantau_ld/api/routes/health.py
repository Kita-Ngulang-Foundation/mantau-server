from __future__ import annotations

from fastapi import APIRouter, Depends

from ...heartbeats import HeartbeatTracker
from ..deps import get_heartbeats

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(heartbeats: HeartbeatTracker = Depends(get_heartbeats)) -> dict:
    """Per-agent status from the last heartbeat received -- as close as this
    server gets to "is the camera up," since it never talks to a camera
    directly (see `store/cameras_repo.py`)."""
    return {"status": "ok", "agents": heartbeats.status()}
