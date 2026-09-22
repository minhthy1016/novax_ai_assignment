"""Parent-child chunking: store the section context a matched chunk belongs to.

Small chunks are matched (precise embeddings); the model receives their parent section
(``context``). ``headings`` keeps the full heading path for display and debugging.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("chunks", "confidential_chunks"):
        op.add_column(table, sa.Column("context", sa.Text()))
        op.add_column(
            table,
            sa.Column(
                "headings",
                postgresql.ARRAY(sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::text[]"),
            ),
        )
    op.add_column("documents", sa.Column("chunker", sa.Text()))


def downgrade() -> None:
    op.drop_column("documents", "chunker")
    for table in ("chunks", "confidential_chunks"):
        op.drop_column(table, "headings")
        op.drop_column(table, "context")
