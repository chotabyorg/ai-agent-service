"""Behavioral business safety on disposable PostgreSQL; fake providers only."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text, update

from core.managed_security import ManagedGrant
from models.agent import AgentTemplate
from models.business import BusinessBinding, BusinessOperation, BusinessPlan, BusinessScope, BusinessTurnPlanLink
from schemas.business import (Ambiguous, BusinessSafetyError, DefinitiveNoEffect, MaterialFacts,
                              ProvenFailedNoEffect, ProvenSucceeded, StillUnknown, SucceededReceipt)
from schemas.managed_turn import ManagedTurnRequest
from services.business_registry import (AdapterRegistration, BusinessRegistry, CapabilityDefinition,
                                       ProviderProfile)
from services.business_repository import BusinessRepository
from services.business_runtime import BusinessRuntime, CommittedReferenceCollector


class BookingArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str


@dataclass
class FakeProvider:
    lose_next: bool = False
    posts: int = 0
    lookups: int = 0
    objects: dict = field(default_factory=dict)
    keys: list = field(default_factory=list)
    fact_revision: int = 1
    read_allowed: bool = True
    lookup_outcome: str = "SUCCEEDED"
    forged: bool = False
    execute_outcome: str = "SUCCEEDED"


@dataclass(frozen=True)
class FakeAdapter:
    provider: FakeProvider
    mode: str

    async def resolve_facts(self, scope, actions, *, execution):
        return MaterialFacts(values={"business": "local-fake"}, revisions={"catalog": self.provider.fact_revision})

    async def receipt_read_allowed(self, scope, *, execution):
        return self.provider.read_allowed

    async def execute(self, command, *, execution):
        provider = self.provider
        provider.posts += 1
        provider.keys.append(command.provider_idempotency_key)
        if provider.execute_outcome == "FAILED":
            return DefinitiveNoEffect(binding=command.receipt_binding(),receipt_id="rejected-1",
                reason_code="DEFINITIVE_NO_EFFECT",evidence={"proof":"REJECTED_BEFORE_EFFECT"})
        identity = command.provider_idempotency_key if self.mode == "NATIVE" else str(command.operation_id)
        if identity not in provider.objects:
            provider.objects[identity] = "booking-" + str(len(provider.objects) + 1)
        object_id = provider.objects[identity]
        if provider.lose_next:
            provider.lose_next = False
            return Ambiguous()
        binding = command.receipt_binding()
        if provider.forged:
            binding = binding.model_copy(update={"operation_id": uuid4()})
        return SucceededReceipt(binding=binding, receipt_id="receipt-" + object_id,
                                object_id=object_id, evidence={"proof": "DURABLE_FAKE_OBJECT"})

    async def lookup(self, command, *, execution):
        self.provider.lookups += 1
        if self.provider.lookup_outcome == "UNKNOWN":
            return StillUnknown()
        if self.provider.lookup_outcome == "FAILED":
            return ProvenFailedNoEffect(binding=command.receipt_binding(), receipt_id="rejected-1",
                                        reason_code="DEFINITIVELY_REJECTED", evidence={"proof": "REJECTED_BEFORE_EFFECT"})
        identity = command.provider_idempotency_key if self.mode == "NATIVE" else str(command.operation_id)
        object_id = self.provider.objects.get(identity)
        if object_id is None:
            return StillUnknown()
        return ProvenSucceeded(binding=command.receipt_binding(), receipt_id="receipt-" + object_id,
                               object_id=object_id, evidence={"proof": "DURABLE_FAKE_OBJECT"})


def actions(*slots):
    return [{"capability": "booking.create", "arguments": {"slot": slot}} for slot in slots]


def make_request(agent_id, *, conversation_id=None, integration_id=None, turn_id=None, owner_id="Exact owner", messages=None):
    messages = messages or [dict(id=uuid4(), sequence=1, actor="CUSTOMER", direction="INBOUND", content_type="TEXT", text="Book at 12")]
    return ManagedTurnRequest(conversation_id=conversation_id or uuid4(), turn_id=turn_id or uuid4(),
                              owner_id=owner_id, integration_id=integration_id or uuid4(), agent_id=agent_id,
                              channel="TELEGRAM", input_revision=len(messages), authority_revision=0,
                              input_first_sequence=len(messages), input_last_sequence=len(messages),
                              context_last_sequence=len(messages), messages=messages,
                              selected_input_message_ids=[messages[-1]["id"]])


def later(request, text_value="New intentional action"):
    messages = [m.model_dump() for m in request.messages]
    messages.append(dict(id=uuid4(), sequence=len(messages) + 1, actor="CUSTOMER", direction="INBOUND",
                         content_type="TEXT", text=text_value))
    return make_request(request.agent_id, conversation_id=request.conversation_id,
                        integration_id=request.integration_id, owner_id=request.owner_id, messages=messages)


async def setup_business(factory, *, mode="LOOKUP", confirmation=False, retention=None):
    owner, agent_id = uuid4(), uuid4()
    async with factory() as session:
        session.add(AgentTemplate(id=agent_id, user_id=owner, name="Business fake", system_prompt="Fake",
                                  llm_provider="openai", model_name="fake", temperature=0.7, is_active=True))
        await session.commit()
    request = make_request(agent_id)
    grant = ManagedGrant(owner_id=request.owner_id, integration_id=request.integration_id,
                          channel=request.channel, agent_id=agent_id, agent_owner_id=owner)
    provider = FakeProvider()
    profile = ProviderProfile(profile_revision=("a" if mode == "NATIVE" else "b" if mode == "LOOKUP" else "c") * 64,
                              mode=mode, dispatch_timeout_seconds=1, test_only=True,
                              native_replay_policy="BOUNDED" if retention else "DURABLE_TEST" if mode == "NATIVE" else "NONE",
                              native_minimum_retention_seconds=retention)
    registration = AdapterRegistration(adapter_id="fake_booking", profile=profile,
                                       capabilities=(CapabilityDefinition(name="booking.create", argument_model=BookingArguments,
                                                                           requires_confirmation=confirmation, public_fields=("slot",)),),
                                       adapter=FakeAdapter(provider, mode))
    registry = BusinessRegistry((registration,))
    repository = BusinessRepository(factory)
    binding = await repository.configure_binding(grant, adapter_id=registration.adapter_id, connection_id=uuid4(),
                                                 catalog_revision=registration.catalog_revision,
                                                 enabled_capabilities=["booking.create"])
    runtime = await BusinessRuntime.open(request, grant, factory, registry)
    return runtime, provider, registry, binding


@pytest.mark.asyncio
async def test_same_turn_normalized_replay_conflict_and_later_intent(db_session):
    runtime, provider, registry, _ = await setup_business(db_session)
    result = await runtime.execute_plan(actions("Cafe\u0301 12:00"))
    assert result.halt_status is None and provider.posts == 1
    plan = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    restarted = await BusinessRuntime.open(runtime.request, runtime.grant, db_session, registry)
    await restarted.execute_plan(actions("Café 12:00"))
    assert restarted.collector.snapshot() == runtime.collector.snapshot() and provider.posts == 1
    with pytest.raises(BusinessSafetyError, match="PLAN_CONFLICT"):
        await restarted.execute_plan(actions("13:00"))
    second = await BusinessRuntime.open(later(runtime.request), runtime.grant, db_session, registry)
    await second.execute_plan(actions("Café 12:00"))
    second_plan = await second.repository.selected_plan(second.request, second.grant)
    assert second_plan.operations[0].id != plan.operations[0].id and provider.posts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["NATIVE", "LOOKUP", "BLIND"])
async def test_response_loss_has_honest_recovery_profile(db_session, mode):
    runtime, provider, registry, _ = await setup_business(db_session, mode=mode)
    provider.lose_next = True
    result = await runtime.execute_plan(actions("12:00"))
    assert result.halt_status == "UNKNOWN"
    operation_id = runtime.collector.snapshot()[0].operation_id
    assert runtime.collector.snapshot()[0].outcome == "UNKNOWN"
    restarted = await BusinessRuntime.open(runtime.request, runtime.grant, db_session, registry)
    await restarted.execute_plan(actions("12:00"))
    assert provider.posts == 1 and len(provider.objects) == 1
    if mode == "NATIVE":
        resolved = await restarted.replay_native(operation_id)
        assert provider.posts == 2 and provider.keys[0] == provider.keys[1]
        assert resolved.operations[0].command.issued_at == (await runtime.repository.selected_plan(runtime.request, runtime.grant)).operations[0].command.issued_at
    else:
        resolved = await restarted.reconcile(operation_id)
        assert provider.posts == 1
        assert provider.lookups == (1 if mode == "LOOKUP" else 0)
    assert len(provider.objects) == 1
    assert resolved.operations[0].status == ("UNKNOWN" if mode == "BLIND" else "SUCCEEDED")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["FAILED", "UNKNOWN"])
async def test_only_authoritative_negative_evidence_resolves_unknown(db_session, outcome):
    runtime, provider, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan(actions("12:00"))
    facts = await runtime._facts(actions("12:00"))
    decision = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id,
                                                       plan.operations[0].id, runtime.registration, facts)
    # B2: permission committed, send never observed. That private test fact is
    # not durable runtime proof; only provider authoritative lookup can resolve.
    await runtime.repository.mark_unknown(runtime.request, runtime.grant, decision.token, lookup_available=True)
    provider.lookup_outcome = outcome
    resolved = await runtime.reconcile(plan.operations[0].id)
    assert resolved.operations[0].status == outcome
    assert provider.posts == 0 and provider.lookups == 1


@pytest.mark.asyncio
async def test_forged_receipt_cannot_claim_success(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    provider.forged = True
    result = await runtime.execute_plan(actions("12:00"))
    assert result.halt_status == "UNKNOWN" and runtime.collector.snapshot()[0].outcome == "UNKNOWN"
    assert provider.posts == 1
    provider.forged = False
    resolved = await runtime.reconcile(runtime.collector.snapshot()[0].operation_id)
    assert resolved.operations[0].status == "SUCCEEDED" and provider.posts == 1


@pytest.mark.asyncio
async def test_native_replay_horizon_requires_remaining_guarantee(db_session):
    runtime, provider, _, _ = await setup_business(db_session, mode="NATIVE", retention=1)
    provider.lose_next = True
    await runtime.execute_plan(actions("12:00"))
    result = await runtime.replay_native(runtime.collector.snapshot()[0].operation_id)
    assert result.operations[0].status == "UNKNOWN" and provider.posts == 1


@pytest.mark.asyncio
async def test_confirmation_is_later_selected_canonical_reference_and_replay_safe(db_session):
    runtime, provider, registry, _ = await setup_business(db_session, confirmation=True)
    result = await runtime.execute_plan(actions("12:00"))
    proposal_id = result.tool_payload["proposal_id"]
    assert result.safe_draft and provider.posts == 0 and runtime.collector.snapshot()[0].outcome is None
    wrong = await BusinessRuntime.open(later(runtime.request, "yes"), runtime.grant, db_session, registry)
    with pytest.raises(BusinessSafetyError, match="CONFIRMATION_REQUIRED"):
        await wrong.confirm(proposal_id)
    confirming_request = later(runtime.request, " confirm " + proposal_id + " ")
    confirming = await BusinessRuntime.open(confirming_request, runtime.grant, db_session, registry)
    await confirming.confirm(proposal_id)
    await confirming.confirm(proposal_id)
    assert provider.posts == 1 and confirming.collector.snapshot()[0].outcome == "SUCCEEDED"
    assert await BusinessRepository.read_evidence(runtime.request, runtime.grant, db_session) == confirming.collector.snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["facts", "authorization", "disabled", "context"])
async def test_stale_confirmation_never_dispatches(db_session, change):
    runtime, provider, registry, binding = await setup_business(db_session, confirmation=True)
    result = await runtime.propose_plan(actions("12:00"))
    if change == "facts":
        provider.fact_revision += 1
    else:
        await runtime.repository.configure_binding(runtime.grant, adapter_id=runtime.registration.adapter_id,
            connection_id=binding.connection_id, catalog_revision=runtime.registration.catalog_revision,
            enabled_capabilities=[] if change == "disabled" else ["booking.create"],
            authorization_generation=2, context_generation=2 if change == "context" else 1)
    confirming = await BusinessRuntime.open(later(runtime.request, "confirm " + result.tool_payload["proposal_id"]),
                                            runtime.grant, db_session, registry)
    outcome = await confirming.confirm(result.tool_payload["proposal_id"])
    assert outcome.halt_status == "FAILED"
    assert provider.posts == 0


@pytest.mark.asyncio
async def test_ref_and_receipt_survive_agent_delete_and_db_read_failure(db_session, monkeypatch):
    runtime, provider, _, _ = await setup_business(db_session)
    await runtime.execute_plan(actions("12:00"))
    refs = runtime.collector.snapshot()
    async with db_session() as session:
        agent = await session.get(AgentTemplate, runtime.request.agent_id)
        await session.delete(agent)
        await session.commit()
    assert await BusinessRepository.read_evidence(runtime.request, runtime.grant, db_session) == refs
    async def unavailable(*args):
        raise ConnectionError
    monkeypatch.setattr(runtime.repository, "collect_refs", unavailable)
    assert await runtime.collect_refs() == refs
    assert provider.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["duplicate", "too_many", "reordered"])
async def test_plan_bound_and_immutable_order(db_session, case):
    runtime, provider, _, _ = await setup_business(db_session)
    if case == "duplicate":
        with pytest.raises(BusinessSafetyError, match="DUPLICATE_ACTION"):
            await runtime.freeze_plan(actions("12", "12"))
    elif case == "too_many":
        with pytest.raises(BusinessSafetyError, match="PLAN_BOUNDS"):
            await runtime.freeze_plan(actions(*map(str, range(33))))
    else:
        await runtime.freeze_plan(actions("12", "13"))
        with pytest.raises(BusinessSafetyError, match="PLAN_CONFLICT"):
            await runtime.freeze_plan(actions("13", "12"))
    assert provider.posts == 0


@pytest.mark.asyncio
async def test_scope_guard_preserves_reserved_candidate_until_terminal_resolution(db_session):
    first, provider, registry, _ = await setup_business(db_session)
    second = await BusinessRuntime.open(later(first.request), first.grant, db_session, registry)
    plan1 = await first.freeze_plan(actions("12"))
    plan2 = await second.freeze_plan(actions("13"))
    facts = await first._facts(actions("12"))
    decision1 = await first.repository.claim_dispatch(first.request, first.grant, plan1.id,
                                                       plan1.operations[0].id, first.registration, facts)
    for stored_state in ("live", "expired", "unknown"):
        if stored_state == "expired":
            async with db_session() as session:
                await session.execute(update(BusinessOperation).where(BusinessOperation.id == plan1.operations[0].id)
                                      .values(dispatch_lease_expires_at=func.clock_timestamp() - text("interval '1 second'")))
                await session.commit()
        elif stored_state == "unknown":
            await first.recover_attempts()
        decision2 = await second.repository.claim_dispatch(second.request, second.grant, plan2.id,
                                                            plan2.operations[0].id, second.registration,
                                                            await second._facts(actions("13")))
        assert decision2.token is None and decision2.reason == "BLOCKED_UNRESOLVED_EFFECT"
        candidate = decision2.plan.operations[0]
        assert candidate.status == "RESERVED" and candidate.attempt_count == candidate.state_version == 0
    provider.lookup_outcome = "FAILED"
    resolved = await first.reconcile(plan1.operations[0].id)
    assert resolved.operations[0].status == "FAILED"
    await second.execute_plan(actions("13"))
    assert provider.posts == 1 and second.collector.snapshot()[0].outcome == "SUCCEEDED"


@pytest.mark.asyncio
async def test_strict_plan_predecessors_and_mixed_effect_refs(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan(actions("12", "13", "14"))
    facts = await runtime._facts(actions("12", "13", "14"))
    blocked = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id,
                                                       plan.operations[1].id, runtime.registration, facts)
    assert blocked.reason == "BLOCKED_PREDECESSOR" and blocked.token is None
    first = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id,
                                                     plan.operations[0].id, runtime.registration, facts)
    await runtime._send(first.token)
    provider.lose_next = True
    outcome = await runtime.execute_plan(actions("12", "13", "14"))
    assert outcome.halt_status == "UNKNOWN" and provider.posts == 2
    states = (await runtime.repository.selected_plan(runtime.request, runtime.grant)).operations
    assert [op.status for op in states] == ["SUCCEEDED", "UNKNOWN", "RESERVED"]
    assert {ref.outcome for ref in runtime.collector.snapshot()} == {"SUCCEEDED", "UNKNOWN", None}


def test_collector_keeps_terminal_evidence_without_accepting_changed_outcome():
    from schemas.business import operation_reference
    collector, identity = CommittedReferenceCollector(), uuid4()
    collector.observe_committed([operation_reference(identity, "SUCCEEDED")])
    collector.observe_committed([operation_reference(identity, "UNKNOWN"), operation_reference(identity, "RESERVED")])
    assert collector.snapshot()[0].outcome == "SUCCEEDED"
    with pytest.raises(BusinessSafetyError, match="REF_CONFLICT"):
        collector.observe_committed([operation_reference(identity, "FAILED")])


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["RESERVATION", "PERMISSION", "RECEIPT"])
async def test_unacknowledged_commit_never_authorizes_duplicate_or_fabricates_success(db_session, monkeypatch, boundary):
    runtime, provider, registry, _ = await setup_business(db_session)
    method = {"RESERVATION": "reserve_plan", "PERMISSION": "claim_dispatch", "RECEIPT": "finalize"}[boundary]
    real = getattr(runtime.repository, method)
    async def lost_acknowledgment(*args, **kwargs):
        await real(*args, **kwargs)
        raise ConnectionError("synthetic commit acknowledgment loss")
    monkeypatch.setattr(runtime.repository, method, lost_acknowledgment)
    if boundary == "RECEIPT":
        async def unavailable(*args, **kwargs):
            raise ConnectionError
        monkeypatch.setattr(runtime.repository, "mark_unknown", unavailable)
    with pytest.raises((ConnectionError, BusinessSafetyError)):
        await runtime.execute_plan(actions("12"))
    assert provider.posts == (1 if boundary == "RECEIPT" else 0)
    if boundary == "RECEIPT":
        assert runtime.collector.snapshot()[0].outcome == "UNKNOWN"
    restarted = await BusinessRuntime.open(runtime.request, runtime.grant, db_session, registry)
    plan = await restarted.repository.selected_plan(runtime.request, runtime.grant)
    assert plan.operations[0].status == {"RESERVATION": "RESERVED", "PERMISSION": "DISPATCHING", "RECEIPT": "SUCCEEDED"}[boundary]
    await restarted.execute_plan(actions("12"))
    assert provider.posts == (0 if boundary == "PERMISSION" else 1)


@pytest.mark.asyncio
async def test_stale_original_receipt_after_normalization_requires_authoritative_lookup(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan(actions("12"))
    decision = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id, plan.operations[0].id,
        runtime.registration, await runtime._facts(actions("12")))
    original_receipt = await runtime.registration.adapter.execute(decision.token.command, execution=None)
    await runtime.repository.mark_unknown(runtime.request, runtime.grant, decision.token, lookup_available=True)
    stale = await runtime.repository.finalize(runtime.request, runtime.grant, decision.token, original_receipt)
    assert stale.operations[0].status == "UNKNOWN"
    resolved = await runtime.reconcile(plan.operations[0].id)
    assert resolved.operations[0].status == "SUCCEEDED" and provider.posts == 1 and provider.lookups == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text_template", ["yes", "да", "Confirm {id}", "confirm {id} please", "\tconfirm {id}", "confirm {id}\n"])
async def test_confirmation_exact_token_rejects_ambiguous_prose_and_unicode_whitespace(db_session, text_template):
    origin, provider, registry, _ = await setup_business(db_session, confirmation=True)
    proposal = await origin.freeze_plan(actions("12"), propose=True)
    request = later(origin.request, text_template.format(id=proposal.id))
    confirmer = await BusinessRuntime.open(request, origin.grant, db_session, registry)
    with pytest.raises(BusinessSafetyError, match="CONFIRMATION_REQUIRED"):
        await confirmer.confirm(proposal.id)
    assert provider.posts == 0


def test_registry_is_immutable_and_closed_business_values_normalize_time_money():
    from decimal import Decimal
    from schemas.business import canonical_value
    from services.business_registry import EMPTY_BUSINESS_REGISTRY
    with pytest.raises(TypeError):
        EMPTY_BUSINESS_REGISTRY._entries = {}
    assert canonical_value({"time": datetime(2026, 10, 7, 12, tzinfo=timezone.utc), "money": Decimal("12.00")}) == {
        "time": "2026-10-07T12:00:00Z", "money": "12"}
    with pytest.raises(ValueError):
        canonical_value(datetime(2026, 10, 7, 12))
    with pytest.raises(ValueError):
        canonical_value(float("nan"))


@pytest.mark.asyncio
async def test_maximum_plan_retains_32_independent_refs(db_session):
    runtime,provider,_,_=await setup_business(db_session)
    outcome=await runtime.execute_plan(actions(*map(str,range(32))))
    assert outcome.halt_status is None and provider.posts==32
    assert len(runtime.collector.snapshot())==32
    assert {ref.outcome for ref in runtime.collector.snapshot()}=={"SUCCEEDED"}


@pytest.mark.asyncio
async def test_definitive_no_effect_failure_stops_ordered_plan_without_false_success(db_session):
    runtime,provider,_,_=await setup_business(db_session)
    provider.execute_outcome="FAILED"
    outcome=await runtime.execute_plan(actions("12","13"))
    assert outcome.halt_status=="UNKNOWN" and outcome.safe_draft is None
    plan=await runtime.repository.selected_plan(runtime.request,runtime.grant)
    assert [op.status for op in plan.operations]==["FAILED","RESERVED"]
    assert provider.posts==1 and provider.objects=={}
    denied=await runtime.repository.claim_dispatch(runtime.request,runtime.grant,plan.id,plan.operations[1].id,
        runtime.registration,await runtime._facts(actions("12","13")))
    assert denied.token is None and denied.reason=="BLOCKED_PREDECESSOR"


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["facts","generation"])
async def test_native_recovery_rechecks_current_authority_and_facts(db_session,change):
    runtime,provider,_,binding=await setup_business(db_session,mode="NATIVE")
    provider.lose_next=True
    await runtime.execute_plan(actions("12"))
    original=await runtime.repository.selected_plan(runtime.request,runtime.grant)
    if change=="facts":
        provider.fact_revision+=1
    else:
        await runtime.repository.configure_binding(runtime.grant,adapter_id=runtime.registration.adapter_id,
            connection_id=binding.connection_id,catalog_revision=runtime.registration.catalog_revision,
            enabled_capabilities=["booking.create"],authorization_generation=2)
    recovered=await runtime.replay_native(original.operations[0].id)
    assert recovered.operations[0].status=="UNKNOWN" and provider.posts==1
    assert recovered.operations[0].command.provider_idempotency_key==original.operations[0].command.provider_idempotency_key
    assert recovered.operations[0].command.issued_at==original.operations[0].command.issued_at
    assert (await runtime.reconcile(original.operations[0].id)).operations[0].status=="SUCCEEDED"
    assert provider.posts==1


@pytest.mark.asyncio
async def test_unknown_evidence_survives_agent_delete_and_cannot_grant_new_dispatch(db_session):
    runtime,provider,_,_=await setup_business(db_session,mode="BLIND")
    provider.lose_next=True
    await runtime.execute_plan(actions("12"))
    original=await runtime.repository.selected_plan(runtime.request,runtime.grant)
    async with db_session() as session:
        await session.delete(await session.get(AgentTemplate,runtime.request.agent_id))
        await session.commit()
    refs=await BusinessRepository.read_evidence(runtime.request,runtime.grant,db_session)
    assert refs[0].outcome=="UNKNOWN"
    denied=await runtime.repository.claim_dispatch(runtime.request,runtime.grant,original.id,original.operations[0].id,
        runtime.registration,MaterialFacts(values={"business":"local-fake"},revisions={"catalog":1}))
    assert denied.token is None and denied.plan.operations[0].status=="UNKNOWN"
    assert provider.posts==1
