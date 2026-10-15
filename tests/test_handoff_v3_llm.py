"""Handoff stays selectable through exclusive managed-business guards."""

import json
from uuid import uuid4

import pytest

from schemas.managed_turn_v3 import ManagedTurnV3Request
from services.managed_turn_service import ManagedControlRequested
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_chotacrm_adapter import crm_settings as crm_settings_fixture
from tests.test_pending_proposal_decision import pending, run_core
from tests.test_preparation_schema_correction import local_runtime, preparation


@pytest.fixture(scope="module")
def crm_settings():
    return crm_settings_fixture.__wrapped__()


def enable_handoff(runtime):
    value = runtime.request.model_dump(mode="json")
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


def handoff_call(call_id="handoff"):
    call = build_tool_call(call_id, "platform_request_human_handoff", json.dumps({
        "reason": "CUSTOMER_REQUEST", "note": "Please connect a person.",
    }))
    call.type = "function"
    return call


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
    assert len(controls) == 1
    assert controls[0][0].type == "HUMAN_HANDOFF"
    assert controls[0][0].request_id
    assert provider["posts"] == 0
    plan = await business.repository.selected_plan(business.request, business.grant)
    assert plan.proposal_state == "PREPARED" and plan.operations[0].status == "RESERVED"


@pytest.mark.asyncio
async def test_handoff_is_selectable_during_one_shape_correction_before_any_effect(
        db_session, crm_settings, monkeypatch):
    business, runtime, provider, args = await local_runtime(db_session, crm_settings, monkeypatch)
    controls = enable_handoff(runtime)
    actions = [{"capability": "booking.create", "arguments": args}]
    invalid = {"actions": json.dumps(actions)}
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        preparation("business_propose", invalid, "bad"),
        build_llm_response(tool_calls=[handoff_call("handoff-after-correction")]),
    )

    with pytest.raises(ManagedControlRequested):
        await run_core(db_session, runtime, model)

    assert len(model.chat.completions.calls) == 2
    assert all("tool_choice" not in call for call in model.chat.completions.calls)
    assert len(controls) == 1
    assert provider["posts"] == 0
    assert await business.repository.selected_plan(business.request, business.grant) is None
