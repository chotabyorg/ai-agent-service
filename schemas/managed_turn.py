"""Versioned internal execution values; canonical storage belongs to Conversation."""

import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

MAX_REQUEST_BYTES = 1048576
MAX_RESULT_BYTES = 262144
MAX_MESSAGES = 256
MAX_PROJECTED_TEXT_BYTES = 524288
Channel = Literal["TELEGRAM", "INSTAGRAM", "WEBSITE_WIDGET", "WHATSAPP", "VOICE"]
Counter = Annotated[StrictInt, Field(ge=1, le=9223372036854775807)]
Revision = Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
ErrorCode = Literal[
    "AGENT_INACTIVE", "AGENT_KEY_MISSING", "AGENT_CONFIGURATION_ERROR", "CONTEXT_LIMIT",
    "MODEL_AUTH_FAILED", "MODEL_INSUFFICIENT_FUNDS", "MODEL_RATE_LIMITED", "MODEL_TIMEOUT",
    "MODEL_UNAVAILABLE", "MODEL_ERROR", "PRIVACY_UNAVAILABLE", "TOOL_SETUP_FAILED",
    "TOOL_EXECUTION_UNKNOWN", "TOOL_CLEANUP_FAILED", "EXECUTION_TIMEOUT", "INVALID_RESULT",
    "INTERNAL_ERROR",
]


def validate_owner(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 255 or not value.strip()
            or value == "*" or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)
            or len(value.encode("utf-8")) > 1020):
        raise ValueError("Invalid owner")
    return value


def validate_text(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 16384 or "\x00" in value
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
            or len(value.encode("utf-8")) > 32768):
        raise ValueError("Invalid text")
    return value


class ManagedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManagedMessage(ManagedValue):
    id: UUID
    sequence: Counter
    actor: Literal["CUSTOMER", "AGENT", "HUMAN", "SYSTEM"]
    direction: Literal["INBOUND", "OUTBOUND"]
    content_type: Literal["TEXT"]
    text: str

    _text = field_validator("text")(validate_text)


class ManagedTurnRequest(ManagedValue):
    contract_version: Annotated[StrictInt, Field(ge=1, le=2)] = 1
    conversation_id: UUID
    turn_id: UUID
    owner_id: str
    integration_id: UUID
    agent_id: UUID
    channel: Channel
    input_revision: Counter
    authority_revision: Revision
    input_first_sequence: Counter
    input_last_sequence: Counter
    context_last_sequence: Counter
    messages: Annotated[list[ManagedMessage], Field(min_length=1, max_length=MAX_MESSAGES)]
    selected_input_message_ids: Annotated[list[UUID], Field(min_length=1, max_length=MAX_MESSAGES)]

    _owner = field_validator("owner_id")(validate_owner)

    @property
    def window_first_sequence(self) -> int:
        return 1 if self.contract_version == 1 else self.messages[0].sequence

    def local_index(self, sequence: int) -> int:
        """Resolve an absolute canonical sequence inside this validated window."""
        if not self.window_first_sequence <= sequence <= self.context_last_sequence:
            raise ValueError("Sequence outside canonical window")
        return sequence - self.window_first_sequence

    @property
    def selected_sequence_range(self) -> range:
        return range(self.input_first_sequence, self.input_last_sequence + 1)

    @property
    def selected_message_slice(self) -> slice:
        return slice(self.local_index(self.input_first_sequence),
                     self.local_index(self.input_last_sequence) + 1)

    @property
    def selected_messages(self) -> list[ManagedMessage]:
        return self.messages[self.selected_message_slice]

    @property
    def selected_local_indices(self) -> list[int]:
        selection = self.selected_message_slice
        return list(range(selection.start, selection.stop))

    @property
    def simple_selection(self) -> bool:
        return (self.input_first_sequence == self.input_last_sequence == self.context_last_sequence
                and self.selected_messages[0].actor == "CUSTOMER")

    @model_validator(mode="after")
    def ordered_projection(self):
        first, last, bound = self.input_first_sequence, self.input_last_sequence, self.context_last_sequence
        if self.contract_version == 1:
            if not 1 <= first <= last <= bound or bound != len(self.messages):
                raise ValueError("Invalid frozen boundary")
            if [m.sequence for m in self.messages] != list(range(1, bound + 1)):
                raise ValueError("Invalid canonical ordering")
        else:
            window_first = self.window_first_sequence
            if not window_first <= first <= last <= bound or self.messages[-1].sequence != bound:
                raise ValueError("Invalid frozen boundary")
            if [m.sequence for m in self.messages] != list(range(window_first, window_first + len(self.messages))):
                raise ValueError("Invalid canonical ordering")
        ids = [m.id for m in self.messages]
        if len(set(ids)) != len(ids) or self.selected_input_message_ids != [m.id for m in self.selected_messages]:
            raise ValueError("Invalid selected input")
        if sum(len(m.text.encode("utf-8")) for m in self.messages) > MAX_PROJECTED_TEXT_BYTES:
            raise ValueError("Projection exceeds limit")
        if self.contract_version == 2 and len(self.model_dump_json().encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds limit")
        return self


class OperationReference(ManagedValue):
    origin: Literal["AI_AGENT_SERVICE"]
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    outcome: Literal["SUCCEEDED", "FAILED", "UNKNOWN"] | None


class ManagedTurnResult(ManagedValue):
    contract_version: Annotated[StrictInt, Field(ge=1, le=2)] = 1
    conversation_id: UUID
    turn_id: UUID
    agent_id: UUID
    input_revision: Counter
    authority_revision: Revision
    input_first_sequence: Counter
    input_last_sequence: Counter
    context_last_sequence: Counter
    status: Literal["DRAFT_READY", "NO_RESPONSE", "FAILED", "UNKNOWN"]
    draft_text: str | None
    error_code: ErrorCode | None
    operation_refs: Annotated[list[OperationReference], Field(max_length=32)] = Field(default_factory=list)

    @field_validator("draft_text")
    @classmethod
    def valid_draft(cls, value):
        return validate_text(value) if value is not None else None

    @model_validator(mode="after")
    def valid_outcome(self):
        if ((self.status == "DRAFT_READY") != (self.draft_text is not None)
                or (self.status in {"FAILED", "UNKNOWN"}) != (self.error_code is not None)):
            raise ValueError("Invalid result shape")
        keys = [(ref.origin, ref.operation_id) for ref in self.operation_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate operation reference")
        self.operation_refs.sort(key=lambda ref: (ref.origin, ref.operation_id))
        return self

    @classmethod
    def from_request(cls, request: ManagedTurnRequest, **outcome):
        echoes = request.model_dump(include={
            "contract_version", "conversation_id", "turn_id", "agent_id", "input_revision",
            "authority_revision", "input_first_sequence", "input_last_sequence", "context_last_sequence",
        })
        return cls(**echoes, **outcome)
