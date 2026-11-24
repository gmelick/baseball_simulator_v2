#!/usr/bin/env sh
# =============================================================================
# operations/jobs/nightly_ingest.sh — the whole nightly data chain, for a run by hand.
#
# For the current season it runs, in order:
#   1. refresh_seasons(YEAR): load the games that became Final;
#   2. player_profile_computor: rebuild the DuckDB profiles and play pools;
#   3. engine_artifacts: rebuild the bundle the sampler reads.
#
# The scheduler does not run this chain. It runs nightly_finals.sh,
# weekly_refresh.sh and nightly_rebuild.sh instead. By hand:
#   docker compose run --rm app sh /app/operations/jobs/nightly_ingest.sh
#
# The script stops at once when BASEBALL_DB_DSN is not set.
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
# Exit 1 when any game failed, so set -e stops the chain before it rebuilds
# on a season it knows is incomplete. The quoted heredoc ('PY') expands no
# shell variable, so the year crosses into Python through the environment.
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
