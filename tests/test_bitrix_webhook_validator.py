import httpx
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
    def __init__(self, responses: list[_FakeResponse] | None = None, error: httpx.HTTPError | None = None):
        self._responses = responses or []
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, json: dict, headers: dict) -> _FakeResponse:
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_rejects_invalid_webhook_format() -> None:
    result = await validate_bitrix_credentials(
        {"webhook_url": "https://portal.example.com/rest/1/secret123/"}
    )

    assert result.status == BitrixCredentialsValidationStatus.INVALID_WEBHOOK
    assert "secret123" not in (result.details or "")


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_rejects_private_or_local_hosts(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    async def fake_is_public_host(_: str) -> bool:
        return False

    monkeypatch.setattr(validator, "_is_public_host", fake_is_public_host)

    result = await validate_bitrix_credentials(
        {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"}
    )

    assert result.status == BitrixCredentialsValidationStatus.UNSAFE_HOST
    assert "secret123" not in (result.details or "")


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_rejects_localhost_format_before_dns() -> None:
    result = await validate_bitrix_credentials(
        {"webhook_url": "https://localhost/rest/1/secret123/"}
    )

    assert result.status == BitrixCredentialsValidationStatus.INVALID_WEBHOOK


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_maps_invalid_webhook_response(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    async def fake_is_public_host(_: str) -> bool:
        return True

    monkeypatch.setattr(validator, "_is_public_host", fake_is_public_host)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient([_FakeResponse(401, {"error": "NO_AUTH_FOUND"})]),
    )

    result = await validate_bitrix_credentials(
        {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"}
    )

    assert result.status == BitrixCredentialsValidationStatus.INVALID_CREDENTIALS
    assert "secret123" not in (result.details or "")


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_maps_access_denied(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    async def fake_is_public_host(_: str) -> bool:
        return True

    monkeypatch.setattr(validator, "_is_public_host", fake_is_public_host)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient([_FakeResponse(403, {"error": "ACCESS_DENIED"})]),
    )

    result = await validate_bitrix_credentials(
        {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"}
    )

    assert result.status == BitrixCredentialsValidationStatus.ACCESS_DENIED
    assert "secret123" not in (result.details or "")


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_maps_upstream_errors(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    async def fake_is_public_host(_: str) -> bool:
        return True

    monkeypatch.setattr(validator, "_is_public_host", fake_is_public_host)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient(error=httpx.ConnectError("boom")),
    )

    result = await validate_bitrix_credentials(
        {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"}
    )

    assert result.status == BitrixCredentialsValidationStatus.UPSTREAM_ERROR


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_maps_rate_limit_to_upstream(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    async def fake_is_public_host(_: str) -> bool:
        return True

    monkeypatch.setattr(validator, "_is_public_host", fake_is_public_host)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient([_FakeResponse(429, {"error": "QUERY_LIMIT_EXCEEDED"})]),
    )

    result = await validate_bitrix_credentials(
        {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"}
    )

    assert result.status == BitrixCredentialsValidationStatus.RATE_LIMITED


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_normalizes_method_suffix_and_query_params(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    async def fake_is_public_host(_: str) -> bool:
        return True

    monkeypatch.setattr(validator, "_is_public_host", fake_is_public_host)
    monkeypatch.setattr(
        validator.httpx,
        "AsyncClient",
        lambda **_: _FakeAsyncClient([_FakeResponse(200, {"result": "2026-01-01T00:00:00+00:00"})]),
    )

    result = await validate_bitrix_credentials(
        {
            "portal_url": "https://portal.bitrix24.com/",
            "webhook_url": "https://portal.bitrix24.com/rest/1/secret123/crm.lead.add.json?ID=42",
        }
    )

    assert result == BitrixCredentialsValidationResult(
        status=BitrixCredentialsValidationStatus.VALID,
        normalized_credentials={
            "webhook_url": "https://portal.bitrix24.com/rest/1/secret123/",
            "portal_url": "https://portal.bitrix24.com",
        },
        details=None,
    )


@pytest.mark.asyncio
async def test_validate_bitrix_credentials_rejects_mismatched_portal_url(monkeypatch) -> None:
    import services.bitrix_webhook_validator as validator

    async def fake_is_public_host(_: str) -> bool:
        return True

    monkeypatch.setattr(validator, "_is_public_host", fake_is_public_host)

    result = await validate_bitrix_credentials(
        {
            "portal_url": "https://other.bitrix24.com",
            "webhook_url": "https://portal.bitrix24.com/rest/1/secret123/",
        }
    )

    assert result.status == BitrixCredentialsValidationStatus.INVALID_FORMAT
    assert "secret123" not in (result.details or "")
