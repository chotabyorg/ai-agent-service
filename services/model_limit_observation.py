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
_ERROR_KINDS = {
    'rate_limit_exceeded': 'RATE_LIMITED',
    'provider_overloaded': 'PROVIDER_OVERLOADED',
    'provider_unavailable': 'PROVIDER_UNAVAILABLE',
    'context_length_exceeded': 'CONTEXT_LENGTH_EXCEEDED',
    'max_tokens_exceeded': 'MAX_TOKENS_EXCEEDED',
    'token_limit_exceeded': 'TOKEN_LIMIT_EXCEEDED',
    'string_too_long': 'STRING_TOO_LONG',
    'authentication': 'AUTHENTICATION',
    'permission_denied': 'PERMISSION_DENIED',
    'payment_required': 'PAYMENT_REQUIRED',
    'invalid_request': 'INVALID_REQUEST',
    'invalid_prompt': 'INVALID_PROMPT',
    'not_found': 'NOT_FOUND',
    'precondition_failed': 'PRECONDITION_FAILED',
    'payload_too_large': 'PAYLOAD_TOO_LARGE',
    'unprocessable': 'UNPROCESSABLE',
    'content_policy_violation': 'CONTENT_POLICY_VIOLATION',
    'refusal': 'REFUSAL',
    'server': 'SERVER',
    'timeout': 'TIMEOUT',
    'unmapped': 'UNMAPPED',
}
_PROVIDER_HINTS = {
    'Makora': 'MAKORA',
    'NextBit': 'NEXTBIT',
    'Cloudflare': 'CLOUDFLARE',
    'OpenAI': 'OPENAI',
}
_HEADERS = ('x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset')


def _finite_metadata_value(metadata, key, allowed):
    value = metadata.get(key)
    if value is None:
        return 'MISSING'
    if type(value) is not str:
        return 'INVALID'
    return allowed.get(value, 'OTHER')


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
        retry_after_state=_retry_after_state(headers),
        error_kind=_finite_metadata_value(metadata, 'error_type', _ERROR_KINDS),
        # A provider hint does not identify every attempt in a fallback chain.
        provider_hint=_finite_metadata_value(metadata, 'provider_name', _PROVIDER_HINTS))
