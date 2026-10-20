"""Finite error evidence, never an inference of the responsible limiter."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math

import httpx
from openai import APIError

from services.model_retry import _error_envelope


_SOURCES = {
    'openrouter_in_flight_budget': 'OPENROUTER_IN_FLIGHT_BUDGET',
    'openrouter_credits': 'OPENROUTER_CREDITS',
    'openrouter_key_limit': 'OPENROUTER_KEY_LIMIT',
    'openrouter_guardrail_budget': 'OPENROUTER_GUARDRAIL_BUDGET',
    'openrouter_workspace_budget': 'OPENROUTER_WORKSPACE_BUDGET',
}
_HEADERS = ('x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset')


def _retry_after_state(headers):
    raw = headers.get('retry-after')
    if raw is None:
        return 'MISSING'
    if len(raw) > 100:
        return 'INVALID'
    try:
        required = float(raw)
    except ValueError:
        try:
            at = parsedate_to_datetime(raw)
            if at.tzinfo is None:
                return 'INVALID'
            required = (at - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 'INVALID'
    if not math.isfinite(required):
        return 'INVALID'
    return 'OVER_BUDGET' if required > 2 else 'WITHIN_BUDGET'


def model_limit_observation(error):
    """Read structured evidence only; do not affect routing, retries or payments."""
    if not isinstance(error, APIError):
        return {}
    metadata = _error_envelope(error).get('metadata')
    metadata = metadata if type(metadata) is dict else {}
    source = metadata.get('limit_source')
    source = ('MISSING' if source is None else
              _SOURCES.get(source, 'OTHER') if type(source) is str else 'OTHER')
    code = metadata.get('provider_code')
    code_state = ('MISSING' if code is None else 'PRESENT'
                  if type(code) in (int, str) and code != '' else 'INVALID')
    response = vars(error).get('response')
    headers = response.headers if type(response) is httpx.Response else httpx.Headers()
    count = sum(name in headers for name in _HEADERS)
    return dict(limit_source=source, provider_code_state=code_state,
        rate_limit_headers='COMPLETE' if count == len(_HEADERS) else 'PARTIAL' if count else 'MISSING',
        retry_after_state=_retry_after_state(headers))
