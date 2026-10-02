"""Add isolated capability configuration, proposals and durable operations.

No existing agents, credentials, prompts or sessions are modified.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = 'd5e6f7g8h9i0'
down_revision = 'c4d5e6f7g8h9'
branch_labels = None
depends_on = None


def uuid(name, *args, **kw):
    return sa.Column(name, pg.UUID(as_uuid=True), *args, **kw)


def fk(name, table, **kw):
    return uuid(name, sa.ForeignKey(table + '.id', ondelete='CASCADE'), **kw)


def upgrade():
    op.create_unique_constraint('uq_agent_id_owner', 'agent_templates', ['id', 'user_id'])
    op.create_table('business_configurations', fk('agent_id', 'agent_templates', primary_key=True), uuid('generation', nullable=False))
    op.create_table('integration_connections', uuid('id', primary_key=True), fk('agent_id', 'agent_templates', nullable=False),
        uuid('owner_id', nullable=False), sa.Column('provider', sa.String(32), nullable=False), uuid('workspace_id', nullable=False),
        sa.Column('workspace_name', sa.String(200), nullable=False), sa.Column('timezone', sa.String(100), nullable=False),
        uuid('grant_id', nullable=False), sa.Column('grant_revision', sa.Integer, nullable=False), sa.Column('active', sa.Boolean, nullable=False),
        sa.UniqueConstraint('id', 'agent_id', name='uq_connection_id_agent'),
        sa.ForeignKeyConstraint(['agent_id', 'owner_id'], ['agent_templates.id', 'agent_templates.user_id'], ondelete='CASCADE', name='fk_connection_agent_owner'))
    op.create_index('ix_integration_connections_agent_id', 'integration_connections', ['agent_id'])
    op.create_index('uq_agent_active_crm_connection', 'integration_connections', ['agent_id'], unique=True, postgresql_where=sa.text('active'))
    op.create_table('capability_bindings', fk('agent_id', 'agent_templates', primary_key=True), sa.Column('capability', sa.String(64), primary_key=True),
        fk('connection_id', 'integration_connections', nullable=False), sa.Column('enabled', sa.Boolean, nullable=False),
        sa.ForeignKeyConstraint(['connection_id', 'agent_id'], ['integration_connections.id', 'integration_connections.agent_id'], ondelete='CASCADE', name='fk_binding_connection_agent'))
    op.create_table('business_conversations', fk('id', 'conversation_sessions', primary_key=True), fk('agent_id', 'agent_templates', nullable=False),
        sa.Column('subject_hash', sa.String(64), nullable=False), uuid('generation', nullable=False), sa.Column('audience', sa.String(40), nullable=False),
        sa.UniqueConstraint('agent_id', 'subject_hash', 'generation', name='uq_business_conversation_subject'))
    op.create_table('business_turns', uuid('id', primary_key=True), fk('session_id', 'business_conversations', nullable=False),
        sa.Column('event_id', sa.String(200), nullable=False), sa.Column('message_hash', sa.String(64), nullable=False),
        sa.Column('answer', sa.String), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.UniqueConstraint('session_id', 'event_id', name='uq_business_turn_event'))
    op.create_table('pending_business_actions', uuid('id', primary_key=True), fk('session_id', 'business_conversations', nullable=False),
        fk('connection_id', 'integration_connections', nullable=False), uuid('generation', nullable=False), uuid('proposed_turn_id', nullable=False),
        uuid('confirmed_turn_id', nullable=True), sa.Column('actions', pg.JSONB, nullable=False), sa.Column('summary', sa.String(8000), nullable=False),
        sa.Column('state', sa.String(24), nullable=False), sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_pending_business_actions_session_id', 'pending_business_actions', ['session_id'])
    op.create_table('business_operations', uuid('id', primary_key=True), fk('session_id', 'business_conversations', nullable=False),
        fk('connection_id', 'integration_connections', nullable=False), sa.Column('capability', sa.String(64), nullable=False),
        sa.Column('intent_id', sa.String(64), nullable=False), sa.Column('arguments_hash', sa.String(64), nullable=False), sa.Column('arguments', pg.JSONB, nullable=False), sa.Column('confirmation', pg.JSONB),
        sa.Column('state', sa.String(24), nullable=False), sa.Column('result', pg.JSONB), sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('session_id', 'capability', 'intent_id', name='uq_business_operation_intent'))
    op.create_index('ix_business_operations_session_id', 'business_operations', ['session_id'])
    op.create_index('ix_business_operations_connection_id', 'business_operations', ['connection_id'])


def downgrade():
    for name in ['business_operations', 'pending_business_actions', 'business_turns', 'business_conversations', 'capability_bindings', 'integration_connections', 'business_configurations']:
        op.drop_table(name)

    op.drop_constraint('uq_agent_id_owner', 'agent_templates', type_='unique')
