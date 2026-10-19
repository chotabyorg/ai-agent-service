"""A split utterance must request its missing contact without failing the turn."""
from copy import deepcopy
import json
from uuid import UUID

import pytest
from sqlalchemy import func, select

from core.config import get_settings
from models.business import BusinessOperation, BusinessPlan
from schemas.business import BusinessToolResult
from schemas.managed_turn import ManagedTurnRequest
from tests.business_effect_helpers import open_business_runtime
from tests.fakes import build_llm_response, build_tool_call
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm, preparation_arguments
from tests.test_business_runtime import later
from tests.test_managed_turns import execute


@pytest.mark.integration
@pytest.mark.parametrize('tool', ['booking_create', 'business_propose'])
@pytest.mark.parametrize('kind,name', [('OTHER', 'Артем'), ('PHONE', 'Артем'), ('PHONE', ' ')])
async def test_missing_spoken_contact_is_a_question_before_lookup_or_reservation(
        api_client, db_session, monkeypatch, tool, kind, name):
    value, args, registry, calls, effect, _ = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='VOICE')
    value['messages'][0]['text'] = (
        'Запишите меня завтра в два часа на мужскую стрижку к Светлане. Меня зовут Артем.')
    request = ManagedTurnRequest.model_validate(value)
    request_bytes = request.model_dump_json()
    grant = next(grant for grant in get_settings().managed_principals[0].grants
        if grant.scope() == (value['owner_id'], UUID(value['integration_id']),
                             value['channel'], UUID(value['agent_id'])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect)
    await runtime.prompt_context()
    args['customer'] = {'display_name': name,
                        'identity': {'kind': kind, 'value': '+375000000000'}}
    arguments = preparation_arguments(tool, args)
    original = deepcopy(arguments)
    result = await runtime.call_tool(tool, arguments)
    assert result.halt_status is None and result.error_code is None
    assert result.safe_draft and 'номер' in result.safe_draft
    assert 'Подтверждаете' not in result.safe_draft
    assert calls['lookup'] == calls['facts'] == calls['execute'] == 0
    assert not effect.permit_requests


    assert arguments == original and request.model_dump_json() == request_bytes
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0

    # The following STT fragment supplies the actual contact. A new ordinary
    # preparation validates the model-authored arguments and freezes a proposal.
    following = later(request, 'Телефон +375291112233.')
    next_runtime = await open_business_runtime(following, grant, db_session, registry, effect_port=effect)
    await next_runtime.prompt_context()
    args['customer'] = {'display_name': 'Артем',
                        'identity': {'kind': 'PHONE', 'value': '+375291112233'}}
    prepared = await next_runtime.call_tool(tool, preparation_arguments(tool, args))
    assert prepared.halt_status is None and 'Подтверждаете' in prepared.safe_draft
    assert calls['lookup'] == 1 and calls['facts'] >= 1 and calls['execute'] == 0
    assert not effect.permit_requests
    async with db_session() as db:
        operations = (await db.scalars(select(BusinessOperation))).all()
        assert len(operations) == 1 and operations[0].status == 'RESERVED'
        assert operations[0].attempt_count == 0


@pytest.mark.integration
@pytest.mark.parametrize('channel,speech', [
    ('VOICE', 'Телефон +375291112233.'),
    ('WEBSITE_WIDGET', 'Запишите меня завтра.'),
])
async def test_other_channels_and_known_phone_still_reject_invalid_customer(
        api_client, db_session, monkeypatch, channel, speech):
    value, args, registry, calls, effect, _ = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel=channel)
    value['messages'][0]['text'] = speech
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
        if grant.scope() == (value['owner_id'], UUID(value['integration_id']),
                             value['channel'], UUID(value['agent_id'])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect)
    await runtime.prompt_context()
    args['customer']['identity']['kind'] = 'OTHER'
    result = await runtime.call_tool('booking_create', args)
    assert result.halt_status == 'FAILED' and result.error_code == 'TOOL_SETUP_FAILED'
    assert result.safe_draft is None
    assert calls['lookup'] == calls['facts'] == calls['execute'] == 0


@pytest.mark.integration
@pytest.mark.parametrize('blocked', ['gate', 'retained', 'guard_failure'])
async def test_missing_contact_question_cannot_hide_retained_or_unknown_authority(
        api_client, db_session, monkeypatch, blocked):
    value, args, registry, calls, effect, _ = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='VOICE')
    value['messages'][0]['text'] = 'Запишите меня завтра.'
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
        if grant.scope() == (value['owner_id'], UUID(value['integration_id']),
                             value['channel'], UUID(value['agent_id'])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect)
    await runtime.prompt_context()
    expected = BusinessToolResult(halt_status='UNKNOWN', error_code='TOOL_EXECUTION_UNKNOWN')

    async def guard(**kwargs):
        if blocked == 'guard_failure':
            raise RuntimeError('Synthetic retained-authority read failed')
        return expected if blocked == 'gate' else None

    monkeypatch.setattr(runtime, 'execution_guard', guard)
    monkeypatch.setattr(runtime, '_halt', lambda unknown: expected)
    if blocked == 'retained':
        monkeypatch.setattr(runtime.collector, 'snapshot', lambda: [object()])
    args['customer']['identity']['kind'] = 'OTHER'
    result = await runtime.call_tool('booking_create', args)
    assert result is expected and result.safe_draft is None
    assert calls['lookup'] == calls['facts'] == calls['execute'] == 0
    assert not effect.permit_requests


@pytest.mark.integration
@pytest.mark.parametrize('tool', ['booking_create', 'business_propose'])
@pytest.mark.parametrize('kind,name', [('OTHER', 'Артем'), ('PHONE', ' ')])
async def test_managed_voice_returns_contact_question_without_another_completion(
        api_client, db_session, monkeypatch, tool, kind, name):
    value, args, _, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='VOICE')
    value['messages'][0]['text'] = (
        'Запишите меня завтра в два часа на мужскую стрижку к Светлане. Меня зовут Артем.')
    args['customer'] = {'display_name': name,
                        'identity': {'kind': kind, 'value': '+375000000000'}}
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call(
        'incomplete-contact', tool, json.dumps(preparation_arguments(tool, args)),
    )]))

    response = await execute(api_client, value)
    result = response.json()
    assert response.status_code == 200 and result['status'] == 'DRAFT_READY'
    assert result['draft_text'] == 'Подскажите, пожалуйста, контактный номер для записи.'
    assert len(model.chat.completions.calls) == 1
    assert calls['lookup'] == calls['facts'] == calls['execute'] == 0
    assert not effect.permit_requests
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
