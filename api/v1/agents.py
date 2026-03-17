"""REST endpoints for agent configuration management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db
from schemas.agent import (
    AgentCreate,
    AgentCredentialsCreate,
    AgentCredentialsResponse,
    AgentResponse,
    AgentUpdate,
)
from services.agent_service import AgentService


router = APIRouter()
agent_service = AgentService()
DbSession = Annotated[AsyncSession, Depends(get_db)]
UserIdHeader = Annotated[UUID, Header(alias="x-user-id")]
UserRoleHeader = Annotated[str | None, Header(alias="x-user-role", default=None)]


@router.post("/", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(payload: AgentCreate, db: DbSession, user_id: UserIdHeader) -> AgentResponse:
    agent = await agent_service.create_agent(db, user_id, payload)
    return AgentResponse.model_validate(agent)


@router.get("/", response_model=list[AgentResponse])
async def list_agents(db: DbSession, user_id: UserIdHeader) -> list[AgentResponse]:
    agents = await agent_service.list_agents(db, user_id)
    return [AgentResponse.model_validate(agent) for agent in agents]


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: UUID, db: DbSession, user_id: UserIdHeader) -> AgentResponse:
    agent = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return AgentResponse.model_validate(agent)


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    payload: AgentUpdate,
    db: DbSession,
    user_id: UserIdHeader,
) -> AgentResponse:
    agent = await agent_service.update_agent(db, user_id, agent_id, payload)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return AgentResponse.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: UUID,
    db: DbSession,
    user_id: UserIdHeader,
    user_role: UserRoleHeader,
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
    credentials = await agent_service.upsert_credentials(db, user_id, agent_id, payload)
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return AgentCredentialsResponse.model_validate(credentials)
