#!/usr/bin/env sh
# =============================================================================
# scripts/weekly_refresh.sh — sweep the current season for any final game the
# nightly window missed (SIM-519 Part D). Postgres only; safe while the app runs.
# Ofelia runs it on Sunday through the crash wrapper.
# =============================================================================
set -eu

export BASEBALL_DB_DSN="${BASEBALL_DB_DSN:-postgresql://baseball_user:baseball_pass@db:5432/baseball_sim}"
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
