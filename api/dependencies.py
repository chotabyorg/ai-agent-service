"""Reusable FastAPI dependencies."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_db_session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Provide an async database session for request handlers."""

    async for session in get_db_session():
        yield session
