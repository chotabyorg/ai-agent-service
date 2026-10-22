"""Durable idempotency receipt for the v3 managed execution boundary."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base


class ManagedExecutionReceipt(Base):
    __tablename__ = "managed_execution_receipts"
    __table_args__ = (
        UniqueConstraint("owner_id", "integration_id", "channel", "agent_id",
                         "conversation_id", "turn_id", name="uq_managed_execution_scope_turn"),
        UniqueConstraint("original_dispatch_id", name="uq_managed_execution_dispatch"),
        CheckConstraint("request_fingerprint ~ '^[0-9a-f]{64}$'",
                        name="ck_managed_execution_request_fingerprint"),
        CheckConstraint("managed_request_fingerprint ~ '^[0-9a-f]{64}$'",
                        name="ck_managed_execution_sidecar_fingerprint"),
        CheckConstraint("execution_state IN ('RUNNING','SEALED')",
                        name="ck_managed_execution_state"),
        CheckConstraint("handoff_slot = 0", name="ck_managed_execution_handoff_slot"),
        Index("ix_managed_execution_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    integration_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    input_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authority_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_first_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    context_last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    handoff_slot: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    original_dispatch_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    managed_request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    execution_state: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    handoff_control: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  server_default=func.now(), onupdate=func.now())
