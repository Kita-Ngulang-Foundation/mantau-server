from __future__ import annotations

import hashlib
import hmac

from fastapi.testclient import TestClient
from mantau_core.contracts import Envelope, EventKind, FallEvent

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings

OWNER = {"X-Mantau-User-ID": "family"}
STRANGER = {"X-Mantau-User-ID": "stranger"}
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64


def _app(tmp_path, **overrides):
    return create_app(Settings(
        db_path=str(tmp_path / "s.db"), control_plane_mode="local_dev",
        recordings_dir=str(tmp_path / "recordings"), **overrides,
    ))


def _setup(client, agent_id="agent-1", camera_id="cam-1", user=OWNER):
    enrolled = client.post("/agents/enroll", json={"agent_id": agent_id}).json()
    client.post("/agent-claims", headers=user,
                json={"claim_code": enrolled["claim_code"], "platform": "linux"})
    client.post("/cameras", headers=user,
                json={"camera_id": camera_id, "name": "Kamar", "agent_id": agent_id})
    return enrolled["secret"]


def _agent(agent_id, secret):
    return {"X-Mantau-Agent-ID": agent_id, "X-Mantau-Agent-Secret": secret}


def _signed(secret, event_id, body, agent_id="agent-1"):
    return {
        "X-Mantau-Agent": agent_id,
        "X-Mantau-Signature": hmac.new(secret.encode(), event_id.encode() + b"." + body,
                                       hashlib.sha256).hexdigest(),
        "Content-Type": "video/mp4",
    }


def test_settings_default_then_versioned_and_delivered_to_the_agent(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        secret = _setup(client)
        default = client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()
        assert default["customized"] is False
        assert default["settings"]["bathroom"]["warning_minutes"] == 20.0

        settings = default["settings"]
        settings["bathroom"]["warning_minutes"] = 15
        settings["zones"] = [{"zone_id": "door", "kind": "bathroom_door", "name": "Pintu",
                              "polygon": [{"x": 0.1, "y": 0.1}, {"x": 0.3, "y": 0.1},
                                          {"x": 0.3, "y": 0.6}]}]
        saved = client.put("/cameras/cam-1/detection-settings", headers=OWNER, json=settings)
        assert saved.status_code == 200
        body = saved.json()
        assert body["settings"]["version"] == 2
        assert body["applied_version"] is None

        command = client.post("/agent-control/commands/poll", headers=_agent("agent-1", secret),
                              json={}).json()
        assert command["command_type"] == "apply_detection_settings"
        assert command["payload"]["settings"]["bathroom"]["warning_minutes"] == 15
        assert client.post(
            f"/agent-control/commands/{command['command_id']}/results",
            headers=_agent("agent-1", secret),
            json={"command_id": command["command_id"], "state": "succeeded",
                  "data": {"camera_id": "cam-1", "detection_settings_version": 2}},
        ).status_code == 204
        current = client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()
        assert current["applied_version"] == 2
        assert current["customized"] is True


def test_settings_are_validated_and_household_scoped(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        _setup(client)
        bad = client.put("/cameras/cam-1/detection-settings", headers=OWNER,
                         json={"timezone": "Nowhere/Here"})
        assert bad.status_code == 422
        assert client.get("/cameras/cam-1/detection-settings",
                          headers=STRANGER).status_code == 404
        assert client.put("/cameras/cam-1/detection-settings", headers=STRANGER,
                          json={}).status_code == 404


def test_only_the_producing_agent_uploads_and_members_download(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        secret = _setup(client)
        other_secret = _setup(client, agent_id="agent-2", camera_id="cam-2", user=STRANGER)
        event = FallEvent(camera_id="cam-1")
        envelope = Envelope.for_event("agent-1", seq=0, event=event).sign(secret)
        client.post("/ingest", json=envelope.model_dump(mode="json"))

        path = f"/events/{event.event_id}/recording"
        assert client.post(path, content=MP4, headers={
            **_signed(secret, event.event_id, MP4), "X-Mantau-Signature": "0" * 64,
        }).status_code == 401
        assert client.post(path, content=MP4, headers=_signed(
            other_secret, event.event_id, MP4, agent_id="agent-2")).status_code == 404
        assert client.post(path, content=MP4, headers={
            **_signed(secret, event.event_id, MP4), "Content-Type": "text/html",
        }).status_code == 415
        assert client.post(path, content=MP4,
                           headers=_signed(secret, event.event_id, MP4)).status_code == 204

        assert client.get(path, headers=OWNER).content == MP4
        assert client.get(path, headers=STRANGER).status_code == 404
        listed = client.get("/events", headers=OWNER).json()
        assert listed[0]["has_recording"] is True


def test_bathroom_events_are_never_recorded_and_uploads_are_bounded(tmp_path):
    with TestClient(_app(tmp_path, recording_max_bytes=100)) as client:
        secret = _setup(client)
        bathroom = FallEvent(camera_id="cam-1", kind=EventKind.BATHROOM_DURATION)
        fall = FallEvent(camera_id="cam-1")
        for seq, event in enumerate((bathroom, fall)):
            envelope = Envelope.for_event("agent-1", seq=seq, event=event).sign(secret)
            client.post("/ingest", json=envelope.model_dump(mode="json"))
        assert client.post(f"/events/{bathroom.event_id}/recording", content=MP4,
                           headers=_signed(secret, bathroom.event_id, MP4)).status_code == 403
        big = b"x" * 101
        assert client.post(f"/events/{fall.event_id}/recording", content=big,
                           headers=_signed(secret, fall.event_id, big)).status_code == 413
        assert not (tmp_path / "recordings").exists() or not any(
            (tmp_path / "recordings").rglob("*.mp4"))


def test_frame_uploads_are_bounded(tmp_path):
    with TestClient(_app(tmp_path, frame_max_bytes=10)) as client:
        secret = _setup(client)
        body = b"y" * 11
        signature = hmac.new(secret.encode(), b"cam-1." + body, hashlib.sha256).hexdigest()
        assert client.post("/cameras/cam-1/frame", content=body, headers={
            "X-Mantau-Agent": "agent-1", "X-Mantau-Signature": signature,
        }).status_code == 413
