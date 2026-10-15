"""AI ownership proof and revoke-before-mutation lifecycle behavior."""

import hashlib
import json
import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from core.config import get_settings
from core.managed_security import parse_principals
from models.agent import AgentTemplate
from schemas.handoff import AgentProofRequest, AgentRevokeAcknowledgement, AgentRevokeRequest, HandoffScope
from services import handoff_lifecycle
from services.agent_service import AgentService
from services.handoff_lifecycle import HandoffLifecycleUnavailable


LIFECYCLE_TOKEN = "handoff-lifecycle-test-token-0123456789"
MANAGED_TOKEN = "managed-conversation-test-token-0123456789"


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

    monkeypatch.setattr(key_module.agent_service, "deactivate_agent", unavailable)
    response = await api_client.delete(
        f"/v1/agents/{agent_id}/openrouter-key", headers={"x-user-id": str(owner_id)}
    )
    assert response.status_code == 503
    async with db_session() as session:
        unchanged = await session.get(AgentTemplate, agent_id)
        assert unchanged.is_active is True
        assert unchanged.encrypted_openrouter_key == "encrypted-test-key"
