"""Durable business execution inside the managed shared-core extension."""

import asyncio
from uuid import UUID

import anyio

from schemas.business import (Ambiguous, AttemptRejected, BusinessAction, BusinessSafetyError, BusinessToolResult,
                              DefinitiveNoEffect, MaterialFacts, ProvenFailedNoEffect,
                              ProvenSucceeded, StillUnknown, SucceededReceipt)
from schemas.managed_turn import OperationReference
from services.business_registry import BusinessRegistry
from services.business_repository import BusinessRepository
from services.managed_runtime import ManagedRuntime


class CommittedReferenceCollector:
    """Acknowledged DB observations only; never a provider/model receipt ledger."""

    def __init__(self):
        self._refs = {}

    def observe_committed(self, refs):
        pending = dict(self._refs)
        for ref in refs:
            ref = OperationReference.model_validate(ref)
            key = ref.origin, ref.operation_id
            previous = pending.get(key)
            if previous and previous.outcome in {"SUCCEEDED", "FAILED"}:
                if ref.outcome in {"SUCCEEDED", "FAILED"} and previous.outcome != ref.outcome:
                    raise BusinessSafetyError("REF_CONFLICT")
                continue
            if previous and previous.outcome == "UNKNOWN" and ref.outcome is None:
                continue
            pending[key] = ref.model_copy()
        if len(pending) > 32:
            raise BusinessSafetyError("REF_BOUNDS")
        self._refs = pending

    def snapshot(self):
        return [self._refs[key].model_copy() for key in sorted(self._refs)]


class BusinessRuntime:
    @classmethod
    async def open(cls, request, grant, session_factory, registry, *, execution=None, collector=None):
        if not isinstance(registry, BusinessRegistry):
            raise BusinessSafetyError("REGISTRY_INVALID")
        self = cls()
        self.request, self.grant = request, grant
        self.repository = BusinessRepository(session_factory)
        self.registry, self.execution = registry, execution
        self.collector = collector if collector is not None else CommittedReferenceCollector()
        context = await self.repository.open_context(request, grant, registry)
        self.scope, self.binding, self.registration = context if context else (None, None, None)
        # Explicit same-scope normalization, never a new provider command.
        if self.scope is not None:
            await self.repository.recover_attempts(request, grant)
        self.last_gate = None
        self.business_scope_seen = self.scope is not None
        await self.collect_refs()
        return self

    async def collect_refs(self):
        try:
            gate = await self.repository.result_gate(self.request, self.grant)
            self.collector.observe_committed(gate.references)
        except BusinessSafetyError:
            self.last_gate = None
            raise
        except Exception:
            # Preserve the original collector fallback, but never reuse an old
            # no-blocker gate as permission for ordinary prose during an outage.
            self.last_gate = None
            return self.collector.snapshot()
        self.last_gate = gate
        self.business_scope_seen = self.business_scope_seen or gate.scope_exists
        return self.collector.snapshot()

    async def guard_result(self, *, allow_incomplete=False, reload=True):
        if reload:
            try:
                await self.collect_refs()
            except Exception:
                self.last_gate = None
                return self._halt(True)
        gate = self.last_gate
        if gate is None:
            return self._halt(True)
        if gate.scope_unresolved or gate.disposition == "UNKNOWN":
            return self._halt(True)
        if gate.disposition == "NO_PLAN" and self.collector.snapshot():
            return self._halt(True)
        if gate.disposition in {"NO_PLAN", "ALL_SUCCEEDED"}:
            return None
        if gate.disposition == "AWAITING_CONFIRMATION":
            try:
                if (self.registration is None or gate.plan.operations[0].command.adapter_id != self.registration.adapter_id
                        or gate.plan.operations[0].command.provider_profile_revision != self.registration.profile.profile_revision
                        or not self.registration.accepts_catalog(gate.plan.catalog_revision)):
                    return self._halt(True)
                return self._proposal(gate.plan)
            except Exception:
                return self._halt(True)
        if gate.disposition == "INCOMPLETE" and allow_incomplete:
            return None
        if gate.disposition == "FAILED":
            return self._halt(any(op.status in {"SUCCEEDED", "DISPATCHING", "UNKNOWN"} for op in gate.plan.operations))
        return self._halt(True)

    def facts_resolver(self, actions, *, allow_disabled=True):
        # This callback is invoked only by the repository after its P transaction
        # has fully closed. It contains no admission right or cached witness.
        async def resolve():
            return await self._facts(actions, allow_disabled=allow_disabled)
        return resolve

    def openai_tools(self):
        if self.registration is None or not self.binding.enabled_capabilities:
            return []
        capabilities = [definition.name for definition in self.registration.capabilities
                        if definition.name in self.binding.enabled_capabilities]
        if not capabilities:
            return []
        action = {"type": "object", "additionalProperties": False, "required": ["capability", "arguments"],
                  "properties": {"capability": {"type": "string", "enum": capabilities},
                                 "arguments": {"type": "object"}}}
        plan = {"type": "object", "additionalProperties": False, "required": ["actions"],
                "properties": {"actions": {"type": "array", "minItems": 1, "maxItems": 32, "items": action}}}
        return [
            {"type": "function", "function": {"name": "business_execute_plan",
                "description": "Execute one immutable ordered business plan. Required consent is enforced by the server.",
                "parameters": plan}},
            {"type": "function", "function": {"name": "business_propose",
                "description": "Freeze a local proposal. This creates no provider effect. Ask for its exact confirmation token.",
                "parameters": plan}},
            {"type": "function", "function": {"name": "business_confirm",
                "description": "Confirm a frozen proposal only using the selected canonical customer confirmation message.",
                "parameters": {"type": "object", "additionalProperties": False,
                               "required": ["proposal_id"], "properties": {"proposal_id": {"type": "string", "format": "uuid"}}}}},
        ]

    async def call_tool(self, name, arguments):
        if name not in {tool["function"]["name"] for tool in self.openai_tools()}:
            raise BusinessSafetyError("TOOL_UNAVAILABLE")
        try:
            if not isinstance(arguments, dict):
                raise BusinessSafetyError("ACTION_INVALID")
            if name == "business_confirm":
                if set(arguments) != {"proposal_id"} or not isinstance(arguments["proposal_id"], str):
                    raise BusinessSafetyError("ACTION_INVALID")
                proposal_id = UUID(arguments["proposal_id"])
                if str(proposal_id) != arguments["proposal_id"]:
                    raise BusinessSafetyError("ACTION_INVALID")
                return await self.confirm(proposal_id)
            if set(arguments) != {"actions"} or not isinstance(arguments["actions"], list):
                raise BusinessSafetyError("ACTION_INVALID")
            if name == "business_propose":
                return await self.propose_plan(arguments["actions"])
            return await self.execute_plan(arguments["actions"])
        except BusinessSafetyError as error:
            return BusinessToolResult(halt_status=error.processing_status,
                                      error_code="TOOL_EXECUTION_UNKNOWN" if error.processing_status == "UNKNOWN" else "TOOL_SETUP_FAILED")
        except (ValueError, TypeError):
            return BusinessToolResult(halt_status="FAILED", error_code="TOOL_SETUP_FAILED")

    async def _adapter_call(self, method, *args):
        """No transaction spans this call; explicit per-call resource ownership."""
        if self.registration is None:
            raise BusinessSafetyError("BINDING_UNAVAILABLE")
        function = getattr(self.registration.adapter, method)
        seconds = self.registration.profile.dispatch_timeout_seconds
        if self.execution is not None:
            with anyio.fail_after(seconds):
                return await self.execution.operation(function(*args, execution=self.execution))
        # Standalone trusted recovery owns the existing Goal2 transport machinery
        # in this very task. A shared registry object owns no request/client state.
        owner = ManagedRuntime(seconds + 2)
        with anyio.CancelScope(shield=True) as lifecycle:
            try:
                with anyio.fail_after(seconds):
                    return await owner.operation(function(*args, execution=owner))
            finally:
                await owner.cleanup(lifecycle)

    async def _facts(self, actions, *, allow_disabled=False):
        if self.scope is None:
            raise BusinessSafetyError("BINDING_UNAVAILABLE")
        # Closed DTOs are checked before this audited read-only provider port.
        normalized = []
        for value in actions:
            action = BusinessAction.model_validate(value)
            capability = self.registration.capability(action.capability)
            if capability is None or (not allow_disabled and capability.name not in self.binding.enabled_capabilities):
                raise BusinessSafetyError("CAPABILITY_DISABLED")
            normalized.append(BusinessAction(capability=action.capability,
                                             arguments=capability.normalize(action.arguments)))
        facts = await self._adapter_call("resolve_facts", self.scope, tuple(normalized))
        if not isinstance(facts, MaterialFacts):
            raise BusinessSafetyError("FACTS_INVALID")
        return facts

    async def freeze_plan(self, actions, *, propose=False):
        if not isinstance(actions, (list, tuple)) or not 1 <= len(actions) <= 32:
            raise BusinessSafetyError("PLAN_BOUNDS")
        plan = await self.repository.reserve_plan(self.request, self.grant, self.registration,
                                                   actions, self.facts_resolver(actions, allow_disabled=False), propose=propose)
        self.collector.observe_committed(plan.references)
        return plan

    def _proposal(self, plan):
        # Only code-owned whitelisted frozen public fields reach this deterministic
        # local proposal. Never reuse a model completion claiming a booking exists.
        projections = []
        for operation in plan.operations:
            definition = self.registration.capability(operation.command.capability)
            projections.append({"capability": definition.name,
                                "arguments": {key: operation.command.arguments[key]
                                              for key in definition.public_fields
                                              if key in operation.command.arguments}})
        return BusinessToolResult(tool_payload={"status": "AWAITING_CONFIRMATION", "proposal_id": str(plan.id),
                                                "actions": projections},
                                  safe_draft="Предложение подготовлено. Для подтверждения отправьте: confirm " + str(plan.id))

    async def propose_plan(self, actions):
        plan = await self.freeze_plan(actions, propose=True)
        if plan.proposal_state == "PREPARED":
            return self._proposal(plan)
        if any(op.status in {"DISPATCHING", "UNKNOWN", "FAILED"} for op in plan.operations):
            return self._halt(any(op.status in {"DISPATCHING", "UNKNOWN", "SUCCEEDED"} for op in plan.operations))
        if all(op.status == "SUCCEEDED" for op in plan.operations):
            return BusinessToolResult(tool_payload={"status": "SUCCEEDED", "proposal_id": str(plan.id)})
        return BusinessToolResult(tool_payload={"status": "CONFIRMED", "proposal_id": str(plan.id)},
                                  safe_draft="Подтверждение сохранено; операция ещё не завершена.")

    async def execute_plan(self, actions):
        plan = await self.freeze_plan(actions)
        if plan.requires_confirmation and plan.proposal_state == "PREPARED":
            return self._proposal(plan)
        return await self._execute_frozen(plan)

    @staticmethod
    def _halt(unknown):
        return BusinessToolResult(halt_status="UNKNOWN" if unknown else "FAILED",
                                  error_code="TOOL_EXECUTION_UNKNOWN")

    async def _execute_frozen(self, plan):
        identities = tuple(op.id for op in plan.operations)
        for identity in identities:
            operation = next(op for op in plan.operations if op.id == identity)
            if operation.status == "SUCCEEDED":
                continue
            if operation.status == "FAILED":
                return self._halt(any(op.status in {"SUCCEEDED", "DISPATCHING", "UNKNOWN"} for op in plan.operations))
            actions = [BusinessAction(capability=op.command.capability, arguments=op.command.arguments)
                       for op in plan.operations]
            decision = await self.repository.claim_dispatch(self.request, self.grant, plan.id, operation.id,
                                                            self.registration, self.facts_resolver(actions))
            self.collector.observe_committed(decision.plan.references)
            plan = decision.plan
            candidate = next(op for op in plan.operations if op.id == identity)
            if decision.token is None:
                # A fresh committed terminal success is observation, not a send
                # token. Every following ordinal starts its own whole P/FRESH/D.
                if candidate.status == "SUCCEEDED":
                    continue
                return self._halt(decision.reason in {"ADMISSION_CONTENDED", "BLOCKED_UNRESOLVED_EFFECT", "BLOCKED_PREDECESSOR", "IN_FLIGHT", "UNKNOWN"}
                                  or any(op.status in {"SUCCEEDED", "DISPATCHING", "UNKNOWN"} for op in plan.operations))
            plan = await self._send(decision.token)
            if next(op for op in plan.operations if op.id == identity).status != "SUCCEEDED":
                return self._halt(True)
        return BusinessToolResult(tool_payload={"status": "SUCCEEDED", "operation_refs":
                                  [ref.model_dump(mode="json") for ref in self.collector.snapshot()]})

    async def _send(self, token):
        # Permission COMMIT has positively returned. Mark processing ambiguity
        # before invoking the owned provider, independently of draft lifetime.
        if self.execution is not None:
            self.execution.tool_dispatch_attempted = True
        try:
            receipt = await self._adapter_call("execute", token.dispatch)
            if isinstance(receipt, AttemptRejected):
                if receipt.binding != token.command.receipt_binding() or receipt.attempt != token.dispatch.attempt:
                    raise BusinessSafetyError("RECEIPT_CONFLICT", processing_status="UNKNOWN")
                # This attempt's rejection cannot erase any prior possible B1.
                plan = await self.repository.mark_unknown(self.request, self.grant, token,
                    lookup_available=self.registration.profile.mode != "BLIND")
                self.collector.observe_committed(plan.references)
                return plan
            if not isinstance(receipt, (SucceededReceipt, DefinitiveNoEffect)):
                raise BusinessSafetyError("PROVIDER_UNKNOWN", processing_status="UNKNOWN")
            plan = await self.repository.finalize(self.request, self.grant, token, receipt)
            self.collector.observe_committed(plan.references)
            return plan
        except BaseException as error:
            # Cancellation still joins an independent uncertainty transaction.
            # A receipt/COMMIT without acknowledgment supplies no success claim.
            with anyio.CancelScope(shield=True, deadline=anyio.current_time() + 2):
                try:
                    plan = await self.repository.mark_unknown(self.request, self.grant, token,
                                                              lookup_available=self.registration.profile.mode != "BLIND")
                    self.collector.observe_committed(plan.references)
                except Exception:
                    plan = None
            if isinstance(error, asyncio.CancelledError):
                raise
            if plan is None:
                raise BusinessSafetyError("PROVIDER_UNKNOWN", processing_status="UNKNOWN") from None
            return plan

    async def confirm(self, proposal_id):
        proposal_id = UUID(str(proposal_id))
        plan = await self.repository.lookup_plan(self.request, self.grant, proposal_id)
        if plan is None:
            raise BusinessSafetyError("PLAN_DENIED")
        actions = [BusinessAction(capability=op.command.capability, arguments=op.command.arguments) for op in plan.operations]
        plan = await self.repository.confirm_plan(self.request, self.grant, plan.id, self.registration,
                                                   self.facts_resolver(actions))
        self.collector.observe_committed(plan.references)
        return await self._execute_frozen(plan)

    async def select_recovery_plan(self, plan_id):
        plan = await self.repository.select_recovery_plan(self.request, self.grant, UUID(str(plan_id)))
        self.collector.observe_committed(plan.references)
        return plan

    async def recover_attempts(self):
        count = await self.repository.recover_attempts(self.request, self.grant)
        await self.collect_refs()
        return count

    async def reconcile(self, operation_id):
        plan = await self.repository.selected_plan(self.request, self.grant)
        operation_id = UUID(str(operation_id))
        operation = next((op for op in plan.operations if op.id == operation_id), None) if plan else None
        if operation is None:
            raise BusinessSafetyError("OPERATION_DENIED")
        if operation.status != "UNKNOWN" or self.registration is None or self.registration.profile.mode == "BLIND":
            return plan
        if (operation.command.adapter_id != self.registration.adapter_id
                or operation.command.provider_profile_revision != self.registration.profile.profile_revision):
            return plan
        try:
            if not await self._adapter_call("receipt_read_allowed", self.scope):
                return plan
            token = await self.repository.claim_reconcile(self.request, self.grant, plan.id, operation.id,
                                                          timeout_seconds=self.registration.profile.dispatch_timeout_seconds)
            if token is None:
                return await self.repository.selected_plan(self.request, self.grant)
            result = await self._adapter_call("lookup", token.command)
            if not isinstance(result, (ProvenSucceeded, ProvenFailedNoEffect, StillUnknown)):
                result = StillUnknown()
            try:
                self.repository._evidence(result, token.command) if isinstance(result, (ProvenSucceeded, ProvenFailedNoEffect)) else None
            except BusinessSafetyError:
                result = StillUnknown()
        except Exception:
            if "token" not in locals() or token is None:
                return plan
            result = StillUnknown()
        plan = await self.repository.finalize_reconcile(self.request, self.grant, token, result)
        self.collector.observe_committed(plan.references)
        return plan

    async def replay_native(self, operation_id):
        plan = await self.repository.selected_plan(self.request, self.grant)
        operation_id = UUID(str(operation_id))
        operation = next((op for op in plan.operations if op.id == operation_id), None) if plan else None
        if operation is None:
            raise BusinessSafetyError("OPERATION_DENIED")
        actions = [BusinessAction(capability=op.command.capability, arguments=op.command.arguments) for op in plan.operations]
        decision = await self.repository.claim_dispatch(self.request, self.grant, plan.id, operation.id,
                                                        self.registration, self.facts_resolver(actions), native_replay=True)
        self.collector.observe_committed(decision.plan.references)
        return await self._send(decision.token) if decision.token else decision.plan
