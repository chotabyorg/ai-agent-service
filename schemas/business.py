"""Closed business values. Caller/model values never confer execution authority."""

import hashlib
import json
import math
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from schemas.managed_turn import Channel, OperationReference, validate_owner

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Handle = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Capability = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")]
Generation = Annotated[StrictInt, Field(ge=1, le=9223372036854775807)]


class BusinessValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_value(value: Any, *, max_bytes: int = 16384) -> Any:
    """NFC/UTC/canonical decimals, finite closed JSON with bounded complexity."""
    count = 0

    def visit(item, depth=0):
        nonlocal count
        count += 1
        if count > 1024 or depth > 16:
            raise ValueError("Business value exceeds bounds")
        if isinstance(item, BaseModel):
            return visit(item.model_dump(mode="python"), depth)
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("Business datetime requires timezone")
            return item.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(item, Decimal):
            if not item.is_finite() or abs(item) > Decimal("1e18"):
                raise ValueError("Invalid business decimal")
            return format(item.normalize(), "f") if item else "0"
        if isinstance(item, str):
            if "\x00" in item or any(0xD800 <= ord(c) <= 0xDFFF for c in item):
                raise ValueError("Invalid business string")
            return unicodedata.normalize("NFC", item)
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("Invalid business object")
                normalized = visit(key, depth + 1)
                if normalized in result:
                    raise ValueError("Duplicate normalized business key")
                result[normalized] = visit(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child, depth + 1) for child in item]
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int) and -9223372036854775808 <= item <= 9223372036854775807:
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        raise ValueError("Invalid business value")

    result = visit(value)
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()) > max_bytes:
        raise ValueError("Business value exceeds bounds")
    return result


def fingerprint(domain: str, value: Any) -> str:
    encoded = json.dumps(canonical_value(value, max_bytes=1048576), ensure_ascii=False,
                         allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((domain + "\n" + encoded).encode()).hexdigest()


class BusinessAction(BusinessValue):
    capability: Capability
    arguments: dict[str, Any]

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value):
        return canonical_value(value)


class BusinessScopeValue(BusinessValue):
    scope_id: UUID
    owner_id: str
    agent_owner_id: UUID
    agent_id: UUID
    integration_id: UUID
    channel: Channel
    conversation_id: UUID

    _owner = field_validator("owner_id")(validate_owner)


class MaterialFacts(BusinessValue):
    values: dict[str, Any] = Field(default_factory=dict)
    revisions: dict[str, Generation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        canonical_value(self.values)
        if len(self.revisions) > 32:
            raise ValueError("Too many fact revisions")
        canonical_value(self.revisions)
        return self


class ReceiptBinding(BusinessValue):
    scope: BusinessScopeValue
    operation_id: UUID
    request_fingerprint: Hash
    adapter_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
    connection_id: UUID
    provider_profile_revision: Hash
    issued_at: datetime
    provider_idempotency_key: Handle | None

    @field_validator("issued_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Receipt time requires timezone")
        return value.astimezone(timezone.utc)


class FrozenProviderCommand(ReceiptBinding):
    capability: Capability
    schema_version: Generation
    arguments: dict[str, Any]
    facts: dict[str, Any]
    fact_revisions: dict[str, Generation]
    authorization_generation: Generation
    context_generation: Generation

    def receipt_binding(self) -> ReceiptBinding:
        return ReceiptBinding.model_validate(self.model_dump(include=set(ReceiptBinding.model_fields)))


class SucceededReceipt(BusinessValue):
    binding: ReceiptBinding
    receipt_id: Handle
    object_id: Handle
    evidence: dict[str, Any]
    kind: Literal["SUCCEEDED"] = "SUCCEEDED"

    @field_validator("evidence")
    @classmethod
    def bounded(cls, value):
        return canonical_value(value)


class DefinitiveNoEffect(BusinessValue):
    binding: ReceiptBinding
    receipt_id: Handle
    evidence: dict[str, Any]
    reason_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
    kind: Literal["FAILED_NO_EFFECT"] = "FAILED_NO_EFFECT"

    _bounded = field_validator("evidence")(SucceededReceipt.bounded.__func__)


class Ambiguous(BusinessValue):
    kind: Literal["AMBIGUOUS"] = "AMBIGUOUS"
    reason_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")] = "PROVIDER_UNKNOWN"


class ProvenSucceeded(SucceededReceipt):
    pass


class ProvenFailedNoEffect(DefinitiveNoEffect):
    pass


class StillUnknown(Ambiguous):
    pass


class BusinessToolResult(BusinessValue):
    tool_payload: dict[str, Any] = Field(default_factory=dict)
    safe_draft: str | None = None
    halt_status: Literal["FAILED", "UNKNOWN"] | None = None
    error_code: Literal["TOOL_SETUP_FAILED", "TOOL_EXECUTION_UNKNOWN"] | None = None


class BusinessSafetyError(Exception):
    """Only closed diagnostic categories leave the business boundary."""

    def __init__(self, code: str, *, processing_status: str = "FAILED"):
        self.code = code
        self.processing_status = processing_status
        super().__init__(code)


def operation_reference(operation_id: UUID, status: str) -> OperationReference:
    return OperationReference(origin="AI_AGENT_SERVICE", operation_id=str(operation_id),
                              outcome=None if status == "RESERVED" else
                              status if status in {"SUCCEEDED", "FAILED"} else "UNKNOWN")
