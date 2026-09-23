"""Results of server inference that outlive the request: confirmations of
events an agent detected itself (HYBRID). Frames are never stored.

A confirmation row names the agent that asked for it; it is only ever shown
on an event produced by that same agent, so a confirmation can never attach
to another household's event even if an event id were guessed.
"""

from __future__ import annotations

import time

from mantau_core.contracts import InferenceConfirmation

from .db import Database


class InferenceRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

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
        await self._db.conn.execute(
            "DELETE FROM inference_confirmations WHERE created_at < ?",
            (time.time() - retention_days * 86400,),
        )
        await self._db.conn.commit()
