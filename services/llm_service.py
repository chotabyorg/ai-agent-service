"""LLM orchestration service with DB, Redis and MCP integration."""

import json
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_clients.manager import MCPManager, MCPPreAcquisitionError
from core.managed_security import managed_logging
from services.managed_tools import (ManagedExecutionError, inspect_managed_tool_result,
                                    parse_managed_tool_arguments, _bounded_json)
from services.managed_http_cleanup import close_managed_helper, own_managed_http
from services.managed_reasoning import managed_reasoning_fields
from services.managed_completion import select_managed_completion, observe_managed_completion
from services.chat_diagnostics import ChatFailureStage, set_chat_failure_stage
from services.agent_service import AgentService
from services.encryption_service import EncryptionService
from services.history_service import HistoryService
from services.knowledge_base_client import KnowledgeBaseClient
from services.knowledge_base_prompt import build_knowledge_base_system_message
from services.prompt_builder import ChatContext, PromptBuilder
from services.prompt_composer import PromptComposer, RuntimeCapabilityContext
from services.session_service import SessionService

logger = logging.getLogger(__name__)


class ChatProcessingError(Exception):
    """Base error for chat message processing."""


class AgentNotFoundError(ChatProcessingError):
    """Raised when the target agent does not exist."""


class AgentInactiveError(ChatProcessingError):
    """Raised when the target agent is disabled."""


class AgentKeyError(Exception):
    """Base error for agent API key issues (separate from ChatProcessingError)."""


class AgentNoApiKeyError(AgentKeyError):
    """Agent has no OpenRouter API key configured."""


class AgentApiKeyInvalidError(AgentKeyError):
    """OpenRouter API key is invalid or revoked."""


class AgentInsufficientFundsError(AgentKeyError):
    """Insufficient funds on the OpenRouter key."""


class AgentRateLimitError(AgentKeyError):
    """Rate limit exceeded on the OpenRouter key."""


class PrivacyProxyConnectionError(Exception):
    """Privacy proxy is unavailable or misconfigured."""


class PrivacyProxyAuthenticationError(Exception):
    """Upstream LLM key was rejected through privacy proxy."""


class PrivacyProxyInsufficientFundsError(Exception):
    """Upstream LLM key has insufficient funds through privacy proxy."""


class PrivacyProxyRateLimitError(Exception):
    """Upstream LLM rate limit was exceeded through privacy proxy."""


@dataclass
class ChatProcessingResult:
    session_id: UUID
    answer: str


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _to_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


class PrivacyProxyCompletions:
    def __init__(
        self,
        *,
        privacy_filter_base_url: str,
        internal_api_token: str,
        privacy_filter_timeout_seconds: float,
        llm_base_url: str,
        agent_api_key: str,
        privacy_session_id: UUID,
    ) -> None:
        self.privacy_filter_base_url = privacy_filter_base_url.rstrip("/")
        self.internal_api_token = internal_api_token
        self.privacy_filter_timeout_seconds = privacy_filter_timeout_seconds
        self.llm_base_url = llm_base_url
        self.agent_api_key = agent_api_key
        self.privacy_session_id = privacy_session_id

    async def create(self, **kwargs: Any) -> Any:
        if not self.internal_api_token:
            raise PrivacyProxyConnectionError("Internal API token is not configured.")

        extra_headers = kwargs.pop("extra_headers", None)
        payload = {
            "privacy_session_id": str(self.privacy_session_id),
            "upstream": {
                "base_url": self.llm_base_url,
                "api_key": self.agent_api_key,
                "extra_headers": extra_headers,
            },
            "chat_completion": {key: value for key, value in kwargs.items() if value is not None},
        }
        url = f"{self.privacy_filter_base_url}/api/v1/privacy-filter/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.internal_api_token}",
            "Content-Type": "application/json",
        }

        try:
            if managed_logging.get():
                client = httpx.AsyncClient(timeout=self.privacy_filter_timeout_seconds, trust_env=False)
                try:
                    own_managed_http(client)
                    response = await client.post(url, headers=headers, json=payload)
                finally:
                    await close_managed_helper(client)
            else:
                async with httpx.AsyncClient(timeout=self.privacy_filter_timeout_seconds) as client:
                    response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise PrivacyProxyConnectionError("Privacy filter request timed out.") from exc
        except httpx.HTTPError as exc:
            raise PrivacyProxyConnectionError("Cannot connect to privacy filter service.") from exc

        if response.status_code in {401, 403}:
            raise PrivacyProxyAuthenticationError("OpenRouter key is invalid. Agent deactivated.")
        if response.status_code == 402:
            raise PrivacyProxyInsufficientFundsError("No funds on OpenRouter. Agent deactivated.")
        if response.status_code == 429:
            raise PrivacyProxyRateLimitError("Rate limit exceeded. Try again later.")
        if response.status_code >= 400:
            raise PrivacyProxyConnectionError("Privacy filter service returned an error.")

        try:
            return _to_namespace(response.json())
        except ValueError as exc:
            raise PrivacyProxyConnectionError("Privacy filter service returned invalid JSON.") from exc


class PrivacyProxyLLMClient:
    def __init__(
        self,
        *,
        privacy_filter_base_url: str,
        internal_api_token: str,
        privacy_filter_timeout_seconds: float,
        llm_base_url: str,
        agent_api_key: str,
        privacy_session_id: UUID,
    ) -> None:
        self.chat = SimpleNamespace(
            completions=PrivacyProxyCompletions(
                privacy_filter_base_url=privacy_filter_base_url,
                internal_api_token=internal_api_token,
                privacy_filter_timeout_seconds=privacy_filter_timeout_seconds,
                llm_base_url=llm_base_url,
                agent_api_key=agent_api_key,
                privacy_session_id=privacy_session_id,
            )
        )


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
        self.prompt_builder = PromptBuilder()
        self.prompt_composer = PromptComposer(self.prompt_builder)
        self.encryption_service = EncryptionService()

    def _get_agent_api_key(self, agent: object) -> str | None:
        """Decrypt and return the agent's OpenRouter API key."""
        encrypted = getattr(agent, "encrypted_openrouter_key", None)
        if not encrypted:
            return None
        return self.encryption_service.decrypt(encrypted)

    async def _deactivate_agent(
        self, agent: object, db_session: AsyncSession, reason: str, runtime=None
    ) -> None:
        """Deactivate an agent due to key problems."""
        agent.is_active = False  # type: ignore[attr-defined]
        if runtime:
            await runtime.operation(db_session.commit())
        else:
            await db_session.commit()
        logger.warning("Agent %s deactivated: %s", getattr(agent, "id", "?"), reason)

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

    async def _build_knowledge_base_message(
        self,
        *,
        agent: object,
        query: str,
        runtime=None,
    ) -> str | None:
        if not self.settings.knowledge_base_enabled:
            return None
        if not self.settings.knowledge_base_service_url or not self.settings.internal_api_token:
            return None

        try:
            client = KnowledgeBaseClient(
                base_url=self.settings.knowledge_base_service_url,
                service_token=self.settings.internal_api_token,
                timeout=self.settings.knowledge_base_timeout_seconds,
                **({"managed": True} if runtime else {}),
            )
            try:
                pending = client.retrieve_context(
                    owner_id=str(agent.user_id),  # type: ignore[attr-defined]
                    agent_id=agent.id,  # type: ignore[attr-defined]
                    query=query,
                )
                response = await runtime.operation(pending) if runtime else await pending
            finally:
                await client.aclose()

            if not response.contexts:
                return None
            return build_knowledge_base_system_message(
                response.contexts,
                max_chars_per_chunk=self.settings.knowledge_base_max_chars_per_chunk,
                max_total_context_chars=self.settings.knowledge_base_max_total_context_chars,
            )
        except ManagedExecutionError:
            raise
        except Exception:
            logger.warning(
                "Knowledge Base retrieval failed for agent %s; continuing without KB context.",
                getattr(agent, "id", "?"),
                exc_info=True,
            )
            return None

    @staticmethod
    def _assistant_message_to_dict(message: object, *, managed: bool = False) -> dict[str, object]:
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
            if managed:
                payload.update(managed_reasoning_fields(message))
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
        user_id: UUID | None = None,
    ) -> ChatProcessingResult:
        set_chat_failure_stage(ChatFailureStage.AGENT_LOOKUP)
        agent = await self.agent_service.get_agent_by_id(
            db_session,
            agent_id,
            user_id=user_id,
        )
        if agent is None:
            raise AgentNotFoundError("Agent not found.")
        if not agent.is_active:
            raise AgentInactiveError("Agent is inactive.")

        set_chat_failure_stage(ChatFailureStage.AGENT_CONFIGURATION)
        agent_api_key = self._get_agent_api_key(agent)
        if not agent_api_key:
            raise AgentNoApiKeyError("Agent has no OpenRouter API key configured.")

        set_chat_failure_stage(ChatFailureStage.SESSION_SETUP)
        conversation_session = await self.session_service.get_or_create_session(
            db_session,
            agent_id=agent_id,
            external_client_id=external_client_id,
            channel=channel,
        )
        session_id = conversation_session.id

        llm_client = self._create_llm_client(agent_api_key, session_id)
        set_chat_failure_stage(ChatFailureStage.CONTEXT_LOAD)
        credentials_records = await self.agent_service.get_credentials(db_session, agent_id)
        history = await self.history_service.load_history(session_id)

        async def persist(clean_answer):
            updated_history = history + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": clean_answer},
            ]
            set_chat_failure_stage(ChatFailureStage.HISTORY_PERSIST)
            await self.history_service.save_history(session_id, updated_history)

        answer = await self._execute_core(
            db_session, agent=agent, llm_client=llm_client, credentials_records=credentials_records,
            communication=history + [{"role": "user", "content": user_message}],
            chat_context=ChatContext(channel=channel, external_client_id=external_client_id),
            query=user_message, completion_hook=persist,
        )
        return ChatProcessingResult(session_id=session_id, answer=answer)

    def _create_llm_client(self, agent_api_key, privacy_session_id, *, managed=False):
        if self.settings.privacy_filter_enabled:
            if managed:
                try:
                    privacy_url = httpx.URL(self.settings.privacy_filter_base_url)
                    valid = privacy_url.scheme in {"http", "https"} and bool(privacy_url.host)
                except Exception:
                    valid = False
                if not valid or not self.settings.internal_api_token:
                    raise ManagedExecutionError("PRIVACY_UNAVAILABLE")
            return PrivacyProxyLLMClient(
                privacy_filter_base_url=self.settings.privacy_filter_base_url,
                internal_api_token=self.settings.internal_api_token,
                privacy_filter_timeout_seconds=self.settings.privacy_filter_timeout_seconds,
                llm_base_url=self.settings.llm_base_url,
                agent_api_key=agent_api_key,
                privacy_session_id=privacy_session_id,
            )
        else:
            return AsyncOpenAI(
                base_url=self.settings.llm_base_url,
                api_key=agent_api_key,
                **({"max_retries": 0, "timeout": self.settings.managed_ai_execution_seconds} if managed else {}),
            )


    @staticmethod
    @asynccontextmanager
    async def _execution_stack(runtime):
        if runtime:
            yield runtime.stack
        else:
            async with AsyncExitStack() as stack:
                yield stack

    async def _execute_core(
        self, db_session, *, agent, llm_client, credentials_records, communication,
        chat_context, query, completion_hook=None, runtime=None, selected_indices=None, simple_selection=True,
    ) -> str:
        """Shared prompt/KB/model/tools/normalization; adapters own persistence."""
        managed_request = runtime.request if runtime else None
        prompt_context = None
        if runtime and runtime.business is not None:
            project = getattr(runtime.business, "prompt_context", None)
            if callable(project):
                prompt_context = RuntimeCapabilityContext.model_validate(
                    await runtime.operation(project())
                )
        system_prompt = self.prompt_composer.compose(
            agent, chat_context, runtime_context=prompt_context,
        )
        knowledge_base_message = await self._build_knowledge_base_message(
            agent=agent,
            query=query,
            **({"runtime": runtime} if runtime else {}),
        )

        messages: list[dict[str, object]] = [
            {"role": "system", "content": system_prompt},
        ]
        if knowledge_base_message:
            messages.append({"role": "system", "content": self.prompt_composer.knowledge_section(knowledge_base_message)})
        messages.extend(communication)

        executed_calls: set[str] = set()

        set_chat_failure_stage(ChatFailureStage.MCP_SETUP)
        async with self._execution_stack(runtime) as stack:
            tool_registry: dict[str, MCPManager | None] = {}
            openai_tools: list[dict[str, object]] = []

            if runtime:
                # Raw MCP is quarantined before lookup/decrypt/construction, not
                # merely removed from model advertisement. Only code-owned
                # business controls can enter the managed registry.
                openai_tools = runtime.business.openai_tools() if runtime.business else []
                tool_registry = {tool["function"]["name"]: None for tool in openai_tools}
            else:
                for credentials_record in credentials_records:
                    manager = MCPManager(
                        service_name=credentials_record.service_name,
                        credentials=self.agent_service.decrypt_credentials_record(
                            credentials_record
                        ),
                    )
                    if runtime:
                        try:
                            await manager.acquire_owned(stack, runtime)
                        except MCPPreAcquisitionError:
                            continue
                        except ManagedExecutionError:
                            raise
                        except Exception:
                            raise ManagedExecutionError("TOOL_SETUP_FAILED") from None
                    else:
                        try:
                            await stack.enter_async_context(manager)
                        except ValueError:
                            continue

                    enabled = credentials_record.enabled_tools
                    try:
                        pending_metadata = manager.get_tools_metadata()
                        metadata = await runtime.operation(pending_metadata) if runtime else await pending_metadata
                    except ManagedExecutionError:
                        raise
                    except Exception:
                        if runtime:
                            raise ManagedExecutionError("TOOL_SETUP_FAILED") from None
                        raise
                    for tool in manager.get_openai_tools():
                        tool_name = tool["function"]["name"]
                        if tool_name in tool_registry:
                            continue
                        tier = metadata.get(tool_name, {}).get("tier", "core")
                        if enabled is None:
                            if tier != "core":
                                continue
                        elif "*" not in enabled and tool_name not in enabled:
                            continue
                        tool_registry[tool_name] = manager
                        openai_tools.append(tool)
            required_proposal_id = (
                str(prompt_context.pending.proposal_id)
                if runtime and prompt_context is not None and prompt_context.pending is not None
                else None
            )
            if required_proposal_id is not None and "business_decide_proposal" not in tool_registry:
                raise ManagedExecutionError("INVALID_RESULT")
            tool_choice = ({"type": "function", "function": {"name": "business_decide_proposal"}}
                           if required_proposal_id is not None else None)
            preparation_schemas = {}
            if runtime:
                from services.managed_preparation import PREPARATION_TOOLS, PreparationSchema
                preparation_schemas = {tool["function"]["name"]: PreparationSchema(tool["function"]["parameters"])
                    for tool in openai_tools if tool["function"]["name"] in PREPARATION_TOOLS}
            preparation_correction = None
            correction_used = False
            seen_tool_ids = set()
            if runtime:
                messages = self._budget_managed_messages(
                    agent, messages, openai_tools, selected_indices, communication, simple_selection,
                    tool_choice=tool_choice, request=managed_request,
                )
            for step_index in range(self.MAX_TOOL_STEPS):
                set_chat_failure_stage(ChatFailureStage.LLM_REQUEST)
                try:
                    if runtime:
                        runtime.check()
                        if runtime.business is not None:
                            gate = await runtime.operation(runtime.business.guard_result(allow_incomplete=True))
                            if gate is not None:
                                if gate.halt_status:
                                    raise ManagedExecutionError(gate.error_code, processing_status=gate.halt_status)
                                return gate.safe_draft
                        self._check_managed_envelope(agent, messages, openai_tools,
                                                     tool_choice=tool_choice, request=managed_request)
                    model_extra_body = self._managed_model_extra_body(agent, request=managed_request) if runtime else {}
                    pending_response = llm_client.chat.completions.create(
                        model=agent.model_name or self.settings.llm_default_model,
                        temperature=agent.temperature,
                        max_tokens=self.settings.llm_max_tokens,
                        messages=messages,
                        tools=openai_tools or None,
                        parallel_tool_calls=False,
                        **({"tool_choice": tool_choice} if tool_choice is not None else {}),
                        **({"extra_body": model_extra_body} if model_extra_body else {}),
                        extra_headers=self._build_extra_headers(),
                    )
                    response = await runtime.operation(pending_response) if runtime else await pending_response
                except AuthenticationError:
                    await self._deactivate_agent(agent, db_session, "OpenRouter API key invalid or revoked", runtime=runtime)
                    if runtime:
                        raise ManagedExecutionError("MODEL_AUTH_FAILED") from None
                    raise AgentApiKeyInvalidError("OpenRouter key is invalid. Agent deactivated.")
                except RateLimitError as exc:
                    error_str = str(exc).lower()
                    if "402" in error_str or "insufficient" in error_str or "credits" in error_str:
                        await self._deactivate_agent(agent, db_session, "Insufficient funds on OpenRouter", runtime=runtime)
                        if runtime:
                            raise ManagedExecutionError("MODEL_INSUFFICIENT_FUNDS") from None
                        raise AgentInsufficientFundsError("No funds on OpenRouter. Agent deactivated.")
                    if runtime:
                        raise ManagedExecutionError("MODEL_RATE_LIMITED") from None
                    raise AgentRateLimitError("Rate limit exceeded. Try again later.")
                except APITimeoutError:
                    if runtime:
                        raise ManagedExecutionError("MODEL_TIMEOUT") from None
                    raise ChatProcessingError("LLM request timed out.")
                except APIConnectionError:
                    if runtime:
                        raise ManagedExecutionError("MODEL_UNAVAILABLE") from None
                    raise ChatProcessingError("Cannot connect to OpenRouter.")
                except APIError as exc:
                    if runtime:
                        raise ManagedExecutionError("MODEL_ERROR") from None
                    logger.error("OpenRouter error for agent %s: %s", agent.id, exc)
                    raise ChatProcessingError(f"LLM error: {exc.message}")
                except PrivacyProxyAuthenticationError:
                    await self._deactivate_agent(agent, db_session, "OpenRouter API key invalid or revoked", runtime=runtime)
                    if runtime:
                        raise ManagedExecutionError("MODEL_AUTH_FAILED") from None
                    raise AgentApiKeyInvalidError("OpenRouter key is invalid. Agent deactivated.")
                except PrivacyProxyInsufficientFundsError:
                    await self._deactivate_agent(agent, db_session, "Insufficient funds on OpenRouter", runtime=runtime)
                    if runtime:
                        raise ManagedExecutionError("MODEL_INSUFFICIENT_FUNDS") from None
                    raise AgentInsufficientFundsError("No funds on OpenRouter. Agent deactivated.")
                except PrivacyProxyRateLimitError:
                    if runtime:
                        raise ManagedExecutionError("MODEL_RATE_LIMITED") from None
                    raise AgentRateLimitError("Rate limit exceeded. Try again later.")
                except PrivacyProxyConnectionError as exc:
                    if runtime:
                        raise ManagedExecutionError("PRIVACY_UNAVAILABLE") from None
                    raise ChatProcessingError(str(exc))

                set_chat_failure_stage(ChatFailureStage.RESPONSE_PROCESSING)
                selected = None
                if runtime:
                    selected = select_managed_completion(response, self._strip_thinking_tags)
                    observe_managed_completion(response, selected,
                        request=runtime.request, step=step_index + 1)
                    message = selected.message
                else:
                    message = response.choices[0].message
                if preparation_correction is not None:
                    calls = getattr(message, "tool_calls", None)
                    if selected.branch != "TOOL_CALLS" or type(calls) is not list or len(calls) != 1:
                        raise ManagedExecutionError("INVALID_RESULT")
                    call = calls[0]
                    if (getattr(call, "type", None) != "function"
                            or not isinstance(getattr(call, "id", None), str) or not call.id
                            or call.id in seen_tool_ids
                            or getattr(getattr(call, "function", None), "name", None) != preparation_correction):
                        raise ManagedExecutionError("INVALID_RESULT")
                if required_proposal_id is not None:
                    # A pending customer's reply must reach the semantic runtime
                    # before reads or ordinary prose can bypass that decision.
                    calls = getattr(message, "tool_calls", None)
                    if selected.branch != "TOOL_CALLS" or type(calls) is not list or len(calls) != 1:
                        raise ManagedExecutionError("INVALID_RESULT")
                    call = calls[0]
                    function = getattr(call, "function", None)
                    if (getattr(call, "type", None) != "function"
                            or not isinstance(getattr(call, "id", None), str) or not call.id
                            or getattr(function, "name", None) != "business_decide_proposal"):
                        raise ManagedExecutionError("INVALID_RESULT")
                    decision_args = parse_managed_tool_arguments(getattr(function, "arguments", None))
                    if (set(decision_args) != {"proposal_id", "intent"}
                            or decision_args["proposal_id"] != required_proposal_id):
                        raise ManagedExecutionError("INVALID_RESULT")
                if (selected is not None and selected.branch != "TOOL_CALLS") or (
                        selected is None and not getattr(message, "tool_calls", None)):
                    if runtime and runtime.business is not None:
                        gate = await runtime.operation(runtime.business.guard_result())
                        if gate is not None:
                            if gate.halt_status:
                                raise ManagedExecutionError(gate.error_code, processing_status=gate.halt_status)
                            return gate.safe_draft
                    if selected is not None:
                        if selected.branch == "INVALID_SHAPE" or not selected.answer:
                            raise ManagedExecutionError("INVALID_RESULT")
                        clean_answer = selected.answer
                    else:
                        raw_answer = message.content or "I processed your request."
                        clean_answer = self._strip_thinking_tags(raw_answer)
                        if not clean_answer:
                            clean_answer = "I processed your request."

                    if completion_hook:
                        await completion_hook(clean_answer)
                    set_chat_failure_stage(ChatFailureStage.MCP_CLEANUP)
                    return clean_answer

                if runtime:
                    feedback = None
                    invalid_call = None
                    # Preflight preparation members before dispatching ANY call
                    # in this completion, including an earlier WRITE_SAFE call.
                    for call in message.tool_calls:
                        name = getattr(getattr(call, "function", None), "name", None)
                        if name not in PREPARATION_TOOLS:
                            continue
                        if name not in preparation_schemas:
                            raise ManagedExecutionError("TOOL_SETUP_FAILED")
                        arguments = parse_managed_tool_arguments(call.function.arguments)
                        feedback = preparation_schemas[name].feedback(arguments)
                        if feedback is not None:
                            invalid_call = call
                            break
                    if invalid_call is not None:
                        if (correction_used or required_proposal_id is not None or len(message.tool_calls) != 1
                                or getattr(invalid_call, "type", None) != "function"
                                or not isinstance(getattr(invalid_call, "id", None), str) or not invalid_call.id
                                or runtime.tool_dispatch_attempted or runtime.business is None):
                            raise ManagedExecutionError("TOOL_SETUP_FAILED")
                        gate = await runtime.operation(runtime.business.guard_result(allow_incomplete=True))
                        if gate is not None and gate.halt_status:
                            raise ManagedExecutionError(gate.error_code, processing_status=gate.halt_status)
                        if gate is not None or runtime.business.collector.snapshot():
                            raise ManagedExecutionError("TOOL_SETUP_FAILED")
                        preparation_correction = invalid_call.function.name
                        correction_used = True
                        tool_choice = {"type": "function", "function": {"name": preparation_correction}}
                        messages.append(self._assistant_message_to_dict(message, managed=True))
                        messages.append({"role": "tool", "tool_call_id": invalid_call.id,
                            "name": preparation_correction, "content": json.dumps(feedback, ensure_ascii=False)})
                        seen_tool_ids.add(invalid_call.id)
                        self._check_managed_envelope(agent, messages, openai_tools,
                                                     tool_choice=tool_choice, request=managed_request)
                        continue
                    seen_tool_ids.update(call.id for call in message.tool_calls
                        if isinstance(getattr(call, "id", None), str))
                messages.append(self._assistant_message_to_dict(message, managed=runtime is not None))
                if runtime:
                    self._check_managed_envelope(agent, messages, openai_tools,
                                                 tool_choice=tool_choice, request=managed_request)

                set_chat_failure_stage(ChatFailureStage.TOOL_EXECUTION)
                for tool_call in message.tool_calls:
                    func_name = tool_call.function.name
                    func_args_str = tool_call.function.arguments

                    if runtime:
                        # Hidden, disabled, custom and raw names stop before
                        # parsing and cannot reach a later scripted completion.
                        if func_name not in tool_registry or runtime.business is None:
                            raise ManagedExecutionError("TOOL_SETUP_FAILED")
                        func_args = parse_managed_tool_arguments(func_args_str)
                        from schemas.business import BusinessSafetyError
                        try:
                            business_result = await runtime.operation(
                                runtime.business.call_tool(func_name, func_args))
                        except BusinessSafetyError as exc:
                            raise ManagedExecutionError("TOOL_EXECUTION_UNKNOWN"
                                if exc.processing_status == "UNKNOWN" else "TOOL_SETUP_FAILED",
                                processing_status=exc.processing_status) from None
                        if business_result.halt_status:
                            raise ManagedExecutionError(business_result.error_code,
                                processing_status=business_result.halt_status)
                        required_proposal_id = None
                        preparation_correction = None
                        tool_choice = None
                        if business_result.safe_draft is not None:
                            # Proposals are bounded frozen public fields. Model
                            # prose is never permission or a completion receipt.
                            return business_result.safe_draft
                        _bounded_json(business_result.tool_payload)
                        tool_result_text = json.dumps(business_result.tool_payload,
                            ensure_ascii=False, allow_nan=False)
                        messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                         "name": func_name, "content": tool_result_text})
                        # Durable normalized replay/conflict owns business
                        # identity; the legacy raw-string loop guard does not.
                        continue

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
                            {"error": "Requested tool is unavailable." if runtime else f"Tool '{func_name}' is not available."},
                            ensure_ascii=False,
                        )
                    else:
                        try:
                            if runtime:
                                runtime.check()
                                runtime.tool_dispatch_attempted = True
                            pending_tool = manager.call_tool(func_name, func_args)
                            tool_result = await runtime.operation(pending_tool) if runtime else await pending_tool
                            if runtime:
                                inspect_managed_tool_result(manager.service_name, tool_result)
                            tool_result_text = self._extract_tool_result_content(tool_result)
                        except ManagedExecutionError:
                            raise
                        except Exception as exc:
                            if runtime:
                                raise ManagedExecutionError("TOOL_EXECUTION_UNKNOWN") from None
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

        if runtime:
            raise ManagedExecutionError("MODEL_ERROR")
        raise ChatProcessingError("Maximum number of LLM tool steps reached.")

    def _managed_model_extra_body(self, agent, *, request=None):
        model = agent.model_name or self.settings.llm_default_model
        if any(profile.matches(request, model) for profile in self.settings.managed_voice_latency_profiles):
            return {"reasoning": {"effort": "none"}, "provider": {"sort": "latency"}}
        if model in self.settings.managed_openrouter_disable_reasoning_models:
            return {"reasoning": {"enabled": False}}
        return {}

    def _managed_envelope(self, agent, messages, tools, *, tool_choice=None, request=None):
        envelope = {"model": agent.model_name or self.settings.llm_default_model,
                "temperature": agent.temperature, "max_tokens": self.settings.llm_max_tokens,
                "messages": messages, "tools": tools or None, "parallel_tool_calls": False}
        if tool_choice is not None:
            envelope["tool_choice"] = tool_choice
        envelope.update(self._managed_model_extra_body(agent, request=request))
        return envelope

    def _check_managed_envelope(self, agent, messages, tools, *, tool_choice=None, request=None):
        try:
            size = len(json.dumps(self._managed_envelope(agent, messages, tools, tool_choice=tool_choice, request=request),
                                  ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, UnicodeError, RecursionError, TypeError):
            raise ManagedExecutionError("INVALID_RESULT") from None
        if size > self.settings.managed_ai_context_bytes:
            raise ManagedExecutionError("CONTEXT_LIMIT")

    def _budget_managed_messages(self, agent, messages, tools, selected_indices, communication, simple, *, tool_choice=None, request=None):
        prefix = messages[:len(messages) - len(communication)]
        retained = list(range(len(communication)))
        selected = set(selected_indices)
        while True:
            current = list(prefix)
            if not simple:
                ordinals = [retained.index(index) + 1 for index in selected_indices]
                current.append({"role": "system", "content":
                    "The current Turn input is at communication positions " +
                    ", ".join(map(str, ordinals)) + ". Other communication messages are context."})
            current.extend(communication[index] for index in retained)
            try:
                self._check_managed_envelope(agent, current, tools, tool_choice=tool_choice, request=request)
                return current
            except ManagedExecutionError as exc:
                if exc.code != "CONTEXT_LIMIT":
                    raise
                removable = next((index for index in retained if index not in selected), None)
                if removable is None:
                    raise
                retained.remove(removable)
