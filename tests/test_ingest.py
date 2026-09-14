import pytest
from mantau_core.contracts import Envelope, FallEvent

from mantau_ld.ingest.dedupe import record_envelope
from mantau_ld.ingest.verify import VerificationError, verify_envelope
from mantau_ld.store.agents_repo import AgentsRepo
from mantau_ld.store.db import Database


async def test_verify_accepts_a_correctly_signed_envelope():
    db = Database(":memory:")
    await db.connect()
    try:
        agents = AgentsRepo(db)
        agent = await agents.enroll("agent-1")
        event = FallEvent(camera_id="cam-1", confidence=0.9)
        envelope = Envelope.for_event("agent-1", seq=0, event=event).sign(agent.secret)

        await verify_envelope(envelope, agents)  # must not raise
    finally:
        await db.close()


async def test_verify_rejects_an_unknown_agent():
    db = Database(":memory:")
    await db.connect()
    try:
        agents = AgentsRepo(db)
        event = FallEvent(camera_id="cam-1")
        envelope = Envelope.for_event("ghost-agent", seq=0, event=event).sign("whatever-secret")

        with pytest.raises(VerificationError) as exc_info:
            await verify_envelope(envelope, agents)
        assert exc_info.value.reason == "unknown_agent"
    finally:
        await db.close()


async def test_verify_rejects_a_bad_signature():
    db = Database(":memory:")
    await db.connect()
    try:
        agents = AgentsRepo(db)
        await agents.enroll("agent-1")
        event = FallEvent(camera_id="cam-1")
        envelope = Envelope.for_event("agent-1", seq=0, event=event).sign("wrong-secret")

        with pytest.raises(VerificationError) as exc_info:
            await verify_envelope(envelope, agents)
        assert exc_info.value.reason == "invalid_signature"
    finally:
        await db.close()


async def test_verify_rejects_an_unsigned_envelope():
    db = Database(":memory:")
    await db.connect()
    try:
        agents = AgentsRepo(db)
        await agents.enroll("agent-1")
        event = FallEvent(camera_id="cam-1")
        envelope = Envelope.for_event("agent-1", seq=0, event=event)  # never signed

        with pytest.raises(VerificationError):
            await verify_envelope(envelope, agents)
    finally:
        await db.close()


async def test_dedupe_first_send_is_accepted_in_order():
    db = Database(":memory:")
    await db.connect()
    try:
        event = FallEvent(camera_id="cam-1")
        envelope = Envelope.for_event("agent-1", seq=0, event=event)
        result = await record_envelope(envelope, db)
        assert result.duplicate is False
        assert result.out_of_order is False
    finally:
        await db.close()


async def test_dedupe_retry_of_the_same_seq_is_a_duplicate():
    db = Database(":memory:")
    await db.connect()
    try:
        event = FallEvent(camera_id="cam-1")
        envelope = Envelope.for_event("agent-1", seq=5, event=event)
        first = await record_envelope(envelope, db)
        second = await record_envelope(envelope, db)  # the agent retried the same send

        assert first.duplicate is False
        assert second.duplicate is True
    finally:
        await db.close()


async def test_dedupe_a_lower_seq_arriving_late_is_accepted_and_flagged():
    db = Database(":memory:")
    await db.connect()
    try:
        event = FallEvent(camera_id="cam-1")
        await record_envelope(Envelope.for_event("agent-1", seq=10, event=event), db)
        late = await record_envelope(Envelope.for_event("agent-1", seq=7, event=event), db)

        assert late.duplicate is False   # seq=7 was never seen before -- genuinely new
        assert late.out_of_order is True  # but it's lower than the highest seen (10)
    finally:
        await db.close()


async def test_dedupe_tracks_agents_independently():
    db = Database(":memory:")
    await db.connect()
    try:
        event = FallEvent(camera_id="cam-1")
        # same seq, two different agents -- must not collide
        a = await record_envelope(Envelope.for_event("agent-a", seq=1, event=event), db)
        b = await record_envelope(Envelope.for_event("agent-b", seq=1, event=event), db)
        assert a.duplicate is False
        assert b.duplicate is False
    finally:
        await db.close()
