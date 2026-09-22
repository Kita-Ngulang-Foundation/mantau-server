"""Server-specific settings, extending mantau_core's shared base. Same
`MANTAU_`-prefixed single namespace as mantau-backend-rtsp -- see that
repo's config.py for why the prefix is never overridden per-subclass.
"""

from __future__ import annotations

from mantau_core.config import CoreSettings
from typing import Literal


class Settings(CoreSettings):
    db_path: str = "data/mantau_ld.db"

    # Disabled preserves the legacy API surface. Local development requires
    # an explicit X-Mantau-User-ID header; production requires configured
    # bearer-token mappings and otherwise fails closed.
    control_plane_mode: Literal["disabled", "local_dev", "production"] = "disabled"
    control_plane_auth_tokens_json: str = "{}"
    control_plane_encryption_key: str = ""
    command_ttl_s: int = 300
    command_delivery_lease_s: int = 30
    agent_offline_after_s: int = 120

    # TEMPORARY, comma-separated chat ids -- see mantau_core.notify.channels.telegram.
    telegram_chat_ids: str = ""

    def telegram_chat_id_list(self) -> list[str]:
        return [c.strip() for c in self.telegram_chat_ids.split(",") if c.strip()]
