#!/usr/bin/env sh
# =============================================================================
# scripts/nightly_ingest.sh — nightly data ingestion chain.
#
# Runs the three steps in dependency order for the CURRENT season:
#   1. refresh_seasons(YEAR)        — load games that have newly become Final
#                                     (the SIM-405fix guard skips future/in-progress)
#   2. player_profile_computor      — rebuild the DuckDB derived profiles + sim pools
#   3. play_pool_cache              — materialize the FAISS play-pool tiles
#
# Invoked by the Ofelia scheduler (docker-compose `scheduler` profile) as a
# fresh container off the app image; see deploy/ofelia/config.ini. Safe to run
# by hand:  docker compose run --rm app sh /app/scripts/nightly_ingest.sh
#
# BASEBALL_DB_DSN must point at the in-container DB (db:5432); a default is set
# below so the job works even if the env only carries the host-side DSN.
# =============================================================================
set -eu

YEAR="$(date -u +%Y)"
export BASEBALL_DB_DSN="${BASEBALL_DB_DSN:-postgresql://baseball_user:baseball_pass@db:5432/baseball_sim}"
export BASEBALL_DUCKDB_PATH="${BASEBALL_DUCKDB_PATH:-/data/baseball_sim.duckdb}"

log() { echo "[nightly-ingest $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

log "start — season ${YEAR}"

log "1/3 refresh_seasons(${YEAR}) — load newly-Final games"
python -c "from pipeline.etl.etl_historical_loader import HistoricalDataLoader; l = HistoricalDataLoader('${BASEBALL_DB_DSN}'); l.refresh_seasons(${YEAR}, ${YEAR}); l.close()"

log "2/3 player_profile_computor --seasons ${YEAR}"
python -m pipeline.batch.player_profile_computor --seasons "${YEAR}"

log "3/3 play_pool_cache --seasons ${YEAR}"
python -m pipeline.batch.play_pool_cache --seasons "${YEAR}"

log "done"
