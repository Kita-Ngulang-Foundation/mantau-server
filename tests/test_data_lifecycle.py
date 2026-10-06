"""Local SQLite lifecycle proof, including file deletion and owner concurrency."""
import asyncio
import json
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor
import threading
import jwt

import pytest
from mantau_core.contracts import CommandResult, CommandType, DetectionSettings, FallEvent

import support
from mantau_ld.store.cameras_repo import CamerasRepo
from mantau_ld.store.control_repo import ControlRepo
from mantau_ld.store.db import Database
from mantau_ld.store.detection_settings_repo import DetectionSettingsRepo
from mantau_ld.store.events_repo import EventsRepo
from mantau_ld.store.lifecycle_repo import LifecycleRepo
from mantau_ld.store.recordings_repo import RecordingsRepo


def _api_headers(subject, authenticated_at):
    claims = jwt.decode(support.token(subject), options={"verify_signature": False})
    if authenticated_at is None:
        claims.pop("auth_time", None)
    else:
        claims["auth_time"] = authenticated_at
    encoded = jwt.encode(claims, support.PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})
    return {"Authorization": f"Bearer {encoded}"}


async def _api_sql(app, sql, params):
    async with app.state.db.serialized():
        result = await app.state.db.conn.execute(sql, params)
        rows = await result.fetchall()
        await app.state.db.conn.commit()
        return [dict(row) for row in rows]


def _api_query(client, sql, params=()):
    return client.portal.call(_api_sql, client.app, sql, params)


def _delete_personal_household(client, headers, household):
    response = client.request("DELETE", f"/households/{household}",
                              headers={**headers, "X-Mantau-Household-ID": household},
                              json={"confirm_household_id": household})
    assert response.status_code == 204, response.text


def _delete_api_profile(client, headers):
    personal = client.get("/households", headers=headers).json()[0]["household_id"]
    _delete_personal_household(client, headers, personal)
    response = client.delete("/households/account/me", headers=headers)
    assert response.status_code == 204, response.text


async def _user(db, user_id, household_id=None, role="member"):
    await db.conn.execute("INSERT INTO users(user_id,created_at,email,display_name) VALUES(?,?,?,?) "
                          "ON CONFLICT(user_id) DO UPDATE SET email=excluded.email,display_name=excluded.display_name",
                          (user_id, time.time(), f"{user_id}@example.test", user_id))
    await db.conn.execute("INSERT OR IGNORE INTO user_identities(oidc_issuer,oidc_subject,user_id) VALUES('test',?,?)",
                          (user_id, user_id))
    if household_id:
        await db.conn.execute("INSERT OR IGNORE INTO household_memberships(household_id,user_id,role,created_at) VALUES(?,?,?,?)",
                              (household_id, user_id, role, time.time()))
    await db.conn.commit()


async def _seed(db, root, household="household-1"):
    user_id, agent_id, camera_id = f"owner-{household}", f"agent-{household}", f"camera-{household}"
    enrolled = await support.enroll_in_db(db, agent_id, household_id=household, user_id=user_id)
    await _user(db, user_id)
    await CamerasRepo(db).create(camera_id, "Room", household_id=household, agent_id=agent_id)
    event = FallEvent(camera_id=camera_id, confidence=0.9)
    events = EventsRepo(db)
    await events.insert(event, household_id=household, agent_id=agent_id)
    await events.acknowledge(household, event.event_id, user_id)
    await events.review(household, event.event_id, "confirmed", user_id)
    await DetectionSettingsRepo(db).save(household, camera_id, DetectionSettings(), user_id)
    control = ControlRepo(db)
    command = await control.queue(agent_id=agent_id, household_id=household,
                                  requested_by_user_id=user_id, command_type=CommandType.DISCOVER,
                                  payload={}, idempotency_key="discover", ttl_s=100)
    await control.record_result(agent_id, CommandResult(command_id=command.command_id,
                                                       state="succeeded", data={"cameras": []}))
    await control.queue(agent_id=agent_id, household_id=household, requested_by_user_id=user_id,
                        command_type=CommandType.CONFIGURE_CAMERA, payload={"camera_id": camera_id},
                        encrypted_payload=b"fixture", idempotency_key="configure", ttl_s=100)
    now = time.time()
    await db.conn.execute(
        "INSERT INTO device_tokens(device_id,user_id,household_id,platform,token,registered_at,last_seen_at) "
        "VALUES(?,?,?,'android',?,'2026-10-05','2026-10-05')",
        (f"phone-{household}", user_id, household, f"private-push-token-{household}"))
    await db.conn.execute(
        "INSERT INTO push_outbox(event_id,household_id,channel,target,alert_json,user_id,device_id) VALUES(?,?,0,'private-push-token','{}',?,?)",
        (event.event_id, household, user_id, f"phone-{household}"))
    await db.conn.execute(
        "INSERT INTO inference_confirmations(event_id,frame_id,agent_id,household_id,confirmed,confidence,created_at) VALUES(?,'frame',?,?,1,0.9,?)",
        (event.event_id, agent_id, household, now))
    await db.conn.execute("INSERT INTO emergency_contacts(contact_id,household_id,name,phone,relation,priority) VALUES(?,?,'Caregiver','123','family',1)",
                          (f"contact-{household}", household))
    await db.conn.execute("INSERT INTO household_invites(code_hash,household_id,created_by,role,created_at,expires_at,consumed_by) VALUES(?,?,?,'member',?,?,?)",
                          (f"invite-{household}", household, user_id, now, now + 100, user_id))
    await db.conn.execute("INSERT INTO ingested_envelopes(agent_id,seq,kind,received_at) VALUES(?,1,'fall_event',?)", (agent_id, now))
    await db.conn.execute("INSERT INTO agent_ownership(agent_id,owner_id,claimed_at) VALUES(?,?,?)", (agent_id, user_id, now))
    await db.conn.execute("INSERT INTO rate_limits(bucket,window_started_at,attempts) VALUES(?,?,1)", (f"invite:{user_id}", now))
    await db.conn.commit()
    recording = await RecordingsRepo(db, str(root)).save(household_id=household, event_id=event.event_id,
                        camera_id=camera_id, body=b"household event clip", content_type="video/mp4")
    return user_id, enrolled, event, recording


async def _count(db, table, clause="", params=()):
    return (await (await db.conn.execute(f"SELECT COUNT(*) FROM {table} {clause}", params)).fetchone())[0]


@pytest.mark.parametrize("role", ["admin", "member", "outsider"])
async def test_only_current_household_owner_can_export_promote_or_delete(tmp_path, role):
    async with Database(":memory:") as db:
        owner, _, _, recording = await _seed(db, tmp_path / "clips")
        actor = f"actor-{role}"
        await _user(db, actor, None if role == "outsider" else "household-1", role)
        repo = LifecycleRepo(db, tmp_path / "clips")
        for operation in (repo.export("household-1", actor), repo.promote_owner("household-1", actor, owner),
                          repo.delete_household("household-1", actor)):
            with pytest.raises(PermissionError, match="owner_required"):
                await operation
        assert recording.path.is_file()
        assert await _count(db, "households") == 1


async def test_owner_cannot_promote_user_outside_household(tmp_path):
    async with Database(":memory:") as db:
        owner, _, _, _ = await _seed(db, tmp_path / "clips")
        await _user(db, "outsider")
        with pytest.raises(LookupError, match="member_not_found"):
            await LifecycleRepo(db, tmp_path / "clips").promote_owner("household-1", owner, "outsider")
        assert await _count(db, "household_memberships", "WHERE role='owner'") == 1


async def test_export_is_household_scoped_and_contains_no_device_or_camera_credentials(tmp_path):
    async with Database(":memory:") as db:
        owner, enrolled, event, _ = await _seed(db, tmp_path / "clips")
        _, other, _, _ = await _seed(db, tmp_path / "clips", "household-2")
        exported = await LifecycleRepo(db, tmp_path / "clips").export("household-1", owner)
        encoded = json.dumps(exported)
        for forbidden in (enrolled.secret, other.secret, "private-camera-password", "private-push-token",
                          "key_hash", "enrollment_key", "storage_key", "encrypted_payload", "household-2"):
            assert forbidden not in encoded
        assert len(exported["events"]) == len(exported["recordings"]) == 1
        assert exported["recordings"][0]["download_path"] == f"/events/{event.event_id}/recording"


async def test_household_deletion_removes_every_owned_table_and_clip_but_preserves_other_household(tmp_path):
    async with Database(str(tmp_path / "lifecycle.db")) as db:
        root = tmp_path / "clips"
        owner, _, _, recording = await _seed(db, root)
        _, other_agent, other_event, other_recording = await _seed(db, root, "household-2")
        orphan = recording.path.parent / "interrupted-upload.part"
        orphan.write_bytes(b"unfinished household data")
        await LifecycleRepo(db, root).delete_household("household-1", owner)
        for table in ("households", "household_memberships", "household_invites", "agents", "cameras",
                      "events", "push_outbox", "inference_confirmations", "recordings", "camera_detection_settings",
                      "queued_commands", "device_tokens", "emergency_contacts", "agent_enrollment_keys"):
            assert await _count(db, table, "WHERE household_id=?", ("household-1",)) == 0, table
        for table in ("agent_control_state", "agent_ownership", "discovery_results", "ingested_envelopes"):
            assert await _count(db, table, "WHERE agent_id=?", ("agent-household-1",)) == 0, table
        assert await _count(db, "command_results") == 1
        assert not recording.path.exists() and not orphan.exists()
        assert other_recording.path.read_bytes() == b"household event clip"
        assert await EventsRepo(db).get("household-2", other_event.event_id) is not None
        assert await _count(db, "agents", "WHERE agent_id=?", (other_agent.agent_id,)) == 1
        assert await (await db.conn.execute("PRAGMA foreign_key_check")).fetchall() == []


async def test_account_deletion_removes_profile_registration_and_attribution_keeps_shared_history(tmp_path):
    async with Database(":memory:") as db:
        root = tmp_path / "clips"
        owner, _, event, recording = await _seed(db, root)
        await _user(db, "co-owner", "household-1")
        repo = LifecycleRepo(db, root)
        await repo.promote_owner("household-1", owner, "co-owner")
        await repo.delete_user(owner)
        for table in ("users", "user_identities", "household_memberships", "device_tokens", "push_outbox"):
            assert await _count(db, table, "WHERE user_id=?", (owner,)) == 0, table
        assert await _count(db, "rate_limits", "WHERE bucket=?", (f"invite:{owner}",)) == 0
        assert await _count(db, "queued_commands", "WHERE owner_id=? OR requested_by_user_id=?", (owner, owner)) == 0
        assert await _count(db, "agent_ownership", "WHERE owner_id=?", (owner,)) == 0
        assert await _count(db, "camera_detection_settings", "WHERE updated_by=?", (owner,)) == 0
        assert await _count(db, "agent_enrollment_keys", "WHERE created_by=?", (owner,)) == 0
        assert await _count(db, "household_invites", "WHERE created_by=? OR consumed_by=?", (owner, owner)) == 0
        record = await EventsRepo(db).record("household-1", event.event_id)
        assert record.acknowledged_by is None and record.reviewed_by is None
        assert record.status == "confirmed" and recording.path.is_file()
        assert await _count(db, "household_memberships", "WHERE role='owner'") == 1
        assert await (await db.conn.execute("PRAGMA foreign_key_check")).fetchall() == []


async def test_concurrent_account_deletion_cannot_remove_last_two_owners(tmp_path):
    async with Database(":memory:") as db:
        root = tmp_path / "clips"
        owner, _, _, _ = await _seed(db, root)
        await _user(db, "co-owner", "household-1", "owner")
        repo = LifecycleRepo(db, root)
        results = await asyncio.gather(repo.delete_user(owner), repo.delete_user("co-owner"), return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ValueError) and str(result) == "last_owner" for result in results) == 1
        assert await _count(db, "household_memberships", "WHERE role='owner'") == 1
        assert await _count(db, "users") == 1


async def test_deleted_legacy_account_is_not_recreated_by_database_reopen(tmp_path):
    path = str(tmp_path / "legacy-account.db")
    root = tmp_path / "clips"
    async with Database(path) as db:
        owner, _, _, _ = await _seed(db, root)
        await _user(db, "co-owner", "household-1", "owner")
        await db.conn.execute("UPDATE agent_ownership SET owner_id='old-owner-subject'")
        await db.conn.execute("INSERT INTO user_identities(oidc_issuer,oidc_subject,user_id) VALUES('legacy','old-owner-subject',?)",
                              (owner,))
        await db.conn.commit()
        await LifecycleRepo(db, root).delete_user(owner)
    async with Database(path) as db:
        assert await _count(db, "users", "WHERE user_id=?", (owner,)) == 0
        assert await _count(db, "user_identities", "WHERE oidc_subject='old-owner-subject'") == 0
        assert await _count(db, "agent_ownership") == 0
        assert await _count(db, "users") == 1
        assert await _count(db, "household_memberships", "WHERE role='owner'") == 1


async def test_household_storage_failure_prevents_false_deletion_and_allows_retry(tmp_path, monkeypatch):
    async with Database(":memory:") as db:
        root = tmp_path / "clips"
        owner, _, _, recording = await _seed(db, root)
        original = type(recording.path).unlink

        def unavailable(path, *args, **kwargs):
            if path == recording.path:
                raise PermissionError("volume unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(recording.path), "unlink", unavailable)
        with pytest.raises(PermissionError, match="volume unavailable"):
            await LifecycleRepo(db, root).delete_household("household-1", owner)
        assert await _count(db, "households") == await _count(db, "events") == 1
        assert recording.path.is_file()
        monkeypatch.setattr(type(recording.path), "unlink", original)
        await LifecycleRepo(db, root).delete_household("household-1", owner)
        assert await _count(db, "households") == 0


def test_account_api_last_owner_guard_does_not_write_deletion_tombstone(tmp_path):
    headers = support.user("api-only-owner")
    with support.client(support.settings(db_path=str(tmp_path / "account-owner.db"),
                                         inference_enabled=False)) as client:
        household = client.get("/households", headers=headers).json()[0]["household_id"]
        response = client.delete("/households/account/me", headers=headers)
        assert response.status_code == 409 and response.json()["detail"] == "last_owner"
        assert _api_query(client, "SELECT * FROM account_deletions") == []
        assert client.get("/households", headers=headers).json()[0]["household_id"] == household


def test_shared_household_account_cleanup_and_cross_household_api_scope(tmp_path):
    owner, member = support.user("api-owner"), support.user("api-member")
    with support.client(support.settings(db_path=str(tmp_path / "shared-lifecycle.db"),
                                         inference_enabled=False)) as client:
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        member_household = client.get("/households", headers=member).json()[0]["household_id"]
        invite = client.post(f"/households/{household}/invites", headers=owner,
                             json={"role": "member"}).json()["invite_code"]
        assert client.post("/households/join", headers=member,
                           json={"invite_code": invite}).status_code == 200
        scoped_member = {**member, "X-Mantau-Household-ID": household}
        assert client.get(f"/households/{household}/export", headers=scoped_member).status_code == 403
        assert client.request("DELETE", f"/households/{household}", headers=scoped_member,
                              json={"confirm_household_id": household}).status_code == 403
        assert client.get(f"/households/{member_household}/export", headers=owner).status_code == 404
        assert client.request("DELETE", f"/households/{member_household}", headers=owner,
                              json={"confirm_household_id": member_household}).status_code == 404
        support.register_device(client, scoped_member, device_id="member-phone", token="member-token")
        _delete_personal_household(client, member, member_household)
        assert client.delete("/households/account/me", headers=member).status_code == 204
        assert client.delete("/devices/member-phone", headers=member).status_code == 401
        assert client.get(f"/households/{household}/members", headers=owner).status_code == 200
        members = client.get(f"/households/{household}/members", headers=owner).json()
        assert len(members) == 1 and members[0]["role"] == "owner"
        assert _api_query(client, "SELECT * FROM device_tokens") == []
        fingerprint = hashlib.sha256((support.ISSUER + '\0api-member').encode()).hexdigest()
        assert _api_query(client, "SELECT identity_hash FROM account_deletions")[0]["identity_hash"] == fingerprint
        assert client.get("/households", headers=member).status_code == 401
        assert _api_query(client, "SELECT COUNT(*) AS count FROM users")[0]["count"] == 1


def test_concurrent_api_account_deletion_preserves_a_shared_household_owner(tmp_path):
    owner, other = support.user("api-concurrent-owner"), support.user("api-concurrent-other")
    with support.client(support.settings(db_path=str(tmp_path / "concurrent-account.db"),
                                         inference_enabled=False)) as client:
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        personal = client.get("/households", headers=other).json()[0]["household_id"]
        invite = client.post(f"/households/{household}/invites", headers=owner,
                             json={"role": "member"}).json()["invite_code"]
        assert client.post("/households/join", headers=other,
                           json={"invite_code": invite}).status_code == 200
        _delete_personal_household(client, other, personal)
        members = client.get(f"/households/{household}/members", headers=owner).json()
        other_id = next(member["user_id"] for member in members if not member["is_me"])
        assert client.put(f"/households/{household}/owners/{other_id}", headers=owner).status_code == 204
        gate = threading.Barrier(2)

        def delete(headers):
            gate.wait(timeout=3)
            return client.delete("/households/account/me", headers=headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(delete, (owner, other)))
        assert sorted(result.status_code for result in results) == [204, 409]
        assert _api_query(client, "SELECT COUNT(*) AS count FROM household_memberships WHERE household_id=? AND role='owner'",
                          (household,))[0]["count"] == 1
        assert len(_api_query(client, "SELECT * FROM account_deletions")) == 1


def test_household_api_storage_permission_failure_is_unavailable_not_owner_denial(tmp_path, monkeypatch):
    owner = support.user("api-storage-owner")
    root = tmp_path / "recordings"
    settings = support.settings(db_path=str(tmp_path / "storage-api.db"), recordings_dir=str(root),
                                inference_enabled=False)
    with support.client(settings) as client:
        enrolled = support.enroll(client, owner, camera_id="storage-camera")
        household = client.get("/households", headers=owner).json()[0]["household_id"]
        event = FallEvent(camera_id="storage-camera", confidence=0.9)
        from mantau_core.contracts import Envelope
        envelope = Envelope.for_event(enrolled["agent_id"], seq=1, event=event).sign(enrolled["secret"])
        assert client.post("/ingest", json=envelope.model_dump(mode="json")).status_code == 200

        async def save():
            return await client.app.state.recordings_repo.save(household_id=household,
                event_id=event.event_id, camera_id=event.camera_id, body=b"stored clip", content_type="video/mp4")

        recording = client.portal.call(save)
        original = type(recording.path).unlink

        def unavailable(path, *args, **kwargs):
            if path == recording.path:
                raise PermissionError("storage unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(recording.path), "unlink", unavailable)
        response = client.request("DELETE", f"/households/{household}", headers=owner,
                                  json={"confirm_household_id": household})
        assert response.status_code == 503 and response.json()["detail"] == "data_deletion_unavailable"
        assert recording.path.is_file()
        assert client.get(f"/events/{event.event_id}", headers=owner).status_code == 200
