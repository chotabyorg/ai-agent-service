"""add enabled_tools to agent_credentials

Revision ID: b3c4d5e6f7g8
Revises: a2b3c4d5e6f7
Create Date: 2026-03-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7g8'
down_revision: Union[str, Sequence[str]] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add enabled_tools (JSONB) column to agent_credentials."""
    op.add_column('agent_credentials', sa.Column('enabled_tools', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Remove enabled_tools column."""
    op.drop_column('agent_credentials', 'enabled_tools')
