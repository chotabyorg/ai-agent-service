"""Internal (service-to-service) endpoints for agent management.

Secured by the X-Service-Token header. Not exposed through the public gateway.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import authorize_agent_ownership, get_db, verify_service_token
from schemas.internal import (
    AgentOwnershipCheckRequest,
    AgentCountResponse,
    DeactivateAllRequest,
    DeactivateAllResponse,
)
from services.agent_service import AgentService
from services.handoff_lifecycle import HandoffLifecycleConflict, HandoffLifecycleUnavailable


router = APIRouter(
    tags=["internal"],
)
agent_service = AgentService()
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/agents/ownership-check", status_code=status.HTTP_204_NO_CONTENT)
async def internal_check_agent_ownership(
    payload: Annotated[
        AgentOwnershipCheckRequest, Depends(authorize_agent_ownership)
    ],
    db: DbSession,
) -> Response:
    """Confirm that an agent belongs to the requested owner, without disclosing either."""
    agent = await agent_service.get_agent_by_id(
        db,
        payload.agent_id,
        user_id=payload.owner_id,
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agents/count", response_model=AgentCountResponse)
async def internal_count_agents(
    service_authorization: Annotated[None, Depends(verify_service_token)],
    db: DbSession,
    user_id: str = Query(..., description="User UUID"),
) -> AgentCountResponse:
    """Return total and active agent counts for the given user."""
    try:
        parsed_user_id = UUID(user_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id",
        ) from exc

    total, active = await agent_service.count_agents(db, parsed_user_id)
    return AgentCountResponse(total=total, active=active)


@router.post("/agents/deactivate-all", response_model=DeactivateAllResponse)
async def internal_deactivate_all_agents(
    service_authorization: Annotated[None, Depends(verify_service_token)],
    payload: DeactivateAllRequest,
    db: DbSession,
) -> DeactivateAllResponse:
    """Deactivate all active agents for a user. Returns the number updated."""
    try:
        deactivated = await agent_service.deactivate_all_agents(db, payload.user_id)
    except HandoffLifecycleConflict:
        raise HTTPException(status_code=409, detail="Agent lifecycle change conflicts with pending state") from None
    except HandoffLifecycleUnavailable:
        raise HTTPException(status_code=503, detail="Handoff grant revocation is unavailable") from None
    return DeactivateAllResponse(deactivated=deactivated)
