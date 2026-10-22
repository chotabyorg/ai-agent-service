"""Workload admission for the explicitly selected owner routers only."""

import secrets
from uuid import UUID

from fastapi import HTTPException, Request

from core.config import get_settings
from core.managed_security import valid_token


def _deny() -> HTTPException:
    return HTTPException(401, detail="Invalid gateway authentication")


def _values(request: Request, name: bytes) -> list[bytes]:
    return [value for key, value in request.scope.get("headers", ()) if key.lower() == name]


def require_owner_gateway(request: Request) -> None:
    """Authenticate before DB dependencies, then validate unambiguous identity.

    Only absent credentials may take the explicit legacy transition. The
    existing endpoint ownership, admin, cookie and Origin checks remain final.
    """
    settings = get_settings()
    values = _values(request, b"x-gateway-token")
    subjects = _values(request, b"x-user-id")
    roles = _values(request, b"x-user-role")
    # Ambiguous identity has no compatibility contract, even before the
    # gateway credential rollout. Missing legacy fields retain endpoint errors.
    if len(subjects) > 1 or len(roles) > 1:
        raise _deny()
    if not values and settings.gateway_owner_auth_mode == "legacy":
        return

    supplied = values[0] if len(values) == 1 else b""
    try:
        eligible = len(values) == 1 and valid_token(supplied.decode("ascii"))
    except UnicodeDecodeError:
        eligible = False
    matches = []
    for principal in settings.gateway_principals:
        for credential in principal.credentials():
            matched = secrets.compare_digest(
                supplied, credential.token.get_secret_value().encode("ascii"),
            )
            if eligible and matched:
                matches.append((principal, credential))
    if len(matches) != 1:
        raise _deny()
    principal, credential = matches[0]
    if principal.revoked or credential.revoked:
        raise _deny()

    # Gateway owner routes do not forward bearer or service credentials. Do not
    # permit another authentication purpose to compete with this assertion.
    if _values(request, b"authorization") or _values(request, b"x-service-token"):
        raise _deny()
    if len(subjects) != 1:
        raise _deny()
    try:
        subject = subjects[0].decode("ascii")
        if not subject or any(char.isspace() or char == "," for char in subject):
            raise ValueError
        UUID(subject)
        if roles:
            role = roles[0].decode("ascii")
            # Empty/missing role retains its existing non-admin meaning. No
            # new role enumeration or privilege normalization is introduced.
            if any(ord(char) < 33 or ord(char) > 126 or char == "," for char in role):
                raise ValueError
    except (UnicodeDecodeError, ValueError, AttributeError):
        raise _deny() from None
