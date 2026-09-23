"""The one SQLite connection the whole server shares, WAL mode, schema on connect.

Every user-visible durable resource is linked to a household. Existing v1
databases are expanded and backfilled without dropping their legacy tables.

`ingested_envelopes` is the dedupe ledger: `PRIMARY KEY (agent_id, seq)`
means a second INSERT for an already-seen pair fails outright -- that
constraint failure, not application logic, is what makes a retried send a
guaranteed no-op. See `ingest/dedupe.py`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_identities (
    oidc_issuer   TEXT NOT NULL,
    oidc_subject  TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    PRIMARY KEY (oidc_issuer, oidc_subject),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS household_invites (
    code_hash     TEXT PRIMARY KEY,
    household_id  TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    role          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    consumed_at   REAL,
    consumed_by   TEXT,
    FOREIGN KEY (household_id) REFERENCES households(household_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS camera_detection_settings (
    camera_id        TEXT PRIMARY KEY,
    household_id     TEXT NOT NULL,
    settings_json    TEXT NOT NULL,
    version          INTEGER NOT NULL,
    updated_at       REAL NOT NULL,
    updated_by       TEXT NOT NULL,
    applied_version  INTEGER,
    applied_at       REAL,
    FOREIGN KEY (camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE,
    FOREIGN KEY (household_id) REFERENCES households(household_id)
);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket             TEXT PRIMARY KEY,
    window_started_at  REAL NOT NULL,
    attempts           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS households (
    household_id  TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS household_memberships (
    household_id  TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    role          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (household_id, user_id),
    FOREIGN KEY (household_id) REFERENCES households(household_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id           TEXT PRIMARY KEY,
    secret             TEXT NOT NULL,
    enrollment_id      TEXT NOT NULL,
    credential_version INTEGER NOT NULL DEFAULT 1,
    household_id       TEXT,
    enrolled_at        REAL NOT NULL,
    last_seen_at       REAL,
    revoked_at         REAL,
    FOREIGN KEY (household_id) REFERENCES households(household_id)
);

CREATE TABLE IF NOT EXISTS cameras (
    camera_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    household_id   TEXT,
    agent_id       TEXT,
    registered_at  REAL NOT NULL,
    FOREIGN KEY (household_id) REFERENCES households(household_id),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    household_id TEXT,
    agent_id     TEXT,
    camera_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    confidence   REAL NOT NULL,
    track_id     INTEGER,
    signals_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'needs_review',
    created_at   REAL NOT NULL,
    FOREIGN KEY (household_id) REFERENCES households(household_id),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id),
    FOREIGN KEY (camera_id) REFERENCES cameras(camera_id)
);

CREATE TABLE IF NOT EXISTS inference_confirmations (
    event_id      TEXT NOT NULL,
    frame_id      TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    household_id  TEXT NOT NULL,
    confirmed     INTEGER NOT NULL,
    confidence    REAL NOT NULL,
    reason        TEXT,
    created_at    REAL NOT NULL,
    PRIMARY KEY (event_id, frame_id)
);

CREATE TABLE IF NOT EXISTS device_tokens (
    device_id      TEXT PRIMARY KEY,
    user_id        TEXT,
    household_id   TEXT,
    platform       TEXT NOT NULL,
    token          TEXT NOT NULL UNIQUE,
    registered_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (household_id) REFERENCES households(household_id)
);

CREATE TABLE IF NOT EXISTS emergency_contacts (
    contact_id  TEXT PRIMARY KEY,
    household_id TEXT,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    relation    TEXT NOT NULL,
    priority    INTEGER NOT NULL,
    FOREIGN KEY (household_id) REFERENCES households(household_id)
);

CREATE TABLE IF NOT EXISTS recordings (
    recording_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    camera_id    TEXT NOT NULL,
    storage_key  TEXT NOT NULL,
    created_at   REAL NOT NULL,
    FOREIGN KEY (household_id) REFERENCES households(household_id),
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE,
    FOREIGN KEY (camera_id) REFERENCES cameras(camera_id)
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

CREATE TABLE IF NOT EXISTS enrollment_claims (
    code_hash       TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    enrollment_id   TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    consumed_at     REAL,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS claim_rate_limits (
    user_id          TEXT PRIMARY KEY,
    window_started_at REAL NOT NULL,
    attempts         INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
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
    household_id        TEXT,
    requested_by_user_id TEXT,
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
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
    FOREIGN KEY (household_id) REFERENCES households(household_id),
    FOREIGN KEY (requested_by_user_id) REFERENCES users(user_id)
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


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(f"{prefix}:{value}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    rows = await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


async def _add_column(conn: aiosqlite.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in await _columns(conn, table):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


async def _migrate_existing(conn: aiosqlite.Connection) -> None:
    """Expand legacy databases in place; old tables/columns remain usable for rollback."""
    await _add_column(conn, "agents", "enrollment_id TEXT")
    await _add_column(conn, "agents", "credential_version INTEGER NOT NULL DEFAULT 1")
    # v3: display-only profile claims for member lists.
    await _add_column(conn, "events", "zone_id TEXT")
    await _add_column(conn, "users", "email TEXT")
    await _add_column(conn, "users", "display_name TEXT")
    # v3: durable acknowledgement and review attribution.
    await _add_column(conn, "events", "acknowledged_at REAL")
    await _add_column(conn, "events", "acknowledged_by TEXT")
    await _add_column(conn, "events", "reviewed_at REAL")
    await _add_column(conn, "events", "reviewed_by TEXT")
    await _add_column(conn, "recordings", "size_bytes INTEGER")
    await _add_column(conn, "recordings", "content_type TEXT")
    await _add_column(conn, "agents", "household_id TEXT REFERENCES households(household_id)")
    await _add_column(conn, "agents", "revoked_at REAL")
    await _add_column(conn, "cameras", "household_id TEXT REFERENCES households(household_id)")
    await _add_column(conn, "events", "household_id TEXT REFERENCES households(household_id)")
    await _add_column(conn, "events", "agent_id TEXT REFERENCES agents(agent_id)")
    await _add_column(conn, "device_tokens", "user_id TEXT REFERENCES users(user_id)")
    await _add_column(conn, "device_tokens", "household_id TEXT REFERENCES households(household_id)")
    await _add_column(conn, "emergency_contacts", "household_id TEXT REFERENCES households(household_id)")
    await _add_column(conn, "queued_commands", "household_id TEXT REFERENCES households(household_id)")
    await _add_column(conn, "queued_commands", "requested_by_user_id TEXT REFERENCES users(user_id)")

    agents = await (await conn.execute(
        "SELECT agent_id,enrolled_at FROM agents WHERE enrollment_id IS NULL OR enrollment_id=''"
    )).fetchall()
    for row in agents:
        enrollment_id = _stable_id("enrollment", f"{row['agent_id']}:{row['enrolled_at']}")
        await conn.execute(
            "UPDATE agents SET enrollment_id=? WHERE agent_id=?", (enrollment_id, row["agent_id"])
        )

    ownership = await (await conn.execute(
        "SELECT agent_id,owner_id,claimed_at FROM agent_ownership ORDER BY claimed_at"
    )).fetchall()
    for row in ownership:
        user_id = _stable_id("user", row["owner_id"])
        household_id = _stable_id("household", row["owner_id"])
        await conn.execute(
            "INSERT OR IGNORE INTO users(user_id,created_at) VALUES(?,?)",
            (user_id, row["claimed_at"]),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO user_identities(oidc_issuer,oidc_subject,user_id) VALUES('legacy',?,?)",
            (row["owner_id"], user_id),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO households(household_id,name,created_at) VALUES(?,?,?)",
            (household_id, "Migrated household", row["claimed_at"]),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO household_memberships(household_id,user_id,role,created_at) "
            "VALUES(?,?,'owner',?)", (household_id, user_id, row["claimed_at"]),
        )
        await conn.execute(
            "UPDATE agents SET household_id=? WHERE agent_id=? AND household_id IS NULL",
            (household_id, row["agent_id"]),
        )
        await conn.execute(
            "UPDATE queued_commands SET household_id=?,requested_by_user_id=? "
            "WHERE agent_id=? AND household_id IS NULL",
            (household_id, user_id, row["agent_id"]),
        )

    await conn.execute(
        "UPDATE cameras SET household_id=(SELECT household_id FROM agents WHERE agents.agent_id=cameras.agent_id) "
        "WHERE household_id IS NULL AND agent_id IS NOT NULL"
    )
    await conn.execute(
        "UPDATE events SET household_id=(SELECT household_id FROM cameras WHERE cameras.camera_id=events.camera_id),"
        "agent_id=(SELECT agent_id FROM cameras WHERE cameras.camera_id=events.camera_id) "
        "WHERE household_id IS NULL"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,strftime('%s','now'))"
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_household ON agents(household_id,agent_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_cameras_household ON cameras(household_id,camera_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_household ON events(household_id,created_at)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_household ON device_tokens(household_id,user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_household ON emergency_contacts(household_id,priority)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_invites_household ON household_invites(household_id)")
    await conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(3,strftime('%s','now'))"
    )


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
        await _migrate_existing(self._conn)
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
