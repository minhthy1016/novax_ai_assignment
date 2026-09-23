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


Each decision lists the alternatives considered and why they lost. Format: context →
decision → consequences.

### Module map

```mermaid
flowchart LR
  client["Client<br/>curl · chat UI"]

  subgraph api["FastAPI · src/opsassist"]
    mw["middleware.py<br/>request ID · JSON logs · metrics"]
    auth["auth.py<br/>role-bound JWT → Principal"]
    routes["api/<br/>chat · stream · search · models · conversations"]
    policy["policy/access.py<br/>AccessScope from DB permissions"]
    retrieval["knowledge/retrieval.py<br/>hybrid search · RRF · relevance gate · top-4 sections"]
    rag["rag.py<br/>grounded prompt · citation check · abstention"]
    gateway["gateway/<br/>routing · retry · fallback · circuit breaker<br/>egress control · usage per attempt"]
    providers["providers/<br/>NIM · Claude · Ollama · mock"]
    agent["agent/ (LangGraph)<br/>route: small talk · knowledge · tool · refuse"]
    tools["tools/ + policy/<br/>typed schemas · permissions<br/>approvals · hash-chained audit"]
  end

  subgraph worker["Dramatiq worker"]
    ingest["knowledge/ingest.py<br/>parse · parent-child chunks · embed · versioned swap"]
  end

  cli["make ingest"]
  pg[("PostgreSQL 17 + pgvector<br/>row-level security<br/>runtime role opsassist_app")]
  redis[("Redis<br/>job queue")]
  models{{"NVIDIA NIM · Anthropic · Ollama (local)"}}

  client --> mw --> auth --> routes
  routes --> agent
  agent --> tools --> pg
  routes --> policy --> retrieval --> pg
  routes --> rag --> gateway --> providers --> models
  retrieval -. "query embedding" .-> gateway
  agent -. "routing call" .-> gateway
  cli --> redis --> ingest --> pg
  routes -. "upload" .-> redis
  ingest -. "passage embeddings" .-> gateway
```

### Document ingestion

```mermaid
flowchart LR
  file["PDF · Markdown · text<br/>+ metadata (department, classification)"]
  queue["make ingest<br/>job row + Redis message"]
  guard{"inside knowledge root?<br/>metadata valid?"}
  parse["parse<br/>headings path across pages"]
  chunk["parent-child chunks<br/>~64-token children, ≤256-token sections"]
  embed["embed children<br/>nomic, local"]
  swap["one transaction:<br/>supersede old version,<br/>insert new chunks"]
  shared[("chunks<br/>public · internal")]
  conf[("confidential_chunks<br/>separate index")]
  failed["job failed<br/>no retry"]
  dlq["retries 1-30 s ×3,<br/>then dead-letter queue"]

  file --> queue --> guard
  guard -- no --> failed
  guard -- yes --> parse --> chunk --> embed --> swap
  swap -- "public · internal" --> shared
  swap -- "confidential" --> conf
  embed -. "transient error" .-> dlq
```

## 3. Decision records

Each decision - context, the alternatives considered, the measurement where there was
one, and the consequences - lives in its own file under
[`docs/decisions/`](docs/decisions/README.md), so this document stays readable and the
reasoning stays available.

| # | Decision | Area |
|---|---|---|
| [D-01](docs/decisions/D-01-pgvector-in-the-primary-postgres-instead-of-a-de.md) | pgvector in the primary Postgres instead of a dedicated vector DB | Storage |
| [D-02](docs/decisions/D-02-deterministic-mock-provider-as-a-first-class-ada.md) | Deterministic mock provider as a first-class adapter | Providers |
| [D-03](docs/decisions/D-03-readiness-vs-liveness.md) | Readiness vs liveness | Operations |
| [D-04](docs/decisions/D-04-correlation-ids-accept-caller-input-only-if-log.md) | Correlation IDs accept caller input only if log-safe | Operations |
| [D-05](docs/decisions/D-05-metric-labels-are-bounded.md) | Metric labels are bounded | Operations |
| [D-06](docs/decisions/D-06-flowise-is-a-client-not-the-orchestrator.md) | Flowise is a client, not the orchestrator | Scope |
| [D-07](docs/decisions/D-07-cubejs-not-used.md) | CubeJS not used | Scope |
| [D-08](docs/decisions/D-08-role-bound-jwt-local-issuer-as-a-stand-in-for-th.md) | Role-bound JWT; local issuer as a stand-in for the company IdP | Security |
| [D-10](docs/decisions/D-10-provider-abstraction-and-routing.md) | Provider abstraction and routing | Providers |
| [D-11](docs/decisions/D-11-retry-fallback-and-circuit-breaking-rules.md) | Retry, fallback and circuit-breaking rules | Providers |
| [D-12](docs/decisions/D-12-embeddings-never-fall-back-across-models.md) | Embeddings never fall back across models | Providers |
| [D-13](docs/decisions/D-13-streaming-fallback-only-before-the-first-token-c.md) | Streaming: fallback only before the first token; cancellation propagates | Providers |
| [D-14](docs/decisions/D-14-usage-accounting-per-attempt.md) | Usage accounting per attempt | Providers |
| [D-15](docs/decisions/D-15-data-classification-routing-to-providers-confirm.md) | Data-classification routing to providers (confirmed with the team lead) | Security |
| [D-16](docs/decisions/D-16-three-model-picker-and-switching-within-a-conver.md) | Three-model picker and switching within a conversation | Providers |
| [D-17](docs/decisions/D-17-least-privilege-runtime-database-role-security-f.md) | Least-privilege runtime database role (security finding) | Security |
| [D-20](docs/decisions/D-20-ingestion-parsing-metadata-chunking-embeddings.md) | Ingestion: parsing, metadata, chunking, embeddings | Knowledge |
| [D-21](docs/decisions/D-21-retrieval-hybrid-search-fusion-relevance-gate-to.md) | Retrieval: hybrid search, fusion, relevance gate, top-K | Knowledge |
| [D-22](docs/decisions/D-22-department-isolation-application-filter-row-leve.md) | Department isolation: application filter + row-level security | Security |
| [D-23](docs/decisions/D-23-document-lifecycle-and-re-indexing.md) | Document lifecycle and re-indexing | Knowledge |
| [D-24](docs/decisions/D-24-ingestion-worker-retries-and-dead-letter-queue.md) | Ingestion worker, retries and dead-letter queue | Knowledge |
| [D-25](docs/decisions/D-25-retrieved-content-is-untrusted.md) | Retrieved content is untrusted | Security |
| [D-26](docs/decisions/D-26-agent-orchestration-on-langgraph.md) | Agent orchestration on LangGraph | Agent |
| [D-27](docs/decisions/D-27-upload-an-authorized-user-becomes-a-content-sour.md) | Upload: an authorized user becomes a content source | Security |
| [D-28](docs/decisions/D-28-the-router-runs-on-its-own-fast-model-measured.md) | The router runs on its own fast model (measured) | Agent |
| [D-29](docs/decisions/D-29-tool-contracts-and-what-the-model-may-influence.md) | Tool contracts and what the model may influence | Agent |
| [D-30](docs/decisions/D-30-server-status-field-level-policy.md) | Server status: field-level policy | Security |
| [D-31](docs/decisions/D-31-sensitive-actions-propose-confirm-execute-once.md) | Sensitive actions: propose, confirm, execute once | Security |
| [D-32](docs/decisions/D-32-tamper-evident-audit.md) | Tamper-evident audit | Security |
| [D-40](docs/decisions/D-40-memory-allowlist-not-model-judgement.md) | Memory: allowlist, not model judgement | Memory |

### Confirmed with the team lead (2026-09-23)
| Question | Answer | Effect on the build |
|---|---|---|
| Is `vpn:approve` global or per department? | Only the IT Head grants VPN access for other departments | Matches: `vpn:approve` is a central authority, not department-scoped. In the fixture only U002 holds it, so U002 plays that role; requester and approver must still differ. |
| What may persistent memory store? | "any will do" | Kept the allowlist (the brief requires "selected, permitted facts" and no secrets), now extendable per deployment through `OPSASSIST_MEMORY_EXTRA_KEYS`. |
| May internal/confidential documents go to Claude or similar? | Confidential must not be exposed to external models; use roles and permissions | Implemented as D-15: the context's highest classification is compared against `OPSASSIST_EGRESS_MAX_CLASSIFICATION`. |
| Should RAG enforce permissions at retrieval time? | "suggest your idea" | Our answer: yes, and in two layers - the SQL filter *and* Postgres row-level security under a non-superuser role, with confidential material in a separate table (D-17, D-22). Filtering after retrieval would already have put the text in memory next to the model. |
| Which cloud for the scale proposal? | AWS (used in production today) | The day-6 proposal targets AWS concretely (D-50 note below). |

### Pending decisions (filled on the day they are made)
- D-50 evaluation design: rubric, judge model, control baselines _(day 5)_
- D-60 **scale proposal on AWS** _(day 6)_: ECS/EKS for the API and workers, Aurora
  PostgreSQL with pgvector (or OpenSearch if the vector tier outgrows it), ElastiCache or
  SQS for the queue, S3 for uploaded documents, Secrets Manager + KMS, OIDC through the
  corporate IdP. Confidential traffic must stay inside the VPC, which points at self-hosted
  inference (vLLM on GPU nodes) for that class rather than a managed model API.

## 4. Security model (engineering view)

The cross-team summary is in [the README](README.md#security-and-data-boundaries). At
engineering level, each control is one of these, and each has its own record:

| Control | Where it lives | Record |
|---|---|---|
| Role-bound tokens; permissions read from the database per request | `auth.py` | [D-08](docs/decisions/D-08-role-bound-jwt-local-issuer-as-a-stand-in-for-th.md) |
| Access scope from permissions; SQL filter + Postgres RLS in one transaction | `policy/access.py`, `knowledge/retrieval.py` | [D-22](docs/decisions/D-22-department-isolation-application-filter-row-leve.md) |
| Runtime database role that cannot bypass RLS | migration 0005 | [D-17](docs/decisions/D-17-least-privilege-runtime-database-role-security-f.md) |
| Confidential material in a separate index, on-box models only | migration 0004, gateway | [D-15](docs/decisions/D-15-data-classification-routing-to-providers-confirm.md) |
| Retrieved text as escaped, unauthorized data; validated citations | `rag.py` | [D-25](docs/decisions/D-25-retrieved-content-is-untrusted.md) |
| Typed tool schemas, permission checks, no shell/deploy/SQL tool | `tools/` | [D-29](docs/decisions/D-29-tool-contracts-and-what-the-model-may-influence.md) |
| Two-person approval pinned by an action hash, executed once | `tools/executor.py` | [D-31](docs/decisions/D-31-sensitive-actions-propose-confirm-execute-once.md) |
| Hash-chained, append-only audit written in the action's transaction | `policy/audit.py` | [D-32](docs/decisions/D-32-tamper-evident-audit.md) |
| Upload confined to the uploader's department; metadata untrusted | `knowledge/upload.py` | [D-27](docs/decisions/D-27-upload-an-authorized-user-becomes-a-content-sour.md) |

Run them: `make test-security` (91 tests).

## 5. Evaluation design
Gold sets and scripts live in [`evaluation/`](evaluation/). Today: a 34-case retrieval gold
set with chunker-independent evidence (`retrieval_cases.jsonl`), a chunking comparison
including a Docling reference (`chunking_eval.py`), relevance-gate calibration, retrieval
through the live API, and answer-level citation/fact/abstention scoring (`answer_eval.py`).
Every rate is reported with a 95% Wilson interval. _Day 5:_ the 30+ case suite with an LLM
judge per claim, injection and isolation categories, cost and latency reporting, and a
complex-PDF set that gives Docling's layout model a fair comparison.

## 6. Scale proposal
_Day 6._ 5,000 employees · 1M documents · 100 concurrent requests · GPU cluster, targeting
**AWS** (the production environment in use) - see D-60 above for the shape it will take.

## 7. Known limitations and future work
Maintained in the README: [known limitations](README.md#known-limitations) and
[future improvements](README.md#future-improvements), so one list stays current.
