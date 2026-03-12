"""Business logic for agent configuration management."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent import AgentCredentials, AgentTemplate
from schemas.agent import AgentCreate, AgentCredentialsCreate, AgentUpdate


class AgentService:
    """CRUD operations for agent templates and their credentials."""

    async def create_agent(self, session: AsyncSession, payload: AgentCreate) -> AgentTemplate:
        agent = AgentTemplate(**payload.model_dump())
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent

    async def get_agent_by_id(self, session: AsyncSession, agent_id: UUID) -> AgentTemplate | None:
        return await session.get(AgentTemplate, agent_id)

    async def update_agent(
        self,
        session: AsyncSession,
        agent_id: UUID,
        payload: AgentUpdate,
    ) -> AgentTemplate | None:
        agent = await self.get_agent_by_id(session, agent_id)
        if agent is None:
            return None

        for field_name, value in payload.model_dump(exclude_unset=True).items():
            setattr(agent, field_name, value)

        await session.commit()
        await session.refresh(agent)
        return agent

    async def upsert_credentials(
        self,
        session: AsyncSession,
        agent_id: UUID,
        payload: AgentCredentialsCreate,
    ) -> AgentCredentials | None:
        agent = await self.get_agent_by_id(session, agent_id)
        if agent is None:
            return None

        credentials = await session.get(
            AgentCredentials,
            {"agent_id": agent_id, "service_name": payload.service_name},
        )

        if credentials is None:
            credentials = AgentCredentials(agent_id=agent_id, **payload.model_dump())
            session.add(credentials)
        else:
            credentials.credentials = payload.credentials

        await session.commit()
        await session.refresh(credentials)
        return credentials

    async def get_credentials(
        self,
        session: AsyncSession,
        agent_id: UUID,
    ) -> Sequence[AgentCredentials]:
        result = await session.execute(
            select(AgentCredentials).where(AgentCredentials.agent_id == agent_id)
        )
        return result.scalars().all()
