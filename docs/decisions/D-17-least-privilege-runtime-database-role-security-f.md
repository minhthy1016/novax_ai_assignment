# D-17: Least-privilege runtime database role (security finding)

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Finding:** the first RLS test showed an unscoped query returning *every* chunk,
  including the confidential one. The app was connecting as the Postgres superuser the
  container creates, and **superusers bypass row-level security even with FORCE**. The
  storage-layer guard existed on paper only.
- **Fix:** migration 0005 creates `opsassist_app` (LOGIN, NOSUPERUSER, NOBYPASSRLS, DML
  only; users/departments read-only). API, worker and ingestion connect as it; only
  migrations and seeding use the owner. A test asserts the runtime role is not a superuser
  and cannot bypass RLS, and another runs an unfiltered query as that role.
- Production: the password comes from `OPSASSIST_APP_DB_PASSWORD`; the dev default is
  rejected outside dev/test. A further split (read-only API role vs. ingestion role) is
  listed under future work.
