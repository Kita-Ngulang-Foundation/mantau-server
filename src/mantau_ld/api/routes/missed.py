"""Missed-event reports from household members. Free text is not accepted:
the body holds a time window, a kind from a fixed list and an optional camera.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ...control_auth import authenticated_user
from ...store.cameras_repo import CamerasRepo
from ...store.db import Database
from ...store.identity_repo import UserPrincipal
from ...store.missed_repo import MissedRepo
from ..deps import get_cameras_repo, get_db

router = APIRouter(prefix="/missed-events", tags=["missed-events"])

MAX_WINDOW = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)
MissedKind = Literal["fall", "stillness", "nocturnal_movement", "bathroom_duration", "other"]


class MissedReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurred_from: datetime
    occurred_to: datetime
    kind: MissedKind
    camera_id: str | None = None

    @field_validator("occurred_from", "occurred_to")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value

    @model_validator(mode="after")
    def _window_rules(self) -> "MissedReportIn":
        if self.occurred_from > self.occurred_to:
            raise ValueError("occurred_from_after_occurred_to")
        if self.occurred_to - self.occurred_from > MAX_WINDOW:
            raise ValueError("window_longer_than_24_hours")
        if self.occurred_to > datetime.now(self.occurred_to.tzinfo) + MAX_FUTURE_SKEW:
            raise ValueError("occurred_to_in_future")
        return self


class MissedReportOut(BaseModel):
    report_id: str
    source: str
    created_at: float
    occurred_from: float
    occurred_to: float
    kind: str
    camera_id: str | None = None


@router.post("", response_model=MissedReportOut, status_code=201)
async def create_missed_report(
    body: MissedReportIn,
    db: Database = Depends(get_db),
    cameras: CamerasRepo = Depends(get_cameras_repo),
    principal: UserPrincipal = Depends(authenticated_user),
) -> MissedReportOut:
    if body.camera_id is not None:
        if await cameras.get_for_household(principal.household_id, body.camera_id) is None:
            raise HTTPException(404, "resource_not_found")
    report = MissedReportOut(
        report_id=uuid.uuid4().hex, source="app", created_at=time.time(),
        occurred_from=body.occurred_from.timestamp(), occurred_to=body.occurred_to.timestamp(),
        kind=body.kind, camera_id=body.camera_id,
    )
    await MissedRepo(db).create(
        report_id=report.report_id, household_id=principal.household_id, source=report.source,
        reported_by=principal.user_id, created_at=report.created_at,
        occurred_from=report.occurred_from, occurred_to=report.occurred_to,
        kind=report.kind, camera_id=report.camera_id,
    )
    return report


@router.get("", response_model=list[MissedReportOut])
async def list_missed_reports(
    db: Database = Depends(get_db),
    principal: UserPrincipal = Depends(authenticated_user),
) -> list[MissedReportOut]:
    rows = await MissedRepo(db).list_for_household(principal.household_id, limit=200)
    return [MissedReportOut(**row) for row in rows]


@router.delete("/{report_id}", status_code=204)
async def delete_missed_report(
    report_id: str,
    db: Database = Depends(get_db),
    principal: UserPrincipal = Depends(authenticated_user),
) -> Response:
    if not await MissedRepo(db).delete(principal.household_id, report_id):
        raise HTTPException(404, "resource_not_found")
    return Response(status_code=204)
