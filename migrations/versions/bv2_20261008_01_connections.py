"""Add owner CRM configuration above the accepted Goal3 ledger.

Revision ID: bv2_20261008_01
Revises: d5e6f7g8h9i0 (current-main managed_business_safety, NOT donor schema)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'bv2_20261008_01'
down_revision = 'd5e6f7g8h9i0'
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table('business_connections',
        sa.Column('id', uuid, primary_key=True),
        sa.Column('agent_id', uuid, nullable=False),
        sa.Column('owner_id', uuid, nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('workspace_id', uuid, nullable=False),
        sa.Column('workspace_name', sa.String(200), nullable=False),
        sa.Column('timezone', sa.String(100), nullable=False),
        sa.Column('grant_id', uuid),
        sa.Column('grant_revision', sa.BigInteger(), nullable=False),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('enabled_capabilities', postgresql.JSONB(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('pending_action', sa.String(16)),
        sa.Column('intent_id', uuid),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("provider = 'chotacrm'", name='provider'),
        sa.CheckConstraint('generation > 0 AND grant_revision > 0', name='generations'),
        sa.CheckConstraint("jsonb_typeof(enabled_capabilities) = 'array' AND jsonb_array_length(enabled_capabilities) <= 18", name='capabilities'),
        sa.CheckConstraint("pending_action IS NULL OR pending_action IN ('CONNECT','DISCONNECT')", name='intent_action'),
        sa.CheckConstraint('(pending_action IS NULL) = (intent_id IS NULL)', name='intent_shape'),
        sa.CheckConstraint('NOT active OR grant_id IS NOT NULL', name='active_grant'),
    )
    op.create_index('uq_business_connections_current', 'business_connections', ['agent_id'], unique=True,
                    postgresql_where=sa.text('active OR pending_action IS NOT NULL'))


def downgrade():
    # Owner configuration cannot be removed while retained ledger references use it.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM business_bindings WHERE adapter_id='chotacrm') THEN RAISE EXCEPTION 'Retained business bindings require connection configuration'; END IF; END $$")
    op.drop_table('business_connections')
