# D-13: Streaming: fallback only before the first token; cancellation propagates

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Fallback after tokens reach the client would splice two answers together. A mid-stream
  failure ends with an explicit `error` event (`partial: true`) and the partial answer is
  stored with status `partial`, excluded from future prompt history.
- Time-to-first-token and inter-chunk idle timeouts are enforced separately.
- Client disconnect cancels the handler, closes the upstream HTTP stream, stores the partial
  answer and records the attempt as `cancelled` with estimated tokens (verified live against
  Ollama: disconnect after 1.5 s → `partial` message + `cancelled` usage row).
