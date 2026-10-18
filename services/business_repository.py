"""Short PostgreSQL transactions are the sole business execution authority."""

import copy
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import anyio

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.managed_security import EXECUTION_NAMESPACE
from models.agent import AgentTemplate
from models.business import (BusinessBinding, BusinessOperation, BusinessPlan, BusinessScope,
                             BusinessTurnContext, BusinessTurnPlanLink, TENANT)
from schemas.conversation_effects import (ExternalEffectPermitRequest,
                                          frozen_command_fingerprint, managed_request_fingerprint,
                                          sidecar_fingerprint)
from schemas.business import (BusinessAction, BusinessSafetyError, BusinessScopeValue,
                              DefinitiveNoEffect, DispatchAttempt, ProviderDispatch, FrozenProviderCommand, MaterialFacts,
                              ProvenFailedNoEffect, ProvenSucceeded, SucceededReceipt,
                              canonical_value, fingerprint, operation_reference)

CONFIRMATION_POLICY = "EXACT_CUSTOMER_REFERENCE_V1"


@dataclass(frozen=True)
class BindingSnapshot:
    id: UUID
    adapter_id: str
    connection_id: UUID
    authorization_generation: int
    context_generation: int
    catalog_revision: str
    enabled_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class OperationSnapshot:
    id: UUID
    ordinal: int
    status: str
    command: FrozenProviderCommand
    state_version: int
    dispatch_epoch: int
    dispatch_nonce: UUID | None
    attempt_count: int
    receipt_id: str | None
    object_id: str | None
    receipt_evidence: dict | None = None
    receipt_fingerprint: str | None = None
    effect_request_turn_id: UUID | None = None
    effect_request_fingerprint: str | None = None
    effect_managed_request_fingerprint: str | None = None
    effect_operation_fingerprint: str | None = None
    effect_requested_at: datetime | None = None
    effect_permit_id: UUID | None = None


@dataclass(frozen=True)
class PlanSnapshot:
    id: UUID
    scope: BusinessScopeValue
    origin_turn_id: UUID
    proposal_state: str
    requires_confirmation: bool
    expires_at: datetime
    operations: tuple[OperationSnapshot, ...]
    catalog_revision: str
    confirmed_turn_id: UUID | None = None
    confirmation_input_ids: tuple[str, ...] = ()
    selection_kind: str | None = None
    binding_id: UUID | None = None
    adapter_id: str | None = None
    connection_id: UUID | None = None
    authorization_generation: int | None = None
    context_generation: int | None = None
    provider_profile_revision: str | None = None

    @property
    def references(self):
        return [operation_reference(op.id, op.status) for op in self.operations]


@dataclass(frozen=True)
class DispatchToken:
    operation_id: UUID
    scope_id: UUID
    plan_id: UUID
    state_version: int
    dispatch_epoch: int
    dispatch_nonce: UUID
    command: FrozenProviderCommand
    native_replay: bool = False
    accept_before: datetime | None = None
    execution_contract: str = "0" * 64

    @property
    def dispatch(self):
        return ProviderDispatch(command=self.command,
            attempt=DispatchAttempt(operation_id=self.operation_id, state_version=self.state_version,
                dispatch_epoch=self.dispatch_epoch, dispatch_nonce=self.dispatch_nonce),
            kind="NATIVE_REPLAY" if self.native_replay else "FIRST", accept_before=self.accept_before,
            execution_contract=self.execution_contract)


@dataclass(frozen=True)
class BusinessResultGate:
    scope_exists: bool
    plan: PlanSnapshot | None
    scope_unresolved: bool
    disposition: str
    selection_kind: str | None = None
    request_conflict: bool = False

    @property
    def references(self):
        return self.plan.references if self.plan else []


@dataclass(frozen=True)
class AdmissionProbe:
    binding: BindingSnapshot
    scope_id: UUID


@dataclass(frozen=True)
class DispatchDecision:
    token: DispatchToken | None
    reason: str
    plan: PlanSnapshot


@dataclass(frozen=True)
class EffectAcquisitionDecision:
    request: ExternalEffectPermitRequest | None
    permit_id: UUID | None
    reason: str
    plan: PlanSnapshot


@dataclass(frozen=True)
class ReconcileToken:
    operation_id: UUID
    scope_id: UUID
    plan_id: UUID
    state_version: int
    reconcile_epoch: int
    reconcile_nonce: UUID
    command: FrozenProviderCommand


def check_grant(value, grant):
    if grant.scope() != (value.owner_id, value.integration_id, value.channel, value.agent_id):
        raise BusinessSafetyError("SCOPE_DENIED")


def tenant_values(value, grant):
    check_grant(value, grant)
    return dict(owner_id=value.owner_id, agent_owner_id=grant.agent_owner_id,
                agent_id=value.agent_id, integration_id=value.integration_id, channel=value.channel)


def request_fingerprint(request):
    # ManagedValue has already checked the full 256-message projection. Preserve
    # the owner's exact text rather than business-string NFC normalization.
    data = json.dumps(request.model_dump(mode="json"), ensure_ascii=False,
                      allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((EXECUTION_NAMESPACE + "\n" + data).encode()).hexdigest()


def scope_value(row):
    return BusinessScopeValue(scope_id=row.id, conversation_id=row.conversation_id,
                              **{column: getattr(row, column) for column in TENANT})


def normalized_actions(registration, actions, facts):
    if not 1 <= len(actions) <= 32:
        raise BusinessSafetyError("PLAN_BOUNDS")
    values = []
    for raw in actions:
        try:
            action = BusinessAction.model_validate(raw)
            capability = registration.capability(action.capability)
            if capability is None:
                raise ValueError
            arguments = capability.normalize(action.arguments)
            payload = canonical_value(dict(schema_version=capability.schema_version,
                                            capability=action.capability, arguments=arguments,
                                            resolved_material_facts=facts.values,
                                            expected_fact_revisions=facts.revisions))
            values.append((capability, payload, fingerprint("chota.business.action.v1", payload)))
        except Exception:
            raise BusinessSafetyError("ACTION_INVALID") from None
    if len({value[2] for value in values}) != len(values):
        raise BusinessSafetyError("DUPLICATE_ACTION")
    return values


class BusinessRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @asynccontextmanager
    async def _session(self, *, decisive=False):
        context = self.session_factory()
        session = await context.__aenter__()
        session.info["business_nowait"] = decisive
        try:
            yield session
        finally:
            # SQLAlchemy owns a shielded close task. Join its completion even
            # when the managed operation's cancellation/deadline has fired;
            # detached rollback/close is never an authorization boundary.
            with anyio.CancelScope(shield=True):
                await context.__aexit__(None, None, None)

    @staticmethod
    async def _clock(session):
        from sqlalchemy import func
        return await session.scalar(select(func.clock_timestamp()))

    @staticmethod
    async def _lock_scope(session, scope_id, *, nowait=None):
        row = await session.scalar(select(BusinessScope).where(BusinessScope.id == scope_id)
                                   .with_for_update(nowait=session.info.get("business_nowait", False) if nowait is None else nowait).execution_options(populate_existing=True))
        if row is None:
            raise BusinessSafetyError("SCOPE_DENIED")
        return row

    @staticmethod
    async def _scope_query(session, value, grant):
        values = tenant_values(value, grant)
        return await session.scalar(select(BusinessScope).filter_by(
            **values, conversation_id=value.conversation_id))

    async def _authority(self, session, value, grant, *, create_scope=False):
        values = tenant_values(value, grant)
        if session.info.get("business_nowait"):
            await session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        agent = await session.scalar(select(AgentTemplate).where(
            AgentTemplate.id == value.agent_id, AgentTemplate.user_id == grant.agent_owner_id)
            .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
        if agent is None or not agent.is_active:
            raise BusinessSafetyError("AUTHORITY_DENIED")
        from business_capabilities.connections import lock_agent_connection, validate_connection_authority
        await lock_agent_connection(session, value.agent_id)
        binding = await session.scalar(select(BusinessBinding).filter_by(**values, active=True)
                                       .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
        if binding is None:
            raise BusinessSafetyError("BINDING_UNAVAILABLE")
        if binding.adapter_id == 'chotacrm':
            await validate_connection_authority(session, binding)
        scope = await self._scope_query(session, value, grant)
        if scope is None and create_scope:
            scope = BusinessScope(id=uuid4(), conversation_id=value.conversation_id, **values)
            session.add(scope)
            await session.flush()
        if scope is None:
            raise BusinessSafetyError("SCOPE_DENIED")
        scope = await self._lock_scope(session, scope.id)
        return binding, scope

    @staticmethod
    def _binding_snapshot(binding):
        return BindingSnapshot(binding.id, binding.adapter_id, binding.connection_id,
                               binding.authorization_generation, binding.context_generation,
                               binding.catalog_revision, tuple(binding.enabled_capabilities))

    @staticmethod
    def _registration(binding, registration):
        if binding.adapter_id != registration.adapter_id or not registration.accepts_catalog(binding.catalog_revision):
            raise BusinessSafetyError("CATALOG_CONFLICT")

    async def configure_binding(self, grant, *, adapter_id, connection_id, catalog_revision,
                                enabled_capabilities, authorization_generation=1,
                                context_generation=1, active=True):
        """Trusted code-only configuration seam; no model/HTTP management API."""
        from schemas.business import Generation, Hash
        from pydantic import TypeAdapter
        TypeAdapter(Hash).validate_python(catalog_revision)
        TypeAdapter(Generation).validate_python(authorization_generation)
        TypeAdapter(Generation).validate_python(context_generation)
        if len(enabled_capabilities) > 32 or len(set(enabled_capabilities)) != len(enabled_capabilities):
            raise BusinessSafetyError("CONFIGURATION_INVALID")
        for capability in enabled_capabilities:
            BusinessAction(capability=capability, arguments={})
        values = dict(owner_id=grant.owner_id, agent_owner_id=grant.agent_owner_id,
                      agent_id=grant.agent_id, integration_id=grant.integration_id, channel=grant.channel)
        async with self._session() as session:
            async with session.begin():
                agent = await session.scalar(select(AgentTemplate).where(
                    AgentTemplate.id == grant.agent_id, AgentTemplate.user_id == grant.agent_owner_id)
                    .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
                if agent is None:
                    raise BusinessSafetyError("AUTHORITY_DENIED")
                current = await session.scalar(select(BusinessBinding).filter_by(**values, active=True)
                                               .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
                now = await self._clock(session)
                if current and current.adapter_id == adapter_id and current.connection_id == connection_id:
                    if (authorization_generation < current.authorization_generation
                            or context_generation < current.context_generation):
                        raise BusinessSafetyError("GENERATION_CONFLICT")
                    changed = (current.catalog_revision != catalog_revision or
                               current.enabled_capabilities != list(enabled_capabilities) or
                               current.active != active)
                    if changed and authorization_generation <= current.authorization_generation:
                        raise BusinessSafetyError("GENERATION_CONFLICT")
                    current.authorization_generation = authorization_generation
                    current.context_generation = context_generation
                    current.catalog_revision = catalog_revision
                    current.enabled_capabilities = list(enabled_capabilities)
                    current.active = active
                    current.updated_at = now
                    binding = current
                else:
                    if current:
                        current.active = False
                        current.updated_at = now
                        await session.flush()
                    binding = BusinessBinding(id=uuid4(), **values, adapter_id=adapter_id,
                                              connection_id=connection_id, catalog_revision=catalog_revision,
                                              enabled_capabilities=list(enabled_capabilities),
                                              authorization_generation=authorization_generation,
                                              context_generation=context_generation, active=active,
                                              created_at=now, updated_at=now)
                    session.add(binding)
                await session.flush()
                result = self._binding_snapshot(binding)
            return result

    async def _turn(self, session, scope, request):
        row = await session.get(BusinessTurnContext, (scope.id, request.turn_id))
        digest = request_fingerprint(request)
        if row:
            if row.request_fingerprint != digest:
                raise BusinessSafetyError("TURN_CONFLICT")
            return row
        row = BusinessTurnContext(scope_id=scope.id, turn_id=request.turn_id,
                                  request_fingerprint=digest, input_revision=request.input_revision,
                                  authority_revision=request.authority_revision,
                                  input_first_sequence=request.input_first_sequence,
                                  input_last_sequence=request.input_last_sequence,
                                  context_last_sequence=request.context_last_sequence,
                                  selected_input_message_ids=[str(i) for i in request.selected_input_message_ids])
        session.add(row)
        await session.flush()
        return row

    async def open_context(self, request, grant, registry):
        check_grant(request, grant)
        if not registry:
            return None
        async with self._session() as session:
            async with session.begin():
                try:
                    binding, scope = await self._authority(session, request, grant, create_scope=True)
                except BusinessSafetyError as error:
                    if error.code == "BINDING_UNAVAILABLE":
                        return None
                    raise
                registration = registry.get(binding.adapter_id)
                if registration is None:
                    raise BusinessSafetyError("CATALOG_CONFLICT")
                self._registration(binding, registration)
                await self._turn(session, scope, request)
                result = scope_value(scope), self._binding_snapshot(binding), registration
            return result

    @staticmethod
    def _command(scope, plan, operation):
        payload = copy.deepcopy(operation.request_payload)
        return FrozenProviderCommand(scope=scope_value(scope), operation_id=operation.id,
                                     request_fingerprint=operation.request_fingerprint,
                                     adapter_id=operation.adapter_id, connection_id=operation.connection_id,
                                     provider_profile_revision=operation.provider_profile_revision,
                                     issued_at=operation.issued_at,
                                     provider_idempotency_key=operation.provider_idempotency_key,
                                     capability=operation.capability, schema_version=operation.schema_version,
                                     arguments=payload["arguments"], facts=payload["resolved_material_facts"],
                                     fact_revisions=payload["expected_fact_revisions"],
                                     authorization_generation=plan.authorization_generation,
                                     context_generation=plan.context_generation)

    async def _snapshot(self, session, scope, plan, *, selection_kind=None):
        operations = (await session.scalars(select(BusinessOperation).where(
            BusinessOperation.plan_id == plan.id).order_by(BusinessOperation.ordinal)
            .execution_options(populate_existing=True))).all()
        if len(operations) != plan.action_count or [op.ordinal for op in operations] != list(range(plan.action_count)):
            raise BusinessSafetyError("INCOMPLETE_PLAN")
        return PlanSnapshot(plan.id, scope_value(scope), plan.origin_turn_id, plan.proposal_state,
                            plan.requires_confirmation, plan.expires_at,
                            tuple(OperationSnapshot(op.id, op.ordinal, op.status, self._command(scope, plan, op),
                                                    op.state_version, op.dispatch_epoch, op.dispatch_nonce,
                                                    op.attempt_count, op.receipt_id, op.object_id,
                                                    copy.deepcopy(op.receipt_evidence), op.receipt_fingerprint,
                                                    op.effect_request_turn_id, op.effect_request_fingerprint,
                                                    op.effect_managed_request_fingerprint,
                                                    op.effect_operation_fingerprint, op.effect_requested_at,
                                                    op.effect_permit_id)
                                  for op in operations), plan.catalog_revision, plan.confirmed_turn_id,
                            tuple(plan.confirmation_input_ids or ()), selection_kind,
                            plan.binding_id, plan.adapter_id, plan.connection_id,
                            plan.authorization_generation, plan.context_generation,
                            plan.provider_profile_revision)

    @staticmethod
    async def _unresolved(session, scope_id):
        return (await session.scalars(select(BusinessOperation.id).where(
            BusinessOperation.scope_id == scope_id,
            (BusinessOperation.status.in_(("DISPATCHING", "UNKNOWN")) |
             ((BusinessOperation.status == "RESERVED") & BusinessOperation.effect_request_turn_id.is_not(None)))))).all()

    @staticmethod
    async def _resolve_fresh(facts_resolver):
        if not callable(facts_resolver):
            raise BusinessSafetyError("FACTS_RESOLVER_REQUIRED")
        facts = await facts_resolver()
        if not isinstance(facts, MaterialFacts):
            raise BusinessSafetyError("FACTS_INVALID")
        # Defensive validation for objects built without normal Pydantic checks.
        return MaterialFacts.model_validate(facts.model_dump(mode="python"))

    async def _admission_probe(self, request, grant, plan_id=None):
        # Managed owners supply their shorter deadline; standalone trusted calls
        # also have a finite admission budget. There is one probe, never a loop.
        with anyio.fail_after(30):
            return await self._probe_transaction(request, grant, plan_id)

    async def _probe_transaction(self, request, grant, plan_id=None):
        async with self._session() as session:
            async with session.begin():
                binding, scope = await self._authority(session, request, grant)
                context = await session.get(BusinessTurnContext, (scope.id, request.turn_id))
                if context and context.request_fingerprint != request_fingerprint(request):
                    raise BusinessSafetyError("TURN_CONFLICT")
                link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                selected_id = plan_id or (link.plan_id if link else None)
                if selected_id is not None:
                    plan = await session.scalar(select(BusinessPlan).where(
                        BusinessPlan.id == selected_id, BusinessPlan.scope_id == scope.id)
                        .with_for_update().execution_options(populate_existing=True))
                    if plan is None:
                        raise BusinessSafetyError("PLAN_DENIED")
                    await session.scalars(select(BusinessOperation).where(BusinessOperation.plan_id == plan.id)
                        .order_by(BusinessOperation.ordinal).with_for_update()
                        .execution_options(populate_existing=True))
                    await self._snapshot(session, scope, plan)
                await self._clock(session)
                result = AdmissionProbe(self._binding_snapshot(binding), scope.id)
            return result

    @staticmethod
    def _check_probe(binding, scope, probe):
        if BusinessRepository._binding_snapshot(binding) != probe.binding or scope.id != probe.scope_id:
            raise BusinessSafetyError("STALE_ADMISSION", processing_status="UNKNOWN")

    @staticmethod
    def _contention(error):
        original = getattr(error, "orig", None)
        return (isinstance(error, IntegrityError) or
                getattr(original, "sqlstate", None) in {"55P03", "57014", "40P01"} or
                getattr(getattr(original, "__cause__", None), "sqlstate", None) in {"55P03", "57014", "40P01"})

    async def reserve_plan(self, request, grant, registration, actions, facts_resolver, *, propose=False):
        initial = await self._resolve_fresh(facts_resolver)
        probe = await self._admission_probe(request, grant)
        fresh = await self._resolve_fresh(facts_resolver)
        if initial != fresh:
            raise BusinessSafetyError("STALE_PREPARATION")
        try:
            return await self._reserve_plan(request, grant, registration, actions, fresh,
                                            propose=propose, probe=probe)
        except DBAPIError as error:
            if not self._contention(error):
                raise
            raise BusinessSafetyError("ADMISSION_CONTENDED", processing_status="UNKNOWN") from None

    async def _reserve_plan(self, request, grant, registration, actions, facts, *, propose=False, probe):
        normalized = normalized_actions(registration, actions, facts)
        requires = propose or any(cap.requires_confirmation for cap, _, _ in normalized)
        async with self._session(decisive=True) as session:
            async with session.begin():
                binding, scope = await self._authority(session, request, grant)
                self._check_probe(binding, scope, probe)
                self._registration(binding, registration)
                await self._turn(session, scope, request)
                if any(cap.name not in binding.enabled_capabilities for cap, _, _ in normalized):
                    raise BusinessSafetyError("CAPABILITY_DISABLED")
                digest = fingerprint("chota.business.plan.v1", {
                    "actions": [{"capability": cap.name, "fingerprint": digest} for cap, _, digest in normalized],
                    "requires_confirmation": requires, "binding_id": binding.id,
                    "authorization_generation": binding.authorization_generation,
                    "context_generation": binding.context_generation, "catalog_revision": binding.catalog_revision,
                    "facts": facts.model_dump(mode="python"),
                })
                link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                if link:
                    if link.selection_kind == "RECOVERY":
                        raise BusinessSafetyError("RECOVERY_READ_ONLY", processing_status="UNKNOWN")
                    plan = await session.scalar(select(BusinessPlan).where(BusinessPlan.id == link.plan_id)
                                                .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
                    if link.selection_kind != "OWN" or plan.origin_turn_id != request.turn_id or plan.plan_fingerprint != digest:
                        raise BusinessSafetyError("PLAN_CONFLICT")
                    result = await self._snapshot(session, scope, plan)
                else:
                    if await self._unresolved(session, scope.id):
                        raise BusinessSafetyError("BLOCKED_UNRESOLVED_EFFECT", processing_status="UNKNOWN")
                    now = await self._clock(session)
                    plan = BusinessPlan(id=uuid4(), scope_id=scope.id, origin_turn_id=request.turn_id,
                                        **tenant_values(request, grant), binding_id=binding.id,
                                        adapter_id=binding.adapter_id, connection_id=binding.connection_id,
                                        provider_profile_revision=registration.profile.profile_revision,
                                        authorization_generation=binding.authorization_generation,
                                        context_generation=binding.context_generation,
                                        catalog_revision=binding.catalog_revision,
                                        facts_fingerprint=fingerprint("chota.business.facts.v1", facts.model_dump(mode="python")),
                                        fact_revisions=copy.deepcopy(facts.revisions), plan_fingerprint=digest,
                                        action_count=len(normalized), requires_confirmation=requires,
                                        proposal_state="PREPARED", expires_at=now + timedelta(seconds=registration.profile.proposal_ttl_seconds),
                                        created_at=now)
                    session.add(plan)
                    await session.flush()
                    for ordinal, (capability, payload, action_digest) in enumerate(normalized):
                        operation_id = uuid4()
                        policy = registration.profile.native_replay_policy
                        session.add(BusinessOperation(
                            id=operation_id, plan_id=plan.id, scope_id=scope.id, origin_turn_id=request.turn_id,
                            ordinal=ordinal, capability=capability.name, schema_version=capability.schema_version,
                            request_payload=payload, request_fingerprint=action_digest,
                            adapter_id=plan.adapter_id, connection_id=plan.connection_id,
                            provider_profile_revision=plan.provider_profile_revision,
                            provider_idempotency_key=f"chota-g3:{operation_id}" if policy != "NONE" else None,
                            native_replay_policy=policy,
                            native_replay_until=now + timedelta(seconds=registration.profile.native_minimum_retention_seconds)
                            if policy == "BOUNDED" else None,
                            issued_at=now, status="RESERVED", state_version=0, dispatch_epoch=0, attempt_count=0,
                            reconcile_state="NONE", reconcile_epoch=0, created_at=now, updated_at=now,
                        ))
                    session.add(BusinessTurnPlanLink(scope_id=scope.id, turn_id=request.turn_id,
                                                     plan_id=plan.id, selection_kind="OWN", created_at=now))
                    await session.flush()
                    result = await self._snapshot(session, scope, plan)
            # Exiting begin positively joins COMMIT before any snapshot escapes.
            return result

    async def prepare_effect_acquisition(self, request, grant, plan_id, operation_id, registration,
                                         facts_resolver):
        """Commit the exact permit request before crossing the sidecar boundary."""
        probe = await self._admission_probe(request, grant, plan_id)
        facts = await self._resolve_fresh(facts_resolver)
        try:
            async with self._session(decisive=True) as session:
                async with session.begin():
                    binding, scope = await self._authority(session, request, grant)
                    self._check_probe(binding, scope, probe)
                    self._registration(binding, registration)
                    context = await self._turn(session, scope, request)
                    plan = await session.scalar(select(BusinessPlan).where(
                        BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id)
                        .with_for_update(nowait=True).execution_options(populate_existing=True))
                    link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                    if plan is None or link is None or link.plan_id != plan_id:
                        raise BusinessSafetyError("PLAN_DENIED")
                    if link.selection_kind not in {"OWN", "CONFIRM"}:
                        raise BusinessSafetyError("RECOVERY_READ_ONLY", processing_status="UNKNOWN")
                    origin = await session.get(BusinessTurnContext, (scope.id, plan.origin_turn_id))
                    if (origin is None or context.authority_revision != request.authority_revision
                            or origin.authority_revision != request.authority_revision):
                        raise BusinessSafetyError("STALE_CONFIRMATION")
                    operations = (await session.scalars(select(BusinessOperation).where(
                        BusinessOperation.plan_id == plan_id).order_by(BusinessOperation.ordinal)
                        .with_for_update(nowait=True).execution_options(populate_existing=True))).all()
                    operation = next((value for value in operations if value.id == operation_id), None)
                    if operation is None:
                        raise BusinessSafetyError("OPERATION_DENIED")
                    if len(operations) != plan.action_count or [op.ordinal for op in operations] != list(range(plan.action_count)):
                        raise BusinessSafetyError("INCOMPLETE_PLAN")
                    if operation.status != "RESERVED":
                        result = EffectAcquisitionDecision(None, operation.effect_permit_id, "NOT_RESERVED",
                                                           await self._snapshot(session, scope, plan,
                                                               selection_kind=link.selection_kind))
                    elif any(op.status != "SUCCEEDED" for op in operations[:operation.ordinal]):
                        raise BusinessSafetyError("BLOCKED_PREDECESSOR")
                    else:
                        unresolved = [identity for identity in await self._unresolved(session, scope.id)
                                      if identity != operation.id]
                        if unresolved:
                            raise BusinessSafetyError("BLOCKED_UNRESOLVED_EFFECT", processing_status="UNKNOWN")
                        now = await self._clock(session)
                        frozen = await self._snapshot(session, scope, plan,
                                                       selection_kind=link.selection_kind)
                        current = (self._current(plan, binding, registration, facts, now)
                                   and all(op.command.capability in binding.enabled_capabilities
                                           for op in frozen.operations)
                                   and (not plan.requires_confirmation
                                        or (plan.proposal_state == "CONFIRMED"
                                            and plan.confirmed_turn_id == request.turn_id)))
                        if not current:
                            # Never invalidate acquisition evidence or an attempted effect.
                            raise BusinessSafetyError("AUTHORITY_DENIED")
                        command = self._command(scope, plan, operation)
                        effect_request = ExternalEffectPermitRequest(
                            contract_version=1,
                            conversation_id=request.conversation_id,
                            turn_id=request.turn_id,
                            origin_turn_id=plan.origin_turn_id,
                            owner_id=request.owner_id,
                            integration_id=request.integration_id,
                            channel=request.channel,
                            agent_id=request.agent_id,
                            authority_revision=context.authority_revision,
                            managed_request_fingerprint=managed_request_fingerprint(request),
                            operation_namespace="main-chota.ai-business.v1",
                            operation_id=operation.id,
                            operation_fingerprint=frozen_command_fingerprint(command),
                        )
                        request_digest = sidecar_fingerprint(effect_request)
                        retained = (operation.effect_request_turn_id, operation.effect_request_fingerprint,
                                    operation.effect_managed_request_fingerprint,
                                    operation.effect_operation_fingerprint)
                        proposed = (request.turn_id, request_digest,
                                    effect_request.managed_request_fingerprint,
                                    effect_request.operation_fingerprint)
                        if operation.effect_request_turn_id is None:
                            operation.effect_request_turn_id = request.turn_id
                            operation.effect_request_fingerprint = request_digest
                            operation.effect_managed_request_fingerprint = effect_request.managed_request_fingerprint
                            operation.effect_operation_fingerprint = effect_request.operation_fingerprint
                            operation.effect_requested_at = now
                            operation.updated_at = now
                        elif retained != proposed:
                            raise BusinessSafetyError("EFFECT_REQUEST_CONFLICT", processing_status="UNKNOWN")
                        await session.flush()
                        result = EffectAcquisitionDecision(
                            effect_request, operation.effect_permit_id,
                            "GRANTED" if operation.effect_permit_id else "REQUESTED",
                            await self._snapshot(session, scope, plan, selection_kind=link.selection_kind))
                return result
        except DBAPIError as error:
            if not self._contention(error):
                raise
            raise BusinessSafetyError("ADMISSION_CONTENDED", processing_status="UNKNOWN") from None

    async def bind_effect_permit(self, request, grant, plan_id, operation_id,
                                 effect_request: ExternalEffectPermitRequest, permit_id: UUID):
        """Bind one immutable positive sidecar result while the operation is still RESERVED."""
        async with self._session() as session:
            async with session.begin():
                scope, plan, operation = await self._retained(session, request, grant, plan_id, operation_id)
                expected = sidecar_fingerprint(effect_request)
                if (operation.effect_request_turn_id != request.turn_id
                        or operation.effect_request_fingerprint != expected
                        or operation.effect_managed_request_fingerprint != effect_request.managed_request_fingerprint
                        or operation.effect_operation_fingerprint != effect_request.operation_fingerprint):
                    raise BusinessSafetyError("EFFECT_REQUEST_CONFLICT", processing_status="UNKNOWN")
                if operation.effect_permit_id is not None and operation.effect_permit_id != permit_id:
                    raise BusinessSafetyError("EFFECT_PERMIT_CONFLICT", processing_status="UNKNOWN")
                if operation.status == "RESERVED" and operation.effect_permit_id is None:
                    operation.effect_permit_id = permit_id
                    operation.updated_at = await self._clock(session)
                    await session.flush()
                snapshot = await self._snapshot(session, scope, plan)
            return snapshot

    async def deny_effect_acquisition(self, request, grant, plan_id, operation_id, reason):
        """Persist exact sidecar denial as a definitive original-operation NO_DISPATCH."""
        if not isinstance(reason, str) or not reason.startswith("EXTERNAL_EFFECT_"):
            raise BusinessSafetyError("EFFECT_DENIAL_INVALID")
        async with self._session() as session:
            async with session.begin():
                scope, plan, operation = await self._retained(session, request, grant, plan_id, operation_id)
                now = await self._clock(session)
                if operation.effect_request_turn_id != request.turn_id:
                    raise BusinessSafetyError("EFFECT_REQUEST_CONFLICT", processing_status="UNKNOWN")
                if operation.status == "RESERVED":
                    operation.status = "FAILED"
                    operation.state_version += 1
                    operation.failure_code = reason
                    operation.receipt_id = "no-dispatch:" + str(operation.id)
                    operation.receipt_evidence = {"kind": "NO_DISPATCH", "reason_code": reason}
                    operation.receipt_fingerprint = fingerprint("chota.business.receipt.v1", operation.receipt_evidence)
                    operation.terminal_at = now
                    operation.updated_at = now
                    plan.proposal_state = "INVALIDATED"
                    await session.flush()
                result = await self._snapshot(session, scope, plan)
            return result

    async def result_gate(self, request, grant):
        async with self._session() as session:
            async with session.begin():
                scope = await self._scope_query(session, request, grant)
                if scope is None:
                    result = BusinessResultGate(False, None, False, "NO_PLAN", None, False)
                else:
                    scope = await self._lock_scope(session, scope.id)
                    context = await session.get(BusinessTurnContext, (scope.id, request.turn_id))
                    request_conflict = bool(context and context.request_fingerprint != request_fingerprint(request))
                    # Retained read permission survives a conflicting replay.
                    # Preserve its genuine refs, but grant no ordinary draft.
                    link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                    snapshot = None
                    if link:
                        plan = await session.scalar(select(BusinessPlan).where(
                            BusinessPlan.id == link.plan_id, BusinessPlan.scope_id == scope.id)
                            .with_for_update().execution_options(populate_existing=True))
                        if plan is None:
                            raise BusinessSafetyError("INCOMPLETE_PLAN")
                        await session.scalars(select(BusinessOperation).where(BusinessOperation.plan_id == plan.id)
                            .order_by(BusinessOperation.ordinal).with_for_update()
                            .execution_options(populate_existing=True))
                        snapshot = await self._snapshot(session, scope, plan,
                                                        selection_kind=link.selection_kind)
                    unresolved = bool(await self._unresolved(session, scope.id))
                    now = await self._clock(session)
                    if request_conflict:
                        disposition = "UNAVAILABLE"
                    elif unresolved:
                        disposition = "UNKNOWN"
                    elif snapshot is None:
                        disposition = "NO_PLAN"
                    elif any(op.status == "FAILED" for op in snapshot.operations):
                        disposition = "FAILED"
                    elif all(op.status == "SUCCEEDED" for op in snapshot.operations):
                        disposition = "ALL_SUCCEEDED"
                    elif (snapshot.requires_confirmation and snapshot.proposal_state == "PREPARED"
                          and snapshot.expires_at > now and all(op.status == "RESERVED" for op in snapshot.operations)):
                        disposition = "AWAITING_CONFIRMATION"
                    else:
                        disposition = "INCOMPLETE"
                    result = BusinessResultGate(True, snapshot, unresolved, disposition,
                                                link.selection_kind if link else None, request_conflict)
            return result

    async def selected_plan(self, request, grant):
        async with self._session() as session:
            scope = await self._scope_query(session, request, grant)
            if scope is None:
                return None
            link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
            if link is None:
                return None
            plan = await session.get(BusinessPlan, link.plan_id)
            return await self._snapshot(session, scope, plan, selection_kind=link.selection_kind)

    async def latest_completed_booking(self, request, grant, registration):
        """Return a historical lookup hint, without selecting or authorizing a plan."""
        async with self._session() as session:
            async with session.begin():
                binding, scope = await self._authority(session, request, grant)
                self._registration(binding, registration)
                await self._turn(session, scope, request)
                if binding.adapter_id != 'chotacrm' or 'booking.get' not in binding.enabled_capabilities:
                    return None
                plan = await self._latest_plan(session, scope.id)
                if (plan is None or plan.proposal_state != 'CONFIRMED'
                        or not plan.requires_confirmation or plan.confirmed_turn_id is None
                        or plan.binding_id != binding.id or plan.connection_id != binding.connection_id
                        or plan.authorization_generation != binding.authorization_generation
                        or plan.context_generation != binding.context_generation
                        or plan.catalog_revision != binding.catalog_revision
                        or await self._unresolved(session, scope.id)):
                    return None
                confirmed = await session.get(BusinessTurnContext, (scope.id, plan.confirmed_turn_id))
                if (confirmed is None or confirmed.authority_revision != request.authority_revision
                        or confirmed.input_last_sequence >= request.input_first_sequence):
                    return None
                snapshot = await self._snapshot(session, scope, plan)
                if (len(snapshot.operations) != 1 or snapshot.operations[0].status != 'SUCCEEDED'
                        or snapshot.operations[0].command.capability not in {
                            'booking.create', 'booking.reschedule', 'booking.cancel'}):
                    return None
                return snapshot

    async def lookup_plan(self, request, grant, plan_id):
        async with self._session() as session:
            scope = await self._scope_query(session, request, grant)
            if scope is None:
                return None
            plan = await session.scalar(select(BusinessPlan).where(
                BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id))
            return await self._snapshot(session, scope, plan) if plan else None

    @staticmethod
    async def _latest_plan(session, scope_id):
        # Canonical input order wins over worker timing. Include terminal and
        # cancelled plans: a newer intent must not resurrect an older proposal.
        return await session.scalar(select(BusinessPlan).join(BusinessTurnContext,
            (BusinessTurnContext.scope_id == BusinessPlan.scope_id) &
            (BusinessTurnContext.turn_id == BusinessPlan.origin_turn_id))
            .where(BusinessPlan.scope_id == scope_id)
            .order_by(BusinessTurnContext.input_last_sequence.desc(),
                      BusinessPlan.created_at.desc(), BusinessPlan.id.desc()).limit(1))

    @staticmethod
    def _semantic_inputs(request, origin):
        selected = request.selected_messages
        if (origin is None or request.turn_id == origin.turn_id
                or origin.authority_revision != request.authority_revision
                or request.input_first_sequence <= origin.input_last_sequence
                or not selected or any(m.actor != "CUSTOMER" or m.direction != "INBOUND"
                    or m.content_type != "TEXT" or str(m.id) in origin.selected_input_message_ids
                    for m in selected)):
            raise BusinessSafetyError("CONFIRMATION_REQUIRED")
        return selected

    @staticmethod
    def _unacquired_reservations(operations, action_count):
        """Return true only for the untouched RESERVED rows of a fresh proposal."""
        if (len(operations) != action_count
                or [operation.ordinal for operation in operations] != list(range(action_count))):
            return False
        return all(
            operation.status == "RESERVED"
            and operation.state_version == 0
            and operation.dispatch_epoch == 0
            and operation.dispatch_nonce is None
            and operation.dispatch_started_at is None
            and operation.dispatch_lease_expires_at is None
            and operation.attempt_count == 0
            and operation.receipt_id is None
            and operation.object_id is None
            and operation.receipt_fingerprint is None
            and operation.receipt_evidence is None
            and operation.terminal_at is None
            and operation.failure_code is None
            and operation.reconcile_state == "NONE"
            and operation.reconcile_epoch == 0
            and operation.reconcile_nonce is None
            and operation.reconcile_lease_expires_at is None
            and operation.last_reconciled_at is None
            and operation.effect_request_turn_id is None
            and operation.effect_request_fingerprint is None
            and operation.effect_managed_request_fingerprint is None
            and operation.effect_operation_fingerprint is None
            and operation.effect_requested_at is None
            and operation.effect_permit_id is None
            for operation in operations
        )

    @staticmethod
    def _stale_split_tombstone(plan, operations):
        """Validate the exact retained retirement marker, never arbitrary failures."""
        if (plan.proposal_state != "INVALIDATED" or not plan.requires_confirmation
                or plan.confirmed_turn_id is not None or plan.confirmation_input_ids is not None
                or plan.confirmation_policy is not None or plan.confirmed_at is not None
                or len(operations) != plan.action_count
                or [operation.ordinal for operation in operations] != list(range(plan.action_count))):
            return False
        return all(
            operation.status == "FAILED"
            and operation.state_version == 1
            and operation.dispatch_epoch == 0
            and operation.dispatch_nonce is None
            and operation.dispatch_started_at is None
            and operation.dispatch_lease_expires_at is None
            and operation.attempt_count == 0
            and operation.receipt_id == "no-dispatch:" + str(operation.id)
            and operation.object_id is None
            and operation.failure_code == "STALE_INTERVENING_SPLIT"
            and operation.receipt_evidence == {
                "kind": "NO_DISPATCH", "reason_code": "STALE_INTERVENING_SPLIT"}
            and operation.receipt_fingerprint == fingerprint(
                "chota.business.receipt.v1",
                {"kind": "NO_DISPATCH", "reason_code": "STALE_INTERVENING_SPLIT"},
            )
            and operation.terminal_at is not None
            and operation.reconcile_state == "NONE"
            and operation.reconcile_epoch == 0
            and operation.reconcile_nonce is None
            and operation.reconcile_lease_expires_at is None
            and operation.last_reconciled_at is None
            and operation.effect_request_turn_id is None
            and operation.effect_request_fingerprint is None
            and operation.effect_managed_request_fingerprint is None
            and operation.effect_operation_fingerprint is None
            and operation.effect_requested_at is None
            and operation.effect_permit_id is None
            for operation in operations
        )

    @staticmethod
    def _stale_split_plan_is_current(plan, binding, registration):
        return (
            plan.binding_id == binding.id
            and plan.adapter_id == binding.adapter_id == registration.adapter_id
            and plan.connection_id == binding.connection_id
            and plan.authorization_generation == binding.authorization_generation
            and plan.context_generation == binding.context_generation
            and plan.catalog_revision == binding.catalog_revision
            and registration.accepts_catalog(plan.catalog_revision)
            and registration.profile.profile_revision == plan.provider_profile_revision
        )

    async def stale_split_proposal_context(self, request, grant, registration):
        """Select only the latest semantic proposal or this branch's exact tombstone.

        Expiry is intentionally ignored so an expired stale proposal remains a
        semantic barrier. This projection never links the old plan to the current
        Turn and is never used as executable authority.
        """
        if getattr(registration, "confirmation_mode", "REFERENCE_V1") != "SEMANTIC_V1":
            return None
        async with self._session() as session:
            async with session.begin():
                binding, scope = await self._authority(session, request, grant)
                self._registration(binding, registration)
                await self._turn(session, scope, request)
                if (await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                        is not None or await self._unresolved(session, scope.id)):
                    return None
                plan = await self._latest_plan(session, scope.id)
                if (plan is None or not plan.requires_confirmation
                        or not self._stale_split_plan_is_current(plan, binding, registration)):
                    return None
                origin = await session.get(BusinessTurnContext, (scope.id, plan.origin_turn_id))
                if origin is None or origin.authority_revision != request.authority_revision:
                    return None
                try:
                    self._semantic_inputs(request, origin)
                except BusinessSafetyError:
                    return None
                operations = (await session.scalars(select(BusinessOperation).where(
                    BusinessOperation.plan_id == plan.id).order_by(BusinessOperation.ordinal)
                    .execution_options(populate_existing=True))).all()
                if plan.proposal_state == "PREPARED":
                    if not self._unacquired_reservations(operations, plan.action_count):
                        return None
                elif not self._stale_split_tombstone(plan, operations):
                    return None
                snapshot = await self._snapshot(session, scope, plan)
                if any(operation.command.capability not in binding.enabled_capabilities
                       for operation in snapshot.operations):
                    return None
                return snapshot

    async def retire_stale_split_proposal(self, request, grant, plan_id, registration):
        """Atomically retire one latest, unacquired split-stale proposal.

        No network call occurs under these locks. A same-request replay may
        positively observe this exact tombstone, but it does not persist a
        reusable replacement authorization.
        """
        if getattr(registration, "confirmation_mode", "REFERENCE_V1") != "SEMANTIC_V1":
            raise BusinessSafetyError("PLAN_DENIED")
        async with self._session(decisive=True) as session:
            async with session.begin():
                binding, scope = await self._authority(session, request, grant)
                self._registration(binding, registration)
                await self._turn(session, scope, request)
                if await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id)) is not None:
                    raise BusinessSafetyError("PLAN_CONFLICT")
                if await self._unresolved(session, scope.id):
                    raise BusinessSafetyError("BLOCKED_UNRESOLVED_EFFECT", processing_status="UNKNOWN")
                latest = await self._latest_plan(session, scope.id)
                if latest is None or latest.id != plan_id:
                    raise BusinessSafetyError("STALE_CONFIRMATION")
                plan = await session.scalar(select(BusinessPlan).where(
                    BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id)
                    .with_for_update(nowait=session.info.get("business_nowait", False))
                    .execution_options(populate_existing=True))
                if (plan is None or not plan.requires_confirmation
                        or not self._stale_split_plan_is_current(plan, binding, registration)):
                    raise BusinessSafetyError("STALE_CONFIRMATION")
                origin = await session.get(BusinessTurnContext, (scope.id, plan.origin_turn_id))
                if origin is None or origin.authority_revision != request.authority_revision:
                    raise BusinessSafetyError("STALE_CONFIRMATION")
                self._semantic_inputs(request, origin)
                operations = (await session.scalars(select(BusinessOperation).where(
                    BusinessOperation.plan_id == plan.id).order_by(BusinessOperation.ordinal)
                    .with_for_update(nowait=session.info.get("business_nowait", False))
                    .execution_options(populate_existing=True))).all()
                if self._stale_split_tombstone(plan, operations):
                    result = await self._snapshot(session, scope, plan)
                else:
                    if (plan.proposal_state != "PREPARED"
                            or not self._unacquired_reservations(operations, plan.action_count)):
                        raise BusinessSafetyError("PLAN_DENIED")
                    now = await self._clock(session)
                    plan.proposal_state = "INVALIDATED"
                    for operation in operations:
                        evidence = {"kind": "NO_DISPATCH", "reason_code": "STALE_INTERVENING_SPLIT"}
                        operation.status = "FAILED"
                        operation.state_version += 1
                        operation.failure_code = "STALE_INTERVENING_SPLIT"
                        operation.receipt_id = "no-dispatch:" + str(operation.id)
                        operation.receipt_evidence = evidence
                        operation.receipt_fingerprint = fingerprint("chota.business.receipt.v1", evidence)
                        operation.terminal_at = now
                        operation.updated_at = now
                    await session.flush()
                    result = await self._snapshot(session, scope, plan)
            return result

    async def pending_proposal(self, request, grant, registration):
        """Read pending public context without selecting its refs for this Turn."""
        async with self._session() as session:
            async with session.begin():
                binding, scope = await self._authority(session, request, grant)
                self._registration(binding, registration)
                await self._turn(session, scope, request)
                plan = await self._latest_plan(session, scope.id)
                now = await self._clock(session)
                if (plan is None or plan.proposal_state != "PREPARED"
                        or not plan.requires_confirmation or plan.expires_at <= now
                        or plan.binding_id != binding.id
                        or plan.authorization_generation != binding.authorization_generation
                        or plan.context_generation != binding.context_generation
                        or plan.catalog_revision != binding.catalog_revision
                        or await self._unresolved(session, scope.id)):
                    return None
                origin = await session.get(BusinessTurnContext, (scope.id, plan.origin_turn_id))
                if origin is None or origin.authority_revision != request.authority_revision:
                    return None
                try:
                    self._semantic_inputs(request, origin)
                except BusinessSafetyError:
                    return None
                snapshot = await self._snapshot(session, scope, plan)
                if any(op.status != "RESERVED" or op.command.capability not in binding.enabled_capabilities
                       for op in snapshot.operations):
                    return None
                return snapshot

    async def cancel_proposal(self, request, grant, plan_id, registration):
        """Cancel only an undispatched frozen proposal; never rewrite its actions."""
        async with self._session(decisive=True) as session:
            async with session.begin():
                binding, scope = await self._authority(session, request, grant)
                self._registration(binding, registration)
                await self._turn(session, scope, request)
                plan = await session.scalar(select(BusinessPlan).where(
                    BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id)
                    .with_for_update(nowait=True).execution_options(populate_existing=True))
                if plan is None:
                    raise BusinessSafetyError("PLAN_DENIED")
                origin = await session.get(BusinessTurnContext, (scope.id, plan.origin_turn_id))
                if origin is None or origin.authority_revision != request.authority_revision:
                    raise BusinessSafetyError("STALE_CONFIRMATION")
                self._semantic_inputs(request, origin)
                link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                if link is not None and link.selection_kind == "RECOVERY":
                    raise BusinessSafetyError("RECOVERY_READ_ONLY", processing_status="UNKNOWN")
                latest = await self._latest_plan(session, scope.id)
                if latest is None or latest.id != plan.id:
                    raise BusinessSafetyError("STALE_CONFIRMATION")
                snapshot = await self._snapshot(session, scope, plan)
                if plan.proposal_state not in {"PREPARED", "CANCELLED"} or any(
                        op.status != "RESERVED" for op in snapshot.operations):
                    raise BusinessSafetyError("PLAN_DENIED")
                if await self._unresolved(session, scope.id):
                    raise BusinessSafetyError("BLOCKED_UNRESOLVED_EFFECT", processing_status="UNKNOWN")
                plan.proposal_state = "CANCELLED"
                # No link: this Turn can now freeze a replacement, but cannot
                # confirm it until a later canonical customer Turn.
                await session.flush()

    async def select_recovery_plan(self, request, grant, plan_id):
        async with self._session() as session:
            async with session.begin():
                scope = await self._scope_query(session, request, grant)
                if scope is None:
                    raise BusinessSafetyError("SCOPE_DENIED")
                scope = await self._lock_scope(session, scope.id)
                await self._turn(session, scope, request)
                plan = await session.scalar(select(BusinessPlan).where(
                    BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id).with_for_update(nowait=session.info.get("business_nowait", False)))
                if plan is None:
                    raise BusinessSafetyError("PLAN_DENIED")
                link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                if link and link.plan_id != plan_id:
                    raise BusinessSafetyError("PLAN_CONFLICT")
                if link is None:
                    session.add(BusinessTurnPlanLink(scope_id=scope.id, turn_id=request.turn_id,
                                                     plan_id=plan_id, selection_kind="RECOVERY"))
                result = await self._snapshot(session, scope, plan)
            return result

    async def select_unresolved_recovery(self, request, grant):
        """Durably make later Turns read-only observers of the original blocker."""
        async with self._session() as session:
            async with session.begin():
                scope = await self._scope_query(session, request, grant)
                if scope is None:
                    return None
                scope = await self._lock_scope(session, scope.id)
                await self._turn(session, scope, request)
                link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                unresolved_id = await session.scalar(select(BusinessOperation.id).where(
                    BusinessOperation.scope_id == scope.id,
                    (BusinessOperation.status.in_(("DISPATCHING", "UNKNOWN")) |
                     ((BusinessOperation.status == "RESERVED") &
                      BusinessOperation.effect_request_turn_id.is_not(None))))
                    .order_by(BusinessOperation.created_at, BusinessOperation.ordinal).limit(1))
                if unresolved_id is None:
                    return await self._snapshot(session, scope,
                        await session.get(BusinessPlan, link.plan_id), selection_kind=link.selection_kind) if link else None
                operation = await session.scalar(select(BusinessOperation).where(
                    BusinessOperation.id == unresolved_id)
                    .with_for_update(nowait=session.info.get("business_nowait", False))
                    .execution_options(populate_existing=True))
                plan = await session.scalar(select(BusinessPlan).where(
                    BusinessPlan.id == operation.plan_id, BusinessPlan.scope_id == scope.id)
                    .with_for_update(nowait=session.info.get("business_nowait", False))
                    .execution_options(populate_existing=True))
                if plan is None:
                    raise BusinessSafetyError("INCOMPLETE_PLAN")
                if link is None:
                    link = BusinessTurnPlanLink(scope_id=scope.id, turn_id=request.turn_id,
                        plan_id=plan.id, selection_kind="RECOVERY", created_at=await self._clock(session))
                    session.add(link)
                elif link.selection_kind == "RECOVERY" and link.plan_id != plan.id:
                    raise BusinessSafetyError("RECOVERY_CONFLICT", processing_status="UNKNOWN")
                await session.flush()
                result = await self._snapshot(session, scope, plan, selection_kind=link.selection_kind)
            return result

    async def close_recovery_acquisition(self, request, grant):
        """Fence a not-yet-dispatched REQUESTED/GRANTED operation on its scope lock."""
        async with self._session() as session:
            async with session.begin():
                scope = await self._scope_query(session, request, grant)
                if scope is None:
                    return None
                scope = await self._lock_scope(session, scope.id)
                await self._turn(session, scope, request)
                link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                if link is None or link.selection_kind != "RECOVERY":
                    return None
                plan = await session.scalar(select(BusinessPlan).where(
                    BusinessPlan.id == link.plan_id, BusinessPlan.scope_id == scope.id)
                    .with_for_update(nowait=session.info.get("business_nowait", False))
                    .execution_options(populate_existing=True))
                if plan is None:
                    raise BusinessSafetyError("INCOMPLETE_PLAN")
                operation = await session.scalar(select(BusinessOperation).where(
                    BusinessOperation.plan_id == plan.id,
                    BusinessOperation.status == "RESERVED",
                    BusinessOperation.effect_request_turn_id.is_not(None))
                    .order_by(BusinessOperation.ordinal).limit(1)
                    .with_for_update(nowait=session.info.get("business_nowait", False))
                    .execution_options(populate_existing=True))
                if operation is not None and operation.status == "RESERVED":
                    now = await self._clock(session)
                    operation.status = "FAILED"
                    operation.state_version += 1
                    operation.failure_code = "RECOVERY_NO_DISPATCH"
                    operation.receipt_id = "no-dispatch:" + str(operation.id)
                    operation.receipt_evidence = {"kind": "NO_DISPATCH", "reason_code": "RECOVERY_NO_DISPATCH"}
                    operation.receipt_fingerprint = fingerprint("chota.business.receipt.v1", operation.receipt_evidence)
                    operation.terminal_at = now
                    operation.reconcile_state = "RESOLVED"
                    operation.updated_at = now
                    await session.flush()
                result = await self._snapshot(session, scope, plan, selection_kind=link.selection_kind)
            return result

    async def invalidate_stale_proposal(self, request, grant):
        """Close only an unacquired stale proposal; acquisition evidence is sticky."""
        async with self._session() as session:
            async with session.begin():
                scope = await self._scope_query(session, request, grant)
                if scope is None:
                    return False
                scope = await self._lock_scope(session, scope.id)
                await self._turn(session, scope, request)
                link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                candidates = []
                if link is not None:
                    candidates = [link.plan_id]
                else:
                    latest = await self._latest_plan(session, scope.id)
                    if latest is not None:
                        candidates = [latest.id]
                for plan_id in candidates:
                    plan = await session.scalar(select(BusinessPlan).where(
                        BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id)
                        .with_for_update(nowait=session.info.get("business_nowait", False))
                        .execution_options(populate_existing=True))
                    if plan is None or plan.proposal_state != "PREPARED":
                        continue
                    origin = await session.get(BusinessTurnContext, (scope.id, plan.origin_turn_id))
                    if origin is not None and origin.authority_revision != request.authority_revision:
                        operations = (await session.scalars(select(BusinessOperation).where(
                            BusinessOperation.plan_id == plan.id).with_for_update(
                                nowait=session.info.get("business_nowait", False))
                                .execution_options(populate_existing=True))).all()
                        if all(op.status == "RESERVED" and op.effect_request_turn_id is None for op in operations):
                            await self._invalidate(session, plan, await self._clock(session), "STALE_AUTHORITY_OR_CONTEXT")
                            await session.flush()
                            return True
                return False

    async def _retained(self, session, request, grant, plan_id, operation_id=None):
        scope = await self._scope_query(session, request, grant)
        if scope is None:
            raise BusinessSafetyError("SCOPE_DENIED")
        scope = await self._lock_scope(session, scope.id)
        plan = await session.scalar(select(BusinessPlan).where(
            BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id)
            .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
        link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
        if plan is None or link is None or link.plan_id != plan_id:
            raise BusinessSafetyError("PLAN_DENIED")
        operation = None
        if operation_id is not None:
            operation = await session.scalar(select(BusinessOperation).where(
                BusinessOperation.id == operation_id, BusinessOperation.plan_id == plan_id)
                .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
            if operation is None:
                raise BusinessSafetyError("OPERATION_DENIED")
        return scope, plan, operation

    @staticmethod
    def _current(plan, binding, registration, facts, now, *, consent=True):
        return (binding.id == plan.binding_id and binding.adapter_id == plan.adapter_id
                and binding.connection_id == plan.connection_id
                and binding.authorization_generation == plan.authorization_generation
                and binding.context_generation == plan.context_generation
                and binding.catalog_revision == plan.catalog_revision
                and registration.accepts_catalog(plan.catalog_revision)
                and registration.profile.profile_revision == plan.provider_profile_revision
                and fingerprint("chota.business.facts.v1", facts.model_dump(mode="python")) == plan.facts_fingerprint
                and plan.expires_at > now
                and plan.proposal_state not in {"INVALIDATED", "CANCELLED"}
                and (not consent or not plan.requires_confirmation or plan.proposal_state == "CONFIRMED"))

    async def _invalidate(self, session, plan, now, code):
        # An invalid plan loses permission, never its original effect evidence.
        operations = (await session.scalars(select(BusinessOperation).where(
            BusinessOperation.plan_id == plan.id).order_by(BusinessOperation.ordinal)
            .with_for_update(nowait=session.info.get("business_nowait", False))
            .execution_options(populate_existing=True))).all()
        if any(op.status == "RESERVED" and op.effect_request_turn_id is not None for op in operations):
            return
        plan.proposal_state = "INVALIDATED"
        for op in operations:
            if op.status != "RESERVED":
                continue
            op.status = "FAILED"
            op.failure_code = code
            op.receipt_id = "no-dispatch:" + str(op.id)
            op.receipt_evidence = {"kind": "NO_DISPATCH", "reason_code": code}
            op.receipt_fingerprint = fingerprint("chota.business.receipt.v1", op.receipt_evidence)
            op.terminal_at = now
            op.state_version += 1
            op.updated_at = now

    async def claim_dispatch(self, request, grant, plan_id, operation_id, registration, facts_resolver, *,
                             effect_request: ExternalEffectPermitRequest | None = None, native_replay=False):
        if not callable(facts_resolver):
            raise BusinessSafetyError("FACTS_RESOLVER_REQUIRED")
        try:
            probe = await self._admission_probe(request, grant, plan_id)
        except BusinessSafetyError as error:
            if error.code not in {"AUTHORITY_DENIED", "BINDING_UNAVAILABLE"}:
                raise
            # No live authority: retained NO_DISPATCH proof is still readable/writable.
            async with self._session() as session:
                async with session.begin():
                    scope, plan, _ = await self._retained(session, request, grant, plan_id, operation_id)
                    await self._invalidate(session, plan, await self._clock(session), "AUTHORITY_DENIED")
                    await session.flush()
                    snapshot = await self._snapshot(session, scope, plan)
                return DispatchDecision(None, "AUTHORITY_DENIED", snapshot)
        facts = await self._resolve_fresh(facts_resolver)
        try:
            return await self._claim_dispatch(request, grant, plan_id, operation_id, registration, facts,
                                               effect_request=effect_request, native_replay=native_replay, probe=probe)
        except DBAPIError as error:
            if not self._contention(error):
                raise
            # No retry or old waitable admission. This is retained observation only.
            snapshot = await self.lookup_plan(request, grant, plan_id)
            if snapshot is None:
                raise BusinessSafetyError("PLAN_DENIED")
            return DispatchDecision(None, "ADMISSION_CONTENDED", snapshot)

    async def _claim_dispatch(self, request, grant, plan_id, operation_id, registration, facts, *,
                              effect_request: ExternalEffectPermitRequest, native_replay=False, probe):
        async with self._session(decisive=True) as session:
            async with session.begin():
                try:
                    binding, scope = await self._authority(session, request, grant)
                except BusinessSafetyError as error:
                    if error.code not in {"AUTHORITY_DENIED", "BINDING_UNAVAILABLE"}:
                        raise
                    scope, plan, operation = await self._retained(session, request, grant, plan_id, operation_id)
                    now = await self._clock(session)
                    await self._invalidate(session, plan, now, "AUTHORITY_DENIED")
                    await session.flush()
                    result = DispatchDecision(None, "AUTHORITY_DENIED", await self._snapshot(session, scope, plan))
                else:
                    self._check_probe(binding, scope, probe)
                    plan = await session.scalar(select(BusinessPlan).where(
                        BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id)
                        .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
                    link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                    if plan is None or link is None or link.plan_id != plan_id:
                        raise BusinessSafetyError("PLAN_DENIED")
                    if link.selection_kind not in {"OWN", "CONFIRM"}:
                        raise BusinessSafetyError("RECOVERY_READ_ONLY", processing_status="UNKNOWN")
                    context = await session.get(BusinessTurnContext, (scope.id, request.turn_id))
                    if context is None or context.request_fingerprint != request_fingerprint(request):
                        raise BusinessSafetyError("TURN_CONFLICT")
                    operations = (await session.scalars(select(BusinessOperation).where(
                        BusinessOperation.plan_id == plan_id).order_by(BusinessOperation.ordinal)
                        .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))).all()
                    if len(operations) != plan.action_count or [op.ordinal for op in operations] != list(range(plan.action_count)):
                        raise BusinessSafetyError("INCOMPLETE_PLAN")
                    operation = next((op for op in operations if op.id == operation_id), None)
                    if operation is None:
                        raise BusinessSafetyError("OPERATION_DENIED")
                    now = await self._clock(session)
                    reason, token = "OBSERVE", None
                    if (fingerprint("chota.business.action.v1", operation.request_payload) != operation.request_fingerprint
                            or operation.capability != operation.request_payload["capability"]
                            or operation.schema_version != operation.request_payload["schema_version"]
                            or operation.provider_profile_revision != registration.profile.profile_revision
                            or operation.native_replay_policy != registration.profile.native_replay_policy
                            or (operation.native_replay_policy != "NONE" and
                                operation.provider_idempotency_key != "chota-g3:" + str(operation.id))):
                        raise BusinessSafetyError("OPERATION_CONFLICT")
                    if operation.status in {"SUCCEEDED", "FAILED"}:
                        reason = "TERMINAL"
                    elif operation.status == "DISPATCHING":
                        reason = "IN_FLIGHT"
                    elif operation.status == "UNKNOWN" and not native_replay:
                        reason = "UNKNOWN"
                    elif any(op.status != "SUCCEEDED" for op in operations[:operation.ordinal]):
                        reason = "BLOCKED_PREDECESSOR"
                    else:
                        unresolved = await self._unresolved(session, scope.id)
                        unresolved = [identity for identity in unresolved if identity != operation.id]
                        if unresolved:
                            reason = "BLOCKED_UNRESOLVED_EFFECT"
                        elif plan.requires_confirmation and plan.proposal_state == "PREPARED":
                            reason = "CONFIRMATION_REQUIRED"
                        elif registration.execution_contract.expected_facts != "CONDITIONAL_EXPECTED_FACTS_V1":
                            reason = "CONDITIONAL_EFFECT_UNAVAILABLE"
                        elif not self._current(plan, binding, registration, facts, now) or operation.capability not in binding.enabled_capabilities:
                            if operation.status == "RESERVED":
                                await self._invalidate(session, plan, now, "STALE_AUTHORITY_OR_CONTEXT")
                            reason = "AUTHORITY_DENIED"
                        elif native_replay and (operation.status != "UNKNOWN" or registration.profile.mode != "NATIVE"
                                               or operation.native_replay_policy == "NONE"
                                               or (operation.native_replay_policy == "BOUNDED" and
                                                   registration.execution_contract.bounded_acceptance != "PROVIDER_ACCEPT_BEFORE_V1")
                                               or (operation.native_replay_policy == "BOUNDED" and
                                                   operation.native_replay_until <= now + timedelta(seconds=registration.profile.dispatch_timeout_seconds))):
                            reason = "NATIVE_REPLAY_DENIED"
                        elif not native_replay and operation.status != "RESERVED":
                            reason = "UNKNOWN"
                        else:
                            if effect_request is None and operation.effect_permit_id is not None:
                                effect_request = await self._retained_effect_request(
                                    session, request, scope, plan, operation)
                            if (effect_request is None or operation.effect_permit_id is None
                                    or operation.effect_request_turn_id != request.turn_id
                                    or operation.effect_request_fingerprint != sidecar_fingerprint(effect_request)
                                    or operation.effect_managed_request_fingerprint != effect_request.managed_request_fingerprint
                                    or operation.effect_operation_fingerprint != effect_request.operation_fingerprint
                                    or effect_request.turn_id != request.turn_id
                                    or effect_request.origin_turn_id != plan.origin_turn_id
                                    or effect_request.operation_id != operation.id
                                    or effect_request.operation_fingerprint != frozen_command_fingerprint(
                                        self._command(scope, plan, operation))):
                                reason = "EFFECT_PERMIT_REQUIRED"
                            else:
                                operation.status = "DISPATCHING"
                                operation.state_version += 1
                                operation.dispatch_epoch += 1
                                operation.attempt_count += 1
                                operation.dispatch_nonce = uuid4()
                                operation.dispatch_started_at = now
                                operation.dispatch_lease_expires_at = now + timedelta(seconds=registration.profile.dispatch_timeout_seconds + 7)
                                operation.reconcile_state = "NONE"
                                operation.reconcile_nonce = None
                                operation.reconcile_lease_expires_at = None
                                operation.updated_at = now
                                await session.flush()
                                token = DispatchToken(operation.id, scope.id, plan.id, operation.state_version,
                                                      operation.dispatch_epoch, operation.dispatch_nonce,
                                                      self._command(scope, plan, operation), native_replay,
                                                      operation.native_replay_until if operation.native_replay_policy == "BOUNDED"
                                                      and registration.execution_contract.bounded_acceptance == "PROVIDER_ACCEPT_BEFORE_V1" else None,
                                                      registration.execution_contract.revision)
                                reason = "PERMITTED"
                    await session.flush()
                    result = DispatchDecision(token, reason, await self._snapshot(session, scope, plan))
            return result

    @staticmethod
    async def _retained_effect_request(session, request, scope, plan, operation):
        """Rebuild only the exact already-permitted request for same-Turn replay."""
        if (operation.effect_permit_id is None
                or operation.effect_request_turn_id != request.turn_id
                or operation.effect_request_fingerprint is None
                or operation.effect_managed_request_fingerprint is None
                or operation.effect_operation_fingerprint is None):
            return None
        context = await session.get(BusinessTurnContext, (scope.id, operation.effect_request_turn_id))
        if context is None:
            raise BusinessSafetyError("EFFECT_REQUEST_CONFLICT", processing_status="UNKNOWN")
        return ExternalEffectPermitRequest(
            contract_version=1,
            conversation_id=request.conversation_id,
            turn_id=operation.effect_request_turn_id,
            origin_turn_id=plan.origin_turn_id,
            owner_id=request.owner_id,
            integration_id=request.integration_id,
            channel=request.channel,
            agent_id=request.agent_id,
            authority_revision=context.authority_revision,
            managed_request_fingerprint=operation.effect_managed_request_fingerprint,
            operation_namespace="main-chota.ai-business.v1",
            operation_id=operation.id,
            operation_fingerprint=operation.effect_operation_fingerprint,
        )

    async def confirm_plan(self, request, grant, plan_id, registration, facts_resolver, *, semantic=False):
        if semantic and getattr(registration, "confirmation_mode", "REFERENCE_V1") != "SEMANTIC_V1":
            raise BusinessSafetyError("CONFIRMATION_REQUIRED")
        probe = await self._admission_probe(request, grant, plan_id)
        facts = await self._resolve_fresh(facts_resolver)
        try:
            return await self._confirm_plan(request, grant, plan_id, registration, facts, probe=probe, semantic=semantic)
        except DBAPIError as error:
            if not self._contention(error):
                raise
            raise BusinessSafetyError("ADMISSION_CONTENDED", processing_status="UNKNOWN") from None

    async def _confirm_plan(self, request, grant, plan_id, registration, facts, *, probe, semantic=False):
        async with self._session(decisive=True) as session:
            async with session.begin():
                binding, scope = await self._authority(session, request, grant)
                self._check_probe(binding, scope, probe)
                await self._turn(session, scope, request)
                plan = await session.scalar(select(BusinessPlan).where(
                    BusinessPlan.id == plan_id, BusinessPlan.scope_id == scope.id)
                    .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
                if plan is None:
                    raise BusinessSafetyError("PLAN_DENIED")
                # A retired/cancelled proposal is never made selectable again.
                # In particular, retirement may win the plan lock before this
                # confirmation attempt; reject before creating a TurnPlanLink.
                if plan.proposal_state not in {"PREPARED", "CONFIRMED"}:
                    raise BusinessSafetyError("STALE_CONFIRMATION")
                origin = await session.get(BusinessTurnContext, (scope.id, plan.origin_turn_id))
                current_turn = await session.get(BusinessTurnContext, (scope.id, request.turn_id))
                if (origin is None or current_turn is None
                        or origin.authority_revision != request.authority_revision
                        or current_turn.authority_revision != request.authority_revision):
                    raise BusinessSafetyError("STALE_CONFIRMATION")
                link = await session.get(BusinessTurnPlanLink, (scope.id, request.turn_id))
                if link is not None and link.selection_kind == "RECOVERY":
                    raise BusinessSafetyError("RECOVERY_READ_ONLY", processing_status="UNKNOWN")
                if link and (link.plan_id != plan_id or link.selection_kind not in {"CONFIRM", "OWN"}):
                    raise BusinessSafetyError("PLAN_CONFLICT")
                operations = (await session.scalars(select(BusinessOperation).where(
                    BusinessOperation.plan_id == plan.id).order_by(BusinessOperation.ordinal)
                    .with_for_update(nowait=True).execution_options(populate_existing=True))).all()
                if len(operations) != plan.action_count or [op.ordinal for op in operations] != list(range(plan.action_count)):
                    raise BusinessSafetyError("INCOMPLETE_PLAN")
                if await self._unresolved(session, scope.id):
                    raise BusinessSafetyError("BLOCKED_UNRESOLVED_EFFECT", processing_status="UNKNOWN")
                now = await self._clock(session)
                frozen = await self._snapshot(session, scope, plan)
                current = (self._current(plan, binding, registration, facts, now, consent=False)
                           and all(op.command.capability in binding.enabled_capabilities for op in frozen.operations))
                if not current:
                    await self._invalidate(session, plan, now, "STALE_CONFIRMATION")
                elif plan.proposal_state == "CONFIRMED":
                    if plan.confirmed_turn_id != request.turn_id:
                        raise BusinessSafetyError("CONFIRMATION_REPLAY_DENIED")
                    confirmed = await session.get(BusinessTurnContext, (scope.id, plan.confirmed_turn_id))
                    if confirmed is None or confirmed.authority_revision != request.authority_revision:
                        raise BusinessSafetyError("STALE_CONFIRMATION")
                else:
                    origin = await session.get(BusinessTurnContext, (scope.id, plan.origin_turn_id))
                    selected = request.selected_messages
                    if semantic:
                        latest = await self._latest_plan(session, scope.id)
                        if latest is None or latest.id != plan.id:
                            raise BusinessSafetyError("STALE_CONFIRMATION")
                        valid = self._semantic_inputs(request, origin)
                    else:
                        valid = [m for m in selected if m.actor == "CUSTOMER" and m.direction == "INBOUND" and m.content_type == "TEXT"
                                 and m.text.strip(" ") in {"confirm " + str(plan.id), "подтверждаю " + str(plan.id)}]
                    if (request.turn_id == plan.origin_turn_id or request.input_first_sequence <= origin.input_last_sequence
                            or not valid or any(str(m.id) in origin.selected_input_message_ids for m in selected)):
                        raise BusinessSafetyError("CONFIRMATION_REQUIRED")
                    frozen = await self._snapshot(session, scope, plan)
                    if (not self._current(plan, binding, registration, facts, now, consent=False)
                            or any(op.command.capability not in binding.enabled_capabilities for op in frozen.operations)):
                        await self._invalidate(session, plan, now, "STALE_CONFIRMATION")
                        result = await self._snapshot(session, scope, plan)
                    else:
                        plan.proposal_state = "CONFIRMED"
                        plan.confirmed_turn_id = request.turn_id
                        plan.confirmation_input_ids = [str(m.id) for m in valid]
                        plan.confirmation_policy = "SEMANTIC_CUSTOMER_TURN_V1" if semantic else CONFIRMATION_POLICY
                        plan.confirmed_at = now
                        result = None
                if link is None:
                    session.add(BusinessTurnPlanLink(scope_id=scope.id, turn_id=request.turn_id,
                                                     plan_id=plan_id, selection_kind="CONFIRM"))
                await session.flush()
                result = await self._snapshot(session, scope, plan)
            return result

    @staticmethod
    def _evidence(receipt, command):
        if not isinstance(receipt, (SucceededReceipt, DefinitiveNoEffect)):
            raise BusinessSafetyError("RECEIPT_INVALID", processing_status="UNKNOWN")
        if receipt.binding != command.receipt_binding():
            raise BusinessSafetyError("RECEIPT_CONFLICT", processing_status="UNKNOWN")
        try:
            # Also validate code-owned adapter objects which were constructed
            # with model_copy/model_construct and bypassed initial Pydantic.
            type(receipt).model_validate(receipt.model_dump(mode="python"))
        except Exception:
            raise BusinessSafetyError("RECEIPT_INVALID", processing_status="UNKNOWN") from None
        data = receipt.model_dump(mode="json")
        canonical_value(data)
        return data, fingerprint("chota.business.receipt.v1", data)

    async def finalize(self, request, grant, token, receipt):
        data, digest = self._evidence(receipt, token.command)
        async with self._session() as session:
            async with session.begin():
                scope, plan, operation = await self._retained(session, request, grant, token.plan_id, token.operation_id)
                if self._command(scope, plan, operation).receipt_binding() != token.command.receipt_binding():
                    raise BusinessSafetyError("RECEIPT_CONFLICT", processing_status="UNKNOWN")
                if operation.status in {"SUCCEEDED", "FAILED"}:
                    if operation.receipt_fingerprint != digest:
                        raise BusinessSafetyError("RECEIPT_CONFLICT", processing_status="UNKNOWN")
                elif (operation.status == "DISPATCHING" and operation.state_version == token.state_version
                      and operation.dispatch_epoch == token.dispatch_epoch and operation.dispatch_nonce == token.dispatch_nonce):
                    self._write_receipt(operation, receipt, data, digest, await self._clock(session))
                # A displaced token is observation only; never last-write-wins.
                await session.flush()
                result = await self._snapshot(session, scope, plan)
            return result

    @staticmethod
    def _write_receipt(operation, receipt, data, digest, now):
        operation.status = "SUCCEEDED" if isinstance(receipt, SucceededReceipt) else "FAILED"
        operation.receipt_id = receipt.receipt_id
        operation.object_id = receipt.object_id if isinstance(receipt, SucceededReceipt) else None
        operation.receipt_evidence = data
        operation.receipt_fingerprint = digest
        operation.failure_code = receipt.reason_code if isinstance(receipt, DefinitiveNoEffect) else None
        operation.terminal_at = now
        operation.state_version += 1
        operation.reconcile_state = "RESOLVED"
        operation.reconcile_nonce = None
        operation.reconcile_lease_expires_at = None
        operation.updated_at = now

    async def mark_unknown(self, request, grant, token, *, lookup_available):
        async with self._session() as session:
            async with session.begin():
                scope, plan, operation = await self._retained(session, request, grant, token.plan_id, token.operation_id)
                if (operation.status == "DISPATCHING" and operation.state_version == token.state_version
                        and operation.dispatch_epoch == token.dispatch_epoch and operation.dispatch_nonce == token.dispatch_nonce):
                    operation.status = "UNKNOWN"
                    operation.state_version += 1
                    operation.reconcile_state = "NEEDED" if lookup_available else "MANUAL_REQUIRED"
                    operation.updated_at = await self._clock(session)
                await session.flush()
                result = await self._snapshot(session, scope, plan)
            return result

    async def recover_attempts(self, request, grant):
        async with self._session() as session:
            async with session.begin():
                scope = await self._scope_query(session, request, grant)
                if scope is None:
                    return 0
                scope = await self._lock_scope(session, scope.id)
                now = await self._clock(session)
                # At most one attempted operation per retained scope; canonical
                # scope serialization makes this bounded independently of age.
                candidates = (await session.execute(select(BusinessOperation.id, BusinessOperation.plan_id).where(
                    BusinessOperation.scope_id == scope.id, BusinessOperation.status == "DISPATCHING",
                    BusinessOperation.dispatch_lease_expires_at <= now)
                    .order_by(BusinessOperation.plan_id, BusinessOperation.ordinal))).all()
                count = 0
                for identity, plan_id in candidates:
                    await session.scalar(select(BusinessPlan.id).where(BusinessPlan.id == plan_id).with_for_update(nowait=session.info.get("business_nowait", False)))
                    operation = await session.scalar(select(BusinessOperation).where(BusinessOperation.id == identity)
                                                     .with_for_update(nowait=session.info.get("business_nowait", False)).execution_options(populate_existing=True))
                    if operation.status != "DISPATCHING" or operation.dispatch_lease_expires_at > now:
                        continue
                    operation.status = "UNKNOWN"
                    operation.state_version += 1
                    operation.reconcile_state = "NEEDED" if operation.native_replay_policy != "NONE" else "MANUAL_REQUIRED"
                    operation.updated_at = now
                    count += 1
                await session.flush()
            return count

    async def claim_reconcile(self, request, grant, plan_id, operation_id, *, timeout_seconds):
        async with self._session() as session:
            async with session.begin():
                scope, plan, operation = await self._retained(session, request, grant, plan_id, operation_id)
                now = await self._clock(session)
                token = None
                if operation.status == "UNKNOWN" and (operation.reconcile_state != "IN_FLIGHT" or operation.reconcile_lease_expires_at <= now):
                    operation.state_version += 1
                    operation.reconcile_epoch += 1
                    operation.reconcile_nonce = uuid4()
                    operation.reconcile_lease_expires_at = now + timedelta(seconds=timeout_seconds + 7)
                    operation.reconcile_state = "IN_FLIGHT"
                    operation.updated_at = now
                    token = ReconcileToken(operation.id, scope.id, plan.id, operation.state_version,
                                           operation.reconcile_epoch, operation.reconcile_nonce,
                                           self._command(scope, plan, operation))
                await session.flush()
            return token

    async def finalize_reconcile(self, request, grant, token, receipt):
        terminal = isinstance(receipt, (ProvenSucceeded, ProvenFailedNoEffect))
        data, digest = self._evidence(receipt, token.command) if terminal else (None, None)
        async with self._session() as session:
            async with session.begin():
                scope, plan, operation = await self._retained(session, request, grant, token.plan_id, token.operation_id)
                if self._command(scope, plan, operation).receipt_binding() != token.command.receipt_binding():
                    raise BusinessSafetyError("RECEIPT_CONFLICT", processing_status="UNKNOWN")
                if (operation.status == "UNKNOWN" and operation.state_version == token.state_version
                        and operation.reconcile_epoch == token.reconcile_epoch and operation.reconcile_nonce == token.reconcile_nonce):
                    now = await self._clock(session)
                    if terminal:
                        self._write_receipt(operation, receipt, data, digest, now)
                    else:
                        operation.state_version += 1
                        operation.reconcile_state = "NEEDED"
                        operation.reconcile_nonce = None
                        operation.reconcile_lease_expires_at = None
                        operation.updated_at = now
                    operation.last_reconciled_at = now
                elif operation.status in {"SUCCEEDED", "FAILED"} and terminal and operation.receipt_fingerprint != digest:
                    raise BusinessSafetyError("RECEIPT_CONFLICT", processing_status="UNKNOWN")
                await session.flush()
                result = await self._snapshot(session, scope, plan)
            return result

    async def collect_refs(self, request, grant):
        plan = await self.selected_plan(request, grant)
        return plan.references if plan else []

    @classmethod
    async def read_evidence(cls, value, grant, session_factory):
        repository = cls(session_factory)
        async with repository._session() as session:
            scope = await repository._scope_query(session, value, grant)
            if scope is None or await session.get(BusinessTurnContext, (scope.id, value.turn_id)) is None:
                return None
            link = await session.get(BusinessTurnPlanLink, (scope.id, value.turn_id))
            if link is None:
                return []
            plan = await session.get(BusinessPlan, link.plan_id)
            return (await repository._snapshot(session, scope, plan)).references
