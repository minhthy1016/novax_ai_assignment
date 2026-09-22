"""Foundation: pgvector extension, departments, users, servers.

Revision ID: 0001
Revises:
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "departments",
        sa.Column("slug", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.CheckConstraint("slug ~ '^[a-z][a-z_]*$'", name="department_slug_format"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("department", sa.Text(), sa.ForeignKey("departments.slug"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "permissions",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id ~ '^U[0-9]{3,}$'", name="user_id_format"),
    )
    op.create_index("ix_users_department", "users", ["department"])
    op.create_table(
        "servers",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("owner_department", sa.Text(), sa.ForeignKey("departments.slug"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("cpu_pct", sa.Numeric(5, 2)),
        sa.Column("memory_pct", sa.Numeric(5, 2)),
        sa.Column("last_check", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('healthy', 'degraded', 'offline')", name="server_status_enum"
        ),
        sa.CheckConstraint(
            "environment IN ('production', 'staging', 'development')", name="server_env_enum"
        ),
    )


def downgrade() -> None:
    op.drop_table("servers")
    op.drop_index("ix_users_department", table_name="users")
    op.drop_table("users")
    op.drop_table("departments")
    # The vector extension is left in place; other databases objects may depend on it.
