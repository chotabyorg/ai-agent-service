"""Code-owned, finite completion observations; never model content."""

from dataclasses import dataclass
import logging
from uuid import UUID


_IDENTITY = object()
_BRANCHES = frozenset({"FINAL", "TOOL_CALLS", "INVALID_SHAPE", "MODEL_FAILURE"})
_FINISHES = frozenset({"STOP", "TOOL_CALLS", "LENGTH", "CONTENT_FILTER",
                       "FUNCTION_CALL", "MISSING", "OTHER"})
_CONTENT = frozenset({"TEXT", "MISSING", "EMPTY", "WHITESPACE",
                     "STRIPPED_EMPTY", "INVALID_TYPE", "NOT_INSPECTED"})
_COUNTS = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
           "cached_tokens", "cache_write_tokens")
_METRICS = _COUNTS + ("model_ms", "first_delta_ms")
_LIMIT_FIELDS = {
    'limit_source': frozenset({'MISSING', 'OTHER', 'OPENROUTER_IN_FLIGHT_BUDGET',
        'OPENROUTER_CREDITS', 'OPENROUTER_KEY_LIMIT', 'OPENROUTER_GUARDRAIL_BUDGET',
        'OPENROUTER_WORKSPACE_BUDGET'}),
    'provider_code_state': frozenset({'MISSING', 'PRESENT', 'INVALID'}),
    'rate_limit_headers': frozenset({'MISSING', 'PARTIAL', 'COMPLETE'}),
    'retry_after_state': frozenset({'MISSING', 'INVALID', 'WITHIN_BUDGET', 'OVER_BUDGET'}),
}


@dataclass(frozen=True, slots=True)
class CompletionDiagnostic:
    turn_id: UUID
    step: int
    branch: str
    finish: str
    visible_content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_nano_credits: int | None = None
    model_ms: int | None = None
    first_delta_ms: int | None = None
    http_status: int | None = None
    failure_category: str | None = None
    limit_source: str | None = None
    provider_code_state: str | None = None
    rate_limit_headers: str | None = None
    retry_after_state: str | None = None


def emit_completion_diagnostic(diagnostic: CompletionDiagnostic) -> None:
    try:
        from core.managed_security import managed_logging
        if not managed_logging.get() or not getattr(logging.Logger.makeRecord, "_managed_boundary", False):
            return
        # Uvicorn's existing error logger has an INFO handler in production.
        # The managed boundary replaces this entire record before rendering.
        logging.getLogger("uvicorn.error").info("managed_completion_observed", extra={
            "_completion_identity": _IDENTITY, "_completion_diagnostic": diagnostic})
    except Exception:
        # Observability must not alter execution or expose handler exceptions.
        pass


def _trusted_completion_record(record):
    """A private marker is necessary but never sufficient: validate every field."""
    if record.__dict__.get("_completion_identity") is not _IDENTITY:
        return None
    value = record.__dict__.get("_completion_diagnostic")
    if type(value) is not CompletionDiagnostic:
        return None
    try:
        if type(value.turn_id) is not UUID or type(value.step) is not int or not 1 <= value.step <= 20:
            return None
        for field, allowed in (("branch", _BRANCHES), ("finish", _FINISHES), ("visible_content", _CONTENT)):
            item = getattr(value, field)
            if type(item) is not str or item not in allowed:
                return None
        fields = {"turn_id": str(value.turn_id), "step": value.step,
                  "branch": value.branch, "finish": value.finish,
                  "visible_content": value.visible_content}
        for field in _METRICS:
            item = getattr(value, field)
            if item is None:
                continue
            if type(item) is not int or not 0 <= item <= 2147483647:
                return None
            fields[field] = item
        if value.cost_nano_credits is not None:
            if type(value.cost_nano_credits) is not int or not 0 <= value.cost_nano_credits <= 10**18:
                return None
            fields['cost_nano_credits'] = value.cost_nano_credits
        if value.http_status is not None:
            if type(value.http_status) is not int or not 100 <= value.http_status <= 599:
                return None
            fields['http_status'] = value.http_status
        if value.failure_category is not None:
            from services.model_retry import FAILURE_CATEGORIES
            if type(value.failure_category) is not str or value.failure_category not in FAILURE_CATEGORIES:
                return None
            fields['failure_category'] = value.failure_category
        for field, allowed in _LIMIT_FIELDS.items():
            item = getattr(value, field)
            if item is None:
                continue
            if value.branch != 'MODEL_FAILURE' or type(item) is not str or item not in allowed:
                return None
            fields[field] = item
    except Exception:
        return None
    template = "managed_completion_observed " + " ".join(name + "=%s" for name in fields)
    safe = logging.LogRecord("uvicorn.error", logging.INFO, "", 0,
                             template, tuple(fields.values()), None)
    safe.event = "managed_completion_observed"
    safe.__dict__.update(fields)
    return safe
