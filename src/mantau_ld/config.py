"""Server-specific settings, extending mantau_core's shared base. Same
`MANTAU_`-prefixed single namespace as mantau-backend-rtsp -- see that
repo's config.py for why the prefix is never overridden per-subclass.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from functools import lru_cache
from pathlib import Path

from mantau_core.config import CoreSettings

FIREBASE_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
)


class Settings(CoreSettings):
    db_path: str = "data/mantau_ld.db"
    # Server build string for /health and telemetry. Empty falls back to
    # RAILWAY_GIT_COMMIT_SHA, then "unknown".
    build_id: str = ""

    # Users sign in with Firebase Authentication. Defaults to the project that
    # delivers push (MANTAU_FCM_PROJECT_ID), which is the same Firebase project.
    firebase_project_id: str = ""
    auth_leeway_s: int = 30
    control_plane_encryption_key: str = ""
    command_ttl_s: int = 300
    command_delivery_lease_s: int = 30
    agent_offline_after_s: int = 30
    # Single-use keys a household owner/admin creates in the app and enters on
    # a new agent. Only their SHA-256 is stored.
    enrollment_key_ttl_s: int = 3600
    household_invite_ttl_s: int = 48 * 3600
    # Event clips uploaded by agents. Keep on a persistent volume.
    recordings_dir: str = "data/recordings"
    recording_max_bytes: int = 20 * 1024 * 1024
    recording_household_max_bytes: int = 1024 * 1024 * 1024
    recording_global_max_bytes: int = 5 * 1024 * 1024 * 1024
    recording_retention_days: int = 30
    event_retention_days: int = 30
    maintenance_interval_s: float = 3600.0
    frame_max_bytes: int = 2 * 1024 * 1024

    # Server inference (POST /agents/{id}/inference): agents without a usable
    # on-device detector upload sampled frames and this server runs the same
    # detector. Frames are processed in memory and never stored.
    inference_enabled: bool = True
    inference_max_frame_bytes: int = 512 * 1024
    inference_max_frame_age_s: float = 10.0
    inference_max_clock_skew_s: float = 5.0
    inference_max_fps: float = 15.0
    inference_session_idle_s: float = 120.0
    inference_max_sessions: int = 8
    inference_workers: int = 2
    inference_idempotency_ttl_s: float = 300.0
    inference_result_retention_days: int = 30
    invite_attempt_limit: int = 10
    invite_attempt_window_s: int = 3600

    # Browser origins allowed to call the API (comma-separated). Empty means no
    # CORS at all -- the mobile app and agents do not need it.
    cors_origins: str = ""
    # Swagger/ReDoc/OpenAPI, off unless explicitly enabled for an operator.
    api_docs_enabled: bool = False

    def cors_origin_list(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def docs_enabled(self) -> bool:
        return self.api_docs_enabled

    @property
    def auth_project_id(self) -> str:
        return (self.firebase_project_id or self.fcm_project_id or "").strip()

    @property
    def auth_issuer(self) -> str:
        project = self.auth_project_id
        return f"https://securetoken.google.com/{project}" if project else ""

    def configuration_problems(self) -> list[str]:
        """Names of settings the server cannot run without. Names only --
        never values -- so /ready and startup logs can show them safely."""
        problems = []
        if not self.auth_project_id:
            problems.append("MANTAU_FIREBASE_PROJECT_ID")
        if not _is_fernet_key(self.control_plane_encryption_key):
            problems.append("MANTAU_CONTROL_PLANE_ENCRYPTION_KEY")
        if not self.push_configured:
            problems.append("MANTAU_FCM_PROJECT_ID/MANTAU_FCM_SERVICE_ACCOUNT_PATH")
        return problems


def _is_fernet_key(value: str) -> bool:
    from cryptography.fernet import Fernet

    try:
        Fernet(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError, TypeError):
        return False
    return True


def build_info(settings: Settings | None = None) -> dict:
    """Which builds are running: server, mantau-core and mantau-AI. No secrets."""
    server = (settings.build_id if settings is not None else "").strip()
    server = (server or os.environ.get("MANTAU_BUILD_ID", "").strip()
              or os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip() or "unknown")
    return {"server": server, "core": _core_build(), "ai": _ai_build()}


@lru_cache(maxsize=1)
def _core_build() -> str:
    # The Dockerfile copies the repository root to /app, where the ref file sits.
    try:
        ref = (Path(__file__).resolve().parents[2] / "mantau-core.ref").read_text(encoding="utf-8")
        return ref.strip() or "unknown"
    except Exception:  # noqa: BLE001 -- a missing ref must not fail /health
        return "unknown"


@lru_cache(maxsize=1)
def _ai_build() -> str:
    try:
        raw = importlib.metadata.distribution("mantau-prototype").read_text("direct_url.json")
        return str(json.loads(raw)["vcs_info"]["commit_id"])
    except Exception:  # noqa: BLE001 -- no git checkout: a local wheel has no vcs_info
        pass
    # The image installs the detector from a local wheel; the Dockerfile copies
    # private-deps/SHA256SUMS next to the repo and checks it.
    try:
        sums = (Path(__file__).resolve().parents[2] / "private-deps" / "SHA256SUMS").read_text(encoding="utf-8")
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = Path(parts[-1]).name
            digest = parts[0].lower()
            if name.startswith("mantau_prototype-") and name.endswith(".whl") and all(
                    c in "0123456789abcdef" for c in digest):
                return "wheel-" + digest[:12]
    except Exception:  # noqa: BLE001 -- unreadable sums must not fail /health
        pass
    return "unknown"
