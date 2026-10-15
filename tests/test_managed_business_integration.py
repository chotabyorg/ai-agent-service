"""Managed policy and genuine PostgreSQL evidence through the shared core."""

from tests.business_effect_helpers import claim_dispatch_with_test_permit, open_business_runtime
import asyncio
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

import main
import services.llm_service as llm_module
from core.config import get_settings
from models.agent import AgentCredentials, AgentTemplate
from models.business import BusinessOperation, BusinessPlan, BusinessScope
from schemas.managed_turn import ManagedTurnRequest
from services.agent_service import AgentService
from services.business_runtime import BusinessRuntime
from tests.fakes import build_llm_response, build_tool_call
from tests.test_chat_flow import store_credentials
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_turns import execute, model, setup, tools


# Frozen discovery inventory: all 26 Bitrix and all 16 Google write names.
RAW_WRITES = (
    "add_contact", "update_contact_communication", "add_lead", "update_lead_status",
    "add_company", "add_company_with_requisites", "add_supplier", "add_deal_with_company",
    "update_deal_stage", "set_deal_products", "sync_deal_payment_date", "create_smart_invoice",
    "add_calendar_event", "add_todo_activity", "move_activity", "send_contact_email",
    "add_contact_calendar_event", "add_task", "send_chat_notification", "create_crm_document",
    "add_smart_process_comment", "add_smart_process_userfield", "create_smart_process_funnel",
    "start_bizproc", "upload_file_to_disk", "update_crm_element",
    "google_sheets_append_row", "google_sheets_update_range", "google_sheets_create_spreadsheet",
    "google_sheets_create_tab", "google_sheets_upsert_lead", "google_sheets_update_lead_status",
    "google_calendar_create_event", "google_calendar_update_event", "google_calendar_delete_event",
    "google_calendar_create_followup", "gmail_create_draft", "gmail_send_draft", "gmail_reply_to_thread",
    "google_drive_create_folder", "google_drive_create_text_file", "google_contacts_upsert_contact",
)


@pytest.mark.integration
@pytest.mark.parametrize("name", RAW_WRITES + ("get_contact", "dynamic_read", "business_execute_plan"))
async def test_all_raw_names_are_quarantined_before_credentials_and_parse(
        api_client, db_session, monkeypatch, model, tools, name):
    agent, value = await setup(api_client, db_session, monkeypatch)
    await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"},
                            owner_id=UUID(agent["user_id"]))
    # Metadata, owner tool overrides and even a collision with a closed business
    # control cannot authorize acquisition from a raw credential record.
    tools.metadata = {name: {"tier": "core", "readOnlyHint": True}}
    async with db_session() as db:
        await db.execute(update(AgentCredentials).where(AgentCredentials.agent_id == UUID(agent["id"]))
                         .values(enabled_tools=["*"]))
        await db.commit()
    async def forbidden(*args, **kwargs):
        raise AssertionError("Managed touched raw credential lookup")
    def forbidden_parse(*args, **kwargs):
        raise AssertionError("Managed parsed a disabled raw invocation")
    monkeypatch.setattr(AgentService, "get_credentials", forbidden)
    monkeypatch.setattr(AgentService, "decrypt_credentials_record", forbidden_parse)
    monkeypatch.setattr(llm_module, "parse_managed_tool_arguments", forbidden_parse)
    model.chat.completions.queue(
        build_llm_response(tool_calls=[build_tool_call("hidden", name, "not-json")]),
        build_llm_response(content="false success"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "FAILED" and result["error_code"] == "TOOL_SETUP_FAILED"
    assert result["draft_text"] is None and result["operation_refs"] == []
    assert len(model.chat.completions.calls) == 1 and len(model.chat.completions._responses) == 1
    assert model.chat.completions.calls[0]["tools"] is None
    assert tools.init_calls == tools.acquisition_calls == tools.metadata_calls == tools.calls == []


MALFORMED = (
    '{"actions":[],"actions":[]}',
    '{"actions":[{"capability":"booking.create","arguments":{"slot":"12","slot":"13"}}]}',
    '{"actions":[{"capability":"booking.create","arguments":{"slot":NaN}}]}',
    '{"actions":[{"capability":"booking.create","arguments":{"slot":Infinity}}]}',
    '{"actions":[],"provider_idempotency_key":"spoof"}',
    '{"actions":[{"capability":"booking.create","arguments":{"slot":"12","operation_id":"spoof"}}]}',
    '{"actions":[{"capability":"booking.create","arguments":{"slot":"12"},"receipt":"spoof"}]}',
    '{"actions":[]}', '[]', '{"actions":' + '[' * 34 + '0' + ']' * 34 + '}',
    json.dumps({"actions": [{"capability": "booking.create", "arguments": {"slot": str(i)}} for i in range(33)]}),
)


@pytest.mark.integration
async def test_work_deadline_with_retained_no_plan_scope_stays_unknown(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    monkeypatch.setattr(get_settings(), "managed_ai_execution_seconds", 1)
    entered = asyncio.Event()
    async def waiting_model(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(model.chat.completions, "create", waiting_model)
    result = (await execute(api_client, value)).json()
    assert entered.is_set()
    assert result["status"] == "UNKNOWN" and result["draft_text"] is None
    assert result["error_code"] == "EXECUTION_TIMEOUT" and result["operation_refs"] == []
    assert provider.commands == [] and provider.fact_calls == 0
    async with db_session() as db:
        assert len((await db.scalars(select(BusinessScope))).all()) == 1
        assert (await db.scalars(select(BusinessPlan))).all() == []
        assert (await db.scalars(select(BusinessOperation))).all() == []


@pytest.mark.integration
@pytest.mark.parametrize("arguments", MALFORMED)
async def test_closed_business_json_rejects_spoofed_or_ambiguous_plan_before_provider(
        api_client, db_session, monkeypatch, model, arguments):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("bad", "business_execute_plan", arguments)]), build_llm_response(content="false success"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "FAILED" and result["draft_text"] is None
    assert result["error_code"] in {"TOOL_SETUP_FAILED", "CONTEXT_LIMIT"}
    assert result["operation_refs"] == [] and provider.commands == [] and provider.fact_calls == 0
    assert len(model.chat.completions.calls) == 1
    async with db_session() as db:
        assert (await db.scalars(select(BusinessOperation))).all() == []


@pytest.mark.integration
@pytest.mark.parametrize("early", ["inactive", "missing_key", "bad_configuration", "business_open"])
async def test_committed_refs_survive_early_repeat_setup_failures(
        api_client, db_session, monkeypatch, model, early):
    agent, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="done"))
    first = (await execute(api_client, value)).json()
    assert first["operation_refs"][0]["outcome"] == "SUCCEEDED"
    if early == "inactive":
        async with db_session() as db:
            await db.execute(update(AgentTemplate).where(AgentTemplate.id == UUID(agent["id"])).values(is_active=False))
            await db.commit()
    elif early == "missing_key":
        monkeypatch.setattr(llm_module.LLMService, "_get_agent_api_key", lambda *args: None)
    elif early == "bad_configuration":
        def invalid(*args):
            raise ValueError("private configuration")
        monkeypatch.setattr(llm_module.LLMService, "_get_agent_api_key", invalid)
    else:
        async def unavailable(*args, **kwargs):
            raise ConnectionError("private database")
        monkeypatch.setattr(BusinessRuntime, "open", unavailable)
    again = (await execute(api_client, value)).json()
    assert again["status"] == "UNKNOWN" and again["draft_text"] is None
    assert again["operation_refs"] == first["operation_refs"]
    assert len(provider.commands) == 1 and len(model.chat.completions.calls) == 2


@pytest.mark.integration
async def test_hidden_raw_call_after_effect_preserves_receipt_and_stops_same_model_response(
        api_client, db_session, monkeypatch, model, tools):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments()),
        build_tool_call("hidden", "get_contact", "not-json"),
        build_tool_call("later", "business_execute_plan", plan_arguments("13"))]),
        build_llm_response(content="false success"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "UNKNOWN" and result["error_code"] == "TOOL_SETUP_FAILED"
    assert result["draft_text"] is None and result["operation_refs"][0]["outcome"] == "SUCCEEDED"
    assert len(provider.commands) == 1 and len(model.chat.completions.calls) == 1 and tools.init_calls == []


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["unknown", "failed"])
async def test_business_non_success_halts_model_and_returns_committed_outcome(
        api_client, db_session, monkeypatch, model, mode):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session, mode=mode)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="false success"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "UNKNOWN" and result["draft_text"] is None
    assert result["operation_refs"][0]["outcome"] == mode.upper()
    assert len(model.chat.completions.calls) == len(provider.commands) == 1


@pytest.mark.integration
async def test_proposal_returns_deterministic_draft_without_continuing_model_or_dispatch(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session, confirmation=True)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("prepare", "business_execute_plan", plan_arguments())]), build_llm_response(content="already booked"))
    result = (await execute(api_client, value)).json()
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        assert plan.proposal_state == "PREPARED" and plan.confirmed_at is None
    assert result["status"] == "DRAFT_READY"
    assert result["draft_text"] == "Предложение подготовлено. Для подтверждения отправьте: confirm " + str(plan.id)
    assert len(result["operation_refs"]) == 1 and result["operation_refs"][0]["outcome"] is None
    assert provider.commands == [] and len(model.chat.completions.calls) == 1


@pytest.mark.integration
async def test_mixed_plan_refs_are_independent_and_residual_steps_are_reserved(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    provider.outcomes["13"] = "unknown"
    actions = [json.loads(plan_arguments(slot))["actions"][0] for slot in ("12", "13", "14")]
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("plan", "business_execute_plan", json.dumps({"actions": actions}))]),
        build_llm_response(content="false success"))
    result = (await execute(api_client, value)).json()
    async with db_session() as db:
        operations = (await db.scalars(select(BusinessOperation).order_by(BusinessOperation.ordinal))).all()
        by_id = {str(op.id): op.status for op in operations}
        assert [op.status for op in operations] == ["SUCCEEDED", "UNKNOWN", "RESERVED"]
        assert [op.attempt_count for op in operations] == [1, 1, 0]
    assert result["status"] == "UNKNOWN" and result["draft_text"] is None
    assert len(result["operation_refs"]) == 3 and len(provider.commands) == 2
    for ref in result["operation_refs"]:
        assert ref["outcome"] == (None if by_id[ref["operation_id"]] == "RESERVED" else by_id[ref["operation_id"]])


@pytest.mark.integration
async def test_blocked_reserved_candidate_is_unknown_without_attempt_or_consent_change(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    request = ManagedTurnRequest.model_validate(value)
    grant = get_settings().managed_principals[0].grants[0]
    registry = main.app.state.business_registry
    first = await open_business_runtime(request, grant, db_session, registry)
    later = {**value, "turn_id": str(uuid4())}
    second = await open_business_runtime(ManagedTurnRequest.model_validate(later), grant, db_session, registry)
    actions = json.loads(plan_arguments())["actions"]
    plan1 = await first.freeze_plan(actions)
    plan2 = await second.freeze_plan(actions)
    decision = await claim_dispatch_with_test_permit(
        first, plan1, plan1.operations[0], actions=actions
    )
    assert decision.token is not None
    async with db_session() as db:
        op = await db.get(BusinessOperation, plan2.operations[0].id)
        before = (op.status, op.attempt_count, op.state_version, op.request_payload)
        plan = await db.get(BusinessPlan, plan2.id)
        consent = (plan.proposal_state, plan.confirmed_turn_id, plan.confirmation_input_ids, plan.confirmed_at)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("candidate", "business_execute_plan", plan_arguments())]), build_llm_response(content="false success"))
    result = (await execute(api_client, later)).json()
    assert result["status"] == "UNKNOWN" and result["error_code"] == "TOOL_EXECUTION_UNKNOWN"
    assert result["draft_text"] is None and result["operation_refs"] == [
        {"origin": "AI_AGENT_SERVICE", "operation_id": str(plan2.operations[0].id), "outcome": None}]
    async with db_session() as db:
        op = await db.get(BusinessOperation, plan2.operations[0].id)
        assert (op.status, op.attempt_count, op.state_version, op.request_payload) == before
        plan = await db.get(BusinessPlan, plan2.id)
        assert (plan.proposal_state, plan.confirmed_turn_id, plan.confirmation_input_ids, plan.confirmed_at) == consent
    assert provider.commands == [] and len(model.chat.completions.calls) == 0


@pytest.mark.integration
async def test_final_reference_reload_failure_cannot_erase_committed_receipt(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    original = BusinessRuntime.collect_refs
    async def unavailable_after_effect(self):
        if provider.commands:
            raise ConnectionError("private database")
        return await original(self)
    monkeypatch.setattr(BusinessRuntime, "collect_refs", unavailable_after_effect)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="done"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "UNKNOWN" and result["draft_text"] is None and result["operation_refs"][0]["outcome"] == "SUCCEEDED"
    async with db_session() as db:
        op = (await db.scalars(select(BusinessOperation))).one()
        assert str(op.id) == result["operation_refs"][0]["operation_id"] and op.receipt_id is not None


@pytest.mark.integration
async def test_final_reference_read_deadline_stops_draft_but_preserves_receipt(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    original = BusinessRuntime.collect_refs
    async def expire_after_effect(self):
        if provider.commands:
            self.execution.deadline = 0
            return self.collector.snapshot()
        return await original(self)
    monkeypatch.setattr(BusinessRuntime, "collect_refs", expire_after_effect)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="done"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "UNKNOWN" and result["error_code"] == "EXECUTION_TIMEOUT"
    assert result["draft_text"] is None and result["operation_refs"][0]["outcome"] == "SUCCEEDED"


@pytest.mark.integration
async def test_invalid_final_draft_cannot_erase_committed_receipt(api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="z" * 65537))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "UNKNOWN" and result["error_code"] == "INVALID_RESULT"
    assert result["draft_text"] is None and result["operation_refs"][0]["outcome"] == "SUCCEEDED"


@pytest.mark.integration
async def test_cleanup_failure_keeps_genuine_unknown_ref(api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session, mode="unknown")
    provider.cleanup_error = True
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="false success"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "UNKNOWN" and result["error_code"] == "TOOL_CLEANUP_FAILED"
    assert result["draft_text"] is None and result["operation_refs"][0]["outcome"] == "UNKNOWN"
    assert len(model.chat.completions.calls) == 1


@pytest.mark.integration
async def test_managed_customer_confirmation_selects_original_plan_and_refs(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session, confirmation=True)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("prepare", "business_propose", plan_arguments())]))
    proposed = (await execute(api_client, value)).json()
    proposal_id = proposed["draft_text"].rsplit(" ", 1)[1]
    message = {**value["messages"][0], "id": str(uuid4()), "sequence": 2, "text": "confirm " + proposal_id}
    later = {**value, "turn_id": str(uuid4()), "input_revision": 2, "input_first_sequence": 2,
             "input_last_sequence": 2, "context_last_sequence": 2,
             "messages": [value["messages"][0], message], "selected_input_message_ids": [message["id"]]}
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("confirm", "business_confirm", json.dumps({"proposal_id": proposal_id}))]),
        build_llm_response(content="done"))
    confirmed = (await execute(api_client, later)).json()
    assert confirmed["status"] == "DRAFT_READY" and len(provider.commands) == 1
    assert confirmed["operation_refs"] == [{**proposed["operation_refs"][0], "outcome": "SUCCEEDED"}]
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        assert plan.confirmed_turn_id == UUID(later["turn_id"])
