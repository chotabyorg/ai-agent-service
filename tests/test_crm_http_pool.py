"""Native HTTP/1 pooling, isolated authority and same-owner physical cleanup."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import anyio
import httpx
import pytest

from business_capabilities.chotacrm_client import ChotaCRMClient
from services.managed_runtime import ManagedRuntime
from services.managed_tools import ManagedExecutionError
from services.voice_signing import managed_signing_scope
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_managed_http_cleanup import tls_contexts


@asynccontextmanager
async def native_peer(tmp_path, *, tls=False, pause_body=False):
    server_context, verify = tls_contexts(tmp_path) if tls else (None, True)
    state = SimpleNamespace(peers=[],tasks=[],requests=[],entered=asyncio.Event(),
                            release=asyncio.Event(),verify=verify)

    async def serve(reader,writer):
        peer = SimpleNamespace(writer=writer,eof=asyncio.Event())
        state.peers.append(peer)
        state.tasks.append(asyncio.current_task())
        try:
            while True:
                raw = await reader.readuntil(b'\r\n\r\n')
                headers = dict(row.split(b':',1) for row in raw.lower().split(b'\r\n')[1:] if b':' in row)
                original = dict(row.split(b':',1) for row in raw.split(b'\r\n')[1:] if b':' in row)
                body = await reader.readexactly(int(headers.get(b'content-length',b'0')))
                authorization = next(value.strip().decode() for key,value in original.items() if key.lower()==b'authorization')
                claims = json.loads(base64.urlsafe_b64decode(authorization.split('.')[1]+'=='))
                state.requests.append(dict(claims=claims,body=json.loads(body),headers=headers))
                encoded = b'{"ok":true}'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nSet-Cookie: synthetic=not-authority\r\nContent-Length: '+str(len(encoded)).encode()+b'\r\n\r\n'+(b'{' if pause_body else encoded))
                await writer.drain()
                state.entered.set()
                if pause_body:
                    while await reader.read(65536):
                        pass
                    break
        except (asyncio.IncompleteReadError,ConnectionError):
            pass
        finally:
            peer.eof.set()
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve,'127.0.0.1',0,ssl=server_context)
    state.origin = ('https' if tls else 'http')+'://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        for peer in state.peers:
            peer.writer.close()
        await asyncio.gather(*state.tasks,return_exceptions=True)


@asynccontextmanager
async def owner():
    execution = ManagedRuntime(10)
    with managed_signing_scope(execution),anyio.CancelScope(shield=True) as lifecycle:
        try:
            yield execution
        finally:
            await execution.cleanup(lifecycle)


def native_port(crm_settings,peer,monkeypatch):
    config = crm_settings.model_copy(update={'business_crm_url':peer.origin})
    if peer.verify is not True:
        original = httpx.AsyncClient

        def verified(**kwargs):
            return original(**kwargs,verify=peer.verify)

        monkeypatch.setattr('business_capabilities.chotacrm_client.httpx.AsyncClient',verified)
    return ChotaCRMClient(config)


async def eof(peer):
    await asyncio.wait_for(asyncio.gather(*(row.eof.wait() for row in peer.peers)),2)


@pytest.mark.parametrize('tls',[False,True],ids=['http','tls'])
async def test_one_native_connection_per_turn_and_fresh_authentication(crm_settings,tmp_path,monkeypatch,tls):
    async with native_peer(tmp_path,tls=tls) as peer:
        port = native_port(crm_settings,peer,monkeypatch)
        async with owner() as execution:
            for index in range(4):
                assert await execution.operation(port.request('POST','facts/synthetic',scope='synthetic-'+str(index),
                    claims={'session_id':'one-turn','owner_id':str(index)},body={'index':index},execution=execution)) == {'ok':True}
            assert len(execution._crm_http_clients) == 1
            client = next(iter(execution._crm_http_clients.values()))
            assert not client.is_closed and len(peer.peers) == 1
        await eof(peer)
        assert client.is_closed and execution._crm_http_clients == {} and execution._crm_http_closed
        assert len(peer.requests) == 4
        assert len({row['claims']['jti'] for row in peer.requests}) == 4
        assert [row['claims']['scope'] for row in peer.requests] == ['synthetic-'+str(index) for index in range(4)]
        assert [row['claims']['owner_id'] for row in peer.requests] == list(map(str,range(4)))
        assert [row['body'] for row in peer.requests] == [{'index':index} for index in range(4)]
        assert all(b'cookie' not in row['headers'] for row in peer.requests)
        with pytest.raises(ManagedExecutionError) as raised:
            await port.request('GET','context',scope='context',claims={},execution=execution)
        assert raised.value.code == 'TOOL_CLEANUP_FAILED' and len(peer.requests) == 4


async def test_five_concurrent_turns_do_not_share_transport_or_authority(crm_settings,tmp_path,monkeypatch):
    async with native_peer(tmp_path) as peer:
        port = native_port(crm_settings,peer,monkeypatch)
        ready,release = [asyncio.Event() for _ in range(5)],asyncio.Event()
        clients,executions = [],[]

        async def run(index):
            async with owner() as execution:
                executions.append(execution)
                for step in range(2):
                    assert await execution.operation(port.request('POST','execute/synthetic-read',scope='read',
                        claims={'session_id':str(index)},body={'index':index,'step':step},execution=execution)) == {'ok':True}
                    if step == 0:
                        ready[index].set()
                        await release.wait()
                assert len(execution._crm_http_clients) == 1
                clients.append(next(iter(execution._crm_http_clients.values())))

        tasks = [asyncio.create_task(run(index)) for index in range(5)]
        try:
            await asyncio.wait_for(asyncio.gather(*(item.wait() for item in ready)),5)
            assert len(peer.peers) == 5 and len(peer.requests) == 5
        finally:
            release.set()
            await asyncio.gather(*tasks)
        await eof(peer)
        assert len(clients) == len({id(client) for client in clients}) == len(peer.peers) == 5
        assert all(client.is_closed for client in clients)
        assert all(item._crm_http_closed and item._crm_http_clients == {} for item in executions)
        assert len(peer.requests) == len({row['claims']['jti'] for row in peer.requests}) == 10
        assert all(row['claims']['session_id'] == str(row['body']['index']) and b'cookie' not in row['headers'] for row in peer.requests)


@pytest.mark.parametrize('tls',[False,True],ids=['http','tls'])
async def test_cancelled_response_body_joins_the_real_native_socket(crm_settings,tmp_path,monkeypatch,tls):
    async with native_peer(tmp_path,tls=tls,pause_body=True) as peer:
        port = native_port(crm_settings,peer,monkeypatch)
        entered,executions,clients = asyncio.Event(),[],[]

        async def run():
            async with owner() as execution:
                executions.append(execution)
                entered.set()
                try:
                    await execution.operation(port.request('POST','execute/synthetic',scope='synthetic',
                        claims={},body={},execution=execution))
                finally:
                    clients.extend(execution._crm_http_clients.values())

        task = asyncio.create_task(run())
        await asyncio.wait_for(entered.wait(),1)
        await asyncio.wait_for(peer.entered.wait(),5)
        executions[0].signal_cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,3)
        await eof(peer)
        assert len(peer.requests) == len(peer.peers) == len(clients) == 1
        assert clients[0].is_closed and executions[0]._crm_http_clients == {}


async def test_cleanup_failure_does_not_become_a_successful_turn(crm_settings,monkeypatch):
    import services.managed_http_cleanup as cleanup
    original = cleanup.close_managed_helper

    async def failed(client):
        await original(client)
        raise ManagedExecutionError('TOOL_CLEANUP_FAILED')

    monkeypatch.setattr(cleanup,'close_managed_helper',failed)
    port = ChotaCRMClient(crm_settings,transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'ok':True})))
    with pytest.raises(ManagedExecutionError) as raised:
        async with owner() as execution:
            assert await port.request('GET','context',scope='context',claims={},execution=execution) == {'ok':True}
            client = next(iter(execution._crm_http_clients.values()))
    assert raised.value.code == 'TOOL_CLEANUP_FAILED'
    assert client.is_closed and execution._crm_http_clients == {} and execution._crm_http_closed


async def test_changed_transport_or_configuration_cannot_reuse_the_old_binding(crm_settings):
    captured = []
    transport = httpx.MockTransport(lambda request:(captured.append(request) or httpx.Response(204)))
    port = ChotaCRMClient(crm_settings.model_copy(),transport=transport)
    async with owner() as execution:
        await port.request('GET','context',scope='context',claims={},execution=execution)
        port.settings = port.settings.model_copy(update={'business_crm_url':'http://changed.synthetic.local'})
        await port.request('GET','context',scope='context',claims={},execution=execution)
        port.transport = httpx.MockTransport(lambda request:httpx.Response(204))
        await port.request('GET','context',scope='context',claims={},execution=execution)
        assert len(execution._crm_http_clients) == 3
        clients = list(execution._crm_http_clients.values())
    assert all(client.is_closed for client in clients)
    assert captured[0].url.host == 'synthetic.local' and captured[1].url.host == 'changed.synthetic.local'


async def test_cleanup_rejects_acquisition_before_its_awaiting_close_finishes(crm_settings,monkeypatch):
    import services.managed_http_cleanup as cleanup
    original = cleanup.close_managed_helper
    calls = []
    port = ChotaCRMClient(crm_settings,transport=httpx.MockTransport(
        lambda request:(calls.append(request) or httpx.Response(204))))

    async def checked(client):
        assert execution._crm_http_closed
        with pytest.raises(ManagedExecutionError):
            await port.request('GET','context',scope='context',claims={},execution=execution)
        await original(client)

    monkeypatch.setattr(cleanup,'close_managed_helper',checked)
    async with owner() as execution:
        await port.request('GET','context',scope='context',claims={},execution=execution)
    assert len(calls) == 1 and execution._crm_http_clients == {}


@pytest.mark.integration
@pytest.mark.parametrize('after_effect',[False,True],ids=['prepared','confirmed'])
async def test_route_cleanup_failure_never_repeats_an_effect(api_client,db_session,monkeypatch,after_effect):
    from sqlalchemy import select
    import services.managed_http_cleanup as cleanup
    from models.business import BusinessOperation,BusinessPlan
    from schemas.managed_turn import ManagedTurnRequest
    from tests.fakes import build_llm_response
    from tests.test_booking_workflow import tool
    from tests.test_business_runtime import later
    from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
    from tests.test_managed_turns import execute
    from tests.test_pending_proposal_decision import decision

    value,args,_,calls,effects,model = await install_managed_crm(
        api_client,db_session,monkeypatch,200,{},allow_execute=True)
    model.chat.completions.queue(tool('booking_create',args,'prepare-http-pool'))
    current = ManagedTurnRequest.model_validate(value)
    if after_effect:
        prepared = await execute(api_client,value)
        assert prepared.json()['status'] == 'DRAFT_READY' and calls['execute'] == 0
        async with db_session() as db:
            proposal = await db.scalar(select(BusinessPlan))
        current = later(current,'Да, подтверждаю запись.')
        model.chat.completions.queue(build_llm_response(tool_calls=[decision(str(proposal.id),'CONFIRM')]))
    original = cleanup.close_managed_helper

    async def failed(client):
        await original(client)
        raise ManagedExecutionError('TOOL_CLEANUP_FAILED')

    monkeypatch.setattr(cleanup,'close_managed_helper',failed)
    response = await execute(api_client,current.model_dump(mode='json',exclude_unset=True))
    result = response.json()
    assert response.status_code == 200
    assert result['status'] == ('UNKNOWN' if after_effect else 'FAILED')
    assert result['error_code'] == 'TOOL_CLEANUP_FAILED' and result['draft_text'] is None
    assert calls['execute'] == int(after_effect) and len(effects.permit_requests) == int(after_effect)
    async with db_session() as db:
        operation = await db.scalar(select(BusinessOperation))
        assert operation.status == ('SUCCEEDED' if after_effect else 'RESERVED')
        assert operation.attempt_count == int(after_effect)
    if after_effect:
        monkeypatch.setattr(cleanup,'close_managed_helper',original)
        completions = len(model.chat.completions.calls)
        replay = await execute(api_client,current.model_dump(mode='json',exclude_unset=True))
        assert replay.status_code == 200 and replay.json()['status'] == 'DRAFT_READY'
        assert calls['execute'] == 1 and len(effects.permit_requests) == 1
        assert len(model.chat.completions.calls) == completions
