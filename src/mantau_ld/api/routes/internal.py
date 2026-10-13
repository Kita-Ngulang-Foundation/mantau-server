"""Explicitly authenticated, read-only inventory for the internal dashboard."""
from __future__ import annotations

import hmac
import time
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ...store.control_repo import ControlRepo
from ...store.db import Database
from ...store.notification_log_repo import NotificationLogRepo
from ..deps import get_control_repo, get_db

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
    frame_cutoff = 15
    now = time.time()
    agents = []
    for row in await control.enrolled_fleet():
        contact = row["last_seen_at"]
        frame = row["last_frame_received_at"]
        fresh_frame = frame is not None and now - frame <= frame_cutoff
        connected = contact is not None and now - contact <= cutoff
        status = "online" if fresh_frame else "connected" if connected else "offline"
        agents.append({
            "agent_id": row["agent_id"], "name": row["name"] or row["agent_id"],
            "household_id": row["household_id"], "household_name": row["household_name"],
            "platform": row["platform"] or "unknown",
            "enrolled_at": row["enrolled_at"], "last_contact_at": row["last_seen_at"],
            "last_frame_received_at": frame, "status": status,
        })
    return {"generated_at": now, "offline_after_s": cutoff,
            "frame_stale_after_s": frame_cutoff, "agents": agents}


@router.get("/notifications", dependencies=[Depends(internal_inventory_access)])
async def notifications(request: Request, response: Response,
                        days: int = Query(7, ge=1, le=30), limit: int = Query(100, ge=1, le=200),
                        cursor: str | None = Query(None, max_length=1024),
                        db: Database = Depends(get_db)) -> dict:
    response.headers['Cache-Control'] = 'private, no-store'
    now = time.time()
    try:
        data = await NotificationLogRepo(db).list(cutoff=now - days * 86400, limit=limit, cursor=cursor)
    except ValueError:
        raise HTTPException(422, 'invalid_notification_cursor') from None
    return {'generated_at': now, 'days': days,
            'retention_days': request.app.state.settings.event_retention_days, **data}
