from datetime import datetime, timezone

from mantau_core.contracts import EventKind, FallEvent, Severity
from mantau_core.notify.channels.push.tokens import DeviceToken, Platform
from mantau_core.notify.recipients import EmergencyContact

from mantau_ld.store.agents_repo import AgentsRepo
from mantau_ld.store.cameras_repo import CamerasRepo
from mantau_ld.store.db import Database
from mantau_ld.store.events_repo import EventsRepo
from mantau_ld.store.recipient_resolver import SqliteRecipientResolver
from mantau_ld.store.sync_db import SyncDatabase
from mantau_ld.store.token_store import SqliteTokenStore


async def _owned_agent(db: Database, *, household_id: str = "household-1",
                       user_id: str = "user-1", agent_id: str = "agent-1"):
    now = datetime.now(timezone.utc).timestamp()
    await db.conn.execute("INSERT INTO users(user_id,created_at) VALUES(?,?)", (user_id, now))
    await db.conn.execute(
        "INSERT INTO households(household_id,name,created_at) VALUES(?,?,?)",
        (household_id, "Test household", now),
    )
    await db.conn.execute(
        "INSERT INTO household_memberships(household_id,user_id,role,created_at) "
        "VALUES(?,?,'owner',?)", (household_id, user_id, now),
    )
    await db.conn.commit()
    agent = await AgentsRepo(db).enroll(agent_id)
    await db.conn.execute(
        "UPDATE agents SET household_id=? WHERE agent_id=?", (household_id, agent_id)
    )
    await db.conn.commit()
    return agent


async def test_agents_repo_enroll_get_touch_revoke():
    db = Database(":memory:")
    await db.connect()
    try:
        repo = AgentsRepo(db)
        agent = await _owned_agent(db)
        assert agent.secret  # a real, non-empty secret was generated
        assert agent.last_seen_at is None

        fetched = await repo.get("agent-1")
        assert fetched.secret == agent.secret

        await repo.touch("agent-1")
        touched = await repo.get("agent-1")
        assert touched.last_seen_at is not None

        assert len(await repo.list_all()) == 1
        assert await repo.revoke("agent-1", "household-1") is True
        assert (await repo.get("agent-1")).revoked_at is not None
        assert await repo.list_all() == []
    finally:
        await db.close()


async def test_agents_repo_re_enroll_issues_a_new_secret():
    db = Database(":memory:")
    await db.connect()
    try:
        repo = AgentsRepo(db)
        first = await repo.enroll("agent-1")
        second = await repo.enroll("agent-1", current_secret=first.secret)
        assert first.secret != second.secret
    finally:
        await db.close()


async def test_cameras_repo_name_for_falls_back_to_raw_id():
    db = Database(":memory:")
    await db.connect()
    try:
        repo = CamerasRepo(db)
        await _owned_agent(db)
        assert await repo.name_for("cam-unregistered", "household-1") == "cam-unregistered"
        await repo.create(
            "cam-1", "Kamar Ibu", household_id="household-1", agent_id="agent-1"
        )
        assert await repo.name_for("cam-1", "household-1") == "Kamar Ibu"
        assert (await repo.get("cam-1")).agent_id == "agent-1"
        assert await repo.delete("household-1", "cam-1") is True
        assert await repo.get("cam-1") is None
    finally:
        await db.close()


async def test_events_repo_insert_list_get_and_status():
    db = Database(":memory:")
    await db.connect()
    try:
        repo = EventsRepo(db)
        await _owned_agent(db)
        await CamerasRepo(db).create(
            "cam-1", "Room", household_id="household-1", agent_id="agent-1"
        )
        event = FallEvent(camera_id="cam-1", kind=EventKind.FALL, severity=Severity.CRITICAL,
                           occurred_at=datetime.now(timezone.utc), confidence=0.9, track_id=3,
                           signals={"velocity": 0.5})
        await repo.insert(event, household_id="household-1", agent_id="agent-1")

        fetched = await repo.get("household-1", event.event_id)
        assert fetched.signals["velocity"] == 0.5
        assert await repo.get_status("household-1", event.event_id) == "needs_review"
        assert await repo.set_status("household-1", event.event_id, "confirmed") is True
        assert await repo.get_status("household-1", event.event_id) == "confirmed"
        assert len(await repo.list_for_household("household-1")) == 1
    finally:
        await db.close()


async def test_token_store_and_resolver(tmp_path):
    db_path = tmp_path / "app.db"
    db = Database(str(db_path))
    await db.connect()
    try:
        await _owned_agent(db)
        await CamerasRepo(db).create(
            "cam-1", "Room", household_id="household-1", agent_id="agent-1"
        )
        sync_db = SyncDatabase(str(db_path))
        try:
            token_store = SqliteTokenStore(sync_db)
            token_store.register(DeviceToken(
                device_id="d1", platform=Platform.ANDROID, token="tok-1",
                user_id="user-1", household_id="household-1",
            ))
            resolver = SqliteRecipientResolver(sync_db, token_store)
            resolver.add_contact("household-1", EmergencyContact(
                contact_id="anak", name="Budi", phone="+62-1",
                relation="Anak", priority=1,
            ))

            assert [t.token for t in resolver.devices_for_camera("cam-1")] == ["tok-1"]
            assert [c.contact_id for c in resolver.emergency_contacts_for_camera("cam-1")] == ["anak"]
        finally:
            sync_db.close()
    finally:
        await db.close()
