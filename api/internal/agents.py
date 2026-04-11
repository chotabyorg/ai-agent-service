"""Internal (service-to-service) endpoints for agent management.

Secured by the X-Service-Token header. Not exposed through the public gateway.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, verify_service_token
from schemas.internal import (
    AgentCountResponse,
    DeactivateAllRequest,
    DeactivateAllResponse,
)
from services.agent_service import AgentService


router = APIRouter(
    tags=["internal"],
    dependencies=[Depends(verify_service_token)],
)
agent_service = AgentService()
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("/agents/count", response_model=AgentCountResponse)
async def internal_count_agents(
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
    payload: DeactivateAllRequest,
    db: DbSession,
) -> DeactivateAllResponse:
    """Deactivate all active agents for a user. Returns the number updated."""
    deactivated = await agent_service.deactivate_all_agents(db, payload.user_id)
    return DeactivateAllResponse(deactivated=deactivated)
