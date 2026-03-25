"""Pydantic schemas for MCP tool management."""

from pydantic import BaseModel


class ToolInfo(BaseModel):
    name: str
    description: str
    enabled: bool


class UpdateEnabledToolsRequest(BaseModel):
    enabled_tools: list[str] | None = None
