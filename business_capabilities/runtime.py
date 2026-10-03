"""Small durable coordinator for policy, frozen consent and provider operations.

It interprets no customer text. The model selects a semantic control action;
the server owns everything that can authorize or repeat a side effect.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select, text, update
from models.business import (BusinessConfiguration, IntegrationConnection, CapabilityBinding,
    BusinessConversation, PendingBusinessAction, BusinessOperation, BusinessTurn, BusinessConnectionIntent)
from models.session import ConversationSession
from models.agent import AgentTemplate
from business_capabilities.registry import REGISTRY, WIRE_REGISTRY, Risk, control_tools, normalize_arguments
from business_capabilities.schemas import PendingReference
from business_capabilities.context import AgentExecutionContext, BusinessRuntimeError, failure
from business_capabilities.adapters.chotacrm import ChotaCRMAdapter
from services.prompt_composer import RuntimeCapabilityContext, CapabilityPrompt, PendingActionPrompt, PendingActionProgress, RecoveredOutcomePrompt


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc)


@asynccontextmanager
async def conversation_lock(db, session_id):
    """Dedicated session advisory lock survives ledger commits and worker crashes."""
    key = int.from_bytes(hashlib.sha256(str(session_id).encode()).digest()[:8], 'big', signed=True)
    async with db.bind.connect() as connection:
        locked = await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key})
        if not locked:
            raise BusinessRuntimeError('OPERATION_IN_PROGRESS')
        try:
            yield
        finally:
            await connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
            await connection.commit()


class CapabilityRuntime:
    def __init__(self, db, settings, context, turn, *, adapter=None, lineage_id=None):
        self.db = db
        self.settings = settings
        self.context = context
        self.lineage_id = lineage_id or context.session_id
        self.turn = turn
        self.turn_id = turn.id
        self.adapter = adapter or ChotaCRMAdapter(settings)
        self.enabled = frozenset()
        self.recovered_outcomes = []

    @staticmethod
    async def check_caller(db, agent, user_id, caller_namespace):
        if (user_id != agent.user_id or not caller_namespace) and await db.get(BusinessConfiguration, agent.id) is not None:
            raise BusinessRuntimeError('PERMISSION_DENIED')

    @staticmethod
    def validate_turn(turn, user_message):
        if turn.created_at < utcnow() - timedelta(days=30):
            raise BusinessRuntimeError('OPERATION_EXPIRED')
        if turn.message_hash != digest(user_message):
            raise BusinessRuntimeError('IDEMPOTENCY_CONFLICT')

    @classmethod
    async def open(cls, db, settings, agent, external_client_id, channel, caller_namespace, user_id,
                   *, event_id=None, user_message='', adapter=None):
        config = await db.get(BusinessConfiguration, agent.id)
        if config is None:
            return None
        if user_id != agent.user_id or not caller_namespace:
            raise BusinessRuntimeError('PERMISSION_DENIED')
        if not settings.business_capabilities_enabled:
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
        connection = await db.scalar(select(IntegrationConnection).where(IntegrationConnection.agent_id == agent.id, IntegrationConnection.active.is_(True)))
        subject_hash = digest([str(agent.id), caller_namespace, channel, external_client_id, 'SELF_ASSERTED_CUSTOMER'])
        lock_key = int.from_bytes(bytes.fromhex(subject_hash)[:8], 'big', signed=True)
        generation, agent_id, owner_id = config.generation, agent.id, agent.user_id
        event_id = event_id or str(uuid4())
        for _ in range(2):
            # Serialize creation/rotation; old rows and operation evidence survive.
            await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
            conversation = await db.scalar(select(BusinessConversation).where(BusinessConversation.agent_id == agent_id,
                BusinessConversation.subject_hash == subject_hash, BusinessConversation.generation == generation,
                BusinessConversation.retired.is_(False)).execution_options(populate_existing=True))
            if conversation is None:
                ancestor = await db.scalar(select(BusinessConversation).where(BusinessConversation.agent_id == agent_id,
                    BusinessConversation.subject_hash == subject_hash, BusinessConversation.generation == generation).limit(1))
                session_id = uuid4()
                db.add(ConversationSession(id=session_id, agent_id=agent_id, external_client_id='business:' + subject_hash, channel=channel))
                await db.flush()
                conversation = BusinessConversation(id=session_id, agent_id=agent_id, subject_hash=subject_hash,
                    generation=generation, audience='SELF_ASSERTED_CUSTOMER', retired=False,
                    lineage_id=ancestor.lineage_id if ancestor else session_id)
                db.add(conversation)
                await db.flush()
            session_id, lineage_id = conversation.id, conversation.lineage_id
            # Do not persist a new turn until customer authority is current: a
            # first fresh utterance must survive rotation, but old event replay must not.
            turn = await db.scalar(select(BusinessTurn).where(BusinessTurn.session_id == session_id, BusinessTurn.event_id == event_id))
            if turn is None:
                turn = BusinessTurn(id=uuid4(), session_id=session_id, event_id=event_id, message_hash=digest(user_message))
            else:
                cls.validate_turn(turn, user_message)
            await db.commit()
            context = AgentExecutionContext(owner_id=owner_id, agent_id=agent_id,
                connection_id=connection.id if connection else UUID(int=0), workspace_id=connection.workspace_id if connection else UUID(int=0),
                grant_id=connection.grant_id if connection else UUID(int=0), grant_revision=connection.grant_revision if connection else 0,
                session_id=session_id, generation=generation)
            runtime = cls(db, settings, context, turn, adapter=adapter, lineage_id=lineage_id)
            try:
                await runtime.authorize()
            except BusinessRuntimeError as exc:
                if exc.code != 'STALE_CUSTOMER_CONTEXT':
                    raise
                await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
                await db.execute(update(BusinessConversation).where(BusinessConversation.id == session_id).values(retired=True))
                await db.execute(update(PendingBusinessAction).where(PendingBusinessAction.session_id == session_id,
                    PendingBusinessAction.state == 'PROPOSED').values(state='CANCELLED'))
                await db.commit()
                continue
            # Provider authorization released the original creation lock. A
            # concurrent delivery may now own this event or have retired its
            # context; arbitrate again and reload before creating a durable turn.
            await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
            current_config = await db.get(BusinessConfiguration, agent_id, populate_existing=True)
            if current_config is None or current_config.generation != generation:
                raise BusinessRuntimeError('STALE_BINDING')
            if await db.get(BusinessConnectionIntent, agent_id, populate_existing=True):
                raise BusinessRuntimeError('CONNECTION_PENDING')
            current_conversation = await db.get(BusinessConversation, session_id, populate_existing=True)
            if current_conversation is None or current_conversation.retired:
                await db.commit()
                continue
            retired_event = await db.scalar(select(BusinessTurn.id).join(BusinessConversation, BusinessTurn.session_id == BusinessConversation.id).where(
                BusinessConversation.lineage_id == lineage_id, BusinessConversation.retired.is_(True), BusinessTurn.event_id == event_id).limit(1))
            if retired_event:
                raise BusinessRuntimeError('STALE_CUSTOMER_CONTEXT')
            saved_turn = await db.scalar(select(BusinessTurn).where(BusinessTurn.session_id == session_id,
                BusinessTurn.event_id == event_id).execution_options(populate_existing=True))
            if saved_turn is not None:
                cls.validate_turn(saved_turn, user_message)
                runtime.turn, runtime.turn_id = saved_turn, saved_turn.id
            else:
                db.add(turn)
            await db.commit()
            return runtime
        raise BusinessRuntimeError('STALE_CUSTOMER_CONTEXT')

    async def authorize(self):
        """Revalidate before history, every model request/reply and every tool."""
        await self.db.commit()  # end previous read snapshot, never hold config cache authority
        config = await self.db.get(BusinessConfiguration, self.context.agent_id, populate_existing=True)
        if config is None or config.generation != self.context.generation:
            raise BusinessRuntimeError('STALE_BINDING')
        if await self.db.get(BusinessConnectionIntent, self.context.agent_id, populate_existing=True):
            raise BusinessRuntimeError('CONNECTION_PENDING')
        conversation = await self.db.get(BusinessConversation, self.context.session_id, populate_existing=True)
        if conversation is None or conversation.retired:
            raise BusinessRuntimeError('STALE_CUSTOMER_CONTEXT')
        connection = await self.db.scalar(select(IntegrationConnection).where(IntegrationConnection.agent_id == self.context.agent_id,
            IntegrationConnection.active.is_(True)).execution_options(populate_existing=True))
        if connection is None:
            if self.context.connection_id.int:
                raise BusinessRuntimeError('CONNECTION_REVOKED')
            self.enabled = frozenset()
            return
        if connection.provider != 'chotacrm':
            raise BusinessRuntimeError('UNSUPPORTED_CAPABILITY')
        if connection.id != self.context.connection_id or connection.owner_id != self.context.owner_id or connection.grant_revision != self.context.grant_revision:
            raise BusinessRuntimeError('STALE_BINDING')
        authorized = await self.adapter.context(self.context)
        local = set((await self.db.scalars(select(CapabilityBinding.capability).where(CapabilityBinding.agent_id == self.context.agent_id,
            CapabilityBinding.connection_id == connection.id, CapabilityBinding.enabled.is_(True)))).all())
        self.enabled = frozenset(local & set(authorized.capabilities) & self.adapter.supported_capabilities)
        self.timezone = authorized.timezone

    async def pending(self):
        row = await self.db.scalar(select(PendingBusinessAction).where(PendingBusinessAction.session_id == self.context.session_id,
            PendingBusinessAction.generation == self.context.generation,
            PendingBusinessAction.state.in_(['PROPOSED', 'DISPATCHING', 'UNKNOWN'])).order_by(PendingBusinessAction.created_at.desc()).limit(1))
        if row and row.state == 'PROPOSED' and row.expires_at <= utcnow():
            row.state = 'EXPIRED'
            await self.db.commit()
            return None
        return row

    async def recover(self):
        """Check uncertain receipts automatically; this never dispatches a write."""
        operations = (await self.db.scalars(select(BusinessOperation).where(BusinessOperation.session_id == self.context.session_id,
            BusinessOperation.state.in_(['UNKNOWN', 'DISPATCHING'])).order_by(BusinessOperation.issued_at).limit(20))).all()
        reported = set()
        for operation in operations:
            if operation.capability not in self.enabled:
                continue
            result = await self.adapter.reconcile(self.context, operation.capability, operation.id)
            if result['status'] != 'OK':
                continue
            await self.record_recovery(operation, result)
            self.recovered_outcomes.append(RecoveredOutcomePrompt(tool_name=REGISTRY[operation.capability].tool_name,
                result=json.dumps(result['data'], ensure_ascii=False, separators=(',', ':'))))
            reported.add(operation.id)
        await self.settle_pending(self.context.session_id, report_completed=True, reported=reported)

    async def record_recovery(self, operation, result):
        """Commit the receipt and this turn's write fence as one durable fact."""
        if operation.state in {'UNKNOWN', 'DISPATCHING'}:
            await self.db.execute(update(BusinessTurn).where(BusinessTurn.id == self.turn_id).values(write_suppressed=True))
        operation.state, operation.result = 'COMPLETED', result
        await self.db.commit()

    async def write_is_suppressed(self):
        # Read durable state, including when the same event was reopened in a
        # fresh worker. The fence applies to fresh writes, never receipt reads.
        return bool(await self.db.scalar(select(BusinessTurn.write_suppressed).where(BusinessTurn.id == self.turn_id)))

    async def settle_pending(self, session_id, *, report_completed=False, reported=frozenset()):
        pending_rows = (await self.db.scalars(select(PendingBusinessAction).where(PendingBusinessAction.session_id == session_id,
            PendingBusinessAction.state.in_(['UNKNOWN', 'DISPATCHING'])))).all()
        for pending in pending_rows:
            completed = []
            for action in pending.actions:
                intent = digest([action['proposal_ref'], action['capability'], digest(action['arguments'])])
                operation = await self.db.scalar(select(BusinessOperation).where(BusinessOperation.session_id == session_id,
                    BusinessOperation.capability == action['capability'], BusinessOperation.intent_id == intent))
                if operation is not None and operation.state == 'COMPLETED':
                    completed.append(operation)
            if len(completed) != len(pending.actions):
                continue
            outcomes = []
            if report_completed:
                # The last operation may have committed before the worker could
                # settle the proposal. Preserve its result instead of silently
                # dropping it from the next model context or authorizing a repeat.
                await self.db.execute(update(BusinessTurn).where(BusinessTurn.id == self.turn_id).values(write_suppressed=True))
                await self.db.commit()
                for operation in completed:
                    if operation.capability not in self.enabled:
                        break
                    result = operation.result if operation.id in reported else await self.adapter.reconcile(self.context, operation.capability, operation.id)
                    if result is None or result['status'] != 'OK':
                        break
                    outcomes.append((operation, result))
                if len(outcomes) != len(completed):
                    continue
            pending.state = 'COMPLETED'
            await self.db.commit()
            for operation, result in outcomes:
                if operation.id not in reported:
                    self.recovered_outcomes.append(RecoveredOutcomePrompt(tool_name=REGISTRY[operation.capability].tool_name,
                        result=json.dumps(result['data'], ensure_ascii=False, separators=(',', ':'))))

    async def prompt_context(self):
        await self.recover()
        # Receipt recovery can await provider access; recheck its customer context
        # before exposing recovered facts or a pending summary to the model.
        await self.authorize()
        pending = await self.pending()
        proposal = None
        if pending:
            progress = await self.pending_progress(pending)
            statuses = {step.status for step in progress}
            partial = statuses == {'COMPLETED', 'UNSTARTED'}
            approved = pending.confirmed_turn_id is not None and pending.state in {'UNKNOWN', 'DISPATCHING'}
            unstarted = statuses == {'UNSTARTED'}
            state = ('EXPIRED' if 'EXPIRED' in statuses else 'PARTIALLY_COMPLETED' if partial
                else 'APPROVED' if approved and unstarted else pending.state)
            permitted = all(action['capability'] in self.enabled for action in pending.actions)
            lineage_sessions = select(BusinessConversation.id).where(BusinessConversation.lineage_id == self.lineage_id)
            unsettled = await self.db.scalar(select(BusinessOperation.id).where(BusinessOperation.session_id.in_(lineage_sessions),
                BusinessOperation.state.in_(['UNKNOWN', 'DISPATCHING'])).limit(1))
            eligible_turn = pending.proposed_turn_id != self.turn_id and not await self.write_is_suppressed() and unsettled is None
            proposal = PendingActionPrompt(action_ref=str(pending.id), tool_name='business_confirm', summary=pending.summary,
                state=state, confirmable=pending.state == 'PROPOSED' and unstarted and permitted and eligible_turn,
                continuable=approved and (partial or unstarted) and permitted and eligible_turn,
                progress=progress)
        return RuntimeCapabilityContext(available=tuple(CapabilityPrompt(tool_name=REGISTRY[c].tool_name, risk=REGISTRY[c].risk.value,
            description=REGISTRY[c].description) for c in sorted(self.enabled)), disabled=tuple(REGISTRY[c].tool_name for c in sorted(REGISTRY.keys() - self.enabled)),
            audience=self.context.audience, pending_action=proposal, recovered_outcomes=tuple(self.recovered_outcomes[-2:]))

    async def pending_progress(self, pending):
        progress = []
        for index, action in enumerate(pending.actions, start=1):
            intent = digest([action['proposal_ref'], action['capability'], digest(action['arguments'])])
            operation = await self.db.scalar(select(BusinessOperation).where(BusinessOperation.session_id == pending.session_id,
                BusinessOperation.capability == action['capability'], BusinessOperation.intent_id == intent))
            if operation is None:
                status = 'EXPIRED' if datetime.fromisoformat(action['expires_at'].replace('Z', '+00:00')) <= utcnow() else 'UNSTARTED'
            else:
                status = {'COMPLETED': 'COMPLETED', 'FAILED': 'FAILED', 'EXPIRED': 'EXPIRED'}.get(operation.state, 'UNRESOLVED')
            progress.append(PendingActionProgress(step=index, tool_name=REGISTRY[action['capability']].tool_name, status=status))
        return tuple(progress)

    def tools(self):
        tools = [REGISTRY[c].tool() for c in sorted(self.enabled)]
        if any(REGISTRY[c].risk == Risk.WRITE_CONFIRM for c in self.enabled):
            tools.extend(control_tools())
        return tools

    async def call(self, tool_name, arguments):
        try:
            await self.authorize()
            if tool_name in {'business_confirm', 'business_cancel'}:
                ref = PendingReference.model_validate(arguments).pending_action_ref
                return await self.confirm(ref) if tool_name == 'business_confirm' else await self.cancel(ref)
            if tool_name == 'business_propose':
                if not isinstance(arguments, dict) or set(arguments) != {'actions'}:
                    return failure('VALIDATION_ERROR')
                actions = arguments['actions']
                if not isinstance(actions, list) or not 1 <= len(actions) <= 2:
                    return failure('VALIDATION_ERROR')
                if any(not isinstance(a, dict) or set(a) != {'capability', 'arguments'}
                       or not isinstance(a['capability'], str) or not isinstance(a['arguments'], dict) for a in actions):
                    return failure('VALIDATION_ERROR')
                if len(actions) == 2 and {a['capability'] for a in actions} != {'booking.create', 'deal.create'}:
                    return failure('VALIDATION_ERROR')
                if len(actions) == 2 and actions[0]['arguments'].get('customer_ref') != actions[1]['arguments'].get('customer_ref'):
                    return failure('VALIDATION_ERROR')
                return await self.propose(actions)
            capability = WIRE_REGISTRY.get(tool_name)
            if capability is None or capability.id not in self.enabled:
                return failure('PERMISSION_DENIED')
            args = normalize_arguments(capability.id, arguments)
            if capability.risk == Risk.READ:
                return await self.adapter.execute(self.context, capability.id, args)
            if capability.risk == Risk.WRITE_CONFIRM:
                return await self.propose([{'capability': capability.id, 'arguments': args}])
            return await self.write(capability.id, args)
        except (ValidationError, ValueError, TypeError, KeyError):
            return failure('VALIDATION_ERROR')
        except BusinessRuntimeError as exc:
            return failure(exc.code)

    async def propose(self, actions):
        for action in actions:
            c = REGISTRY.get(action['capability'])
            if c is None or c.id not in self.enabled or c.risk != Risk.WRITE_CONFIRM:
                return failure('PERMISSION_DENIED')
            action['arguments'] = normalize_arguments(c.id, action['arguments'])
        unsettled = await self.reconcile_unsettled()
        if unsettled:
            return unsettled
        previous = await self.pending()
        if previous and [{'capability': a['capability'], 'arguments': a['arguments']} for a in previous.actions] == actions:
            return self.proposal_result(previous)
        prepared = []
        for action in actions:
            result = await self.adapter.prepare(self.context, action['capability'], action['arguments'], self.turn_id)
            if result.get('status') != 'OK':
                return result
            payload = result['data']
            # Provider normalized arguments are frozen only after canonical validation.
            normalize_arguments(action['capability'], payload['arguments'])
            prepared.append({'capability': action['capability'], **payload})
        summary = '; '.join(json.dumps(a['summary'], ensure_ascii=False, separators=(',', ':')) for a in prepared)
        if len(summary) > 8000 or len(json.dumps({'actions': prepared}, ensure_ascii=False).encode()) > 16000:
            return failure('VALIDATION_ERROR')
        if previous:
            previous.state = 'CANCELLED'
        row = PendingBusinessAction(id=uuid4(), session_id=self.context.session_id, connection_id=self.context.connection_id,
            generation=self.context.generation, proposed_turn_id=self.turn_id, actions=prepared,
            summary=summary, state='PROPOSED',
            expires_at=min(datetime.fromisoformat(a['expires_at'].replace('Z', '+00:00')) for a in prepared))
        self.db.add(row)
        await self.db.commit()
        return self.proposal_result(row)

    @staticmethod
    def proposal_result(row):
        return {'status': 'ERROR', 'error': {'code': 'CONFIRMATION_REQUIRED', 'message': 'Ask the customer to confirm these business facts.', 'retry': 'NEVER'},
                'pending_action': {'ref': str(row.id), 'summary': row.summary, 'expires_at': row.expires_at.isoformat()}}

    async def action_by_ref(self, ref):
        row = await self.db.get(PendingBusinessAction, UUID(ref), populate_existing=True)
        if (row is None or row.session_id != self.context.session_id or row.connection_id != self.context.connection_id or row.generation != self.context.generation):
            raise BusinessRuntimeError('NOT_FOUND')
        return row

    async def cancel(self, ref):
        row = await self.action_by_ref(ref)
        if row.state != 'PROPOSED':
            return failure('STALE_PROPOSAL')
        row.state = 'CANCELLED'
        await self.db.commit()
        return {'status': 'OK', 'data': {'cancelled': True}}

    async def confirm(self, ref):
        row = await self.action_by_ref(ref)
        if row.proposed_turn_id == self.turn_id:
            return failure('CONFIRMATION_REQUIRED')
        if row.state not in {'PROPOSED', 'COMPLETED', 'UNKNOWN', 'DISPATCHING'} or (row.state == 'PROPOSED' and row.expires_at <= utcnow()):
            return failure('STALE_PROPOSAL')
        if any(a['capability'] not in self.enabled for a in row.actions):
            return failure('PERMISSION_DENIED')
        if any(step.status == 'EXPIRED' for step in await self.pending_progress(row)):
            return failure('STALE_PROPOSAL')
        if row.state == 'PROPOSED':
            unsettled = await self.reconcile_unsettled()
            if unsettled:
                return unsettled
            row.confirmed_turn_id = self.turn_id
            row.state = 'DISPATCHING'
            await self.db.commit()
        results = []
        for action in row.actions:
            confirmation = {'proposal_ref': action['proposal_ref'], 'proposed_turn_id': str(row.proposed_turn_id),
                'confirmed_turn_id': str(row.confirmed_turn_id), 'payload_hash': action['payload_hash']}
            result = await self.write(action['capability'], action['arguments'], confirmation)
            results.append(result)
            if result['status'] != 'OK':
                # A recovery fence pauses an unstarted composition leg without
                # consuming or replacing the already confirmed frozen proposal.
                if result.get('error', {}).get('code') != 'PREVIOUS_ACTION_COMPLETED':
                    row.state = 'UNKNOWN' if result.get('error', {}).get('retry') == 'RECONCILE' else 'CANCELLED'
                await self.db.commit()
                return result if len(results) == 1 else {'status': 'ERROR', 'error': result['error'], 'completed_actions': [{'ref': r['data'].get('ref'), 'effect': r['effect'], 'operation_ref': r['operation_ref']} for r in results[:-1]]}
        row.state = 'COMPLETED'
        await self.db.commit()
        return results[0] if len(results) == 1 else {'status': 'OK', 'data': {'results': [
            {'status': 'OK', 'ref': r['data'].get('ref'), 'effect': r['effect'], 'operation_ref': r['operation_ref']} for r in results]}}

    async def reconcile_unsettled(self, *, except_id=None):
        lineage_sessions = select(BusinessConversation.id).where(BusinessConversation.lineage_id == self.lineage_id)
        operations = (await self.db.scalars(select(BusinessOperation).where(BusinessOperation.session_id.in_(lineage_sessions),
            BusinessOperation.state.in_(['UNKNOWN', 'DISPATCHING'])))).all()
        for op in operations:
            if op.id == except_id:
                continue
            # Reconciliation stays in the original provider session. It cannot
            # mint a new mutation or expose retired customer facts to the model.
            context = replace(self.context, session_id=op.session_id)
            result = await self.adapter.reconcile(context, op.capability, op.id)
            if result['status'] == 'OK':
                await self.record_recovery(op, result)
                await self.settle_pending(op.session_id)
            else:
                return failure('UNKNOWN_WRITE_RESULT', 'RECONCILE')
        if await self.write_is_suppressed():
            # Every subsequent model call/reopened event remains fenced, even
            # though successful recovery removed the unresolved operation state.
            return failure('PREVIOUS_ACTION_COMPLETED')
        return None

    async def write(self, capability, arguments, confirmation=None):
        await self.authorize()
        if capability not in self.enabled:
            return failure('PERMISSION_DENIED')
        argument_hash = digest(arguments)
        intent_id = digest([confirmation['proposal_ref'] if confirmation else str(self.turn_id), capability, argument_hash])
        operation = await self.db.scalar(select(BusinessOperation).where(BusinessOperation.session_id == self.context.session_id,
            BusinessOperation.capability == capability, BusinessOperation.intent_id == intent_id))
        if operation:
            if operation.state == 'EXPIRED':
                return failure('OPERATION_EXPIRED')
            if operation.state == 'FAILED':
                return operation.result
            # A saved success is reauthorized and reconciled at the provider too.
            result = await self.adapter.reconcile(self.context, capability, operation.id)
            if result['status'] != 'OK':
                return failure('UNKNOWN_WRITE_RESULT', 'RECONCILE')
            await self.record_recovery(operation, result)
            return result
        else:
            # Share the owner configuration lock: reconfiguration either observes
            # DISPATCHING, or this writer observes the new generation before send.
            await self.db.scalar(select(AgentTemplate).where(AgentTemplate.id == self.context.agent_id,
                AgentTemplate.user_id == self.context.owner_id).with_for_update())
            current = await self.db.get(BusinessConfiguration, self.context.agent_id, populate_existing=True)
            if current is None or current.generation != self.context.generation:
                raise BusinessRuntimeError('STALE_BINDING')
            if await self.db.get(BusinessConnectionIntent, self.context.agent_id, populate_existing=True):
                raise BusinessRuntimeError('CONNECTION_PENDING')
            bound = await self.db.scalar(select(CapabilityBinding).where(CapabilityBinding.agent_id == self.context.agent_id,
                CapabilityBinding.connection_id == self.context.connection_id, CapabilityBinding.capability == capability,
                CapabilityBinding.enabled.is_(True)))
            if bound is None:
                raise BusinessRuntimeError('PERMISSION_DENIED')
            if await self.write_is_suppressed():
                return failure('PREVIOUS_ACTION_COMPLETED')
            # A redelivery after response loss uses the previous durable operation,
            # even when the ingress did not supply a stable event id.
            uncertain = await self.db.scalar(select(BusinessOperation).where(BusinessOperation.session_id == self.context.session_id,
                BusinessOperation.capability == capability, BusinessOperation.arguments_hash == argument_hash,
                BusinessOperation.state.in_(['UNKNOWN', 'DISPATCHING'])).limit(1))
            if uncertain:
                recovered = await self.adapter.reconcile(self.context, capability, uncertain.id)
                if recovered['status'] != 'OK':
                    return failure('UNKNOWN_WRITE_RESULT', 'RECONCILE')
                await self.record_recovery(uncertain, recovered)
                return recovered
            unsettled = await self.reconcile_unsettled()
            if unsettled:
                return unsettled
            if REGISTRY[capability].risk == Risk.WRITE_CONFIRM and not confirmation:
                return failure('CONFIRMATION_REQUIRED')
            operation = BusinessOperation(id=uuid4(), session_id=self.context.session_id, connection_id=self.context.connection_id,
                capability=capability, intent_id=intent_id, arguments_hash=argument_hash, arguments=arguments, confirmation=confirmation,
                state='DISPATCHING', issued_at=utcnow())
            self.db.add(operation)
            # Persist the operation before dispatch. A crash now stays uncertain; never fresh retry.
            await self.db.commit()
            metadata = {'id': str(operation.id), 'issued_at': operation.issued_at.isoformat()}
            if confirmation:
                metadata['confirmation'] = confirmation
            result = await self.adapter.execute(self.context, capability, arguments, metadata)
        if result['status'] == 'OK':
            operation.state = 'COMPLETED'
        elif result.get('error', {}).get('retry') == 'RECONCILE':
            operation.state = 'UNKNOWN'
        else:
            operation.state = 'FAILED'
        operation.result = result
        await self.db.commit()
        return result

    async def finish(self, answer):
        await self.authorize()
        self.turn.answer = answer
        await self.db.commit()
