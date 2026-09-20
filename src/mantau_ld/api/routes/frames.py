"""Live view: the agent pushes JPEG frames up, the app pulls them back down.

The upload is signed the same way `/ingest` is (HMAC-SHA256 with the agent's
enrolled secret) but deliberately does NOT use an Envelope: envelopes carry a
sequence number through dedupe and the durable spool, which is exactly wrong
for frames -- they're disposable, and a retried frame is a stale frame.

Read endpoints are unauthenticated, matching `/events` and the rest of this
server's current posture (see the README's "Known gaps" -- no auth anywhere
yet). That's a real gap to close before this is in front of anyone's actual
camera, not something this route invented.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ...frames import FrameStore
from ...store.agents_repo import AgentsRepo
from ..deps import get_agents_repo, get_frames

router = APIRouter(tags=["frames"])

_BOUNDARY = "mantauframe"
# One JPEG per push at the agent's frame rate; this only bounds how long a
# viewer waits before we re-send the current frame to keep the connection warm.
_STREAM_IDLE_TIMEOUT_S = 5.0


async def _verify(camera_id: str, body: bytes, agent_id: str, signature: str, agents: AgentsRepo) -> None:
    agent = await agents.get(agent_id)
    if agent is None:
        raise HTTPException(401, "unknown_agent")
    expected = hmac.new(
        agent.secret.encode("utf-8"), camera_id.encode("utf-8") + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "invalid_signature")


@router.post("/cameras/{camera_id}/frame", status_code=204)
async def push_frame(
    camera_id: str,
    request: Request,
    x_mantau_agent: str = Header(...),
    x_mantau_signature: str = Header(...),
    agents: AgentsRepo = Depends(get_agents_repo),
    frames: FrameStore = Depends(get_frames),
) -> Response:
    body = await request.body()
    await _verify(camera_id, body, x_mantau_agent, x_mantau_signature, agents)
    frames.put(camera_id, body)
    return Response(status_code=204)


@router.get("/cameras/{camera_id}/snapshot.jpg")
async def snapshot(camera_id: str, frames: FrameStore = Depends(get_frames)) -> Response:
    frame = frames.latest(camera_id)
    if frame is None:
        raise HTTPException(404, "no frame received for this camera yet")
    return Response(
        content=frame.jpeg,
        media_type="image/jpeg",
        # The whole point is that this changes constantly -- a cached snapshot
        # is a frozen "live" view, which is worse than an error.
        headers={"Cache-Control": "no-store"},
    )


@router.get("/cameras/{camera_id}/live.mjpeg")
async def live(camera_id: str, frames: FrameStore = Depends(get_frames)) -> StreamingResponse:
    async def stream():
        # Start from whatever is already current: a viewer opening a stream on
        # a live camera must not stare at nothing until the next push happens.
        current = frames.latest(camera_id)
        while True:
            if current is not None:
                jpeg = current.jpeg
                yield (
                    f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode() + jpeg + b"\r\n"
            nxt = await frames.wait_for_next(camera_id, timeout_s=_STREAM_IDLE_TIMEOUT_S)
            # On idle timeout, re-send what we have rather than going silent --
            # keeps the connection (and any proxy in front of it) alive.
            current = nxt if nxt is not None else frames.latest(camera_id)

    return StreamingResponse(
        stream(),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )
