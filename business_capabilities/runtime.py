"""Small durable coordinator for policy, frozen consent and provider operations.

It interprets no customer text. The model selects a semantic control action;
the server owns everything that can authorize or repeat a side effect.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select, text, update
from models.business import (BusinessConfiguration, IntegrationConnection, CapabilityBinding,
    BusinessConversation, PendingBusinessAction, BusinessOperation, BusinessTurn)
from models.session import ConversationSession
from models.agent import AgentTemplate
from business_capabilities.registry import REGISTRY, WIRE_REGISTRY, Risk, control_tools, normalize_arguments
from business_capabilities.schemas import PendingReference
from business_capabilities.context import AgentExecutionContext, BusinessRuntimeError, failure
from business_capabilities.adapters.chotacrm import ChotaCRMAdapter
from services.prompt_composer import RuntimeCapabilityContext, CapabilityPrompt, PendingActionPrompt, RecoveredOutcomePrompt


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
    def __init__(self, db, settings, context, turn, *, adapter=None):
        self.db = db
        self.settings = settings
        self.context = context
        self.turn = turn
        self.turn_id = turn.id
        self.adapter = adapter or ChotaCRMAdapter(settings)
        self.enabled = frozenset()
        self.recovered_outcomes = []

    @staticmethod
    async def check_caller(db, agent, user_id, caller_namespace):
        if (user_id != agent.user_id or not caller_namespace) and await db.get(BusinessConfiguration, agent.id) is not None:
            raise BusinessRuntimeError('PERMISSION_DENIED')

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
        # Serialize only new conversation creation. Legacy session rows remain untouched.
        lock_key = int.from_bytes(bytes.fromhex(subject_hash)[:8], 'big', signed=True)
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
        conversation = await db.scalar(select(BusinessConversation).where(BusinessConversation.agent_id == agent.id,
            BusinessConversation.subject_hash == subject_hash, BusinessConversation.generation == config.generation))
        if conversation is None:
            session_id = uuid4()
            db.add(ConversationSession(id=session_id, agent_id=agent.id, external_client_id='business:' + subject_hash, channel=channel))
            await db.flush()
            conversation = BusinessConversation(id=session_id, agent_id=agent.id, subject_hash=subject_hash,
                generation=config.generation, audience='SELF_ASSERTED_CUSTOMER')
            db.add(conversation)
            await db.flush()
        session_id, generation = conversation.id, config.generation
        # Turn event id, when supplied by a trusted ingress, binds retries to one turn.
        event_id = event_id or str(uuid4())
        turn = await db.scalar(select(BusinessTurn).where(BusinessTurn.session_id == session_id, BusinessTurn.event_id == event_id))
        if turn is None:
            turn = BusinessTurn(id=uuid4(), session_id=session_id, event_id=event_id, message_hash=digest(user_message))
            db.add(turn)
        elif turn.created_at < utcnow() - timedelta(days=30):
            raise BusinessRuntimeError('OPERATION_EXPIRED')
        elif turn.message_hash != digest(user_message):
            raise BusinessRuntimeError('IDEMPOTENCY_CONFLICT')
        await db.commit()
        context = AgentExecutionContext(owner_id=agent.user_id, agent_id=agent.id,
            connection_id=connection.id if connection else UUID(int=0), workspace_id=connection.workspace_id if connection else UUID(int=0),
            grant_id=connection.grant_id if connection else UUID(int=0), grant_revision=connection.grant_revision if connection else 0,
            session_id=session_id, generation=generation)
        runtime = cls(db, settings, context, turn, adapter=adapter)
        await runtime.authorize()
        return runtime

    async def authorize(self):
        """Revalidate before history, every model request/reply and every tool."""
        await self.db.commit()  # end previous read snapshot, never hold config cache authority
        config = await self.db.get(BusinessConfiguration, self.context.agent_id, populate_existing=True)
        if config is None or config.generation != self.context.generation:
            raise BusinessRuntimeError('STALE_BINDING')
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
        for operation in operations:
            if operation.capability not in self.enabled:
                continue
            result = await self.adapter.reconcile(self.context, operation.capability, operation.id)
            if result['status'] != 'OK':
                continue
            operation.state, operation.result = 'COMPLETED', result
            self.recovered_outcomes.append(RecoveredOutcomePrompt(tool_name=REGISTRY[operation.capability].tool_name,
                result=json.dumps(result['data'], ensure_ascii=False, separators=(',', ':'))))
            await self.db.commit()
        pending = await self.pending()
        if pending and pending.state in {'UNKNOWN', 'DISPATCHING'}:
            complete = True
            for action in pending.actions:
                intent = digest([action['proposal_ref'], action['capability'], digest(action['arguments'])])
                operation = await self.db.scalar(select(BusinessOperation).where(BusinessOperation.session_id == self.context.session_id,
                    BusinessOperation.capability == action['capability'], BusinessOperation.intent_id == intent))
                complete &= operation is not None and operation.state == 'COMPLETED'
            if complete:
                pending.state = 'COMPLETED'
                await self.db.commit()

    async def prompt_context(self):
        await self.recover()
        pending = await self.pending()
        proposal = None
        if pending:
            proposal = PendingActionPrompt(action_ref=str(pending.id), tool_name='business_confirm', summary=pending.summary,
                state=pending.state, confirmable=pending.state == 'PROPOSED' and pending.proposed_turn_id != self.turn_id)
        return RuntimeCapabilityContext(available=tuple(CapabilityPrompt(tool_name=REGISTRY[c].tool_name, risk=REGISTRY[c].risk.value,
            description=REGISTRY[c].description) for c in sorted(self.enabled)), disabled=tuple(REGISTRY[c].tool_name for c in sorted(REGISTRY.keys() - self.enabled)),
            audience=self.context.audience, pending_action=proposal, recovered_outcomes=tuple(self.recovered_outcomes[-2:]))

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
                if any(not isinstance(a, dict) or set(a) != {'capability', 'arguments'} for a in actions):
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
        if row.state == 'PROPOSED':
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
                row.state = 'UNKNOWN' if result.get('error', {}).get('retry') == 'RECONCILE' else 'CANCELLED'
                await self.db.commit()
                return result if len(results) == 1 else {'status': 'ERROR', 'error': result['error'], 'completed_actions': [{'ref': r['data'].get('ref'), 'effect': r['effect'], 'operation_ref': r['operation_ref']} for r in results[:-1]]}
        row.state = 'COMPLETED'
        await self.db.commit()
        return results[0] if len(results) == 1 else {'status': 'OK', 'data': {'results': [
            {'status': 'OK', 'ref': r['data'].get('ref'), 'effect': r['effect'], 'operation_ref': r['operation_ref']} for r in results]}}

    async def reconcile_unsettled(self, *, except_id=None):
        operations = (await self.db.scalars(select(BusinessOperation).where(BusinessOperation.session_id == self.context.session_id,
            BusinessOperation.state.in_(['UNKNOWN', 'DISPATCHING'])))).all()
        for op in operations:
            if op.id == except_id:
                continue
            result = await self.adapter.reconcile(self.context, op.capability, op.id)
            if result['status'] == 'OK':
                op.state, op.result = 'COMPLETED', result
                await self.db.commit()
            else:
                # A missing/revoked/malformed receipt is not evidence of no mutation.
                return failure('UNKNOWN_WRITE_RESULT', 'RECONCILE')
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
        else:
            # Share the owner configuration lock: reconfiguration either observes
            # DISPATCHING, or this writer observes the new generation before send.
            await self.db.scalar(select(AgentTemplate).where(AgentTemplate.id == self.context.agent_id,
                AgentTemplate.user_id == self.context.owner_id).with_for_update())
            current = await self.db.get(BusinessConfiguration, self.context.agent_id, populate_existing=True)
            if current is None or current.generation != self.context.generation:
                raise BusinessRuntimeError('STALE_BINDING')
            bound = await self.db.scalar(select(CapabilityBinding).where(CapabilityBinding.agent_id == self.context.agent_id,
                CapabilityBinding.connection_id == self.context.connection_id, CapabilityBinding.capability == capability,
                CapabilityBinding.enabled.is_(True)))
            if bound is None:
                raise BusinessRuntimeError('PERMISSION_DENIED')
            # A redelivery after response loss uses the previous durable operation,
            # even when the ingress did not supply a stable event id.
            uncertain = await self.db.scalar(select(BusinessOperation).where(BusinessOperation.session_id == self.context.session_id,
                BusinessOperation.capability == capability, BusinessOperation.arguments_hash == argument_hash,
                BusinessOperation.state.in_(['UNKNOWN', 'DISPATCHING'])).limit(1))
            if uncertain:
                recovered = await self.adapter.reconcile(self.context, capability, uncertain.id)
                if recovered['status'] != 'OK':
                    return failure('UNKNOWN_WRITE_RESULT', 'RECONCILE')
                uncertain.state, uncertain.result = 'COMPLETED', recovered
                await self.db.commit()
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
