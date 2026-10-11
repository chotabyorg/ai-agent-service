"""Move every existing Agent to the new default model.

Revision ID: luna_20261012_01
Revises: bv2_20261008_01

Only model_name changes. Previous per-agent values cannot be reconstructed;
image rollback must not attempt a database downgrade of this revision.
"""
from alembic import op
import sqlalchemy as sa

revision = "luna_20261012_01"
down_revision = "bv2_20261008_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Freeze the historical target here; never import the mutable live catalog.
    op.execute(sa.text("UPDATE agent_templates SET model_name = :model_name")
               .bindparams(model_name="openai/gpt-6-luna"))


def downgrade() -> None:
    raise RuntimeError(
        "Cannot restore previous per-agent model names after the Luna data migration; "
        "use a compatible application image without downgrading this revision."
    )
