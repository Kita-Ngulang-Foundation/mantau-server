from __future__ import annotations

from fastapi.testclient import TestClient
from mantau_core.contracts import Envelope, EventKind, FallEvent

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings

USER = {"X-Mantau-User-ID": "family"}
OTHER = {"X-Mantau-User-ID": "stranger"}


def _setup(client):
    enrolled = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()
    assert client.post("/agent-claims", headers=USER, json={
        "claim_code": enrolled["claim_code"], "platform": "linux",
    }).status_code == 200
    assert client.post("/cameras", headers=USER, json={
        "camera_id": "cam-1", "name": "Kamar Ibu", "agent_id": "agent-1",
    }).status_code == 201
    return enrolled["secret"]


def _ingest(client, secret, seq, event):
    envelope = Envelope.for_event("agent-1", seq=seq, event=event).sign(secret)
    assert client.post("/ingest", json=envelope.model_dump(mode="json")).status_code == 200


def _app(tmp_path):
    return create_app(Settings(db_path=str(tmp_path / "ev.db"), control_plane_mode="local_dev"))


def test_event_history_pages_and_filters_by_kind(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        secret = _setup(client)
        kinds = [EventKind.FALL, EventKind.STILLNESS, EventKind.FALL,
                 EventKind.BATHROOM_DURATION, EventKind.FALL]
        for seq, kind in enumerate(kinds):
            _ingest(client, secret, seq, FallEvent(camera_id="cam-1", kind=kind,
                                                   signals={"duration_s": 60.0 * seq}))
        first = client.get("/events", headers=USER, params={"limit": 2}).json()
        assert [e["kind"] for e in first] == ["fall", "bathroom_duration"]
        assert first[0]["camera_name"] == "Kamar Ibu"
        rest = client.get("/events", headers=USER,
                          params={"limit": 10, "before": first[-1]["created_at"]}).json()
        assert [e["kind"] for e in rest] == ["fall", "stillness", "fall"]
        falls = client.get("/events", headers=USER, params={"kind": "fall"}).json()
        assert len(falls) == 3
        assert client.get("/events", headers=OTHER).json() == []


def test_ack_and_review_are_attributed_to_the_caller(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        secret = _setup(client)
        event = FallEvent(camera_id="cam-1")
        _ingest(client, secret, 0, event)

        first = client.post(f"/events/{event.event_id}/ack", headers=USER,
                            json={"member_id": "someone-else"})
        assert first.json()["first_ack"] is True
        again = client.post(f"/events/{event.event_id}/ack", headers=USER)
        assert again.json()["first_ack"] is False

        reviewed = client.post(f"/events/{event.event_id}/status", headers=USER,
                               json={"status": "confirmed"}).json()
        assert reviewed["status"] == "confirmed"
        assert reviewed["acknowledged_by"] == reviewed["reviewed_by"]
        assert reviewed["acknowledged_by"] != "someone-else"
        assert client.post(f"/events/{event.event_id}/status", headers=USER,
                           json={"status": "deleted"}).status_code == 400


def test_contacts_update_reorder_and_validate(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        ids = [client.post("/contacts", headers=USER, json={
            "name": name, "phone": phone, "relation": "Anak",
        }).json()["contact_id"] for name, phone in (("Andi", "+62811111"), ("Sari", "0812 2222"))]

        updated = client.put(f"/contacts/{ids[0]}", headers=USER, json={
            "name": "Andi P.", "phone": "+62811111", "relation": "Anak sulung",
        })
        assert updated.json()["name"] == "Andi P."

        ordered = client.put("/contacts/order", headers=USER,
                             json={"contact_ids": [ids[1], ids[0]]}).json()
        assert [c["contact_id"] for c in ordered] == [ids[1], ids[0]]
        assert client.put("/contacts/order", headers=USER,
                          json={"contact_ids": [ids[0]]}).status_code == 400

        assert client.post("/contacts", headers=USER, json={
            "name": "X", "phone": "not a phone", "relation": "Tetangga",
        }).status_code == 422
        assert client.put(f"/contacts/{ids[0]}", headers=OTHER, json={
            "name": "Hijack", "phone": "+62800000", "relation": "x",
        }).status_code == 404
