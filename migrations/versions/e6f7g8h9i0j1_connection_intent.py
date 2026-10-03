"""Persist first-party connection identity before remote grant mutation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = 'e6f7g8h9i0j1'
down_revision = 'd5e6f7g8h9i0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('business_connection_intents',
        sa.Column('agent_id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('owner_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('connection_id', pg.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column('workspace_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('workspace_name', sa.String(200), nullable=False),
        sa.Column('timezone', sa.String(100), nullable=False),
        sa.Column('capabilities', pg.JSONB, nullable=False),
        sa.Column('action', sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(['agent_id', 'owner_id'], ['agent_templates.id', 'agent_templates.user_id'],
            ondelete='CASCADE', name='fk_connection_intent_owner'))


def downgrade():
    op.drop_table('business_connection_intents')
