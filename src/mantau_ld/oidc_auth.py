"""OIDC JWT bearer validation without coupling app users to agent identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import jwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientError


class SigningKeyClient(Protocol):
    def get_signing_key_from_jwt(self, token: str): ...


class OidcConfigurationError(RuntimeError):
    pass


class OidcTokenError(ValueError):
    pass


@dataclass(frozen=True)
class OidcIdentity:
    issuer: str
    subject: str


class OidcAuthenticator:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: list[str],
        leeway_s: int = 30,
        key_client: SigningKeyClient | None = None,
    ) -> None:
        self.issuer = issuer.strip()
        self.audience = audience.strip()
        self.jwks_url = jwks_url.strip()
        self.algorithms = tuple(algorithms)
        self.leeway_s = max(0, leeway_s)
        self._key_client = key_client
        if self.configured and self._key_client is None:
            self._key_client = PyJWKClient(self.jwks_url, cache_jwk_set=True, lifespan=300)

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.audience and self.jwks_url and self.algorithms)

    def authenticate(self, authorization: str) -> OidcIdentity:
        if not self.configured or self._key_client is None:
            raise OidcConfigurationError("OIDC authentication is not configured")
        scheme, separator, token = authorization.strip().partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            raise OidcTokenError("invalid bearer token")
        try:
            signing_key = self._key_client.get_signing_key_from_jwt(token.strip())
            claims = jwt.decode(
                token.strip(),
                signing_key.key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (InvalidTokenError, PyJWKClientError, ValueError, TypeError) as exc:
            raise OidcTokenError("invalid bearer token") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise OidcTokenError("invalid bearer token")
        return OidcIdentity(issuer=self.issuer, subject=subject.strip())
