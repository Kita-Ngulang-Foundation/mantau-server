from fastapi.testclient import TestClient
from mantau_core.contracts import EventKind, FallEvent
import support
from test_recording_relay import _app, _agent, _setup, _ingest, OWNER, STRANGER


def test_only_current_agent_owned_non_bathroom_events_are_claimable(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        enrolled, own = _setup(client)
        other = support.enroll(client, STRANGER, agent_id="other-agent", camera_id="other-camera")
        foreign = FallEvent(camera_id="other-camera")
        bathroom = FallEvent(camera_id="cam-1", kind=EventKind.BATHROOM_DURATION)
        _ingest(client, other, foreign, 0)
        _ingest(client, enrolled, bathroom, 1)
        route = "/agent-control/recordings/legacy-check"
        assert client.post(route, json={"event_ids": [own.event_id]}).status_code == 401
        response = client.post(route, headers=_agent(enrolled),
            json={"event_ids": [own.event_id, foreign.event_id, bathroom.event_id, "unknown"]})
        assert response.status_code == 200
        assert [row["event_id"] for row in response.json()["recordings"]] == [own.event_id]
        assert response.json()["recordings"][0]["camera_id"] == "cam-1"
        assert response.json()["recordings"][0]["occurred_at_ms"] > 0
        assert not list((tmp_path / "recordings").rglob("*.mp4"))
        assert client.post(route, headers=_agent(enrolled),
            json={"event_ids": ["../escape"]}).status_code == 422
        assert client.post(route, headers=_agent(enrolled),
            json={"event_ids": [own.event_id]*6}).status_code == 422
        assert client.delete("/cameras/cam-1", headers=OWNER).status_code in (200, 204)
        assert client.post(route, headers=_agent(enrolled),
            json={"event_ids": [own.event_id]}).json()["recordings"] == []
