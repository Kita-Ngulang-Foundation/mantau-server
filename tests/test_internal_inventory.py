import sqlite3
import time
from fastapi.testclient import TestClient
import support


def test_inventory_fails_closed_and_does_not_accept_customer_or_agent_identity(tmp_path):
    settings = support.settings(db_path=str(tmp_path/'agents.db'), internal_agents_token='inventory-test-key')
    with TestClient(support.app(settings)) as client:
        enrolled = support.enroll(client, support.user('customer'))
        for headers in [{}, support.user('customer'), {'Authorization': 'Bearer incorrect'},
                        support.agent_headers(enrolled)]:
            assert client.get('/internal/agents', headers=headers).status_code == 401
        assert client.get('/agents', headers={'Authorization': 'Bearer inventory-test-key'}).status_code == 401
        assert client.post('/internal/agents', headers={'Authorization': 'Bearer inventory-test-key'}).status_code == 405
    with TestClient(support.app(support.settings())) as client:
        assert client.get('/internal/agents').status_code == 503


def test_inventory_lists_active_enrollments_across_households_and_uses_contact_cutoff(tmp_path):
    file = tmp_path/'agents.db'
    settings = support.settings(db_path=str(file), internal_agents_token='inventory-test-key', agent_offline_after_s=30)
    with TestClient(support.app(settings)) as client:
        a = support.enroll(client, support.user('a'), agent_id='fresh')
        support.enroll(client, support.user('b'), agent_id='stale')
        support.enroll(client, support.user('a'), agent_id='never')
        support.enroll(client, support.user('b'), agent_id='revoked')
        client.delete('/agents/revoked', headers=support.user('b')).raise_for_status()
        with sqlite3.connect(file) as db:
            db.execute('UPDATE agents SET last_seen_at=? WHERE agent_id=?', (time.time()-5, 'fresh'))
            db.execute('UPDATE agents SET last_seen_at=? WHERE agent_id=?', (time.time()-31, 'stale'))
        response = client.get('/internal/agents', headers={'Authorization': 'Bearer inventory-test-key'})
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'private, no-store'
        data = response.json()
        assert data['offline_after_s'] == 30
        agents = {row['agent_id']: row for row in data['agents']}
        assert {key: row['status'] for key, row in agents.items()} == {'fresh': 'online', 'stale': 'offline', 'never': 'offline'}
        assert len({row['household_id'] for row in agents.values()}) == 2
        assert agents['never']['last_contact_at'] is None
        assert a['secret'] not in response.text
        assert all(set(row) == {'agent_id', 'name', 'household_id', 'household_name', 'platform', 'enrolled_at', 'last_contact_at', 'status'} for row in agents.values())
