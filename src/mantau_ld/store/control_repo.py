from __future__ import annotations

import json
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

from mantau_core.contracts import CommandResult, CommandState, CommandType

from ..control_crypto import CredentialCipher
from .db import Database


@dataclass
class StoredCommand:
    command_id: str
    agent_id: str
    command_type: str
    state: str
    payload: dict
    encrypted_payload: bytes | None
    created_at: float
    expires_at: float


class IdempotencyConflict(ValueError):
    pass


class ClaimRateLimited(PermissionError):
    pass


class ControlRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_claim_code(
        self, agent_id: str, enrollment_id: str, *, ttl_s: int = 600
    ) -> str:
        code = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16].upper()
        code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        now = time.time()
        await self._db.conn.execute(
            "UPDATE enrollment_claims SET expires_at=? WHERE agent_id=? AND consumed_at IS NULL",
            (now, agent_id),
        )
        await self._db.conn.execute(
            "INSERT INTO enrollment_claims(code_hash,agent_id,enrollment_id,created_at,expires_at) "
            "VALUES(?,?,?,?,?)", (code_hash, agent_id, enrollment_id, now, now + ttl_s),
        )
        await self._db.conn.commit()
        return code

    async def claim(
        self,
        code: str,
        *,
        user_id: str,
        household_id: str,
        platform: str,
        attempt_limit: int,
        attempt_window_s: int,
    ) -> str | None:
        now = time.time()
        code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        await self._db.conn.execute("BEGIN IMMEDIATE")
        try:
            rate = await (await self._db.conn.execute(
                "SELECT window_started_at,attempts FROM claim_rate_limits WHERE user_id=?",
                (user_id,),
            )).fetchone()
            if rate is None or now - rate["window_started_at"] >= attempt_window_s:
                await self._db.conn.execute(
                    "INSERT INTO claim_rate_limits(user_id,window_started_at,attempts) VALUES(?,?,1) "
                    "ON CONFLICT(user_id) DO UPDATE SET window_started_at=excluded.window_started_at,attempts=1",
                    (user_id, now),
                )
            elif rate["attempts"] >= attempt_limit:
                await self._db.conn.rollback()
                raise ClaimRateLimited("claim attempt limit reached")
            else:
                await self._db.conn.execute(
                    "UPDATE claim_rate_limits SET attempts=attempts+1 WHERE user_id=?", (user_id,)
                )
            cursor = await self._db.conn.execute(
                "SELECT c.agent_id FROM enrollment_claims c JOIN agents a ON a.agent_id=c.agent_id "
                "WHERE c.code_hash=? AND c.consumed_at IS NULL AND c.expires_at>? "
                "AND c.enrollment_id=a.enrollment_id AND a.household_id IS NULL AND a.revoked_at IS NULL",
                (code_hash, now),
            )
            row = await cursor.fetchone()
            if row is None:
                await self._db.conn.commit()
                return None
            agent_id = row["agent_id"]
            updated = await self._db.conn.execute(
                "UPDATE agents SET household_id=? WHERE agent_id=? AND household_id IS NULL",
                (household_id, agent_id),
            )
            if updated.rowcount != 1:
                await self._db.conn.commit()
                return None
            await self._db.conn.execute(
                "INSERT INTO agent_ownership(agent_id,owner_id,claimed_at) VALUES(?,?,?) "
                "ON CONFLICT(agent_id) DO NOTHING", (agent_id, user_id, now),
            )
            await self._db.conn.execute(
                "UPDATE enrollment_claims SET consumed_at=? WHERE code_hash=? AND consumed_at IS NULL",
                (now, code_hash),
            )
            await self._db.conn.execute(
                "INSERT INTO agent_control_state(agent_id,platform,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET platform=excluded.platform,updated_at=excluded.updated_at",
                (agent_id, platform, now),
            )
            await self._db.conn.execute("DELETE FROM claim_rate_limits WHERE user_id=?", (user_id,))
            await self._db.conn.commit()
            return agent_id
        except BaseException:
            await self._db.conn.rollback()
            raise

    async def owns(self, household_id: str, agent_id: str) -> bool:
        row = await (await self._db.conn.execute(
            "SELECT 1 FROM agents WHERE household_id=? AND agent_id=? AND revoked_at IS NULL",
            (household_id, agent_id),
        )).fetchone()
        return row is not None

    async def owned_agents(self, household_id: str):
        cursor = await self._db.conn.execute(
            "SELECT a.agent_id,a.enrolled_at,a.last_seen_at,s.* FROM agents a "
            "LEFT JOIN agent_control_state s ON s.agent_id=a.agent_id "
            "WHERE a.household_id=? AND a.revoked_at IS NULL ORDER BY a.enrolled_at", (household_id,)
        )
        return await cursor.fetchall()

    async def get_state(self, household_id: str, agent_id: str):
        cursor = await self._db.conn.execute(
            "SELECT a.agent_id,a.last_seen_at,s.* FROM agents a "
            "LEFT JOIN agent_control_state s ON s.agent_id=a.agent_id "
            "WHERE a.household_id=? AND a.agent_id=? AND a.revoked_at IS NULL",
            (household_id, agent_id),
        )
        return await cursor.fetchone()

    async def update_agent_report(self, agent_id: str, report: dict) -> None:
        now = time.time()
        capabilities = report.get("capabilities")
        await self._db.conn.execute(
            "INSERT INTO agent_control_state(agent_id,platform,capabilities_json,setup_status,"
            "health_state,requested_inference_mode,effective_inference_mode,camera_connectivity,"
            "health_explanation,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET platform=excluded.platform,"
            "capabilities_json=excluded.capabilities_json,setup_status=CASE "
            "WHEN agent_control_state.setup_status IN ('discovering','configuring_camera','selecting_mode','failed') "
            "THEN agent_control_state.setup_status ELSE excluded.setup_status END,"
            "health_state=excluded.health_state,requested_inference_mode=excluded.requested_inference_mode,"
            "effective_inference_mode=excluded.effective_inference_mode,"
            "camera_connectivity=excluded.camera_connectivity,health_explanation=excluded.health_explanation,"
            "updated_at=excluded.updated_at",
            (agent_id, report.get("platform"), json.dumps(capabilities) if capabilities else None,
             report.get("setup_status", "active"), report.get("health_state", "online"),
             report.get("requested_inference_mode"), report.get("effective_inference_mode"),
             report.get("camera_connectivity", "unknown"), report.get("health_explanation"), now),
        )
        await self._db.conn.commit()

    async def set_requested_mode(self, agent_id: str, mode: str) -> None:
        await self._db.conn.execute(
            "UPDATE agent_control_state SET requested_inference_mode=?,setup_status='selecting_mode',updated_at=? "
            "WHERE agent_id=?", (mode, time.time(), agent_id),
        )
        await self._db.conn.commit()

    async def queue(self, *, agent_id: str, household_id: str, requested_by_user_id: str,
                    command_type: CommandType,
                    payload: dict, idempotency_key: str, ttl_s: int,
                    encrypted_payload: bytes | None = None) -> StoredCommand:
        existing = await (await self._db.conn.execute(
            "SELECT * FROM queued_commands WHERE household_id=? AND agent_id=? AND idempotency_key=?",
            (household_id, agent_id, idempotency_key),
        )).fetchone()
        if existing is not None:
            if (existing["command_type"] != command_type.value
                    or json.loads(existing["payload_json"]) != payload):
                raise IdempotencyConflict("idempotency key was already used for another command")
            return self._row_command(existing)
        now = time.time()
        command_id = f"cmd-{uuid.uuid4().hex}"
        await self._db.conn.execute(
            "INSERT INTO queued_commands(command_id,agent_id,owner_id,household_id,requested_by_user_id,"
            "command_type,state,payload_json,encrypted_payload,idempotency_key,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (command_id, agent_id, requested_by_user_id, household_id, requested_by_user_id,
             command_type.value, CommandState.QUEUED.value,
             json.dumps(payload, sort_keys=True), encrypted_payload, idempotency_key, now, now + ttl_s),
        )
        setup_status = {
            CommandType.DISCOVER: "discovering",
            CommandType.CAMERA_TEST: "configuring_camera",
            CommandType.CONFIGURE_CAMERA: "configuring_camera",
            CommandType.SET_INFERENCE_MODE: "selecting_mode",
        }.get(command_type)
        if setup_status:
            await self._db.conn.execute(
                "UPDATE agent_control_state SET setup_status=?,updated_at=? WHERE agent_id=?",
                (setup_status, now, agent_id),
            )
        await self._db.conn.commit()
        return StoredCommand(command_id, agent_id, command_type.value, "queued", payload,
                             encrypted_payload, now, now + ttl_s)

    async def poll(self, agent_id: str, cipher: CredentialCipher | None,
                   *, delivery_lease_s: int = 30) -> StoredCommand | None:
        now = time.time()
        await self._db.conn.execute(
            "UPDATE queued_commands SET state='expired',completed_at=?,encrypted_payload=NULL "
            "WHERE agent_id=? AND state IN ('queued','delivered') AND expires_at<=?", (now, agent_id, now),
        )
        await self._db.conn.execute(
            "UPDATE queued_commands SET state='queued',delivered_at=NULL "
            "WHERE agent_id=? AND state='delivered' AND delivered_at<=? AND expires_at>?",
            (agent_id, now - delivery_lease_s, now),
        )
        row = await (await self._db.conn.execute(
            "SELECT * FROM queued_commands WHERE agent_id=? AND "
            "((state='queued' AND expires_at>?) OR (state='running' AND delivered_at<=?)) "
            "ORDER BY created_at LIMIT 1", (agent_id, now, now - delivery_lease_s),
        )).fetchone()
        if row is None:
            await self._db.conn.commit()
            return None
        await self._db.conn.execute(
            "UPDATE queued_commands SET state=CASE WHEN state='running' THEN state ELSE 'delivered' END,"
            "delivered_at=? WHERE command_id=?",
            (now, row["command_id"]),
        )
        await self._db.conn.commit()
        command = self._row_command(row)
        command.state = "delivered"
        if command.encrypted_payload is not None:
            if cipher is None:
                raise RuntimeError("credential encryption is not configured")
            command.payload.update(cipher.decrypt(command.encrypted_payload))
        return command

    async def record_result(self, agent_id: str, result: CommandResult) -> bool:
        row = await (await self._db.conn.execute(
            "SELECT command_type,state,expires_at FROM queued_commands WHERE command_id=? AND agent_id=?",
            (result.command_id, agent_id),
        )).fetchone()
        if row is None:
            return False
        now = time.time()
        if row["state"] in ("queued", "delivered") and row["expires_at"] <= now:
            await self._db.conn.execute(
                "UPDATE queued_commands SET state='expired',completed_at=?,encrypted_payload=NULL WHERE command_id=?",
                (now, result.command_id),
            )
            await self._db.conn.commit()
            return False
        if row["state"] in ("succeeded", "failed", "expired"):
            return row["state"] == result.state.value
        allowed = {
            "queued": {"running", "succeeded", "failed"},
            "delivered": {"running", "succeeded", "failed"},
            "running": {"running", "succeeded", "failed"},
        }
        if result.state.value not in allowed.get(row["state"], set()):
            return False
        completed = result.completed_at.timestamp() if result.completed_at else time.time()
        await self._db.conn.execute(
            "UPDATE queued_commands SET state=?,completed_at=?,"
            "encrypted_payload=CASE WHEN ? IN ('running','succeeded','failed') THEN NULL ELSE encrypted_payload END "
            "WHERE command_id=?",
            (result.state.value, completed, result.state.value, result.command_id),
        )
        await self._db.conn.execute(
            "INSERT INTO command_results(command_id,state,failure_reason,message,data_json,completed_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(command_id) DO UPDATE SET state=excluded.state,"
            "failure_reason=excluded.failure_reason,message=excluded.message,data_json=excluded.data_json,"
            "completed_at=excluded.completed_at",
            (result.command_id, result.state.value,
             result.failure_reason.value if result.failure_reason else None,
             result.message, json.dumps(result.data, sort_keys=True), completed),
        )
        if row["command_type"] == CommandType.DISCOVER.value and result.state is CommandState.SUCCEEDED:
            await self._db.conn.execute(
                "INSERT INTO discovery_results(agent_id,command_id,result_json,discovered_at) VALUES(?,?,?,?) "
                "ON CONFLICT(agent_id,command_id) DO UPDATE SET result_json=excluded.result_json,"
                "discovered_at=excluded.discovered_at",
                (agent_id, result.command_id, json.dumps(result.data.get("cameras", [])), completed),
            )
        if result.state is CommandState.FAILED:
            await self._db.conn.execute(
                "UPDATE agent_control_state SET setup_status='failed',health_explanation=?,updated_at=? "
                "WHERE agent_id=?", (result.message, completed, agent_id),
            )
        elif result.state is CommandState.SUCCEEDED:
            next_status = {
                CommandType.DISCOVER.value: "configuring_camera",
                CommandType.CONFIGURE_CAMERA.value: "selecting_mode",
                CommandType.SET_INFERENCE_MODE.value: "active",
            }.get(row["command_type"])
            if next_status:
                effective_mode = result.data.get("effective_mode") \
                    if row["command_type"] == CommandType.SET_INFERENCE_MODE.value else None
                await self._db.conn.execute(
                    "UPDATE agent_control_state SET setup_status=?,"
                    "effective_inference_mode=COALESCE(?,effective_inference_mode),"
                    "health_explanation=NULL,updated_at=? WHERE agent_id=?",
                    (next_status, effective_mode, completed, agent_id),
                )
        await self._db.conn.commit()
        return True

    async def discovery(self, agent_id: str) -> list[dict]:
        row = await (await self._db.conn.execute(
            "SELECT result_json FROM discovery_results WHERE agent_id=? ORDER BY discovered_at DESC LIMIT 1",
            (agent_id,),
        )).fetchone()
        return json.loads(row["result_json"]) if row else []

    async def command_status(self, household_id: str, agent_id: str, command_id: str) -> dict | None:
        row = await (await self._db.conn.execute(
            "SELECT q.state,q.expires_at,r.failure_reason,r.message,r.data_json "
            "FROM queued_commands q LEFT JOIN command_results r USING(command_id) "
            "WHERE q.household_id=? AND q.agent_id=? AND q.command_id=?",
            (household_id, agent_id, command_id),
        )).fetchone()
        if row is None:
            return None
        state = row["state"]
        if state in ("queued", "delivered") and row["expires_at"] <= time.time():
            state = "expired"
        return {"command_id": command_id, "state": state,
                "failure_reason": row["failure_reason"], "message": row["message"],
                "data": json.loads(row["data_json"]) if row["data_json"] else {}}

    @staticmethod
    def _row_command(row) -> StoredCommand:
        return StoredCommand(row["command_id"], row["agent_id"], row["command_type"], row["state"],
                             json.loads(row["payload_json"]), row["encrypted_payload"],
                             row["created_at"], row["expires_at"])
