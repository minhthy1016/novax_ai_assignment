# D-02: Deterministic mock provider as a first-class adapter

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Decision:** a mock provider with injectable latency, timeouts and 5xx errors.
- **Why:** reproducible evaluation runs, offline CI, and a provider-failure demo that does not
  depend on a real outage.
