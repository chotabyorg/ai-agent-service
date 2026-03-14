from types import SimpleNamespace

import pytest

from mcp_clients.manager import MCPManager


class FakeAsyncContextManager:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeClientSession:
    instances: list["FakeClientSession"] = []

    def __init__(self, read_stream, write_stream) -> None:
        self.read_stream = read_stream
        self.write_stream = write_stream
        self.initialized = False
        self.calls: list[tuple[str, dict[str, object]]] = []
        FakeClientSession.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="find_contact",
                    description="Find a CRM contact",
                    inputSchema={"type": "object", "properties": {"phone": {"type": "string"}}},
                )
            ]
        )

    async def call_tool(self, tool_name: str, tool_args: dict[str, object]):
        self.calls.append((tool_name, tool_args))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": true}')])


@pytest.mark.contract
def test_build_env_injects_bitrix_webhook() -> None:
    manager = MCPManager("bitrix", {"webhook_url": "https://example.bitrix/rest"})

    env = manager._build_env()

    assert env["B24_WEBHOOK_URL"] == "https://example.bitrix/rest"


@pytest.mark.contract
def test_build_env_raises_for_unsupported_service() -> None:
    manager = MCPManager("custom", {"token": "secret"})

    with pytest.raises(ValueError, match="Unsupported MCP service"):
        manager._build_env()


@pytest.mark.contract
@pytest.mark.asyncio
async def test_initialize_loads_tools_and_call_tool_routes_to_client_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeClientSession.instances.clear()

    def fake_stdio_client(server_params):
        return FakeAsyncContextManager(("reader", "writer"))

    monkeypatch.setattr("mcp_clients.manager.stdio_client", fake_stdio_client)
    monkeypatch.setattr("mcp_clients.manager.ClientSession", FakeClientSession)

    manager = MCPManager("bitrix", {"webhook_url": "https://example.bitrix/rest"})

    tools = await manager.initialize()
    openai_tools = manager.get_openai_tools()
    result = await manager.call_tool("find_contact", {"phone": "+123"})
    await manager.close()

    assert len(tools) == 1
    assert openai_tools == [
        {
            "type": "function",
            "function": {
                "name": "find_contact",
                "description": "Find a CRM contact",
                "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}},
            },
        }
    ]
    assert FakeClientSession.instances[0].initialized is True
    assert FakeClientSession.instances[0].calls == [("find_contact", {"phone": "+123"})]
    assert result.content[0].text == '{"ok": true}'
