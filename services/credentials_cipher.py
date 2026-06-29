"""Reversible envelope encryption for MCP credentials.

MCP credentials (Bitrix webhooks, Google Workspace OAuth tokens, etc.) are
stored in PostgreSQL as JSONB. They must remain reversible because the values
are needed at runtime to launch MCP subprocesses and call external APIs.

Storage formats
---------------

Encrypted (new):
    {"__enc__": "v1", "data": "<base64 Fernet token>"}

Plaintext (legacy):
    {"webhook_url": "...", "client_id": "..."}

The presence of the ``__enc__`` marker is the only way to tell the two apart,
so plaintext dicts must not use that key. Reading either format transparently
returns the decrypted dictionary; writing always produces the envelope form.
"""

from __future__ import annotations

import json
from typing import Any

from services.encryption_service import EncryptionService

ENCRYPTED_ENVELOPE_VERSION = "v1"
_ENVELOPE_VERSION_KEY = "__enc__"
_ENVELOPE_DATA_KEY = "data"


def is_encrypted_envelope(value: Any) -> bool:
    """Return True if *value* matches the encrypted envelope shape."""

    return (
        isinstance(value, dict)
        and isinstance(value.get(_ENVELOPE_VERSION_KEY), str)
        and isinstance(value.get(_ENVELOPE_DATA_KEY), str)
    )


def encrypt_credentials(
    plaintext: dict[str, Any],
    *,
    encryption_service: EncryptionService | None = None,
) -> dict[str, Any]:
    """Serialize *plaintext* to JSON and wrap it in an encrypted envelope."""

    if not isinstance(plaintext, dict):
        raise TypeError("Credentials payload must be a dict before encryption.")

    service = encryption_service or EncryptionService()
    serialized = json.dumps(plaintext, ensure_ascii=False, sort_keys=True)
    token = service.encrypt(serialized)
    return {
        _ENVELOPE_VERSION_KEY: ENCRYPTED_ENVELOPE_VERSION,
        _ENVELOPE_DATA_KEY: token,
    }


def decrypt_credentials(
    stored: dict[str, Any] | None,
    *,
    encryption_service: EncryptionService | None = None,
) -> dict[str, Any]:
    """Return the plaintext credentials dict from *stored*.

    Backward compatibility: plaintext dicts (legacy rows written before
    encryption-at-rest landed) are returned as-is so they keep working after
    deploy without requiring a synchronous data migration.
    """

    if stored is None:
        return {}
    if not isinstance(stored, dict):
        raise TypeError("Stored credentials value must be a dict or None.")
    if not is_encrypted_envelope(stored):
        # Legacy plaintext row — return a shallow copy so callers cannot mutate
        # the SQLAlchemy-tracked attribute by accident.
        return dict(stored)

    version = stored[_ENVELOPE_VERSION_KEY]
    if version != ENCRYPTED_ENVELOPE_VERSION:
        raise ValueError(
            f"Unsupported encrypted credentials envelope version: {version!r}"
        )

    service = encryption_service or EncryptionService()
    plaintext_json = service.decrypt(stored[_ENVELOPE_DATA_KEY])
    payload = json.loads(plaintext_json)
    if not isinstance(payload, dict):
        raise ValueError("Decrypted credentials payload is not a JSON object.")
    return payload
