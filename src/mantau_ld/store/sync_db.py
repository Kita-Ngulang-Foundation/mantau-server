"""A second, synchronous connection to the same SQLite file -- needed because
`mantau_core`'s `TokenStore`/`RecipientResolver` protocols are deliberately
synchronous (called from inside `FCMPushChannel.send()` without an `await`).
Uses the same `_connect_target` as `Database` so `":memory:"` resolves to
SQLite's shared-cache URI form instead of opening a second, disconnected,
empty database. See `db.py`'s docstring.
"""

from __future__ import annotations

import sqlite3
import threading

from .db import _connect_target


class SyncDatabase:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        uri, is_uri = _connect_target(path)
        self._conn = sqlite3.connect(uri, uri=is_uri, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._conn.close()
