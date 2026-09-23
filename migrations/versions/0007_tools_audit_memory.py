"""Tools, approvals, tamper-evident audit, persistent memory, upload provenance.

* ``audit_log`` is append-only and hash-chained: each row stores the previous row's hash,
  so removing or editing a record breaks the chain (verified by ``/api/audit/verify``).
* ``pending_actions`` holds sensitive actions awaiting an authorized, different approver.
  The action hash pins the exact arguments that were proposed.
* ``tickets`` / ``vpn_profiles`` are the (fictional) systems the tools act on.
* ``user_memories`` stores only permitted facts, inspectable and deletable by the user.
* ``conversations.summary`` supports the token-budget strategy for long conversations.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("actor_role", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),  # tool_call | approval | upload | ...
        sa.Column("tool", sa.Text()),
        sa.Column("arguments", postgresql.JSONB()),  # validated + redacted
        sa.Column("decision", sa.Text(), nullable=False),  # allow | deny | pending | executed
        sa.Column("reason", sa.Text()),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("pending_action_id", sa.Uuid()),
        sa.Column("action_hash", sa.Text()),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False, unique=True),
        sa.CheckConstraint(
            "decision IN ('allow', 'deny', 'pending', 'executed', 'error')",
            name="audit_decision_enum",
        ),
    )
    op.create_index("ix_audit_actor_created", "audit_log", ["actor_id", "created_at"])
    # Append-only: the runtime role may INSERT and SELECT, never UPDATE or DELETE.
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM opsassist_app")

    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid()),
        sa.Column("requester_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("action_hash", sa.Text(), nullable=False),
        sa.Column("requires_permission", sa.Text(), nullable=False),
        sa.Column("approve_permission", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("approver_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'executed', 'expired', 'failed')",
            name="pending_action_status_enum",
        ),
    )
    op.create_index("ix_pending_actions_requester", "pending_actions", ["requester_id", "status"])

    op.create_table(
        "tickets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low')", name="ticket_severity_enum"
        ),
    )
    op.execute("CREATE SEQUENCE ticket_number_seq START 1042")

    op.create_table(
        "vpn_profiles",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("employee_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approved_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("pending_action_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("CREATE SEQUENCE vpn_profile_seq START 1")

    op.create_table(
        "user_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="explicit"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "key", name="user_memory_unique_key"),
    )

    op.add_column("conversations", sa.Column("summary", sa.Text()))
    op.add_column("conversations", sa.Column("summary_upto_seq", sa.BigInteger()))
    op.add_column("documents", sa.Column("uploaded_by", sa.Text()))


def downgrade() -> None:
    op.drop_column("documents", "uploaded_by")
    op.drop_column("conversations", "summary_upto_seq")
    op.drop_column("conversations", "summary")
    op.drop_table("user_memories")
    op.execute("DROP SEQUENCE IF EXISTS vpn_profile_seq")
    op.drop_table("vpn_profiles")
    op.execute("DROP SEQUENCE IF EXISTS ticket_number_seq")
    op.drop_table("tickets")
    op.drop_table("pending_actions")
    op.drop_table("audit_log")
