"""Per-camera detection settings, versioned, household-owned."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from mantau_core.contracts import DetectionSettings, Zone
from pydantic import ValidationError

from .db import Database


@dataclass(frozen=True)
class StoredSettings:
    settings: DetectionSettings
    applied_version: int | None
    stored: bool


def _tolerant(raw: str) -> DetectionSettings:
    """Stored settings; zones saved before outline validation existed that are
    now invalid (self-intersecting, degenerate) are dropped rather than failing."""
    try:
        return DetectionSettings.model_validate_json(raw)
    except ValidationError:
        data = json.loads(raw)
        valid = []
        for zone in data.get("zones", []):
            try:
                valid.append(Zone.model_validate(zone))
            except ValidationError:
                continue
        data["zones"] = [z.model_dump(mode="json") for z in valid]
        return DetectionSettings.model_validate(data)


class DetectionSettingsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, household_id: str, camera_id: str) -> StoredSettings:
        row = await (await self._db.conn.execute(
            "SELECT settings_json, applied_version FROM camera_detection_settings "
            "WHERE household_id=? AND camera_id=?", (household_id, camera_id),
        )).fetchone()
        if row is None:
            return StoredSettings(DetectionSettings(), None, False)
        return StoredSettings(_tolerant(row["settings_json"]), row["applied_version"], True)

    async def save(self, household_id: str, camera_id: str, settings: DetectionSettings,
                   user_id: str) -> DetectionSettings:
        """Stores `settings` as the next version; the client's version is
        ignored so two editors never produce the same number."""
        await self._db.conn.execute("BEGIN IMMEDIATE")
        try:
            row = await (await self._db.conn.execute(
                "SELECT version FROM camera_detection_settings WHERE camera_id=?", (camera_id,),
            )).fetchone()
            version = (row["version"] if row else 1) + 1
            stored = settings.model_copy(update={"version": version})
            await self._db.conn.execute(
                "INSERT INTO camera_detection_settings(camera_id,household_id,settings_json,version,"
                "updated_at,updated_by) VALUES(?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET "
                "settings_json=excluded.settings_json, version=excluded.version, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by "
                "WHERE camera_detection_settings.household_id=excluded.household_id",
                (camera_id, household_id, stored.model_dump_json(), version, time.time(), user_id),
            )
            await self._db.conn.commit()
        except BaseException:
            await self._db.conn.rollback()
            raise
        return stored

    async def mark_applied(self, agent_id: str, camera_id: str, version: int) -> None:
        """Only for a camera served by the reporting agent."""
        await self._db.conn.execute(
            "UPDATE camera_detection_settings SET applied_version=?, applied_at=? "
            "WHERE camera_id=? AND version>=? AND EXISTS("
            "SELECT 1 FROM cameras c WHERE c.camera_id=? AND c.agent_id=?)",
            (version, time.time(), camera_id, version, camera_id, agent_id),
        )
        await self._db.conn.commit()
