"""Activity features on the server: zone validation, household privacy of
settings, duplicate delivery, zone ids on events, and the activity rules the
server runs itself for agents in CLOUD mode."""

from __future__ import annotations

import json
import time
import uuid
from itertools import count

from fastapi.testclient import TestClient
from mantau_core.activity import FrameObservation, Perception, PersonObservation, Posture
from mantau_core.contracts import Envelope, EventKind, FallEvent, Severity
from mantau_core.contracts import inference as contract

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings

A = {"X-Mantau-User-ID": "family-a"}
B = {"X-Mantau-User-ID": "family-b"}
SEQ = count(1)
SQUARE = [{"x": 0.1, "y": 0.1}, {"x": 0.4, "y": 0.1}, {"x": 0.4, "y": 0.4}, {"x": 0.1, "y": 0.4}]
BOW_TIE = [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.5}, {"x": 0.5, "y": 0.1}, {"x": 0.1, "y": 0.5}]
SLIVER = [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.1001}]


class LyingDetector:
    """Server-side fake: whatever the frame, one person lies on an unzoned floor."""

    def __init__(self, camera_id, config, clock):
        self.camera_id, self.clock = camera_id, clock

    def perceive(self, image, ts_ms):
        person = PersonObservation(track_id=1, bbox=(0.25, 0.0, 0.1, 0.3), posture=Posture.LYING,
                                   confidence=0.9)
        return Perception(events=[], observation=FrameObservation(
            camera_id=self.camera_id, at=self.clock(), people=(person,)))

    def close(self):
        pass


def _client(**overrides) -> TestClient:
    settings = Settings(db_path=":memory:", control_plane_mode="local_dev", **overrides)
    return TestClient(create_app(settings, inference_factory=LyingDetector,
                                 inference_decoder=lambda jpeg: jpeg))


def _setup(client, agent_id, camera_id, user) -> str:
    enrolled = client.post("/agents/enroll", json={"agent_id": agent_id}).json()
    assert client.post("/agent-claims", headers=user, json={
        "claim_code": enrolled["claim_code"], "platform": "linux_x86_64"}).status_code == 200
    assert client.post("/cameras", headers=user, json={
        "camera_id": camera_id, "name": "Kamar", "agent_id": agent_id}).status_code == 201
    return enrolled["secret"]


def _settings(zones) -> dict:
    return {"version": 1, "zones": [{"zone_id": "z1", "kind": "floor", "polygon": zones}]}


def _ingest(client, secret, event, agent_id="agent-a"):
    envelope = Envelope.for_event(agent_id, next(SEQ), event).sign(secret)
    return client.post("/ingest", json=envelope.model_dump(mode="json"))


def test_self_intersecting_and_degenerate_zones_are_rejected():
    with _client() as client:
        _setup(client, "agent-a", "cam-a", A)
        for polygon in (BOW_TIE, SLIVER):
            r = client.put("/cameras/cam-a/detection-settings", headers=A, json=_settings(polygon))
            assert r.status_code == 422
            assert "polygon" not in r.text or "Invalid value" in r.text  # no input echoed
        ok = client.put("/cameras/cam-a/detection-settings", headers=A, json=_settings(SQUARE))
        assert ok.status_code == 200


def test_settings_are_household_private():
    with _client() as client:
        _setup(client, "agent-a", "cam-a", A)
        _setup(client, "agent-b", "cam-b", B)
        assert client.put("/cameras/cam-a/detection-settings", headers=A,
                          json=_settings(SQUARE)).status_code == 200
        assert client.get("/cameras/cam-a/detection-settings", headers=B).status_code == 404
        assert client.put("/cameras/cam-a/detection-settings", headers=B,
                          json=_settings(SQUARE)).status_code == 404
        assert client.get("/cameras/cam-a/detection-settings",
                          headers=A).json()["settings"]["zones"][0]["zone_id"] == "z1"


def test_zones_saved_before_validation_are_dropped_not_fatal():
    with _client() as client:
        _setup(client, "agent-a", "cam-a", A)
        assert client.put("/cameras/cam-a/detection-settings", headers=A,
                          json=_settings(SQUARE)).status_code == 200
        db = client.app.state.db

        async def corrupt():
            raw = json.dumps({**_settings(SQUARE), "zones": [
                {"zone_id": "bad", "kind": "floor", "polygon": BOW_TIE},
                {"zone_id": "good", "kind": "bed", "polygon": SQUARE}]})
            await db.conn.execute("UPDATE camera_detection_settings SET settings_json=?", (raw,))
            await db.conn.commit()

        client.portal.call(corrupt)
        zones = client.get("/cameras/cam-a/detection-settings", headers=A).json()["settings"]["zones"]
        assert [z["zone_id"] for z in zones] == ["good"]


def test_activity_events_keep_their_zone_and_are_delivered_once():
    with _client() as client:
        secret = _setup(client, "agent-a", "cam-a", A)
        event = FallEvent(camera_id="cam-a", kind=EventKind.BATHROOM_DURATION,
                          severity=Severity.WARNING, zone_id="bathroom",
                          signals={"duration_s": 1200.0}, event_id="a" * 32)
        assert _ingest(client, secret, event).status_code == 200
        # Same event again under a new sequence number (a restart re-sent it).
        assert _ingest(client, secret, event).status_code == 200
        history = client.get("/events", headers=A).json()
        assert [(e["kind"], e["zone_id"]) for e in history] == [("bathroom_duration", "bathroom")]
        assert client.get("/events", headers=B).json() == []


def _upload(client, secret, *, captured_ms, ts_ms, agent_id="agent-a", camera_id="cam-a"):
    frame_id = uuid.uuid4().hex
    fields = dict(agent_id=agent_id, camera_id=camera_id, session_id="s1", frame_id=frame_id,
                  ts_ms=ts_ms, captured_at_ms=captured_ms, event_ids=[])
    return client.post(f"/agents/{agent_id}/inference", content=b"frame", headers={
        "Content-Type": "image/jpeg", "X-Mantau-Agent": agent_id, "X-Mantau-Camera": camera_id,
        "X-Mantau-Session": "s1", "X-Mantau-Frame": frame_id, "X-Mantau-Frame-Ts": str(ts_ms),
        "X-Mantau-Captured-At": str(captured_ms),
        "X-Mantau-Signature": contract.sign(secret, body=b"frame", **fields)})


def test_cloud_agents_get_activity_rules_on_the_server():
    # Frames stamped 1 s apart over 70 s of capture time (freshness relaxed for the test).
    with _client(inference_max_fps=10_000, inference_max_frame_age_s=10_000) as client:
        secret = _setup(client, "agent-a", "cam-a", A)
        assert client.put("/cameras/cam-a/detection-settings", headers=A, json={
            "version": 1, "stillness": {"floor_minutes": 0.5}}).status_code == 200
        start = int(time.time() * 1000) - 80_000
        found = []
        for i in range(70):
            r = _upload(client, secret, captured_ms=start + i * 1000, ts_ms=i * 1000)
            assert r.status_code == 200, r.text
            found += r.json()["events"]
        kinds = [(e["kind"], e["severity"]) for e in found]
        assert kinds == [("stillness", "warning"), ("stillness", "critical")]
        stored = client.get(f"/events/{found[0]['event_id']}", headers=A).json()
        assert stored["signals"]["duration_s"] == 30.0
        assert client.get(f"/events/{found[0]['event_id']}", headers=B).status_code == 404
