"""Business logic for conversation sessions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.session import ConversationSession
from schemas.session import SessionCreate


class SessionService:
    """CRUD operations for conversation sessions."""

    async def create_session(
        self,
        session: AsyncSession,
        payload: SessionCreate,
    ) -> ConversationSession:
        conversation_session = ConversationSession(**payload.model_dump())
        session.add(conversation_session)
        await session.commit()
        await session.refresh(conversation_session)
        return conversation_session

    async def get_session_by_id(
        self,
        session: AsyncSession,
        session_id: UUID,
    ) -> ConversationSession | None:
        return await session.get(ConversationSession, session_id)

    async def get_or_create_session(
        self,
        session: AsyncSession,
        agent_id: UUID,
        external_client_id: str,
        channel: str,
    ) -> ConversationSession:
        result = await session.execute(
            select(ConversationSession).where(
                ConversationSession.agent_id == agent_id,
                ConversationSession.external_client_id == external_client_id,
                ConversationSession.channel == channel,
            )
        )
        conversation_session = result.scalar_one_or_none()
        if conversation_session is not None:
            return conversation_session

        conversation_session = ConversationSession(
            agent_id=agent_id,
            external_client_id=external_client_id,
            channel=channel,
        )
        session.add(conversation_session)
        await session.commit()
        await session.refresh(conversation_session)
        return conversation_session
