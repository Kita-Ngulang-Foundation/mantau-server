"""Per-camera stream settings: gated on agents that apply them, preserved
for apps that predate them, and inference frames that stand in for live view."""

from __future__ import annotations

import time
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mantau_core.contracts import inference as contract

import support
from test_inference import USER_A, _client, _setup

OWNER = support.user("family")
LOW = {"max_width": 384, "jpeg_quality": 50, "detection_fps": 5.0,
       "live_from_detection": True, "motion_saver": True}


def _app(tmp_path):
    return support.app(support.settings(
        db_path=str(tmp_path / "s.db"), recordings_dir=str(tmp_path / "recordings"),
        control_plane_encryption_key=Fernet.generate_key().decode("ascii"),
        command_delivery_lease_s=0,
    ))


def _agent(enrolled):
    return {"X-Mantau-Agent-ID": enrolled["agent_id"], "X-Mantau-Agent-Secret": enrolled["secret"]}


def _report(client, enrolled, status):
    response = client.post("/agent-control/commands/poll", headers=_agent(enrolled), json={"status": status})
    assert response.status_code in (200, 204)


def _poll_settings(client, enrolled, wait_s=5.0):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        response = client.post("/agent-control/commands/poll", headers=_agent(enrolled), json={})
        if response.status_code == 200 and response.json()["command_type"] == "apply_detection_settings":
            return response.json()["payload"]["settings"]
        time.sleep(0.05)
    raise AssertionError("agent never received apply_detection_settings")


def test_defaults_are_served_and_old_agents_are_not_marked_supported(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        support.enroll(client, OWNER, camera_id="cam-1")
        body = client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()
        assert body["stream_supported"] is False
        assert body["settings"]["stream"] == {"max_width": 640, "jpeg_quality": 65, "detection_fps": 10.0,
                                              "live_from_detection": False, "motion_saver": False}


def test_changing_stream_needs_an_updated_agent(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled = support.enroll(client, OWNER, camera_id="cam-1")
        settings = client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()["settings"]
        settings["stream"] = LOW
        old = client.put("/cameras/cam-1/detection-settings", headers=OWNER, json=settings)
        assert old.status_code == 409 and old.json()["detail"] == "agent_stream_upgrade_required"

        _report(client, enrolled, {"stream_settings": True})
        assert client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()["stream_supported"] is True
        ok = client.put("/cameras/cam-1/detection-settings", headers=OWNER, json=settings)
        assert ok.status_code == 200 and ok.json()["stream_supported"] is True
        assert ok.json()["settings"]["stream"] == LOW
        assert _poll_settings(client, enrolled)["stream"] == LOW


def test_old_agents_never_receive_the_stream_section(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled = support.enroll(client, OWNER, camera_id="cam-1")
        settings = client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()["settings"]
        settings["stillness"]["other_minutes"] = 60
        assert client.put("/cameras/cam-1/detection-settings", headers=OWNER, json=settings).status_code == 200
        delivered = _poll_settings(client, enrolled)
        assert "stream" not in delivered and delivered["stillness"]["other_minutes"] == 60


def test_support_follows_the_latest_status_report(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled = support.enroll(client, OWNER, camera_id="cam-1")
        _report(client, enrolled, {"stream_settings": True})
        _report(client, enrolled, {"health_state": "healthy"})  # downgraded agent
        assert client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()["stream_supported"] is False


def test_an_app_without_stream_settings_keeps_the_saved_ones(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled = support.enroll(client, OWNER, camera_id="cam-1")
        _report(client, enrolled, {"stream_settings": True})
        settings = client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()["settings"]
        settings["stream"] = LOW
        saved = client.put("/cameras/cam-1/detection-settings", headers=OWNER, json=settings).json()["settings"]
        old_app = {k: v for k, v in saved.items() if k != "stream"}
        old_app["stillness"]["other_minutes"] = 90
        again = client.put("/cameras/cam-1/detection-settings", headers=OWNER, json=old_app)
        assert again.status_code == 200
        assert again.json()["settings"]["stream"] == LOW
        assert again.json()["settings"]["stillness"]["other_minutes"] == 90


def _upload(client, secret, *, live: bool, body=b"standing"):
    frame_id = uuid.uuid4().hex
    captured = int(time.time() * 1000)
    fields = dict(agent_id="agent-a", camera_id="cam-a", session_id="s1", frame_id=frame_id,
                  ts_ms=captured, captured_at_ms=captured, event_ids=[])
    headers = {
        "Content-Type": "image/jpeg", "X-Mantau-Agent": "agent-a", "X-Mantau-Camera": "cam-a",
        "X-Mantau-Session": "s1", "X-Mantau-Frame": frame_id, "X-Mantau-Frame-Ts": str(captured),
        "X-Mantau-Captured-At": str(captured),
        "X-Mantau-Signature": contract.sign(secret, body=body, **fields),
    }
    if live:
        headers["X-Mantau-Live"] = "1"
    return client.post("/agents/agent-a/inference", content=body, headers=headers)


def test_inference_frame_feeds_live_view_only_when_asked():
    with _client() as client:
        secret = _setup(client)
        plain = _upload(client, secret, live=False)
        assert plain.status_code == 200 and "X-Mantau-Live-Frame" not in plain.headers
        assert client.get("/cameras/cam-a/snapshot.jpg", headers=USER_A).status_code == 404

        live = _upload(client, secret, live=True, body=b"standing")
        assert live.status_code == 200 and live.json()["processed"] is True
        assert live.headers["X-Mantau-Live-Frame"] == "1"
        # The snapshot request above counts as a viewer for a few seconds.
        assert live.headers["X-Mantau-Live-Viewers"] == "1"
        snapshot = client.get("/cameras/cam-a/snapshot.jpg", headers=USER_A)
        assert snapshot.status_code == 200 and snapshot.content == b"standing"


def test_live_frame_is_not_stored_for_a_rejected_upload():
    with _client() as client:
        secret = _setup(client)
        forged = _upload(client, "wrong-secret", live=True)
        assert forged.status_code == 401 and "X-Mantau-Live-Frame" not in forged.headers
        assert client.get("/cameras/cam-a/snapshot.jpg", headers=USER_A).status_code == 404
