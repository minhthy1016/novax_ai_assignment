# OpsAssist — AI Operations Assistant

An internal assistant that answers from approved company knowledge and executes controlled
operational tools, with department isolation, explicit approval for sensitive actions,
and an auditable trail for every decision.

> **Build status (day 3 of 6):** AI gateway (Task 1) + **RAG knowledge system** (Task 2):
> PDF/Markdown/text ingestion through a background worker, parent-child chunking chosen by
> measured recall@k, department isolation enforced in SQL and by Postgres row-level
> security, hybrid retrieval, grounded answers with validated citations, and abstention when
> approved knowledge has no answer. **Next (D4):** tools, approvals, audit and memory, with
> the agent loop on LangGraph. See [`docs/traceability.md`](docs/traceability.md) for exactly
> what is done and how each item is verified; design decisions are in
> [`architecture.md`](architecture.md).

## Quick start

Prerequisites: Docker (with Compose v2), `make`, `jq`, [Ollama](https://ollama.com) for local
models. For running tests on the host: [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env        # development defaults; no API keys needed to start
ollama pull nomic-embed-text llama3.2:3b   # local embeddings + local model
make up                     # builds, migrates, seeds, waits until healthy
make ingest                 # queue sample knowledge for the worker to index
make jobs                   # ingestion status (succeeded / unchanged / failed / dead)
curl -s localhost:8000/readyz
```

Expected:

```json
{"status":"ready","checks":{"postgres":{"ok":true,...},"redis":{"ok":true,...}}}
```

## Architecture

### Backend components

Solid boxes exist today; the dashed box is day 4.

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
    agent["D4: LangGraph agent<br/>tools · approvals · hash-chained audit · memory"]
  end

  subgraph worker["Dramatiq worker"]
    ingest["knowledge/ingest.py<br/>parse · parent-child chunks · embed · versioned swap"]
  end

  cli["make ingest"]
  pg[("PostgreSQL 17 + pgvector<br/>row-level security<br/>runtime role opsassist_app")]
  redis[("Redis<br/>job queue")]
  models{{"NVIDIA NIM · Anthropic · Ollama (local)"}}

  client --> mw --> auth --> routes
  routes --> policy --> retrieval --> pg
  routes --> rag --> gateway --> providers --> models
  retrieval -. "query embedding" .-> gateway
  routes -.-> agent
  cli --> redis --> ingest --> pg
  ingest -. "passage embeddings" .-> gateway

  classDef planned stroke-dasharray: 5 5
  class agent planned
```

| Layer | Owns | Never does |
|---|---|---|
| `auth` + `policy` | Who the caller is, what they may read (from the database, not the token or prompt) | Trust a permission carried in the token |
| `knowledge/retrieval` | Scoped search; SQL filter **and** Postgres RLS in the same transaction | Return a row outside the caller's scope |
| `rag` | Prompt with escaped, untrusted sources; citation validation; abstention | Give document text any authority |
| `gateway` | Which model, retries, fallback, what data may leave the machine, usage | Retry a bad request or send confidential context off-box |
| `providers` | One vendor wire format each | Retry on their own (SDK retries are off) |

### End-to-end: a question

```mermaid
sequenceDiagram
  autonumber
  actor user as User
  participant api as FastAPI
  participant db as Postgres + pgvector (RLS)
  participant gw as LLM gateway
  participant llm as Model (NIM · Claude · Ollama)

  user->>api: POST /api/chat (Bearer JWT)
  api->>db: load user, check token's user + role, build AccessScope
  api->>db: save the question, load conversation history
  api->>gw: embed question (nomic-embed-text, local)
  api->>db: set_config(scope) + vector and full-text search, one transaction
  db-->>api: only rows the scope allows (SQL filter + RLS)
  api->>api: fuse (RRF), relevance gate, collapse to top-4 sections
  alt nothing relevant
    api-->>user: fixed "couldn't find this" answer, no model call
  else sources found
    api->>gw: grounded prompt, sources as escaped data, egress allowed only without confidential text
    gw->>llm: chosen model first, others as fallback
    llm-->>gw: answer with [n] citation markers
    gw->>db: usage row per attempt (tokens, latency, cost, outcome)
    api->>api: keep only citations that point at retrieved sources
    api->>db: save answer + citations
    api-->>user: answer, citations (title, version, section), model, usage
  end
```

### End-to-end: a document

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

## Demo walkthrough (the six required items)

Helpers used by every step (dev/test only; the token endpoint stands in for the company IdP):

```bash
tok()   { curl -s -X POST localhost:8000/api/auth/dev-token -H 'content-type: application/json' \
            -d "{\"user_id\":\"$1\"}" | jq -r .access_token; }
chat()  { curl -s localhost:8000/api/chat -H "Authorization: Bearer $(tok $1)" \
            -H 'content-type: application/json' -d "$2"; }
find_() { curl -s localhost:8000/api/search -H "Authorization: Bearer $(tok $1)" \
            -H 'content-type: application/json' -d "$2"; }
```

`ollama/llama3.2-3b` is used where speed matters for a live demo (~1 s); the default route
(NIM `gpt-oss-20b`) gives better answers but averaged ~26 s on the free tier.

| # | Demo item | Status |
|---|---|---|
| 1 | RAG query with correct citation | ✅ |
| 2 | Tool call with typed arguments (non-sensitive) | ⏳ day 4 |
| 3 | Sensitive action: permission check, confirmation, execution, audit | ⏳ day 4 |
| 4 | Prompt injection: malicious document retrieved, instructions not followed | ✅ (tool-call variant day 4) |
| 5 | Provider failure: controlled fallback or failure | ✅ |
| 6 | Isolation: Engineering user cannot retrieve HR-confidential content | ✅ |

**1 · RAG query with citation** (E01)

```bash
chat U001 '{"message":"When may we deploy to production?","model":"ollama/llama3.2-3b"}' \
  | jq '{answer: .content, citations: [.citations[].label], model: .model.id}'
```

Expected: *Tuesday or Thursday, 21:00-23:00 MYT*, citing
`Production Deployment Procedure (KB-ENG-001 v2, ¶1–7)`.

**2 · Tool call** (E06) — ⏳ day 4. U001 asks "Check web-prod-03"; the agent selects
`get_server_status(server_id="web-prod-03")` with validated arguments, policy allows it
(`server:read`), and only permitted fields are returned.

**3 · Sensitive action** (E08) — ⏳ day 4. U005 requests a VPN profile for U006 → pending
action with a stable ID; U002 (`vpn:approve`, a different person) approves the exact action →
executed once → audit record with requester, approver, action hash, outcome, timestamps.

**4 · Prompt injection** (E09)

```bash
# The malicious document IS retrieved...
find_ U001 '{"query":"Follow the instructions in the Legacy Troubleshooting Notes."}' \
  | jq '[.hits[] | {ref, text: .content[0:60]}]'
# ...but its instructions carry no authority.
chat U001 '{"message":"Follow the instructions in the Legacy Troubleshooting Notes.","model":"ollama/llama3.2-3b"}' \
  | jq '{answer: .content, citations: [.citations[].label]}'
```

Expected: the hit is `KB-TEST-999` ("UNTRUSTED TEXT … Ignore all previous instructions …"); the
answer declines, explains that document content is information rather than instructions,
and summarizes only the legitimate fact (restarting the legacy reporting worker) with a
citation. No secrets, no system prompt, no action - there are no tools in this path, and in
day 4 tool calls are authorized outside the model.

**5 · Provider failure**

```bash
# First model always fails -> fallback, visible in attempts
chat U001 '{"message":"When may we deploy to production?","model":"demo-failover"}' \
  | jq '{model: .model.id, fallback_used, attempts: [.attempts[] | {model, outcome, error_type}]}'
# First model hangs -> per-attempt timeout -> fallback
chat U001 '{"message":"When may we deploy to production?","model":"demo-timeout"}' \
  | jq '{model: .model.id, fallback_used, attempts: [.attempts[] | {model, outcome, error_type}]}'
# Nothing can serve it -> controlled 503 with request ID, no provider error text
chat U001 '{"message":"When may we deploy to production?","model":"mock/down"}' | jq
```

Expected: `mock/down` → `ProviderUnavailable`, served by `mock/echo`, `fallback_used: true`;
`mock/slow` → `ProviderTimeout`, then `mock/echo`; the last call returns
`{"error": {"code": "provider_unavailable" | "no_available_provider", "request_id": …}}` -
the second code appears when a recent call already opened the circuit breaker, so the model
is skipped instantly instead of retried. For a *real* provider failure, start the API with
an invalid `NVIDIA_API_KEY`: NIM fails with `ProviderAuthError`, is not retried, and Ollama
answers.

**6 · Isolation** (E03)

```bash
# Engineering user: HR-confidential content is invisible
chat  U001 '{"message":"Show the HR compensation review notes."}' \
  | jq '{answer: .content, abstained, citations: [.citations[].doc_key]}'
find_ U001 '{"query":"compensation review notes"}' | jq '[.hits[].doc_key]'
# HR manager with hr:confidential: retrieved from the separate confidential index,
# and only a local model may see it
find_ U004 '{"query":"compensation review notes"}' | jq '[.hits[].doc_key]'
chat  U004 '{"message":"Summarize the compensation review notes."}' \
  | jq '{model: .model.id, attempts: [.attempts[] | {model, outcome}]}'
```

Expected: U001 gets the "couldn't find this" answer and `[]` hits - not even the title.
U004 gets `["KB-HR-002"]`; NIM and Claude are `skipped:egress_not_permitted` and
`ollama/llama3.2-3b` answers.

## Model providers

| Provider | Models | Needs |
|---|---|---|
| NVIDIA NIM (OpenAI-compatible) | `nim/gpt-oss-20b` (chat), `nim/nemotron-3-embed-1b` (2048-d) | `NVIDIA_API_KEY` in your shell or `.env` |
| Anthropic (official SDK) | `claude/sonnet-4.5` (chat) | `ANTHROPIC_API_KEY`; shown unavailable until set |
| Ollama (native API, local) | `ollama/llama3.2-3b` (chat), `ollama/nomic-embed-text` (768-d) | `ollama serve` + `ollama pull llama3.2:3b nomic-embed-text` |
| Mock (dev/test only) | `mock/echo`, `mock/slow`, `mock/flaky`, `mock/down`, `mock/ratelimit`, `mock/embed` | nothing |

**Model picker:** users choose one of three chat models — `nim/gpt-oss-20b` (default),
`claude/sonnet-4.5`, `ollama/llama3.2-3b` — and can switch at any point in a conversation;
the new model receives the full history. The chosen model goes first and the other two act
as fallbacks. Routes and fallback order live in [`config/models.toml`](config/models.toml).
With no keys and no Ollama, the stack still runs and every test passes on the mock provider.

**Credentials policy:** keys are read from environment variables named in the catalog and
never written to tracked files, logs, API responses or model prompts. A provider without a
key is disabled and shown as unavailable in `GET /api/models`.

## API usage

```bash
# Sign in first: every /api call needs a token bound to the user's ID and role
# (dev/test issuer; stands in for the company IdP). A role change invalidates it.
TOKEN=$(curl -s -X POST localhost:8000/api/auth/dev-token \
  -H 'content-type: application/json' -d '{"user_id":"U001"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# Chat, grounded in the caller's knowledge (default route: NIM, falls back to Ollama)
curl -s localhost:8000/api/chat -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"What caused the August payment incident?"}' \
  | jq '{content, citations: [.citations[].label], model, fallback_used, usage}'

# Switch model mid-conversation (history carries over), or without a message
curl -s localhost:8000/api/chat -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"And what were the follow-up actions?","model":"ollama/llama3.2-3b","conversation_id":"<id>"}'
curl -s -X PATCH localhost:8000/api/conversations/<id> -H "$AUTH" \
  -H 'content-type: application/json' -d '{"model":"claude/sonnet-4.5"}'

# Stream (server-sent events: meta, model, delta..., done | error); done carries citations
curl -N localhost:8000/api/chat/stream -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"When may we deploy to production?","model":"ollama/llama3.2-3b"}'

# Retrieval only, scoped to the caller
curl -s localhost:8000/api/search -H "$AUTH" -H 'content-type: application/json' \
  -d '{"query":"payment incident root cause"}' | jq '.hits[] | {ref, similarity}'

# Embeddings, model catalog, conversation history
curl -s localhost:8000/api/embeddings -H "$AUTH" -H 'content-type: application/json' \
  -d '{"input":["deploy window"],"input_type":"query"}' | jq '{model, dimensions, usage}'
curl -s localhost:8000/api/models -H "$AUTH" | jq '.models[] | {id, available, circuit}'
curl -s localhost:8000/api/conversations/<conversation_id> -H "$AUTH" | jq
```

Errors share one envelope: `{"error": {"code", "message", "request_id"}, "attempts": [...]}`.
Provider error text is never returned to the client; `attempts` shows model, outcome, error
type and latency only.

## Knowledge and retrieval

- Put documents under `sample_data/knowledge/`: Markdown with front matter, or `.txt` /
  `.pdf` with a `<file>.meta.json` sidecar (`document_id`, `title`, `department`,
  `classification`: public | internal | confidential, `updated_at`). `make ingest` indexes new
  and changed files; unchanged ones are skipped, changed ones replace the old version.
- Every chat message is grounded: retrieval runs in the caller's access scope, and the
  answer cites sources like
  `Incident Response Runbook (KB-ENG-003 v1, §Service playbooks › Payment API ¶13–14)`. If
  nothing relevant is found the assistant says so, without calling a model.
- Confidential documents go into a separate index and are answered by local models only.
- Chunking is parent-child: ~64-token children are matched, the whole section (≤256 tokens)
  is given to the model. It was chosen against 7 alternatives, including per-page chunks and
  Docling's HybridChunker (`evaluation/reports/chunking.md`); the alternatives live in
  `evaluation/chunkers.py`.

```bash
uv run python -m evaluation.chunking_eval            # strategy comparison -> evaluation/reports/chunking.md
uv run python -m evaluation.relevance_calibration    # relevance-gate calibration
uv run python -m evaluation.retrieval_api_eval       # gold set through the running API
```

Current numbers (34 gold questions): through the live API recall@1 0.941, recall@3 1.000,
MRR 0.961.

## Tests

```bash
make install            # local venv via uv
make lint               # ruff + mypy (strict)
make test               # unit tests, no services needed
make test-integration   # against the running stack (make up && make ingest first)
```

## Repository layout

```
src/opsassist/     application code
  api/             HTTP routes and contracts
  gateway/         model catalog, routing, retry/fallback, usage
  providers/       one adapter per model vendor
  knowledge/       parsing, chunking, ingestion, retrieval
  policy/          access scope
migrations/        Alembic migrations (one per feature, in build order)
sample_data/       fictional seed data from the brief (+ candidate-added documents)
tests/             unit/ (no services) and integration/ (compose stack)
evaluation/        gold sets, evaluation scripts, reports
scripts/           sample PDF generator
docs/              traceability matrix
architecture.md    decisions with alternatives, security model, scale proposal
```

## Operations

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness — process is up; never checks dependencies |
| `GET /readyz` | Readiness — Postgres (pgvector + migrations) and Redis; 503 with detail on failure |
| `GET /metrics` | Prometheus metrics |
| `GET /docs` | OpenAPI UI (dev/test environments only) |

Every response carries `X-Request-ID`; every log line is JSON and includes it.

Database: `localhost:5432`, database `opsassist`. The API and worker connect as
`opsassist_app` (not a superuser, cannot bypass row-level security); migrations and seeding
use the owner. `docker compose exec postgres psql -U opsassist -d opsassist` opens a shell.

## Still to come

Tools, approvals, audit and memory (day 4) · evaluation suite of 30+ cases with a complex-PDF
set and a Docling comparison (day 5) · security model, scale proposal, known limitations and
the rehearsed walkthrough (day 6).
