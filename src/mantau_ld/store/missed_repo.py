"""Missed-event reports: a household member says something happened that the
system did not alert on. Stored for every household; the author is kept
only as `reported_by` and is never returned to the app.
"""

from __future__ import annotations

from .db import Database
from .transactions import serialized_repository

_COLUMNS = "report_id,source,created_at,occurred_from,occurred_to,kind,camera_id"


@serialized_repository
class MissedRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, *, report_id: str, household_id: str, source: str,
                     reported_by: str | None, created_at: float, occurred_from: float,
                     occurred_to: float, kind: str, camera_id: str | None) -> None:
        await self._db.conn.execute(
            "INSERT INTO missed_reports(report_id,household_id,source,reported_by,created_at,"
            "occurred_from,occurred_to,kind,camera_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (report_id, household_id, source, reported_by, created_at,
             occurred_from, occurred_to, kind, camera_id),
        )
        await self._db.conn.commit()

    async def list_for_household(self, household_id: str, *, limit: int = 200) -> list[dict]:
        """Newest first. `reported_by` is deliberately left out."""
        rows = await (await self._db.conn.execute(
            f"SELECT {_COLUMNS} FROM missed_reports WHERE household_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (household_id, limit),
        )).fetchall()
        return [dict(row) for row in rows]

    async def delete(self, household_id: str, report_id: str) -> bool:
        cursor = await self._db.conn.execute(
            "DELETE FROM missed_reports WHERE household_id=? AND report_id=?",
            (household_id, report_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0
