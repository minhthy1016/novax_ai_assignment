# OpsAssist — AI Operations Assistant

An internal assistant that answers from approved company knowledge and executes controlled
operational tools, with department isolation, explicit approval for sensitive actions,
and an auditable trail for every decision.

> **Build status (day 3 of 6):** AI gateway (Task 1) + **RAG knowledge system** (Task 2):
> PDF/Markdown/text ingestion through a background worker, department isolation enforced in
> SQL and by Postgres row-level security, hybrid retrieval, grounded answers with validated
> citations, and abstention when approved knowledge has no answer.
> See [`docs/traceability.md`](docs/traceability.md) for exactly what is done and how each
> item is verified.

## Quick start

Prerequisites: Docker (with Compose v2), `make`. For running tests on the host: [uv](https://docs.astral.sh/uv/).

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
as fallbacks. Routes and fallback order live in [`config/models.toml`](config/models.toml). With no keys and
no Ollama, the stack still runs and every test passes on the mock provider.

**Credentials policy:** keys are read from environment variables named in the catalog and
never written to tracked files, logs, API responses or model prompts. A provider without a
key is disabled and shown as unavailable in `GET /api/models`.

## API usage

```bash
# 1. Sign in first: every /api call needs a token bound to the user's ID and role
#    (dev/test issuer; stands in for the company IdP). A role change invalidates it.
TOKEN=$(curl -s -X POST localhost:8000/api/auth/dev-token \
  -H 'content-type: application/json' -d '{"user_id":"U001"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# 2. Chat (default route: NIM gpt-oss-20b, falls back to local Ollama)
curl -s localhost:8000/api/chat -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"What is a connection pool?"}' | jq '{content, model, fallback_used, usage}'

# 2b. Switch model mid-conversation (history carries over), or without a message:
curl -s localhost:8000/api/chat -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"Now answer briefly","model":"ollama/llama3.2-3b","conversation_id":"<id>"}'
curl -s -X PATCH localhost:8000/api/conversations/<id> -H "$AUTH" \
  -H 'content-type: application/json' -d '{"model":"claude/sonnet-4.5"}'

# 3. Stream (server-sent events: meta, model, delta..., done | error)
curl -N localhost:8000/api/chat/stream -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"Count to five","model":"chat-local"}'

# 4. Provider failure demo: first target always fails, gateway falls back
curl -s localhost:8000/api/chat -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"hi","model":"demo-failover"}' | jq '{model, fallback_used, attempts}'

# 5. Embeddings, model catalog, conversation history
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
  answer cites sources like `Production Deployment Procedure (KB-ENG-001 v1, ¶1–4)`. If
  nothing relevant is found the assistant says so, without calling a model.
- `POST /api/search` runs the same scoped retrieval and returns ranked chunks with scores.
- Confidential documents go into a separate index and are answered by local models only.

```bash
curl -s localhost:8000/api/chat -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"When may we deploy to production?"}' | jq '{content, citations, abstained}'
curl -s localhost:8000/api/search -H "$AUTH" -H 'content-type: application/json' \
  -d '{"query":"payment incident root cause"}' | jq '.hits[] | {ref, similarity}'
```

## Tests

```bash
make install            # local venv via uv
make lint               # ruff + mypy (strict)
make test               # unit tests, no services needed
make test-integration   # against the running stack (make up first)
```

## Repository layout

```
src/opsassist/     application code (API, providers, retrieval, tools, policy)
migrations/        Alembic migrations (one per feature, in build order)
sample_data/       fictional seed data from the brief
tests/             unit/ (no services) and integration/ (compose stack)
evaluation/        eval dataset, runner, reports
docs/              traceability matrix, runbooks, walkthrough script
architecture.md    architecture, decisions with alternatives, security model, scale proposal
```

## Operations

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness — process is up; never checks dependencies |
| `GET /readyz` | Readiness — Postgres (pgvector + migrations) and Redis; 503 with detail on failure |
| `GET /metrics` | Prometheus metrics |
| `GET /docs` | OpenAPI UI (dev/test environments only) |

Every response carries `X-Request-ID`; every log line is JSON and includes it.

## Sections to come
API usage · provider configuration · credentials policy · security model · evaluation ·
known limitations — added as each component lands.
