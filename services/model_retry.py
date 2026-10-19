"""Bounded recovery of a failed MODEL request, without replaying tools."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError


FAILURE_CATEGORIES = frozenset({'RATE_LIMITED', 'SERVER_ERROR', 'CLIENT_ERROR',
                              'TRANSPORT', 'TIMEOUT', 'UNKNOWN'})


@dataclass(frozen=True)
class ModelFailure:
    """Closed provider classification; never retains bodies or headers."""

    category: str
    http_status: int | None = None


def _error_envelope(error: APIError) -> dict:
    """Inspect only the provider's structured envelope, never exception text."""
    body = vars(error).get('body')
    if type(body) is not dict:
        return {}
    nested = body.get('error')
    return nested if type(nested) is dict else body


def _in_flight_budget(error: APIError) -> bool:
    """Recognize the documented temporary OpenRouter reservation limit."""
    metadata = _error_envelope(error).get('metadata')
    return type(metadata) is dict and metadata.get('limit_source') == 'openrouter_in_flight_budget'


def model_payment_required(error: APIError) -> bool:
    """Distinguish structured payment refusal from a temporary in-flight limit."""
    code = _error_envelope(error).get('code')
    payment = vars(error).get('status_code') == 402 or (type(code) is int and code == 402)
    metadata = _error_envelope(error).get('metadata')
    payment = payment or (type(metadata) is dict and metadata.get('error_type') == 'payment_required')
    return payment and not _in_flight_budget(error)


def classify_model_failure(error: APIError) -> ModelFailure:
    """Recognize HTTP errors and documented SDK stream error codes."""
    status = vars(error).get('status_code')
    status = status if type(status) is int and 100 <= status <= 599 else None
    if model_payment_required(error):
        return ModelFailure('CLIENT_ERROR', status)
    if _in_flight_budget(error) and (status == 402 or _error_envelope(error).get('code') == 402):
        return ModelFailure('RATE_LIMITED', status)
    code = status
    if code is None:
        code = _error_envelope(error).get('code')
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
        # The SDK also wraps response/stream/cleanup errors in this class.
        # Only a proven connection setup failure is a transport retry signal.
        return ModelFailure('TRANSPORT' if isinstance(error.__cause__, httpx.ConnectError) else 'UNKNOWN', status)
    return ModelFailure('UNKNOWN', status)


def model_retry_delay(error: APIError) -> float | None:
    """Honor Retry-After only inside a two-second interactive wait budget."""
    failure = classify_model_failure(error)
    if failure.category not in {'RATE_LIMITED', 'SERVER_ERROR', 'TRANSPORT'}:
        return None
    if _in_flight_budget(error) and (not isinstance(error, APIStatusError)
                                   or 'retry-after' not in error.response.headers):
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
