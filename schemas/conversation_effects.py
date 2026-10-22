"""Private, business-semantics-free Conversation authority sidecar DTOs."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, field_validator

from core.managed_security import EXECUTION_NAMESPACE
from schemas.managed_turn import (
    Channel,
    ManagedTurnRequest,
    ManagedValue,
    DEFAULT_MAX_PARTIAL_CHARS,
    DEFAULT_STREAM_PARTIALS,
    STREAM_EXTENSION_FIELDS,
    canonical_managed_request_fields,
    managed_request_fields_with_extensions,
    validate_owner,
)

Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
BusinessEffectNamespace = Literal["main-chota.ai-business.v1"]


class EffectReason(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    AUTHORITY_CHANGED = "AUTHORITY_CHANGED"
    EXECUTION_INACTIVE = "EXECUTION_INACTIVE"
    INPUT_BEFORE_AUTHORITY = "INPUT_BEFORE_AUTHORITY"
    INPUT_INCOMPLETE = "INPUT_INCOMPLETE"
    INPUT_SUPERSEDED = "INPUT_SUPERSEDED"
    ORIGIN_STALE = "ORIGIN_STALE"
    ORIGIN_INCOMPLETE = "ORIGIN_INCOMPLETE"
    INTERVENING_SPLIT = "INTERVENING_SPLIT"


class ExternalEffectContext(ManagedValue):
    contract_version: Literal[1]
    conversation_id: UUID
    turn_id: UUID
    origin_turn_id: UUID
    owner_id: str
    integration_id: UUID
    channel: Channel
    agent_id: UUID
    authority_revision: Revision
    managed_request_fingerprint: Digest

    _owner = field_validator("owner_id")(validate_owner)

    @field_validator("contract_version", mode="before")
    @classmethod
    def strict_contract_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("contract_version must be integer 1")
        return value


class ExternalEffectEligibilityRequest(ExternalEffectContext):
    pass


class ExternalEffectPermitRequest(ExternalEffectContext):
    operation_namespace: BusinessEffectNamespace
    operation_id: UUID
    operation_fingerprint: Digest


class ExternalEffectEligibilityResult(ManagedValue):
    contract_version: Literal[1]
    request_fingerprint: Digest
    eligible: StrictBool
    reason: EffectReason
    authority_revision: Revision
    authority_input_floor_sequence: Revision
    split_input_floor_sequence: Revision

    @field_validator("contract_version", mode="before")
    @classmethod
    def strict_contract_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("contract_version must be integer 1")
        return value


class ExternalEffectPermitResult(ManagedValue):
    contract_version: Literal[1]
    request_fingerprint: Digest
    permit_id: UUID
    replayed: StrictBool

    @field_validator("contract_version", mode="before")
    @classmethod
    def strict_contract_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("contract_version must be integer 1")
        return value


def sidecar_fingerprint(request: ManagedValue) -> str:
    """Conversation's canonical request hash for the complete sidecar DTO."""
    identity = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def managed_request_fingerprint(request: ManagedTurnRequest) -> str:
    """Match Conversation's selected canonical managed request profile."""
    return _managed_fields_fingerprint(canonical_managed_request_fields(request))


def _managed_fields_fingerprint(fields: dict) -> str:
    identity = json.dumps(
        {"execution_namespace": EXECUTION_NAMESPACE,
         "request": fields},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def retained_managed_fingerprint_matches(stored: str | None, request: ManagedTurnRequest) -> bool:
    """Recognize a retained acquisition, never change a fresh sidecar request.

    Only omitted-both-field requests may match the historical defaulted profile.
    The repository must still validate the complete original permit-request tuple;
    Conversation independently decides whether that exact request has authority.
    """
    if stored == managed_request_fingerprint(request):
        return True
    if any(name in request.model_fields_set for name in STREAM_EXTENSION_FIELDS):
        return False
    return stored == _managed_fields_fingerprint(managed_request_fields_with_extensions(
        request, stream_partials=DEFAULT_STREAM_PARTIALS,
        max_partial_chars=DEFAULT_MAX_PARTIAL_CHARS,
    ))


def frozen_command_fingerprint(command) -> str:
    """Hash exactly the immutable provider command, never dispatch-attempt data."""
    identity = json.dumps(
        command.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()
