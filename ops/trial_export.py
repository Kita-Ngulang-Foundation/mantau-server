"""Field-trial telemetry export. Read-only; it enforces the anonymity rules.

Run from /app in the server container. Reads the database through one
read-only snapshot and writes CSV files into --out (created if missing, must be
empty if it exists). Only households in trial_cohort are exported, and only rows
inside each household's trial dates.

--kind engineering: one combined CSV per table, keyed by cohort code in the
household column; cameras, agents and members appear as per-household indexes
(C1, A1, M1). Camera names, user ids, device ids, push tokens, emails and alert
payloads are never written. The output is pseudonymous, not anonymous: store it
privately. --include-notes adds the free-text dismiss note to events.csv.

--kind investor: summary.csv only. Aggregates across households. The households
column counts the contributing households (those with at least one underlying
row for that figure); a figure with fewer than 3 is suppressed. Latency
percentiles also need at least 10 samples. No codes, no per-household rows, no
exact timestamps and no free text. Group labels are whitelisted.

Monitored time: a camera's trial minutes run from max(window start, its
registration minute) to min(window end, now, its revocation). A minute m (its
UTC start epoch) is monitored when a heartbeat from that camera (or a
camera-less heartbeat from its agent) has camera_reachable = 1 and
|sent_at - m| <= 90 seconds, or when camera_minutes has frames_received > 0 for
that camera and minute. detector_alive is not used.

The tz database must be present (the python slim image ships tzdata; on Windows
install the tzdata package to run against a copy).
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_HOUSEHOLDS = 3
LATENCY_MIN_SAMPLES = 10
MONITOR_TOLERANCE_SECONDS = 90
DAY_SECONDS = 86400
DAYPARTS = ('00-06', '06-12', '12-18', '18-24')
LATENCY_NAMES = ('captured_to_delivered', 'delivered_to_received', 'received_to_opened')
INVESTOR_KINDS = ('fall', 'stillness', 'nocturnal_movement', 'bathroom_duration')
INVESTOR_SEVERITIES = ('info', 'warning', 'critical')
INVESTOR_DISMISS_REASONS = ('kejadian_ringan', 'tidak_ada_kejadian', 'lainnya')
USAGE_TYPES = (
    'app_open', 'screen_view', 'notification_received', 'notification_opened',
    'notification_permission', 'token_unregistered',
)
USAGE_VALUES = {
    'screen_view': frozenset({
        'home', 'live_view', 'notifications', 'missed_event', 'recordings', 'recording_detail', 'profile',
        'edit_profile', 'family', 'emergency_contacts', 'data_lifecycle', 'detection_settings', 'zone_editor',
        'agent_setup',
    }),
    'notification_opened': frozenset({'push_tap', 'in_app'}),
    'notification_permission': frozenset({'enabled', 'disabled'}),
}
ENGINEERING_FILES = ('events.csv', 'minutes.csv', 'heartbeats.csv', 'missed.csv', 'usage.csv', 'uptime.csv')
MINUTE_COLUMNS = (
    'agent_id', 'camera_id', 'minute', 'frames_received', 'frames_processed', 'rate_limited',
    'out_of_order', 'undecodable', 'confirmations', 'server_ms_mean', 'server_ms_max',
    'frames_people_0', 'frames_people_1', 'frames_people_multi', 'cov_no_person_s', 'cov_one_person_s',
    'cov_multi_person_s', 'cov_low_confidence_s', 'cov_outage_s', 'events_emitted',
    'hidden_low_confidence', 'hidden_excluded', 'capture_lag_ms_min', 'server_build', 'config_hash',
)
EVENT_COLUMNS = (
    'event_id', 'camera_id', 'agent_id', 'kind', 'severity', 'zone_id', 'status', 'confidence',
    'created_at', 'occurred_at', 'captured_at', 'detected_at', 'queued_at', 'first_sent_at',
    'first_delivered_at', 'candidate_at', 'confirmed_at', 'acknowledged_at', 'reviewed_at',
    'dismiss_reason', 'signals_json', 'build_json',
)
USAGE_COLUMNS = ('id', 'user_id', 'type', 'at', 'event_id', 'value', 'app_version')
EVENT_HEADER = [
    'household', 'event_id', 'camera', 'agent', 'kind', 'severity', 'status', 'zone_kind', 'confidence',
    'created_at', 'occurred_at', 'captured_at', 'detected_at', 'queued_at', 'first_sent_at',
    'first_delivered_at', 'candidate_at', 'confirmed_at', 'acknowledged_at', 'reviewed_at',
    'first_received_at', 'dismiss_reason', 'research_candidate', 'candidate_outcome',
    'alert_suppressed', 'build_json',
]
MINUTE_HEADER = ['household', 'agent', 'camera'] + [c for c in MINUTE_COLUMNS if c not in ('agent_id', 'camera_id')]
HEARTBEAT_HEADER = [
    'household', 'agent', 'camera', 'sent_at', 'received_at', 'camera_reachable', 'detector_alive',
    'queue_depth', 'agent_version', 'stats_json',
]
MISSED_HEADER = [
    'report_id', 'household', 'source', 'reporter', 'created_at', 'occurred_from', 'occurred_to', 'kind', 'camera',
]
USAGE_HEADER = ['household', 'member', 'type', 'at', 'event_id', 'value', 'app_version']
UPTIME_HEADER = ['household', 'camera', 'trial_minutes', 'monitored_minutes', 'uptime_share']
INVESTOR_HEADER = ['figure', 'group', 'value', 'households']
ENGINEERING_HEADERS = {
    'events.csv': EVENT_HEADER,
    'minutes.csv': MINUTE_HEADER,
    'heartbeats.csv': HEARTBEAT_HEADER,
    'missed.csv': MISSED_HEADER,
    'usage.csv': USAGE_HEADER,
    'uptime.csv': UPTIME_HEADER,
}


class Index:
    """Assigns PREFIX1, PREFIX2, ... in registration order; None maps to an empty cell."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.values: dict = {}

    def get(self, key) -> str:
        if key is None:
            return ''
        if key not in self.values:
            self.values[key] = f'{self.prefix}{len(self.values) + 1}'
        return self.values[key]


def open_snapshot(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=30000')
    connection.execute('BEGIN')
    return connection


def fetch(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def load_json(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def flag(value):
    return int(value) if isinstance(value, bool) else value


def signals_of(event: dict) -> dict:
    signals = load_json(event['signals_json'], {})
    return signals if isinstance(signals, dict) else {}


def is_alert(signals: dict) -> bool:
    if signals.get('alert_suppressed') == 1:
        return False
    if signals.get('research_candidate') == 1:
        return signals.get('candidate_outcome') == 1
    return True


def numeric_stats(text) -> str:
    stats = load_json(text, {})
    if not isinstance(stats, dict):
        return ''
    kept = {key: value for key, value in stats.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}
    return json.dumps(kept, sort_keys=True)


def daypart(epoch: float, zone) -> str:
    return DAYPARTS[datetime.fromtimestamp(epoch, zone).hour // 6]


def latencies(event: dict, first_received) -> dict:
    found = {}

    def gap(name, later, earlier):
        if later is None or earlier is None:
            return
        value = later - earlier
        if value >= 0:
            found[name] = value

    gap('captured_to_delivered', event['first_delivered_at'], event['captured_at'])
    gap('delivered_to_received', first_received, event['first_delivered_at'])
    gap('received_to_opened', event['acknowledged_at'], first_received)
    return found


def percentile(values: list, share: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return ordered[rank - 1]


def tally(table: dict, key: str, household_id: str) -> None:
    entry = table.setdefault(key, [0, set()])
    entry[0] += 1
    entry[1].add(household_id)


def write_csv(path: Path, header: list, rows: list) -> None:
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def load_cohort(connection: sqlite3.Connection) -> list[dict]:
    cohort = []
    for row in fetch(connection, 'SELECT household_id, code, start_date, end_date, tz FROM trial_cohort ORDER BY code'):
        zone = ZoneInfo(row['tz'])
        start_day = date.fromisoformat(row['start_date'])
        end_day = date.fromisoformat(row['end_date']) + timedelta(days=1)
        cohort.append({
            'hid': row['household_id'],
            'code': row['code'],
            'zone': zone,
            'start': int(datetime.combine(start_day, datetime.min.time(), tzinfo=zone).timestamp()),
            'end': int(datetime.combine(end_day, datetime.min.time(), tzinfo=zone).timestamp()),
        })
    return cohort


def camera_windows(cameras: list, scope: list, start: int, cap: float) -> dict:
    """Per camera: (first counted instant, last counted instant) inside the trial window."""
    by_id = {c['camera_id']: c for c in cameras}
    windows = {}
    for camera_id in scope:
        low, high = start, cap
        found = by_id.get(camera_id)
        if found is not None:
            if found['registered_at'] is not None:
                low = max(low, int(found['registered_at']) // 60 * 60)
            if found['revoked_at'] is not None:
                high = min(high, found['revoked_at'])
        windows[camera_id] = (low, high)
    return windows


def trial_minutes_of(window: tuple) -> int:
    low, high = window
    return max(0, int(high - low)) // 60


def monitored_minutes(windows: dict, beats: list, minutes: list, agent_cameras: dict) -> dict:
    """Return {camera_id: set of monitored minute starts} for each camera's counted minutes."""
    monitored = {camera: set() for camera in windows}

    def credit(camera, minute: int) -> None:
        low, high = windows[camera]
        if low <= minute and minute + 60 <= high:
            monitored[camera].add(minute)

    for beat in beats:
        if beat['camera_reachable'] != 1 or beat['sent_at'] is None:
            continue
        if beat['camera_id'] is not None:
            targets = [beat['camera_id']]
        else:
            # A heartbeat without camera or agent says nothing about any camera.
            targets = agent_cameras.get(beat['agent_id'], []) if beat['agent_id'] is not None else []
        sent = beat['sent_at']
        for step in range(math.ceil((sent - MONITOR_TOLERANCE_SECONDS) / 60),
                          math.floor((sent + MONITOR_TOLERANCE_SECONDS) / 60) + 1):
            minute = step * 60
            if abs(sent - minute) > MONITOR_TOLERANCE_SECONDS:
                continue
            for camera in targets:
                if camera in windows:
                    credit(camera, minute)
    for row in minutes:
        camera = row['camera_id']
        if camera in windows and (row['frames_received'] or 0) > 0:
            credit(camera, int(row['minute']))
    return monitored


def load_household(connection: sqlite3.Connection, household: dict, now: float, include_notes: bool) -> dict:
    hid, start, end = household['hid'], household['start'], household['end']
    cap = min(end, now)
    window = (hid, start, end)
    cameras = fetch(
        connection,
        'SELECT camera_id, agent_id, registered_at, revoked_at FROM cameras '
        'WHERE household_id = ? ORDER BY registered_at, camera_id',
        (hid,),
    )
    beats_wide = fetch(
        connection,
        'SELECT id, agent_id, camera_id, sent_at, received_at, camera_reachable, detector_alive, queue_depth, '
        'agent_version, stats_json FROM heartbeat_log WHERE household_id = ? AND sent_at >= ? AND sent_at < ? '
        'ORDER BY sent_at, id',
        (hid, start - 120, end + 120),
    )
    heartbeats = [beat for beat in beats_wide if start <= beat['sent_at'] < end]
    minutes = fetch(
        connection,
        f'SELECT {", ".join(MINUTE_COLUMNS)} FROM camera_minutes '
        'WHERE household_id = ? AND minute >= ? AND minute < ? ORDER BY minute, agent_id, camera_id',
        window,
    )
    notes = ', dismiss_note' if include_notes else ''
    events = fetch(
        connection,
        f'SELECT {", ".join(EVENT_COLUMNS)}{notes} FROM events '
        'WHERE household_id = ? AND created_at >= ? AND created_at < ? ORDER BY created_at, event_id',
        window,
    )
    received = {
        row['event_id']: row['first_received']
        for row in fetch(
            connection,
            'SELECT event_id, MIN(received_at) AS first_received FROM push_outbox '
            'WHERE household_id = ? AND received_at IS NOT NULL GROUP BY event_id',
            (hid,),
        )
    }
    zones = {}
    for row in fetch(connection, 'SELECT camera_id, settings_json FROM camera_detection_settings WHERE household_id = ?', (hid,)):
        settings = load_json(row['settings_json'], {})
        items = settings.get('zones', []) if isinstance(settings, dict) else []
        if not isinstance(items, list):
            items = []
        zones[row['camera_id']] = {
            item['zone_id']: item.get('kind') or ''
            for item in items
            if isinstance(item, dict) and isinstance(item.get('zone_id'), (str, int))
        }
    members = [row['user_id'] for row in fetch(
        connection,
        'SELECT user_id FROM household_memberships WHERE household_id = ? ORDER BY created_at, user_id',
        (hid,),
    )]
    usage = fetch(
        connection,
        f'SELECT {", ".join(USAGE_COLUMNS)} FROM usage_events '
        'WHERE household_id = ? AND at >= ? AND at < ? ORDER BY at, id',
        window,
    )
    missed = fetch(
        connection,
        'SELECT report_id, source, reported_by, created_at, occurred_from, occurred_to, kind, camera_id '
        'FROM missed_reports WHERE household_id = ? AND occurred_from >= ? AND occurred_from < ? '
        'ORDER BY occurred_from, report_id',
        window,
    )

    agent_cameras: dict = {}
    agent_registered: dict = {}
    for camera in cameras:
        agent_cameras.setdefault(camera['agent_id'], []).append(camera['camera_id'])
        if camera['registered_at'] is not None and camera['agent_id'] is not None:
            previous = agent_registered.get(camera['agent_id'])
            agent_registered[camera['agent_id']] = (
                camera['registered_at'] if previous is None else min(previous, camera['registered_at'])
            )
    scope = [c['camera_id'] for c in cameras if c['revoked_at'] is None or c['revoked_at'] >= start]
    for row in heartbeats + minutes:
        if row['camera_id'] is not None and row['camera_id'] not in scope:
            scope.append(row['camera_id'])
    windows = camera_windows(cameras, scope, start, cap)
    monitored = monitored_minutes(windows, beats_wide, minutes, agent_cameras)

    agent_first_seen: dict = {}
    for when, agent in ([(b['sent_at'], b['agent_id']) for b in heartbeats]
                        + [(m['minute'], m['agent_id']) for m in minutes]
                        + [(e['created_at'], e['agent_id']) for e in events]):
        if agent is not None and (agent not in agent_first_seen or when < agent_first_seen[agent]):
            agent_first_seen[agent] = when
    seen_cameras = {row['camera_id'] for row in heartbeats + minutes + events if row['camera_id'] is not None}

    return {
        'hid': hid,
        'code': household['code'],
        'zone': household['zone'],
        'elapsed_days': max(0.0, cap - start) / DAY_SECONDS,
        'cameras': cameras,
        'scope': scope,
        'trial_by_camera': {camera: trial_minutes_of(windows[camera]) for camera in scope},
        'monitored': monitored,
        'heartbeats': heartbeats,
        'minutes': minutes,
        'events': events,
        'received': received,
        'zones': zones,
        'members': members,
        'usage': usage,
        'missed': missed,
        'agent_cameras_registered': agent_registered,
        'agent_first_seen': agent_first_seen,
        'seen_cameras': seen_cameras,
    }


def engineering_rows(data: dict, cameras: Index, agents: Index, members: dict, include_notes: bool) -> dict:
    code = data['code']
    rows = {name: [] for name in ENGINEERING_FILES}
    for ev in data['events']:
        signals = signals_of(ev)
        if ev['zone_id'] is None:
            zone_kind = ''
        else:
            zone_kind = data['zones'].get(ev['camera_id'], {}).get(ev['zone_id'], 'unknown')
        row = [
            code, ev['event_id'], cameras.get(ev['camera_id']), agents.get(ev['agent_id']), ev['kind'],
            ev['severity'], ev['status'], zone_kind, ev['confidence'], ev['created_at'], ev['occurred_at'],
            ev['captured_at'], ev['detected_at'], ev['queued_at'], ev['first_sent_at'], ev['first_delivered_at'],
            ev['candidate_at'], ev['confirmed_at'], ev['acknowledged_at'], ev['reviewed_at'],
            data['received'].get(ev['event_id']), ev['dismiss_reason'],
            flag(signals.get('research_candidate')), flag(signals.get('candidate_outcome')),
            flag(signals.get('alert_suppressed')), ev['build_json'],
        ]
        if include_notes:
            row.append(ev['dismiss_note'])
        rows['events.csv'].append(row)
    rest = [c for c in MINUTE_COLUMNS if c not in ('agent_id', 'camera_id')]
    for minute in data['minutes']:
        rows['minutes.csv'].append(
            [code, agents.get(minute['agent_id']), cameras.get(minute['camera_id'])] + [minute[c] for c in rest]
        )
    for beat in data['heartbeats']:
        rows['heartbeats.csv'].append([
            code, agents.get(beat['agent_id']), cameras.get(beat['camera_id']), beat['sent_at'],
            beat['received_at'], beat['camera_reachable'], beat['detector_alive'], beat['queue_depth'],
            beat['agent_version'], numeric_stats(beat['stats_json']),
        ])
    for report in data['missed']:
        reporter = 'team' if report['source'] == 'team' else members.get(report['reported_by'], '')
        rows['missed.csv'].append([
            report['report_id'], code, report['source'], reporter, report['created_at'],
            report['occurred_from'], report['occurred_to'], report['kind'], cameras.get(report['camera_id']),
        ])
    for use in data['usage']:
        if use['type'] not in USAGE_TYPES:
            continue
        allowed = USAGE_VALUES.get(use['type'])
        value = use['value'] if allowed is not None and use['value'] in allowed else ''
        rows['usage.csv'].append([
            code, members.get(use['user_id'], ''), use['type'], use['at'], use['event_id'],
            value, use['app_version'],
        ])
    for camera_id in data['scope']:
        trial = data['trial_by_camera'][camera_id]
        monitored = len(data['monitored'][camera_id])
        share = round(monitored / trial, 4) if trial else ''
        rows['uptime.csv'].append([code, cameras.get(camera_id), trial, monitored, share])
    return rows


def export_engineering(connection: sqlite3.Connection, cohort: list, now: float, out: Path,
                       include_notes: bool) -> str:
    tables = {name: [] for name in ENGINEERING_FILES}
    for household in cohort:
        data = load_household(connection, household, now, include_notes)
        cameras = Index('C')
        for camera in data['cameras']:
            cameras.get(camera['camera_id'])
        for camera_id in sorted(data['seen_cameras'], key=str):
            cameras.get(camera_id)
        agents = Index('A')
        agent_order = []
        for agent in set(data['agent_cameras_registered']) | set(data['agent_first_seen']):
            when = data['agent_cameras_registered'].get(agent, data['agent_first_seen'].get(agent, 0))
            agent_order.append((when, agent))
        for _, agent in sorted(agent_order, key=lambda pair: (pair[0], str(pair[1]))):
            agents.get(agent)
        members = {}
        for position, user in enumerate(data['members'], start=1):
            members.setdefault(user, f'M{position}')
        rows = engineering_rows(data, cameras, agents, members, include_notes)
        for name in ENGINEERING_FILES:
            tables[name].extend(rows[name])
    out.mkdir(parents=True, exist_ok=True)
    lines = [f'Engineering export: {len(cohort)} households to {out}']
    for name in ENGINEERING_FILES:
        header = ENGINEERING_HEADERS[name]
        if name == 'events.csv' and include_notes:
            header = EVENT_HEADER + ['dismiss_note']
        write_csv(out / name, header, tables[name])
        lines.append(f'  {name}: {len(tables[name])} rows')
    return '\n'.join(lines)


def investor_figures(loads: list) -> tuple[list, int]:
    rows: list = []
    suppressed = 0

    def add(figure: str, group: str, value, households: set) -> None:
        nonlocal suppressed
        if len(households) < MIN_HOUSEHOLDS:
            suppressed += 1
            return
        rows.append([figure, group, value, len(households)])

    days = sum(d['elapsed_days'] for d in loads)

    monitored_total = trial_total = 0
    monitored_households: set = set()
    for d in loads:
        trial = sum(d['trial_by_camera'].values())
        if trial == 0:
            continue
        monitored_total += sum(len(d['monitored'][c]) for c in d['scope'])
        trial_total += trial
        monitored_households.add(d['hid'])
    if trial_total:
        add('monitored_hours', 'all', round(monitored_total / 60, 1), monitored_households)
        add('uptime_share', 'all', round(monitored_total / trial_total, 4), monitored_households)

    alerts = []
    for d in loads:
        for ev in d['events']:
            if is_alert(signals_of(ev)):
                alerts.append((d, ev))
    if alerts and days > 0:
        by_kind: dict = {}
        by_part: dict = {}
        for d, ev in alerts:
            kind = ev['kind'] if ev['kind'] in INVESTOR_KINDS else 'other'
            severity = ev['severity'] if ev['severity'] in INVESTOR_SEVERITIES else 'other'
            tally(by_kind, f'{kind}/{severity}', d['hid'])
            tally(by_part, daypart(ev['created_at'], d['zone']), d['hid'])
        add('alerts_per_household_day', 'all', round(len(alerts) / days, 4), {d['hid'] for d, _ in alerts})
        for group, (count, households) in sorted(by_kind.items()):
            add('alerts_per_household_day', group, round(count / days, 4), households)
        for group, (count, households) in sorted(by_part.items()):
            add('alerts_per_household_day_by_daypart', group, round(count / days, 4), households)

    if alerts:
        dismissed: dict = {}
        for d, ev in alerts:
            if ev['status'] == 'dismissed':
                reason = ev['dismiss_reason'] if ev['dismiss_reason'] in INVESTOR_DISMISS_REASONS else 'unspecified'
                tally(dismissed, reason, d['hid'])
                tally(dismissed, 'any', d['hid'])
        for group, (count, households) in sorted(dismissed.items()):
            add('dismissed_share', group, round(count / len(alerts), 4), households)

    samples = {name: ([], set()) for name in LATENCY_NAMES}
    for d, ev in alerts:
        for name, value in latencies(ev, d['received'].get(ev['event_id'])).items():
            samples[name][0].append(value)
            samples[name][1].add(d['hid'])
    for name in LATENCY_NAMES:
        values, households = samples[name]
        if len(values) >= LATENCY_MIN_SAMPLES:
            add('latency_p50_seconds', name, round(percentile(values, 0.50), 3), households)
            add('latency_p95_seconds', name, round(percentile(values, 0.95), 3), households)

    missed_count = 0
    missed_households: set = set()
    for d in loads:
        if d['missed']:
            missed_count += len(d['missed'])
            missed_households.add(d['hid'])
    if missed_households and days > 0:
        add('missed_reports_per_household_week', 'all', round(missed_count / (days / 7), 3), missed_households)

    opens = 0
    open_households: set = set()
    for d in loads:
        for use in d['usage']:
            if use['type'] == 'app_open':
                opens += 1
                open_households.add(d['hid'])
    if open_households and days > 0:
        add('app_opens_per_household_day', 'all', round(opens / days, 3), open_households)

    last_permission: dict = {}
    for d in loads:
        for use in d['usage']:
            if use['type'] == 'notification_permission':
                last_permission[(d['hid'], use['user_id'])] = use['value']
    if last_permission:
        enabled = sum(1 for value in last_permission.values() if value == 'enabled')
        add('notifications_enabled_share', 'all', round(enabled / len(last_permission), 4),
            {hid for hid, _ in last_permission})
    return rows, suppressed


def export_investor(connection: sqlite3.Connection, cohort: list, now: float, out: Path) -> str:
    rows: list = []
    suppressed = 0
    if len(cohort) >= MIN_HOUSEHOLDS:
        loads = [load_household(connection, household, now, False) for household in cohort]
        rows, suppressed = investor_figures(loads)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / 'summary.csv', INVESTOR_HEADER, rows)
    note = '' if len(cohort) >= MIN_HOUSEHOLDS else ' (fewer than 3 cohort households: header only)'
    return (f'Investor export to {out}{note}: summary.csv {len(rows)} figure rows written, '
            f'{suppressed} suppressed (fewer than {MIN_HOUSEHOLDS} households)')


def check_out_dir(out: Path) -> None:
    if out.exists():
        if not out.is_dir():
            raise ValueError(f'output path is not a directory: {out}')
        if any(out.iterdir()):
            raise ValueError(f'output directory must be empty: {out}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, required=True, help='Existing server SQLite database (opened read-only)')
    parser.add_argument('--out', type=Path, required=True, help='Output directory; created if missing, must be empty')
    parser.add_argument('--kind', choices=('engineering', 'investor'), required=True)
    parser.add_argument('--include-notes', action='store_true',
                        help='Engineering only: include the free-text dismiss note in events.csv')
    args = parser.parse_args()
    if args.include_notes and args.kind != 'engineering':
        print('note: --include-notes applies to --kind engineering only and is ignored', file=sys.stderr)
    include_notes = args.include_notes and args.kind == 'engineering'
    now = time.time()
    try:
        if not args.db.is_file():
            raise ValueError(f'database file not found: {args.db}')
        out = args.out.absolute()
        check_out_dir(out)
        with closing(open_snapshot(args.db)) as connection:
            try:
                cohort = load_cohort(connection)
                if args.kind == 'engineering':
                    message = export_engineering(connection, cohort, now, out, include_notes)
                else:
                    message = export_investor(connection, cohort, now, out)
            finally:
                connection.rollback()
    except (sqlite3.Error, ZoneInfoNotFoundError, TypeError, ValueError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        raise SystemExit(1)
    print(message)


if __name__ == '__main__':
    main()
