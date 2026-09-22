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

.PHONY: help install up down reset migrate seed lint fmt test test-integration logs
