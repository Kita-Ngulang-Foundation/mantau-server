from __future__ import annotations

import sqlite3

from mantau_ld.store.db import Database


LEGACY_SCHEMA = """
CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY, secret TEXT NOT NULL,
    enrolled_at REAL NOT NULL, last_seen_at REAL
);
CREATE TABLE cameras (
    camera_id TEXT PRIMARY KEY, name TEXT NOT NULL,
    agent_id TEXT, registered_at REAL NOT NULL
);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, kind TEXT NOT NULL,
    severity TEXT NOT NULL, occurred_at TEXT NOT NULL, confidence REAL NOT NULL,
    track_id INTEGER, signals_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_review', created_at REAL NOT NULL
);
CREATE TABLE device_tokens (
    device_id TEXT PRIMARY KEY, platform TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
    registered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE emergency_contacts (
    contact_id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
    relation TEXT NOT NULL, priority INTEGER NOT NULL
);
CREATE TABLE ingested_envelopes (
    agent_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
    received_at REAL NOT NULL, PRIMARY KEY (agent_id, seq)
);
CREATE TABLE claim_codes (
    code TEXT PRIMARY KEY, agent_id TEXT NOT NULL, created_at REAL NOT NULL,
    expires_at REAL NOT NULL, claimed_at REAL
);
CREATE TABLE agent_ownership (
    agent_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, claimed_at REAL NOT NULL
);
CREATE TABLE agent_control_state (
    agent_id TEXT PRIMARY KEY, platform TEXT, capabilities_json TEXT,
    setup_status TEXT NOT NULL DEFAULT 'not_started',
    health_state TEXT NOT NULL DEFAULT 'offline', requested_inference_mode TEXT,
    effective_inference_mode TEXT, camera_connectivity TEXT NOT NULL DEFAULT 'unknown',
    health_explanation TEXT, updated_at REAL NOT NULL
);
CREATE TABLE queued_commands (
    command_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, owner_id TEXT NOT NULL,
    command_type TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL,
    encrypted_payload BLOB, idempotency_key TEXT NOT NULL, created_at REAL NOT NULL,
    expires_at REAL NOT NULL, delivered_at REAL, completed_at REAL,
    UNIQUE(owner_id, agent_id, idempotency_key)
);
CREATE TABLE command_results (
    command_id TEXT PRIMARY KEY, state TEXT NOT NULL, failure_reason TEXT,
    message TEXT, data_json TEXT NOT NULL, completed_at REAL
);
CREATE TABLE discovery_results (
    agent_id TEXT NOT NULL, command_id TEXT NOT NULL, result_json TEXT NOT NULL,
    discovered_at REAL NOT NULL, PRIMARY KEY (agent_id, command_id)
);
"""


async def test_legacy_database_expands_and_backfills_durable_ownership(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as legacy:
        legacy.executescript(LEGACY_SCHEMA)
        legacy.execute(
            "INSERT INTO agents VALUES('agent-1','agent-secret',100,NULL)"
        )
        legacy.execute(
            "INSERT INTO agent_ownership VALUES('agent-1','legacy-user',101)"
        )
        legacy.execute(
            "INSERT INTO cameras VALUES('camera-1','Room','agent-1',102)"
        )
        legacy.execute(
            "INSERT INTO events VALUES('event-1','camera-1','fall','critical',"
            "'2026-01-01T00:00:00+00:00',0.9,NULL,'{}','needs_review',103)"
        )
        legacy.execute(
            "INSERT INTO queued_commands VALUES('command-1','agent-1','legacy-user',"
            "'restart','queued','{}',NULL,'once',104,204,NULL,NULL)"
        )
        legacy.execute(
            "INSERT INTO claim_codes VALUES('legacy-code','agent-1',100,200,NULL)"
        )

    db = Database(str(db_path))
    await db.connect()
    try:
        agent = await (await db.conn.execute(
            "SELECT enrollment_id,credential_version,household_id FROM agents "
            "WHERE agent_id='agent-1'"
        )).fetchone()
        assert agent["enrollment_id"].startswith("enrollment-")
        assert agent["credential_version"] == 1
        assert agent["household_id"]

        household_id = agent["household_id"]
        assert (await (await db.conn.execute(
            "SELECT household_id FROM cameras WHERE camera_id='camera-1'"
        )).fetchone())["household_id"] == household_id
        event = await (await db.conn.execute(
            "SELECT household_id,agent_id FROM events WHERE event_id='event-1'"
        )).fetchone()
        assert (event["household_id"], event["agent_id"]) == (household_id, "agent-1")
        command = await (await db.conn.execute(
            "SELECT household_id,requested_by_user_id FROM queued_commands "
            "WHERE command_id='command-1'"
        )).fetchone()
        assert command["household_id"] == household_id
        assert command["requested_by_user_id"]

        assert await (await db.conn.execute(
            "SELECT 1 FROM household_memberships WHERE household_id=?",
            (household_id,),
        )).fetchone()
        assert await (await db.conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=2"
        )).fetchone()
        assert await (await db.conn.execute(
            "SELECT 1 FROM claim_codes WHERE code='legacy-code'"
        )).fetchone()

        agent_fks = await (await db.conn.execute("PRAGMA foreign_key_list(agents)")).fetchall()
        assert "households" in {row["table"] for row in agent_fks}
        recording_fks = await (
            await db.conn.execute("PRAGMA foreign_key_list(recordings)")
        ).fetchall()
        assert {row["table"] for row in recording_fks} >= {
            "households", "events", "cameras",
        }
    finally:
        await db.close()

    reopened = Database(str(db_path))
    await reopened.connect()
    try:
        assert (await (await reopened.conn.execute(
            "SELECT COUNT(*) AS count FROM schema_migrations WHERE version=2"
        )).fetchone())["count"] == 1
        assert await (await reopened.conn.execute(
            "SELECT 1 FROM events WHERE event_id='event-1'"
        )).fetchone()
    finally:
        await reopened.close()
