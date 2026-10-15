"""Persist one immutable Conversation effect request per retained operation.

Revision ID: g5_20261015_01
Revises: luna_20261012_01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "g5_20261015_01"
down_revision = "luna_20261012_01"
branch_labels = None
depends_on = None


LEGACY_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION guard_retained_business() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_identity jsonb; new_identity jsonb;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Retained business evidence cannot be deleted' USING ERRCODE = '23514';
  END IF;
  IF TG_TABLE_NAME = 'business_operations' THEN
    old_identity := to_jsonb(OLD) - ARRAY['status','state_version','dispatch_epoch','dispatch_nonce',
      'dispatch_started_at','dispatch_lease_expires_at','attempt_count','receipt_id','object_id',
      'receipt_fingerprint','receipt_evidence','terminal_at','failure_code','reconcile_state',
      'reconcile_epoch','reconcile_nonce','reconcile_lease_expires_at','last_reconciled_at','updated_at'];
    new_identity := to_jsonb(NEW) - ARRAY['status','state_version','dispatch_epoch','dispatch_nonce',
      'dispatch_started_at','dispatch_lease_expires_at','attempt_count','receipt_id','object_id',
      'receipt_fingerprint','receipt_evidence','terminal_at','failure_code','reconcile_state',
      'reconcile_epoch','reconcile_nonce','reconcile_lease_expires_at','last_reconciled_at','updated_at'];
    IF OLD.status IN ('SUCCEEDED','FAILED') AND
       (to_jsonb(OLD) - ARRAY['updated_at','last_reconciled_at']) IS DISTINCT FROM
       (to_jsonb(NEW) - ARRAY['updated_at','last_reconciled_at']) THEN
      RAISE EXCEPTION 'Terminal business evidence is immutable' USING ERRCODE = '23514';
    END IF;
  ELSIF TG_TABLE_NAME = 'business_plans' THEN
    old_identity := to_jsonb(OLD) - ARRAY['proposal_state','confirmed_turn_id','confirmation_input_ids',
                                        'confirmation_policy','confirmed_at'];
    new_identity := to_jsonb(NEW) - ARRAY['proposal_state','confirmed_turn_id','confirmation_input_ids',
                                        'confirmation_policy','confirmed_at'];
    IF OLD.confirmed_turn_id IS NOT NULL AND
       (OLD.confirmed_turn_id, OLD.confirmation_input_ids, OLD.confirmation_policy, OLD.confirmed_at)
       IS DISTINCT FROM (NEW.confirmed_turn_id, NEW.confirmation_input_ids, NEW.confirmation_policy, NEW.confirmed_at) THEN
      RAISE EXCEPTION 'Business consent evidence is immutable' USING ERRCODE = '23514';
    END IF;
  ELSIF TG_TABLE_NAME = 'business_bindings' THEN
    old_identity := to_jsonb(OLD) - ARRAY['authorization_generation','context_generation','catalog_revision',
                                        'enabled_capabilities','active','updated_at'];
    new_identity := to_jsonb(NEW) - ARRAY['authorization_generation','context_generation','catalog_revision',
                                        'enabled_capabilities','active','updated_at'];
    IF NEW.authorization_generation < OLD.authorization_generation OR NEW.context_generation < OLD.context_generation THEN
      RAISE EXCEPTION 'Business generations cannot decrease' USING ERRCODE = '23514';
    END IF;
  ELSE
    old_identity := to_jsonb(OLD); new_identity := to_jsonb(NEW);
  END IF;
  IF old_identity IS DISTINCT FROM new_identity THEN
    RAISE EXCEPTION 'Business action identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
"""


GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION guard_retained_business() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_identity jsonb; new_identity jsonb;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Retained business evidence cannot be deleted' USING ERRCODE = '23514';
  END IF;
  IF TG_TABLE_NAME = 'business_operations' THEN
    IF OLD.effect_request_turn_id IS NOT NULL AND
       (OLD.effect_request_turn_id, OLD.effect_request_fingerprint,
        OLD.effect_managed_request_fingerprint, OLD.effect_operation_fingerprint,
        OLD.effect_requested_at) IS DISTINCT FROM
       (NEW.effect_request_turn_id, NEW.effect_request_fingerprint,
        NEW.effect_managed_request_fingerprint, NEW.effect_operation_fingerprint,
        NEW.effect_requested_at) THEN
      RAISE EXCEPTION 'Business effect request identity is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.effect_request_turn_id IS NULL AND NEW.effect_request_turn_id IS NOT NULL AND
       (OLD.status <> 'RESERVED' OR OLD.dispatch_epoch <> 0 OR OLD.attempt_count <> 0
        OR OLD.state_version <> NEW.state_version OR NEW.status <> 'RESERVED'
        OR NEW.dispatch_epoch <> 0 OR NEW.attempt_count <> 0
        OR OLD.dispatch_nonce IS NOT NULL OR NEW.dispatch_nonce IS NOT NULL
        OR OLD.receipt_id IS NOT NULL OR NEW.receipt_id IS NOT NULL
        OR OLD.terminal_at IS NOT NULL OR NEW.terminal_at IS NOT NULL
        OR NEW.effect_permit_id IS NOT NULL) THEN
      RAISE EXCEPTION 'Business effect request can only be assigned once before dispatch' USING ERRCODE = '23514';
    END IF;
    IF OLD.effect_request_turn_id IS NULL AND NEW.effect_request_turn_id IS NOT NULL AND
       (to_jsonb(OLD) - ARRAY['effect_request_turn_id','effect_request_fingerprint',
         'effect_managed_request_fingerprint','effect_operation_fingerprint','effect_requested_at','updated_at'])
       IS DISTINCT FROM
       (to_jsonb(NEW) - ARRAY['effect_request_turn_id','effect_request_fingerprint',
         'effect_managed_request_fingerprint','effect_operation_fingerprint','effect_requested_at','updated_at']) THEN
      RAISE EXCEPTION 'Business effect request assignment cannot change operation state' USING ERRCODE = '23514';
    END IF;
    IF OLD.effect_permit_id IS NOT NULL AND OLD.effect_permit_id IS DISTINCT FROM NEW.effect_permit_id THEN
      RAISE EXCEPTION 'Business effect permit is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.effect_permit_id IS NULL AND NEW.effect_permit_id IS NOT NULL AND
       (OLD.effect_request_turn_id IS NULL OR OLD.status <> 'RESERVED'
        OR NEW.status <> 'RESERVED' OR OLD.state_version <> NEW.state_version
        OR OLD.dispatch_epoch <> 0 OR NEW.dispatch_epoch <> 0
        OR OLD.attempt_count <> 0 OR NEW.attempt_count <> 0
        OR OLD.dispatch_nonce IS NOT NULL OR NEW.dispatch_nonce IS NOT NULL
        OR OLD.receipt_id IS NOT NULL OR NEW.receipt_id IS NOT NULL
        OR OLD.terminal_at IS NOT NULL OR NEW.terminal_at IS NOT NULL
        OR OLD.effect_request_turn_id IS DISTINCT FROM NEW.effect_request_turn_id
        OR OLD.effect_request_fingerprint IS DISTINCT FROM NEW.effect_request_fingerprint
        OR OLD.effect_managed_request_fingerprint IS DISTINCT FROM NEW.effect_managed_request_fingerprint
        OR OLD.effect_operation_fingerprint IS DISTINCT FROM NEW.effect_operation_fingerprint
        OR OLD.effect_requested_at IS DISTINCT FROM NEW.effect_requested_at) THEN
      RAISE EXCEPTION 'Business effect permit requires its retained request' USING ERRCODE = '23514';
    END IF;
    IF OLD.effect_permit_id IS NULL AND NEW.effect_permit_id IS NOT NULL AND
       (to_jsonb(OLD) - ARRAY['effect_permit_id','updated_at']) IS DISTINCT FROM
       (to_jsonb(NEW) - ARRAY['effect_permit_id','updated_at']) THEN
      RAISE EXCEPTION 'Business effect permit assignment cannot change operation state' USING ERRCODE = '23514';
    END IF;
    old_identity := to_jsonb(OLD) - ARRAY['status','state_version','dispatch_epoch','dispatch_nonce',
      'dispatch_started_at','dispatch_lease_expires_at','attempt_count','receipt_id','object_id',
      'receipt_fingerprint','receipt_evidence','terminal_at','failure_code','reconcile_state',
      'reconcile_epoch','reconcile_nonce','reconcile_lease_expires_at','last_reconciled_at','updated_at',
      'effect_request_turn_id','effect_request_fingerprint','effect_managed_request_fingerprint',
      'effect_operation_fingerprint','effect_requested_at','effect_permit_id'];
    new_identity := to_jsonb(NEW) - ARRAY['status','state_version','dispatch_epoch','dispatch_nonce',
      'dispatch_started_at','dispatch_lease_expires_at','attempt_count','receipt_id','object_id',
      'receipt_fingerprint','receipt_evidence','terminal_at','failure_code','reconcile_state',
      'reconcile_epoch','reconcile_nonce','reconcile_lease_expires_at','last_reconciled_at','updated_at',
      'effect_request_turn_id','effect_request_fingerprint','effect_managed_request_fingerprint',
      'effect_operation_fingerprint','effect_requested_at','effect_permit_id'];
    IF OLD.status IN ('SUCCEEDED','FAILED') AND
       (to_jsonb(OLD) - ARRAY['updated_at','last_reconciled_at']) IS DISTINCT FROM
       (to_jsonb(NEW) - ARRAY['updated_at','last_reconciled_at']) THEN
      RAISE EXCEPTION 'Terminal business evidence is immutable' USING ERRCODE = '23514';
    END IF;
  ELSIF TG_TABLE_NAME = 'business_plans' THEN
    old_identity := to_jsonb(OLD) - ARRAY['proposal_state','confirmed_turn_id','confirmation_input_ids',
                                        'confirmation_policy','confirmed_at'];
    new_identity := to_jsonb(NEW) - ARRAY['proposal_state','confirmed_turn_id','confirmation_input_ids',
                                        'confirmation_policy','confirmed_at'];
    IF OLD.confirmed_turn_id IS NOT NULL AND
       (OLD.confirmed_turn_id, OLD.confirmation_input_ids, OLD.confirmation_policy, OLD.confirmed_at)
       IS DISTINCT FROM (NEW.confirmed_turn_id, NEW.confirmation_input_ids, NEW.confirmation_policy, NEW.confirmed_at) THEN
      RAISE EXCEPTION 'Business consent evidence is immutable' USING ERRCODE = '23514';
    END IF;
  ELSIF TG_TABLE_NAME = 'business_bindings' THEN
    old_identity := to_jsonb(OLD) - ARRAY['authorization_generation','context_generation','catalog_revision',
                                        'enabled_capabilities','active','updated_at'];
    new_identity := to_jsonb(NEW) - ARRAY['authorization_generation','context_generation','catalog_revision',
                                        'enabled_capabilities','active','updated_at'];
    IF NEW.authorization_generation < OLD.authorization_generation OR NEW.context_generation < OLD.context_generation THEN
      RAISE EXCEPTION 'Business generations cannot decrease' USING ERRCODE = '23514';
    END IF;
  ELSE
    old_identity := to_jsonb(OLD); new_identity := to_jsonb(NEW);
  END IF;
  IF old_identity IS DISTINCT FROM new_identity THEN
    RAISE EXCEPTION 'Business action identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.add_column("business_operations", sa.Column("effect_request_turn_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("business_operations", sa.Column("effect_request_fingerprint", sa.String(64), nullable=True))
    op.add_column("business_operations", sa.Column("effect_managed_request_fingerprint", sa.String(64), nullable=True))
    op.add_column("business_operations", sa.Column("effect_operation_fingerprint", sa.String(64), nullable=True))
    op.add_column("business_operations", sa.Column("effect_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("business_operations", sa.Column("effect_permit_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_business_operations_effect_request_turn", "business_operations", "business_turn_contexts",
        ["scope_id", "effect_request_turn_id"], ["scope_id", "turn_id"], ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_business_operations_effect_permit", "business_operations", ["effect_permit_id"])
    op.create_check_constraint(
        "ck_business_operations_effect_request_shape", "business_operations",
        "(effect_request_turn_id IS NULL AND effect_request_fingerprint IS NULL AND "
        "effect_managed_request_fingerprint IS NULL AND effect_operation_fingerprint IS NULL AND "
        "effect_requested_at IS NULL) OR (effect_request_turn_id IS NOT NULL AND "
        "effect_request_fingerprint IS NOT NULL AND effect_request_fingerprint ~ '^[a-f0-9]{64}$' AND "
        "effect_managed_request_fingerprint IS NOT NULL AND effect_managed_request_fingerprint ~ '^[a-f0-9]{64}$' AND "
        "effect_operation_fingerprint IS NOT NULL AND effect_operation_fingerprint ~ '^[a-f0-9]{64}$' AND effect_requested_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_business_operations_effect_permit_shape", "business_operations",
        "effect_permit_id IS NULL OR effect_request_turn_id IS NOT NULL",
    )
    op.drop_index("uq_business_operations_unresolved_scope", table_name="business_operations")
    op.create_index(
        "uq_business_operations_unresolved_scope", "business_operations", ["scope_id"], unique=True,
        postgresql_where=sa.text(
            "status IN ('DISPATCHING','UNKNOWN') OR "
            "(status = 'RESERVED' AND effect_request_turn_id IS NOT NULL)"
        ),
    )
    op.execute(sa.text(GUARD_FUNCTION))


def downgrade() -> None:
    bind = op.get_bind()
    # Fence acquisition writers before observing retained evidence. Without
    # this lock, an UPDATE could assign a request after the EXISTS check but
    # before the following DDL acquires its own table lock, erasing the only
    # durable record of a committed Conversation permit on downgrade.
    bind.execute(sa.text("LOCK TABLE business_operations IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM business_operations "
        "WHERE effect_request_turn_id IS NOT NULL OR effect_permit_id IS NOT NULL)"
    )):
        raise RuntimeError("Refusing downgrade while business effect acquisition evidence exists")
    op.execute(sa.text(LEGACY_GUARD_FUNCTION))
    op.drop_index("uq_business_operations_unresolved_scope", table_name="business_operations")
    op.create_index(
        "uq_business_operations_unresolved_scope", "business_operations", ["scope_id"], unique=True,
        postgresql_where=sa.text("status IN ('DISPATCHING','UNKNOWN')"),
    )
    op.drop_constraint("uq_business_operations_effect_permit", "business_operations", type_="unique")
    op.drop_constraint("ck_business_operations_effect_permit_shape", "business_operations", type_="check")
    op.drop_constraint("ck_business_operations_effect_request_shape", "business_operations", type_="check")
    op.drop_constraint("fk_business_operations_effect_request_turn", "business_operations", type_="foreignkey")
    op.drop_column("business_operations", "effect_permit_id")
    op.drop_column("business_operations", "effect_requested_at")
    op.drop_column("business_operations", "effect_operation_fingerprint")
    op.drop_column("business_operations", "effect_managed_request_fingerprint")
    op.drop_column("business_operations", "effect_request_fingerprint")
    op.drop_column("business_operations", "effect_request_turn_id")
