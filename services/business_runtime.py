"""Durable business execution inside the managed shared-core extension."""

import asyncio
import json
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
        from business_capabilities.connections import sync_business_binding
        await sync_business_binding(request, grant, session_factory, registry)
        context = await self.repository.open_context(request, grant, registry)
        self.scope, self.binding, self.registration = context if context else (None, None, None)
        # Explicit same-scope normalization, never a new provider command.
        if self.scope is not None:
            await self.repository.recover_attempts(request, grant)
        self.last_gate = None
        self.business_scope_seen = self.scope is not None
        self._proposal_decision = None
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
            if (self.registration is not None and self.registration.adapter_id == 'chotacrm'
                    and all(op.status == 'FAILED' for op in gate.plan.operations)):
                return self._failed_business_draft()
            return self._halt(any(op.status in {"SUCCEEDED", "DISPATCHING", "UNKNOWN"} for op in gate.plan.operations))
        return self._halt(True)

    def completed_confirmation_replay(self):
        """Entry-only reply from this confirmation Turn's retained booking receipt.

        The fresh result gate has already checked scope and request fingerprint.
        This grants no dispatch authority and does not alter the final guard or
        normal model continuation after the first successful confirmation.
        """
        gate = self.last_gate
        if (gate is None or gate.scope_unresolved or gate.disposition != "ALL_SUCCEEDED"
                or self.registration is None or self.registration.adapter_id != "chotacrm"
                or not self.semantic_confirmation):
            return None
        plan = gate.plan
        if (plan is None or plan.proposal_state != "CONFIRMED" or not plan.requires_confirmation
                or plan.confirmed_turn_id != self.request.turn_id
                or plan.origin_turn_id == self.request.turn_id or len(plan.operations) != 1):
            return None
        operation = plan.operations[0]
        if operation.command.capability != "booking.create":
            return None
        try:
            from business_capabilities.runtime_tools import completed_booking_reply

            receipt = SucceededReceipt.model_validate(operation.receipt_evidence)
            _, digest = self.repository._evidence(receipt, operation.command)
            if (plan.confirmation_input_ids != tuple(str(m.id) for m in self.request.selected_messages)
                    or operation.status != "SUCCEEDED" or operation.command.adapter_id != "chotacrm"
                    or digest != operation.receipt_fingerprint
                    or receipt.receipt_id != operation.receipt_id or receipt.object_id != operation.object_id):
                return self._halt(True)
            return BusinessToolResult(safe_draft=completed_booking_reply(operation, receipt))
        except Exception:
            # Corrupt or incomplete retained evidence cannot become success prose.
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
        tools = [
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
        if self.semantic_confirmation:
            tools[1]["function"]["description"] = "Freeze a business proposal without provider effects. Ask naturally using its frozen summary."
            tools[-1] = {"type": "function", "function": {
                "name": "business_decide_proposal",
                "description": "Interpret the complete selected customer reply about the existing frozen proposal. CONFIRM only unchanged acceptance; CHANGE for any requested modification; REJECT for cancellation; UNRELATED for other messages. Never supply new action contents.",
                "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["proposal_id", "intent"], "properties": {
                        "proposal_id": {"type": "string", "format": "uuid"},
                        "intent": {"type": "string", "enum": ["CONFIRM", "REJECT", "CHANGE", "UNRELATED"]}}}}}
        if self.registration.adapter_id == 'chotacrm':
            from business_capabilities.runtime_tools import canonical_tools
            return canonical_tools(self, tools)
        return tools

    @property
    def semantic_confirmation(self):
        return getattr(self.registration, "confirmation_mode", "REFERENCE_V1") == "SEMANTIC_V1"

    async def prompt_context(self):
        if not self.semantic_confirmation or self.scope is None:
            return {"pending": None}
        plan = await self.repository.pending_proposal(self.request, self.grant, self.registration)
        return {"pending": None if plan is None else {
            "proposal_id": str(plan.id), "summary": self._proposal_summary(plan), "confirmable": True}}

    async def call_tool(self, name, arguments):
        if name not in {tool["function"]["name"] for tool in self.openai_tools()}:
            raise BusinessSafetyError("TOOL_UNAVAILABLE")
        try:
            if not isinstance(arguments, dict):
                raise BusinessSafetyError("ACTION_INVALID")
            if self.registration.adapter_id == 'chotacrm':
                from business_capabilities.runtime_tools import call_capability
                result = await call_capability(self, name, arguments)
                if result is not None:
                    return result
            if name == "business_decide_proposal":
                if (set(arguments) != {"proposal_id", "intent"}
                        or not isinstance(arguments["proposal_id"], str)
                        or arguments["intent"] not in {"CONFIRM", "REJECT", "CHANGE", "UNRELATED"}):
                    raise BusinessSafetyError("ACTION_INVALID")
                proposal_id = UUID(arguments["proposal_id"])
                if str(proposal_id) != arguments["proposal_id"]:
                    raise BusinessSafetyError("ACTION_INVALID")
                decision = (proposal_id, arguments["intent"])
                if self._proposal_decision is not None and self._proposal_decision != decision:
                    raise BusinessSafetyError("CONFIRMATION_CONFLICT")
                self._proposal_decision = decision
                if arguments["intent"] == "CONFIRM":
                    return await self.confirm(proposal_id, semantic=True)
                if arguments["intent"] in {"REJECT", "CHANGE"}:
                    await self.repository.cancel_proposal(self.request, self.grant, proposal_id, self.registration)
                    return BusinessToolResult(tool_payload={"status": "CANCELLED", "intent": arguments["intent"],
                                                            "new_confirmation_required": arguments["intent"] == "CHANGE"})
                # Unrelated text grants no effect or selected operation refs.
                pending = await self.repository.pending_proposal(self.request, self.grant, self.registration)
                if pending is None or pending.id != proposal_id:
                    raise BusinessSafetyError("PLAN_DENIED")
                return BusinessToolResult(tool_payload={"status": "UNCHANGED", "intent": "UNRELATED"})
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
        if self.semantic_confirmation:
            summary = self._proposal_summary(plan)
            return BusinessToolResult(tool_payload={"status": "AWAITING_CONFIRMATION", "proposal_id": str(plan.id),
                                                    "summary": summary}, safe_draft=summary + " Подтверждаете?")
        return BusinessToolResult(tool_payload={"status": "AWAITING_CONFIRMATION", "proposal_id": str(plan.id),
                                                "actions": projections},
                                  safe_draft="Предложение подготовлено. Для подтверждения отправьте: confirm " + str(plan.id))

    def _proposal_summary(self, plan):
        if self.registration.adapter_id == 'chotacrm':
            from business_capabilities.runtime_tools import proposal_summary
            return proposal_summary(plan)
        labels = {"booking.create": "Запись", "booking.reschedule": "Перенос записи",
                  "booking.cancel": "Отмена записи", "deal.create": "Заказ", "deal.move": "Изменение заказа"}
        fields = {"slot": "время", "start_at": "дата и время", "end_at": "окончание",
                  "offering_name": "услуга", "resource_name": "мастер", "display_name": "имя",
                  "customer_name": "имя", "phone": "телефон", "summary": "детали",
                  "title": "название", "comment": "комментарий", "stage": "статус"}
        parts = []
        for operation in plan.operations:
            definition = self.registration.capability(operation.command.capability)
            values = []
            for key in definition.public_fields:
                if key in operation.command.arguments:
                    value = operation.command.arguments[key]
                    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    values.append(f"{fields.get(key, 'детали')}: {rendered}")
            parts.append(labels.get(definition.name, "Действие") + (": " + "; ".join(values) if values else ""))
        summary = ". ".join(parts) + "."
        if len(summary) > 8000:
            raise BusinessSafetyError("PROPOSAL_BOUNDS")
        return summary

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

    @staticmethod
    def _failed_business_draft():
        return BusinessToolResult(tool_payload={'status': 'FAILED'},
            safe_draft='Не удалось выполнить действие: условия изменились. Давайте уточним детали и попробуем другой вариант.')

    async def _execute_frozen(self, plan):
        identities = tuple(op.id for op in plan.operations)
        for identity in identities:
            operation = next(op for op in plan.operations if op.id == identity)
            if operation.status == "SUCCEEDED":
                continue
            if operation.status == "FAILED":
                if self.registration.adapter_id == 'chotacrm' and all(op.status == 'FAILED' for op in plan.operations):
                    return self._failed_business_draft()
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
                if self.registration.adapter_id == 'chotacrm' and all(op.status == 'FAILED' for op in plan.operations):
                    return self._failed_business_draft()
                return self._halt(True)
        if self.registration.adapter_id == 'chotacrm':
            from business_capabilities.runtime_tools import successful_results
            return await successful_results(self, plan)
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

    async def confirm(self, proposal_id, *, semantic=False):
        proposal_id = UUID(str(proposal_id))
        plan = await self.repository.lookup_plan(self.request, self.grant, proposal_id)
        if plan is None:
            raise BusinessSafetyError("PLAN_DENIED")
        actions = [BusinessAction(capability=op.command.capability, arguments=op.command.arguments) for op in plan.operations]
        plan = await self.repository.confirm_plan(self.request, self.grant, plan.id, self.registration,
                                                   self.facts_resolver(actions), semantic=semantic)
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
