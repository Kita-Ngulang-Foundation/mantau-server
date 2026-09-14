"""Agent enrollment -- out of band, once per agent, before it can ever send
a signed envelope. See `../../../../protocol/PROTOCOL.md`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...store.agents_repo import AgentsRepo
from ..deps import get_agents_repo

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentEnroll(BaseModel):
    agent_id: str


class AgentEnrolled(BaseModel):
    agent_id: str
    secret: str  # shown exactly once -- there is no "show again" route


class AgentOut(BaseModel):
    agent_id: str
    enrolled_at: float
    last_seen_at: float | None


@router.post("/enroll", response_model=AgentEnrolled, status_code=201)
async def enroll(body: AgentEnroll, repo: AgentsRepo = Depends(get_agents_repo)) -> AgentEnrolled:
    """Enrolling an already-enrolled id issues a fresh secret (the old one
    stops working immediately) -- equivalent to revoke + re-enroll."""
    agent = await repo.enroll(body.agent_id)
    return AgentEnrolled(agent_id=agent.agent_id, secret=agent.secret)


@router.get("", response_model=list[AgentOut])
async def list_agents(repo: AgentsRepo = Depends(get_agents_repo)) -> list[AgentOut]:
    return [
        AgentOut(agent_id=a.agent_id, enrolled_at=a.enrolled_at, last_seen_at=a.last_seen_at)
        for a in await repo.list_all()
    ]


@router.delete("/{agent_id}", status_code=204)
async def revoke(agent_id: str, repo: AgentsRepo = Depends(get_agents_repo)) -> None:
    """Every envelope this agent sends afterward fails verification (401) --
    there is no grace period."""
    if not await repo.revoke(agent_id):
        raise HTTPException(404, f"agent {agent_id!r} not found")
