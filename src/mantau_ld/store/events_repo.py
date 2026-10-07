"""Persistence for fall/anomaly events.

`status` mirrors the app's own `FallStatus` enum exactly (needs_review /
dismissed / confirmed). Recording availability is indexed separately from
the event payload; history retention removes both delivery work and clips.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from mantau_core.contracts import EventKind, FallEvent, Severity

from .db import Database
from .transactions import serialized_repository

VALID_STATUSES = {"needs_review", "dismissed", "confirmed"}


def _row_to_event(row: aiosqlite.Row) -> FallEvent:
    return FallEvent(
        event_id=row["event_id"], camera_id=row["camera_id"],
        kind=EventKind(row["kind"]), severity=Severity(row["severity"]),
        occurred_at=row["occurred_at"], confidence=row["confidence"],
        track_id=row["track_id"], signals=json.loads(row["signals_json"]),
        zone_id=row["zone_id"] if "zone_id" in row.keys() else None,
    )


@dataclass(frozen=True)
class EventRecord:
    """An event plus what the household did about it."""
    event: FallEvent
    status: str
    camera_name: str
    created_at: float
    acknowledged_at: float | None
    acknowledged_by: str | None
    reviewed_at: float | None
    reviewed_by: str | None
    has_recording: bool
    recording_permitted: bool = True
    # Latest server-inference confirmation (HYBRID) from the event's own agent.
    server_confirmed: bool | None = None
    server_confirmation_confidence: float | None = None


_RECORD_SQL = (
    "SELECT e.*, c.name AS camera_name, "
    "(EXISTS(SELECT 1 FROM recordings r WHERE r.event_id=e.event_id) OR "
    "EXISTS(SELECT 1 FROM agent_recordings ar WHERE ar.event_id=e.event_id AND ar.household_id=e.household_id)) AS has_recording, "
    "(c.camera_id IS NOT NULL AND c.revoked_at IS NULL AND c.household_id=e.household_id) AS recording_permitted, "
    "(SELECT ic.confirmed FROM inference_confirmations ic WHERE ic.event_id=e.event_id "
    " AND ic.agent_id=e.agent_id ORDER BY ic.created_at DESC LIMIT 1) AS server_confirmed, "
    "(SELECT ic.confidence FROM inference_confirmations ic WHERE ic.event_id=e.event_id "
    " AND ic.agent_id=e.agent_id ORDER BY ic.created_at DESC LIMIT 1) AS server_confidence "
    "FROM events e LEFT JOIN cameras c ON c.camera_id=e.camera_id "
)


def _row_to_record(row: aiosqlite.Row) -> EventRecord:
    return EventRecord(
        event=_row_to_event(row), status=row["status"],
        camera_name=row["camera_name"] or row["camera_id"], created_at=row["created_at"],
        acknowledged_at=row["acknowledged_at"], acknowledged_by=row["acknowledged_by"],
        reviewed_at=row["reviewed_at"], reviewed_by=row["reviewed_by"],
        has_recording=bool(row["has_recording"]),
        recording_permitted=bool(row["recording_permitted"]),
        server_confirmed=(None if row["server_confirmed"] is None
                          else bool(row["server_confirmed"])),
        server_confirmation_confidence=row["server_confidence"],
    )


@serialized_repository
class EventsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def records(self, household_id: str, *, limit: int = 50,
                      before: float | None = None, kind: str | None = None) -> list[EventRecord]:
        """Newest first. `before` is the `created_at` of the last record of
        the previous page (keyset pagination: stable while events arrive)."""
        sql = _RECORD_SQL + "WHERE e.household_id=?"
        params: list = [household_id]
        if before is not None:
            sql += " AND e.created_at<?"
            params.append(before)
        if kind is not None:
            sql += " AND e.kind=?"
            params.append(kind)
        sql += " ORDER BY e.created_at DESC LIMIT ?"
        params.append(limit)
        rows = await (await self._db.conn.execute(sql, params)).fetchall()
        return [_row_to_record(r) for r in rows]

    async def record(self, household_id: str, event_id: str) -> EventRecord | None:
        row = await (await self._db.conn.execute(
            _RECORD_SQL + "WHERE e.household_id=? AND e.event_id=?", (household_id, event_id),
        )).fetchone()
        return _row_to_record(row) if row else None

    async def agent_for(self, event_id: str) -> str | None:
        row = await (await self._db.conn.execute(
            "SELECT agent_id FROM events WHERE event_id=?", (event_id,),
        )).fetchone()
        return row["agent_id"] if row else None

    async def acknowledge(self, household_id: str, event_id: str, user_id: str) -> bool:
        """Records the first acknowledgement only. True if this was it."""
        cursor = await self._db.conn.execute(
            "UPDATE events SET acknowledged_at=?, acknowledged_by=? "
            "WHERE household_id=? AND event_id=? AND acknowledged_at IS NULL",
            (time.time(), user_id, household_id, event_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def review(self, household_id: str, event_id: str, status: str, user_id: str) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}, must be one of {VALID_STATUSES}")
        cursor = await self._db.conn.execute(
            "UPDATE events SET status=?, reviewed_at=?, reviewed_by=? "
            "WHERE household_id=? AND event_id=?",
            (status, time.time(), user_id, household_id, event_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def insert(self, event: FallEvent, *, household_id: str, agent_id: str) -> bool:
        """Store an event; False when this event id is already stored (activity
        rules give an episode the same id every time, so a re-delivery is a no-op)."""
        cursor = await self._db.conn.execute(
            "INSERT OR IGNORE INTO events(event_id,household_id,agent_id,camera_id,kind,severity,"
            "occurred_at,confidence,track_id,signals_json,zone_id,status,created_at) "
            # created_at is the history cursor, so it must be unique per
            # household: a coarse clock (Windows: ~15 ms) can repeat a value.
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'needs_review',MAX(?,COALESCE("
            "(SELECT MAX(created_at) FROM events WHERE household_id=?),0)+0.000001))",
            (event.event_id, household_id, agent_id, event.camera_id,
             event.kind.value, event.severity.value,
             event.occurred_at.isoformat(), event.confidence, event.track_id,
             json.dumps(event.signals), event.zone_id, time.time(), household_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def list_for_household(self, household_id: str, *, limit: int = 100) -> list[FallEvent]:
        cursor = await self._db.conn.execute(
            "SELECT * FROM events WHERE household_id=? ORDER BY created_at DESC LIMIT ?",
            (household_id, limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_event(r) for r in rows]

    async def get(self, household_id: str, event_id: str) -> FallEvent | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM events WHERE household_id=? AND event_id=?", (household_id, event_id)
        )
        row = await cursor.fetchone()
        return _row_to_event(row) if row else None

    async def get_status(self, household_id: str, event_id: str) -> str | None:
        cursor = await self._db.conn.execute(
            "SELECT status FROM events WHERE household_id=? AND event_id=?",
            (household_id, event_id),
        )
        row = await cursor.fetchone()
        return row["status"] if row else None

    async def set_status(self, household_id: str, event_id: str, status: str) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}, must be one of {VALID_STATUSES}")
        cursor = await self._db.conn.execute(
            "UPDATE events SET status=? WHERE household_id=? AND event_id=?",
            (status, household_id, event_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def prune(self, retention_days: int, *, recordings_root: str | Path | None = None,
                    household_id: str | None = None, batch_size: int = 200) -> int:
        """Remove one bounded batch of expired history and its delivery records.

        Retention uses server storage time, so newly received older detections
        remain reviewable. A configured recording root permits clip cleanup;
        without it, events with indexed clips are retained. Files are removed
        before database changes; a storage error preserves the rows for retry,
        and retries tolerate files already removed by a partial cleanup.
        """
        if retention_days < 1 or not 1 <= batch_size <= 500:
            raise ValueError("Positive retention and batch_size between 1 and 500 required")
        if self._db.in_atomic:
            raise RuntimeError("Event retention requires its own transaction")
        root = Path(recordings_root).resolve() if recordings_root is not None else None
        async with self._db.transaction():
            sql = "SELECT e.event_id,e.household_id FROM events e WHERE e.created_at<?"
            params: list = [time.time() - retention_days * 86400]
            if household_id is not None:
                sql += " AND e.household_id=?"
                params.append(household_id)
            if root is None:
                sql += " AND NOT EXISTS(SELECT 1 FROM recordings r WHERE r.event_id=e.event_id)"
            sql += " ORDER BY e.created_at,e.event_id LIMIT ?"
            params.append(batch_size)
            expired = await (await self._db.conn.execute(sql, params)).fetchall()
            if not expired:
                return 0
            ids = [row["event_id"] for row in expired]
            placeholders = ",".join("?" for _ in ids)
            clips = await (await self._db.conn.execute(
                "SELECT r.storage_key,r.household_id,e.household_id AS event_household_id "
                "FROM recordings r JOIN events e ON e.event_id=r.event_id "
                f"WHERE e.event_id IN ({placeholders})",
                ids)).fetchall()
            paths = []
            for clip in clips:
                if clip["household_id"] != clip["event_household_id"]:
                    raise ValueError("Recording household does not match event")
                path = (root / clip["storage_key"]).resolve()
                # A corrupted index or symlink must never target another
                # household or a location outside the configured volume.
                household_root = (root / clip["household_id"]).resolve()
                household_root.relative_to(root)
                path.relative_to(household_root)
                paths.append(path)
            for path in paths:
                path.unlink(missing_ok=True)
            await self._db.conn.execute(
                f"DELETE FROM push_outbox WHERE event_id IN ({placeholders})", ids)
            await self._db.conn.execute(
                f"DELETE FROM inference_confirmations WHERE event_id IN ({placeholders})", ids)
            # Explicit removal also works for upgraded databases whose legacy
            # recording foreign key did not specify ON DELETE CASCADE.
            await self._db.conn.execute(
                f"DELETE FROM recordings WHERE event_id IN ({placeholders})", ids)
            deleted = await self._db.conn.execute(
                f"DELETE FROM events WHERE event_id IN ({placeholders})", ids)
        return deleted.rowcount
