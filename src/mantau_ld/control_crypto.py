"""Encryption boundary for short-lived camera command credentials."""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise RuntimeError("MANTAU_CONTROL_PLANE_ENCRYPTION_KEY is required")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("MANTAU_CONTROL_PLANE_ENCRYPTION_KEY must be a Fernet key") from exc

    def encrypt(self, value: dict) -> bytes:
        return self._fernet.encrypt(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    def decrypt(self, value: bytes) -> dict:
        try:
            return json.loads(self._fernet.decrypt(value))
        except (InvalidToken, ValueError, TypeError) as exc:
            raise RuntimeError("encrypted command payload cannot be decrypted") from exc
