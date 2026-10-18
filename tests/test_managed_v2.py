"""Absolute bounded windows through the managed wire/model and frozen Business seams."""

from tests.business_effect_helpers import open_business_runtime
import copy
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import services.llm_service as llm_module
from core.config import get_settings
from core.managed_security import EXECUTION_NAMESPACE
from models.business import BusinessOperation, BusinessPlan, BusinessTurnContext
from schemas.business import BusinessSafetyError
from schemas.managed_evidence import ManagedEvidenceRequest
from schemas.managed_turn import MAX_REQUEST_BYTES, ManagedTurnRequest, ManagedTurnResult, OperationReference
from services.business_repository import (
    BusinessRepository,
    request_fingerprint,
    request_fingerprint_matches,
)
from services.business_runtime import BusinessRuntime
from services.managed_runtime import privacy_id
from tests.fakes import build_llm_response
from tests.test_business_natural_confirmation import decide, setup_semantic
from tests.test_business_runtime import actions, setup_business
from tests.test_chat_flow import FakeKnowledgeBaseClient
from tests.test_managed_turns import execute, model, payload, setup


# Serialized size of defaulted streaming opt-in fields; keeps the exercised
# byte boundary exact after the wire format grew.
_STREAMING_WIRE_OVERHEAD = len(b'"stream_partials":false,"max_partial_chars":2000,')


def window_payload(*, first=245, last=500, selected_first=None, selected_last=None, **overrides):
    selected_first = last if selected_first is None else selected_first
    selected_last = last if selected_last is None else selected_last
    messages = [dict(id=str(uuid4()), sequence=sequence,
        actor="CUSTOMER" if sequence % 2 == 0 else "AGENT",
        direction="INBOUND" if sequence % 2 == 0 else "OUTBOUND",
        content_type="TEXT", text=f"canonical {sequence}") for sequence in range(first, last + 1)]
    selected = [m["id"] for m in messages if selected_first <= m["sequence"] <= selected_last]
    return payload(contract_version=2, input_first_sequence=selected_first,
        input_last_sequence=selected_last, context_last_sequence=last,
        messages=messages, selected_input_message_ids=selected, **overrides)


def bounded_later(request, text):
    """A later canonical window whose old proposal-origin Message is absent."""
    last = max(500, request.context_last_sequence + 1)
    value = window_payload(first=last - 255, last=last)
    for key in ("owner_id", "integration_id", "agent_id", "conversation_id", "channel", "authority_revision"):
        value[key] = request.model_dump(mode="json")[key]
    value["input_revision"] = request.input_revision + 1
    value["messages"][-1].update(actor="CUSTOMER", direction="INBOUND", text=text)
    return ManagedTurnRequest.model_validate(value)


@pytest.mark.parametrize("selected_first,indices", [(500, [255]), (497, [252, 253, 254, 255])])
def test_v2_absolute_window_selects_the_exact_canonical_range(selected_first, indices):
    request = ManagedTurnRequest.model_validate(window_payload(selected_first=selected_first))
    assert request.window_first_sequence == 245
    assert request.local_index(500) == 255
    assert request.selected_local_indices == indices
    assert [m.sequence for m in request.selected_messages] == list(range(selected_first, 501))
    assert list(request.selected_sequence_range) == list(range(selected_first, 501))
    assert request.simple_selection is (selected_first == 500)
    assert "window_first_sequence" not in request.model_dump()
    assert "context_first_sequence" not in request.model_json_schema()["properties"]
    for outside in (244, 501):
        with pytest.raises(ValueError, match="outside canonical window"):
            request.local_index(outside)
    result = ManagedTurnResult.from_request(request, status="NO_RESPONSE", draft_text=None, error_code=None)
    assert result.contract_version == 2 and result.context_last_sequence == 500


@pytest.mark.parametrize("change", ["empty", "gap", "duplicate_sequence", "reorder", "wrong_last",
    "input_before_window", "input_after_window", "selection_wrong_id", "selection_reorder",
    "selection_missing_id", "duplicate_id", "count", "huge_end", "version_bool", "version_three"])
def test_v2_rejects_invalid_canonical_windows_and_selection(change):
    value = window_payload(selected_first=497)
    if change == "empty":
        value["messages"] = []
    elif change == "gap":
        value["messages"][1]["sequence"] += 1
    elif change == "duplicate_sequence":
        value["messages"][1]["sequence"] = 245
    elif change == "reorder":
        value["messages"][0], value["messages"][1] = value["messages"][1], value["messages"][0]
    elif change == "wrong_last":
        value["context_last_sequence"] = 501
    elif change == "input_before_window":
        value["input_first_sequence"] = 244
    elif change == "input_after_window":
        value["input_last_sequence"] = 501
    elif change == "selection_wrong_id":
        value["selected_input_message_ids"][0] = str(uuid4())
    elif change == "selection_reorder":
        value["selected_input_message_ids"].reverse()
    elif change == "selection_missing_id":
        value["selected_input_message_ids"].pop()
    elif change == "duplicate_id":
        value["messages"][1]["id"] = value["messages"][0]["id"]
    elif change == "count":
        value["messages"].insert(0, {**value["messages"][0], "id": str(uuid4()), "sequence": 244})
    elif change == "huge_end":
        value["context_last_sequence"] = value["messages"][-1]["sequence"] = 9223372036854775807
    else:
        value["contract_version"] = True if change == "version_bool" else 3
    with pytest.raises(ValidationError):
        ManagedTurnRequest.model_validate(value)


@pytest.mark.parametrize("count", [255, 256, 257])
def test_v2_message_count_boundary(count):
    value = window_payload(first=500 - count + 1)
    if count <= 256:
        assert len(ManagedTurnRequest.model_validate(value).messages) == count
    else:
        with pytest.raises(ValidationError):
            ManagedTurnRequest.model_validate(value)


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_v2_utf8_text_byte_boundary(delta):
    value = window_payload(first=485)
    for message in value["messages"]:
        message["text"] = "🌍" * 8192
    if delta == -1:
        value["messages"][0]["text"] = "🌍" * 8191 + "€"
    elif delta == 1:
        value["messages"].insert(0, {**value["messages"][0], "sequence": 484, "id": str(uuid4()), "text": "x"})
    assert sum(len(m["text"].encode()) for m in value["messages"]) == 524288 + delta
    if delta <= 0:
        ManagedTurnRequest.model_validate(value)
    else:
        with pytest.raises(ValidationError, match="Projection exceeds limit"):
            ManagedTurnRequest.model_validate(value)


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_v2_full_serialized_request_byte_boundary(delta):
    value = window_payload(first=469)
    for message in value["messages"]:
        message["text"] = "x" * 16000
    compact = lambda: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    controls, multibyte = divmod(
        MAX_REQUEST_BYTES + delta - _STREAMING_WIRE_OVERHEAD - len(compact()), 5
    )
    for message in value["messages"]:
        take = min(16000, controls)
        message["text"] = "\x01" * take + "x" * (16000 - take)
        controls -= take
    value["messages"][-1]["text"] = "é" * multibyte + value["messages"][-1]["text"][multibyte:]
    assert len(compact()) == MAX_REQUEST_BYTES + delta - _STREAMING_WIRE_OVERHEAD
    assert sum(len(m["text"].encode()) for m in value["messages"]) <= 524288
    if delta <= 0:
        assert len(ManagedTurnRequest.model_validate(value).model_dump_json().encode()) == MAX_REQUEST_BYTES + delta
    else:
        with pytest.raises(ValidationError, match="Request exceeds limit"):
            ManagedTurnRequest.model_validate(value)


def test_v1_pre_stream_and_extension_fingerprint_vectors_remain_distinct():
    value = payload(conversation_id=str(UUID(int=1)), turn_id=str(UUID(int=2)), owner_id="Exact Café owner",
        integration_id=str(UUID(int=3)), agent_id=str(UUID(int=4)), channel="WEBSITE_WIDGET",
        messages=[dict(id=str(UUID(int=5)), sequence=1, actor="CUSTOMER", direction="INBOUND",
            content_type="TEXT", text="Café 🌍")], selected_input_message_ids=[str(UUID(int=5))])
    explicit = ManagedTurnRequest.model_validate(value)
    value.pop("contract_version")
    omitted = ManagedTurnRequest.model_validate(value)
    assert explicit.model_dump_json() == omitted.model_dump_json()
    assert explicit.window_first_sequence == 1 and explicit.selected_local_indices == [0]
    assert request_fingerprint(explicit) == request_fingerprint(omitted) == \
        "0bc08928c202523069f4465c469b6207b6a065e8b97b4ef7f9a9ac23c93ddcee"
    extended = ManagedTurnRequest.model_validate({**value, "stream_partials": False})
    assert request_fingerprint(extended) == \
        "8fd6425840d08077bef77aece4e6a811ce20babeaa95d7d2b0f7172d7a0d1195"
    v2 = ManagedTurnRequest.model_validate({**value, "contract_version": 2})
    assert request_fingerprint(v2) != request_fingerprint(explicit)
    assert privacy_id(v2) == privacy_id(explicit)
    assert EXECUTION_NAMESPACE == "main-chota.conversation.v1"
    retained_result = ManagedTurnResult.from_request(explicit, status="NO_RESPONSE", draft_text=None, error_code=None)
    assert ManagedTurnResult.model_validate(retained_result.model_dump()).contract_version == 1
    old_window = window_payload()
    old_window["contract_version"] = 1
    with pytest.raises(ValidationError):
        ManagedTurnRequest.model_validate(old_window)
    old_window.pop("contract_version")
    with pytest.raises(ValidationError):
        ManagedTurnRequest.model_validate(old_window)


def test_only_omitted_stream_fields_get_legacy_business_context_fallback():
    base = payload(
        conversation_id=str(UUID(int=1)), turn_id=str(UUID(int=2)), owner_id="Exact Café owner",
        integration_id=str(UUID(int=3)), agent_id=str(UUID(int=4)), channel="WEBSITE_WIDGET",
        messages=[dict(id=str(UUID(int=5)), sequence=1, actor="CUSTOMER", direction="INBOUND",
            content_type="TEXT", text="Café 🌍")], selected_input_message_ids=[str(UUID(int=5))],
    )
    omitted = ManagedTurnRequest.model_validate(base)
    old_extension_digest = "8fd6425840d08077bef77aece4e6a811ce20babeaa95d7d2b0f7172d7a0d1195"
    assert request_fingerprint_matches(old_extension_digest, omitted)

    changed_core = {**base, "messages": [{**base["messages"][0], "text": "changed"}]}
    changed = ManagedTurnRequest.model_validate(changed_core)
    assert not request_fingerprint_matches(old_extension_digest, changed)

    explicit_default = ManagedTurnRequest.model_validate({**base, "stream_partials": False})
    explicit_custom = ManagedTurnRequest.model_validate({**base, "max_partial_chars": 501})
    assert not request_fingerprint_matches(request_fingerprint(omitted), explicit_default)
    assert not request_fingerprint_matches(old_extension_digest, explicit_custom)
    assert request_fingerprint(explicit_custom) != request_fingerprint(omitted)


@pytest.mark.integration
@pytest.mark.parametrize("selected_first", [500, 497])
@pytest.mark.parametrize("model_budget", [131072, 10000])
async def test_v2_model_receives_correct_selected_messages_and_kb_query(
        api_client, db_session, monkeypatch, model, selected_first, model_budget):
    agent, value = await setup(api_client, db_session, monkeypatch)
    value.update(window_payload(agent_id=agent["id"], selected_first=selected_first,
        owner_id=value["owner_id"], integration_id=value["integration_id"]))
    FakeKnowledgeBaseClient.reset()
    monkeypatch.setattr(llm_module, "KnowledgeBaseClient", FakeKnowledgeBaseClient)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", True)
    monkeypatch.setattr(get_settings(), "internal_api_token", "test-internal")
    monkeypatch.setattr(get_settings(), "managed_ai_context_bytes", model_budget)
    model.chat.completions.queue(build_llm_response(content="selected answer"))
    response = await execute(api_client, value)
    assert response.status_code == 200 and response.json()["status"] == "DRAFT_READY"
    assert response.json()["contract_version"] == 2
    model_messages = model.chat.completions.calls[0]["messages"]
    assert model_messages[-1] == dict(role="user", content="canonical 500")
    assert model_messages[-2] == dict(role="assistant", content="canonical 499")
    expected = "\n\n".join(f"canonical {i}" for i in range(selected_first, 501))
    assert FakeKnowledgeBaseClient.calls[0]["query"] == expected
    selectors = [m for m in model_messages if "communication positions" in m["content"]]
    assert len(selectors) == (0 if selected_first == 500 else 1)
    communication = [m for m in model_messages if m["role"] != "system"]
    assert [m["content"] for m in communication[-(501 - selected_first):]] == \
        [f"canonical {i}" for i in range(selected_first, 501)]
    if model_budget == 10000:
        assert not any(m["content"] == "canonical 245" for m in communication)
    if selectors:
        positions = [communication.index(next(m for m in communication if m["content"] == f"canonical {i}")) + 1
                     for i in range(selected_first, 501)]
        assert "positions " + ", ".join(map(str, positions)) + "." in selectors[0]["content"]
        if model_budget == 131072:
            assert positions == [253, 254, 255, 256]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["+", "ага", "ок", "👍", "давайте"])
async def test_v2_natural_confirmation_uses_frozen_origin_outside_window_and_retains_receipt(db_session, text):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    request = bounded_later(original.request, text)
    assert original.request.input_last_sequence < request.window_first_sequence
    assert not set(original.request.selected_input_message_ids) & {m.id for m in request.messages}
    customer = await open_business_runtime(request, original.grant, db_session, registry)
    assert (await customer.prompt_context())["pending"]["proposal_id"] == str(proposal.id)
    result = await decide(customer, proposal, "CONFIRM")
    assert result.halt_status is None and provider.posts == 1
    expected_id = str(proposal.operations[0].id)
    refs = customer.collector.snapshot()
    assert refs == [OperationReference(origin="AI_AGENT_SERVICE", operation_id=expected_id, outcome="SUCCEEDED")]
    async with db_session() as session:
        stored = await session.get(BusinessPlan, proposal.id)
        assert stored.confirmation_input_ids == [str(request.selected_messages[0].id)]
        assert stored.confirmation_policy == "SEMANTIC_CUSTOMER_TURN_V1"
        operation = await session.get(BusinessOperation, proposal.operations[0].id)
        frozen = (operation.request_payload, operation.request_fingerprint, operation.provider_idempotency_key,
                  operation.receipt_evidence, operation.receipt_id, operation.receipt_fingerprint)
    restarted = await open_business_runtime(ManagedTurnRequest.model_validate_json(request.model_dump_json()),
        original.grant, db_session, registry)
    await decide(restarted, proposal, "CONFIRM")
    assert restarted.collector.snapshot() == refs and provider.posts == 1
    async with db_session() as session:
        operation = await session.get(BusinessOperation, proposal.operations[0].id)
        assert (operation.request_payload, operation.request_fingerprint, operation.provider_idempotency_key,
                operation.receipt_evidence, operation.receipt_id, operation.receipt_fingerprint) == frozen
        assert len((await session.scalars(select(BusinessOperation))).all()) == 1
    identity = request.model_dump(include={"owner_id", "integration_id", "channel", "agent_id", "conversation_id", "turn_id"})
    evidence = ManagedEvidenceRequest(**identity)
    assert evidence.contract_version == 1
    assert await BusinessRepository.read_evidence(evidence, original.grant, db_session) == refs


@pytest.mark.asyncio
async def test_v2_explicit_confirmation_uses_shared_selection(db_session):
    original, provider, registry, _ = await setup_business(db_session, confirmation=True)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    request = bounded_later(original.request, "confirm " + str(proposal.id))
    customer = await open_business_runtime(request, original.grant, db_session, registry)
    assert (await customer.confirm(proposal.id)).halt_status is None
    assert provider.posts == 1
    assert customer.collector.snapshot()[0].operation_id == str(proposal.operations[0].id)


@pytest.mark.asyncio
async def test_v2_change_still_freezes_replacement_and_requires_later_customer_turn(db_session):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    request = bounded_later(original.request, "да, только лучше в 19")
    changer = await open_business_runtime(request, original.grant, db_session, registry)
    assert (await decide(changer, proposal, "CHANGE")).tool_payload["status"] == "CANCELLED"
    replacement = await changer.freeze_plan(actions("19:00"), propose=True)
    assert replacement.id != proposal.id and provider.posts == 0
    assert (await decide(changer, replacement, "CONFIRM")).halt_status == "FAILED"
    confirmer = await open_business_runtime(bounded_later(request, "+"), original.grant, db_session, registry)
    await decide(confirmer, replacement, "CONFIRM")
    assert provider.posts == 1 and replacement.operations[0].command.arguments["slot"] == "19:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,text,state", [("REJECT", "нет", "CANCELLED"), ("UNRELATED", "Как вас найти?", "PREPARED")])
async def test_v2_reject_and_unrelated_leave_effect_policy_unchanged(db_session, intent, text, state):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    customer = await open_business_runtime(bounded_later(original.request, text), original.grant, db_session, registry)
    assert (await decide(customer, proposal, intent)).halt_status is None
    assert provider.posts == 0 and not customer.collector.snapshot()
    assert (await decide(customer, proposal, "CONFIRM")).halt_status == "FAILED"
    async with db_session() as session:
        assert (await session.get(BusinessPlan, proposal.id)).proposal_state == state


@pytest.mark.asyncio
async def test_v2_unknown_remains_unknown_after_exact_restart_and_blocks_later_effect(db_session):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    request = bounded_later(original.request, "+")
    customer = await open_business_runtime(request, original.grant, db_session, registry)
    provider.lose_next = True
    assert (await decide(customer, proposal, "CONFIRM")).halt_status == "UNKNOWN"
    refs = customer.collector.snapshot()
    assert refs[0].outcome == "UNKNOWN" and provider.posts == 1
    restarted = await open_business_runtime(request, original.grant, db_session, registry)
    assert (await decide(restarted, proposal, "CONFIRM")).halt_status == "UNKNOWN"
    assert restarted.collector.snapshot() == refs and provider.posts == 1
    later = await open_business_runtime(bounded_later(request, "Еще запись"), original.grant, db_session, registry)
    assert later.recovery_mode
    with pytest.raises(BusinessSafetyError, match="RECOVERY_READ_ONLY"):
        await later.freeze_plan(actions("19:00"), propose=True)
    retained = await later.repository.selected_plan(later.request, later.grant)
    assert retained.operations[0].status == "UNKNOWN"
    assert provider.posts == 1


@pytest.mark.asyncio
async def test_retained_turn_rejects_changed_window_identity_and_wire_version(db_session):
    original, _, registry = await setup_semantic(db_session)
    request = bounded_later(original.request, "+")
    await open_business_runtime(request, original.grant, db_session, registry)
    restarted = ManagedTurnRequest.model_validate_json(request.model_dump_json())
    assert request_fingerprint(request) == request_fingerprint(restarted)
    await open_business_runtime(restarted, original.grant, db_session, registry)
    changed_window = request.model_dump(mode="json")
    changed_window["messages"].pop(0)
    changed_identity = copy.deepcopy(request.model_dump(mode="json"))
    changed_identity["messages"][0]["id"] = str(uuid4())
    for changed in (changed_window, changed_identity):
        with pytest.raises(BusinessSafetyError, match="TURN_CONFLICT"):
            await open_business_runtime(ManagedTurnRequest.model_validate(changed), original.grant, db_session, registry)
    changed_version = original.request.model_dump(mode="json")
    changed_version["contract_version"] = 2
    with pytest.raises(BusinessSafetyError, match="TURN_CONFLICT"):
        await open_business_runtime(ManagedTurnRequest.model_validate(changed_version), original.grant, db_session, registry)


@pytest.mark.asyncio
async def test_existing_business_turn_context_replays_old_defaulted_hash_but_rejects_changed_core(
    db_session, monkeypatch,
):
    from services import business_repository as business_repository_module

    current_fingerprint = business_repository_module.request_fingerprint

    def old_defaulted_fingerprint(request):
        fields = request.model_dump(mode="json")
        fields["stream_partials"] = False
        fields["max_partial_chars"] = 2000
        return current_fingerprint(ManagedTurnRequest.model_validate(fields))

    # Insert the exact former AI representation from source history; do not
    # mutate an immutable retained row to manufacture the compatibility case.
    with monkeypatch.context() as legacy_insert:
        legacy_insert.setattr(
            business_repository_module, "request_fingerprint", old_defaulted_fingerprint
        )
        original, _, registry = await setup_semantic(db_session)
    wire = original.request.model_dump(mode="json")
    wire.pop("stream_partials")
    wire.pop("max_partial_chars")
    replay_request = ManagedTurnRequest.model_validate(wire)
    extension_request = ManagedTurnRequest.model_validate({
        **wire, "stream_partials": False, "max_partial_chars": 2000,
    })
    old_defaulted_hash = request_fingerprint(extension_request)
    async with db_session() as session, session.begin():
        context = await session.scalar(select(BusinessTurnContext).where(
            BusinessTurnContext.turn_id == replay_request.turn_id
        ))
        assert context is not None
        assert context.request_fingerprint == old_defaulted_hash

    # The existing context uses the old AI full-default digest. The retained
    # BusinessTurnContext matcher accepts that exact historical shape while
    # the request itself uses the pre-stream wire representation.
    await open_business_runtime(replay_request, original.grant, db_session, registry)

    changed = replay_request.model_dump(mode="json")
    changed.pop("stream_partials")
    changed.pop("max_partial_chars")
    changed["messages"][-1]["text"] += " changed"
    with pytest.raises(BusinessSafetyError, match="TURN_CONFLICT"):
        await open_business_runtime(
            ManagedTurnRequest.model_validate(changed), original.grant, db_session, registry
        )


@pytest.mark.asyncio
async def test_legacy_context_recovery_keeps_effect_permit_and_operation_reference(db_session, monkeypatch):
    from schemas.conversation_effects import managed_request_fingerprint
    from services import business_repository as business_repository_module

    current_fingerprint = business_repository_module.request_fingerprint

    def old_defaulted_fingerprint(request):
        fields = request.model_dump(mode="json")
        fields["stream_partials"] = False
        fields["max_partial_chars"] = 2000
        return current_fingerprint(ManagedTurnRequest.model_validate(fields))

    # Seed the immutable Business context as an older AI release did, then
    # exercise the normal omitted-fields wire request through effect acquisition.
    with monkeypatch.context() as legacy_insert:
        legacy_insert.setattr(
            business_repository_module, "request_fingerprint", old_defaulted_fingerprint
        )
        runtime, provider, registry, _ = await setup_business(db_session)

    provider.lose_next = True
    result = await runtime.execute_plan(actions("18:00"))
    assert result.halt_status == "UNKNOWN" and provider.posts == 1 and provider.lookups == 0
    initial_ref = runtime.collector.snapshot()[0]
    assert initial_ref.outcome == "UNKNOWN"
    operation_id = UUID(initial_ref.operation_id)

    async with db_session() as session:
        operation = await session.get(BusinessOperation, operation_id)
        retained_effect = (
            operation.effect_request_turn_id,
            operation.effect_request_fingerprint,
            operation.effect_managed_request_fingerprint,
            operation.effect_operation_fingerprint,
            operation.effect_permit_id,
        )
    assert retained_effect[0] == runtime.request.turn_id
    assert retained_effect[1] and retained_effect[3] and retained_effect[4]
    assert retained_effect[2] == managed_request_fingerprint(runtime.request)
    assert len(runtime.effect_port.permit_requests) == 1
    assert runtime.effect_port.permit_requests[0].managed_request_fingerprint == retained_effect[2]

    legacy_wire = runtime.request.model_dump(mode="json", exclude_unset=True)
    assert "stream_partials" not in legacy_wire and "max_partial_chars" not in legacy_wire
    reloaded = ManagedTurnRequest.model_validate_json(
        json.dumps(legacy_wire, ensure_ascii=False)
    )
    restarted = await open_business_runtime(reloaded, runtime.grant, db_session, registry)
    gate = await restarted.repository.result_gate(reloaded, runtime.grant)
    assert gate.disposition == "UNKNOWN" and not gate.request_conflict
    assert gate.references == [initial_ref]

    reconciled = await restarted.reconcile(operation_id)
    assert reconciled.operations[0].status == "SUCCEEDED"
    assert provider.posts == 1 and provider.lookups == 1
    assert restarted.collector.snapshot() == [initial_ref.model_copy(update={"outcome": "SUCCEEDED"})]
    terminal_gate = await restarted.repository.result_gate(reloaded, runtime.grant)
    assert terminal_gate.disposition == "ALL_SUCCEEDED" and not terminal_gate.request_conflict
    assert terminal_gate.references == restarted.collector.snapshot()

    async with db_session() as session:
        operation = await session.get(BusinessOperation, operation_id)
        assert (
            operation.effect_request_turn_id,
            operation.effect_request_fingerprint,
            operation.effect_managed_request_fingerprint,
            operation.effect_operation_fingerprint,
            operation.effect_permit_id,
        ) == retained_effect


@pytest.mark.asyncio
async def test_v2_selected_customer_id_overlap_still_rejects_confirmation(db_session):
    original, provider, registry = await setup_semantic(db_session)
    proposal = await original.freeze_plan(actions("18:00"), propose=True)
    value = bounded_later(original.request, "+").model_dump(mode="json")
    original_id = str(original.request.selected_input_message_ids[0])
    value["messages"][-1]["id"] = original_id
    value["selected_input_message_ids"] = [original_id]
    customer = await open_business_runtime(ManagedTurnRequest.model_validate(value), original.grant, db_session, registry)
    assert (await customer.prompt_context())["pending"] is None
    assert (await decide(customer, proposal, "CONFIRM")).halt_status == "FAILED"
    assert provider.posts == 0
