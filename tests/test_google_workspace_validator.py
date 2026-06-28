from unittest.mock import AsyncMock

import services.google_workspace_validator as validator


async def test_google_workspace_validator_rejects_empty_scopes(monkeypatch) -> None:
    monkeypatch.setattr(
        validator,
        "_default_refresh_check",
        AsyncMock(return_value=True),
    )

    result = await validator.validate_google_workspace_credentials({
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "scopes": "",
    })

    assert result.status is validator.GoogleCredentialsValidationStatus.INSUFFICIENT_SCOPES
    assert result.normalized_credentials is None


async def test_google_workspace_validator_rejects_invalid_refresh_token(monkeypatch) -> None:
    refresh_check = AsyncMock(return_value=False)
    monkeypatch.setattr(validator, "_default_refresh_check", refresh_check)

    result = await validator.validate_google_workspace_credentials({
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "definitely-invalid",
        "scopes": "https://www.googleapis.com/auth/spreadsheets.readonly",
    })

    assert result.status is validator.GoogleCredentialsValidationStatus.INVALID_REFRESH_TOKEN
    assert result.normalized_credentials is None
    assert "definitely-invalid" not in (result.details or "")
    refresh_check.assert_awaited_once()
