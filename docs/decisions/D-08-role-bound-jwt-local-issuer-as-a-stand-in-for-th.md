# D-08: Role-bound JWT; local issuer as a stand-in for the company IdP

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Nothing under `/api` runs without a valid token (enforced by a test that enumerates every
  route from the OpenAPI schema). Only `/healthz`, `/readyz`, `/metrics` and the dev-only
  issuer are public — probes and scrapers cannot carry user tokens.
- Tokens bind **user ID + assigned role** (`sub`, `role`). Each request re-checks both
  against the database: unknown/deactivated user or a changed role → 401, sign in again.
  **Permissions are never read from the token** — always from the database — so revoking
  one is immediate. Verification pins the algorithm (rejects `alg=none`), audience, issuer,
  and requires `role`.
- `POST /api/auth/dev-token` mints tokens for seeded users and is **only mounted in dev/test**.
- **Alternative rejected:** permissions inside the token (stateless) — revocation would wait
  for expiry, unacceptable for `vpn:create` / `hr:confidential`.
- **Production:** validate OIDC tokens from the corporate IdP; the issuer endpoint goes away.
