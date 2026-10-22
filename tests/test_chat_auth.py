from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

import api.v1.chat as chat_route
from core.config import Settings


def test_strict_chat_auth_rejects_empty_token_without_exposing_it() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        Settings(
            _env_file=None,
            chat_s2s_auth_required=True,
            chat_s2s_token="",
        )

    assert "CHAT_S2S_TOKEN must be configured" in str(exc_info.value)


def test_compatibility_chat_auth_accepts_empty_token() -> None:
    settings = Settings(
        _env_file=None,
        chat_s2s_auth_required=False,
        chat_s2s_token="",
    )

    assert settings.chat_s2s_token.get_secret_value() == ""


@pytest.mark.asyncio
async def test_compatibility_mode_allows_legacy_callers(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_route,
        "get_settings",
        lambda: SimpleNamespace(
            chat_s2s_token=SecretStr(""),
            chat_s2s_auth_required=False,
        ),
    )

    assert await chat_route.authenticate_chat_caller() is None


@pytest.mark.asyncio
async def test_wrong_supplied_token_is_rejected_in_compatibility_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_route,
        "get_settings",
        lambda: SimpleNamespace(
            chat_s2s_token=SecretStr("expected"),
            chat_s2s_auth_required=False,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await chat_route.authenticate_chat_caller(x_service_token="wrong")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_supplied_token_is_rejected_without_comparison_error(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_route,
        "get_settings",
        lambda: SimpleNamespace(
            chat_s2s_token=SecretStr("expected"),
            chat_s2s_auth_required=True,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await chat_route.authenticate_chat_caller(
            x_service_token="not-expected-\u00e9",
            x_user_id=str(uuid4()),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_strict_mode_requires_token_and_owner(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_route,
        "get_settings",
        lambda: SimpleNamespace(
            chat_s2s_token=SecretStr("expected"),
            chat_s2s_auth_required=True,
        ),
    )

    with pytest.raises(HTTPException) as missing_token:
        await chat_route.authenticate_chat_caller(x_user_id=str(uuid4()))
    assert missing_token.value.status_code == 401

    with pytest.raises(HTTPException) as missing_owner:
        await chat_route.authenticate_chat_caller(x_service_token="expected")
    assert missing_owner.value.status_code == 401


@pytest.mark.asyncio
async def test_strict_mode_accepts_valid_token_and_parses_owner(monkeypatch) -> None:
    owner_id = uuid4()
    monkeypatch.setattr(
        chat_route,
        "get_settings",
        lambda: SimpleNamespace(
            chat_s2s_token=SecretStr("expected"),
            chat_s2s_auth_required=True,
        ),
    )

    result = await chat_route.authenticate_chat_caller(
        x_service_token="expected",
        x_user_id=str(owner_id),
    )

    assert result == owner_id


@pytest.mark.asyncio
async def test_invalid_owner_header_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_route,
        "get_settings",
        lambda: SimpleNamespace(
            chat_s2s_token=SecretStr(""),
            chat_s2s_auth_required=False,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await chat_route.authenticate_chat_caller(x_user_id="not-a-uuid")

    assert exc_info.value.status_code == 401
