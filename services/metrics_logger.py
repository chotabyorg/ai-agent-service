"""
MetricsLogger — structured JSON logging для сбора метрик AI-агента.

Логирует каждый этап pipeline: получение запроса → LLM → MCP/Bitrix → ответ.
Уровень детализации управляется через env METRICS_LOG_LEVEL.
"""
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

METRICS_LOG_LEVEL = os.getenv("METRICS_LOG_LEVEL", "normal")  # detailed | normal | minimal

logger = logging.getLogger("chota.metrics")


@dataclass
class PipelineMetrics:
    """Собирает метрики одного запроса через pipeline."""

    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: Optional[str] = None
    session_id: Optional[str] = None

    # Timestamps (epoch seconds)
    ts_received: Optional[float] = None
    ts_llm_start: Optional[float] = None
    ts_llm_end: Optional[float] = None
    ts_response_sent: Optional[float] = None

    # LLM details
    llm_model: Optional[str] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None

    # MCP/Bitrix aggregated details
    mcp_calls_count: int = 0
    mcp_total_latency_ms: float = 0.0
    mcp_service: Optional[str] = None
    mcp_last_action: Optional[str] = None
    mcp_last_result: Optional[str] = None  # "success" | "error" | "skipped"

    # Internal MCP tracking
    _mcp_call_start: Optional[float] = field(default=None, repr=False)

    # Intent & action
    action_taken: Optional[str] = None  # "auto_reply" | "escalate" | "mcp_action"

    # Result
    status: str = "success"  # "success" | "error"
    error_message: Optional[str] = None

    # Content (only in detailed mode)
    message_text: Optional[str] = None
    response_text: Optional[str] = None

    def mark_received(self) -> None:
        self.ts_received = time.time()

    def mark_llm_start(self) -> None:
        self.ts_llm_start = time.time()

    def mark_llm_end(
        self,
        model: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
    ) -> None:
        self.ts_llm_end = time.time()
        if model:
            self.llm_model = model
        if tokens_in is not None:
            self.tokens_input = tokens_in
        if tokens_out is not None:
            self.tokens_output = tokens_out

    def mark_mcp_start(self, service: Optional[str] = None) -> None:
        self._mcp_call_start = time.time()
        if service:
            self.mcp_service = service

    def mark_mcp_end(self, action: Optional[str] = None, result: str = "success") -> None:
        if self._mcp_call_start is not None:
            call_latency_ms = (time.time() - self._mcp_call_start) * 1000
            self.mcp_total_latency_ms = round(self.mcp_total_latency_ms + call_latency_ms, 1)
            self._mcp_call_start = None
        self.mcp_calls_count += 1
        if action:
            self.mcp_last_action = action
        self.mcp_last_result = result

    def mark_response_sent(self) -> None:
        self.ts_response_sent = time.time()

    def mark_error(self, error: str) -> None:
        self.status = "error"
        self.error_message = error

    @property
    def total_latency_ms(self) -> Optional[float]:
        if self.ts_received and self.ts_response_sent:
            return round((self.ts_response_sent - self.ts_received) * 1000, 1)
        return None

    @property
    def llm_latency_ms(self) -> Optional[float]:
        if self.ts_llm_start and self.ts_llm_end:
            return round((self.ts_llm_end - self.ts_llm_start) * 1000, 1)
        return None

    def emit(self) -> None:
        """Записать метрики в лог."""
        level = os.getenv("METRICS_LOG_LEVEL", METRICS_LOG_LEVEL)

        if level == "minimal":
            entry: dict = {
                "type": "pipeline_metrics",
                "request_id": self.request_id,
                "status": self.status,
                "total_latency_ms": self.total_latency_ms,
                "error": self.error_message,
            }
        else:
            entry = {
                "type": "pipeline_metrics",
                "request_id": self.request_id,
                "agent_id": self.agent_id,
                "session_id": self.session_id,
                "status": self.status,
                "total_latency_ms": self.total_latency_ms,
                "llm_latency_ms": self.llm_latency_ms,
                "mcp_calls_count": self.mcp_calls_count,
                "mcp_total_latency_ms": self.mcp_total_latency_ms if self.mcp_calls_count > 0 else None,
                "llm_model": self.llm_model,
                "tokens_input": self.tokens_input,
                "tokens_output": self.tokens_output,
                "mcp_service": self.mcp_service,
                "mcp_last_action": self.mcp_last_action,
                "mcp_last_result": self.mcp_last_result,
                "action_taken": self.action_taken,
                "error": self.error_message,
            }

            if level == "detailed":
                entry["message_text"] = self.message_text
                entry["response_text"] = self.response_text
                entry["ts_received"] = self.ts_received
                entry["ts_llm_start"] = self.ts_llm_start
                entry["ts_llm_end"] = self.ts_llm_end
                entry["ts_response_sent"] = self.ts_response_sent

        # Remove None values for cleaner logs
        entry = {k: v for k, v in entry.items() if v is not None}
        logger.info(json.dumps(entry, ensure_ascii=False))
