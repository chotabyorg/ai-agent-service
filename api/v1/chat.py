"""REST endpoints for chat completions."""

import hmac
import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

try:
    from mcp.shared.exceptions import MCPError as _MCPError
except ImportError:
    from mcp.shared.exceptions import McpError as _MCPError

from api.dependencies import get_db
from core.config import get_settings
from schemas.chat import ChatRequest, ChatResponse
from services.chat_diagnostics import (
    ChatDiagnosticContext,
    bind_chat_diagnostic_context,
    reset_chat_diagnostic_context,
)
from business_capabilities.context import BusinessRuntimeError
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

_MCP_ERROR_CODE_MIN = -(2**31)
_MCP_ERROR_CODE_MAX = 2**31 - 1


def _normalized_exception_type(exc: Exception) -> str:
    """Return only a bounded Python class name, never exception details."""

    name = type(exc).__name__
    if name.isascii() and name.isidentifier() and len(name) <= 64:
        return name
    return "Exception"


def _mcp_error_code(exc: Exception) -> int | None:
    """Extract only a bounded integer code from a recognized MCP exception."""

    if not isinstance(exc, _MCPError):
        return None

    try:
        error_data = getattr(exc, "error", None)
        code = getattr(error_data, "code", None) if error_data is not None else None
        if code is None:
            code = getattr(exc, "code", None)
    except Exception:
        return None

    if type(code) is not int or not _MCP_ERROR_CODE_MIN <= code <= _MCP_ERROR_CODE_MAX:
        return None
    return code


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
                mcp_error_code = _mcp_error_code(exc)
                if mcp_error_code is None:
                    logger.error(
                        "chat_processing_failed failure_stage=%s exception_type=%s",
                        diagnostics.failure_stage.value,
                        _normalized_exception_type(exc),
                    )
                else:
                    logger.error(
                        "chat_processing_failed failure_stage=%s exception_type=%s mcp_error_code=%d",
                        diagnostics.failure_stage.value,
                        _normalized_exception_type(exc),
                        mcp_error_code,
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


def authenticated_namespace(settings, supplied):
    if not supplied:
        return None
    try:
        callers = json.loads(getattr(settings, 'chat_s2s_callers_json', None).get_secret_value()) if getattr(settings, 'chat_s2s_callers_json', None) else {}
    except (ValueError, TypeError):
        return None
    if not isinstance(callers, dict):
        return None
    for name, token in callers.items():
        if isinstance(name, str) and 1 <= len(name) <= 100 and isinstance(token, str) and token and hmac.compare_digest(supplied.encode(), token.encode()):
            return name
    token = settings.chat_s2s_token.get_secret_value()
    if token and hmac.compare_digest(supplied.encode(), token.encode()):
        return 'shared-s2s'
    return None


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
    token_is_valid = authenticated_namespace(settings, x_service_token) is not None

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
    request: Request,
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
            caller_namespace=authenticated_namespace(get_settings(), request.headers.get('X-Service-Token')),
            event_id=payload.event_id,
        )
    except BusinessRuntimeError as exc:
        code = 403 if exc.code in {'PERMISSION_DENIED', 'STALE_BINDING', 'CONNECTION_REVOKED'} else 409 if exc.code in {'OPERATION_IN_PROGRESS', 'IDEMPOTENCY_CONFLICT', 'OPERATION_EXPIRED'} else 503
        raise HTTPException(code, {'code': exc.code, 'message': 'Business tools are temporarily unavailable.'}) from None
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
