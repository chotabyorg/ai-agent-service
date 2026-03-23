"""REST endpoint for available LLM model discovery."""

from fastapi import APIRouter

from config.llm_models import AVAILABLE_MODELS
from schemas.llm_model import LLMModelResponse

router = APIRouter()


@router.get("/models", response_model=list[LLMModelResponse])
async def list_models() -> list[dict]:
    return AVAILABLE_MODELS
