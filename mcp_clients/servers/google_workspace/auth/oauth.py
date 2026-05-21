"""Google OAuth 2.0 helpers for the Google Workspace MCP server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp_clients.servers.google_workspace.config.settings import GoogleWorkspaceSettings


class GoogleAuthError(RuntimeError):
    """Raised when Google OAuth configuration or credentials are unavailable."""

    def __init__(self, code: str, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable


def validate_required_scopes(configured_scopes: list[str], required_scopes: list[str]) -> None:
    """Ensure the configured OAuth scope set covers a feature."""

    missing = sorted(set(required_scopes) - set(configured_scopes))
    if missing:
        raise GoogleAuthError(
            "GOOGLE_SCOPE_MISSING",
            "Google OAuth scopes are missing: " + ", ".join(missing),
        )


def _load_local_token(settings: GoogleWorkspaceSettings) -> dict[str, Any]:
    token_path = Path(settings.google_token_file)
    if not token_path.exists():
        return {}
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleAuthError(
            "GOOGLE_TOKEN_STORAGE_INVALID",
            f"Local Google token storage is unreadable: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise GoogleAuthError("GOOGLE_TOKEN_STORAGE_INVALID", "Local Google token storage must be a JSON object.")
    return data


def get_refresh_token(settings: GoogleWorkspaceSettings) -> str:
    """Resolve the refresh token from env or local dev token storage."""

    if settings.google_refresh_token:
        return settings.google_refresh_token
    if settings.google_token_storage_mode == "local":
        token_data = _load_local_token(settings)
        refresh_token = token_data.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            return refresh_token
    raise GoogleAuthError(
        "GOOGLE_AUTH_REQUIRED",
        "Google OAuth authorization is required.",
    )


def build_credentials(settings: GoogleWorkspaceSettings, required_scopes: list[str]):
    """Build refreshable Google credentials for API clients."""

    validate_required_scopes(settings.scopes, required_scopes)
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleAuthError(
            "GOOGLE_AUTH_CONFIG_MISSING",
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required.",
        )

    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise GoogleAuthError(
            "GOOGLE_DEPENDENCY_MISSING",
            "Google API dependencies are not installed.",
            recoverable=False,
        ) from exc

    return Credentials(
        token=None,
        refresh_token=get_refresh_token(settings),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=settings.scopes,
    )


def build_authorization_url(settings: GoogleWorkspaceSettings, scopes: list[str] | None = None) -> str:
    """Build an OAuth consent URL for local development setup."""

    if not settings.google_client_id or not settings.google_redirect_uri:
        raise GoogleAuthError(
            "GOOGLE_AUTH_CONFIG_MISSING",
            "GOOGLE_CLIENT_ID and GOOGLE_REDIRECT_URI are required.",
        )

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise GoogleAuthError(
            "GOOGLE_DEPENDENCY_MISSING",
            "google-auth-oauthlib is not installed.",
            recoverable=False,
        ) from exc

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.google_redirect_uri],
            }
        },
        scopes=scopes or settings.scopes,
        redirect_uri=settings.google_redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url
