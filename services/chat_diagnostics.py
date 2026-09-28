"""Request-local, non-sensitive diagnostics for unexpected chat failures."""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum


class ChatFailureStage(str, Enum):
    """Closed set of coarse stages that may fail while handling chat."""

    REQUEST = "request"
    AGENT_LOOKUP = "agent_lookup"
    AGENT_CONFIGURATION = "agent_configuration"
    SESSION_SETUP = "session_setup"
    CONTEXT_LOAD = "context_load"
    MCP_SETUP = "mcp_setup"
    MCP_INITIALIZE = "mcp_initialize"
    MCP_LIST_TOOLS = "mcp_list_tools"
    MCP_METADATA_READ = "mcp_metadata_read"
    LLM_REQUEST = "llm_request"
    RESPONSE_PROCESSING = "response_processing"
    TOOL_EXECUTION = "tool_execution"
    HISTORY_PERSIST = "history_persist"
    MCP_CLEANUP = "mcp_cleanup"


@dataclass(slots=True)
class ChatDiagnosticContext:
    """Per-request stage state; it deliberately contains no request data."""

    failure_stage: ChatFailureStage = ChatFailureStage.REQUEST


_current_context: ContextVar[ChatDiagnosticContext | None] = ContextVar(
    "chat_diagnostic_context",
    default=None,
)


def bind_chat_diagnostic_context(
    context: ChatDiagnosticContext,
) -> Token[ChatDiagnosticContext | None]:
    """Bind diagnostic state to the current request task."""

    return _current_context.set(context)


def reset_chat_diagnostic_context(token: Token[ChatDiagnosticContext | None]) -> None:
    """Restore the previous context after the request completes."""

    _current_context.reset(token)


def set_chat_failure_stage(stage: ChatFailureStage) -> None:
    """Update the current request's stage without storing request-specific data."""

    context = _current_context.get()
    if context is not None:
        context.failure_stage = stage
