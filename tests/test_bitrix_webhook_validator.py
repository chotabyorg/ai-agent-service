import pytest

from services.bitrix_webhook_validator import (
    BitrixCredentialsValidationResult,
    BitrixCredentialsValidationStatus,
    validate_bitrix_credentials,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, json: dict, headers: dict) -> _FakeResponse:
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_rejects_bad_portal_format() -> None:
    result = await validate_bitrix_credentials({"portal_url": "http://portal.bitrix24.com"})
    assert result.status == BitrixCredentialsValidationStatus.INVALID_FORMAT


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_rejects_unsafe_host(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    monkeypatch.setattr(validator, "_is_public_host", lambda _: False)

    result = await validate_bitrix_credentials({"portal_url": "https://portal.bitrix24.com"})
    assert result.status == BitrixCredentialsValidationStatus.UNSAFE_HOST


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_validates_portal_only(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    monkeypatch.setattr(validator, "_is_public_host", lambda _: True)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient([_FakeResponse(401, {"error": "NO_AUTH_FOUND"})]),
    )

    result = await validate_bitrix_credentials({"portal_url": "https://portal.bitrix24.com/"})
    assert result.status == BitrixCredentialsValidationStatus.VALID
    assert result.normalized_credentials == {"portal_url": "https://portal.bitrix24.com"}


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_rejects_invalid_webhook_format(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    monkeypatch.setattr(validator, "_is_public_host", lambda _: True)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient([_FakeResponse(401, {"error": "NO_AUTH_FOUND"})]),
    )

    result = await validate_bitrix_credentials(
        {"portal_url": "https://portal.bitrix24.com", "webhook_url": "https://portal.bitrix24.com/rest/nope"}
    )
    assert result.status == BitrixCredentialsValidationStatus.INVALID_WEBHOOK


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_accepts_portal_in_legacy_webhook_field(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    monkeypatch.setattr(validator, "_is_public_host", lambda _: True)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient([_FakeResponse(401, {"error": "NO_AUTH_FOUND"})]),
    )

    result = await validate_bitrix_credentials({"webhook_url": "https://portal.bitrix24.com"})
    assert result.status == BitrixCredentialsValidationStatus.VALID
    assert result.normalized_credentials == {"portal_url": "https://portal.bitrix24.com"}


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_rejects_webhook_wrong_token(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    monkeypatch.setattr(validator, "_is_public_host", lambda _: True)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient(
            [
                _FakeResponse(401, {"error": "NO_AUTH_FOUND"}),
                _FakeResponse(401, {"error": "NO_AUTH_FOUND"}),
            ]
        ),
    )

    result = await validate_bitrix_credentials(
        {
            "portal_url": "https://portal.bitrix24.com",
            "webhook_url": "https://portal.bitrix24.com/rest/1/badtoken/",
        }
    )
    assert result.status == BitrixCredentialsValidationStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_accepts_portal_and_webhook(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    monkeypatch.setattr(validator, "_is_public_host", lambda _: True)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient(
            [
                _FakeResponse(401, {"error": "NO_AUTH_FOUND"}),
                _FakeResponse(200, {"result": "2026-01-01T00:00:00+00:00"}),
            ]
        ),
    )

    result = await validate_bitrix_credentials(
        {
            "portal_url": "https://portal.bitrix24.com/",
            "webhook_url": "https://portal.bitrix24.com/rest/1/secret123/crm.contact.get.json?ID=42",
        }
    )
    assert result == BitrixCredentialsValidationResult(
        status=BitrixCredentialsValidationStatus.VALID,
        normalized_credentials={
            "portal_url": "https://portal.bitrix24.com",
            "webhook_url": "https://portal.bitrix24.com/rest/1/secret123/",
        },
        details=None,
    )
