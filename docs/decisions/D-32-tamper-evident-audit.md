# D-32: Tamper-evident audit

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Every decision (allow, deny, pending, executed, error) is appended with
  `hash = sha256(prev_hash || canonical(record))`, written **in the same transaction as the
  action**, so a tool result cannot exist without its audit record.
- The runtime role may INSERT and SELECT on `audit_log` but **not UPDATE or DELETE**; an edit
  made as the owner is detected by `/api/audit/verify`, which reports the first broken id.
  Both are covered by integration tests.
- Arguments and results are redacted before storage; users read only their own records.
