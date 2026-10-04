"""Shared test helpers: real RS256-signed Firebase-style ID tokens checked by
the real authenticator, and real enrollment-key enrollment."""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mantau_core.notify.alert import Alert
from mantau_core.notify.protocol import Delivery, DeliveryStatus

from mantau_ld.api.app import create_app
from mantau_ld.config import FIREBASE_JWKS_URL, Settings
from mantau_ld.oidc_auth import OidcAuthenticator

PROJECT_ID = "mantau-test"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StaticKeyClient:
    def __init__(self, public_key) -> None:
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=self.public_key)


class RecordingNotifier:
    """Stands in for FCM: records each (target token, alert) it is given."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Alert]] = []

    async def send(self, alert: Alert, target: str) -> Delivery:
        self.sent.append((target, alert))
        return Delivery(event_id=alert.event_id, status=DeliveryStatus.DELIVERED, detail=target)

    async def close(self) -> None:
        pass


def settings(**overrides) -> Settings:
    values = dict(db_path=":memory:", firebase_project_id=PROJECT_ID)
    values.update(overrides)
    return Settings(**values)


def authenticator(key=PRIVATE_KEY) -> OidcAuthenticator:
    return OidcAuthenticator(
        issuer=ISSUER, audience=PROJECT_ID, jwks_url=FIREBASE_JWKS_URL,
        algorithms=["RS256"], leeway_s=0, key_client=StaticKeyClient(key.public_key()),
    )


def app(app_settings: Settings | None = None, **kwargs):
    kwargs.setdefault("oidc_authenticator", authenticator())
    kwargs.setdefault("push_notifier", RecordingNotifier())
    return create_app(app_settings or settings(), **kwargs)


def client(app_settings: Settings | None = None, **kwargs) -> TestClient:
    return TestClient(app(app_settings, **kwargs))


def token(subject: str, *, email: str | None = None, name: str | None = None,
          issuer: str = ISSUER, audience: str = PROJECT_ID, key=PRIVATE_KEY,
          expires_at: int | None = None) -> str:
    now = int(time.time())
    claims = {
        "iss": issuer, "aud": audience, "sub": subject, "user_id": subject,
        "iat": now, "auth_time": now, "exp": expires_at if expires_at is not None else now + 3600,
        "firebase": {"sign_in_provider": "password"},
    }
    if email:
        claims["email"] = email
    if name:
        claims["name"] = name
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def user(subject: str, **claims) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(subject, **claims)}"}


def enrollment_key(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/enrollment-keys", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["enrollment_key"]


def enroll(client: TestClient, headers: dict[str, str], *, agent_id: str = "agent-1",
           platform: str = "linux_x86_64", camera_id: str | None = None,
           camera_name: str = "Kamar Ibu") -> dict:
    """Enroll `agent_id` into the household of `headers`' user. Returns
    `{"agent_id", "secret"}`; registers `camera_id` for it when given."""
    response = client.post("/agents/enroll", json={
        "enrollment_key": enrollment_key(client, headers),
        "agent_id": agent_id, "platform": platform,
    })
    assert response.status_code == 201, response.text
    if camera_id:
        assert client.post("/cameras", headers=headers, json={
            "camera_id": camera_id, "name": camera_name, "agent_id": agent_id,
        }).status_code == 201
    return response.json()


def register_device(client: TestClient, headers: dict[str, str], *, device_id: str = "phone-1",
                    token: str = "fcm-token-1") -> None:
    assert client.post("/devices/register", headers=headers, json={
        "device_id": device_id, "platform": "android", "token": token,
    }).status_code == 204


def agent_headers(enrolled: dict) -> dict[str, str]:
    return {"X-Mantau-Agent-ID": enrolled["agent_id"], "X-Mantau-Agent-Secret": enrolled["secret"]}


async def enroll_in_db(db, agent_id: str = "agent-1", *, household_id: str = "household-1",
                       user_id: str = "user-1"):
    """Repository-level enrollment: creates the user/household when missing,
    then a real enrollment key, and enrolls `agent_id` with it."""
    from mantau_ld.store.agents_repo import AgentsRepo

    now = time.time()
    await db.conn.execute("INSERT OR IGNORE INTO users(user_id,created_at) VALUES(?,?)",
                          (user_id, now))
    await db.conn.execute("INSERT OR IGNORE INTO households(household_id,name,created_at) "
                          "VALUES(?,?,?)", (household_id, "Home", now))
    await db.conn.execute("INSERT OR IGNORE INTO household_memberships(household_id,user_id,"
                          "role,created_at) VALUES(?,?,'owner',?)", (household_id, user_id, now))
    await db.conn.commit()
    repo = AgentsRepo(db)
    _, key = await repo.create_enrollment_key(household_id, user_id, ttl_s=60)
    return await repo.enroll(key, agent_id, name=agent_id, platform="linux_x86_64")
