from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .db import Database


class HouseholdAccessDenied(LookupError):
    pass


class HouseholdSelectionRequired(ValueError):
    pass


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
