#!/usr/bin/env sh
# =============================================================================
# operations/jobs/weekly_refresh.sh — sweep the current season for any final game the nightly window missed.
#
# Postgres only, so it runs while the app serves. Ofelia runs it on Sunday
# through the crash wrapper. Exits 1 when any game failed.
# =============================================================================
set -eu

# No default password: the container composes the DSN from .env.
: "${BASEBALL_DB_DSN:?BASEBALL_DB_DSN is not set - the jobs service composes it from .env (see .env.example)}"
export BASEBALL_DB_DSN
export YEAR="$(date -u +%Y)"

echo "[weekly-refresh $(date -u '+%Y-%m-%dT%H:%M:%SZ')] refresh_seasons(${YEAR})"
python - <<'PY'
import logging
import os
import sys

from ingest.games.load_games import HistoricalDataLoader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
year = int(os.environ["YEAR"])
loader = HistoricalDataLoader()
try:
    summary = loader.refresh_seasons(year, year)
finally:
    loader.close()
print(f"[weekly-refresh] {summary}")
sys.exit(1 if summary["failed"] else 0)
PY
