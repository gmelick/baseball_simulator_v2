#!/usr/bin/env sh
# =============================================================================
# scripts/nightly_ingest.sh — nightly data ingestion chain.
#
# Runs the three steps in dependency order for the CURRENT season:
#   1. refresh_seasons(YEAR)        — load games that have newly become Final
#                                     (a guard skips future and in-progress games)
#   2. player_profile_computor      — rebuild the DuckDB derived profiles + sim pools
#   3. engine_artifacts             — rebuild the engine-artifact bundle the
#                                     full-pool sampler reads
#
# The scheduler does not run this chain. It runs scripts/nightly_finals.sh
# (step 1's job, a three-day window) nightly and scripts/weekly_refresh.sh on
# Sunday; steps 2 and 3 are scripts/nightly_rebuild.sh, the 08:00 UTC job (it
# runs beside the app, which holds no DuckDB handle). This script is the whole
# chain for a by-hand run:
#   docker compose run --rm app sh /app/scripts/nightly_ingest.sh
#
# BASEBALL_DB_DSN must point at the in-container DB (db:5432). The script has no
# default: it exits at once, naming the variable, when the DSN is not set.
# =============================================================================
set -eu

YEAR="$(date -u +%Y)"
# No default password: the container composes the DSN from .env.
: "${BASEBALL_DB_DSN:?BASEBALL_DB_DSN is not set - the jobs service composes it from .env (see .env.example)}"
export BASEBALL_DB_DSN
export BASEBALL_DUCKDB_PATH="${BASEBALL_DUCKDB_PATH:-/data/baseball_sim.duckdb}"

log() { echo "[nightly-ingest $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

log "start — season ${YEAR}"

log "1/3 refresh_seasons(${YEAR}) — load newly-Final games"
# refresh_seasons returns {attempted, loaded, failed, skipped, rows_written}
# and contains per-game failures (a single malformed feed does not abort the
# run; _CONSECUTIVE_FAILURE_LIMIT successive failures do). Exit
# non-zero if ANY game failed, so `set -e` stops the chain here rather than
# rebuilding profiles and the artifact bundle on a knowingly incomplete season.
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
        "profiles/artifacts on an incomplete season. Re-run after investigating; "
        "already-loaded games are skipped.",
        file=sys.stderr,
    )
    sys.exit(1)
PY

log "2/3 player_profile_computor --seasons ${YEAR}"
python -m pipeline.batch.player_profile_computor --seasons "${YEAR}"

log "3/3 engine_artifacts --what all"
python -m pipeline.batch.engine_artifacts --what all

log "done"
