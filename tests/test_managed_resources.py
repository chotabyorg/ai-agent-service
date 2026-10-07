"""Real pinned SDK scopes at the direct resource-owner seam outside managed policy."""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

import mcp_clients.manager as manager_module
import services.llm_service as llm_module
from core.config import get_settings
from tests.test_managed_turns import SENTINEL, execute, setup
from tests.test_chat_flow import store_credentials
from uuid import UUID


@pytest.fixture
def owned_resources(monkeypatch, tmp_path):
    from services.bitrix_webhook_validator import BitrixCredentialsValidationResult, BitrixCredentialsValidationStatus

    async def validate_child(credentials):
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.VALID, normalized_credentials=credentials,
        )
    monkeypatch.setattr("api.v1.agents.validate_bitrix_credentials", validate_child)
    pids = tmp_path / "child_pids"
    events, handles, clients = [], [], []
    original_stdio = manager_module.stdio_client
    original_session = manager_module.ClientSession

    @asynccontextmanager
    async def observed_stdio(params, errlog):
        events.append(("stdio_enter", asyncio.current_task()))
        handles.append(errlog)
        try:
            async with original_stdio(params, errlog=errlog) as value:
                if state.get("slow_enter"):
                    state["in_enter"].set()
                    await state["release_enter"].wait()
                yield value
        finally:
            events.append(("stdio_exit", asyncio.current_task()))

    class ObservedSession(original_session):
        async def __aenter__(self):
            events.append(("session_enter", asyncio.current_task()))
            return await super().__aenter__()

        async def __aexit__(self, *args):
            try:
                return await super().__aexit__(*args)
            finally:
                events.append(("session_exit", asyncio.current_task()))

    monkeypatch.setattr(manager_module, "stdio_client", observed_stdio)
    monkeypatch.setattr(manager_module, "ClientSession", ObservedSession)

    def create_client(**kwargs):
        async def response(request):
            if state.get("block_model"):
                state["model_started"].set()
                await state["model_release"].wait()
            if state["model_error"]:
                raise RuntimeError(SENTINEL)
            return httpx.Response(200, json={"id":"synthetic", "object":"chat.completion", "created":0,
                "model":"synthetic", "choices":[{"index":0,"finish_reason":"stop", "message":{"role":"assistant","content":"safe draft"}}]})
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        client = AsyncOpenAI(**kwargs, http_client=transport_client)
        clients.append((client, transport_client))
        events.append(("client_enter", asyncio.current_task()))
        original_close = client.close
        async def close():
            events.append(("client_exit", asyncio.current_task()))
            await original_close()
        client.close = close
        return client
    monkeypatch.setattr(llm_module, "AsyncOpenAI", create_client)
    monkeypatch.setattr(get_settings(), "managed_ai_execution_seconds", 1.0)
    state = dict(pids=pids, events=events, handles=handles, clients=clients, model_error=False)
    return state


def configure_child(monkeypatch, state, mode, second=None):
    script = str(Path(__file__).with_name("managed_mcp_child.py"))
    configs = {"bitrix": dict(script_path=script,
        env_mapping={"mode":"GOAL2_CHILD_MODE", "pids":"GOAL2_CHILD_PIDS"})}
    if second:
        configs["google_workspace"] = dict(script_path=script,
            env_mapping={"mode":"GOAL2_CHILD_MODE", "pids":"GOAL2_CHILD_PIDS"})
    monkeypatch.setattr(get_settings(), "mcp_services", configs)


async def component_execute(state, db_session, mode, second=None):
    """Actual MCPManager/Goal2 owner, never a managed policy override.

    The endpoint intentionally cannot exercise stdio now. Preserve real SDK
    acquisition/cancellation/LIFO/child-reap proof by directly owning components
    with the existing same-task resource owner and native client cleanup.
    """
    import anyio
    from services.managed_runtime import ManagedRuntime, join_owner
    from services.managed_turn_service import _OwnedDBContext, _close_client
    from services.managed_tools import ManagedExecutionError
    from services.managed_http_cleanup import own_managed_http

    runtime = ManagedRuntime(get_settings().managed_ai_execution_seconds)
    async def provider():
        async with db_session() as db:
            yield db
    async def owned():
        status, code = "DRAFT_READY", None
        cancellation = None
        with anyio.CancelScope(shield=True) as lifecycle:
            try:
                await runtime.stack.enter_async_context(_OwnedDBContext(provider))
                client = llm_module.LLMService()._create_llm_client("synthetic", UUID(int=1), managed=True)
                runtime.stack.push_async_callback(_close_client, client)
                own_managed_http(client._client)
                for family, child_mode in [("bitrix", mode), *([("google_workspace", second)] if second else [])]:
                    manager = manager_module.MCPManager(family, {"mode": child_mode, "pids": str(state["pids"])})
                    try:
                        await manager.acquire_owned(runtime.stack, runtime)
                        await runtime.operation(manager.get_tools_metadata())
                    except ManagedExecutionError:
                        raise
                    except Exception:
                        raise ManagedExecutionError("TOOL_SETUP_FAILED") from None
                await runtime.operation(client.chat.completions.create(model="synthetic", messages=[{"role":"user", "content":"fixture"}]))
            except asyncio.CancelledError as error:
                cancellation = error
            except ManagedExecutionError as error:
                status, code = "FAILED", error.code
            finally:
                try:
                    await runtime.cleanup(lifecycle)
                except BaseException:
                    status, code = "FAILED", "TOOL_CLEANUP_FAILED"
        if cancellation or runtime.cancel_requested:
            raise cancellation or asyncio.CancelledError()
        return {"status": status, "error_code": code, "operation_refs": []}
    owner = asyncio.create_task(owned(), name="managed-turn-owner")
    return await join_owner(owner, runtime)


def assert_released(state, before):
    tasks = set(asyncio.all_tasks()) - before - {asyncio.current_task()}
    assert not tasks, [(task.get_name(), str(task.get_coro())) for task in tasks]
    events = state["events"]
    assert events and len({task for _, task in events}) == 1
    assert {task.get_name() for _, task in events} == {"managed-turn-owner"}
    assert len([stage for stage, _ in events if stage == "stdio_enter"]) == len([stage for stage, _ in events if stage == "stdio_exit"])
    assert len([stage for stage, _ in events if stage == "session_enter"]) == len([stage for stage, _ in events if stage == "session_exit"])
    assert all(handle.closed for handle in state["handles"])
    assert all(client.is_closed() and pool.is_closed for client, pool in state["clients"])
    if state["pids"].exists():
        for line in state["pids"].read_text().splitlines():
            pid = int(line)
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            with pytest.raises(ProcessLookupError):
                os.killpg(pid, 0)


@pytest.mark.integration
@pytest.mark.parametrize("mode,code", [("stall_initialize","EXECUTION_TIMEOUT"), ("stall_list","EXECUTION_TIMEOUT"),
    ("stall_metadata","EXECUTION_TIMEOUT"), ("fail_list","TOOL_SETUP_FAILED"), ("fail_metadata","TOOL_SETUP_FAILED"),
    ("malformed_protocol","EXECUTION_TIMEOUT"), ("ignore_term","EXECUTION_TIMEOUT"), ("success",None)])
async def test_real_owned_lifo_deadline_partial_setup_and_stubborn_child(api_client, db_session, monkeypatch, owned_resources, caplog, capfd, mode, code):
    state = owned_resources
    agent, value = await setup(api_client, db_session, monkeypatch)
    configure_child(monkeypatch, state, mode)
    await store_credentials(api_client, agent["id"], "bitrix", {"mode":mode, "pids":str(state["pids"]), "webhook_url":"https://synthetic.invalid/rest", "client_id":"fake", "client_secret":"fake", "refresh_token":"fake"}, owner_id=UUID(agent["user_id"]))
    before = set(asyncio.all_tasks())
    started = time.monotonic()
    result = await component_execute(state, db_session, mode)
    assert time.monotonic() - started < 9
    assert result["error_code"] == code, result
    assert result["status"] == ("FAILED" if code else "DRAFT_READY")
    assert result["operation_refs"] == []
    assert_released(state, before)
    output = capfd.readouterr()
    assert SENTINEL not in json.dumps(result) + caplog.text + output.err + output.out


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["fail_list", "stall_metadata"])
async def test_successful_manager_then_partial_second_unwinds_every_scope(api_client, db_session, monkeypatch, owned_resources, mode):
    state = owned_resources
    agent, value = await setup(api_client, db_session, monkeypatch)
    configure_child(monkeypatch, state, "success", second=mode)
    for service, child_mode in (("bitrix","success"), ("google_workspace",mode)):
        await store_credentials(api_client, agent["id"], service, {"mode":child_mode, "pids":str(state["pids"]), "webhook_url":"https://synthetic.invalid/rest", "client_id":"fake", "client_secret":"fake", "refresh_token":"fake"}, owner_id=UUID(agent["user_id"]))
    before = set(asyncio.all_tasks())
    result = await component_execute(state, db_session, "success", second=mode)
    assert result["error_code"] == ("TOOL_SETUP_FAILED" if mode == "fail_list" else "EXECUTION_TIMEOUT")
    assert len(state["pids"].read_text().splitlines()) == 2
    assert_released(state, before)


@pytest.mark.integration
async def test_raw_http_cancellation_repeated_joins_owner_and_resources(api_client, db_session, monkeypatch, owned_resources):
    state = owned_resources
    agent, value = await setup(api_client, db_session, monkeypatch)
    configure_child(monkeypatch, state, "ignore_term")
    await store_credentials(api_client, agent["id"], "bitrix", {"mode":"ignore_term", "pids":str(state["pids"]), "webhook_url":"https://synthetic.invalid/rest", "client_id":"fake", "client_secret":"fake", "refresh_token":"fake"}, owner_id=UUID(agent["user_id"]))
    before = set(asyncio.all_tasks())
    request = asyncio.create_task(component_execute(state, db_session, "ignore_term"), name="cancelled-component-request")
    for _ in range(150):
        if state["pids"].exists():
            break
        await asyncio.sleep(.01)
    assert state["pids"].exists()
    request.cancel()
    await asyncio.sleep(.05)
    request.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert time.monotonic() - started < 9
    assert_released(state, before)


@pytest.mark.integration
async def test_real_direct_client_failure_closes_pool(api_client, db_session, monkeypatch, owned_resources):
    state = owned_resources
    _, value = await setup(api_client, db_session, monkeypatch)
    state["model_error"] = True
    before = set(asyncio.all_tasks())
    result = (await execute(api_client, value)).json()
    assert result["status"] == "FAILED" and result["error_code"] == "MODEL_UNAVAILABLE"
    assert_released(state, before)


@pytest.mark.integration
async def test_cancel_during_real_stdio_enter_defers_until_resource_registered(api_client, db_session, monkeypatch, owned_resources):
    state = owned_resources
    state.update(slow_enter=True, in_enter=asyncio.Event(), release_enter=asyncio.Event())
    agent, value = await setup(api_client, db_session, monkeypatch)
    configure_child(monkeypatch, state, "success")
    await store_credentials(api_client, agent["id"], "bitrix", {"mode":"success", "pids":str(state["pids"]), "webhook_url":"https://synthetic.invalid/rest"}, owner_id=UUID(agent["user_id"]))
    before = set(asyncio.all_tasks())
    request = asyncio.create_task(component_execute(state, db_session, "success"))
    await state["in_enter"].wait()
    request.cancel()
    await asyncio.sleep(.03)
    assert not request.done()
    state["release_enter"].set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not any(stage == "session_enter" for stage, _ in state["events"])
    assert_released(state, before)


@pytest.mark.integration
async def test_real_stdio_spawn_failure_closes_registered_handles(api_client, db_session, monkeypatch, owned_resources):
    state = owned_resources
    agent, value = await setup(api_client, db_session, monkeypatch)
    configure_child(monkeypatch, state, "success")
    await store_credentials(api_client, agent["id"], "bitrix", {"mode":"success", "pids":str(state["pids"]), "webhook_url":"https://synthetic.invalid/rest"}, owner_id=UUID(agent["user_id"]))
    original = manager_module.MCPManager._build_server_params
    def invalid_command(self):
        params = original(self)
        params.command = "/nonexistent/goal2-synthetic-python"
        return params
    monkeypatch.setattr(manager_module.MCPManager, "_build_server_params", invalid_command)
    before = set(asyncio.all_tasks())
    result = await component_execute(state, db_session, "success")
    assert result["status"] == "FAILED" and result["error_code"] == "TOOL_SETUP_FAILED"
    assert_released(state, before)


@pytest.mark.integration
async def test_actual_asgi_disconnect_signals_and_joins_owner(api_client, db_session, monkeypatch, owned_resources):
    import main
    from tests.test_managed_turns import TOKEN

    state = owned_resources
    agent, value = await setup(api_client, db_session, monkeypatch)
    configure_child(monkeypatch, state, "ignore_term")
    await store_credentials(api_client, agent["id"], "bitrix", {"mode":"ignore_term", "pids":str(state["pids"]), "webhook_url":"https://synthetic.invalid/rest"}, owner_id=UUID(agent["user_id"]))
    state.update(block_model=True, model_started=asyncio.Event(), model_release=asyncio.Event())
    received = 0
    async def receive():
        nonlocal received
        received += 1
        if received == 1:
            return dict(type="http.request", body=json.dumps(value).encode(), more_body=False)
        await asyncio.wait_for(state["model_started"].wait(), 2)
        return dict(type="http.disconnect")
    sent = []
    async def send(event):
        sent.append(event)
    scope = dict(type="http", path="/internal/v1/managed-turns/execute", method="POST", scheme="http",
                 http_version="1.1", server=("127.0.0.1", 8000), client=("127.0.0.1", 1234),
                 root_path="", query_string=b"", headers=[(b"x-service-token", TOKEN.encode()),
                                                           (b"content-type", b"application/json")])
    before = set(asyncio.all_tasks())
    with pytest.raises(asyncio.CancelledError):
        await main.app(scope, receive, send)
    assert not sent and not state["pids"].exists()
    assert_released(state, before)
