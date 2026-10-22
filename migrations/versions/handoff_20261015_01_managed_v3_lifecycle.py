"""Add durable managed v3 receipts and AI Handoff lifecycle generations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "handoff_20261015_01"
down_revision = "g5_20261015_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_templates", sa.Column(
        "handoff_lifecycle_generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    op.add_column("agent_templates", sa.Column(
        "handoff_lifecycle_state", sa.String(length=32), nullable=False,
        server_default=sa.text("'STABLE'")))
    op.add_column("agent_templates", sa.Column(
        "handoff_lifecycle_source_generation", sa.BigInteger(), nullable=True))
    op.add_column("agent_templates", sa.Column(
        "handoff_lifecycle_scope_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.execute(sa.text("UPDATE agent_templates SET handoff_lifecycle_generation=1 WHERE is_active IS TRUE"))
    op.create_check_constraint(
        "ck_agent_handoff_lifecycle_generation", "agent_templates",
        "handoff_lifecycle_generation >= 0")
    op.create_check_constraint(
        "ck_agent_handoff_lifecycle_state", "agent_templates",
        "(handoff_lifecycle_state = 'STABLE' AND handoff_lifecycle_source_generation IS NULL "
        "AND (handoff_lifecycle_scope_snapshot IS NULL OR "
        "jsonb_typeof(handoff_lifecycle_scope_snapshot) = 'array')) OR "
        "(handoff_lifecycle_state IN ('PENDING_DEACTIVATE','PENDING_DELETE') AND "
        "handoff_lifecycle_source_generation IS NOT NULL AND "
        "handoff_lifecycle_generation = handoff_lifecycle_source_generation + 1 AND "
        "jsonb_typeof(handoff_lifecycle_scope_snapshot) = 'array' AND "
        "CASE WHEN jsonb_typeof(handoff_lifecycle_scope_snapshot) = 'array' "
        "THEN jsonb_array_length(handoff_lifecycle_scope_snapshot) > 0 ELSE FALSE END)")

    op.create_table(
        "managed_execution_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("integration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_revision", sa.BigInteger(), nullable=False),
        sa.Column("authority_revision", sa.BigInteger(), nullable=False),
        sa.Column("input_first_sequence", sa.BigInteger(), nullable=False),
        sa.Column("input_last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("context_last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("handoff_slot", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("original_dispatch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("managed_request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("execution_state", sa.String(length=16), nullable=False, server_default=sa.text("'RUNNING'")),
        sa.Column("handoff_control", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_id", "integration_id", "channel", "agent_id", "conversation_id", "turn_id",
                            name="uq_managed_execution_scope_turn"),
        sa.UniqueConstraint("original_dispatch_id", name="uq_managed_execution_dispatch"),
        sa.CheckConstraint("request_fingerprint ~ '^[0-9a-f]{64}$'",
                           name="ck_managed_execution_request_fingerprint"),
        sa.CheckConstraint("managed_request_fingerprint ~ '^[0-9a-f]{64}$'",
                           name="ck_managed_execution_sidecar_fingerprint"),
        sa.CheckConstraint("execution_state IN ('RUNNING','SEALED')",
                           name="ck_managed_execution_state"),
        sa.CheckConstraint("handoff_slot = 0", name="ck_managed_execution_handoff_slot"),
    )
    op.create_index("ix_managed_execution_created_at", "managed_execution_receipts", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM managed_execution_receipts)")):
        raise RuntimeError("Refusing downgrade while managed v3 execution evidence exists")
    if bind.scalar(sa.text(
            "SELECT EXISTS (SELECT 1 FROM agent_templates WHERE handoff_lifecycle_generation > 1 "
            "OR handoff_lifecycle_state <> 'STABLE' "
            "OR handoff_lifecycle_source_generation IS NOT NULL "
            "OR handoff_lifecycle_scope_snapshot IS NOT NULL)")):
        raise RuntimeError("Refusing downgrade while AI Handoff lifecycle evidence exists")
    op.drop_index("ix_managed_execution_created_at", table_name="managed_execution_receipts")
    op.drop_table("managed_execution_receipts")
    op.drop_constraint("ck_agent_handoff_lifecycle_state", "agent_templates", type_="check")
    op.drop_constraint("ck_agent_handoff_lifecycle_generation", "agent_templates", type_="check")
    op.drop_column("agent_templates", "handoff_lifecycle_source_generation")
    op.drop_column("agent_templates", "handoff_lifecycle_scope_snapshot")
    op.drop_column("agent_templates", "handoff_lifecycle_state")
    op.drop_column("agent_templates", "handoff_lifecycle_generation")
