# D-22: Department isolation: application filter + row-level security

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Access scope is computed from database permissions: `docs:<dept>` for internal,
  `docs:<dept>` + `<dept>:confidential` for confidential, public for everyone.
- Enforced twice in the same transaction: the SQL filter, and Postgres RLS policies driven
  by `set_config('app.read_departments' | 'app.confidential_departments', ..., true)`.
  Missing settings mean nothing is readable (fail closed). Writes require `app.ingest=on`.
- **Confidential documents live in a separate table** (`confidential_chunks`) with their own
  policy, never in the shared index - KB-HR-002 itself requires it. That table is not even
  queried unless the scope includes a confidential department.
- `documents` is under RLS too, so titles of unreadable documents cannot leak through joins.
