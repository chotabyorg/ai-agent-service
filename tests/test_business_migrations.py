"""Exercise the security epoch upgrade against pre-barrier stored state."""
from uuid import uuid4
import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import NullPool

from core.config import get_settings
from tests.conftest import PROJECT_ROOT, _set_postgres_env, _restore_env


def test_security_upgrade_retires_old_context_and_preserves_unknown_ledger(postgres_url):
    # A separate disposable database avoids changing the test suite's schema.
    database = 'epoch_' + uuid4().hex
    admin = create_async_engine(postgres_url.replace('psycopg2','asyncpg'), isolation_level='AUTOCOMMIT', poolclass=NullPool)
    async def create_database():
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE {database}'))
    asyncio.run(create_database())
    url = admin.url.set(database=database).render_as_string(hide_password=False)
    previous = _set_postgres_env(url)
    get_settings.cache_clear()
    engine = create_async_engine(url, poolclass=NullPool)
    config = Config(str(PROJECT_ROOT / 'alembic.ini'))
    ids = {key: uuid4() for key in ['agent', 'owner', 'session', 'generation', 'connection', 'workspace', 'grant', 'proposal', 'operation', 'turn']}
    async def seed():
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO agent_templates(id,user_id,name,system_prompt,llm_provider,model_name,temperature,is_active,timezone) VALUES(:agent,:owner,'preserved','untouched','test','test',0.7,true,'UTC')"), ids)
            await connection.execute(text("INSERT INTO business_configurations(agent_id,generation) VALUES(:agent,:generation)"), ids)
            await connection.execute(text("INSERT INTO integration_connections(id,agent_id,owner_id,provider,workspace_id,workspace_name,timezone,grant_id,grant_revision,active) VALUES(:connection,:agent,:owner,'chotacrm',:workspace,'Test','UTC',:grant,1,true)"), ids)
            await connection.execute(text("INSERT INTO conversation_sessions(id,agent_id,external_client_id,channel) VALUES(:session,:agent,'synthetic','website')"), ids)
            await connection.execute(text("INSERT INTO business_conversations(id,agent_id,subject_hash,generation,audience) VALUES(:session,:agent,'synthetic',:generation,'SELF_ASSERTED_CUSTOMER')"), ids)
            await connection.execute(text("INSERT INTO business_turns(id,session_id,event_id,message_hash,answer) VALUES(:turn,:session,'old-event','hash','PRIVATE_ANSWER')"), ids)
            await connection.execute(text("INSERT INTO pending_business_actions(id,session_id,connection_id,generation,proposed_turn_id,actions,summary,state,expires_at) VALUES(:proposal,:session,:connection,:generation,:turn,'[]','PRIVATE_PROPOSAL','PROPOSED',now()+interval '1 hour')"), ids)
            await connection.execute(text("INSERT INTO business_operations(id,session_id,connection_id,capability,intent_id,arguments_hash,arguments,state,issued_at) VALUES(:operation,:session,:connection,'booking.create','intent','hash','{}','UNKNOWN',now())"), ids)
    async def verify():
        async with engine.connect() as connection:
            conversation = (await connection.execute(text('SELECT retired,lineage_id,generation FROM business_conversations WHERE id=:session'), ids)).one()
            assert tuple(conversation) == (True, ids['session'], ids['generation'])
            assert await connection.scalar(text('SELECT state FROM pending_business_actions WHERE id=:proposal'), ids) == 'CANCELLED'
            assert await connection.scalar(text('SELECT state FROM business_operations WHERE id=:operation'), ids) == 'UNKNOWN'
            assert await connection.scalar(text('SELECT event_id FROM business_turns WHERE id=:turn'), ids) == 'old-event'
            assert await connection.scalar(text('SELECT write_suppressed FROM business_turns WHERE id=:turn'), ids) is True
            assert await connection.scalar(text('SELECT system_prompt FROM agent_templates WHERE id=:agent'), ids) == 'untouched'
    async def seed_pre_fence_turn():
        from business_capabilities.runtime import digest
        ids['current_session'], ids['before_g_turn'] = uuid4(), uuid4()
        ids['subject_hash'] = digest([str(ids['agent']), 'test-ingress', 'website', 'before-g-client', 'SELF_ASSERTED_CUSTOMER'])
        ids['message_hash'] = digest('Synthetic request')
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO conversation_sessions(id,agent_id,external_client_id,channel) VALUES(:current_session,:agent,'before-g-client','website')"), ids)
            await connection.execute(text("INSERT INTO business_conversations(id,agent_id,subject_hash,generation,audience,retired,lineage_id) VALUES(:current_session,:agent,:subject_hash,:generation,'SELF_ASSERTED_CUSTOMER',false,:current_session)"), ids)
            await connection.execute(text("INSERT INTO business_turns(id,session_id,event_id,message_hash) VALUES(:before_g_turn,:current_session,'before-g-event',:message_hash)"), ids)
            await connection.execute(text("INSERT INTO capability_bindings(agent_id,capability,connection_id,enabled) VALUES(:agent,'customer.add_note',:connection,true)"), ids)
    async def verify_migrated_turn_behavior():
        from core.config import Settings
        from models.agent import AgentTemplate
        from business_capabilities.runtime import CapabilityRuntime
        from tests.test_business_runtime import FakeAdapter
        provider = FakeAdapter()
        settings = Settings(_env_file=None, business_capabilities_enabled=True)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            agent = await db.get(AgentTemplate, ids['agent'])
            old = await CapabilityRuntime.open(db, settings, agent, 'before-g-client', 'website', 'test-ingress', ids['owner'],
                event_id='before-g-event', user_message='Synthetic request', adapter=provider)
            assert old.turn_id == ids['before_g_turn']
            args = {'customer_ref': 'synthetic', 'body': 'Synthetic note'}
            assert (await old.call('customer_add_note', args))['error']['code'] == 'PREVIOUS_ACTION_COMPLETED'
            assert provider.executions == []
            fresh = await CapabilityRuntime.open(db, settings, agent, 'before-g-client', 'website', 'test-ingress', ids['owner'],
                event_id='after-g-event', user_message='New deliberate request', adapter=provider)
            assert (await fresh.call('customer_add_note', args))['status'] == 'OK'
            assert len(provider.executions) == 1
    try:
        command.upgrade(config, 'e6f7g8h9i0j1')
        asyncio.run(seed())
        command.upgrade(config, 'head')
        asyncio.run(verify())
        # The recovery-fence migration is independently reversible with live
        # canonical rows; neither the older ledger nor context is removed.
        command.downgrade(config, 'f7g8h9i0j1k2')
        asyncio.run(seed_pre_fence_turn())
        command.upgrade(config, 'head')
        asyncio.run(verify())
        asyncio.run(verify_migrated_turn_behavior())
    finally:
        asyncio.run(engine.dispose())
        _restore_env(previous)
        get_settings.cache_clear()
        async def cleanup():
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE {database} WITH (FORCE)'))
            await admin.dispose()
        asyncio.run(cleanup())
