"""Voice-only cache hint, without messages, turn claims or execution authority."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt, field_validator

from schemas.managed_turn import ManagedValue, validate_owner


class VoiceWarmupRequest(ManagedValue):
    contract_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    owner_id: str
    integration_id: UUID
    channel: Literal["VOICE"] = "VOICE"
    agent_id: UUID
    call_id: UUID

    _owner = field_validator("owner_id")(validate_owner)


class VoiceWarmupResult(ManagedValue):
    status: Literal["WARMED", "SKIPPED", "FAILED"]
    elapsed_ms: Annotated[int, Field(ge=0, le=30000)] = 0
    cached_tokens: Annotated[int, Field(ge=0, le=1048576)] = 0
