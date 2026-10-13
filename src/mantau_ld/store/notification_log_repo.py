"""Read-only operational alarm log. Deliberately excludes all media and tokens."""
from __future__ import annotations

import base64
import json
from mantau_core.contracts import FallEvent
from mantau_core.notify.templates import render
from .db import Database


# Match the durable outbox's history-only candidate guard, including rest suppression.
ALARM = """(COALESCE(json_extract(e.signals_json,'$.research_candidate'),0) != 1 OR
 (json_extract(e.signals_json,'$.candidate_outcome')=1 AND
  COALESCE(json_extract(e.signals_json,'$.alert_suppressed'),0) != 1))"""


def encode_cursor(created_at: float, event_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at, event_id]).encode()).decode()


def decode_cursor(value: str) -> tuple[float, str]:
    import math
    try:
        at, event_id = json.loads(base64.b64decode(value, altchars=b'-_', validate=True))
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
            raise ValueError()
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
            raise ValueError()
        return float(at), event_id
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid notification cursor") from None


class NotificationLogRepo:
    def __init__(self, db: Database):
        self.db = db

    async def list(self, *, cutoff: float, limit: int, cursor: str | None = None) -> dict:
        where = f"e.created_at >= ? AND {ALARM}"
        params: list = [cutoff]
        if cursor:
            at, event_id = decode_cursor(cursor)
            where += " AND (e.created_at < ? OR (e.created_at = ? AND e.event_id < ?))"
            params.extend([at, at, event_id])
        async with self.db.serialized():
            events = await (await self.db.conn.execute(
                f"SELECT e.*, h.name household_name, c.name camera_name FROM events e "
                "LEFT JOIN households h ON h.household_id=e.household_id "
                "LEFT JOIN cameras c ON c.camera_id=e.camera_id "
                f"WHERE {where} ORDER BY e.created_at DESC,e.event_id DESC LIMIT ?",
                (*params, limit + 1))).fetchall()
            summary = await (await self.db.conn.execute(
                f"SELECT COUNT(*) alarms, COALESCE(SUM(EXISTS(SELECT 1 FROM push_outbox p "
                "WHERE p.event_id=e.event_id AND p.received_at IS NOT NULL)),0) receipt_confirmed, "
                "COALESCE(SUM(NOT EXISTS(SELECT 1 FROM push_outbox p WHERE p.event_id=e.event_id)),0) no_recipients "
                f"FROM events e WHERE e.created_at >= ? AND {ALARM}", (cutoff,))).fetchone()
            failures = await (await self.db.conn.execute(
                "SELECT COUNT(*) n FROM push_outbox p JOIN events e ON e.event_id=p.event_id "
                f"WHERE e.created_at >= ? AND {ALARM} AND p.state='failed'", (cutoff,))).fetchone()
            visible = events[:limit]
            deliveries = []
            if visible:
                ids = [row['event_id'] for row in visible]
                deliveries = await (await self.db.conn.execute(
                    "SELECT p.event_id,p.user_id,p.device_id,p.state,p.attempts,p.sent_at,"
                    "p.delivered_at,p.received_at,p.alert_json,u.display_name recipient_name "
                    "FROM push_outbox p LEFT JOIN users u ON u.user_id=p.user_id "
                    f"WHERE p.event_id IN ({','.join('?' for _ in ids)}) ORDER BY p.id", ids)).fetchall()
        by_event: dict[str, list] = {}
        for row in deliveries:
            by_event.setdefault(row['event_id'], []).append(row)
        alarms = []
        for event in visible:
            recipients = by_event.get(event['event_id'], [])
            title = body = None
            for recipient in recipients:
                try:
                    payload = json.loads(recipient['alert_json'])
                    if isinstance(payload.get('title'), str) and isinstance(payload.get('body'), str):
                        title, body = payload['title'][:200], payload['body'][:2000]
                        break
                except (ValueError, TypeError, AttributeError):
                    continue
            message_source = 'saved_notification' if title is not None else 'alarm_description'
            if title is None:
                model = FallEvent(event_id=event['event_id'], camera_id=event['camera_id'],
                    kind=event['kind'], severity=event['severity'], occurred_at=event['occurred_at'],
                    confidence=event['confidence'], signals=json.loads(event['signals_json']))
                title, body = render(model, camera_name=event['camera_name'])
            alarms.append({
                'event_id': event['event_id'], 'occurred_at': event['occurred_at'],
                'recorded_at': event['created_at'], 'household_id': event['household_id'],
                'household_name': event['household_name'], 'camera_id': event['camera_id'],
                'camera_name': event['camera_name'], 'agent_id': event['agent_id'],
                'kind': event['kind'], 'severity': event['severity'], 'review_status': event['status'],
                'title': title, 'body': body, 'message_source': message_source,
                'deliveries': [{
                    'user_id': row['user_id'], 'recipient_name': row['recipient_name'],
                    'device_id': row['device_id'], 'state': row['state'], 'attempts': row['attempts'],
                    'sent_at': row['sent_at'], 'fcm_accepted_at': row['delivered_at'],
                    'received_at': row['received_at'],
                } for row in recipients],
            })
        next_cursor = (encode_cursor(visible[-1]['created_at'], visible[-1]['event_id'])
                       if len(events) > limit else None)
        return {'alarms': alarms, 'summary': {**dict(summary), 'failed_deliveries': failures['n']},
                'next_cursor': next_cursor}
