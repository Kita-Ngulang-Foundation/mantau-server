"""One durable delivery owner. Event history and recipient work commit together."""
from __future__ import annotations

import asyncio
import logging
import time

from mantau_core.contracts import EventKind, Severity
from mantau_core.notify import PushBinding
from mantau_core.notify.alert import Alert
from mantau_core.notify.templates import render
from mantau_core.notify.protocol import Delivery, DeliveryStatus
from mantau_core.notify.channels.push.errors import PushDeliveryError, PushErrorKind
from mantau_core.telemetry import Stage

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


def silent_marker(event) -> bool:
    """A night summary without bed exits: the coverage marker that lets the app
    tell a calm night from an unmonitored one. Stored, never pushed."""
    return (event.kind is EventKind.NOCTURNAL_MOVEMENT and event.severity is Severity.INFO
            and event.signals.get("summary") == 1.0 and event.signals.get("bed_exits") == 0.0)


class PushOutbox:
    def __init__(self, events, cameras, fanout):
        self.db, self.events, self.cameras, self.fanout = events._db, events, cameras, fanout
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.worker = None
        self.trace_for = None

    def _stamp(self, event_id, stage):
        trace = self.trace_for(event_id) if self.trace_for is not None else None
        if trace is not None and not trace.has(stage):
            trace.stamp(stage)

    async def start(self):
        if self.worker is not None and not self.worker.done():
            return
        await self._recover_sending()
        self.worker = asyncio.create_task(self.run(), name='push-outbox')

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        for binding in self.fanout.channels:
            await binding.notifier.close()

    async def _recover_sending(self):
        async with self.lock, self.db.serialized():
            await self.db.conn.execute(
                "UPDATE push_outbox SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END,"
                "next_attempt_at=? WHERE state='sending'", (MAX_ATTEMPTS, time.time()))
            await self.db.conn.commit()

    async def enqueue(self, event, household_id, agent_id):
        async with self.db.transaction():
            inserted = await self.events.insert(event, household_id=household_id, agent_id=agent_id)
            if not inserted:
                return False
            if silent_marker(event):
                return True  # history only: the app's night calendar reads it
            name = await self.cameras.name_for(event.camera_id, household_id)
            title, body = render(event, camera_name=name)
            alert = Alert.from_event(event, camera_name=name, title=title, body=body)
            alert.household_id = household_id
            for channel, binding in enumerate(self.fanout.channels):
                # Resolve production recipients on the atomic connection; the
                # synchronous resolver cannot share this membership snapshot.
                targets = None if isinstance(binding, PushBinding) else set(
                    binding.targets_for_camera(event.camera_id))
                recipients = await (await self.db.conn.execute(
                    "SELECT d.token,d.user_id,d.device_id FROM device_tokens d "
                    "JOIN household_memberships m ON m.user_id=d.user_id "
                    "WHERE m.household_id=?", (household_id,))).fetchall()
                for recipient in recipients:
                    if targets is not None and recipient['token'] not in targets:
                        continue
                    await self.db.conn.execute(
                        "INSERT OR IGNORE INTO push_outbox(event_id,household_id,channel,target,"
                        "alert_json,user_id,device_id,state,attempts,next_attempt_at) "
                        "VALUES(?,?,?,?,?,?,?,'pending',0,0)",
                        (event.event_id, household_id, channel, recipient['token'],
                         alert.model_dump_json(), recipient['user_id'], recipient['device_id']))
        self.wake.set()
        self._stamp(event.event_id, Stage.QUEUED)
        return True

    async def deliver_pending(self):
        if self.db.in_atomic:
            raise RuntimeError("Push delivery must follow the enclosing transaction commit")
        async with self.lock:
            async with self.db.serialized():
                rows = await (await self.db.conn.execute(
                    "SELECT * FROM push_outbox WHERE state='pending' AND next_attempt_at<=? "
                    "ORDER BY next_attempt_at,id LIMIT 50", (time.time(),))).fetchall()
            for row in rows:
                attempts = row['attempts'] + 1
                async with self.db.transaction():
                    owned = await (await self.db.conn.execute(
                        "SELECT 1 FROM device_tokens d JOIN household_memberships m ON m.user_id=d.user_id "
                        "WHERE d.device_id=? AND d.user_id=? AND d.token=? AND m.household_id=?",
                        (row['device_id'], row['user_id'], row['target'], row['household_id']))).fetchone()
                    state = ('sending' if owned and 0 <= row['channel'] < len(self.fanout.channels)
                             else 'cancelled')
                    if row['attempts'] >= MAX_ATTEMPTS:
                        state = 'failed'
                    # Count attempts before network I/O, including process crashes.
                    claimed = await self.db.conn.execute(
                        "UPDATE push_outbox SET state=?,attempts=? WHERE id=? AND state='pending'",
                        (state, attempts if state == 'sending' else row['attempts'], row['id']))
                if state != 'sending' or claimed.rowcount != 1:
                    continue
                state, reason = 'delivered', None
                self._stamp(row['event_id'], Stage.SENT)
                try:
                    delivery = await asyncio.wait_for(self.fanout.channels[row['channel']].notifier.send(
                        Alert.model_validate_json(row['alert_json']), row['target']), timeout=12)
                    if delivery.status is not DeliveryStatus.DELIVERED:
                        state, reason = 'pending', 'delivery_failed'
                    else:
                        self._stamp(row['event_id'], Stage.DELIVERED)
                except PushDeliveryError as exc:
                    reason = exc.kind.value
                    state = 'failed' if exc.kind in (PushErrorKind.UNREGISTERED, PushErrorKind.INVALID) else 'pending'
                    delivery = Delivery(event_id=row['event_id'], status=DeliveryStatus.FAILED,
                                        detail=reason)
                except Exception as exc:
                    state, reason = 'pending', type(exc).__name__
                    delivery = Delivery(event_id=row['event_id'], status=DeliveryStatus.FAILED,
                                        detail=reason)
                self.fanout.tracker.record(delivery, target=row['target'])
                if state == 'pending' and attempts >= MAX_ATTEMPTS:
                    state = 'failed'
                async with self.db.serialized():
                    await self.db.conn.execute(
                        "UPDATE push_outbox SET state=?,next_attempt_at=?,last_error=? WHERE id=?",
                        (state, time.time()+min(300, 2 ** attempts), reason, row['id']))
                    await self.db.conn.commit()

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.deliver_pending()
            except Exception as exc:
                # Storage failures must not kill the sole recovery owner.
                log.error("push outbox recovery failed (%s)", type(exc).__name__)
                try:
                    await self._recover_sending()
                except Exception as recovery_error:
                    log.error("push outbox storage unavailable (%s)", type(recovery_error).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass
