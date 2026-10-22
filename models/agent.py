"""Agent-related ORM models."""

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base

if TYPE_CHECKING:
    from models.session import ConversationSession


class AgentTemplate(Base):
    """Agent configuration stored in PostgreSQL."""

    __tablename__ = "agent_templates"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    llm_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    prompt_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)
    timezone: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Europe/Minsk", server_default=text("'Europe/Minsk'")
    )
    encrypted_openrouter_key: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    handoff_lifecycle_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    handoff_lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="STABLE", server_default=text("'STABLE'")
    )
    handoff_lifecycle_source_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    handoff_lifecycle_scope_snapshot: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)

    credentials: Mapped[list["AgentCredentials"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
    )
    sessions: Mapped[list["ConversationSession"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
    )


class AgentCredentials(Base):
    """Per-agent secrets required for MCP-backed tools."""

    __tablename__ = "agent_credentials"

    agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent_templates.id", ondelete="CASCADE"),
        primary_key=True,
    )
    service_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    # The JSONB value is either a legacy credentials object or a versioned
    # encrypted envelope array. Request payloads remain object-only.
    credentials: Mapped[Any] = mapped_column(JSONB, nullable=False)
    enabled_tools: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=None)

    agent: Mapped["AgentTemplate"] = relationship(back_populates="credentials")
