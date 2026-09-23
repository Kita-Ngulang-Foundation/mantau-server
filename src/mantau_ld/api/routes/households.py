"""Households of the signed-in user, their members, and invites.

`GET /households` and `POST /households/join` need no household selection.
Every other route acts on the caller's selected household (the path id must
match it), so a user can never read or change another household.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...control_auth import authenticated_identity, authenticated_user
from ...oidc_auth import OidcIdentity
from ...store.identity_repo import (
    AlreadyMember, InviteInvalid, LastOwner, RateLimited, UserPrincipal,
)

router = APIRouter(prefix="/households", tags=["households"])

_MANAGERS = ("owner", "admin")


class HouseholdOut(BaseModel):
    household_id: str
    name: str
    role: str


class HouseholdRename(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class MemberOut(BaseModel):
    user_id: str
    role: str
    email: str | None
    display_name: str | None
    joined_at: datetime
    is_me: bool


class InviteCreate(BaseModel):
    role: str = Field(default="member", pattern="^(member|admin)$")


class InviteOut(BaseModel):
    invite_code: str
    role: str
    expires_at: datetime


class InviteAccept(BaseModel):
    invite_code: str = Field(min_length=6, max_length=32)


def _same_household(principal: UserPrincipal, household_id: str) -> None:
    if principal.household_id != household_id:
        raise HTTPException(404, "resource_not_found")


def _manager(principal: UserPrincipal) -> None:
    if principal.role not in _MANAGERS:
        raise HTTPException(403, "forbidden")


@router.get("", response_model=list[HouseholdOut])
async def list_households(
    request: Request, identity: OidcIdentity = Depends(authenticated_identity)
) -> list[dict]:
    return await request.app.state.identity_repo.households(identity.issuer, identity.subject)


@router.post("/join", response_model=HouseholdOut)
async def join_household(body: InviteAccept, request: Request,
                         identity: OidcIdentity = Depends(authenticated_identity)) -> dict:
    repo = request.app.state.identity_repo
    settings = request.app.state.settings
    user_id = await repo.ensure_user(identity)
    try:
        household_id = await repo.accept_invite(
            body.invite_code, user_id,
            attempt_limit=settings.invite_attempt_limit,
            attempt_window_s=settings.invite_attempt_window_s,
        )
    except RateLimited as exc:
        raise HTTPException(429, "rate_limited") from exc
    except InviteInvalid as exc:
        raise HTTPException(404, "invite_not_found") from exc
    except AlreadyMember as exc:
        raise HTTPException(409, "already_member") from exc
    households = await repo.households(identity.issuer, identity.subject)
    return next(h for h in households if h["household_id"] == household_id)


@router.patch("/{household_id}", response_model=HouseholdOut)
async def rename_household(household_id: str, body: HouseholdRename, request: Request,
                           principal: UserPrincipal = Depends(authenticated_user)) -> dict:
    _same_household(principal, household_id)
    _manager(principal)
    await request.app.state.identity_repo.rename_household(household_id, body.name.strip())
    return {"household_id": household_id, "name": body.name.strip(), "role": principal.role}


@router.get("/{household_id}/members", response_model=list[MemberOut])
async def list_members(household_id: str, request: Request,
                       principal: UserPrincipal = Depends(authenticated_user)) -> list[dict]:
    _same_household(principal, household_id)
    rows = await request.app.state.identity_repo.members(household_id)
    return [
        {**row, "joined_at": datetime.fromtimestamp(row.pop("created_at"), timezone.utc),
         "is_me": row["user_id"] == principal.user_id}
        for row in rows
    ]


@router.post("/{household_id}/invites", response_model=InviteOut, status_code=201)
async def create_invite(household_id: str, body: InviteCreate, request: Request,
                        principal: UserPrincipal = Depends(authenticated_user)) -> dict:
    _same_household(principal, household_id)
    _manager(principal)
    if body.role == "admin" and principal.role != "owner":
        raise HTTPException(403, "forbidden")
    code, expires_at = await request.app.state.identity_repo.create_invite(
        household_id, principal.user_id, role=body.role,
        ttl_s=request.app.state.settings.household_invite_ttl_s,
    )
    return {"invite_code": code, "role": body.role,
            "expires_at": datetime.fromtimestamp(expires_at, timezone.utc)}


@router.delete("/{household_id}/members/{user_id}", status_code=204)
async def remove_member(household_id: str, user_id: str, request: Request,
                        principal: UserPrincipal = Depends(authenticated_user)) -> None:
    """Owners/admins remove others (only owners remove owners or admins); any
    member may leave. The last owner cannot leave or be removed."""
    _same_household(principal, household_id)
    repo = request.app.state.identity_repo
    if user_id != principal.user_id:
        _manager(principal)
        target = next((m for m in await repo.members(household_id) if m["user_id"] == user_id), None)
        if target is None:
            raise HTTPException(404, "resource_not_found")
        if target["role"] in _MANAGERS and principal.role != "owner":
            raise HTTPException(403, "forbidden")
    try:
        removed = await repo.remove_member(household_id, user_id)
    except LastOwner as exc:
        raise HTTPException(409, "last_owner") from exc
    if not removed:
        raise HTTPException(404, "resource_not_found")
