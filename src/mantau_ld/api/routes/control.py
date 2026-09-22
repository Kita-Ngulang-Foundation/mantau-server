from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from mantau_core.contracts import (
    AgentClaimStatus, CameraRequestMetadata, ClaimStatus, CommandReceipt,
    CommandResult, CommandState, CommandType, ControlCommand, InferenceMode,
)
from pydantic import BaseModel, Field

from ...control_auth import authenticated_agent, current_user
from ...control_crypto import CredentialCipher
from ...store.cameras_repo import CamerasRepo
from ...store.control_repo import ControlRepo, IdempotencyConflict
from ..deps import get_cameras_repo, get_control_repo
from .agents import _agent_status

router = APIRouter(tags=["control-plane"])


class ClaimRequest(BaseModel):
    claim_code: str
    platform: str


class CameraCredentialsIn(BaseModel):
    username: str
    password: str = Field(repr=False)


class CameraCommandRequest(BaseModel):
    camera: CameraRequestMetadata
    credentials: CameraCredentialsIn


class InferenceModeRequest(BaseModel):
    mode: InferenceMode


class AgentPollRequest(BaseModel):
    status: dict = Field(default_factory=dict)


def _owner(request: Request) -> str:
    return current_user(request)


async def _require_owned(repo: ControlRepo, owner_id: str, agent_id: str) -> None:
    if not await repo.owns(owner_id, agent_id):
        raise HTTPException(404, "agent_not_found")


def _key(value: str | None) -> str:
    if not value:
        raise HTTPException(400, "idempotency_key_required")
    return value


def _cipher(request: Request) -> CredentialCipher:
    try:
        return CredentialCipher(request.app.state.settings.control_plane_encryption_key)
    except RuntimeError as exc:
        raise HTTPException(503, "credential_encryption_not_configured") from exc


async def _queue(request: Request, repo: ControlRepo, owner_id: str, agent_id: str,
                 command_type: CommandType, payload: dict, idempotency_key: str,
                 encrypted_payload: bytes | None = None) -> CommandReceipt:
    await _require_owned(repo, owner_id, agent_id)
    try:
        command = await repo.queue(
            agent_id=agent_id, owner_id=owner_id, command_type=command_type,
            payload=payload, encrypted_payload=encrypted_payload,
            idempotency_key=idempotency_key, ttl_s=request.app.state.settings.command_ttl_s,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(409, "idempotency_key_conflict") from exc
    return CommandReceipt(command_id=command.command_id, accepted=True,
                          state=CommandState(command.state))


@router.post("/agent-claims")
async def claim_agent(body: ClaimRequest, request: Request,
                      repo: ControlRepo = Depends(get_control_repo)):
    owner_id = _owner(request)
    platform = "linux_x86_64" if body.platform == "linux" else body.platform
    agent_id = await repo.claim(body.claim_code.strip().upper(), owner_id, platform)
    if agent_id is None:
        raise HTTPException(404, "invalid_or_expired_claim_code")
    row = await repo.get_state(owner_id, agent_id)
    return _agent_status(row, request.app.state.settings.agent_offline_after_s)


@router.get("/agents/{agent_id}/setup")
async def setup_status(agent_id: str, request: Request,
                       repo: ControlRepo = Depends(get_control_repo)):
    owner_id = _owner(request)
    row = await repo.get_state(owner_id, agent_id)
    if row is None:
        raise HTTPException(404, "agent_not_found")
    status = row["setup_status"] or "not_started"
    messages = {
        "not_started": "Agent is claimed and waiting for setup.",
        "waiting_for_agent": "Waiting for the agent to connect.",
        "discovering": "Discovering cameras on the agent LAN.",
        "configuring_camera": "Applying camera configuration.",
        "selecting_mode": "Applying inference mode.",
        "active": "Agent setup is active.",
        "failed": "Agent setup failed.",
    }
    return {"setup_status": status, "message": messages.get(status, status)}


@router.post("/agents/{agent_id}/commands/discover", response_model=CommandReceipt)
async def discover(agent_id: str, request: Request,
                   idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                   repo: ControlRepo = Depends(get_control_repo)):
    return await _queue(request, repo, _owner(request), agent_id, CommandType.DISCOVER,
                        {}, _key(idempotency_key))


@router.get("/agents/{agent_id}/discovery")
async def discovery(agent_id: str, request: Request,
                    repo: ControlRepo = Depends(get_control_repo)):
    owner_id = _owner(request)
    await _require_owned(repo, owner_id, agent_id)
    return await repo.discovery(agent_id)


@router.post("/agents/{agent_id}/camera-tests")
async def camera_test(agent_id: str, body: CameraCommandRequest, request: Request,
                      idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                      repo: ControlRepo = Depends(get_control_repo)):
    cipher = _cipher(request)
    metadata = body.camera.model_dump(mode="json")
    metadata["username_present"] = bool(body.credentials.username)
    encrypted = cipher.encrypt(body.credentials.model_dump())
    receipt = await _queue(request, repo, _owner(request), agent_id, CommandType.CAMERA_TEST,
                           metadata, _key(idempotency_key), encrypted)
    return {"success": True, "message": "Camera test queued.", **receipt.model_dump(mode="json")}


@router.put("/agents/{agent_id}/camera")
async def configure_camera(agent_id: str, body: CameraCommandRequest, request: Request,
                           idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                           repo: ControlRepo = Depends(get_control_repo),
                           cameras: CamerasRepo = Depends(get_cameras_repo)):
    cipher = _cipher(request)
    metadata = body.camera.model_dump(mode="json")
    metadata["username_present"] = bool(body.credentials.username)
    encrypted = cipher.encrypt(body.credentials.model_dump())
    await _queue(request, repo, _owner(request), agent_id, CommandType.CONFIGURE_CAMERA,
                 metadata, _key(idempotency_key), encrypted)
    camera = await cameras.create(body.camera.camera_id, body.camera.name, agent_id=agent_id)
    return {"camera_id": camera.camera_id, "name": camera.name, "agent_id": camera.agent_id}


@router.put("/agents/{agent_id}/inference-mode")
async def set_inference_mode(agent_id: str, body: InferenceModeRequest, request: Request,
                             idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                             repo: ControlRepo = Depends(get_control_repo)):
    owner_id = _owner(request)
    await _queue(request, repo, owner_id, agent_id, CommandType.SET_INFERENCE_MODE,
                 {"mode": body.mode.value}, _key(idempotency_key))
    await repo.set_requested_mode(agent_id, body.mode.value)
    row = await repo.get_state(owner_id, agent_id)
    return _agent_status(row, request.app.state.settings.agent_offline_after_s)


@router.post("/agents/{agent_id}/commands/restart", response_model=CommandReceipt)
async def restart(agent_id: str, request: Request,
                  idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                  repo: ControlRepo = Depends(get_control_repo)):
    return await _queue(request, repo, _owner(request), agent_id, CommandType.RESTART,
                        {}, _key(idempotency_key))


@router.post("/agents/{agent_id}/commands/reconfigure", response_model=CommandReceipt)
async def reconfigure(agent_id: str, request: Request,
                      idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                      repo: ControlRepo = Depends(get_control_repo)):
    return await _queue(request, repo, _owner(request), agent_id, CommandType.RECONFIGURE,
                        {}, _key(idempotency_key))


@router.post("/agent-control/commands/poll", response_model=ControlCommand | None)
async def poll_commands(body: AgentPollRequest, request: Request, response: Response,
                        agent=Depends(authenticated_agent),
                        repo: ControlRepo = Depends(get_control_repo)):
    if request.app.state.settings.control_plane_mode == "disabled":
        raise HTTPException(404, "control_plane_disabled")
    if body.status:
        # The validated status model has no credential-bearing fields; reject
        # obvious mistakes before persistence as a second redaction boundary.
        lowered = json.dumps(body.status).lower()
        if any(token in lowered for token in ("password", "agent_secret", "private_key", "rtsp://")):
            raise HTTPException(400, "secret_in_status")
        await repo.update_agent_report(agent.agent_id, body.status)
    await request.app.state.agents_repo.touch(agent.agent_id)
    cipher = None
    if request.app.state.settings.control_plane_encryption_key:
        cipher = CredentialCipher(request.app.state.settings.control_plane_encryption_key)
    command = await repo.poll(
        agent.agent_id, cipher,
        delivery_lease_s=request.app.state.settings.command_delivery_lease_s,
    )
    if command is None:
        response.status_code = 204
        return None
    return ControlCommand(
        command_id=command.command_id, command_type=command.command_type,
        state=command.state, payload=command.payload,
        created_at=datetime.fromtimestamp(command.created_at, timezone.utc),
        expires_at=datetime.fromtimestamp(command.expires_at, timezone.utc),
    )


@router.post("/agent-control/commands/{command_id}/results", status_code=204)
async def submit_result(command_id: str, result: CommandResult,
                        agent=Depends(authenticated_agent),
                        repo: ControlRepo = Depends(get_control_repo)):
    if command_id != result.command_id:
        raise HTTPException(400, "command_id_mismatch")
    if result.state not in (CommandState.RUNNING, CommandState.SUCCEEDED, CommandState.FAILED):
        raise HTTPException(400, "invalid_agent_command_state")
    if result.state is CommandState.FAILED and result.failure_reason is None:
        raise HTTPException(400, "failure_reason_required")
    lowered = json.dumps(result.model_dump(mode="json")).lower()
    if any(token in lowered for token in
           ("password", "agent_secret", "private_key", "credentials", "rtsp://")):
        raise HTTPException(400, "secret_in_command_result")
    if not await repo.record_result(agent.agent_id, result):
        raise HTTPException(404, "command_not_found")
