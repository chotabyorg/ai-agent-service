"""Owner configuration only. Retained effect evidence stays in models.business."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, BigInteger, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base


class BusinessConnection(Base):
    __tablename__ = 'business_connections'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default='chotacrm')
    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    workspace_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    grant_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    grant_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    enabled_capabilities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # A persisted intent blocks dispatch, and its stable connection identity
    # makes owner retries safe after a remote response or local commit is lost.
    pending_action: Mapped[str | None] = mapped_column(String(16))
    intent_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))
    __table_args__ = (
        Index('uq_business_connections_current', 'agent_id', unique=True,
              postgresql_where=text('active OR pending_action IS NOT NULL')),
        CheckConstraint("provider = 'chotacrm'", name='provider'),
        CheckConstraint('generation > 0 AND grant_revision > 0', name='generations'),
        CheckConstraint("jsonb_typeof(enabled_capabilities) = 'array' AND jsonb_array_length(enabled_capabilities) <= 18", name='capabilities'),
        CheckConstraint("pending_action IS NULL OR pending_action IN ('CONNECT','DISCONNECT')", name='intent_action'),
        CheckConstraint('(pending_action IS NULL) = (intent_id IS NULL)', name='intent_shape'),
        CheckConstraint('NOT active OR grant_id IS NOT NULL', name='active_grant'),
    )
