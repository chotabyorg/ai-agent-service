"""Internal, independently authenticated AgentTurn execution; no delivery."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.dependencies import get_db
from core.config import get_settings
from core.managed_boundary import boundary_error
from schemas.managed_turn import ManagedTurnRequest, ManagedTurnResult
from services.managed_runtime import ManagedRuntime, join_owner
from services.managed_turn_service import ManagedAgentNotFound, execute_managed_owned

router = APIRouter()


def _request_schema():
    # Body is parsed by the ASGI boundary, so inline its small local definitions
    # rather than emitting JSON-schema root refs that do not resolve in OpenAPI.
    schema = ManagedTurnRequest.model_json_schema()
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
    owner = asyncio.create_task(execute_managed_owned(value, grant, runtime, provider),
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
