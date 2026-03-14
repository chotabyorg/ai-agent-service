"""Pydantic schemas for chat completions."""

from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Incoming chat completion payload."""

    agent_id: UUID
    external_client_id: str = Field(min_length=1, max_length=255)
    channel: str = Field(min_length=1, max_length=50)
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    """Outgoing chat completion payload."""

    session_id: UUID
    answer: str
