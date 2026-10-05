from datetime import datetime, timezone

from mantau_core.contracts import FallEvent
from mantau_core.notify import Fanout, PushBinding
from mantau_core.notify.channels.push.tokens import DeviceToken, Platform
from mantau_core.notify.delivery import DeliveryTracker
from mantau_core.telemetry import Stage

from mantau_ld.alerts.dispatcher import AlertDispatcher
from mantau_ld.store.cameras_repo import CamerasRepo
import support
from mantau_ld.store.db import Database
from mantau_ld.store.events_repo import EventsRepo
from mantau_ld.store.recipient_resolver import SqliteRecipientResolver
from mantau_ld.store.sync_db import SyncDatabase
from mantau_ld.store.token_store import SqliteTokenStore


def _dispatcher(db, cameras):
    """Use the real production fanout and recipient registration seam."""
    sync_db = SyncDatabase(db.path)
    token_store = SqliteTokenStore(sync_db)
    token_store.register(DeviceToken(device_id="phone-1", platform=Platform.ANDROID,
                                     token="push-token", user_id="user-1",
                                     household_id="household-1"))
    notifier = support.RecordingNotifier()
    fanout = Fanout(channels=[PushBinding(notifier=notifier, resolver=SqliteRecipientResolver(
        sync_db, token_store))], tracker=DeliveryTracker())
    return AlertDispatcher(EventsRepo(db), cameras, fanout), notifier, sync_db


async def _setup_owned_camera(db: Database, camera_id: str, name: str) -> None:
    now = datetime.now(timezone.utc).timestamp()
    await db.conn.execute("INSERT INTO users(user_id,created_at) VALUES('user-1',?)", (now,))
    await db.conn.execute(
        "INSERT INTO households(household_id,name,created_at) VALUES('household-1','Home',?)",
        (now,),
    )
    await db.conn.execute(
        "INSERT INTO household_memberships(household_id,user_id,role,created_at) "
        "VALUES('household-1','user-1','owner',?)", (now,),
    )
    await db.conn.commit()
    await support.enroll_in_db(db, "agent-1")
    await CamerasRepo(db).create(
        camera_id, name, household_id="household-1", agent_id="agent-1"
    )


async def test_dispatch_resolves_camera_name_and_sends():
    db = Database(":memory:")
    await db.connect()
    dispatcher = sync_db = None
    try:
        cameras = CamerasRepo(db)
        await _setup_owned_camera(db, "cam-1", "Kamar Ibu")
        dispatcher, notifier, sync_db = _dispatcher(db, cameras)

        event = FallEvent(camera_id="cam-1", confidence=0.9,
                           occurred_at=datetime.now(timezone.utc))
        await dispatcher.dispatch(event, household_id="household-1", agent_id="agent-1")

        assert len(notifier.sent) == 1
        target, alert = notifier.sent[0]
        assert (target, alert.event_id, alert.camera_name, alert.household_id) == (
            "push-token", event.event_id, "Kamar Ibu", "household-1")
        assert await dispatcher.events_repo.get("household-1", event.event_id) is not None
        trace = dispatcher.get_trace(event.event_id)
        await dispatcher.dispatch(event, household_id="household-1", agent_id="agent-1")
        assert len(notifier.sent) == 1
        assert len(await dispatcher.events_repo.records("household-1")) == 1
        assert dispatcher.get_trace(event.event_id) is trace
        rows = await (await db.conn.execute("SELECT state,attempts FROM push_outbox")).fetchall()
        assert [(row["state"], row["attempts"]) for row in rows] == [("delivered", 1)]
    finally:
        if dispatcher is not None:
            await dispatcher.outbox.close()
        if sync_db is not None:
            sync_db.close()
        await db.close()


async def test_dispatch_can_use_raw_camera_id_as_display_name():
    db = Database(":memory:")
    await db.connect()
    dispatcher = sync_db = None
    try:
        await _setup_owned_camera(db, "cam-unregistered", "cam-unregistered")
        dispatcher, notifier, sync_db = _dispatcher(db, CamerasRepo(db))
        event = FallEvent(camera_id="cam-unregistered", confidence=0.9)
        await dispatcher.dispatch(event, household_id="household-1", agent_id="agent-1")
        assert len(notifier.sent) == 1
        assert notifier.sent[0][1].event_id == event.event_id
        assert notifier.sent[0][1].camera_name == "cam-unregistered"
    finally:
        if dispatcher is not None:
            await dispatcher.outbox.close()
        if sync_db is not None:
            sync_db.close()
        await db.close()


async def test_dispatch_stamps_captured_from_occurred_at():
    db = Database(":memory:")
    await db.connect()
    dispatcher = sync_db = None
    try:
        await _setup_owned_camera(db, "cam-1", "Room")
        dispatcher, _, sync_db = _dispatcher(db, CamerasRepo(db))
        occurred_at = datetime.fromtimestamp(1_000_000.0, tz=timezone.utc)
        event = FallEvent(camera_id="cam-1", occurred_at=occurred_at)
        await dispatcher.dispatch(event, household_id="household-1", agent_id="agent-1")

        trace = dispatcher.get_trace(event.event_id)
        assert trace is not None
        assert trace.has(Stage.CAPTURED)
        assert trace.has(Stage.QUEUED)
        assert trace.has(Stage.SENT)
        assert trace.has(Stage.DELIVERED)
        assert trace.to_summary()["captured"] == 0
        assert trace.elapsed(start=Stage.CAPTURED, end=Stage.DETECTED) == 0
        assert trace.within_budget(5.0) is not None  # measurable at all
    finally:
        if dispatcher is not None:
            await dispatcher.outbox.close()
        if sync_db is not None:
            sync_db.close()
        await db.close()
