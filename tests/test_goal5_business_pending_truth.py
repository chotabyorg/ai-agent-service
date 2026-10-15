"""A model's final prose cannot overrule a retained pending proposal."""

from uuid import UUID

import pytest
from sqlalchemy import select

import services.llm_service as llm_module
from models.business import BusinessOperation, BusinessPlan
from schemas.conversation_effects import EffectReason
from services.business_runtime import BusinessRuntime
from tests.business_effect_helpers import FakeConversationEffectPort
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_turns import execute, setup


@pytest.fixture
def model(monkeypatch):
    client = ScriptedLLMClient()
    monkeypatch.setattr(llm_module, "AsyncOpenAI", lambda **kwargs: client)
    return client


class PendingTruthEffectPort(FakeConversationEffectPort):
    """Explicit authority results for the exact same managed request replay."""

    def __init__(self):
        super().__init__()
        self.mode = "eligible"
        self.unavailable_requests = []
        self.denied_requests = []
        self.denied_responses = []

    async def eligibility(self, request):
        if self.mode == "unavailable":
            self.eligibility_requests.append(request)
            self.unavailable_requests.append(request)
            raise ConnectionError("synthetic Conversation authority unavailable")
        if self.mode == "denied":
            self.denied_requests.append(request)
            was_eligible, old_reason = self.eligible, self.denial_reason
            try:
                self.eligible = False
                self.denial_reason = EffectReason.AUTHORITY_CHANGED
                result = await super().eligibility(request)
                self.denied_responses.append(result)
                return result
            finally:
                self.eligible, self.denial_reason = was_eligible, old_reason
        return await super().eligibility(request)


async def _plan_state(db_session):
    async with db_session() as session:
        plan = (await session.scalars(select(BusinessPlan))).one()
        operations = (await session.scalars(
            select(BusinessOperation).where(BusinessOperation.plan_id == plan.id)
            .order_by(BusinessOperation.ordinal)
        )).all()
        plan_state = (
            plan.id, plan.scope_id, plan.origin_turn_id, plan.proposal_state,
            plan.confirmed_turn_id, tuple(plan.confirmation_input_ids or ()),
            plan.confirmation_policy, plan.confirmed_at, plan.plan_fingerprint,
        )
        operation_state = tuple((
            operation.id, operation.plan_id, operation.scope_id, operation.origin_turn_id,
            operation.ordinal, operation.status, operation.state_version,
            operation.dispatch_epoch, operation.attempt_count,
            operation.effect_request_turn_id, operation.effect_request_fingerprint,
            operation.effect_managed_request_fingerprint,
            operation.effect_operation_fingerprint, operation.effect_permit_id,
            operation.receipt_id,
        ) for operation in operations)
        return plan_state, operation_state


@pytest.mark.integration
@pytest.mark.parametrize("authority_mode", ["unavailable", "denied"])
async def test_same_request_retained_proposal_replaces_scripted_false_success(
        api_client, db_session, monkeypatch, model, authority_mode):
    """Exercise HTTP routing, LLM finalization and the durable pending-plan gate."""
    _, value = await setup(api_client, db_session, monkeypatch)
    port = PendingTruthEffectPort()
    setup_open = BusinessRuntime.open.__func__

    async def open_with_effect_port(cls, *args, **kwargs):
        kwargs["effect_port"] = port
        return await setup_open(cls, *args, **kwargs)

    monkeypatch.setattr(BusinessRuntime, "open", classmethod(open_with_effect_port))
    provider = await install_business(monkeypatch, value, db_session, confirmation=True)

    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("proposal", "business_propose", plan_arguments())
    ]), build_llm_response(content="Бронирование завершено и подтверждено."))

    # First request creates an exact OWN PREPARED plan. Re-submit the exact
    # same body after authority changes; no new Turn or confirmation is used.
    proposed_response = await execute(api_client, value)
    assert proposed_response.status_code == 200
    proposed = proposed_response.json()
    assert proposed["status"] == "DRAFT_READY"
    assert len(proposed["operation_refs"]) == 1
    assert proposed["operation_refs"][0]["outcome"] is None
    before = await _plan_state(db_session)
    plan_id = before[0][0]
    operation_id = before[1][0][0]
    assert before[0][2] == UUID(value["turn_id"])
    assert before[0][3] == "PREPARED"
    assert len(before[1]) == 1 and before[1][0][5] == "RESERVED"
    assert all(field is None for field in before[1][0][9:14])
    assert proposed["operation_refs"] == [{
        "origin": "AI_AGENT_SERVICE", "operation_id": str(operation_id), "outcome": None,
    }]
    assert port.permit_requests == [] and provider.commands == []

    call_count_before = len(port.eligibility_requests)
    port.mode = authority_mode
    repeated_response = await execute(api_client, value)
    assert repeated_response.status_code == 200
    repeated = repeated_response.json()

    # This result passes through LLMService.final_result_guard and the managed
    # endpoint's final guard. Scripted model prose is not allowed to claim the
    # pending mutation succeeded while authority is unavailable or denies it.
    assert repeated["status"] == "DRAFT_READY"
    assert repeated["draft_text"] == (
        "Сохранённое предложение сейчас нельзя подтвердить. "
        "Действие по нему не выполнялось."
    )
    assert repeated["draft_text"] != "Бронирование завершено и подтверждено."
    assert repeated["operation_refs"] == [{
        "origin": "AI_AGENT_SERVICE", "operation_id": str(operation_id), "outcome": None,
    }]
    assert len(model.chat.completions.calls) == 2
    assert not model.chat.completions._responses

    new_requests = port.eligibility_requests[call_count_before:]
    assert new_requests
    assert all(request.turn_id == UUID(value["turn_id"])
               and request.origin_turn_id == UUID(value["turn_id"])
               for request in new_requests)
    if authority_mode == "unavailable":
        assert port.unavailable_requests
    else:
        assert port.denied_requests
        assert all(response.reason == EffectReason.AUTHORITY_CHANGED
                   and response.eligible is False
                   for response in port.denied_responses)
        assert all(request in new_requests for request in port.denied_requests)

    after = await _plan_state(db_session)
    assert after == before
    assert after[0][0] == plan_id
    assert after[1][0][0] == operation_id
    assert port.permit_requests == []
    assert provider.commands == []
