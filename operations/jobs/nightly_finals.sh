#!/usr/bin/env sh
# =============================================================================
# operations/jobs/nightly_finals.sh — load the last few days' final games.
#
# Loads every game that became Final from today minus DAYS (default 3) to
# today, in Eastern time, the league's day. The window catches a suspended game
# finished later and a night the host was off. The loader skips games already
# loaded. The job writes Postgres only, so it runs while the app serves.
#
# Ofelia runs it nightly through the crash wrapper:
#   sh /app/operations/jobs/with_retry.sh 6 sh /app/operations/jobs/nightly_finals.sh
# By hand, a longer window:  make ingest-catch-up DAYS=14
# Exits 1 when any game failed.
# =============================================================================
set -eu

# No default password: the container composes the DSN from .env.
: "${BASEBALL_DB_DSN:?BASEBALL_DB_DSN is not set - the jobs service composes it from .env (see .env.example)}"
export BASEBALL_DB_DSN
export DAYS="${DAYS:-3}"

log() { echo "[nightly-finals $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
log "start — the last ${DAYS} day(s), Eastern"

python - <<'PY'
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ingest.games.load_games import HistoricalDataLoader

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
