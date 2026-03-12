"""REST endpoints for agent configuration management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
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


@router.post("/", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(payload: AgentCreate, db: DbSession) -> AgentResponse:
    agent = await agent_service.create_agent(db, payload)
    return AgentResponse.model_validate(agent)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: UUID, db: DbSession) -> AgentResponse:
    agent = await agent_service.get_agent_by_id(db, agent_id)
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
) -> AgentResponse:
    agent = await agent_service.update_agent(db, agent_id, payload)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return AgentResponse.model_validate(agent)


@router.put(
    "/{agent_id}/credentials",
    response_model=AgentCredentialsResponse,
)
async def upsert_agent_credentials(
    agent_id: UUID,
    payload: AgentCredentialsCreate,
    db: DbSession,
) -> AgentCredentialsResponse:
    credentials = await agent_service.upsert_credentials(db, agent_id, payload)
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return AgentCredentialsResponse.model_validate(credentials)
