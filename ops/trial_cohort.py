"""Field-trial cohort and team-entered missed-event reports.

Run from /app in the server container against the live database file
(/data/mantau_ld.db). Telemetry is written only for households listed in
trial_cohort, and only inside their trial dates. Add a household only after its
participation agreement is signed. Missed reports entered here are the team's
WhatsApp reports; they carry no free text.

The table definitions are copied verbatim from src/mantau_ld/store/db.py so the
schema cannot drift between this tool and the server.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

COHORT_TABLE = """CREATE TABLE IF NOT EXISTS trial_cohort (
    household_id  TEXT PRIMARY KEY,
    code          TEXT UNIQUE,
    start_date    TEXT,
    end_date      TEXT,
    tz            TEXT
)"""
MISSED_TABLE = """CREATE TABLE IF NOT EXISTS missed_reports (
    report_id      TEXT PRIMARY KEY,
    household_id   TEXT NOT NULL,
    source         TEXT NOT NULL,
    reported_by    TEXT,
    created_at     REAL NOT NULL,
    occurred_from  REAL NOT NULL,
    occurred_to    REAL NOT NULL,
    kind           TEXT NOT NULL,
    camera_id      TEXT
)"""
MISSED_INDEX = ('CREATE INDEX IF NOT EXISTS idx_missed_reports_household '
                'ON missed_reports(household_id, created_at)')
SCHEMA_STATEMENTS = (COHORT_TABLE, MISSED_TABLE, MISSED_INDEX)
CODE_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,16}')
DATE_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}')
KINDS = ('fall', 'stillness', 'nocturnal_movement', 'bathroom_duration', 'other')
MAX_WINDOW_SECONDS = 24 * 3600
FUTURE_SLACK_SECONDS = 300


class CliError(Exception):
    """A validation failure shown to the operator without a traceback."""


def ensure_tables(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()


def require_household(connection: sqlite3.Connection, household: str) -> None:
    found = connection.execute('SELECT 1 FROM households WHERE household_id = ?', (household,)).fetchone()
    if found is None:
        raise CliError(f'Household {household} does not exist')


def cohort_row(connection: sqlite3.Connection, household: str):
    return connection.execute(
        'SELECT code, start_date, end_date, tz FROM trial_cohort WHERE household_id = ?', (household,)
    ).fetchone()


def parse_day(value: str, label: str) -> date:
    if DATE_PATTERN.fullmatch(value) is None:
        raise CliError(f'{label} must be YYYY-MM-DD, got {value!r}')
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CliError(f'{label} is not a valid date: {value!r}') from error


def check_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except Exception as error:
        raise CliError(f'Unknown time zone: {name!r}') from error
    return name


def cmd_add(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    require_household(connection, args.household)
    if CODE_PATTERN.fullmatch(args.code) is None:
        raise CliError('code must match [A-Za-z0-9_-], 1 to 16 characters')
    if connection.execute('SELECT 1 FROM households WHERE household_id = ?', (args.code,)).fetchone() is not None:
        raise CliError(f'code {args.code} equals a household id; choose another code')
    start = parse_day(args.start, 'start')
    end = parse_day(args.end, 'end')
    if start > end:
        raise CliError('start must be on or before end')
    zone = check_zone(args.tz)
    existing = cohort_row(connection, args.household)
    changes = []
    if existing is not None:
        old_code, old_start, old_end, old_zone = existing
        if old_code != args.code:
            raise CliError(f'household {args.household} already has code {old_code}; the code cannot be changed')
        for label, old, new in (('start', old_start, start.isoformat()),
                                ('end', old_end, end.isoformat()),
                                ('tz', old_zone, zone)):
            if old != new:
                changes.append(f'{label} {old} -> {new}')
    taken = connection.execute(
        'SELECT household_id FROM trial_cohort WHERE code = ? AND household_id <> ?',
        (args.code, args.household),
    ).fetchone()
    if taken is not None:
        raise CliError(f'code {args.code} is already used by another household')
    connection.execute(
        'INSERT OR REPLACE INTO trial_cohort (household_id, code, start_date, end_date, tz) '
        'VALUES (?, ?, ?, ?, ?)',
        (args.household, args.code, start.isoformat(), end.isoformat(), zone),
    )
    connection.commit()
    if existing is None:
        print(f'Cohort added: {args.household} code {args.code} {start.isoformat()} to {end.isoformat()} {zone}')
    else:
        print(f'Cohort updated: {args.household} code {args.code}; '
              + ('; '.join(changes) if changes else 'no field changes'))


def cmd_end(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = cohort_row(connection, args.household)
    if row is None:
        raise CliError(f'Household {args.household} is not in the trial cohort')
    end = parse_day(args.date, 'date')
    if end < date.fromisoformat(row[1]):
        raise CliError(f'end date must be on or after the start date {row[1]}')
    connection.execute(
        'UPDATE trial_cohort SET end_date = ? WHERE household_id = ?', (end.isoformat(), args.household)
    )
    connection.commit()
    print(f'Cohort end set: {args.household} ends {end.isoformat()}')


def cmd_list(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = connection.execute(
        'SELECT household_id, code, start_date, end_date, tz FROM trial_cohort ORDER BY code'
    ).fetchall()
    for row in rows:
        print('\t'.join(str(value) for value in row))


def parse_instant(value: str, zone_name: str | None) -> float:
    text = value.strip()
    if text[-1:] in ('Z', 'z'):
        text = text[:-1] + '+00:00'
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise CliError(f'Invalid datetime: {value!r}') from error
    if moment.tzinfo is None:
        if zone_name is None:
            raise CliError(f'Naive datetime {value!r} needs a household in the trial cohort (its tz applies)')
        moment = moment.replace(tzinfo=ZoneInfo(zone_name))
    return moment.timestamp()


def cmd_missed(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    require_household(connection, args.household)
    row = cohort_row(connection, args.household)
    zone_name = row[3] if row is not None else None
    start = parse_instant(args.occurred_from, zone_name)
    end = parse_instant(args.occurred_to, zone_name)
    if start > end:
        raise CliError('--from must be on or before --to')
    if end - start > MAX_WINDOW_SECONDS:
        raise CliError('the report window must be at most 24 hours')
    if end > time.time() + FUTURE_SLACK_SECONDS:
        raise CliError('the report window is in the future by more than 5 minutes')
    if args.camera is not None:
        camera = connection.execute(
            'SELECT 1 FROM cameras WHERE camera_id = ? AND household_id = ?', (args.camera, args.household)
        ).fetchone()
        if camera is None:
            raise CliError(f'Camera {args.camera} does not belong to household {args.household}')
    report_id = uuid.uuid4().hex
    connection.execute(
        'INSERT INTO missed_reports (report_id, household_id, source, reported_by, created_at, '
        'occurred_from, occurred_to, kind, camera_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (report_id, args.household, 'team', 'team', time.time(), start, end, args.kind, args.camera),
    )
    connection.commit()
    print(f'Missed report {report_id} recorded: {args.household} {args.kind} {start} to {end}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, required=True, help='Existing server SQLite database file')
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('add', help='Add or update a household in the trial cohort')
    add.add_argument('--household', required=True)
    add.add_argument('--code', required=True)
    add.add_argument('--start', required=True, help='YYYY-MM-DD')
    add.add_argument('--end', required=True, help='YYYY-MM-DD')
    add.add_argument('--tz', required=True, help='IANA zone, e.g. Asia/Jakarta')
    end = commands.add_parser('end', help='Set the last trial date of a cohort household')
    end.add_argument('--household', required=True)
    end.add_argument('--date', required=True, help='YYYY-MM-DD')
    commands.add_parser('list', help='List the trial cohort')
    missed = commands.add_parser('missed', help='Record a team-reported missed event (WhatsApp)')
    missed.add_argument('--household', required=True)
    missed.add_argument('--from', dest='occurred_from', required=True, help='ISO datetime')
    missed.add_argument('--to', dest='occurred_to', required=True, help='ISO datetime')
    missed.add_argument('--kind', required=True, choices=KINDS)
    missed.add_argument('--camera', help='Camera id of the household (optional)')
    args = parser.parse_args()

    handlers = {'add': cmd_add, 'end': cmd_end, 'list': cmd_list, 'missed': cmd_missed}
    try:
        if not args.db.is_file():
            raise CliError(f'database file not found: {args.db}')
        with closing(sqlite3.connect(str(args.db), timeout=30)) as connection:
            connection.execute('PRAGMA busy_timeout=30000')
            if args.command != 'list':
                ensure_tables(connection)
            handlers[args.command](connection, args)
    except (CliError, sqlite3.Error, ZoneInfoNotFoundError, TypeError, ValueError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
