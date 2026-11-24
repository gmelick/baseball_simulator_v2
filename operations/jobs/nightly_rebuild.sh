#!/usr/bin/env sh
# =============================================================================
# operations/jobs/nightly_rebuild.sh — rebuild the current season's profiles and the engine bundle.
#
# The profile step writes DuckDB and needs its write lock; the bundle step reads
# it and publishes a new bundle version. The app holds no handle on the file, so
# the scheduler runs this job beside the app at 08:00 UTC. The app restarts
# itself on the new bundle (api/bundle_watch.py). By hand:
#   docker compose run --rm app sh /app/operations/jobs/nightly_rebuild.sh
# =============================================================================
set -eu

YEAR="$(date -u +%Y)"
# No default password: the container composes the DSN from .env.
: "${BASEBALL_DB_DSN:?BASEBALL_DB_DSN is not set - the jobs service composes it from .env (see .env.example)}"
export BASEBALL_DB_DSN
export BASEBALL_DUCKDB_PATH="${BASEBALL_DUCKDB_PATH:-/data/baseball_sim.duckdb}"

log() { echo "[nightly-rebuild $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# The profiles read Postgres, so the night's finals load first.
# The wait never fails the rebuild.
log "0/2 wait for the finals load"
sh /app/operations/jobs/wait_for_ingest.sh

log "1/2 player_profile_computor --seasons ${YEAR}"
python -m pipeline.batch.player_profile_computor --seasons "${YEAR}"

log "2/2 engine_artifacts --what all"
python -m pipeline.batch.engine_artifacts --what all

log "done"
