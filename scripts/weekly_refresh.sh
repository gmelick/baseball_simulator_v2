#!/usr/bin/env sh
# =============================================================================
# scripts/weekly_refresh.sh — sweep the current season for any final game the
# nightly window missed. Postgres only; safe while the app runs.
# Ofelia runs it on Sunday through the crash wrapper.
# =============================================================================
set -eu

# No default password: the container composes the DSN from .env.
: "${BASEBALL_DB_DSN:?BASEBALL_DB_DSN is not set - the jobs service composes it from .env (see .env.example)}"
export BASEBALL_DB_DSN
export YEAR="$(date -u +%Y)"

echo "[weekly-refresh $(date -u '+%Y-%m-%dT%H:%M:%SZ')] refresh_seasons(${YEAR})"
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
print(f"[weekly-refresh] {summary}")
sys.exit(1 if summary["failed"] else 0)
PY
