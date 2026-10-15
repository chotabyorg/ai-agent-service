"""Fail-closed AI lifecycle supplier for explicitly enrolled Telegram scopes."""

import hashlib
import json
from uuid import UUID, uuid5

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from models.agent import AgentTemplate
from schemas.handoff import AgentRevokeAcknowledgement, AgentRevokeRequest

URL_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
MAX_RESPONSE_BYTES = 8192
MAX_GENERATION = 9223372036854775807


class HandoffLifecycleConflict(Exception):
    """A different or stale lifecycle transition already owns this agent."""


class HandoffLifecycleUnavailable(Exception):
    """Conversation did not return an exact durable revoke acknowledgement."""


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def lifecycle_command_id(scope: dict, agent_owner_id: UUID, target_generation: int) -> UUID:
    name = _canonical([
        "main-chota.handoff-lifecycle.v1", scope["owner_id"], scope["integration_id"],
        scope["channel"], scope["agent_id"], str(agent_owner_id), target_generation, "REVOKE",
    ]).decode("utf-8")
    return uuid5(URL_NAMESPACE, name)


def _configured_scope_snapshot(agent_id: UUID) -> list[dict]:
    settings = get_settings()
    grants = {
        grant.scope(): grant
        for principal in settings.managed_principals
        for grant in principal.grants
    }
    snapshot = []
    for scope in settings.handoff_scopes:
        if scope.agent_id != agent_id:
            continue
        grant = grants.get(scope.key())
        if grant is None:
            raise HandoffLifecycleUnavailable
        snapshot.append({
            "owner_id": scope.owner_id,
            "integration_id": str(scope.integration_id),
            "channel": scope.channel,
            "agent_id": str(scope.agent_id),
            "agent_owner_id": str(grant.agent_owner_id),
        })
    snapshot.sort(key=lambda value: (
        value["owner_id"], value["integration_id"], value["channel"],
        value["agent_id"], value["agent_owner_id"],
    ))
    return snapshot


def _validated_scope_snapshot(raw, agent_id: UUID) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise HandoffLifecycleUnavailable
    keys = set()
    result = []
    try:
        for item in raw:
            if not isinstance(item, dict) or set(item) != {
                    "owner_id", "integration_id", "channel", "agent_id", "agent_owner_id"}:
                raise ValueError
            value = dict(item)
            for key in ("integration_id", "agent_id", "agent_owner_id"):
                if not isinstance(value[key], str) or str(UUID(value[key])) != value[key]:
                    raise ValueError
            if (value["channel"] != "TELEGRAM" or value["agent_id"] != str(agent_id)
                    or not isinstance(value["owner_id"], str) or not value["owner_id"]):
                raise ValueError
            key = (value["owner_id"], value["integration_id"], value["channel"], value["agent_id"])
            if key in keys:
                raise ValueError
            keys.add(key)
            result.append(value)
        result.sort(key=lambda value: (
            value["owner_id"], value["integration_id"], value["channel"],
            value["agent_id"], value["agent_owner_id"],
        ))
        return result
    except Exception:
        raise HandoffLifecycleUnavailable from None


async def record_proven_scope(agent: AgentTemplate, scope, agent_owner_id: UUID,
                              session: AsyncSession) -> None:
    """Retain the exact scope/owner pair so later config removal cannot skip revoke."""
    identity = {
        "owner_id": scope.owner_id,
        "integration_id": str(scope.integration_id),
        "channel": scope.channel,
        "agent_id": str(scope.agent_id),
        "agent_owner_id": str(agent_owner_id),
    }
    scopes = _merge_scope_snapshots(
        agent.handoff_lifecycle_scope_snapshot,
        [identity],
        agent.id,
    )
    if scopes != (agent.handoff_lifecycle_scope_snapshot or []):
        agent.handoff_lifecycle_scope_snapshot = scopes
        await session.commit()


def _merge_scope_snapshots(existing, configured, agent_id: UUID) -> list[dict]:
    by_key = {}
    for item in _validated_scope_snapshot(existing, agent_id) if existing else []:
        key = (item["owner_id"], item["integration_id"], item["channel"], item["agent_id"])
        by_key[key] = item
    for item in _validated_scope_snapshot(configured, agent_id) if configured else []:
        key = (item["owner_id"], item["integration_id"], item["channel"], item["agent_id"])
        previous = by_key.get(key)
        if previous is not None and previous["agent_owner_id"] != item["agent_owner_id"]:
            # An alias-to-account change is a rebind, not permission to replace
            # the identity that must be revoked from Conversation.
            raise HandoffLifecycleConflict
        by_key[key] = item
    return sorted(by_key.values(), key=lambda value: (
        value["owner_id"], value["integration_id"], value["channel"],
        value["agent_id"], value["agent_owner_id"],
    ))


def _revoke_request(scope: dict, source_generation: int, target_generation: int) -> AgentRevokeRequest:
    owner_id = UUID(scope["agent_owner_id"])
    return AgentRevokeRequest(
        contract_version=1,
        lifecycle_source="AI_AGENT",
        command_id=lifecycle_command_id(scope, owner_id, target_generation),
        owner_id=scope["owner_id"],
        integration_id=scope["integration_id"],
        channel=scope["channel"],
        agent_id=scope["agent_id"],
        agent_owner_id=owner_id,
        source_agent_lifecycle_generation=source_generation,
        target_agent_lifecycle_generation=target_generation,
    )


async def _send_revoke(client: httpx.AsyncClient, scope: dict,
                       source_generation: int, target_generation: int) -> None:
    settings = get_settings()
    request = _revoke_request(scope, source_generation, target_generation)
    payload = request.model_dump(mode="json")
    body = _canonical(payload)
    expected_fingerprint = hashlib.sha256(body).hexdigest()
    try:
        async with client.stream(
            "POST", settings.handoff_conversation_url.rstrip("/") + "/internal/v1/inbox-grants/revoke",
            content=body,
            headers={
                "X-Service-Token": settings.handoff_lifecycle_token.get_secret_value(),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        ) as response:
            if response.status_code != 200:
                raise ValueError
            content_types = response.headers.get_list("content-type")
            encodings = response.headers.get_list("content-encoding")
            if (len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
                    or len(encodings) > 1
                    or any(value.strip().lower() != "identity" for value in encodings)):
                raise ValueError
            content_length = response.headers.get("content-length")
            if content_length is not None and (int(content_length) < 0 or int(content_length) > MAX_RESPONSE_BYTES):
                raise ValueError
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError
                data.extend(chunk)
        ack = AgentRevokeAcknowledgement.model_validate(json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        ))
        expected_echo = {
            key: value for key, value in payload.items()
        }
        actual_echo = ack.model_dump(mode="json", include=set(expected_echo))
        if (actual_echo != expected_echo or ack.request_fingerprint != expected_fingerprint
                or ack.active is not False):
            raise ValueError
    except Exception:
        raise HandoffLifecycleUnavailable from None


async def _revoke_all(scopes: list[dict], source_generation: int,
                      target_generation: int, runtime=None) -> None:
    settings = get_settings()
    if not scopes or not settings.handoff_conversation_url or not settings.handoff_lifecycle_token.get_secret_value():
        raise HandoffLifecycleUnavailable
    timeout = httpx.Timeout(settings.handoff_lifecycle_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            for scope in scopes:
                call = _send_revoke(client, scope, source_generation, target_generation)
                if runtime is not None:
                    await runtime.operation(call)
                else:
                    await call
    except HandoffLifecycleUnavailable:
        raise
    except Exception:
        raise HandoffLifecycleUnavailable from None


async def transition_agent_lifecycle(
    session: AsyncSession,
    agent_id: UUID,
    *,
    action: str,
    owner_id: UUID | None = None,
    clear_openrouter_key: bool = False,
    runtime=None,
):
    """Revoke first, then atomically deactivate/delete; pending retries replay IDs."""
    if action not in {"DEACTIVATE", "DELETE"}:
        raise ValueError("Unsupported lifecycle action")
    if clear_openrouter_key and action != "DEACTIVATE":
        raise ValueError("OpenRouter key removal requires deactivation")
    agent = await session.scalar(select(AgentTemplate).where(
        AgentTemplate.id == agent_id,
    ).with_for_update().execution_options(populate_existing=True))
    if agent is None or (owner_id is not None and agent.user_id != owner_id):
        return None
    target_state = "PENDING_DEACTIVATE" if action == "DEACTIVATE" else "PENDING_DELETE"
    if agent.handoff_lifecycle_state != "STABLE":
        if agent.handoff_lifecycle_state != target_state:
            raise HandoffLifecycleConflict
        if agent.handoff_lifecycle_source_generation is None:
            raise HandoffLifecycleUnavailable
        source_generation = agent.handoff_lifecycle_source_generation
        target_generation = agent.handoff_lifecycle_generation
        scopes = _validated_scope_snapshot(agent.handoff_lifecycle_scope_snapshot, agent.id)
    else:
        if action == "DEACTIVATE" and not agent.is_active:
            if clear_openrouter_key and agent.encrypted_openrouter_key is not None:
                agent.encrypted_openrouter_key = None
                await session.commit()
            return agent
        configured = _configured_scope_snapshot(agent.id)
        scopes = _merge_scope_snapshots(agent.handoff_lifecycle_scope_snapshot, configured, agent.id)
        if not scopes:
            if action == "DEACTIVATE":
                agent.is_active = False
                if clear_openrouter_key:
                    agent.encrypted_openrouter_key = None
                if agent.handoff_lifecycle_generation >= MAX_GENERATION:
                    raise HandoffLifecycleConflict
                agent.handoff_lifecycle_generation += 1
                await session.commit()
                return agent
            await session.delete(agent)
            await session.commit()
            return True
        if agent.handoff_lifecycle_generation >= MAX_GENERATION:
            raise HandoffLifecycleConflict
        source_generation = agent.handoff_lifecycle_generation
        target_generation = source_generation + 1
        agent.handoff_lifecycle_generation = target_generation
        agent.handoff_lifecycle_source_generation = source_generation
        agent.handoff_lifecycle_state = target_state
        agent.handoff_lifecycle_scope_snapshot = scopes
        await session.commit()

    # Pending retries already have an immutable durable transition. Release
    # their row lock before the cross-service call; the exact command is safe
    # to replay and the row is revalidated under lock after the ACK.
    if session.in_transaction():
        await session.commit()
    await _revoke_all(scopes, source_generation, target_generation, runtime)
    agent = await session.scalar(select(AgentTemplate).where(
        AgentTemplate.id == agent_id,
    ).with_for_update().execution_options(populate_existing=True))
    if agent is None and action == "DELETE":
        # An overlapping exact retry may observe the first caller's completed
        # delete after its own replayed revoke ACK.
        return True
    if (agent is not None and action == "DEACTIVATE"
            and agent.handoff_lifecycle_state == "STABLE" and not agent.is_active):
        if clear_openrouter_key and agent.encrypted_openrouter_key is not None:
            agent.encrypted_openrouter_key = None
            await session.commit()
        return agent
    if (agent is None or agent.handoff_lifecycle_state != target_state
            or agent.handoff_lifecycle_source_generation != source_generation
            or agent.handoff_lifecycle_generation != target_generation
            or _validated_scope_snapshot(agent.handoff_lifecycle_scope_snapshot, agent.id) != scopes):
        raise HandoffLifecycleConflict
    if action == "DEACTIVATE":
        agent.is_active = False
        if clear_openrouter_key:
            agent.encrypted_openrouter_key = None
        agent.handoff_lifecycle_state = "STABLE"
        agent.handoff_lifecycle_source_generation = None
        await session.commit()
        return agent
    await session.delete(agent)
    await session.commit()
    return True


async def activate_agent(session: AsyncSession, agent_id: UUID, owner_id: UUID) -> AgentTemplate | None:
    """Advance lifecycle before a newly active agent can prove/enroll again."""
    agent = await session.scalar(select(AgentTemplate).where(
        AgentTemplate.id == agent_id, AgentTemplate.user_id == owner_id,
    ).with_for_update().execution_options(populate_existing=True))
    if agent is None:
        return None
    if agent.handoff_lifecycle_state != "STABLE":
        raise HandoffLifecycleConflict
    if not agent.is_active:
        if agent.handoff_lifecycle_generation >= MAX_GENERATION:
            raise HandoffLifecycleConflict
        agent.handoff_lifecycle_generation += 1
        agent.is_active = True
    return agent
