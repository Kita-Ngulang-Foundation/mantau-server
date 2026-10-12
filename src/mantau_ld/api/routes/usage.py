"""App usage events from household members (field-trial telemetry).

Items are checked one by one and filtered, never rejected: an unknown type,
an unknown value or a bad timestamp drops that item only. Only the batch size
and the top-level shape can fail the request (422). Usage rows are stored
through TelemetryRepo, which is cohort-gated and never raises.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...control_auth import authenticated_user
from ...store.db import Database
from ...store.events_repo import EventsRepo
from ...store.identity_repo import UserPrincipal
from ...store.telemetry_repo import TelemetryRepo
from ..deps import get_db, get_events_repo, get_telemetry_repo

router = APIRouter(prefix="/usage", tags=["usage"])

MAX_ITEMS = 100
MAX_AGE_S = 30 * 86400
MAX_SKEW_S = 300
APP_VERSION_PATTERN = re.compile(r"[0-9A-Za-z._+-]{1,32}")

SCREENS = frozenset({
    "home", "live_view", "notifications", "missed_event", "recordings", "recording_detail",
    "profile", "edit_profile", "family", "emergency_contacts", "data_lifecycle",
    "detection_settings", "zone_editor", "agent_setup",
})

# type -> allowed values. An empty set means the type takes no value (None only).
USAGE_TYPES: dict[str, frozenset[str]] = {
    "app_open": frozenset(),
    "screen_view": SCREENS,
    "notification_received": frozenset(),
    "notification_opened": frozenset({"push_tap", "in_app"}),
    "notification_permission": frozenset({"enabled", "disabled"}),
}


class UsageBatch(BaseModel):
    items: list[Any] = Field(max_length=MAX_ITEMS)


class UsageAccepted(BaseModel):
    accepted: int
    dropped: int


def _parse(item: Any, now: float) -> tuple[dict, str | None] | None:
    """One raw item -> (usage row without device id, device id), or None when dropped."""
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if not isinstance(kind, str) or kind not in USAGE_TYPES:
        return None
    allowed = USAGE_TYPES[kind]
    if allowed:
        raw_value = item.get("value")
        if not isinstance(raw_value, str) or raw_value not in allowed:
            return None
        value: str | None = raw_value
    else:
        value = None  # a value given for a type without one is dropped, the item is kept
    at = item.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    try:
        at = float(at)
    except OverflowError:
        return None  # an int too large for a float
    if not math.isfinite(at):
        return None
    if at > now + MAX_SKEW_S or at < now - MAX_AGE_S:
        return None
    event_id = item.get("event_id")
    event_id = event_id if isinstance(event_id, str) and event_id else None
    device_id = item.get("device_id")
    device_id = device_id if isinstance(device_id, str) and device_id else None
    app_version = item.get("app_version")
    # A build string only: no free text reaches the usage log.
    app_version = (app_version if isinstance(app_version, str)
                   and APP_VERSION_PATTERN.fullmatch(app_version) else None)
    row = {"type": kind, "at": float(at), "event_id": event_id, "value": value,
           "app_version": app_version}
    return row, device_id


async def _owns_device(db: Database, user_id: str, device_id: str) -> bool:
    async with db.serialized():
        row = await (await db.conn.execute(
            "SELECT 1 FROM device_tokens WHERE device_id=? AND user_id=?",
            (device_id, user_id),
        )).fetchone()
    return row is not None


@router.post("", status_code=202, response_model=UsageAccepted)
async def post_usage(
    body: UsageBatch,
    db: Database = Depends(get_db),
    events: EventsRepo = Depends(get_events_repo),
    telemetry: TelemetryRepo = Depends(get_telemetry_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> UsageAccepted:
    now = time.time()
    parsed = [p for p in (_parse(item, now) for item in body.items) if p is not None]
    candidate_ids = {row["event_id"] for row, _ in parsed if row["event_id"]}
    known_ids = await events.household_event_ids(principal.household_id, list(candidate_ids))
    rows: list[dict] = []
    for row, device_id in parsed:
        if row["event_id"] not in known_ids:
            row["event_id"] = None  # not one of this household's events: keep the item, drop the link
        if (row["type"] == "notification_received" and device_id and row["event_id"]
                and await _owns_device(db, principal.user_id, device_id)):
            await telemetry.mark_received(principal.household_id, principal.user_id,
                                          device_id, row["event_id"], row["at"])
        rows.append(row)
    await telemetry.add_usage(principal.household_id, principal.user_id, rows)
    return UsageAccepted(accepted=len(rows), dropped=len(body.items) - len(rows))
