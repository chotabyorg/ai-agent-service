"""Typed Goal3 provider adapter. Config-only instance; each call owns its resources."""
from sqlalchemy import select
from pydantic import Field

from models.business import BusinessBinding, BusinessOperation, BusinessPlan
from models.business_connection import BusinessConnection
from schemas.business import (Ambiguous, BusinessSafetyError, MaterialFacts,
    ProvenSucceeded, StillUnknown, SucceededReceipt, DefinitiveNoEffect, ProvenFailedNoEffect,
    canonical_value, fingerprint)
from business_capabilities.chotacrm_client import ChotaCRMClient
from business_capabilities.context import AgentExecutionContext, BusinessRuntimeError
from business_capabilities.registry import REGISTRY, Risk
from business_capabilities.results import (AuthorizedContext, Output, validate_result,
    BookingProposalSummary, CancelProposalSummary, DealProposalSummary, MoveProposalSummary)


class FactsProof(Output):
    arguments: dict
    payload_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    facts_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    summary: dict


def action_key(action):
    return fingerprint('chota.crm.action.v1', action)


class ChotaCRMAdapter:
    def __init__(self, settings, session_factory, *, transport=None):
        self.client = ChotaCRMClient(settings, transport=transport)
        self.session_factory = session_factory

    async def _context(self, scope, *, connection_id=None, live=True):
        async with self.session_factory() as db:
            if connection_id is None:
                # Reload binding and its connection in one statement. This is
                # fresh on every adapter call; no authority crosses READs.
                row = await db.scalar(select(BusinessConnection).join(
                    BusinessBinding, BusinessBinding.connection_id == BusinessConnection.id).where(
                    BusinessBinding.agent_id == scope.agent_id,
                    BusinessBinding.agent_owner_id == scope.agent_owner_id,
                    BusinessBinding.owner_id == scope.owner_id,
                    BusinessBinding.integration_id == scope.integration_id,
                    BusinessBinding.channel == scope.channel,
                    BusinessBinding.active.is_(True)))
            else:
                row = await db.get(BusinessConnection, connection_id)
            if (row is None or row.agent_id != scope.agent_id or row.owner_id != scope.agent_owner_id
                    or row.grant_id is None or (live and (not row.active or row.pending_action))):
                raise BusinessSafetyError('BINDING_UNAVAILABLE')
            context = AgentExecutionContext(owner_id=row.owner_id, agent_id=row.agent_id,
                connection_id=row.id, workspace_id=row.workspace_id, grant_id=row.grant_id,
                grant_revision=row.grant_revision, session_id=scope.scope_id,
                audience='SELF_ASSERTED_CUSTOMER')
            return context, row.generation, tuple(row.enabled_capabilities)

    async def _authorized(self, scope, execution, *, connection_id=None, live=True):
        context, generation, enabled = await self._context(scope, connection_id=connection_id, live=live)
        data = await self.client.request('GET', 'context', scope='context', claims=context.claims(), execution=execution)
        remote = AuthorizedContext.model_validate(data)
        if (remote.workspace_id != context.workspace_id or remote.grant_id != context.grant_id
                or remote.revision != context.grant_revision or set(remote.capabilities) != set(enabled)):
            raise BusinessSafetyError('BINDING_UNAVAILABLE')
        return context, generation, enabled

    async def read(self, scope, action, *, execution):
        definition = REGISTRY.get(action.capability)
        if definition is None or definition.risk != Risk.READ:
            raise BusinessSafetyError('ACTION_INVALID')
        context, _, enabled = await self._authorized(scope, execution)
        if action.capability not in enabled:
            raise BusinessSafetyError('CAPABILITY_DISABLED')
        result = await self.client.request('POST', f'execute/{action.capability}', scope=action.capability,
            claims=context.claims(), body={'arguments': action.arguments}, execution=execution)
        return validate_result(action.capability, result)

    async def resolve_facts(self, scope, actions, *, execution):
        context, generation, enabled = await self._authorized(scope, execution)
        proofs = {}
        for action in actions:
            definition = REGISTRY.get(action.capability)
            if definition is None or definition.risk == Risk.READ or action.capability not in enabled:
                raise BusinessSafetyError('CAPABILITY_DISABLED')
            result = await self.client.request('POST', f'facts/{action.capability}', scope=action.capability,
                claims=context.claims(), body={'arguments': action.arguments}, execution=execution)
            if not isinstance(result, dict) or result.get('status') != 'OK':
                safe = validate_result(action.capability, result)
                raise BusinessRuntimeError(safe.get('error', {}).get('code', 'PROVIDER_UNAVAILABLE'))
            proof = FactsProof.model_validate(result['data'])
            normalized = definition.input.model_validate(proof.arguments).model_dump(mode='python', by_alias=True, exclude_none=True)
            if canonical_value(normalized) != action.arguments:
                raise BusinessSafetyError('FACTS_INVALID')
            # Facts are frozen but not exposed wholesale. Only code-owned DTO
            # projections may be used to describe the proposal to the model.
            summary_model = {'booking.create': BookingProposalSummary,
                'booking.reschedule': BookingProposalSummary, 'booking.cancel': CancelProposalSummary,
                'deal.create': DealProposalSummary, 'deal.move': MoveProposalSummary}.get(action.capability)
            summary = summary_model.model_validate(proof.summary).model_dump(mode='json', exclude_none=True) if summary_model else {}
            proofs[action_key(action)] = dict(payload_hash=proof.payload_hash, facts_hash=proof.facts_hash, summary=summary)
        return MaterialFacts(values=dict(connection_id=str(context.connection_id), grant_id=str(context.grant_id),
            actions=proofs), revisions=dict(grant=context.grant_revision, configuration=generation))

    async def execute(self, dispatch, *, execution):
        command = dispatch.command
        context, generation, enabled = await self._context(command.scope, connection_id=command.connection_id)
        if (command.capability not in enabled or generation != command.authorization_generation
                or command.fact_revisions.get('grant') != context.grant_revision):
            return Ambiguous()
        from schemas.business import BusinessAction
        proof = command.facts['actions'][action_key(BusinessAction(capability=command.capability, arguments=command.arguments))]
        operation = dict(id=str(command.operation_id), issued_at=command.issued_at.isoformat(),
                         expected_facts_hash=proof['facts_hash'])
        if REGISTRY[command.capability].risk == Risk.WRITE_CONFIRM:
            async with self.session_factory() as db:
                row = await db.get(BusinessOperation, command.operation_id)
                plan = await db.get(BusinessPlan, row.plan_id) if row else None
                if (plan is None or plan.scope_id != command.scope.scope_id or plan.proposal_state != 'CONFIRMED'
                        or plan.confirmed_turn_id is None or plan.origin_turn_id == plan.confirmed_turn_id):
                    return Ambiguous()
                operation['managed_confirmation'] = dict(proposed_turn_id=str(plan.origin_turn_id),
                    confirmed_turn_id=str(plan.confirmed_turn_id), payload_hash=proof['payload_hash'])
        try:
            result = await self.client.request('POST', f'execute/{command.capability}', scope=command.capability,
                claims=context.claims(), body=dict(arguments=command.arguments, operation=operation), execution=execution)
            return self._receipt(command, result)
        except BusinessRuntimeError:
            return Ambiguous()

    @staticmethod
    def _receipt(command, value, *, recovered=False):
        result = validate_result(command.capability, value, write=True)
        if (result.get('status') == 'ERROR' and result.get('effect') == 'NOT_APPLIED'
                and result.get('operation_id') == str(command.operation_id)):
            return (ProvenFailedNoEffect if recovered else DefinitiveNoEffect)(
                binding=command.receipt_binding(), receipt_id=result['operation_ref'],
                reason_code=result['error']['code'], evidence={'result': result})
        if result.get('status') != 'OK':
            return StillUnknown() if recovered else Ambiguous()
        if result.get('operation_id') != str(command.operation_id):
            return StillUnknown() if recovered else Ambiguous()
        data = result['data']
        object_id = data.get('ref') or data.get('note_ref')
        if not object_id:
            return StillUnknown() if recovered else Ambiguous()
        result['effect'] = 'APPLIED'
        return (ProvenSucceeded if recovered else SucceededReceipt)(binding=command.receipt_binding(),
            receipt_id=result['operation_ref'], object_id=object_id, evidence={'result': result})

    async def lookup(self, command, *, execution):
        try:
            context, _, _ = await self._context(command.scope, connection_id=command.connection_id, live=False)
            result = await self.client.request('GET', f'operations/{command.operation_id}', scope=command.capability,
                claims=context.claims(), execution=execution)
            return self._receipt(command, result, recovered=True)
        except (BusinessRuntimeError, BusinessSafetyError, ValueError):
            return StillUnknown()

    async def receipt_read_allowed(self, scope, *, execution):
        # AI's retained ledger is readable independently. Provider lookup still
        # verifies its original grant/tenant/session and customer projection.
        return True
