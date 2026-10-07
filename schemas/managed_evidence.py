"""Bounded retained-effect references; no execution or provider authority."""

from typing import Annotated
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from schemas.managed_turn import Channel, ManagedValue, OperationReference, validate_owner

MAX_EVIDENCE_REQUEST_BYTES = 4096


class ManagedEvidenceRequest(ManagedValue):
    contract_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    owner_id: str
    integration_id: UUID
    channel: Channel
    agent_id: UUID
    conversation_id: UUID
    turn_id: UUID

    _owner = field_validator("owner_id")(validate_owner)


class ManagedEvidenceResult(ManagedValue):
    contract_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    conversation_id: UUID
    turn_id: UUID
    agent_id: UUID
    operation_refs: Annotated[list[OperationReference], Field(max_length=32)]

    @model_validator(mode="after")
    def canonical_references(self):
        keys = [(ref.origin, ref.operation_id) for ref in self.operation_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate operation reference")
        self.operation_refs.sort(key=lambda ref: (ref.origin, ref.operation_id))
        return self
