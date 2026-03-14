"""Pydantic schemas for MCP service discovery."""

from pydantic import BaseModel, Field


class MCPServiceResponse(BaseModel):
    """Serialized metadata for a supported MCP integration."""

    service_name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    required_keys: list[str]
