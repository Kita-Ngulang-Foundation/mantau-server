"""Latest heartbeat per agent, in memory -- not persisted.

A restart losing this is fine: a fresh heartbeat arrives within the agent's
normal interval anyway, and nothing safety-critical (an alert, an ack) is
ever routed through this tracker. It exists purely so `/ready` can answer
"is this agent's camera actually reachable right now" without the agent
needing a second, separate polling endpoint.
"""

from __future__ import annotations

from mantau_core.contracts import Heartbeat


class HeartbeatTracker:
    def __init__(self) -> None:
        self._latest: dict[str, Heartbeat] = {}

    def record(self, heartbeat: Heartbeat) -> None:
        self._latest[heartbeat.agent_id] = heartbeat

    def latest(self, agent_id: str) -> Heartbeat | None:
        return self._latest.get(agent_id)

    def status(self) -> dict[str, dict]:
        return {
            agent_id: {
                "camera_id": hb.camera_id,
                "camera_reachable": hb.camera_reachable,
                "detector_alive": hb.detector_alive,
                "queue_depth": hb.queue_depth,
                "sent_at": hb.sent_at.isoformat(),
            }
            for agent_id, hb in self._latest.items()
        }
