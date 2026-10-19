"""Failed booking reads stop prose generation without inventing absence or an effect."""
import json

import httpx
import pytest
from sqlalchemy import func, select

from models.business import BusinessOperation, BusinessPlan
from schemas.business import BusinessToolResult
from tests.fakes import ScriptedLLMClient, build_llm_response
from tests.test_chotacrm_adapter import crm_settings, setup
from tests.test_pending_proposal_decision import run_core
from tests.test_preparation_schema_correction import local_runtime, preparation


def install_failure(business, code):
    client = business.registration.adapter.client
    original = client.transport
    reads = []
    async def handle(request):
        if request.url.path.endswith('/execute/booking.get'):
            reads.append(json.loads(request.content)['arguments'])
            return httpx.Response(200, json={'status': 'ERROR', 'error': {
                'code': code, 'message': 'PRIVATE_PROVIDER_TEXT', 'retry': 'NEVER'}})
        return await original.handle_async_request(request)
    client.transport = httpx.MockTransport(handle)
    return reads


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['PROVIDER_UNAVAILABLE', 'NOT_FOUND', 'PERMISSION_DENIED'])
async def test_failure_has_distinct_honest_reply_without_model_continuation_or_effect(
        db_session, crm_settings, monkeypatch, code):
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    reads = install_failure(business, code)
    model = ScriptedLLMClient()
    forbidden = build_llm_response(content='Запись не найдена. Продиктуйте ваш номер заново.')
    model.chat.completions.queue(preparation('booking_get', {'booking_ref': 'booking_test'}, 'read'), forbidden)
    reply = await run_core(db_session, runtime, model)
    assert ('найти' in reply) is (code == 'NOT_FOUND')
    assert ('не удаётся проверить' in reply) is (code != 'NOT_FOUND')
    assert 'номер' not in reply and 'PRIVATE_' not in reply
    assert reads == [{'booking_ref': 'booking_test'}]
    assert len(model.chat.completions.calls) == 1 and list(model.chat.completions._responses) == [forbidden]
    assert provider['posts'] == 0
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0


@pytest.mark.asyncio
async def test_post_read_guard_keeps_precedence_over_friendly_failure_reply(
        db_session, crm_settings, monkeypatch):
    business, _, _, _ = await setup(db_session, crm_settings)
    reads = install_failure(business, 'PROVIDER_UNAVAILABLE')
    stopped = BusinessToolResult(halt_status='UNKNOWN', error_code='TOOL_EXECUTION_UNKNOWN')
    async def guard(**kwargs):
        return stopped if reads else None
    monkeypatch.setattr(business, 'execution_guard', guard)
    result = await business.call_tool('booking_get', {'booking_ref': 'booking_test'})
    assert result is stopped and result.safe_draft is None
