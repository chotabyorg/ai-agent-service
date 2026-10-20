"""Selected managed completion validation and observational metadata only."""

from dataclasses import dataclass
from types import SimpleNamespace

from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion import ChatCompletion, Choice

from core.managed_completion_diagnostics import CompletionDiagnostic, emit_completion_diagnostic
from schemas.managed_turn import ManagedTurnRequest
from services.completion_timing import CompletionTiming
from services.completion_accounting import accounting_metrics
from services.router_metadata_observation import RouterObservation, error_router_observation


_MISSING = object()
_FINISH = {"stop": "STOP", "tool_calls": "TOOL_CALLS", "length": "LENGTH",
           "content_filter": "CONTENT_FILTER", "function_call": "FUNCTION_CALL"}


def _field(value, name, sdk_type):
    if type(value) not in (sdk_type, SimpleNamespace):
        return _MISSING
    stored = object.__getattribute__(value, "__dict__")
    return stored.get(name, _MISSING) if type(stored) is dict else _MISSING


def _choice(response):
    choices = _field(response, "choices", ChatCompletion)
    return choices[0] if type(choices) is list and choices else _MISSING


@dataclass(frozen=True, slots=True)
class SelectedCompletion:
    message: object = None
    branch: str = "INVALID_SHAPE"
    visible_content: str = "NOT_INSPECTED"
    answer: str | None = None


def select_managed_completion(response, clean_text):
    message = _field(_choice(response), "message", Choice)
    if type(message) not in (ChatCompletionMessage, SimpleNamespace):
        return SelectedCompletion()
    tool_calls = _field(message, "tool_calls", ChatCompletionMessage)
    if tool_calls is not _MISSING and tool_calls is not None and type(tool_calls) is not list:
        return SelectedCompletion()
    if type(tool_calls) is list and tool_calls:
        return SelectedCompletion(message, "TOOL_CALLS")
    content = _field(message, "content", ChatCompletionMessage)
    if content is _MISSING or content is None:
        state, answer = "MISSING", None
    elif type(content) is not str:
        state, answer = "INVALID_TYPE", None
    elif not content:
        state, answer = "EMPTY", None
    elif not content.strip():
        state, answer = "WHITESPACE", None
    else:
        answer = clean_text(content)
        state = "TEXT" if answer else "STRIPPED_EMPTY"
    return SelectedCompletion(message, "FINAL", state, answer)


def is_stopped_prose(response, selected):
    """Allow format repair only for a complete, visible, non-tool response."""
    return (selected.branch == "FINAL" and selected.visible_content == "TEXT"
            and bool(selected.answer)
            and _field(_choice(response), "finish_reason", Choice) == "stop")


def observe_managed_completion(response, selected, *, request, step, timing=None):
    try:
        if type(request) is not ManagedTurnRequest:
            return
        finish = _field(_choice(response), "finish_reason", Choice)
        category = "MISSING" if finish is _MISSING or finish is None else (
            _FINISH.get(finish, "OTHER") if type(finish) is str else "OTHER")
        usage = _field(response, "usage", ChatCompletion)
        counts = accounting_metrics(usage)
        if type(timing) is CompletionTiming:
            for name in ('model_ms', 'first_delta_ms'):
                value = getattr(timing, name)
                if type(value) is int and 0 <= value <= 2147483647:
                    counts[name] = value
            if type(timing.router_observation) is RouterObservation:
                counts.update(timing.router_observation.fields())
        emit_completion_diagnostic(CompletionDiagnostic(turn_id=request.turn_id, step=step,
            branch=selected.branch, finish=category,
            visible_content=selected.visible_content, **counts))
    except Exception:
        # Bad metadata or an emission failure cannot change the selected result.
        pass


def observe_model_failure(error, *, request, step, timing):
    """Retain bounded status and elapsed time, never provider bodies or headers."""
    try:
        from openai import APIError
        if type(request) is not ManagedTurnRequest or not isinstance(error, APIError):
            return
        from services.model_retry import classify_model_failure
        from services.model_limit_observation import model_limit_observation
        failure = classify_model_failure(error)
        timing.complete()
        route = (error_router_observation(error).fields()
                 if type(timing) is CompletionTiming and timing.router_observation is not None else {})
        emit_completion_diagnostic(CompletionDiagnostic(turn_id=request.turn_id, step=step,
            branch='MODEL_FAILURE', finish='MISSING', visible_content='NOT_INSPECTED',
            model_ms=timing.model_ms, http_status=failure.http_status,
            failure_category=failure.category, **model_limit_observation(error), **route))
    except Exception:
        pass
