"""ORM models.

Tables arrive with the features that own them, each in its own migration, so the schema
history mirrors the build order: 0001 identity + inventory, 0002 conversations + usage.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Department(Base):
    """Department slugs are the unit of data isolation across documents, tools and audit."""

    __tablename__ = "departments"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)

    __table_args__ = (CheckConstraint("slug ~ '^[a-z][a-z_]*$'", name="department_slug_format"),)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    department: Mapped[str] = mapped_column(ForeignKey("departments.slug"))
    role: Mapped[str] = mapped_column(Text)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("id ~ '^U[0-9]{3,}$'", name="user_id_format"),)


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment: Mapped[str] = mapped_column(Text)
    owner_department: Mapped[str] = mapped_column(ForeignKey("departments.slug"))
    status: Mapped[str] = mapped_column(Text)
    cpu_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    memory_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    last_check: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('healthy', 'degraded', 'offline')", name="server_status_enum"),
        CheckConstraint(
            "environment IN ('production', 'staging', 'development')", name="server_env_enum"
        ),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Ordering key. created_at is not enough: now() is the transaction start time, so two
    # messages written in one transaction would tie.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    # complete | partial (stream cancelled or failed mid-way) | error (no answer produced)
    status: Mapped[str] = mapped_column(Text, default="complete")
    model_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="message_role_enum"),
        CheckConstraint("status IN ('complete', 'partial', 'error')", name="message_status_enum"),
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    )


class LLMUsage(Base):
    """One row per provider attempt, including failed, skipped and cancelled ones:
    retries and fallbacks cost money and latency, so they must be visible."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_id: Mapped[str] = mapped_column(Text, index=True)
    user_id: Mapped[str | None] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int]
    outcome: Mapped[str] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[float]
    ttft_ms: Mapped[float | None]
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    tokens_estimated: Mapped[bool] = mapped_column(default=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 8), default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('chat', 'embedding')", name="usage_kind_enum"),
        CheckConstraint(
            "outcome IN ('success', 'error', 'timeout', 'cancelled', 'skipped')",
            name="usage_outcome_enum",
        ),
        Index("ix_llm_usage_user_created", "user_id", "created_at"),
    )
