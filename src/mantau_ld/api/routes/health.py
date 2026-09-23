from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ...heartbeats import HeartbeatTracker
from ..deps import get_heartbeats

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, heartbeats: HeartbeatTracker = Depends(get_heartbeats)):
    """Deploy health check: 503 until required production settings exist and
    the database answers. Lists missing setting names, never values. Per-agent
    heartbeats are shown only in local_dev (production hides tenant data)."""
    settings = request.app.state.settings
    problems = settings.configuration_problems()
    try:
        await (await request.app.state.db.conn.execute("SELECT 1")).fetchone()
    except Exception:
        problems = [*problems, "database"]
    if problems:
        return JSONResponse(status_code=503, content={"status": "unavailable", "missing": problems})
    if settings.control_plane_mode == "local_dev":
        return {"status": "ok", "agents": heartbeats.status()}
    return {"status": "ok"}
