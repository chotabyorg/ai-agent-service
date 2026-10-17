"""Operator-selected Voice scope; this config never grants execution authority."""
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemas.managed_turn import ManagedTurnRequest, validate_owner

ModelName = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")]
ProviderName = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")]


class VoiceLatencyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: str
    integration_id: UUID
    agent_id: UUID
    # Match the stored agent model; never mutate the shared agent/template.
    model: ModelName
    completion_model: ModelName | None = None
    provider_only: tuple[ProviderName, ...] = Field(default=(), max_length=8)
    max_input_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_output_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    reuse_signing_key: bool = False
    concise_speech: bool = False

    _owner = field_validator("owner_id")(validate_owner)

    @model_validator(mode="after")
    def validate_model_route(self):
        routing = bool(self.provider_only) and self.max_input_price is not None and self.max_output_price is not None
        if self.completion_model is not None:
            if not routing or len(set(self.provider_only)) != len(self.provider_only):
                raise ValueError("Voice model replacement requires unique providers and both price ceilings")
        elif self.provider_only or self.max_input_price is not None or self.max_output_price is not None:
            raise ValueError("Voice routing fields require an explicit replacement model")
        return self

    def extra_body(self):
        if self.completion_model is None:
            return {"reasoning": {"effort": "none"}, "provider": {"sort": "latency"}}
        # Allowlist and price ceilings are the hard boundary; inside them the
        # router picks the currently fastest provider (latency-sorted) and may
        # fall back within the same allowlist on an error. No preference order,
        # so nextbit/makora move with live latency instead of a fixed pin.
        return {"reasoning": {"enabled": False}, "provider": {
            "only": list(self.provider_only), "sort": "latency", "allow_fallbacks": True,
            "max_price": {"prompt": self.max_input_price, "completion": self.max_output_price},
        }}

    def matches(self, request: ManagedTurnRequest | None, model: str) -> bool:
        return (type(request) is ManagedTurnRequest and request.channel == "VOICE"
                and (request.owner_id, request.integration_id, request.agent_id, model)
                == (self.owner_id, self.integration_id, self.agent_id, self.model))
