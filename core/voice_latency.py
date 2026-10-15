"""Operator-selected Voice scope; this config never grants execution authority."""
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.managed_turn import ManagedTurnRequest, validate_owner


class VoiceLatencyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: str
    integration_id: UUID
    agent_id: UUID
    model: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")

    _owner = field_validator("owner_id")(validate_owner)

    def matches(self, request: ManagedTurnRequest | None, model: str) -> bool:
        return (type(request) is ManagedTurnRequest and request.channel == "VOICE"
                and (request.owner_id, request.integration_id, request.agent_id, model)
                == (self.owner_id, self.integration_id, self.agent_id, self.model))
