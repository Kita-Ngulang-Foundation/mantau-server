"""Implements `mantau_core.notify.channels.push.tokens.TokenStore` against SQLite.

`tokens_for_camera` resolves camera ownership and returns the devices of every
current member of that household. A registration belongs to a user, so a
member of several households gets alerts from all of them, and removing a
member stops their alerts at once.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mantau_core.notify.channels.push.tokens import DeviceToken, Platform

from .sync_db import SyncDatabase


class SqliteTokenStore:
    def __init__(self, db: SyncDatabase) -> None:
        self._db = db

    def register(self, token: DeviceToken) -> None:
        if not token.user_id or not token.household_id:
            raise ValueError("owned device token required")
        with self._db.lock:
            self._db.conn.execute(
                "INSERT INTO device_tokens(device_id,user_id,household_id,platform,token,registered_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET platform=excluded.platform, "
                "token=excluded.token,last_seen_at=excluded.last_seen_at "
                "WHERE device_tokens.user_id=excluded.user_id "
                "AND device_tokens.household_id=excluded.household_id",
                (token.device_id, token.user_id, token.household_id, token.platform.value, token.token,
                 token.registered_at.isoformat(), token.last_seen_at.isoformat()),
            )
            self._db.conn.commit()

    def tokens_for_camera(self, camera_id: str) -> list[DeviceToken]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT d.* FROM device_tokens d "
                "JOIN cameras c ON c.camera_id=? "
                "JOIN household_memberships m "
                "  ON m.household_id=c.household_id AND m.user_id=d.user_id "
                "WHERE d.user_id IS NOT NULL",
                (camera_id,),
            ).fetchall()
        return [
            DeviceToken(device_id=r["device_id"], platform=Platform(r["platform"]),
                        token=r["token"], user_id=r["user_id"], household_id=r["household_id"],
                        registered_at=r["registered_at"],
                        last_seen_at=r["last_seen_at"])
            for r in rows
        ]

    def touch(self, token: str, *, at: datetime | None = None) -> None:
        ts = (at or datetime.now(timezone.utc)).isoformat()
        with self._db.lock:
            self._db.conn.execute(
                "UPDATE device_tokens SET last_seen_at = ? WHERE token = ?", (ts, token)
            )
            self._db.conn.commit()

    def prune(self, token: str) -> None:
        with self._db.lock:
            self._db.conn.execute("DELETE FROM device_tokens WHERE token = ?", (token,))
            self._db.conn.commit()

    def delete_owned(self, device_id: str, *, user_id: str, household_id: str) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "DELETE FROM device_tokens WHERE device_id=? AND user_id=? AND household_id=?",
                (device_id, user_id, household_id),
            )
            self._db.conn.commit()
