import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from core.config import get_settings
from services.credentials_cipher import (
    CredentialEnvelopeError,
    decrypt_credentials,
    encrypt_credentials,
    is_encrypted_envelope,
)
from services.encryption_service import EncryptionService


def _cipher() -> EncryptionService:
    return EncryptionService()


def test_envelope_round_trip_and_ordinary_payload_is_not_an_envelope() -> None:
    service = _cipher()
    payload = {
        "webhook_url": "https://example.invalid/rest/token/",
        "nested": {"enabled": True},
        "chota:mcp-credentials": ["user data"],
    }

    envelope = encrypt_credentials(payload, encryption_service=service)

    assert is_encrypted_envelope(envelope)
    assert payload["webhook_url"] not in str(envelope)
    assert decrypt_credentials(envelope, encryption_service=service) == payload
    assert decrypt_credentials(payload, encryption_service=service) == payload
    assert not is_encrypted_envelope(payload)


@pytest.mark.parametrize(
    "envelope",
    [
        ["chota:mcp-credentials"],
        ["chota:mcp-credentials", 99, "ciphertext"],
        ["chota:mcp-credentials", True, "ciphertext"],
        ["chota:mcp-credentials", 1, ""],
        ["chota:mcp-credentials", 1, "not-a-valid-fernet-token"],
    ],
)
def test_malformed_or_unknown_envelopes_fail_closed(envelope) -> None:
    with pytest.raises(CredentialEnvelopeError):
        decrypt_credentials(envelope, encryption_service=_cipher())


def test_wrong_key_fails_closed(monkeypatch) -> None:
    envelope = encrypt_credentials({"token": "test-only-value"}, encryption_service=_cipher())
    monkeypatch.setattr(
        get_settings(),
        "encryption_key",
        SecretStr(Fernet.generate_key().decode()),
    )

    with pytest.raises(CredentialEnvelopeError):
        decrypt_credentials(envelope, encryption_service=_cipher())
