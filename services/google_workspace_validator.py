"""Validation and normalization for Google Workspace credentials."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

ALLOWED_GOOGLE_SCOPES = frozenset({
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.readonly",
})

SCOPE_GROUPS: dict[str, set[str]] = {
    "sheets": {"https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/spreadsheets.readonly"},
    "calendar": {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    },
    "gmail": {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    },
    "drive": {
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    },
    "contacts": {"https://www.googleapis.com/auth/contacts"},
}

REQUIRED_FIELDS = frozenset({
    "client_id",
    "client_secret",
    "refresh_token",
})

OPTIONAL_FIELDS = frozenset({
    "redirect_uri",
    "scopes",
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


@dataclass(slots=True)
class GoogleCredentialsValidationResult:
    status: GoogleCredentialsValidationStatus
    normalized_credentials: dict[str, Any] | None = None
    details: str | None = None


def _parse_scopes(scopes_raw: str) -> list[str]:
    parts = scopes_raw.replace(",", " ").split()
    return [scope.strip() for scope in parts if scope.strip()]


def validate_google_workspace_credentials(
    credentials: dict[str, Any],
) -> GoogleCredentialsValidationResult:
    if not isinstance(credentials, dict):
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.INVALID_FIELD_TYPE,
            details="Google Workspace credentials must be a JSON object.",
        )

    missing = [f for f in REQUIRED_FIELDS if not isinstance(credentials.get(f), str) or not credentials[f].strip()]
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

    if scopes:
        all_allowed = set(ALLOWED_GOOGLE_SCOPES)
        unknown = sorted(set(scopes) - all_allowed)
        if unknown:
            return GoogleCredentialsValidationResult(
                status=GoogleCredentialsValidationStatus.INVALID_SCOPES,
                details=f"Unsupported scopes: {', '.join(unknown)}",
            )

        scope_set = set(scopes)
        for product, valid_scopes in SCOPE_GROUPS.items():
            if scope_set & valid_scopes:
                break
        else:
            return GoogleCredentialsValidationResult(
                status=GoogleCredentialsValidationStatus.INSUFFICIENT_SCOPES,
                details="Provided scopes do not cover any supported Google Workspace product. "
                        "Minimum: at least one supported scope is required.",
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

    return GoogleCredentialsValidationResult(
        status=GoogleCredentialsValidationStatus.VALID,
        normalized_credentials=normalized,
    )
