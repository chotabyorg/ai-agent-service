"""Add retained managed business-operation safety authority.

Revision ID: d5e6f7g8h9i0
Revises: c4d5e6f7g8h9

DDL is frozen here, independent of future ORM changes. No live-agent/Session FK.
"""
from alembic import op
import sqlalchemy as sa

revision = "d5e6f7g8h9i0"
down_revision = "c4d5e6f7g8h9"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE business_bindings (
	id UUID NOT NULL,
	adapter_id VARCHAR(64) NOT NULL,
	connection_id UUID NOT NULL,
	authorization_generation BIGINT NOT NULL,
	context_generation BIGINT NOT NULL,
	catalog_revision VARCHAR(64) NOT NULL,
	enabled_capabilities JSONB NOT NULL,
	active BOOLEAN DEFAULT false NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	owner_id VARCHAR(255) NOT NULL,
	agent_owner_id UUID NOT NULL,
	agent_id UUID NOT NULL,
	integration_id UUID NOT NULL,
	channel VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_business_bindings PRIMARY KEY (id),
	CONSTRAINT uq_business_bindings_provenance UNIQUE (id, owner_id, agent_owner_id, agent_id, integration_id, channel, adapter_id, connection_id),
	CONSTRAINT ck_business_bindings_channel CHECK (channel IN ('TELEGRAM','INSTAGRAM','WEBSITE_WIDGET','WHATSAPP','VOICE')),
	CONSTRAINT ck_business_bindings_generations CHECK (authorization_generation > 0 AND context_generation > 0),
	CONSTRAINT ck_business_bindings_catalog_hash CHECK (catalog_revision ~ '^[a-f0-9]{64}$'),
	CONSTRAINT ck_business_bindings_capabilities CHECK (jsonb_typeof(enabled_capabilities) = 'array' AND jsonb_array_length(enabled_capabilities) BETWEEN 0 AND 32)
)""",
    """CREATE UNIQUE INDEX uq_business_bindings_active ON business_bindings (owner_id, agent_owner_id, agent_id, integration_id, channel) WHERE active""",
    """CREATE TABLE business_scopes (
	id UUID NOT NULL,
	conversation_id UUID NOT NULL,
	owner_id VARCHAR(255) NOT NULL,
	agent_owner_id UUID NOT NULL,
	agent_id UUID NOT NULL,
	integration_id UUID NOT NULL,
	channel VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_business_scopes PRIMARY KEY (id),
	CONSTRAINT uq_business_scopes_complete UNIQUE (owner_id, agent_owner_id, agent_id, integration_id, channel, conversation_id),
	CONSTRAINT uq_business_scopes_provenance UNIQUE (id, owner_id, agent_owner_id, agent_id, integration_id, channel),
	CONSTRAINT ck_business_scopes_channel CHECK (channel IN ('TELEGRAM','INSTAGRAM','WEBSITE_WIDGET','WHATSAPP','VOICE')),
	CONSTRAINT ck_business_scopes_owner CHECK (length(owner_id) BETWEEN 1 AND 255 AND owner_id <> '*')
)""",
    """CREATE TABLE business_turn_contexts (
	scope_id UUID NOT NULL,
	turn_id UUID NOT NULL,
	request_fingerprint VARCHAR(64) NOT NULL,
	input_revision BIGINT NOT NULL,
	authority_revision BIGINT NOT NULL,
	input_first_sequence BIGINT NOT NULL,
	input_last_sequence BIGINT NOT NULL,
	context_last_sequence BIGINT NOT NULL,
	selected_input_message_ids JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_business_turn_contexts PRIMARY KEY (scope_id, turn_id),
	CONSTRAINT fk_business_turn_contexts_scope_id_business_scopes FOREIGN KEY(scope_id) REFERENCES business_scopes (id) ON DELETE RESTRICT,
	CONSTRAINT ck_business_turn_contexts_request_hash CHECK (request_fingerprint ~ '^[a-f0-9]{64}$'),
	CONSTRAINT ck_business_turn_contexts_bounds CHECK (input_revision > 0 AND authority_revision >= 0 AND 1 <= input_first_sequence AND input_first_sequence <= input_last_sequence AND input_last_sequence <= context_last_sequence),
	CONSTRAINT ck_business_turn_contexts_input_ids CHECK (jsonb_typeof(selected_input_message_ids) = 'array' AND jsonb_array_length(selected_input_message_ids) BETWEEN 1 AND 256)
)""",
    """CREATE TABLE business_plans (
	id UUID NOT NULL,
	scope_id UUID NOT NULL,
	origin_turn_id UUID NOT NULL,
	binding_id UUID NOT NULL,
	adapter_id VARCHAR(64) NOT NULL,
	connection_id UUID NOT NULL,
	provider_profile_revision VARCHAR(64) NOT NULL,
	authorization_generation BIGINT NOT NULL,
	context_generation BIGINT NOT NULL,
	catalog_revision VARCHAR(64) NOT NULL,
	facts_fingerprint VARCHAR(64) NOT NULL,
	fact_revisions JSONB NOT NULL,
	plan_fingerprint VARCHAR(64) NOT NULL,
	action_count INTEGER NOT NULL,
	requires_confirmation BOOLEAN NOT NULL,
	proposal_state VARCHAR(16) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	confirmed_turn_id UUID,
	confirmation_input_ids JSONB,
	confirmation_policy VARCHAR(64),
	confirmed_at TIMESTAMP WITH TIME ZONE,
	owner_id VARCHAR(255) NOT NULL,
	agent_owner_id UUID NOT NULL,
	agent_id UUID NOT NULL,
	integration_id UUID NOT NULL,
	channel VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_business_plans PRIMARY KEY (id),
	CONSTRAINT uq_business_plans_origin UNIQUE (scope_id, origin_turn_id),
	CONSTRAINT uq_business_plans_scope UNIQUE (id, scope_id),
	CONSTRAINT uq_business_plans_operation_provenance UNIQUE (id, scope_id, origin_turn_id, adapter_id, connection_id, provider_profile_revision),
	CONSTRAINT fk_business_plans_scope_id_business_scopes FOREIGN KEY(scope_id, owner_id, agent_owner_id, agent_id, integration_id, channel) REFERENCES business_scopes (id, owner_id, agent_owner_id, agent_id, integration_id, channel) ON DELETE RESTRICT,
	CONSTRAINT fk_business_plans_binding_id_business_bindings FOREIGN KEY(binding_id, owner_id, agent_owner_id, agent_id, integration_id, channel, adapter_id, connection_id) REFERENCES business_bindings (id, owner_id, agent_owner_id, agent_id, integration_id, channel, adapter_id, connection_id) ON DELETE RESTRICT,
	CONSTRAINT fk_business_plans_origin_turn FOREIGN KEY(scope_id, origin_turn_id) REFERENCES business_turn_contexts (scope_id, turn_id) ON DELETE RESTRICT,
	CONSTRAINT fk_business_plans_confirmed_turn FOREIGN KEY(scope_id, confirmed_turn_id) REFERENCES business_turn_contexts (scope_id, turn_id) ON DELETE RESTRICT,
	CONSTRAINT ck_business_plans_channel CHECK (channel IN ('TELEGRAM','INSTAGRAM','WEBSITE_WIDGET','WHATSAPP','VOICE')),
	CONSTRAINT ck_business_plans_bounds CHECK (action_count BETWEEN 1 AND 32 AND authorization_generation > 0 AND context_generation > 0),
	CONSTRAINT ck_business_plans_hashes CHECK (provider_profile_revision ~ '^[a-f0-9]{64}$' AND catalog_revision ~ '^[a-f0-9]{64}$' AND facts_fingerprint ~ '^[a-f0-9]{64}$' AND plan_fingerprint ~ '^[a-f0-9]{64}$'),
	CONSTRAINT ck_business_plans_facts CHECK (jsonb_typeof(fact_revisions) = 'object' AND octet_length(fact_revisions::text) <= 16384),
	CONSTRAINT ck_business_plans_proposal_state CHECK (proposal_state IN ('PREPARED','CONFIRMED','INVALIDATED','CANCELLED')),
	CONSTRAINT ck_business_plans_confirmation_shape CHECK ((proposal_state <> 'CONFIRMED' OR (confirmed_turn_id IS NOT NULL AND confirmation_input_ids IS NOT NULL AND confirmation_policy IS NOT NULL AND confirmed_at IS NOT NULL)) AND ((confirmed_turn_id IS NULL AND confirmation_input_ids IS NULL AND confirmation_policy IS NULL AND confirmed_at IS NULL) OR (confirmed_turn_id IS NOT NULL AND confirmation_input_ids IS NOT NULL AND confirmation_policy IS NOT NULL AND confirmed_at IS NOT NULL)) AND (confirmed_turn_id IS NULL OR confirmed_turn_id <> origin_turn_id))
)""",
    """CREATE TABLE business_operations (
	id UUID NOT NULL,
	plan_id UUID NOT NULL,
	scope_id UUID NOT NULL,
	origin_turn_id UUID NOT NULL,
	ordinal INTEGER NOT NULL,
	capability VARCHAR(64) NOT NULL,
	schema_version BIGINT NOT NULL,
	request_fingerprint VARCHAR(64) NOT NULL,
	request_payload JSONB NOT NULL,
	adapter_id VARCHAR(64) NOT NULL,
	connection_id UUID NOT NULL,
	provider_profile_revision VARCHAR(64) NOT NULL,
	provider_idempotency_key VARCHAR(128),
	native_replay_policy VARCHAR(16) NOT NULL,
	native_replay_until TIMESTAMP WITH TIME ZONE,
	issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(16) NOT NULL,
	state_version BIGINT DEFAULT 0 NOT NULL,
	dispatch_epoch BIGINT DEFAULT 0 NOT NULL,
	dispatch_nonce UUID,
	dispatch_started_at TIMESTAMP WITH TIME ZONE,
	dispatch_lease_expires_at TIMESTAMP WITH TIME ZONE,
	attempt_count BIGINT DEFAULT 0 NOT NULL,
	receipt_id VARCHAR(128),
	object_id VARCHAR(128),
	receipt_fingerprint VARCHAR(64),
	receipt_evidence JSONB,
	terminal_at TIMESTAMP WITH TIME ZONE,
	failure_code VARCHAR(64),
	reconcile_state VARCHAR(16) DEFAULT 'NONE' NOT NULL,
	reconcile_epoch BIGINT DEFAULT 0 NOT NULL,
	reconcile_nonce UUID,
	reconcile_lease_expires_at TIMESTAMP WITH TIME ZONE,
	last_reconciled_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_business_operations PRIMARY KEY (id),
	CONSTRAINT uq_business_operations_identity UNIQUE (scope_id, origin_turn_id, ordinal),
	CONSTRAINT uq_business_operations_plan_ordinal UNIQUE (plan_id, ordinal),
	CONSTRAINT fk_business_operations_plan_id_business_plans FOREIGN KEY(plan_id, scope_id, origin_turn_id, adapter_id, connection_id, provider_profile_revision) REFERENCES business_plans (id, scope_id, origin_turn_id, adapter_id, connection_id, provider_profile_revision) ON DELETE RESTRICT,
	CONSTRAINT ck_business_operations_bounds CHECK (ordinal BETWEEN 0 AND 31 AND schema_version > 0 AND state_version >= 0 AND dispatch_epoch >= 0 AND attempt_count >= 0 AND reconcile_epoch >= 0),
	CONSTRAINT ck_business_operations_hashes CHECK (request_fingerprint ~ '^[a-f0-9]{64}$' AND provider_profile_revision ~ '^[a-f0-9]{64}$' AND (receipt_fingerprint IS NULL OR receipt_fingerprint ~ '^[a-f0-9]{64}$')),
	CONSTRAINT ck_business_operations_payload CHECK (jsonb_typeof(request_payload) = 'object' AND octet_length(request_payload::text) <= 16384 AND (receipt_evidence IS NULL OR (jsonb_typeof(receipt_evidence) = 'object' AND octet_length(receipt_evidence::text) <= 16384))),
	CONSTRAINT ck_business_operations_native_policy CHECK (native_replay_policy IN ('NONE','BOUNDED','DURABLE_TEST') AND ((native_replay_policy = 'NONE') = (provider_idempotency_key IS NULL)) AND ((native_replay_policy = 'BOUNDED') = (native_replay_until IS NOT NULL))),
	CONSTRAINT ck_business_operations_status CHECK (status IN ('RESERVED','DISPATCHING','SUCCEEDED','FAILED','UNKNOWN')),
	CONSTRAINT ck_business_operations_state_shape CHECK ((status <> 'RESERVED' OR (dispatch_epoch = 0 AND dispatch_nonce IS NULL AND attempt_count = 0 AND receipt_id IS NULL AND terminal_at IS NULL)) AND (status NOT IN ('DISPATCHING','UNKNOWN') OR (dispatch_epoch >= 1 AND dispatch_nonce IS NOT NULL AND dispatch_started_at IS NOT NULL AND dispatch_lease_expires_at IS NOT NULL AND attempt_count >= 1 AND terminal_at IS NULL AND receipt_id IS NULL)) AND (status NOT IN ('SUCCEEDED','FAILED') OR (terminal_at IS NOT NULL AND receipt_id IS NOT NULL AND receipt_fingerprint IS NOT NULL AND receipt_evidence IS NOT NULL)) AND (status <> 'SUCCEEDED' OR object_id IS NOT NULL) AND (status <> 'FAILED' OR failure_code IS NOT NULL)),
	CONSTRAINT ck_business_operations_reconcile_shape CHECK (reconcile_state IN ('NONE','NEEDED','IN_FLIGHT','RESOLVED','MANUAL_REQUIRED') AND ((reconcile_state = 'IN_FLIGHT') = (reconcile_nonce IS NOT NULL AND reconcile_lease_expires_at IS NOT NULL)) AND (reconcile_state <> 'IN_FLIGHT' OR status = 'UNKNOWN'))
)""",
    """CREATE INDEX ix_business_operations_dispatch_expiry ON business_operations (dispatch_lease_expires_at, id) WHERE status = 'DISPATCHING'""",
    """CREATE INDEX ix_business_operations_scope_status ON business_operations (scope_id, status)""",
    """CREATE UNIQUE INDEX uq_business_operations_unresolved_scope ON business_operations (scope_id) WHERE status IN ('DISPATCHING','UNKNOWN')""",
    """CREATE TABLE business_turn_plan_links (
	scope_id UUID NOT NULL,
	turn_id UUID NOT NULL,
	plan_id UUID NOT NULL,
	selection_kind VARCHAR(8) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_business_turn_plan_links PRIMARY KEY (scope_id, turn_id),
	CONSTRAINT fk_business_turn_plan_links_scope_id_business_turn_contexts FOREIGN KEY(scope_id, turn_id) REFERENCES business_turn_contexts (scope_id, turn_id) ON DELETE RESTRICT,
	CONSTRAINT fk_business_turn_plan_links_plan_id_business_plans FOREIGN KEY(plan_id, scope_id) REFERENCES business_plans (id, scope_id) ON DELETE RESTRICT,
	CONSTRAINT ck_business_turn_plan_links_selection CHECK (selection_kind IN ('OWN','CONFIRM','RECOVERY'))
)""",
)

def upgrade():
    for statement in DDL:
        op.execute(sa.text(statement))
    op.execute(sa.text(GUARD_FUNCTION))
    for name in TABLES:
        op.execute(sa.text('CREATE TRIGGER retained_business_guard BEFORE UPDATE OR DELETE ON '
                           + name + ' FOR EACH ROW EXECUTE FUNCTION guard_retained_business()'))

def downgrade():
    # An ordinary downgrade is never permission to destroy effect evidence.
    for name in TABLES:
        if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM ' + name + ' LIMIT 1)')):
            raise RuntimeError("Refusing downgrade of populated retained business authority")
    for name in reversed(TABLES):
        op.drop_table(name)
    op.execute(sa.text('DROP FUNCTION guard_retained_business()'))

TABLES = ('business_bindings', 'business_scopes', 'business_turn_contexts', 'business_plans', 'business_operations', 'business_turn_plan_links')

# Mutable authorization/attempt tokens are distinct from immutable effect
# identity. Terminal evidence cannot be overwritten even by a stale SQL writer.
GUARD_FUNCTION = """
CREATE FUNCTION guard_retained_business() RETURNS trigger LANGUAGE plpgsql AS $$
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
