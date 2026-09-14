"""Confirm an envelope actually came from a known, enrolled agent.

Two independent checks, both required: the `agent_id` must be enrolled, AND
the signature must verify against THAT agent's secret. Neither alone proves
anything -- an unenrolled `agent_id` with a plausible-looking `sig` is
meaningless, and a known `agent_id` with a bad `sig` could be a forged sender.
"""

from __future__ import annotations

from mantau_core.contracts import Envelope

from ..store.agents_repo import AgentsRepo


class VerificationError(Exception):
    """`reason` matches one of PROTOCOL.md's rejection reasons."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def verify_envelope(envelope: Envelope, agents: AgentsRepo) -> None:
    """Raises VerificationError if the envelope should be rejected outright.

    Deliberately side-effect-free: bumping the agent's `last_seen_at` is the
    caller's job, done only once ingest actually succeeds, not here.
    """
    agent = await agents.get(envelope.agent_id)
    if agent is None:
        raise VerificationError("unknown_agent")
    if not envelope.verify(agent.secret):
        raise VerificationError("invalid_signature")
