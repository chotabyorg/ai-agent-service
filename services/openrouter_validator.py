"""Validate OpenRouter API keys via the /auth/key endpoint."""

from enum import Enum

import httpx


class KeyValidationResult(Enum):
    VALID = "valid"
    INVALID = "invalid"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


async def validate_openrouter_key(api_key: str) -> KeyValidationResult:
    """Check a key against GET /api/v1/auth/key on OpenRouter."""
    if not api_key or len(api_key) < 20:
        return KeyValidationResult.INVALID

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code == 200:
                return KeyValidationResult.VALID
            if response.status_code == 401:
                return KeyValidationResult.INVALID
            if response.status_code == 402:
                return KeyValidationResult.INSUFFICIENT_FUNDS
            if response.status_code == 429:
                return KeyValidationResult.RATE_LIMITED
            return KeyValidationResult.ERROR
    except httpx.HTTPError:
        return KeyValidationResult.ERROR
