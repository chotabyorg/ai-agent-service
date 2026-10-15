"""Natural model decisions retain the canonical Goal3 proposal/effect identity."""
from tests.business_effect_helpers import open_business_runtime
import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from models.business import BusinessPlan
from services.business_registry import BusinessRegistry
from services.business_runtime import BusinessRuntime
from tests.test_business_runtime import actions, later, setup_business


async def setup_semantic(factory, *, ttl=300):
    original, provider, registry, binding = await setup_business(factory, confirmation=True)
    registration = replace(original.registration, confirmation_mode="SEMANTIC_V1",
        profile=original.registration.profile.model_copy(update={"proposal_ttl_seconds": ttl}))
    registry = BusinessRegistry((registration,))
    await original.repository.configure_binding(original.grant, adapter_id=registration.adapter_id,
        connection_id=binding.connection_id, catalog_revision=registration.catalog_revision,
        enabled_capabilities=["booking.create"], authorization_generation=2)
    runtime = await open_business_runtime(original.request, original.grant, factory, registry)
    return runtime, provider, registry


async def next_runtime(factory, original, registry, message):
    return await open_business_runtime(later(original.request, message), original.grant, factory, registry)


async def decide(runtime, proposal, intent):
    await runtime.prompt_context()
    return await runtime.call_tool("business_decide_proposal", {"proposal_id": str(proposal.id), "intent": intent})


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["+", "ага", "ок", "👍", "давайте"])
async def test_model_semantic_acceptance_executes_original_frozen_operation(db_session, message):
    original, provider, registry = await setup_semantic(db_session)
    result = await original.propose_plan(actions("18:00"))
    proposal = await original.repository.selected_plan(original.request, original.grant)
    assert str(proposal.id) not in result.safe_draft and "18:00" in result.safe_draft
    assert provider.posts == 0
    confirmer = await next_runtime(db_session, original, registry, message)
    context = await confirmer.prompt_context()
    assert context["pending"]["proposal_id"] == str(proposal.id)
    assert not confirmer.collector.snapshot()  # prompt context is not effect evidence
    result = await decide(confirmer, proposal, "CONFIRM")
    assert result.halt_status is None and provider.posts == 1
    refs = confirmer.collector.snapshot()
    assert refs[0].operation_id == str(proposal.operations[0].id) and refs[0].outcome == "SUCCEEDED"
    restarted = await open_business_runtime(confirmer.request, confirmer.grant, db_session, registry)
    await decide(restarted, proposal, "CONFIRM")
    assert provider.posts == 1
    async with db_session() as session:
        stored = await session.get(BusinessPlan, proposal.id)
        assert stored.confirmation_policy == "SEMANTIC_CUSTOMER_TURN_V1"
        assert stored.confirmation_input_ids == [str(confirmer.request.messages[-1].id)]


@pytest.mark.asyncio
async def test_changed_reply_cancels_old_and_requires_new_later_confirmation(db_session):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    changer = await next_runtime(db_session, original, registry, "да, только лучше в 19")
    assert (await decide(changer, proposal, "CHANGE")).tool_payload["status"] == "CANCELLED"
    changed = await changer.freeze_plan(actions("19:00"), propose=True)
    assert changed.id != proposal.id and provider.posts == 0
    assert (await decide(changer, changed, "CONFIRM")).halt_status == "FAILED"
    confirmer = await next_runtime(db_session, changer, registry, "+")
    assert (await confirmer.prompt_context())["pending"]["proposal_id"] == str(changed.id)
    await decide(confirmer, changed, "CONFIRM")
    assert provider.posts == 1
    async with db_session() as session:
        assert (await session.get(BusinessPlan, proposal.id)).proposal_state == "CANCELLED"
    assert changed.operations[0].command.arguments["slot"] == "19:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,message,state", [("REJECT", "нет", "CANCELLED"), ("UNRELATED", "Как вас найти?", "PREPARED")])
async def test_nonconfirmation_never_dispatches_or_attaches_old_refs(db_session, intent, message, state):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    customer = await next_runtime(db_session, original, registry, message)
    assert (await decide(customer, proposal, intent)).halt_status is None
    assert provider.posts == 0 and not customer.collector.snapshot()
    assert (await decide(customer, proposal, "CONFIRM")).halt_status == "FAILED"
    async with db_session() as session:
        assert (await session.get(BusinessPlan, proposal.id)).proposal_state == state


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["same_turn", "other_conversation", "noncustomer", "expired", "disabled", "facts", "superseded"])
async def test_semantic_confirmation_server_side_denials(db_session, invalid):
    original, provider, registry = await setup_semantic(db_session, ttl=1 if invalid == "expired" else 300)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    customer = await next_runtime(db_session, original, registry, "+")
    if invalid == "same_turn":
        customer = original
    elif invalid == "other_conversation":
        customer = await open_business_runtime(customer.request.model_copy(update={"conversation_id": uuid4()}),
            original.grant, db_session, registry)
    elif invalid == "noncustomer":
        request = later(original.request, "+")
        messages = list(request.messages)
        messages[-1] = messages[-1].model_copy(update={"actor": "SYSTEM"})
        customer = await open_business_runtime(request.model_copy(update={"messages": messages}), original.grant, db_session, registry)
    elif invalid == "expired":
        await asyncio.sleep(1.1)
    elif invalid == "disabled":
        binding = original.binding
        await original.repository.configure_binding(original.grant, adapter_id=original.registration.adapter_id,
            connection_id=binding.connection_id, catalog_revision=original.registration.catalog_revision,
            enabled_capabilities=[], authorization_generation=3)
    elif invalid == "facts":
        provider.fact_revision = 2
    elif invalid == "superseded":
        replacement_turn = await next_runtime(db_session, original, registry, "Лучше 19")
        await replacement_turn.freeze_plan(actions("19:00"), propose=True)
        customer = await next_runtime(db_session, replacement_turn, registry, "+")
    result = await decide(customer, proposal, "CONFIRM")
    assert result.halt_status in {"FAILED", "UNKNOWN"} and provider.posts == 0


@pytest.mark.asyncio
async def test_tool_rejects_payload_override_and_internal_provider_selection(db_session):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    customer = await next_runtime(db_session, original, registry, "+")
    await customer.prompt_context()
    for key, value in (("arguments", {"slot": "19:00"}), ("connection_id", str(uuid4())), ("workspace_id", str(uuid4()))):
        result = await customer.call_tool("business_decide_proposal", {"proposal_id": str(proposal.id), "intent": "CONFIRM", key: value})
        assert result.halt_status == "FAILED"
    assert provider.posts == 0


@pytest.mark.asyncio
async def test_unknown_original_blocks_another_semantic_proposal(db_session):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    customer = await next_runtime(db_session, original, registry, "+")
    provider.lose_next = True
    result = await decide(customer, proposal, "CONFIRM")
    assert result.halt_status == "UNKNOWN" and provider.posts == 1
    retry = await open_business_runtime(customer.request, customer.grant, db_session, registry)
    assert (await decide(retry, proposal, "CONFIRM")).halt_status == "UNKNOWN"
    assert provider.posts == 1 and len(provider.objects) == 1
