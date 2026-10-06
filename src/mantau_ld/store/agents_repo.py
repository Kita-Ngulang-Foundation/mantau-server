"""Agent enrollment keys, enrollment, and agent credentials.

A household owner or admin creates a single-use enrollment key in the app; the
agent presents it once to `POST /agents/enroll` and is created directly inside
that household. There is no anonymous enrollment and no separate claim step.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

from .db import Database
from .transactions import serialized_repository

# Crockford base32: no I, L, O, U, so a key read aloud or retyped is unambiguous.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_KEY_CHARS = 20  # 100 bits
KEY_PREFIX = "MTU"


@dataclass
class Agent:
    agent_id: str
    name: str
    secret: str
    enrollment_id: str
    credential_version: int
    household_id: str | None
    enrolled_at: float
    last_seen_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class EnrollmentKey:
    key_id: str
    household_id: str
    created_at: float
    expires_at: float
    consumed_at: float | None
    revoked_at: float | None
    agent_id: str | None

    def status(self, now: float | None = None) -> str:
        if self.consumed_at is not None:
            return "used"
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= (now or time.time()):
            return "expired"
        return "pending"


class EnrollmentKeyInvalid(PermissionError):
    """Unknown, expired, revoked, or already used enrollment key."""


class AgentIdTaken(ValueError):
    pass


def normalize_enrollment_key(value: str) -> str:
    """Accepts `MTU-ABCDE-FGHJK-...` in any case, with or without separators."""
    cleaned = "".join(ch for ch in value.upper() if ch.isascii() and ch.isalnum())
    if cleaned.startswith(KEY_PREFIX):
        cleaned = cleaned[len(KEY_PREFIX):]
    return cleaned


def _hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _format_key(body: str) -> str:
    return "-".join([KEY_PREFIX, *(body[i:i + 5] for i in range(0, len(body), 5))])


@serialized_repository
class AgentsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_enrollment_key(self, household_id: str, created_by: str, *,
                                    ttl_s: int) -> tuple[EnrollmentKey, str]:
        """A new single-use key for `household_id`. Returns the record and the
        key itself, which is shown once; only its SHA-256 is stored."""
        body = "".join(secrets.choice(_ALPHABET) for _ in range(_KEY_CHARS))
        now = time.time()
        key = EnrollmentKey(
            key_id=f"ekey-{uuid.uuid4().hex}", household_id=household_id,
            created_at=now, expires_at=now + ttl_s,
            consumed_at=None, revoked_at=None, agent_id=None,
        )
        await self._db.conn.execute(
            "INSERT INTO agent_enrollment_keys(key_id,key_hash,household_id,created_by,"
            "created_at,expires_at) VALUES(?,?,?,?,?,?)",
            (key.key_id, _hash(body), household_id, created_by, now, key.expires_at),
        )
        await self._db.conn.commit()
        return key, _format_key(body)

    async def get_enrollment_key(self, household_id: str, key_id: str) -> EnrollmentKey | None:
        row = await (await self._db.conn.execute(
            "SELECT * FROM agent_enrollment_keys WHERE key_id=? AND household_id=?",
            (key_id, household_id),
        )).fetchone()
        return None if row is None else self._key_row(row)

    async def revoke_enrollment_key(self, household_id: str, key_id: str) -> bool:
        cursor = await self._db.conn.execute(
            "UPDATE agent_enrollment_keys SET revoked_at=? WHERE key_id=? AND household_id=? "
            "AND consumed_at IS NULL AND revoked_at IS NULL",
            (time.time(), key_id, household_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def enroll(self, enrollment_key: str, agent_id: str, *, name: str,
                     platform: str) -> Agent:
        """Consume `enrollment_key` and create `agent_id` inside its household,
        atomically. Returns the agent with its secret (shown once)."""
        normalized = normalize_enrollment_key(enrollment_key)
        if len(normalized) != _KEY_CHARS:
            raise EnrollmentKeyInvalid("invalid enrollment key")
        secret = secrets.token_hex(32)  # 256 bits
        now = time.time()
        await self._db.conn.execute("BEGIN IMMEDIATE")
        try:
            key = await (await self._db.conn.execute(
                "SELECT k.key_id,k.household_id FROM agent_enrollment_keys k JOIN households h "
                "ON h.household_id=k.household_id WHERE k.key_hash=? AND k.consumed_at IS NULL "
                "AND k.revoked_at IS NULL AND k.expires_at>?",
                (_hash(normalized), now),
            )).fetchone()
            if key is None:
                raise EnrollmentKeyInvalid("invalid enrollment key")
            if await (await self._db.conn.execute(
                "SELECT 1 FROM agents WHERE agent_id=?", (agent_id,)
            )).fetchone() is not None:
                raise AgentIdTaken(agent_id)
            enrollment_id = f"enrollment-{key['key_id']}"
            await self._db.conn.execute(
                "INSERT INTO agents(agent_id,name,secret,enrollment_id,credential_version,"
                "household_id,enrolled_at,last_seen_at,revoked_at) VALUES(?,?,?,?,1,?,?,NULL,NULL)",
                (agent_id, name, secret, enrollment_id, key["household_id"], now),
            )
            await self._db.conn.execute(
                "INSERT INTO agent_control_state(agent_id,platform,updated_at) VALUES(?,?,?)",
                (agent_id, platform, now),
            )
            await self._db.conn.execute(
                "UPDATE agent_enrollment_keys SET consumed_at=?,agent_id=? WHERE key_id=?",
                (now, agent_id, key["key_id"]),
            )
            await self._db.conn.commit()
        except BaseException:
            await self._db.conn.rollback()
            raise
        return Agent(
            agent_id=agent_id, name=name, secret=secret, enrollment_id=enrollment_id,
            credential_version=1, household_id=key["household_id"],
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

    async def rename(self, agent_id: str, household_id: str, name: str) -> bool:
        cursor = await self._db.conn.execute(
            "UPDATE agents SET name=? WHERE agent_id=? AND household_id=? AND revoked_at IS NULL",
            (name, agent_id, household_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

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
            agent_id=row["agent_id"], name=row["name"] or row["agent_id"], secret=row["secret"],
            enrollment_id=row["enrollment_id"],
            credential_version=int(row["credential_version"] or 1),
            household_id=row["household_id"], enrolled_at=row["enrolled_at"],
            last_seen_at=row["last_seen_at"], revoked_at=row["revoked_at"],
        )

    @staticmethod
    def _key_row(row) -> EnrollmentKey:
        return EnrollmentKey(
            key_id=row["key_id"], household_id=row["household_id"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            consumed_at=row["consumed_at"], revoked_at=row["revoked_at"],
            agent_id=row["agent_id"],
        )
