"""Sanitized stage timing for the synthetic paired HTTP business path."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _ms(start_ns: int, end_ns: int) -> float:
    return round((end_ns - start_ns) / 1_000_000, 3)


async def _wait_fake_acceptance(bridge, timeout=20):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with bridge.engines["provider"].connect() as connection:
            row = (await connection.execute(text(
                "SELECT created_at,accepted_at,decision FROM fake_requests "
                "WHERE kind='POST' ORDER BY sequence DESC LIMIT 1"
            ))).mappings().first()
        if row is not None and row["accepted_at"] is not None:
            return {
                "observed_monotonic_ns": time.monotonic_ns(),
                "request_created_at_utc": row["created_at"].isoformat(),
                "accepted_at_utc": row["accepted_at"].isoformat(),
                "decision": row["decision"],
            }
        await asyncio.sleep(0.005)
    raise AssertionError("Synthetic provider did not durably accept the expected write")


def _event(record):
    return {
        "request_received_monotonic_ns": record["received_at_monotonic_ns"],
        "upstream_response_monotonic_ns": record.get("upstream_response_at_monotonic_ns"),
        "response_sent_monotonic_ns": record.get("response_sent_at_monotonic_ns"),
        "status": record.get("response_status"),
    }


async def test_synthetic_write_stage_latency_trace(bridge):
    """Observe real service stages without exporting synthetic request bodies."""
    import time as monotonic_clock

    await bridge.script(slots=("12:00",))
    ingress = {}
    accepted = await bridge.telegram_ingest("Запиши меня на 12:00", trace=ingress)
    sequence = accepted["conversation"]["last_message_sequence"]
    turn = await bridge.turn(accepted, sequence, sequence)
    claim = await bridge.claim(turn)
    ready_observed_ns = monotonic_clock.monotonic_ns()
    bridge.provider_proxy.clear_capture()
    bridge.effect_proxy.clear_capture()

    provider_acceptance_task = asyncio.create_task(_wait_fake_acceptance(bridge))
    execute_started_ns = monotonic_clock.monotonic_ns()
    response = await bridge.execute(claim)
    execute_finished_ns = monotonic_clock.monotonic_ns()
    assert response.status_code == 200, response.text[:200]
    provider_acceptance = await provider_acceptance_task
    assert provider_acceptance["decision"] == "ACCEPTED"

    ai_calls = [row for row in bridge.provider_proxy.snapshot()
                if row["path"] == "/internal/v1/managed-turns/execute"]
    assert len(ai_calls) == 1
    ai_call = _event(ai_calls[0])
    assert ai_call["status"] == 200

    effect_rows = [row for row in bridge.effect_proxy.snapshot()
                   if row["path"] in {
                       "/internal/v1/external-effect-eligibility",
                       "/internal/v1/external-effect-permits",
                   }]
    eligibility = [_event(row) for row in effect_rows
                   if row["path"].endswith("eligibility")]
    permits = [_event(row) for row in effect_rows
               if row["path"].endswith("permits")]
    assert eligibility and all(event["status"] == 200 for event in eligibility)
    assert len(permits) == 1 and permits[0]["status"] == 200

    trace = {
        "measurement": "synthetic paired HTTP WRITE_SAFE booking.create",
        "model": "deterministic local fake; not real model latency",
        "business_binding": "active mutation-capable test binding",
        "clock": "monotonic_ns for all local stage durations; PostgreSQL UTC timestamps separately",
        "events": {
            "telegram_ingress_request_started_monotonic_ns": ingress[
                "ingress_request_started_monotonic_ns"],
            "telegram_ingress_accepted_monotonic_ns": ingress[
                "ingress_accepted_monotonic_ns"],
            "conversation_turn_claimed_ready_monotonic_ns": ready_observed_ns,
            "managed_ai_http": ai_call,
            "eligibility_http_calls": eligibility,
            "business_permit_http_calls": permits,
            "fake_provider_durable_acceptance": provider_acceptance,
            "execute_call_started_monotonic_ns": execute_started_ns,
            "execute_call_finished_monotonic_ns": execute_finished_ns,
        },
        "durations_ms": {
            "telegram_accept_to_turn_claimed_ready": _ms(
                ingress["ingress_accepted_monotonic_ns"], ready_observed_ns),
            "ready_observation_to_managed_ai_request": _ms(
                ready_observed_ns, ai_call["request_received_monotonic_ns"]),
            "managed_ai_service_response": _ms(
                ai_call["request_received_monotonic_ns"],
                ai_call["upstream_response_monotonic_ns"]),
            "business_permit_http_roundtrip": _ms(
                permits[0]["request_received_monotonic_ns"],
                permits[0]["upstream_response_monotonic_ns"]),
            "permit_response_to_fake_acceptance_observed": _ms(
                permits[0]["upstream_response_monotonic_ns"],
                provider_acceptance["observed_monotonic_ns"]),
            "complete_execute_call": _ms(execute_started_ns, execute_finished_ns),
        },
        "call_counts": {
            "eligibility_http": len(eligibility),
            "business_permit_http": len(permits),
            "business_provider_post": 1,
        },
        "not_exercised": {
            "conversation_outbound_delivery_authorization":
                "The paired runner starts Conversation, AI, and fake business provider; "
                "it does not start the separate Telegram outbound delivery worker.",
            "telegram_provider_acceptance": "Not exercised by this paired harness.",
            "unbound_and_strictly_read_only_preflight_counts":
                "Not measured by this mutation-capable binding scenario.",
            "settling_quiet_window":
                "This observation uses the actual Conversation turn-select/claim path "
                "with settling disabled; it does not include a configured quiet window.",
        },
    }

    output = os.environ.get("GOAL5_LATENCY_EVIDENCE")
    if output:
        path = Path(output)
        if not path.is_absolute() or not path.parent.is_dir():
            raise AssertionError("Latency evidence path must be absolute in an existing directory")
        with path.open("x", encoding="utf-8") as handle:
            json.dump(trace, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        path.chmod(0o600)

