"""Only availability metadata is stored; local agent clips are transferred on demand."""
from mantau_core.contracts import AgentRecordingsSnapshot
from .transactions import serialized_repository


@serialized_repository
class AgentRecordingsRepo:
    def __init__(self, db):
        self._db = db
        self.db = db

    async def snapshot(self, agent_id: str, household_id: str, snapshot: AgentRecordingsSnapshot | None):
        async with self.db.transaction():
            await self.db.conn.execute('UPDATE agents SET local_recordings_supported=? WHERE agent_id=?',
                                       (int(snapshot is not None), agent_id))
            await self.db.conn.execute('DELETE FROM agent_recordings WHERE agent_id=?', (agent_id,))
            if snapshot is not None:
                for clip in snapshot.recordings:
                    row = await (await self.db.conn.execute(
                        'SELECT e.event_id FROM events e JOIN cameras c ON c.camera_id=e.camera_id '
                        'WHERE e.event_id=? AND e.agent_id=? AND e.household_id=? '
                        'AND e.kind!=? AND c.revoked_at IS NULL AND c.agent_id=?',
                        (clip.event_id, agent_id, household_id, 'bathroom_duration', agent_id))).fetchone()
                    if row is not None:
                        await self.db.conn.execute(
                            'INSERT INTO agent_recordings(event_id,agent_id,household_id,size_bytes,captured_at_ms) VALUES(?,?,?,?,?)',
                            (clip.event_id, agent_id, household_id, clip.size_bytes, clip.captured_at_ms))
                # Five newest incidents across cameras in this household; keep event history intact.
                await self.db.conn.execute(
                    'DELETE FROM agent_recordings WHERE household_id=? AND event_id NOT IN '
                    '(SELECT ar.event_id FROM agent_recordings ar JOIN events e ON e.event_id=ar.event_id '
                    'WHERE ar.household_id=? ORDER BY e.occurred_at DESC,ar.event_id DESC LIMIT 5)',
                    (household_id, household_id))
            await self.db.conn.commit()

    async def get(self, household_id, event_id):
        return await (await self.db.conn.execute(
            'SELECT ar.* FROM agent_recordings ar JOIN cameras c ON c.camera_id='
            '(SELECT camera_id FROM events WHERE event_id=ar.event_id) JOIN agents a ON a.agent_id=ar.agent_id '
            'WHERE ar.household_id=? AND ar.event_id=? AND c.revoked_at IS NULL '
            'AND c.household_id=? AND a.revoked_at IS NULL AND a.household_id=?',
            (household_id,event_id,household_id,household_id))).fetchone()

    async def supported(self, agent_id):
        row = await (await self.db.conn.execute(
            'SELECT local_recordings_supported FROM agents WHERE agent_id=? AND revoked_at IS NULL', (agent_id,))).fetchone()
        return bool(row and row[0])
