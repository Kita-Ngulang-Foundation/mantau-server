"""Persistence for fall/anomaly events.

`status` mirrors the app's own `FallStatus` enum exactly (needs_review /
dismissed / confirmed). Clip attachment is out of scope this pass -- no clip
extraction runs on the agent's live path yet, so `FallEvent.clip` always
round-trips as None.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

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
    # Latest server-inference confirmation (HYBRID) from the event's own agent.
    server_confirmed: bool | None = None
    server_confirmation_confidence: float | None = None


_RECORD_SQL = (
    "SELECT e.*, c.name AS camera_name, "
    "EXISTS(SELECT 1 FROM recordings r WHERE r.event_id=e.event_id) AS has_recording, "
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
        server_confirmed=(None if row["server_confirmed"] is None
                          else bool(row["server_confirmed"])),
        server_confirmation_confidence=row["server_confidence"],
    )


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
