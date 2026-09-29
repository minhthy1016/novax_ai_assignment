```yaml
document_id: KB-AIP-002
title: LLM Gateway and Providers
department: ai_platform
classification: internal
status: outline
derived_from:
  - docs/decisions/D-02-deterministic-mock-provider-as-a-first-class-ada.md
  - docs/decisions/D-10-provider-abstraction-and-routing.md
  - docs/decisions/D-11-retry-fallback-and-circuit-breaking-rules.md
  - docs/decisions/D-12-embeddings-never-fall-back-across-models.md
  - docs/decisions/D-13-streaming-fallback-only-before-the-first-token-c.md
  - docs/decisions/D-14-usage-accounting-per-attempt.md
  - docs/decisions/D-15-data-classification-routing-to-providers-confirm.md
  - docs/decisions/D-16-three-model-picker-and-switching-within-a-conver.md
  - docs/decisions/D-28-the-router-runs-on-its-own-fast-model-measured.md
  - config/models.toml
  - src/opsassist/gateway/gateway.py
```

# LLM Gateway and Providers

**Scope:** how the assistant talks to models, and what happens when a model fails.

## 1. The abstraction
- **Two protocols,** `ChatProvider` and `EmbeddingProvider`.
- **Adapters translate wire formats** and map failures into one error hierarchy: timeout,
  unavailable, rate-limited, auth, model not found, bad request, bad response.
- **The gateway decides retry and fallback from the error type,** never from vendor codes.

## 2. Adapters
- **NVIDIA NIM** through the OpenAI-compatible API. The same adapter covers vLLM and
  OpenAI.
- **Claude** through the official SDK:
  - SDK retries are off, so the gateway is the only retry layer;
  - server errors are classified by status code.
- **Ollama** through its native API: NDJSON streaming, its own usage fields.
- **A deterministic mock** whose model names select behaviours (echo, slow, flaky, down,
  rate-limited).
  - It runs in development and test only; enabling it in production fails configuration
    validation.

## 3. Catalog and routes
- `config/models.toml` declares providers, models, prices, context windows and routes.
- **Business code asks for a route or a model id,** so swapping models is a configuration
  change.
- **The catalog is validated at startup:** unknown references, mixed-kind routes, missing
  dimensions.
- **A provider without its API key is disabled and reported** by `GET /api/models`; it is
  not an error.
- **Model picker:**
  - The user's chosen model goes first; the others follow as fallbacks.
  - The choice is stored on the conversation. Switching models keeps the whole history.

## 4. Retry, fallback and circuit breaking
- **Per error type:**

  | Error | Retry | Fallback | Trips the breaker |
  |---|---|---|---|
  | Timeout, 5xx, connection | yes, full-jitter backoff | yes | yes |
  | Rate-limited | yes, honouring `Retry-After` | yes | yes |
  | Auth | no | yes | yes |
  | Model not found | no | yes | no |
  | Bad request | no | no | no |

- **Deadlines:** each attempt has its own timeout, inside an overall request deadline.
- **Circuit breakers are per model,** not per provider, and per API instance.

## 5. Streaming
- **Fallback only before the first token.** After that, a failure ends the stream with an
  error event, and the partial answer is stored as `partial`, excluded from later history.
- **Two timeouts:** time to first token, and idle time between chunks.
- **A client disconnect cancels the upstream request.** The attempt is recorded as
  cancelled, with estimated tokens.
- **The stream path runs the same agent routing as the non-streaming path.** Only
  knowledge answers stream tokens.

## 6. Usage accounting
- **One `llm_usage` row per attempt,** including failures, skips and cancellations.
- Each row holds: request id, user, conversation, latency, time to first token, tokens
  (flagged when estimated) and estimated cost.
- **Usage writes never fail the user's request.**

## 7. Data egress
- **Providers are marked** `data_egress = true` (hosted) or `false` (on the box).
- **The context's most sensitive classification decides** whether it may go to a hosted
  provider. Confidential context never does.
- **The rule holds for the whole conversation.** Once a conversation has used confidential
  context, every later turn stays on-box.
- **Blocked targets are reported as skipped,** so the restriction is visible in the
  attempts.

## 8. Model roles
- **Router:** its own small, fast local model, independent of the user's answer model, so
  a slow hosted model never gates every turn.
- **Escalation:** a larger local model, used once for a weak answer.
- **Conversation summary:** written on-box only.

## Excluded from this page
- Latency and timeout measurements of specific providers, router comparison results.
- API keys and endpoint values (variable names only).
