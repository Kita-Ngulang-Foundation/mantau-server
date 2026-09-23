from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings
from mantau_ld.oidc_auth import OidcAuthenticator

# Firebase Authentication issues ID tokens with these exact shapes.
PROJECT = "mantau-fce89"
ISSUER = f"https://securetoken.google.com/{PROJECT}"
JWKS = "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _StaticKeyClient:
    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=KEY.public_key())


def _app(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "households.db"),
        control_plane_mode="production",
        oidc_issuer=ISSUER,
        oidc_audience=PROJECT,
        oidc_jwks_url=JWKS,
        oidc_algorithms="RS256",
        oidc_leeway_s=0,
    )
    authenticator = OidcAuthenticator(
        issuer=ISSUER, audience=PROJECT, jwks_url=JWKS, algorithms=["RS256"],
        leeway_s=0, key_client=_StaticKeyClient(),
    )
    return create_app(settings, oidc_authenticator=authenticator)


def _firebase_headers(uid: str) -> dict[str, str]:
    """A Firebase ID token: no `nbf`, plus Firebase's own claims."""
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER, "aud": PROJECT, "sub": uid, "user_id": uid,
            "auth_time": now, "iat": now, "exp": now + 3600,
            "email": f"{uid}@example.test", "email_verified": False,
            "firebase": {"identities": {}, "sign_in_provider": "password"},
        },
        KEY, algorithm="RS256", headers={"kid": "firebase-key"},
    )
    return {"Authorization": f"Bearer {token}"}


def _add_membership(tmp_path, user_headers_client, headers, household_id, name, role):
    """Adds a second household for the user created by the first request."""
    user_headers_client.get("/households", headers=headers)
    with sqlite3.connect(tmp_path / "households.db") as db:
        (user_id,) = db.execute("SELECT user_id FROM users").fetchone()
        db.execute(
            "INSERT INTO households(household_id,name,created_at) VALUES(?,?,?)",
            (household_id, name, time.time()),
        )
        db.execute(
            "INSERT INTO household_memberships(household_id,user_id,role,created_at) "
            "VALUES(?,?,?,?)",
            (household_id, user_id, role, time.time() + 1),
        )


def test_firebase_id_token_authenticates_and_creates_one_household(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        headers = _firebase_headers("firebase-uid-1")
        households = client.get("/households", headers=headers)
        assert households.status_code == 200
        assert [h["role"] for h in households.json()] == ["owner"]
        # A single membership needs no explicit selection.
        assert client.get("/agents", headers=headers).json() == []


def test_households_requires_authentication(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        assert client.get("/households").status_code == 401
        response = client.get("/households", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401
        assert response.json() == {"detail": "unauthorized"}


def test_several_households_are_listed_and_must_be_chosen(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        headers = _firebase_headers("firebase-uid-1")
        _add_membership(tmp_path, client, headers, "household-nenek", "Rumah Nenek", "member")

        listed = client.get("/households", headers=headers).json()
        assert [h["name"] for h in listed] == ["My household", "Rumah Nenek"]

        response = client.get("/agents", headers=headers)
        assert response.status_code == 400
        assert response.json() == {"detail": "household_required"}

        chosen = {**headers, "X-Mantau-Household-ID": "household-nenek"}
        assert client.get("/agents", headers=chosen).status_code == 200


def test_another_users_household_cannot_be_selected(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        owner = _firebase_headers("firebase-uid-owner")
        foreign_id = client.get("/households", headers=owner).json()[0]["household_id"]
        stranger = _firebase_headers("firebase-uid-stranger")
        assert all(
            h["household_id"] != foreign_id
            for h in client.get("/households", headers=stranger).json()
        )
        response = client.get(
            "/cameras", headers={**stranger, "X-Mantau-Household-ID": foreign_id}
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "resource_not_found"}
