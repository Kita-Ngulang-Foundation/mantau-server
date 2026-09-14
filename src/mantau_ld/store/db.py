"""The one SQLite connection the whole server shares, WAL mode, schema on connect.

Same single-account simplification as mantau-backend-rtsp: `device_tokens`
and `emergency_contacts` are account-global, not per-camera.

`ingested_envelopes` is the dedupe ledger: `PRIMARY KEY (agent_id, seq)`
means a second INSERT for an already-seen pair fails outright -- that
constraint failure, not application logic, is what makes a retried send a
guaranteed no-op. See `ingest/dedupe.py`.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    secret        TEXT NOT NULL,
    enrolled_at   REAL NOT NULL,
    last_seen_at  REAL
);

CREATE TABLE IF NOT EXISTS cameras (
    camera_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    agent_id       TEXT,
    registered_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    camera_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    confidence   REAL NOT NULL,
    track_id     INTEGER,
    signals_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'needs_review',
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS device_tokens (
    device_id      TEXT PRIMARY KEY,
    platform       TEXT NOT NULL,
    token          TEXT NOT NULL UNIQUE,
    registered_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emergency_contacts (
    contact_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    relation    TEXT NOT NULL,
    priority    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ingested_envelopes (
    agent_id     TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    received_at  REAL NOT NULL,
    PRIMARY KEY (agent_id, seq)
);
"""


def _connect_target(path: str) -> tuple[str, bool]:
    """Turn a configured db path into what sqlite3/aiosqlite should open, and
    whether that's a URI.

    Plain ":memory:" gives every connection its OWN private database -- wrong
    here, since `SyncDatabase` opens a second connection to the "same" path
    for `TokenStore`/`RecipientResolver`. SQLite's shared-cache URI form makes
    multiple connections to ":memory:" actually share one database.
    """
    if path == ":memory:":
        return "file::memory:?cache=shared", True
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path, False


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        uri, is_uri = _connect_target(self.path)
        self._conn = await aiosqlite.connect(uri, uri=is_uri)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was never called")
        return self._conn

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
