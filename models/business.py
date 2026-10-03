"""Additive capability configuration and durable conversation operation records."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from db.database import Base


class BusinessConfiguration(Base):
    __tablename__ = 'business_configurations'
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('agent_templates.id', ondelete='CASCADE'), primary_key=True)
    generation: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, default=uuid4)
    # Presence opts in permanently: disconnect does not resurrect dormant raw CRM tools.


class IntegrationConnection(Base):
    __tablename__ = 'integration_connections'
    __table_args__ = (Index('uq_agent_active_crm_connection', 'agent_id', unique=True, postgresql_where=text('active')),
        UniqueConstraint('id', 'agent_id', name='uq_connection_id_agent'),
        ForeignKeyConstraint(['agent_id', 'owner_id'], ['agent_templates.id', 'agent_templates.user_id'], ondelete='CASCADE', name='fk_connection_agent_owner'))
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('agent_templates.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default='chotacrm')
    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    workspace_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    grant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    grant_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CapabilityBinding(Base):
    __tablename__ = 'capability_bindings'
    __table_args__ = (ForeignKeyConstraint(['connection_id', 'agent_id'], ['integration_connections.id', 'integration_connections.agent_id'], ondelete='CASCADE', name='fk_binding_connection_agent'),)
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('agent_templates.id', ondelete='CASCADE'), primary_key=True)
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('integration_connections.id', ondelete='CASCADE'), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


class BusinessConversation(Base):
    __tablename__ = 'business_conversations'
    __table_args__ = (Index('uq_business_conversation_current', 'agent_id', 'subject_hash', 'generation', unique=True, postgresql_where=text('NOT retired')),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('conversation_sessions.id', ondelete='CASCADE'), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('agent_templates.id', ondelete='CASCADE'), nullable=False)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    audience: Mapped[str] = mapped_column(String(40), nullable=False)
    retired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text('false'))
    lineage_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, default=uuid4, index=True)


class PendingBusinessAction(Base):
    __tablename__ = 'pending_business_actions'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('business_conversations.id', ondelete='CASCADE'), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('integration_connections.id', ondelete='CASCADE'), nullable=False)
    generation: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    proposed_turn_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    confirmed_turn_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Immutable provider-prepared actions, each with resolved business facts and hash.
    actions: Mapped[list] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str] = mapped_column(String(8000), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default='PROPOSED')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BusinessOperation(Base):
    __tablename__ = 'business_operations'
    __table_args__ = (UniqueConstraint('session_id', 'capability', 'intent_id', name='uq_business_operation_intent'),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('business_conversations.id', ondelete='CASCADE'), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('integration_connections.id', ondelete='CASCADE'), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirmation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default='DISPATCHING')
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BusinessTurn(Base):
    __tablename__ = 'business_turns'
    __table_args__ = (UniqueConstraint('session_id', 'event_id', name='uq_business_turn_event'),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('business_conversations.id', ondelete='CASCADE'), nullable=False)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    message_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    answer: Mapped[str | None] = mapped_column(String, nullable=True)


class BusinessConnectionIntent(Base):
    """Recoverable first connection identity, persisted before provider mutation."""
    __tablename__ = 'business_connection_intents'
    __table_args__ = (ForeignKeyConstraint(['agent_id', 'owner_id'],
        ['agent_templates.id', 'agent_templates.user_id'], ondelete='CASCADE', name='fk_connection_intent_owner'),)
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, unique=True)
    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    workspace_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    capabilities: Mapped[list] = mapped_column(JSONB, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False, default='CONNECT')
