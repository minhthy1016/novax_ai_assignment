```yaml
document_id: KB-AIP-004
title: Security and Data Boundaries
department: ai_platform
classification: internal
status: outline
derived_from:
  - docs/decisions/D-08-role-bound-jwt-local-issuer-as-a-stand-in-for-th.md
  - docs/decisions/D-15-data-classification-routing-to-providers-confirm.md
  - docs/decisions/D-17-least-privilege-runtime-database-role-security-f.md
  - docs/decisions/D-22-department-isolation-application-filter-row-leve.md
  - docs/decisions/D-25-retrieved-content-is-untrusted.md
  - docs/decisions/D-27-upload-an-authorized-user-becomes-a-content-sour.md
  - docs/decisions/D-32-tamper-evident-audit.md
  - docs/decisions/D-40-memory-allowlist-not-model-judgement.md
  - docs/decisions/D-61-rate-limiting-per-caller-token-buckets.md
  - src/opsassist/policy/access.py
```

# Security and Data Boundaries

**Scope:** who may see and do what, and where each control lives. The principle: the model
proposes, the backend decides. No control depends on a prompt.

## 1. Identity
- **Tokens are bound to a user and a role.** Permissions are loaded from the database on
  every request, never trusted from the token or the prompt. A role change invalidates
  old tokens.
- **Development and test use a local token issuer,** standing in for the company identity
  provider. Production would validate OIDC tokens, and nothing else changes.

## 2. Access scope and isolation
- **Scope comes from permissions:**
  - `docs:<department>` grants internal documents;
  - adding `<department>:confidential` grants confidential ones;
  - public documents are for everyone.
- **Enforced twice, in one transaction:**
  - the application's SQL filter;
  - Postgres row-level security, driven by transaction-local settings. Missing settings mean
    nothing is readable (fail closed).
- **The runtime database role is not a superuser** and cannot bypass row-level security.
  Migrations and seeding use the owner role.
- **Confidential material lives in a separate table** with its own policy, and is not even
  queried unless the scope includes it.
- **Document titles are under row-level security too,** so unreadable titles cannot leak
  through joins.
- **Tools inherit the same scope.**

## 3. Data egress
- **Hosted providers may receive public and internal context only.** The bar is one
  setting and can be lowered to public-only.
- **The rule is decided on the whole conversation:** its most sensitive context is recorded
  before any model sees it.
- **Summaries and near-miss reviews** of confidential material stay on-box.

## 4. Untrusted content
- **Retrieved text is data:** escaped, placed in the user turn, with no authority.
- **Tools cannot be called from the knowledge path.**
- **Uploads:** metadata inside a file is untrusted; the department comes from the
  uploader's permission; credential-shaped strings are rejected.

## 5. Audit
- **Every decision is appended to a hash chain,** in the same transaction as the action:
  allow, deny, pending, executed, error.
- **The runtime role may insert and read audit rows, not update or delete them.** An edit
  made as the owner is detected by `/api/audit/verify`.
- **Arguments and results are redacted before storage;** users read only their own
  records.

## 6. Abuse and secrets
- **Rate limits:** per-caller token buckets in Redis, with a stricter budget for routes that
  cost a model call. They fail open: they are a capacity control, not an authorization
  control.
- **Secrets:** settings hold secrets as secret types, so they never appear in logs.
  Production refuses the development token secret. Persistent memory rejects credential-
  shaped values.

## Excluded from this page
- Passwords, keys and environment values (variable names only).
- The content of any confidential document, and which people hold which permission in the
  sample data.
