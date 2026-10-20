"""Private complete S2S grants, safe configuration and request-local diagnostics."""

import json
import logging
import re
from contextvars import ContextVar
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator

from schemas.managed_turn import Channel, ManagedValue, validate_owner
from core.managed_completion_diagnostics import _trusted_completion_record
from core.voice_latency_diagnostics import _trusted_voice_timing_record
from core.managed_business_diagnostics import _trusted_business_record
from core.managed_wait_diagnostics import _trusted_wait_record

EXECUTION_NAMESPACE = "main-chota.conversation.v1"
MANAGED_SECRET_ENV_KEYS = frozenset({
    "managed_ai_auth_principals",
    "internal_ai_auth_principals",
    "gateway_ai_auth_principals",
    "gateway_ai_owner_token",
    # Names used only by the disposable pinned-gateway runtime proof.
    "owner_ai_token",
    "owner_cp_token",
    "business_conversation_token",
})
managed_logging: ContextVar[bool] = ContextVar("managed_logging", default=False)


def valid_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", value))


class ManagedGrant(ManagedValue):
    owner_id: str
    integration_id: UUID
    channel: Channel
    agent_id: UUID
    agent_owner_id: UUID

    _owner = field_validator("owner_id")(validate_owner)

    def scope(self):
        return self.owner_id, self.integration_id, self.channel, self.agent_id


class ManagedPrincipal(ManagedValue):
    principal_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    token: SecretStr
    caller: Literal["conversation-service"]
    execution_namespace: Literal["main-chota.conversation.v1"]
    grants: Annotated[list[ManagedGrant], Field(min_length=1, max_length=64)]


class OwnershipCredential(BaseModel):
    """One versioned ownership-check credential."""

    model_config = ConfigDict(extra="forbid")

    version: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    token: SecretStr
    revoked: StrictBool = False


class OwnershipGrant(BaseModel):
    """Exact owner and agent pair authorized for ownership checks."""

    model_config = ConfigDict(extra="forbid")

    owner_id: UUID
    agent_id: UUID

    def scope(self):
        return self.owner_id, self.agent_id


class OwnershipPrincipal(BaseModel):
    """Purpose-bound caller allowed to check bounded owner/agent pairs."""

    model_config = ConfigDict(extra="forbid")

    principal_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    caller: Literal["control-plane-service", "knowledge-base-service"]
    purpose: Literal["agent-ownership-check"]
    current: OwnershipCredential
    previous: OwnershipCredential | None = None
    revoked: StrictBool = False
    all_tenants: StrictBool = False
    grants: Annotated[list[OwnershipGrant], Field(max_length=64)] = Field(default_factory=list)

    def credentials(self) -> tuple[OwnershipCredential, ...]:
        return (self.current,) if self.previous is None else (self.current, self.previous)

    def allows(self, owner_id: UUID, agent_id: UUID) -> bool:
        if self.all_tenants:
            return True
        return any(grant.scope() == (owner_id, agent_id) for grant in self.grants)


class GatewayPrincipal(BaseModel):
    """Gateway workload assertion, limited to AI owner router admission."""

    model_config = ConfigDict(extra="forbid")

    principal_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    caller: Literal["chota-gateway"]
    purpose: Literal["gateway-owner-api"]
    audience: Literal["ai-agent-service"]
    current: OwnershipCredential
    previous: OwnershipCredential | None = None
    revoked: StrictBool = False

    def credentials(self) -> tuple[OwnershipCredential, ...]:
        return (self.current,) if self.previous is None else (self.current, self.previous)


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def parse_gateway_principals(
    raw: str,
    other_tokens: tuple[str, ...],
) -> tuple[GatewayPrincipal, ...]:
    """Validate receiver/purpose and every version without rendering secrets."""
    if not raw:
        return ()
    try:
        values = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError
        principals = tuple(GatewayPrincipal.model_validate(value) for value in values)
        ids: set[str] = set()
        tokens: set[str] = set()
        for principal in principals:
            if principal.principal_id in ids:
                raise ValueError
            ids.add(principal.principal_id)
            versions: set[str] = set()
            for credential in principal.credentials():
                token = credential.token.get_secret_value()
                if (credential.version in versions or not valid_token(token)
                        or token in tokens or token in other_tokens):
                    raise ValueError
                versions.add(credential.version)
                tokens.add(token)
        return principals
    except Exception:
        raise ValueError("Invalid gateway owner authentication configuration") from None


def parse_ownership_principals(
    raw: str,
    other_tokens: tuple[str, ...],
) -> tuple[OwnershipPrincipal, ...]:
    """Parse a bounded purpose-specific registry without exposing rejected values."""
    if not raw:
        return ()
    try:
        values = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError
        principals = tuple(OwnershipPrincipal.model_validate(value) for value in values)
        ids: set[str] = set()
        tokens: set[str] = set()
        for principal in principals:
            if principal.principal_id in ids:
                raise ValueError
            ids.add(principal.principal_id)

            if principal.all_tenants == bool(principal.grants):
                # Exactly one authorization mode must be selected.
                raise ValueError

            version_ids: set[str] = set()
            for credential in principal.credentials():
                token = credential.token.get_secret_value()
                if (credential.version in version_ids
                        or not valid_token(token)
                        or token in tokens
                        or token in other_tokens):
                    raise ValueError
                version_ids.add(credential.version)
                tokens.add(token)

            grants: set[tuple[UUID, UUID]] = set()
            for grant in principal.grants:
                if grant.scope() in grants:
                    raise ValueError
                grants.add(grant.scope())
        return principals
    except Exception:
        raise ValueError("Invalid internal ownership authentication configuration") from None


def parse_principals(raw: str, other_tokens: tuple[str, ...]) -> tuple[ManagedPrincipal, ...]:
    """Never expose registry content or pydantic rejected inputs at startup."""
    if not raw:
        return ()
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not 1 <= len(values) <= 32:
            raise ValueError
        principals = tuple(ManagedPrincipal.model_validate(value) for value in values)
        ids, tokens, grants, owner_mapping = set(), set(), set(), {}
        for principal in principals:
            token = principal.token.get_secret_value()
            if (not valid_token(token) or token in tokens or token in other_tokens
                    or principal.principal_id in ids):
                raise ValueError
            ids.add(principal.principal_id)
            tokens.add(token)
            for grant in principal.grants:
                if grant.scope() in grants:
                    raise ValueError
                grants.add(grant.scope())
                previous = owner_mapping.setdefault(grant.owner_id, grant.agent_owner_id)
                if previous != grant.agent_owner_id:
                    raise ValueError
        return principals
    except Exception:
        raise ValueError("Invalid managed AI authentication configuration") from None


def install_managed_log_boundary() -> None:
    """Sanitize records at creation, including dependency handlers and tracebacks.

    ContextVars keep concurrent legacy records unchanged. Sanitization also removes
    arbitrary extra fields, so verbose SQL/provider formatters cannot reveal them.
    """
    if getattr(logging.Logger.makeRecord, "_managed_boundary", False):
        return
    original = logging.Logger.makeRecord

    def make_record(self, *args, **kwargs):
        record = original(self, *args, **kwargs)
        if managed_logging.get():
            safe = _trusted_completion_record(record) or _trusted_voice_timing_record(record)
            if safe is None:
                safe = _trusted_business_record(record)
            if safe is None:
                safe = _trusted_wait_record(record)
            if safe is None and self.name == "uvicorn.access":
                # uvicorn's AccessFormatter unpacks exactly five positional fields.
                # Keep that shape while never echoing the request line or query.
                safe = logging.LogRecord(self.name, record.levelno, record.pathname,
                                         record.lineno, '%s - "%s %s HTTP/%s" %s',
                                         ("-", "-", "-", "1.1", "0"), None)
            if safe is None:
                safe = logging.LogRecord(record.name, record.levelno, record.pathname,
                                     record.lineno, "Managed execution diagnostic", (), None)
            record.__dict__.clear()
            record.__dict__.update(safe.__dict__)
        return record

    make_record._managed_boundary = True
    logging.Logger.makeRecord = make_record
