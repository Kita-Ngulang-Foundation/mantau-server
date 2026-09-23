"""Server-specific settings, extending mantau_core's shared base. Same
`MANTAU_`-prefixed single namespace as mantau-backend-rtsp -- see that
repo's config.py for why the prefix is never overridden per-subclass.
"""

from __future__ import annotations

from mantau_core.config import CoreSettings
from typing import Literal


class Settings(CoreSettings):
    db_path: str = "data/mantau_ld.db"

    # Production is the safe default. The legacy X-Mantau-User-ID identity is
    # accepted only when local_dev is selected explicitly.
    control_plane_mode: Literal["disabled", "local_dev", "production"] = "production"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_algorithms: str = "RS256"
    oidc_leeway_s: int = 30
    # Retained as a parsed setting during rollout, but no longer accepted as
    # production authentication.
    control_plane_auth_tokens_json: str = "{}"
    control_plane_encryption_key: str = ""
    command_ttl_s: int = 300
    command_delivery_lease_s: int = 30
    agent_offline_after_s: int = 120
    claim_code_ttl_s: int = 600
    claim_attempt_limit: int = 5
    claim_attempt_window_s: int = 60

    # Browser origins allowed to call the API (comma-separated). Empty means no
    # CORS at all -- the mobile app and agents do not need it.
    cors_origins: str = ""
    # Swagger/ReDoc/OpenAPI. Always on in local_dev; off in production unless
    # explicitly enabled for an operator.
    api_docs_enabled: bool = False

    # TEMPORARY, comma-separated chat ids -- see mantau_core.notify.channels.telegram.
    telegram_chat_ids: str = ""

    def telegram_chat_id_list(self) -> list[str]:
        return [c.strip() for c in self.telegram_chat_ids.split(",") if c.strip()]

    def cors_origin_list(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def docs_enabled(self) -> bool:
        return self.api_docs_enabled or self.control_plane_mode == "local_dev"

    def configuration_problems(self) -> list[str]:
        """Names of settings production cannot run without. Names only --
        never values -- so /ready and startup logs can show them safely."""
        if self.control_plane_mode == "local_dev":
            return []
        if self.control_plane_mode != "production":
            return ["MANTAU_CONTROL_PLANE_MODE"]
        problems = [
            name for name, value in (
                ("MANTAU_OIDC_ISSUER", self.oidc_issuer),
                ("MANTAU_OIDC_AUDIENCE", self.oidc_audience),
                ("MANTAU_OIDC_JWKS_URL", self.oidc_jwks_url),
                ("MANTAU_OIDC_ALGORITHMS", self.oidc_algorithm_list()),
            ) if not value
        ]
        if not _is_fernet_key(self.control_plane_encryption_key):
            problems.append("MANTAU_CONTROL_PLANE_ENCRYPTION_KEY")
        if not self.push_configured:
            problems.append("MANTAU_FCM_PROJECT_ID/MANTAU_FCM_SERVICE_ACCOUNT_PATH")
        return problems

    def oidc_algorithm_list(self) -> list[str]:
        return [value.strip() for value in self.oidc_algorithms.split(",") if value.strip()]


def _is_fernet_key(value: str) -> bool:
    from cryptography.fernet import Fernet

    try:
        Fernet(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError, TypeError):
        return False
    return True
