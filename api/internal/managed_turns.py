"""Internal, independently authenticated AgentTurn execution; no delivery."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.dependencies import get_db
from core.config import get_settings
from core.managed_boundary import boundary_error
from schemas.managed_turn import ManagedTurnRequest, ManagedTurnResult
from schemas.managed_evidence import ManagedEvidenceRequest, ManagedEvidenceResult
from services.managed_runtime import ManagedRuntime, join_owner
from services.managed_turn_service import ManagedAgentNotFound, execute_managed_owned
from schemas.voice_warmup import VoiceWarmupRequest, VoiceWarmupResult
from services.voice_warmup import WarmupLimiter, warm_owned

router = APIRouter()
warmup_limiter = WarmupLimiter()


@router.post("/voice/warmup", response_model=VoiceWarmupResult,
             tags=["internal-managed-turns"],
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/json": {"schema": VoiceWarmupRequest.model_json_schema()}}}})
async def warm_voice(request: Request):
    runtime = ManagedRuntime(2.5)
    provider = request.app.dependency_overrides.get(get_db, get_db)
    owner = asyncio.create_task(warm_owned(
        request.scope["voice_warmup_request"], request.scope["managed_grant"], runtime,
        provider, _business_dependency(request, get_business_registry), warmup_limiter),
        name="voice-warmup-owner")

    async def watch_disconnect():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                runtime.signal_cancel()
                return

    disconnect = asyncio.create_task(watch_disconnect(), name="voice-warmup-disconnect")
    try:
        result = await join_owner(owner, runtime)
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
    finally:
        disconnect.cancel()
        await asyncio.gather(disconnect, return_exceptions=True)


def _request_schema(model=ManagedTurnRequest):
    # Body is parsed by the ASGI boundary, so inline its small local definitions
    # rather than emitting JSON-schema root refs that do not resolve in OpenAPI.
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(item) for key, item in value.items()}
        if isinstance(value, list):
            return [inline(item) for item in value]
        return value

    return inline(schema)


def get_business_session_factory(request: Request):
    """Independent short transactions, explicitly on the agent database."""
    from db import database

    factory = getattr(request.app.state, "business_session_factory", None)
    if factory is None:
        if get_db in request.app.dependency_overrides:
            # A test/custom DB dependency must never fall back to runtime DB.
            raise RuntimeError("Explicit managed business database required")
        factory = database.AsyncSessionLocal
    if not callable(factory):
        raise RuntimeError("Invalid managed business database")
    return factory


def get_business_registry(request: Request):
    from services.business_registry import BusinessRegistry, EMPTY_BUSINESS_REGISTRY

    registry = getattr(request.app.state, "business_registry", EMPTY_BUSINESS_REGISTRY)
    if not isinstance(registry, BusinessRegistry):
        raise RuntimeError("Code-owned managed business registry required")
    return registry


def _business_dependency(request, dependency):
    # These are synchronous code-owned dependencies. No body/provider value can
    # choose a factory, adapter catalog or transport destination.
    return request.app.dependency_overrides.get(dependency, dependency)(request)


@router.post("/managed-turns/execute", response_model=ManagedTurnResult,
             tags=["internal-managed-turns"],
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/json": {"schema": _request_schema()}}}})
async def execute_managed_turn(request: Request):
    value = request.scope["managed_turn_request"]
    principal = request.scope["managed_principal"]
    grant = next((grant for grant in principal.grants if grant.scope() == (
        value.owner_id, value.integration_id, value.channel, value.agent_id,
    )), None)
    if grant is None:
        return boundary_error(404, "AGENT_NOT_FOUND")
    runtime = ManagedRuntime(get_settings().managed_ai_execution_seconds, request=value)
    provider = request.app.dependency_overrides.get(get_db, get_db)
    factory = _business_dependency(request, get_business_session_factory)
    registry = _business_dependency(request, get_business_registry)
    owner = asyncio.create_task(execute_managed_owned(value, grant, runtime, provider,
                                                     factory, registry),
                                name="managed-turn-owner")

    async def watch_disconnect():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                runtime.signal_cancel()
                return

    disconnect = asyncio.create_task(watch_disconnect(), name="managed-turn-disconnect")
    try:
        result = await join_owner(owner, runtime)
    except ManagedAgentNotFound:
        return boundary_error(404, "AGENT_NOT_FOUND")
    finally:
        disconnect.cancel()
        try:
            await disconnect
        except asyncio.CancelledError:
            pass
    return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/managed-turns/evidence", response_model=ManagedEvidenceResult,
             tags=["internal-managed-turns"],
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/json": {"schema": _request_schema(ManagedEvidenceRequest)}}}})
async def managed_turn_evidence(request: Request):
    """Read retained scoped refs only; never open execution/recovery/provider."""
    from services.business_repository import BusinessRepository

    value = request.scope["managed_evidence_request"]
    grant = request.scope["managed_grant"]
    refs = await BusinessRepository.read_evidence(
        value, grant, _business_dependency(request, get_business_session_factory))
    if refs is None:
        return boundary_error(404, "AGENT_NOT_FOUND")
    result = ManagedEvidenceResult(conversation_id=value.conversation_id,
        turn_id=value.turn_id, agent_id=value.agent_id, operation_refs=refs)
    return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
