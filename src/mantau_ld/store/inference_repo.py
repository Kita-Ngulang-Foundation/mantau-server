"""Results of server inference that outlive the request: confirmations of
events an agent detected itself (HYBRID). Frames are never stored.

A confirmation row names the agent that asked for it; it is only ever shown
on an event produced by that same agent, so a confirmation can never attach
to another household's event even if an event id were guessed.
"""

from __future__ import annotations

import time
from mantau_core.contracts import InferenceResult

from mantau_core.contracts import InferenceConfirmation

from .db import Database
from .transactions import serialized_repository


@serialized_repository
class InferenceRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def answered(self, agent_id: str, frame_id: str, request_hash: str):
        row = await (await self._db.conn.execute(
            'SELECT request_hash,result_json FROM inference_answers WHERE agent_id=? AND frame_id=?',
            (agent_id, frame_id))).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise ValueError('frame_id_conflict')
        return InferenceResult.model_validate_json(row[1]).model_copy(update={'duplicate': True})

    async def save_answer(self, agent_id, camera_id, household_id, request_hash, result):
        # Persist alert-bearing answers only; ordinary video-rate frames use
        # the bounded in-memory dedupe cache and never fill the database.
        if not result.events and not result.confirmations:
            return
        await self._db.conn.execute(
            'INSERT INTO inference_answers(agent_id,frame_id,camera_id,household_id,request_hash,result_json,created_at) '
            'VALUES(?,?,?,?,?,?,?)', (agent_id, result.frame_id, camera_id, household_id,
                                    request_hash, result.model_dump_json(), time.time()))
        await self._db.conn.commit()

    async def save_confirmation(self, confirmation: InferenceConfirmation, *, frame_id: str,
                                agent_id: str, household_id: str) -> None:
        await self._db.conn.execute(
            "INSERT OR IGNORE INTO inference_confirmations(event_id,frame_id,agent_id,household_id,"
            "confirmed,confidence,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (confirmation.event_id, frame_id, agent_id, household_id,
             int(confirmation.confirmed), confirmation.confidence, confirmation.reason, time.time()),
        )
        await self._db.conn.commit()

    async def prune(self, retention_days: int) -> None:
        await self._db.conn.execute('DELETE FROM inference_answers WHERE created_at < ?',
                                   (time.time() - retention_days * 86400,))
        await self._db.conn.execute(
            "DELETE FROM inference_confirmations WHERE created_at < ?",
            (time.time() - retention_days * 86400,),
        )
        await self._db.conn.commit()
