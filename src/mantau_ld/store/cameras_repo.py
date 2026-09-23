"""Camera name resolution ONLY -- deliberately thin compared to
mantau-backend-rtsp's `CamerasRepo`. This server never connects to a camera
(that's the whole point of Scenario 2: the agent does, on the LAN); it only
needs `camera_id -> display name` so an alert can say "Kamar Ibu" instead of
a raw id. The app registers this mapping directly; the agent separately
discovers/connects to the physical camera and reports events tagged with the
same `camera_id` to correlate the two.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database


@dataclass
class CameraInfo:
    camera_id: str
    name: str
    household_id: str
    agent_id: str | None


class CamerasRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self, camera_id: str, name: str, *, household_id: str, agent_id: str | None = None
    ) -> CameraInfo:
        if agent_id is not None:
            owned = await (await self._db.conn.execute(
                "SELECT 1 FROM agents WHERE agent_id=? AND household_id=? AND revoked_at IS NULL",
                (agent_id, household_id),
            )).fetchone()
            if owned is None:
                raise LookupError("agent unavailable")
        await self._db.conn.execute(
            "INSERT INTO cameras(camera_id,name,household_id,agent_id,registered_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(camera_id) DO UPDATE SET name=excluded.name,agent_id=excluded.agent_id "
            "WHERE cameras.household_id=excluded.household_id",
            (camera_id, name, household_id, agent_id, time.time()),
        )
        row = await (await self._db.conn.execute(
            "SELECT * FROM cameras WHERE camera_id=? AND household_id=?", (camera_id, household_id)
        )).fetchone()
        if row is None:
            await self._db.conn.rollback()
            raise LookupError("camera unavailable")
        await self._db.conn.commit()
        return self._row(row)

    async def get(self, camera_id: str) -> CameraInfo | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row(row)

    async def get_for_household(self, household_id: str, camera_id: str) -> CameraInfo | None:
        row = await (await self._db.conn.execute(
            "SELECT * FROM cameras WHERE household_id=? AND camera_id=?",
            (household_id, camera_id),
        )).fetchone()
        return self._row(row) if row else None

    async def get_for_agent(self, agent_id: str, camera_id: str) -> CameraInfo | None:
        row = await (await self._db.conn.execute(
            "SELECT c.* FROM cameras c JOIN agents a ON a.agent_id=c.agent_id "
            "WHERE c.camera_id=? AND c.agent_id=? AND c.household_id=a.household_id "
            "AND a.revoked_at IS NULL", (camera_id, agent_id),
        )).fetchone()
        return self._row(row) if row else None

    async def list_all(self) -> list[CameraInfo]:
        cursor = await self._db.conn.execute("SELECT * FROM cameras ORDER BY registered_at ASC")
        rows = await cursor.fetchall()
        return [self._row(row) for row in rows]

    async def list_for_household(self, household_id: str) -> list[CameraInfo]:
        rows = await (await self._db.conn.execute(
            "SELECT * FROM cameras WHERE household_id=? ORDER BY registered_at", (household_id,)
        )).fetchall()
        return [self._row(row) for row in rows]

    async def name_for(self, camera_id: str, household_id: str) -> str:
        """Falls back to the raw id -- an alert must never fail to send just
        because a display name wasn't registered yet."""
        camera = await self.get_for_household(household_id, camera_id)
        return camera.name if camera else camera_id

    async def delete(self, household_id: str, camera_id: str) -> bool:
        cursor = await self._db.conn.execute(
            "DELETE FROM cameras WHERE household_id=? AND camera_id=?", (household_id, camera_id)
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row(row) -> CameraInfo:
        return CameraInfo(
            camera_id=row["camera_id"], name=row["name"],
            household_id=row["household_id"], agent_id=row["agent_id"],
        )
