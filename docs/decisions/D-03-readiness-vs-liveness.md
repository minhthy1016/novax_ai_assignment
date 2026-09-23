# D-03: Readiness vs liveness

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- `/healthz` never touches dependencies (a slow database must not get healthy processes
  restarted). `/readyz` checks Postgres (incl. pgvector and migration state) and Redis with a
  bounded timeout and returns 503 with per-dependency detail. Failure detail carries the
  exception type only — connection errors can embed DSNs with credentials.
