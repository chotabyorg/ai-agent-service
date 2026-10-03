"""Retire stale customer history without dropping durable operation lineage."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = 'f7g8h9i0j1k2'
down_revision = 'e6f7g8h9i0j1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('business_conversations', sa.Column('retired', sa.Boolean, nullable=False, server_default=sa.text('false')))
    op.add_column('business_conversations', sa.Column('lineage_id', pg.UUID(as_uuid=True), nullable=True))
    # Security epoch transition: old provider pruning may already have removed
    # the evidence needed to invalidate a retained answer. Retire every pre-
    # barrier canonical conversation, but preserve its ledger and lineage.
    op.execute('UPDATE business_conversations SET lineage_id = id, retired = true')
    op.execute("UPDATE pending_business_actions SET state = 'CANCELLED' WHERE state = 'PROPOSED'")
    op.alter_column('business_conversations', 'lineage_id', nullable=False)
    op.create_index('ix_business_conversations_lineage_id', 'business_conversations', ['lineage_id'])
    op.drop_constraint('uq_business_conversation_subject', 'business_conversations', type_='unique')
    op.create_index('uq_business_conversation_current', 'business_conversations', ['agent_id', 'subject_hash', 'generation'],
        unique=True, postgresql_where=sa.text('NOT retired'))


def downgrade():
    # Preserve archived records while restoring the old uniqueness shape; their
    # inert security generations remain unique and cannot become active history.
    op.execute('UPDATE business_conversations SET generation = id WHERE retired')
    op.drop_index('uq_business_conversation_current', table_name='business_conversations')
    op.create_unique_constraint('uq_business_conversation_subject', 'business_conversations', ['agent_id', 'subject_hash', 'generation'])
    op.drop_index('ix_business_conversations_lineage_id', table_name='business_conversations')
    op.drop_column('business_conversations', 'lineage_id')
    op.drop_column('business_conversations', 'retired')
