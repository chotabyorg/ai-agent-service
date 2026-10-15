"""Goal5 I1 authority-epoch boundaries for retained business proposals."""

from dataclasses import replace

import pytest

from schemas.business import BusinessSafetyError
from tests.business_effect_helpers import open_business_runtime
from services.business_registry import BusinessRegistry
from tests.test_business_runtime import actions, later, make_request, setup_business


def at_epoch(request, revision):
    """The frozen request helper defaults to epoch zero; retain its exact history."""
    return request.model_copy(update={"authority_revision": revision})


async def semantic_business(factory):
    original, provider, _, binding = await setup_business(factory, confirmation=True)
    registration = replace(original.registration, confirmation_mode="SEMANTIC_V1")
    registry = BusinessRegistry((registration,))
    await original.repository.configure_binding(
        original.grant,
        adapter_id=registration.adapter_id,
        connection_id=binding.connection_id,
        catalog_revision=registration.catalog_revision,
        enabled_capabilities=["booking.create"],
        authorization_generation=2,
    )
    runtime = await open_business_runtime(
        original.request, original.grant, factory, registry
    )
    return runtime, provider, registry


async def next_epoch_runtime(factory, original, registry, message, revision):
    request = at_epoch(later(original.request, message), revision)
    return request, await open_business_runtime(
        request, original.grant, factory, registry
    )


@pytest.mark.asyncio
async def test_i1_same_epoch_proposal_confirmation_keeps_normal_consent_flow(db_session):
    original, provider, registry = await semantic_business(db_session)
    proposal = await original.propose_plan(actions("12:00"))
    plan = await original.repository.selected_plan(original.request, original.grant)
    assert proposal.tool_payload["status"] == "AWAITING_CONFIRMATION"
    assert plan.proposal_state == "PREPARED" and provider.posts == 0

    request = at_epoch(later(original.request, "Да, подтверждаю"), 0)
    confirmer = await open_business_runtime(
        request, original.grant, db_session, registry
    )
    context = await confirmer.prompt_context()
    assert context["pending"]["proposal_id"] == str(plan.id)
    result = await confirmer.call_tool(
        "business_decide_proposal",
        {"proposal_id": str(plan.id), "intent": "CONFIRM"},
    )
    assert result.halt_status is None
    assert provider.posts == 1
    completed = await confirmer.repository.selected_plan(
        confirmer.request, confirmer.grant
    )
    assert completed.operations[0].status == "SUCCEEDED"
    assert completed.confirmed_turn_id == confirmer.request.turn_id


@pytest.mark.asyncio
async def test_i1_epoch_plus_one_invalidates_old_proposal_across_mutation_paths(db_session):
    original, provider, registry = await semantic_business(db_session)
    await original.propose_plan(actions("12:00"))
    plan = await original.repository.selected_plan(original.request, original.grant)
    assert plan.proposal_state == "PREPARED"

    request, stale = await next_epoch_runtime(
        db_session, original, registry, "Да, подтверждаю", 1
    )
    assert (await stale.prompt_context())["pending"] is None

    confirmed = await stale.call_tool(
        "business_decide_proposal",
        {"proposal_id": str(plan.id), "intent": "CONFIRM"},
    )
    cancel_attempt = await open_business_runtime(
        request, original.grant, db_session, registry
    )
    await cancel_attempt.prompt_context()
    rejected = await cancel_attempt.call_tool(
        "business_decide_proposal",
        {"proposal_id": str(plan.id), "intent": "REJECT"},
    )
    assert confirmed.halt_status == rejected.halt_status == "FAILED"

    with pytest.raises(BusinessSafetyError, match="STALE_CONFIRMATION"):
        await cancel_attempt.repository.cancel_proposal(
            cancel_attempt.request, cancel_attempt.grant, plan.id, cancel_attempt.registration
        )
    with pytest.raises(BusinessSafetyError):
        await stale.repository.claim_dispatch(
            stale.request,
            stale.grant,
            plan.id,
            plan.operations[0].id,
            stale.registration,
            stale.facts_resolver(actions("12:00")),
        )

    # Same-turn immutable replay observes the invalidated original operation;
    # it cannot turn that proposal into a fresh provider command.
    replay = await original.execute_plan(actions("12:00"))
    assert replay.halt_status == "FAILED"
    retained = await original.repository.selected_plan(
        original.request, original.grant
    )
    assert retained.proposal_state == "INVALIDATED"
    assert retained.operations[0].status == "FAILED"
    assert provider.posts == 0
    assert request.authority_revision == 1


@pytest.mark.asyncio
async def test_i1_return_to_epoch_two_cannot_reuse_old_proposal_or_confirmation(db_session):
    original, provider, registry = await semantic_business(db_session)
    await original.propose_plan(actions("12:00"))
    proposal = await original.repository.selected_plan(original.request, original.grant)

    # Persist an E0 canonical customer confirmation for this proposal without
    # dispatching. It remains historical evidence when authority later returns.
    confirmation_request = at_epoch(later(original.request, "Да"), 0)
    confirmation_turn = await open_business_runtime(
        confirmation_request, original.grant, db_session, registry
    )
    await confirmation_turn.repository.confirm_plan(
        confirmation_turn.request,
        confirmation_turn.grant,
        proposal.id,
        confirmation_turn.registration,
        confirmation_turn.facts_resolver(actions("12:00")),
        semantic=True,
    )
    assert provider.posts == 0

    epoch_one_request = at_epoch(
        later(confirmation_request, "Intervening authority epoch"), 1
    )
    epoch_one = await open_business_runtime(
        epoch_one_request, original.grant, db_session, registry
    )
    assert epoch_one.request.authority_revision == 1
    epoch_two_request = at_epoch(
        later(epoch_one_request, "Authority returned"), 2
    )
    epoch_two = await open_business_runtime(
        epoch_two_request, original.grant, db_session, registry
    )
    assert (await epoch_two.prompt_context())["pending"] is None

    # A newly selected Turn may carry the old confirmation text in its ordered
    # history, but origin/current epoch checks must keep it from reviving E0.
    old_message = confirmation_turn.request.messages[-1]
    reselected_request = make_request(
        epoch_two_request.agent_id,
        conversation_id=epoch_two_request.conversation_id,
        integration_id=epoch_two_request.integration_id,
        owner_id=epoch_two_request.owner_id,
        messages=[message.model_dump() for message in epoch_two_request.messages],
        contract_version=epoch_two_request.contract_version,
        input_revision=epoch_two_request.input_revision,
    ).model_copy(update={
        "authority_revision": 2,
        "input_first_sequence": old_message.sequence,
        "input_last_sequence": old_message.sequence,
        "selected_input_message_ids": [old_message.id],
    })
    reselected = await open_business_runtime(
        reselected_request, original.grant, db_session, registry
    )
    assert (await reselected.prompt_context())["pending"] is None
    replay = await reselected.call_tool(
        "business_decide_proposal",
        {"proposal_id": str(proposal.id), "intent": "CONFIRM"},
    )
    assert replay.halt_status == "FAILED"
    with pytest.raises(BusinessSafetyError, match="STALE_CONFIRMATION"):
        await reselected.repository.confirm_plan(
            reselected.request,
            reselected.grant,
            proposal.id,
            reselected.registration,
            reselected.facts_resolver(actions("12:00")),
            semantic=True,
        )
    retained = await reselected.repository.lookup_plan(
        reselected.request, reselected.grant, proposal.id
    )
    assert retained.origin_turn_id == original.request.turn_id
    assert retained.confirmed_turn_id == confirmation_turn.request.turn_id
    assert retained.operations[0].status == "RESERVED"
    assert provider.posts == 0


@pytest.mark.asyncio
async def test_i1_fresh_epoch_two_write_safe_and_confirmation_need_no_extra_consent(db_session):
    write_origin, write_provider, write_registry, _ = await setup_business(db_session)
    epoch_one_request = at_epoch(later(write_origin.request, "Epoch one"), 1)
    epoch_two_request = at_epoch(later(epoch_one_request, "Returned to epoch two"), 2)
    write_runtime = await open_business_runtime(
        epoch_two_request, write_origin.grant, db_session, write_registry
    )
    written = await write_runtime.execute_plan(actions("14:00"))
    assert written.halt_status is None and write_provider.posts == 1
    write_plan = await write_runtime.repository.selected_plan(
        write_runtime.request, write_runtime.grant
    )
    assert write_plan.operations[0].status == "SUCCEEDED"

    confirm_origin, confirm_provider, confirm_registry, _ = await setup_business(
        db_session, confirmation=True
    )
    confirm_epoch_one = at_epoch(later(confirm_origin.request, "Epoch one"), 1)
    confirm_epoch_two = at_epoch(later(confirm_epoch_one, "Returned to epoch two"), 2)
    proposer = await open_business_runtime(
        confirm_epoch_two, confirm_origin.grant, db_session, confirm_registry
    )
    proposal_result = await proposer.execute_plan(actions("15:00"))
    proposal = await proposer.repository.selected_plan(
        proposer.request, proposer.grant
    )
    assert proposal_result.tool_payload["status"] == "AWAITING_CONFIRMATION"
    assert proposal.operations[0].status == "RESERVED" and confirm_provider.posts == 0

    confirmation_request = at_epoch(
        later(proposer.request, "confirm " + str(proposal.id)), 2
    )
    confirmer = await open_business_runtime(
        confirmation_request, proposer.grant, db_session, confirm_registry
    )
    confirmed = await confirmer.confirm(proposal.id)
    assert confirmed.halt_status is None and confirm_provider.posts == 1
    completed = await confirmer.repository.selected_plan(
        confirmer.request, confirmer.grant
    )
    assert completed.operations[0].status == "SUCCEEDED"
    assert completed.confirmed_turn_id == confirmer.request.turn_id
