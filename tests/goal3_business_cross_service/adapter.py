"""Code-owned stateless HTTP fake adapter; only the provider is replaced."""

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, ConfigDict, Field

from schemas.business import (Ambiguous, AttemptRejected, DefinitiveNoEffect, MaterialFacts,
                              ProvenFailedNoEffect, ProvenSucceeded, StillUnknown,
                              SucceededReceipt)
from services.business_registry import (AdapterRegistration, BusinessRegistry, ExecutionContract,
                                       CapabilityDefinition, ProviderProfile)
from services.managed_http_cleanup import close_managed_helper, own_managed_http


class BookingArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class HTTPBookingAdapter:
    base_url: str
    timeout: int = 1

    async def request(self, method, path, execution, body=None):
        client = own_managed_http(httpx.AsyncClient(
            timeout=self.timeout, trust_env=False, follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False)))
        # Resource lifetime belongs to this actual managed owner. No shared client,
        # request state, detached cleanup task or fake transport is used.
        execution.stack.push_async_callback(close_managed_helper, client)
        try:
            response = await execution.operation(client.request(
                method, self.base_url + path, json=body))
            response.raise_for_status()
            return response.json()
        finally:
            await close_managed_helper(client)

    async def resolve_facts(self, scope, actions, *, execution):
        return MaterialFacts.model_validate(await self.request("GET", "/facts", execution))

    async def receipt_read_allowed(self, scope, *, execution):
        return True

    async def execute(self, dispatch, *, execution):
        if dispatch.accept_before is not None and datetime.now(timezone.utc) >= dispatch.accept_before:
            return AttemptRejected(binding=dispatch.command.receipt_binding(), attempt=dispatch.attempt,
                                   reason_code="LOCAL_ACCEPTANCE_EXPIRED")
        try:
            value = await self.request("POST", "/execute", execution,
                                       dispatch.model_dump(mode="json"))
            kind = value.get("kind")
            if kind == "SUCCEEDED":
                return SucceededReceipt.model_validate(value)
            if kind == "FAILED_NO_EFFECT":
                return DefinitiveNoEffect.model_validate(value)
            if kind == "ATTEMPT_REJECTED":
                return AttemptRejected.model_validate(value)
            return Ambiguous()
        except httpx.HTTPError:
            return Ambiguous()

    async def lookup(self, command, *, execution):
        try:
            value = await self.request("GET", "/operations/" + str(command.operation_id), execution)
            if value.get("kind") == "SUCCEEDED":
                return ProvenSucceeded.model_validate(value)
            if value.get("kind") == "FAILED_NO_EFFECT":
                return ProvenFailedNoEffect.model_validate(value)
            return StillUnknown()
        except httpx.HTTPError:
            return StillUnknown()


def registry(base_url, *, native_policy="DURABLE_TEST", horizon=None, timeout=1, proposal_ttl=300,
             bounded_acceptance="PROVIDER_ACCEPT_BEFORE_V1", retained_legacy=False):
    values = []
    for mode in ("LOOKUP", "BLIND", "NATIVE"):
        profile = ProviderProfile(
            profile_revision=hashlib.sha256((mode + native_policy + str(horizon) + str(proposal_ttl)).encode()).hexdigest(),
            mode=mode, dispatch_timeout_seconds=timeout, test_only=True,
            native_replay_policy=native_policy if mode == "NATIVE" else "NONE",
            native_minimum_retention_seconds=horizon if mode == "NATIVE" else None,
            proposal_ttl_seconds=proposal_ttl,
        )
        entry = AdapterRegistration(
            adapter_id="fake_" + mode.lower(), profile=profile,
            capabilities=(CapabilityDefinition("booking.create", BookingArguments,
                                               public_fields=("slot",)),
                          CapabilityDefinition("booking.confirm_create", BookingArguments,
                                               requires_confirmation=True, public_fields=("slot",))),
            adapter=HTTPBookingAdapter(base_url, timeout),
            execution_contract=ExecutionContract(expected_facts="CONDITIONAL_EXPECTED_FACTS_V1",
                                                 bounded_acceptance=bounded_acceptance),
        )
        if retained_legacy:
            entry = replace(entry, retained_catalog_revisions=(entry.legacy_catalog_revision,))
        values.append(entry)
    return BusinessRegistry(tuple(values))
