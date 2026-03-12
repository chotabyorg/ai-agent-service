"""Pydantic schemas for conversation sessions."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    """Payload for creating a conversation session."""

    agent_id: UUID
    external_client_id: str = Field(min_length=1, max_length=255)
    channel: str = Field(min_length=1, max_length=50)


class SessionResponse(BaseModel):
    """Serialized conversation session."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    external_client_id: str
    channel: str
    created_at: datetime
