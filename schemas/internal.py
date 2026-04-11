"""Pydantic schemas for service-to-service /internal endpoints."""

from uuid import UUID

from pydantic import BaseModel, Field


class AgentCountResponse(BaseModel):
    """Counts of agents for a given user."""

    total: int = Field(ge=0)
    active: int = Field(ge=0)


class DeactivateAllRequest(BaseModel):
    """Request body for bulk deactivating all agents of a user."""

    user_id: UUID


class DeactivateAllResponse(BaseModel):
    """Number of agents that were deactivated."""

    deactivated: int = Field(ge=0)
