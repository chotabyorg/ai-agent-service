"""Strict v3 wire and deterministic identity contract tests."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from schemas.handoff import (
    AgentProofRequest,
    AgentRevokeAcknowledgement,
    AgentRevokeRequest,
)
from schemas.managed_turn import ManagedTurnRequest
from schemas.managed_turn_v3 import (
    DeliveryObservationFact,
    ManagedTurnV3Request,
)
from services.managed_execution_receipts import (
    handoff_request_id,
    managed_v3_dispatch_id,
    managed_v3_request_fingerprint,
)
from schemas.conversation_effects import managed_request_fingerprint


def v3_payload(**overrides):
    message_id = UUID(int=5)
    value = dict(
        contract_version=3,
        conversation_id=str(UUID(int=1)),
        turn_id=str(UUID(int=2)),
        owner_id="Exact Cafe\u0301 owner",
        integration_id=str(UUID(int=3)),
        agent_id=str(UUID(int=4)),
        channel="TELEGRAM",
        input_revision=1,
        authority_revision=7,
        input_first_sequence=1,
        input_last_sequence=1,
        context_last_sequence=1,
        messages=[dict(id=str(message_id), sequence=1, actor="CUSTOMER", direction="INBOUND",
                       content_type="TEXT", text="Cafe\u0301 🌍")],
        selected_input_message_ids=[str(message_id)],
        communication_facts=[],
        prior_operation_refs=[],
    )
    value.update(overrides)
    return value


def test_v3_is_required_and_rejects_numeric_coercion():
    value = v3_payload()
    assert ManagedTurnV3Request.model_validate(value).contract_version == 3
    assert "contract_version" in ManagedTurnV3Request.model_json_schema()["required"]
    for bad in (True, 3.0, "3", 2, 4):
        with pytest.raises(ValidationError):
            ManagedTurnV3Request.model_validate({**value, "contract_version": bad})
    for bad_id in (1, str(UUID(int=0xabcdef)).upper()):
        with pytest.raises(ValidationError):
            ManagedTurnV3Request.model_validate({**value, "conversation_id": bad_id})
    value.pop("contract_version")
    with pytest.raises(ValidationError):
        ManagedTurnV3Request.model_validate(value)


def test_v3_roundtrip_keeps_exact_closed_conversation_wire_profile():
    value = v3_payload()
    assert ManagedTurnV3Request.model_validate(value).model_dump(mode="json") == value
    for name, supplied in (("stream_partials", False), ("max_partial_chars", 2000)):
        with pytest.raises(ValidationError, match="Extra inputs"):
            ManagedTurnV3Request.model_validate({**value, name: supplied})


def test_v3_outbound_human_and_system_messages_require_one_latest_delivery_fact():
    message_id = str(UUID(int=5))
    for actor in ("HUMAN", "SYSTEM"):
        value = v3_payload(messages=[dict(id=message_id, sequence=1, actor=actor, direction="OUTBOUND",
                                          content_type="TEXT", text="operator response")])
        with pytest.raises(ValidationError, match="delivery fact"):
            ManagedTurnV3Request.model_validate(value)
        value["communication_facts"] = [dict(
            kind="DELIVERY_OBSERVATION", message_id=message_id, delivery_attempt_id=None,
            source_authority_revision=7, status="NOT_ACQUIRED",
        )]
        parsed = ManagedTurnV3Request.model_validate(value)
        assert parsed.communication_facts[0].status == "NOT_ACQUIRED"
        value["communication_facts"].append(dict(value["communication_facts"][0]))
        with pytest.raises(ValidationError, match="latest delivery"):
            ManagedTurnV3Request.model_validate(value)


@pytest.mark.parametrize("status,attempt", [
    ("NOT_ACQUIRED", None), ("GENERATED", str(uuid4())), ("UNKNOWN", str(uuid4())),
])
def test_delivery_attempt_nullability_is_closed(status, attempt):
    fact = DeliveryObservationFact.model_validate(dict(
        kind="DELIVERY_OBSERVATION", message_id=uuid4(), delivery_attempt_id=attempt,
        source_authority_revision=0, status=status,
    ))
    assert fact.status == status
    invalid_attempt = str(uuid4()) if attempt is None else None
    with pytest.raises(ValidationError):
        DeliveryObservationFact.model_validate(dict(
            kind="DELIVERY_OBSERVATION", message_id=uuid4(), delivery_attempt_id=invalid_attempt,
            source_authority_revision=0, status=status,
        ))


def test_prior_operation_references_must_be_unique_and_sorted():
    first = dict(origin_turn_id=str(UUID(int=10)), operation_ref=dict(
        origin="AI_AGENT_SERVICE", operation_id="a", outcome="UNKNOWN"))
    second = dict(origin_turn_id=str(UUID(int=11)), operation_ref=dict(
        origin="AI_AGENT_SERVICE", operation_id="b", outcome=None))
    parsed = ManagedTurnV3Request.model_validate(v3_payload(prior_operation_refs=[first, second]))
    assert len(parsed.prior_operation_refs) == 2
    for refs in ([second, first], [first, first]):
        with pytest.raises(ValidationError, match="unique and sorted"):
            ManagedTurnV3Request.model_validate(v3_payload(prior_operation_refs=refs))


def test_v1_v2_dto_and_sidecar_fingerprint_remain_unchanged():
    legacy = dict(
        conversation_id=str(UUID(int=1)), turn_id=str(UUID(int=2)), owner_id="Exact Cafe\u0301 owner",
        integration_id=str(UUID(int=3)), agent_id=str(UUID(int=4)), channel="WEBSITE_WIDGET",
        input_revision=1, authority_revision=0, input_first_sequence=1, input_last_sequence=1,
        context_last_sequence=1, messages=[dict(id=str(UUID(int=5)), sequence=1, actor="CUSTOMER",
            direction="INBOUND", content_type="TEXT", text="Cafe\u0301 🌍")],
        selected_input_message_ids=[str(UUID(int=5))],
    )
    v1 = ManagedTurnRequest.model_validate(legacy)
    assert managed_request_fingerprint(v1) == "a4f8bf8a3cf30a658d96e9cd6579f9ba35136fbd81bbd622fdab78cbfe51f326"
    v3 = ManagedTurnV3Request.model_validate(v3_payload())
    # V3 has its own identity digest. The legacy sidecar digest is retained as a
    # distinct namespace and continues to include the entire request snapshot.
    digest = managed_v3_request_fingerprint(v3)
    assert digest == "5f0deb859ca1b30a54da2e9a5ba8a205070d8012955a853691685b53ca6b29c2"
    assert managed_request_fingerprint(v3) == "4270e26fbc307bb11e51db5a38b7374ae7b73244acf5a2f601d0796ba0e19b50"
    assert managed_v3_dispatch_id(v3, digest) == UUID("c6997361-cb5b-5b8d-adf7-a358e1af27fd")
    assert handoff_request_id(v3) == UUID("3a05d5b0-bff6-5066-9d42-e65e6c421625")


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_ai_lifecycle_contract_versions_are_strict(version):
    body = dict(contract_version=version, owner_id="opaque", integration_id=uuid4(),
                channel="TELEGRAM", agent_id=uuid4())
    with pytest.raises(ValidationError):
        AgentProofRequest.model_validate(body)
    with pytest.raises(ValidationError):
        AgentRevokeRequest.model_validate({
            **body, "lifecycle_source": "AI_AGENT", "command_id": uuid4(), "agent_owner_id": uuid4(),
            "source_agent_lifecycle_generation": 1, "target_agent_lifecycle_generation": 2,
        })


def test_lifecycle_uuid_is_canonical_and_ack_boolean_is_strict():
    proof = dict(contract_version=1, owner_id="opaque", integration_id=uuid4(),
                 channel="TELEGRAM", agent_id=uuid4())
    with pytest.raises(ValidationError):
        AgentProofRequest.model_validate({**proof, "agent_id": str(proof["agent_id"]).upper()})
    assert AgentProofRequest.model_validate(proof)
    command = AgentRevokeRequest(
        **proof, lifecycle_source="AI_AGENT", command_id=uuid4(), agent_owner_id=uuid4(),
        source_agent_lifecycle_generation=1, target_agent_lifecycle_generation=2,
    )
    ack_body = dict(
        **command.model_dump(), request_fingerprint="a" * 64, grant_generation=1,
        active=False, replayed=False,
    )
    assert AgentRevokeAcknowledgement.model_validate(ack_body).active is False
    with pytest.raises(ValidationError):
        AgentRevokeAcknowledgement.model_validate({**ack_body, "active": 0})
