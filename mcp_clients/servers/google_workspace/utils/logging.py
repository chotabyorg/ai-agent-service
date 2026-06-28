"""Logging utilities for Google Workspace MCP."""

import logging
from typing import Any


SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "client_secret",
    "authorization",
    "token",
    "api_key",
}


def configure_logging(level: str) -> None:
    """Configure process logging without noisy formatting."""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def redact(value: Any) -> Any:
    """Redact secrets from nested dict/list values before logging."""

    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
