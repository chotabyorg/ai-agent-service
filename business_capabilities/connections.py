"""Bridge agent owner configuration to the existing complete-scope bindings."""
from sqlalchemy import or_, select
from models.agent import AgentTemplate
from models.business import BusinessBinding
from models.business_connection import BusinessConnection
from schemas.business import BusinessSafetyError
from services.business_repository import tenant_values


async def validate_connection_authority(session, binding):
    """Called with the connection lock already held, before admission commit."""
    row = await session.get(BusinessConnection, binding.connection_id, populate_existing=True)
    if (row is None or row.agent_id != binding.agent_id or row.owner_id != binding.agent_owner_id
            or not row.active or row.pending_action or row.generation != binding.authorization_generation
            or row.generation != binding.context_generation
            or set(row.enabled_capabilities) != set(binding.enabled_capabilities)):
        raise BusinessSafetyError('BINDING_UNAVAILABLE')


async def lock_agent_connection(session, agent_id, *, shared=False):
    return await session.scalar(select(BusinessConnection).where(
        BusinessConnection.agent_id == agent_id,
        or_(BusinessConnection.active.is_(True), BusinessConnection.pending_action.is_not(None)))
        .with_for_update(read=shared, nowait=session.info.get('business_nowait', False))
        .execution_options(populate_existing=True))


async def sync_business_binding(request, grant, session_factory, registry):
    registration = registry.get('chotacrm')
    if registration is None:
        return
    values = tenant_values(request, grant)
    async with session_factory() as db:
        async with db.begin():
            agent = await db.scalar(select(AgentTemplate).where(AgentTemplate.id == grant.agent_id,
                AgentTemplate.user_id == grant.agent_owner_id).with_for_update())
            if agent is None or not agent.is_active:
                raise BusinessSafetyError('AUTHORITY_DENIED')
            row = await lock_agent_connection(db, grant.agent_id)
            if row is None:
                return
            if row.owner_id != grant.agent_owner_id or not row.active or row.pending_action:
                raise BusinessSafetyError('BINDING_UNAVAILABLE')
            current = await db.scalar(select(BusinessBinding).filter_by(**values, active=True).with_for_update())
            if current:
                if current.connection_id != row.id or current.adapter_id != 'chotacrm':
                    raise BusinessSafetyError('BINDING_UNAVAILABLE')
                current.authorization_generation = current.context_generation = row.generation
                current.enabled_capabilities = list(row.enabled_capabilities)
                current.catalog_revision = registration.catalog_revision
            else:
                db.add(BusinessBinding(**values, adapter_id='chotacrm', connection_id=row.id,
                    authorization_generation=row.generation, context_generation=row.generation,
                    enabled_capabilities=list(row.enabled_capabilities),
                    catalog_revision=registration.catalog_revision, active=True))
