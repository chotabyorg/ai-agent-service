"""An unrenderable stale proposal projection must stay read-only."""

import pytest

from tests.test_goal5_business_stale_intent import later_runtime, semantic_business


@pytest.mark.asyncio
async def test_unrenderable_stale_summary_cannot_authorize_change(db_session):
    original, provider, registry, old_plan, port = await semantic_business(db_session)
    _, stale = await later_runtime(
        db_session, original, registry, port, "Book a different time")

    def fail_summary(_candidate):
        raise ValueError("synthetic unsafe projection")

    stale._proposal_summary = fail_summary
    context = await stale.prompt_context()

    assert context["pending"]["proposal_id"] == str(old_plan.id)
    assert context["pending"]["confirmable"] is False
    assert stale._stale_split_current_eligible is False
    assert stale.effect_eligible is False

    result = await stale.call_tool("business_decide_proposal", {
        "proposal_id": str(old_plan.id), "intent": "CHANGE",
    })
    assert result.tool_payload["status"] == "STALE_PROPOSAL_RETAINED"

    retained = await original.repository.lookup_plan(
        original.request, original.grant, old_plan.id)
    assert retained.proposal_state == "PREPARED"
    assert all(op.status == "RESERVED" and op.effect_permit_id is None
               and op.effect_request_turn_id is None for op in retained.operations)
    assert provider.posts == 0 and port.permit_requests == []
