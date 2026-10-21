"""Historical pre-dispatch authority keeps its original immutable identity."""

import json
from uuid import uuid4

import pytest

from schemas.managed_turn import ManagedTurnRequest
from schemas.business import BusinessSafetyError
from schemas.conversation_effects import managed_request_fingerprint, retained_managed_fingerprint_matches
from services import business_repository as repository_module
from tests.business_effect_helpers import open_business_runtime
from tests.test_business_runtime import actions, make_request, setup_business


def extension_request(request):
    fields = request.model_dump(mode="json")
    fields.update(stream_partials=False, max_partial_chars=2000)
    return ManagedTurnRequest.model_validate(fields)


async def historical_acquisition(factory, monkeypatch, phase, *, damaged=None, mode="LOOKUP"):
    business_hash = repository_module.request_fingerprint
    managed_hash = repository_module.managed_request_fingerprint
    with monkeypatch.context() as old_release:
        old_release.setattr(repository_module, "request_fingerprint",
                            lambda request: business_hash(extension_request(request)))
        old_release.setattr(repository_module, "managed_request_fingerprint",
                            lambda request: "f" * 64 if damaged == "managed" else
                            managed_hash(extension_request(request)))
        if damaged == "command":
            old_release.setattr(repository_module, "frozen_command_fingerprint", lambda command: "f" * 64)
        runtime, provider, registry, _ = await setup_business(factory, mode=mode)
        plan = await runtime.freeze_plan(actions("12:00"))
        acquisition = await runtime.repository.prepare_effect_acquisition(
            runtime.request, runtime.grant, plan.id, plan.operations[0].id,
            runtime.registration, runtime.facts_resolver(actions("12:00")),
        )
        assert acquisition.request is not None and acquisition.permit_id is None
        permit = None
        if phase != "BEFORE_PERMIT":
            permit = await runtime.effect_port.permit(acquisition.request)
        if phase == "BOUND":
            await runtime.repository.bind_effect_permit(
                runtime.request, runtime.grant, plan.id, plan.operations[0].id,
                acquisition.request, permit.permit_id,
            )
    wire = runtime.request.model_dump(mode="json", exclude_unset=True)
    assert "stream_partials" not in wire and "max_partial_chars" not in wire
    request = ManagedTurnRequest.model_validate_json(json.dumps(wire, ensure_ascii=False))
    restarted = await open_business_runtime(request, runtime.grant, factory, registry,
                                            effect_port=runtime.effect_port)
    return runtime, restarted, provider, plan, acquisition, permit


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["BEFORE_PERMIT", "REPLY_LOST", "BOUND"])
async def test_original_turn_resumes_exact_historical_acquisition(db_session, monkeypatch, phase):
    original, restarted, provider, plan, acquisition, permit = await historical_acquisition(
        db_session, monkeypatch, phase,
    )
    assert provider.posts == 0
    assert await restarted.execution_guard() is None
    result = await restarted.execute_plan(actions("12:00"))
    assert result.halt_status is None and provider.posts == 1
    retained = await restarted.repository.selected_plan(restarted.request, restarted.grant)
    operation = retained.operations[0]
    assert operation.id == plan.operations[0].id and operation.status == "SUCCEEDED"
    assert operation.effect_managed_request_fingerprint == acquisition.request.managed_request_fingerprint
    assert operation.effect_request_fingerprint == repository_module.sidecar_fingerprint(acquisition.request)
    assert operation.effect_operation_fingerprint == acquisition.request.operation_fingerprint
    assert operation.effect_request_turn_id == original.request.turn_id
    if permit is not None:
        assert operation.effect_permit_id == permit.permit_id
    expected_calls = 2 if phase == "REPLY_LOST" else 1
    assert len(restarted.effect_port.permit_requests) == expected_calls
    assert all(request == acquisition.request for request in restarted.effect_port.permit_requests)
    assert len(restarted.collector.snapshot()) == 1
    assert restarted.collector.snapshot()[0].operation_id == str(operation.id)
    assert restarted.collector.snapshot()[0].outcome == "SUCCEEDED"


@pytest.mark.parametrize("change", ["owner", "integration", "agent", "conversation", "turn",
                                   "input", "authority", "text", "stream", "limit", "contract"])
def test_retained_profile_match_cannot_change_semantic_identity(change):
    request = make_request(uuid4())
    stored = managed_request_fingerprint(extension_request(request))
    assert retained_managed_fingerprint_matches(stored, request)
    fields = request.model_dump(mode="json", exclude_unset=True)
    if change == "owner":
        fields["owner_id"] += " changed"
    elif change in {"integration", "agent", "conversation", "turn"}:
        fields[change + "_id"] = str(uuid4())
    elif change in {"input", "authority"}:
        fields[change + "_revision"] += 1
    elif change == "text":
        fields["messages"][-1]["text"] += " changed"
    elif change == "stream":
        fields.update(contract_version=2, stream_partials=True)
    elif change == "limit":
        fields["max_partial_chars"] = 501
    else:
        fields["contract_version"] = 2
    assert not retained_managed_fingerprint_matches(stored, ManagedTurnRequest.model_validate(fields))
    # An explicit current profile may not fall back to the pre-extension digest.
    assert not retained_managed_fingerprint_matches(
        managed_request_fingerprint(request), extension_request(request),
    )
    assert not retained_managed_fingerprint_matches("f" * 64, request)
    assert not retained_managed_fingerprint_matches(None, request)


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged", ["managed", "command"])
async def test_unrecognized_retained_tuple_cannot_mint_replacement_authority(db_session, monkeypatch, damaged):
    _, restarted, provider, plan, acquisition, _ = await historical_acquisition(
        db_session, monkeypatch, "BEFORE_PERMIT", damaged=damaged,
    )
    with pytest.raises(BusinessSafetyError, match="EFFECT_REQUEST_CONFLICT"):
        await restarted.repository.prepare_effect_acquisition(
            restarted.request, restarted.grant, plan.id, plan.operations[0].id,
            restarted.registration, restarted.facts_resolver(actions("12:00")),
        )
    retained = await restarted.repository.selected_plan(restarted.request, restarted.grant)
    assert retained.operations[0].effect_request_fingerprint == repository_module.sidecar_fingerprint(acquisition.request)
    assert retained.operations[0].status == "RESERVED" and retained.operations[0].effect_permit_id is None
    assert provider.posts == 0 and not restarted.effect_port.permit_requests


@pytest.mark.asyncio
async def test_exact_historical_request_still_requires_positive_conversation_permit(db_session, monkeypatch):
    _, restarted, provider, plan, acquisition, _ = await historical_acquisition(
        db_session, monkeypatch, "BEFORE_PERMIT",
    )
    restarted.effect_port.eligible = False
    result = await restarted.execute_plan(actions("12:00"))
    assert result.tool_payload["status"] == "FAILED" and provider.posts == 0
    assert restarted.effect_port.permit_requests == [acquisition.request]
    retained = await restarted.repository.selected_plan(restarted.request, restarted.grant)
    assert retained.id == plan.id and retained.operations[0].status == "FAILED"
    assert retained.operations[0].effect_permit_id is None
    assert retained.operations[0].effect_request_fingerprint == repository_module.sidecar_fingerprint(acquisition.request)


@pytest.mark.asyncio
async def test_historical_acquisition_response_loss_stays_recover_only(db_session, monkeypatch):
    original, restarted, provider, plan, acquisition, _ = await historical_acquisition(
        db_session, monkeypatch, "REPLY_LOST",
    )
    provider.lose_next = True
    result = await restarted.execute_plan(actions("12:00"))
    assert result.halt_status == "UNKNOWN" and provider.posts == 1
    calls = list(restarted.effect_port.permit_requests)
    reloaded = await open_business_runtime(
        restarted.request, original.grant, db_session, restarted.registry,
        effect_port=restarted.effect_port,
    )
    guard = await reloaded.execution_guard()
    assert guard.halt_status == "UNKNOWN" and provider.posts == 1
    reconciled = await reloaded.reconcile(plan.operations[0].id)
    assert reconciled.operations[0].status == "SUCCEEDED"
    assert provider.posts == 1 and provider.lookups == 1
    assert reloaded.effect_port.permit_requests == calls
    assert all(request == acquisition.request for request in calls)


@pytest.mark.asyncio
async def test_historical_unknown_native_replay_keeps_original_permit_profile(db_session, monkeypatch):
    original, restarted, provider, plan, acquisition, _ = await historical_acquisition(
        db_session, monkeypatch, "BOUND", mode="NATIVE",
    )
    provider.lose_next = True
    lost = await restarted.execute_plan(actions("12:00"))
    assert lost.halt_status == "UNKNOWN" and provider.posts == 1
    retained = await restarted.repository.selected_plan(restarted.request, restarted.grant)
    permit_calls = list(restarted.effect_port.permit_requests)
    reloaded = await open_business_runtime(
        restarted.request, original.grant, db_session, restarted.registry,
        effect_port=restarted.effect_port,
    )
    assert (await reloaded.execution_guard()).halt_status == "UNKNOWN"
    recovered = await reloaded.replay_native(plan.operations[0].id)
    assert recovered.operations[0].status == "SUCCEEDED"
    assert provider.posts == 2 and len(provider.objects) == 1
    assert provider.keys[0] == provider.keys[1]
    assert recovered.operations[0].command == retained.operations[0].command
    assert recovered.operations[0].effect_permit_id == retained.operations[0].effect_permit_id
    assert recovered.operations[0].effect_managed_request_fingerprint == acquisition.request.managed_request_fingerprint
    assert reloaded.effect_port.permit_requests == permit_calls
