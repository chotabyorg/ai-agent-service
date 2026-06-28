"""Common validation schemas."""

from typing import Any

from pydantic import BaseModel, Field


class ToolInput(BaseModel):
    """Base model with strict unknown-field rejection."""

    model_config = {"extra": "forbid"}


class LeadInput(ToolInput):
    """Lead payload used by Google Workspace lead tools."""

    name: str | None = Field(default=None, max_length=255)
    company: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=64)
    telegram: str | None = Field(default=None, max_length=128)
    website: str | None = Field(default=None, max_length=512)
    source: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default="new", max_length=64)
    notes: str | None = Field(default=None, max_length=5000)
    lastContactedAt: str | None = Field(default=None, max_length=64)
    nextFollowUpAt: str | None = Field(default=None, max_length=64)
    owner: str | None = Field(default=None, max_length=255)
    customFields: dict[str, Any] = Field(default_factory=dict)


class LeadIdentifier(ToolInput):
    """Lead lookup fields used for lead status updates."""

    email: str | None = None
    phone: str | None = None
    telegram: str | None = None
    website: str | None = None
    customKey: str | None = None
    customValue: str | None = None
