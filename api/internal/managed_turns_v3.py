"""Durable v3 execution and read-only control evidence boundary."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.dependencies import get_db
from api.internal.managed_turns import (
    _business_dependency,
    _request_schema,
    get_business_registry,
    get_business_session_factory,
)
from core.config import get_settings
from core.managed_boundary import boundary_error
from schemas.managed_turn_v3 import (
    ControlEvidenceRequest,
    ControlEvidenceResult,
    ManagedTurnV3Request,
    ManagedTurnV3Result,
)
from services.managed_execution_receipts import (
    ManagedExecutionReceiptService,
    ReceiptConflict,
    managed_v3_dispatch_id,
    managed_v3_request_fingerprint,
)
from services.managed_runtime import ManagedRuntime, join_owner
from services.managed_turn_service import ManagedAgentNotFound, execute_managed_owned

router = APIRouter()


def _enrolled(value) -> bool:
    identity = (value.owner_id, value.integration_id, value.channel, value.agent_id)
    return any(scope.key() == identity for scope in get_settings().handoff_scopes)


@router.post("/managed-turns/execute", response_model=ManagedTurnV3Result,
             tags=["internal-managed-turns"],
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/json": {"schema": _request_schema(ManagedTurnV3Request)}}}})
async def execute_managed_turn_v3(request: Request):
    value = request.scope["managed_turn_v3_request"]
    principal = request.scope["managed_principal"]
    grant = next((item for item in principal.grants if item.scope() == (
        value.owner_id, value.integration_id, value.channel, value.agent_id,
    )), None)
    if grant is None or not _enrolled(value):
        return boundary_error(404, "AGENT_NOT_FOUND")

    fingerprint = managed_v3_request_fingerprint(value)
    dispatch_id = managed_v3_dispatch_id(value, fingerprint)
    factory = _business_dependency(request, get_business_session_factory)
    registry = _business_dependency(request, get_business_registry)
    receipts = ManagedExecutionReceiptService(factory)
    try:
        claim = await receipts.claim(value, fingerprint, dispatch_id)
    except ReceiptConflict:
        return boundary_error(409, "EXECUTION_CONFLICT")
    except Exception:
        return boundary_error(503, "EVIDENCE_UNAVAILABLE")

    receipt = claim.receipt
    if receipt.result_payload is not None and (
            receipt.execution_state == "SEALED" or receipt.handoff_control is not None):
        return JSONResponse(receipt.result_payload, headers={"Cache-Control": "no-store"})
    if not claim.created:
        # RUNNING without a committed control is not permission to invoke the
        # model a second time. The matching evidence route remains UNSEALED.
        progress = ManagedTurnV3Result.from_request(
            value, original_dispatch_id=dispatch_id, request_fingerprint=fingerprint,
            result_kind="EXECUTION_RESULT", processing_outcome="UNKNOWN", draft_text=None,
            error_code="EXECUTION_TIMEOUT", operation_refs=[], control_request=None,
        )
        return JSONResponse(progress.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    async def latch_control(control, operation_refs):
        await receipts.latch_control(value, control, fingerprint, dispatch_id, operation_refs)

    runtime = ManagedRuntime(
        get_settings().managed_ai_execution_seconds,
        request=value,
        control_latch=latch_control,
        request_fingerprint=fingerprint,
        original_dispatch_id=dispatch_id,
    )
    provider = request.app.dependency_overrides.get(get_db, get_db)
    owner = asyncio.create_task(execute_managed_owned(value, grant, runtime, provider,
                                                     factory, registry),
                                name="managed-turn-v3-owner")

    async def watch_disconnect():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                runtime.signal_cancel()
                return

    disconnect = asyncio.create_task(watch_disconnect(), name="managed-turn-v3-disconnect")
    try:
        result = await join_owner(owner, runtime)
        result = await receipts.seal(value, fingerprint, dispatch_id, result)
    except ManagedAgentNotFound:
        return boundary_error(404, "AGENT_NOT_FOUND")
    except ReceiptConflict:
        return boundary_error(409, "EXECUTION_CONFLICT")
    except Exception:
        return boundary_error(503, "EVIDENCE_UNAVAILABLE")
    finally:
        disconnect.cancel()
        try:
            await disconnect
        except asyncio.CancelledError:
            pass
    return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/managed-turns/control-evidence", response_model=ControlEvidenceResult,
             tags=["internal-managed-turns"],
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/json": {"schema": _request_schema(ControlEvidenceRequest)}}}})
async def managed_turn_control_evidence_v3(request: Request):
    value = request.scope["control_evidence_v3_request"]
    principal = request.scope["managed_principal"]
    grant = next((item for item in principal.grants if item.scope() == (
        value.owner_id, value.integration_id, value.channel, value.agent_id,
    )), None)
    if grant is None or not _enrolled(value):
        return boundary_error(404, "AGENT_NOT_FOUND")
    factory = _business_dependency(request, get_business_session_factory)
    try:
        result = await ManagedExecutionReceiptService(factory).evidence(value)
    except ReceiptConflict:
        return boundary_error(404, "AGENT_NOT_FOUND")
    if result.evidence_kind == "UNAVAILABLE":
        return boundary_error(503, "EVIDENCE_UNAVAILABLE")
    return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
