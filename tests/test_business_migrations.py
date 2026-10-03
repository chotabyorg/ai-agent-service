"""Exercise the security epoch upgrade against pre-barrier stored state."""
from uuid import uuid4
import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
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
            assert await connection.scalar(text('SELECT system_prompt FROM agent_templates WHERE id=:agent'), ids) == 'untouched'
    try:
        command.upgrade(config, 'e6f7g8h9i0j1')
        asyncio.run(seed())
        command.upgrade(config, 'head')
        asyncio.run(verify())
    finally:
        asyncio.run(engine.dispose())
        _restore_env(previous)
        get_settings.cache_clear()
        async def cleanup():
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE {database} WITH (FORCE)'))
            await admin.dispose()
        asyncio.run(cleanup())
