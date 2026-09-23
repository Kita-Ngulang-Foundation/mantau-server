from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

from .db import Database


class HouseholdAccessDenied(LookupError):
    pass


class HouseholdSelectionRequired(ValueError):
    pass


class InviteInvalid(LookupError):
    """Unknown, expired, or already used invite code."""


class AlreadyMember(ValueError):
    pass


class LastOwner(ValueError):
    """A household must keep at least one owner."""


class RateLimited(RuntimeError):
    pass


ROLES = ("owner", "admin", "member")


@dataclass(frozen=True)
class UserPrincipal:
    user_id: str
    household_id: str
    role: str
    issuer: str
    subject: str


class IdentityRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def ensure_user(self, identity) -> str:
        """Resolve (creating on first sight) and refresh display claims."""
        user_id = await self._ensure_user(identity.issuer, identity.subject)
        email = getattr(identity, "email", None)
        name = getattr(identity, "name", None)
        if email or name:
            await self._db.conn.execute(
                "UPDATE users SET email=COALESCE(?,email), display_name=COALESCE(?,display_name) "
                "WHERE user_id=?", (email, name, user_id),
            )
            await self._db.conn.commit()
        return user_id

    async def rename_household(self, household_id: str, name: str) -> None:
        await self._db.conn.execute(
            "UPDATE households SET name=? WHERE household_id=?", (name, household_id)
        )
        await self._db.conn.commit()

    async def members(self, household_id: str) -> list[dict]:
        rows = await (await self._db.conn.execute(
            "SELECT m.user_id, m.role, m.created_at, u.email, u.display_name "
            "FROM household_memberships m JOIN users u ON u.user_id=m.user_id "
            "WHERE m.household_id=? ORDER BY m.created_at", (household_id,),
        )).fetchall()
        return [dict(row) for row in rows]

    async def create_invite(self, household_id: str, created_by: str, *, role: str,
                            ttl_s: int) -> tuple[str, float]:
        """A single-use invite code. Only its SHA-256 is stored."""
        if role not in ("admin", "member"):
            raise ValueError("invalid role")
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        now = time.time()
        await self._db.conn.execute(
            "INSERT INTO household_invites(code_hash,household_id,created_by,role,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (_hash(code), household_id, created_by, role, now, now + ttl_s),
        )
        await self._db.conn.commit()
        return code, now + ttl_s

    async def accept_invite(self, code: str, user_id: str, *, attempt_limit: int,
                            attempt_window_s: int) -> str:
        """Joins the invite's household; returns its id. Atomic and single-use."""
        now = time.time()
        await self._db.conn.execute("BEGIN IMMEDIATE")
        try:
            bucket = f"invite:{user_id}"
            rate = await (await self._db.conn.execute(
                "SELECT window_started_at,attempts FROM rate_limits WHERE bucket=?", (bucket,),
            )).fetchone()
            if rate is None or now - rate["window_started_at"] >= attempt_window_s:
                await self._db.conn.execute(
                    "INSERT INTO rate_limits(bucket,window_started_at,attempts) VALUES(?,?,1) "
                    "ON CONFLICT(bucket) DO UPDATE SET window_started_at=excluded.window_started_at,"
                    "attempts=1", (bucket, now),
                )
            elif rate["attempts"] >= attempt_limit:
                await self._db.conn.commit()
                raise RateLimited("too many invite attempts")
            else:
                await self._db.conn.execute(
                    "UPDATE rate_limits SET attempts=attempts+1 WHERE bucket=?", (bucket,)
                )
            invite = await (await self._db.conn.execute(
                "SELECT * FROM household_invites WHERE code_hash=? AND consumed_at IS NULL "
                "AND expires_at>?", (_hash(code.strip().upper()), now),
            )).fetchone()
            if invite is None:
                await self._db.conn.commit()
                raise InviteInvalid("invite unavailable")
            existing = await (await self._db.conn.execute(
                "SELECT 1 FROM household_memberships WHERE household_id=? AND user_id=?",
                (invite["household_id"], user_id),
            )).fetchone()
            if existing is not None:
                await self._db.conn.commit()
                raise AlreadyMember("already a member")
            await self._db.conn.execute(
                "UPDATE household_invites SET consumed_at=?,consumed_by=? WHERE code_hash=?",
                (now, user_id, invite["code_hash"]),
            )
            await self._db.conn.execute(
                "INSERT INTO household_memberships(household_id,user_id,role,created_at) "
                "VALUES(?,?,?,?)", (invite["household_id"], user_id, invite["role"], now),
            )
            await self._db.conn.commit()
            return invite["household_id"]
        except (RateLimited, InviteInvalid, AlreadyMember):
            raise
        except BaseException:
            await self._db.conn.rollback()
            raise

    async def remove_member(self, household_id: str, user_id: str) -> bool:
        """Removes a membership and that user's push registrations tied to
        it. Refuses to remove the last owner."""
        await self._db.conn.execute("BEGIN IMMEDIATE")
        try:
            row = await (await self._db.conn.execute(
                "SELECT role FROM household_memberships WHERE household_id=? AND user_id=?",
                (household_id, user_id),
            )).fetchone()
            if row is None:
                await self._db.conn.commit()
                return False
            if row["role"] == "owner":
                owners = await (await self._db.conn.execute(
                    "SELECT COUNT(*) FROM household_memberships WHERE household_id=? AND role='owner'",
                    (household_id,),
                )).fetchone()
                if owners[0] <= 1:
                    await self._db.conn.commit()
                    raise LastOwner("household needs an owner")
            await self._db.conn.execute(
                "DELETE FROM household_memberships WHERE household_id=? AND user_id=?",
                (household_id, user_id),
            )
            await self._db.conn.execute(
                "DELETE FROM device_tokens WHERE household_id=? AND user_id=?",
                (household_id, user_id),
            )
            await self._db.conn.commit()
            return True
        except LastOwner:
            raise
        except BaseException:
            await self._db.conn.rollback()
            raise

    async def households(self, issuer: str, subject: str) -> list[dict]:
        """Every household the user belongs to. Needs no household selection,
        so a user with several memberships can discover which to choose."""
        user_id = await self._ensure_user(issuer, subject)
        rows = await (await self._db.conn.execute(
            "SELECT h.household_id, h.name, m.role FROM household_memberships m "
            "JOIN households h ON h.household_id = m.household_id "
            "WHERE m.user_id=? ORDER BY m.created_at",
            (user_id,),
        )).fetchall()
        return [
            {"household_id": r["household_id"], "name": r["name"], "role": r["role"]}
            for r in rows
        ]

    async def resolve(
        self, issuer: str, subject: str, *, requested_household_id: str | None = None
    ) -> UserPrincipal:
        user_id = await self._ensure_user(issuer, subject)
        memberships = await (await self._db.conn.execute(
            "SELECT household_id,role FROM household_memberships WHERE user_id=? ORDER BY created_at",
            (user_id,),
        )).fetchall()
        if requested_household_id:
            membership = next(
                (item for item in memberships if item["household_id"] == requested_household_id), None
            )
            if membership is None:
                raise HouseholdAccessDenied("household unavailable")
        elif len(memberships) == 1:
            membership = memberships[0]
        elif not memberships:
            raise HouseholdAccessDenied("household unavailable")
        else:
            raise HouseholdSelectionRequired("household selection required")
        return UserPrincipal(
            user_id=user_id,
            household_id=membership["household_id"],
            role=membership["role"],
            issuer=issuer,
            subject=subject,
        )

    async def _ensure_user(self, issuer: str, subject: str) -> str:
        """The user for (issuer, subject); a first sign-in creates the user
        and a household they own."""
        row = await (await self._db.conn.execute(
            "SELECT user_id FROM user_identities WHERE oidc_issuer=? AND oidc_subject=?",
            (issuer, subject),
        )).fetchone()
        if row is None:
            # Legacy owner ids can be bridged only in explicit local-dev mode.
            # An OIDC subject that happens to match an old owner id must never
            # inherit that owner's household in production.
            legacy = None
            if issuer == "local-dev":
                legacy = await (await self._db.conn.execute(
                    "SELECT user_id FROM user_identities "
                    "WHERE oidc_issuer='legacy' AND oidc_subject=?", (subject,),
                )).fetchone()
            if legacy is not None:
                user_id = legacy["user_id"]
                await self._db.conn.execute(
                    "INSERT INTO user_identities(oidc_issuer,oidc_subject,user_id) VALUES(?,?,?)",
                    (issuer, subject, user_id),
                )
            else:
                user_id = f"user-{uuid.uuid4().hex}"
                household_id = f"household-{uuid.uuid4().hex}"
                now = time.time()
                await self._db.conn.execute(
                    "INSERT INTO users(user_id,created_at) VALUES(?,?)", (user_id, now)
                )
                await self._db.conn.execute(
                    "INSERT INTO user_identities(oidc_issuer,oidc_subject,user_id) VALUES(?,?,?)",
                    (issuer, subject, user_id),
                )
                await self._db.conn.execute(
                    "INSERT INTO households(household_id,name,created_at) VALUES(?,?,?)",
                    (household_id, "My household", now),
                )
                await self._db.conn.execute(
                    "INSERT INTO household_memberships(household_id,user_id,role,created_at) "
                    "VALUES(?,?,'owner',?)", (household_id, user_id, now),
                )
            await self._db.conn.commit()
            return user_id
        return row["user_id"]


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
