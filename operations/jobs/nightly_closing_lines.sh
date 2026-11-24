#!/usr/bin/env sh
# =============================================================================
# operations/jobs/nightly_closing_lines.sh — load the odds of every finished
# game that is missing them.
#
# The live service stores each game's closing line at first pitch. It stores no
# opening line, and it misses every game while it is down. This job runs the
# historical loader over this season and the last one. The loader picks only the
# Final games with no stored opening or closing row, so a game the live service
# missed gets both lines here, and a loaded game costs nothing.
#
# Ofelia runs it at 10:30 UTC in the app container, which holds ODDS_API_KEY
# (deploy/ofelia/config.ini). It is its own job so a vendor outage never stops
# the nightly rebuild. To run it by hand:
#   docker compose exec app sh /app/operations/jobs/nightly_closing_lines.sh
# =============================================================================
set -eu

: "${BASEBALL_DB_DSN:?BASEBALL_DB_DSN is not set - the app service composes it from .env (see .env.example)}"
export BASEBALL_DB_DSN

log() { echo "[nightly-closing-lines $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# Without the key every vendor read fails, so stop with an error.
if [ -z "${ODDS_API_KEY:-}" ]; then
    log "ERROR: ODDS_API_KEY is not set; nothing loaded"
    exit 1
fi

# Last season too, so a late-season game still missing odds is retried after
# New Year's Day.
YEAR="$(date -u +%Y)"
LAST_YEAR="$((YEAR - 1))"

cd "$(dirname "$0")/../.."
log "start: the Final games of ${LAST_YEAR} and ${YEAR} that are missing odds"
python -m betting.historical_loader --seasons "${LAST_YEAR}" "${YEAR}"
log "done"
