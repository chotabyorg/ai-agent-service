"""Durable one-owner execution receipts for managed v3 calls."""

import hashlib
import json
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from models.managed_execution import ManagedExecutionReceipt
from schemas.managed_turn_v3 import (
    ControlEvidenceRequest,
    ControlEvidenceResult,
    HandoffControlRequest,
    ManagedTurnV3Result,
)
from schemas.conversation_effects import managed_request_fingerprint

URL_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
HANDOFF_TOOL_NAME = "platform_request_human_handoff"


class ReceiptConflict(Exception):
    pass


class ReceiptClaim:
    def __init__(self, receipt, *, created):
        self.receipt = receipt
        self.created = created


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def managed_v3_request_fingerprint(request) -> str:
    return hashlib.sha256(_canonical(request.model_dump(mode="json"))).hexdigest()


def managed_v3_dispatch_id(request, request_fingerprint: str) -> UUID:
    name = _canonical([
        "main-chota.managed-execution.v3", request.owner_id, str(request.integration_id),
        request.channel, str(request.conversation_id), str(request.turn_id), request_fingerprint,
    ]).decode("utf-8")
    return uuid5(URL_NAMESPACE, name)


def handoff_request_id(request) -> UUID:
    name = _canonical([
        "main-chota.handoff.v1", request.owner_id, str(request.integration_id),
        request.channel, str(request.conversation_id), str(request.turn_id), 0,
    ]).decode("utf-8")
    return uuid5(URL_NAMESPACE, name)


def control_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": HANDOFF_TOOL_NAME,
            "description": "Request a human to take over this customer conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "enum": [
                        "CUSTOMER_REQUEST", "AGENT_LIMIT", "BUSINESS_UNCERTAIN",
                        "POLICY_REQUIRES_HUMAN", "OTHER",
                    ]},
                    "note": {"type": ["string", "null"], "minLength": 1, "maxLength": 1000},
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    }


def parse_control_arguments(raw: str) -> tuple[str, str | None]:
    from schemas.managed_turn_v3 import HandoffReason, HandoffControlRequest
    from pydantic import TypeAdapter

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or set(value) - {"reason", "note"} or "reason" not in value:
            raise ValueError
        reason = TypeAdapter(HandoffReason).validate_python(value["reason"])
        note = HandoffControlRequest(type="HUMAN_HANDOFF", request_id=UUID(int=0),
                                     reason=reason, note=value.get("note")).note
        return reason, note
    except Exception:
        raise ValueError("Invalid Handoff tool arguments") from None


class ManagedExecutionReceiptService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _identity(request):
        return dict(owner_id=request.owner_id, integration_id=request.integration_id,
                    channel=request.channel, agent_id=request.agent_id,
                    conversation_id=request.conversation_id, turn_id=request.turn_id)

    async def claim(self, request, request_fingerprint: str, original_dispatch_id: UUID):
        payload = request.model_dump(mode="json")
        sidecar = managed_request_fingerprint(request)
        identity = self._identity(request)
        values = dict(
            **identity,
            input_revision=request.input_revision,
            authority_revision=request.authority_revision,
            input_first_sequence=request.input_first_sequence,
            input_last_sequence=request.input_last_sequence,
            context_last_sequence=request.context_last_sequence,
            handoff_slot=0,
            original_dispatch_id=original_dispatch_id,
            request_fingerprint=request_fingerprint,
            managed_request_fingerprint=sidecar,
            request_payload=payload,
            execution_state="RUNNING",
        )
        async with self.session_factory() as session:
            inserted = await session.execute(
                insert(ManagedExecutionReceipt).values(**values)
                .on_conflict_do_nothing(index_elements=[
                    ManagedExecutionReceipt.owner_id, ManagedExecutionReceipt.integration_id,
                    ManagedExecutionReceipt.channel, ManagedExecutionReceipt.agent_id,
                    ManagedExecutionReceipt.conversation_id, ManagedExecutionReceipt.turn_id,
                ])
                .returning(ManagedExecutionReceipt.id)
            )
            created_id = inserted.scalar_one_or_none()
            await session.commit()
        async with self.session_factory() as session:
            statement = select(ManagedExecutionReceipt).where(
                *[getattr(ManagedExecutionReceipt, key) == value for key, value in identity.items()]
            ).with_for_update()
            receipt = await session.scalar(statement)
            if receipt is None:
                raise ReceiptConflict
            if (receipt.request_fingerprint != request_fingerprint
                    or receipt.original_dispatch_id != original_dispatch_id
                    or receipt.managed_request_fingerprint != sidecar
                    or receipt.request_payload != payload):
                raise ReceiptConflict
            # The unique dispatch UUID is an independent collision guard.
            await session.commit()
            return ReceiptClaim(receipt, created=created_id is not None)

    async def latch_control(self, request, control: HandoffControlRequest,
                            request_fingerprint: str, original_dispatch_id: UUID,
                            operation_refs: list) -> ManagedTurnV3Result:
        provisional = ManagedTurnV3Result.from_request(
            request, original_dispatch_id=original_dispatch_id,
            request_fingerprint=request_fingerprint,
            result_kind="PLATFORM_CONTROL_REQUEST", processing_outcome="UNKNOWN",
            draft_text=None, error_code="EXECUTION_TIMEOUT", operation_refs=operation_refs,
            control_request=control,
        ).model_dump(mode="json")
        async with self.session_factory() as session:
            row = await session.scalar(select(ManagedExecutionReceipt).where(
                *[getattr(ManagedExecutionReceipt, key) == value
                  for key, value in self._identity(request).items()]
            ).with_for_update())
            if (row is None or row.request_fingerprint != request_fingerprint
                    or row.original_dispatch_id != original_dispatch_id):
                raise ReceiptConflict
            if row.handoff_control is not None:
                if row.handoff_control != control.model_dump(mode="json"):
                    raise ReceiptConflict
                if row.result_payload is not None:
                    return ManagedTurnV3Result.model_validate(row.result_payload)
            row.handoff_control = control.model_dump(mode="json")
            row.result_payload = provisional
            await session.commit()
        return ManagedTurnV3Result.model_validate(provisional)

    async def seal(self, request, request_fingerprint: str, original_dispatch_id: UUID,
                   result: ManagedTurnV3Result) -> ManagedTurnV3Result:
        payload = result.model_dump(mode="json")
        async with self.session_factory() as session:
            row = await session.scalar(select(ManagedExecutionReceipt).where(
                *[getattr(ManagedExecutionReceipt, key) == value
                  for key, value in self._identity(request).items()]
            ).with_for_update())
            if (row is None or row.request_fingerprint != request_fingerprint
                    or row.original_dispatch_id != original_dispatch_id):
                raise ReceiptConflict
            if row.handoff_control is not None and (
                    result.result_kind != "PLATFORM_CONTROL_REQUEST"
                    or result.control_request.model_dump(mode="json") != row.handoff_control):
                raise ReceiptConflict
            if row.execution_state == "SEALED" and row.result_payload != payload:
                raise ReceiptConflict
            row.result_payload = payload
            row.execution_state = "SEALED"
            await session.commit()
        return result

    async def evidence(self, request: ControlEvidenceRequest) -> ControlEvidenceResult:
        identity = dict(
            owner_id=request.owner_id, integration_id=request.integration_id,
            channel=request.channel, agent_id=request.agent_id,
            conversation_id=request.conversation_id, turn_id=request.turn_id,
        )
        try:
            async with self.session_factory() as session:
                row = await session.scalar(select(ManagedExecutionReceipt).where(
                    *[getattr(ManagedExecutionReceipt, key) == value for key, value in identity.items()]
                ))
        except Exception:
            return ControlEvidenceResult.for_request(request, evidence_kind="UNAVAILABLE")
        if row is None:
            return ControlEvidenceResult.for_request(request, evidence_kind="UNSEALED")
        if (row.request_fingerprint != request.request_fingerprint
                or row.original_dispatch_id != request.original_dispatch_id
                or row.input_revision != request.input_revision
                or row.authority_revision != request.authority_revision
                or row.input_first_sequence != request.input_first_sequence
                or row.input_last_sequence != request.input_last_sequence
                or row.context_last_sequence != request.context_last_sequence):
            raise ReceiptConflict
        if row.handoff_control is not None:
            result = ManagedTurnV3Result.model_validate(row.result_payload) if row.result_payload else None
            if result is None:
                return ControlEvidenceResult.for_request(
                    request, evidence_kind="UNAVAILABLE")
            return ControlEvidenceResult.for_request(
                request, evidence_kind="CONTROL_REQUEST", execution_state=row.execution_state,
                processing_outcome=result.processing_outcome, error_code=result.error_code,
                control_request=result.control_request, operation_refs=result.operation_refs,
            )
        if row.execution_state == "SEALED" and row.result_payload is not None:
            result = ManagedTurnV3Result.model_validate(row.result_payload)
            return ControlEvidenceResult.for_request(
                request, evidence_kind="SEALED_NO_CONTROL", execution_state="SEALED",
                processing_outcome=result.processing_outcome, error_code=result.error_code,
                operation_refs=result.operation_refs,
            )
        return ControlEvidenceResult.for_request(request, evidence_kind="UNSEALED")
