import pytest

from services.encryption_service import EncryptionService, mask_api_key


class TestEncryptionService:
    def test_encrypt_decrypt_roundtrip(self) -> None:
        service = EncryptionService()
        plaintext = "sk-or-v1-abc123xyz"
        encrypted = service.encrypt(plaintext)
        assert encrypted != plaintext
        assert service.decrypt(encrypted) == plaintext

    def test_decrypt_invalid_data_raises(self) -> None:
        service = EncryptionService()
        with pytest.raises(Exception):
            service.decrypt("not-a-valid-fernet-token")

    def test_encrypt_produces_different_tokens_for_same_input(self) -> None:
        service = EncryptionService()
        a = service.encrypt("same-key")
        b = service.encrypt("same-key")
        # Fernet tokens include a timestamp, so they differ
        assert a != b
        assert service.decrypt(a) == service.decrypt(b)


class TestMaskApiKey:
    def test_masks_long_key(self) -> None:
        assert mask_api_key("sk-or-v1-abc123xyz") == "sk-or-v1...***xyz"

    def test_masks_short_key(self) -> None:
        assert mask_api_key("short") == "***"

    def test_masks_exactly_10_chars(self) -> None:
        assert mask_api_key("1234567890") == "***"

    def test_masks_11_chars(self) -> None:
        assert mask_api_key("12345678901") == "12345678...***901"
