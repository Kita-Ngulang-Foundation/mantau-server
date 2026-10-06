"""Retention preserves current alerts and actually removes expired clip files."""
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from mantau_core.contracts import FallEvent

import support
import mantau_ld.store.events_repo as events_module
from mantau_ld.store.cameras_repo import CamerasRepo
from mantau_ld.store.agents_repo import AgentsRepo
from mantau_ld.store.db import Database
from mantau_ld.store.events_repo import EventsRepo
from mantau_ld.store.recordings_repo import RecordingsRepo

NOW = 1_900_000_000.0
DAY = 86400


async def _seed(db, root, *, household="household-1", label="old", age_days=31, clip=True,
                state="pending"):
    agent_id, camera_id = f"agent-{household}", f"camera-{household}"
    if await AgentsRepo(db).get(agent_id) is None:
        await support.enroll_in_db(db, agent_id, household_id=household, user_id=f"user-{household}")
    await CamerasRepo(db).create(camera_id, "Room", household_id=household, agent_id=agent_id)
    event = FallEvent(event_id=f"{household}-{label}", camera_id=camera_id, confidence=0.9)
    await EventsRepo(db).insert(event, household_id=household, agent_id=agent_id)
    await db.conn.execute("UPDATE events SET created_at=? WHERE event_id=?",
                          (NOW - age_days * DAY, event.event_id))
    await db.conn.execute(
        "INSERT INTO push_outbox(event_id,household_id,channel,target,alert_json,user_id,device_id,state) "
        "VALUES(?,?,0,'token','{}',?,'phone',?)",
        (event.event_id, household, f"user-{household}", state))
    await db.conn.execute(
        "INSERT INTO inference_confirmations(event_id,frame_id,agent_id,household_id,confirmed,"
        "confidence,created_at) VALUES(?,'frame',?,?,1,0.9,?)",
        (event.event_id, agent_id, household, NOW))
    await db.conn.commit()
    recording = None
    if clip:
        recording = await RecordingsRepo(db, str(root)).save(
            household_id=household, event_id=event.event_id, camera_id=camera_id,
            body=b"persisted test clip", content_type="video/mp4")
    return event, recording


async def _count(db, table):
    return (await (await db.conn.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(events_module, "time", SimpleNamespace(time=lambda: NOW))


async def test_expired_history_removes_outbox_confirmations_clip_metadata_and_disk(tmp_path, fixed_clock):
    async with Database(str(tmp_path / "retention.db")) as db:
        root = tmp_path / "clips"
        old, old_clip = await _seed(db, root)
        current, current_clip = await _seed(db, root, label="current", age_days=5, state="delivered")
        repo = EventsRepo(db)
        assert await repo.prune(30, recordings_root=root) == 1
        assert await repo.get("household-1", old.event_id) is None
        assert await repo.get("household-1", current.event_id) is not None
        assert not old_clip.path.exists()
        assert current_clip.path.read_bytes() == b"persisted test clip"
        assert await _count(db, "push_outbox") == 1
        assert await _count(db, "inference_confirmations") == 1
        assert await _count(db, "recordings") == 1
        assert (await repo.record("household-1", current.event_id)).has_recording is True
        assert await (await db.conn.execute("PRAGMA foreign_key_check")).fetchall() == []
        assert await repo.prune(30, recordings_root=root) == 0


async def test_household_retention_and_batched_pruning_preserve_other_households(tmp_path, fixed_clock):
    async with Database(":memory:") as db:
        root = tmp_path / "clips"
        first, _ = await _seed(db, root, label="first")
        second, _ = await _seed(db, root, label="second")
        other, other_clip = await _seed(db, root, household="household-2")
        repo = EventsRepo(db)
        assert await repo.prune(30, recordings_root=root, household_id="household-1", batch_size=1) == 1
        assert len(await repo.records("household-1")) == 1
        assert await repo.prune(30, recordings_root=root, household_id="household-1", batch_size=1) == 1
        assert await repo.get("household-1", first.event_id) is None
        assert await repo.get("household-1", second.event_id) is None
        assert await repo.get("household-2", other.event_id) is not None
        assert other_clip.path.is_file()
        assert await _count(db, "push_outbox") == 1


async def test_no_recording_root_retains_indexed_clips_and_missing_files_are_retryable(tmp_path, fixed_clock):
    async with Database(":memory:") as db:
        root = tmp_path / "clips"
        clipped, recording = await _seed(db, root)
        unrecorded, _ = await _seed(db, root, label="no-clip", clip=False)
        repo = EventsRepo(db)
        assert await repo.prune(30) == 1
        assert await repo.get("household-1", unrecorded.event_id) is None
        assert await repo.get("household-1", clipped.event_id) is not None
        assert recording.path.is_file()
        recording.path.unlink()
        assert await repo.prune(30, recordings_root=root) == 1
        assert await _count(db, "recordings") == 0


async def test_exact_retention_boundary_and_newly_stored_old_capture_are_preserved(tmp_path, fixed_clock):
    async with Database(":memory:") as db:
        root = tmp_path / "clips"
        boundary, _ = await _seed(db, root, age_days=30, clip=False)
        repo = EventsRepo(db)
        old_capture = FallEvent(camera_id=boundary.camera_id, confidence=0.9,
                                occurred_at=datetime.fromtimestamp(0, timezone.utc))
        await repo.insert(old_capture, household_id="household-1", agent_id="agent-household-1")
        assert await repo.prune(30, recordings_root=root) == 0
        assert await repo.get("household-1", boundary.event_id) is not None
        assert await repo.get("household-1", old_capture.event_id) is not None


async def test_storage_failure_preserves_database_rows_for_later_cleanup(tmp_path, fixed_clock, monkeypatch):
    async with Database(":memory:") as db:
        root = tmp_path / "clips"
        event, recording = await _seed(db, root)
        original = type(recording.path).unlink

        def unavailable(path, *args, **kwargs):
            if path == recording.path:
                raise PermissionError("recording storage temporarily unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(recording.path), "unlink", unavailable)
        with pytest.raises(PermissionError):
            await EventsRepo(db).prune(30, recordings_root=root)
        assert recording.path.is_file()
        assert await _count(db, "events") == await _count(db, "push_outbox") == 1
        assert await _count(db, "recordings") == await _count(db, "inference_confirmations") == 1
        monkeypatch.setattr(type(recording.path), "unlink", original)
        assert await EventsRepo(db).prune(30, recordings_root=root) == 1


async def test_corrupted_storage_key_cannot_delete_outside_household(tmp_path, fixed_clock):
    async with Database(":memory:") as db:
        root = tmp_path / "clips"
        event, _ = await _seed(db, root)
        foreign = tmp_path / "outside.mp4"
        foreign.write_bytes(b"must remain")
        await db.conn.execute("UPDATE recordings SET storage_key=? WHERE event_id=?",
                              (str(foreign), event.event_id))
        await db.conn.commit()
        with pytest.raises(ValueError):
            await EventsRepo(db).prune(30, recordings_root=root)
        assert foreign.read_bytes() == b"must remain"
        assert await _count(db, "events") == await _count(db, "push_outbox") == 1


@pytest.mark.parametrize("days,batch", [(0, 200), (-1, 200), (30, 0), (30, 501)])
async def test_invalid_retention_cannot_remove_current_history(days, batch, tmp_path):
    async with Database(":memory:") as db:
        await _seed(db, tmp_path / "clips", age_days=0, clip=False)
        with pytest.raises(ValueError):
            await EventsRepo(db).prune(days, batch_size=batch)
        assert await _count(db, "events") == 1
