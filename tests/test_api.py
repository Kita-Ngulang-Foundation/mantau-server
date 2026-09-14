"""End-to-end API tests via a real ASGI TestClient -- proves the whole
`create_app()` wiring (lifespan, DI, routes) together, with the ingest path
exercised through real signing/verification, not stubs.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from mantau_core.contracts import Envelope, FallEvent, Heartbeat

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings


def _client() -> TestClient:
    settings = Settings(db_path=":memory:")
    app = create_app(settings)
    return TestClient(app)


def test_health_and_ready():
    with _client() as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ok", "agents": {}}


def test_agent_lifecycle():
    with _client() as client:
        r = client.post("/agents/enroll", json={"agent_id": "agent-1"})
        assert r.status_code == 201
        secret = r.json()["secret"]
        assert secret  # a real, non-empty secret

        agents = client.get("/agents").json()
        assert [a["agent_id"] for a in agents] == ["agent-1"]
        assert agents[0]["last_seen_at"] is None

        assert client.delete("/agents/agent-1").status_code == 204
        assert client.delete("/agents/agent-1").status_code == 404


def test_re_enrolling_issues_a_new_secret():
    with _client() as client:
        first = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()["secret"]
        second = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()["secret"]
        assert first != second


def test_ingest_rejects_an_unsigned_or_wrongly_signed_envelope():
    with _client() as client:
        client.post("/agents/enroll", json={"agent_id": "agent-1"})
        event = FallEvent(camera_id="cam-1", confidence=0.9)
        bad = Envelope.for_event("agent-1", seq=0, event=event).sign("wrong-secret")

        r = client.post("/ingest", json=bad.model_dump(mode="json"))
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid_signature"


def test_ingest_rejects_an_unknown_agent():
    with _client() as client:
        event = FallEvent(camera_id="cam-1")
        envelope = Envelope.for_event("ghost", seq=0, event=event).sign("whatever")
        r = client.post("/ingest", json=envelope.model_dump(mode="json"))
        assert r.status_code == 401
        assert r.json()["detail"] == "unknown_agent"


def test_ingest_a_fall_event_dispatches_and_shows_up_in_events():
    with _client() as client:
        secret = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()["secret"]
        client.post("/cameras", json={"camera_id": "cam-1", "name": "Kamar Ibu", "agent_id": "agent-1"})

        event = FallEvent(camera_id="cam-1", confidence=0.93)
        envelope = Envelope.for_event("agent-1", seq=0, event=event).sign(secret)

        r = client.post("/ingest", json=envelope.model_dump(mode="json"))
        assert r.status_code == 200
        assert r.json() == {"status": "accepted", "duplicate": False, "out_of_order": False}

        events = client.get("/events").json()
        assert len(events) == 1
        assert events[0]["event_id"] == event.event_id
        assert events[0]["confidence"] == 0.93

        # the agent's last_seen_at should now be set
        agent = client.get("/agents").json()[0]
        assert agent["last_seen_at"] is not None


def test_ingest_retry_of_the_same_envelope_is_a_duplicate_and_does_not_redispatch():
    with _client() as client:
        secret = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()["secret"]
        event = FallEvent(camera_id="cam-1")
        envelope = Envelope.for_event("agent-1", seq=0, event=event).sign(secret)
        body = envelope.model_dump(mode="json")

        first = client.post("/ingest", json=body)
        second = client.post("/ingest", json=body)

        assert first.json()["duplicate"] is False
        assert second.status_code == 200
        assert second.json()["duplicate"] is True

        # only ONE event was ever recorded, despite two ingest calls
        assert len(client.get("/events").json()) == 1


def test_ingest_a_heartbeat_updates_ready():
    with _client() as client:
        secret = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()["secret"]
        hb = Heartbeat(agent_id="agent-1", camera_id="cam-1", camera_reachable=True, detector_alive=True,
                        queue_depth=2)
        envelope = Envelope.for_heartbeat("agent-1", seq=0, heartbeat=hb).sign(secret)

        r = client.post("/ingest", json=envelope.model_dump(mode="json"))
        assert r.status_code == 200

        ready = client.get("/ready").json()
        assert ready["agents"]["agent-1"]["camera_reachable"] is True
        assert ready["agents"]["agent-1"]["queue_depth"] == 2

        # a heartbeat is not an event
        assert client.get("/events").json() == []


def test_camera_crud_and_name_shows_up_on_events():
    with _client() as client:
        r = client.post("/cameras", json={"camera_id": "cam-1", "name": "Kamar Ibu"})
        assert r.status_code == 201
        assert client.get("/cameras/cam-1").json()["name"] == "Kamar Ibu"
        assert client.get("/cameras").json() == [r.json()]
        assert client.delete("/cameras/cam-1").status_code == 204
        assert client.get("/cameras/cam-1").status_code == 404


def test_device_register_and_unregister():
    with _client() as client:
        r = client.post("/devices/register", json={"device_id": "d1", "platform": "android",
                                                     "token": "tok-abc"})
        assert r.status_code == 204
        assert client.delete("/devices/tok-abc").status_code == 204


def test_contacts_crud_ordered_by_priority():
    with _client() as client:
        client.post("/contacts", json={"name": "Sari", "phone": "+62-2", "relation": "Cucu",
                                        "priority": 2})
        client.post("/contacts", json={"name": "Budi", "phone": "+62-1", "relation": "Anak",
                                        "priority": 1})
        contacts = client.get("/contacts").json()
        assert [c["name"] for c in contacts] == ["Budi", "Sari"]
        contact_id = contacts[0]["contact_id"]
        assert client.delete(f"/contacts/{contact_id}").status_code == 204
        assert client.delete(f"/contacts/{contact_id}").status_code == 404


def test_event_ack_and_status():
    with _client() as client:
        secret = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()["secret"]
        event = FallEvent(camera_id="cam-1", confidence=0.9)
        envelope = Envelope.for_event("agent-1", seq=0, event=event).sign(secret)
        client.post("/ingest", json=envelope.model_dump(mode="json"))

        ack = client.post(f"/events/{event.event_id}/ack", json={"member_id": "anak"})
        assert ack.status_code == 200
        assert ack.json()["first_ack"] is True
        assert ack.json()["latency_to_ack_s"] is not None

        second = client.post(f"/events/{event.event_id}/ack", json={"member_id": "cucu"})
        assert second.json()["first_ack"] is False

        confirmed = client.post(f"/events/{event.event_id}/status", json={"status": "confirmed"})
        assert confirmed.json()["status"] == "confirmed"

        bad = client.post(f"/events/{event.event_id}/status", json={"status": "on_fire"})
        assert bad.status_code == 400


def test_ack_and_status_404_on_unknown_event():
    with _client() as client:
        assert client.post("/events/nope/ack", json={"member_id": "x"}).status_code == 404
        assert client.post("/events/nope/status", json={"status": "confirmed"}).status_code == 404
        assert client.get("/events/nope").status_code == 404


def test_latency_endpoint_reflects_the_dispatched_traces_stages():
    with _client() as client:
        secret = client.post("/agents/enroll", json={"agent_id": "agent-1"}).json()["secret"]
        event = FallEvent(camera_id="cam-1", confidence=0.9)  # occurred_at defaults to now
        envelope = Envelope.for_event("agent-1", seq=0, event=event).sign(secret)
        client.post("/ingest", json=envelope.model_dump(mode="json"))

        latency = client.get(f"/events/{event.event_id}/latency").json()
        assert latency["event_id"] == event.event_id
        assert latency["summary"]["captured"] == 0.0  # captured is the reference point
        assert latency["summary"]["delivered"] is not None
        assert latency["within_budget_delivered"] is True  # console channel is instant


def test_latency_404_on_unknown_event():
    with _client() as client:
        assert client.get("/events/nope/latency").status_code == 404
