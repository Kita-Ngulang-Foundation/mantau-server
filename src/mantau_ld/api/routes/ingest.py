"""The one endpoint the agent ever calls: `POST /ingest`. See
`../../../../protocol/PROTOCOL.md` for the full contract this implements.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mantau_core.contracts import Envelope, PayloadKind
from pydantic import BaseModel

from ...alerts.dispatcher import AlertDispatcher
from ...heartbeats import HeartbeatTracker
from ...ingest.dedupe import record_envelope
from ...ingest.verify import VerificationError, verify_envelope
from ...store.agents_repo import AgentsRepo
from ...store.cameras_repo import CamerasRepo
from ...store.db import Database
from ..deps import get_agents_repo, get_cameras_repo, get_db, get_dispatcher, get_heartbeats

router = APIRouter(tags=["ingest"])


class IngestResponse(BaseModel):
    status: str
    duplicate: bool = False
    out_of_order: bool = False


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    envelope: Envelope,
    agents: AgentsRepo = Depends(get_agents_repo),
    cameras: CamerasRepo = Depends(get_cameras_repo),
    db: Database = Depends(get_db),
    dispatcher: AlertDispatcher = Depends(get_dispatcher),
    heartbeats: HeartbeatTracker = Depends(get_heartbeats),
) -> IngestResponse:
    try:
        await verify_envelope(envelope, agents)
    except VerificationError as exc:
        raise HTTPException(401, "unauthorized") from exc

    agent = await agents.get(envelope.agent_id)
    if agent is None or agent.household_id is None or agent.revoked_at is not None:
        raise HTTPException(401, "unauthorized")
    event = envelope.event() if envelope.kind is PayloadKind.FALL_EVENT else None
    heartbeat = envelope.heartbeat() if envelope.kind is PayloadKind.HEARTBEAT else None
    if heartbeat is not None and heartbeat.agent_id != envelope.agent_id:
        raise HTTPException(401, "unauthorized")
    camera_id = event.camera_id if event is not None else heartbeat.camera_id if heartbeat else None
    if camera_id is not None and await cameras.get_for_agent(envelope.agent_id, camera_id) is None:
        raise HTTPException(404, "resource_not_found")

    result = await record_envelope(envelope, db)
    await agents.touch(envelope.agent_id)

    if result.duplicate:
        # Already processed -- do NOT dispatch again. Still 200: the agent's
        # retry succeeded from its point of view, nothing was lost.
        return IngestResponse(status="accepted", duplicate=True, out_of_order=result.out_of_order)

    if envelope.kind is PayloadKind.FALL_EVENT:
        await dispatcher.dispatch(event, household_id=agent.household_id, agent_id=agent.agent_id)
    elif envelope.kind is PayloadKind.HEARTBEAT:
        heartbeats.record(heartbeat)

    return IngestResponse(status="accepted", duplicate=False, out_of_order=result.out_of_order)
