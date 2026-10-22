"""Strict wire and failure-boundary tests for the Conversation effect sidecar."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from core.config import Settings
from schemas.conversation_effects import (
    ExternalEffectEligibilityRequest,
    ExternalEffectEligibilityResult,
    ExternalEffectPermitRequest,
    ExternalEffectPermitResult,
    sidecar_fingerprint,
    managed_request_fingerprint,
)
from schemas.managed_turn import ManagedTurnRequest
from services.conversation_effects import (
    DefinitiveEffectDenial,
    EffectPortUnavailable,
    HttpConversationEffectPort,
)


FIXTURE = Path(__file__).parent / "fixtures" / "external_effects" / "contract-v1.json"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _port(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    return HttpConversationEffectPort("http://conversation.test", SecretStr("service-token-test"), client), client


def test_managed_identity_uses_pre_stream_or_complete_extension_shape():
    from uuid import UUID

    value = dict(
        conversation_id=str(UUID(int=1)), turn_id=str(UUID(int=2)),
        owner_id="Exact Café owner", integration_id=str(UUID(int=3)),
        agent_id=str(UUID(int=4)), channel="WEBSITE_WIDGET", input_revision=1,
        authority_revision=0, input_first_sequence=1, input_last_sequence=1,
        context_last_sequence=1,
        messages=[dict(id=str(UUID(int=5)), sequence=1, actor="CUSTOMER",
                       direction="INBOUND", content_type="TEXT", text="Café 🌍")],
        selected_input_message_ids=[str(UUID(int=5))],
    )
    pre_stream = ManagedTurnRequest.model_validate(value)
    assert not {"stream_partials", "max_partial_chars"} & pre_stream.model_fields_set
    assert pre_stream.stream_partials is False and pre_stream.max_partial_chars == 2000
    assert managed_request_fingerprint(pre_stream) == \
        "a4f8bf8a3cf30a658d96e9cd6579f9ba35136fbd81bbd622fdab78cbfe51f326"
    extension = ManagedTurnRequest.model_validate({**value, "stream_partials": False})
    assert managed_request_fingerprint(extension) == \
        "ab2fb9275e1b6325b131b6b36a59f47bbcbc8a7d17a4493b76199ea584c50204"
    custom = ManagedTurnRequest.model_validate({**value, "max_partial_chars": 501})
    assert managed_request_fingerprint(custom) not in {
        managed_request_fingerprint(pre_stream), managed_request_fingerprint(extension)
    }


@pytest.mark.parametrize("contract_version", [True, 1.0, "1", 2])
def test_sidecar_contract_version_rejects_coercion(contract_version):
    fixture = _fixture()
    for dto, payload in (
        (ExternalEffectEligibilityRequest, fixture["context"]),
        (ExternalEffectPermitRequest, fixture["permit_request"]),
        (ExternalEffectEligibilityResult, fixture["eligibility_result"]),
        (ExternalEffectPermitResult, fixture["permit_result"]),
    ):
        with pytest.raises(ValidationError):
            dto.model_validate({**payload, "contract_version": contract_version})


def test_conversation_authority_configuration_is_an_independent_pair():
    token = "conversation-service-token-123456789"
    assert Settings(_env_file=None).business_conversation_url == ""
    for values in (
        {"business_conversation_url": "http://conversation.test"},
        {"business_conversation_token": token},
        {"business_conversation_url": "http://conversation.test", "business_conversation_token": token,
         "internal_api_token": token},
        {"business_conversation_url": "http://user:pass@conversation.test", "business_conversation_token": token},
        {"business_conversation_url": "http://conversation.test", "business_conversation_token": "short"},
    ):
        with pytest.raises(RuntimeError, match="Invalid Business Conversation authority configuration"):
            Settings(_env_file=None, **values)
    settings = Settings(_env_file=None, business_conversation_url="http://conversation.test",
                        business_conversation_token=token)
    assert settings.business_conversation_url == "http://conversation.test"


@pytest.mark.asyncio
async def test_frozen_shared_fixture_hashes_and_service_token_header():
    fixture = _fixture()
    eligibility_request = ExternalEffectEligibilityRequest.model_validate(fixture["context"])
    permit_request = ExternalEffectPermitRequest.model_validate(fixture["permit_request"])
    assert sidecar_fingerprint(eligibility_request) == fixture["eligibility_result"]["request_fingerprint"]
    assert sidecar_fingerprint(permit_request) == fixture["permit_result"]["request_fingerprint"]
    observed = []

    async def handler(request):
        observed.append(request)
        if request.url.path.endswith("eligibility"):
            payload = fixture["eligibility_result"]
        else:
            payload = fixture["permit_result"]
        return httpx.Response(200, headers={"content-type": "application/json"}, json=payload)

    port, client = _port(handler)
    try:
        await port.eligibility(eligibility_request)
        await port.permit(permit_request)
    finally:
        await client.aclose()

    assert [request.url.path for request in observed] == [
        "/internal/v1/external-effect-eligibility",
        "/internal/v1/external-effect-permits",
    ]
    for request in observed:
        assert request.headers["x-service-token"] == "service-token-test"
        assert "authorization" not in request.headers
        assert "bearer" not in request.headers.get("authorization", "").lower()
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_only_bounded_json_identity_409_is_a_definitive_denial():
    fixture = _fixture()
    request = ExternalEffectPermitRequest.model_validate(fixture["permit_request"])

    async def valid_handler(_):
        return httpx.Response(409, headers={"content-type": "application/json"}, json=fixture["permit_denial"])

    port, client = _port(valid_handler)
    try:
        with pytest.raises(DefinitiveEffectDenial) as denial:
            await port.permit(request)
        assert denial.value.reason.value == "INPUT_SUPERSEDED"
    finally:
        await client.aclose()

    for headers, body in (
        ({"content-type": "text/plain"}, json.dumps(fixture["permit_denial"])),
        ({"content-type": "application/json", "content-encoding": "gzip"},
         json.dumps(fixture["permit_denial"])),
        ({"content-type": "application/json"}, "{"),
        ({"content-type": "application/json"}, " " * 8193),
    ):
        async def invalid_handler(_, *, headers=headers, body=body):
            return httpx.Response(409, headers=headers, content=body.encode("utf-8"))

        port, client = _port(invalid_handler)
        try:
            with pytest.raises(EffectPortUnavailable):
                await port.permit(request)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_context_conflict_and_permit_conflict_remain_ambiguous():
    fixture = _fixture()
    request = ExternalEffectPermitRequest.model_validate(fixture["permit_request"])

    for payload in (fixture["context_conflict"], fixture["permit_conflict"]):
        async def handler(_, *, payload=payload):
            return httpx.Response(409, headers={"content-type": "application/json"}, json=payload)

        port, client = _port(handler)
        try:
            with pytest.raises(EffectPortUnavailable):
                await port.permit(request)
        finally:
            await client.aclose()
