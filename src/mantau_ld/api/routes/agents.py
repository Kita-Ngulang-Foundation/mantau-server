"""Agent enrollment and the household's agents.

A household owner or admin creates a single-use enrollment key in the app
(`POST /enrollment-keys`) and enters it on the new agent, which presents it
once to `POST /agents/enroll`. The agent is created inside that household and
receives its own credential; nobody ever types or copies that credential.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from mantau_core.contracts import AgentPlatform
from pydantic import BaseModel, Field

from ...control_auth import authenticated_user
from ...store.agents_repo import AgentIdTaken, AgentsRepo, EnrollmentKeyInvalid
from ...store.control_repo import ControlRepo
from ...store.identity_repo import UserPrincipal
from ..deps import get_agents_repo, get_control_repo

router = APIRouter(tags=["agents"])

_MANAGERS = ("owner", "admin")


class EnrollmentKeyOut(BaseModel):
    key_id: str
    enrollment_key: str  # shown exactly once
    expires_at: datetime


class EnrollmentKeyStatus(BaseModel):
    key_id: str
    status: str  # pending | used | expired | revoked
    expires_at: datetime
    agent_id: str | None


class AgentEnroll(BaseModel):
    enrollment_key: str = Field(min_length=10, max_length=64)
    agent_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    name: str | None = Field(default=None, min_length=1, max_length=60)
    platform: AgentPlatform = AgentPlatform.OTHER


class AgentEnrolled(BaseModel):
    agent_id: str
    secret: str  # shown exactly once -- there is no "show again" route


class AgentRename(BaseModel):
    name: str = Field(min_length=1, max_length=60)


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _require_manager(principal: UserPrincipal) -> None:
    if principal.role not in _MANAGERS:
        raise HTTPException(403, "forbidden")


@router.post("/enrollment-keys", response_model=EnrollmentKeyOut, status_code=201)
async def create_enrollment_key(request: Request, repo: AgentsRepo = Depends(get_agents_repo),
                                principal: UserPrincipal = Depends(authenticated_user)):
    """A single-use key that adds one agent to the caller's household."""
    _require_manager(principal)
    key, secret_key = await repo.create_enrollment_key(
        principal.household_id, principal.user_id,
        ttl_s=request.app.state.settings.enrollment_key_ttl_s,
    )
    return EnrollmentKeyOut(key_id=key.key_id, enrollment_key=secret_key,
                            expires_at=_utc(key.expires_at))


@router.get("/enrollment-keys/{key_id}", response_model=EnrollmentKeyStatus)
async def enrollment_key_status(key_id: str, repo: AgentsRepo = Depends(get_agents_repo),
                                principal: UserPrincipal = Depends(authenticated_user)):
    """Polled by the app until the agent has used the key."""
    key = await repo.get_enrollment_key(principal.household_id, key_id)
    if key is None:
        raise HTTPException(404, "resource_not_found")
    return EnrollmentKeyStatus(key_id=key.key_id, status=key.status(),
                               expires_at=_utc(key.expires_at), agent_id=key.agent_id)


@router.delete("/enrollment-keys/{key_id}", status_code=204)
async def revoke_enrollment_key(key_id: str, repo: AgentsRepo = Depends(get_agents_repo),
                                principal: UserPrincipal = Depends(authenticated_user)) -> None:
    _require_manager(principal)
    await repo.revoke_enrollment_key(principal.household_id, key_id)


@router.post("/agents/enroll", response_model=AgentEnrolled, status_code=201)
async def enroll(body: AgentEnroll, repo: AgentsRepo = Depends(get_agents_repo)) -> AgentEnrolled:
    """Consumes the enrollment key. An unknown, expired, revoked, or used key
    answers 401 without saying which; an `agent_id` already in use answers
    409 and leaves the key unused, so the agent can retry with another id."""
    try:
        agent = await repo.enroll(
            body.enrollment_key, body.agent_id,
            name=(body.name or body.agent_id).strip(), platform=body.platform.value,
        )
    except EnrollmentKeyInvalid as exc:
        raise HTTPException(401, "invalid_enrollment_key") from exc
    except AgentIdTaken as exc:
        raise HTTPException(409, "agent_id_taken") from exc
    return AgentEnrolled(agent_id=agent.agent_id, secret=agent.secret)


@router.get("/agents")
async def list_agents(request: Request, control: ControlRepo = Depends(get_control_repo),
                      principal: UserPrincipal = Depends(authenticated_user)):
    return [_agent_status(row, request.app.state.settings.agent_offline_after_s)
            for row in await control.owned_agents(principal.household_id)]


@router.patch("/agents/{agent_id}")
async def rename_agent(agent_id: str, body: AgentRename, request: Request,
                       repo: AgentsRepo = Depends(get_agents_repo),
                       control: ControlRepo = Depends(get_control_repo),
                       principal: UserPrincipal = Depends(authenticated_user)):
    _require_manager(principal)
    if not await repo.rename(agent_id, principal.household_id, body.name.strip()):
        raise HTTPException(404, "resource_not_found")
    row = await control.get_state(principal.household_id, agent_id)
    return _agent_status(row, request.app.state.settings.agent_offline_after_s)


def _agent_status(row, offline_after_s: int) -> dict:
    last_seen = row["last_seen_at"]
    online = last_seen is not None and time.time() - last_seen <= offline_after_s
    capabilities = json.loads(row["capabilities_json"]) if row["capabilities_json"] else None
    frame_at = row["last_frame_at"]
    inference_at = row["last_inference_at"]
    protected = (online and frame_at is not None and inference_at is not None
                 and time.time()-min(frame_at,inference_at) <= 15)
    return {
        "schema_version": 1,
        "agent_id": row["agent_id"],
        "name": row["name"] or row["agent_id"],
        "platform": row["platform"],
        "claim_status": "claimed",
        "setup_status": "active" if protected else ("waiting_for_agent" if row["setup_status"] == "active" else row["setup_status"] or "not_started"),
        "health_state": "offline" if not online else "online" if protected else "degraded",
        "requested_inference_mode": row["requested_inference_mode"],
        "effective_inference_mode": row["effective_inference_mode"],
        "capabilities": capabilities,
        "camera_connectivity": row["camera_connectivity"] or "unknown",
        "last_heartbeat_at": last_seen,
        "last_frame_at": frame_at,
        "last_inference_at": inference_at,
        "last_server_contact_at": last_seen,
        "health_explanation": ("Agent heartbeat is stale" if not online else None if protected
                               else "Waiting for fresh successfully processed camera frames"),
    }


@router.delete("/agents/{agent_id}", status_code=204)
async def revoke(agent_id: str, repo: AgentsRepo = Depends(get_agents_repo),
                 principal: UserPrincipal = Depends(authenticated_user)) -> None:
    """Every request this agent makes afterward fails authentication (401) --
    there is no grace period."""
    _require_manager(principal)
    if not await repo.revoke(agent_id, principal.household_id):
        raise HTTPException(404, "resource_not_found")
