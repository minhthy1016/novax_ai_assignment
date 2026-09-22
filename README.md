# OpsAssist — AI Operations Assistant

An internal assistant that answers from approved company knowledge and executes controlled
operational tools, with department isolation, explicit approval for sensitive actions,
and an auditable trail for every decision.

> **Build status (day 1 of 6):** foundation only — containerized API, Postgres + pgvector,
> Redis, migrations, seed data, health/readiness, JSON logs with correlation IDs, metrics,
> CI. See [`docs/traceability.md`](docs/traceability.md) for exactly what is done and how
> each item is verified.

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
