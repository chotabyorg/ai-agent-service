import pytest

from mcp_clients.servers.google_workspace.auth.oauth import (
    GoogleAuthError,
    build_credentials,
    get_refresh_token,
    validate_required_scopes,
)
from mcp_clients.servers.google_workspace.config.settings import (
    CALENDAR_SCOPE,
    CONTACTS_SCOPE,
    DRIVE_FILE_SCOPE,
    DRIVE_METADATA_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    GoogleWorkspaceSettings,
    SHEETS_SCOPE,
)


def test_validate_required_scopes_raises_for_missing_sheet_scope() -> None:
    with pytest.raises(GoogleAuthError) as exc_info:
        validate_required_scopes([], [SHEETS_SCOPE])

    assert exc_info.value.code == "GOOGLE_SCOPE_MISSING"
    assert exc_info.value.recoverable is True


def test_get_refresh_token_requires_authorization_when_missing(tmp_path) -> None:
    settings = GoogleWorkspaceSettings(
        google_refresh_token="",
        google_token_storage_mode="local",
        google_token_file=str(tmp_path / "missing.json"),
    )

    with pytest.raises(GoogleAuthError) as exc_info:
        get_refresh_token(settings)

    assert exc_info.value.code == "GOOGLE_AUTH_REQUIRED"
    assert str(exc_info.value) == "Google OAuth authorization is required."


def test_build_credentials_requires_calendar_scope_before_token_lookup(tmp_path) -> None:
    settings = GoogleWorkspaceSettings(
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_refresh_token="refresh-token",
        google_scopes=SHEETS_SCOPE,
    )

    with pytest.raises(GoogleAuthError) as exc_info:
        build_credentials(settings, [CALENDAR_SCOPE])

    assert exc_info.value.code == "GOOGLE_SCOPE_MISSING"


def test_build_credentials_requires_gmail_send_scope_separately() -> None:
    settings = GoogleWorkspaceSettings(
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_refresh_token="refresh-token",
        google_scopes=f"{GMAIL_READONLY_SCOPE} {GMAIL_COMPOSE_SCOPE}",
    )

    with pytest.raises(GoogleAuthError) as exc_info:
        build_credentials(settings, [GMAIL_SEND_SCOPE])

    assert exc_info.value.code == "GOOGLE_SCOPE_MISSING"


def test_build_credentials_requires_drive_metadata_scope_separately() -> None:
    settings = GoogleWorkspaceSettings(
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_refresh_token="refresh-token",
        google_scopes=DRIVE_FILE_SCOPE,
    )

    with pytest.raises(GoogleAuthError) as exc_info:
        build_credentials(settings, [DRIVE_METADATA_SCOPE])

    assert exc_info.value.code == "GOOGLE_SCOPE_MISSING"


def test_build_credentials_requires_contacts_scope() -> None:
    settings = GoogleWorkspaceSettings(
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_refresh_token="refresh-token",
        google_scopes=DRIVE_FILE_SCOPE,
    )

    with pytest.raises(GoogleAuthError) as exc_info:
        build_credentials(settings, [CONTACTS_SCOPE])

    assert exc_info.value.code == "GOOGLE_SCOPE_MISSING"
