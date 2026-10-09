#!/usr/bin/env sh
# =============================================================================
# scripts/nightly_rebuild.sh — rebuild the profiles and the engine bundle for
# the current season (SIM-519 Part D; steps 2 and 3 of the old nightly chain).
#
# Both steps write DuckDB and need its write lock, which the running app's
# forkserver holds (SIM-524). The scheduler job for this script stays
# DISABLED until that ticket lands. Run it by hand with the app stopped:
#   docker compose stop app
#   docker compose run --rm app sh /app/scripts/nightly_rebuild.sh
#   docker compose up -d app
# =============================================================================
set -eu

YEAR="$(date -u +%Y)"
export BASEBALL_DB_DSN="${BASEBALL_DB_DSN:-postgresql://baseball_user:baseball_pass@db:5432/baseball_sim}"
export BASEBALL_DUCKDB_PATH="${BASEBALL_DUCKDB_PATH:-/data/baseball_sim.duckdb}"

log() { echo "[nightly-rebuild $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

log "1/2 player_profile_computor --seasons ${YEAR}"
python -m pipeline.batch.player_profile_computor --seasons "${YEAR}"

log "2/2 engine_artifacts --what all"
python -m pipeline.batch.engine_artifacts --what all

log "done"
