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
from core.managed_security import MANAGED_SECRET_ENV_KEYS


class MCPPreAcquisitionError(ValueError):
    """Unsupported service or missing credential before any resource acquisition."""


def _set_chat_failure_stage(stage: str) -> None:
    """Set a chat phase without importing the eager services package at module load."""

    from services.chat_diagnostics import ChatFailureStage, set_chat_failure_stage

    set_chat_failure_stage(ChatFailureStage(stage))


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
        env = {key: value for key, value in os.environ.items()
               if key.lower() not in MANAGED_SECRET_ENV_KEYS}
        config = self._get_service_config()
        for cred_key, env_key in config["env_mapping"].items():
            value = self.credentials.get(cred_key)
            if not value:
                raise ValueError(f"Missing credential '{cred_key}' for {self.service_name}")
            env[env_key] = str(value)
        for cred_key, env_key in config.get("optional_env_mapping", {}).items():
            value = self.credentials.get(cred_key)
            if value:
                env[env_key] = str(value)
        if any(key.lower() in MANAGED_SECRET_ENV_KEYS for key in env):
            raise RuntimeError("MCP environment configuration is invalid")
        return env

    async def acquire_owned(self, stack: AsyncExitStack, runtime) -> "MCPManager":
        """Register each SDK resource in its owner task before setup awaits.

        ValueError during parameter validation is the sole skippable stage. Failed
        post-spawn initialize/list/metadata unwinds through the owner's stack.
        """
        try:
            server_params = self._build_server_params()
        except ValueError:
            raise MCPPreAcquisitionError("MCP configuration unavailable") from None
        runtime.check()
        errlog = open(os.devnull, "w", encoding="utf-8")
        stack.callback(errlog.close)
        # Clear fields after transport/session exits, also on partial acquisition.
        # A separate finally callback is kept outside the SDK scope callbacks.
        runtime.managers.append(self)
        runtime.started_stdio_count += 1
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(server_params, errlog=errlog)
        )
        runtime.check()
        self.session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        runtime.check()
        await runtime.operation(self.session.initialize())
        server_tools = await runtime.operation(self.session.list_tools())
        self._tools = list(server_tools.tools)
        return self

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
        _set_chat_failure_stage("mcp_initialize")
        await self.session.initialize()

        _set_chat_failure_stage("mcp_list_tools")
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
        _set_chat_failure_stage("mcp_metadata_read")
        result = await self.session.read_resource("tools://metadata")
        content = result.contents[0].text if result.contents else "{}"
        return json.loads(content)
