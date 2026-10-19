"""Visible completion guard, finite observations and retained Business authority."""

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from openai.types.chat.chat_completion import ChatCompletion

import core.managed_completion_diagnostics as diagnostics
from core.managed_security import managed_logging, install_managed_log_boundary
import services.managed_completion as completion
from services.business_runtime import BusinessRuntime
from services.llm_service import LLMService, _to_namespace
from services.completion_timing import CompletionTiming
from schemas.managed_turn import ManagedTurnRequest
from tests.fakes import build_llm_response, build_tool_call
from tests.test_managed_business_evidence import evidence, retained_rows
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_turns import execute, model, payload, setup

SECRET = "completion-private-sentinel"


@pytest.fixture(autouse=True)
def capture_production_logger(monkeypatch, request):
    # Alembic's test setup can disable pre-existing Uvicorn loggers. Production
    # uses Uvicorn's own config, covered separately in a fresh subprocess below.
    if request.node.get_closest_marker("integration"):
        request.getfixturevalue("migrated_postgres")
    install_managed_log_boundary()
    logger = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(logger, "level", logging.INFO)


def observed(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "managed_completion_observed"]


def assert_private(caplog):
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in repr(record.__dict__)
        assert SECRET not in logging.Formatter("%(name)s %(message)s").format(record)


@pytest.mark.integration
@pytest.mark.parametrize("content,state", [(None, "MISSING"), ("", "EMPTY"),
    (" \n\t", "WHITESPACE"), ("<thinking>" + SECRET + "</thinking>", "STRIPPED_EMPTY"),
    (5, "INVALID_TYPE"), ({"secret": SECRET}, "INVALID_TYPE")])
async def test_empty_final_is_invalid_without_fallback_or_effect(
        api_client, db_session, monkeypatch, model, caplog, content, state):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(content=content))
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert (result["status"], result["error_code"], result["draft_text"]) == ("FAILED", "INVALID_RESULT", None)
    assert result["operation_refs"] == [] and provider.commands == []
    assert len(model.chat.completions.calls) == 1 and model.closed
    assert len(observed(caplog)) == 1 and observed(caplog)[0].visible_content == state
    assert_private(caplog)


@pytest.mark.integration
@pytest.mark.parametrize("finish,expected", [("stop", "STOP"), ("length", "LENGTH"),
    ("content_filter", "CONTENT_FILTER"), (None, "MISSING"), (SECRET, "OTHER"), (7, "OTHER")])
async def test_finish_metadata_cannot_reject_visible_answer(
        api_client, db_session, monkeypatch, model, caplog, finish, expected):
    _, value = await setup(api_client, db_session, monkeypatch)
    model.chat.completions.queue(build_llm_response(content=" Ответ готов. ", finish_reason=finish))
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and result["draft_text"] == "Ответ готов."
    assert observed(caplog)[0].finish == expected
    assert_private(caplog)


@pytest.mark.integration
async def test_literal_fallback_and_missing_finish_are_valid_text(api_client, db_session, monkeypatch, model, caplog):
    _, value = await setup(api_client, db_session, monkeypatch)
    reply = build_llm_response(content="I processed your request.")
    del reply.choices[0].finish_reason
    model.chat.completions.queue(reply)
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and result["draft_text"] == "I processed your request."
    assert observed(caplog)[0].finish == "MISSING"


class Hostile:
    def __getattribute__(self, name):
        raise AssertionError("unsupported properties must not run")
    def __str__(self):
        raise AssertionError("unsupported coercion must not run")
    def __repr__(self):
        raise AssertionError("unsupported repr must not run")


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["choices_missing", "choices_empty", "choices_wrong", "message_missing",
                                      "message_wrong", "tools_wrong", "response_hostile", "content_missing"])
async def test_invalid_shape_is_closed_and_never_dispatches(
        api_client, db_session, monkeypatch, model, caplog, kind):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    reply = build_llm_response(content="valid")
    if kind == "choices_missing": del reply.choices
    elif kind == "choices_empty": reply.choices = []
    elif kind == "choices_wrong": reply.choices = Hostile()
    elif kind == "message_missing": del reply.choices[0].message
    elif kind == "message_wrong": reply.choices[0].message = Hostile()
    elif kind == "tools_wrong": reply.choices[0].message.tool_calls = Hostile()
    elif kind == "response_hostile": reply = Hostile()
    else: del reply.choices[0].message.content
    if kind == "response_hostile":
        async def create(**kwargs):
            # The usual fake's isinstance(Exception) check inspects __class__.
            model.chat.completions.calls.append(kwargs)
            return reply
        monkeypatch.setattr(model.chat.completions, "create", create)
    else:
        model.chat.completions.queue(reply)
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result["error_code"] == "INVALID_RESULT" and result["draft_text"] is None
    assert provider.commands == [] and len(model.chat.completions.calls) == 1
    assert_private(caplog)


@pytest.mark.parametrize("shape", ["sdk", "proxy"])
@pytest.mark.integration
async def test_real_sdk_and_proxy_storage_observation(api_client, db_session, monkeypatch, model, caplog, shape):
    _, value = await setup(api_client, db_session, monkeypatch)
    raw = {"id": SECRET, "model": SECRET, "created": 1, "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "Готово", "reasoning": SECRET}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12,
                  "completion_tokens_details": {"reasoning_tokens": 3},
                  "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 1},
                  "cost": 0.000001239, "private_extra": SECRET}, "private_extra": SECRET}
    reply = ChatCompletion.model_validate(raw) if shape == "sdk" else _to_namespace(raw)
    model.chat.completions.queue(reply)
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and result["draft_text"] == "Готово"
    record = observed(caplog)[0]
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens, record.reasoning_tokens) == (5, 7, 12, 3)
    assert (record.cached_tokens, record.cache_write_tokens, record.cost_nano_credits) == (4, 1, 1239)
    assert record.turn_id == value["turn_id"] and record.step == 1
    assert type(record.model_ms) is int and record.model_ms >= 0
    assert 'first_delta_ms' not in record.__dict__  # Buffered completion has no token clock.
    assert_private(caplog)


@pytest.mark.parametrize("bad", [True, -1, 2147483648, 1.5, float("nan"), float("inf"), SECRET, None])
def test_invalid_counts_are_omitted_without_changing_content(caplog, bad):
    reply = build_llm_response(content="valid")
    reply.usage = SimpleNamespace(prompt_tokens=bad, completion_tokens=bad, total_tokens=bad,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=bad))
    selected = completion.select_managed_completion(reply, LLMService._strip_thinking_tags)
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            completion.observe_managed_completion(reply, selected, request=ManagedTurnRequest.model_validate(payload()), step=1)
    finally:
        managed_logging.reset(mark)
    assert selected.answer == "valid" and len(observed(caplog)) == 1
    assert all(name not in observed(caplog)[0].__dict__ for name in diagnostics._COUNTS)
    assert_private(caplog)


def test_hostile_usage_is_not_inspected_and_first_choice_policy_stays(caplog):
    reply = build_llm_response(content="first")
    reply.choices.append(Hostile())
    reply.usage = Hostile()
    reply.choices[0].finish_reason = Hostile()
    selected = completion.select_managed_completion(reply, LLMService._strip_thinking_tags)
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            completion.observe_managed_completion(reply, selected, request=ManagedTurnRequest.model_validate(payload()), step=1)
    finally:
        managed_logging.reset(mark)
    assert selected.answer == "first" and observed(caplog)[0].finish == "OTHER"


@pytest.mark.parametrize('bad', [None, True, -1, 2147483648, 1.5, float('nan'), SECRET])
def test_only_finite_local_timings_reach_the_log(caplog, bad):
    reply = build_llm_response(content='valid')
    # Provider fields with the same names do not supply local measurements.
    reply.model_ms = SECRET
    reply.first_delta_ms = SECRET
    timing = CompletionTiming(model_ms=1200, first_delta_ms=bad)
    selected = completion.select_managed_completion(reply, str.strip)
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            completion.observe_managed_completion(reply, selected,
                request=ManagedTurnRequest.model_validate(payload()), step=1, timing=timing)
    finally:
        managed_logging.reset(mark)
    record = observed(caplog)[0]
    assert record.model_ms == 1200 and 'first_delta_ms' not in record.__dict__
    assert selected.answer == 'valid'
    assert_private(caplog)


@pytest.mark.integration
async def test_succeeded_receipt_survives_empty_final_and_exact_replay(
        api_client, db_session, fake_redis, monkeypatch, model, caplog):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())], finish_reason=SECRET),
        build_llm_response(content="<thinking>" + SECRET + "</thinking>"))
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result["status"] == "UNKNOWN" and result["error_code"] == "INVALID_RESULT"
    assert result["draft_text"] is None and result["operation_refs"][0]["outcome"] == "SUCCEEDED"
    assert len(provider.commands) == 1 and len(model.chat.completions.calls) == 2
    assert [r.branch for r in observed(caplog)] == ["TOOL_CALLS", "FINAL"]
    assert (await evidence(api_client, value)).json()["operation_refs"] == result["operation_refs"]
    model.chat.completions.queue(build_llm_response(content="Сохранённый результат"))
    replay = (await execute(api_client, value)).json()
    assert replay["operation_refs"] == result["operation_refs"] and len(provider.commands) == 1
    assert SECRET not in repr(await retained_rows(db_session)) and await fake_redis.keys("*") == []
    assert_private(caplog)


@pytest.mark.integration
@pytest.mark.parametrize("invalid_shape", [False, True])
async def test_fresh_durable_proposal_wins_and_replay_needs_no_model(
        api_client, db_session, monkeypatch, model, invalid_shape):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session, confirmation=True)
    active = []
    original = BusinessRuntime.open
    async def capture(cls, *args, **kwargs):
        runtime = await original(*args, **kwargs)
        active.append(runtime)
        return runtime
    monkeypatch.setattr(BusinessRuntime, "open", classmethod(capture))
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        await active[-1].freeze_plan(json.loads(plan_arguments())["actions"], propose=True)
        return SimpleNamespace(choices=[]) if invalid_shape else build_llm_response(content="")
    monkeypatch.setattr(model.chat.completions, "create", create)
    result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and "confirm " in result["draft_text"]
    assert result["operation_refs"][0]["outcome"] is None and provider.commands == []
    replay = (await execute(api_client, value)).json()
    assert replay["draft_text"] == result["draft_text"] and replay["operation_refs"] == result["operation_refs"]
    assert len(calls) == 1


@pytest.mark.integration
@pytest.mark.parametrize("guard_kind", ["failure_draft", "unknown", "incomplete"])
async def test_authoritative_final_guard_precedes_empty_rejection(
        api_client, db_session, monkeypatch, model, guard_kind):
    _, value = await setup(api_client, db_session, monkeypatch)
    original = BusinessRuntime.final_result_guard
    async def guard(self, *, allow_incomplete=False, reload=True):
        if not allow_incomplete and model.chat.completions.calls:
            return self._failed_business_draft() if guard_kind == "failure_draft" else self._halt(guard_kind == "unknown")
        return await original(self, allow_incomplete=allow_incomplete, reload=reload)
    monkeypatch.setattr(BusinessRuntime, "final_result_guard", guard)
    model.chat.completions.queue(build_llm_response(content=""))
    result = (await execute(api_client, value)).json()
    if guard_kind == "failure_draft":
        assert result["status"] == "DRAFT_READY" and result["draft_text"] == BusinessRuntime._failed_business_draft().safe_draft
    else:
        assert result["status"] == ("UNKNOWN" if guard_kind == "unknown" else "FAILED")
        assert result["error_code"] != "INVALID_RESULT" and result["draft_text"] is None


@pytest.mark.integration
async def test_observation_failure_cannot_change_result_or_cleanup(api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    def broken(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(completion, "emit_completion_diagnostic", broken)
    model.chat.completions.queue(build_llm_response(content="valid"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and result["draft_text"] == "valid" and model.closed


def diagnostic(**changes):
    return diagnostics.CompletionDiagnostic(**{ "turn_id": uuid4(), "step": 1, "branch": "FINAL",
        "finish": "STOP", "visible_content": "TEXT", **changes})


@pytest.mark.parametrize("changes", [{"step": True}, {"step": 0}, {"step": 21}, {"turn_id": SECRET},
    {"branch": "FINAL_" + SECRET}, {"finish": "STOP_" + SECRET}, {"visible_content": SECRET},
    {"prompt_tokens": True}, {"reasoning_tokens": -1}, {"total_tokens": 2147483648},
    {"cached_tokens": True}, {"cache_write_tokens": -1},
    {"cost_nano_credits": True}, {"cost_nano_credits": -1}, {"cost_nano_credits": 10**18+1},
    {"model_ms": True}, {"first_delta_ms": -1}, {"model_ms": 2147483648},
    {"http_status": True}, {"http_status": 99}, {"http_status": 600}, {"http_status": SECRET}])
def test_identity_is_not_enough_for_invalid_dto(caplog, changes):
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            diagnostics.emit_completion_diagnostic(diagnostic(**changes))
    finally:
        managed_logging.reset(mark)
    assert observed(caplog) == [] and caplog.records[-1].getMessage() == "Managed execution diagnostic"
    assert_private(caplog)


def test_trusted_record_discards_all_original_fields_and_fake_identity_cannot_escape(caplog):
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("arbitrary." + SECRET).warning(SECRET + " %s", SECRET,
                exc_info=(RuntimeError, RuntimeError(SECRET), None), stack_info=True,
                extra={"_completion_identity": diagnostics._IDENTITY,
                       "_completion_diagnostic": diagnostic(), "secret_extra": SECRET})
            logging.getLogger("uvicorn.error").warning(SECRET,
                extra={"_completion_identity": "_IDENTITY", "_completion_diagnostic": diagnostic()})
    finally:
        managed_logging.reset(mark)
    assert len(observed(caplog)) == 1
    record = observed(caplog)[0]
    assert record.name == "uvicorn.error" and record.pathname == "" and record.exc_info is None and record.stack_info is None
    assert_private(caplog)


@pytest.mark.parametrize("boundary_absent", [False, True])
def test_emitter_fails_closed_without_context_or_installed_boundary(monkeypatch, boundary_absent):
    calls = []
    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "info", lambda *args, **kwargs: calls.append((args, kwargs)))
    if boundary_absent:
        monkeypatch.setattr(logging.Logger, "makeRecord", lambda *args, **kwargs: None)
    mark = managed_logging.set(boundary_absent)
    try:
        diagnostics.emit_completion_diagnostic(diagnostic(visible_content=SECRET))
    finally:
        managed_logging.reset(mark)
    assert calls == []


def test_absent_canonical_request_only_suppresses_observation(caplog):
    reply = build_llm_response(content="valid")
    selected = completion.select_managed_completion(reply, LLMService._strip_thinking_tags)
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            completion.observe_managed_completion(reply, selected, request=None, step=1)
    finally:
        managed_logging.reset(mark)
    assert selected.answer == "valid" and observed(caplog) == []


@pytest.mark.integration
async def test_existing_step_budget_bounds_observations_and_never_repeats_effect(
        api_client, db_session, monkeypatch, model, caplog):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(*[build_llm_response(tool_calls=[
        build_tool_call(str(index), "business_execute_plan", plan_arguments())], finish_reason="length")
        for index in range(LLMService.MAX_TOOL_STEPS)])
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result["status"] == "UNKNOWN" and result["error_code"] == "MODEL_ERROR"
    assert result["operation_refs"][0]["outcome"] == "SUCCEEDED" and len(provider.commands) == 1
    assert [r.step for r in observed(caplog)] == list(range(1, 21))
    assert all(r.branch == "TOOL_CALLS" and r.finish == "LENGTH" for r in observed(caplog))


async def test_concurrent_legacy_and_managed_records_remain_isolated(caplog):
    async def managed():
        mark = managed_logging.set(True)
        try:
            await asyncio.sleep(0)
            diagnostics.emit_completion_diagnostic(diagnostic())
        finally:
            managed_logging.reset(mark)
    async def legacy():
        await asyncio.sleep(0)
        logging.getLogger("legacy").warning("legacy-preserved", extra={"legacy_extra": "unchanged"})
    with caplog.at_level(logging.INFO):
        await asyncio.gather(managed(), legacy())
    assert len(observed(caplog)) == 1
    assert any(r.getMessage() == "legacy-preserved" and r.legacy_extra == "unchanged" for r in caplog.records)


def test_production_uvicorn_formatter_renders_finite_fields_without_secrets():
    source = '''
import logging.config
from uuid import UUID
from uvicorn.config import LOGGING_CONFIG
from core.managed_security import install_managed_log_boundary, managed_logging
from core.managed_completion_diagnostics import CompletionDiagnostic, emit_completion_diagnostic
logging.config.dictConfig(LOGGING_CONFIG)
install_managed_log_boundary()
token=managed_logging.set(True)
emit_completion_diagnostic(CompletionDiagnostic(UUID('00000000-0000-0000-0000-000000000001'),1,'FINAL','LENGTH','EMPTY',completion_tokens=1024))
logging.getLogger('uvicorn.error').warning('completion-private-sentinel %s','completion-private-sentinel',exc_info=(ValueError,ValueError('completion-private-sentinel'),None),extra={'secret':'completion-private-sentinel'})
managed_logging.reset(token)
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, env=env, timeout=20)
    assert result.returncode == 0
    output = result.stdout + result.stderr
    assert "managed_completion_observed turn_id=00000000-0000-0000-0000-000000000001 step=1 branch=FINAL finish=LENGTH visible_content=EMPTY completion_tokens=1024" in output
    assert SECRET not in output and "Logging error" not in output
