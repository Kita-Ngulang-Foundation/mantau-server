"""Server-specific settings, extending mantau_core's shared base. Same
`MANTAU_`-prefixed single namespace as mantau-backend-rtsp -- see that
repo's config.py for why the prefix is never overridden per-subclass.
"""

from __future__ import annotations

from mantau_core.config import CoreSettings


class Settings(CoreSettings):
    db_path: str = "data/mantau_ld.db"

    # TEMPORARY, comma-separated chat ids -- see mantau_core.notify.channels.telegram.
    telegram_chat_ids: str = ""

    def telegram_chat_id_list(self) -> list[str]:
        return [c.strip() for c in self.telegram_chat_ids.split(",") if c.strip()]
