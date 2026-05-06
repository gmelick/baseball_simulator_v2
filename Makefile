# =============================================================================
# MLB Baseball Simulation Platform — Makefile
#
# Prerequisites: Docker Desktop (or Docker Engine + Compose plugin)
# All commands are self-contained — no local Python install required.
#
# Acceptance gate (SIM-145):
#   git clone <repo> && cp .env.example .env && make dev
#   make migrate   (in separate terminal once db is healthy)
#   make test
# =============================================================================

.PHONY: help dev down build migrate test test-unit test-integration test-regression lint \
        type-check format shell logs clean nuke

# Default target — show help
help:
	@echo ""
	@echo "MLB Baseball Simulation Platform"
	@echo "================================="
	@echo ""
	@echo "  make dev               Build + start all services (foreground)"
	@echo "  make down              Stop and remove containers + networks"
	@echo "  make build             Force rebuild Docker images"
	@echo "  make migrate           Apply all Alembic migrations (requires db healthy)"
	@echo ""
	@echo "  make test              Run full test suite (unit + integration)"
	@echo "  make test-unit         Unit tests only (no Docker deps)"
	@echo "  make test-regression   Regression gate (golden-file engine drift detection)"
	@echo "  make test-integration  Integration tests (spins up testcontainers)"
	@echo ""
	@echo "  make lint              Ruff lint check"
	@echo "  make format            Ruff auto-format"
	@echo "  make type-check        Mypy static type analysis"
	@echo ""
	@echo "  make shell             Open a bash shell inside the app container"
	@echo "  make logs              Tail logs for all services"
	@echo "  make clean             Remove Python artifacts (__pycache__, .coverage, etc.)"
	@echo "  make nuke              Full teardown: containers + volumes (destructive!)"
	@echo ""

# ---------------------------------------------------------------------------
# Docker Compose shortcuts
# ---------------------------------------------------------------------------

## Start all services (db, redis, app) — foreground with log streaming
dev: _require_env_file
	docker compose up --build

## Start detached (background)
dev-bg: _require_env_file
	docker compose up --build -d
	@echo "Services started. Run 'make logs' to follow logs."
	@echo "Run 'make migrate' to apply database migrations."

## Stop all services, remove containers and networks (volumes preserved)
down:
	docker compose down

## Force rebuild all images (bypasses layer cache)
build:
	docker compose build --no-cache

## Apply all Alembic migrations to the PostgreSQL database.
## Uses the 'migrate' one-shot service defined in docker-compose.yml.
migrate: _require_env_file
	@echo "Applying Alembic migrations..."
	docker compose run --rm \
	  -e BASEBALL_DB_DSN=postgresql+psycopg2://$${POSTGRES_USER:-baseball_user}:$${POSTGRES_PASSWORD:-baseball_pass}@db:5432/$${POSTGRES_DB:-baseball_sim} \
	  app alembic upgrade head
	@echo "Migrations complete."

## Tail logs for all services (Ctrl-C to exit)
logs:
	docker compose logs -f

## Open a bash shell inside the running app container
shell:
	docker compose exec app bash

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

## Run the full test suite inside Docker (unit + integration)
test: _require_env_file
	docker compose run --rm \
	  -e BASEBALL_DB_DSN=postgresql+psycopg2://$${POSTGRES_USER:-baseball_user}:$${POSTGRES_PASSWORD:-baseball_pass}@db:5432/$${POSTGRES_DB:-baseball_sim} \
	  -e REDIS_URL=redis://redis:6379/0 \
	  app pytest tests/ -v --tb=short --timeout=60 \
	       --cov=similarity --cov=pipeline --cov=api \
	       --cov-report=term-missing --cov-report=xml:coverage.xml

## Unit tests only — no live DB or Redis required, fast feedback loop
## Can run locally without Docker if deps are installed in a venv.
test-unit:
	docker compose run --rm app \
	  pytest tests/unit/ -v --tb=short --timeout=30 \
	         --cov=similarity --cov=pipeline \
	         --cov-report=term-missing

## Integration tests — testcontainers spins up ephemeral PostgreSQL + Redis
## Mirrors the CI weekly integration job.
test-regression:
	docker compose run --rm app \
	  pytest tests/regression/ -v --tb=short --timeout=60 \
	         -m regression

## Integration tests — testcontainers spins up ephemeral PostgreSQL + Redis
## Mirrors the CI weekly integration job.
test-integration: _require_env_file
	docker compose run --rm app \
	  pytest tests/integration/ -v --tb=short --timeout=120 \
	         -m integration

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

## Ruff: lint all Python source (same config as CI Job 2)
lint:
	docker compose run --rm app ruff check . --output-format=full

## Ruff: auto-format source files
format:
	docker compose run --rm app ruff format .

## Mypy: static type checking (same config as CI Job 2)
type-check:
	docker compose run --rm app \
	  mypy similarity/ pipeline/ api/ --ignore-missing-imports \
	       --python-version 3.11

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

## Remove Python build artifacts from the working directory
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .coverage coverage.xml htmlcov/ .pytest_cache/ .ruff_cache/ .mypy_cache/
	@echo "Clean done."

## Full teardown — stops containers AND removes volumes (drops PostgreSQL data!)
## Use with caution. You will need to run 'make migrate' again afterward.
nuke:
	@echo "WARNING: This will delete all Docker volumes including the PostgreSQL database."
	@read -p "Are you sure? [y/N] " yn; \
	  case $$yn in [Yy]*) docker compose down -v; echo "Done.";; \
	  *) echo "Aborted.";; esac

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

## Ensure .env exists before any command that reads it
_require_env_file:
	@if [ ! -f .env ]; then \
	  echo ""; \
	  echo "ERROR: .env file not found."; \
	  echo "  Run: cp .env.example .env"; \
	  echo "  Then edit .env with your database credentials."; \
	  echo ""; \
	  exit 1; \
	fi
