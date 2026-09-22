"""ORM models.

Only identity and inventory live here on day one. Knowledge, conversation, usage, tool
approval and audit tables arrive with the features that own them, each in its own
migration, so the schema history mirrors the build order.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ARRAY, CheckConstraint, DateTime, ForeignKey, Numeric, Text, func
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
