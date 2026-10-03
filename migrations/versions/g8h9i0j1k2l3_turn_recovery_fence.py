"""Persist per-turn suppression after uncertain-write recovery."""
from alembic import op
import sqlalchemy as sa

revision = 'g8h9i0j1k2l3'
down_revision = 'f7g8h9i0j1k2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('business_turns', sa.Column('write_suppressed', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    # Earlier turns did not record recovery. Redelivering such a turn must not
    # create a fresh effect; existing receipt replay and new turns still work.
    op.execute('UPDATE business_turns SET write_suppressed = true')


def downgrade():
    op.drop_column('business_turns', 'write_suppressed')
