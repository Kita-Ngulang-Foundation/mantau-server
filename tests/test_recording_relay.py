"""Clips stay on the agent; the server only relays one authenticated transfer."""
from __future__ import annotations

import hashlib
import hmac
import time
from concurrent.futures import ThreadPoolExecutor

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mantau_core.contracts import Envelope, EventKind, FallEvent

import support

OWNER = support.user("family")
STRANGER = support.user("stranger")
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64


def _app(tmp_path):
    return support.app(support.settings(
        db_path=str(tmp_path / "s.db"), recordings_dir=str(tmp_path / "recordings"),
        control_plane_encryption_key=Fernet.generate_key().decode("ascii"),
        command_delivery_lease_s=0,
    ))


def _agent(enrolled):
    return {"X-Mantau-Agent-ID": enrolled["agent_id"], "X-Mantau-Agent-Secret": enrolled["secret"]}


def _signed(secret, event_id, body, agent_id="agent-1"):
    return {
        "X-Mantau-Agent": agent_id, "Content-Type": "video/mp4",
        "X-Mantau-Signature": hmac.new(secret.encode(), event_id.encode() + b"." + body,
                                       hashlib.sha256).hexdigest(),
    }


def _ingest(client, enrolled, event, seq):
    envelope = Envelope.for_event(enrolled["agent_id"], seq=seq, event=event).sign(enrolled["secret"])
    assert client.post("/ingest", json=envelope.model_dump(mode="json")).status_code < 300


def _report(client, enrolled, event_id, size=len(MP4)):
    status = {"local_recordings": [{"event_id": event_id, "size_bytes": size, "captured_at_ms": 5}]}
    return client.post("/agent-control/commands/poll", headers=_agent(enrolled), json={"status": status})


def _poll_command(client, enrolled, wait_s=5.0):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        response = client.post("/agent-control/commands/poll", headers=_agent(enrolled), json={})
        if response.status_code == 200:
            return response.json()
        time.sleep(0.05)
    raise AssertionError("agent never received upload_recording")


def _setup(client):
    enrolled = support.enroll(client, OWNER, camera_id="cam-1")
    event = FallEvent(camera_id="cam-1")
    _ingest(client, enrolled, event, 0)
    return enrolled, event


def test_clip_is_relayed_once_without_server_storage(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled, event = _setup(client)
        path = f"/events/{event.event_id}/recording"
        assert client.get(path, headers=OWNER).status_code == 404  # not yet reported
        assert _report(client, enrolled, event.event_id).status_code in (200, 204)
        assert client.get("/events", headers=OWNER).json()[0]["has_recording"] is True
        settings = client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()
        assert settings["recordings_supported"] is True

        with ThreadPoolExecutor(max_workers=1) as pool:
            download = pool.submit(client.get, path, headers=OWNER)
            command = _poll_command(client, enrolled)
            assert command["command_type"] == "upload_recording"
            transfer_id = command["payload"]["transfer_id"]
            assert command["payload"]["event_id"] == event.event_id
            uploaded = client.post(f"{path}?transfer_id={transfer_id}", content=MP4,
                                   headers=_signed(enrolled["secret"], event.event_id, MP4))
            assert uploaded.status_code == 204
            response = download.result(timeout=10)
        assert response.status_code == 200 and response.content == MP4
        assert response.headers["cache-control"] == "private, no-store"
        # One use only; nothing was written to the recordings directory.
        again = client.post(f"{path}?transfer_id={transfer_id}", content=MP4,
                            headers=_signed(enrolled["secret"], event.event_id, MP4))
        assert again.status_code == 410
        assert not (tmp_path / "recordings").exists() or not any((tmp_path / "recordings").rglob("*"))
        assert client.app.state.recording_relay.pending == {}


def test_updated_agent_cannot_use_legacy_stored_upload(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled, event = _setup(client)
        _report(client, enrolled, event.event_id)
        response = client.post(f"/events/{event.event_id}/recording", content=MP4,
                               headers=_signed(enrolled["secret"], event.event_id, MP4))
        assert response.status_code == 409
        assert response.json()["detail"] == "recording_transfer_required"


def test_relay_rejects_wrong_size_unknown_transfer_and_strangers(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled, event = _setup(client)
        _report(client, enrolled, event.event_id)
        path = f"/events/{event.event_id}/recording"
        client.app.state.recording_relay.timeout_s = 2.0
        assert client.get(path, headers=STRANGER).status_code == 404
        bogus = client.post(f"{path}?transfer_id={'a' * 32}", content=MP4,
                            headers=_signed(enrolled["secret"], event.event_id, MP4))
        assert bogus.status_code == 410
        with ThreadPoolExecutor(max_workers=1) as pool:
            download = pool.submit(client.get, path, headers=OWNER)
            transfer_id = _poll_command(client, enrolled)["payload"]["transfer_id"]
            wrong = MP4 + b"extra"
            rejected = client.post(f"{path}?transfer_id={transfer_id}", content=wrong,
                                   headers=_signed(enrolled["secret"], event.event_id, wrong))
            assert rejected.status_code == 410
            assert download.result(timeout=10).status_code == 503


def test_agent_that_never_answers_gives_503_and_frees_the_slot(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled, event = _setup(client)
        _report(client, enrolled, event.event_id)
        client.app.state.recording_relay.timeout_s = 0.2
        response = client.get(f"/events/{event.event_id}/recording", headers=OWNER)
        assert response.status_code == 503
        assert response.json()["detail"] == "recording_agent_unavailable"
        assert client.app.state.recording_relay.pending == {}


def test_only_two_transfers_run_at_once(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled, event = _setup(client)
        _report(client, enrolled, event.event_id)
        client.app.state.recording_relay.max_transfers = 0
        response = client.get(f"/events/{event.event_id}/recording", headers=OWNER)
        assert response.status_code == 503
        assert response.json()["detail"] == "recording_transfer_busy"


def test_bathroom_events_and_bad_snapshots_are_not_offered(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled, fall = _setup(client)
        bathroom = FallEvent(camera_id="cam-1", kind=EventKind.BATHROOM_DURATION)
        _ingest(client, enrolled, bathroom, 1)
        status = {"local_recordings": [
            {"event_id": bathroom.event_id, "size_bytes": 10, "captured_at_ms": 1},
            {"event_id": "unknown-event", "size_bytes": 10, "captured_at_ms": 1},
        ]}
        client.post("/agent-control/commands/poll", headers=_agent(enrolled), json={"status": status})
        assert client.get(f"/events/{bathroom.event_id}/recording", headers=OWNER).status_code == 404
        bad = client.post("/agent-control/commands/poll", headers=_agent(enrolled), json={
            "status": {"local_recordings": [{"event_id": "../x", "size_bytes": 1, "captured_at_ms": 1}]}})
        assert bad.status_code == 422


def test_recordings_toggle_requires_updated_agent_and_strips_unknown_field(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled, _ = _setup(client)
        settings = client.get("/cameras/cam-1/detection-settings", headers=OWNER).json()["settings"]
        settings["recordings"] = {"enabled": False}
        old = client.put("/cameras/cam-1/detection-settings", headers=OWNER, json=settings)
        assert old.status_code == 409 and old.json()["detail"] == "agent_recordings_upgrade_required"
        _report(client, enrolled, "none-yet")
        ok = client.put("/cameras/cam-1/detection-settings", headers=OWNER, json=settings)
        assert ok.status_code == 200 and ok.json()["recordings_supported"] is True
        command = _poll_command(client, enrolled)
        assert command["payload"]["settings"]["recordings"] == {"enabled": False}
