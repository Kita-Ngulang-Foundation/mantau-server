"""Explicitly authenticated, read-only inventory for the internal dashboard."""
from __future__ import annotations

import hmac
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...store.control_repo import ControlRepo
from ..deps import get_control_repo
from .agents import _agent_status

router = APIRouter(prefix="/internal", tags=["internal"])


def internal_inventory_access(request: Request) -> None:
    expected = request.app.state.settings.internal_agents_token.get_secret_value()
    if not expected:
        raise HTTPException(503, "internal_inventory_unavailable")
    scheme, separator, supplied = request.headers.get("Authorization", "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.encode(), expected.encode()
    ):
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


@router.get("/agents", dependencies=[Depends(internal_inventory_access)])
async def fleet(request: Request, response: Response,
                control: ControlRepo = Depends(get_control_repo)) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    cutoff = request.app.state.settings.agent_offline_after_s
    agents = []
    for row in await control.enrolled_fleet():
        status = _agent_status(row, cutoff)
        agents.append({
            "agent_id": row["agent_id"], "name": status["name"],
            "household_id": row["household_id"], "household_name": row["household_name"],
            "platform": status["platform"] or "unknown",
            "enrolled_at": row["enrolled_at"], "last_contact_at": row["last_seen_at"],
            "status": "offline" if status["health_state"] == "offline" else "online",
        })
    return {"generated_at": time.time(), "offline_after_s": cutoff, "agents": agents}
