"""add openrouter key and deactivate agents

Revision ID: c4d5e6f7g8h9
Revises: b3c4d5e6f7g8
Create Date: 2026-04-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7g8h9"
down_revision: Union[str, None] = "b3c4d5e6f7g8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_templates",
        sa.Column("encrypted_openrouter_key", sa.Text(), nullable=True),
    )
    # Deactivate all existing agents (they have no key yet)
    op.execute("UPDATE agent_templates SET is_active = false")
    # Change default for new agents
    op.alter_column("agent_templates", "is_active", server_default=text("false"))


def downgrade() -> None:
    op.drop_column("agent_templates", "encrypted_openrouter_key")
    op.execute("UPDATE agent_templates SET is_active = true")
    op.alter_column("agent_templates", "is_active", server_default=text("true"))
