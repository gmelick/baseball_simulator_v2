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
# SIM-441: refresh_seasons now returns {attempted, loaded, failed, skipped,
# rows_written} and contains per-game failures (a single malformed feed no longer
# aborts the run; _CONSECUTIVE_FAILURE_LIMIT successive failures still do). Exit
# non-zero if ANY game failed, so `set -e` stops the chain here rather than
# rebuilding profiles and FAISS tiles on a knowingly incomplete season.
# NOTE: the heredoc delimiter is QUOTED ('PY'), so the shell performs NO
# parameter expansion inside it — which is what we want for Python code, but it
# means ${YEAR} would arrive as a literal string. The year is therefore passed
# through the ENVIRONMENT and read with os.environ, not interpolated.
export YEAR
python - <<'PY'
import os
import sys

from pipeline.etl.etl_historical_loader import HistoricalDataLoader

year = int(os.environ["YEAR"])
loader = HistoricalDataLoader()
try:
    summary = loader.refresh_seasons(year, year)
finally:
    loader.close()

print(f"[nightly-ingest] refresh_seasons summary: {summary}")
if summary["failed"]:
    print(
        f"[nightly-ingest] {summary['failed']} game(s) failed — refusing to rebuild "
        "profiles/tiles on an incomplete season. Re-run after investigating; "
        "already-loaded games are skipped.",
        file=sys.stderr,
    )
    sys.exit(1)
PY

log "2/3 player_profile_computor --seasons ${YEAR}"
python -m pipeline.batch.player_profile_computor --seasons "${YEAR}"

log "3/3 play_pool_cache --seasons ${YEAR}"
python -m pipeline.batch.play_pool_cache --seasons "${YEAR}"

log "done"
