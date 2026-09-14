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
from ...store.db import Database
from ..deps import get_agents_repo, get_db, get_dispatcher, get_heartbeats

router = APIRouter(tags=["ingest"])


class IngestResponse(BaseModel):
    status: str
    duplicate: bool = False
    out_of_order: bool = False


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    envelope: Envelope,
    agents: AgentsRepo = Depends(get_agents_repo),
    db: Database = Depends(get_db),
    dispatcher: AlertDispatcher = Depends(get_dispatcher),
    heartbeats: HeartbeatTracker = Depends(get_heartbeats),
) -> IngestResponse:
    try:
        await verify_envelope(envelope, agents)
    except VerificationError as exc:
        raise HTTPException(401, exc.reason)

    result = await record_envelope(envelope, db)
    await agents.touch(envelope.agent_id)

    if result.duplicate:
        # Already processed -- do NOT dispatch again. Still 200: the agent's
        # retry succeeded from its point of view, nothing was lost.
        return IngestResponse(status="accepted", duplicate=True, out_of_order=result.out_of_order)

    if envelope.kind is PayloadKind.FALL_EVENT:
        await dispatcher.dispatch(envelope.event())
    elif envelope.kind is PayloadKind.HEARTBEAT:
        heartbeats.record(envelope.heartbeat())

    return IngestResponse(status="accepted", duplicate=False, out_of_order=result.out_of_order)
