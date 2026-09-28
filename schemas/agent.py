"""Pydantic schemas for agent management."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentCreate(BaseModel):
    """Payload for creating a new agent template."""

    name: str = Field(min_length=1, max_length=255)
    system_prompt: str | None = Field(default=None, min_length=1)
    prompt_config: dict | None = None
    llm_provider: str = Field(default="openrouter", min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=255)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    is_active: bool = False
    timezone: str = "Europe/Minsk"

    @model_validator(mode="after")
    def require_prompt_source(self) -> "AgentCreate":
        if not self.prompt_config and not self.system_prompt:
            raise ValueError("Either prompt_config or system_prompt must be provided.")
        return self


class AgentUpdate(BaseModel):
    """Payload for partially updating an agent template."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    system_prompt: str | None = Field(default=None, min_length=1)
    prompt_config: dict | None = None
    llm_provider: str | None = Field(default=None, min_length=1, max_length=64)
    model_name: str | None = Field(default=None, min_length=1, max_length=255)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    is_active: bool | None = None
    timezone: str | None = None


class AgentResponse(BaseModel):
    """Serialized agent template."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    name: str
    system_prompt: str
    prompt_config: dict | None = None
    llm_provider: str
    model_name: str
    temperature: float
    is_active: bool
    timezone: str
    has_openrouter_key: bool = False
    openrouter_key_preview: str | None = None


class SetOpenRouterKeyRequest(BaseModel):
    """Payload for setting an agent's OpenRouter API key."""

    api_key: str = Field(min_length=1)


class OpenRouterKeyInfo(BaseModel):
    """Info about an agent's OpenRouter key (no secrets)."""

    has_key: bool
    key_preview: str | None = None


class SetOpenRouterKeyResponse(BaseModel):
    """Response after successfully setting a key."""

    valid: bool
    key_preview: str


class PromptConfigResponse(BaseModel):
    """Serialized prompt_config of an agent."""

    prompt_config: dict | None = None


class PromptPreviewResponse(BaseModel):
    """Rendered system prompt preview."""

    rendered_prompt: str
    channel: str


class AgentCredentialsCreate(BaseModel):
    """Payload for creating or updating MCP credentials."""

    service_name: str = Field(min_length=1, max_length=100)
    credentials: dict[str, Any]


class AgentCredentialsSummary(BaseModel):
    """Safe summary of agent credentials — no secrets exposed."""

    model_config = ConfigDict(from_attributes=True)

    agent_id: UUID
    service_name: str
    has_credentials: bool = True


class AgentCredentialsResponse(BaseModel):
    """Serialized agent credentials (safe summary, no raw secrets)."""

    model_config = ConfigDict(from_attributes=True)

    agent_id: UUID
    service_name: str
    has_credentials: bool = True
    credential_keys: list[str] = Field(default_factory=list)

    @classmethod
    def from_orm_credentials(cls, credentials) -> "AgentCredentialsResponse":
        from services.credentials_cipher import decrypt_credentials

        plaintext = decrypt_credentials(credentials.credentials)
        return cls(
            agent_id=credentials.agent_id,
            service_name=credentials.service_name,
            has_credentials=True,
            credential_keys=list(plaintext.keys()),
        )
