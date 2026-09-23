# D-11: Retry, fallback and circuit-breaking rules

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

| Error | Retry | Fallback | Trips breaker |
|---|---|---|---|
| timeout / 5xx / connection | yes, full-jitter exponential backoff | yes | yes |
| 429 | yes, honours `Retry-After` up to the cap | yes | yes |
| 401/403 | no | yes | yes |
| 404 model not found | no | yes | no |
| 400/422 | no | **no** (our request is wrong everywhere) | no |
- Each attempt has its own timeout, bounded by an overall request deadline, so retries can
  never exceed what the caller was promised.
- **Breakers are per model, not per provider** — found by an integration test: a failing
  model tripped the breaker for healthy siblings on the same provider. NIM hosts each model
  as a separate deployment, so per-model isolation matches reality.
- Breaker state is per API instance (see §6 for shared state at scale).
