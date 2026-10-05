"""Separate app-user (Firebase) authentication from enrolled-agent authentication."""

from __future__ import annotations

import hmac
import hashlib

from fastapi import HTTPException, Request

from .oidc_auth import OidcConfigurationError, OidcIdentity, OidcTokenError
from .store.identity_repo import (
    HouseholdAccessDenied,
    HouseholdSelectionRequired,
    UserPrincipal,
)


def _unauthorized() -> HTTPException:
    return HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


async def assert_identity_active(request: Request, identity: OidcIdentity) -> None:
    """Call while holding the database lock through the protected operation."""
    identity_hash = hashlib.sha256((identity.issuer+'\0'+identity.subject).encode()).hexdigest()
    deleted = await (await request.app.state.db.conn.execute(
        'SELECT deleted_at FROM account_deletions WHERE identity_hash=?', (identity_hash,))).fetchone()
    if deleted is not None and (identity.authenticated_at is None or identity.authenticated_at <= deleted[0]):
        raise _unauthorized()


async def authenticated_identity(request: Request) -> OidcIdentity:
    """The app user (a Firebase ID token), before any household is chosen."""
    identity = await verified_identity(request)
    async with request.app.state.db.transaction():
        await assert_identity_active(request, identity)
        await request.app.state.identity_repo.ensure_user(identity)
    return identity


async def verified_identity(request: Request) -> OidcIdentity:
    """Verify a token without provisioning a profile during account cleanup."""
    try:
        identity = request.app.state.oidc_authenticator.authenticate(
            request.headers.get("Authorization", "")
        )
    except OidcConfigurationError as exc:
        raise HTTPException(503, "authentication_unavailable") from exc
    except OidcTokenError as exc:
        raise _unauthorized() from exc
    # Cleanup verifies ownership without provisioning, but stale credentials
    # must also be barred from interfering with a newly recreated profile.
    async with request.app.state.db.serialized():
        await assert_identity_active(request, identity)
    return identity


async def authenticated_user(request: Request) -> UserPrincipal:
    requested_household = request.headers.get("X-Mantau-Household-ID", "").strip() or None
    try:
        async with request.app.state.db.transaction():
            identity = await authenticated_identity(request)
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
