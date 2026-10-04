from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from mantau_core.contracts import Envelope, FallEvent

import support
from mantau_ld.api.app import create_app

JPEG = b"test-jpeg"
OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_firebase_tokens_with_wrong_expiry_issuer_audience_or_signature_are_rejected(tmp_path):
    invalid_tokens = [
        support.token("user-a", expires_at=int(time.time()) - 60),
        support.token("user-a", issuer="https://securetoken.google.com/other-project"),
        support.token("user-a", audience="other-project"),
        support.token("user-a", key=OTHER_PRIVATE_KEY),
    ]
    with support.client(support.settings(db_path=str(tmp_path / "tokens.db"))) as client:
        for token in invalid_tokens:
            response = client.get("/agents", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401
            assert response.json() == {"detail": "unauthorized"}
            assert token not in response.text
        # The old development header is not an identity.
        assert client.get("/agents", headers={"X-Mantau-User-ID": "user-a"}).status_code == 401


def test_without_a_firebase_project_user_routes_fail_closed(tmp_path):
    settings = support.settings(db_path=str(tmp_path / "closed.db"), firebase_project_id="")
    with TestClient(create_app(settings)) as client:
        response = client.get("/agents", headers=support.user("user-a"))
        assert response.status_code == 503
        assert response.json() == {"detail": "authentication_unavailable"}


def test_cross_household_routes_and_notification_recipients_are_isolated(tmp_path):
    settings = support.settings(db_path=str(tmp_path / "identity.db"))
    with TestClient(support.app(settings)) as client:
        user_a = support.user("user-a")
        user_b = support.user("user-b")
        enrolled = support.enroll(client, user_a, agent_id="agent-a", camera_id="camera-a",
                                  camera_name="Room A")

        assert client.post("/devices/register", headers=user_a, json={
            "device_id": "device-a", "platform": "android", "token": "token-a",
        }).status_code == 204
        assert client.post("/devices/register", headers=user_b, json={
            "device_id": "device-b", "platform": "android", "token": "token-b",
        }).status_code == 204
        contact = client.post("/contacts", headers=user_a, json={
            "name": "Family A", "phone": "+62 811 0001", "relation": "Child", "priority": 1,
        }).json()

        event = FallEvent(camera_id="camera-a", confidence=0.91)
        envelope = Envelope.for_event("agent-a", seq=0, event=event).sign(enrolled["secret"])
        assert client.post("/ingest", json=envelope.model_dump(mode="json")).status_code == 200

        signature = hmac.new(
            enrolled["secret"].encode(), b"camera-a." + JPEG, hashlib.sha256
        ).hexdigest()
        assert client.post("/cameras/camera-a/frame", content=JPEG, headers={
            "X-Mantau-Agent": "agent-a", "X-Mantau-Signature": signature,
            "Content-Type": "image/jpeg",
        }).status_code == 204
        command = client.post(
            "/agents/agent-a/commands/restart",
            headers={**user_a, "Idempotency-Key": "restart-a"},
        ).json()

        assert client.get("/agents", headers=user_b).json() == []
        assert client.get("/agents/agent-a/setup", headers=user_b).status_code == 404
        assert client.delete("/agents/agent-a", headers=user_b).status_code == 404
        assert client.get("/cameras", headers=user_b).json() == []
        assert client.get("/cameras/camera-a", headers=user_b).status_code == 404
        assert client.post("/cameras", headers=user_b, json={
            "camera_id": "camera-a", "name": "Takeover", "agent_id": "agent-a",
        }).status_code == 404
        assert client.delete("/cameras/camera-a", headers=user_b).status_code == 404
        assert client.get("/events", headers=user_b).json() == []
        assert client.get(f"/events/{event.event_id}", headers=user_b).status_code == 404
        assert client.post(
            f"/events/{event.event_id}/status", headers=user_b, json={"status": "confirmed"}
        ).status_code == 404
        assert client.post(
            f"/events/{event.event_id}/ack", headers=user_b, json={"member_id": "user-b"}
        ).status_code == 404
        assert client.get(
            f"/events/{event.event_id}/latency", headers=user_b
        ).status_code == 404
        assert client.get("/contacts", headers=user_b).json() == []
        assert client.delete(
            f"/contacts/{contact['contact_id']}", headers=user_b
        ).status_code == 404
        assert client.get(
            f"/agents/agent-a/commands/{command['command_id']}", headers=user_b
        ).status_code == 404
        assert client.post(
            "/agents/agent-a/commands/restart",
            headers={**user_b, "Idempotency-Key": "takeover"},
        ).status_code == 404
        assert client.get(
            "/cameras/camera-a/snapshot.jpg", headers=user_b
        ).status_code == 404

        assert client.delete("/devices/device-a", headers=user_b).status_code == 204
        assert client.post("/devices/register", headers=user_b, json={
            "device_id": "device-a", "platform": "android", "token": "replacement",
        }).status_code == 204
        recipients = client.app.state.resolver.devices_for_camera("camera-a")
        assert [token.token for token in recipients] == ["token-a"]
        with sqlite3.connect(tmp_path / "identity.db") as db:
            assert db.execute(
                "SELECT COUNT(*) FROM device_tokens WHERE token='token-a'"
            ).fetchone()[0] == 1


def test_enrolled_agent_can_only_poll_and_submit_results_for_itself(tmp_path):
    user = support.user("user-a")
    with support.client(support.settings(db_path=str(tmp_path / "agent-scope.db"))) as client:
        agent_a = support.enroll(client, user, agent_id="agent-a", camera_id="camera-a")
        agent_b = support.enroll(client, user, agent_id="agent-b", camera_id="camera-b")
        command = client.post(
            "/agents/agent-a/commands/restart",
            headers={**user, "Idempotency-Key": "agent-a-only"},
        ).json()
        b_headers = {
            "X-Mantau-Agent-ID": "agent-b",
            "X-Mantau-Agent-Secret": agent_b["secret"],
        }
        assert client.post(
            "/agent-control/commands/poll", headers=b_headers, json={}
        ).status_code == 204
        assert client.post(
            f"/agent-control/commands/{command['command_id']}/results",
            headers=b_headers,
            json={"command_id": command["command_id"], "state": "succeeded"},
        ).status_code == 404
        a_headers = {
            "X-Mantau-Agent-ID": "agent-a",
            "X-Mantau-Agent-Secret": agent_a["secret"],
        }
        assert client.post(
            "/agent-control/commands/poll", headers=a_headers, json={}
        ).json()["command_id"] == command["command_id"]


def test_expired_enrollment_keys_and_revoked_agents_are_refused(tmp_path):
    user = support.user("user-a")
    settings = support.settings(db_path=str(tmp_path / "keys.db"), enrollment_key_ttl_s=0)
    with support.client(settings) as client:
        created = client.post("/enrollment-keys", headers=user).json()
        assert client.get(f"/enrollment-keys/{created['key_id']}",
                          headers=user).json()["status"] == "expired"
        assert client.post("/agents/enroll", json={
            "enrollment_key": created["enrollment_key"], "agent_id": "agent-x",
        }).status_code == 401

    with support.client(support.settings(db_path=str(tmp_path / "revoke.db"))) as client:
        enrolled = support.enroll(client, user, agent_id="agent-r")
        headers = support.agent_headers(enrolled)
        assert client.post("/agent-control/commands/poll", headers=headers,
                           json={}).status_code == 204
        assert client.delete("/agents/agent-r", headers=user).status_code == 204
        assert client.post("/agent-control/commands/poll", headers=headers,
                           json={}).status_code == 401
        # A revoked id stays taken.
        retry = client.post("/agents/enroll", json={
            "enrollment_key": support.enrollment_key(client, user), "agent_id": "agent-r",
        })
        assert retry.status_code == 409


def test_only_owners_and_admins_create_enrollment_keys(tmp_path):
    owner = support.user("owner")
    member = support.user("member")
    with support.client(support.settings(db_path=str(tmp_path / "roles.db"))) as client:
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        invite = client.post(f"/households/{household}/invites", headers=owner,
                             json={"role": "member"}).json()["invite_code"]
        assert client.post("/households/join", headers=member,
                           json={"invite_code": invite}).status_code == 200
        as_member = {**member, "X-Mantau-Household-ID": household}
        assert client.post("/enrollment-keys", headers=as_member).status_code == 403
        support.enroll(client, owner, agent_id="agent-o")
        assert client.delete("/agents/agent-o", headers=as_member).status_code == 403
        # Members still see the household's agents.
        assert [a["agent_id"] for a in client.get("/agents", headers=as_member).json()] == [
            "agent-o"]


def test_enrollment_rejects_malformed_agent_ids_and_keys(tmp_path):
    user = support.user("user-a")
    with support.client(support.settings(db_path=str(tmp_path / "ids.db"))) as client:
        key = support.enrollment_key(client, user)
        for bad in ("", "a", "../etc", "agent id", "x" * 65):
            assert client.post("/agents/enroll", json={
                "enrollment_key": key, "agent_id": bad}).status_code == 422
        assert client.post("/agents/enroll", json={"agent_id": "agent-1"}).status_code == 422
        assert client.post("/agents/enroll", json={
            "enrollment_key": "MTU-ÄÄÄÄÄ-ÄÄÄÄÄ-ÄÄÄÄÄ-ÄÄÄÄÄ", "agent_id": "agent-1",
        }).status_code == 401
