"""User and enrolled-agent authentication for the control plane."""

from __future__ import annotations

import hmac
import json

from fastapi import HTTPException, Request


def current_user(request: Request) -> str:
    settings = request.app.state.settings
    if settings.control_plane_mode == "disabled":
        raise HTTPException(503, "control_plane_disabled")
    if settings.control_plane_mode == "local_dev":
        user_id = request.headers.get("X-Mantau-User-ID", "").strip()
        if not user_id:
            raise HTTPException(401, "missing_local_development_identity")
        return user_id
    try:
        tokens = json.loads(settings.control_plane_auth_tokens_json)
    except (TypeError, ValueError):
        raise HTTPException(503, "control_plane_auth_misconfigured")
    if not isinstance(tokens, dict):
        raise HTTPException(503, "control_plane_auth_misconfigured")
    authorization = request.headers.get("Authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    for token, user_id in tokens.items():
        if supplied and hmac.compare_digest(str(token), supplied):
            return str(user_id)
    if not tokens:
        raise HTTPException(503, "control_plane_auth_not_configured")
    raise HTTPException(401, "invalid_bearer_token")


async def authenticated_agent(request: Request):
    agent_id = request.headers.get("X-Mantau-Agent-ID", "")
    supplied = request.headers.get("X-Mantau-Agent-Secret", "")
    if not agent_id or not supplied:
        raise HTTPException(401, "missing_agent_credentials")
    agent = await request.app.state.agents_repo.get(agent_id)
    if agent is None or not hmac.compare_digest(agent.secret, supplied):
        raise HTTPException(401, "invalid_agent_credentials")
    return agent
