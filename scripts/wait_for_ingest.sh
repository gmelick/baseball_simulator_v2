#!/usr/bin/env sh
# =============================================================================
# scripts/wait_for_ingest.sh — the nightly rebuild's first step: make sure the
# finals load has run today before the rebuild reads Postgres (SIM-563
# follow-up, 2026-10-11).
#
# Why: the finals load (07:00 UTC) and the rebuild (08:00 UTC) rely on the hour
# between them. On 2026-10-11 the host slept through both times, and the
# scheduler fired both jobs in the same second on wake. That day the finals
# load won the race; nothing made sure it would.
#
# What it does, reading the marks scripts/with_mark.sh writes:
#   1. waits INGEST_GRACE_S (default 60 s), so a job fired in the same second
#      has time to write its mark;
#   2. while the finals load or the Sunday season sweep runs, waits (at most
#      INGEST_WAIT_S, default 2 hours; a mark older than 6 hours is stale);
#   3. when the finals load has finished today (UTC), the rebuild goes on,
#      whatever its exit code (it loaded what it could);
#   4. when it has not run today, runs it here, first.
# It never fails the rebuild: on a timeout it logs and lets the rebuild go on.
# =============================================================================
set -u

MARK_DIR="${INGEST_MARK_DIR:-/data/ingest_marks}"
GRACE_S="${INGEST_GRACE_S:-60}"
WAIT_S="${INGEST_WAIT_S:-7200}"
POLL_S="${INGEST_POLL_S:-30}"
STALE_S="${INGEST_STALE_S:-21600}"
FINALS_CMD="${INGEST_FINALS_CMD:-sh /app/scripts/with_mark.sh nightly-finals sh /app/scripts/with_retry.sh 6 sh /app/scripts/nightly_finals.sh}"

log() { echo "[wait-for-ingest $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# True when a fresh <name>.running mark exists.
running() {
  f="$MARK_DIR/$1.running"
  [ -f "$f" ] || return 1
  started="$(cat "$f" 2>/dev/null || echo 0)"
  age=$(( $(date -u +%s) - ${started:-0} ))
  [ "$age" -lt "$STALE_S" ]
}

sleep "$GRACE_S"
deadline=$(( $(date -u +%s) + WAIT_S ))
today="$(date -u +%F)"

while running nightly-finals || running weekly-refresh; do
  if [ "$(date -u +%s)" -ge "$deadline" ]; then
    log "an ingest job still runs after ${WAIT_S}s; the rebuild goes on"
    exit 0
  fi
  log "an ingest job is running; waiting ${POLL_S}s"
  sleep "$POLL_S"
done

last="$(cat "$MARK_DIR/nightly-finals.last" 2>/dev/null || true)"
if [ "${last%% *}" = "$today" ]; then
  log "the finals load ran today (${last}); the rebuild goes on"
  exit 0
fi

log "no finals load today (last: ${last:-none}); running it first"
# shellcheck disable=SC2086 -- FINALS_CMD is a command line on purpose
$FINALS_CMD || log "the finals load exited non-zero; the rebuild goes on"
exit 0
