# OpsAssist — AI Operations Assistant

An internal assistant that answers from approved company knowledge and executes controlled
operational tools, with department isolation, explicit approval for sensitive actions,
and an auditable trail for every decision.

> **Build status (day 2 of 6):** foundation + **AI gateway** (Task 1): chat, streaming,
> embeddings, model catalog, conversations; NVIDIA NIM + native Ollama + deterministic mock
> adapters with retry, fallback, circuit breaking, cancellation and per-attempt usage.
> See [`docs/traceability.md`](docs/traceability.md) for exactly what is done and how each
> item is verified.

## Quick start

Prerequisites: Docker (with Compose v2), `make`. For running tests on the host: [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env        # development defaults; no API keys needed to start
make up                     # builds, migrates, seeds, waits until healthy
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
| Ollama (native API, local) | `ollama/llama3.2-3b` (chat), `ollama/nomic-embed-text` (768-d) | `ollama serve` + `ollama pull llama3.2:3b nomic-embed-text` |
| Mock (dev/test only) | `mock/echo`, `mock/slow`, `mock/flaky`, `mock/down`, `mock/ratelimit`, `mock/embed` | nothing |

Routes and fallback order live in [`config/models.toml`](config/models.toml). With no keys and
no Ollama, the stack still runs and every test passes on the mock provider.

**Credentials policy:** keys are read from environment variables named in the catalog and
never written to tracked files, logs, API responses or model prompts. A provider without a
key is disabled and shown as unavailable in `GET /api/models`.

## API usage

```bash
# 1. Get a token for a seeded user (dev/test only; stands in for the company IdP)
TOKEN=$(curl -s -X POST localhost:8000/api/auth/dev-token \
  -H 'content-type: application/json' -d '{"user_id":"U001"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# 2. Chat (default route: NIM gpt-oss-20b, falls back to local Ollama)
curl -s localhost:8000/api/chat -H "$AUTH" -H 'content-type: application/json' \
  -d '{"message":"What is a connection pool?"}' | jq '{content, model, fallback_used, usage}'

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
