"""Seq-based idempotency: a retried envelope must never cause a second alert.

`ingested_envelopes` has `PRIMARY KEY (agent_id, seq)` -- the uniqueness
constraint itself enforces this, not application logic re-checking an
in-memory set (which wouldn't survive a restart, and this absolutely must
survive a restart: the agent's spool will retry a send it isn't sure landed,
possibly after the server came back up).

An envelope with a LOWER seq than one already processed for its agent is
still a genuinely NEW `(agent_id, seq)` pair if that exact pair hasn't been
seen before -- so it's accepted, just flagged `out_of_order=True`. Only an
EXACT repeat of a pair already in the ledger is a duplicate.

What this does NOT do: hold back an out-of-order envelope to reassemble
strict sequence for a downstream consumer. A network that delivers seq 5
before seq 4 processes both, in receipt order, each exactly once. Reassembly
would need a per-agent reorder buffer with a flush timeout -- real
additional work, not implemented here.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from mantau_core.contracts import Envelope

from ..store.db import Database


@dataclass
class DedupeResult:
    duplicate: bool
    out_of_order: bool


async def record_envelope(envelope: Envelope, db: Database) -> DedupeResult:
    """Insert `(agent_id, seq)` into the ledger.

    `duplicate=True` means this exact pair was already recorded (a retried
    send) -- the caller must NOT act on the payload again. `out_of_order=True`
    means this seq is lower than the highest already recorded for this agent
    (still accepted; see module docstring).
    """
    cursor = await db.conn.execute(
        "SELECT MAX(seq) AS max_seq FROM ingested_envelopes WHERE agent_id = ?",
        (envelope.agent_id,),
    )
    row = await cursor.fetchone()
    max_seq = row["max_seq"] if row and row["max_seq"] is not None else None
    out_of_order = max_seq is not None and envelope.seq < max_seq

    try:
        await db.conn.execute(
            "INSERT INTO ingested_envelopes (agent_id, seq, kind, received_at) VALUES (?, ?, ?, ?)",
            (envelope.agent_id, envelope.seq, envelope.kind.value, time.time()),
        )
        await db.conn.commit()
    except sqlite3.IntegrityError:
        return DedupeResult(duplicate=True, out_of_order=False)
    return DedupeResult(duplicate=False, out_of_order=out_of_order)
