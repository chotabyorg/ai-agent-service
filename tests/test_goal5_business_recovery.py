"""Goal5 sticky UNKNOWN recovery acceptance on PostgreSQL-backed AI state."""

from dataclasses import dataclass, replace
from uuid import uuid4

import pytest

from business_capabilities.schemas import CatalogSearch
from models.business_connection import BusinessConnection
from schemas.business import Ambiguous, BusinessSafetyError, ProvenFailedNoEffect, ProvenSucceeded
from services.business_registry import BusinessRegistry, CapabilityDefinition
from tests.business_effect_helpers import open_business_runtime
from tests.test_business_runtime import FakeAdapter, actions, later, setup_business


async def _unknown_original(factory, *, adapter_type=FakeAdapter, adapter_options=None):
    original, provider, registry, _ = await setup_business(factory)
    registration = replace(original.registration, adapter=adapter_type(provider, "LOOKUP", **(adapter_options or {})))
    registry = BusinessRegistry((registration,))
    original = await open_business_runtime(original.request, original.grant, factory, registry)
    provider.lose_next = True
    result = await original.execute_plan(actions("12:00"))
    assert result.halt_status == "UNKNOWN"
    plan = await original.repository.selected_plan(original.request, original.grant)
    assert plan is not None and plan.operations[0].status == "UNKNOWN"
    return original, provider, registry, plan


async def _recovery(factory, original, registry, provider, *, lookup_outcome="UNKNOWN", text="Попробуй ещё раз"):
    provider.lookup_outcome = lookup_outcome
    runtime = await open_business_runtime(
        later(original.request, text), original.grant, factory, registry,
    )
    assert runtime.recovery_mode
    retained = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    assert retained is not None and retained.selection_kind == "RECOVERY"
    assert retained.id == (await original.repository.selected_plan(original.request, original.grant)).id
    await runtime.prompt_context()
    return runtime


def _refs_by_operation(runtime):
    return {reference.operation_id: reference.outcome for reference in runtime.collector.snapshot()}


@pytest.mark.asyncio
async def test_goal5_i2_try_again_keeps_unknown_recovery_read_only(db_session):
    original, provider, registry, plan = await _unknown_original(db_session)
    recovery = await _recovery(db_session, original, registry, provider)

    assert await recovery.execution_guard() is None
    tools = {tool["function"]["name"] for tool in recovery.openai_tools()}
    assert tools == {"business_operation_status"}
    with pytest.raises(BusinessSafetyError, match="TOOL_UNAVAILABLE"):
        await recovery.call_tool("business_execute_plan", {"actions": actions("13:00")})
    status = await recovery.call_tool("business_operation_status", {})
    final = await recovery.final_result_guard()

    retained = await recovery.repository.selected_plan(recovery.request, recovery.grant)
    assert retained is not None and retained.id == plan.id
    assert retained.operations[0].status == "UNKNOWN" and retained.operations[0].attempt_count == 1
    assert status.tool_payload == {"status": "RECOVERY_READ_ONLY", "operations": [
        {"operation_id": str(plan.operations[0].id), "status": "UNKNOWN"},
    ]}
    assert final.safe_draft and "не подтверждён" in final.safe_draft
    assert _refs_by_operation(recovery) == {str(plan.operations[0].id): "UNKNOWN"}
    assert provider.posts == 1


@pytest.mark.asyncio
async def test_goal5_i2_unknown_question_allows_model_status_and_safe_read(db_session):
    original, provider, registry, plan = await _readable_unknown_original(db_session)
    provider.lookup_outcome = "UNKNOWN"
    recovery = await open_business_runtime(
        later(original.request, "Я записан?"), original.grant, db_session, registry,
    )
    await recovery.prompt_context()

    names = {tool["function"]["name"] for tool in recovery.openai_tools()}
    assert names == {"catalog_search", "business_operation_status"}
    assert await recovery.execution_guard() is None
    read = await recovery.call_tool("catalog_search", {"query": ""})
    status = await recovery.call_tool("business_operation_status", {})
    final = await recovery.final_result_guard()

    assert read.tool_payload == {"read": "allowed", "query": ""}
    assert status.tool_payload["operations"] == [
        {"operation_id": str(plan.operations[0].id), "status": "UNKNOWN"},
    ]
    assert final.safe_draft and "не подтверждён" in final.safe_draft
    assert provider.posts == 1 and provider.lookups == 1
    assert _refs_by_operation(recovery) == {str(plan.operations[0].id): "UNKNOWN"}


@pytest.mark.asyncio
async def test_goal5_i2_lookup_success_is_sticky_and_truthful(db_session):
    original, provider, registry, plan = await _unknown_original(db_session)
    recovery = await _recovery(
        db_session, original, registry, provider, lookup_outcome="SUCCEEDED", text="Я записан?",
    )
    final = await recovery.final_result_guard()
    retained = await recovery.repository.selected_plan(recovery.request, recovery.grant)
    with pytest.raises(BusinessSafetyError, match="TOOL_UNAVAILABLE"):
        await recovery.call_tool("business_execute_plan", {"actions": actions("13:00")})

    assert retained is not None and retained.selection_kind == "RECOVERY"
    assert retained.operations[0].status == "SUCCEEDED"
    assert final.safe_draft == "Предыдущее действие подтверждено как выполненное."
    assert _refs_by_operation(recovery) == {str(plan.operations[0].id): "SUCCEEDED"}
    assert {tool["function"]["name"] for tool in recovery.openai_tools()} == {"business_operation_status"}
    assert provider.posts == 1 and provider.lookups == 1


@pytest.mark.asyncio
async def test_goal5_i2_lookup_no_effect_stays_sticky_then_fresh_turn_can_write(db_session):
    original, provider, registry, original_plan = await _unknown_original(
        db_session, adapter_type=AmbiguousNoEffectOnceAdapter,
    )
    recovery = await _recovery(
        db_session, original, registry, provider, lookup_outcome="FAILED", text="Я записан?",
    )
    final = await recovery.final_result_guard()
    retained = await recovery.repository.selected_plan(recovery.request, recovery.grant)
    with pytest.raises(BusinessSafetyError, match="TOOL_UNAVAILABLE"):
        await recovery.call_tool("business_execute_plan", {"actions": actions("13:00")})
    assert retained is not None and retained.selection_kind == "RECOVERY"
    assert retained.operations[0].status == "FAILED"
    assert final.safe_draft and "подтверждено невыполненных: 1" in final.safe_draft
    assert _refs_by_operation(recovery) == {str(original_plan.operations[0].id): "FAILED"}
    assert provider.posts == 1

    fresh = await open_business_runtime(
        later(recovery.request, "Новая запись на 13:00"), recovery.grant, db_session, registry,
    )
    assert not fresh.recovery_mode
    result = await fresh.execute_plan(actions("13:00"))
    new_plan = await fresh.repository.selected_plan(fresh.request, fresh.grant)
    assert result.halt_status is None and new_plan is not None
    assert new_plan.id != original_plan.id and new_plan.operations[0].status == "SUCCEEDED"
    assert provider.posts == 2


@pytest.mark.asyncio
async def test_goal5_i2_lookup_still_unknown_keeps_original_reference_and_no_new_plan(db_session):
    original, provider, registry, plan = await _unknown_original(db_session)
    recovery = await _recovery(db_session, original, registry, provider, lookup_outcome="UNKNOWN")
    final = await recovery.final_result_guard()
    retained = await recovery.repository.selected_plan(recovery.request, recovery.grant)

    assert retained is not None and retained.id == plan.id and retained.selection_kind == "RECOVERY"
    assert retained.operations[0].status == "UNKNOWN"
    assert final.tool_payload == {"status": "RECOVERY_READ_ONLY", "operations": ["UNKNOWN"]}
    assert final.safe_draft and "Новая попытка не выполнялась." in final.safe_draft
    assert _refs_by_operation(recovery) == {str(plan.operations[0].id): "UNKNOWN"}
    assert provider.posts == 1 and provider.lookups == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["permission", "timeout", "malformed", "foreign_receipt"])
async def test_goal5_i2_reconcile_failures_do_not_unlock_mutation(db_session, failure):
    original, provider, registry, plan = await _unknown_original(
        db_session, adapter_type=FaultyLookupAdapter, adapter_options={"failure": failure},
    )
    if failure == "permission":
        provider.read_allowed = False
    recovery = await _recovery(db_session, original, registry, provider, text="Я записан?")
    final = await recovery.final_result_guard()
    with pytest.raises(BusinessSafetyError, match="TOOL_UNAVAILABLE"):
        await recovery.call_tool("business_execute_plan", {"actions": actions("13:00")})
    retained = await recovery.repository.selected_plan(recovery.request, recovery.grant)

    assert retained is not None and retained.id == plan.id
    assert retained.operations[0].status == "UNKNOWN"
    assert final.safe_draft and "не подтверждён" in final.safe_draft
    assert _refs_by_operation(recovery) == {str(plan.operations[0].id): "UNKNOWN"}
    assert provider.posts == 1


@pytest.mark.asyncio
async def test_goal5_i2_unavailable_final_evidence_beats_recovery_draft(db_session, monkeypatch):
    original, provider, registry, plan = await _unknown_original(db_session)
    recovery = await _recovery(db_session, original, registry, provider)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("retained evidence unavailable")

    monkeypatch.setattr(recovery.repository, "result_gate", unavailable)
    final = await recovery.final_result_guard()

    assert final.halt_status == "UNKNOWN" and final.safe_draft is None
    assert _refs_by_operation(recovery) == {str(plan.operations[0].id): "UNKNOWN"}
    assert provider.posts == 1


class ReadableFakeAdapter(FakeAdapter):
    async def read(self, scope, action, *, execution):
        return {"read": "allowed", "query": action.arguments["query"]}


async def _readable_unknown_original(factory):
    base, provider, _, binding = await setup_business(factory)
    registration = replace(
        base.registration,
        adapter_id="chotacrm",
        capabilities=base.registration.capabilities + (
            CapabilityDefinition(name="catalog.search", argument_model=CatalogSearch),
        ),
        adapter=ReadableFakeAdapter(provider, "LOOKUP"),
    )
    registry = BusinessRegistry((registration,))
    async with factory() as session:
        session.add(BusinessConnection(
            id=binding.connection_id,
            agent_id=base.grant.agent_id,
            owner_id=base.grant.agent_owner_id,
            workspace_id=uuid4(),
            workspace_name="Recovery test workspace",
            timezone="Europe/Minsk",
            grant_id=uuid4(),
            grant_revision=1,
            generation=2,
            enabled_capabilities=["booking.create", "catalog.search"],
            active=True,
        ))
        await session.commit()
    await base.repository.configure_binding(
        base.grant,
        adapter_id=registration.adapter_id,
        connection_id=binding.connection_id,
        catalog_revision=registration.catalog_revision,
        enabled_capabilities=["booking.create", "catalog.search"],
        authorization_generation=2,
        context_generation=2,
    )
    original = await open_business_runtime(base.request, base.grant, factory, registry)
    provider.lose_next = True
    result = await original.execute_plan(actions("12:00"))
    assert result.halt_status == "UNKNOWN"
    plan = await original.repository.selected_plan(original.request, original.grant)
    assert plan is not None and plan.operations[0].status == "UNKNOWN"
    return original, provider, registry, plan


class AmbiguousNoEffectOnceAdapter(FakeAdapter):
    async def execute(self, dispatch, *, execution):
        if self.provider.lose_next:
            self.provider.lose_next = False
            self.provider.posts += 1
            return Ambiguous()
        return await super().execute(dispatch, execution=execution)


@dataclass(frozen=True)
class FaultyLookupAdapter(FakeAdapter):
    failure: str = "timeout"

    async def lookup(self, command, *, execution):
        if self.failure == "timeout":
            raise TimeoutError
        if self.failure == "malformed":
            return {"status": "SUCCEEDED"}
        if self.failure == "foreign_receipt":
            binding = command.receipt_binding().model_copy(update={"operation_id": uuid4()})
            return ProvenSucceeded(
                binding=binding, receipt_id="foreign-receipt", object_id="foreign-object",
                evidence={"proof": "DURABLE_FAKE_OBJECT"},
            )
        return ProvenFailedNoEffect(
            binding=command.receipt_binding(), receipt_id="rejected-1",
            reason_code="DEFINITIVELY_REJECTED", evidence={"proof": "REJECTED_BEFORE_EFFECT"},
        )
