"""Unit tests for the MCP credentials envelope cipher."""

from __future__ import annotations

import pytest

from services.credentials_cipher import (
    ENCRYPTED_ENVELOPE_VERSION,
    decrypt_credentials,
    encrypt_credentials,
    is_encrypted_envelope,
)


class TestIsEncryptedEnvelope:
    def test_recognises_valid_envelope(self) -> None:
        assert is_encrypted_envelope({"__enc__": "v1", "data": "abc"}) is True

    def test_rejects_plaintext_dict(self) -> None:
        assert is_encrypted_envelope({"webhook_url": "x"}) is False

    def test_rejects_envelope_with_non_string_data(self) -> None:
        assert is_encrypted_envelope({"__enc__": "v1", "data": 42}) is False

    def test_rejects_non_dict(self) -> None:
        assert is_encrypted_envelope("not a dict") is False
        assert is_encrypted_envelope(None) is False


class TestEncryptCredentials:
    def test_produces_envelope_and_hides_plaintext(self) -> None:
        plaintext = {"webhook_url": "https://example.bitrix24.ru/rest/1/secret/"}
        envelope = encrypt_credentials(plaintext)

        assert is_encrypted_envelope(envelope)
        assert envelope["__enc__"] == ENCRYPTED_ENVELOPE_VERSION
        serialised = repr(envelope)
        assert "https://example.bitrix24.ru" not in serialised
        assert "secret" not in serialised

    def test_rejects_non_dict_payloads(self) -> None:
        with pytest.raises(TypeError):
            encrypt_credentials("not a dict")  # type: ignore[arg-type]

    def test_round_trip_returns_original_dict(self) -> None:
        plaintext = {
            "client_id": "abc.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret",
            "refresh_token": "1//refresh",
            "scopes": "https://www.googleapis.com/auth/spreadsheets.readonly",
        }
        assert decrypt_credentials(encrypt_credentials(plaintext)) == plaintext


class TestDecryptCredentials:
    def test_returns_empty_dict_for_none(self) -> None:
        assert decrypt_credentials(None) == {}

    def test_returns_copy_for_legacy_plaintext(self) -> None:
        legacy = {"webhook_url": "https://example.bitrix/rest"}
        decrypted = decrypt_credentials(legacy)

        assert decrypted == legacy
        # Mutating the result must not corrupt the underlying SQLAlchemy value.
        decrypted["webhook_url"] = "mutated"
        assert legacy["webhook_url"] == "https://example.bitrix/rest"

    def test_rejects_unknown_envelope_version(self) -> None:
        with pytest.raises(ValueError):
            decrypt_credentials({"__enc__": "v999", "data": "abc"})

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(TypeError):
            decrypt_credentials("not a dict")  # type: ignore[arg-type]
