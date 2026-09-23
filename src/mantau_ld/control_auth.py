"""Separate app-user OIDC authentication from enrolled-agent authentication."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from .oidc_auth import OidcConfigurationError, OidcIdentity, OidcTokenError
from .store.identity_repo import (
    HouseholdAccessDenied,
    HouseholdSelectionRequired,
    UserPrincipal,
)


def _unauthorized() -> HTTPException:
    return HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


async def authenticated_identity(request: Request) -> OidcIdentity:
    """The app user, before any household is chosen."""
    settings = request.app.state.settings
    if settings.control_plane_mode == "local_dev":
        subject = request.headers.get("X-Mantau-User-ID", "").strip()
        if not subject:
            raise _unauthorized()
        identity = OidcIdentity(issuer="local-dev", subject=subject)
    elif settings.control_plane_mode == "production":
        try:
            identity = request.app.state.oidc_authenticator.authenticate(
                request.headers.get("Authorization", "")
            )
        except OidcConfigurationError as exc:
            raise HTTPException(503, "authentication_unavailable") from exc
        except OidcTokenError as exc:
            raise _unauthorized() from exc
    else:
        # Retaining the value lets older deployments fail closed rather than
        # failing to parse configuration and silently choosing a user.
        raise HTTPException(503, "authentication_unavailable")
    await request.app.state.identity_repo.ensure_user(identity)
    return identity


async def authenticated_user(request: Request) -> UserPrincipal:
    identity = await authenticated_identity(request)
    requested_household = request.headers.get("X-Mantau-Household-ID", "").strip() or None
    try:
        return await request.app.state.identity_repo.resolve(
            identity.issuer, identity.subject, requested_household_id=requested_household
        )
    except HouseholdSelectionRequired as exc:
        raise HTTPException(400, "household_required") from exc
    except HouseholdAccessDenied as exc:
        raise HTTPException(404, "resource_not_found") from exc


current_user = authenticated_user


async def authenticated_agent(request: Request):
    agent_id = request.headers.get("X-Mantau-Agent-ID", "")
    supplied = request.headers.get("X-Mantau-Agent-Secret", "")
    if not agent_id or not supplied:
        raise _unauthorized()
    agent = await request.app.state.agents_repo.get(agent_id)
    if (agent is None or agent.revoked_at is not None
            or not hmac.compare_digest(agent.secret, supplied)):
        raise _unauthorized()
    return agent
