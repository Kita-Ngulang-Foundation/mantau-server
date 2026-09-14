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
    agent_id: str | None


class CamerasRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, camera_id: str, name: str, *, agent_id: str | None = None) -> CameraInfo:
        await self._db.conn.execute(
            "INSERT INTO cameras (camera_id, name, agent_id, registered_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(camera_id) DO UPDATE SET name=excluded.name, agent_id=excluded.agent_id",
            (camera_id, name, agent_id, time.time()),
        )
        await self._db.conn.commit()
        return CameraInfo(camera_id=camera_id, name=name, agent_id=agent_id)

    async def get(self, camera_id: str) -> CameraInfo | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return CameraInfo(camera_id=row["camera_id"], name=row["name"], agent_id=row["agent_id"])

    async def list_all(self) -> list[CameraInfo]:
        cursor = await self._db.conn.execute("SELECT * FROM cameras ORDER BY registered_at ASC")
        rows = await cursor.fetchall()
        return [CameraInfo(camera_id=r["camera_id"], name=r["name"], agent_id=r["agent_id"]) for r in rows]

    async def name_for(self, camera_id: str) -> str:
        """Falls back to the raw id -- an alert must never fail to send just
        because a display name wasn't registered yet."""
        camera = await self.get(camera_id)
        return camera.name if camera else camera_id

    async def delete(self, camera_id: str) -> bool:
        cursor = await self._db.conn.execute("DELETE FROM cameras WHERE camera_id = ?", (camera_id,))
        await self._db.conn.commit()
        return cursor.rowcount > 0
