"""The live-view path: a signed frame push from the agent, read back by the app.

Uses a real ASGI TestClient and real HMAC signing (same as the ingest tests)
rather than stubbing the verification out -- a frame route that accepts an
unsigned body would be exactly the bug worth catching here.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi.testclient import TestClient

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings
from mantau_ld.frames import FrameStore

JPEG = b"\xff\xd8\xff\xe0not-a-real-jpeg-but-opaque-bytes\xff\xd9"


def _client() -> TestClient:
    return TestClient(create_app(Settings(db_path=":memory:")))


def _sign(secret: str, camera_id: str, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"), camera_id.encode("utf-8") + b"." + body, hashlib.sha256
    ).hexdigest()


def _enroll(client: TestClient, agent_id: str = "agent-1") -> str:
    return client.post("/agents/enroll", json={"agent_id": agent_id}).json()["secret"]


def _push(client: TestClient, secret: str, *, camera_id="cam-1", agent_id="agent-1", body=JPEG):
    return client.post(
        f"/cameras/{camera_id}/frame",
        content=body,
        headers={
            "Content-Type": "image/jpeg",
            "X-Mantau-Agent": agent_id,
            "X-Mantau-Signature": _sign(secret, camera_id, body),
        },
    )


def test_pushed_frame_is_served_back_as_a_snapshot():
    with _client() as client:
        secret = _enroll(client)
        assert _push(client, secret).status_code == 204

        r = client.get("/cameras/cam-1/snapshot.jpg")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content == JPEG
        # A cached "live" view is a frozen one.
        assert r.headers["cache-control"] == "no-store"


def test_latest_frame_wins():
    with _client() as client:
        secret = _enroll(client)
        _push(client, secret, body=b"older")
        _push(client, secret, body=b"newer")
        assert client.get("/cameras/cam-1/snapshot.jpg").content == b"newer"


def test_snapshot_404s_before_any_frame_arrives():
    with _client() as client:
        assert client.get("/cameras/never-pushed/snapshot.jpg").status_code == 404


def test_frame_push_rejects_a_bad_signature():
    with _client() as client:
        _enroll(client)
        r = client.post(
            "/cameras/cam-1/frame",
            content=JPEG,
            headers={
                "Content-Type": "image/jpeg",
                "X-Mantau-Agent": "agent-1",
                "X-Mantau-Signature": "0" * 64,
            },
        )
        assert r.status_code == 401
        assert client.get("/cameras/cam-1/snapshot.jpg").status_code == 404


def test_frame_push_rejects_an_unknown_agent():
    with _client() as client:
        r = _push(client, "some-secret-never-enrolled")
        assert r.status_code == 401


def test_store_exposes_the_current_frame_for_a_joining_viewer():
    """What the MJPEG stream relies on to greet a late-joining viewer.

    Regression: the stream used to only forward frames arriving AFTER the
    viewer connected, so joining a running camera showed nothing until the
    next push. It now seeds from `latest()`; this pins that behaviour.

    (The stream endpoint itself is exercised against real uvicorn rather than
    TestClient -- an endless multipart body deadlocks TestClient's portal on
    teardown.)
    """
    store = FrameStore()
    assert store.latest("cam-1") is None
    store.put("cam-1", JPEG)
    assert store.latest("cam-1").jpeg == JPEG
    assert store.is_live("cam-1")


def test_signature_is_bound_to_the_camera_it_was_signed_for():
    """A frame signed for cam-1 must not be replayable onto cam-2."""
    with _client() as client:
        secret = _enroll(client)
        r = client.post(
            "/cameras/cam-2/frame",
            content=JPEG,
            headers={
                "Content-Type": "image/jpeg",
                "X-Mantau-Agent": "agent-1",
                "X-Mantau-Signature": _sign(secret, "cam-1", JPEG),
            },
        )
        assert r.status_code == 401
