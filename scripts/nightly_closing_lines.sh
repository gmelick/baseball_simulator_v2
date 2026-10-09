#!/usr/bin/env sh
# =============================================================================
# scripts/nightly_closing_lines.sh — the nightly closing pass (SIM-546).
#
# A closing row is the last price a book posted before first pitch. The live
# pipeline promotes each game's closing rows at first pitch. This job is the
# fallback and the reconciliation: it runs the historical loader's closing pass
# over the games of yesterday and the day before (UTC dates; two dates cover a
# late West-coast game that turns Final after midnight UTC).
#
#   * A game the live pipeline missed (a restart, a crash, a failed promotion)
#     gets its closing rows here.
#   * Where a book's own closing snapshot matches the promoted row, the
#     loader's row deduplicates (the live marker rewrote the row's hash).
#   * Where it differs, the loader adds a second closing row with a later
#     fetch time, and every reader takes the latest one.
#
# The job runs only when ODDS_PROVIDER is "bettingpros". Any other value (or
# none) writes nothing and exits 0 with one log line: the mock provider must
# never write mock closing rows into a real store. With the real provider and
# no ODDS_API_KEY, the job exits 1, because every vendor read would fail.
#
# Invoked by the Ofelia scheduler (docker-compose `scheduler` profile) at 09:30
# UTC, inside the running app container (job-exec): that container reads the
# host's .env, where ODDS_API_KEY lives; see deploy/ofelia/config.ini. It is a
# separate job from nightly_ingest.sh on purpose: a vendor outage must not stop
# the profile and artifact rebuild. Safe to run by hand:
#   docker compose exec app env ODDS_PROVIDER=bettingpros \
#       sh /app/scripts/nightly_closing_lines.sh
# =============================================================================
set -eu

export BASEBALL_DB_DSN="${BASEBALL_DB_DSN:-postgresql://baseball_user:baseball_pass@db:5432/baseball_sim}"

log() { echo "[nightly-closing-lines $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

PROVIDER="$(printf '%s' "${ODDS_PROVIDER:-}" | tr '[:upper:]' '[:lower:]')"
if [ "${PROVIDER}" != "bettingpros" ]; then
    log "skipped: ODDS_PROVIDER is '${ODDS_PROVIDER:-unset}', not 'bettingpros' (no rows written)"
    exit 0
fi
if [ -z "${ODDS_API_KEY:-}" ]; then
    log "ERROR: ODDS_PROVIDER is bettingpros but ODDS_API_KEY is not set; nothing loaded"
    exit 1
fi

# GNU date (the app image is Debian): yesterday and the day before, in UTC.
D1="$(date -u -d 'yesterday' +%Y-%m-%d)"
D2="$(date -u -d '2 days ago' +%Y-%m-%d)"
Y1="$(date -u -d 'yesterday' +%Y)"
Y2="$(date -u -d '2 days ago' +%Y)"
SEASONS="${Y1}"
if [ "${Y2}" != "${Y1}" ]; then
    SEASONS="${Y2} ${Y1}"
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

log "start: closing pass for the games of ${D1} and ${D2} (seasons ${SEASONS})"
# SEASONS is one or two words; the split is intended.
# shellcheck disable=SC2086
python "${REPO_DIR}/scripts/load_historical_odds.py" \
    --provider bettingpros \
    --seasons ${SEASONS} \
    --line-types closing \
    --game-dates "${D1}" "${D2}"
log "done"
