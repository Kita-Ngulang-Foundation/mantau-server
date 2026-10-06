"""Desired authorization, truthful health, upgrade and durable setup behavior."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import hashlib
import hmac

import pytest

import support
from mantau_core.contracts import Envelope, FallEvent
from mantau_ld.api.routes import frames as frames_routes
from test_control_plane import _settings, USER, OTHER
from test_inference import FakeDetector, _decode, _upload

CAMERA = {"camera": {"camera_id": "cam-a", "name": "Room", "host": "192.0.2.1"},
          "credentials": {"username": "operator", "password": "private-camera-value"}}


class RuntimeFailureDetector(FakeDetector):
    def perceive(self, image, ts_ms):
        if image == b"runtime-failure":
            raise RuntimeError("temporary inference outage")
        return super().perceive(image, ts_ms)


def _client(tmp_path, **overrides):
    return support.client(_settings(tmp_path, inference_max_fps=10_000, **overrides),
                          inference_factory=RuntimeFailureDetector, inference_decoder=_decode)


def _setup(client):
    return support.enroll(client, USER, agent_id="agent-a", camera_id="cam-a")


def _health(client):
    return client.get("/agents", headers=USER).json()[0]


async def _sql(app, query, params):
    async with app.state.db.serialized():
        result = await app.state.db.conn.execute(query, params)
        rows = await result.fetchall()
        await app.state.db.conn.commit()
        return [dict(row) for row in rows]


def _query(client, query, params=()):
    return client.portal.call(_sql, client.app, query, params)


def _join(client, role):
    if role == "owner":
        return USER
    household = client.get("/households", headers=USER).json()[0]["household_id"]
    invite = client.post(f"/households/{household}/invites", headers=USER,
                         json={"role": role}).json()["invite_code"]
    headers = support.user(f"safety-{role}")
    assert client.post("/households/join", headers=headers,
                       json={"invite_code": invite}).status_code == 200
    return {**headers, "X-Mantau-Household-ID": household}


def test_protection_requires_actual_fresh_inference_and_recovers_after_expiry(tmp_path):
    with _client(tmp_path) as client:
        enrolled = _setup(client)
        assert _health(client)["health_state"] == "offline"
        agent_headers = support.agent_headers(enrolled)
        assert client.post("/agent-control/commands/poll", headers=agent_headers, json={
            "status": {"health_state": "online", "setup_status": "active",
                       "camera_connectivity": "reachable"}}).status_code == 204
        assert _health(client)["health_state"] == "degraded"
        assert _upload(client, enrolled["secret"]).status_code == 200
        protected = _health(client)
        assert protected["health_state"] == "online"
        assert protected["setup_status"] == "active"
        assert protected["last_frame_at"] is not None and protected["last_inference_at"] is not None
        _query(client, "UPDATE agents SET last_frame_at=last_frame_at-16,last_inference_at=last_inference_at-16")
        assert _health(client)["health_state"] == "degraded"
        assert _health(client)["setup_status"] != "active"
        assert _upload(client, enrolled["secret"], ts_ms=2000).status_code == 200
        assert _health(client)["health_state"] == "online"


@pytest.mark.parametrize("failure", ["runtime", "capacity"])
def test_current_inference_refusal_immediately_removes_protection_and_can_recover(tmp_path, failure):
    with _client(tmp_path, inference_max_sessions=1) as client:
        enrolled = _setup(client)
        assert _upload(client, enrolled["secret"]).status_code == 200
        assert _health(client)["health_state"] == "online"
        failed = _upload(client, enrolled["secret"],
                         body=b"runtime-failure" if failure == "runtime" else b"standing",
                         session_id="s1" if failure == "runtime" else "capacity-session", ts_ms=2000)
        assert failed.status_code == 503
        assert _health(client)["health_state"] == "degraded"
        assert _health(client)["setup_status"] != "active"
        assert _upload(client, enrolled["secret"], ts_ms=3000).status_code == 200
        assert _health(client)["health_state"] == "online"


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_role_policy_covers_commands_camera_mutations_and_settings(tmp_path, role):
    with _client(tmp_path) as client:
        _setup(client)
        principal = _join(client, role)
        assert client.get("/cameras/cam-a", headers=principal).status_code == 200
        operations = [
            ("POST", "/agents/agent-a/commands/discover", None, 200),
            ("POST", "/agents/agent-a/commands/restart", None, 200),
            ("POST", "/agents/agent-a/commands/reconfigure", None, 200),
            ("POST", "/agents/agent-a/camera-tests", CAMERA, 200),
            ("PUT", "/agents/agent-a/camera", CAMERA, 200),
            ("PUT", "/agents/agent-a/inference-mode", {"mode": "EDGE"}, 200),
            ("PUT", "/cameras/cam-a/detection-settings", {"version": 1}, 200),
            ("POST", "/cameras", {"camera_id": "side-camera", "name": "Side", "agent_id": "agent-a"}, 201),
            ("DELETE", "/cameras/cam-a", None, 204),
        ]
        for index, (method, path, body, allowed) in enumerate(operations):
            response = client.request(method, path, json=body,
                                      headers={**principal, "Idempotency-Key": f"role-{index}"})
            assert response.status_code == (403 if role == "member" else allowed), response.text
            if role != "member" and path.endswith("inference-mode"):
                assert response.json()["requested_inference_mode"] == "CLOUD"
        if role == "member":
            assert _query(client, "SELECT * FROM queued_commands") == []
            assert client.get("/cameras/cam-a", headers=USER).status_code == 200


def test_cross_household_manager_cannot_mutate_other_household_devices(tmp_path):
    with _client(tmp_path) as client:
        _setup(client)
        for method, path, body in [
            ("POST", "/agents/agent-a/commands/restart", None),
            ("PUT", "/cameras/cam-a/detection-settings", {"version": 1}),
            ("DELETE", "/cameras/cam-a", None),
        ]:
            response = client.request(method, path, json=body,
                                      headers={**OTHER, "Idempotency-Key": "cross-household"})
            assert response.status_code == 404
        assert _query(client, "SELECT * FROM queued_commands") == []


def test_saved_two_minute_floor_and_applied_version_survive_existing_migration_four(tmp_path):
    with _client(tmp_path) as client:
        _setup(client)
        saved = client.put("/cameras/cam-a/detection-settings", headers=USER,
                           json={"version": 1, "stillness": {"floor_minutes": 2.0}})
        assert saved.status_code == 200
        _query(client, "UPDATE camera_detection_settings SET applied_version=version")
        assert _query(client, "SELECT version FROM schema_migrations WHERE version=4")
    for _ in range(2):
        with _client(tmp_path) as client:
            settings = client.get("/cameras/cam-a/detection-settings", headers=USER).json()
            assert settings["settings"]["stillness"]["floor_minutes"] == 2.0
            assert settings["settings"]["version"] == settings["applied_version"] == 2
            assert _query(client, "SELECT COUNT(*) AS count FROM schema_migrations WHERE version=4")[0]["count"] == 1


def test_concurrent_settings_edit_conflicts_instead_of_overwriting_and_queues_once(tmp_path):
    with _client(tmp_path) as client:
        _setup(client)
        gate = threading.Barrier(2)

        def edit(confidence):
            gate.wait(timeout=3)
            return client.put("/cameras/cam-a/detection-settings", headers=USER,
                              json={"version": 1, "fall": {"min_confidence": confidence}})

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(edit, (0.7, 0.8)))
        assert sorted(response.status_code for response in responses) == [200, 409]
        winner = next(response for response in responses if response.status_code == 200).json()
        actual = client.get("/cameras/cam-a/detection-settings", headers=USER).json()
        assert actual["settings"] == winner["settings"]
        assert actual["settings"]["version"] == 2
        assert len(_query(client, "SELECT * FROM queued_commands WHERE command_type='apply_detection_settings'")) == 1


def test_settings_queue_failure_rolls_back_saved_version_and_can_retry(tmp_path):
    with _client(tmp_path) as client:
        _setup(client)
        _query(client, "CREATE TRIGGER fail_settings_queue BEFORE INSERT ON queued_commands "
               "WHEN NEW.command_type='apply_detection_settings' "
               "BEGIN SELECT RAISE(ABORT,'injected command failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected command failure"):
            client.put("/cameras/cam-a/detection-settings", headers=USER,
                       json={"version": 1, "fall": {"enabled": False}})
        assert _query(client, "SELECT * FROM camera_detection_settings") == []
        assert _query(client, "SELECT * FROM queued_commands") == []
        _query(client, "DROP TRIGGER fail_settings_queue")
        assert client.put("/cameras/cam-a/detection-settings", headers=USER,
                          json={"version": 1, "fall": {"enabled": False}}).status_code == 200


def test_offline_camera_removal_survives_restart_and_cannot_replay_old_configuration(tmp_path):
    with _client(tmp_path) as client:
        enrolled = _setup(client)
        configured = client.put("/agents/agent-a/camera", json=CAMERA,
                                headers={**USER, "Idempotency-Key": "old-configure"})
        assert configured.status_code == 200
        assert client.delete("/cameras/cam-a", headers=USER).status_code == 204
        assert client.get("/cameras/cam-a", headers=USER).status_code == 404
        commands = _query(client, "SELECT command_type,state,encrypted_payload FROM queued_commands")
        assert next(row for row in commands if row["command_type"] == "configure_camera")["state"] == "expired"
        assert all(row["encrypted_payload"] is None for row in commands)
    with _client(tmp_path) as client:
        command = client.post("/agent-control/commands/poll", headers=support.agent_headers(enrolled), json={})
        assert command.status_code == 200
        assert command.json()["command_type"] == "remove_camera"
        assert command.json()["payload"] == {"camera_id": "cam-a"}
        late_result = client.post(f"/agent-control/commands/{configured.json()['command_id']}/results",
                                 headers=support.agent_headers(enrolled),
                                 json={"command_id": configured.json()["command_id"], "state": "succeeded"})
        assert late_result.status_code == 404
        replay = client.put("/agents/agent-a/camera", json=CAMERA,
                            headers={**USER, "Idempotency-Key": "old-configure"})
        assert replay.status_code in (404, 409)
        assert client.get("/cameras/cam-a", headers=USER).status_code == 404
        assert _upload(client, enrolled["secret"]).status_code == 404


@pytest.mark.parametrize("removed", ["agent", "camera"])
def test_ingest_rechecks_revocation_after_initial_camera_validation(tmp_path, monkeypatch, removed):
    with _client(tmp_path) as client:
        enrolled = _setup(client)
        repo = client.app.state.cameras_repo
        original = repo.get_for_agent
        removed_once = False

        async def invalidate_after_initial_lookup(agent_id, camera_id):
            nonlocal removed_once
            camera = await original(agent_id, camera_id)
            if camera is not None and not removed_once:
                removed_once = True
                if removed == "agent":
                    await client.app.state.agents_repo.revoke(agent_id, camera.household_id)
                else:
                    await repo.delete(camera.household_id, camera_id)
            return camera

        monkeypatch.setattr(repo, "get_for_agent", invalidate_after_initial_lookup)
        event = FallEvent(camera_id="cam-a", confidence=0.91)
        envelope = Envelope.for_event("agent-a", seq=0, event=event).sign(enrolled["secret"])
        rejected = client.post("/ingest", json=envelope.model_dump(mode="json"))
        assert rejected.status_code == (401 if removed == "agent" else 404)
        for table in ("events", "push_outbox", "ingested_envelopes"):
            assert _query(client, f"SELECT * FROM {table}") == []


@pytest.mark.parametrize("removed", ["agent", "camera"])
def test_frame_push_rechecks_revocation_after_signature_validation(tmp_path, monkeypatch, removed):
    with _client(tmp_path) as client:
        enrolled = _setup(client)
        original = frames_routes._verify

        async def invalidate_after_signature(camera_id, body, agent_id, signature, agents):
            await original(camera_id, body, agent_id, signature, agents)
            agent = await agents.get(agent_id)
            if removed == "agent":
                await agents.revoke(agent_id, agent.household_id)
            else:
                await client.app.state.cameras_repo.delete(agent.household_id, camera_id)

        monkeypatch.setattr(frames_routes, "_verify", invalidate_after_signature)
        body = b"signed-fresh-frame"
        signature = hmac.new(enrolled["secret"].encode(), b"cam-a." + body, hashlib.sha256).hexdigest()
        rejected = client.post("/cameras/cam-a/frame", content=body,
                               headers={"X-Mantau-Agent": "agent-a", "X-Mantau-Signature": signature})
        assert rejected.status_code == 401
        assert client.app.state.frames.latest("cam-a") is None
