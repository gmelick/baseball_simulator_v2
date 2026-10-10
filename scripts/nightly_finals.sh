#!/usr/bin/env sh
# =============================================================================
# scripts/nightly_finals.sh — load the last few days' final games (SIM-519 Part D).
#
# Loads every game that became Final from (today - DAYS) to today, today in
# Eastern time (the league's day). DAYS defaults to 3: the window catches a
# suspended game completed later, a game the previous night missed and a host
# that was off for a night. The loader skips games already loaded, so the
# window costs a few schedule reads. Postgres only: no DuckDB write, so it is
# safe while the app runs (the profile + artifact rebuild is
# scripts/nightly_rebuild.sh, the 08:00 UTC job that takes the DuckDB lock).
#
# Ofelia runs it nightly through the crash wrapper:
#   sh /app/scripts/with_retry.sh 6 sh /app/scripts/nightly_finals.sh
# By hand, a longer window:  make ingest-catch-up DAYS=14
# Exits non-zero when any game failed, so the record is honest.
# =============================================================================
set -eu

export BASEBALL_DB_DSN="${BASEBALL_DB_DSN:-postgresql://baseball_user:baseball_pass@db:5432/baseball_sim}"
export DAYS="${DAYS:-3}"

log() { echo "[nightly-finals $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
log "start — the last ${DAYS} day(s), Eastern"

python - <<'PY'
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pipeline.etl.etl_historical_loader import HistoricalDataLoader

days = int(os.environ["DAYS"])
end = datetime.now(ZoneInfo("America/New_York")).date()
start = end - timedelta(days=days)
loader = HistoricalDataLoader()
try:
    summary = loader.load_date_range(start, end)
finally:
    loader.close()

print(f"[nightly-finals] {start}..{end}: {summary}")
if summary["failed"]:
    print(f"[nightly-finals] {summary['failed']} game(s) failed", file=sys.stderr)
    sys.exit(1)
PY

log "done"
