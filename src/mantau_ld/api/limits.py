"""Bounded request bodies for binary uploads."""

from __future__ import annotations

from fastapi import HTTPException, Request


async def read_limited(request: Request, max_bytes: int) -> bytes:
    """The request body, refusing anything over `max_bytes` without buffering
    it all first (413)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(413, "payload_too_large")
        except ValueError as exc:
            raise HTTPException(400, "invalid_content_length") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(413, "payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)
