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
        type-check format shell logs clean nuke profile-computor play-pool-cache calibrate

# Default target — show help.
##
## Cross-platform: uses Python `print()` instead of multiple `@echo` lines.
## Windows cmd.exe prints the literal "" instead of a blank line for
## `echo ""`, so the bash form was visually broken under Windows GNU Make.
help:
	@py -c "print('''\nMLB Baseball Simulation Platform\n=================================\n\n  make dev               Build + start all services (foreground)\n  make down              Stop and remove containers + networks\n  make build             Force rebuild Docker images\n  make migrate           Apply all Alembic migrations (requires db healthy)\n\n  make test              Run full test suite (unit + integration)\n  make test-unit         Unit tests only (no Docker deps)\n  make test-regression   Regression gate (golden-file engine drift detection)\n  make test-integration  Integration tests (spins up testcontainers)\n\n  make lint              Ruff lint check\n  make format            Ruff auto-format\n  make type-check        Mypy static type analysis\n\n  make shell             Open a bash shell inside the app container\n  make logs              Tail logs for all services\n  make clean             Remove Python artifacts (__pycache__, .coverage, etc.)\n  make nuke              Full teardown: containers + volumes (destructive!)\n''')"

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
##
## Cross-platform note: BASEBALL_DB_DSN is read from .env by the migrate
## container (env_file in docker-compose.yml).  alembic env.py auto-coerces
## the postgresql:// scheme to postgresql+psycopg2:// for SQLAlchemy.
## This avoids the bash $${VAR:-default} substitution that doesn't expand
## under cmd.exe on Windows GNU Make.
migrate: _require_env_file
	@echo "Applying Alembic migrations..."
	docker compose run --rm app alembic upgrade head
	@echo "Migrations complete."

# ---------------------------------------------------------------------------
# Nightly batch jobs
# ---------------------------------------------------------------------------
# These run in order each night: the profile computor rebuilds the DuckDB
# pools (sim.pitch_pool / sim.outcome_pool), then the play-pool cache
# serializer (SIM-301) materializes FAISS tiles from those pools.  The cache
# build MUST run AFTER the profile computor.  See the crontab note at the
# bottom of pipeline/batch/play_pool_cache.py.

## Nightly: rebuild DuckDB player profiles + sim pools from Postgres.
profile-computor: _require_env_file
	docker compose run --rm app python -m pipeline.batch.player_profile_computor

## Nightly (SIM-301): materialize play-pool FAISS tiles from the DuckDB pools.
## Idempotent — only stale or missing tiles are rebuilt.  Runs AFTER
## profile-computor.  Pass FLAGS="--no-recency-boost --seasons 2024" to tune.
play-pool-cache:
	docker compose run --rm app python -m pipeline.batch.play_pool_cache $(FLAGS)

## Nightly (SIM-406): fit + persist the CalibrationReport over the DuckDB profiles.
## Runs AFTER profile-computor (it reads the derived.* season-metrics tables) and
## writes CALIBRATION_REPORT_PATH (/data/calibration.json), which the API loads at
## boot to calibrate EVERY similarity engine + the win-prob map.  Default fits all
## seasons incl. the arsenal W2 anchor; pass FLAGS="--no-arsenal" to skip the slow
## pitcher-engine W2 sample, or FLAGS="--seasons 2024 --validate" to subset/verify.
calibrate: _require_env_file
	docker compose run --rm app python scripts/fit_calibration.py $(FLAGS)

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
##
## BASEBALL_DB_DSN and REDIS_URL come from .env via env_file (docker-compose.yml).
## Cross-platform: no bash $${VAR:-default} substitution that breaks on cmd.exe.
test: _require_env_file
	docker compose run --rm app \
	  pytest tests/ -v --tb=short --timeout=60 \
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
	       --python-version 3.13

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

## Remove Python build artifacts from the working directory.
##
## Cross-platform: uses Python instead of `find` + `rm -rf` so it works
## under cmd.exe (Windows GNU Make has no `find` and no `rm`).  Walks the
## tree exactly once; ignores PermissionError / FileNotFoundError so a
## locked .pyc or already-deleted dir doesn't fail the recipe.
clean:
	@py -c "import os, shutil, pathlib; \
	root = pathlib.Path('.'); \
	[shutil.rmtree(p, ignore_errors=True) for p in root.rglob('__pycache__') if p.is_dir()]; \
	[p.unlink(missing_ok=True) for p in root.rglob('*.pyc') if p.is_file()]; \
	[shutil.rmtree(d, ignore_errors=True) for d in ('htmlcov', '.pytest_cache', '.ruff_cache', '.mypy_cache')]; \
	[pathlib.Path(f).unlink(missing_ok=True) for f in ('.coverage', 'coverage.xml')]; \
	print('Clean done.')"

## Full teardown — stops containers AND removes volumes (drops PostgreSQL data!)
## Use with caution. You will need to run 'make migrate' again afterward.
##
## The Y/N prompt is a Python one-liner so the recipe works under both bash
## (Linux / macOS / WSL / Git Bash) AND cmd.exe (Windows GNU Make).  The
## previous bash ``read -p ... case ... esac`` form failed on Windows with
## "'read' is not recognized as an internal or external command" because
## cmd.exe has no ``read`` builtin.  Python (already a project prerequisite)
## provides input() that behaves identically across all shells.
##
## Non-interactive override: ``make nuke NUKE_CONFIRM=yes`` skips the prompt,
## useful for CI / scripted teardowns.
nuke:
	@echo "WARNING: This will delete all Docker volumes including the PostgreSQL database."
	@py -c "import sys, os; ans = os.environ.get('NUKE_CONFIRM') or input('Are you sure? [y/N] '); sys.exit(0 if ans.strip().lower() in ('y','yes') else 1)" \
	  && docker compose down -v && echo "Done." \
	  || echo "Aborted."

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

## Ensure .env exists before any command that reads it
##
## Implemented as a Python one-liner so the recipe works under both bash
## (Linux / macOS / WSL / Git Bash) and cmd.exe (Windows GNU Make).  The
## previous `if [ ! -f .env ]; then ...` form failed on Windows because
## cmd.exe interprets `!` as delayed-expansion and exited 255 with the
## error "! was unexpected at this time."  Python is already a project
## prerequisite (see requirements.txt + Dockerfile), so introducing it
## here adds no new dependency.
_require_env_file:
	@py -c "import os, sys; os.path.isfile('.env') or print('\nERROR: .env file not found.\n  Run: cp .env.example .env\n  Then edit .env with your database credentials.\n', file=sys.stderr) or sys.exit(1)"
