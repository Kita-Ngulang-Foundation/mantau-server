"""Authorized data lifecycle; credentials never enter an export."""
from pathlib import Path
import shutil
import hashlib
import time

from .transactions import serialized_repository

class OwnershipRequired(PermissionError):
    pass


@serialized_repository
class LifecycleRepo:
    def __init__(self, db, recordings_root):
        self._db = db
        self.root = Path(recordings_root).resolve()

    async def _owner(self, household_id, user_id):
        row = await (await self._db.conn.execute(
            "SELECT role FROM household_memberships WHERE household_id=? AND user_id=?",
            (household_id, user_id))).fetchone()
        if row is None or row[0] != 'owner':
            raise OwnershipRequired('owner_required')

    async def promote_owner(self, household_id, actor, target):
        async with self._db.transaction():
            await self._owner(household_id, actor)
            cursor = await self._db.conn.execute(
                "UPDATE household_memberships SET role='owner' WHERE household_id=? AND user_id=?",
                (household_id, target))
            if cursor.rowcount != 1:
                raise LookupError('member_not_found')

    async def export(self, household_id, user_id):
        await self._owner(household_id, user_id)
        result = {'schema_version': 1, 'household_id': household_id}
        tables = {
            'household': ('households', 'household_id,name,created_at'),
            'members': ('household_memberships', 'user_id,role,created_at'),
            'agents': ('agents', 'agent_id,name,enrolled_at,last_seen_at,revoked_at'),
            'cameras': ('cameras', 'camera_id,name,agent_id,registered_at,revoked_at'),
            'events': ('events', '*'),
            'contacts': ('emergency_contacts', '*'),
            'detection_settings': ('camera_detection_settings', 'camera_id,settings_json,version,applied_version'),
            'recordings': ('recordings', 'event_id,camera_id,created_at,size_bytes,content_type'),
        }
        for key, (table, columns) in tables.items():
            rows = await (await self._db.conn.execute(
                f"SELECT {columns} FROM {table} WHERE household_id=?", (household_id,))).fetchall()
            result[key] = [dict(row) for row in rows]
        # Eligible recordings remain available from the authenticated event download endpoint.
        for recording in result['recordings']:
            recording['download_path'] = f"/events/{recording['event_id']}/recording"
        return result

    async def delete_household(self, household_id, user_id):
        async with self._db.transaction():
            await self._owner(household_id, user_id)
            rows = await (await self._db.conn.execute(
                'SELECT storage_key FROM recordings WHERE household_id=?', (household_id,))).fetchall()
            paths = []
            configured_household_root = self.root / household_id
            if configured_household_root.is_symlink():
                raise ValueError('unsafe_household_path')
            household_root = configured_household_root.resolve()
            if household_root == self.root or not household_root.is_relative_to(self.root):
                raise ValueError('unsafe_household_path')
            for row in rows:
                path = (self.root / row[0]).resolve()
                if not path.is_relative_to(household_root):
                    raise ValueError('unsafe_recording_path')
                paths.append(path)
            # Storage failure prevents claiming deletion. A retry tolerates already deleted files.
            for path in paths:
                path.unlink(missing_ok=True)
            # Interrupted uploads and obsolete unindexed clips also belong to
            # this household. The resolved target above is confined to its
            # own directory; rmtree does not follow child directory symlinks.
            if household_root.exists():
                shutil.rmtree(household_root)
            for table in ('push_outbox', 'inference_confirmations', 'recordings', 'events',
                          'camera_detection_settings', 'cameras', 'queued_commands',
                          'device_tokens', 'emergency_contacts', 'agent_enrollment_keys'):
                await self._db.conn.execute(f'DELETE FROM {table} WHERE household_id=?', (household_id,))
            await self._db.conn.execute(
                'DELETE FROM ingested_envelopes WHERE agent_id IN (SELECT agent_id FROM agents WHERE household_id=?)',
                (household_id,))
            await self._db.conn.execute('DELETE FROM agents WHERE household_id=?', (household_id,))
            await self._db.conn.execute('DELETE FROM households WHERE household_id=?', (household_id,))

    async def delete_user(self, user_id):
        async with self._db.transaction():
            alone = await (await self._db.conn.execute(
                "SELECT 1 FROM household_memberships m WHERE user_id=? AND role='owner' "
                "AND (SELECT COUNT(*) FROM household_memberships n WHERE n.household_id=m.household_id AND role='owner')=1",
                (user_id,))).fetchone()
            if alone:
                raise ValueError('last_owner')
            identities = await (await self._db.conn.execute(
                'SELECT oidc_issuer,oidc_subject FROM user_identities WHERE user_id=?', (user_id,))).fetchall()
            for identity in identities:
                fingerprint = hashlib.sha256((identity[0]+'\0'+identity[1]).encode()).hexdigest()
                await self._db.conn.execute(
                    'INSERT INTO account_deletions(identity_hash,deleted_at) VALUES(?,?) '
                    'ON CONFLICT(identity_hash) DO UPDATE SET deleted_at=excluded.deleted_at',
                    (fingerprint, time.time()))
            await self._db.conn.execute('DELETE FROM device_tokens WHERE user_id=?', (user_id,))
            await self._db.conn.execute('DELETE FROM push_outbox WHERE user_id=?', (user_id,))
            await self._db.conn.execute('UPDATE queued_commands SET requested_by_user_id=NULL WHERE requested_by_user_id=?', (user_id,))
            # Legacy owner_id is part of an idempotency uniqueness key. A
            # command-specific tombstone avoids collisions between deletions.
            await self._db.conn.execute(
                "UPDATE queued_commands SET owner_id='deleted:'||command_id WHERE owner_id=?", (user_id,))
            await self._db.conn.execute('UPDATE events SET acknowledged_by=NULL WHERE acknowledged_by=?', (user_id,))
            await self._db.conn.execute('UPDATE events SET reviewed_by=NULL WHERE reviewed_by=?', (user_id,))
            await self._db.conn.execute(
                "UPDATE camera_detection_settings SET updated_by='deleted-user' WHERE updated_by=?", (user_id,))
            await self._db.conn.execute(
                "UPDATE agent_enrollment_keys SET created_by='deleted-user' WHERE created_by=?", (user_id,))
            await self._db.conn.execute(
                "UPDATE household_invites SET created_by='deleted-user' WHERE created_by=?", (user_id,))
            await self._db.conn.execute(
                'UPDATE household_invites SET consumed_by=NULL WHERE consumed_by=?', (user_id,))
            await self._db.conn.execute('DELETE FROM rate_limits WHERE bucket=?', (f'invite:{user_id}',))
            # The legacy startup bridge must not recreate deleted ownership.
            await self._db.conn.execute(
                "DELETE FROM agent_ownership WHERE owner_id=? OR owner_id IN ("
                "SELECT oidc_subject FROM user_identities WHERE user_id=? AND oidc_issuer='legacy')",
                (user_id, user_id))
            await self._db.conn.execute('DELETE FROM users WHERE user_id=?', (user_id,))
