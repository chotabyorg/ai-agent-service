"""REST endpoints for per-agent OpenRouter API key management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db
from schemas.agent import (
    OpenRouterKeyInfo,
    SetOpenRouterKeyRequest,
    SetOpenRouterKeyResponse,
)
from services.agent_service import AgentService
from services.encryption_service import EncryptionService, mask_api_key
from services.handoff_lifecycle import HandoffLifecycleConflict, HandoffLifecycleUnavailable
from services.openrouter_validator import KeyValidationResult, validate_openrouter_key

router = APIRouter()
agent_service = AgentService()
encryption_service = EncryptionService()
DbSession = Annotated[AsyncSession, Depends(get_db)]
UserIdHeader = Annotated[UUID, Header(alias="x-user-id")]


@router.put("/{agent_id}/openrouter-key", response_model=SetOpenRouterKeyResponse)
async def set_openrouter_key(
    agent_id: UUID,
    payload: SetOpenRouterKeyRequest,
    db: DbSession,
    user_id: UserIdHeader,
) -> SetOpenRouterKeyResponse:
    agent = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    result = await validate_openrouter_key(payload.api_key)

    if result == KeyValidationResult.INVALID:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="OpenRouter API key is invalid. Please check your key and try again.")
    if result == KeyValidationResult.INSUFFICIENT_FUNDS:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Insufficient funds on OpenRouter key.")
    if result in (KeyValidationResult.RATE_LIMITED, KeyValidationResult.ERROR):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not validate key with OpenRouter.")

    agent.encrypted_openrouter_key = encryption_service.encrypt(payload.api_key)
    await db.commit()

    return SetOpenRouterKeyResponse(
        valid=True,
        key_preview=mask_api_key(payload.api_key),
    )


@router.get("/{agent_id}/openrouter-key", response_model=OpenRouterKeyInfo)
async def get_openrouter_key(
    agent_id: UUID,
    db: DbSession,
    user_id: UserIdHeader,
) -> OpenRouterKeyInfo:
    agent = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if not agent.encrypted_openrouter_key:
        return OpenRouterKeyInfo(has_key=False)

    plaintext = encryption_service.decrypt(agent.encrypted_openrouter_key)
    return OpenRouterKeyInfo(has_key=True, key_preview=mask_api_key(plaintext))


@router.delete("/{agent_id}/openrouter-key", status_code=status.HTTP_204_NO_CONTENT)
async def delete_openrouter_key(
    agent_id: UUID,
    db: DbSession,
    user_id: UserIdHeader,
) -> None:
    # The lifecycle transition keeps the agent pending while Conversation
    # revokes retained grants. It clears the key in the same final commit that
    # marks the agent inactive, so activation cannot win between those writes.
    try:
        agent = await agent_service.remove_openrouter_key(db, agent_id, user_id)
    except HandoffLifecycleConflict:
        raise HTTPException(status_code=409, detail="Agent lifecycle change conflicts with pending state") from None
    except HandoffLifecycleUnavailable:
        raise HTTPException(status_code=503, detail="Handoff grant revocation is unavailable") from None
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
