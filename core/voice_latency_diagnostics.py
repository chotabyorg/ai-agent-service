"""Finite Voice timings, without arguments, results, prompts or transcripts."""
from dataclasses import dataclass
import logging
import time
from uuid import UUID

from business_capabilities.registry import WIRE_REGISTRY


_IDENTITY = object()
_TEXT_IDENTITY = object()
_PHASES = frozenset({"MODEL", "TOOL", "PREPARE", "SETUP", "BUSINESS", "CONTEXT", "KNOWLEDGE"})
_TOOLS = frozenset(WIRE_REGISTRY) | {
    "business_propose", "business_decide_proposal", "business_confirm",
    "business_execute_plan", "booking_search_options",
}


@dataclass(frozen=True, slots=True)
class VoiceTiming:
    turn_id: UUID
    step: int
    phase: str
    elapsed_ms: int
    tool: str | None = None


def emit_voice_timing(value):
    _emit_timing(value, _IDENTITY, 'voice_phase_timing')


def emit_text_timing(value):
    """Use the same finite fields for buffered managed-text stages."""
    _emit_timing(value, _TEXT_IDENTITY, 'managed_text_phase_timing')


def _emit_timing(value, identity, event):
    try:
        from core.managed_security import managed_logging
        if not managed_logging.get() or not getattr(logging.Logger.makeRecord, "_managed_boundary", False):
            return
        logging.getLogger("uvicorn.error").info(event, extra={
            "_voice_timing_identity": identity, "_voice_timing": value})
    except Exception:
        pass


def emit_voice_checkpoint(runtime, phase):
    started = getattr(runtime, "voice_started_at", None)
    if started is not None:
        emit_voice_timing(VoiceTiming(runtime.request.turn_id, 0, phase,
                                      max(0, round((time.monotonic() - started) * 1000))))


def emit_text_checkpoint(runtime, phase):
    """Cumulative text setup checkpoints, measured from the owned-task start."""
    request = getattr(runtime, 'request', None)
    channel = getattr(request, 'channel', None)
    if request is None or getattr(channel, 'value', channel) == 'VOICE':
        return
    started = getattr(runtime, 'voice_started_at', None)
    if started is not None:
        emit_text_timing(VoiceTiming(request.turn_id, 0, phase,
            max(0, round((time.monotonic() - started) * 1000))))


def _trusted_voice_timing_record(record):
    identity = record.__dict__.get("_voice_timing_identity")
    if identity is not _IDENTITY and identity is not _TEXT_IDENTITY:
        return None
    value = record.__dict__.get("_voice_timing")
    if type(value) is not VoiceTiming:
        return None
    if (type(value.turn_id) is not UUID or type(value.step) is not int
            or not 0 <= value.step <= 20 or type(value.elapsed_ms) is not int
            or not 0 <= value.elapsed_ms <= 2147483647
            or type(value.phase) is not str or value.phase not in _PHASES):
        return None
    if value.phase == "TOOL":
        if type(value.tool) is not str or value.tool not in _TOOLS:
            return None
    elif value.tool is not None:
        return None
    fields = {"turn_id": str(value.turn_id), "step": value.step,
              "phase": value.phase, "elapsed_ms": value.elapsed_ms}
    if value.tool is not None:
        fields["tool"] = value.tool
    event = 'voice_phase_timing' if identity is _IDENTITY else 'managed_text_phase_timing'
    safe = logging.LogRecord("uvicorn.error", logging.INFO, "", 0,
        event + " " + " ".join(key + "=%s" for key in fields), tuple(fields.values()), None)
    safe.event = event
    safe.__dict__.update(fields)
    return safe
