"""Retained AI-owned effect evidence; no FK to mutable agents or Sessions."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint,
                        Index, Integer, String, UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base

TENANT = ("owner_id", "agent_owner_id", "agent_id", "integration_id", "channel")
CHANNEL_CHECK = "channel IN ('TELEGRAM','INSTAGRAM','WEBSITE_WIDGET','WHATSAPP','VOICE')"


class TenantColumns:
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_owner_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    integration_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)


class CreatedColumns:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                server_default=text("now()"))


class BusinessScope(TenantColumns, CreatedColumns, Base):
    __tablename__ = "business_scopes"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(*TENANT, "conversation_id", name="uq_business_scopes_complete"),
        UniqueConstraint("id", *TENANT, name="uq_business_scopes_provenance"),
        CheckConstraint(CHANNEL_CHECK, name="channel"),
        CheckConstraint("length(owner_id) BETWEEN 1 AND 255 AND owner_id <> '*'", name="owner"),
    )


class BusinessBinding(TenantColumns, CreatedColumns, Base):
    __tablename__ = "business_bindings"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    adapter_id: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    authorization_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    context_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    catalog_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                server_default=text("now()"))
    __table_args__ = (
        UniqueConstraint("id", *TENANT, "adapter_id", "connection_id", name="uq_business_bindings_provenance"),
        Index("uq_business_bindings_active", *TENANT, unique=True, postgresql_where=text("active")),
        CheckConstraint(CHANNEL_CHECK, name="channel"),
        CheckConstraint("authorization_generation > 0 AND context_generation > 0", name="generations"),
        CheckConstraint("catalog_revision ~ '^[a-f0-9]{64}$'", name="catalog_hash"),
        CheckConstraint("jsonb_typeof(enabled_capabilities) = 'array' AND jsonb_array_length(enabled_capabilities) BETWEEN 0 AND 32", name="capabilities"),
    )


class BusinessTurnContext(CreatedColumns, Base):
    __tablename__ = "business_turn_contexts"
    scope_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    turn_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authority_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_first_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    context_last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    selected_input_message_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["business_scopes.id"], ondelete="RESTRICT"),
        CheckConstraint("request_fingerprint ~ '^[a-f0-9]{64}$'", name="request_hash"),
        CheckConstraint("input_revision > 0 AND authority_revision >= 0 AND 1 <= input_first_sequence AND input_first_sequence <= input_last_sequence AND input_last_sequence <= context_last_sequence", name="bounds"),
        CheckConstraint("jsonb_typeof(selected_input_message_ids) = 'array' AND jsonb_array_length(selected_input_message_ids) BETWEEN 1 AND 256", name="input_ids"),
    )


class BusinessPlan(TenantColumns, CreatedColumns, Base):
    __tablename__ = "business_plans"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    scope_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    origin_turn_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    binding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    adapter_id: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    provider_profile_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    context_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    catalog_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    facts_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_revisions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action_count: Mapped[int] = mapped_column(Integer, nullable=False)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    proposal_state: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_turn_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    confirmation_input_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    confirmation_policy: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("scope_id", "origin_turn_id", name="uq_business_plans_origin"),
        UniqueConstraint("id", "scope_id", name="uq_business_plans_scope"),
        UniqueConstraint("id", "scope_id", "origin_turn_id", "adapter_id", "connection_id", "provider_profile_revision", name="uq_business_plans_operation_provenance"),
        ForeignKeyConstraint(["scope_id", *TENANT], ["business_scopes.id", *["business_scopes." + c for c in TENANT]], ondelete="RESTRICT"),
        ForeignKeyConstraint(["binding_id", *TENANT, "adapter_id", "connection_id"], ["business_bindings.id", *["business_bindings." + c for c in TENANT], "business_bindings.adapter_id", "business_bindings.connection_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["scope_id", "origin_turn_id"], ["business_turn_contexts.scope_id", "business_turn_contexts.turn_id"], name="fk_business_plans_origin_turn", ondelete="RESTRICT"),
        ForeignKeyConstraint(["scope_id", "confirmed_turn_id"], ["business_turn_contexts.scope_id", "business_turn_contexts.turn_id"], name="fk_business_plans_confirmed_turn", ondelete="RESTRICT"),
        CheckConstraint(CHANNEL_CHECK, name="channel"),
        CheckConstraint("action_count BETWEEN 1 AND 32 AND authorization_generation > 0 AND context_generation > 0", name="bounds"),
        CheckConstraint("provider_profile_revision ~ '^[a-f0-9]{64}$' AND catalog_revision ~ '^[a-f0-9]{64}$' AND facts_fingerprint ~ '^[a-f0-9]{64}$' AND plan_fingerprint ~ '^[a-f0-9]{64}$'", name="hashes"),
        CheckConstraint("jsonb_typeof(fact_revisions) = 'object' AND octet_length(fact_revisions::text) <= 16384", name="facts"),
        CheckConstraint("proposal_state IN ('PREPARED','CONFIRMED','INVALIDATED','CANCELLED')", name="proposal_state"),
        CheckConstraint("(proposal_state <> 'CONFIRMED' OR (confirmed_turn_id IS NOT NULL AND confirmation_input_ids IS NOT NULL AND confirmation_policy IS NOT NULL AND confirmed_at IS NOT NULL)) AND ((confirmed_turn_id IS NULL AND confirmation_input_ids IS NULL AND confirmation_policy IS NULL AND confirmed_at IS NULL) OR (confirmed_turn_id IS NOT NULL AND confirmation_input_ids IS NOT NULL AND confirmation_policy IS NOT NULL AND confirmed_at IS NOT NULL)) AND (confirmed_turn_id IS NULL OR confirmed_turn_id <> origin_turn_id)", name="confirmation_shape"),
    )


class BusinessTurnPlanLink(CreatedColumns, Base):
    __tablename__ = "business_turn_plan_links"
    scope_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    turn_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    selection_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["scope_id", "turn_id"], ["business_turn_contexts.scope_id", "business_turn_contexts.turn_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["plan_id", "scope_id"], ["business_plans.id", "business_plans.scope_id"], ondelete="RESTRICT"),
        CheckConstraint("selection_kind IN ('OWN','CONFIRM','RECOVERY')", name="selection"),
    )


class BusinessOperation(CreatedColumns, Base):
    __tablename__ = "business_operations"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    plan_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    scope_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    origin_turn_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    adapter_id: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    provider_profile_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    native_replay_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    native_replay_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Issued time belongs to the immutable original operation, not to an attempt.
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    dispatch_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    dispatch_nonce: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    receipt_id: Mapped[str | None] = mapped_column(String(128))
    object_id: Mapped[str | None] = mapped_column(String(128))
    receipt_fingerprint: Mapped[str | None] = mapped_column(String(64))
    receipt_evidence: Mapped[dict | None] = mapped_column(JSONB)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    reconcile_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'NONE'"))
    reconcile_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    reconcile_nonce: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reconcile_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Immutable Conversation authority acquisition, separate from provider state.
    effect_request_turn_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    effect_request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    effect_managed_request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    effect_operation_fingerprint: Mapped[str | None] = mapped_column(String(64))
    effect_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effect_permit_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (
        UniqueConstraint("scope_id", "origin_turn_id", "ordinal", name="uq_business_operations_identity"),
        UniqueConstraint("plan_id", "ordinal", name="uq_business_operations_plan_ordinal"),
        ForeignKeyConstraint(["plan_id", "scope_id", "origin_turn_id", "adapter_id", "connection_id", "provider_profile_revision"], ["business_plans.id", "business_plans.scope_id", "business_plans.origin_turn_id", "business_plans.adapter_id", "business_plans.connection_id", "business_plans.provider_profile_revision"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["scope_id", "effect_request_turn_id"], ["business_turn_contexts.scope_id", "business_turn_contexts.turn_id"], name="fk_business_operations_effect_request_turn", ondelete="RESTRICT"),
        UniqueConstraint("effect_permit_id", name="uq_business_operations_effect_permit"),
        Index("uq_business_operations_unresolved_scope", "scope_id", unique=True, postgresql_where=text("status IN ('DISPATCHING','UNKNOWN') OR (status = 'RESERVED' AND effect_request_turn_id IS NOT NULL)")),
        Index("ix_business_operations_scope_status", "scope_id", "status"),
        Index("ix_business_operations_dispatch_expiry", "dispatch_lease_expires_at", "id", postgresql_where=text("status = 'DISPATCHING'")),
        CheckConstraint("ordinal BETWEEN 0 AND 31 AND schema_version > 0 AND state_version >= 0 AND dispatch_epoch >= 0 AND attempt_count >= 0 AND reconcile_epoch >= 0", name="bounds"),
        CheckConstraint("request_fingerprint ~ '^[a-f0-9]{64}$' AND provider_profile_revision ~ '^[a-f0-9]{64}$' AND (receipt_fingerprint IS NULL OR receipt_fingerprint ~ '^[a-f0-9]{64}$')", name="hashes"),
        CheckConstraint("(effect_request_turn_id IS NULL AND effect_request_fingerprint IS NULL AND effect_managed_request_fingerprint IS NULL AND effect_operation_fingerprint IS NULL AND effect_requested_at IS NULL) OR (effect_request_turn_id IS NOT NULL AND effect_request_fingerprint IS NOT NULL AND effect_request_fingerprint ~ '^[a-f0-9]{64}$' AND effect_managed_request_fingerprint IS NOT NULL AND effect_managed_request_fingerprint ~ '^[a-f0-9]{64}$' AND effect_operation_fingerprint IS NOT NULL AND effect_operation_fingerprint ~ '^[a-f0-9]{64}$' AND effect_requested_at IS NOT NULL)", name="effect_request_shape"),
        CheckConstraint("effect_permit_id IS NULL OR effect_request_turn_id IS NOT NULL", name="effect_permit_shape"),
        CheckConstraint("jsonb_typeof(request_payload) = 'object' AND octet_length(request_payload::text) <= 16384 AND (receipt_evidence IS NULL OR (jsonb_typeof(receipt_evidence) = 'object' AND octet_length(receipt_evidence::text) <= 16384))", name="payload"),
        CheckConstraint("native_replay_policy IN ('NONE','BOUNDED','DURABLE_TEST') AND ((native_replay_policy = 'NONE') = (provider_idempotency_key IS NULL)) AND ((native_replay_policy = 'BOUNDED') = (native_replay_until IS NOT NULL))", name="native_policy"),
        CheckConstraint("status IN ('RESERVED','DISPATCHING','SUCCEEDED','FAILED','UNKNOWN')", name="status"),
        CheckConstraint("(status <> 'RESERVED' OR (dispatch_epoch = 0 AND dispatch_nonce IS NULL AND attempt_count = 0 AND receipt_id IS NULL AND terminal_at IS NULL)) AND (status NOT IN ('DISPATCHING','UNKNOWN') OR (dispatch_epoch >= 1 AND dispatch_nonce IS NOT NULL AND dispatch_started_at IS NOT NULL AND dispatch_lease_expires_at IS NOT NULL AND attempt_count >= 1 AND terminal_at IS NULL AND receipt_id IS NULL)) AND (status NOT IN ('SUCCEEDED','FAILED') OR (terminal_at IS NOT NULL AND receipt_id IS NOT NULL AND receipt_fingerprint IS NOT NULL AND receipt_evidence IS NOT NULL)) AND (status <> 'SUCCEEDED' OR object_id IS NOT NULL) AND (status <> 'FAILED' OR failure_code IS NOT NULL)", name="state_shape"),
        CheckConstraint("reconcile_state IN ('NONE','NEEDED','IN_FLIGHT','RESOLVED','MANUAL_REQUIRED') AND ((reconcile_state = 'IN_FLIGHT') = (reconcile_nonce IS NOT NULL AND reconcile_lease_expires_at IS NOT NULL)) AND (reconcile_state <> 'IN_FLIGHT' OR status = 'UNKNOWN')", name="reconcile_shape"),
    )
