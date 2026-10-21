"""Handoff stays selectable through exclusive managed-business guards."""

import json
from uuid import uuid4

import pytest

from schemas.managed_turn_v3 import ManagedTurnV3Request
from services.managed_turn_service import ManagedControlRequested
from services.managed_tools import ManagedExecutionError
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_chotacrm_adapter import crm_settings as crm_settings_fixture
from tests.test_pending_proposal_decision import pending, run_core
from tests.test_preparation_schema_correction import local_runtime, preparation


@pytest.fixture(scope="module")
def crm_settings():
    return crm_settings_fixture.__wrapped__()


def enable_handoff(runtime):
    value = runtime.request.model_dump(mode="json")
    value.pop("stream_partials", None)
    value.pop("max_partial_chars", None)
    value.update(contract_version=3, communication_facts=[], prior_operation_refs=[])
    runtime.request = ManagedTurnV3Request.model_validate(value)
    runtime.handoff_enabled = True
    runtime.request_fingerprint = "a" * 64
    runtime.original_dispatch_id = uuid4()
    controls = []

    async def latch(control, operation_refs):
        controls.append((control, operation_refs))

    runtime.control_latch = latch
    return controls


def handoff_call(call_id="handoff", arguments=None):
    raw_arguments = arguments if isinstance(arguments, str) else json.dumps(arguments or {
        "reason": "CUSTOMER_REQUEST", "note": "Please connect a person.",
    })
    call = build_tool_call(call_id, "platform_request_human_handoff", raw_arguments)
    call.type = "function"
    return call


def business_call(name, arguments, call_id="business"):
    call = build_tool_call(call_id, name, json.dumps(arguments))
    call.type = "function"
    return call


async def assert_no_business_effect(business, runtime, provider, controls, model):
    assert controls == []
    assert provider["posts"] == 0
    assert runtime.tool_dispatch_attempted is False
    assert runtime.collector.snapshot() == []
    assert await business.repository.selected_plan(business.request, business.grant) is None
    assert len(model.chat.completions.calls) == 1
    assert len(model.chat.completions._responses) == 1


@pytest.mark.asyncio
async def test_handoff_is_selectable_while_proposal_decision_is_pending(
        db_session, crm_settings, monkeypatch):
    business, runtime, provider, _proposal_id = await pending(
        db_session, crm_settings, monkeypatch, text="нужен человек")
    controls = enable_handoff(runtime)
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[handoff_call()]))

    with pytest.raises(ManagedControlRequested):
        await run_core(db_session, runtime, model)

    assert "tool_choice" not in model.chat.completions.calls[0]
    assert "platform_request_human_handoff" in {
        tool["function"]["name"] for tool in model.chat.completions.calls[0]["tools"]}
    assert len(controls) == 1
    assert controls[0][0].type == "HUMAN_HANDOFF"
    assert controls[0][0].request_id
    assert len(model.chat.completions.calls) == 1
    assert provider["posts"] == 0
    plan = await business.repository.selected_plan(business.request, business.grant)
    assert plan.proposal_state == "PREPARED" and plan.operations[0].status == "RESERVED"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["business_propose", "booking_create"])
async def test_handoff_is_selectable_during_one_shape_correction_before_any_effect(
        db_session, crm_settings, monkeypatch, tool_name):
    business, runtime, provider, args = await local_runtime(
        db_session, crm_settings, monkeypatch, generic=tool_name == "business_propose")
    controls = enable_handoff(runtime)
    actions = [{"capability": "booking.create", "arguments": args}]
    invalid = ({"actions": json.dumps(actions)} if tool_name == "business_propose"
               else {**args, "customer": None})
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        preparation(tool_name, invalid, "bad"),
        build_llm_response(tool_calls=[handoff_call("handoff-after-correction")]),
    )

    with pytest.raises(ManagedControlRequested):
        await run_core(db_session, runtime, model)

    assert len(model.chat.completions.calls) == 2
    assert all("tool_choice" not in call for call in model.chat.completions.calls)
    assert all("platform_request_human_handoff" in {
        tool["function"]["name"] for tool in call["tools"]}
        for call in model.chat.completions.calls)
    assert len(controls) == 1
    assert provider["posts"] == 0
    assert await business.repository.selected_plan(business.request, business.grant) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("control_first", [False, True])
async def test_mixed_business_and_handoff_batch_is_rejected_before_any_dispatch(
        db_session, crm_settings, monkeypatch, control_first):
    business, runtime, provider, args = await local_runtime(db_session, crm_settings, monkeypatch)
    controls = enable_handoff(runtime)
    model = ScriptedLLMClient()
    business_call_value = business_call("booking_create", args, "booking")
    control_call = handoff_call("handoff-mixed")
    calls = ([control_call, business_call_value] if control_first
             else [business_call_value, control_call])
    model.chat.completions.queue(
        build_llm_response(tool_calls=calls),
        build_llm_response(content="This continuation must not run."),
    )

    with pytest.raises(ManagedExecutionError) as failure:
        await run_core(db_session, runtime, model)

    assert failure.value.code == "INVALID_RESULT"
    await assert_no_business_effect(business, runtime, provider, controls, model)


@pytest.mark.asyncio
async def test_duplicate_handoff_batch_is_rejected_before_latch_or_dispatch(
        db_session, crm_settings, monkeypatch):
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    controls = enable_handoff(runtime)
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        build_llm_response(tool_calls=[handoff_call("handoff-one"), handoff_call("handoff-two")]),
        build_llm_response(content="This continuation must not run."),
    )

    with pytest.raises(ManagedExecutionError) as failure:
        await run_core(db_session, runtime, model)

    assert failure.value.code == "INVALID_RESULT"
    await assert_no_business_effect(business, runtime, provider, controls, model)


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    "not-json",
    "{}",
    json.dumps({"reason": "CUSTOMER_REQUEST", "request_id": str(uuid4())}),
    json.dumps({"reason": "CUSTOMER_REQUEST", "owner_id": "other-owner"}),
    json.dumps({"reason": "CUSTOMER_REQUEST", "agent_id": str(uuid4())}),
    json.dumps({"reason": "CUSTOMER_REQUEST", "conversation_id": str(uuid4())}),
    json.dumps({"reason": "CUSTOMER_REQUEST", "integration_id": str(uuid4())}),
    json.dumps({"reason": "CUSTOMER_REQUEST", "request_fingerprint": "a" * 64}),
])
async def test_malformed_or_trusted_handoff_arguments_are_rejected_without_effects(
        db_session, crm_settings, monkeypatch, arguments):
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    controls = enable_handoff(runtime)
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        build_llm_response(tool_calls=[handoff_call("handoff-invalid", arguments)]),
        build_llm_response(content="This continuation must not run."),
    )

    with pytest.raises(ManagedExecutionError) as failure:
        await run_core(db_session, runtime, model)

    assert failure.value.code == "INVALID_RESULT"
    await assert_no_business_effect(business, runtime, provider, controls, model)
