"""Owner-authorized first-party connection management; no credentials accepted."""
from uuid import UUID, uuid4
from sqlalchemy import delete, select
from models.agent import AgentTemplate
from models.business import BusinessConfiguration, IntegrationConnection, CapabilityBinding, BusinessOperation
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
        return await db.scalar(select(IntegrationConnection).where(IntegrationConnection.agent_id == agent_id, IntegrationConnection.active.is_(True)))

    async def state(self, db, agent_id, workspaces):
        connection = await self.connection(db, agent_id)
        workspace = None
        enabled = set()
        if connection:
            workspace = next((w for w in workspaces['items'] if str(w['id']) == str(connection.workspace_id)), None)
            if workspace is None:
                raise BusinessRuntimeError('PERMISSION_DENIED')
            enabled = set((await db.scalars(select(CapabilityBinding.capability).where(CapabilityBinding.agent_id == agent_id,
                CapabilityBinding.connection_id == connection.id, CapabilityBinding.enabled.is_(True)))).all())
        return {'connected': bool(connection), 'runtime_mode': 'CANONICAL' if await db.get(BusinessConfiguration, agent_id) else 'LEGACY', 'workspace': workspace,
                'capabilities': [{'id': c.id, 'family': c.family, 'risk': c.risk.value,
                                  'enabled': c.id in enabled, 'default_enabled': c.default_enabled} for c in CAPABILITIES]}

    async def ensure_settled(self, db, connection):
        if connection and await db.scalar(select(BusinessOperation.id).where(BusinessOperation.connection_id == connection.id,
                BusinessOperation.state.in_(['DISPATCHING', 'UNKNOWN'])).limit(1)):
            raise BusinessRuntimeError('UNKNOWN_WRITE_RESULT')

    async def configure(self, db, agent_id, owner_id, cookie, *, workspace_id=None, enabled=None):
        await self.owner(db, agent_id, owner_id, lock=True)
        current = await self.connection(db, agent_id)
        await self.ensure_settled(db, current)
        if workspace_id is None:
            if not current:
                raise BusinessRuntimeError('NOT_FOUND')
            workspace_id = current.workspace_id
        if current and current.workspace_id != workspace_id:
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        workspaces = await self.adapter.workspaces(owner_id, cookie)
        workspace = next((w for w in workspaces['items'] if str(w['id']) == str(workspace_id)), None)
        if not workspace:
            raise BusinessRuntimeError('PERMISSION_DENIED')
        if enabled is None:
            if current and current.workspace_id == workspace_id:
                enabled = list((await db.scalars(select(CapabilityBinding.capability).where(CapabilityBinding.agent_id == agent_id, CapabilityBinding.enabled.is_(True)))).all())
            else:
                enabled = list(DEFAULTS)
        if len(enabled) != len(set(enabled)) or set(enabled) - REGISTRY.keys():
            raise BusinessRuntimeError('VALIDATION_ERROR')
        connection_id = current.id if current else uuid4()
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
        await db.commit()
        return await self.state(db, agent_id, workspaces)

    async def disconnect(self, db, agent_id, owner_id, cookie):
        await self.owner(db, agent_id, owner_id, lock=True)
        current = await self.connection(db, agent_id)
        await self.ensure_settled(db, current)
        if current:
            await self.adapter.revoke(owner_id, agent_id, current.id, cookie)
            current.active = False
            await db.execute(delete(CapabilityBinding).where(CapabilityBinding.agent_id == agent_id))
            config = await db.get(BusinessConfiguration, agent_id)
            config.generation = uuid4()
        await db.commit()
