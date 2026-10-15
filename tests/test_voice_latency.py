"""Voice profile wire isolation, budget accounting and tool authority regressions."""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from openai import AsyncOpenAI

import services.llm_service as llm_module
from core.config import Settings, get_settings
from core.voice_latency import VoiceLatencyProfile
from schemas.managed_turn import ManagedTurnRequest
from services.llm_service import LLMService
from services.managed_tools import ManagedExecutionError
from tests.fakes import build_llm_response, build_tool_call
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_turns import configure, execute, model, payload, setup


def profile(value, model):
    return VoiceLatencyProfile(**{k: value[k] for k in ("owner_id", "integration_id", "agent_id")}, model=model)


@pytest.mark.parametrize("channel", ["TELEGRAM", "WEBSITE_WIDGET", "INSTAGRAM", "WHATSAPP"])
def test_text_channels_cannot_match_even_with_same_identity(channel):
    value = payload(channel="VOICE")
    selected = profile(value, "synthetic/model")
    value["channel"] = channel
    assert not selected.matches(ManagedTurnRequest.model_validate(value), "synthetic/model")


@pytest.mark.parametrize("field", ["owner_id", "integration_id", "agent_id", "model"])
def test_every_scope_field_must_match(field):
    value = payload(channel="VOICE")
    selected = profile(value, "synthetic/model")
    model = "synthetic/model"
    if field == "model":
        model += "-other"
    else:
        value[field] = str(uuid4())
    assert not selected.matches(ManagedTurnRequest.model_validate(value), model)
    assert not selected.matches(None, model)


@pytest.mark.parametrize("overrides", [
    {"privacy_filter_enabled": True}, {"llm_base_url": "https://another.invalid/v1"},
])
def test_profile_rejects_incompatible_transport(overrides):
    with pytest.raises(RuntimeError, match="requires direct OpenRouter transport"):
        Settings(_env_file=None, managed_voice_latency_profiles=[profile(payload(), "synthetic/model")], **overrides)


def test_profile_fields_are_counted_before_provider_dispatch():
    value = payload(channel="VOICE")
    request = ManagedTurnRequest.model_validate(value)
    service = LLMService()
    service.settings = Settings(_env_file=None, managed_ai_context_bytes=1024)
    agent = SimpleNamespace(model_name="synthetic/model", temperature=0.4)
    messages = [{"role": "user", "content": ""}]
    size = len(json.dumps(service._managed_envelope(agent, messages, [])).encode())
    messages[0]["content"] = "x" * (1024 - size)
    service._check_managed_envelope(agent, messages, [], request=request)
    service.settings.managed_voice_latency_profiles = [profile(value, agent.model_name)]
    with pytest.raises(ManagedExecutionError, match="CONTEXT_LIMIT"):
        service._check_managed_envelope(agent, messages, [], request=request)


@pytest.mark.asyncio
async def test_sdk_wire_voice_only_then_managed_text_and_legacy_unchanged(api_client, db_session, monkeypatch):
    agent, value = await setup(api_client, db_session, monkeypatch, channel="VOICE")
    monkeypatch.setattr(get_settings(), "managed_voice_latency_profiles", [profile(value, agent['model_name'])])
    captured = []
    def create_client(**kwargs):
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion",
                "created": 0, "model": agent['model_name'], "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "safe draft"}}]})
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(llm_module, "AsyncOpenAI", create_client)
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    for channel in ("TELEGRAM", "WEBSITE_WIDGET"):
        value.update(channel=channel, turn_id=str(uuid4()), conversation_id=str(uuid4()))
        configure(monkeypatch, value, agent['user_id'])
        assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    # A public request calling itself voice does not acquire the managed profile.
    legacy = await api_client.post('/v1/chat/completions', json={
        'agent_id': agent['id'], 'external_client_id': 'legacy-voice', 'channel': 'voice', 'message': 'Hello'})
    assert legacy.status_code == 200
    assert len(captured) == 4
    assert captured[0]['reasoning'] == {'effort': 'none'}
    assert captured[0]['provider'] == {'sort': 'latency'}
    for body in captured[1:]:
        assert 'reasoning' not in body and 'provider' not in body
    for body in captured:
        assert body['model'] == agent['model_name'] and body['temperature'] == 0.4
        assert body['max_tokens'] == 1024 and body['parallel_tool_calls'] is False


@pytest.mark.asyncio
async def test_voice_profile_cannot_bypass_booking_confirmation(api_client, db_session, monkeypatch, model):
    agent, value = await setup(api_client, db_session, monkeypatch, channel="VOICE")
    monkeypatch.setattr(get_settings(), "managed_voice_latency_profiles", [profile(value, agent['model_name'])])
    authority = await install_business(monkeypatch, value, db_session, confirmation=True)
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call(
        tool_id="voice-prepare", name="business_execute_plan", arguments=plan_arguments())]))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY'
    assert authority.commands == []
    assert result['operation_refs'] == []
    assert model.chat.completions.calls[0]['extra_body']['reasoning'] == {'effort': 'none'}


@pytest.mark.asyncio
async def test_failed_profile_request_never_retries_with_text_defaults(api_client, db_session, monkeypatch):
    agent, value = await setup(api_client, db_session, monkeypatch, channel="VOICE")
    monkeypatch.setattr(get_settings(), "managed_voice_latency_profiles", [profile(value, agent['model_name'])])
    calls = []
    def create_client(**kwargs):
        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(404, json={'error': {'message': 'synthetic unsupported endpoint'}})
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(llm_module, "AsyncOpenAI", create_client)
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED' and result['draft_text'] is None
    assert len(calls) == 1
