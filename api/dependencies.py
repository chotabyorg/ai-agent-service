"""Reusable FastAPI dependencies."""

import secrets
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from db.database import get_db_session
from services.subscription_client import SubscriptionClient


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Provide an async database session for request handlers."""

    async for session in get_db_session():
        yield session


def get_subscription_client(request: Request) -> SubscriptionClient | None:
    """Return the subscription client stored on app.state, or ``None``."""

    return getattr(request.app.state, "subscription_client", None)


SubscriptionClientDep = Annotated[
    SubscriptionClient | None, Depends(get_subscription_client)
]


def verify_service_token(request: Request) -> None:
    """Validate the X-Service-Token header for service-to-service calls.

    Uses secrets.compare_digest to avoid timing attacks. Fail-closed: if the
    server-side INTERNAL_API_TOKEN is empty, every request is rejected.
    """

    expected = get_settings().internal_api_token or ""
    provided = request.headers.get("X-Service-Token") or ""

    if not expected or not provided or not secrets.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
        )
