"""Per-camera detection settings, versioned, household-owned."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from mantau_core.contracts import CommandType, DetectionSettings, Zone
from pydantic import ValidationError

from .control_repo import ControlRepo
from .db import Database

log = logging.getLogger("mantau_ld")

FLOOR_DEFAULT_MIGRATION = 4
OLD_FLOOR_MINUTES = 2.0
NEW_FLOOR_MINUTES = 0.5


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


def _floor_minutes(data):
    stillness = data.get("stillness") if isinstance(data, dict) else None
    return stillness.get("floor_minutes") if isinstance(stillness, dict) else None


async def migrate_floor_default(db: Database, control: ControlRepo, *, ttl_s: int) -> tuple[int, int] | None:
    """One-time move of stored `stillness.floor_minutes` from the old default
    2.0 to the new default 0.5 (schema_migrations version 4).

    Settings are stored whole, so a camera saved once through the app keeps
    2.0 and would never see the new default. Only an exact 2.0 changes; every
    other field and value stays as stored. Each migrated camera gets a new
    version, and a camera with an agent gets `apply_detection_settings`
    queued. Everything, including the version-4 mark, commits in one
    transaction or not at all. Returns (migrated cameras, queued commands),
    or None when an earlier startup already ran it."""
    conn = db.conn
    await conn.execute("BEGIN IMMEDIATE")
    try:
        done = await (await conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (FLOOR_DEFAULT_MIGRATION,),
        )).fetchone()
        if done is not None:
            await conn.rollback()
            return None
        rows = await (await conn.execute(
            "SELECT s.camera_id,s.household_id,s.settings_json,s.version,s.updated_by,c.agent_id "
            "FROM camera_detection_settings s LEFT JOIN cameras c ON c.camera_id=s.camera_id "
            "ORDER BY s.camera_id",
        )).fetchall()
        migrated = queued = 0
        now = time.time()
        for row in rows:
            try:
                data = json.loads(row["settings_json"])
            except ValueError:
                continue
            floor = _floor_minutes(data)
            if isinstance(floor, bool) or floor != OLD_FLOOR_MINUTES:
                continue
            version = row["version"] + 1
            data["stillness"]["floor_minutes"] = NEW_FLOOR_MINUTES
            data["version"] = version
            raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            try:
                settings = _tolerant(raw)
            except ValidationError:
                log.warning("floor-default migration: camera %s has unreadable settings; left as is",
                            row["camera_id"])
                continue
            # Keeps the previous editor: requested_by_user_id must name a user.
            await conn.execute(
                "UPDATE camera_detection_settings SET settings_json=?,version=?,updated_at=? "
                "WHERE camera_id=?", (raw, version, now, row["camera_id"]),
            )
            migrated += 1
            if row["agent_id"]:
                await control.queue(
                    agent_id=row["agent_id"], household_id=row["household_id"],
                    requested_by_user_id=row["updated_by"],
                    command_type=CommandType.APPLY_DETECTION_SETTINGS,
                    payload={"camera_id": row["camera_id"],
                             "settings": settings.model_dump(mode="json")},
                    idempotency_key=(f"detection-settings-{row['camera_id']}-{version}"
                                     "-floor-default-migration"),
                    ttl_s=ttl_s, commit=False,
                )
                queued += 1
        await conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (FLOOR_DEFAULT_MIGRATION, now),
        )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    return migrated, queued
