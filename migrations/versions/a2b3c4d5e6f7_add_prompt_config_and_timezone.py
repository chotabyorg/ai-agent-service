"""add prompt_config and timezone to agent_templates

Revision ID: a2b3c4d5e6f7
Revises: 1ce833c27973
Create Date: 2026-03-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str]] = '1ce833c27973'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add prompt_config (JSONB) and timezone columns."""
    op.add_column('agent_templates', sa.Column('prompt_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('agent_templates', sa.Column('timezone', sa.String(length=50), nullable=False, server_default='Europe/Minsk'))


def downgrade() -> None:
    """Remove prompt_config and timezone columns."""
    op.drop_column('agent_templates', 'timezone')
    op.drop_column('agent_templates', 'prompt_config')
