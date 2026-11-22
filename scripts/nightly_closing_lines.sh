#!/usr/bin/env sh
# =============================================================================
# scripts/nightly_closing_lines.sh — the nightly closing pass.
#
# A closing row is the last price a book posted before first pitch. The live
# pipeline promotes each game's closing rows at first pitch. This job is the
# fallback and the reconciliation: it runs the historical loader
# (python -m betting.history) over the games of yesterday and the day before
# (UTC dates; two dates cover a
# late West-coast game that turns Final after midnight UTC).
#
#   * A game the live pipeline missed (a restart, a crash, a failed promotion)
#     gets its closing rows here.
#   * Where a book's own closing snapshot matches the promoted row, the
#     loader's row deduplicates (the live marker rewrote the row's hash).
#   * Where it differs, the loader adds a second closing row with a later
#     fetch time, and every reader takes the latest one.
#
# It loads the opening and the closing rows. The live pipeline stores no
# opening row; BettingPros returns each market's opener with every offer, so
# this pass recovers it. Without ODDS_API_KEY the job exits 1, because every
# vendor read would fail.
#
# Invoked by the Ofelia scheduler (docker-compose `scheduler` profile) at 10:30
# UTC, inside the running app container (job-exec): that container reads the
# host's .env, where ODDS_API_KEY lives; see deploy/ofelia/config.ini. It is a
# separate job from nightly_ingest.sh on purpose: a vendor outage must not stop
# the profile and artifact rebuild. Safe to run by hand:
#   docker compose exec app sh /app/scripts/nightly_closing_lines.sh
# =============================================================================
set -eu

# No default password: the container composes the DSN from .env.
: "${BASEBALL_DB_DSN:?BASEBALL_DB_DSN is not set - the app service composes it from .env (see .env.example)}"
export BASEBALL_DB_DSN

log() { echo "[nightly-closing-lines $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

if [ -z "${ODDS_API_KEY:-}" ]; then
    log "ERROR: ODDS_API_KEY is not set; nothing loaded"
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

log "start: opening and closing pass for the games of ${D1} and ${D2} (seasons ${SEASONS})"
# SEASONS is one or two words; the split is intended.
cd "${REPO_DIR}"
# shellcheck disable=SC2086
python -m betting.history \
    --seasons ${SEASONS} \
    --line-types opening closing \
    --game-dates "${D1}" "${D2}"
log "done"
