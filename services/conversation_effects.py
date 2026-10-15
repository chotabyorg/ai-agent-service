"""Fail-closed HTTP boundary for Conversation's opaque business-effect authority."""

from __future__ import annotations

import json
from typing import Protocol

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from schemas.conversation_effects import (
    EffectReason,
    ExternalEffectEligibilityRequest,
    ExternalEffectEligibilityResult,
    ExternalEffectPermitRequest,
    ExternalEffectPermitResult,
    sidecar_fingerprint,
)

MAX_RESPONSE_BYTES = 8192
BUSINESS_EFFECT_NAMESPACE = "main-chota.ai-business.v1"


class EffectPortError(Exception):
    """Closed sidecar failure; response content and credentials never escape."""


class DefinitiveEffectDenial(EffectPortError):
    def __init__(self, reason: EffectReason):
        self.reason = reason
        super().__init__("EXTERNAL_EFFECT_DENIED")


class EffectPortUnavailable(EffectPortError):
    pass


class ConversationEffectPort(Protocol):
    async def eligibility(self, request: ExternalEffectEligibilityRequest) -> ExternalEffectEligibilityResult: ...

    async def permit(self, request: ExternalEffectPermitRequest) -> ExternalEffectPermitResult: ...


class UnavailableConversationEffectPort:
    """Read-only default when no separately provisioned sidecar is configured."""

    async def eligibility(self, request):
        raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")

    async def permit(self, request):
        raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")


class HttpConversationEffectPort:
    def __init__(self, base_url: str, token: SecretStr, client: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def eligibility(self, request: ExternalEffectEligibilityRequest) -> ExternalEffectEligibilityResult:
        result = await self._post(
            "/internal/v1/external-effect-eligibility", request, ExternalEffectEligibilityResult,
            definitive_denial=False,
        )
        if result.eligible != (result.reason == EffectReason.ELIGIBLE):
            raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")
        return result

    async def permit(self, request: ExternalEffectPermitRequest) -> ExternalEffectPermitResult:
        return await self._post(
            "/internal/v1/external-effect-permits", request, ExternalEffectPermitResult,
            definitive_denial=True,
        )

    async def _post(self, path, request, response_type, *, definitive_denial):
        try:
            encoded = json.dumps(
                request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
            async with self._client.stream(
                "POST", self.base_url + path, content=encoded,
                headers={
                    "X-Service-Token": self._token.get_secret_value(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.status_code == 409 and definitive_denial:
                    if (_single_header(response, "content-type") != "application/json"
                            or _single_header(response, "content-encoding", required=False) not in {None, "identity"}):
                        raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")
                    data = await _bounded_json(response)
                    denial = _definitive_denial(data)
                    if denial is not None:
                        raise denial
                if response.status_code != 200:
                    raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")
                if _single_header(response, "content-type") != "application/json":
                    raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")
                if _single_header(response, "content-encoding", required=False) not in {None, "identity"}:
                    raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")
                data = await _bounded_json(response)
            result = response_type.model_validate(data)
            if result.request_fingerprint != sidecar_fingerprint(request):
                raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")
            return result
        except DefinitiveEffectDenial:
            raise
        except EffectPortError:
            raise
        except (httpx.HTTPError, ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
            raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE") from None


async def _bounded_json(response):
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) < 0 or int(content_length) > MAX_RESPONSE_BYTES:
                raise ValueError
        except (TypeError, ValueError):
            raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE") from None
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
            raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")
        body.extend(chunk)
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE") from None


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _single_header(response, name, *, required=True):
    values = response.headers.get_list(name)
    if not values and not required:
        return None
    if len(values) != 1:
        raise EffectPortUnavailable("BUSINESS_EFFECT_AUTHORITY_UNAVAILABLE")
    return values[0].strip().lower()


def _definitive_denial(data):
    if not isinstance(data, dict) or set(data) != {"detail"} or not isinstance(data["detail"], dict):
        return None
    detail = data["detail"]
    if set(detail) != {"code", "reason"} or detail.get("code") != "EXTERNAL_EFFECT_DENIED":
        return None
    try:
        reason = TypeAdapter(EffectReason).validate_python(detail["reason"])
    except ValidationError:
        return None
    if reason == EffectReason.ELIGIBLE:
        return None
    return DefinitiveEffectDenial(reason)


def build_conversation_effect_port(settings, execution):
    """Create a request-owned, bounded transport; absent config always denies."""
    base_url = settings.business_conversation_url
    token = settings.business_conversation_token
    if not base_url or not token.get_secret_value():
        return UnavailableConversationEffectPort()
    if execution is None or getattr(execution, "stack", None) is None:
        return UnavailableConversationEffectPort()
    from services.managed_http_cleanup import close_managed_helper, own_managed_http

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.business_conversation_timeout_seconds),
        follow_redirects=False,
        trust_env=False,
        transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
    )
    own_managed_http(client)
    execution.stack.push_async_callback(close_managed_helper, client)
    return HttpConversationEffectPort(base_url, token, client)
