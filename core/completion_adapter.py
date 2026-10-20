"""Explicit capabilities for the existing OpenAI-compatible completion port."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class CompletionAdapterProfile(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: Literal['openrouter', 'openai_compatible']
    streaming: bool = True
    tools: bool = True
    named_tool_choice: bool = True
    json_object: bool = False
    json_schema: bool = False
    reasoning: bool = False
    cancellation: bool = True
    # A declaration, never an instruction to fabricate absent usage or prices.
    reports_usage: bool = True

    @model_validator(mode='after')
    def managed_requirements(self):
        if not self.tools or not self.cancellation:
            raise ValueError('Managed completions require tools and cancellation')
        return self

    def validate_request(self, request):
        from services.managed_tools import ManagedExecutionError
        unsupported = (
            request.get('stream') and not self.streaming
            or request.get('tools') and not self.tools
            or isinstance(request.get('tool_choice'), dict) and not self.named_tool_choice
            or (request.get('response_format') or {}).get('type') == 'json_object' and not self.json_object
            or (request.get('response_format') or {}).get('type') == 'json_schema' and not self.json_schema
            or bool((request.get('extra_body') or {}).get('reasoning')) and not self.reasoning
            or self.kind != 'openrouter' and bool(request.get('extra_body'))
        )
        if unsupported:
            raise ManagedExecutionError('TOOL_SETUP_FAILED')
