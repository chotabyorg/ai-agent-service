"""Versioned envelope encryption for MCP credential dictionaries."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from services.encryption_service import EncryptionService

ENCRYPTED_ENVELOPE_MARKER = "chota:mcp-credentials"
ENCRYPTED_ENVELOPE_VERSION = 1


class CredentialEnvelopeError(ValueError):
    """Stored MCP credentials are malformed or cannot be decrypted."""


def is_encrypted_envelope(value: Any) -> bool:
    """Identify the tagged storage array without interpreting user dictionaries.

    API credential payloads are dictionaries, while the envelope is a tagged
    JSON array. A user-supplied dictionary therefore cannot be mistaken for an
    envelope even if it contains keys that resemble envelope metadata.
    """

    return (
        isinstance(value, list)
        and bool(value)
        and value[0] == ENCRYPTED_ENVELOPE_MARKER
    )


def encrypt_credentials(
    plaintext: dict[str, Any],
    *,
    encryption_service: EncryptionService | None = None,
) -> list[Any]:
    """Encrypt a credential object into a tagged, versioned JSON array."""

    if not isinstance(plaintext, dict):
        raise TypeError("MCP credentials must be a JSON object.")

    try:
        service = encryption_service or EncryptionService()
        serialized = json.dumps(
            plaintext,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        ciphertext = service.encrypt(serialized)
    except Exception:
        raise CredentialEnvelopeError("MCP credentials could not be encrypted.") from None
    return [ENCRYPTED_ENVELOPE_MARKER, ENCRYPTED_ENVELOPE_VERSION, ciphertext]


def decrypt_credentials(
    stored: Any,
    *,
    encryption_service: EncryptionService | None = None,
) -> dict[str, Any]:
    """Return plaintext for an envelope or a copy of a legacy JSON object.

    All dictionaries are legacy credential payloads. Tagged arrays are
    reserved for envelopes and fail closed on malformed data or versions.
    Other JSON shapes are rejected because the API has always accepted objects.
    """

    if isinstance(stored, dict):
        return deepcopy(stored)

    if not is_encrypted_envelope(stored):
        raise CredentialEnvelopeError("Stored MCP credentials have an unsupported format.")

    if len(stored) != 3:
        raise CredentialEnvelopeError("Stored MCP credential envelope is malformed.")
    if type(stored[1]) is not int or stored[1] != ENCRYPTED_ENVELOPE_VERSION:
        raise CredentialEnvelopeError("Stored MCP credential envelope version is unsupported.")

    ciphertext = stored[2]
    if not isinstance(ciphertext, str) or not ciphertext:
        raise CredentialEnvelopeError("Stored MCP credential envelope is malformed.")

    try:
        service = encryption_service or EncryptionService()
        serialized = service.decrypt(ciphertext)
        payload = json.loads(serialized)
    except Exception:
        # Do not propagate crypto/base64/parser exception text into API errors
        # or logs; those exceptions can contain untrusted stored values.
        raise CredentialEnvelopeError("Stored MCP credentials could not be decrypted.") from None

    if not isinstance(payload, dict):
        raise CredentialEnvelopeError("Decrypted MCP credentials are not a JSON object.")
    return payload
