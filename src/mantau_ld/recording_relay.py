"""Bounded, one-use in-memory relay. No clip file, cache or video history is stored here."""
import asyncio
from dataclasses import dataclass
import uuid
from starlette.responses import Response


class RelayBusy(Exception):
    pass


@dataclass
class Transfer:
    transfer_id: str
    household_id: str
    agent_id: str
    event_id: str
    size_bytes: int
    future: asyncio.Future


class RecordingRelay:
    def __init__(self, max_transfers=2, max_bytes=20 * 1024 * 1024, timeout_s=50):
        self.max_transfers = max_transfers
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        self.pending = {}

    def open(self, household_id, agent_id, event_id, size_bytes):
        if len(self.pending) >= self.max_transfers or not 0 < size_bytes <= self.max_bytes:
            raise RelayBusy()
        transfer = Transfer(uuid.uuid4().hex, household_id, agent_id, event_id, size_bytes,
                            asyncio.get_running_loop().create_future())
        self.pending[transfer.transfer_id] = transfer
        return transfer

    def accept(self, transfer_id, household_id, agent_id, event_id, body):
        transfer = self.pending.get(transfer_id)
        if (transfer is None or transfer.future.done()
                or (transfer.household_id,transfer.agent_id,transfer.event_id) != (household_id,agent_id,event_id)
                or len(body) != transfer.size_bytes or len(body) > self.max_bytes):
            return False
        transfer.future.set_result(body)
        return True

    async def receive(self, transfer):
        return await asyncio.wait_for(asyncio.shield(transfer.future), timeout=self.timeout_s)

    def close(self, transfer):
        self.pending.pop(transfer.transfer_id, None)
        if not transfer.future.done():
            transfer.future.cancel()


class RelayResponse(Response):
    """Keep the memory slot until ASGI finishes sending or the client disconnects."""
    def __init__(self, body, relay, transfer):
        super().__init__(body, media_type='video/mp4', headers={'Cache-Control': 'private, no-store'})
        self.relay, self.transfer = relay, transfer

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.relay.close(self.transfer)
