"""Immutable, code-owned adapter catalog; production starts with no adapters."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, StrictInt, model_validator

from schemas.business import (Ambiguous, AttemptRejected, BusinessScopeValue, BusinessValue, DefinitiveNoEffect,
                              FrozenProviderCommand, ProviderDispatch, Hash, MaterialFacts, ProvenFailedNoEffect,
                              ProvenSucceeded, StillUnknown, SucceededReceipt, canonical_value,
                              fingerprint)


class ProviderProfile(BusinessValue):
    profile_revision: Hash
    mode: Literal["NATIVE", "LOOKUP", "BLIND"]
    dispatch_timeout_seconds: StrictInt = Field(ge=1, le=30, default=5)
    native_replay_policy: Literal["NONE", "BOUNDED", "DURABLE_TEST"] = "NONE"
    native_minimum_retention_seconds: StrictInt | None = Field(ge=1, le=315360000, default=None)
    test_only: bool = False
    proposal_ttl_seconds: StrictInt = Field(ge=1, le=3600, default=300)

    @model_validator(mode="after")
    def honest_profile(self):
        if self.mode != "NATIVE" and self.native_replay_policy != "NONE":
            raise ValueError("Non-native profile cannot replay")
        if self.mode == "NATIVE" and self.native_replay_policy == "NONE":
            raise ValueError("Native guarantee required")
        if self.native_replay_policy == "BOUNDED" and self.native_minimum_retention_seconds is None:
            raise ValueError("Native retention required")
        if self.native_replay_policy == "DURABLE_TEST" and not self.test_only:
            raise ValueError("Durable test guarantee requires test adapter")
        return self


class ExecutionContract(BusinessValue):
    """Code-owned audited effect ports. Missing guarantees grant no mutation."""
    expected_facts: Literal["UNAVAILABLE", "CONDITIONAL_EXPECTED_FACTS_V1"] = "UNAVAILABLE"
    bounded_acceptance: Literal["FENCED", "PROVIDER_ACCEPT_BEFORE_V1"] = "FENCED"

    @property
    def revision(self):
        return fingerprint("chota.business.execution-contract.v1", self)


class BusinessAdapter(Protocol):
    """Implementations are stateless/config-only; each call receives its owner."""

    async def resolve_facts(self, scope: BusinessScopeValue, actions: tuple, *, execution) -> MaterialFacts: ...
    async def execute(self, dispatch: ProviderDispatch, *, execution) -> SucceededReceipt | DefinitiveNoEffect | AttemptRejected | Ambiguous: ...
    async def lookup(self, command: FrozenProviderCommand, *, execution) -> ProvenSucceeded | ProvenFailedNoEffect | StillUnknown: ...
    async def receipt_read_allowed(self, scope: BusinessScopeValue, *, execution) -> bool: ...


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    argument_model: type[BaseModel]
    schema_version: int = 1
    requires_confirmation: bool = False
    public_fields: tuple[str, ...] = ()

    def __post_init__(self):
        from schemas.business import BusinessAction
        BusinessAction(capability=self.name, arguments={})
        if (self.argument_model.model_config.get("extra") != "forbid"
                or not 1 <= self.schema_version <= 9223372036854775807):
            raise ValueError("Closed capability schema required")

    def normalize(self, arguments: dict[str, Any]) -> dict:
        # Validate types before serializing: naive datetime/nonfinite Decimal
        # must not turn into an apparently normalized arbitrary string.
        return canonical_value(self.argument_model.model_validate(arguments))


@dataclass(frozen=True)
class AdapterRegistration:
    adapter_id: str
    profile: ProviderProfile
    capabilities: tuple[CapabilityDefinition, ...]
    adapter: BusinessAdapter
    execution_contract: ExecutionContract = ExecutionContract()
    retained_catalog_revisions: tuple[str, ...] = ()

    def __post_init__(self):
        import re
        if (not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.adapter_id)
                or not self.capabilities or len({c.name for c in self.capabilities}) != len(self.capabilities)):
            raise ValueError("Invalid business registration")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "retained_catalog_revisions", tuple(self.retained_catalog_revisions))
        if (any(value != self.legacy_catalog_revision for value in self.retained_catalog_revisions)
                or (self.retained_catalog_revisions and self.profile.native_replay_policy == "BOUNDED"
                    and self.execution_contract.bounded_acceptance != "FENCED")):
            raise ValueError("Exact conservative retained catalog required")

    @property
    def legacy_catalog_revision(self) -> str:
        return fingerprint("chota.business.catalog.v1", {
            "adapter_id": self.adapter_id, "profile": self.profile.model_dump(mode="json"),
            "capabilities": [{"name": c.name, "schema_version": c.schema_version,
                              "requires_confirmation": c.requires_confirmation,
                              "public_fields": list(c.public_fields),
                              "schema": c.argument_model.model_json_schema()} for c in self.capabilities],
        })

    @property
    def catalog_revision(self) -> str:
        return fingerprint("chota.business.catalog.v2", {
            "legacy_catalog_revision": self.legacy_catalog_revision,
            "execution_contract": self.execution_contract.model_dump(mode="json"),
        })

    def accepts_catalog(self, revision: str) -> bool:
        return revision == self.catalog_revision or revision in self.retained_catalog_revisions

    def capability(self, name: str) -> CapabilityDefinition | None:
        return next((c for c in self.capabilities if c.name == name), None)


class BusinessRegistry(Mapping[str, AdapterRegistration]):
    __slots__ = ("_entries",)

    def __init__(self, entries=()):
        values = tuple(entries.values()) if isinstance(entries, Mapping) else tuple(entries)
        if any(not isinstance(entry, AdapterRegistration) for entry in values):
            raise ValueError("Code-owned registrations required")
        if len({entry.adapter_id for entry in values}) != len(values):
            raise ValueError("Duplicate adapter")
        object.__setattr__(self, "_entries", MappingProxyType({entry.adapter_id: entry for entry in values}))

    def __setattr__(self, name, value):
        raise TypeError("Business registry is immutable")

    def __getitem__(self, key):
        return self._entries[key]

    def __iter__(self):
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)


EMPTY_BUSINESS_REGISTRY = BusinessRegistry()
