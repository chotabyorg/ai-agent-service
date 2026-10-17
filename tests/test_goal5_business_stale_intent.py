"""Semantic stale-proposal barriers after Conversation proves an input split."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from core.config import get_settings
from core.voice_latency import VoiceLatencyProfile
from models.agent import AgentTemplate
from models.business import BusinessOperation
from schemas.business import BusinessSafetyError
from schemas.conversation_effects import EffectReason
from services.business_registry import BusinessRegistry
from services.llm_service import LLMService
from services.managed_tools import ManagedExecutionError
from services.managed_runtime import ManagedRuntime
from services.prompt_builder import ChatContext
from tests.business_effect_helpers import (
    FakeConversationEffectPort,
    open_business_runtime,
)
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_business_runtime import actions, later, setup_business


class SplitAwareEffectPort(FakeConversationEffectPort):
    """Model the frozen Conversation sidecar response without classifying text."""

    def __init__(self, original_turn_id, *, old_reason=EffectReason.INTERVENING_SPLIT,
                 current_reason=EffectReason.ELIGIBLE, corrupt_old_fingerprint=False,
                 fail_old=False):
        super().__init__()
        self.original_turn_id = original_turn_id
        self.old_reason = old_reason
        self.current_reason = current_reason
        self.corrupt_old_fingerprint = corrupt_old_fingerprint
        self.fail_old = fail_old
        self.eligibility_responses = []

    async def eligibility(self, request):
        is_old_origin = (request.origin_turn_id == self.original_turn_id
                         and request.turn_id != self.original_turn_id)
        if is_old_origin and self.fail_old:
            self.eligibility_requests.append(request)
            self.eligibility_responses.append((request, None))
            raise RuntimeError("synthetic sidecar unavailable")
        result = await super().eligibility(request)
        if is_old_origin and self.corrupt_old_fingerprint:
            result = result.model_copy(update={"request_fingerprint": "0" * 64})
        else:
            reason = (self.old_reason if is_old_origin else
                      self.current_reason if request.origin_turn_id == request.turn_id
                      and request.turn_id != self.original_turn_id else
                      EffectReason.ELIGIBLE)
            if reason == EffectReason.ELIGIBLE:
                result = result.model_copy(update={
                    "eligible": True,
                    "reason": EffectReason.ELIGIBLE,
                    "split_input_floor_sequence": 0,
                })
            else:
                result = result.model_copy(update={
                    "eligible": False,
                    "reason": reason,
                    "split_input_floor_sequence": 2 if reason == EffectReason.INTERVENING_SPLIT else 0,
                })
        self.eligibility_responses.append((request, result))
        return result


async def semantic_business(factory, *, ttl=300, old_reason=EffectReason.INTERVENING_SPLIT,
                            current_reason=EffectReason.ELIGIBLE, corrupt_old_fingerprint=False,
                            fail_old=False, slots=("18:00",), include_safe_write=False,
                            channel="TELEGRAM"):
    base, provider, _, binding = await setup_business(
        factory, confirmation=True, channel=channel)
    capabilities = base.registration.capabilities
    enabled_capabilities = ["booking.create"]
    if include_safe_write:
        from dataclasses import replace as dataclass_replace

        safe_write = dataclass_replace(
            capabilities[0], name="booking.safe_write", requires_confirmation=False)
        capabilities = (*capabilities, safe_write)
        enabled_capabilities.append("booking.safe_write")
    registration = replace(
        base.registration,
        capabilities=capabilities,
        confirmation_mode="SEMANTIC_V1",
        profile=base.registration.profile.model_copy(
            update={"proposal_ttl_seconds": ttl}),
    )
    registry = BusinessRegistry((registration,))
    await base.repository.configure_binding(
        base.grant,
        adapter_id=registration.adapter_id,
        connection_id=binding.connection_id,
        catalog_revision=registration.catalog_revision,
        enabled_capabilities=enabled_capabilities,
        authorization_generation=2,
    )
    port = SplitAwareEffectPort(
        base.request.turn_id,
        old_reason=old_reason,
        current_reason=current_reason,
        corrupt_old_fingerprint=corrupt_old_fingerprint,
        fail_old=fail_old,
    )
    original = await open_business_runtime(
        base.request, base.grant, factory, registry, effect_port=port)
    await original.propose_plan(actions(*slots))
    plan = await original.repository.selected_plan(original.request, original.grant)
    assert plan is not None and plan.proposal_state == "PREPARED"
    assert all(op.status == "RESERVED" for op in plan.operations)
    return original, provider, registry, plan, port


async def later_runtime(factory, original, registry, port, message, *, execution=None):
    request = later(original.request, message)
    runtime = await open_business_runtime(
        request, original.grant, factory, registry,
        execution=execution, effect_port=port)
    return request, runtime


async def run_managed_core(factory, request, business, model):
    managed = ManagedRuntime(20, request=request)
    managed.business = business
    business.execution = managed
    async with factory() as session:
        agent = await session.get(AgentTemplate, request.agent_id)
        try:
            return await LLMService()._execute_core(
                session,
                agent=agent,
                llm_client=model,
                credentials_records=(),
                communication=[{
                    "role": "user" if item.actor == "CUSTOMER" else "assistant",
                    "content": item.text,
                } for item in request.messages],
                chat_context=ChatContext(channel="telegram", external_client_id="synthetic"),
                query="\n".join(item.text for item in request.selected_messages),
                runtime=managed,
                selected_indices=request.selected_local_indices,
                simple_selection=request.simple_selection,
            )
        finally:
            await managed.stack.aclose()


def semantic_call(proposal_id, intent, call_id="decision"):
    call = build_tool_call(call_id, "business_decide_proposal", json.dumps({
        "proposal_id": str(proposal_id), "intent": intent,
    }))
    call.type = "function"
    return call


def proposal_call(slot, call_id="fresh-proposal"):
    call = build_tool_call(call_id, "business_propose", json.dumps({
        "actions": [{"capability": "booking.create", "arguments": {"slot": slot}}],
    }))
    call.type = "function"
    return call


def execute_call(capability, slot, call_id="fresh-execute"):
    call = build_tool_call(call_id, "business_execute_plan", json.dumps({
        "actions": [{"capability": capability, "arguments": {"slot": slot}}],
    }))
    call.type = "function"
    return call


async def test_stale_split_requires_forced_decision_and_blocks_bare_confirmation(
        db_session, monkeypatch):
    original, provider, registry, old_plan, port = await semantic_business(db_session)
    request, stale = await later_runtime(
        db_session, original, registry, port, "Да")
    context = await stale.prompt_context()
    assert context["pending"]["proposal_id"] == str(old_plan.id)
    assert isinstance(context["pending"]["summary"], str)
    assert context["pending"]["confirmable"] is False
    assert [tool["function"]["name"] for tool in stale.openai_tools()] == [
        "business_decide_proposal"]
    assert [(item.turn_id, item.origin_turn_id) for item in port.eligibility_requests[-2:]] == [
        (request.turn_id, old_plan.origin_turn_id),
        (request.turn_id, request.turn_id),
    ]
    assert [result.reason for observed_request, result in port.eligibility_responses[-2:]
            if observed_request.turn_id == request.turn_id] == [
        EffectReason.INTERVENING_SPLIT, EffectReason.ELIGIBLE]

    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[
        semantic_call(old_plan.id, "CONFIRM")]))
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    from services.managed_tools import ManagedExecutionError

    with pytest.raises(ManagedExecutionError, match="TOOL_SETUP_FAILED"):
        await run_managed_core(db_session, request, stale, model)
    assert model.chat.completions.calls[0]["tool_choice"] == {
        "type": "function", "function": {"name": "business_decide_proposal"}}
    assert provider.posts == 0 and port.permit_requests == []
    retained = await original.repository.lookup_plan(
        original.request, original.grant, old_plan.id)
    assert retained.proposal_state == "PREPARED"
    assert all(op.status == "RESERVED" and op.effect_permit_id is None
               and op.effect_request_turn_id is None for op in retained.operations)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["REJECT", "UNRELATED", "wrong_id"])
async def test_stale_nonchange_decisions_and_direct_mutations_leave_plan_locked(
        db_session, intent):
    original, provider, registry, old_plan, port = await semantic_business(db_session)
    _, stale = await later_runtime(db_session, original, registry, port, "Как дела?")
    await stale.prompt_context()
    selected_id = old_plan.id if intent != "wrong_id" else uuid4()
    semantic_intent = "CHANGE" if intent == "wrong_id" else intent
    result = await stale.call_tool("business_decide_proposal", {
        "proposal_id": str(selected_id), "intent": semantic_intent,
    })
    assert result.tool_payload.get("status") != "STALE_PROPOSAL_RETIRED"
    assert provider.posts == 0 and port.permit_requests == []
    with pytest.raises(BusinessSafetyError):
        await stale.propose_plan(actions("21:00"))
    with pytest.raises(BusinessSafetyError):
        await stale.execute_plan(actions("21:00"))
    retained = await original.repository.lookup_plan(
        original.request, original.grant, old_plan.id)
    assert retained.proposal_state == "PREPARED"
    assert all(op.status == "RESERVED" and op.attempt_count == 0
               and op.effect_permit_id is None for op in retained.operations)


@pytest.mark.asyncio
async def test_semantic_change_refreshes_model_tools_and_requires_fresh_confirmation(
        db_session, monkeypatch):
    original, provider, registry, old_plan, port = await semantic_business(db_session)
    request, stale = await later_runtime(
        db_session, original, registry, port, "Запиши заново на 18:00")
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        build_llm_response(tool_calls=[semantic_call(old_plan.id, "CHANGE")]),
        build_llm_response(tool_calls=[proposal_call("18:00")]),
    )
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    response = await run_managed_core(db_session, request, stale, model)
    assert response and "18:00" in response
    assert model.chat.completions.calls[0]["tool_choice"] == {
        "type": "function", "function": {"name": "business_decide_proposal"}}
    next_tools = {tool["function"]["name"]
                  for tool in model.chat.completions.calls[1]["tools"]}
    assert "business_propose" in next_tools
    assert "tool_choice" not in model.chat.completions.calls[1]

    fresh_plan = await stale.repository.selected_plan(stale.request, stale.grant)
    assert fresh_plan.id != old_plan.id
    assert fresh_plan.origin_turn_id == request.turn_id
    assert fresh_plan.proposal_state == "PREPARED"
    assert all(op.status == "RESERVED" and op.effect_permit_id is None
               and op.effect_request_turn_id is None for op in fresh_plan.operations)
    retired = await original.repository.lookup_plan(
        original.request, original.grant, old_plan.id)
    assert retired.proposal_state == "INVALIDATED"
    assert all(op.status == "FAILED" and op.attempt_count == 0
               and op.effect_permit_id is None for op in retired.operations)
    async with db_session() as session:
        retired_rows = (await session.scalars(select(BusinessOperation).where(
            BusinessOperation.plan_id == old_plan.id))).all()
    assert all(row.failure_code == "STALE_INTERVENING_SPLIT"
               and row.receipt_evidence["kind"] == "NO_DISPATCH"
               for row in retired_rows)
    assert provider.posts == 0 and port.permit_requests == []

    confirmation_request, confirmer = await later_runtime(
        db_session, stale, registry, port, "Да")
    pending = await confirmer.prompt_context()
    assert pending["pending"]["proposal_id"] == str(fresh_plan.id)
    assert pending["pending"]["confirmable"] is True
    result = await confirmer.call_tool("business_decide_proposal", {
        "proposal_id": str(fresh_plan.id), "intent": "CONFIRM",
    })
    assert result.halt_status is None and provider.posts == 1
    completed = await confirmer.repository.selected_plan(
        confirmation_request, confirmer.grant)
    assert completed.operations[0].status == "SUCCEEDED"
    assert completed.confirmed_turn_id == confirmation_request.turn_id


@pytest.mark.asyncio
async def test_stale_change_allows_same_parameters_through_write_safe_policy(
        db_session, monkeypatch):
    original, provider, registry, old_plan, port = await semantic_business(
        db_session, include_safe_write=True)
    request, stale = await later_runtime(
        db_session, original, registry, port, "Создай новую запись на 18:00")
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        build_llm_response(tool_calls=[semantic_call(old_plan.id, "CHANGE")]),
        build_llm_response(tool_calls=[execute_call("booking.safe_write", "18:00")]),
        build_llm_response(content="Записал."),
    )
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    await run_managed_core(db_session, request, stale, model)
    assert "business_execute_plan" in {
        tool["function"]["name"] for tool in model.chat.completions.calls[1]["tools"]}
    assert provider.posts == 1

    old = await original.repository.lookup_plan(
        original.request, original.grant, old_plan.id)
    fresh = await stale.repository.selected_plan(stale.request, stale.grant)
    assert old.proposal_state == "INVALIDATED"
    assert fresh.id != old_plan.id and fresh.origin_turn_id == request.turn_id
    assert fresh.operations[0].status == "SUCCEEDED"
    assert fresh.operations[0].command.arguments == old_plan.operations[0].command.arguments
    assert fresh.operations[0].effect_permit_id is not None
    permit = port.permit_requests[0]
    assert permit.turn_id == request.turn_id and permit.origin_turn_id == request.turn_id


@pytest.mark.asyncio
async def test_voice_profile_bytes_are_included_in_stale_change_refresh_budget(
        db_session, monkeypatch):
    original, provider, registry, old_plan, port = await semantic_business(
        db_session, channel="VOICE")
    request, stale = await later_runtime(
        db_session, original, registry, port, "Запиши заново на 18:00")
    settings = get_settings()
    profile = VoiceLatencyProfile(
        owner_id=request.owner_id,
        integration_id=request.integration_id,
        agent_id=request.agent_id,
        model="fake",
    )
    monkeypatch.setattr(settings, "managed_voice_latency_profiles", [profile])
    monkeypatch.setattr(settings, "managed_ai_context_bytes", 131072)
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[
        semantic_call(old_plan.id, "CHANGE")]))
    service = LLMService()
    original_check = service._check_managed_envelope
    expected_sizes = {}
    refresh_observations = []

    def bounded_check(agent, messages, tools, *, tool_choice=None, request=None):
        with_profile = len(json.dumps(service._managed_envelope(
            agent, messages, tools, tool_choice=tool_choice, request=request),
            ensure_ascii=False, allow_nan=False).encode("utf-8"))
        without_profile = len(json.dumps(service._managed_envelope(
            agent, messages, tools, tool_choice=tool_choice, request=None),
            ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if tool_choice is None and not refresh_observations:
            # The limit is set just below this call's profile envelope, so the
            # voice profile's extra bytes are exactly what pushes it over.
            budget = max(1024, without_profile)
            assert with_profile > budget
            expected_sizes.update(with_profile=with_profile,
                                 without_profile=without_profile, budget=budget)
            monkeypatch.setattr(settings, "managed_ai_context_bytes", budget)
            refresh_observations.append(request)
        return original_check(agent, messages, tools, tool_choice=tool_choice, request=request)

    monkeypatch.setattr(service, "_check_managed_envelope", bounded_check)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    managed = ManagedRuntime(20, request=request)
    managed.business = stale
    stale.execution = managed
    async with db_session() as session:
        agent = await session.get(AgentTemplate, request.agent_id)
        try:
            with pytest.raises(ManagedExecutionError, match="CONTEXT_LIMIT") as raised:
                await service._execute_core(
                    session,
                    agent=agent,
                    llm_client=model,
                    credentials_records=(),
                    communication=[{
                        "role": "user" if item.actor == "CUSTOMER" else "assistant",
                        "content": item.text,
                    } for item in request.messages],
                    chat_context=ChatContext(channel="voice", external_client_id="synthetic"),
                    query="\n".join(item.text for item in request.selected_messages),
                    runtime=managed,
                    selected_indices=request.selected_local_indices,
                    simple_selection=request.simple_selection,
                )
        finally:
            await managed.stack.aclose()
    assert raised.value.code == "CONTEXT_LIMIT"
    assert expected_sizes["without_profile"] <= expected_sizes["budget"]
    assert expected_sizes["budget"] < expected_sizes["with_profile"]
    assert len(refresh_observations) == 1 and refresh_observations[0] is request
    assert len(model.chat.completions.calls) == 1
    assert model.chat.completions.calls[0]["extra_body"] == {
        "reasoning": {"effort": "none"}, "provider": {"sort": "latency"}}
    assert provider.posts == 0 and port.permit_requests == []


@pytest.mark.asyncio
async def test_expired_stale_proposal_model_change_keeps_a_nonconfirmable_restart_barrier(
        db_session):
    original, provider, registry, old_plan, port = await semantic_business(
        db_session, ttl=1)
    await asyncio.sleep(1.05)
    request, stale = await later_runtime(
        db_session, original, registry, port, "Да")
    context = await stale.prompt_context()
    assert context["pending"]["proposal_id"] == str(old_plan.id)
    assert context["pending"]["confirmable"] is False
    # CHANGE is the model's explicit semantic decision here; the bare user text
    # is intentionally not interpreted as a confirmation by this unit seam.
    change = await stale.call_tool("business_decide_proposal", {
        "proposal_id": str(old_plan.id), "intent": "CHANGE",
    })
    assert change.tool_payload["status"] == "STALE_PROPOSAL_RETIRED"

    restarted = await open_business_runtime(
        request, original.grant, db_session, registry, effect_port=port)
    after_restart = await restarted.prompt_context()
    assert after_restart["pending"]["proposal_id"] == str(old_plan.id)
    assert after_restart["pending"]["confirmable"] is False
    rejected = await restarted.call_tool("business_decide_proposal", {
        "proposal_id": str(old_plan.id), "intent": "CONFIRM",
    })
    assert rejected.halt_status == "FAILED"
    assert provider.posts == 0 and port.permit_requests == []
    tombstone = await restarted.repository.lookup_plan(
        restarted.request, restarted.grant, old_plan.id)
    assert tombstone.proposal_state == "INVALIDATED"
    assert all(op.status == "FAILED" and op.effect_permit_id is None
               for op in tombstone.operations)
    async with db_session() as session:
        tombstone_rows = (await session.scalars(select(BusinessOperation).where(
            BusinessOperation.plan_id == old_plan.id))).all()
    assert all(row.failure_code == "STALE_INTERVENING_SPLIT"
               and row.receipt_evidence["kind"] == "NO_DISPATCH"
               for row in tombstone_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "port_options",
    [
        {"old_reason": EffectReason.AUTHORITY_CHANGED},
        {"old_reason": EffectReason.ORIGIN_STALE},
        {"corrupt_old_fingerprint": True},
        {"fail_old": True},
        {"current_reason": EffectReason.INPUT_SUPERSEDED},
    ],
)
async def test_only_exact_split_plus_positive_self_eligibility_allows_retirement(
        db_session, port_options):
    original, provider, registry, old_plan, port = await semantic_business(
        db_session, **port_options)
    _, candidate = await later_runtime(
        db_session, original, registry, port, "Book another time")
    context = await candidate.prompt_context()
    tools = {tool["function"]["name"] for tool in candidate.openai_tools()}

    if (port_options.get("current_reason", EffectReason.ELIGIBLE)
            != EffectReason.ELIGIBLE
            and port_options.get("old_reason", EffectReason.INTERVENING_SPLIT)
            == EffectReason.INTERVENING_SPLIT):
        assert context["pending"]["proposal_id"] == str(old_plan.id)
        assert context["pending"]["confirmable"] is False
        assert "business_decide_proposal" in tools
        outcome = await candidate.call_tool("business_decide_proposal", {
            "proposal_id": str(old_plan.id), "intent": "CHANGE",
        })
        assert outcome.tool_payload["status"] == "STALE_PROPOSAL_RETAINED"
    else:
        assert context["pending"] is None
        assert "business_decide_proposal" not in tools
        with pytest.raises(BusinessSafetyError):
            await candidate.propose_plan(actions("21:00"))

    retained = await original.repository.lookup_plan(
        original.request, original.grant, old_plan.id)
    assert retained.proposal_state == "PREPARED"
    assert all(op.status == "RESERVED" and op.effect_permit_id is None
               and op.effect_request_turn_id is None for op in retained.operations)
    assert provider.posts == 0 and port.permit_requests == []
