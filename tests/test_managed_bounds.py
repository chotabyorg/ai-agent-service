"""Projection/model/tool budgets and narrow adapter result classification."""
import copy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from core.config import get_settings
from core.managed_security import MANAGED_SECRET_ENV_KEYS
from mcp_clients.manager import MCPManager
from schemas.managed_turn import ManagedTurnRequest, ManagedTurnResult
from services.llm_service import LLMService
from services.managed_tools import ManagedExecutionError, inspect_managed_tool_result
from tests.fakes import build_llm_response, build_tool_call, build_tool_result
from tests.test_chat_flow import FakeKnowledgeBaseClient, store_credentials
from tests.test_managed_turns import (SENTINEL, CapturedClient, OwnedFakeManager, execute,
                                      model, payload, setup, tools)


def test_full_prefix_rejects_duplicate_gap_reorder_wrong_selection_and_all_bounds():
    value = payload()
    value["messages"] = [dict(id=str(uuid4()), sequence=i, actor="CUSTOMER", direction="INBOUND",
        content_type="TEXT", text="x") for i in range(1,257)]
    value.update(input_first_sequence=1,input_last_sequence=1,context_last_sequence=256,
                 selected_input_message_ids=[value["messages"][0]["id"]])
    ManagedTurnRequest.model_validate(value)
    variants = []
    for change in ("duplicate", "gap", "reorder", "selection", "count", "text_chars", "text_bytes", "text_nul", "text_surrogate", "total"):
        item = copy.deepcopy(value)
        if change == "duplicate":
            item["messages"][1]["id"] = item["messages"][0]["id"]
        elif change == "gap":
            item["messages"][1]["sequence"] = 3
        elif change == "reorder":
            item["messages"][0],item["messages"][1] = item["messages"][1],item["messages"][0]
        elif change == "selection":
            item["selected_input_message_ids"] = [item["messages"][1]["id"]]
        elif change == "count":
            item["messages"].append({**item["messages"][-1], "id":str(uuid4()), "sequence":257})
            item["context_last_sequence"] = 257
        elif change == "text_chars":
            item["messages"][0]["text"] = "a" * 16385
        elif change == "text_bytes":
            item["messages"][0]["text"] = "🌍" * 8193
        elif change == "text_nul":
            item["messages"][0]["text"] = "\x00"
        elif change == "text_surrogate":
            item["messages"][0]["text"] = "\ud800"
        elif change == "total":
            for message in item["messages"]:
                message["text"] = "x" * 3000
        variants.append(item)
    for variant in variants:
        with pytest.raises(ValidationError):
            ManagedTurnRequest.model_validate(variant)


@pytest.mark.parametrize("status,draft,code", [("DRAFT_READY",None,None),("DRAFT_READY","text","MODEL_ERROR"),
    ("FAILED","draft","MODEL_ERROR"),("UNKNOWN",None,None),("NO_RESPONSE",None,"MODEL_ERROR")])
def test_closed_result_shapes(status,draft,code):
    with pytest.raises(ValidationError):
        ManagedTurnResult.from_request(ManagedTurnRequest.model_validate(payload()),
            status=status,draft_text=draft,error_code=code,operation_refs=[])


@pytest.mark.parametrize("service,result,code", [
    ("bitrix", build_tool_result('{"error":"x","error":"y"}'), "INVALID_RESULT"),
    ("bitrix", build_tool_result('{"error":NaN}'), "INVALID_RESULT"),
    ("bitrix", build_tool_result('NaN'), "INVALID_RESULT"),
    ("bitrix", build_tool_result('{malformed'), "INVALID_RESULT"),
    ("bitrix", SimpleNamespace(structuredContent={"value":object()}, content=[]), "INVALID_RESULT"),
    ("bitrix", SimpleNamespace(structuredContent={"value":"\ud800"}, content=[]), "INVALID_RESULT"),
    ("bitrix", build_tool_result("x" * 131073), "CONTEXT_LIMIT"),
    ("bitrix", build_tool_result(*(["x"] * 33)), "CONTEXT_LIMIT"),
    ("bitrix", SimpleNamespace(structuredContent={"value":[0] * 4097}, content=[]), "CONTEXT_LIMIT"),
    ("bitrix", build_tool_result("["*34 + "0" + "]"*34), "CONTEXT_LIMIT"),
    ("google_workspace", build_tool_result('{"ok":false}'), "INVALID_RESULT"),
    ("custom", SimpleNamespace(isError=True, content=[]), "TOOL_EXECUTION_UNKNOWN"),
])
def test_classifier_bounded_invalid_and_protocol_failure(service,result,code):
    with pytest.raises(ManagedExecutionError) as error:
        inspect_managed_tool_result(service,result)
    assert error.value.code == code


def test_classifier_cyclic_and_conflicting_representations():
    cyclic=[]
    cyclic.append(cyclic)
    with pytest.raises(ManagedExecutionError, match="INVALID_RESULT"):
        inspect_managed_tool_result("bitrix", SimpleNamespace(structuredContent=cyclic,content=[]))
    with pytest.raises(ManagedExecutionError, match="TOOL_EXECUTION_UNKNOWN"):
        inspect_managed_tool_result("google_workspace", SimpleNamespace(
            structuredContent={"ok":False,"error":SENTINEL},content=[SimpleNamespace(text='{"ok":true}')]))


@pytest.mark.parametrize("service,text", [("bitrix",'{"result":[{"ID":1,"error":"ordinary CRM field"},{"ID":2}]}'),
    ("google_workspace",'{"ok":true,"data":{"error":"ordinary record"}}'), ("bitrix",'opaque successful text'),
    ("custom",'{"error":"unknown family payload"}')])
def test_classifier_success_does_not_scan_business_data(service,text):
    inspect_managed_tool_result(service,build_tool_result(text))


def test_new_managed_secret_env_denylist_and_mapped_credentials(monkeypatch):
    for key in ("MANAGED_AI_AUTH_PRINCIPALS", "managed_ai_auth_principals", "Managed_AI_AUTH_Principals"):
        monkeypatch.setenv(key, SENTINEL)
    env = MCPManager("bitrix",{"webhook_url":"synthetic-tool-credential"})._build_env()
    assert all(key.lower() not in MANAGED_SECRET_ENV_KEYS for key in env)
    assert env["B24_WEBHOOK_URL"] == "synthetic-tool-credential"
    for mapping in ("env_mapping", "optional_env_mapping"):
        monkeypatch.setattr(get_settings(),"mcp_services",{"bitrix":dict(script_path="none",env_mapping={}, **{
            mapping: {"webhook_url":"mAnAgEd_AI_AUTH_PrInCiPaLs"}})}) if mapping == "optional_env_mapping" else monkeypatch.setattr(
                get_settings(),"mcp_services",{"bitrix":dict(script_path="none",env_mapping={"webhook_url":"managed_ai_auth_principals"})})
        with pytest.raises(RuntimeError, match="MCP environment configuration is invalid"):
            MCPManager("bitrix",{"webhook_url":"synthetic"})._build_env()


def test_ai_budget_drops_whole_unselected_and_recomputes_selector(monkeypatch):
    service = LLMService()
    monkeypatch.setattr(service.settings,"managed_ai_context_bytes",1500)
    communication = [dict(role="user",content="x"*600),dict(role="user",content="selected"),
                     dict(role="assistant",content="last"),dict(role="user",content="y"*600)]
    agent = SimpleNamespace(model_name="synthetic",temperature=.4)
    messages=[dict(role="system",content="trusted"),*communication]
    result=service._budget_managed_messages(agent,messages,[],[1],communication,False)
    assert result[0] == messages[0] and "positions 1." in result[1]["content"]
    assert result[2] == communication[1] and result[3:] == communication[2:]
    assert all(m["content"] != "x"*600 for m in result)
    monkeypatch.setattr(service.settings,"managed_ai_context_bytes",100)
    with pytest.raises(ManagedExecutionError,match="CONTEXT_LIMIT"):
        service._budget_managed_messages(agent,messages,[],[1],communication,False)


@pytest.mark.integration
async def test_selected_query_limit_rejected_before_kb_model_or_tools(api_client,db_session,monkeypatch,model):
    _,value=await setup(api_client,db_session,monkeypatch)
    value["messages"]=[dict(id=str(uuid4()),sequence=i,actor="CUSTOMER",direction="INBOUND",content_type="TEXT",text="x"*16384) for i in range(1,4)]
    value.update(input_first_sequence=1,input_last_sequence=3,context_last_sequence=3, selected_input_message_ids=[m["id"] for m in value["messages"]])
    result=(await execute(api_client,value)).json()
    assert result["status"] == "FAILED" and result["error_code"] == "CONTEXT_LIMIT"
    assert model.chat.completions.calls == []


@pytest.mark.integration
@pytest.mark.parametrize("kind",["oversized","malformed","structured_conflict"])
async def test_invalid_tool_results_stop_actual_loop(api_client,db_session,monkeypatch,model,tools,kind):
    agent,value=await setup(api_client,db_session,monkeypatch)
    await store_credentials(api_client,agent["id"],"bitrix",{"webhook_url":"https://synthetic.invalid/rest"},owner_id=UUID(agent["user_id"]))
    tools.result = {"oversized":build_tool_result("x"*131073), "malformed":build_tool_result('{"x":NaN}'),
        "structured_conflict":SimpleNamespace(structuredContent={"error":SENTINEL},content=[SimpleNamespace(text='{"ok":true}')])}[kind]
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call("one","mutate","{}")]),build_llm_response(content="should not consume"))
    result=(await execute(api_client,value)).json()
    assert result["status"] == "UNKNOWN" and result["draft_text"] is None
    assert len(model.chat.completions.calls) == 1 and len(tools.calls) == 1


@pytest.mark.integration
async def test_no_tool_cleanup_and_invalid_result_are_failed(api_client,db_session,monkeypatch,model,tools):
    agent,value=await setup(api_client,db_session,monkeypatch)
    await store_credentials(api_client,agent["id"],"bitrix",{"webhook_url":"https://synthetic.invalid/rest"},owner_id=UUID(agent["user_id"]))
    tools.cleanup_error=True
    model.chat.completions.queue(build_llm_response(content="answer"))
    result=(await execute(api_client,value)).json()
    assert result["status"]=="FAILED" and result["error_code"]=="TOOL_CLEANUP_FAILED"
    tools.cleanup_error=False
    model.chat.completions.queue(build_llm_response(content="x"*16385))
    result=(await execute(api_client,value)).json()
    assert result["status"]=="FAILED" and result["error_code"]=="INVALID_RESULT"
