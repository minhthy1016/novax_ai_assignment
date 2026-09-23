# D-06: Flowise is a client, not the orchestrator

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Decision:** Flowise may be used as a demo chat UI calling `/api/chat`. It holds no
  credentials and makes no policy decisions.
- **Why:** policy, confirmation state and typed tool schemas must be unit-testable code in the
  trusted API. Low-code flows with custom-code nodes widen the arbitrary-execution surface,
  which the brief lists as a critical finding.
