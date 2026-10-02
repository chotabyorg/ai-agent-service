"""Canonical contracts and transport boundaries (no external account access)."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError, SecretStr
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from business_capabilities.registry import CAPABILITIES, REGISTRY, DEFAULTS, Risk, normalize_arguments, control_tools
from business_capabilities.results import validate_result
from business_capabilities.context import AgentExecutionContext
from business_capabilities.adapters.chotacrm import ChotaCRMAdapter


def test_exact_registry_risks_and_defaults():
    assert len(CAPABILITIES) == 18
    assert len({c.tool_name for c in CAPABILITIES}) == 18
    assert {c.id for c in CAPABILITIES if c.risk == Risk.WRITE_SAFE} == {'customer.create', 'customer.add_note', 'inquiry.capture', 'followup.create'}
    assert set(REGISTRY) - set(DEFAULTS) == {'deal.create', 'deal.move'}
    assert sum(c.risk == Risk.READ for c in CAPABILITIES) == 9


def test_all_advertised_tool_schemas_are_closed_resolved_and_nonnullable():
    tools = [c.tool() for c in CAPABILITIES] + control_tools()
    def check(value):
        if isinstance(value, dict):
            assert '$ref' not in value
            assert value.get('type') != 'null'
            if value.get('type') == 'object':
                assert value['additionalProperties'] is False
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    for tool in tools:
        check(tool['function']['parameters'])


@pytest.mark.parametrize('field', ['workspace_id','connection_id','owner_id','tenant','actor_type','permission','operation','confirmed'])
def test_model_cannot_supply_authority(field):
    with pytest.raises(ValidationError):
        normalize_arguments('catalog.search', {field: str(uuid4())})


@pytest.mark.parametrize('args', [{'query': '\x00'}, {'query': '\ud800'}, {'query': None}, {'limit': 21}, {'limit': True}, {'cursor': ''}])
def test_invalid_bounded_text_page(args):
    with pytest.raises(ValidationError):
        normalize_arguments('catalog.search', args)


def test_datetime_identity_quantity_contracts():
    with pytest.raises(ValidationError):
        normalize_arguments('booking.create', {'customer_ref':'c','offering_ref':'o','resource_ref':'r','start_at':'2026-10-12T15:00:00'})
    with pytest.raises(ValidationError):
        normalize_arguments('customer.find', {'name':'Влад'})
    with pytest.raises(ValidationError):
        normalize_arguments('deal.create', {'customer_ref':'c','title':'Order','currency':'BYN','items':[{'offering_ref':'o','quantity':'0'}]})


def test_raw_provider_data_and_errors_cannot_reach_model():
    result = validate_result('catalog.search', {'status':'OK','data':{'items':[], 'workspace_id':'secret'}})
    assert result['error']['code'] == 'PROVIDER_UNAVAILABLE'
    result = validate_result('booking.create', {'status':'OK','data':{}}, write=True)
    assert result['error']['code'] == 'UNKNOWN_WRITE_RESULT'
    result = validate_result('catalog.search', {'status':'ERROR','error':{'code':'PERMISSION_DENIED','message':'password=SECRET','retry':'NEVER'}})
    assert 'SECRET' not in json.dumps(result)


def test_customer_context_private_fields_fail_closed():
    result = validate_result('customer.get_context', {'status':'OK','data':{'customer':{'ref':'c','display_name':'Влад','archived':False,'identities':[]},'sections':{'NOTES':{'status':'OK','items':[{'body':'staff secret'}]}},'truncated':False}})
    assert result['status'] == 'ERROR'


@pytest.mark.asyncio
async def test_adapter_rs256_scope_and_no_cookie_runtime():
    import base64
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    settings = SimpleNamespace(business_capabilities_enabled=True,business_crm_private_key=SecretStr(pem),business_crm_key_id='test',business_crm_url='http://crm',business_crm_timeout_seconds=1)
    ctx = AgentExecutionContext(*[uuid4() for _ in range(5)], grant_revision=1, session_id=uuid4(), generation=uuid4())
    seen=[]
    def handler(request):
        seen.append(request)
        payload = request.headers['authorization'].split(' ')[1].split('.')[1]
        claims=json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload)%4)))
        assert claims['scope']=='catalog.search'
        assert claims['agent_id']==str(ctx.agent_id)
        assert claims['aud']=='chota-crm-agent'
        assert claims['exp']-claims['iat']==45
        assert 'cookie' not in request.headers and 'x-user-id' not in request.headers
        return httpx.Response(200,json={'status':'OK','data':{'items':[]}})
    adapter=ChotaCRMAdapter(settings,transport=httpx.MockTransport(handler))
    assert (await adapter.execute(ctx,'catalog.search',{}))['status']=='OK'
    assert len(seen)==1


@pytest.mark.asyncio
async def test_transport_lost_write_is_unknown_and_never_retried():
    settings=SimpleNamespace(business_capabilities_enabled=True,business_crm_url='http://crm',business_crm_timeout_seconds=1)
    seen=[]
    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout('secret upstream url',request=request)
    adapter=ChotaCRMAdapter(settings,transport=httpx.MockTransport(handler))
    adapter.assertion=lambda *args:'signed-test'
    ctx=AgentExecutionContext(*[uuid4() for _ in range(5)],grant_revision=1,session_id=uuid4(),generation=uuid4())
    result=await adapter.execute(ctx,'customer.create',{}, {'id':str(uuid4()),'issued_at':datetime.now(timezone.utc).isoformat()})
    assert result['error']['code']=='UNKNOWN_WRITE_RESULT'
    assert len(seen)==1


@pytest.mark.parametrize('service,credentials',[('bitrix',{'webhook_url':'synthetic'}),('google_workspace',{'client_id':'synthetic','client_secret':'x','refresh_token':'x'})])
def test_new_s2s_authority_secrets_never_enter_mcp_children(monkeypatch,service,credentials):
    from mcp_clients.manager import MCPManager
    monkeypatch.setenv('BUSINESS_CRM_PRIVATE_KEY','synthetic-private-secret')
    monkeypatch.setenv('chat_s2s_callers_json','synthetic-caller-secret')
    monkeypatch.setenv('LEGACY_TEST_SETTING','preserved')
    env=MCPManager(service_name=service,credentials=credentials)._build_env()
    assert 'BUSINESS_CRM_PRIVATE_KEY' not in env
    assert 'chat_s2s_callers_json' not in env
    assert env['LEGACY_TEST_SETTING']=='preserved'


@pytest.mark.asyncio
async def test_adapter_no_arbitrary_provider_rpc():
    adapter=ChotaCRMAdapter(SimpleNamespace())
    assert (await adapter.execute(None,'../../admin',{}))['error']['code']=='UNSUPPORTED_CAPABILITY'
    assert (await adapter.prepare(None,'admin.delete',{},None))['error']['code']=='UNSUPPORTED_CAPABILITY'


def test_equivalent_offsets_normalize_to_same_business_payload():
    one=normalize_arguments('followup.create',{'customer_ref':'c','title':'Call','due_at':'2026-10-12T18:00:00+03:00'})
    two=normalize_arguments('followup.create',{'customer_ref':'c','title':'Call','due_at':'2026-10-12T15:00:00Z'})
    assert one==two
