"""Reusable FastAPI dependencies."""

import secrets
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.managed_security import OwnershipPrincipal, valid_token
from db.database import get_db_session
from schemas.internal import AgentOwnershipCheckRequest
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

    provided = _single_service_token(request)
    expected = get_settings().internal_api_token or ""
    try:
        expected_bytes = expected.encode("ascii")
    except UnicodeEncodeError:
        expected_bytes = b""
    if (provided is None or not expected_bytes
            or not secrets.compare_digest(expected_bytes, provided)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
        )


def _single_service_token(request: Request) -> bytes | None:
    """Return one syntactically safe service token with no competing auth header."""

    headers = request.scope.get("headers", ())
    service_tokens = [value for key, value in headers if key.lower() == b"x-service-token"]
    authorization_headers = [key for key, _ in headers if key.lower() == b"authorization"]
    if len(service_tokens) != 1 or authorization_headers:
        return None
    value = service_tokens[0]
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not text or any(ord(char) < 33 or ord(char) > 126 or char == "," for char in text):
        return None
    return value


def _unauthorized_service_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid service token",
    )


def authenticate_internal_ownership(request: Request) -> OwnershipPrincipal | None:
    """Authenticate an ownership principal, or the exact legacy token in transition."""

    settings = get_settings()
    supplied = _single_service_token(request)
    eligible = supplied is not None
    supplied = supplied or b""
    try:
        ownership_credential_shape = eligible and valid_token(supplied.decode("ascii"))
    except UnicodeDecodeError:
        ownership_credential_shape = False

    matches: list[tuple[OwnershipPrincipal, object]] = []
    # Compare every configured version so rotation and revocation do not create
    # an early-exit timing signal. Tokens in this registry are validated ASCII.
    for principal in settings.ownership_principals:
        for credential in principal.credentials():
            matched = secrets.compare_digest(
                supplied,
                credential.token.get_secret_value().encode("ascii"),
            )
            if ownership_credential_shape and matched:
                matches.append((principal, credential))

    if not eligible:
        raise _unauthorized_service_token()
    if len(matches) > 1:
        raise _unauthorized_service_token()
    if matches:
        # A recognized but revoked version never falls through to legacy auth
        # or another credential source.
        principal, credential = matches[0]
        if principal.revoked or credential.revoked:
            raise _unauthorized_service_token()
        return principal

    legacy = settings.internal_api_token or ""
    try:
        legacy_bytes = legacy.encode("ascii")
    except UnicodeEncodeError:
        legacy_bytes = b""
    if (not settings.internal_ownership_auth_required and legacy_bytes
            and secrets.compare_digest(legacy_bytes, supplied)):
        return None
    raise _unauthorized_service_token()


def authorize_agent_ownership(
    payload: AgentOwnershipCheckRequest,
    principal: OwnershipPrincipal | None = Depends(authenticate_internal_ownership),
) -> AgentOwnershipCheckRequest:
    """Authenticate and authorize the body before the endpoint opens a DB session."""

    if principal is not None and not principal.allows(payload.owner_id, payload.agent_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return payload
