"""Recording admission preserves history and cannot escape household storage."""
import asyncio
import os
import subprocess

import pytest
from mantau_core.contracts import FallEvent

import support
import mantau_ld.store.recordings_repo as module
from mantau_ld.store.agents_repo import AgentsRepo
from mantau_ld.store.cameras_repo import CamerasRepo
from mantau_ld.store.db import Database
from mantau_ld.store.events_repo import EventsRepo
from mantau_ld.store.recordings_repo import RecordingsCapacityExceeded, RecordingsRepo


async def seed(db, household="household-1", events=("event-1", "event-2", "event-3")):
    agent, camera = f"agent-{household}", f"camera-{household}"
    if await AgentsRepo(db).get(agent) is None:
        await support.enroll_in_db(db, agent, household_id=household, user_id=f"user-{household}")
    await CamerasRepo(db).create(camera, "Room", household_id=household, agent_id=agent)
    for event in events:
        await EventsRepo(db).insert(FallEvent(event_id=event, camera_id=camera, confidence=0.9),
                                    household_id=household, agent_id=agent)
    return camera


async def save(repo, event="event-1", body=b"1234", household="household-1"):
    return await repo.save(household_id=household, event_id=event,
                           camera_id=f"camera-{household}", body=body, content_type="video/mp4")


async def test_household_limit_preserves_current_history_and_allows_overwrite(tmp_path):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        repo = RecordingsRepo(db, str(tmp_path / "clips"), household_max_bytes=6, global_max_bytes=20)
        first = await save(repo)
        await save(repo, "event-2", b"12")
        with pytest.raises(RecordingsCapacityExceeded):
            await save(repo, "event-3", b"1")
        with pytest.raises(RecordingsCapacityExceeded):
            await save(repo, body=b"12345")
        assert first.path.read_bytes() == b"1234"
        assert await EventsRepo(db).get("household-1", "event-3") is not None
        changed = await save(repo, body=b"1")
        assert changed.size_bytes == 1
        await save(repo, "event-3", b"123")
        assert (await repo.get("household-1", "event-2")).path.read_bytes() == b"12"
        assert not list(repo.root.rglob("*.part"))


async def test_global_limit_and_concurrent_admission_are_serialized(tmp_path):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        await seed(db, "household-2", ("event-other",))
        repo = RecordingsRepo(db, str(tmp_path / "clips"), household_max_bytes=10, global_max_bytes=6)
        outcomes = await asyncio.gather(save(repo), save(repo, "event-2"), return_exceptions=True)
        assert sum(isinstance(outcome, RecordingsCapacityExceeded) for outcome in outcomes) == 1
        await save(repo, "event-other", b"12", "household-2")
        with pytest.raises(RecordingsCapacityExceeded):
            await save(repo, "event-3", b"1")
        assert sum(path.stat().st_size for path in repo.root.rglob("*.mp4")) == 6


async def test_legacy_null_sizes_are_counted(tmp_path):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        repo = RecordingsRepo(db, str(tmp_path / "clips"), household_max_bytes=5)
        await save(repo)
        await db.conn.execute("UPDATE recordings SET size_bytes=NULL")
        await db.conn.commit()
        with pytest.raises(RecordingsCapacityExceeded):
            await save(repo, "event-2", b"12")
        assert (await repo.get("household-1", "event-1")).path.read_bytes() == b"1234"


@pytest.mark.parametrize("household,event", [("../outside", "event-1"), ("household-1", "../outside"),
    ("household-1", "a/b"), ("C:\\outside", "event-1"), ("NUL", "event-1"),
    ("household-1", "event-1.")])
async def test_rejects_traversal_and_platform_aliases(tmp_path, household, event):
    async with Database(str(tmp_path / "s.db")) as db:
        repo = RecordingsRepo(db, str(tmp_path / "clips"))
        with pytest.raises(ValueError):
            await save(repo, event, household=household)
        assert not repo.root.exists()


async def test_corrupt_index_is_rejected_for_reads_and_prune(tmp_path):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        repo = RecordingsRepo(db, str(tmp_path / "clips"))
        clip = await save(repo)
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"untouched")
        await db.conn.execute("UPDATE recordings SET storage_key='../outside.mp4',created_at=0")
        await db.conn.commit()
        with pytest.raises(ValueError):
            await repo.get("household-1", "event-1")
        with pytest.raises(ValueError):
            await repo.prune(30)
        assert outside.read_bytes() == b"untouched"
        assert clip.path.read_bytes() == b"1234"


async def test_household_symlink_is_rejected(tmp_path):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        repo = RecordingsRepo(db, str(tmp_path / "clips"))
        repo.root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = repo.root / "household-1"
        junction = False
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                pytest.skip(f"Symlink creation unavailable on this host: {exc}")
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True)
            if result.returncode:
                pytest.skip("Neither symlinks nor directory junctions are available")
            junction = True
        try:
            with pytest.raises(ValueError):
                await save(repo)
            assert not list(outside.iterdir())
        finally:
            if junction:
                os.rmdir(link)
            else:
                link.unlink()


async def test_fsync_failure_keeps_prior_clip_and_cleans_staged_files(tmp_path, monkeypatch):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        repo = RecordingsRepo(db, str(tmp_path / "clips"))
        first = await save(repo)
        def fail(_fd):
            raise OSError("disk failure")
        monkeypatch.setattr(module.os, "fsync", fail)
        with pytest.raises(OSError, match="disk failure"):
            await save(repo, body=b"changed")
        assert first.path.read_bytes() == b"1234"
        assert (await repo.get("household-1", "event-1")).size_bytes == 4
        assert not list(repo.root.rglob("*.part"))


async def test_replace_failure_keeps_prior_clip_and_rolls_back_index(tmp_path, monkeypatch):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        repo = RecordingsRepo(db, str(tmp_path / "clips"))
        first = await save(repo)
        def fail(_source, _destination):
            raise OSError("replace failure")
        monkeypatch.setattr(module.os, "replace", fail)
        with pytest.raises(OSError, match="replace failure"):
            await save(repo, body=b"changed")
        assert first.path.read_bytes() == b"1234"
        assert (await repo.get("household-1", "event-1")).size_bytes == 4
        assert not list(repo.root.rglob("*.part"))


@pytest.mark.parametrize("existing", [True, False])
async def test_commit_failure_restores_files_and_metadata(tmp_path, monkeypatch, existing):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        repo = RecordingsRepo(db, str(tmp_path / "clips"))
        if existing:
            await save(repo)
        async def fail():
            raise OSError("commit failure")
        monkeypatch.setattr(db._conn, "commit", fail)
        with pytest.raises(OSError, match="commit failure"):
            await save(repo, body=b"changed")
        clip = await repo.get("household-1", "event-1")
        if existing:
            assert clip.path.read_bytes() == b"1234"
            assert clip.size_bytes == 4
        else:
            assert clip is None
            assert not list(repo.root.rglob("*.mp4"))
        assert not list(repo.root.rglob("*.part"))


async def test_prune_is_bounded_and_keeps_current_history(tmp_path):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        repo = RecordingsRepo(db, str(tmp_path / "clips"))
        await save(repo)
        await save(repo, "event-2")
        current = await save(repo, "event-3")
        await db.conn.execute("UPDATE recordings SET created_at=0 WHERE event_id != 'event-3'")
        await db.conn.commit()
        assert await repo.prune(30, batch_size=1) == 1
        assert await repo.prune(30, batch_size=1) == 1
        assert await repo.prune(30, batch_size=1) == 0
        assert current.path.read_bytes() == b"1234"
        assert await EventsRepo(db).get("household-1", "event-1") is not None


async def test_mismatched_household_event_cannot_be_stored(tmp_path):
    async with Database(str(tmp_path / "s.db")) as db:
        await seed(db)
        await seed(db, "household-2", ("event-other",))
        repo = RecordingsRepo(db, str(tmp_path / "clips"))
        with pytest.raises(ValueError, match="belong"):
            await save(repo, "event-other")
        assert not repo.root.exists()
