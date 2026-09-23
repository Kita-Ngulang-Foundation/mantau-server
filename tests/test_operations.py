from __future__ import annotations

import json
import logging

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings


def _service_account(tmp_path) -> str:
    """A syntactically valid (never used) Firebase service-account key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({
        "type": "service_account", "project_id": "mantau-fce89",
        "private_key_id": "test", "private_key": pem,
        "client_email": "test@mantau-fce89.iam.gserviceaccount.com",
        "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token",
    }))
    return str(path)


def _production(tmp_path, **overrides) -> Settings:
    values = dict(
        db_path=str(tmp_path / "ops.db"),
        control_plane_mode="production",
        oidc_issuer="https://securetoken.google.com/mantau-fce89",
        oidc_audience="mantau-fce89",
        oidc_jwks_url="https://www.googleapis.com/service_accounts/v1/jwk/x",
        control_plane_encryption_key=Fernet.generate_key().decode(),
        fcm_project_id="mantau-fce89",
        fcm_service_account_path=_service_account(tmp_path),
    )
    values.update(overrides)
    return Settings(**values)


def test_ready_reports_missing_production_settings_by_name_only(tmp_path):
    settings = _production(
        tmp_path, oidc_issuer="", control_plane_encryption_key="not-a-fernet-key",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert "MANTAU_OIDC_ISSUER" in body["missing"]
    assert "MANTAU_CONTROL_PLANE_ENCRYPTION_KEY" in body["missing"]
    assert "not-a-fernet-key" not in response.text


def test_ready_is_ok_when_production_is_fully_configured(tmp_path):
    with TestClient(create_app(_production(tmp_path))) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    # Production never exposes per-agent tenant data on a public route.
    assert response.json() == {"status": "ok"}


def test_unknown_mode_is_not_ready(tmp_path):
    settings = Settings(db_path=str(tmp_path / "off.db"), control_plane_mode="disabled")
    with TestClient(create_app(settings)) as client:
        assert client.get("/ready").status_code == 503


def test_api_docs_are_hidden_in_production(tmp_path):
    with TestClient(create_app(_production(tmp_path))) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404


def test_api_docs_are_available_in_local_dev_or_when_enabled(tmp_path):
    local = Settings(db_path=str(tmp_path / "local.db"), control_plane_mode="local_dev")
    with TestClient(create_app(local)) as client:
        assert client.get("/openapi.json").status_code == 200
    enabled = _production(tmp_path, api_docs_enabled=True)
    with TestClient(create_app(enabled)) as client:
        assert client.get("/openapi.json").status_code == 200


def test_cors_is_off_unless_origins_are_configured(tmp_path):
    preflight = {
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "GET",
    }
    with TestClient(create_app(_production(tmp_path))) as client:
        response = client.options("/events", headers=preflight)
        assert "access-control-allow-origin" not in response.headers

    settings = _production(tmp_path, cors_origins="https://console.mantau.id")
    with TestClient(create_app(settings)) as client:
        allowed = client.options("/events", headers={
            **preflight, "Origin": "https://console.mantau.id",
            "Access-Control-Request-Headers": "authorization,x-mantau-household-id",
        })
        assert allowed.headers["access-control-allow-origin"] == "https://console.mantau.id"
        denied = client.options("/events", headers=preflight)
        assert denied.headers.get("access-control-allow-origin") != "https://evil.example"


def test_logs_never_contain_credentials(tmp_path, caplog):
    secrets = {
        "jwt": "eyJhbGciOiJSUzI1NiJ9.secret-jwt-payload.signature",
        "agent_secret": "agent-secret-value-123",
        "password": "camera-password-456",
        "fcm": "fcm-device-token-789",
    }
    caplog.set_level(logging.DEBUG)
    settings = Settings(db_path=str(tmp_path / "logs.db"), control_plane_mode="local_dev")
    with TestClient(create_app(settings)) as client:
        user = {"X-Mantau-User-ID": "family", "Authorization": f"Bearer {secrets['jwt']}"}
        client.get("/agents", headers=user)
        client.post("/devices/register", headers=user, json={
            "device_id": "install-1", "platform": "android", "token": secrets["fcm"],
        })
        client.post("/agent-control/commands/poll", headers={
            "X-Mantau-Agent-ID": "agent-x", "X-Mantau-Agent-Secret": secrets["agent_secret"],
        }, json={"status": {}})
        client.put("/agents/agent-x/camera", headers=user, json={
            "camera": {"host": "192.0.2.1", "port": 554, "main_path": "/main",
                       "name": "Room", "camera_id": "c"},
            "credentials": {"username": "admin", "password": secrets["password"]},
        })
        rejected = client.post("/cameras", headers=user, json={
            "camera_id": "c", "name": f"rtsp://admin:{secrets['password']}@192.0.2.1/main",
        })
        assert rejected.status_code == 422
        assert secrets["password"] not in rejected.text
    logged = caplog.text
    for value in secrets.values():
        assert value not in logged
