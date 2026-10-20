"""A rejected preparation cannot become a spoken, invented proposal."""
import pytest
from sqlalchemy import func, select

from business_capabilities.registry import DEFAULTS
from models.business import BusinessOperation, BusinessPlan
from services.llm_service import LLMService
from tests.fakes import build_llm_response
from tests.test_chotacrm_managed_proposal_errors import (
    existing_customer_lookup, install_managed_crm, preparation_arguments,
)
from tests.test_managed_streaming import chunk
from tests.test_managed_turns import execute
from tests.test_preparation_schema_correction import preparation


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
@pytest.mark.parametrize('tool', ['booking_create', 'business_propose'])
@pytest.mark.parametrize('error_code', ['NOT_FOUND', 'BOOKING_CONFLICT', 'PROVIDER_UNAVAILABLE'])
@pytest.mark.parametrize('partials', [False, True])
async def test_refused_proposal_and_later_read_cannot_publish_false_confirmation(
        api_client, db_session, monkeypatch, channel, tool, error_code, partials):
    value, arguments, _, calls, effects, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200,
        {'status': 'ERROR', 'error': {'code': error_code, 'message': 'synthetic', 'retry': 'NEVER'}},
        lookup_response=existing_customer_lookup(), channel=channel,
        enabled_capabilities=[*DEFAULTS, 'deal.create'] if tool == 'business_propose' else None)
    corrected = {key: val for key, val in arguments.items() if key != 'customer'}
    corrected['customer_ref'] = 'customer_existing'
    model.chat.completions.queue(
        preparation(tool, preparation_arguments(tool, arguments), 'inline-customer'),
        preparation(tool, preparation_arguments(tool, corrected), 'exact-reference'),
        preparation('booking_check_availability', {key: corrected[key] for key in
                    ('resource_ref', 'offering_ref', 'start_at')}, 'advisory-only'))
    original_create = model.chat.completions.create
    requests, published = [], []
    false_answer = 'Предложение подготовлено. Подтверждаете запись на выбранное время?'

    async def create(**kwargs):
        requests.append(kwargs)
        if len(requests) <= 3:
            return await original_create(**kwargs)
        assert len(requests) == 4
        if kwargs.get('stream'):
            async def generate():
                yield chunk(text='Предложение подготовлено. ')
                yield chunk(text='Подтверждаете запись на выбранное время?')
                yield chunk(finish='stop')
            return generate()
        return build_llm_response(content=false_answer)

    monkeypatch.setattr(model.chat.completions, 'create', create)
    if partials:
        core = LLMService._execute_core

        async def publish(text):
            published.append(text)

        async def with_partials(self, *args, **kwargs):
            kwargs['partial_hook'] = publish
            return await core(self, *args, **kwargs)

        monkeypatch.setattr(LLMService, '_execute_core', with_partials)
    response = await execute(api_client, value)
    assert response.status_code == 200 and response.json()['status'] == 'DRAFT_READY'
    assert not published, 'An invented proposal reached the partial publisher'
    answer = response.json()['draft_text']
    assert answer != false_answer and 'Не удалось подготовить предложение' in answer
    assert 'Подтверждаете' not in answer
    assert not requests[-1].get('stream')
    assert calls['lookup'] == 1 and calls['facts'] == 1 and calls['execute'] == 0
    assert not effects.permit_requests
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
