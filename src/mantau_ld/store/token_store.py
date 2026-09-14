"""Implements `mantau_core.notify.channels.push.tokens.TokenStore` against SQLite.

`tokens_for_camera` returns every registered device regardless of
`camera_id` -- the account-global simplification documented in `db.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mantau_core.notify.channels.push.tokens import DeviceToken, Platform

from .sync_db import SyncDatabase


class SqliteTokenStore:
    def __init__(self, db: SyncDatabase) -> None:
        self._db = db

    def register(self, token: DeviceToken) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "INSERT INTO device_tokens (device_id, platform, token, registered_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET platform=excluded.platform, "
                "token=excluded.token, last_seen_at=excluded.last_seen_at",
                (token.device_id, token.platform.value, token.token,
                 token.registered_at.isoformat(), token.last_seen_at.isoformat()),
            )
            self._db.conn.commit()

    def tokens_for_camera(self, camera_id: str) -> list[DeviceToken]:
        with self._db.lock:
            rows = self._db.conn.execute("SELECT * FROM device_tokens").fetchall()
        return [
            DeviceToken(device_id=r["device_id"], platform=Platform(r["platform"]),
                        token=r["token"], registered_at=r["registered_at"],
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
