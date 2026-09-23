"""POST /agents/{id}/inference: signed frames in, server-side detection out.

The detector and JPEG decoder are replaced with deterministic fakes so the
route's own behavior -- authentication, size limits, idempotency, ordering,
freshness, isolation, event dispatch and HYBRID confirmation storage -- is
tested exactly. `test_real_detector_*` runs the real MediaPipe pipeline when
the `detection` extra is installed.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mantau_core.activity import FrameObservation, Perception, PersonObservation, Posture
from mantau_core.contracts import Envelope, FallEvent
from mantau_core.contracts import inference as contract

from mantau_ld.api.app import create_app
from mantau_ld.config import Settings

USER_A = {"X-Mantau-User-ID": "user-a"}
USER_B = {"X-Mantau-User-ID": "user-b"}


class FakeDetector:
    """Frame bytes drive the outcome: b"fall" confirms a fall, b"lying" shows a
    person lying down, b"standing" a person standing, anything else nobody."""

    instances: list["FakeDetector"] = []

    def __init__(self, camera_id: str, config: dict, clock) -> None:
        self.camera_id, self.config, self.clock = camera_id, config, clock
        self.frames: list[int] = []
        self.closed = False
        FakeDetector.instances.append(self)

    def perceive(self, image: bytes, ts_ms: int) -> Perception:
        self.frames.append(ts_ms)
        posture = {b"fall": Posture.LYING, b"lying": Posture.LYING,
                   b"standing": Posture.STANDING}.get(image)
        people = () if posture is None else (
            PersonObservation(track_id=1, bbox=(0.1, 0.5, 0.6, 0.3), posture=posture,
                              confidence=0.9),)
        events = [FallEvent(camera_id=self.camera_id, occurred_at=self.clock(), confidence=0.87,
                            track_id=1, signals={"velocity": 0.6})] if image == b"fall" else []
        return Perception(events=events, observation=FrameObservation(
            camera_id=self.camera_id, at=self.clock(), people=people))

    def close(self) -> None:
        self.closed = True


def _streams() -> list[FakeDetector]:
    """Detectors built for camera streams (not the startup availability probe)."""
    return [d for d in FakeDetector.instances if d.camera_id != "probe"]


def _decode(jpeg: bytes):
    return None if jpeg == b"not-an-image" else jpeg


def _client(**overrides) -> TestClient:
    FakeDetector.instances = []
    # Tests upload back to back; only the rate-limit test keeps a real fps cap.
    overrides.setdefault("inference_max_fps", 10_000.0)
    settings = Settings(db_path=":memory:", control_plane_mode="local_dev", **overrides)
    return TestClient(create_app(settings, inference_factory=FakeDetector,
                                 inference_decoder=_decode))


def _setup(client: TestClient, agent_id="agent-a", camera_id="cam-a", user=USER_A) -> str:
    enrolled = client.post("/agents/enroll", json={"agent_id": agent_id}).json()
    assert client.post("/agent-claims", headers=user, json={
        "claim_code": enrolled["claim_code"], "platform": "linux_x86_64"}).status_code == 200
    assert client.post("/cameras", headers=user, json={
        "camera_id": camera_id, "name": "Room", "agent_id": agent_id}).status_code == 201
    return enrolled["secret"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _upload(client: TestClient, secret: str, body: bytes = b"standing", *, agent_id="agent-a",
            path_agent=None, camera_id="cam-a", session_id="s1", frame_id=None, ts_ms=1000,
            captured_at_ms=None, event_ids=(), content_type="image/jpeg", sign_as=None):
    frame_id = frame_id or uuid.uuid4().hex
    captured_at_ms = _now_ms() if captured_at_ms is None else captured_at_ms
    fields = dict(agent_id=agent_id, camera_id=camera_id, session_id=session_id,
                  frame_id=frame_id, ts_ms=ts_ms, captured_at_ms=captured_at_ms,
                  event_ids=list(event_ids))
    signature = contract.sign(sign_as or secret, body=body, **fields)
    headers = {
        "Content-Type": content_type, "X-Mantau-Agent": agent_id, "X-Mantau-Camera": camera_id,
        "X-Mantau-Session": session_id, "X-Mantau-Frame": frame_id,
        "X-Mantau-Frame-Ts": str(ts_ms), "X-Mantau-Captured-At": str(captured_at_ms),
        "X-Mantau-Signature": signature,
    }
    if event_ids:
        headers["X-Mantau-Event-Ids"] = ",".join(event_ids)
    return client.post(f"/agents/{path_agent or agent_id}/inference", content=body,
                       headers=headers)


_SEQ = iter(range(1, 1_000_000))


def _ingest(client: TestClient, secret: str, *, agent_id="agent-a", camera_id="cam-a") -> str:
    event = FallEvent(camera_id=camera_id, confidence=0.9)
    envelope = Envelope.for_event(agent_id, seq=next(_SEQ), event=event).sign(secret)
    assert client.post("/ingest", json=envelope.model_dump(mode="json")).status_code == 200
    return event.event_id


# -- capability ---------------------------------------------------------------------

def test_capability_is_advertised():
    with _client() as client:
        cap = contract.InferenceCapability.model_validate(
            client.get("/inference/capability").json())
    assert cap.available and cap.detector == "mediapipe"
    assert cap.max_frame_bytes == 512 * 1024 and cap.max_fps == 10_000.0


def test_capability_reports_unavailable_detector_and_endpoint_refuses():
    def broken(*args):
        raise ImportError("no mantau")

    app = create_app(Settings(db_path=":memory:", control_plane_mode="local_dev"),
                     inference_factory=broken, inference_decoder=_decode)
    with TestClient(app) as client:
        cap = client.get("/inference/capability").json()
        assert cap["available"] is False and "ImportError" in cap["reason"]
        secret = _setup(client)
        assert _upload(client, secret).status_code == 503


def test_capability_reports_disabled():
    with _client(inference_enabled=False) as client:
        assert client.get("/inference/capability").json()["available"] is False


# -- authentication and limits ------------------------------------------------------

def test_signed_frame_is_processed():
    with _client() as client:
        secret = _setup(client)
        r = _upload(client, secret)
        assert r.status_code == 200, r.text
        result = contract.InferenceResult.model_validate(r.json())
    assert result.processed and not result.duplicate and result.people == 1
    assert result.events == []


@pytest.mark.parametrize("change", ["bad_secret", "path_mismatch", "tampered_body"])
def test_authentication_failures(change):
    with _client() as client:
        secret = _setup(client)
        _setup(client, "agent-b", "cam-b", USER_B)
        if change == "bad_secret":
            r = _upload(client, secret, sign_as="wrong-secret")
        elif change == "path_mismatch":
            r = _upload(client, secret, path_agent="agent-b")
        else:
            fields = dict(agent_id="agent-a", camera_id="cam-a", session_id="s1", frame_id="f1",
                          ts_ms=1, captured_at_ms=_now_ms(), event_ids=[])
            headers = {"Content-Type": "image/jpeg", "X-Mantau-Agent": "agent-a",
                       "X-Mantau-Camera": "cam-a", "X-Mantau-Session": "s1",
                       "X-Mantau-Frame": "f1", "X-Mantau-Frame-Ts": "1",
                       "X-Mantau-Captured-At": str(fields["captured_at_ms"]),
                       "X-Mantau-Signature": contract.sign(secret, body=b"standing", **fields)}
            r = client.post("/agents/agent-a/inference", content=b"fall", headers=headers)
        assert r.status_code == 401
        assert all(not d.frames for d in _streams())


def test_live_frame_signature_is_not_accepted():
    import hashlib
    import hmac
    with _client() as client:
        secret = _setup(client)
        good = _upload(client, secret, frame_id="f-live")
        assert good.status_code == 200
        frame_sig = hmac.new(secret.encode(), b"cam-a." + b"standing", hashlib.sha256).hexdigest()
        r = client.post("/agents/agent-a/inference", content=b"standing", headers={
            "Content-Type": "image/jpeg", "X-Mantau-Agent": "agent-a", "X-Mantau-Camera": "cam-a",
            "X-Mantau-Session": "s1", "X-Mantau-Frame": "f2", "X-Mantau-Frame-Ts": "2",
            "X-Mantau-Captured-At": str(_now_ms()), "X-Mantau-Signature": frame_sig})
        assert r.status_code == 401


def test_unclaimed_and_revoked_agents_are_refused():
    with _client() as client:
        unclaimed = client.post("/agents/enroll", json={"agent_id": "agent-u"}).json()
        assert _upload(client, unclaimed["secret"], agent_id="agent-u",
                       camera_id="cam-u").status_code == 401
        secret = _setup(client)
        assert client.delete("/agents/agent-a", headers=USER_A).status_code in (200, 204)
        assert _upload(client, secret).status_code == 401


def test_size_and_type_limits():
    with _client(inference_max_frame_bytes=64) as client:
        secret = _setup(client)
        assert _upload(client, secret, body=b"x" * 65).status_code == 413
        assert _upload(client, secret, content_type="image/png").status_code == 415
        assert _upload(client, secret, body=b"not-an-image").status_code == 422


def test_malformed_headers_are_rejected():
    with _client() as client:
        secret = _setup(client)
        assert _upload(client, secret, session_id="bad session").status_code == 400
        too_many = tuple(f"e{i}" for i in range(contract.MAX_EVENT_IDS + 1))
        assert _upload(client, secret, event_ids=too_many).status_code == 400


# -- freshness, ordering, idempotency ------------------------------------------------

def test_stale_and_future_frames_are_rejected():
    with _client() as client:
        secret = _setup(client)
        assert _upload(client, secret, captured_at_ms=_now_ms() - 11_000).status_code == 422
        assert _upload(client, secret, captured_at_ms=_now_ms() + 6_000).status_code == 422


def test_out_of_order_frames_are_not_processed():
    with _client() as client:
        secret = _setup(client)
        first = _upload(client, secret, ts_ms=2000)
        assert first.json()["processed"] is True, first.text
        late = _upload(client, secret, ts_ms=1500).json()
        assert late["processed"] is False and late["reason"] == "out_of_order"
        assert _streams()[0].frames == [2000]


def test_retried_frame_runs_the_detector_once_and_alerts_once():
    with _client() as client:
        secret = _setup(client)
        captured = _now_ms()
        first = _upload(client, secret, b"fall", frame_id="frame-1", captured_at_ms=captured)
        again = _upload(client, secret, b"fall", frame_id="frame-1", captured_at_ms=captured)
        assert first.status_code == again.status_code == 200
        assert first.json()["duplicate"] is False and again.json()["duplicate"] is True
        assert first.json()["events"] == again.json()["events"]
        assert _streams()[0].frames == [1000]
        assert len(client.get("/events", headers=USER_A).json()) == 1


def test_answered_frame_is_returned_even_after_it_went_stale():
    with _client(inference_max_frame_age_s=1.0) as client:
        secret = _setup(client)
        captured = _now_ms()
        assert _upload(client, secret, frame_id="frame-s", captured_at_ms=captured).status_code == 200
        time.sleep(1.2)
        r = _upload(client, secret, frame_id="frame-s", captured_at_ms=captured)
        assert r.status_code == 200 and r.json()["duplicate"] is True


def test_rate_limit_and_session_capacity():
    with _client(inference_max_fps=1.0, inference_max_sessions=1) as client:
        secret = _setup(client)
        assert _upload(client, secret, ts_ms=1).status_code == 200
        assert _upload(client, secret, ts_ms=2).status_code == 429
        assert _upload(client, secret, session_id="s2", ts_ms=3).status_code == 503


def test_idle_sessions_expire_and_close_their_detector():
    with _client(inference_session_idle_s=0.1, inference_max_sessions=1) as client:
        secret = _setup(client)
        assert _upload(client, secret, session_id="s1").status_code == 200
        time.sleep(0.2)
        assert _upload(client, secret, session_id="s2").status_code == 200
        assert _streams()[0].closed


# -- results through the existing event path -----------------------------------------

def test_server_detected_fall_is_stored_and_alerted_under_the_agent():
    with _client() as client:
        secret = _setup(client)
        captured = _now_ms()
        result = _upload(client, secret, b"fall", captured_at_ms=captured).json()
        assert len(result["events"]) == 1
        event = result["events"][0]
        assert event["signals"]["server_inference"] == 1.0
        stored = client.get(f"/events/{event['event_id']}", headers=USER_A).json()
        assert stored["camera_id"] == "cam-a"
        occurred = datetime.fromisoformat(stored["occurred_at"])
        assert abs(occurred.timestamp() * 1000 - captured) < 2
        latency = client.get(f"/events/{event['event_id']}/latency", headers=USER_A).json()
        assert latency["summary"]["captured"] == 0.0


def test_detector_clock_is_the_capture_time():
    with _client() as client:
        secret = _setup(client)
        captured = _now_ms() - 3000
        _upload(client, secret, captured_at_ms=captured)
        clock_value = _streams()[0].clock()
        assert abs(clock_value.timestamp() * 1000 - captured) < 2


def test_hybrid_confirmation_round_trip():
    with _client() as client:
        secret = _setup(client)
        event_id = _ingest(client, secret)
        confirmed = _upload(client, secret, b"lying", event_ids=(event_id,)).json()
        assert confirmed["confirmations"] == [{
            "schema_version": 1, "event_id": event_id, "confirmed": True, "confidence": 0.9,
            "reason": None}]
        stored = client.get(f"/events/{event_id}", headers=USER_A).json()
        assert stored["server_confirmed"] is True
        assert stored["server_confirmation_confidence"] == 0.9
        # Confirmation frames use their own detector with the motion gate off.
        confirm = [d for d in _streams() if d.config]
        assert confirm and confirm[0].config == {"motion": {"enabled": False}}

        second = _ingest(client, secret)
        rejected = _upload(client, secret, b"standing", event_ids=(second,), ts_ms=2000).json()
        assert rejected["confirmations"][0]["confirmed"] is False
        assert rejected["confirmations"][0]["reason"] == "nobody_lying"
        assert client.get(f"/events/{second}", headers=USER_A).json()["server_confirmed"] is False


def test_confirmation_before_the_event_arrives_is_kept():
    with _client() as client:
        secret = _setup(client)
        future_event = FallEvent(camera_id="cam-a", confidence=0.9)
        _upload(client, secret, b"lying", event_ids=(future_event.event_id,))
        envelope = Envelope.for_event("agent-a", seq=next(_SEQ), event=future_event).sign(secret)
        assert client.post("/ingest", json=envelope.model_dump(mode="json")).status_code == 200
        stored = client.get(f"/events/{future_event.event_id}", headers=USER_A).json()
        assert stored["server_confirmed"] is True


# -- household isolation -------------------------------------------------------------

def test_cross_household_camera_and_events_are_isolated():
    with _client() as client:
        secret_a = _setup(client)
        secret_b = _setup(client, "agent-b", "cam-b", USER_B)
        # Agent B cannot upload against household A's camera.
        assert _upload(client, secret_b, agent_id="agent-b", camera_id="cam-a").status_code == 404
        # Agent B cannot confirm (or learn about) household A's event.
        event_a = _ingest(client, secret_a)
        r = _upload(client, secret_b, b"lying", agent_id="agent-b", camera_id="cam-b",
                    event_ids=(event_a,)).json()
        assert r["confirmations"] == [{"schema_version": 1, "event_id": event_a,
                                       "confirmed": False, "confidence": 0.0,
                                       "reason": "unknown_event"}]
        assert client.get(f"/events/{event_a}", headers=USER_A).json()["server_confirmed"] is None
        # A fall B's frames produce belongs to household B only.
        fall = _upload(client, secret_b, b"fall", agent_id="agent-b", camera_id="cam-b",
                       ts_ms=5000).json()["events"][0]["event_id"]
        assert client.get(f"/events/{fall}", headers=USER_A).status_code == 404
        assert client.get(f"/events/{fall}", headers=USER_B).status_code == 200
        # Frame ids are scoped per agent: B reusing A's frame id is not a duplicate.
        a = _upload(client, secret_a, frame_id="same-id", ts_ms=9000).json()
        b = _upload(client, secret_b, agent_id="agent-b", camera_id="cam-b", frame_id="same-id",
                    ts_ms=9000).json()
        assert a["duplicate"] is False and b["duplicate"] is False


# -- the real detector ----------------------------------------------------------------

def test_real_detector_finds_a_fall_in_uploaded_frames():
    pytest.importorskip("mantau.api.streaming")
    cv2 = pytest.importorskip("cv2")
    clip = Path(__file__).resolve().parents[2] / "mantau-AI" / "data" / "falls" / "video_1.mp4"
    if not clip.exists():
        pytest.skip(f"{clip} not present")
    app = create_app(Settings(db_path=":memory:", control_plane_mode="local_dev",
                              inference_max_fps=60))
    with TestClient(app) as client:
        secret = _setup(client)
        cap = cv2.VideoCapture(str(clip))
        fps = cap.get(cv2.CAP_PROP_FPS)
        events, index = [], 0
        while True:
            ok, image = cap.read()
            if not ok:
                break
            ok, jpeg = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            r = _upload(client, secret, jpeg.tobytes(), ts_ms=int(index * 1000 / fps))
            assert r.status_code == 200, r.text
            events += r.json()["events"]
            index += 1
            time.sleep(1 / 60)
        cap.release()
    assert len(events) == 1
