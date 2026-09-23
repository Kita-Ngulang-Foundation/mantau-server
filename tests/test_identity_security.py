from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from mantau_core.contracts import Envelope, FallEvent

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings
from mantau_ld.oidc_auth import OidcAuthenticator


ISSUER = "https://identity.example.test/"
AUDIENCE = "mantau-app"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JPEG = b"test-jpeg"


class _StaticKeyClient:
    def __init__(self, public_key) -> None:
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=self.public_key)


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        db_path=str(tmp_path / "identity.db"),
        control_plane_mode="production",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url="https://identity.example.test/.well-known/jwks.json",
        oidc_algorithms="RS256",
        oidc_leeway_s=0,
    )
    values.update(overrides)
    return Settings(**values)


def _authenticator() -> OidcAuthenticator:
    return OidcAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://identity.example.test/.well-known/jwks.json",
        algorithms=["RS256"],
        leeway_s=0,
        key_client=_StaticKeyClient(PRIVATE_KEY.public_key()),
    )


def _token(
    subject: str,
    *,
    expires_at: int | None = None,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    key=PRIVATE_KEY,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "iat": now,
            "exp": expires_at if expires_at is not None else now + 300,
        },
        key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def _headers(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject)}"}


def _enroll_claim_camera(
    client: TestClient, headers: dict[str, str], *, agent_id: str = "agent-a",
    camera_id: str = "camera-a",
) -> dict:
    enrolled = client.post("/agents/enroll", json={"agent_id": agent_id}).json()
    assert client.post("/agent-claims", headers=headers, json={
        "claim_code": enrolled["claim_code"], "platform": "linux",
    }).status_code == 200
    assert client.post("/cameras", headers=headers, json={
        "camera_id": camera_id, "name": "Room A", "agent_id": agent_id,
    }).status_code == 201
    return enrolled


def test_oidc_rejects_expired_wrong_issuer_audience_and_signature(tmp_path):
    app = create_app(_settings(tmp_path), oidc_authenticator=_authenticator())
    invalid_tokens = [
        _token("user-a", expires_at=int(time.time()) - 60),
        _token("user-a", issuer="https://wrong.example.test/"),
        _token("user-a", audience="wrong-audience"),
        _token("user-a", key=OTHER_PRIVATE_KEY),
    ]
    with TestClient(app) as client:
        for token in invalid_tokens:
            response = client.get("/agents", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401
            assert response.json() == {"detail": "unauthorized"}
            assert token not in response.text


def test_production_does_not_accept_legacy_header_or_missing_oidc_configuration(tmp_path):
    settings = Settings(db_path=str(tmp_path / "closed.db"), control_plane_mode="production")
    with TestClient(create_app(settings)) as client:
        response = client.get("/agents", headers={"X-Mantau-User-ID": "legacy-user"})
        assert response.status_code == 503
        assert response.json() == {"detail": "authentication_unavailable"}


def test_legacy_identity_requires_explicit_local_development_mode(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "local.db"), control_plane_mode="local_dev"
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/agents").status_code == 401
        assert client.get(
            "/agents", headers={"X-Mantau-User-ID": "local-user"}
        ).json() == []


def test_cross_household_routes_and_notification_recipients_are_isolated(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, oidc_authenticator=_authenticator())) as client:
        user_a = _headers("user-a")
        user_b = _headers("user-b")
        enrolled = _enroll_claim_camera(client, user_a)

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
    user = {"X-Mantau-User-ID": "user-a"}
    settings = Settings(
        db_path=str(tmp_path / "agent-scope.db"), control_plane_mode="local_dev"
    )
    with TestClient(create_app(settings)) as client:
        agent_a = _enroll_claim_camera(
            client, user, agent_id="agent-a", camera_id="camera-a"
        )
        agent_b = _enroll_claim_camera(
            client, user, agent_id="agent-b", camera_id="camera-b"
        )
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


def test_agent_reenrollment_requires_current_identity_and_preserves_owner(tmp_path):
    user_a = {"X-Mantau-User-ID": "user-a"}
    user_b = {"X-Mantau-User-ID": "user-b"}
    settings = Settings(
        db_path=str(tmp_path / "takeover.db"), control_plane_mode="local_dev"
    )
    with TestClient(create_app(settings)) as client:
        enrolled = _enroll_claim_camera(
            client, user_a, agent_id="agent-a", camera_id="camera-a"
        )
        original_code = enrolled["claim_code"]
        # Without proof an existing id is simply taken; nothing changes.
        taken = client.post("/agents/enroll", json={"agent_id": "agent-a"})
        assert taken.status_code == 409
        assert taken.json() == {"detail": "agent_id_taken"}
        wrong = client.post("/agents/enroll", json={"agent_id": "agent-a"}, headers={
            "X-Mantau-Agent-ID": "agent-a", "X-Mantau-Agent-Secret": "wrong-secret",
        })
        assert wrong.status_code == 401
        assert "wrong-secret" not in wrong.text

        rotated = client.post("/agents/enroll", json={"agent_id": "agent-a"}, headers={
            "X-Mantau-Agent-ID": "agent-a",
            "X-Mantau-Agent-Secret": enrolled["secret"],
        })
        assert rotated.status_code == 201
        assert rotated.json()["claim_code"] is None
        assert client.get("/agents", headers=user_b).json() == []
        assert client.post("/agent-claims", headers=user_b, json={
            "claim_code": original_code, "platform": "linux",
        }).status_code == 404
        assert client.post("/agent-control/commands/poll", headers={
            "X-Mantau-Agent-ID": "agent-a",
            "X-Mantau-Agent-Secret": enrolled["secret"],
        }, json={}).status_code == 401
        assert client.post("/agent-control/commands/poll", headers={
            "X-Mantau-Agent-ID": "agent-a",
            "X-Mantau-Agent-Secret": rotated.json()["secret"],
        }, json={}).status_code == 204


def test_claims_expire_are_rate_limited_and_are_bound_to_current_enrollment(tmp_path):
    user = {"X-Mantau-User-ID": "user-a"}
    settings = Settings(
        db_path=str(tmp_path / "claims.db"), control_plane_mode="local_dev",
        claim_attempt_limit=2, claim_attempt_window_s=60,
    )
    with TestClient(create_app(settings)) as client:
        first = client.post("/agents/enroll", json={"agent_id": "agent-a"}).json()
        rotated = client.post("/agents/enroll", json={"agent_id": "agent-a"}, headers={
            "X-Mantau-Agent-ID": "agent-a", "X-Mantau-Agent-Secret": first["secret"],
        }).json()
        assert client.post("/agent-claims", headers=user, json={
            "claim_code": first["claim_code"], "platform": "linux",
        }).status_code == 404
        assert client.post("/agent-claims", headers=user, json={
            "claim_code": rotated["claim_code"], "platform": "linux",
        }).status_code == 200
        assert client.post("/agent-claims", headers=user, json={
            "claim_code": rotated["claim_code"], "platform": "linux",
        }).status_code == 404

    expired_settings = Settings(
        db_path=str(tmp_path / "expired-claim.db"), control_plane_mode="local_dev",
        claim_code_ttl_s=0,
    )
    with TestClient(create_app(expired_settings)) as client:
        expired = client.post("/agents/enroll", json={"agent_id": "agent-expired"}).json()
        assert client.post("/agent-claims", headers=user, json={
            "claim_code": expired["claim_code"], "platform": "linux",
        }).status_code == 404

    limited_settings = Settings(
        db_path=str(tmp_path / "limited-claim.db"), control_plane_mode="local_dev",
        claim_attempt_limit=2, claim_attempt_window_s=60,
    )
    with TestClient(create_app(limited_settings)) as client:
        for _ in range(2):
            assert client.post("/agent-claims", headers=user, json={
                "claim_code": "INVALID", "platform": "linux",
            }).status_code == 404
        assert client.post("/agent-claims", headers=user, json={
            "claim_code": "INVALID", "platform": "linux",
        }).status_code == 429


def test_unclaimed_agent_refreshes_its_claim_code_without_rotating(tmp_path):
    user = {"X-Mantau-User-ID": "user-a"}
    settings = Settings(db_path=str(tmp_path / "refresh.db"), control_plane_mode="local_dev")
    with TestClient(create_app(settings)) as client:
        enrolled = client.post("/agents/enroll", json={"agent_id": "agent-r"}).json()
        agent = {"X-Mantau-Agent-ID": "agent-r", "X-Mantau-Agent-Secret": enrolled["secret"]}

        assert client.post("/agent-control/claim-code").status_code == 401
        assert client.post("/agent-control/claim-code", headers={
            **agent, "X-Mantau-Agent-Secret": "wrong",
        }).status_code == 401

        fresh = client.post("/agent-control/claim-code", headers=agent)
        assert fresh.status_code == 201
        new_code = fresh.json()["claim_code"]
        assert new_code != enrolled["claim_code"]

        # The superseded code no longer works; the fresh one does, once.
        assert client.post("/agent-claims", headers=user, json={
            "claim_code": enrolled["claim_code"], "platform": "linux",
        }).status_code == 404
        assert client.post("/agent-claims", headers=user, json={
            "claim_code": new_code, "platform": "linux",
        }).status_code == 200

        # The secret still works: refreshing never rotates the credential.
        assert client.post("/agent-control/commands/poll", headers=agent,
                           json={}).status_code == 204
        # A claimed agent never receives another code.
        again = client.post("/agent-control/claim-code", headers=agent)
        assert again.status_code == 409
        assert again.json() == {"detail": "agent_already_claimed"}


def test_enrollment_rejects_malformed_agent_ids(tmp_path):
    settings = Settings(db_path=str(tmp_path / "ids.db"), control_plane_mode="local_dev")
    with TestClient(create_app(settings)) as client:
        for bad in ("", "a", "../etc", "agent id", "x" * 65):
            assert client.post("/agents/enroll", json={"agent_id": bad}).status_code == 422
