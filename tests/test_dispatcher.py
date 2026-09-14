from datetime import datetime, timezone

from mantau_core.contracts import FallEvent
from mantau_core.telemetry import Stage

from mantau_ld.alerts.dispatcher import AlertDispatcher
from mantau_ld.store.cameras_repo import CamerasRepo
from mantau_ld.store.db import Database
from mantau_ld.store.events_repo import EventsRepo


class _FakeFanout:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send(self, event, *, camera_name: str, trace=None) -> list:
        self.sent.append((event, camera_name))
        if trace is not None:
            trace.stamp(Stage.QUEUED)
            trace.stamp(Stage.SENT)
            trace.stamp(Stage.DELIVERED)
        return []


async def test_dispatch_resolves_camera_name_and_sends():
    db = Database(":memory:")
    await db.connect()
    try:
        cameras = CamerasRepo(db)
        await cameras.create("cam-1", "Kamar Ibu")
        events_repo = EventsRepo(db)
        fanout = _FakeFanout()
        dispatcher = AlertDispatcher(events_repo, cameras, fanout)

        event = FallEvent(camera_id="cam-1", confidence=0.9,
                           occurred_at=datetime.now(timezone.utc))
        await dispatcher.dispatch(event)

        assert fanout.sent == [(event, "Kamar Ibu")]
        assert await events_repo.get(event.event_id) is not None
    finally:
        await db.close()


async def test_dispatch_falls_back_to_raw_camera_id_when_unregistered():
    db = Database(":memory:")
    await db.connect()
    try:
        fanout = _FakeFanout()
        dispatcher = AlertDispatcher(EventsRepo(db), CamerasRepo(db), fanout)
        event = FallEvent(camera_id="cam-unregistered", confidence=0.9)
        await dispatcher.dispatch(event)
        assert fanout.sent == [(event, "cam-unregistered")]
    finally:
        await db.close()


async def test_dispatch_stamps_captured_from_occurred_at():
    db = Database(":memory:")
    await db.connect()
    try:
        dispatcher = AlertDispatcher(EventsRepo(db), CamerasRepo(db), _FakeFanout())
        occurred_at = datetime.fromtimestamp(1_000_000.0, tz=timezone.utc)
        event = FallEvent(camera_id="cam-1", occurred_at=occurred_at)
        await dispatcher.dispatch(event)

        trace = dispatcher.get_trace(event.event_id)
        assert trace is not None
        assert trace.has(Stage.CAPTURED)
        assert trace.has(Stage.DELIVERED)
        assert trace.within_budget(5.0) is not None  # measurable at all
    finally:
        await db.close()
