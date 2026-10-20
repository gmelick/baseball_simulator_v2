#!/usr/bin/env sh
# =============================================================================
# scripts/nightly_rebuild.sh — rebuild the profiles and the engine bundle for
# the current season (steps 2 and 3 of scripts/nightly_ingest.sh).
#
# Both steps write DuckDB and need its write lock. The running app holds no
# handle on the file (the rebuild beside the app, SIM-524), so the scheduler
# runs this at 08:00 UTC beside the app (deploy/ofelia/config.ini). By hand:
#   docker compose run --rm app sh /app/scripts/nightly_rebuild.sh
# Step 2 publishes a new bundle version; the app then restarts itself
# to load the new profiles and bundle (api/bundle_watch.py).
# =============================================================================
set -eu

YEAR="$(date -u +%Y)"
# No default password: the container composes the DSN from .env.
: "${BASEBALL_DB_DSN:?BASEBALL_DB_DSN is not set - the jobs service composes it from .env (see .env.example)}"
export BASEBALL_DB_DSN
export BASEBALL_DUCKDB_PATH="${BASEBALL_DUCKDB_PATH:-/data/baseball_sim.duckdb}"

log() { echo "[nightly-rebuild $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# The profiles read Postgres, so the night's finals must be
# loaded first. The wait never fails the rebuild (scripts/wait_for_ingest.sh).
log "0/2 wait for the finals load"
sh /app/scripts/wait_for_ingest.sh

log "1/2 player_profile_computor --seasons ${YEAR}"
python -m pipeline.batch.player_profile_computor --seasons "${YEAR}"

log "2/2 engine_artifacts --what all"
python -m pipeline.batch.engine_artifacts --what all

log "done"
