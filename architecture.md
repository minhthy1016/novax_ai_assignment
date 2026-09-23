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
- **Structure recovery:** the parser tracks the full heading path ("Service playbooks >
  Payment API") and carries it across page breaks. PDFs have no markup, so headings are
  detected heuristically (standalone short line, numbered "2.1 API Tier" or title case).
  Before this, PDF chunks had no section context at all and page 2 lost page 1's heading.
- **Metadata schema:** `documents` = doc_key, version, title, department, classification,
  status (active/superseded/deleted), content hash, embedding model, chunker, source path,
  MIME, document date. Chunks denormalize department + classification (filtered in the same
  index scan), plus locator, heading path, page, matched text, parent context, `is_active`.
- **Chunking - measured, not assumed** (`evaluation/chunking_eval.py`, report in
  `evaluation/reports/chunking.md`): 34 gold questions over 10 documents (two long,
  heading-dependent ones added for this), gold evidence is exact source text independent of
  any chunker, same local embedder and access control for every strategy. Hybrid mode (as
  in production):

  | Strategy | Recall@1 | Recall@3 | MRR | Tokens to model @3 |
  |---|---:|---:|---:|---:|
  | structural-64 (first version) | 0.897 | 0.971 | 0.949 | 152 |
  | one chunk per page / whole doc | 0.853 | 1.000 | 0.926 | **941** |
  | fixed window 128/32 (structure-blind) | 0.853 | 1.000 | 0.917 | 401 |
  | Docling HybridChunker, 256 tok (reference) | 0.912 | 0.971 | 0.949 | 263 |
  | hierarchical-128 (Docling-style, ours) | 0.971 | 1.000 | 0.980 | 246 |
  | **parent-child 64/256 (chosen)** | **0.971** | **1.000** | **0.985** | 268 |

  Page-based chunks are the worst trade-off (lower precision, 6x the tokens). Small
  structural chunks lose context (R@1 0.897). Heading-aware strategies win; parent-child ties
  or leads everywhere at the same cost and **decouples matching granularity (small chunks,
  sharp embeddings) from context granularity (the whole section to the model)**, which matters
  more as documents get longer. Caveat: 34 cases - one case moves a metric by ~0.03; the gap
  to the first version is consistent in both vector-only and hybrid modes, the gap between
  the top strategies is not significant.
- Production code contains only parent-child (`knowledge/chunking.py`); the strategies it was
  measured against live in `evaluation/chunkers.py` and reuse the same building blocks, so
  the comparison differs only in the strategy.
- **Docling** was evaluated as a reference (HybridChunker with a tiktoken tokenizer, run in a
  separate environment), not adopted as a dependency: on these documents it did not beat the
  in-house hierarchical chunker, and it brings PyTorch and layout models into the image. For
  scanned PDFs, tables and complex layouts its layout model is the right tool (§6).
- **Parent-child mechanics:** children are packed to ~64 tokens inside a section; the section
  (≤256 tokens, never crossing a heading) is stored as `context`. The full heading path is
  prepended to what is embedded. Changing the chunker changes the content hash, so documents
  are re-indexed rather than silently mixing chunkings.
- **Embedding model: `nomic-embed-text` via Ollama (768-d, local)** with its task prefixes
  (`search_document:` / `search_query:`). Documents never leave the machine (required for
  confidential material, D-15), zero marginal cost, 768 dims fit pgvector HNSW. NIM
  `nemotron-3-embed-1b` (2048-d) was rejected for the index: data egress, and >2000 dims
  needs `halfvec`. The embedding model is recorded per chunk; changing it forces a re-index
  (D-12). CI uses a deterministic 768-d hashed mock with stopwords removed.
- **Citations:** the stable reference is `KB-ENG-003@v1#§Service playbooks › Payment API
  ¶13–14` (doc, version, section, paragraphs; PDFs add pages). The citation snippet is the
  *matched* passage, so the exact supporting text is shown even though the locator names the
  whole section.

### D-21 Retrieval: hybrid search, fusion, relevance gate, top-K
- Vector (HNSW, cosine) and full-text (tsvector, OR of terms) candidates, 20 each, fused
  with Reciprocal Rank Fusion (k=60). Full-text matters: on the gold set it lifts Recall@1 of
  the first chunker from 0.824 (vector only) to 0.897.
- Sibling children of one section are collapsed, so **top-K = 4 means four distinct
  sections** (~270 tokens of context in the gold set).
- **Relevance gate = cost/noise filter, not the abstention mechanism.** Calibration
  (`evaluation/relevance_calibration.py`) over the 34 gold questions and 12 questions that
  must be abstained: the weakest real match scores 0.610 but two out-of-scope questions score
  up to 0.658 - **no similarity threshold separates them** (the embedder sees the topic as
  related even when the user may not read the answering document). The gate is therefore set
  just below the weakest real match (`min_relevance = 0.60`, relative margin 0.10) so it never
  drops real evidence; abstention on the harder cases happens at generation (grounded prompt),
  and is measured in the evaluation. Clear misses (parental leave, weather) still abstain
  without any model call. Isolation never depends on this gate.
- **Through the live API** (`evaluation/retrieval_api_eval.py`; RLS, gate, dedupe, top-4):
  recall@1 0.941, recall@3 1.000, MRR 0.961.
- **Reranking:** no cross-encoder. RRF already fuses two signals and the candidate sets are
  small; recall@3 is 1.000 through the API. At the 1M-document scale a cross-encoder over the
  top ~50 is proposed (§6).
- **Nothing admitted -> fixed abstention without calling any model.**

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

### D-26 Agent orchestration on LangGraph
- **Implemented as planned.** The VPN flow is "propose → wait for a different person's
  approval → resume → execute exactly once", which maps onto LangGraph's `interrupt()` plus a
  Postgres checkpointer (durable, resumable across restarts).
- Outside the graph, in plain tested code: authorization, typed tool schemas, the
  pending-action store with action hashes, idempotency, and the hash-chained audit log. The
  graph decides *what to do next*; it never decides *whether it is allowed*.
- Routes: `small_talk` (deterministic, no model call), `knowledge`, `tool`, `refuse`. Before
  routing, "hi" was answered with "I couldn't find this in the approved knowledge" because
  every message went to retrieval. Small talk still cannot state anything about company
  knowledge - its reply is a fixed capability sentence.
- The router's output is **validated before it can act**: unknown tool names, malformed JSON
  or prose all fall back to the knowledge path, which cannot act.
- A conversation is one durable thread, so a paused approval survives a restart and resumes
  with the approver's decision - that is how the requester's conversation gains the final
  answer.
- **Cost:** one extra dependency and one extra model call per turn (visible in `llm_usage`).
  A hand-written state machine would have avoided both.

### D-28 The router runs on its own fast model (measured)
- **Finding:** when the router used the user's answer model, NIM `gpt-oss-20b` timed out
  twice at 60 s on the classification prompt; the gateway fell back and every turn silently
  became knowledge-only. The symptom looked like bad classification; it was latency.
- **Decision:** `OPSASSIST_ROUTER_MODEL` (default `ollama/llama3.2-3b`) routes independently
  of the answer model. Routing is small, frequent and cheap; a slow hosted model must not
  gate every turn. CI uses the deterministic mock, whose rule-based router keeps the agent
  paths under test without a real model.

### D-29 Tool contracts and what the model may influence
- Four tools, each a narrow function over typed fields: `search_internal_docs`,
  `get_server_status`, `create_support_ticket` and the sensitive `create_vpn_profile`.
  **No deploy tool, no generic execute/SQL/URL tool** (E11), so "deploy now and skip
  approval" cannot be honoured by any path.
- The model may propose a tool name and arguments. Everything after that is code: schema
  validation with `extra="forbid"`, a permission check against the database, and an audit
  record for allow *and* deny.
- Names resolve server-side (`employee_name` → employee id), so the model never invents an
  identifier; an ambiguous name returns an error asking for the id.
- `create_support_ticket` is idempotent for 10 minutes on (user, title, severity, details).
- Tool results are rendered from the returned data, never summarized by a model, so the
  assistant cannot describe a success that did not happen.

### D-30 Server status: field-level policy
- `server:read` shows identity, environment, status and last check for **every** server;
  **CPU and memory only to the owning department and IT Operations**, because another team's
  utilisation is their capacity information. Non-owners see an explicit "hidden" marker
  rather than a silent omission.
- U003 (HR, no `server:read`) is denied outright, as E07 expects.

### D-31 Sensitive actions: propose, confirm, execute once
- A sensitive tool never executes on the requester's turn. It becomes a **pending action**
  with a stable id and an **action hash** over the exact validated arguments plus requester.
- Approval requires the approve permission, a **different person**, an unexpired action, and
  the **matching hash** in the request body, so an approver can only confirm what was
  proposed.
- Execution is guarded by the pending row's status under a row lock: a second approval
  reports "already executed" instead of creating a second profile.
- The requester's permission is re-checked at execution: losing `vpn:create` while waiting
  rejects the action.

### D-32 Tamper-evident audit
- Every decision (allow, deny, pending, executed, error) is appended with
  `hash = sha256(prev_hash || canonical(record))`, written **in the same transaction as the
  action**, so a tool result cannot exist without its audit record.
- The runtime role may INSERT and SELECT on `audit_log` but **not UPDATE or DELETE**; an edit
  made as the owner is detected by `/api/audit/verify`, which reports the first broken id.
  Both are covered by integration tests.
- Arguments and results are redacted before storage; users read only their own records.

### D-27 Upload: an authorized user becomes a content source
- Uploading is a *write* into the knowledge base, gated by `kb:write:<department>` (held by
  the three managers in the sample data, candidate-added).
- **Metadata inside the file is untrusted.** The department comes from the permission; a file
  claiming another department is rejected rather than silently corrected. Classification
  defaults to the strictest the uploader may write; `confidential` needs
  `<department>:confidential`; `public` needs a publish permission nobody holds.
- Size and type limits, sanitized names, no execution, and a **credential scan** that rejects
  key-shaped strings.
- Poisoning by a legitimate owner cannot be prevented, so it is made visible: provenance
  (`uploaded_by`) is stored and audited, versions roll back, documents can be deleted.
  Requiring approval before a document joins the index is the next step if that risk
  outweighs convenience.
- Uploads live on a **shared volume** (object storage in production) because the API writes
  them and the worker reads them - found by an integration test, where the first version
  wrote into the API container and the worker failed with "file not found".

### D-40 Memory: allowlist, not model judgement
- **Persistent memory** stores only five preference keys (language, timezone, team,
  response_style, default_server), each short and scanned for credentials. A model cannot
  decide that a salary figure is worth remembering. Everything is listed and deletable
  through `/api/memory`.
- **Conversation memory** is the recent turns within a token budget plus a rolling summary of
  older ones. The summary prompt treats the transcript as data and is best-effort: if the
  model is unavailable the previous summary stands and the conversation still works.

### Pending decisions (filled on the day they are made)
- D-50 evaluation design: rubric, judge model, control baselines _(day 5)_

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
- The relevance gate cannot separate answerable from unanswerable questions on similarity
  alone (see D-21); abstention on near-topic questions relies on the grounded prompt.
- The gold retrieval set has 34 cases; differences under ~0.03 are within one case.
- Citation locators name the parent section; the precise matched passage is in the snippet.
