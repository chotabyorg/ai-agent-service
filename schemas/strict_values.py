"""Strict JSON scalar types for new private versioned contracts."""

from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator


def _canonical_uuid(value):
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("UUID must be a canonical string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("UUID must be a canonical string") from None
    if str(parsed) != value:
        raise ValueError("UUID must be canonical lowercase form")
    return parsed


CanonicalUUID = Annotated[UUID, BeforeValidator(_canonical_uuid)]
