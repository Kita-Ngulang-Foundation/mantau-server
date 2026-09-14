"""Agent enrollment: the shared HMAC secret every envelope from that agent is
signed with (see `../../protocol/PROTOCOL.md`). No rotation flow in this
version -- revoke and re-enroll is the only way to change a secret.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from .db import Database


@dataclass
class Agent:
    agent_id: str
    secret: str
    enrolled_at: float
    last_seen_at: float | None


class AgentsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def enroll(self, agent_id: str) -> Agent:
        """Generate and store a new secret for `agent_id`. Enrolling an
        already-enrolled id issues a fresh secret (equivalent to revoke +
        re-enroll in one step)."""
        secret = secrets.token_hex(32)  # 256 bits
        now = time.time()
        await self._db.conn.execute(
            "INSERT INTO agents (agent_id, secret, enrolled_at, last_seen_at) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(agent_id) DO UPDATE SET secret=excluded.secret, "
            "enrolled_at=excluded.enrolled_at, last_seen_at=NULL",
            (agent_id, secret, now),
        )
        await self._db.conn.commit()
        return Agent(agent_id=agent_id, secret=secret, enrolled_at=now, last_seen_at=None)

    async def get(self, agent_id: str) -> Agent | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Agent(agent_id=row["agent_id"], secret=row["secret"],
                     enrolled_at=row["enrolled_at"], last_seen_at=row["last_seen_at"])

    async def list_all(self) -> list[Agent]:
        cursor = await self._db.conn.execute("SELECT * FROM agents ORDER BY enrolled_at ASC")
        rows = await cursor.fetchall()
        return [Agent(agent_id=r["agent_id"], secret=r["secret"],
                       enrolled_at=r["enrolled_at"], last_seen_at=r["last_seen_at"]) for r in rows]

    async def touch(self, agent_id: str, *, at: float | None = None) -> None:
        await self._db.conn.execute(
            "UPDATE agents SET last_seen_at = ? WHERE agent_id = ?", (at or time.time(), agent_id)
        )
        await self._db.conn.commit()

    async def revoke(self, agent_id: str) -> bool:
        cursor = await self._db.conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        await self._db.conn.commit()
        return cursor.rowcount > 0
