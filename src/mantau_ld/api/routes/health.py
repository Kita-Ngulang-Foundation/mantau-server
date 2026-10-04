from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    """Deploy health check: 503 until required settings exist and the
    database answers. Lists missing setting names, never values."""
    problems = request.app.state.settings.configuration_problems()
    try:
        await (await request.app.state.db.conn.execute("SELECT 1")).fetchone()
    except Exception:
        problems = [*problems, "database"]
    if problems:
        return JSONResponse(status_code=503, content={"status": "unavailable", "missing": problems})
    return {"status": "ok"}
