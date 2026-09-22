# Architecture

> Status: **day 3** (gateway, providers, conversations, RAG knowledge system). Sections marked _TBD_ are filled as each component lands, so this
> document only ever describes what the code actually does.

## 1. Request path

The system is one vertical slice. Every request passes the same trust boundaries in order:

```
Client (curl / chat UI / Flowise demo)
  │  bearer token
  ▼
FastAPI ─ CorrelationMiddleware (request ID, JSON log line, HTTP metrics)
  │       rate limit (Redis)                                           [day 4]
  ▼
AuthN → Principal{user, department, permissions}  (JWT; permissions loaded per request)
  ▼
Orchestrator ─ decides: answer from knowledge | call a tool | refuse   [day 3-4]
  ├─ Retrieval: AccessScope -> SQL filter + Postgres RLS -> hybrid rank -> gate -> top-K
  ├─ Policy engine: authorizes each tool call against the principal     [day 4]
  ├─ Tools: typed schemas, pending-action approval for sensitive ones   [day 4]
  └─ Provider gateway: routing, retry, timeout, fallback, usage
  ▼
Response with citations ─ audit record (hash-chained) ─ metrics ─ trace (Opik, optional)
```

The model is treated as an untrusted planner. It never holds credentials, never sees
documents the caller cannot read, and its tool requests are re-validated by the policy
engine before anything executes.

## 2. Components

| Component | Technology | Responsibility |
|---|---|---|
| API | Python 3.12, FastAPI, Pydantic v2 | Contracts, auth, orchestration, policy |
| Relational store | PostgreSQL 17 | Users, conversations, usage, pending actions, audit |
| Vector store | pgvector (same Postgres) | Chunk embeddings + department / classification / version metadata |
| Cache, limits, queue | Redis 7 | Rate limits, cache, worker broker |
| Worker | Dramatiq on Redis | Ingestion and re-indexing with retries and a dead-letter queue |
| LLM providers | NVIDIA NIM (OpenAI-compatible), Claude (official Anthropic SDK), Ollama (native API), deterministic mock | Behind `ChatProvider` / `EmbeddingProvider` protocols |
| Observability | structlog JSON, Prometheus, Opik (optional profile) | Logs, metrics, LLM traces and eval experiments |
| Demo UI | Flowise (optional profile) | Thin client of `/api/chat` only — holds no policy or credentials |

## 3. Decisions

Each decision lists the alternatives considered and why they lost. Format: context →
decision → consequences.

### D-01 pgvector in the primary Postgres instead of a dedicated vector DB
- **Context:** isolation filters must be enforced in a trusted layer, and document versions,
  ACLs and audit records need to stay consistent with the chunks they describe.
- **Decision:** store embeddings in Postgres with pgvector. Filtering happens in the same SQL
  query as similarity search, and Postgres row-level security adds a second storage-layer guard.
- **Alternatives:** Qdrant / Weaviate (better ANN at very large scale, payload filters) —
  but ACL metadata would then live in two systems with no transaction across them.
- **Consequences:** simple and transactional at this size; the scale proposal (§6) covers when
  and how to move to a sharded vector tier.

### D-02 Deterministic mock provider as a first-class adapter
- **Decision:** a mock provider with injectable latency, timeouts and 5xx errors.
- **Why:** reproducible evaluation runs, offline CI, and a provider-failure demo that does not
  depend on a real outage.

### D-03 Readiness vs liveness
- `/healthz` never touches dependencies (a slow database must not get healthy processes
  restarted). `/readyz` checks Postgres (incl. pgvector and migration state) and Redis with a
  bounded timeout and returns 503 with per-dependency detail. Failure detail carries the
  exception type only — connection errors can embed DSNs with credentials.

### D-04 Correlation IDs accept caller input only if log-safe
- A caller-supplied `X-Request-ID` is kept only if it matches `[A-Za-z0-9._-]{8,64}`;
  otherwise a new ID is minted. This keeps cross-service tracing while blocking log injection.

### D-05 Metric labels are bounded
- Route templates (`/api/conversations/{id}`), never raw paths; no user or document IDs as
  labels. Unbounded label cardinality is a common way to take down a metrics backend.

### D-06 Flowise is a client, not the orchestrator
- **Decision:** Flowise may be used as a demo chat UI calling `/api/chat`. It holds no
  credentials and makes no policy decisions.
- **Why:** policy, confirmation state and typed tool schemas must be unit-testable code in the
  trusted API. Low-code flows with custom-code nodes widen the arbitrary-execution surface,
  which the brief lists as a critical finding.

### D-07 CubeJS not used
- The structured data here (4 servers, 6 users) does not need a semantic layer; adding one
  would add a service without adding capability. It is a candidate for a future
  `query_metrics` tool over governed analytics data (§7).

### D-08 Role-bound JWT; local issuer as a stand-in for the company IdP
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

### D-10 Provider abstraction and routing
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

### D-16 Three-model picker and switching within a conversation
- `selectable` in the catalog lists the models end users can choose:
  `nim/gpt-oss-20b`, `claude/sonnet-4.5`, `ollama/llama3.2-3b`. Choosing one puts it first
  and the other two follow as fallbacks, so a choice is honoured when possible and the
  response says when it was not (`fallback_used`, per-attempt outcomes).
- The choice is stored on the conversation. Sending `model` switches it; omitting it keeps
  the current model; `PATCH /api/conversations/{id}` switches without a message. History
  is model-independent, so the new model sees the whole conversation (Task 4 conversation
  memory). Each assistant message records which model produced it.
- Outside dev/test only picker models are accepted; routes and mock/demo models are
  dev/test-only. A stored choice later removed from the catalog falls back to the default
  instead of failing the next message.

### D-11 Retry, fallback and circuit-breaking rules
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

### D-12 Embeddings never fall back across models
- Vectors from different models live in different spaces; silently switching would corrupt
  retrieval. Embedding routes must have exactly one target (enforced by the catalog
  validator). Embedding failures retry, then fail loudly.

### D-13 Streaming: fallback only before the first token; cancellation propagates
- Fallback after tokens reach the client would splice two answers together. A mid-stream
  failure ends with an explicit `error` event (`partial: true`) and the partial answer is
  stored with status `partial`, excluded from future prompt history.
- Time-to-first-token and inter-chunk idle timeouts are enforced separately.
- Client disconnect cancels the handler, closes the upstream HTTP stream, stores the partial
  answer and records the attempt as `cancelled` with estimated tokens (verified live against
  Ollama: disconnect after 1.5 s → `partial` message + `cancelled` usage row).

### D-14 Usage accounting per attempt
- One `llm_usage` row per provider attempt — including failures, skips and cancellations —
  with request ID, user, conversation, latency, TTFT, tokens (flagged when estimated) and
  estimated cost. Retries and fallbacks cost money and latency; hiding them hides the bill.
- Costs use reference prices from the catalog (NIM trial usage is free; prices show what the
  same traffic would cost on a paid endpoint).
- Usage writes are shielded from cancellation and never fail the user's request.

### D-15 Data-classification routing to providers
- Every provider declares `data_egress`. When the retrieved context contains a
  `confidential` chunk, the gateway call is made with `allow_egress = false`: hosted
  providers (NIM, Claude) are skipped and reported as `skipped:egress_not_permitted`, so only
  on-box models (Ollama) see confidential text. If none is available the request fails
  (503) rather than leaking. Verified live: U004's compensation question was answered by
  Ollama with NIM and Claude visibly skipped.
- `internal` material may go to hosted providers (assumes a data-processing agreement);
  tightening this is one flag per provider.

### D-17 Least-privilege runtime database role (security finding)
- **Finding:** the first RLS test showed an unscoped query returning *every* chunk,
  including the confidential one. The app was connecting as the Postgres superuser the
  container creates, and **superusers bypass row-level security even with FORCE**. The
  storage-layer guard existed on paper only.
- **Fix:** migration 0005 creates `opsassist_app` (LOGIN, NOSUPERUSER, NOBYPASSRLS, DML
  only; users/departments read-only). API, worker and ingestion connect as it; only
  migrations and seeding use the owner. A test asserts the runtime role is not a superuser
  and cannot bypass RLS, and another runs an unfiltered query as that role.
- Production: the password comes from `OPSASSIST_APP_DB_PASSWORD`; the dev default is
  rejected outside dev/test. A further split (read-only API role vs. ingestion role) is
  listed under future work.

### D-20 Ingestion: parsing, metadata, chunking, embeddings
- **Formats:** Markdown (front matter), plain text and PDF (`pypdf`, layout mode so paragraph
  gaps survive extraction) with a `.meta.json` sidecar. Metadata is **mandatory and
  validated**: a document without department and classification is rejected, never indexed
  as "no ACL". Ingestion is confined to the knowledge root (no arbitrary file reads).
- **Metadata schema:** `documents` = doc_key, version, title, department, classification,
  status (active/superseded/deleted), content hash, embedding model, source path, MIME,
  document date. Chunks denormalize department + classification (filtered in the same index
  scan), plus locator, section, page, token count, embedding model, `is_active`.
- **Chunking:** structure-aware (headings, list items, paragraphs never split unless
  oversized), target ~64 tokens, max 160, one sentence of overlap only when an oversized
  paragraph is split, title+section header prepended to the embedded text. At a 120-token
  target every sample document collapsed into one chunk and citations could only say
  "¶1–7"; at 64 the deploy window is cited as ¶1–4 and the injection text lands in its own
  chunk. To be re-measured (64 vs 120 vs whole-doc) in the evaluation.
- **Embedding model: `nomic-embed-text` via Ollama (768-d, local).** Documents never leave
  the machine (required for confidential material, D-15), zero marginal cost, 768 dims fit
  pgvector HNSW. NIM `nemotron-3-embed-1b` (2048-d) was rejected for the index: data egress,
  and >2000 dims needs `halfvec`. The embedding model is recorded per chunk; changing it
  forces a re-index instead of silently mixing vector spaces (D-12). CI uses a deterministic
  768-d hashed mock with stopwords removed.
- **Citations:** `KB-ENG-001@v1#¶1–4` is stable (doc key, version, locator); the label
  shown to users is "Production Deployment Procedure (KB-ENG-001 v1, ¶1–4)". PDF locators
  include pages ("p.1 ¶3").

### D-21 Retrieval: hybrid search, fusion, relevance gate, top-K
- Vector (HNSW, cosine) and full-text (tsvector, OR of terms) candidates, 20 each, fused
  with Reciprocal Rank Fusion (k=60) - no score calibration between two scales needed.
- **Admission is by semantic similarity**: ≥ the model's `min_relevance` AND within 0.10 of
  the best hit. Full-text only affects ranking; letting it admit on its own let single
  common words ("incident", "notes") pull in unrelated chunks.
- **Calibration** (nomic, sample queries): correct top hits scored 0.65-0.79; irrelevant
  hits for users without access scored 0.57-0.59. `min_relevance = 0.60`:

  | Query (user) | Before gate | After gate |
  |---|---|---|
  | deploy window (U001) | ENG-001 ×2 + PUB 0.589 | ENG-001 ¶1–4, ¶5–7 |
  | payment incident (U001) | ENG-002 ×2 + PUB 0.416 | ENG-002 ×2 |
  | deploy window (U003, no eng access) | PUB 0.589, HR 0.569 | nothing → abstain |
  | parental leave (U001) | nothing | nothing → abstain |
  | compensation notes (U004) | HR-002 | HR-002 (confidential table) |

- **Top-K = 4:** the corpus answers are 1-2 chunks; 4 leaves room for multi-part
  questions (E12 used both incident chunks) within ~300 tokens of context.
- **Reranking:** no cross-encoder. RRF fusion already reorders by two signals, the
  candidate sets are tiny, and a reranker adds a model and latency to every request. At the
  1M-document scale a cross-encoder over the top ~50 is proposed (§6); the eval will show
  whether it pays for itself here.
- **Nothing admitted → fixed abstention without calling any model** (cheapest possible
  answer, and it cannot hallucinate).

### D-22 Department isolation: application filter + row-level security
- Access scope is computed from database permissions: `docs:<dept>` for internal,
  `docs:<dept>` + `<dept>:confidential` for confidential, public for everyone.
- Enforced twice in the same transaction: the SQL filter, and Postgres RLS policies driven
  by `set_config('app.read_departments' | 'app.confidential_departments', ..., true)`.
  Missing settings mean nothing is readable (fail closed). Writes require `app.ingest=on`.
- **Confidential documents live in a separate table** (`confidential_chunks`) with their own
  policy, never in the shared index - KB-HR-002 itself requires it. That table is not even
  queried unless the scope includes a confidential department.
- `documents` is under RLS too, so titles of unreadable documents cannot leak through joins.

### D-23 Document lifecycle and re-indexing
- A content hash over text + metadata makes ingestion idempotent ("unchanged").
  Reclassifying a document changes the hash and re-indexes it into the right table.
- New versions are swapped in atomically: supersede old → deactivate its chunks → insert new,
  in one transaction under a per-document advisory lock. A partial unique index guarantees
  at most one active version per document. Old versions stay (inactive) so past citations
  still resolve. Deletion marks the document `deleted` and deactivates its chunks.

### D-24 Ingestion worker, retries and dead-letter queue
- Dramatiq on Redis with the AsyncIO middleware. Transient errors retry with exponential
  backoff (1-30 s, 3 retries); permanent ones (parse error, missing metadata, policy
  violation, path outside the root) fail immediately - retrying cannot fix them. Exhausted
  messages go to Dramatiq's dead-letter queue and the job is marked `dead`. Every job and
  attempt is visible in `ingestion_jobs` (`make jobs`).

### D-25 Retrieved content is untrusted
- Sources are placed in the user turn inside `<source>` elements with escaped content (a
  document cannot close its element or forge a system block - unit-tested), and the system
  prompt gives them no authority. Citation markers are validated against the sources that
  were actually provided; invalid ones are stripped and counted. Full-width markers (`【2】`,
  emitted by gpt-oss) are normalized.
- Live check (E09): asked to follow KB-TEST-999's instructions, gpt-oss declined, explained
  that document text is information rather than instructions, and summarized the legitimate
  content with a citation. There are no tools in this mode, so nothing could be executed;
  D4 adds tools behind an authorization layer the model cannot bypass.

### Pending decisions (filled on the day they are made)
- D-20 chunking, embedding model, top-K, reranking — with measurements _(day 3)_
- D-21 confidential-document partitioning _(day 3)_
- D-30 server-status field-level policy (does ownership change access?) _(day 4)_
- D-31 pending-action approval model and action hashing _(day 4)_
- D-32 tamper-aware audit design _(day 4)_
- D-40 memory: what may be persisted, token budget _(day 4)_

## 4. Security model
_TBD (day 4)._ Threat model, trust boundaries, and how each critical finding is prevented.

## 5. Evaluation design
_TBD (day 5)._

## 6. Scale proposal
_TBD (day 6)._ 5,000 employees · 1M documents · 100 concurrent AI requests · GPU cluster.

## 7. Known limitations and future work
_Maintained continuously._
- Authentication is a local token issuer for the assessment, not an SSO/OIDC integration.
- Circuit-breaker state is per API instance, not shared.
- Token counts are estimated (~4 chars/token) only when a provider omits usage; such rows are
  flagged `tokens_estimated`.
- Conversation history uses a simple recent-messages window within a token budget; summaries
  arrive with Task 4.
- Retrieval uses the latest message only; follow-ups like "and on Thursday?" are not yet
  rewritten into standalone queries (Task 4).
- Prompt adherence varies run to run: in two live runs of E10, one answer opened with "No,"
  despite the instruction to keep "not confirmed" wording. The evaluation measures this
  over repeated runs rather than relying on single samples.
- One runtime database role for API and worker; a read-only API role is future work.
- The relevance threshold is calibrated on a small query set; it is re-measured in the
  evaluation (false abstentions vs. irrelevant context).
