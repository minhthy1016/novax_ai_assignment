# D-15: Data-classification routing to providers (confirmed with the team lead)

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Every provider declares `data_egress`. The context's **most sensitive classification**
  decides whether a call may leave the box, compared against
  `OPSASSIST_EGRESS_MAX_CLASSIFICATION` (default `internal`).
- **Confidential context never reaches an external model** - confirmed explicitly for Claude
  and similar services. Hosted providers are skipped and reported as
  `skipped:egress_not_permitted`, so only on-box models (Ollama) see the text; if none is
  available the request fails (503) rather than leaking. Verified live: U004's compensation
  question was answered by Ollama with NIM and Claude visibly skipped.
- `internal` material may go to hosted providers today. Setting the variable to `public`
  keeps internal documents on-box as well - one environment variable, no code change.
