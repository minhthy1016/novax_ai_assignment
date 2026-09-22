# Architecture

> Status: **skeleton (day 1)**. Sections marked _TBD_ are filled as each component lands, so this
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
AuthN → Principal{user, department, permissions}                        [day 4]
  ▼
Orchestrator ─ decides: answer from knowledge | call a tool | refuse   [day 3-4]
  ├─ Retrieval: filters by principal in SQL + Postgres RLS, then ranks  [day 3]
  ├─ Policy engine: authorizes each tool call against the principal     [day 4]
  ├─ Tools: typed schemas, pending-action approval for sensitive ones   [day 4]
  └─ Provider gateway: routing, retry, timeout, fallback, usage         [day 2]
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
| Worker | Dramatiq _(day 3)_ | Ingestion, re-indexing, eval jobs, dead-letter handling |
| LLM providers | NVIDIA NIM, Anthropic, xAI, Ollama, mock | Behind one `Provider` interface |
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

### Pending decisions (filled on the day they are made)
- D-10 provider routing, fallback order and data-classification routing _(day 2)_
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
