"""Least-privilege runtime role so row-level security actually applies.

The API, worker and ingestion connect as ``opsassist_app``: LOGIN, but NOSUPERUSER and
NOBYPASSRLS, with DML on application tables only (no DDL, no role management). Postgres
superusers bypass RLS even with FORCE ROW LEVEL SECURITY, so connecting as the schema
owner would have silently disabled the storage-layer isolation. Found by
tests/integration/test_rag_api.py::test_row_level_security_blocks_reads_even_without_the_app_filter.

The password comes from OPSASSIST_APP_DB_PASSWORD (never from this file).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""

from collections.abc import Sequence

from alembic import op
from psycopg import sql

from opsassist.config import get_settings

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "opsassist_app"


def upgrade() -> None:
    password = get_settings().app_db_password.get_secret_value()
    literal = sql.Literal(password).as_string(None)
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
              PASSWORD {literal};
          ELSE
            ALTER ROLE {ROLE} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
              PASSWORD {literal};
          END IF;
        END $$;
        """
    )
    op.execute(f"GRANT CONNECT ON DATABASE {op.get_bind().engine.url.database} TO {ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")
    # Identity data and schema bookkeeping are read-only for the runtime role.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON users, departments, alembic_version FROM {ROLE}")
    # Tables created by later migrations get the same DML grants automatically.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {ROLE}")
