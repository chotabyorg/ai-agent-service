"""Historical pre-dispatch authority keeps its original immutable identity."""

import json

import pytest

from schemas.managed_turn import ManagedTurnRequest
from services import business_repository as repository_module
from tests.business_effect_helpers import open_business_runtime
from tests.test_business_runtime import actions, setup_business


def extension_request(request):
    fields = request.model_dump(mode="json")
    fields.update(stream_partials=False, max_partial_chars=2000)
    return ManagedTurnRequest.model_validate(fields)


async def historical_acquisition(factory, monkeypatch, phase):
    business_hash = repository_module.request_fingerprint
    managed_hash = repository_module.managed_request_fingerprint
    with monkeypatch.context() as old_release:
        old_release.setattr(repository_module, "request_fingerprint",
                            lambda request: business_hash(extension_request(request)))
        old_release.setattr(repository_module, "managed_request_fingerprint",
                            lambda request: managed_hash(extension_request(request)))
        runtime, provider, registry, _ = await setup_business(factory)
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
