"""Business logic for agent configuration management."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent import AgentCredentials, AgentTemplate
from schemas.agent import AgentCreate, AgentCredentialsCreate, AgentUpdate
from services.prompt_builder import PromptBuilder


class AgentService:
    """CRUD operations for agent templates and their credentials."""

    def __init__(self) -> None:
        self.prompt_builder = PromptBuilder()

    async def create_agent(
        self,
        session: AsyncSession,
        user_id: UUID,
        payload: AgentCreate,
    ) -> AgentTemplate:
        data = payload.model_dump()
        if payload.prompt_config:
            data.setdefault("system_prompt", "")
            agent = AgentTemplate(user_id=user_id, **data)
            agent.system_prompt = self.prompt_builder.build_static(agent)
        else:
            agent = AgentTemplate(user_id=user_id, **data)
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent

    async def list_agents(
        self,
        session: AsyncSession,
        user_id: UUID,
    ) -> Sequence[AgentTemplate]:
        result = await session.execute(
            select(AgentTemplate).where(AgentTemplate.user_id == user_id)
        )
        return result.scalars().all()

    async def get_agent_by_id(
        self,
        session: AsyncSession,
        agent_id: UUID,
        user_id: UUID | None = None,
    ) -> AgentTemplate | None:
        if user_id is None:
            return await session.get(AgentTemplate, agent_id)

        result = await session.execute(
            select(AgentTemplate).where(
                AgentTemplate.id == agent_id,
                AgentTemplate.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def update_agent(
        self,
        session: AsyncSession,
        user_id: UUID,
        agent_id: UUID,
        payload: AgentUpdate,
    ) -> AgentTemplate | None:
        agent = await self.get_agent_by_id(session, agent_id, user_id=user_id)
        if agent is None:
            return None

        updates = payload.model_dump(exclude_unset=True)
        for field_name, value in updates.items():
            setattr(agent, field_name, value)

        if "prompt_config" in updates and updates["prompt_config"] is not None:
            agent.system_prompt = self.prompt_builder.build_static(agent)

        await session.commit()
        await session.refresh(agent)
        return agent

    async def update_prompt_config(
        self,
        session: AsyncSession,
        user_id: UUID,
        agent_id: UUID,
        config_dict: dict,
    ) -> AgentTemplate | None:
        agent = await self.get_agent_by_id(session, agent_id, user_id=user_id)
        if agent is None:
            return None
        agent.prompt_config = config_dict
        agent.system_prompt = self.prompt_builder.build_static(agent)
        await session.commit()
        await session.refresh(agent)
        return agent

    async def upsert_credentials(
        self,
        session: AsyncSession,
        user_id: UUID,
        agent_id: UUID,
        payload: AgentCredentialsCreate,
    ) -> AgentCredentials | None:
        agent = await self.get_agent_by_id(session, agent_id, user_id=user_id)
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

    async def delete_agent(
        self,
        session: AsyncSession,
        agent_id: UUID,
        user_id: UUID,
        user_role: str | None = None,
    ) -> bool | None:
        """Delete agent. Returns None if not found, False if forbidden, True on success."""
        agent = await session.get(AgentTemplate, agent_id)
        if agent is None:
            return None
        if agent.user_id != user_id and user_role != "admin":
            return False
        await session.delete(agent)
        await session.commit()
        return True

    async def list_credentials(
        self,
        session: AsyncSession,
        user_id: UUID,
        agent_id: UUID,
    ) -> Sequence[AgentCredentials] | None:
        """List all credentials for an agent owned by user. Returns None if agent not found."""
        agent = await self.get_agent_by_id(session, agent_id, user_id=user_id)
        if agent is None:
            return None
        result = await session.execute(
            select(AgentCredentials).where(AgentCredentials.agent_id == agent_id)
        )
        return result.scalars().all()

    async def delete_credentials(
        self,
        session: AsyncSession,
        user_id: UUID,
        agent_id: UUID,
        service_name: str,
    ) -> bool | None:
        """Delete credentials. Returns None if not found, True on success."""
        agent = await self.get_agent_by_id(session, agent_id, user_id=user_id)
        if agent is None:
            return None
        credentials = await session.get(
            AgentCredentials,
            {"agent_id": agent_id, "service_name": service_name},
        )
        if credentials is None:
            return None
        await session.delete(credentials)
        await session.commit()
        return True

    async def get_credentials(
        self,
        session: AsyncSession,
        agent_id: UUID,
    ) -> Sequence[AgentCredentials]:
        result = await session.execute(
            select(AgentCredentials).where(AgentCredentials.agent_id == agent_id)
        )
        return result.scalars().all()

    async def count_agents(
        self,
        session: AsyncSession,
        user_id: UUID,
    ) -> tuple[int, int]:
        """Return (total, active) agent counts for the given user."""
        stmt = select(
            func.count(AgentTemplate.id),
            func.count(AgentTemplate.id).filter(AgentTemplate.is_active.is_(True)),
        ).where(AgentTemplate.user_id == user_id)
        total, active = (await session.execute(stmt)).one()
        return int(total or 0), int(active or 0)

    async def deactivate_all_agents(
        self,
        session: AsyncSession,
        user_id: UUID,
    ) -> int:
        """Deactivate all active agents for a user. Returns the number updated."""
        stmt = (
            update(AgentTemplate)
            .where(
                AgentTemplate.user_id == user_id,
                AgentTemplate.is_active.is_(True),
            )
            .values(is_active=False)
        )
        result = await session.execute(stmt)
        await session.commit()
        return int(result.rowcount or 0)
