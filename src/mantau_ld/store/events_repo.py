"""Persistence for fall/anomaly events.

`status` mirrors the app's own `FallStatus` enum exactly (needs_review /
dismissed / confirmed). Clip attachment is out of scope this pass -- no clip
extraction runs on the agent's live path yet, so `FallEvent.clip` always
round-trips as None.
"""

from __future__ import annotations

import json
import time

import aiosqlite
from mantau_core.contracts import EventKind, FallEvent, Severity

from .db import Database

VALID_STATUSES = {"needs_review", "dismissed", "confirmed"}


def _row_to_event(row: aiosqlite.Row) -> FallEvent:
    return FallEvent(
        event_id=row["event_id"], camera_id=row["camera_id"],
        kind=EventKind(row["kind"]), severity=Severity(row["severity"]),
        occurred_at=row["occurred_at"], confidence=row["confidence"],
        track_id=row["track_id"], signals=json.loads(row["signals_json"]),
    )


class EventsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, event: FallEvent, *, household_id: str, agent_id: str) -> None:
        await self._db.conn.execute(
            "INSERT INTO events(event_id,household_id,agent_id,camera_id,kind,severity,occurred_at,"
            "confidence,track_id,signals_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'needs_review',?)",
            (event.event_id, household_id, agent_id, event.camera_id,
             event.kind.value, event.severity.value,
             event.occurred_at.isoformat(), event.confidence, event.track_id,
             json.dumps(event.signals), time.time()),
        )
        await self._db.conn.commit()

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
