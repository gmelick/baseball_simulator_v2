# =============================================================================
# MLB Baseball Simulation Platform — Makefile
#
# Prerequisites: Docker Desktop (or Docker Engine + Compose plugin)
# All commands are self-contained — no local Python install required.
#
# Acceptance gate (SIM-145):
#   git clone <repo> && cp .env.example .env && make gen-secrets && make dev
#   make migrate   (in separate terminal once db is healthy)
#   make test
#
# SIM-443b: every compose command needs the secrets in .env (the passwords
# have no defaults). `make gen-secrets` appends any that are missing.
# =============================================================================

.PHONY: help dev down build migrate test test-unit test-integration test-regression lint \
        type-check format shell logs clean nuke profile-computor engine-artifacts calibrate \
        validate-props load-historical-odds ingest-catch-up live-logs db-roles gen-secrets \
        backup backup-status backup-drill restore-postgres backup-offsite

# Default target — show help.
##
## Cross-platform: uses Python `print()` instead of multiple `@echo` lines.
## Windows cmd.exe prints the literal "" instead of a blank line for
## `echo ""`, so the bash form was visually broken under Windows GNU Make.
help:
	@py -c "print('''\nMLB Baseball Simulation Platform\n=================================\n\n  make dev               Build + start all services (foreground)\n  make down              Stop and remove containers + networks\n  make build             Force rebuild Docker images\n  make migrate           Apply all Alembic migrations (requires db healthy)\n  make gen-secrets       Append any missing secret to .env (passwords, keys)\n  make db-roles          Create/refresh the baseball_app + baseball_ro roles\n                         (ROTATE_ADMIN_PASSWORD=1 also resets baseball_user)\n\n  make test              Run full test suite (unit + integration)\n  make test-unit         Unit tests only (no Docker deps)\n  make test-regression   Regression gate (golden-file engine drift detection)\n  make test-integration  Integration tests (spins up testcontainers)\n\n  make lint              Ruff lint check\n  make format            Ruff auto-format\n  make type-check        Mypy static type analysis\n\n  make shell             Open a bash shell inside the app container\n  make logs              Tail logs for all services\n  make clean             Remove Python artifacts (__pycache__, .coverage, etc.)\n  make nuke              Full teardown: containers + volumes (destructive!)\n\n  make backup            Back up Postgres, DuckDB and the bundle into BACKUP_DIR\n  make backup-offsite    Copy the newest sets to OFFSITE_BACKUP_DIR (OneDrive) now\n  make backup-status     Show the newest backup set of each store and its age,\n                         and whether the off-site copy mirrors it\n  make backup-drill      Restore the newest set into a scratch cluster and check it\n                         (SET=<ts> picks a set; OFFSITE=1 restores from the copy)\n  make restore-postgres  Restore SET=<ts> into an EMPTY database (refuses a full one)\n''')"

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
## SIM-443b: the migrate service is the ONLY service that logs in as the
## cluster owner (POSTGRES_USER); compose builds its DSN from POSTGRES_PASSWORD.
## The app logs in as baseball_app, which cannot create or alter a table, so a
## migration through the app service would fail. Naming the service enables
## its `tools` profile (Compose v2). alembic env.py auto-coerces the
## postgresql:// scheme to postgresql+psycopg2:// for SQLAlchemy.
migrate: _require_env_file
	@echo "Applying Alembic migrations..."
	docker compose run --rm migrate
	@echo "Migrations complete."

## SIM-443b: create or refresh the two least-privilege roles (baseball_app,
## baseball_ro) and their grants, inside the running db container. Idempotent:
## it resets both passwords from .env every run. Add ROTATE_ADMIN_PASSWORD=1
## to also set baseball_user's password from POSTGRES_PASSWORD. Run it after
## the db service is healthy (a fresh volume runs it on first start).
db-roles: _require_env_file
	docker compose exec -T -e ROTATE_ADMIN_PASSWORD=$(or $(ROTATE_ADMIN_PASSWORD),0) db sh /opt/baseball/roles/setup_roles.sh

## SIM-443b: append every missing secret to .env (a present key, even blank,
## is left alone). It prints key names only, never a value.
gen-secrets:
	py scripts/gen_secrets.py --append .env

# ---------------------------------------------------------------------------
# Backups of the three data stores (SIM-525)
# ---------------------------------------------------------------------------
# The scheduler runs the same two steps every day at 10:00 UTC. The sets land
# in BACKUP_DIR (.env; default ./backups), mounted at /backups. At 10:30 UTC
# the off-site copy (SIM-568) copies the newest sets into OFFSITE_BACKUP_DIR
# (.env; default ./backups_offsite), mounted at /offsite in the jobs container.

## Back up now. The Postgres dump runs in the db container as the postgres OS
## user, over the socket. The data step copies the DuckDB file, the bundle and
## the small /data files in the jobs container, and leaves nightly-backup.last.
## --new-set: a manual run always makes a fresh set, even within an hour of
## the scheduled one (the scheduled run's crash retry skips a finished store).
## Run it from cmd.exe: Git Bash rewrites the container paths (CLAUDE.md 2a).
backup: _require_env_file
	docker compose exec -T -u postgres db sh /opt/baseball/backup/pg_backup.sh
	docker compose exec -T jobs sh /app/scripts/with_mark.sh nightly-backup python -m pipeline.backup.data_stores --new-set

## Show the newest complete set of each store, its age and its size. The
## second line shows the off-site copy of each store (SIM-568): MIRRORED when
## it holds the newest local set, else BEHIND or none.
backup-status: _require_env_file
	docker compose exec -T jobs python -m pipeline.backup.data_stores --status
	docker compose exec -T jobs python -m pipeline.backup.offsite --status

## The off-site copy now (SIM-568): copy the newest complete set of each store
## from BACKUP_DIR into OFFSITE_BACKUP_DIR (the OneDrive folder), mounted at
## /offsite in the jobs container. The scheduler runs the same step at 10:30
## UTC. It leaves nightly-backup-offsite.last. Run it from cmd.exe.
## Never run it during the scheduled copy (from 10:30 UTC until its mark
## goes), and never during a `make backup` (that run can prune a local set
## while the copy reads it).
## The first line refuses (exit 2) while another copy runs, that is while a
## nightly-backup-offsite.running mark is under 6 hours old (360 minutes, the
## copy's own limit for a stale mark). An older mark is a dead run's, and the
## copy goes ahead. The script holds no $ and no %, so make, Git Bash and
## cmd.exe pass it to the container unchanged.
backup-offsite: _require_env_file
	docker compose exec -T jobs sh -c "if find /data/ingest_marks -maxdepth 1 -name nightly-backup-offsite.running -mmin -360 2>/dev/null | grep -q .; then echo 'another off-site copy runs (a nightly-backup-offsite.running mark under 6 hours old)' >&2; exit 2; fi"
	docker compose exec -T jobs sh /app/scripts/with_mark.sh nightly-backup-offsite python -m pipeline.backup.offsite

## The restore drill: restore a Postgres set into a scratch cluster, check the
## table counts, verify the DuckDB and bundle copies, then remove the cluster.
## It runs on the host and leaves a report in BACKUP_DIR/drills/.
## OFFSITE=1 restores from the off-site copy instead (SIM-568); the report
## then lands in OFFSITE_BACKUP_DIR/drills/. Only the value 1 turns it on:
## OFFSITE=0 (or any other value) runs the local drill.
##   make backup-drill SET=20261015T100000Z
##   make backup-drill OFFSITE=1
backup-drill: _require_env_file
	py scripts/backup_drill.py --set $(or $(SET),latest) $(if $(filter 1,$(OFFSITE)),--offsite,)

## Restore one Postgres set into the running db container. The script refuses
## a database that still holds the raw or sim schema, so it never overwrites
## live data. Afterwards run `make db-roles` (the grants) and
## `docker compose run --rm migrate alembic current` (expect the head).
##   make restore-postgres SET=20261015T100000Z
restore-postgres: _require_env_file
	@py -c "import sys; '$(SET)'.strip() or print('ERROR: name the set: make restore-postgres SET=<YYYYmmddTHHMMSSZ>', file=sys.stderr) or sys.exit(2)"
	docker compose exec -T -u postgres db sh /opt/baseball/backup/pg_restore.sh /backups/postgres/$(SET)

# ---------------------------------------------------------------------------
# Nightly batch jobs
# ---------------------------------------------------------------------------
# These run in order each night: the profile computor rebuilds the DuckDB
# pools (sim.pitch_pool / sim.outcome_pool), then the engine-artifact builder
# (SIM-422) materializes the bundle the full-pool sampler reads.  The bundle
# build MUST run AFTER the profile computor (scripts/nightly_ingest.sh runs
# the chain).

## SIM-519 Part D: load the final games of the last DAYS days (default 3) by
## hand, through the crash wrapper. Postgres only; safe while the app runs.
##   make ingest-catch-up DAYS=14
ingest-catch-up: _require_env_file
	docker compose run --rm -e DAYS=$(or $(DAYS),3) app sh /app/scripts/with_mark.sh nightly-finals sh /app/scripts/with_retry.sh 6 sh /app/scripts/nightly_finals.sh

## SIM-519 Part C: follow the live service's log (schedule polls, lineup writes,
## push subscriptions).
live-logs:
	docker compose logs -f --tail=200 live

## Nightly: rebuild DuckDB player profiles + sim pools from Postgres.
profile-computor: _require_env_file
	docker compose run --rm app python -m pipeline.batch.player_profile_computor

## Nightly (SIM-422/486): build the engine-artifact bundle the production
## sampler reads (pools + pitcher similarity + actor embeddings).  Runs AFTER
## profile-computor.  Pass FLAGS="--what pool" to rebuild the pools only.
engine-artifacts: _require_env_file
	docker compose run --rm app python -m pipeline.batch.engine_artifacts --what all $(FLAGS)

## Nightly (SIM-406): fit + persist the CalibrationReport over the DuckDB profiles.
## Runs AFTER profile-computor (it reads the derived.* season-metrics tables) and
## writes CALIBRATION_REPORT_PATH (/data/calibration.json), which the API loads at
## boot to calibrate EVERY similarity engine + the win-prob map.  Default fits all
## seasons incl. the arsenal W2 anchor; pass FLAGS="--no-arsenal" to skip the slow
## pitcher-engine W2 sample, or FLAGS="--seasons 2024 --validate" to subset/verify.
calibrate: _require_env_file
	docker compose run --rm app python scripts/fit_calibration.py $(FLAGS)

## Offline (SIM-407): validate win-prob + prop PMFs against real completed games and
## (with --write-calibration) fit the win-prob reliability curve back into the
## CalibrationReport.  Runs real sims, so it is slow — cap a smoke run with
## FLAGS="--seasons 2024 --max-games 50".  Typical full run:
## FLAGS="--seasons 2023 2024 --iterations 100 --write-calibration".
validate-props: _require_env_file
	docker compose run --rm app python scripts/validate_props.py $(FLAGS)

## Offline (SIM-435): backfill OPENING + CLOSING odds for Final games into
## raw.game_odds / raw.prop_odds so the accuracy comparison has closing lines
## to score against.  SIM-555: one row per book (`bp:<id>`), guarded, batched.
## Network-bound.  Name the provider in FLAGS: --provider bettingpros for real
## lines, or --provider mock for the deterministic wiring smoke.  The loader
## refuses to run the mock unasked.  The app container reads ODDS_PROVIDER and
## ODDS_API_KEY from .env.  A --provider flag wins over .env.
## --book NAME loads one book's rows.  Example:
## FLAGS="--seasons 2024 --max-games 5 --provider bettingpros".
load-historical-odds: _require_env_file
	docker compose run --rm app python scripts/load_historical_odds.py $(FLAGS)

## Tail logs for all services (Ctrl-C to exit)
logs:
	docker compose logs -f

## Open a bash shell inside the running app container
shell:
	docker compose exec app bash

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

## SIM-567 (the test lane's odds provider): compose mounts only the five code
## dirs (api/, pipeline/, similarity/, simulation/, db/). The -v "$(CURDIR):/app"
## mount below puts the whole checkout over the image's copy, tests/, scripts/
## and pyproject.toml included. These targets then test the checkout, not the
## last image build. From a worktree, compose reads the worktree's own
## compose file and .env. That project has no app image, so compose builds
## one. Run these targets from the main checkout. They test its code, not a
## worktree's.
TESTS = tests/unit/

## Run the full test suite inside Docker (unit + integration)
##
## BASEBALL_DB_DSN and REDIS_URL come from .env via env_file (docker-compose.yml).
## Cross-platform: no bash $${VAR:-default} substitution that breaks on cmd.exe.
test: _require_env_file
	docker compose run --rm -v "$(CURDIR):/app" app \
	  pytest tests/ -v --tb=short --timeout=60 \
	       --cov=similarity --cov=pipeline --cov=simulation --cov=betting --cov=api \
	       --cov-report=term-missing --cov-report=xml:coverage.xml

## Unit tests only — no live DB or Redis required, fast feedback loop
## Can run locally without Docker if deps are installed in a venv.
## TESTS="tests/unit/test_x.py tests/unit/test_y.py" names the files to run.
test-unit:
	docker compose run --rm -v "$(CURDIR):/app" app \
	  pytest $(TESTS) -v --tb=short --timeout=30 \
	         --cov=similarity --cov=pipeline --cov=simulation --cov=betting --cov=api \
	         --cov-report=term-missing

## Regression tests — the engine invariant gate (no snapshots; CLAUDE.md §7).
test-regression:
	docker compose run --rm -v "$(CURDIR):/app" app \
	  pytest tests/regression/ -v --tb=short --timeout=60 \
	         -m regression

## Integration tests — testcontainers spins up ephemeral PostgreSQL + Redis
## Mirrors the CI weekly integration job.
test-integration: _require_env_file
	docker compose run --rm -v "$(CURDIR):/app" app \
	  pytest tests/integration/ -v --tb=short --timeout=120 \
	         -m integration

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

## Ruff: lint all Python source (same config as CI Job 2)
lint:
	docker compose run --rm -v "$(CURDIR):/app" app ruff check . --output-format=full

## Ruff: auto-format source files
format:
	docker compose run --rm -v "$(CURDIR):/app" app ruff format .

## Mypy: static type checking (same config as CI Job 2)
type-check:
	docker compose run --rm -v "$(CURDIR):/app" app \
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
	@echo "WARNING: This will delete all Docker volumes including the PostgreSQL database. Take a backup first: make backup (SIM-525)"
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
	@py -c "import os, sys; os.path.isfile('.env') or print('\nERROR: .env file not found.\n  Run: cp .env.example .env\n  Then: py scripts/gen_secrets.py --append .env\n', file=sys.stderr) or sys.exit(1)"
