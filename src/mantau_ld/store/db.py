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

CREATE TABLE IF NOT EXISTS claim_codes (
    code        TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    claimed_at  REAL,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_ownership (
    agent_id    TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    claimed_at  REAL NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_control_state (
    agent_id                    TEXT PRIMARY KEY,
    platform                    TEXT,
    capabilities_json           TEXT,
    setup_status                TEXT NOT NULL DEFAULT 'not_started',
    health_state                TEXT NOT NULL DEFAULT 'offline',
    requested_inference_mode    TEXT,
    effective_inference_mode    TEXT,
    camera_connectivity         TEXT NOT NULL DEFAULT 'unknown',
    health_explanation          TEXT,
    updated_at                  REAL NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS queued_commands (
    command_id          TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    owner_id            TEXT NOT NULL,
    command_type        TEXT NOT NULL,
    state               TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    encrypted_payload   BLOB,
    idempotency_key     TEXT NOT NULL,
    created_at          REAL NOT NULL,
    expires_at          REAL NOT NULL,
    delivered_at        REAL,
    completed_at        REAL,
    UNIQUE(owner_id, agent_id, idempotency_key),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS command_results (
    command_id       TEXT PRIMARY KEY,
    state            TEXT NOT NULL,
    failure_reason   TEXT,
    message          TEXT,
    data_json        TEXT NOT NULL,
    completed_at     REAL,
    FOREIGN KEY (command_id) REFERENCES queued_commands(command_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discovery_results (
    agent_id       TEXT NOT NULL,
    command_id     TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    discovered_at REAL NOT NULL,
    PRIMARY KEY (agent_id, command_id),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_commands_agent_state
ON queued_commands(agent_id, state, created_at);
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
