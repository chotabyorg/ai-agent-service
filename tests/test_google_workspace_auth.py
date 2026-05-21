import pytest

from mcp_clients.servers.google_workspace.auth.oauth import (
    GoogleAuthError,
    get_refresh_token,
    validate_required_scopes,
)
from mcp_clients.servers.google_workspace.config.settings import GoogleWorkspaceSettings, SHEETS_SCOPE


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
