"""Owner-authorized first-party connection management; no credentials accepted."""
from uuid import UUID, uuid4
from sqlalchemy import delete, select
from models.agent import AgentTemplate
from models.business import BusinessConfiguration, IntegrationConnection, CapabilityBinding, BusinessOperation, BusinessConnectionIntent
from business_capabilities.registry import CAPABILITIES, REGISTRY, DEFAULTS
from business_capabilities.context import BusinessRuntimeError
from business_capabilities.adapters.chotacrm import ChotaCRMAdapter


class BusinessConnectionService:
    def __init__(self, settings, adapter=None):
        self.adapter = adapter or ChotaCRMAdapter(settings)

    async def owner(self, db, agent_id, owner_id, *, lock=False):
        stmt = select(AgentTemplate).where(AgentTemplate.id == agent_id, AgentTemplate.user_id == owner_id)
        if lock:
            stmt = stmt.with_for_update()
        agent = await db.scalar(stmt)
        if agent is None:
            raise BusinessRuntimeError('NOT_FOUND')
        return agent

    async def connection(self, db, agent_id):
        return await db.scalar(select(IntegrationConnection).where(IntegrationConnection.agent_id == agent_id, IntegrationConnection.active.is_(True)).execution_options(populate_existing=True))

    async def state(self, db, agent_id, workspaces):
        connection = await self.connection(db, agent_id)
        pending = await db.get(BusinessConnectionIntent, agent_id, populate_existing=True)
        workspace = None
        enabled = set()
        if connection or pending:
            workspace_id = connection.workspace_id if connection else pending.workspace_id
            workspace = next((w for w in workspaces['items'] if str(w['id']) == str(workspace_id)), None)
            if workspace is None:
                if pending:
                    # Previously verified descriptor only; no business data or current
                    # membership is inferred. Owner can still cancel lost delegation.
                    workspace = {'id': str(pending.workspace_id), 'name': pending.workspace_name, 'timezone': pending.timezone}
                else:
                    raise BusinessRuntimeError('PERMISSION_DENIED')
        if connection and not pending:
            enabled = set((await db.scalars(select(CapabilityBinding.capability).where(CapabilityBinding.agent_id == agent_id,
                CapabilityBinding.connection_id == connection.id, CapabilityBinding.enabled.is_(True)))).all())
        return {'connected': bool(connection), 'pending_connection_action': pending.action if pending else None, 'runtime_mode': 'CANONICAL' if await db.get(BusinessConfiguration, agent_id) else 'LEGACY', 'workspace': workspace,
                'capabilities': [{'id': c.id, 'family': c.family, 'risk': c.risk.value,
                                  'enabled': c.id in enabled, 'default_enabled': c.default_enabled} for c in CAPABILITIES]}

    async def ensure_settled(self, db, connection):
        if connection and await db.scalar(select(BusinessOperation.id).where(BusinessOperation.connection_id == connection.id,
                BusinessOperation.state.in_(['DISPATCHING', 'UNKNOWN'])).limit(1)):
            raise BusinessRuntimeError('UNKNOWN_WRITE_RESULT')

    async def configure(self, db, agent_id, owner_id, cookie, *, workspace_id=None, enabled=None):
        await self.owner(db, agent_id, owner_id, lock=True)
        current = await self.connection(db, agent_id)
        pending = await db.get(BusinessConnectionIntent, agent_id, populate_existing=True)
        await self.ensure_settled(db, current)
        if pending and pending.action != 'CONNECT':
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        if workspace_id is None:
            if not current and not pending:
                raise BusinessRuntimeError('NOT_FOUND')
            workspace_id = current.workspace_id if current else pending.workspace_id
        if (current and current.workspace_id != workspace_id) or (pending and pending.workspace_id != workspace_id):
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        workspaces = await self.adapter.workspaces(owner_id, cookie)
        workspace = next((w for w in workspaces['items'] if str(w['id']) == str(workspace_id)), None)
        if not workspace:
            raise BusinessRuntimeError('PERMISSION_DENIED')
        if enabled is None:
            if pending:
                enabled = pending.capabilities
            elif current and current.workspace_id == workspace_id:
                enabled = list((await db.scalars(select(CapabilityBinding.capability).where(CapabilityBinding.agent_id == agent_id, CapabilityBinding.enabled.is_(True)))).all())
            else:
                enabled = list(DEFAULTS)
        if len(enabled) != len(set(enabled)) or set(enabled) - REGISTRY.keys():
            raise BusinessRuntimeError('VALIDATION_ERROR')
        if pending and set(enabled) != set(pending.capabilities):
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        if current is None:
            if pending is None:
                pending = BusinessConnectionIntent(agent_id=agent_id, owner_id=owner_id, connection_id=uuid4(),
                    workspace_id=workspace_id, workspace_name=workspace['name'], timezone=workspace['timezone'],
                    capabilities=list(enabled), action='CONNECT')
                db.add(pending)
                config = await db.get(BusinessConfiguration, agent_id)
                if config is None:
                    db.add(BusinessConfiguration(agent_id=agent_id, generation=uuid4()))
                else:
                    config.generation = uuid4()
                # This commit is deliberately before the remote mutation. No grant,
                # bindings or cookie are stored as execution authority at this point.
                await db.commit()
            connection_id = pending.connection_id
            # The intent commit released the row lock; recheck after reacquiring.
            # A concurrent request may already have completed or cancelled it.
            await self.owner(db, agent_id, owner_id, lock=True)
            pending = await db.get(BusinessConnectionIntent, agent_id, populate_existing=True)
            current = await self.connection(db, agent_id)
            if current:
                if current.id != connection_id:
                    raise BusinessRuntimeError('CONNECTION_CONFLICT')
                return await self.state(db, agent_id, workspaces)
            if pending is None or pending.connection_id != connection_id or pending.action != 'CONNECT':
                raise BusinessRuntimeError('CONNECTION_CONFLICT')
        else:
            connection_id = current.id
        grant = await self.adapter.configure(owner_id, agent_id, connection_id, workspace_id, enabled, cookie)
        if set(grant.capabilities) != set(enabled):
            raise BusinessRuntimeError('PERMISSION_DENIED')
        config = await db.get(BusinessConfiguration, agent_id)
        if config is None:
            config = BusinessConfiguration(agent_id=agent_id, generation=uuid4())
            db.add(config)
        elif not current or current.grant_revision != grant.revision:
            config.generation = uuid4()
        if current is None:
            current = IntegrationConnection(id=connection_id, agent_id=agent_id, owner_id=owner_id,
                provider='chotacrm', workspace_id=workspace_id, workspace_name=workspace['name'], timezone=workspace['timezone'],
                grant_id=grant.grant_id, grant_revision=grant.revision, active=True)
            db.add(current)
        else:
            current.workspace_id = workspace_id
            current.workspace_name = workspace['name']
            current.timezone = workspace['timezone']
            current.grant_id = grant.grant_id
            current.grant_revision = grant.revision
        await db.flush()
        await db.execute(delete(CapabilityBinding).where(CapabilityBinding.agent_id == agent_id))
        for capability in CAPABILITIES:
            db.add(CapabilityBinding(agent_id=agent_id, capability=capability.id, connection_id=connection_id, enabled=capability.id in enabled))
        if pending:
            await db.delete(pending)
        await db.commit()
        return await self.state(db, agent_id, workspaces)

    async def disconnect(self, db, agent_id, owner_id, cookie):
        await self.owner(db, agent_id, owner_id, lock=True)
        current = await self.connection(db, agent_id)
        pending = await db.get(BusinessConnectionIntent, agent_id, populate_existing=True)
        await self.ensure_settled(db, current)
        if not current and not pending:
            await db.commit()
            return
        if pending is None:
            pending = BusinessConnectionIntent(agent_id=agent_id, owner_id=owner_id, connection_id=current.id,
                workspace_id=current.workspace_id, workspace_name=current.workspace_name, timezone=current.timezone,
                capabilities=[], action='DISCONNECT')
            db.add(pending)
        else:
            pending.action = 'DISCONNECT'
        connection_id = pending.connection_id
        # Persist cancellation before dispatch; runtime refuses history/tools while
        # it exists, including if the remote revoke never received this request.
        await db.commit()
        await self.owner(db, agent_id, owner_id, lock=True)
        pending = await db.get(BusinessConnectionIntent, agent_id, populate_existing=True)
        if pending is None:
            await db.commit()
            return
        if pending.connection_id != connection_id or pending.action != 'DISCONNECT':
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        current = await self.connection(db, agent_id)
        if current and current.id != connection_id:
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        # Cookie is freshly verified by CRM, but membership is not required to
        # revoke this owner's delegation. Repeat-safe even if PUT never arrived.
        await self.adapter.revoke(owner_id, agent_id, connection_id, cookie)
        if current:
            current.active = False
            await db.execute(delete(CapabilityBinding).where(CapabilityBinding.agent_id == agent_id))
        await db.delete(pending)
        config = await db.get(BusinessConfiguration, agent_id)
        config.generation = uuid4()
        await db.commit()
