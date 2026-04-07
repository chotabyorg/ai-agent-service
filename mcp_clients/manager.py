"""Isolated MCP subprocess manager for tenant-scoped tool access."""

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from core.config import get_settings


class MCPManager:
    """Manage a single isolated MCP server subprocess."""

    def __init__(self, service_name: str, credentials: dict[str, Any]) -> None:
        self.settings = get_settings()
        self.service_name = service_name.lower()
        self.credentials = credentials
        self.stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self._tools: list[Any] = []

    async def __aenter__(self) -> "MCPManager":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _get_service_config(self) -> dict:
        config = self.settings.mcp_services.get(self.service_name)
        if not config:
            raise ValueError(f"Unsupported MCP service: {self.service_name}")
        return config

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        config = self._get_service_config()
        for cred_key, env_key in config["env_mapping"].items():
            value = self.credentials.get(cred_key)
            if not value:
                raise ValueError(f"Missing credential '{cred_key}' for {self.service_name}")
            env[env_key] = str(value)
        return env

    def _get_server_script_path(self) -> Path:
        config = self._get_service_config()
        return Path(__file__).resolve().parent.parent / config["script_path"]

    def _build_server_params(self) -> StdioServerParameters:
        script_path = self._get_server_script_path()
        return StdioServerParameters(
            command=sys.executable,
            args=[str(script_path)],
            env=self._build_env(),
        )

    async def initialize(self) -> list[Any]:
        """Start the MCP subprocess, initialize session and fetch tool metadata."""

        if self.session is not None:
            return self._tools

        self.stack = AsyncExitStack()
        server_params = self._build_server_params()
        read_stream, write_stream = await self.stack.enter_async_context(stdio_client(server_params))
        self.session = await self.stack.enter_async_context(ClientSession(read_stream, write_stream))
        await self.session.initialize()

        server_tools = await self.session.list_tools()
        self._tools = list(server_tools.tools)
        return self._tools

    async def close(self) -> None:
        """Stop the MCP subprocess and clear the active session."""

        if self.stack is not None:
            await self.stack.aclose()

        self.stack = None
        self.session = None
        self._tools = []

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """Convert MCP tool descriptors into OpenAI tool format."""

        return [self.to_openai_tool(tool) for tool in self._tools]

    @staticmethod
    def to_openai_tool(tool: Any) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "parameters": getattr(tool, "inputSchema", None)
                or {"type": "object", "properties": {}},
            },
        }

    async def call_tool(self, tool_name: str, tool_args: dict[str, Any] | None = None) -> Any:
        """Route a tool invocation to the active MCP session."""

        if self.session is None:
            raise RuntimeError("MCP session is not initialized.")

        return await self.session.call_tool(tool_name, tool_args or {})

    async def get_tools_metadata(self) -> dict[str, dict]:
        """Читает tools://metadata из MCP-сервера."""
        if self.session is None:
            raise RuntimeError("MCP session is not initialized.")
        result = await self.session.read_resource("tools://metadata")
        content = result.contents[0].text if result.contents else "{}"
        return json.loads(content)
