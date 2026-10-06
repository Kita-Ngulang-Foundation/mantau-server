"""Server inference: `POST /agents/{agent_id}/inference` and its capability.

Agents that cannot run the fall detector themselves (no usable model, too
slow, or the family chose CLOUD/HYBRID) upload sampled JPEG frames here. The
wire format, signature and semantics are `mantau_core.contracts.inference`.

Falls the server detects go through the same `AlertDispatcher` as events an
agent ingests (stored under that agent, pushed to the household) and are
returned so the agent can attach a clip. HYBRID confirmation results are
stored against the agent's own events and returned. The frame itself is
never stored: it is decoded in memory and dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from mantau_core.contracts import InferenceCapability, InferenceResult
from mantau_core.contracts import inference as contract

from ...alerts.dispatcher import AlertDispatcher
from ...inference.service import (
    CapacityExceeded, InferenceService, InferenceUnavailable, RateLimited, UndecodableFrame,
)
from ...store.agents_repo import AgentsRepo
from ...store.cameras_repo import CamerasRepo
from ...store.events_repo import EventsRepo
from ...store.inference_repo import InferenceRepo
from ..deps import get_agents_repo, get_cameras_repo, get_dispatcher, get_events_repo
from ..limits import read_limited

router = APIRouter(tags=["inference"])

_ID_MAX = 128


def get_inference(request: Request) -> InferenceService:
    return request.app.state.inference


def get_inference_repo(request: Request) -> InferenceRepo:
    return request.app.state.inference_repo


@router.get("/inference/capability", response_model=InferenceCapability)
async def capability(service: InferenceService = Depends(get_inference)) -> InferenceCapability:
    """Whether this server runs the detector. No tenant data; agents read it
    at startup to decide whether CLOUD/HYBRID can exist at all."""
    return service.capability()


def _id(value: str, name: str) -> str:
    if not value or len(value) > _ID_MAX or not all(c.isalnum() or c in "-_." for c in value):
        raise HTTPException(400, f"invalid_{name}")
    return value


def _int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(400, f"invalid_{name}") from exc


@router.post("/agents/{agent_id}/inference", response_model=InferenceResult)
async def infer(
    agent_id: str,
    request: Request,
    x_mantau_agent: str = Header(...),
    x_mantau_camera: str = Header(...),
    x_mantau_session: str = Header(...),
    x_mantau_frame: str = Header(...),
    x_mantau_frame_ts: str = Header(...),
    x_mantau_captured_at: str = Header(...),
    x_mantau_signature: str = Header(...),
    x_mantau_event_ids: str = Header(""),
    agents: AgentsRepo = Depends(get_agents_repo),
    cameras: CamerasRepo = Depends(get_cameras_repo),
    events: EventsRepo = Depends(get_events_repo),
    dispatcher: AlertDispatcher = Depends(get_dispatcher),
    service: InferenceService = Depends(get_inference),
    results: InferenceRepo = Depends(get_inference_repo),
) -> InferenceResult:
    settings = request.app.state.settings
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type != "image/jpeg":
        raise HTTPException(415, "unsupported_media_type")
    body = await read_limited(request, settings.inference_max_frame_bytes)

    camera_id = _id(x_mantau_camera, "camera")
    session_id = _id(x_mantau_session, "session")
    frame_id = _id(x_mantau_frame, "frame")
    ts_ms = _int(x_mantau_frame_ts, "frame_ts")
    captured_at_ms = _int(x_mantau_captured_at, "captured_at")
    event_ids = tuple(e for e in x_mantau_event_ids.split(",") if e) if x_mantau_event_ids else ()
    if len(event_ids) > contract.MAX_EVENT_IDS:
        raise HTTPException(400, "too_many_event_ids")
    for event_id in event_ids:
        _id(event_id, "event_id")

    # Authentication: path and header name the same enrolled, claimed agent,
    # and the signature covers every header above plus the frame.
    agent = await agents.get(x_mantau_agent)
    if (agent_id != x_mantau_agent or agent is None or agent.revoked_at is not None
            or agent.household_id is None
            or not contract.verify(agent.secret, x_mantau_signature, agent_id=agent_id,
                                   camera_id=camera_id, session_id=session_id,
                                   frame_id=frame_id, ts_ms=ts_ms,
                                   captured_at_ms=captured_at_ms, event_ids=event_ids,
                                   body=body)):
        raise HTTPException(401, "unauthorized")
    if await cameras.get_for_agent(agent_id, camera_id) is None:
        raise HTTPException(404, "resource_not_found")
    await agents.touch(agent_id)
    async def failed_processing():
        async with request.app.state.db.serialized():
            await request.app.state.db.conn.execute(
                "UPDATE agents SET last_inference_at=NULL WHERE agent_id=?", (agent_id,))
            await request.app.state.db.conn.commit()
    if not service.available:
        await failed_processing()
        raise HTTPException(503, "inference_unavailable")

    request_hash = hashlib.sha256(x_mantau_signature.encode()).hexdigest()
    try:
        durable_answer = await results.answered(agent_id, frame_id, request_hash)
    except ValueError as exc:
        raise HTTPException(409, 'frame_id_conflict') from exc
    if durable_answer is not None:
        return durable_answer
    cached = service.answered(agent_id, frame_id)
    if cached is not None:
        return cached

    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    pending_computation = service.pending_computation(agent_id, frame_id)
    if pending_computation is not None and pending_computation[0] != request_hash:
        raise HTTPException(409, 'frame_id_conflict')
    if pending_computation is None and now_ms - captured_at_ms > settings.inference_max_frame_age_s * 1000:
        raise HTTPException(422, "stale_frame")
    if captured_at_ms - now_ms > settings.inference_max_clock_skew_s * 1000:
        raise HTTPException(422, "clock_skew")
    captured_at = datetime.fromtimestamp(captured_at_ms / 1000, timezone.utc)

    household_id = agent.household_id
    camera_settings = (await request.app.state.detection_settings_repo.get(
        household_id, camera_id)).settings

    async def work() -> InferenceResult:
        # Only this agent's own events can be confirmed; unknown ids are
        # allowed (the event's /ingest may still be in flight) because a
        # confirmation is only ever shown on an event of the same agent.
        owners = {event_id: await events.agent_for(event_id) for event_id in event_ids}
        foreign = {e for e, owner in owners.items() if owner is not None and owner != agent_id}
        try:
            result = service.computation(agent_id, frame_id, request_hash)
            if result is None:
                try:
                    result = await service.infer(
                        agent_id=agent_id, camera_id=camera_id, session_id=session_id,
                        frame_id=frame_id, ts_ms=ts_ms, captured_at=captured_at,
                        event_ids=tuple(e for e in event_ids if e not in foreign), jpeg=body,
                        settings=camera_settings)
                    if foreign:
                        result = result.model_copy(update={'confirmations': [
                            *result.confirmations,
                            *(contract.InferenceConfirmation(event_id=e, confirmed=False, confidence=0.0,
                              reason='unknown_event') for e in sorted(foreign))]})
                    service.retain_computation(agent_id, frame_id, request_hash, result)
                except BaseException:
                    service.complete_computation(agent_id, frame_id)
                    raise
        except ValueError as exc:
            raise HTTPException(409, 'frame_id_conflict') from exc
        except InferenceUnavailable as exc:
            await failed_processing()
            raise HTTPException(503, "inference_unavailable") from exc
        except CapacityExceeded as exc:
            await failed_processing()
            raise HTTPException(503, "inference_capacity") from exc
        except RateLimited as exc:
            raise HTTPException(429, "rate_limited") from exc
        except UndecodableFrame as exc:
            await failed_processing()
            raise HTTPException(422, "undecodable_frame") from exc
        async with request.app.state.db.transaction():
            if await cameras.get_for_agent(agent_id, camera_id) is None:
                raise HTTPException(404, "resource_not_found")
            if result.processed:
                await request.app.state.db.conn.execute(
                    "UPDATE agents SET last_frame_at=?,last_inference_at=? WHERE agent_id=?",
                    (captured_at.timestamp(), datetime.now(timezone.utc).timestamp(), agent_id))
            else:
                await failed_processing()
            for event in result.events:
                await dispatcher.dispatch(event, household_id=household_id, agent_id=agent_id, deliver=False)
            for confirmation in result.confirmations:
                await results.save_confirmation(confirmation, frame_id=frame_id,
                                                agent_id=agent_id, household_id=household_id)
            await results.save_answer(agent_id, camera_id, household_id, request_hash, result)
        service.complete_computation(agent_id, frame_id)
        await dispatcher.outbox.deliver_pending()
        return result

    return await service.once(agent_id, frame_id, work)
