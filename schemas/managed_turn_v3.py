"""Typed v3 control and retained-evidence envelopes."""

from typing import Annotated, Literal, Union

from pydantic import Field, StrictInt, field_validator, model_validator

from schemas.managed_turn import (
    Counter,
    ErrorCode,
    ManagedTurnRequest,
    ManagedValue,
    OperationReference,
    Revision,
    MAX_REQUEST_BYTES,
    validate_owner,
    validate_text,
)
from schemas.strict_values import CanonicalUUID

HandoffReason = Literal[
    "CUSTOMER_REQUEST", "AGENT_LIMIT", "BUSINESS_UNCERTAIN",
    "POLICY_REQUIRES_HUMAN", "OTHER",
]
ContractVersion3 = Annotated[StrictInt, Field(ge=3, le=3)]


class AuthorityEventFact(ManagedValue):
    kind: Literal["AUTHORITY_EVENT"]
    event_id: CanonicalUUID
    authority_revision: Revision
    from_mode: Literal["AGENT", "HUMAN"]
    to_mode: Literal["AGENT", "HUMAN"]
    case_id: CanonicalUUID


class ManagedMessageV3(ManagedValue):
    id: CanonicalUUID
    sequence: Counter
    actor: Literal["CUSTOMER", "AGENT", "HUMAN", "SYSTEM"]
    direction: Literal["INBOUND", "OUTBOUND"]
    content_type: Literal["TEXT"]
    text: str

    _text = field_validator("text")(validate_text)


class DeliveryObservationFact(ManagedValue):
    kind: Literal["DELIVERY_OBSERVATION"]
    message_id: CanonicalUUID
    delivery_attempt_id: CanonicalUUID | None
    source_authority_revision: Revision
    status: Literal[
        "NOT_ACQUIRED", "GENERATED", "DISPATCH_REQUESTED", "PROVIDER_ACCEPTED",
        "DELIVERED", "READ", "FAILED", "SUPERSEDED", "UNKNOWN", "PARTIAL",
        "INTERRUPTED",
    ]

    @model_validator(mode="after")
    def attempt_matches_status(self):
        if (self.status == "NOT_ACQUIRED") != (self.delivery_attempt_id is None):
            raise ValueError("Delivery attempt identity does not match status")
        return self


CommunicationFact = Annotated[
    Union[AuthorityEventFact, DeliveryObservationFact], Field(discriminator="kind")
]


class PriorManagedOperationReference(ManagedValue):
    origin_turn_id: CanonicalUUID
    operation_ref: OperationReference


class ManagedTurnV3Request(ManagedTurnRequest):
    """Closed v3 snapshot. The v1/v2 request contract remains unchanged."""

    contract_version: ContractVersion3
    conversation_id: CanonicalUUID
    turn_id: CanonicalUUID
    integration_id: CanonicalUUID
    agent_id: CanonicalUUID
    messages: Annotated[list[ManagedMessageV3], Field(min_length=1, max_length=256)]
    selected_input_message_ids: Annotated[list[CanonicalUUID], Field(min_length=1, max_length=256)]
    communication_facts: Annotated[list[CommunicationFact], Field(max_length=64)]
    prior_operation_refs: Annotated[list[PriorManagedOperationReference], Field(max_length=32)]

    @model_validator(mode="after")
    def validate_v3_snapshot(self):
        if len(self.model_dump_json().encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds limit")
        keys = [
            (item.origin_turn_id, item.operation_ref.origin, item.operation_ref.operation_id)
            for item in self.prior_operation_refs
        ]
        if len(keys) != len(set(keys)) or keys != sorted(keys, key=lambda x: (str(x[0]), x[1], x[2])):
            raise ValueError("Prior operation references must be unique and sorted")
        projected = {
            message.id for message in self.messages
            if message.actor in {"HUMAN", "SYSTEM"} and message.direction == "OUTBOUND"
        }
        observations = [
            fact.message_id for fact in self.communication_facts
            if isinstance(fact, DeliveryObservationFact)
        ]
        if len(observations) != len(set(observations)):
            raise ValueError("Only the latest delivery observation may be projected per message")
        if not projected.issubset(set(observations)):
            raise ValueError("Every HUMAN/SYSTEM outbound message requires its latest delivery fact")
        return self


class HandoffControlRequest(ManagedValue):
    type: Literal["HUMAN_HANDOFF"]
    request_id: CanonicalUUID
    reason: HandoffReason
    note: str | None

    @field_validator("note")
    @classmethod
    def bounded_note(cls, value):
        if value is None:
            return None
        if (not isinstance(value, str) or not 1 <= len(value) <= 1000
                or len(value.encode("utf-8")) > 4000 or "\x00" in value
                or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
            raise ValueError("Invalid handoff note")
        return value


class ManagedTurnV3Result(ManagedValue):
    contract_version: ContractVersion3
    result_kind: Literal["EXECUTION_RESULT", "PLATFORM_CONTROL_REQUEST"]
    original_dispatch_id: CanonicalUUID
    request_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    conversation_id: CanonicalUUID
    turn_id: CanonicalUUID
    agent_id: CanonicalUUID
    input_revision: Counter
    authority_revision: Revision
    input_first_sequence: Counter
    input_last_sequence: Counter
    context_last_sequence: Counter
    processing_outcome: Literal["DRAFT_READY", "NO_RESPONSE", "FAILED", "UNKNOWN"]
    draft_text: str | None
    error_code: ErrorCode | None
    operation_refs: Annotated[list[OperationReference], Field(max_length=32)]
    control_request: HandoffControlRequest | None

    @field_validator("draft_text")
    @classmethod
    def valid_draft(cls, value):
        return validate_text(value) if value is not None else None

    @model_validator(mode="after")
    def valid_result_shape(self):
        is_control = self.result_kind == "PLATFORM_CONTROL_REQUEST"
        if is_control != (self.control_request is not None):
            raise ValueError("Control result shape mismatch")
        if is_control:
            if self.processing_outcome not in {"NO_RESPONSE", "FAILED", "UNKNOWN"} or self.draft_text is not None:
                raise ValueError("Invalid platform control outcome")
        elif (self.processing_outcome == "DRAFT_READY") != (self.draft_text is not None):
            raise ValueError("Invalid execution draft")
        if ((self.processing_outcome in {"FAILED", "UNKNOWN"}) != (self.error_code is not None)
                or (self.processing_outcome == "NO_RESPONSE" and self.error_code is not None)
                or (self.processing_outcome == "DRAFT_READY" and self.error_code is not None)):
            raise ValueError("Invalid processing error shape")
        keys = [(ref.origin, ref.operation_id) for ref in self.operation_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate operation reference")
        self.operation_refs.sort(key=lambda ref: (ref.origin, ref.operation_id))
        return self

    @classmethod
    def from_request(cls, request, *, original_dispatch_id, request_fingerprint, **values):
        echoes = request.model_dump(include={
            "conversation_id", "turn_id", "agent_id", "input_revision", "authority_revision",
            "input_first_sequence", "input_last_sequence", "context_last_sequence",
        })
        return cls(contract_version=3, original_dispatch_id=original_dispatch_id,
                   request_fingerprint=request_fingerprint, **echoes, **values)


class ControlEvidenceRequest(ManagedValue):
    contract_version: ContractVersion3
    owner_id: str
    integration_id: CanonicalUUID
    channel: Literal["TELEGRAM"]
    agent_id: CanonicalUUID
    conversation_id: CanonicalUUID
    turn_id: CanonicalUUID
    input_revision: Counter
    authority_revision: Revision
    input_first_sequence: Counter
    input_last_sequence: Counter
    context_last_sequence: Counter
    request_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    original_dispatch_id: CanonicalUUID

    _owner = field_validator("owner_id")(validate_owner)


class ControlEvidenceResult(ManagedValue):
    contract_version: ContractVersion3 = 3
    evidence_kind: Literal["CONTROL_REQUEST", "SEALED_NO_CONTROL", "UNSEALED", "UNAVAILABLE"]
    conversation_id: CanonicalUUID
    turn_id: CanonicalUUID
    agent_id: CanonicalUUID
    input_revision: Counter
    authority_revision: Revision
    input_first_sequence: Counter
    input_last_sequence: Counter
    context_last_sequence: Counter
    original_dispatch_id: CanonicalUUID
    request_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    execution_state: Literal["RUNNING", "SEALED"] | None
    processing_outcome: Literal["DRAFT_READY", "NO_RESPONSE", "FAILED", "UNKNOWN"] | None
    error_code: ErrorCode | None
    control_request: HandoffControlRequest | None
    operation_refs: Annotated[list[OperationReference], Field(max_length=32)]

    @model_validator(mode="after")
    def evidence_truth(self):
        if self.evidence_kind == "CONTROL_REQUEST" and self.control_request is None:
            raise ValueError("Control evidence requires a control request")
        if self.evidence_kind == "SEALED_NO_CONTROL" and (
                self.execution_state != "SEALED" or self.control_request is not None
                or self.processing_outcome is None):
            raise ValueError("Invalid sealed no-control evidence")
        if self.evidence_kind in {"UNSEALED", "UNAVAILABLE"} and (
                self.control_request is not None or self.execution_state is not None
                or self.processing_outcome is not None or self.error_code is not None
                or self.operation_refs):
            raise ValueError("Unproven evidence cannot assert an outcome")
        keys = [(ref.origin, ref.operation_id) for ref in self.operation_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate operation reference")
        self.operation_refs.sort(key=lambda ref: (ref.origin, ref.operation_id))
        return self

    @classmethod
    def for_request(cls, request, *, evidence_kind, execution_state=None,
                    processing_outcome=None, error_code=None, control_request=None,
                    operation_refs=None):
        echoes = request.model_dump(include={
            "conversation_id", "turn_id", "agent_id", "input_revision", "authority_revision",
            "input_first_sequence", "input_last_sequence", "context_last_sequence",
            "original_dispatch_id", "request_fingerprint",
        })
        return cls(evidence_kind=evidence_kind, execution_state=execution_state,
                   processing_outcome=processing_outcome, error_code=error_code,
                   control_request=control_request, operation_refs=operation_refs or [], **echoes)

