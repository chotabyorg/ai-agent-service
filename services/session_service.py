"""Business logic for conversation sessions."""

from uuid import UUID

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
