"""Push token lifecycle -- register on login, unregister on logout/uninstall.
FCM itself prunes a dead token automatically; this route is for the
app-initiated cases.
"""

from __future__ import annotations
import asyncio

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from mantau_core.notify.channels.push.tokens import DeviceToken, Platform
from pydantic import BaseModel

from ...control_auth import authenticated_user, verified_identity, assert_identity_active
from ...oidc_auth import OidcIdentity
from ...store.identity_repo import UserPrincipal
from ...store.token_store import SqliteTokenStore
from ..deps import get_token_store

router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceRegister(BaseModel):
    device_id: str
    platform: str  # "android" | "ios"
    token: str


@router.post("/register", status_code=204)
async def register_device(
    body: DeviceRegister,
    request: Request,
    store: SqliteTokenStore = Depends(get_token_store),
    principal: UserPrincipal = Depends(authenticated_user),
) -> None:
    try:
        async with request.app.state.db.serialized():
            await asyncio.to_thread(store.register, DeviceToken(
                device_id=body.device_id, platform=Platform(body.platform), token=body.token,
                user_id=principal.user_id, household_id=principal.household_id,
            ))
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, "resource_conflict") from exc


@router.delete("/{device_id}", status_code=204)
async def unregister_device(
    device_id: str,
    request: Request,
    store: SqliteTokenStore = Depends(get_token_store),
    identity: OidcIdentity = Depends(verified_identity),
) -> None:
    async with request.app.state.db.serialized():
        await assert_identity_active(request, identity)
        row = await (await request.app.state.db.conn.execute(
            'SELECT user_id FROM user_identities WHERE oidc_issuer=? AND oidc_subject=?',
            (identity.issuer, identity.subject))).fetchone()
        if row is None:
            return
        await asyncio.to_thread(store.delete_owned,
            device_id, user_id=row[0], household_id=''
        )
