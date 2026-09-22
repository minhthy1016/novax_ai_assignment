"""Conversations, messages and per-attempt LLM usage.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="complete"),
        sa.Column("model_id", sa.Text()),
        sa.Column("request_id", sa.Text()),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="message_role_enum"),
        sa.CheckConstraint(
            "status IN ('complete', 'partial', 'error')", name="message_status_enum"
        ),
    )
    op.create_index("ix_messages_conversation_seq", "messages", ["conversation_id", "seq"])

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("conversation_id", sa.Uuid()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("error_type", sa.Text()),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("ttft_ms", sa.Float()),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cost_usd", sa.Numeric(12, 8), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("kind IN ('chat', 'embedding')", name="usage_kind_enum"),
        sa.CheckConstraint(
            "outcome IN ('success', 'error', 'timeout', 'cancelled', 'skipped')",
            name="usage_outcome_enum",
        ),
    )
    op.create_index("ix_llm_usage_request_id", "llm_usage", ["request_id"])
    op.create_index("ix_llm_usage_user_created", "llm_usage", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("llm_usage")
    op.drop_table("messages")
    op.drop_table("conversations")
