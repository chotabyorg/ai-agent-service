"""Conversation session ORM model."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base


class ConversationSession(Base):
    """A dialogue session bound to an external client and channel."""

    __tablename__ = "conversation_sessions"
    __table_args__ = (
        Index(
            "ix_conversation_sessions_agent_external_channel",
            "agent_id",
            "external_client_id",
            "channel",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_client_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    agent: Mapped["AgentTemplate"] = relationship(back_populates="sessions")
