"""Split or ungrounded contacts must not spend the unrelated booking READs."""
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import func, select

from core.config import get_settings
from models.business import BusinessOperation, BusinessPlan
from schemas.business import BusinessToolResult
from schemas.managed_turn import ManagedTurnRequest
from tests.business_effect_helpers import open_business_runtime
from tests.test_booking_identity_search import managed_setup
from tests.test_booking_search_options import ARGUMENTS, install_reads
from tests.test_booking_workflow import tool
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_chotacrm_managed_proposal_errors import existing_customer_lookup, install_managed_crm
from tests.test_managed_turns import execute


@pytest.mark.integration
@pytest.mark.parametrize('speech', [
    'Запишите меня завтра на стрижку. Меня зовут Влад.',
    'Запишите меня завтра. Мой телефон +375299998877.',
    'Запишите меня. Телефон +375291112233 или +375299998877.',
])
async def test_contact_question_precedes_all_searches_without_an_extra_completion(
        api_client, db_session, monkeypatch, speech):
    value, arguments, calls, effect, model, reads = await managed_setup(
        api_client, db_session, monkeypatch, 'VOICE', existing_customer_lookup())
    value['messages'][0]['text'] = speech
    before = deepcopy(value)
    search = {**ARGUMENTS, 'customer': arguments['customer']}
    original = deepcopy(search)
    model.chat.completions.queue(tool('booking_search_options', search, 'search'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY', result
    assert 'номер' in result['draft_text'] and 'Подтверждаете' not in result['draft_text']
    assert len(model.chat.completions.calls) == 1
    assert reads == [] and calls['lookup'] == calls['facts'] == calls['execute'] == 0
    assert not effect.permit_requests and search == original and value == before
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0


@pytest.mark.integration
@pytest.mark.parametrize('blocked', ['gate', 'retained', 'guard_failure'])
async def test_early_question_preserves_unknown_and_retained_evidence(
        api_client, db_session, monkeypatch, blocked):
    value, arguments, registry, calls, effect, _ = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='VOICE')
    value['messages'][0]['text'] = 'Запишите меня завтра. Меня зовут Влад.'
    request = ManagedTurnRequest.model_validate(value)
    grant = next(g for g in get_settings().managed_principals[0].grants
        if g.scope() == (value['owner_id'], UUID(value['integration_id']), 'VOICE', UUID(value['agent_id'])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect)
    await runtime.prompt_context()
    expected = BusinessToolResult(halt_status='UNKNOWN', error_code='TOOL_EXECUTION_UNKNOWN')

    async def guard(**kwargs):
        if blocked == 'guard_failure':
            raise RuntimeError('Synthetic authority read failed')
        return expected if blocked == 'gate' else None

    monkeypatch.setattr(runtime, 'execution_guard', guard)
    monkeypatch.setattr(runtime, '_halt', lambda unknown: expected)
    if blocked == 'retained':
        monkeypatch.setattr(runtime.collector, 'snapshot', lambda: [object()])
    reads = install_reads(runtime)
    result = await runtime.call_tool('booking_search_options', {**ARGUMENTS, 'customer': arguments['customer']})
    assert result is expected and result.safe_draft is None
    assert reads == [] and calls['lookup'] == calls['facts'] == calls['execute'] == 0
    assert not effect.permit_requests
