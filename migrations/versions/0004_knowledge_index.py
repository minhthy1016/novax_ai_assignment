"""Knowledge index: documents, shared and confidential chunk tables, ingestion jobs,
and Postgres row-level security as a second isolation layer.

RLS model (defense in depth - the application also filters every query):
* Reads are allowed only for rows whose department is listed in the per-transaction
  settings ``app.read_departments`` / ``app.confidential_departments`` (public rows are
  readable by everyone). The settings are set with ``set_config(..., true)`` from the
  caller's AccessScope. If they are missing, nothing is readable: fail closed.
* Writes require ``app.ingest = 'on'``, which only the ingestion code path sets.
* FORCE ROW LEVEL SECURITY applies the policies to the table owner too, so the
  application's own database user cannot bypass them by accident.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIMS = 768
READ_DEPTS = "string_to_array(coalesce(current_setting('app.read_departments', true), ''), ',')"
CONF_DEPTS = (
    "string_to_array(coalesce(current_setting('app.confidential_departments', true), ''), ',')"
)
INGEST = "coalesce(current_setting('app.ingest', true), '') = 'on'"


def _chunk_table(name: str, classification_check: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("doc_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("department", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("section", sa.Text()),
        sa.Column("page", sa.Integer()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embed_text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(DIMS), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "fts",
            sa.dialects.postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', embed_text)", persisted=True),
        ),
        sa.CheckConstraint(classification_check, name=f"{name}_classification"),
        sa.UniqueConstraint("document_id", "chunk_index", name=f"{name}_doc_chunk_unique"),
    )
    op.execute(
        f"CREATE INDEX ix_{name}_embedding ON {name} "
        "USING hnsw (embedding vector_cosine_ops) WHERE is_active"
    )
    op.execute(f"CREATE INDEX ix_{name}_fts ON {name} USING gin (fts) WHERE is_active")
    op.execute(f"CREATE INDEX ix_{name}_acl ON {name} (department, classification) WHERE is_active")
    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {name}_ingest ON {name} FOR ALL USING ({INGEST}) WITH CHECK ({INGEST})"
    )


def upgrade() -> None:
    op.add_column("messages", sa.Column("citations", sa.dialects.postgresql.JSONB()))
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("doc_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("department", sa.Text(), sa.ForeignKey("departments.slug"), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("doc_updated_at", sa.Date(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "classification IN ('public', 'internal', 'confidential')",
            name="document_classification_enum",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'deleted')", name="document_status_enum"
        ),
        sa.UniqueConstraint("doc_key", "version", name="document_key_version_unique"),
    )
    # The re-indexing invariant: never two active versions of one document.
    op.execute(
        "CREATE UNIQUE INDEX ux_documents_one_active ON documents (doc_key) WHERE status = 'active'"
    )
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY documents_read ON documents FOR SELECT USING ("
        "classification = 'public' "
        f"OR (classification = 'internal' AND department = ANY ({READ_DEPTS})) "
        f"OR (classification = 'confidential' AND department = ANY ({CONF_DEPTS})))"
    )
    op.execute(
        "CREATE POLICY documents_ingest ON documents FOR ALL "
        f"USING ({INGEST}) WITH CHECK ({INGEST})"
    )

    _chunk_table("chunks", "classification IN ('public', 'internal')")
    op.execute(
        "CREATE POLICY chunks_read ON chunks FOR SELECT USING ("
        f"classification = 'public' OR department = ANY ({READ_DEPTS}))"
    )
    _chunk_table("confidential_chunks", "classification = 'confidential'")
    op.execute(
        "CREATE POLICY confidential_chunks_read ON confidential_chunks FOR SELECT USING ("
        f"department = ANY ({CONF_DEPTS}))"
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("doc_key", sa.Text()),
        sa.Column("version", sa.Integer()),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'unchanged', 'failed', 'dead')",
            name="ingestion_job_status_enum",
        ),
    )


def downgrade() -> None:
    op.drop_table("ingestion_jobs")
    op.drop_table("confidential_chunks")
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_column("messages", "citations")
