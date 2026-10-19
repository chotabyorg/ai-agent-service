"""Bounded recovery of a failed MODEL request, without replaying tools."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, RateLimitError


FAILURE_CATEGORIES = frozenset({'RATE_LIMITED', 'SERVER_ERROR', 'CLIENT_ERROR',
                              'TRANSPORT', 'TIMEOUT', 'UNKNOWN'})


@dataclass(frozen=True)
class ModelFailure:
    """Closed provider classification; never retains bodies or headers."""

    category: str
    http_status: int | None = None


def classify_model_failure(error: APIError) -> ModelFailure:
    """Recognize HTTP errors and documented SDK stream error codes."""
    status = vars(error).get('status_code')
    status = status if type(status) is int and 100 <= status <= 599 else None
    # Preserve the existing SDK compatibility path for a payment refusal
    # wrapped as RateLimitError; it must never consume a recovery attempt.
    if isinstance(error, RateLimitError) and any(x in str(error).lower() for x in ('402','insufficient','credits')):
        return ModelFailure('CLIENT_ERROR', status)
    code = status
    if code is None:
        body = vars(error).get('body')
        if type(body) is dict:
            nested = body.get('error')
            code = (nested if type(nested) is dict else body).get('code')
    if type(code) is int:
        if code == 429:
            return ModelFailure('RATE_LIMITED', status)
        if code in {500, 502, 503, 504}:
            return ModelFailure('SERVER_ERROR', status)
        if 400 <= code <= 499:
            return ModelFailure('CLIENT_ERROR', status)
    elif type(code) is str and code in {'server_error', 'rate_limit_exceeded'}:
        return ModelFailure('SERVER_ERROR' if code == 'server_error' else 'RATE_LIMITED', status)
    if isinstance(error, APITimeoutError):
        return ModelFailure('TIMEOUT', status)
    if isinstance(error, APIConnectionError):
        return ModelFailure('TRANSPORT', status)
    return ModelFailure('UNKNOWN', status)


def model_retry_delay(error: APIError) -> float | None:
    """Honor Retry-After only inside a two-second interactive wait budget."""
    failure = classify_model_failure(error)
    if failure.category not in {'RATE_LIMITED', 'SERVER_ERROR', 'TRANSPORT'}:
        return None
    delay = random.uniform(.5, 1.) if failure.category == 'RATE_LIMITED' else random.uniform(.15, .35)
    if isinstance(error, APIStatusError):
        raw = error.response.headers.get('retry-after')
        if raw is not None:
            if len(raw) > 100:
                return None
            try:
                required = float(raw)
            except ValueError:
                try:
                    at = parsedate_to_datetime(raw)
                    if at.tzinfo is None:
                        return None
                    required = (at - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    return None
            if not math.isfinite(required) or required > 2:
                return None
            delay = max(delay, required)
    return delay
