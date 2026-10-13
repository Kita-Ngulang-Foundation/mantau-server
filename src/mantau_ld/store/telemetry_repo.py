"""Field-trial telemetry. Trial rows exist only for households in `trial_cohort`
while today (in the cohort's own timezone) is inside its dates.

Operational phone receipts update existing notification delivery rows for all
households. They contain only identifiers and a timestamp, without media.

Every write is best effort: a failed write is logged by exception type and
dropped. Nothing here may fail or delay an event insert, a push, a heartbeat
accept or a fall-confirmation timer. Nothing here calls `db.conn.rollback()`,
which inside an atomic block would fail the product transaction; writes that
run inside one use a savepoint on the raw connection instead.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from ..config import Settings, build_info
from ..inference.service import config_hash
from .db import Database
from .detection_settings_repo import DetectionSettingsRepo

log = logging.getLogger("mantau_ld")

COHORT_TTL_S = 60.0
STAMP_COLUMNS = (
    "captured_at", "detected_at", "queued_at", "first_sent_at", "first_delivered_at",
    "candidate_at", "confirmed_at", "build_json",
)
OUTBOX_STAMP_COLUMNS = ("sent_at", "delivered_at")
MINUTE_COLUMNS = (
    "household_id", "agent_id", "camera_id", "minute", "frames_received", "frames_processed",
    "rate_limited", "out_of_order", "undecodable", "confirmations", "server_ms_mean",
    "server_ms_max", "frames_people_0", "frames_people_1", "frames_people_multi",
    "cov_no_person_s", "cov_one_person_s", "cov_multi_person_s", "cov_low_confidence_s",
    "cov_outage_s", "events_emitted", "hidden_low_confidence", "hidden_excluded",
    "capture_lag_ms_min", "server_build", "config_hash",
)
# Counters that add up across flushes of the same minute.
MINUTE_ADD = (
    "frames_received", "frames_processed", "rate_limited", "out_of_order", "undecodable",
    "confirmations", "frames_people_0", "frames_people_1", "frames_people_multi",
    "events_emitted", "hidden_low_confidence", "hidden_excluded",
)
MINUTE_SUM = (
    "cov_no_person_s", "cov_one_person_s", "cov_multi_person_s", "cov_low_confidence_s",
    "cov_outage_s",
)


def _minute_upsert_sql() -> str:
    sets = [f"{c}=COALESCE(camera_minutes.{c},0)+COALESCE(excluded.{c},0)"
            for c in (*MINUTE_ADD, *MINUTE_SUM)]
    sets.append(
        "server_ms_max=CASE WHEN camera_minutes.server_ms_max IS NULL THEN excluded.server_ms_max "
        "WHEN excluded.server_ms_max IS NULL THEN camera_minutes.server_ms_max "
        "ELSE MAX(camera_minutes.server_ms_max,excluded.server_ms_max) END")
    sets.append(
        "capture_lag_ms_min=CASE WHEN camera_minutes.capture_lag_ms_min IS NULL THEN excluded.capture_lag_ms_min "
        "WHEN excluded.capture_lag_ms_min IS NULL THEN camera_minutes.capture_lag_ms_min "
        "ELSE MIN(camera_minutes.capture_lag_ms_min,excluded.capture_lag_ms_min) END")
    # Frame-weighted mean: frames_processed is the weight on both sides.
    sets.append(
        "server_ms_mean=CASE WHEN camera_minutes.server_ms_mean IS NULL OR COALESCE(camera_minutes.frames_processed,0)=0 "
        "THEN excluded.server_ms_mean "
        "WHEN excluded.server_ms_mean IS NULL OR COALESCE(excluded.frames_processed,0)=0 "
        "THEN camera_minutes.server_ms_mean "
        "ELSE (camera_minutes.server_ms_mean*COALESCE(camera_minutes.frames_processed,0)"
        "+excluded.server_ms_mean*COALESCE(excluded.frames_processed,0))*1.0"
        "/(COALESCE(camera_minutes.frames_processed,0)+COALESCE(excluded.frames_processed,0)) END")
    sets.append("server_build=excluded.server_build")
    sets.append("config_hash=excluded.config_hash")
    placeholders = ",".join("?" * len(MINUTE_COLUMNS))
    return (f"INSERT INTO camera_minutes({','.join(MINUTE_COLUMNS)}) VALUES({placeholders}) "
            f"ON CONFLICT(agent_id,camera_id,minute) DO UPDATE SET {','.join(sets)}")


MINUTE_UPSERT = _minute_upsert_sql()


def _in_window(start, end, tz) -> bool:
    """Today in the cohort's timezone is between its dates, inclusive. Bad data is outside."""
    try:
        today = datetime.now(ZoneInfo(tz)).date().isoformat()
        return str(start) <= today <= str(end)
    except Exception:  # noqa: BLE001 -- unknown timezone or missing tzdata
        return False


def _best_effort(default):
    """Turn any failure into a logged, dropped write that returns `default`."""
    def decorate(fn):
        @wraps(fn)
        async def operation(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- telemetry never raises
                log.warning("telemetry %s dropped (%s)", fn.__name__, type(exc).__name__)
                return default
        return operation
    return decorate


class TelemetryRepo:
    def __init__(self, db: Database, settings: Settings | None = None) -> None:
        self._db = db
        self._settings = settings
        self._cohort: dict[str, tuple] = {}
        self._cohort_loaded_at: float | None = None

    # -- cohort ----------------------------------------------------------------
    @_best_effort(False)
    async def in_cohort(self, household_id: str) -> bool:
        if self._cohort_loaded_at is None or time.monotonic() - self._cohort_loaded_at > COHORT_TTL_S:
            await self._load_cohort()
        window = self._cohort.get(household_id)
        return window is not None and _in_window(*window)

    async def _load_cohort(self) -> None:
        self._cohort_loaded_at = time.monotonic()
        try:
            async with self._db.serialized():
                rows = await (await self._db.conn.execute(
                    "SELECT household_id,start_date,end_date,tz FROM trial_cohort")).fetchall()
            self._cohort = {row["household_id"]: (row["start_date"], row["end_date"], row["tz"])
                            for row in rows}
        except Exception as exc:  # noqa: BLE001 -- no cohort means no telemetry, nothing else
            self._cohort = {}
            log.warning("trial cohort unreadable (%s)", type(exc).__name__)

    # -- writes ----------------------------------------------------------------
    async def _write(self, statements: list[tuple[str, tuple]]) -> None:
        """Run `statements` as one best-effort unit. Never raises."""
        try:
            async with self._db.serialized():
                if self._db.in_atomic:
                    await self._write_in_savepoint(statements)
                else:
                    await self._write_autocommit(statements)
        except Exception as exc:  # noqa: BLE001 -- dropped telemetry, not a failed product write
            log.warning("telemetry write dropped (%s)", type(exc).__name__)

    async def _write_in_savepoint(self, statements: list[tuple[str, tuple]]) -> None:
        conn = self._db._conn
        await conn.execute("SAVEPOINT telemetry")
        try:
            for sql, params in statements:
                await conn.execute(sql, params)
        except Exception:
            try:
                await conn.execute("ROLLBACK TO telemetry")
            except Exception:  # noqa: BLE001
                pass
            try:
                await conn.execute("RELEASE telemetry")
            except Exception:  # noqa: BLE001
                pass
            raise
        await conn.execute("RELEASE telemetry")

    async def _write_autocommit(self, statements: list[tuple[str, tuple]]) -> None:
        try:
            for sql, params in statements:
                await self._db.conn.execute(sql, params)
            await self._db.conn.commit()
        except Exception:
            try:
                await self._db._conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise

    async def _fetch_one(self, sql: str, params: tuple = ()):
        async with self._db.serialized():
            return await (await self._db.conn.execute(sql, params)).fetchone()

    @_best_effort(None)
    async def stamp_event(self, event_id: str, household_id: str, **columns) -> None:
        """First value wins: each stamp is written only while the column is NULL."""
        values = {name: value for name, value in columns.items()
                  if name in STAMP_COLUMNS and value is not None}
        if not values or not await self.in_cohort(household_id):
            return
        await self._write([
            (f"UPDATE events SET {name}=? WHERE event_id=? AND {name} IS NULL", (value, event_id))
            for name, value in values.items()
        ])

    @_best_effort(None)
    async def stamp_outbox(self, row_id: int, household_id: str, column: str, at: float) -> None:
        if column not in OUTBOX_STAMP_COLUMNS or not await self.in_cohort(household_id):
            return
        await self._write([(f"UPDATE push_outbox SET {column}=? WHERE id=? AND {column} IS NULL",
                            (at, row_id))])

    @_best_effort(None)
    async def log_heartbeat(self, household_id: str, heartbeat, received_at: float) -> None:
        if not await self.in_cohort(household_id):
            return
        stats = heartbeat.stats
        await self._write([(
            "INSERT INTO heartbeat_log(household_id,agent_id,camera_id,sent_at,received_at,"
            "camera_reachable,detector_alive,queue_depth,agent_version,stats_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (household_id, heartbeat.agent_id, heartbeat.camera_id, heartbeat.sent_at.timestamp(),
             received_at, int(heartbeat.camera_reachable), int(heartbeat.detector_alive),
             heartbeat.queue_depth, heartbeat.agent_version,
             json.dumps(stats, sort_keys=True) if stats else None),
        )])

    @_best_effort(None)
    async def upsert_minutes(self, rows: list[dict]) -> None:
        statements = []
        for row in rows:
            if await self.in_cohort(row.get("household_id")):
                statements.append((MINUTE_UPSERT, tuple(row.get(name) for name in MINUTE_COLUMNS)))
        if statements:
            await self._write(statements)

    @_best_effort(None)
    async def add_usage(self, household_id: str, user_id: str, rows: list[dict]) -> None:
        if not rows or not await self.in_cohort(household_id):
            return
        await self._write([(
            "INSERT INTO usage_events(household_id,user_id,type,at,event_id,value,app_version) "
            "VALUES(?,?,?,?,?,?,?)",
            (household_id, user_id, row.get("type"), row.get("at"), row.get("event_id"),
             row.get("value"), row.get("app_version")),
        ) for row in rows])

    @_best_effort(None)
    async def mark_received(self, household_id: str, user_id: str, device_id: str,
                            event_id: str, at: float) -> None:
        await self._write([(
            "UPDATE push_outbox SET received_at=? WHERE event_id=? AND device_id=? AND user_id=? "
            "AND household_id=? AND received_at IS NULL",
            (at, event_id, device_id, user_id, household_id),
        )])

    @_best_effort(None)
    async def log_token_unregistered(self, household_id: str, user_id: str, at: float) -> None:
        if not await self.in_cohort(household_id):
            return
        await self._write([(
            "INSERT INTO usage_events(household_id,user_id,type,at) VALUES(?,?,'token_unregistered',?)",
            (household_id, user_id, at),
        )])

    # -- reads -----------------------------------------------------------------
    @_best_effort(None)
    async def latest_agent_version(self, agent_id: str) -> str | None:
        row = await self._fetch_one(
            "SELECT agent_version FROM heartbeat_log WHERE agent_id=? AND agent_version IS NOT NULL "
            "ORDER BY received_at DESC LIMIT 1", (agent_id,))
        return row["agent_version"] if row is not None else None

    @_best_effort(None)
    async def build_json_for(self, household_id: str, camera_id: str, agent_id: str) -> str | None:
        """Build and detector identity an event was made with, for trial cohort only."""
        if not await self.in_cohort(household_id):
            return None
        settings = (await DetectionSettingsRepo(self._db).get(household_id, camera_id)).settings
        build = build_info(self._settings)
        payload = {
            "server": build["server"], "core": build["core"], "ai": build["ai"],
            "detector": settings.fall.model, "config_hash": config_hash(settings),
            "agent": await self.latest_agent_version(agent_id),
        }
        return json.dumps(payload, sort_keys=True)
