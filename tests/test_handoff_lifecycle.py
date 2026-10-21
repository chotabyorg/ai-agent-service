"""AI ownership proof and revoke-before-mutation lifecycle behavior."""

import hashlib
import json
import asyncio
from uuid import uuid4

import httpx
import pytest

from core.config import get_settings
from core.managed_security import parse_principals
from models.agent import AgentTemplate
from schemas.handoff import AgentProofRequest, AgentRevokeAcknowledgement, AgentRevokeRequest, HandoffScope
from schemas.agent import AgentUpdate
from services import handoff_lifecycle
from services.agent_service import AgentService
from services.agent_service import AgentActivationUnavailable
from services.handoff_lifecycle import HandoffLifecycleConflict, HandoffLifecycleUnavailable


LIFECYCLE_TOKEN = "handoff-lifecycle-test-token-0123456789"
MANAGED_TOKEN = "managed-conversation-test-token-0123456789"


@pytest.mark.parametrize("token", [" " + LIFECYCLE_TOKEN, LIFECYCLE_TOKEN + "\n"])
def test_handoff_token_is_validated_exactly_as_sent(token):
    settings = get_settings().model_copy(update={
        "handoff_lifecycle_token": type(get_settings().handoff_lifecycle_token)(token),
        "handoff_conversation_url": "http://conversation.invalid",
    })
    with pytest.raises(RuntimeError, match="Handoff lifecycle"):
        settings.validate_handoff_lifecycle()


@pytest.mark.integration
@pytest.mark.parametrize("failure_type", [HandoffLifecycleConflict, HandoffLifecycleUnavailable])
async def test_bulk_deactivation_attempts_every_agent_before_reporting_lifecycle_failure(
        db_session, monkeypatch, failure_type):
    account_id = uuid4()
    ids = [uuid4() for _ in range(3)]
    async with db_session() as session:
        session.add_all([AgentTemplate(id=agent_id, user_id=account_id, name="bulk lifecycle",
                                      system_prompt="fixture", llm_provider="fixture",
                                      model_name="fixture", is_active=True) for agent_id in ids])
        await session.commit()
    service = AgentService()
    original = service.deactivate_agent
    called = []

    async def deactivate(session, agent_id, owner_id):
        called.append(agent_id)
        if len(called) == 1:
            raise failure_type()
        return await original(session, agent_id, owner_id)

    monkeypatch.setattr(service, "deactivate_agent", deactivate)
    async with db_session() as session:
        with pytest.raises(failure_type):
            await service.deactivate_all_agents(session, account_id)
    assert set(called) == set(ids) and len(called) == 3
    async with db_session() as session:
        agents = [await session.get(AgentTemplate, agent_id) for agent_id in called]
        assert agents[0].is_active
        assert all(not agent.is_active for agent in agents[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [HandoffLifecycleConflict, HandoffLifecycleUnavailable])
async def test_key_error_deactivation_keeps_the_original_error_path(monkeypatch, failure_type):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from services.llm_service import LLMService
    service = LLMService()
    monkeypatch.setattr(service.agent_service, "deactivate_agent", AsyncMock(side_effect=failure_type()))
    await service._deactivate_agent(SimpleNamespace(id=uuid4(), user_id=uuid4()), None, "key failed")
    assert service.agent_service.deactivate_agent.await_count == 1


def configure_handoff(monkeypatch, scope, agent_owner_id):
    settings = get_settings()
    principal = dict(
        principal_id="conversation-handoff-tests",
        token=MANAGED_TOKEN,
        caller="conversation-service",
        execution_namespace="main-chota.conversation.v1",
        grants=[dict(
            owner_id=scope.owner_id,
            integration_id=str(scope.integration_id),
            channel=scope.channel,
            agent_id=str(scope.agent_id),
            agent_owner_id=str(agent_owner_id),
        )],
    )
    parsed = parse_principals(json.dumps([principal]), ())
    monkeypatch.setattr(settings, "_managed_principals", parsed)
    monkeypatch.setattr(settings, "_handoff_scopes", (scope,))
    monkeypatch.setattr(settings, "handoff_conversation_url", "http://conversation.invalid")
    monkeypatch.setattr(settings, "handoff_lifecycle_token", type(settings.handoff_lifecycle_token)(LIFECYCLE_TOKEN))
    monkeypatch.setattr(settings, "handoff_lifecycle_timeout_seconds", 1.0)


class FakeResponse:
    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self.headers = httpx.Headers({"content-type": "application/json"})
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def aiter_bytes(self):
        yield self.body


class FakeConversationClient:
    replies = []
    calls = []

    def __init__(self, **kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def stream(self, method, url, *, content, headers):
        assert method == "POST"
        assert url == "http://conversation.invalid/internal/v1/inbox-grants/revoke"
        assert headers["X-Service-Token"] == LIFECYCLE_TOKEN
        assert headers["Accept-Encoding"] == "identity"
        payload = json.loads(content)
        self.calls.append(payload)
        status, replayed = self.replies.pop(0)
        if status != 200:
            return FakeResponse(status)
        request = AgentRevokeRequest.model_validate(payload)
        ack = AgentRevokeAcknowledgement.from_request(
            request,
            request_fingerprint=hashlib.sha256(content).hexdigest(),
            grant_generation=1,
            active=False,
            replayed=replayed,
        )
        return FakeResponse(200, json.dumps(ack.model_dump(mode="json")).encode())


@pytest.mark.integration
async def test_proof_records_scope_and_config_removal_cannot_skip_revoke(
        api_client, db_session, monkeypatch):
    account_id = uuid4()
    scope = HandoffScope(owner_id="opaque-telegram-owner", integration_id=uuid4(),
                         channel="TELEGRAM", agent_id=uuid4())
    configure_handoff(monkeypatch, scope, account_id)
    FakeConversationClient.calls = []
    async with db_session() as session:
        agent = AgentTemplate(
            id=scope.agent_id,
            user_id=account_id,
            name="handoff agent",
            system_prompt="hello",
            llm_provider="openrouter",
            model_name="example/model",
            temperature=0.5,
            is_active=True,
            handoff_lifecycle_generation=1,
        )
        session.add(agent)
        await session.commit()

    request = AgentProofRequest(
        contract_version=1,
        owner_id=scope.owner_id,
        integration_id=scope.integration_id,
        channel=scope.channel,
        agent_id=scope.agent_id,
    )
    response = await api_client.post(
        "/internal/v1/handoff/agent-proof",
        json=request.model_dump(mode="json"),
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert response.status_code == 200
    assert response.json() == {
        **request.model_dump(mode="json"),
        "agent_owner_id": str(account_id),
        "agent_lifecycle_generation": 1,
    }
    async with db_session() as session:
        agent = await session.get(AgentTemplate, scope.agent_id)
        assert agent.handoff_lifecycle_scope_snapshot == [{
            "owner_id": scope.owner_id,
            "integration_id": str(scope.integration_id),
            "channel": "TELEGRAM",
            "agent_id": str(scope.agent_id),
            "agent_owner_id": str(account_id),
        }]

    # The explicit setting may be removed after a successful proof. The durable
    # AI row still carries the exact scope and account that must be revoked.
    monkeypatch.setattr(get_settings(), "_handoff_scopes", ())
    monkeypatch.setattr(handoff_lifecycle.httpx, "AsyncClient", FakeConversationClient)
    FakeConversationClient.replies = [(503, False), (200, True)]
    async with db_session() as session:
        with pytest.raises(HandoffLifecycleUnavailable):
            await AgentService().deactivate_agent(session, scope.agent_id, account_id)
    async with db_session() as session:
        pending = await session.get(AgentTemplate, scope.agent_id)
        assert pending.is_active is True
        assert pending.handoff_lifecycle_state == "PENDING_DEACTIVATE"
        assert pending.handoff_lifecycle_source_generation == 1
        assert pending.handoff_lifecycle_generation == 2
        retained_snapshot = pending.handoff_lifecycle_scope_snapshot
    async with db_session() as session:
        agent = await AgentService().deactivate_agent(session, scope.agent_id, account_id)
        assert agent is not None and agent.is_active is False
        assert agent.handoff_lifecycle_state == "STABLE"
        assert agent.handoff_lifecycle_generation == 2
        assert agent.handoff_lifecycle_source_generation is None
        assert agent.handoff_lifecycle_scope_snapshot == retained_snapshot
    assert len(FakeConversationClient.calls) == 2
    assert FakeConversationClient.calls[0] == FakeConversationClient.calls[1]
    command = FakeConversationClient.calls[0]
    assert command["source_agent_lifecycle_generation"] == 1
    assert command["target_agent_lifecycle_generation"] == 2
    assert command["lifecycle_source"] == "AI_AGENT"


@pytest.mark.integration
async def test_reactivation_cannot_rebind_proven_scope_to_replacement_account(
        api_client, db_session, monkeypatch):
    original_account_id = uuid4()
    replacement_account_id = uuid4()
    scope = HandoffScope(owner_id="opaque-rebind-owner", integration_id=uuid4(),
                         channel="TELEGRAM", agent_id=uuid4())
    configure_handoff(monkeypatch, scope, original_account_id)
    FakeConversationClient.calls = []
    async with db_session() as session:
        session.add(AgentTemplate(
            id=scope.agent_id,
            user_id=original_account_id,
            name="rebind guard",
            system_prompt="private conversation context",
            llm_provider="openrouter",
            model_name="example/model",
            temperature=0.5,
            is_active=True,
            encrypted_openrouter_key="encrypted-test-key",
            handoff_lifecycle_generation=1,
        ))
        await session.commit()

    request = AgentProofRequest(
        contract_version=1,
        owner_id=scope.owner_id,
        integration_id=scope.integration_id,
        channel=scope.channel,
        agent_id=scope.agent_id,
    )
    proof = await api_client.post(
        "/internal/v1/handoff/agent-proof",
        json=request.model_dump(mode="json"),
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert proof.status_code == 200
    assert proof.json()["agent_owner_id"] == str(original_account_id)

    monkeypatch.setattr(handoff_lifecycle.httpx, "AsyncClient", FakeConversationClient)
    FakeConversationClient.replies = [(200, False)]
    async with db_session() as session:
        deactivated = await AgentService().deactivate_agent(
            session, scope.agent_id, original_account_id)
        assert deactivated is not None and not deactivated.is_active
        assert deactivated.handoff_lifecycle_generation == 2
        assert deactivated.handoff_lifecycle_state == "STABLE"

    async with db_session() as session:
        reactivated = await AgentService().update_agent(
            session,
            original_account_id,
            scope.agent_id,
            AgentUpdate(is_active=True),
        )
        assert reactivated is not None and reactivated.is_active
        assert reactivated.handoff_lifecycle_generation == 3
        assert reactivated.handoff_lifecycle_scope_snapshot == [{
            "owner_id": scope.owner_id,
            "integration_id": str(scope.integration_id),
            "channel": "TELEGRAM",
            "agent_id": str(scope.agent_id),
            "agent_owner_id": str(original_account_id),
        }]

    # The same opaque owner/integration/agent tuple is now misconfigured to a
    # different account. The proof boundary must not disclose the old binding.
    configure_handoff(monkeypatch, scope, replacement_account_id)
    replacement_proof = await api_client.post(
        "/internal/v1/handoff/agent-proof",
        json=request.model_dump(mode="json"),
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert replacement_proof.status_code == 404
    assert replacement_proof.json() == {
        "detail": {"code": "AGENT_NOT_FOUND", "message": "Agent not found."}
    }
    assert "private conversation context" not in replacement_proof.text
    assert str(original_account_id) not in replacement_proof.text
    assert str(replacement_account_id) not in replacement_proof.text

    # A later deactivation cannot overwrite the retained account identity or
    # send Conversation a revoke for the replacement account.
    async with db_session() as session:
        with pytest.raises(HandoffLifecycleConflict):
            await AgentService().deactivate_agent(
                session, scope.agent_id, original_account_id)
    assert len(FakeConversationClient.calls) == 1
    assert FakeConversationClient.calls[0]["agent_owner_id"] == str(original_account_id)
    async with db_session() as session:
        unchanged = await session.get(AgentTemplate, scope.agent_id)
        assert unchanged is not None
        assert unchanged.user_id == original_account_id
        assert unchanged.is_active is True
        assert unchanged.handoff_lifecycle_state == "STABLE"
        assert unchanged.handoff_lifecycle_generation == 3
        assert unchanged.handoff_lifecycle_scope_snapshot == [{
            "owner_id": scope.owner_id,
            "integration_id": str(scope.integration_id),
            "channel": "TELEGRAM",
            "agent_id": str(scope.agent_id),
            "agent_owner_id": str(original_account_id),
        }]


@pytest.mark.integration
async def test_proof_uses_dedicated_token_and_denies_inactive_or_pending_agent(
        api_client, db_session, monkeypatch):
    account_id = uuid4()
    scope = HandoffScope(owner_id="opaque-proof-owner", integration_id=uuid4(),
                         channel="TELEGRAM", agent_id=uuid4())
    configure_handoff(monkeypatch, scope, account_id)
    async with db_session() as session:
        session.add(AgentTemplate(
            id=scope.agent_id, user_id=account_id, name="inactive",
            system_prompt="hello", llm_provider="openrouter", model_name="example/model",
            temperature=0.5, is_active=False, handoff_lifecycle_generation=1,
        ))
        await session.commit()
    request = dict(contract_version=1, owner_id=scope.owner_id,
                   integration_id=str(scope.integration_id), channel=scope.channel,
                   agent_id=str(scope.agent_id))
    for token in (None, MANAGED_TOKEN):
        headers = {} if token is None else {"X-Service-Token": token}
        response = await api_client.post("/internal/v1/handoff/agent-proof", json=request, headers=headers)
        assert response.status_code == 401
    response = await api_client.post(
        "/internal/v1/handoff/agent-proof", json=request,
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert response.status_code == 409


@pytest.mark.integration
async def test_pending_lifecycle_releases_agent_lock_before_revoke_and_blocks_proof(
        api_client, db_session, monkeypatch):
    account_id = uuid4()
    scope = HandoffScope(owner_id="opaque-lock-owner", integration_id=uuid4(),
                         channel="TELEGRAM", agent_id=uuid4())
    configure_handoff(monkeypatch, scope, account_id)
    async with db_session() as session:
        agent = AgentTemplate(
            id=scope.agent_id, user_id=account_id, name="locked agent", system_prompt="hello",
            llm_provider="openrouter", model_name="example/model", temperature=0.5,
            is_active=True, handoff_lifecycle_generation=1,
            handoff_lifecycle_scope_snapshot=[{
                "owner_id": scope.owner_id, "integration_id": str(scope.integration_id),
                "channel": scope.channel, "agent_id": str(scope.agent_id),
                "agent_owner_id": str(account_id),
            }],
        )
        session.add(agent)
        await session.commit()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_revoke(*_args, **_kwargs):
        started.set()
        await release.wait()

    monkeypatch.setattr(handoff_lifecycle, "_revoke_all", slow_revoke)
    deactivation = asyncio.create_task(_deactivate(db_session, scope.agent_id, account_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    proof = await api_client.post(
        "/internal/v1/handoff/agent-proof",
        json=dict(contract_version=1, owner_id=scope.owner_id,
                  integration_id=str(scope.integration_id), channel=scope.channel,
                  agent_id=str(scope.agent_id)),
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert proof.status_code == 409
    release.set()
    result = await asyncio.wait_for(deactivation, timeout=2)
    assert result is not None and result.is_active is False


async def _deactivate(factory, agent_id, account_id):
    async with factory() as session:
        return await AgentService().deactivate_agent(session, agent_id, account_id)


@pytest.mark.integration
async def test_openrouter_key_delete_keeps_active_agent_when_revoke_is_unavailable(
        api_client, db_session, monkeypatch):
    import api.v1.openrouter_key as key_module

    owner_id = uuid4()
    async with db_session() as session:
        agent = AgentTemplate(
            user_id=owner_id, name="key agent", system_prompt="hello",
            llm_provider="openrouter", model_name="example/model", temperature=0.5,
            is_active=True, encrypted_openrouter_key="encrypted-test-key",
            handoff_lifecycle_generation=1,
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        agent_id = agent.id

    async def unavailable(*_args, **_kwargs):
        raise HandoffLifecycleUnavailable

    monkeypatch.setattr(key_module.agent_service, "remove_openrouter_key", unavailable)
    response = await api_client.delete(
        f"/v1/agents/{agent_id}/openrouter-key", headers={"x-user-id": str(owner_id)}
    )
    assert response.status_code == 503
    async with db_session() as session:
        unchanged = await session.get(AgentTemplate, agent_id)
        assert unchanged.is_active is True
        assert unchanged.encrypted_openrouter_key == "encrypted-test-key"


@pytest.mark.integration
async def test_key_delete_fences_activation_and_proof_until_revoke_finishes(
        api_client, db_session, monkeypatch):
    account_id = uuid4()
    scope = HandoffScope(owner_id="opaque-delete-first-owner", integration_id=uuid4(),
                         channel="TELEGRAM", agent_id=uuid4())
    configure_handoff(monkeypatch, scope, account_id)
    async with db_session() as session:
        session.add(AgentTemplate(
            id=scope.agent_id, user_id=account_id, name="delete first",
            system_prompt="hello", llm_provider="openrouter", model_name="example/model",
            temperature=0.5, is_active=True, encrypted_openrouter_key="encrypted-key",
            handoff_lifecycle_generation=1,
        ))
        await session.commit()

    proof_body = AgentProofRequest(
        contract_version=1, owner_id=scope.owner_id, integration_id=scope.integration_id,
        channel=scope.channel, agent_id=scope.agent_id,
    ).model_dump(mode="json")
    proof = await api_client.post(
        "/internal/v1/handoff/agent-proof", json=proof_body,
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert proof.status_code == 200

    revoke_started = asyncio.Event()
    allow_revoke = asyncio.Event()
    revokes = []

    async def paused_revoke(scopes, source_generation, target_generation, runtime=None):
        revokes.append((scopes, source_generation, target_generation))
        revoke_started.set()
        await allow_revoke.wait()

    monkeypatch.setattr(handoff_lifecycle, "_revoke_all", paused_revoke)
    delete_task = asyncio.create_task(api_client.delete(
        f"/v1/agents/{scope.agent_id}/openrouter-key",
        headers={"x-user-id": str(account_id)},
    ))
    await asyncio.wait_for(revoke_started.wait(), timeout=2)
    try:
        async with db_session() as session:
            pending = await session.get(AgentTemplate, scope.agent_id)
            assert pending.is_active is True
            assert pending.encrypted_openrouter_key == "encrypted-key"
            assert pending.handoff_lifecycle_state == "PENDING_DEACTIVATE"
            assert pending.handoff_lifecycle_generation == 2

        async with db_session() as session:
            with pytest.raises(HandoffLifecycleConflict):
                await AgentService().update_agent(
                    session, account_id, scope.agent_id, AgentUpdate(is_active=True))

        pending_proof = await api_client.post(
            "/internal/v1/handoff/agent-proof", json=proof_body,
            headers={"X-Service-Token": LIFECYCLE_TOKEN},
        )
        assert pending_proof.status_code == 409
    finally:
        allow_revoke.set()

    deleted = await asyncio.wait_for(delete_task, timeout=3)
    assert deleted.status_code == 204
    assert [(source, target) for _, source, target in revokes] == [(1, 2)]
    async with db_session() as session:
        agent = await session.get(AgentTemplate, scope.agent_id)
        assert agent.is_active is False
        assert agent.encrypted_openrouter_key is None
        assert agent.handoff_lifecycle_state == "STABLE"
        assert agent.handoff_lifecycle_generation == 2
    after_delete_proof = await api_client.post(
        "/internal/v1/handoff/agent-proof", json=proof_body,
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert after_delete_proof.status_code == 409


@pytest.mark.integration
async def test_activation_lock_wins_then_key_delete_revokes_new_generation(
        api_client, db_session, monkeypatch):
    from services import agent_service as agent_service_module

    account_id = uuid4()
    scope = HandoffScope(owner_id="opaque-activate-first-owner", integration_id=uuid4(),
                         channel="TELEGRAM", agent_id=uuid4())
    configure_handoff(monkeypatch, scope, account_id)
    async with db_session() as session:
        session.add(AgentTemplate(
            id=scope.agent_id, user_id=account_id, name="activate first",
            system_prompt="hello", llm_provider="openrouter", model_name="example/model",
            temperature=0.5, is_active=True, encrypted_openrouter_key="encrypted-key",
            handoff_lifecycle_generation=1,
        ))
        await session.commit()
    proof_body = AgentProofRequest(
        contract_version=1, owner_id=scope.owner_id, integration_id=scope.integration_id,
        channel=scope.channel, agent_id=scope.agent_id,
    ).model_dump(mode="json")
    proof = await api_client.post(
        "/internal/v1/handoff/agent-proof", json=proof_body,
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert proof.status_code == 200

    revokes = []

    async def record_revoke(scopes, source_generation, target_generation, runtime=None):
        revokes.append((scopes, source_generation, target_generation))

    monkeypatch.setattr(handoff_lifecycle, "_revoke_all", record_revoke)
    async with db_session() as session:
        deactivated = await AgentService().deactivate_agent(session, scope.agent_id, account_id)
        assert deactivated is not None and not deactivated.is_active
        assert deactivated.handoff_lifecycle_generation == 2

    activation_locked = asyncio.Event()
    release_activation = asyncio.Event()
    actual_activate = agent_service_module.activate_agent

    async def hold_activation_lock(session, agent_id, owner_id):
        agent = await actual_activate(session, agent_id, owner_id)
        assert agent is not None and agent.is_active
        assert agent.handoff_lifecycle_generation == 3
        activation_locked.set()
        await release_activation.wait()
        return agent

    monkeypatch.setattr(agent_service_module, "activate_agent", hold_activation_lock)

    async def activate():
        async with db_session() as session:
            return await AgentService().update_agent(
                session, account_id, scope.agent_id, AgentUpdate(is_active=True))

    activation_task = asyncio.create_task(activate())
    await asyncio.wait_for(activation_locked.wait(), timeout=2)
    delete_task = asyncio.create_task(api_client.delete(
        f"/v1/agents/{scope.agent_id}/openrouter-key",
        headers={"x-user-id": str(account_id)},
    ))
    release_activation.set()
    activated = await asyncio.wait_for(activation_task, timeout=3)
    assert activated is not None
    deleted = await asyncio.wait_for(delete_task, timeout=3)
    assert deleted.status_code == 204
    assert [(source, target) for _, source, target in revokes] == [(1, 2), (3, 4)]
    async with db_session() as session:
        agent = await session.get(AgentTemplate, scope.agent_id)
        assert agent.is_active is False
        assert agent.encrypted_openrouter_key is None
        assert agent.handoff_lifecycle_state == "STABLE"
        assert agent.handoff_lifecycle_generation == 4
    after_delete_proof = await api_client.post(
        "/internal/v1/handoff/agent-proof", json=proof_body,
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert after_delete_proof.status_code == 409


@pytest.mark.integration
async def test_key_delete_retry_does_not_succeed_after_plain_deactivate_then_reactivate(
        api_client, db_session, monkeypatch):
    account_id = uuid4()
    scope = HandoffScope(owner_id="opaque-overlap-owner", integration_id=uuid4(),
                         channel="TELEGRAM", agent_id=uuid4())
    configure_handoff(monkeypatch, scope, account_id)
    async with db_session() as session:
        session.add(AgentTemplate(
            id=scope.agent_id, user_id=account_id, name="overlapping lifecycle",
            system_prompt="hello", llm_provider="openrouter", model_name="example/model",
            temperature=0.5, is_active=True, encrypted_openrouter_key="encrypted-key",
            handoff_lifecycle_generation=1,
        ))
        await session.commit()
    proof_body = AgentProofRequest(
        contract_version=1, owner_id=scope.owner_id, integration_id=scope.integration_id,
        channel=scope.channel, agent_id=scope.agent_id,
    ).model_dump(mode="json")
    proof = await api_client.post(
        "/internal/v1/handoff/agent-proof", json=proof_body,
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert proof.status_code == 200

    key_revoke_started = asyncio.Event()
    plain_revoke_started = asyncio.Event()
    release_key_revoke = asyncio.Event()
    release_plain_revoke = asyncio.Event()
    revokes = []

    async def overlapping_revoke(scopes, source_generation, target_generation, runtime=None):
        revokes.append((source_generation, target_generation))
        if len(revokes) == 1:
            key_revoke_started.set()
            await release_key_revoke.wait()
        elif len(revokes) == 2:
            plain_revoke_started.set()
            await release_plain_revoke.wait()

    monkeypatch.setattr(handoff_lifecycle, "_revoke_all", overlapping_revoke)
    key_delete_task = asyncio.create_task(api_client.delete(
        f"/v1/agents/{scope.agent_id}/openrouter-key",
        headers={"x-user-id": str(account_id)},
    ))
    await asyncio.wait_for(key_revoke_started.wait(), timeout=2)

    async def plain_deactivate():
        async with db_session() as session:
            return await AgentService().deactivate_agent(session, scope.agent_id, account_id)

    plain_deactivate_task = asyncio.create_task(plain_deactivate())
    await asyncio.wait_for(plain_revoke_started.wait(), timeout=2)
    release_plain_revoke.set()
    plain_result = await asyncio.wait_for(plain_deactivate_task, timeout=3)
    assert plain_result is not None and not plain_result.is_active
    assert plain_result.handoff_lifecycle_generation == 2
    assert plain_result.encrypted_openrouter_key == "encrypted-key"

    # The plain retry may finish the shared pending revoke, but the key-delete
    # request must not claim success if activation wins before its own final lock.
    async with db_session() as session:
        activated = await AgentService().update_agent(
            session, account_id, scope.agent_id, AgentUpdate(is_active=True))
        assert activated is not None and activated.is_active
        assert activated.handoff_lifecycle_generation == 3
    current_proof = await api_client.post(
        "/internal/v1/handoff/agent-proof", json=proof_body,
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert current_proof.status_code == 200
    assert current_proof.json()["agent_lifecycle_generation"] == 3

    release_key_revoke.set()
    key_delete = await asyncio.wait_for(key_delete_task, timeout=3)
    assert key_delete.status_code == 409
    assert revokes == [(1, 2), (1, 2)]
    async with db_session() as session:
        still_active = await session.get(AgentTemplate, scope.agent_id)
        assert still_active.is_active is True
        assert still_active.encrypted_openrouter_key == "encrypted-key"
        assert still_active.handoff_lifecycle_state == "STABLE"
        assert still_active.handoff_lifecycle_generation == 3

    # A retry starts a new transition for the now-current generation and only
    # reports success after that generation is revoked and the key is cleared.
    async def successful_revoke(scopes, source_generation, target_generation, runtime=None):
        revokes.append((source_generation, target_generation))

    monkeypatch.setattr(handoff_lifecycle, "_revoke_all", successful_revoke)
    retry = await api_client.delete(
        f"/v1/agents/{scope.agent_id}/openrouter-key",
        headers={"x-user-id": str(account_id)},
    )
    assert retry.status_code == 204
    assert revokes == [(1, 2), (1, 2), (3, 4)]
    final_proof = await api_client.post(
        "/internal/v1/handoff/agent-proof", json=proof_body,
        headers={"X-Service-Token": LIFECYCLE_TOKEN},
    )
    assert final_proof.status_code == 409
    async with db_session() as session:
        removed = await session.get(AgentTemplate, scope.agent_id)
        assert removed.is_active is False
        assert removed.encrypted_openrouter_key is None
        assert removed.handoff_lifecycle_generation == 4


@pytest.mark.integration
async def test_activation_refreshes_agent_preloaded_before_key_removal(
        db_session):
    account_id = uuid4()
    agent_id = uuid4()
    async with db_session() as session:
        session.add(AgentTemplate(
            id=agent_id, user_id=account_id, name="stale activation",
            system_prompt="hello", llm_provider="openrouter", model_name="example/model",
            temperature=0.5, is_active=False, encrypted_openrouter_key="encrypted-key",
            handoff_lifecycle_generation=0,
        ))
        await session.commit()

    async with db_session() as stale_session:
        stale_agent = await stale_session.get(AgentTemplate, agent_id)
        assert stale_agent.encrypted_openrouter_key == "encrypted-key"
        async with db_session() as removal_session:
            removed = await AgentService().remove_openrouter_key(
                removal_session, agent_id, account_id)
            assert removed is not None
            assert removed.is_active is False
            assert removed.encrypted_openrouter_key is None
        with pytest.raises(AgentActivationUnavailable):
            await AgentService().update_agent(
                stale_session, account_id, agent_id, AgentUpdate(is_active=True))
        assert stale_agent.encrypted_openrouter_key is None
        assert stale_agent.is_active is False
        assert stale_agent.handoff_lifecycle_generation == 0
