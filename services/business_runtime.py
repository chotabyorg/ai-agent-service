"""Durable business execution inside the managed shared-core extension."""

import asyncio
import json
from uuid import UUID

import anyio

from schemas.business import (AttemptRejected, BusinessAction, BusinessSafetyError, BusinessToolResult,
                              DefinitiveNoEffect, MaterialFacts, ProvenFailedNoEffect,
                              ProvenSucceeded, StillUnknown, SucceededReceipt, fingerprint)
from schemas.conversation_effects import (ExternalEffectEligibilityRequest,
    ExternalEffectEligibilityResult, ExternalEffectPermitRequest, ExternalEffectPermitResult,
    managed_request_fingerprint, sidecar_fingerprint)
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
    async def open(cls, request, grant, session_factory, registry, *, execution=None, collector=None,
                   effect_port=None):
        if not isinstance(registry, BusinessRegistry):
            raise BusinessSafetyError("REGISTRY_INVALID")
        self = cls()
        self.request, self.grant = request, grant
        self.repository = BusinessRepository(session_factory)
        self.registry, self.execution = registry, execution
        if effect_port is None:
            from core.config import get_settings
            from services.conversation_effects import build_conversation_effect_port
            effect_port = build_conversation_effect_port(get_settings(), execution)
        self.effect_port = effect_port
        self.collector = collector if collector is not None else CommittedReferenceCollector()
        from business_capabilities.connections import sync_business_binding
        await sync_business_binding(request, grant, session_factory, registry)
        context = await self.repository.open_context(request, grant, registry)
        self.scope, self.binding, self.registration = context if context else (None, None, None)
        # Explicit same-scope normalization, never a new provider command.
        if self.scope is not None:
            await self.repository.recover_attempts(request, grant)
        self.last_gate = None
        self._booking_workflow_started = False
        self._business_diagnostics_used = False
        self._business_diagnostics_terminal = False
        self._last_business_diagnostic = None
        self._business_diagnostic_observations = 0
        self.business_scope_seen = self.scope is not None
        self._proposal_decision = None
        self.effect_eligible = False
        self.effect_origin_turn_id = None
        self._prompt_context_prepared = False
        self._stale_split_plan_id = None
        self._stale_split_origin_turn_id = None
        self._stale_split_current_eligible = False
        self._stale_split_observed_request_fingerprint = None
        self._stale_split_change_committed = False
        self._stale_split_change_request_fingerprint = None
        self.recovery_mode = False
        self.recovery_plan_id = None
        await self.collect_refs()
        if self.scope is not None:
            await self.repository.invalidate_stale_proposal(request, grant)
            recovery = await self.repository.select_unresolved_recovery(request, grant)
            if recovery is not None and recovery.selection_kind == "RECOVERY":
                self.recovery_mode = True
                self.recovery_plan_id = recovery.id
                await self.repository.close_recovery_acquisition(request, grant)
                await self.collect_refs()
        return self

    def observe_business_diagnostic(self, *, stage, capability, refusal_category="NONE",
                                    lookup_source="NOT_APPLICABLE", lookup_mode="NOT_APPLICABLE",
                                    lookup_result="NOT_RUN", proposal_present=None,
                                    operation_present=None):
        """Emit a bounded observation using only closed runtime categories."""
        try:
            if getattr(self, "_business_diagnostic_observations", 0) >= 16:
                return
            request = getattr(self, "request", None)
            turn_id = getattr(request, "turn_id", None)
            if type(turn_id) is not UUID:
                return
            from core.managed_business_diagnostics import (
                ManagedBusinessDiagnostic, refusal_category as normalize_refusal,
            )
            previous = getattr(self, "_last_business_diagnostic", None)
            if previous is not None and stage in {"PROPOSAL", "TERMINAL"}:
                if lookup_source == "NOT_APPLICABLE":
                    lookup_source = previous.lookup_source
                if lookup_mode == "NOT_APPLICABLE":
                    lookup_mode = previous.lookup_mode
                if lookup_result == "NOT_RUN":
                    lookup_result = previous.lookup_result
            if proposal_present is None:
                gate = getattr(self, "last_gate", None)
                proposal_present = bool(
                    gate is not None and gate.plan is not None
                    and gate.plan.proposal_state in {"PREPARED", "CONFIRMED"}
                )
            if operation_present is None:
                collector = getattr(self, "collector", None)
                operation_present = bool(collector.snapshot()) if collector is not None else False
            diagnostic = ManagedBusinessDiagnostic(
                turn_id=turn_id, stage=stage, capability=capability,
                refusal_category=normalize_refusal(refusal_category), lookup_source=lookup_source,
                lookup_mode=lookup_mode, lookup_result=lookup_result,
                proposal_present=proposal_present, operation_present=operation_present,
            )
            from core.managed_business_diagnostics import emit_business_diagnostic
            self._business_diagnostic_observations += 1
            self._business_diagnostics_used = True
            self._last_business_diagnostic = diagnostic
            emit_business_diagnostic(diagnostic)
        except Exception:
            # Observation construction, collector inspection and logging are
            # best-effort and must never alter the managed business result.
            return

    def finish_business_diagnostics(self, final_outcome, *, proposal_present=None,
                                    operation_present=None):
        """Emit at most one terminal category after managed Business work ends."""
        try:
            if (not getattr(self, "_business_diagnostics_used", False)
                    or getattr(self, "_business_diagnostics_terminal", False)):
                return
            previous = getattr(self, "_last_business_diagnostic", None)
            if previous is None:
                return
            if final_outcome not in {"DRAFT_READY", "FAILED", "UNKNOWN"}:
                final_outcome = "UNKNOWN"
            if proposal_present is None:
                gate = getattr(self, "last_gate", None)
                proposal_present = bool(
                    gate is not None and gate.plan is not None
                    and gate.plan.proposal_state in {"PREPARED", "CONFIRMED"}
                )
            if operation_present is None:
                collector = getattr(self, "collector", None)
                operation_present = bool(collector.snapshot()) if collector is not None else False
            from dataclasses import replace
            from core.managed_business_diagnostics import emit_business_diagnostic
            terminal = replace(previous, stage="TERMINAL", proposal_present=bool(proposal_present),
                               operation_present=bool(operation_present), final_outcome=final_outcome)
            self._business_diagnostics_terminal = True
            emit_business_diagnostic(terminal)
        except Exception:
            return

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

    async def _reload_gate(self, reload):
        if reload:
            try:
                await self.collect_refs()
            except Exception:
                self.last_gate = None
                return None
        return self.last_gate

    async def execution_guard(self, *, allow_incomplete=False, reload=True):
        """Permit ordinary model/READ work while keeping RECOVERY read-only."""
        gate = await self._reload_gate(reload)
        if gate is None or gate.request_conflict:
            return self._halt(True)
        if self.recovery_mode:
            recovery = self._recovery_result(gate)
            if recovery is None or recovery.halt_status:
                return recovery or self._halt(True)
            return None
        if self._original_acquisition(gate):
            self.effect_eligible = True
            return None
        return await self._normal_guard(gate, allow_incomplete=allow_incomplete)

    async def guard_result(self, *, allow_incomplete=False, reload=True):
        """Final-answer authority gate; RECOVERY always replaces model prose."""
        gate = await self._reload_gate(reload)
        if gate is None or gate.request_conflict:
            return self._halt(True)
        if self.recovery_mode:
            return self._recovery_result(gate) or self._halt(True)
        return await self._normal_guard(gate, allow_incomplete=allow_incomplete, final_answer=True)

    final_result_guard = guard_result

    def _original_acquisition(self, gate):
        if (gate.selection_kind not in {"OWN", "CONFIRM"} or gate.plan is None
                or gate.request_conflict):
            return False
        current_digest = managed_request_fingerprint(self.request)
        return any(op.status == "RESERVED" and op.effect_request_turn_id == self.request.turn_id
                   and op.effect_request_fingerprint and op.effect_managed_request_fingerprint == current_digest
                   for op in gate.plan.operations)

    async def _normal_guard(self, gate, *, allow_incomplete=False, final_answer=False):
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
                continuation = any(op.effect_request_turn_id == self.request.turn_id
                                   for op in gate.plan.operations)
                if not self.effect_eligible and not continuation:
                    self.effect_origin_turn_id = gate.plan.origin_turn_id
                    self.effect_eligible = await self._effect_eligibility(gate.plan.origin_turn_id)
                if not self.effect_eligible and not continuation:
                    if final_answer:
                        # Ordinary model/READ work can continue without current
                        # effect authority, but its prose cannot resolve or
                        # describe this retained pending action as completed.
                        # Keep the durable PREPARED plan untouched and avoid
                        # repeating its old confirmation instruction.
                        return BusinessToolResult(
                            tool_payload={"status": "AWAITING_CONFIRMATION",
                                          "proposal_id": str(gate.plan.id),
                                          "confirmable": False},
                            safe_draft="Сохранённое предложение сейчас нельзя подтвердить. "
                                       "Действие по нему не выполнялось.",
                        )
                    return None
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

    def _recovery_result(self, gate):
        """Validate retained facts and produce a deterministic read-only outcome."""
        try:
            if (not self.recovery_mode or not gate.scope_exists or gate.plan is None
                    or gate.selection_kind != "RECOVERY" or gate.plan.id != self.recovery_plan_id):
                return self._halt(True)
            plan = gate.plan
            if not plan.operations:
                return self._halt(True)
            outcomes = []
            for operation in plan.operations:
                if operation.status in {"SUCCEEDED", "FAILED"}:
                    evidence = operation.receipt_evidence
                    if not isinstance(evidence, dict):
                        return self._halt(True)
                    if evidence.get("kind") == "NO_DISPATCH":
                        digest = fingerprint("chota.business.receipt.v1", evidence)
                        if (operation.receipt_id != "no-dispatch:" + str(operation.id)
                                or operation.receipt_fingerprint != digest
                                or operation.status != "FAILED"):
                            return self._halt(True)
                    else:
                        receipt = (SucceededReceipt.model_validate(evidence) if operation.status == "SUCCEEDED"
                                   else DefinitiveNoEffect.model_validate(evidence))
                        _, digest = self.repository._evidence(receipt, operation.command)
                        if (digest != operation.receipt_fingerprint or receipt.receipt_id != operation.receipt_id
                                or (operation.status == "SUCCEEDED" and receipt.object_id != operation.object_id)):
                            return self._halt(True)
                elif operation.receipt_evidence is not None or operation.receipt_id is not None:
                    return self._halt(True)
                outcomes.append(operation.status)
            if any(status in {"UNKNOWN", "DISPATCHING"} for status in outcomes):
                draft = (
                    f"Подтверждено выполненных действий: {outcomes.count('SUCCEEDED')}; "
                    f"подтверждено невыполненных: {outcomes.count('FAILED')}; "
                    f"не начинались: {outcomes.count('RESERVED')}. "
                    f"Результат ещё не подтверждён для действий: "
                    f"{outcomes.count('UNKNOWN') + outcomes.count('DISPATCHING')}. "
                    "Новая попытка не выполнялась."
                )
            elif all(status == "SUCCEEDED" for status in outcomes):
                draft = None
                if len(plan.operations) == 1 and self.registration is not None and self.registration.adapter_id == "chotacrm":
                    operation = plan.operations[0]
                    if operation.command.capability in {"booking.create", "booking.reschedule", "booking.cancel"}:
                        from business_capabilities.runtime_tools import completed_booking_reply
                        receipt = SucceededReceipt.model_validate(operation.receipt_evidence)
                        draft = completed_booking_reply(operation, receipt)
                draft = draft or "Предыдущее действие подтверждено как выполненное."
            else:
                draft = (
                    f"Подтверждено выполненных действий: {outcomes.count('SUCCEEDED')}; "
                    f"подтверждено невыполненных: {outcomes.count('FAILED')}; "
                    f"не выполнялись: {outcomes.count('RESERVED')}. "
                    "Повторной попытки не было."
                )
            return BusinessToolResult(tool_payload={"status": "RECOVERY_READ_ONLY", "operations": outcomes},
                                      safe_draft=draft)
        except Exception:
            return self._halt(True)

    def completed_confirmation_replay(self):
        """Reply from this confirmation Turn's validated retained booking receipt.

        The caller must first obtain a fresh result gate checking scope and
        request fingerprint. Used both at replay entry and after semantic CONFIRM;
        this grants no dispatch authority and does not alter the final guard.
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
        if operation.command.capability not in {"booking.create", "booking.reschedule", "booking.cancel"}:
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
        if self.registration is None or self.binding is None:
            return []
        if self.recovery_mode:
            if self.registration.adapter_id == "chotacrm":
                from business_capabilities.runtime_tools import canonical_tools
                return canonical_tools(self, [])
            return [self._status_tool()]
        if not self.binding.enabled_capabilities:
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
        if self._stale_split_plan_id is not None and not self._stale_split_change_committed:
            tools = [tool for tool in tools
                     if tool["function"]["name"] == "business_decide_proposal"]
            if tools:
                tools[0]["function"]["description"] = (
                    "This saved proposal is stale and cannot be confirmed. Choose CHANGE only when the complete "
                    "current customer message independently specifies a new action; a bare agreement does not "
                    "inherit its facts. REJECT and UNRELATED never authorize a replacement. Pass only this saved "
                    "proposal_id and the semantic intent; never pass new action contents."
                )
        elif self.registration.adapter_id == 'chotacrm':
            from business_capabilities.runtime_tools import canonical_tools
            tools = canonical_tools(self, tools)
        elif not self.effect_eligible:
            tools = []
        # Omission is part of the tool contract. Some OpenAI-compatible routes
        # otherwise normalize optional properties into required strict inputs.
        # This wire hint does not change authoritative closed runtime validation.
        for tool in tools:
            tool['function']['strict'] = False
        return tools

    @staticmethod
    def _status_tool():
        return {"type": "function", "function": {
            "name": "business_operation_status",
            "description": "Read the status of this Turn's retained business operation. No identifiers or arguments are accepted.",
            "parameters": {"type": "object", "additionalProperties": False}, "strict": True,
        }}

    @property
    def semantic_confirmation(self):
        return getattr(self.registration, "confirmation_mode", "REFERENCE_V1") == "SEMANTIC_V1"

    async def prompt_context(self):
        self._prompt_context_prepared = True
        if self.scope is None or self.registration is None:
            return {"pending": None}
        if self.recovery_mode:
            plan = await self.repository.selected_plan(self.request, self.grant)
            if plan is not None:
                unknown = next((op for op in plan.operations if op.status == "UNKNOWN"), None)
                if unknown is not None:
                    await self.reconcile(unknown.id)
                    await self.collect_refs()
            return {"pending": None}
        selected = await self.repository.selected_plan(self.request, self.grant)
        if self._stale_split_change_committed:
            if self._stale_split_change_request_fingerprint != managed_request_fingerprint(self.request):
                self.effect_eligible = False
                return {"pending": None}
            self.effect_origin_turn_id = self.request.turn_id
            self.effect_eligible = True
            return {"pending": None}

        self._stale_split_plan_id = None
        self._stale_split_origin_turn_id = None
        self._stale_split_current_eligible = False
        self._stale_split_observed_request_fingerprint = None
        candidate = selected
        if candidate is None and self.semantic_confirmation:
            # This narrow selector includes expired PREPARED rows and our exact
            # tombstone. It runs before pending_proposal's ordinary TTL filter.
            candidate = await self.repository.stale_split_proposal_context(
                self.request, self.grant, self.registration)

        if candidate is not None and selected is None and self.semantic_confirmation:
            old_result = await self._eligibility_result(candidate.origin_turn_id)
            designated_tombstone = candidate.proposal_state == "INVALIDATED"
            split_proven = (old_result is not None
                            and old_result.reason.value == "INTERVENING_SPLIT"
                            and old_result.authority_revision == self.request.authority_revision
                            and old_result.split_input_floor_sequence > 0)
            if designated_tombstone or split_proven:
                current_result = await self._eligibility_result(self.request.turn_id)
                self._stale_split_plan_id = candidate.id
                self._stale_split_origin_turn_id = candidate.origin_turn_id
                self._stale_split_observed_request_fingerprint = managed_request_fingerprint(self.request)
                self._stale_split_current_eligible = bool(
                    current_result is not None and current_result.eligible
                    and current_result.reason.value == "ELIGIBLE"
                    and current_result.authority_revision == self.request.authority_revision
                    and current_result.split_input_floor_sequence == 0)
                self.effect_origin_turn_id = self.request.turn_id
                self.effect_eligible = self._stale_split_current_eligible
                try:
                    summary = self._proposal_summary(candidate)
                except Exception:
                    # A generic fallback is useful for explaining the retained
                    # barrier, but an unrenderable projection cannot authorize
                    # a semantic CHANGE that retires the old proposal.
                    self._stale_split_current_eligible = False
                    self.effect_eligible = False
                    summary = "Сохранённое предложение нельзя подтвердить автоматически."
                return {"pending": {"proposal_id": str(candidate.id), "summary": summary,
                                    "confirmable": False}}

        if candidate is None:
            candidate = (await self.repository.pending_proposal(
                self.request, self.grant, self.registration)
                if self.semantic_confirmation else None)
        origin_turn_id = candidate.origin_turn_id if candidate is not None else self.request.turn_id
        self.effect_origin_turn_id = origin_turn_id
        continuation = bool(candidate is not None and any(
            op.effect_request_turn_id == self.request.turn_id for op in candidate.operations))
        result = None if continuation else await self._eligibility_result(origin_turn_id)
        self.effect_eligible = continuation or bool(result and result.eligible)
        if not self.semantic_confirmation or not self.effect_eligible:
            return {"pending": None}
        plan = await self.repository.pending_proposal(self.request, self.grant, self.registration)
        return {"pending": None if plan is None else {
            "proposal_id": str(plan.id), "summary": self._proposal_summary(plan), "confirmable": True}}

    async def _effect_eligibility(self, origin_turn_id):
        result = await self._eligibility_result(origin_turn_id)
        return bool(result and result.eligible)

    async def _eligibility_result(self, origin_turn_id):
        request = ExternalEffectEligibilityRequest(
            contract_version=1, conversation_id=self.request.conversation_id,
            turn_id=self.request.turn_id, origin_turn_id=origin_turn_id,
            owner_id=self.request.owner_id, integration_id=self.request.integration_id,
            channel=self.request.channel, agent_id=self.request.agent_id,
            authority_revision=self.request.authority_revision,
            managed_request_fingerprint=managed_request_fingerprint(self.request),
        )
        try:
            result = await self.effect_port.eligibility(request)
            if not isinstance(result, ExternalEffectEligibilityResult):
                return None
            result = ExternalEffectEligibilityResult.model_validate(result.model_dump())
            if (result.request_fingerprint != sidecar_fingerprint(request)
                    or result.eligible != (result.reason.value == "ELIGIBLE")
                    or result.authority_revision != request.authority_revision
                    or (result.reason.value == "INTERVENING_SPLIT"
                        and result.split_input_floor_sequence <= 0)
                    or (result.eligible and result.split_input_floor_sequence != 0)):
                return None
            return result
        except Exception:
            return None

    async def _ensure_effect_eligibility(self, origin_turn_id):
        if self.effect_eligible and self.effect_origin_turn_id == origin_turn_id:
            return True
        plan = await self.repository.selected_plan(self.request, self.grant)
        if plan is not None and any(op.effect_request_turn_id == self.request.turn_id
                                    and op.effect_request_fingerprint for op in plan.operations):
            self.effect_origin_turn_id = plan.origin_turn_id
            self.effect_eligible = True
            return True
        self.effect_origin_turn_id = origin_turn_id
        self.effect_eligible = await self._effect_eligibility(origin_turn_id)
        if not self.effect_eligible:
            raise BusinessSafetyError("EFFECT_AUTHORITY_UNAVAILABLE")
        return True

    async def call_tool(self, name, arguments):
        if name not in {tool["function"]["name"] for tool in self.openai_tools()}:
            raise BusinessSafetyError("TOOL_UNAVAILABLE")
        try:
            if not isinstance(arguments, dict):
                raise BusinessSafetyError("ACTION_INVALID")
            if name == "business_operation_status":
                if arguments or not self.recovery_mode:
                    raise BusinessSafetyError("ACTION_INVALID")
                gate = await self.repository.result_gate(self.request, self.grant)
                if (gate.request_conflict or gate.selection_kind != "RECOVERY" or gate.plan is None
                        or gate.plan.id != self.recovery_plan_id):
                    return self._halt(True)
                return BusinessToolResult(tool_payload={"status": "RECOVERY_READ_ONLY",
                    "operations": [{"operation_id": str(op.id), "status": op.status}
                                   for op in gate.plan.operations]})
            if self.recovery_mode:
                from business_capabilities.runtime_tools import Risk, WIRE_REGISTRY
                capability = WIRE_REGISTRY.get(name)
                if capability is None or capability.risk != Risk.READ:
                    raise BusinessSafetyError("RECOVERY_READ_ONLY", processing_status="UNKNOWN")
            elif (name in {"business_execute_plan", "business_propose", "business_confirm",
                           "business_decide_proposal"} and not self.effect_eligible
                  and self._stale_split_plan_id is None):
                raise BusinessSafetyError("EFFECT_AUTHORITY_UNAVAILABLE")
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
                if self._stale_split_plan_id is not None and not self._stale_split_change_committed:
                    if proposal_id != self._stale_split_plan_id:
                        raise BusinessSafetyError("PLAN_DENIED")
                    self._proposal_decision = decision
                    intent = arguments["intent"]
                    if intent == "CONFIRM":
                        raise BusinessSafetyError("PLAN_DENIED")
                    if intent == "REJECT":
                        return BusinessToolResult(
                            tool_payload={"status": "STALE_PROPOSAL_RETAINED", "intent": intent},
                            safe_draft="Понял. Старое предложение не будет выполнено.")
                    if intent == "UNRELATED":
                        return BusinessToolResult(
                            tool_payload={"status": "STALE_PROPOSAL_RETAINED", "intent": intent},
                            safe_draft="Уточните, пожалуйста, какое новое действие вы имеете в виду.")
                    if (not self._stale_split_current_eligible
                            or not self.effect_eligible
                            or self.effect_origin_turn_id != self.request.turn_id
                            or self._stale_split_observed_request_fingerprint
                                != managed_request_fingerprint(self.request)):
                        return BusinessToolResult(
                            tool_payload={"status": "STALE_PROPOSAL_RETAINED", "intent": intent},
                            safe_draft="Не удалось проверить новое действие. Пожалуйста, попробуйте позже.")
                    retired = await self.repository.retire_stale_split_proposal(
                        self.request, self.grant, proposal_id, self.registration)
                    if retired.id != proposal_id or retired.proposal_state != "INVALIDATED":
                        raise BusinessSafetyError("STALE_CONFIRMATION")
                    self._stale_split_change_committed = True
                    self._stale_split_change_request_fingerprint = managed_request_fingerprint(self.request)
                    self.effect_origin_turn_id = self.request.turn_id
                    self.effect_eligible = True
                    return BusinessToolResult(tool_payload={
                        "status": "STALE_PROPOSAL_RETIRED", "proposal_id": str(proposal_id),
                        "intent": "CHANGE", "new_confirmation_required": False,
                    })
                self._proposal_decision = decision
                if arguments["intent"] == "CONFIRM":
                    return await self.confirm(proposal_id, semantic=True)
                if arguments["intent"] in {"REJECT", "CHANGE"}:
                    pending = await self.repository.pending_proposal(self.request, self.grant, self.registration)
                    if pending is None or pending.id != proposal_id:
                        raise BusinessSafetyError("PLAN_DENIED")
                    await self._ensure_effect_eligibility(pending.origin_turn_id)
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
            self.observe_business_diagnostic(stage="TOOL_GUARD", capability=name,
                                             refusal_category=error.code)
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
        await self._ensure_plan_mutation_gate()
        if not isinstance(actions, (list, tuple)) or not 1 <= len(actions) <= 32:
            raise BusinessSafetyError("PLAN_BOUNDS")
        plan = await self.repository.reserve_plan(self.request, self.grant, self.registration,
                                                   actions, self.facts_resolver(actions, allow_disabled=False), propose=propose)
        self.collector.observe_committed(plan.references)
        return plan

    async def _ensure_plan_mutation_gate(self):
        if self.recovery_mode:
            raise BusinessSafetyError("RECOVERY_READ_ONLY", processing_status="UNKNOWN")
        if not self._prompt_context_prepared:
            await self.prompt_context()
        if self._stale_split_plan_id is not None and not self._stale_split_change_committed:
            raise BusinessSafetyError("STALE_CONFIRMATION")
        if (self._stale_split_change_committed
                and self._stale_split_change_request_fingerprint
                    != managed_request_fingerprint(self.request)):
            self.effect_eligible = False
        if not self.effect_eligible:
            raise BusinessSafetyError("EFFECT_AUTHORITY_UNAVAILABLE")

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
        await self._ensure_plan_mutation_gate()
        existing = await self.repository.selected_plan(self.request, self.grant)
        await self._ensure_effect_eligibility(existing.origin_turn_id if existing else self.request.turn_id)
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
        await self._ensure_plan_mutation_gate()
        existing = await self.repository.selected_plan(self.request, self.grant)
        await self._ensure_effect_eligibility(existing.origin_turn_id if existing else self.request.turn_id)
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
            acquisition = await self.repository.prepare_effect_acquisition(
                self.request, self.grant, plan.id, operation.id, self.registration,
                self.facts_resolver(actions))
            self.collector.observe_committed(acquisition.plan.references)
            plan = acquisition.plan
            candidate = next(op for op in plan.operations if op.id == identity)
            if candidate.status == "SUCCEEDED":
                continue
            if candidate.status != "RESERVED":
                if candidate.status == "FAILED" and self.registration.adapter_id == "chotacrm":
                    return self._failed_business_draft()
                return self._halt(any(op.status in {"SUCCEEDED", "DISPATCHING", "UNKNOWN"}
                                      for op in plan.operations))
            effect_request = acquisition.request
            if effect_request is None:
                return self._halt(True)
            permit_id = acquisition.permit_id
            if permit_id is None:
                try:
                    permit = await self.effect_port.permit(effect_request)
                    if (not isinstance(permit, ExternalEffectPermitResult)
                            or permit.request_fingerprint != sidecar_fingerprint(effect_request)):
                        raise ValueError
                except Exception as error:
                    from services.conversation_effects import DefinitiveEffectDenial
                    if isinstance(error, DefinitiveEffectDenial):
                        plan = await self.repository.deny_effect_acquisition(
                            self.request, self.grant, plan.id, operation.id,
                            "EXTERNAL_EFFECT_" + error.reason.value)
                        self.collector.observe_committed(plan.references)
                        return self._failed_business_draft() if self.registration.adapter_id == "chotacrm" else \
                            BusinessToolResult(tool_payload={"status": "FAILED"},
                                safe_draft="Операция не выполнялась: разрешение на действие не получено.")
                    raise BusinessSafetyError("EFFECT_ACQUISITION_UNKNOWN",
                                              processing_status="UNKNOWN") from None
                permit_id = permit.permit_id
                plan = await self.repository.bind_effect_permit(
                    self.request, self.grant, plan.id, operation.id, effect_request, permit_id)
                self.collector.observe_committed(plan.references)
                candidate = next(op for op in plan.operations if op.id == identity)
                if candidate.status == "SUCCEEDED":
                    continue
                if candidate.status != "RESERVED":
                    return self._halt(any(op.status in {"SUCCEEDED", "DISPATCHING", "UNKNOWN"}
                                          for op in plan.operations))
            decision = await self.repository.claim_dispatch(self.request, self.grant, plan.id, operation.id,
                                                            self.registration, self.facts_resolver(actions),
                                                            effect_request=effect_request)
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
        if not self._prompt_context_prepared:
            await self.prompt_context()
        if proposal_id == self._stale_split_plan_id:
            raise BusinessSafetyError("PLAN_DENIED")
        plan = await self.repository.lookup_plan(self.request, self.grant, proposal_id)
        if plan is None:
            raise BusinessSafetyError("PLAN_DENIED")
        await self._ensure_effect_eligibility(plan.origin_turn_id)
        actions = [BusinessAction(capability=op.command.capability, arguments=op.command.arguments) for op in plan.operations]
        plan = await self.repository.confirm_plan(self.request, self.grant, plan.id, self.registration,
                                                   self.facts_resolver(actions), semantic=semantic)
        self.collector.observe_committed(plan.references)
        result = await self._execute_frozen(plan)
        if (not semantic or self.registration.adapter_id != "chotacrm"
                or len(plan.operations) != 1 or plan.operations[0].command.capability not in {
                    "booking.create", "booking.reschedule", "booking.cancel"}
                or result.halt_status or result.safe_draft is not None):
            return result
        # A completed canonical confirmation is terminal for this customer Turn.
        # Reload durable scope/receipt evidence before answering, rather than
        # giving the model stale pending-proposal context and another tool step.
        guard = await self.guard_result()
        if guard is not None:
            return guard
        completed = self.completed_confirmation_replay()
        if completed is None:
            return self._halt(True)
        if completed.halt_status:
            return completed
        return result.model_copy(update={"safe_draft": completed.safe_draft})

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
        from schemas.conversation_effects import frozen_command_fingerprint
        operation_request = ExternalEffectPermitRequest(
            contract_version=1, conversation_id=self.request.conversation_id,
            turn_id=self.request.turn_id, origin_turn_id=plan.origin_turn_id,
            owner_id=self.request.owner_id, integration_id=self.request.integration_id,
            channel=self.request.channel, agent_id=self.request.agent_id,
            authority_revision=self.request.authority_revision,
            managed_request_fingerprint=managed_request_fingerprint(self.request),
            operation_namespace="main-chota.ai-business.v1", operation_id=operation.id,
            operation_fingerprint=frozen_command_fingerprint(operation.command),
        )
        decision = await self.repository.claim_dispatch(self.request, self.grant, plan.id, operation.id,
                                                        self.registration, self.facts_resolver(actions),
                                                        effect_request=operation_request, native_replay=True)
        self.collector.observe_committed(decision.plan.references)
        return await self._send(decision.token) if decision.token else decision.plan
