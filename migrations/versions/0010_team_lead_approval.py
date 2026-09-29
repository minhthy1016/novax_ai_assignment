"""Team leads, and pending actions that name who may approve them.

A user may have team leads. A support ticket raised by such a user is not opened at once:
it becomes a pending action that only one of the requester's team leads may approve. That is
a relationship, not a permission, so the pending action records the eligible approvers
(`approver_ids`) at the time it is proposed. Actions approved by permission, such as a VPN
profile, leave it empty.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("team_leads", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
    )
    op.add_column("pending_actions", sa.Column("approver_ids", postgresql.ARRAY(sa.Text())))


def downgrade() -> None:
    op.drop_column("pending_actions", "approver_ids")
    op.drop_column("users", "team_leads")
