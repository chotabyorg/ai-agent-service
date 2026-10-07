"""Pure ASGI path boundary authenticates before receiving or parsing any body."""

import asyncio
import hmac
import json
import re

from starlette.responses import JSONResponse

from core.config import get_settings
from core.managed_security import install_managed_log_boundary, managed_logging, valid_token
from schemas.managed_turn import MAX_REQUEST_BYTES, ManagedTurnRequest
from schemas.managed_evidence import MAX_EVIDENCE_REQUEST_BYTES, ManagedEvidenceRequest

MANAGED_PATH = "/internal/v1/managed-turns/execute"
EVIDENCE_PATH = "/internal/v1/managed-turns/evidence"
MANAGED_PATHS = {
    MANAGED_PATH: (ManagedTurnRequest, MAX_REQUEST_BYTES, "managed_turn_request"),
    EVIDENCE_PATH: (ManagedEvidenceRequest, MAX_EVIDENCE_REQUEST_BYTES, "managed_evidence_request"),
}
BOUNDARY_MESSAGES = {
    "UNAUTHENTICATED": "Managed authentication required.",
    "AGENT_NOT_FOUND": "Agent not found.",
    "PAYLOAD_TOO_LARGE": "Managed request exceeds limit.",
    "UNSUPPORTED_MEDIA_TYPE": "Managed request requires uncompressed JSON.",
    "INVALID_CONTEXT": "Invalid managed context.",
    "INTERNAL_ERROR": "Managed execution failed.",
}


def boundary_error(status, code):
    return JSONResponse({"detail": {"code": code, "message": BOUNDARY_MESSAGES[code]}},
                        status_code=status, headers={"Cache-Control": "no-store"})


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


class ManagedTurnBoundary:
    def __init__(self, app):
        self.app = app
        install_managed_log_boundary()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") not in MANAGED_PATHS:
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        schema, body_cap, scope_key = MANAGED_PATHS[path]
        token = managed_logging.set(True)
        try:
            headers = scope.get("headers", [])
            tokens = [value for key, value in headers if key.lower() == b"x-service-token"]
            supplied = tokens[0] if len(tokens) == 1 else b""
            try:
                eligible = len(tokens) == 1 and valid_token(supplied.decode("ascii"))
            except UnicodeError:
                eligible = False
            principal = None
            # Compare every token, even after a match, without logging the registry.
            for candidate in get_settings().managed_principals:
                matched = hmac.compare_digest(supplied, candidate.token.get_secret_value().encode("ascii"))
                if eligible and matched:
                    principal = candidate
            if principal is None:
                return await boundary_error(401, "UNAUTHENTICATED")(scope, receive, send)
            # Evidence has one exact route. Neither aliases nor other methods
            # can fall through to another parser or a redirect with a new cap.
            if path == EVIDENCE_PATH and scope["path"] != EVIDENCE_PATH:
                return await boundary_error(404, "AGENT_NOT_FOUND")(scope, receive, send)
            if path == EVIDENCE_PATH and scope.get("method") != "POST":
                response = boundary_error(405, "INVALID_CONTEXT")
                response.headers["Allow"] = "POST"
                return await response(scope, receive, send)
            media = [value for key, value in headers if key.lower() == b"content-type"]
            encodings = [value for key, value in headers if key.lower() == b"content-encoding"]
            if (len(media) != 1 or media[0].split(b";", 1)[0].strip().lower() != b"application/json"
                    or len(encodings) > 1
                    or any(value.lower().strip() != b"identity" for value in encodings)):
                return await boundary_error(415, "UNSUPPORTED_MEDIA_TYPE")(scope, receive, send)
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    raise asyncio.CancelledError
                chunk = event.get("body", b"")
                if len(body) + len(chunk) > body_cap:
                    return await boundary_error(413, "PAYLOAD_TOO_LARGE")(scope, receive, send)
                body.extend(chunk)
                if not event.get("more_body", False):
                    break
            try:
                raw = json.loads(body.decode("utf-8") if path == EVIDENCE_PATH else body,
                                 object_pairs_hook=_pairs,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                value = schema.model_validate(raw)
            except Exception:
                return await boundary_error(422, "INVALID_CONTEXT")(scope, receive, send)
            scope["managed_principal"] = principal
            scope[scope_key] = value
            grant = next((grant for grant in principal.grants if grant.scope() == (
                value.owner_id, value.integration_id, value.channel, value.agent_id,
            )), None)
            if grant is None:
                return await boundary_error(404, "AGENT_NOT_FOUND")(scope, receive, send)
            scope["managed_grant"] = grant
            started = False

            async def safe_send(event):
                nonlocal started
                if event["type"] == "http.response.start":
                    started = True
                    event["headers"] = [(key, value) for key, value in event.get("headers", [])
                                        if key.lower() != b"cache-control"]
                    event["headers"].append((b"cache-control", b"no-store"))
                    request_ids = [v for k, v in headers if k.lower() == b"x-request-id"]
                    if len(request_ids) == 1 and re.fullmatch(rb"[A-Za-z0-9._:-]{1,64}", request_ids[0]):
                        event["headers"].append((b"x-request-id", request_ids[0]))
                await send(event)

            try:
                await self.app(scope, receive, safe_send)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not started:
                    await boundary_error(500, "INTERNAL_ERROR")(scope, receive, send)
        finally:
            managed_logging.reset(token)
