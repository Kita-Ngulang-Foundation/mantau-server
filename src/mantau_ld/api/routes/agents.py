"""Agent enrollment -- out of band, once per agent, before it can ever send
a signed envelope. See `../../../../protocol/PROTOCOL.md`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...store.agents_repo import AgentIdentityProofRequired, AgentsRepo
from ...control_auth import authenticated_user
from ...store.control_repo import ControlRepo
from ...store.identity_repo import UserPrincipal
from ..deps import get_agents_repo, get_control_repo

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentEnroll(BaseModel):
    agent_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AgentEnrolled(BaseModel):
    agent_id: str
    secret: str  # shown exactly once -- there is no "show again" route
    claim_code: str | None = None


class AgentOut(BaseModel):
    agent_id: str
    enrolled_at: float
    last_seen_at: float | None


@router.post("/enroll", response_model=AgentEnrolled, status_code=201)
async def enroll(body: AgentEnroll, request: Request,
                 repo: AgentsRepo = Depends(get_agents_repo),
                 control: ControlRepo = Depends(get_control_repo)) -> AgentEnrolled:
    """Initial enrollment is create-only; an existing identity is rotated only
    with proof of its current secret (X-Mantau-Agent-ID/-Secret headers).

    Without proof, an existing id answers 409 so a new device picks another
    name; with wrong proof it answers 401. Neither changes the stored agent."""
    proof_id = request.headers.get("X-Mantau-Agent-ID", "")
    current_secret = request.headers.get("X-Mantau-Agent-Secret", "")
    if proof_id != body.agent_id:
        current_secret = ""
    try:
        agent = await repo.enroll(body.agent_id, current_secret=current_secret or None)
    except AgentIdentityProofRequired as exc:
        if not current_secret:
            raise HTTPException(409, "agent_id_taken") from exc
        raise HTTPException(401, "unauthorized") from exc
    claim_code = None
    if agent.household_id is None:
        claim_code = await control.create_claim_code(
            agent.agent_id, agent.enrollment_id,
            ttl_s=request.app.state.settings.claim_code_ttl_s,
        )
    return AgentEnrolled(agent_id=agent.agent_id, secret=agent.secret, claim_code=claim_code)


@router.get("")
async def list_agents(request: Request, repo: AgentsRepo = Depends(get_agents_repo),
                      control: ControlRepo = Depends(get_control_repo),
                      principal: UserPrincipal = Depends(authenticated_user)):
    return [_agent_status(row, request.app.state.settings.agent_offline_after_s)
            for row in await control.owned_agents(principal.household_id)]


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
async def revoke(agent_id: str, repo: AgentsRepo = Depends(get_agents_repo),
                 principal: UserPrincipal = Depends(authenticated_user)) -> None:
    """Every envelope this agent sends afterward fails verification (401) --
    there is no grace period."""
    if principal.role not in ("owner", "admin"):
        raise HTTPException(403, "forbidden")
    if not await repo.revoke(agent_id, principal.household_id):
        raise HTTPException(404, "resource_not_found")
