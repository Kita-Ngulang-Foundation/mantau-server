"""Authenticated upload -> candidate history -> exactly-once push or no push."""
from mantau_core.activity import Perception
from mantau_core.contracts import FallEvent, Severity
import asyncio
from datetime import datetime, timezone, timedelta
import support
from test_inference import FakeDetector, _decode, _setup, _upload, USER_A, USER_B


class CandidateDetector(FakeDetector):
    def __init__(self, *args):
        super().__init__(*args)
        self.event_id = "candidate-" + self.camera_id
        self.at = None
        self.done = False

    def event(self, outcome):
        return FallEvent(event_id=self.event_id, camera_id=self.camera_id,
            occurred_at=self.at, track_id=1, confidence=0.99,
            severity=Severity.CRITICAL if outcome == 1 else Severity.INFO,
            signals={"research_candidate": 1, "candidate_outcome": outcome,
                     "peak_p": 0.99, "tier": 1, "down_fraction": 1,
                     "confirm_s": 5, "confirmation_elapsed_s": 5 if outcome else 0,
                     "bbox_x1": 0.1, "bbox_y1": 0.5, "bbox_x2": 0.7, "bbox_y2": 0.8})

    def perceive(self, image, ts_ms):
        result = super().perceive(image, ts_ms)
        if self.at is None:
            self.at = self.clock()
            events = [self.event(0)]
        elif image == b"standing":
            events = [self.event(2)]
            self.done = True
        elif ts_ms >= 6000:
            events = [self.event(1)]
            self.done = True
        else:
            events = []
        return Perception(events, result.observation)

    def advance(self, ts_ms):
        if not self.done and self.at is not None and ts_ms >= 6000:
            self.done = True
            event = self.event(1)
            event.signals["track_lost_down"] = 1
            return [event]
        return []


def client_and_notifier():
    notifier = support.RecordingNotifier()
    client = support.client(support.settings(inference_max_fps=10000),
        inference_factory=CandidateDetector, inference_decoder=_decode, push_notifier=notifier)
    return client, notifier


def register(client):
    response = client.post("/devices/register", headers=USER_A,
        json={"device_id": "family-phone", "token": "local-recipient", "platform": "android"})
    status = response.status_code
    assert status == 204


def test_recovered_candidate_is_history_only_and_household_scoped():
    client, notifier = client_and_notifier()
    with client:
        key = _setup(client)
        _setup(client, "agent-b", "cam-b", USER_B)
        register(client)
        first = _upload(client, key, b"lying", ts_ms=1000)
        assert first.status_code == 200
        assert not notifier.sent
        result = _upload(client, key, b"standing", ts_ms=2000)
        assert result.status_code == 200
        history = client.get("/events", headers=USER_A).json()
        assert len(history) == 1 and history[0]["severity"] == "info"
        assert history[0]["signals"]["candidate_outcome"] == 2
        assert not notifier.sent
        assert client.get("/events/candidate-cam-a", headers=USER_B).status_code == 404


def test_confirmed_candidate_updates_same_history_row_and_pushes_once():
    client, notifier = client_and_notifier()
    with client:
        key = _setup(client)
        register(client)
        _upload(client, key, b"lying", ts_ms=1000)
        first = client.get("/events", headers=USER_A).json()[0]
        result = _upload(client, key, b"lying", ts_ms=6000, frame_id="confirmed-frame")
        assert result.status_code == 200
        # Dispatch replay exercises the durable event-id boundary, independently
        # of the signed-frame response cache.
        event = FallEvent.model_validate(result.json()["events"][0])
        async def dispatch_again():
            camera = await client.app.state.cameras_repo.get("cam-a")
            await client.app.state.dispatcher.dispatch(event, household_id=camera.household_id, agent_id=camera.agent_id)
        client.portal.call(dispatch_again)
        history = client.get("/events", headers=USER_A).json()
        assert len(history) == 1 and history[0]["severity"] == "critical"
        assert history[0]["created_at"] == first["created_at"]
        assert len(notifier.sent) == 1


def test_timer_confirms_and_persists_when_no_more_uploads_arrive():
    client, notifier = client_and_notifier()
    with client:
        key = _setup(client)
        register(client)
        _upload(client, key, b"lying", ts_ms=1000)
        async def expire():
            service = client.app.state.inference
            session = next(iter(service._sessions.values()))
            session.last_frame_monotonic -= 5.01
            await service.tick()
            await client.app.state.dispatcher.outbox.deliver_pending()
        client.portal.call(expire)
        history = client.get("/events", headers=USER_A).json()
        assert history[0]["signals"]["track_lost_down"] == 1
        assert history[0]["severity"] == "critical"
        assert len(notifier.sent) == 1


def test_saved_settings_reach_detector_and_rollback_applies_on_next_frame():
    client, _ = client_and_notifier()
    with client:
        key = _setup(client)
        loaded = client.get("/cameras/cam-a/detection-settings", headers=USER_A).json()["settings"]
        loaded["fall"].update(tau_loose=0.94, tau_high=0.99, confirm_high_s=6, model="s1b")
        assert client.put("/cameras/cam-a/detection-settings", headers=USER_A, json=loaded).status_code == 200
        assert _upload(client, key, b"lying", ts_ms=1000).status_code == 200
        session = next(iter(client.app.state.inference._sessions.values()))
        assert session.stream.config["fall_model"] == "s1b"
        assert session.stream.config["fall_confirmation"]["tau_loose"] == 0.94


def test_restart_recovers_a_durable_down_candidate_and_keeps_original_deadline(tmp_path):
    db_path = str(tmp_path / "candidate-restart.db")
    notifier = support.RecordingNotifier()
    def new_client():
        return support.client(support.settings(db_path=db_path, inference_max_fps=10000),
            inference_factory=CandidateDetector, inference_decoder=_decode, push_notifier=notifier)
    with new_client() as client:
        key = _setup(client)
        register(client)
        _upload(client, key, b"lying", ts_ms=1000)
        async def prepare_expired():
            db = client.app.state.db
            async with db.transaction():
                await db.conn.execute("UPDATE events SET occurred_at=?,signals_json=json_set(signals_json,'$.last_down',1) "
                    "WHERE event_id=?", ((datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat(), "candidate-cam-a"))
        client.portal.call(prepare_expired)
        assert not notifier.sent
    with new_client() as client:
        client.portal.call(asyncio.sleep, 0.2)
        client.portal.call(client.app.state.dispatcher.outbox.deliver_pending)
        history = client.get("/events", headers=USER_A).json()
        assert len(history) == 1 and history[0]["severity"] == "critical"
        assert history[0]["signals"]["recovered_after_restart"] == 1
        assert len(notifier.sent) == 1


def test_terminal_candidate_cannot_be_downgraded_by_a_late_pending_snapshot():
    client, notifier = client_and_notifier()
    with client:
        key = _setup(client)
        register(client)
        pending = _upload(client, key, b"lying", ts_ms=1000).json()["events"][0]
        _upload(client, key, b"lying", ts_ms=6000)
        async def replay_pending():
            camera = await client.app.state.cameras_repo.get("cam-a")
            await client.app.state.dispatcher.dispatch(FallEvent.model_validate(pending),
                household_id=camera.household_id, agent_id=camera.agent_id)
        client.portal.call(replay_pending)
        history = client.get("/events", headers=USER_A).json()[0]
        assert history["severity"] == "critical" and history["signals"]["candidate_outcome"] == 1
        assert len(notifier.sent) == 1
