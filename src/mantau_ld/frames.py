"""Latest-frame-per-camera, in memory, nothing else.

Deliberately NOT in SQLite and NOT through the envelope/spool/dedupe path
that events take: a frame is only worth showing while it's current. Replaying
a spooled frame from a minute ago is worse than showing nothing, and writing
every frame to the DB would churn the disk for data nobody reads twice.

Single-hub by design (see the app's live view) -- one process holds the
frames, so a restart drops them and the next agent push refills within a
frame interval.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass


_REORDER_WINDOW_MS = 5_000


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    received_at: float
    household_id: str
    agent_id: str
    camera_id: str
    captured_at_ms: int | None = None


class FrameStore:
    """Latest frame per camera, plus a way to wait for the next one.

    `wait_for_next` is what makes the MJPEG endpoint stream at the rate the
    agent actually pushes instead of busy-polling: each push wakes every
    waiter once, then a fresh Event is installed for the following frame.
    """

    def __init__(self, *, stale_after_s: float = 15.0, snapshot_viewer_s: float = 5.0) -> None:
        self._frames: dict[str, Frame] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._stale_after_s = stale_after_s
        self._streams: dict[str, int] = {}
        self._snapshot_at: dict[str, float] = {}
        self._snapshot_viewer_s = snapshot_viewer_s

    @contextmanager
    def watching(self, camera_id: str):
        """An open live stream; agents upload at video rate while any exist."""
        self._streams[camera_id] = self._streams.get(camera_id, 0) + 1
        try:
            yield
        finally:
            remaining = self._streams.get(camera_id, 1) - 1
            if remaining > 0:
                self._streams[camera_id] = remaining
            else:
                self._streams.pop(camera_id, None)

    def snapshot_requested(self, camera_id: str) -> None:
        self._snapshot_at[camera_id] = time.monotonic()

    def viewers(self, camera_id: str) -> int:
        """Open streams, plus one for a snapshot fetched in the last few seconds."""
        recent = time.monotonic() - self._snapshot_at.get(camera_id, float("-inf"))
        return self._streams.get(camera_id, 0) + (1 if recent <= self._snapshot_viewer_s else 0)

    def put(self, camera_id: str, jpeg: bytes, *, household_id: str, agent_id: str,
            captured_at_ms: int | None = None) -> bool:
        """Stores the frame unless a newer one from the same agent is already
        current (agents upload several frames at once). False when dropped."""
        current = self._frames.get(camera_id)
        if (captured_at_ms is not None and current is not None
                and current.agent_id == agent_id and current.captured_at_ms is not None
                # Only a short reorder window: a much older time means the
                # agent's clock or stream restarted, and the frame is new.
                and current.captured_at_ms - _REORDER_WINDOW_MS
                < captured_at_ms <= current.captured_at_ms):
            return False
        self._frames[camera_id] = Frame(
            jpeg=jpeg, received_at=time.monotonic(), household_id=household_id,
            agent_id=agent_id, camera_id=camera_id, captured_at_ms=captured_at_ms,
        )
        waiter = self._waiters.pop(camera_id, None)
        if waiter is not None:
            waiter.set()
        return True

    def latest(self, camera_id: str, *, household_id: str | None = None) -> Frame | None:
        frame = self._frames.get(camera_id)
        if frame is not None and household_id is not None and frame.household_id != household_id:
            return None
        return frame

    def is_live(self, camera_id: str) -> bool:
        frame = self._frames.get(camera_id)
        if frame is None:
            return False
        return (time.monotonic() - frame.received_at) <= self._stale_after_s

    async def wait_for_next(
        self, camera_id: str, *, household_id: str, timeout_s: float
    ) -> Frame | None:
        waiter = self._waiters.get(camera_id)
        if waiter is None:
            waiter = asyncio.Event()
            self._waiters[camera_id] = waiter
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout_s)
        except TimeoutError:
            return None
        return self.latest(camera_id, household_id=household_id)
