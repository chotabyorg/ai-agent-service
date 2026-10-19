"""Voice decisions remain mandatory on providers rejecting named tool_choice."""
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import select

from core.config import get_settings
from models.agent import AgentTemplate
from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from tests.fakes import build_llm_response
from tests.test_business_runtime import later
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_managed_turns import execute
from tests.test_pending_proposal_decision import decision
from tests.test_preparation_schema_correction import preparation
from tests.test_text_booking_recovery import failure
from tests.test_voice_latency import profile


@pytest.mark.integration
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('followup', ['read', 'second_decision'])
async def test_unrelated_voice_decision_restores_reads_without_unlocking_proposal(
        api_client, db_session, monkeypatch, stream, followup):
    from tests.test_booking_workflow import tool
    from tests.test_voice_search_progress import install
    value, args, calls, model, searches = await install(api_client, db_session, monkeypatch)
    async with db_session() as db:
        agent = await db.get(AgentTemplate, UUID(value['agent_id']))
    monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles', [
        profile(value, agent.model_name, stream)])
    model.chat.completions.queue(preparation('booking_create', args, 'prepare'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    async with db_session() as db:
        proposal_id = str((await db.scalars(select(BusinessPlan))).one().id)
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id, 'UNRELATED')]))
    if followup == 'read':
        model.chat.completions.queue(tool('resource_search', {}, 'read'),
                                     build_llm_response(content='У нас работает Анна.'))
    else:
        model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM')]))
    request = later(ManagedTurnRequest.model_validate(value), 'Какие мастера у вас работают?')
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    names = {tool['function']['name'] for tool in model.chat.completions.calls[2]['tools']}
    assert 'resource_search' in names and 'business_decide_proposal' not in names
    if followup == 'read':
        assert result['status'] == 'DRAFT_READY' and len(searches) == 1
    else:
        assert result['status'] == 'FAILED' and result['error_code'] == 'TOOL_SETUP_FAILED'
        assert not searches
    assert calls['execute'] == 0
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert str(plan.id) == proposal_id and plan.proposal_state == 'PREPARED'
        assert operation.attempt_count == 0


@pytest.mark.integration
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('intent,text,state,effects', [
    ('CONFIRM', 'Да, подтверждаю запись.', 'CONFIRMED', 1),
    ('CHANGE', 'Нет, измените время на час позже.', 'CANCELLED', 0),
    ('REJECT', 'Нет, не записывайте.', 'CANCELLED', 0),
    ('UNRELATED', 'Подскажите ваш адрес.', 'PREPARED', 0),
])
async def test_voice_pending_decision_does_not_require_provider_forced_choice(
        api_client, db_session, monkeypatch, stream, intent, text, state, effects):
    value, args, _, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='VOICE', allow_execute=True)
    if stream:
        async with db_session() as db:
            agent = await db.get(AgentTemplate, UUID(value['agent_id']))
        monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles', [profile(value, agent.model_name, True)])
    model.chat.completions.queue(preparation('booking_create', args, 'prepare'))
    proposed = await execute(api_client, value)
    assert proposed.json()['status'] == 'DRAFT_READY'
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        proposal_id = str(plan.id)
    captured = []
    create = model.chat.completions.create

    async def rejects_named_choice(**kwargs):
        captured.append(deepcopy(kwargs))
        if kwargs.get('tool_choice'):
            raise failure(502)
        return await create(**kwargs)

    monkeypatch.setattr(model.chat.completions, 'create', rejects_named_choice)
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id, intent)]),
                                 build_llm_response(content='Уточните, пожалуйста, нужные изменения.'))
    request = later(ManagedTurnRequest.model_validate(value), text)
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'DRAFT_READY'
    assert 'tool_choice' not in captured[0]
    assert [tool['function']['name'] for tool in captured[0]['tools']] == ['business_decide_proposal']
    assert calls['execute'] == effects
    assert len(effect.permit_requests) == effects
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert plan.proposal_state == state
        assert operation.attempt_count == effects
    if intent in {'CHANGE', 'REJECT'}:
        assert proposal_id not in captured[1]['messages'][0]['content']
        assert 'booking_create' in {tool['function']['name'] for tool in captured[1]['tools']}
        assert 'business_decide_proposal' not in {tool['function']['name'] for tool in captured[1]['tools']}


@pytest.mark.integration
@pytest.mark.parametrize('invalid', ['prose', 'wrong_proposal', 'wrong_tool', 'multiple'])
async def test_voice_without_forced_choice_still_rejects_invalid_decisions(
        api_client, db_session, monkeypatch, invalid):
    value, args, _, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='VOICE', allow_execute=True)
    model.chat.completions.queue(preparation('booking_create', args, 'prepare'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        proposal_id = str(plan.id)
    call = decision(proposal_id, 'CONFIRM')
    if invalid == 'wrong_proposal':
        call = decision('unselected-proposal', 'CONFIRM')
    if invalid == 'wrong_tool':
        call.function.name = 'booking_create'
    response = build_llm_response(tool_calls=[call])
    if invalid == 'prose':
        response = build_llm_response(content='Всё готово, вы записаны.')
    if invalid == 'multiple':
        response.choices[0].message.tool_calls.append(decision(proposal_id, 'CONFIRM'))
    model.chat.completions.queue(response)
    if invalid == 'prose':
        # A second prose response must still fail before any dispatch.
        model.chat.completions.queue(build_llm_response(content='Всё готово, вы записаны.'))
    request = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю запись.')
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'FAILED' and result['error_code'] == 'INVALID_RESULT'
    assert calls['execute'] == 0 and not effect.permit_requests
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert plan.proposal_state == 'PREPARED'
        assert operation.status == 'RESERVED' and operation.attempt_count == 0
