"""Private complete S2S grants, safe configuration and request-local diagnostics."""

import json
import logging
import re
from contextvars import ContextVar
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from schemas.managed_turn import Channel, ManagedValue, validate_owner
from core.managed_completion_diagnostics import _trusted_completion_record
from core.voice_latency_diagnostics import _trusted_voice_timing_record
from core.managed_business_diagnostics import _trusted_business_record

EXECUTION_NAMESPACE = "main-chota.conversation.v1"
MANAGED_SECRET_ENV_KEYS = frozenset({"managed_ai_auth_principals", "business_conversation_token"})
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
