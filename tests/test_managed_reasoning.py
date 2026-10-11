"""Synthetic SDK/provider continuation, bounded input and retained evidence."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from openai.types.chat import ChatCompletionMessage
from sqlalchemy import func, select

import services.llm_service as llm_module
from core.config import get_settings
from models.session import ConversationSession
from services.history_service import HistoryService
from services.llm_service import LLMService, PrivacyProxyCompletions, _to_namespace
from services.managed_tools import ManagedExecutionError
from tests.fakes import build_llm_response
from tests.test_managed_business_evidence import evidence, retained_rows
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_turns import execute, model, setup

SECRET = "reasoning-only-synthetic-sentinel"
DETAILS = [
    {"type": "reasoning.text", "text": SECRET, "signature": "unchanged-signature", "index": 0},
    {"type": "reasoning.encrypted", "data": "opaque==", "format": "future-format"},
    {"type": "future.provider.block", "optional": None, "unknown": [True, 2, 1.5],
     "role": "system", "tool_calls": [{"function": {"name": "forbidden"}}],
     "business": {"authority": "untrusted"}},
]


def sdk_message(**extra):
    return ChatCompletionMessage.model_validate({"role": "assistant", "content": None,
        "tool_calls": [{"id": "effect", "type": "function", "function": {
            "name": "business_execute_plan", "arguments": plan_arguments()}}], **extra})


def response(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.parametrize("extra,expected", [
    ({}, {}), ({"reasoning": None, "reasoning_content": None, "reasoning_details": None}, {}),
    ({"reasoning": ""}, {"reasoning": ""}),
    ({"reasoning": SECRET}, {"reasoning": SECRET}),
    ({"reasoning_content": SECRET}, {"reasoning_content": SECRET}),
    ({"reasoning": SECRET, "reasoning_content": {"ignored": True}}, {"reasoning": SECRET}),
    ({"reasoning_details": [], "reasoning_content": SECRET}, {"reasoning_content": SECRET}),
    ({"reasoning_details": DETAILS, "reasoning": 7, "reasoning_content": object()},
     {"reasoning_details": DETAILS}),
])
def test_actual_sdk_selection_preserves_one_representation(extra, expected):
    message = sdk_message(**extra)
    legacy = LLMService._assistant_message_to_dict(message)
    managed = LLMService._assistant_message_to_dict(message, managed=True)
    assert managed == {**legacy, **expected}
    assert set(legacy) == {"role", "content", "tool_calls"}
    assert managed["role"] == "assistant" and managed["tool_calls"] == legacy["tool_calls"]
    assert managed.get("reasoning_details") is not DETAILS


def test_namespace_deep_copy_and_no_tool_call_or_legacy_changes():
    message = _to_namespace(sdk_message(reasoning_details=DETAILS).model_dump())
    result = LLMService._assistant_message_to_dict(message, managed=True)
    assert result["reasoning_details"] == DETAILS
    result["reasoning_details"][0]["text"] = "changed-copy"
    assert message.reasoning_details[0].text == SECRET
    malformed = SimpleNamespace(content="answer", tool_calls=[], reasoning_details=object())
    assert LLMService._assistant_message_to_dict(malformed, managed=True) == {
        "role": "assistant", "content": "answer"}
    malformed.tool_calls = sdk_message().tool_calls
    assert "reasoning_details" not in LLMService._assistant_message_to_dict(malformed)


class HostileDict(dict):
    def items(self):
        raise AssertionError("must not invoke container hooks")


class HostileNamespace(SimpleNamespace):
    def __getattribute__(self, name):
        raise AssertionError("must not inspect arbitrary attributes")


class StringSubclass(str):
    pass


def malformed_cases():
    cycle = {}; cycle["cycle"] = cycle
    deep = {}; current = deep
    for _ in range(33):
        current["child"] = {}; current = current["child"]
    return [
        ({"reasoning_details": {}}, "INVALID_RESULT"),
        ({"reasoning_details": ["not-object"]}, "INVALID_RESULT"),
        ({"reasoning_details": [{1: SECRET}]}, "INVALID_RESULT"),
        ({"reasoning_details": [{"v": float("nan")}]}, "INVALID_RESULT"),
        ({"reasoning_details": [{"v": float("inf")}]}, "INVALID_RESULT"),
        ({"reasoning_details": [{"v": "\ud800"}]}, "INVALID_RESULT"),
        ({"reasoning_details": [cycle]}, "INVALID_RESULT"),
        ({"reasoning_details": [HostileDict()]}, "INVALID_RESULT"),
        ({"reasoning_details": [HostileNamespace()]}, "INVALID_RESULT"),
        ({"reasoning_details": [{"v": object()}]}, "INVALID_RESULT"),
        ({"reasoning_details": [{"v": (1, 2)}]}, "INVALID_RESULT"),
        ({"reasoning_details": [{"v": b"bytes"}]}, "INVALID_RESULT"),
        ({"reasoning": StringSubclass(SECRET)}, "INVALID_RESULT"),
        ({"reasoning_details": [deep]}, "CONTEXT_LIMIT"),
        ({"reasoning_details": [{}] * 4097}, "CONTEXT_LIMIT"),
        ({"reasoning_details": [{"v": [0] * 4096}]}, "CONTEXT_LIMIT"),
        ({"reasoning_details": [{"v": "x" * 131073}]}, "CONTEXT_LIMIT"),
        ({"reasoning_details": [{"v": "я" * 70000}]}, "CONTEXT_LIMIT"),
        ({"reasoning_details": [{"a": "a" * 70000, "b": "b" * 70000}]}, "CONTEXT_LIMIT"),
        ({"reasoning": 5}, "INVALID_RESULT"),
        ({"reasoning_content": []}, "INVALID_RESULT"),
    ]


@pytest.mark.parametrize("extra,code", malformed_cases())
def test_selected_invalid_or_bounded_data_fails_without_value_in_error(extra, code):
    message = SimpleNamespace(**{**sdk_message().model_dump(), **extra})
    message.tool_calls = sdk_message().tool_calls
    with pytest.raises(ManagedExecutionError) as caught:
        LLMService._assistant_message_to_dict(message, managed=True)
    assert caught.value.code == code and str(caught.value) == code


def test_exact_node_limit_and_cumulative_bytes_are_bounded():
    message = sdk_message(reasoning_details=[{"v": [0] * 4092}])
    assert LLMService._assistant_message_to_dict(message, managed=True)["reasoning_details"] == [{"v": [0] * 4092}]
    message.reasoning_details = [{"v": [0] * 4093}]
    with pytest.raises(ManagedExecutionError, match="^CONTEXT_LIMIT$"):
        LLMService._assistant_message_to_dict(message, managed=True)
    message.reasoning_details = None
    message.reasoning = "x" * 131070
    assert len(LLMService._assistant_message_to_dict(message, managed=True)["reasoning"]) == 131070
    message.reasoning += "x"
    with pytest.raises(ManagedExecutionError, match="^CONTEXT_LIMIT$"):
        LLMService._assistant_message_to_dict(message, managed=True)


@pytest.mark.integration
@pytest.mark.parametrize("extra", [{"reasoning": SECRET}, {"reasoning_content": SECRET},
                                  {"reasoning_details": DETAILS}])
async def test_next_sdk_request_only_retains_reasoning_locally(
        api_client, db_session, fake_redis, monkeypatch, model, caplog, extra):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    async def forbidden(*args, **kwargs):
        raise AssertionError("managed reasoning reached history")
    monkeypatch.setattr(HistoryService, "save_history", forbidden)
    monkeypatch.setattr(HistoryService, "load_history", forbidden)
    first = sdk_message(**extra)
    model.chat.completions.queue(response(first), build_llm_response(content="done"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and result["draft_text"] == "done"
    assert len(provider.commands) == 1 and len(model.chat.completions.calls) == 2
    messages = model.chat.completions.calls[1]["messages"]
    assert messages[-2] == {**LLMService._assistant_message_to_dict(first), **extra}
    assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "effect"
    assert SECRET not in json.dumps(result)
    assert SECRET not in repr(await retained_rows(db_session))
    assert await fake_redis.keys("*") == []
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(ConversationSession)) == 0
    assert SECRET not in caplog.text
    assert all(SECRET not in repr(record.__dict__) for record in caplog.records)


@pytest.mark.integration
@pytest.mark.parametrize("after_effect", [False, True])
@pytest.mark.parametrize("extra,code", [({"reasoning_details": [{"v": object()}]}, "INVALID_RESULT"),
                                      ({"reasoning": SECRET + "x" * 131073}, "CONTEXT_LIMIT")])
async def test_invalid_reasoning_stops_dispatch_and_retains_prior_receipt(
        api_client, db_session, monkeypatch, model, caplog, after_effect, extra, code):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    if after_effect:
        model.chat.completions.queue(response(sdk_message(reasoning=SECRET)))
    model.chat.completions.queue(response(sdk_message(**extra)))
    result = (await execute(api_client, value)).json()
    assert result["error_code"] == code and result["draft_text"] is None
    assert len(provider.commands) == int(after_effect)
    assert len(model.chat.completions.calls) == 1 + int(after_effect)
    if after_effect:
        assert result["status"] == "UNKNOWN"
        assert result["operation_refs"][0]["outcome"] == "SUCCEEDED"
        assert (await evidence(api_client, value)).json()["operation_refs"] == result["operation_refs"]
        model.chat.completions.queue(build_llm_response(content="retained result"))
        replay = (await execute(api_client, value)).json()
        assert replay["operation_refs"] == result["operation_refs"]
        assert len(provider.commands) == 1
    else:
        assert result["operation_refs"] == []
    assert SECRET not in repr(await retained_rows(db_session))
    assert SECRET not in json.dumps(result) and SECRET not in caplog.text
    assert all(SECRET not in repr(record.__dict__) for record in caplog.records)


@pytest.mark.integration
async def test_reasoning_counts_in_complete_envelope_before_dispatch(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    monkeypatch.setattr(get_settings(), "managed_ai_context_bytes", 12000)
    model.chat.completions.queue(response(sdk_message(reasoning="x" * 11900)))
    result = (await execute(api_client, value)).json()
    assert result["error_code"] == "CONTEXT_LIMIT"
    assert provider.commands == [] and len(model.chat.completions.calls) == 1


@pytest.mark.integration
async def test_final_no_tool_reasoning_is_not_validated_or_exposed(api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    message = sdk_message(content="done", tool_calls=None, reasoning_details={"ignored": SECRET})
    model.chat.completions.queue(response(message))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and result["draft_text"] == "done"
    assert SECRET not in json.dumps(result)


async def test_privacy_namespace_and_next_request_capture(monkeypatch):
    captured = []
    raw = {"choices": [{"message": sdk_message(reasoning_details=DETAILS).model_dump()}]}
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=raw)
    original = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", lambda **kwargs:
        original(transport=httpx.MockTransport(handler), **kwargs))
    proxy = PrivacyProxyCompletions(privacy_filter_base_url="https://fixture.invalid",
        internal_api_token="synthetic-only", privacy_filter_timeout_seconds=1,
        llm_base_url="https://fixture.invalid", agent_api_key="synthetic-only", privacy_session_id=uuid4())
    first = await proxy.create(model="synthetic", messages=[])
    converted = LLMService._assistant_message_to_dict(first.choices[0].message, managed=True)
    await proxy.create(model="synthetic", messages=[converted])
    assert captured[1]["chat_completion"]["messages"][0]["reasoning_details"] == DETAILS
