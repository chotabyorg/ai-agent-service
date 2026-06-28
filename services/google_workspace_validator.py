"""Validation and normalization for Google Workspace credentials."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mcp_clients.servers.google_workspace.config.settings import (
    ALLOWED_GOOGLE_SCOPES,
    GOOGLE_PRODUCT_SCOPES,
)

REQUIRED_AUTH_FIELDS = frozenset({
    "client_id",
    "client_secret",
    "refresh_token",
})
REQUIRED_FIELDS = REQUIRED_AUTH_FIELDS | {"scopes"}

OPTIONAL_FIELDS = frozenset({
    "redirect_uri",
    "token_storage_mode",
})

SENSITIVE_FIELDS = frozenset({
    "client_secret",
    "refresh_token",
    "access_token",
    "private_key",
})


class GoogleCredentialsValidationStatus(Enum):
    VALID = "valid"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_SCOPES = "invalid_scopes"
    INSUFFICIENT_SCOPES = "insufficient_scopes"
    INVALID_FIELD_TYPE = "invalid_field_type"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_REFRESH_TOKEN = "invalid_refresh_token"


@dataclass(slots=True)
class GoogleCredentialsValidationResult:
    status: GoogleCredentialsValidationStatus
    normalized_credentials: dict[str, Any] | None = None
    details: str | None = None


def _parse_scopes(scopes_raw: str) -> list[str]:
    parts = scopes_raw.replace(",", " ").split()
    return [scope.strip() for scope in parts if scope.strip()]


async def check_refresh_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> bool:
    try:
        import httpx
    except ImportError:
        return True

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            return response.status_code == 200
    except Exception:
        return True


_default_refresh_check = check_refresh_token


def set_refresh_check(check: callable) -> None:
    global _default_refresh_check
    _default_refresh_check = check


async def _validate_refresh_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> GoogleCredentialsValidationResult | None:
    try:
        is_valid = await _default_refresh_check(client_id, client_secret, refresh_token)
    except Exception:
        return None
    if not is_valid:
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INVALID_REFRESH_TOKEN,
            details="Invalid refresh token. Could not obtain an access token from Google.",
        )
    return None


async def validate_google_workspace_credentials(
    credentials: dict[str, Any],
) -> GoogleCredentialsValidationResult:
    if not isinstance(credentials, dict):
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INVALID_FIELD_TYPE,
            details="Google Workspace credentials must be a JSON object.",
        )

    missing = [
        field
        for field in REQUIRED_AUTH_FIELDS
        if not isinstance(credentials.get(field), str) or not credentials[field].strip()
    ]
    if missing:
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.MISSING_REQUIRED_FIELD,
            details=f"Missing required fields: {', '.join(sorted(missing))}",
        )

    client_id = credentials["client_id"]
    client_secret = credentials["client_secret"]
    refresh_token = credentials["refresh_token"]

    if not isinstance(client_id, str) or len(client_id.strip()) > 512:
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INVALID_FIELD_TYPE,
            details="client_id must be a non-empty string (max 512 chars).",
        )

    if not isinstance(client_secret, str) or len(client_secret.strip()) > 512:
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INVALID_FIELD_TYPE,
            details="client_secret must be a non-empty string (max 512 chars).",
        )

    if not isinstance(refresh_token, str) or len(refresh_token.strip()) > 2048:
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INVALID_FIELD_TYPE,
            details="refresh_token must be a non-empty string (max 2048 chars).",
        )

    scopes_raw = credentials.get("scopes", "")
    if isinstance(scopes_raw, str) and scopes_raw.strip():
        scopes = _parse_scopes(scopes_raw)
    else:
        scopes = []

    if not scopes:
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INSUFFICIENT_SCOPES,
            details="At least one supported Google Workspace scope is required.",
        )

    all_allowed = set(ALLOWED_GOOGLE_SCOPES)
    unknown = sorted(set(scopes) - all_allowed)
    if unknown:
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INVALID_SCOPES,
            details=f"Unsupported scopes: {', '.join(unknown)}",
        )

    scope_set = set(scopes)
    if not any(scope_set & valid_scopes for valid_scopes in GOOGLE_PRODUCT_SCOPES.values()):
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INSUFFICIENT_SCOPES,
            details="Provided scopes do not cover any supported Google Workspace product.",
        )

    all_known_fields = REQUIRED_FIELDS | OPTIONAL_FIELDS
    unknown_keys = sorted(set(credentials.keys()) - all_known_fields)
    if unknown_keys:
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.UNKNOWN_FIELD,
            details=f"Unknown credential fields: {', '.join(unknown_keys)}. "
                     f"Allowed: {', '.join(sorted(all_known_fields))}",
        )

    normalized = {
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "refresh_token": refresh_token.strip(),
    }

    if scopes:
        normalized["scopes"] = scopes_raw.strip()

    redirect_uri = credentials.get("redirect_uri")
    if isinstance(redirect_uri, str) and redirect_uri.strip():
        normalized["redirect_uri"] = redirect_uri.strip()

    token_storage_mode = credentials.get("token_storage_mode")
    if isinstance(token_storage_mode, str) and token_storage_mode.strip():
        normalized["token_storage_mode"] = token_storage_mode.strip()

    refresh_result = await _validate_refresh_token(
        client_id.strip(), client_secret.strip(), refresh_token.strip()
    )
    if refresh_result is not None:
        return refresh_result

    return GoogleCredentialsValidationResult(
        status=GoogleCredentialsValidationStatus.VALID,
        normalized_credentials=normalized,
    )
