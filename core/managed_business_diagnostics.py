"""Finite, code-owned Business execution observations for managed turns."""

from dataclasses import dataclass
import logging
from uuid import UUID

from business_capabilities.registry import CAPABILITIES

_IDENTITY = object()
_STAGES = frozenset({"PREPARATION", "LOOKUP", "PROPOSAL", "TERMINAL", "TOOL_INPUT", "TOOL_READ", "TOOL_GUARD"})
_CAPABILITIES = frozenset({
    "booking.create", "booking_create", "business_propose",
    "customer.find", "customer_find", "business_decide_proposal", "booking_search_options", "NOT_APPLICABLE",
}) | frozenset(value for capability in CAPABILITIES for value in (capability.id, capability.tool_name))
_REFUSALS = frozenset({
    "NONE", "IDENTITY_CONFLICT", "AMBIGUOUS_IDENTITY", "LOOKUP_UNAVAILABLE",
    "LOOKUP_INVALID", "CAPABILITY_DISABLED", "AUTHORITY_BLOCKED", "OTHER",
    "BOOKING_CONFLICT", "STATE_CONFLICT", "NOT_FOUND", "PERMISSION_DENIED",
    "PROVIDER_UNAVAILABLE", "VALIDATION_ERROR", "STALE_CUSTOMER_CONTEXT",
    "ACTION_INVALID", "BINDING_UNAVAILABLE", "EFFECT_AUTHORITY_UNAVAILABLE",
    "FACTS_INVALID", "PLAN_DENIED", "REQUEST_CONFLICT", "RESULT_GATE_UNAVAILABLE",
})
_LOOKUP_SOURCES = frozenset({"NOT_APPLICABLE", "MODEL", "BOOKING_PREFLIGHT"})
_LOOKUP_MODES = frozenset({"NOT_APPLICABLE", "IDENTITY_ONLY", "NAME_FILTERED"})
_LOOKUP_RESULTS = frozenset({"NOT_RUN", "NONE", "SINGLE", "AMBIGUOUS", "ERROR"})
_FINAL_OUTCOMES = frozenset({"NOT_FINAL", "DRAFT_READY", "FAILED", "UNKNOWN"})
_REFERENCE_SOURCES = frozenset({"NOT_APPLICABLE", "NOT_SUPPLIED", "READ_THIS_TURN",
    "WRONG_KIND_THIS_TURN", "NOT_OBSERVED_THIS_TURN", "OBSERVATION_LIMIT", "UNAVAILABLE"})
_SCHEMA_RULES = frozenset({"NOT_APPLICABLE", "schema", "type", "required", "additionalProperties",
    "oneOf", "anyOf", "allOf", "not", "const", "enum", "minLength", "maxLength", "pattern",
    "format", "minItems", "maxItems", "uniqueItems", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "multipleOf"})
_SCHEMA_FIELDS = frozenset({"NOT_APPLICABLE", "ROOT", "OTHER", "query", "cursor", "limit",
    "actions", "arguments", "capability", "customer_ref", "customer", "identity", "display_name",
    "offering_ref", "resource_ref", "start_at", "from", "to", "booking_ref", "expected_revision", "kind", "value",
    "proposal_id", "intent", "tool_calls", "tool_call_id", "tool_name", "type", "assistant_message", "envelope"})


def refusal_category(value):
    return value if type(value) is str and value in _REFUSALS else "OTHER"


@dataclass(frozen=True, slots=True)
class ManagedBusinessDiagnostic:
    turn_id: UUID
    stage: str
    capability: str
    refusal_category: str
    lookup_source: str
    lookup_mode: str
    lookup_result: str
    proposal_present: bool
    operation_present: bool
    final_outcome: str = "NOT_FINAL"
    schema_rule: str = "NOT_APPLICABLE"
    schema_field: str = "NOT_APPLICABLE"
    offering_ref_source: str = "NOT_APPLICABLE"
    resource_ref_source: str = "NOT_APPLICABLE"
    customer_ref_source: str = "NOT_APPLICABLE"


def emit_business_diagnostic(diagnostic: ManagedBusinessDiagnostic) -> None:
    try:
        from core.managed_security import managed_logging
        if not managed_logging.get() or not getattr(logging.Logger.makeRecord, "_managed_boundary", False):
            return
        logging.getLogger("uvicorn.error").info("managed_business_observed", extra={
            "_business_identity": _IDENTITY, "_business_diagnostic": diagnostic,
        })
    except Exception:
        # Diagnostics never change business execution or expose logger failures.
        pass


def _trusted_business_record(record):
    """A private marker is necessary but every field must still be canonical."""
    if record.__dict__.get("_business_identity") is not _IDENTITY:
        return None
    value = record.__dict__.get("_business_diagnostic")
    if type(value) is not ManagedBusinessDiagnostic:
        return None
    try:
        if type(value.turn_id) is not UUID:
            return None
        for field, allowed in (
            ("stage", _STAGES), ("capability", _CAPABILITIES),
            ("refusal_category", _REFUSALS), ("lookup_source", _LOOKUP_SOURCES),
            ("lookup_mode", _LOOKUP_MODES), ("lookup_result", _LOOKUP_RESULTS),
            ("final_outcome", _FINAL_OUTCOMES),
            ("schema_rule", _SCHEMA_RULES), ("schema_field", _SCHEMA_FIELDS),
            ("offering_ref_source", _REFERENCE_SOURCES), ("resource_ref_source", _REFERENCE_SOURCES),
            ("customer_ref_source", _REFERENCE_SOURCES),
        ):
            item = getattr(value, field)
            if type(item) is not str or item not in allowed:
                return None
        if type(value.proposal_present) is not bool or type(value.operation_present) is not bool:
            return None
        fields = {
            "turn_id": str(value.turn_id), "stage": value.stage,
            "capability": value.capability, "refusal_category": value.refusal_category,
            "lookup_source": value.lookup_source, "lookup_mode": value.lookup_mode,
            "lookup_result": value.lookup_result,
            "proposal_present": value.proposal_present,
            "operation_present": value.operation_present,
            "final_outcome": value.final_outcome,
            "schema_rule": value.schema_rule, "schema_field": value.schema_field,
            "offering_ref_source": value.offering_ref_source,
            "resource_ref_source": value.resource_ref_source,
            "customer_ref_source": value.customer_ref_source,
        }
    except Exception:
        return None
    template = "managed_business_observed " + " ".join(name + "=%s" for name in fields)
    safe = logging.LogRecord("uvicorn.error", logging.INFO, "", 0,
                             template, tuple(fields.values()), None)
    safe.event = "managed_business_observed"
    safe.__dict__.update(fields)
    return safe
