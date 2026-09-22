"""Agent enrollment -- out of band, once per agent, before it can ever send
a signed envelope. See `../../../../protocol/PROTOCOL.md`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...store.agents_repo import AgentsRepo
from ...control_auth import current_user
from ...store.control_repo import ControlRepo
from ..deps import get_agents_repo, get_control_repo

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentEnroll(BaseModel):
    agent_id: str


class AgentEnrolled(BaseModel):
    agent_id: str
    secret: str  # shown exactly once -- there is no "show again" route
    claim_code: str | None = None


class AgentOut(BaseModel):
    agent_id: str
    enrolled_at: float
    last_seen_at: float | None


@router.post("/enroll", response_model=AgentEnrolled, status_code=201)
async def enroll(body: AgentEnroll, repo: AgentsRepo = Depends(get_agents_repo),
                 control: ControlRepo = Depends(get_control_repo)) -> AgentEnrolled:
    """Enrolling an already-enrolled id issues a fresh secret (the old one
    stops working immediately) -- equivalent to revoke + re-enroll."""
    agent = await repo.enroll(body.agent_id)
    claim_code = await control.create_claim_code(agent.agent_id)
    return AgentEnrolled(agent_id=agent.agent_id, secret=agent.secret, claim_code=claim_code)


@router.get("")
async def list_agents(request: Request, repo: AgentsRepo = Depends(get_agents_repo),
                      control: ControlRepo = Depends(get_control_repo)):
    # Compatibility window: when the new control plane is disabled this is
    # byte-for-byte the legacy inventory. Enabling it switches to owned views.
    if request.app.state.settings.control_plane_mode != "disabled":
        owner_id = current_user(request)
        return [_agent_status(row, request.app.state.settings.agent_offline_after_s)
                for row in await control.owned_agents(owner_id)]
    return [
        AgentOut(agent_id=a.agent_id, enrolled_at=a.enrolled_at, last_seen_at=a.last_seen_at)
        for a in await repo.list_all()
    ]


def _agent_status(row, offline_after_s: int) -> dict:
    import json
    import time
    last_seen = row["last_seen_at"]
    online = last_seen is not None and time.time() - last_seen <= offline_after_s
    capabilities = json.loads(row["capabilities_json"]) if row["capabilities_json"] else None
    return {
        "schema_version": 1,
        "agent_id": row["agent_id"],
        "name": row["agent_id"],
        "platform": row["platform"],
        "claim_status": "claimed",
        "setup_status": row["setup_status"] or "not_started",
        "health_state": (row["health_state"] or "online") if online else "offline",
        "requested_inference_mode": row["requested_inference_mode"],
        "effective_inference_mode": row["effective_inference_mode"],
        "capabilities": capabilities,
        "camera_connectivity": row["camera_connectivity"] or "unknown",
        "last_heartbeat_at": last_seen,
        "last_frame_at": None,
        "health_explanation": row["health_explanation"] if online else "Agent heartbeat is stale",
    }


@router.delete("/{agent_id}", status_code=204)
async def revoke(agent_id: str, repo: AgentsRepo = Depends(get_agents_repo)) -> None:
    """Every envelope this agent sends afterward fails verification (401) --
    there is no grace period."""
    if not await repo.revoke(agent_id):
        raise HTTPException(404, f"agent {agent_id!r} not found")
