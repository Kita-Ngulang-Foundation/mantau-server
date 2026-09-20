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
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    received_at: float


class FrameStore:
    """Latest frame per camera, plus a way to wait for the next one.

    `wait_for_next` is what makes the MJPEG endpoint stream at the rate the
    agent actually pushes instead of busy-polling: each push wakes every
    waiter once, then a fresh Event is installed for the following frame.
    """

    def __init__(self, *, stale_after_s: float = 15.0) -> None:
        self._frames: dict[str, Frame] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._stale_after_s = stale_after_s

    def put(self, camera_id: str, jpeg: bytes) -> None:
        self._frames[camera_id] = Frame(jpeg=jpeg, received_at=time.monotonic())
        waiter = self._waiters.pop(camera_id, None)
        if waiter is not None:
            waiter.set()

    def latest(self, camera_id: str) -> Frame | None:
        return self._frames.get(camera_id)

    def is_live(self, camera_id: str) -> bool:
        frame = self._frames.get(camera_id)
        if frame is None:
            return False
        return (time.monotonic() - frame.received_at) <= self._stale_after_s

    async def wait_for_next(self, camera_id: str, *, timeout_s: float) -> Frame | None:
        waiter = self._waiters.get(camera_id)
        if waiter is None:
            waiter = asyncio.Event()
            self._waiters[camera_id] = waiter
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout_s)
        except TimeoutError:
            return None
        return self._frames.get(camera_id)
