"""Fernet-based encryption for sensitive strings (API keys)."""

import base64

from cryptography.fernet import Fernet

from core.config import get_settings


class EncryptionService:
    """Encrypts and decrypts strings using Fernet symmetric encryption."""

    def __init__(self) -> None:
        key = get_settings().encryption_key.get_secret_value()
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string, return base64-encoded Fernet token."""
        token = self._fernet.encrypt(plaintext.encode())
        return base64.urlsafe_b64encode(token).decode()

    def decrypt(self, encrypted: str) -> str:
        """Decrypt a base64-encoded Fernet token back to string."""
        token = base64.urlsafe_b64decode(encrypted.encode())
        return self._fernet.decrypt(token).decode()


def mask_api_key(key: str) -> str:
    """Mask an API key for display: sk-or-v1-abc123xyz -> sk-or-v1-...***xyz"""
    if len(key) <= 10:
        return "***"
    return key[:8] + "...***" + key[-3:]
