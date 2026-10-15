"""The actual SDK request must preserve optional-field omission semantics."""
import json
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI
import pytest
from pydantic import ValidationError

from business_capabilities.registry import CAPABILITIES, DEFAULTS, normalize_arguments
from services.business_runtime import BusinessRuntime


def business_tools(adapter):
    runtime = BusinessRuntime()
    runtime.effect_eligible = True  # SDK serialization projection after positive authority
    runtime.recovery_mode = False
    runtime._stale_split_plan_id = None
    runtime._stale_split_change_committed = False
    runtime.binding = SimpleNamespace(enabled_capabilities=DEFAULTS)
    runtime.registration = SimpleNamespace(adapter_id=adapter, confirmation_mode='SEMANTIC_V1',
        capabilities=[SimpleNamespace(name=c.id) for c in CAPABILITIES])
    return runtime.openai_tools()


@pytest.mark.parametrize('adapter', ['chotacrm', 'synthetic'])
async def test_sdk_serializes_explicit_non_strict_business_tools(adapter):
    tools = business_tools(adapter)
    requests = []

    def receive(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload['tools'] == tools
        assert all(tool['function']['strict'] is False for tool in payload['tools'])
        return httpx.Response(200, json={
            'id': 'synthetic', 'object': 'chat.completion', 'created': 1, 'model': 'synthetic',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': 'synthetic'}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as http:
        async with AsyncOpenAI(api_key='synthetic-not-a-credential',
                               base_url='https://synthetic.invalid/v1',
                               http_client=http, max_retries=0) as client:
            await client.chat.completions.create(model='synthetic',
                messages=[{'role': 'user', 'content': 'synthetic'}], tools=tools)
    assert len(requests) == 1
    if adapter == 'chotacrm':
        schema = next(t['function']['parameters'] for t in tools
                      if t['function']['name'] == 'catalog_search')
        assert 'cursor' not in schema.get('required', [])
        assert schema['additionalProperties'] is False
        assert schema['properties']['cursor']['minLength'] == 1


def test_explicit_wire_mode_does_not_accept_blank_or_null_runtime_cursor():
    assert 'cursor' not in normalize_arguments('catalog.search', {'query': 'маникюр'})
    for cursor in (' ', '', None, 0):
        with pytest.raises(ValidationError):
            normalize_arguments('catalog.search', {'query': 'маникюр', 'cursor': cursor})


def test_no_binding_still_exposes_no_tools():
    runtime = BusinessRuntime()
    runtime.registration = None
    assert runtime.openai_tools() == []
