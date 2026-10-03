"""Concurrent event redelivery through the normal runtime and chat boundaries."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from business_capabilities.context import BusinessRuntimeError
from business_capabilities.runtime import CapabilityRuntime, digest, utcnow
from models.agent import AgentTemplate
from models.business import BusinessTurn, BusinessConfiguration
from tests.test_business_runtime import setup_business
from tests.test_business_continuation import install_chat
from tests.fakes import build_llm_response


def authorize_together(provider):
    original = provider.context
    ready = asyncio.Event()
    calls = 0
    async def context(ctx):
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        return await original(ctx)
    provider.context = context


@pytest.mark.asyncio
@pytest.mark.parametrize('conflicting', [False, True])
async def test_concurrent_new_event_open_reloads_one_turn(setup_business, db_session, conflicting):
    db, agent, settings, provider, _ = setup_business
    authorize_together(provider)
    async def open_turn(message):
        async with db_session() as session:
            local_agent = await session.get(AgentTemplate, agent.id)
            return await CapabilityRuntime.open(session, settings, local_agent, 'client', 'website', 'website-gateway', agent.user_id,
                event_id='event', user_message=message, adapter=provider)
    results = await asyncio.gather(open_turn('Привет'), open_turn('Другое сообщение' if conflicting else 'Привет'), return_exceptions=True)
    successes = [r for r in results if isinstance(r, CapabilityRuntime)]
    if conflicting:
        errors = [r for r in results if isinstance(r, BusinessRuntimeError)]
        assert len(successes) == len(errors) == 1
        assert errors[0].code == 'IDEMPOTENCY_CONFLICT'
    else:
        assert len(successes) == 2
        assert successes[0].turn_id == successes[1].turn_id
    assert await db.scalar(select(func.count()).select_from(BusinessTurn)) == 1
    assert provider.executions == []


@pytest.mark.asyncio
@pytest.mark.parametrize('conflicting', [False, True])
async def test_concurrent_chat_event_is_replay_in_progress_or_conflict_not_500(api_client, setup_business, monkeypatch, conflicting):
    db, agent, settings, provider, _ = setup_business
    authorize_together(provider)
    calls = 0
    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.02)
        return build_llm_response(content='Здравствуйте!')
    headers, payload = install_chat(monkeypatch, setup_business, complete)
    async def send(message):
        return await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'event_id': 'same-event', 'message': message})
    responses = await asyncio.gather(send('Привет'), send('Другое сообщение' if conflicting else 'Привет'))
    assert any(r.status_code == 200 for r in responses)
    assert all(r.status_code in {200, 409} for r in responses), [r.text for r in responses]
    if conflicting:
        conflict = next(r for r in responses if r.status_code == 409)
        assert conflict.json()['detail']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert calls == 1
    assert await db.scalar(select(func.count()).select_from(BusinessTurn)) == 1
    assert provider.executions == []


@pytest.mark.asyncio
async def test_waiting_chat_delivery_refreshes_completed_answer_after_lock(api_client, setup_business, monkeypatch):
    import services.llm_service as llm_module
    _, agent, _, provider, _ = setup_business
    authorize_together(provider)
    original_lock = llm_module.conversation_lock
    finished = asyncio.Event()
    lock_calls = 0
    @asynccontextmanager
    async def ordered_lock(db, session_id):
        nonlocal lock_calls
        lock_calls += 1
        position = lock_calls
        if position == 2:
            await asyncio.wait_for(finished.wait(), 5)
        async with original_lock(db, session_id):
            yield
        if position == 1:
            finished.set()
    monkeypatch.setattr(llm_module, 'conversation_lock', ordered_lock)
    calls = 0
    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.05)
        return build_llm_response(content='Сохранённый ответ')
    headers, payload = install_chat(monkeypatch, setup_business, complete)
    async def send():
        return await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'message': 'Привет', 'event_id': 'event'})
    responses = await asyncio.gather(send(), send())
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['expired_turn', 'changed_context'])
async def test_post_authorization_arbitration_rechecks_age_and_context(setup_business, db_session, change):
    db, agent, settings, provider, _ = setup_business
    original = provider.context
    async def context(ctx):
        result = await original(ctx)
        async with db_session() as concurrent:
            if change == 'expired_turn':
                concurrent.add(BusinessTurn(id=uuid4(), session_id=ctx.session_id, event_id='event',
                    message_hash=digest('Привет'), created_at=utcnow()-timedelta(days=31)))
            else:
                (await concurrent.get(BusinessConfiguration, agent.id)).generation = uuid4()
            await concurrent.commit()
        return result
    provider.context = context
    with pytest.raises(BusinessRuntimeError) as error:
        await CapabilityRuntime.open(db, settings, agent, 'client', 'website', 'website-gateway', agent.user_id,
            event_id='event', user_message='Привет', adapter=provider)
    assert error.value.code == ('OPERATION_EXPIRED' if change == 'expired_turn' else 'STALE_BINDING')
    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(BusinessTurn)) == (1 if change == 'expired_turn' else 0)
    assert provider.executions == []
