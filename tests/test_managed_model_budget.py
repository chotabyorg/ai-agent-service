"""Opt-in OpenRouter control changes only the named managed model's wire budget."""
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from openai import AsyncOpenAI

import services.llm_service as llm_module
from core.config import Settings, get_settings
from models.agent import AgentTemplate
from services.llm_service import LLMService
from services.managed_tools import ManagedExecutionError
from tests.test_managed_turns import execute, setup

TARGET = 'xiaomi/mimo-v2.5'


def test_named_reasoning_control_is_counted_in_managed_envelope():
    service = LLMService()
    service.settings = Settings(_env_file=None,
        managed_openrouter_disable_reasoning_models=[TARGET])
    envelope = service._managed_envelope(SimpleNamespace(model_name=TARGET, temperature=0.4), [], [])
    assert envelope['reasoning'] == {'enabled': False}
    assert envelope['max_tokens'] == 1024


@pytest.mark.parametrize('overrides', [
    {'privacy_filter_enabled': True}, {'llm_base_url': 'https://another.invalid/v1'},
])
def test_unsupported_transport_is_rejected_without_settings_or_credentials(overrides):
    sentinel = 'synthetic-secret-never-render'
    with pytest.raises(RuntimeError) as failure:
        Settings(_env_file=None, llm_api_key=sentinel,
            managed_openrouter_disable_reasoning_models=[TARGET], **overrides)
    assert str(failure.value) == 'Managed reasoning control requires direct OpenRouter transport'
    assert sentinel not in str(failure.value)
    assert Settings(_env_file=None, **overrides).managed_openrouter_disable_reasoning_models == []


def test_text_reasoning_control_is_channel_scoped_and_opt_in():
    service = LLMService()
    agent = SimpleNamespace(model_name="openai/gpt-6-luna", temperature=0.4)
    service.settings = Settings(_env_file=None, managed_text_disable_reasoning=True)
    assert service._managed_model_extra_body(
        agent, request=SimpleNamespace(channel="TELEGRAM")) == {"reasoning": {"enabled": False}}
    assert service._managed_model_extra_body(
        agent, request=SimpleNamespace(channel="VOICE")) == {}
    service.settings = Settings(_env_file=None)
    assert service._managed_model_extra_body(
        agent, request=SimpleNamespace(channel="TELEGRAM")) == {}


def test_text_reasoning_control_requires_direct_openrouter():
    with pytest.raises(RuntimeError) as failure:
        Settings(_env_file=None, managed_text_disable_reasoning=True,
                 llm_base_url="https://another.invalid/v1")
    assert str(failure.value) == "Managed reasoning control requires direct OpenRouter transport"


def test_text_parallel_tool_calls_is_channel_scoped_and_opt_in():
    service = LLMService()
    agent = SimpleNamespace(model_name="openai/gpt-6-luna", temperature=0.4)
    service.settings = Settings(_env_file=None, managed_text_parallel_tool_calls=True)
    assert service._managed_parallel_tool_calls(SimpleNamespace(channel="TELEGRAM")) is True
    assert service._managed_parallel_tool_calls(SimpleNamespace(channel="VOICE")) is False
    assert service._managed_parallel_tool_calls(None) is False
    envelope = service._managed_envelope(agent, [], [], request=SimpleNamespace(channel="TELEGRAM"))
    assert envelope["parallel_tool_calls"] is True
    service.settings = Settings(_env_file=None)
    assert service._managed_parallel_tool_calls(SimpleNamespace(channel="TELEGRAM")) is False


def test_reasoning_control_counts_against_context_limit():
    service = LLMService()
    service.settings = Settings(_env_file=None, managed_ai_context_bytes=1024)
    agent = SimpleNamespace(model_name=TARGET, temperature=0.4)
    messages = [{'role': 'user', 'content': ''}]
    base_size = len(json.dumps(service._managed_envelope(agent, messages, [])).encode())
    messages[0]['content'] = 'x' * (1024 - base_size)
    service._check_managed_envelope(agent, messages, [])
    service.settings.managed_openrouter_disable_reasoning_models = [TARGET]
    with pytest.raises(ManagedExecutionError, match='CONTEXT_LIMIT'):
        service._check_managed_envelope(agent, messages, [])


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled_models,expected', [
    ([], None), ([TARGET], {'enabled': False}), ([TARGET + ':free'], None), (['*'], None),
])
async def test_pinned_sdk_wire_is_managed_only_and_exact_model(
        api_client, db_session, monkeypatch, enabled_models, expected):
    agent, value = await setup(api_client, db_session, monkeypatch)
    async with db_session() as db:
        row = await db.get(AgentTemplate, UUID(agent['id']))
        row.model_name = TARGET
        row.temperature = 0.4
        await db.commit()
    monkeypatch.setattr(get_settings(), 'managed_openrouter_disable_reasoning_models', enabled_models)
    captured = []
    constructors = []

    def create_client(**kwargs):
        constructors.append({k: v for k, v in kwargs.items() if k in ('max_retries', 'timeout')})
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
                'created': 0, 'model': TARGET, 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': 'safe draft'}}]})
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(llm_module, 'AsyncOpenAI', create_client)
    managed = (await execute(api_client, value)).json()
    assert managed['status'] == 'DRAFT_READY'
    legacy = await api_client.post('/v1/chat/completions', json={
        'agent_id': agent['id'], 'external_client_id': 'legacy-budget', 'channel': 'telegram', 'message': 'Hello'})
    assert legacy.status_code == 200
    assert len(captured) == 2
    assert captured[0].get('reasoning') == expected
    if expected is None:
        assert 'reasoning' not in captured[0]
    assert 'extra_body' not in captured[0]  # SDK flattens it into the real JSON body.
    assert 'reasoning' not in captured[1]
    for body in captured:
        assert body['model'] == TARGET and body['temperature'] == 0.4
        assert body['max_tokens'] == 1024
        assert 'provider' not in body  # No routing constraint/fallback mutation.
    assert constructors[0] == {'max_retries': 0, 'timeout': 90.0}


@pytest.mark.asyncio
async def test_ignored_control_and_empty_truncated_completion_still_fail_without_retry(
        api_client, db_session, monkeypatch):
    agent, value = await setup(api_client, db_session, monkeypatch)
    monkeypatch.setattr(get_settings(), 'managed_openrouter_disable_reasoning_models', [agent['model_name']])
    captured = []
    def create_client(**kwargs):
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
                'created': 0, 'model': agent['model_name'], 'choices': [{'index': 0, 'finish_reason': 'length',
                'message': {'role': 'assistant', 'content': None}}], 'usage': {
                    'prompt_tokens': 100, 'completion_tokens': 1024, 'total_tokens': 1124,
                    'completion_tokens_details': {'reasoning_tokens': 1024}}})
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(llm_module, 'AsyncOpenAI', create_client)
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED' and result['error_code'] == 'INVALID_RESULT'
    assert result['draft_text'] is None and result['operation_refs'] == []
    assert len(captured) == 1 and captured[0]['reasoning'] == {'enabled': False}
