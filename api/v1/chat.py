"""REST endpoints for chat completions."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db
from schemas.chat import ChatRequest, ChatResponse
from services.llm_service import (
    AgentApiKeyInvalidError,
    AgentInactiveError,
    AgentInsufficientFundsError,
    AgentNoApiKeyError,
    AgentNotFoundError,
    AgentRateLimitError,
    ChatProcessingError,
    LLMService,
)


router = APIRouter()
llm_service = LLMService()
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/completions", response_model=ChatResponse)
async def create_chat_completion(payload: ChatRequest, db: DbSession) -> ChatResponse:
    try:
        result = await llm_service.process_chat_message(
            db_session=db,
            agent_id=payload.agent_id,
            external_client_id=payload.external_client_id,
            channel=payload.channel,
            user_message=payload.message,
        )
    except AgentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AgentInactiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except AgentNoApiKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except AgentApiKeyInvalidError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except AgentInsufficientFundsError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        ) from exc
    except AgentRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
        ) from exc
    except ChatProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return ChatResponse(session_id=result.session_id, answer=result.answer)
