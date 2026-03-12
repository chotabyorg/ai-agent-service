"""Pydantic schemas for chat completions."""

from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Incoming chat completion payload."""

    session_id: UUID
    agent_id: UUID
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    """Outgoing chat completion payload."""

    answer: str
