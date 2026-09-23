"""Push token lifecycle -- register on login, unregister on logout/uninstall.
FCM itself prunes a dead token automatically; this route is for the
app-initiated cases.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from mantau_core.notify.channels.push.tokens import DeviceToken, Platform
from pydantic import BaseModel

from ...control_auth import authenticated_user
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
    store: SqliteTokenStore = Depends(get_token_store),
    principal: UserPrincipal = Depends(authenticated_user),
) -> None:
    try:
        store.register(DeviceToken(
            device_id=body.device_id, platform=Platform(body.platform), token=body.token,
            user_id=principal.user_id, household_id=principal.household_id,
        ))
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, "resource_conflict") from exc


@router.delete("/{device_id}", status_code=204)
async def unregister_device(
    device_id: str,
    store: SqliteTokenStore = Depends(get_token_store),
    principal: UserPrincipal = Depends(authenticated_user),
) -> None:
    store.delete_owned(
        device_id, user_id=principal.user_id, household_id=principal.household_id
    )
