"""The "server accepts" half of the wire contract: an envelope shaped
exactly like `../../protocol/examples/fall_event_envelope.json` (same field
names, same payload shape, same signing scheme) must be accepted by a real
enrolled agent's `/ingest` call.

This does NOT reuse the golden fixture's baked-in `sig` -- that was signed
with a fixed test secret, and `AgentsRepo.enroll()` always mints a fresh
random one (by design; there is no "enroll with a caller-chosen secret" in
the public API, see `store/agents_repo.py`). So this test enrolls a real
agent, takes the secret enrollment actually returns, and signs an envelope
carrying the SAME field values and structure as the golden fixture. That is
the honest way to test "the server accepts this shape" without adding a
test-only backdoor to enrollment.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from mantau_core.contracts import Envelope, EventKind, FallEvent, Heartbeat, Severity

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "protocol" / "examples"
FIXED_TIME = datetime(2026, 9, 13, 4, 12, 3, 114000, tzinfo=timezone.utc)
USER = {"X-Mantau-User-ID": "fixture-user"}


def _load(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def _enroll_claim_camera(client: TestClient, camera_id: str) -> str:
    enrolled = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()
    assert client.post("/agent-claims", headers=USER, json={
        "claim_code": enrolled["claim_code"], "platform": "linux",
    }).status_code == 200
    assert client.post("/cameras", headers=USER, json={
        "camera_id": camera_id, "name": camera_id, "agent_id": "agent-1",
    }).status_code == 201
    return enrolled["secret"]


def test_server_accepts_a_fall_event_shaped_like_the_golden_fixture():
    golden = _load("fall_event_envelope.json")["payload"]

    settings = Settings(db_path=":memory:", control_plane_mode="local_dev")
    with TestClient(create_app(settings)) as client:
        secret = _enroll_claim_camera(client, golden["camera_id"])

        event = FallEvent(
            event_id=golden["event_id"], camera_id=golden["camera_id"],
            kind=EventKind(golden["kind"]), severity=Severity(golden["severity"]),
            occurred_at=FIXED_TIME, confidence=golden["confidence"],
            track_id=golden["track_id"], signals=golden["signals"],
        )
        envelope = Envelope.for_event("agent-1", seq=0, event=event).sign(secret)

        r = client.post("/ingest", json=envelope.model_dump(mode="json"))
        assert r.status_code == 200
        assert r.json()["duplicate"] is False

        stored = client.get(f"/events/{event.event_id}", headers=USER).json()
        assert stored["confidence"] == golden["confidence"]
        assert stored["camera_id"] == golden["camera_id"]


def test_server_accepts_a_heartbeat_shaped_like_the_golden_fixture():
    golden = _load("heartbeat_envelope.json")["payload"]

    settings = Settings(db_path=":memory:", control_plane_mode="local_dev")
    with TestClient(create_app(settings)) as client:
        secret = _enroll_claim_camera(client, golden["camera_id"])

        heartbeat = Heartbeat(
            agent_id="agent-1", camera_id=golden["camera_id"], sent_at=FIXED_TIME,
            camera_reachable=golden["camera_reachable"], detector_alive=golden["detector_alive"],
            queue_depth=golden["queue_depth"],
        )
        envelope = Envelope.for_heartbeat("agent-1", seq=0, heartbeat=heartbeat).sign(secret)

        r = client.post("/ingest", json=envelope.model_dump(mode="json"))
        assert r.status_code == 200

        ready = client.get("/ready").json()
        assert ready["agents"]["agent-1"]["camera_reachable"] == golden["camera_reachable"]
