"""REST endpoints for chat completions."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db
from schemas.chat import ChatRequest, ChatResponse
from services.llm_service import (
    AgentInactiveError,
    AgentNotFoundError,
    ChatProcessingError,
    LLMService,
    SessionNotFoundError,
)


router = APIRouter()
llm_service = LLMService()
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/completions", response_model=ChatResponse)
async def create_chat_completion(payload: ChatRequest, db: DbSession) -> ChatResponse:
    try:
        answer = await llm_service.process_chat_message(
            db_session=db,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            user_message=payload.message,
        )
    except AgentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AgentInactiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ChatProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ChatResponse(answer=answer)
