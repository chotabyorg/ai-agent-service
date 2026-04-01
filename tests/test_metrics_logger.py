"""Tests for PipelineMetrics structured logging."""

import json
import logging
import time
from unittest.mock import patch

import pytest

from services.metrics_logger import PipelineMetrics


class TestPipelineMetricsLatency:
    def test_total_latency_ms_calculated_correctly(self):
        metrics = PipelineMetrics()
        metrics.ts_received = 1000.0
        metrics.ts_response_sent = 1001.5

        assert metrics.total_latency_ms == 1500.0

    def test_llm_latency_ms_calculated_correctly(self):
        metrics = PipelineMetrics()
        metrics.ts_llm_start = 1000.0
        metrics.ts_llm_end = 1000.2

        assert metrics.llm_latency_ms == 200.0

    def test_total_latency_none_when_timestamps_missing(self):
        metrics = PipelineMetrics()
        metrics.ts_received = 1000.0

        assert metrics.total_latency_ms is None

    def test_llm_latency_none_when_timestamps_missing(self):
        metrics = PipelineMetrics()
        assert metrics.llm_latency_ms is None


class TestPipelineMetricsMcp:
    def test_mcp_calls_aggregated(self):
        metrics = PipelineMetrics()
        metrics.mark_mcp_start(service="bitrix24")
        time.sleep(0.01)
        metrics.mark_mcp_end(action="create_lead", result="success")

        metrics.mark_mcp_start(service="bitrix24")
        time.sleep(0.01)
        metrics.mark_mcp_end(action="update_lead", result="success")

        assert metrics.mcp_calls_count == 2
        assert metrics.mcp_total_latency_ms > 0
        assert metrics.mcp_last_action == "update_lead"
        assert metrics.mcp_last_result == "success"

    def test_mcp_total_latency_accumulates(self):
        metrics = PipelineMetrics()

        metrics.mark_mcp_start()
        time.sleep(0.01)
        metrics.mark_mcp_end()

        first_latency = metrics.mcp_total_latency_ms

        metrics.mark_mcp_start()
        time.sleep(0.01)
        metrics.mark_mcp_end()

        assert metrics.mcp_total_latency_ms > first_latency
        assert metrics.mcp_calls_count == 2


class TestPipelineMetricsError:
    def test_mark_error_sets_status(self):
        metrics = PipelineMetrics()
        metrics.mark_error("Something went wrong")

        assert metrics.status == "error"
        assert metrics.error_message == "Something went wrong"

    def test_initial_status_is_success(self):
        metrics = PipelineMetrics()
        assert metrics.status == "success"


class TestPipelineMetricsEmit:
    def _capture_emit(self, metrics: PipelineMetrics, log_level: str) -> dict:
        captured = []
        with patch.dict("os.environ", {"METRICS_LOG_LEVEL": log_level}):
            with patch("services.metrics_logger.logger") as mock_logger:
                mock_logger.info.side_effect = lambda msg: captured.append(json.loads(msg))
                metrics.emit()
        assert len(captured) == 1
        return captured[0]

    def test_normal_mode_excludes_message_text(self):
        metrics = PipelineMetrics()
        metrics.ts_received = 1000.0
        metrics.ts_response_sent = 1001.0
        metrics.message_text = "Hello"
        metrics.response_text = "Hi there"

        entry = self._capture_emit(metrics, "normal")

        assert "message_text" not in entry
        assert "response_text" not in entry
        assert "total_latency_ms" in entry

    def test_detailed_mode_includes_all_timestamps(self):
        metrics = PipelineMetrics()
        metrics.ts_received = 1000.0
        metrics.ts_llm_start = 1000.1
        metrics.ts_llm_end = 1000.5
        metrics.ts_response_sent = 1001.0
        metrics.message_text = "Hello"
        metrics.response_text = "Hi there"

        entry = self._capture_emit(metrics, "detailed")

        assert entry["ts_received"] == 1000.0
        assert entry["ts_llm_start"] == 1000.1
        assert entry["ts_llm_end"] == 1000.5
        assert entry["ts_response_sent"] == 1001.0
        assert entry["message_text"] == "Hello"
        assert entry["response_text"] == "Hi there"

    def test_minimal_mode_includes_only_base_fields(self):
        metrics = PipelineMetrics()
        metrics.ts_received = 1000.0
        metrics.ts_response_sent = 1001.0
        metrics.agent_id = "agent-123"
        metrics.llm_model = "gemini-flash"

        entry = self._capture_emit(metrics, "minimal")

        assert "type" in entry
        assert "request_id" in entry
        assert "status" in entry
        assert "total_latency_ms" in entry
        assert "agent_id" not in entry
        assert "llm_model" not in entry

    def test_emit_omits_none_values(self):
        metrics = PipelineMetrics()
        metrics.ts_received = 1000.0
        metrics.ts_response_sent = 1001.0

        entry = self._capture_emit(metrics, "normal")

        for value in entry.values():
            assert value is not None

    def test_mcp_total_latency_excluded_when_no_mcp_calls(self):
        metrics = PipelineMetrics()
        metrics.ts_received = 1000.0
        metrics.ts_response_sent = 1001.0

        entry = self._capture_emit(metrics, "normal")

        assert "mcp_total_latency_ms" not in entry
