"""Typed fake-provider seam for real managed PostgreSQL/resource regressions.

The fake authority below is deliberately memory-only and claims no process
restart proof. The separate Goal3 HTTP harness owns durable provider acceptance.
"""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

from pydantic import Field

import main
from core.config import get_settings
from schemas.business import (Ambiguous, BusinessValue, DefinitiveNoEffect,
                              MaterialFacts, ProvenSucceeded, StillUnknown,
                              SucceededReceipt)
from services.business_registry import (AdapterRegistration, BusinessRegistry,
    CapabilityDefinition, ProviderProfile, ExecutionContract)
from services.business_repository import BusinessRepository


class BookingArguments(BusinessValue):
    slot: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class BookingAdapter:
    authority: object

    async def resolve_facts(self, scope, actions, *, execution):
        self.authority.fact_calls += 1
        return MaterialFacts(values={"availability": "fixture"}, revisions={"catalog": 1})

    async def receipt_read_allowed(self, scope, *, execution):
        return True

    async def execute(self, dispatch, *, execution):
        command = dispatch.command
        assert command.facts == {"availability": "fixture"} and command.fact_revisions == {"catalog": 1}
        assert execution is not None
        assert asyncio.current_task().get_name() == "managed-turn-owner"
        self.authority.commands.append(command)
        receipt = SucceededReceipt(binding=command.receipt_binding(),
            receipt_id="receipt:" + str(command.operation_id),
            object_id="booking:" + str(command.operation_id), evidence={"proof": "created"})
        self.authority.receipts[command.operation_id] = receipt
        if self.authority.cleanup_error:
            async def fail_close():
                raise RuntimeError("managed-secret-sentinel")
            execution.stack.push_async_callback(fail_close)
        mode = self.authority.outcomes.get(command.arguments["slot"], self.authority.mode)
        if mode == "unknown":
            return Ambiguous()
        if mode == "failed":
            return DefinitiveNoEffect(binding=command.receipt_binding(),
                receipt_id=receipt.receipt_id, evidence={"proof": "rejected"}, reason_code="REJECTED")
        if mode == "raise":
            raise RuntimeError("managed-secret-sentinel")
        if mode == "block":
            self.authority.entered.set()
            await self.authority.release.wait()
        return receipt

    async def lookup(self, command, *, execution):
        receipt = self.authority.receipts.get(command.operation_id)
        return ProvenSucceeded(**receipt.model_dump()) if receipt else StillUnknown()


async def install_business(monkeypatch, value, db_session, *, confirmation=False, mode="success"):
    principal = next(p for p in get_settings().managed_principals if p.grants[0].agent_id == UUID(value["agent_id"]))
    grant = next(g for g in principal.grants if g.scope() == (
        value["owner_id"], UUID(value["integration_id"]), value["channel"], UUID(value["agent_id"])))
    authority = SimpleNamespace(commands=[], receipts={}, fact_calls=0, mode=mode, outcomes={},
        cleanup_error=False, entered=asyncio.Event(), release=asyncio.Event())
    capability = CapabilityDefinition(name="booking.create", argument_model=BookingArguments,
        requires_confirmation=confirmation, public_fields=("slot",))
    entry = AdapterRegistration(adapter_id="fake_booking", profile=ProviderProfile(
        profile_revision="a" * 64, mode="LOOKUP", dispatch_timeout_seconds=1, test_only=True),
        capabilities=(capability,), adapter=BookingAdapter(authority),
        execution_contract=ExecutionContract(expected_facts="CONDITIONAL_EXPECTED_FACTS_V1"))
    await BusinessRepository(db_session).configure_binding(grant, adapter_id=entry.adapter_id,
        connection_id=uuid4(), catalog_revision=entry.catalog_revision,
        enabled_capabilities=[capability.name], active=True)
    monkeypatch.setattr(main.app.state, "business_registry", BusinessRegistry((entry,)))
    return authority


def plan_arguments(slot="12:00"):
    return '{"actions":[{"capability":"booking.create","arguments":{"slot":"' + slot + '"}}]}'
