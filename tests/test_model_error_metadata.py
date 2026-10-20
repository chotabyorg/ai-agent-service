"""Canonical error metadata remains finite through the actual SDK HTTP/SSE path."""
from copy import deepcopy
from dataclasses import replace
import json
import logging
from uuid import uuid4

import httpx
from openai import APIError, AsyncOpenAI
import pytest

import core.managed_completion_diagnostics as diagnostics
from core.managed_security import managed_logging
from services.model_limit_observation import (
    _ERROR_KINDS, _PROVIDER_HINTS, model_limit_observation,
)
from services.model_retry import classify_model_failure, model_retry_delay
from tests.test_model_limit_observation import failure, SECRET
from tests.test_managed_completion import capture_production_logger  # noqa: F401


@pytest.mark.parametrize('key,allowed,field', [
    ('error_type', _ERROR_KINDS, 'error_kind'),
    ('provider_name', _PROVIDER_HINTS, 'provider_hint'),
])
def test_all_supported_metadata_values_pass_the_logging_allowlist(key, allowed, field):
    assert set(allowed.values()) | {'MISSING', 'INVALID', 'OTHER'} == diagnostics._LIMIT_FIELDS[field]
    for raw, expected in allowed.items():
        observed = model_limit_observation(failure(metadata={key: raw, 'raw': SECRET}))
        assert observed[field] == expected and SECRET not in repr(observed)


@pytest.mark.parametrize('value,expected', [
    (None, 'MISSING'), (SECRET, 'OTHER'), ('NextBit\n' + SECRET, 'OTHER'),
    ('', 'OTHER'), (True, 'INVALID'), (429, 'INVALID'), ([], 'INVALID'),
    ({'value': SECRET}, 'INVALID'),
])
def test_untrusted_metadata_is_never_echoed(value, expected):
    result = model_limit_observation(failure(metadata={'error_type': value, 'provider_name': value}))
    assert result['error_kind'] == result['provider_hint'] == expected
    assert SECRET not in repr(result)


def test_observation_does_not_rewrite_error_or_classification():
    error = failure(metadata={'error_type': 'provider_overloaded', 'provider_name': 'NextBit',
        'provider_code': SECRET, 'raw': {'message': SECRET}}, headers={'Retry-After': '60'})
    original = deepcopy(error.body)
    before = classify_model_failure(error)
    assert model_retry_delay(error) is None
    observed = model_limit_observation(error)
    assert observed['error_kind'] == 'PROVIDER_OVERLOADED'
    assert observed['provider_hint'] == 'NEXTBIT'
    assert error.body == original and classify_model_failure(error) == before
    assert before.category == 'RATE_LIMITED' and model_retry_delay(error) is None


@pytest.mark.parametrize('stream', [False, True])
async def test_sdk_preserves_canonical_error_metadata_in_http_and_sse(stream):
    payload = {'error': {'code': 429, 'message': SECRET, 'metadata': {
        'error_type': 'rate_limit_exceeded', 'provider_name': 'NextBit',
        'provider_code': SECRET, 'raw': SECRET}}}
    requests = []

    def respond(request):
        requests.append(request)
        if stream:
            data = 'data: ' + json.dumps(payload) + '\n\ndata: [DONE]\n\n'
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                content=data.encode())
        return httpx.Response(429, json=payload, headers={'Retry-After': '60'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        async with AsyncOpenAI(api_key='synthetic', base_url='https://synthetic.invalid/v1',
                http_client=http_client, max_retries=0) as sdk:
            with pytest.raises(APIError) as caught:
                result = await sdk.chat.completions.create(model='synthetic',
                    messages=[{'role': 'user', 'content': 'synthetic'}], stream=stream)
                if stream:
                    async for _ in result:
                        pytest.fail('The synthetic error must precede every content chunk')
    observed = model_limit_observation(caught.value)
    assert len(requests) == 1
    assert observed['error_kind'] == 'RATE_LIMITED'
    assert observed['provider_hint'] == 'NEXTBIT'
    assert observed['provider_code_state'] == 'PRESENT'
    assert observed['retry_after_state'] == ('MISSING' if stream else 'OVER_BUDGET')
    assert classify_model_failure(caught.value).category == 'RATE_LIMITED'
    assert SECRET not in repr(observed)


@pytest.mark.parametrize('field,value', [
    ('error_kind', 'PROVIDER_UNAVAILABLE'), ('provider_hint', 'NEXTBIT')])
def test_new_fields_are_logged_only_for_failure_records(caplog, field, value):
    base = diagnostics.CompletionDiagnostic(uuid4(), 1, 'MODEL_FAILURE', 'MISSING', 'NOT_INSPECTED')
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            diagnostics.emit_completion_diagnostic(replace(base, **{field: value}))
            diagnostics.emit_completion_diagnostic(replace(base, branch='FINAL', finish='STOP',
                visible_content='TEXT', **{field: value}))
    finally:
        managed_logging.reset(mark)
    rows = [r for r in caplog.records if getattr(r, 'event', None) == 'managed_completion_observed']
    assert len(rows) == 1 and rows[0].branch == 'MODEL_FAILURE'
    assert getattr(rows[0], field) == value
