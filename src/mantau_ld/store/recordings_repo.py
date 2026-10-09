"""Clips stored by agents older than local clip retention, and their removal.

The server no longer stores clips: uploads go through the one-use relay
(routes/recordings.py). `purge_all` runs at startup and deletes whatever an
older server version stored under `recordings_dir`. `save`, `get` and `prune`
are kept for the existing tests, which seed such leftover rows; no route calls
them.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .transactions import serialized_repository

log = logging.getLogger(__name__)


class RecordingsCapacityExceeded(ValueError):
    """Admission exceeds storage budget; existing history remains available."""


@dataclass(frozen=True)
class Recording:
    recording_id: str
    household_id: str
    event_id: str
    path: Path
    size_bytes: int
    content_type: str


@serialized_repository
class RecordingsRepo:
    def __init__(self, db: Database, root: str, *, household_max_bytes: int = 1024**3,
                 global_max_bytes: int = 5 * 1024**3) -> None:
        if household_max_bytes <= 0 or global_max_bytes <= 0:
            raise ValueError("Recording storage limits must be positive")
        self._db = db
        self.root = Path(root).resolve()
        self.household_max_bytes = household_max_bytes
        self.global_max_bytes = global_max_bytes

    @staticmethod
    def _validate_id(value: str) -> None:
        reserved = {"CON", "PRN", "AUX", "NUL"}
        reserved.update(f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10))
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value)
                or value.endswith(".") or value.split(".")[0].upper() in reserved):
            raise ValueError("Invalid recording identifier")

    def _path(self, household_id: str, event_id: str) -> Path:
        self._validate_id(household_id)
        self._validate_id(event_id)
        household = self.root / household_id
        path = household / f"{event_id}.mp4"
        # resolve also follows Windows junctions. Do not follow links installed
        # at the configured root, household directory, or individual clip.
        if (self.root.resolve() != self.root or household.resolve() != household
                or path.resolve() != path):
            raise ValueError("Recording path escapes its household storage")
        return path

    def _indexed_path(self, row) -> Path:
        path = self._path(row["household_id"], row["event_id"])
        if row["storage_key"] != f"{row['household_id']}/{row['event_id']}.mp4":
            raise ValueError("Invalid indexed recording path")
        return path

    async def _reconcile_sizes(self) -> None:
        rows = await (await self._db.conn.execute(
            "SELECT * FROM recordings WHERE size_bytes IS NULL",
        )).fetchall()
        for row in rows:
            path = self._indexed_path(row)
            size = path.stat().st_size if path.is_file() else 0
            await self._db.conn.execute(
                "UPDATE recordings SET size_bytes=? WHERE recording_id=?",
                (size, row["recording_id"]),
            )

    @staticmethod
    def _temporary(parent: Path) -> Path:
        fd, name = tempfile.mkstemp(prefix=".recording-", suffix=".part", dir=parent)
        os.close(fd)
        return Path(name)

    async def save(self, *, household_id: str, event_id: str, camera_id: str,
                   body: bytes, content_type: str) -> Recording:
        if self._db.in_atomic:
            raise RuntimeError("Save recordings outside a caller-owned transaction")
        path = self._path(household_id, event_id)
        recording_id = f"rec-{event_id}"
        temporary = backup = None
        replaced = False
        try:
            async with self._db.transaction():
                event = await (await self._db.conn.execute(
                    "SELECT event_id FROM events WHERE event_id=? AND household_id=? AND camera_id=?",
                    (event_id, household_id, camera_id),
                )).fetchone()
                if event is None:
                    raise ValueError("Recording does not belong to this household event")
                await self._reconcile_sizes()
                totals = await (await self._db.conn.execute(
                    "SELECT COALESCE(SUM(size_bytes),0) AS total, "
                    "COALESCE(SUM(CASE WHEN household_id=? THEN size_bytes ELSE 0 END),0) AS household, "
                    "COALESCE(SUM(CASE WHEN recording_id=? THEN size_bytes ELSE 0 END),0) AS previous "
                    "FROM recordings", (household_id, recording_id),
                )).fetchone()
                additional = len(body) - totals["previous"]
                if (totals["household"] + additional > self.household_max_bytes
                        or totals["total"] + additional > self.global_max_bytes):
                    raise RecordingsCapacityExceeded("Recording storage capacity reached")
                path.parent.mkdir(parents=True, exist_ok=True)
                self._path(household_id, event_id)
                temporary = self._temporary(path.parent)
                with temporary.open("wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists():
                    backup = self._temporary(path.parent)
                    shutil.copyfile(path, backup)
                await self._db.conn.execute(
                    "INSERT INTO recordings(recording_id,household_id,event_id,camera_id,storage_key,"
                    "created_at,size_bytes,content_type) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(recording_id) DO UPDATE SET size_bytes=excluded.size_bytes, "
                    "created_at=excluded.created_at, content_type=excluded.content_type",
                    (recording_id, household_id, event_id, camera_id,
                     f"{household_id}/{event_id}.mp4", time.time(), len(body), content_type),
                )
                self._path(household_id, event_id)
                os.replace(temporary, path)
                replaced = True
        except BaseException:
            if replaced:
                self._path(household_id, event_id)
                if backup is not None:
                    os.replace(backup, path)
                else:
                    path.unlink(missing_ok=True)
            raise
        finally:
            for staged in (temporary, backup):
                if staged is not None:
                    staged.unlink(missing_ok=True)
        return Recording(recording_id, household_id, event_id, path, len(body), content_type)

    async def get(self, household_id: str, event_id: str) -> Recording | None:
        self._path(household_id, event_id)
        row = await (await self._db.conn.execute(
            "SELECT * FROM recordings WHERE household_id=? AND event_id=?",
            (household_id, event_id),
        )).fetchone()
        if row is None:
            return None
        path = self._indexed_path(row)
        if not path.is_file():
            return None
        return Recording(row["recording_id"], household_id, event_id, path,
                         row["size_bytes"] if row["size_bytes"] is not None else path.stat().st_size,
                         row["content_type"] or "video/mp4")

    async def prune(self, retention_days: int, *, batch_size: int = 256) -> int:
        """Remove one bounded batch of expired clips without deleting events."""
        if retention_days <= 0 or batch_size <= 0:
            raise ValueError("Retention and batch size must be positive")
        cutoff = time.time() - retention_days * 86400
        rows = await (await self._db.conn.execute(
            "SELECT * FROM recordings WHERE created_at<? ORDER BY created_at,recording_id LIMIT ?",
            (cutoff, batch_size),
        )).fetchall()
        paths = [self._indexed_path(row) for row in rows]
        for row, path in zip(rows, paths):
            path.unlink(missing_ok=True)
            await self._db.conn.execute("DELETE FROM recordings WHERE recording_id=?",
                                        (row["recording_id"],))
        await self._db.conn.commit()
        return len(rows)

    async def purge_all(self) -> int:
        """Delete every stored clip file and index row; return the rows removed.

        Idempotent. A file that cannot be deleted keeps its row, so the next
        start retries it. A corrupt index row is dropped without following its
        path. Interrupted uploads and clips without a row are removed from the
        household folders too; links are never followed."""
        if self._db.in_atomic:
            raise RuntimeError("Purge recordings outside a caller-owned transaction")
        rows = await (await self._db.conn.execute("SELECT * FROM recordings")).fetchall()
        removed = 0
        for row in rows:
            try:
                path = self._indexed_path(row)
            except ValueError:
                path = None
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not delete stored clip %s; retrying on next start",
                                row["storage_key"])
                    continue
            await self._db.conn.execute("DELETE FROM recordings WHERE recording_id=?",
                                        (row["recording_id"],))
            removed += 1
        await self._db.conn.commit()
        if self.root.is_dir() and not self.root.is_symlink():
            for household in self.root.iterdir():
                if (household.is_symlink() or not household.is_dir()
                        or household.resolve() != household):
                    continue
                for item in household.iterdir():
                    if (item.is_file() and not item.is_symlink()
                            and (item.suffix == ".mp4" or item.name.startswith(".recording-"))):
                        try:
                            item.unlink(missing_ok=True)
                        except OSError:
                            log.warning("Could not delete stored clip file %s", item.name)
                try:
                    household.rmdir()
                except OSError:
                    pass
        if removed:
            log.info("Deleted %d stored clips; the server keeps no clips", removed)
        return removed
