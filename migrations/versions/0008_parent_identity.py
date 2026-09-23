"""Explicit parent identity for parent-child chunks.

Sibling chunks were collapsed at retrieval by matching their locator string. The locator is
display text, so an identifier is the right key: `parent_index` is the section's ordinal
within the document version, making (document_id, parent_index) the parent's identity.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("chunks", "confidential_chunks"):
        op.add_column(table, sa.Column("parent_index", sa.Integer()))
        op.create_index(f"ix_{table}_parent", table, ["document_id", "parent_index"])


def downgrade() -> None:
    for table in ("chunks", "confidential_chunks"):
        op.drop_index(f"ix_{table}_parent", table_name=table)
        op.drop_column(table, "parent_index")
