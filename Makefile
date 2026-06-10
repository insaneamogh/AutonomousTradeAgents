# ─────────────────────────────────────────────────────────────────────
# Cross-stack developer commands. The blessed entry points.
# ─────────────────────────────────────────────────────────────────────

.PHONY: help install dev test lint typecheck infra-up infra-down migrate dev-api dev-api-legacy dev-api-postgres clean

help:
	@echo "  install            Install JS + Python deps"
	@echo "  dev                Start everything (mobile + api + agents) in parallel"
	@echo "  dev-mobile         Expo dev server"
	@echo "  dev-api            FastAPI dev server. Auth REQUIRED (DEV_AUTH_BYPASS=0)."
	@echo "  dev-api-legacy     FastAPI dev server with DEV_AUTH_BYPASS=1 (pre-mobile-auth)."
	@echo "  dev-api-postgres   FastAPI dev server with PostgresStore (needs infra-up + migrate)"
	@echo "  dev-agents         LangGraph agent runtime"
	@echo "  infra-up           Start local Postgres + TimescaleDB + Redis"
	@echo "  infra-down         Stop local infra"
	@echo "  migrate            Apply Alembic migrations to the local Postgres"
	@echo "  test               Run all tests (JS + Python)"
	@echo "  lint               Run linters (ruff + eslint)"
	@echo "  typecheck          Run typecheckers (mypy + tsc)"
	@echo "  clean              Wipe node_modules, .venv, .turbo, dist"

install:
	pnpm install
	uv sync --all-packages

dev:
	pnpm dev

dev-mobile:
	pnpm --filter @app/mobile dev

dev-api:
	# Mobile auth has shipped (Phase 3). The default dev runner now requires
	# a real Bearer token on every protected route. Use ``dev-api-legacy`` if
	# you need the pre-mobile-auth behavior (legacy demo / smoke).
	DEV_AUTH_BYPASS=0 uv run --package api uvicorn app.main:app --reload --port 8000

dev-api-legacy:
	# Pre-Phase-3 behavior: routes resolve to the fixture user when no
	# Authorization header is present. Useful for the old mobile demo + for
	# any tooling that hasn't been updated to use the auth flow yet.
	DEV_AUTH_BYPASS=1 uv run --package api uvicorn app.main:app --reload --port 8000

dev-api-postgres:
	USE_POSTGRES=1 DEV_AUTH_BYPASS=0 uv run --package api uvicorn app.main:app --reload --port 8000

migrate:
	uv run alembic -c infra/migrations/alembic.ini upgrade head

dev-agents:
	uv run --package agents python -m trading_agents.runtime

infra-up:
	docker compose -f infra/docker-compose.yml up -d

infra-down:
	docker compose -f infra/docker-compose.yml down

test:
	pnpm test
	uv run pytest

lint:
	pnpm lint
	uv run ruff check .

typecheck:
	pnpm typecheck
	uv run mypy apps packages

clean:
	pnpm clean || true
	rm -rf node_modules .turbo .venv
	find . -type d -name __pycache__ -exec rm -rf {} +
