"""Golden adapter composition and managed provider/configuration failure policies."""
import copy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from openai import APIConnectionError, APIError, APITimeoutError, AuthenticationError, RateLimitError

import main
import services.llm_service as llm_module
from core.config import get_settings
from services.managed_runtime import privacy_id
from models.agent import AgentCredentials, AgentTemplate
from schemas.managed_turn import ManagedTurnRequest
from services.credentials_cipher import encrypt_credentials
from services.prompt_composer import PromptComposer
from tests.fakes import build_llm_response, build_tool_call, build_tool_result
from tests.test_chat_flow import FakeKnowledgeBaseClient, store_credentials
from tests.test_managed_turns import SENTINEL, execute, model, setup, tools


@pytest.mark.integration
@pytest.mark.parametrize("canonical,channel", [("TELEGRAM","telegram"),("INSTAGRAM","instagram"),
    ("WEBSITE_WIDGET","website"),("WHATSAPP","whatsapp"),("VOICE","voice")])
async def test_shared_kb_prompt_model_and_tool_continuation_golden_parity(api_client,db_session,monkeypatch,model,tools,canonical,channel):
    agent,value=await setup(api_client,db_session,monkeypatch,channel=canonical)
    await store_credentials(api_client,agent["id"],"bitrix",{"webhook_url":"https://synthetic.invalid/rest"},owner_id=UUID(agent["user_id"]))
    tools.result=build_tool_result('{"result":[{"ID":"fake"}]}')
    monkeypatch.setattr(PromptComposer,"_channel_and_time",lambda self,agent,context: "trusted channel="+context.channel)
    FakeKnowledgeBaseClient.reset()
    FakeKnowledgeBaseClient.contexts=[SimpleNamespace(content="Known company fact",source_filename="fixture.pdf",chunk_index=2,score=.9,metadata={"hidden":SENTINEL})]
    monkeypatch.setattr(llm_module,"KnowledgeBaseClient",FakeKnowledgeBaseClient)
    monkeypatch.setattr(get_settings(),"knowledge_base_enabled",True)
    monkeypatch.setattr(get_settings(),"internal_api_token","test-internal")
    from api.v1.chat import llm_service
    # The disposable migration fixture clears the settings cache after the
    # eagerly created legacy service. Align both explicit test configurations.
    monkeypatch.setattr(llm_service.settings,"knowledge_base_enabled",True)
    monkeypatch.setattr(llm_service.settings,"internal_api_token","test-internal")
    tool=build_llm_response(tool_calls=[build_tool_call("same","mutate",'{"read":true}')])
    model.chat.completions.queue(copy.deepcopy(tool),build_llm_response(content="answer"),build_llm_response(content="answer"))
    legacy=await api_client.post("/v1/chat/completions",json=dict(agent_id=agent["id"],external_client_id="parity",channel=channel,message=value["messages"][0]["text"]))
    managed=await execute(api_client,value)
    assert legacy.status_code==managed.status_code==200
    assert managed.json()["status"]=="DRAFT_READY"
    calls=model.chat.completions.calls
    # Shared prompt/KB/model parameters retain parity; managed MCP is deliberately
    # unavailable before acquisition. Legacy still exercises tool continuation.
    assert {**calls[0], "tools": None} == calls[2]
    assert len(calls) == 3 and calls[2]["tools"] is None
    assert len(tools.calls) == 1
    messages=calls[0]["messages"]
    assert "trusted channel="+channel in messages[0]["content"]
    assert messages[0]["content"].startswith("<platform_policy>")
    assert messages[1]["role"]=="system" and "Known company fact" in messages[1]["content"]
    assert SENTINEL not in json.dumps(calls)
    assert [call["query"] for call in FakeKnowledgeBaseClient.calls]==[value["messages"][0]["text"]]*2


@pytest.mark.integration
@pytest.mark.parametrize("kind,code,deactivated", [("auth","MODEL_AUTH_FAILED",True),
    ("funds","MODEL_INSUFFICIENT_FUNDS",True),("rate","MODEL_RATE_LIMITED",False),
    ("timeout","MODEL_TIMEOUT",False),("connection","MODEL_UNAVAILABLE",False),("api","MODEL_ERROR",False)])
async def test_model_error_codes_and_existing_key_deactivation(api_client,db_session,monkeypatch,model,kind,code,deactivated,caplog):
    """Keep final error codes and deactivation unchanged after bounded recovery."""
    agent,value=await setup(api_client,db_session,monkeypatch)
    request=httpx.Request("POST","http://synthetic")
    errors=dict(auth=AuthenticationError(SENTINEL,response=httpx.Response(401,request=request),body={}),
        funds=RateLimitError("402 insufficient "+SENTINEL,response=httpx.Response(429,request=request),body={}),
        rate=RateLimitError(SENTINEL,response=httpx.Response(429,request=request),body={}),
        timeout=APITimeoutError(request=request),connection=APIConnectionError(message=SENTINEL,request=request),
        api=APIError(SENTINEL,request=request,body={}))
    model.chat.completions.queue(errors[kind])
    if kind in {'rate','connection'}:
        model.chat.completions.queue(errors[kind])
    result=await execute(api_client,value)
    assert result.json()["status"]=="FAILED" and result.json()["error_code"]==code
    assert model.closed and SENTINEL not in result.text+caplog.text
    async with db_session() as db:
        stored=await db.get(AgentTemplate,UUID(agent["id"]))
        assert stored.is_active is not deactivated


@pytest.mark.integration
async def test_managed_encrypted_credentials_and_enabled_tiers(api_client,db_session,monkeypatch,model,tools):
    agent,value=await setup(api_client,db_session,monkeypatch)
    await store_credentials(api_client,agent["id"],"bitrix",{"webhook_url":"https://synthetic.invalid/rest"},owner_id=UUID(agent["user_id"]))
    async with db_session() as db:
        record=await db.get(AgentCredentials,(UUID(agent["id"]),"bitrix"))
        record.credentials=encrypt_credentials({"webhook_url":"https://synthetic.invalid/rest"})
        record.enabled_tools=None
        await db.commit()
    tools.metadata={"mutate":{"tier":"core"},"second":{"tier":"advanced"}}
    credentials=[]
    original=tools.__init__
    def record(self,service_name,credentials_value=None,**kwargs):
        values=kwargs.get("credentials",credentials_value)
        credentials.append(values)
        original(self,service_name,values)
    monkeypatch.setattr(tools,"__init__",record)
    model.chat.completions.queue(build_llm_response(content="answer"))
    assert (await execute(api_client,value)).json()["status"]=="DRAFT_READY"
    assert credentials == [] and tools.init_calls == tools.calls == []
    assert model.chat.completions.calls[0]["tools"] is None


@pytest.mark.integration
async def test_privacy_payload_uses_derived_id_and_no_direct_fallback(api_client,db_session,monkeypatch,model,caplog):
    _,value=await setup(api_client,db_session,monkeypatch)
    monkeypatch.setattr(get_settings(),"privacy_filter_enabled",True)
    monkeypatch.setattr(get_settings(),"internal_api_token","test-privacy-token")
    calls,clients=[],[]
    original=httpx.AsyncClient
    async def reply(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={"choices":[{"message":{"content":"privacy answer","tool_calls":None}}]})
    def create_proxy(**kwargs):
        client=original(transport=httpx.MockTransport(reply),**kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(llm_module.httpx,"AsyncClient",create_proxy)
    result=await execute(api_client,value)
    assert result.json()["status"]=="DRAFT_READY"
    assert calls[0]["privacy_session_id"]==str(privacy_id(ManagedTurnRequest.model_validate(value)))
    assert calls[0]["privacy_session_id"]!=value["conversation_id"]
    assert model.chat.completions.calls==[] and clients[0].is_closed
    monkeypatch.setattr(get_settings(),"privacy_filter_base_url","")
    result=await execute(api_client,value)
    assert result.json()["status"]=="FAILED" and result.json()["error_code"]=="PRIVACY_UNAVAILABLE"
    assert model.chat.completions.calls==[] and SENTINEL not in caplog.text+result.text


def test_openapi_has_internal_managed_wire_with_valid_inline_request_definitions():
    path=main.app.openapi()["paths"]["/internal/v1/managed-turns/execute"]["post"]
    schema=path["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["messages"]["items"]["properties"]["actor"]["enum"]==["CUSTOMER","AGENT","HUMAN","SYSTEM"]
    assert "#/$defs/" not in json.dumps(schema)


@pytest.mark.integration
async def test_runtime_db_error_suppresses_real_sql_parameters_and_exception(api_client,db_session,monkeypatch,model,caplog,capfd):
    import logging
    from sqlalchemy import text
    from services.agent_service import AgentService

    _,value=await setup(api_client,db_session,monkeypatch)
    async def bad_query(self,db,agent_id,user_id=None):
        original=db.bind.echo
        db.bind.echo=True
        try:
            await db.execute(text("SELECT CAST(:private_value AS INTEGER)"),{"private_value":SENTINEL})
        finally:
            db.bind.echo=original
    monkeypatch.setattr(AgentService,"get_agent_by_id",bad_query)
    with caplog.at_level(logging.DEBUG):
        result=await execute(api_client,value)
    assert result.json()["status"]=="FAILED" and result.json()["error_code"]=="INTERNAL_ERROR"
    captured=capfd.readouterr()
    assert SENTINEL not in result.text+caplog.text+captured.out+captured.err
    assert model.chat.completions.calls==[]
