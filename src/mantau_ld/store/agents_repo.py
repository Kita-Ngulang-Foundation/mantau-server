"""Agent enrollment and proof-bound credential rotation."""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass

from .db import Database


@dataclass
class Agent:
    agent_id: str
    secret: str
    enrollment_id: str
    credential_version: int
    household_id: str | None
    enrolled_at: float
    last_seen_at: float | None
    revoked_at: float | None


class AgentIdentityProofRequired(PermissionError):
    pass


class AgentsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def enroll(self, agent_id: str, *, current_secret: str | None = None) -> Agent:
        """Create an identity, or rotate it only with proof of the current key."""
        secret = secrets.token_hex(32)  # 256 bits
        enrollment_id = f"enrollment-{uuid.uuid4().hex}"
        now = time.time()
        await self._db.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = await (await self._db.conn.execute(
                "SELECT * FROM agents WHERE agent_id=?", (agent_id,)
            )).fetchone()
            if existing is None:
                await self._db.conn.execute(
                    "INSERT INTO agents(agent_id,secret,enrollment_id,credential_version,household_id,"
                    "enrolled_at,last_seen_at,revoked_at) VALUES(?,?,?,1,NULL,?,NULL,NULL)",
                    (agent_id, secret, enrollment_id, now),
                )
                credential_version = 1
                household_id = None
            else:
                if (existing["revoked_at"] is not None or not current_secret
                        or not secrets.compare_digest(existing["secret"], current_secret)):
                    raise AgentIdentityProofRequired("current agent identity proof is required")
                credential_version = int(existing["credential_version"] or 1) + 1
                household_id = existing["household_id"]
                await self._db.conn.execute(
                    "UPDATE agents SET secret=?,enrollment_id=?,credential_version=?,"
                    "enrolled_at=?,last_seen_at=NULL WHERE agent_id=?",
                    (secret, enrollment_id, credential_version, now, agent_id),
                )
            await self._db.conn.commit()
        except BaseException:
            await self._db.conn.rollback()
            raise
        return Agent(
            agent_id=agent_id, secret=secret, enrollment_id=enrollment_id,
            credential_version=credential_version, household_id=household_id,
            enrolled_at=now, last_seen_at=None, revoked_at=None,
        )

    async def get(self, agent_id: str) -> Agent | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row(row)

    async def list_all(self) -> list[Agent]:
        cursor = await self._db.conn.execute(
            "SELECT * FROM agents WHERE revoked_at IS NULL ORDER BY enrolled_at ASC"
        )
        rows = await cursor.fetchall()
        return [self._row(row) for row in rows]

    async def list_for_household(self, household_id: str) -> list[Agent]:
        rows = await (await self._db.conn.execute(
            "SELECT * FROM agents WHERE household_id=? AND revoked_at IS NULL ORDER BY enrolled_at",
            (household_id,),
        )).fetchall()
        return [self._row(row) for row in rows]

    async def touch(self, agent_id: str, *, at: float | None = None) -> None:
        await self._db.conn.execute(
            "UPDATE agents SET last_seen_at=? WHERE agent_id=? AND revoked_at IS NULL",
            (at or time.time(), agent_id),
        )
        await self._db.conn.commit()

    async def revoke(self, agent_id: str, household_id: str) -> bool:
        cursor = await self._db.conn.execute(
            "UPDATE agents SET revoked_at=? WHERE agent_id=? AND household_id=? AND revoked_at IS NULL",
            (time.time(), agent_id, household_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row(row) -> Agent:
        return Agent(
            agent_id=row["agent_id"], secret=row["secret"],
            enrollment_id=row["enrollment_id"],
            credential_version=int(row["credential_version"] or 1),
            household_id=row["household_id"], enrolled_at=row["enrolled_at"],
            last_seen_at=row["last_seen_at"], revoked_at=row["revoked_at"],
        )
