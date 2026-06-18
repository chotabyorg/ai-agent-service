"""REST endpoints for agent configuration management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import SubscriptionClientDep, get_db
from schemas.agent import (
    AgentCreate,
    AgentCredentialsCreate,
    AgentCredentialsResponse,
    AgentCredentialsSummary,
    AgentResponse,
    AgentUpdate,
    PromptConfigResponse,
    PromptPreviewResponse,
)
from schemas.prompt_config import PromptConfig
from models.agent import AgentTemplate
from services.agent_service import AgentService
from services.bitrix_webhook_validator import (
    BitrixCredentialsValidationStatus,
    validate_bitrix_credentials,
)
from services.google_workspace_validator import (
    GoogleCredentialsValidationStatus,
    validate_google_workspace_credentials,
)
from services.prompt_builder import ChatContext, PromptBuilder


def _build_agent_response(agent: AgentTemplate) -> AgentResponse:
    """Build AgentResponse with computed key fields."""
    has_key = agent.encrypted_openrouter_key is not None
    preview = None
    if has_key:
        try:
            from services.encryption_service import EncryptionService, mask_api_key

            enc = EncryptionService()
            preview = mask_api_key(enc.decrypt(agent.encrypted_openrouter_key))
        except Exception:
            preview = None
    response = AgentResponse.model_validate(agent)
    response.has_openrouter_key = has_key
    response.openrouter_key_preview = preview
    return response


router = APIRouter()
agent_service = AgentService()
DbSession = Annotated[AsyncSession, Depends(get_db)]
UserIdHeader = Annotated[UUID, Header(alias="x-user-id")]
UserRoleHeader = Annotated[str | None, Header(alias="x-user-role")]


@router.post("/", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(payload: AgentCreate, db: DbSession, user_id: UserIdHeader) -> AgentResponse:
    agent = await agent_service.create_agent(db, user_id, payload)
    return _build_agent_response(agent)


@router.get("/", response_model=list[AgentResponse])
async def list_agents(db: DbSession, user_id: UserIdHeader) -> list[AgentResponse]:
    agents = await agent_service.list_agents(db, user_id)
    return [_build_agent_response(agent) for agent in agents]


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: UUID, db: DbSession, user_id: UserIdHeader) -> AgentResponse:
    agent = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return _build_agent_response(agent)


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    payload: AgentUpdate,
    db: DbSession,
    user_id: UserIdHeader,
    subscription_client: SubscriptionClientDep = None,
) -> AgentResponse:
    if payload.is_active is True:
        # 1. Subscription check (before OpenRouter key check)
        if subscription_client is not None:
            check_result = await subscription_client.check(str(user_id))
            if not check_result.get("allowed"):
                raise HTTPException(
                    status_code=402,
                    detail=check_result.get("reason", "subscription_required"),
                )

        # 2. OpenRouter key check
        agent_check = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
        if agent_check is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found",
            )
        if not agent_check.encrypted_openrouter_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot activate agent without OpenRouter API key.",
            )
    agent = await agent_service.update_agent(db, user_id, agent_id, payload)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return _build_agent_response(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: UUID,
    db: DbSession,
    user_id: UserIdHeader,
    user_role: UserRoleHeader = None,
) -> None:
    result = await agent_service.delete_agent(db, agent_id, user_id, user_role)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    if result is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )


@router.get("/{agent_id}/prompt-config", response_model=PromptConfigResponse)
async def get_prompt_config(
    agent_id: UUID,
    db: DbSession,
    user_id: UserIdHeader,
) -> PromptConfigResponse:
    agent = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return PromptConfigResponse(prompt_config=agent.prompt_config)


@router.put("/{agent_id}/prompt-config", response_model=PromptConfigResponse)
async def update_prompt_config(
    agent_id: UUID,
    payload: dict,
    db: DbSession,
    user_id: UserIdHeader,
) -> PromptConfigResponse:
    try:
        PromptConfig(**payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(),
        ) from exc

    agent = await agent_service.update_prompt_config(db, user_id, agent_id, payload)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return PromptConfigResponse(prompt_config=agent.prompt_config)


@router.get("/{agent_id}/prompt-preview", response_model=PromptPreviewResponse)
async def get_prompt_preview(
    agent_id: UUID,
    db: DbSession,
    user_id: UserIdHeader,
    channel: str = Query(default="telegram"),
) -> PromptPreviewResponse:
    agent = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    prompt_builder = PromptBuilder()
    context = ChatContext(channel=channel, external_client_id="preview")
    rendered = prompt_builder.build(agent, context)
    return PromptPreviewResponse(rendered_prompt=rendered, channel=channel)


@router.get("/{agent_id}/credentials", response_model=list[AgentCredentialsSummary])
async def list_agent_credentials(
    agent_id: UUID,
    db: DbSession,
    user_id: UserIdHeader,
) -> list[AgentCredentialsSummary]:
    credentials = await agent_service.list_credentials(db, user_id, agent_id)
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return [
        AgentCredentialsSummary(agent_id=c.agent_id, service_name=c.service_name)
        for c in credentials
    ]


@router.delete(
    "/{agent_id}/credentials/{service_name}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_agent_credentials(
    agent_id: UUID,
    service_name: str,
    db: DbSession,
    user_id: UserIdHeader,
) -> None:
    result = await agent_service.delete_credentials(db, user_id, agent_id, service_name)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Credentials not found",
        )


@router.put(
    "/{agent_id}/credentials",
    response_model=AgentCredentialsResponse,
)
async def upsert_agent_credentials(
    agent_id: UUID,
    payload: AgentCredentialsCreate,
    db: DbSession,
    user_id: UserIdHeader,
) -> AgentCredentialsResponse:
    agent = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    if payload.service_name.strip().lower() == "bitrix":
        webhook_raw = payload.credentials.get("webhook_url")
        if not isinstance(webhook_raw, str) or not webhook_raw.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="webhook_url is required for Bitrix credentials.",
            )

        validation = await validate_bitrix_credentials(payload.credentials)
        if validation.status in {
            BitrixCredentialsValidationStatus.INVALID_FORMAT,
            BitrixCredentialsValidationStatus.INVALID_WEBHOOK,
            BitrixCredentialsValidationStatus.UNSAFE_HOST,
            BitrixCredentialsValidationStatus.INVALID_CREDENTIALS,
            BitrixCredentialsValidationStatus.ACCESS_DENIED,
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=validation.details or "Bitrix credentials are invalid.",
            )
        if validation.status in {
            BitrixCredentialsValidationStatus.RATE_LIMITED,
            BitrixCredentialsValidationStatus.UPSTREAM_ERROR,
        }:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=validation.details or "Could not validate Bitrix webhook due to upstream issue.",
            )

        payload.credentials = {
            **payload.credentials,
            **(validation.normalized_credentials or {}),
        }
        if not isinstance(payload.credentials.get("webhook_url"), str):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="webhook_url is required for Bitrix credentials.",
            )

    elif payload.service_name.strip().lower() == "google_workspace":
        validation = validate_google_workspace_credentials(payload.credentials)
        if validation.status in {
            GoogleCredentialsValidationStatus.MISSING_REQUIRED_FIELD,
            GoogleCredentialsValidationStatus.INVALID_SCOPES,
            GoogleCredentialsValidationStatus.INSUFFICIENT_SCOPES,
            GoogleCredentialsValidationStatus.INVALID_FIELD_TYPE,
            GoogleCredentialsValidationStatus.UNKNOWN_FIELD,
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=validation.details or "Google Workspace credentials are invalid.",
            )

        payload.credentials = {
            **payload.credentials,
            **(validation.normalized_credentials or {}),
        }

    credentials = await agent_service.upsert_credentials(db, user_id, agent_id, payload)
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return AgentCredentialsResponse.from_orm_credentials(credentials)
