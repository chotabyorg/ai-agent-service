import json
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


def build_tool_call(tool_id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=tool_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def build_llm_response(
    *,
    content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls,
                )
            )
        ]
    )


def build_tool_result(*texts: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text) for text in texts],
    )


class ScriptedCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: deque[Any] = deque()

    def queue(self, *responses: Any) -> None:
        self._responses.extend(responses)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No scripted LLM response available for this call.")

        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


class ScriptedLLMClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=ScriptedCompletions())


@dataclass
class FakeMCPController:
    tools_by_service: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tool_results: dict[str, Any] = field(default_factory=dict)
    enter_errors: dict[str, Exception] = field(default_factory=dict)
    call_errors: dict[str, Exception] = field(default_factory=dict)
    init_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def make_manager_class(self):
        controller = self

        class FakeMCPManager:
            def __init__(self, service_name: str, credentials: dict[str, Any]) -> None:
                self.service_name = service_name.lower()
                self.credentials = credentials
                controller.init_calls.append(
                    {
                        "service_name": self.service_name,
                        "credentials": credentials,
                    }
                )

            async def __aenter__(self):
                error = controller.enter_errors.get(self.service_name)
                if error is not None:
                    raise error
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

            def get_openai_tools(self) -> list[dict[str, Any]]:
                return controller.tools_by_service.get(self.service_name, [])

            async def call_tool(self, tool_name: str, tool_args: dict[str, Any] | None = None) -> Any:
                tool_args = tool_args or {}
                controller.tool_calls.append(
                    {
                        "service_name": self.service_name,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    }
                )
                error = controller.call_errors.get(tool_name)
                if error is not None:
                    raise error
                return controller.tool_results.get(tool_name, build_tool_result(json.dumps({})))

        return FakeMCPManager
