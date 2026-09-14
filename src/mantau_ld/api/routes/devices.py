"""Push token lifecycle -- register on login, unregister on logout/uninstall.
FCM itself prunes a dead token automatically; this route is for the
app-initiated cases.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from mantau_core.notify.channels.push.tokens import DeviceToken, Platform
from pydantic import BaseModel

from ...store.token_store import SqliteTokenStore
from ..deps import get_token_store

router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceRegister(BaseModel):
    device_id: str
    platform: str  # "android" | "ios"
    token: str


@router.post("/register", status_code=204)
async def register_device(
    body: DeviceRegister, store: SqliteTokenStore = Depends(get_token_store)
) -> None:
    store.register(DeviceToken(device_id=body.device_id, platform=Platform(body.platform), token=body.token))


@router.delete("/{token}", status_code=204)
async def unregister_device(token: str, store: SqliteTokenStore = Depends(get_token_store)) -> None:
    store.prune(token)
