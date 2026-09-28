"""The most sensitive classification a conversation's answers have drawn on.

Egress was decided per turn, from that turn's sources only. The history sent with the next
turn can quote an earlier confidential answer, so the rule has to hold for the whole
conversation: once a conversation has used confidential context, no later turn may leave
the box (D-15).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("context_classification", sa.Text()))


def downgrade() -> None:
    op.drop_column("conversations", "context_classification")
