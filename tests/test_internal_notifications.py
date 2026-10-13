"""Operational metadata across households, without trial enrollment or media."""
import json
import time
import pytest
from mantau_core.contracts import Envelope, FallEvent
import support
from test_delivery_recovery import _pause, _query, ScriptedNotifier

ADMIN = {'Authorization': 'Bearer notification-log-test-key'}
OWNER = support.user('notification-owner')


def setup(client, *, phone=True):
    enrolled = support.enroll(client, OWNER, camera_id='camera')
    if phone:
        support.register_device(client, OWNER, device_id='phone', token='private-fcm-token')
    return enrolled


def ingest(client, enrolled, event, seq=1):
    response = client.post('/ingest', json=Envelope.for_event(enrolled['agent_id'], seq=seq,
        event=event).sign(enrolled['secret']).model_dump(mode='json'))
    assert response.status_code == 200, response.text


def app(**kwargs):
    return support.client(support.settings(internal_agents_token='notification-log-test-key'), **kwargs)


def test_alarm_requires_internal_auth_and_is_read_only():
    with app() as client:
        enrolled = setup(client)
        for headers in ({}, OWNER, support.agent_headers(enrolled), {'Authorization': 'Bearer wrong'}):
            assert client.get('/internal/notifications', headers=headers).status_code == 401
        assert client.post('/internal/notifications', headers=ADMIN).status_code == 405
        assert client.get('/events', headers=ADMIN).status_code == 401
    with support.client() as client:
        assert client.get('/internal/notifications').status_code == 503


def test_actual_delivery_and_phone_receipt_are_distinct_outside_trial_cohort():
    with app() as client:
        _pause(client)
        enrolled = setup(client)
        event = FallEvent(camera_id='camera', event_id='actual-alarm', confidence=.99)
        ingest(client, enrolled, event)
        assert _query(client, 'SELECT * FROM trial_cohort') == []
        response = client.get('/internal/notifications', headers=ADMIN)
        assert response.headers['cache-control'] == 'private, no-store'
        before = response.json()['alarms'][0]
        delivery = before['deliveries'][0]
        assert delivery['state'] == 'delivered'
        assert delivery['sent_at'] is not None and delivery['fcm_accepted_at'] is not None
        assert delivery['received_at'] is None
        assert before['title'] and before['body']
        assert before['message_source'] == 'saved_notification'
        assert 'private-fcm-token' not in response.text and enrolled['secret'] not in response.text
        received = time.time()
        item = {'type': 'notification_received', 'at': received, 'event_id': event.event_id, 'device_id': 'phone'}
        assert client.post('/usage', headers=OWNER, json={'items': [item]}).status_code == 202
        assert client.get('/internal/notifications', headers=ADMIN).json()['alarms'][0]['deliveries'][0]['received_at'] == received
        # A repeated or delayed report cannot move the first recorded receipt.
        item['at'] += 1
        client.post('/usage', headers=OWNER, json={'items': [item]})
        data = client.get('/internal/notifications', headers=ADMIN).json()
        assert data['summary']['receipt_confirmed'] == 1
        assert data['alarms'][0]['deliveries'][0]['received_at'] == received
        # Ordinary usage remains cohort-gated.
        assert _query(client, 'SELECT * FROM usage_events') == []


def test_foreign_user_or_device_cannot_claim_a_phone_receipt():
    with app() as client:
        _pause(client)
        enrolled = setup(client)
        event = FallEvent(camera_id='camera', event_id='alarm')
        ingest(client, enrolled, event)
        foreign = support.user('foreign')
        support.register_device(client, foreign, device_id='foreign-phone', token='foreign-token')
        for headers, device in ((foreign, 'phone'), (OWNER, 'foreign-phone'), (OWNER, 'unknown')):
            assert client.post('/usage', headers=headers, json={'items': [{
                'type': 'notification_received', 'at': time.time(), 'event_id': 'alarm', 'device_id': device
            }]}).status_code == 202
        row = client.get('/internal/notifications', headers=ADMIN).json()['alarms'][0]['deliveries'][0]
        assert row['received_at'] is None


def test_history_only_candidates_are_excluded_and_no_recipient_alarm_still_appears():
    with app() as client:
        _pause(client)
        enrolled = setup(client, phone=False)
        for index, outcome in enumerate((0, 2, 3, 5)):
            ingest(client, enrolled, FallEvent(camera_id='camera', event_id=f'history-{outcome}',
                signals={'research_candidate': 1., 'candidate_outcome': float(outcome)}), index + 1)
        ingest(client, enrolled, FallEvent(camera_id='camera', event_id='suppressed',
            signals={'research_candidate': 1., 'candidate_outcome': 1., 'alert_suppressed': 1.}), 6)
        ingest(client, enrolled, FallEvent(camera_id='camera', event_id='confirmed',
            signals={'research_candidate': 1., 'candidate_outcome': 1.}), 7)
        data = client.get('/internal/notifications', headers=ADMIN).json()
        assert [row['event_id'] for row in data['alarms']] == ['confirmed']
        assert data['alarms'][0]['deliveries'] == []
        assert data['alarms'][0]['message_source'] == 'alarm_description'
        assert data['summary']['no_recipients'] == data['summary']['alarms'] == 1


def test_paging_same_timestamp_across_households_and_media_allowlist():
    with app() as client:
        _pause(client)
        enrolled = setup(client)
        for i in range(3):
            ingest(client, enrolled, FallEvent(camera_id='camera', event_id=f'alarm-{i}'), i + 1)
        other = support.enroll(client, support.user('second-owner'), agent_id='second-agent', camera_id='second-camera')
        ingest(client, other, FallEvent(camera_id='second-camera', event_id='other-alarm'))
        _query(client, 'UPDATE events SET created_at=?', (time.time(),))
        payload = _query(client, 'SELECT alert_json FROM push_outbox LIMIT 1')[0]['alert_json']
        payload = {**json.loads(payload), 'snapshot_url': 'PRIVATE-IMAGE', 'video': 'PRIVATE-VIDEO'}
        _query(client, 'UPDATE push_outbox SET alert_json=?', (json.dumps(payload),))
        ids = []
        url = '/internal/notifications?limit=2'
        for _ in range(2):
            response = client.get(url, headers=ADMIN)
            assert 'PRIVATE-VIDEO' not in response.text and 'PRIVATE-IMAGE' not in response.text
            data = response.json()
            ids.extend(row['event_id'] for row in data['alarms'])
            url = '/internal/notifications?limit=2&cursor=' + (data['next_cursor'] or '')
        assert len(set(ids)) == len(ids) == 4 and data['next_cursor'] is None
        assert data['summary']['alarms'] == 4
        for query in ('days=31', 'limit=201', 'cursor=garbage', 'cursor=W3RydWUsICJhIl0='):
            assert client.get('/internal/notifications?' + query, headers=ADMIN).status_code == 422


def test_failed_delivery_stays_visible_as_failed_not_received():
    from mantau_core.notify.channels.push.errors import PushErrorKind
    with app(push_notifier=ScriptedNotifier(failures=1, kind=PushErrorKind.UNREGISTERED)) as client:
        _pause(client)
        enrolled = setup(client)
        ingest(client, enrolled, FallEvent(camera_id='camera', event_id='failed'))
        data = client.get('/internal/notifications', headers=ADMIN).json()
        assert data['summary']['failed_deliveries'] == 1
        row = data['alarms'][0]['deliveries'][0]
        assert row['state'] == 'failed' and row['sent_at'] is not None
        assert row['fcm_accepted_at'] is None and row['received_at'] is None
