"""Households of the signed-in user, their members, and invites.

`GET /households` and `POST /households/join` need no household selection.
Every other route acts on the caller's selected household (the path id must
match it), so a user can never read or change another household.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...control_auth import authenticated_identity, authenticated_user, verified_identity, assert_identity_active
from ...oidc_auth import OidcIdentity
from ...store.identity_repo import (
    AlreadyMember, InviteInvalid, LastOwner, RateLimited, UserPrincipal,
)
from ...store.lifecycle_repo import LifecycleRepo, OwnershipRequired

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
    async with request.app.state.db.transaction():
        await assert_identity_active(request, identity)
        return await request.app.state.identity_repo.households(identity.issuer, identity.subject)


@router.post("/join", response_model=HouseholdOut)
async def join_household(body: InviteAccept, request: Request,
                         identity: OidcIdentity = Depends(authenticated_identity)) -> dict:
    repo = request.app.state.identity_repo
    settings = request.app.state.settings
    try:
        # The invite repository persists failed-attempt rate limits itself.
        # Hold serialization through the guard without rolling those attempts back.
        async with request.app.state.db.serialized():
            await assert_identity_active(request, identity)
            user_id = await repo.ensure_user(identity)
            household_id = await repo.accept_invite(
                body.invite_code, user_id,
                attempt_limit=settings.invite_attempt_limit,
                attempt_window_s=settings.invite_attempt_window_s,
            )
            households = await repo.households(identity.issuer, identity.subject)
    except RateLimited as exc:
        raise HTTPException(429, "rate_limited") from exc
    except InviteInvalid as exc:
        raise HTTPException(404, "invite_not_found") from exc
    except AlreadyMember as exc:
        raise HTTPException(409, "already_member") from exc
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


def _lifecycle(request):
    return LifecycleRepo(request.app.state.db, request.app.state.settings.recordings_dir)


@router.put('/{household_id}/owners/{user_id}', status_code=204)
async def promote_owner(household_id: str, user_id: str, request: Request,
                        principal: UserPrincipal = Depends(authenticated_user)):
    _same_household(principal, household_id)
    try:
        await _lifecycle(request).promote_owner(household_id, principal.user_id, user_id)
    except OwnershipRequired as exc:
        raise HTTPException(403, 'owner_required') from exc
    except LookupError as exc:
        raise HTTPException(404, 'resource_not_found') from exc


@router.get('/{household_id}/export')
async def export_household(household_id: str, request: Request,
                           principal: UserPrincipal = Depends(authenticated_user)):
    _same_household(principal, household_id)
    try:
        from fastapi.responses import JSONResponse
        return JSONResponse(await _lifecycle(request).export(household_id, principal.user_id),
                            headers={'Cache-Control': 'private, no-store'})
    except OwnershipRequired as exc:
        raise HTTPException(403, 'owner_required') from exc


class DeletionConfirmation(BaseModel):
    confirm_household_id: str


@router.delete('/{household_id}', status_code=204)
async def delete_household(household_id: str, body: DeletionConfirmation, request: Request,
                           principal: UserPrincipal = Depends(authenticated_user)):
    _same_household(principal, household_id)
    if body.confirm_household_id != household_id:
        raise HTTPException(400, 'confirmation_required')
    cameras = await request.app.state.cameras_repo.list_for_household(household_id)
    try:
        await _lifecycle(request).delete_household(household_id, principal.user_id)
    except OwnershipRequired as exc:
        raise HTTPException(403, 'owner_required') from exc
    except OSError as exc:
        raise HTTPException(503, 'data_deletion_unavailable') from exc
    for camera in cameras:
        request.app.state.frames.forget(camera.camera_id)


@router.delete('/account/me', status_code=204)
async def delete_account(request: Request, identity: OidcIdentity = Depends(verified_identity)):
    try:
        async with request.app.state.db.transaction():
            await assert_identity_active(request, identity)
            row = await (await request.app.state.db.conn.execute(
                'SELECT user_id FROM user_identities WHERE oidc_issuer=? AND oidc_subject=?',
                (identity.issuer, identity.subject))).fetchone()
            if row is not None:
                await _lifecycle(request).delete_user(row[0])
    except ValueError as exc:
        raise HTTPException(409, 'last_owner') from exc


@router.get('/{household_id}/diagnostics')
async def diagnostics(household_id: str, request: Request,
                      principal: UserPrincipal = Depends(authenticated_user)):
    _same_household(principal, household_id)
    async with request.app.state.db.serialized():
        deliveries = await (await request.app.state.db.conn.execute(
            'SELECT state,COUNT(*) AS count FROM push_outbox WHERE household_id=? GROUP BY state',
            (household_id,))).fetchall()
        storage = await (await request.app.state.db.conn.execute(
            'SELECT COALESCE(SUM(size_bytes),0) FROM recordings WHERE household_id=?',
            (household_id,))).fetchone()
    settings = request.app.state.settings
    return {'event_retention_days': settings.event_retention_days,
            'recording_retention_days': settings.recording_retention_days,
            'recording_bytes': storage[0], 'delivery_states': {row['state']:row['count'] for row in deliveries},
            'household_storage_limit_bytes': settings.recording_household_max_bytes,
            'inference_available': request.app.state.inference.available}
