"""Cross-turn parsed keys never substitute fresh assertions or live authority."""
import asyncio

import httpx
import pytest
from pydantic import SecretStr

from business_capabilities.chotacrm_client import ChotaCRMClient
from business_capabilities.context import BusinessRuntimeError
from services.voice_signing import managed_signing_scope, _current
from tests.test_voice_tool_latency import keys, pem, settings, unpack, count_loads  # noqa: F401


@pytest.mark.asyncio
async def test_five_owners_share_one_parse_but_send_fresh_scoped_assertions(keys, monkeypatch):
    counts=count_loads(monkeypatch)
    tokens=[];entries=[]
    async def handle(request):
        tokens.append(request.headers['Authorization'][7:])
        await asyncio.sleep(0)
        return httpx.Response(200,json={'status':'OK'})
    client=ChotaCRMClient(settings(keys[0]),transport=httpx.MockTransport(handle),share_signing_material=True)
    async def turn(index):
        owner=object()
        with managed_signing_scope(owner):
            entries.append(_current.get())
            for scope in ('context','catalog.search','context','resource.search'):
                await client.request('GET','context',scope=scope,claims={'session_id':str(index)},execution=owner)
    await asyncio.gather(*(turn(i) for i in range(5)))
    claims=[unpack(token,keys[0]) for token in tokens]
    assert counts==[1] and len(tokens)==20 and len({v['jti'] for v in claims})==20
    assert {v['session_id'] for v in claims}=={str(i) for i in range(5)}
    assert all(v['exp']-v['iat']==45 for v in claims)
    assert entries==[{}]*5 and _current.get() is None


def test_rotation_evicts_one_entry_invalid_rotation_cannot_fall_back(keys, monkeypatch):
    counts=count_loads(monkeypatch)
    config=settings(keys[0]);client=ChotaCRMClient(config,share_signing_material=True)
    def sign():
        owner=object()
        with managed_signing_scope(owner):return client.assertion('context',{},execution=owner)
    unpack(sign(),keys[0])
    config.business_crm_private_key=SecretStr(pem(keys[1]))
    unpack(sign(),keys[1])
    config.business_crm_private_key=SecretStr('invalid')
    for _ in range(2):
        with pytest.raises(BusinessRuntimeError):sign()
        assert client._signing_material is None
    config.business_crm_private_key=SecretStr(pem(keys[0]))
    unpack(sign(),keys[0])
    assert len(counts)==5
    config.business_crm_key_id=''
    with pytest.raises(BusinessRuntimeError):sign()
    assert client._signing_material is None


def test_ordinary_call_wrong_owner_and_opt_out_do_not_use_shared_entry(keys, monkeypatch):
    counts=count_loads(monkeypatch)
    client=ChotaCRMClient(settings(keys[0]),share_signing_material=True)
    owner=object()
    with managed_signing_scope(owner):
        client.assertion('context',{},execution=object())
        client.assertion('context',{})
    assert client._signing_material is None and len(counts)==2
    for _ in range(2):client.assertion('context',{},execution=owner)
    assert client._signing_material is None and len(counts)==4
    isolated=ChotaCRMClient(client.settings)
    for _ in range(2):
        with managed_signing_scope(owner):isolated.assertion('context',{},execution=owner)
    assert isolated._signing_material is None and len(counts)==6


def test_distinct_clients_do_not_share_keys_or_claims(keys, monkeypatch):
    counts=count_loads(monkeypatch)
    first=ChotaCRMClient(settings(keys[0]),share_signing_material=True)
    second=ChotaCRMClient(settings(keys[1]),share_signing_material=True)
    for client,key in ((first,keys[0]),(second,keys[1]),(first,keys[0])):
        owner=object()
        with managed_signing_scope(owner):
            unpack(client.assertion('context',{'session_id':str(id(owner))},execution=owner),key)
    assert len(counts)==2
