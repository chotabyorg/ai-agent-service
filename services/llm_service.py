"""LLM orchestration service with DB, Redis and MCP integration."""

import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_clients.manager import MCPManager
from services.agent_service import AgentService
from services.history_service import HistoryService
from services.session_service import SessionService


class ChatProcessingError(Exception):
    """Base error for chat message processing."""


class AgentNotFoundError(ChatProcessingError):
    """Raised when the target agent does not exist."""


class AgentInactiveError(ChatProcessingError):
    """Raised when the target agent is disabled."""


@dataclass
class ChatProcessingResult:
    session_id: UUID
    answer: str


class LLMService:
    """Generate responses using agent config, Redis history and MCP tools."""

    MAX_TOOL_STEPS = 20

    def __init__(
        self,
        history_service: HistoryService | None = None,
        agent_service: AgentService | None = None,
        session_service: SessionService | None = None,
    ) -> None:
        from core.config import get_settings

        self.settings = get_settings()
        self.history_service = history_service or HistoryService()
        self.agent_service = agent_service or AgentService()
        self.session_service = session_service or SessionService()
        self.llm = AsyncOpenAI(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
        )

    @staticmethod
    def _strip_thinking_tags(raw_answer: str) -> str:
        return re.sub(r"<thinking>.*?</thinking>", "", raw_answer, flags=re.DOTALL).strip()

    def _build_extra_headers(self) -> dict[str, str] | None:
        headers: dict[str, str] = {}
        if self.settings.llm_http_referer:
            headers["HTTP-Referer"] = self.settings.llm_http_referer
        if self.settings.llm_app_title:
            headers["X-Title"] = self.settings.llm_app_title
        return headers or None

    @staticmethod
    def _assistant_message_to_dict(message: object) -> dict[str, object]:
        tool_calls = []
        for tool_call in getattr(message, "tool_calls", []) or []:
            tool_calls.append(
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
            )

        payload: dict[str, object] = {
            "role": "assistant",
            "content": getattr(message, "content", "") or "",
        }
        if tool_calls:
            payload["tool_calls"] = tool_calls
        return payload

    @staticmethod
    def _extract_tool_result_content(tool_result: object) -> str:
        content_blocks = getattr(tool_result, "content", None)
        if not content_blocks:
            return "{}"

        if len(content_blocks) == 1:
            text = getattr(content_blocks[0], "text", None)
            if text is not None:
                return text

        text_blocks = [
            block.text
            for block in content_blocks
            if getattr(block, "type", "text") == "text" and getattr(block, "text", None) is not None
        ]
        if text_blocks:
            return json.dumps(text_blocks, ensure_ascii=False)

        return "{}"

    async def process_chat_message(
        self,
        db_session: AsyncSession,
        agent_id: UUID,
        external_client_id: str,
        channel: str,
        user_message: str,
    ) -> ChatProcessingResult:
        agent = await self.agent_service.get_agent_by_id(db_session, agent_id)
        if agent is None:
            raise AgentNotFoundError("Agent not found.")
        if not agent.is_active:
            raise AgentInactiveError("Agent is inactive.")

        conversation_session = await self.session_service.get_or_create_session(
            db_session,
            agent_id=agent_id,
            external_client_id=external_client_id,
            channel=channel,
        )
        session_id = conversation_session.id

        credentials_records = await self.agent_service.get_credentials(db_session, agent_id)
        history = await self.history_service.load_history(session_id)
        messages: list[dict[str, object]] = [
            {"role": "system", "content": agent.system_prompt},
            *history,
            {"role": "user", "content": user_message},
        ]

        executed_calls: set[str] = set()

        async with AsyncExitStack() as stack:
            tool_registry: dict[str, MCPManager] = {}
            openai_tools: list[dict[str, object]] = []

            for credentials_record in credentials_records:
                manager = MCPManager(
                    service_name=credentials_record.service_name,
                    credentials=credentials_record.credentials,
                )
                try:
                    await stack.enter_async_context(manager)
                except ValueError:
                    continue

                for tool in manager.get_openai_tools():
                    tool_name = tool["function"]["name"]
                    if tool_name in tool_registry:
                        continue
                    tool_registry[tool_name] = manager
                    openai_tools.append(tool)

            for _ in range(self.MAX_TOOL_STEPS):
                response = await self.llm.chat.completions.create(
                    model=agent.model_name or self.settings.llm_default_model,
                    temperature=agent.temperature,
                    messages=messages,
                    tools=openai_tools or None,
                    parallel_tool_calls=False,
                    extra_headers=self._build_extra_headers(),
                )

                message = response.choices[0].message
                if not message.tool_calls:
                    raw_answer = message.content or "I processed your request."
                    clean_answer = self._strip_thinking_tags(raw_answer)
                    if not clean_answer:
                        clean_answer = "I processed your request."

                    updated_history = history + [
                        {"role": "user", "content": user_message},
                        {"role": "assistant", "content": clean_answer},
                    ]
                    await self.history_service.save_history(session_id, updated_history)
                    return ChatProcessingResult(session_id=session_id, answer=clean_answer)

                messages.append(self._assistant_message_to_dict(message))

                for tool_call in message.tool_calls:
                    func_name = tool_call.function.name
                    func_args_str = tool_call.function.arguments

                    try:
                        func_args = json.loads(func_args_str)
                    except json.JSONDecodeError:
                        func_args = {}

                    call_signature = f"{func_name}:{func_args_str}"
                    if call_signature in executed_calls:
                        tool_result_text = json.dumps(
                            {
                                "error": (
                                    "CRITICAL SYSTEM WARNING: identical tool call blocked to prevent "
                                    "an infinite loop."
                                )
                            },
                            ensure_ascii=False,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": func_name,
                                "content": tool_result_text,
                            }
                        )
                        continue

                    executed_calls.add(call_signature)
                    manager = tool_registry.get(func_name)
                    if manager is None:
                        tool_result_text = json.dumps(
                            {"error": f"Tool '{func_name}' is not available."},
                            ensure_ascii=False,
                        )
                    else:
                        try:
                            tool_result = await manager.call_tool(func_name, func_args)
                            tool_result_text = self._extract_tool_result_content(tool_result)
                        except Exception as exc:
                            tool_result_text = json.dumps(
                                {"error": f"Tool execution failed: {exc}"},
                                ensure_ascii=False,
                            )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": func_name,
                            "content": tool_result_text,
                        }
                    )

        raise ChatProcessingError("Maximum number of LLM tool steps reached.")
