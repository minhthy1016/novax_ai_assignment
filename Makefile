.DEFAULT_GOAL := help
COMPOSE := docker compose

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Install Python deps locally (uv)
	uv sync

up: ## Build and start the stack (migrations + seed run automatically)
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

reset: ## Stop the stack and delete all data volumes
	$(COMPOSE) down -v

migrate: ## Apply migrations from the host
	uv run alembic upgrade head

seed: ## Load fixtures from the host
	uv run python -m opsassist.seed

ingest: ## Queue all sample knowledge for the worker to (re)index
	$(COMPOSE) exec api python -m opsassist.knowledge.ingest

ingest-inline: ## Index sample knowledge in-process (no worker)
	$(COMPOSE) exec api python -m opsassist.knowledge.ingest --inline

jobs: ## Show recent ingestion jobs
	$(COMPOSE) exec postgres psql -U opsassist -c "SELECT source_path, status, attempts, doc_key, version, detail, updated_at FROM ingestion_jobs ORDER BY updated_at DESC LIMIT 20"

lint: ## Ruff + mypy
	uv run ruff check src tests migrations
	uv run ruff format --check src tests migrations
	uv run mypy src

fmt: ## Auto-format
	uv run ruff check --fix src tests migrations
	uv run ruff format src tests migrations

test: ## Unit tests (no services needed)
	uv run pytest -m "not integration"

test-integration: ## Integration tests (needs `make up`)
	uv run pytest -m integration

logs: ## Tail API logs
	$(COMPOSE) logs -f api

.PHONY: help install up down reset migrate seed ingest ingest-inline jobs lint fmt test test-integration logs
