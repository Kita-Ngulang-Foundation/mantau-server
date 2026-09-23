"""Event clips on local disk (a persistent volume), indexed in SQLite.

Paths are derived from ids the server generated -- never from client input --
so an upload can never write outside `recordings_dir`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .db import Database


@dataclass(frozen=True)
class Recording:
    recording_id: str
    household_id: str
    event_id: str
    path: Path
    size_bytes: int
    content_type: str


class RecordingsRepo:
    def __init__(self, db: Database, root: str) -> None:
        self._db = db
        self.root = Path(root)

    def _path(self, household_id: str, event_id: str) -> Path:
        return self.root / household_id / f"{event_id}.mp4"

    async def save(self, *, household_id: str, event_id: str, camera_id: str,
                   body: bytes, content_type: str) -> Recording:
        path = self._path(household_id, event_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        temporary.write_bytes(body)
        os.replace(temporary, path)
        recording_id = f"rec-{event_id}"
        await self._db.conn.execute(
            "INSERT INTO recordings(recording_id,household_id,event_id,camera_id,storage_key,"
            "created_at,size_bytes,content_type) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(recording_id) DO UPDATE SET size_bytes=excluded.size_bytes, "
            "created_at=excluded.created_at",
            (recording_id, household_id, event_id, camera_id,
             f"{household_id}/{event_id}.mp4", time.time(), len(body), content_type),
        )
        await self._db.conn.commit()
        return Recording(recording_id, household_id, event_id, path, len(body), content_type)

    async def get(self, household_id: str, event_id: str) -> Recording | None:
        row = await (await self._db.conn.execute(
            "SELECT * FROM recordings WHERE household_id=? AND event_id=?",
            (household_id, event_id),
        )).fetchone()
        if row is None:
            return None
        path = self.root / row["storage_key"]
        if not path.is_file():
            return None
        return Recording(row["recording_id"], household_id, event_id, path,
                         row["size_bytes"] or path.stat().st_size,
                         row["content_type"] or "video/mp4")

    async def prune(self, retention_days: int) -> int:
        """Deletes clips older than the retention window. Returns count."""
        cutoff = time.time() - retention_days * 86400
        rows = await (await self._db.conn.execute(
            "SELECT recording_id, storage_key FROM recordings WHERE created_at<?", (cutoff,),
        )).fetchall()
        for row in rows:
            (self.root / row["storage_key"]).unlink(missing_ok=True)
        await self._db.conn.execute("DELETE FROM recordings WHERE created_at<?", (cutoff,))
        await self._db.conn.commit()
        return len(rows)
