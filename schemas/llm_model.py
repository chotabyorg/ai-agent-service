"""Pydantic schemas for LLM model discovery."""

from pydantic import BaseModel


class LLMModelResponse(BaseModel):
    """Available LLM model metadata."""

    display_name: str
    model_id: str
