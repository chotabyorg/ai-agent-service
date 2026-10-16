"""Finite Voice timings, without arguments, results, prompts or transcripts."""
from dataclasses import dataclass
import logging
import time
from uuid import UUID

from business_capabilities.registry import WIRE_REGISTRY


_IDENTITY = object()
_PHASES = frozenset({"MODEL", "TOOL", "PREPARE", "SETUP", "BUSINESS", "CONTEXT", "KNOWLEDGE"})
_TOOLS = frozenset(WIRE_REGISTRY) | {
    "business_propose", "business_decide_proposal", "business_confirm",
    "business_execute_plan",
}


@dataclass(frozen=True, slots=True)
class VoiceTiming:
    turn_id: UUID
    step: int
    phase: str
    elapsed_ms: int
    tool: str | None = None


def emit_voice_timing(value):
    try:
        from core.managed_security import managed_logging
        if not managed_logging.get() or not getattr(logging.Logger.makeRecord, "_managed_boundary", False):
            return
        logging.getLogger("uvicorn.error").info("voice_phase_timing", extra={
            "_voice_timing_identity": _IDENTITY, "_voice_timing": value})
    except Exception:
        pass


def emit_voice_checkpoint(runtime, phase):
    started = getattr(runtime, "voice_started_at", None)
    if started is not None:
        emit_voice_timing(VoiceTiming(runtime.request.turn_id, 0, phase,
                                      max(0, round((time.monotonic() - started) * 1000))))


def _trusted_voice_timing_record(record):
    if record.__dict__.get("_voice_timing_identity") is not _IDENTITY:
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
    safe = logging.LogRecord("uvicorn.error", logging.INFO, "", 0,
        "voice_phase_timing " + " ".join(key + "=%s" for key in fields), tuple(fields.values()), None)
    safe.event = "voice_phase_timing"
    safe.__dict__.update(fields)
    return safe
