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


def _firebase_headers_with(uid: str, email: str) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": PROJECT, "sub": uid, "iat": now, "exp": now + 3600,
         "email": email, "name": uid.title()},
        KEY, algorithm="RS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_family_member_joins_with_a_single_use_invite(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        owner = _firebase_headers_with("ibu-owner", "anak@example.test")
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        invite = client.post(f"/households/{household}/invites", headers=owner, json={})
        assert invite.status_code == 201
        code = invite.json()["invite_code"]

        member = _firebase_headers_with("cucu", "cucu@example.test")
        joined = client.post("/households/join", headers=member, json={"invite_code": code.lower()})
        assert joined.status_code == 200
        assert joined.json()["household_id"] == household
        assert joined.json()["role"] == "member"

        # Single use: nobody else, and not the same user twice.
        other = _firebase_headers_with("stranger", "s@example.test")
        assert client.post("/households/join", headers=other,
                           json={"invite_code": code}).status_code == 404

        # The member now has two households and must choose.
        both = client.get("/households", headers=member).json()
        assert {h["household_id"] for h in both} >= {household}
        scoped = {**member, "X-Mantau-Household-ID": household}
        members = client.get(f"/households/{household}/members", headers=scoped).json()
        assert [(m["email"], m["role"], m["is_me"]) for m in members] == [
            ("anak@example.test", "owner", False), ("cucu@example.test", "member", True),
        ]


def test_members_cannot_invite_or_remove_others(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        owner = _firebase_headers_with("owner", "o@example.test")
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        code = client.post(f"/households/{household}/invites", headers=owner, json={}).json()["invite_code"]
        member = _firebase_headers_with("member", "m@example.test")
        client.post("/households/join", headers=member, json={"invite_code": code})
        scoped = {**member, "X-Mantau-Household-ID": household}

        assert client.post(f"/households/{household}/invites", headers=scoped,
                           json={}).status_code == 403
        owner_id = client.get(f"/households/{household}/members", headers=owner).json()[0]["user_id"]
        assert client.delete(f"/households/{household}/members/{owner_id}",
                             headers=scoped).status_code == 403
        assert client.patch(f"/households/{household}", headers=scoped,
                            json={"name": "Taken over"}).status_code == 403


def test_removed_member_stops_receiving_alerts(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        owner = _firebase_headers_with("owner", "o@example.test")
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        code = client.post(f"/households/{household}/invites", headers=owner, json={}).json()["invite_code"]
        member = _firebase_headers_with("member", "m@example.test")
        client.post("/households/join", headers=member, json={"invite_code": code})
        scoped = {**member, "X-Mantau-Household-ID": household}
        with sqlite3.connect(tmp_path / "households.db") as db:
            db.execute(
                "INSERT INTO cameras(camera_id,name,household_id,agent_id,registered_at) "
                "VALUES('cam-1','Kamar',?,NULL,0)", (household,),
            )
        for headers, device, token in ((owner, "dev-o", "tok-o"), (scoped, "dev-m", "tok-m")):
            assert client.post("/devices/register", headers=headers, json={
                "device_id": device, "platform": "android", "token": token,
            }).status_code == 204

        recipients = client.app.state.resolver.devices_for_camera
        assert sorted(t.token for t in recipients("cam-1")) == ["tok-m", "tok-o"]

        member_id = next(m["user_id"] for m in client.get(
            f"/households/{household}/members", headers=owner).json() if not m["is_me"])
        assert client.delete(f"/households/{household}/members/{member_id}",
                             headers=owner).status_code == 204
        assert [t.token for t in recipients("cam-1")] == ["tok-o"]
        assert client.get(f"/households/{household}/members",
                          headers=scoped).status_code == 404


def test_last_owner_cannot_leave(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        owner = _firebase_headers_with("owner", "o@example.test")
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        me = client.get(f"/households/{household}/members", headers=owner).json()[0]["user_id"]
        response = client.delete(f"/households/{household}/members/{me}", headers=owner)
        assert response.status_code == 409
        assert response.json() == {"detail": "last_owner"}


def test_invite_guessing_is_rate_limited(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        guesser = _firebase_headers_with("guesser", "g@example.test")
        codes = [client.post("/households/join", headers=guesser,
                             json={"invite_code": f"AAAA-BBBB-{i:04d}"}).status_code
                 for i in range(12)]
        assert codes[:10] == [404] * 10
        assert codes[10:] == [429, 429]


def test_owner_renames_the_household(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        owner = _firebase_headers_with("owner", "o@example.test")
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        assert client.patch(f"/households/{household}", headers=owner,
                            json={"name": "Rumah Ibu"}).status_code == 200
        assert client.get("/households", headers=owner).json()[0]["name"] == "Rumah Ibu"
