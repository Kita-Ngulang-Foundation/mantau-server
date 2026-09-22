from __future__ import annotations

import sqlite3

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings


USER = {"X-Mantau-User-ID": "user-a"}
OTHER = {"X-Mantau-User-ID": "user-b"}


def _settings(tmp_path, **overrides):
    values = dict(
        db_path=str(tmp_path / "control.db"), control_plane_mode="local_dev",
        control_plane_encryption_key=Fernet.generate_key().decode("ascii"),
        command_delivery_lease_s=0,
    )
    values.update(overrides)
    return Settings(**values)


def _enroll_and_claim(client: TestClient):
    enrolled = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()
    claimed = client.post("/agent-claims", headers=USER, json={
        "claim_code": enrolled["claim_code"], "platform": "raspberry_pi",
    })
    assert claimed.status_code == 200
    return enrolled


def _agent_headers(enrolled):
    return {"X-Mantau-Agent-ID": enrolled["agent_id"],
            "X-Mantau-Agent-Secret": enrolled["secret"]}


def test_claims_are_owned_and_control_requests_require_identity(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        enrolled = _enroll_and_claim(client)
        assert client.get("/agents").status_code == 401
        assert [a["agent_id"] for a in client.get("/agents", headers=USER).json()] == ["agent-1"]
        assert client.get("/agents", headers=OTHER).json() == []
        assert client.get("/agents/agent-1/setup", headers=OTHER).status_code == 404
        assert client.post("/agent-claims", headers=OTHER, json={
            "claim_code": enrolled["claim_code"], "platform": "linux",
        }).status_code == 404


def test_command_retry_delivery_restart_and_agent_return(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        enrolled = _enroll_and_claim(client)
        url = "/agents/agent-1/commands/restart"
        first = client.post(url, headers={**USER, "Idempotency-Key": "restart-once"})
        retry = client.post(url, headers={**USER, "Idempotency-Key": "restart-once"})
        assert first.json()["command_id"] == retry.json()["command_id"]
        conflict = client.post("/agents/agent-1/commands/reconfigure",
                               headers={**USER, "Idempotency-Key": "restart-once"})
        assert conflict.status_code == 409

        # It starts offline, then authenticated polling marks it online.
        assert client.get("/agents", headers=USER).json()[0]["health_state"] == "offline"
        polled = client.post("/agent-control/commands/poll", headers=_agent_headers(enrolled),
                             json={"status": {"health_state": "online"}})
        assert polled.status_code == 200
        assert polled.json()["command_type"] == "restart"
        assert client.get("/agents", headers=USER).json()[0]["health_state"] == "online"

        command_id = polled.json()["command_id"]
        result = {"schema_version": 1, "command_id": command_id, "state": "succeeded",
                  "failure_reason": None, "message": "Restart requested.",
                  "data": {"restart_requested": True}, "completed_at": None}
        assert client.post(f"/agent-control/commands/{command_id}/results",
                           headers=_agent_headers(enrolled), json=result).status_code == 204
        assert client.post("/agent-control/commands/poll", headers=_agent_headers(enrolled),
                           json={}).status_code == 204


def test_agent_status_and_results_reject_secret_material(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        enrolled = _enroll_and_claim(client)
        agent_headers = _agent_headers(enrolled)
        assert client.post("/agent-control/commands/poll", headers=agent_headers,
                           json={"status": {"password": "must-not-store"}}).status_code == 400
        queued = client.post("/agents/agent-1/commands/restart",
                             headers={**USER, "Idempotency-Key": "redaction"}).json()
        leaked = {"schema_version": 1, "command_id": queued["command_id"],
                  "state": "failed", "failure_reason": "execution_failed",
                  "message": "password=must-not-store", "data": {}, "completed_at": None}
        assert client.post(f"/agent-control/commands/{queued['command_id']}/results",
                           headers=agent_headers, json=leaked).status_code == 400


def test_expired_commands_are_not_delivered(tmp_path):
    with TestClient(create_app(_settings(tmp_path, command_ttl_s=0))) as client:
        enrolled = _enroll_and_claim(client)
        queued = client.post("/agents/agent-1/commands/reconfigure",
                             headers={**USER, "Idempotency-Key": "expires"})
        assert queued.status_code == 200
        assert client.post("/agent-control/commands/poll", headers=_agent_headers(enrolled),
                           json={}).status_code == 204
    with sqlite3.connect(tmp_path / "control.db") as db:
        assert db.execute("SELECT state FROM queued_commands").fetchone()[0] == "expired"


def test_camera_credentials_are_encrypted_redacted_and_deleted_on_ack(tmp_path):
    password = "camera-password-never-store-plain"
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        enrolled = _enroll_and_claim(client)
        body = {
            "camera": {"name": "Room", "camera_id": "cam-1", "host": "192.0.2.10",
                       "port": 554, "main_path": "/main", "username_present": True},
            "credentials": {"username": "operator", "password": password},
        }
        queued = client.post("/agents/agent-1/camera-tests", json=body,
                             headers={**USER, "Idempotency-Key": "camera-test"})
        assert queued.status_code == 200
        assert password not in queued.text
        assert password not in client.get("/agents", headers=USER).text

        with sqlite3.connect(tmp_path / "control.db") as db:
            payload, encrypted = db.execute(
                "SELECT payload_json,encrypted_payload FROM queued_commands").fetchone()
            assert password not in payload
            assert password.encode() not in encrypted

        command = client.post("/agent-control/commands/poll", headers=_agent_headers(enrolled),
                              json={}).json()
        assert command["payload"]["password"] == password
        running = {"schema_version": 1, "command_id": command["command_id"], "state": "running",
                   "failure_reason": None, "message": None, "data": {}, "completed_at": None}
        assert client.post(f"/agent-control/commands/{command['command_id']}/results",
                           headers=_agent_headers(enrolled), json=running).status_code == 204

        with sqlite3.connect(tmp_path / "control.db") as db:
            assert db.execute("SELECT encrypted_payload FROM queued_commands").fetchone()[0] is None


def test_production_mode_fails_closed_without_auth_configuration(tmp_path):
    settings = _settings(tmp_path, control_plane_mode="production",
                         control_plane_auth_tokens_json="{}")
    with TestClient(create_app(settings)) as client:
        assert client.get("/agents").status_code == 503
