"""Durable recipient delivery, restart recovery and authorization at retry time."""
from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest
from mantau_core.contracts import Envelope, FallEvent
from mantau_core.notify.channels.push.errors import PushDeliveryError, PushErrorKind
from mantau_core.notify.protocol import Delivery, DeliveryStatus
from mantau_core.telemetry import Stage

import support

OWNER = support.user("delivery-owner")
MEMBER = support.user("delivery-member")


class ScriptedNotifier(support.RecordingNotifier):
    def __init__(self, failures=0, kind=PushErrorKind.RETRYABLE):
        super().__init__()
        self.failures, self.kind = failures, kind
        self.attempts = []
        self.delivered = threading.Event()

    async def send(self, alert, target):
        self.attempts.append(target)
        if self.failures:
            self.failures -= 1
            raise PushDeliveryError(self.kind, "secret-bearing provider detail")
        self.sent.append((target, alert))
        self.delivered.set()
        return Delivery(event_id=alert.event_id, status=DeliveryStatus.DELIVERED)


async def _pause_worker(outbox):
    # Let a send already in progress persist its outcome before pausing.
    async with outbox.lock:
        outbox.worker.cancel()
    await asyncio.gather(outbox.worker, return_exceptions=True)
    outbox.worker = None


def _pause(client):
    client.portal.call(_pause_worker, client.app.state.dispatcher.outbox)


async def _sql(app, query, params=()):
    async with app.state.db.serialized():
        result = await app.state.db.conn.execute(query, params)
        rows = await result.fetchall()
        await app.state.db.conn.commit()
        return [dict(row) for row in rows]


def _query(client, query, params=()):
    return client.portal.call(_sql, client.app, query, params)


def _rows(client):
    return _query(client, "SELECT * FROM push_outbox ORDER BY id")


def _retry(client):
    _query(client, "UPDATE push_outbox SET next_attempt_at=0 WHERE state='pending'")
    client.portal.call(client.app.state.dispatcher.outbox.deliver_pending)


def _setup(client, *, member=False):
    enrolled = support.enroll(client, OWNER, agent_id="delivery-agent", camera_id="delivery-camera")
    household = client.get("/households", headers=OWNER).json()[0]["household_id"]
    recipient = OWNER
    if member:
        invite = client.post(f"/households/{household}/invites", headers=OWNER,
                             json={"role": "member"}).json()["invite_code"]
        assert client.post("/households/join", headers=MEMBER,
                           json={"invite_code": invite}).status_code == 200
        recipient = {**MEMBER, "X-Mantau-Household-ID": household}
    support.register_device(client, recipient, device_id="delivery-phone", token="delivery-token")
    event = FallEvent(camera_id="delivery-camera", confidence=0.9)
    envelope = Envelope.for_event(enrolled["agent_id"], seq=1, event=event).sign(enrolled["secret"])
    return enrolled, household, recipient, envelope


def _ingest(client, envelope):
    return client.post("/ingest", json=envelope.model_dump(mode="json"))


def test_transient_failure_retries_recipient_without_duplicate_history():
    notifier = ScriptedNotifier(failures=1)
    with support.client(push_notifier=notifier) as client:
        _pause(client)
        enrolled, household, _, envelope = _setup(client)
        assert _ingest(client, envelope).status_code == 200
        row = _rows(client)[0]
        assert (row["state"], row["attempts"], row["last_error"]) == ("pending", 1, "retryable")
        assert row["next_attempt_at"] > 0
        # Neither an envelope retry nor a new envelope for the same event
        # should send before backoff or add another history/outbox row.
        assert _ingest(client, envelope).json()["duplicate"] is True
        replay = Envelope.for_event(enrolled["agent_id"], seq=2,
                                    event=envelope.event()).sign(enrolled["secret"])
        assert _ingest(client, replay).status_code == 200
        assert notifier.attempts == ["delivery-token"]
        _retry(client)
        assert (_rows(client)[0]["state"], _rows(client)[0]["attempts"]) == ("delivered", 2)
        assert len(notifier.sent) == 1
        assert notifier.sent[0][1].household_id == household
        assert len(client.get("/events", headers=OWNER).json()) == 1
        assert len(_rows(client)) == 1
        _retry(client)
        assert len(notifier.attempts) == 2


@pytest.mark.parametrize("interrupted", [False, True])
def test_file_database_recovers_pending_or_interrupted_delivery_after_restart(tmp_path, interrupted):
    settings = support.settings(db_path=str(tmp_path / "delivery.db"), inference_enabled=False)
    first = ScriptedNotifier(failures=1)
    with support.client(settings, push_notifier=first) as client:
        _pause(client)
        _, _, _, envelope = _setup(client)
        assert _ingest(client, envelope).status_code == 200
        _query(client, "UPDATE push_outbox SET state=?,next_attempt_at=0",
               ("sending" if interrupted else "pending",))
    recovered = ScriptedNotifier()
    with support.client(settings, push_notifier=recovered) as client:
        assert recovered.delivered.wait(timeout=3), "startup did not resume durable delivery"
        _pause(client)
        client.portal.call(client.app.state.dispatcher.outbox.deliver_pending)
        assert len(recovered.sent) == 1
        assert (_rows(client)[0]["state"], _rows(client)[0]["attempts"]) == ("delivered", 2)
        assert len(client.get("/events", headers=OWNER).json()) == 1
        assert _ingest(client, envelope).json()["duplicate"] is True
        assert len(recovered.sent) == 1


@pytest.mark.parametrize("change", ["membership", "unregister", "rotate_token", "new_account"])
def test_pending_recipient_is_cancelled_when_membership_or_registration_changes(change):
    notifier = ScriptedNotifier(failures=1)
    with support.client(push_notifier=notifier) as client:
        _pause(client)
        _, household, recipient, envelope = _setup(client, member=True)
        assert _ingest(client, envelope).status_code == 200
        if change == "membership":
            members = client.get(f"/households/{household}/members", headers=OWNER).json()
            member_id = next(item["user_id"] for item in members if not item["is_me"])
            assert client.delete(f"/households/{household}/members/{member_id}",
                                 headers=OWNER).status_code == 204
        elif change == "rotate_token":
            support.register_device(client, recipient, device_id="delivery-phone", token="new-token")
        else:
            assert client.delete("/devices/delivery-phone", headers=recipient).status_code == 204
            if change == "new_account":
                support.register_device(client, support.user("different-account"),
                                        device_id="delivery-phone", token="delivery-token")
        _retry(client)
        assert _rows(client)[0]["state"] == "cancelled"
        assert _rows(client)[0]["attempts"] == 1
        assert notifier.attempts == ["delivery-token"]
        assert notifier.sent == []


@pytest.mark.parametrize("kind", [PushErrorKind.UNREGISTERED, PushErrorKind.INVALID])
def test_permanent_provider_failure_is_terminal_without_unbounded_retry(kind):
    notifier = ScriptedNotifier(failures=10, kind=kind)
    with support.client(push_notifier=notifier) as client:
        _pause(client)
        _, _, _, envelope = _setup(client)
        assert _ingest(client, envelope).status_code == 200
        row = _rows(client)[0]
        assert (row["state"], row["attempts"], row["last_error"]) == ("failed", 1, kind.value)
        _retry(client)
        assert notifier.attempts == ["delivery-token"]


def test_retry_budget_exhaustion_is_terminal():
    notifier = ScriptedNotifier(failures=100)
    with support.client(push_notifier=notifier) as client:
        _pause(client)
        _, _, _, envelope = _setup(client)
        assert _ingest(client, envelope).status_code == 200
        for _ in range(10):
            _retry(client)
        assert (_rows(client)[0]["state"], _rows(client)[0]["attempts"]) == ("failed", 8)
        assert len(notifier.attempts) == 8


def test_recipient_enqueue_failure_rolls_back_event_and_envelope_dedupe():
    notifier = ScriptedNotifier()
    with support.client(push_notifier=notifier) as client:
        _pause(client)
        _, _, _, envelope = _setup(client)
        support.register_device(client, OWNER, device_id="second-phone", token="second-token")
        _query(client, "CREATE TRIGGER fail_outbox BEFORE INSERT ON push_outbox "
               "WHEN NEW.target='second-token' BEGIN SELECT RAISE(ABORT,'injected enqueue failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected enqueue failure"):
            _ingest(client, envelope)
        assert _rows(client) == []
        assert _query(client, "SELECT * FROM events") == []
        assert _query(client, "SELECT * FROM ingested_envelopes") == []
        assert notifier.sent == []
        _query(client, "DROP TRIGGER fail_outbox")
        retried = _ingest(client, envelope)
        assert retried.status_code == 200, retried.text
        assert retried.json()["duplicate"] is False
        assert len(client.get("/events", headers=OWNER).json()) == 1
        assert len(_rows(client)) == len(notifier.sent) == 2


def test_latency_records_real_attempt_and_success_separately():
    notifier = ScriptedNotifier(failures=1)
    with support.client(push_notifier=notifier) as client:
        _pause(client)
        dispatcher = client.app.state.dispatcher
        dispatcher.outbox.trace_for = dispatcher.get_trace
        _, _, _, envelope = _setup(client)
        assert _ingest(client, envelope).status_code == 200
        trace = dispatcher.get_trace(envelope.event().event_id)
        assert trace.has(Stage.QUEUED) and trace.has(Stage.SENT)
        assert not trace.has(Stage.DELIVERED)
        sent_at = trace.to_summary()["sent"]
        _retry(client)
        assert trace.has(Stage.DELIVERED)
        assert trace.to_summary()["sent"] == sent_at


def test_network_delivery_is_rejected_inside_atomic_database_transaction():
    notifier = ScriptedNotifier()
    with support.client(push_notifier=notifier) as client:
        _pause(client)
        outbox = client.app.state.dispatcher.outbox

        async def attempt():
            async with outbox.db.transaction():
                await outbox.deliver_pending()

        with pytest.raises(RuntimeError, match="enclosing transaction commit"):
            client.portal.call(attempt)
        assert notifier.sent == []


def test_background_recovery_survives_a_storage_failure(monkeypatch):
    notifier = ScriptedNotifier()
    with support.client(push_notifier=notifier) as client:
        _pause(client)
        enrolled, household, _, envelope = _setup(client)
        outbox = client.app.state.dispatcher.outbox
        client.portal.call(outbox.enqueue, envelope.event(), household, enrolled["agent_id"])
        original = outbox.deliver_pending
        fail_once = True

        async def transient_storage_failure():
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise sqlite3.OperationalError("private storage path")
            await original()

        monkeypatch.setattr(outbox, "deliver_pending", transient_storage_failure)
        client.portal.call(outbox.start)
        assert notifier.delivered.wait(timeout=3), "recovery owner died after a storage failure"
        _pause(client)
        assert _rows(client)[0]["state"] == "delivered"
        assert len(notifier.sent) == 1
