"""REST endpoints for chat completions."""

import hmac
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from api.dependencies import get_db
from core.config import get_settings
from schemas.chat import ChatRequest, ChatResponse
from services.chat_diagnostics import (
    ChatDiagnosticContext,
    bind_chat_diagnostic_context,
    reset_chat_diagnostic_context,
)
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


logger = logging.getLogger(__name__)


def _normalized_exception_type(exc: Exception) -> str:
    """Return only a bounded Python class name, never exception details."""

    name = type(exc).__name__
    if name.isascii() and name.isidentifier() and len(name) <= 64:
        return name
    return "Exception"


class ChatDiagnosticRoute(APIRoute):
    """Add safe diagnostics while preserving the framework's plain-text 500."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def diagnostic_handler(request: Request) -> Response:
            diagnostics = ChatDiagnosticContext()
            context_token = bind_chat_diagnostic_context(diagnostics)
            try:
                return await original_handler(request)
            except HTTPException as exc:
                if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
                    logger.error(
                        "chat_processing_failed failure_stage=%s exception_type=%s status_code=%d",
                        diagnostics.failure_stage.value,
                        _normalized_exception_type(exc),
                        exc.status_code,
                    )
                raise
            except RequestValidationError:
                raise
            except Exception as exc:
                logger.error(
                    "chat_processing_failed failure_stage=%s exception_type=%s",
                    diagnostics.failure_stage.value,
                    _normalized_exception_type(exc),
                )
                return PlainTextResponse(
                    "Internal Server Error",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            finally:
                reset_chat_diagnostic_context(context_token)

        return diagnostic_handler


router = APIRouter(route_class=ChatDiagnosticRoute)
llm_service = LLMService()
DbSession = Annotated[AsyncSession, Depends(get_db)]


async def authenticate_chat_caller(
    x_service_token: Annotated[str | None, Header(alias="X-Service-Token")] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> UUID | None:
    """Authenticate a chat caller and parse its trusted owner assertion.

    Compatibility mode allows old callers to omit both headers while the
    deployment is rolled out. Any supplied token must still match the
    dedicated chat credential. Production enables strict mode after callers
    and secrets have been deployed.
    """
    settings = get_settings()
    expected_token = settings.chat_s2s_token.get_secret_value()
    token_is_valid = bool(
        expected_token
        and x_service_token
        and hmac.compare_digest(
            x_service_token.encode("utf-8"),
            expected_token.encode("utf-8"),
        )
    )

    if x_service_token and not token_is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
        )
    if settings.chat_s2s_auth_required and not token_is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
        )
    if settings.chat_s2s_auth_required and not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing user identity",
        )
    if not x_user_id:
        return None

    try:
        return UUID(x_user_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user identity",
        ) from exc


@router.post("/completions", response_model=ChatResponse)
async def create_chat_completion(
    payload: ChatRequest,
    user_id: Annotated[UUID | None, Depends(authenticate_chat_caller)],
    db: DbSession,
) -> ChatResponse:
    try:
        result = await llm_service.process_chat_message(
            db_session=db,
            agent_id=payload.agent_id,
            external_client_id=payload.external_client_id,
            channel=payload.channel,
            user_message=payload.message,
            user_id=user_id,
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
