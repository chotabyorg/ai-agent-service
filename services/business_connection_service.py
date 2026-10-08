"""One first-party CRM per agent; durable owner intents, no network under locks."""
from uuid import uuid4

from sqlalchemy import or_, select

from models.agent import AgentTemplate
from models.business import BusinessBinding, BusinessOperation
from models.business_connection import BusinessConnection
from business_capabilities.registry import CAPABILITIES, DEFAULTS, REGISTRY
from business_capabilities.context import BusinessRuntimeError
from business_capabilities.chotacrm_client import ChotaCRMClient


class BusinessConnectionService:
    def __init__(self, settings, adapter=None):
        self.adapter = adapter or ChotaCRMClient(settings)

    async def owner(self, db, agent_id, owner_id, *, lock=False):
        stmt = select(AgentTemplate).where(AgentTemplate.id == agent_id, AgentTemplate.user_id == owner_id)
        if lock:
            stmt = stmt.with_for_update()
        agent = await db.scalar(stmt)
        if agent is None:
            raise BusinessRuntimeError('NOT_FOUND')
        return agent

    async def connection(self, db, agent_id, *, lock=False):
        stmt = select(BusinessConnection).where(BusinessConnection.agent_id == agent_id,
            or_(BusinessConnection.active.is_(True), BusinessConnection.pending_action.is_not(None)))
        if lock:
            stmt = stmt.with_for_update()
        return await db.scalar(stmt.execution_options(populate_existing=True))

    async def state(self, db, agent_id, workspaces):
        row = await self.connection(db, agent_id)
        workspace = None
        if row:
            workspace = next((w for w in workspaces['items'] if str(w['id']) == str(row.workspace_id)), None)
            if workspace is None:
                if not row.pending_action:
                    raise BusinessRuntimeError('PERMISSION_DENIED')
                workspace = dict(id=str(row.workspace_id), name=row.workspace_name, timezone=row.timezone)
        enabled = set(row.enabled_capabilities) if row and row.active and not row.pending_action else set()
        return dict(connected=bool(row and row.active), workspace=workspace,
            pending_connection_action=row.pending_action if row else None,
            runtime_mode='CANONICAL' if row else 'LEGACY',
            capabilities=[dict(id=c.id, family=c.family, risk=c.risk.value,
                               enabled=c.id in enabled, default_enabled=c.default_enabled) for c in CAPABILITIES])

    async def _settled(self, db, row):
        if row and await db.scalar(select(BusinessOperation.id).where(
                BusinessOperation.connection_id == row.id,
                BusinessOperation.status.in_(['DISPATCHING', 'UNKNOWN'])).limit(1)):
            raise BusinessRuntimeError('UNKNOWN_WRITE_RESULT')

    async def _disable_bindings(self, db, row):
        rows = (await db.scalars(select(BusinessBinding).where(
            BusinessBinding.connection_id == row.id).with_for_update())).all()
        for binding in rows:
            binding.active = False

    async def configure(self, db, agent_id, owner_id, cookie, *, workspace_id=None, enabled=None):
        await self.owner(db, agent_id, owner_id)
        await db.commit()
        workspaces = await self.adapter.workspaces(owner_id, cookie)
        await self.owner(db, agent_id, owner_id, lock=True)
        row = await self.connection(db, agent_id, lock=True)
        await self._settled(db, row)
        if row and row.pending_action == 'DISCONNECT':
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        workspace_id = workspace_id or (row.workspace_id if row else None)
        if workspace_id is None:
            raise BusinessRuntimeError('NOT_FOUND')
        if row and workspace_id != row.workspace_id:
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        workspace = next((w for w in workspaces['items'] if str(w['id']) == str(workspace_id)), None)
        if workspace is None:
            raise BusinessRuntimeError('PERMISSION_DENIED')
        enabled = list(enabled if enabled is not None else row.enabled_capabilities if row else DEFAULTS)
        if len(enabled) != len(set(enabled)) or set(enabled) - REGISTRY.keys():
            raise BusinessRuntimeError('VALIDATION_ERROR')
        if row and row.pending_action and set(enabled) != set(row.enabled_capabilities):
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        if row is None:
            row = BusinessConnection(id=uuid4(), agent_id=agent_id, owner_id=owner_id,
                workspace_id=workspace_id, workspace_name=workspace['name'], timezone=workspace['timezone'],
                generation=1, grant_revision=1, active=False, enabled_capabilities=enabled)
            db.add(row)
        if row.pending_action is None:
            row.generation += 1
            row.intent_id = uuid4()
            row.pending_action = 'CONNECT'
            row.enabled_capabilities = enabled
            await self._disable_bindings(db, row)
        connection_id, intent_id = row.id, row.intent_id
        await db.commit()
        # Stable remote grant identity and immutable pending payload. No DB lock
        # or open transaction spans this S2S request.
        grant = await self.adapter.configure(owner_id, agent_id, connection_id, workspace_id, enabled, cookie)
        if set(grant.capabilities) != set(enabled):
            raise BusinessRuntimeError('PERMISSION_DENIED')
        await self.owner(db, agent_id, owner_id, lock=True)
        row = await self.connection(db, agent_id, lock=True)
        if row is None or row.id != connection_id or row.intent_id != intent_id or row.pending_action != 'CONNECT':
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        row.grant_id, row.grant_revision = grant.grant_id, grant.revision
        row.active, row.pending_action, row.intent_id = True, None, None
        await db.commit()
        return await self.state(db, agent_id, workspaces)

    async def disconnect(self, db, agent_id, owner_id, cookie):
        await self.owner(db, agent_id, owner_id, lock=True)
        row = await self.connection(db, agent_id, lock=True)
        if row is None:
            await db.commit()
            return
        await self._settled(db, row)
        if row.pending_action != 'DISCONNECT':
            row.pending_action, row.intent_id = 'DISCONNECT', uuid4()
            row.generation += 1
            await self._disable_bindings(db, row)
        connection_id, intent_id = row.id, row.intent_id
        await db.commit()
        await self.adapter.revoke(owner_id, agent_id, connection_id, cookie)
        await self.owner(db, agent_id, owner_id, lock=True)
        row = await self.connection(db, agent_id, lock=True)
        if row is None:
            await db.commit()
            return
        if row.id != connection_id or row.intent_id != intent_id or row.pending_action != 'DISCONNECT':
            raise BusinessRuntimeError('CONNECTION_CONFLICT')
        row.active, row.pending_action, row.intent_id = False, None, None
        await db.commit()
