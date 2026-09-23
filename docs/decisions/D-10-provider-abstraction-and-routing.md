# D-10: Provider abstraction and routing

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Two protocols, `ChatProvider` and `EmbeddingProvider`; adapters translate wire formats and
  map failures into one error hierarchy (`ProviderTimeout`, `ProviderUnavailable`,
  `ProviderRateLimited`, `ProviderAuthError`, `ProviderModelNotFound`, `ProviderBadRequest`,
  `ProviderResponseError`). The gateway decides retry/fallback from the error *type*, never
  from vendor-specific codes.
- **Claude** via the official `anthropic` SDK (1.x) with SDK retries disabled
  (`max_retries=0`) so the gateway is the single retry layer. SDK 1.x removed sampling
  kwargs; `temperature` is sent via `extra_body` for models that accept it (Sonnet 4.5), and
  the catalog flag `supports_temperature = false` strips it for models that reject it.
  Server errors are classified **by status code**: in SDK 1.x, 503/504/529 are sibling
  classes of `InternalServerError`, and a class-based mapping misrouted 529 "overloaded"
  as a bad request (which would have disabled fallback exactly when it is needed) — caught
  by a unit test. Disabled until `ANTHROPIC_API_KEY` is set.
- Adapters: **NVIDIA NIM** via the OpenAI-compatible API (also covers vLLM/OpenAI), **Ollama
  via its native API** (NDJSON streaming, different usage fields — deliberately not the
  OpenAI shim, so the abstraction is exercised by two genuinely different wire formats), and
  a **deterministic mock** whose model names select behaviours (`echo`, `slow`, `flaky`,
  `down`, `ratelimit`).
- `config/models.toml` declares providers, models, prices and routes. Business logic asks for
  a route (`chat-default`) or a model id; swapping models is a config change. The catalog is
  validated at startup (unknown references, mixed-kind routes, missing dimensions).
- Default chat route: `nim/gpt-oss-20b` → `ollama/llama3.2-3b`. A provider whose API key is
  absent is disabled (reported by `GET /api/models`), not an error.
- The mock provider is on in dev/test only; enabling it in prod fails config validation.
- **Alternatives:** LiteLLM (broad provider coverage, but its retry/fallback semantics would
  be a black box we must defend in review, and mid-stream fallback/cancellation behaviour
  would be theirs).
