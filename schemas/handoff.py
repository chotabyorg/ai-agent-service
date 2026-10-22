"""Strict internal contracts for the common text-channel lifecycle supplier."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from schemas.managed_turn import ManagedValue, validate_owner
from schemas.strict_values import CanonicalUUID

Generation = Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
TargetGeneration = Annotated[StrictInt, Field(ge=1, le=9223372036854775807)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
ContractVersion1 = Annotated[StrictInt, Field(ge=1, le=1)]
HandoffChannel = Literal["TELEGRAM", "WEBSITE_WIDGET"]


class HandoffScope(ManagedValue):
    owner_id: str
    integration_id: CanonicalUUID
    channel: HandoffChannel
    agent_id: CanonicalUUID

    _owner = field_validator("owner_id")(validate_owner)

    def key(self):
        return self.owner_id, self.integration_id, self.channel, self.agent_id


class AgentProofRequest(HandoffScope):
    contract_version: ContractVersion1


class AgentProofResponse(HandoffScope):
    contract_version: ContractVersion1 = 1
    agent_owner_id: CanonicalUUID
    agent_lifecycle_generation: Generation


class AgentRevokeRequest(HandoffScope):
    contract_version: ContractVersion1
    lifecycle_source: Literal["AI_AGENT"]
    command_id: CanonicalUUID
    agent_owner_id: CanonicalUUID
    source_agent_lifecycle_generation: Generation
    target_agent_lifecycle_generation: TargetGeneration

    @model_validator(mode="after")
    def contiguous(self):
        if self.target_agent_lifecycle_generation != self.source_agent_lifecycle_generation + 1:
            raise ValueError("Lifecycle generation must advance by one")
        return self


class AgentRevokeAcknowledgement(ManagedValue):
    model_config = ConfigDict(extra="forbid")

    contract_version: ContractVersion1
    lifecycle_source: Literal["AI_AGENT"]
    command_id: CanonicalUUID
    owner_id: str
    integration_id: CanonicalUUID
    channel: HandoffChannel
    agent_id: CanonicalUUID
    agent_owner_id: CanonicalUUID
    source_agent_lifecycle_generation: Generation
    target_agent_lifecycle_generation: TargetGeneration
    request_fingerprint: Digest
    grant_generation: Annotated[StrictInt, Field(ge=1, le=9223372036854775807)]
    active: StrictBool
    replayed: StrictBool

    _owner = field_validator("owner_id")(validate_owner)

    @model_validator(mode="after")
    def remains_revoked(self):
        if self.active is not False:
            raise ValueError("Revoke acknowledgement must remain inactive")
        return self

    @classmethod
    def from_request(cls, request, **values):
        fields = request.model_dump(include={
            "contract_version", "lifecycle_source", "command_id", "owner_id", "integration_id",
            "channel", "agent_id", "agent_owner_id", "source_agent_lifecycle_generation",
            "target_agent_lifecycle_generation",
        })
        return cls(**fields, **values)

