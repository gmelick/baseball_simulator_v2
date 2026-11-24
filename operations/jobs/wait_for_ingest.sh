#!/usr/bin/env sh
# =============================================================================
# operations/jobs/wait_for_ingest.sh — make sure the finals load ran today before the rebuild.
#
# The finals load (07:00 UTC) and the rebuild (08:00 UTC) rely on the hour
# between them. A host that sleeps through both fires both in the same second
# on wake, and nothing else orders them.
#
# Reading the marks that with_mark.sh writes, the script:
#   1. waits INGEST_GRACE_S (60 s), so a job fired in the same second can
#      write its mark;
#   2. waits while the finals load or the Sunday sweep runs (at most
#      INGEST_WAIT_S, 2 hours; a mark older than 6 hours is stale);
#   3. lets the rebuild go on when the finals load finished today (UTC),
#      whatever its exit code;
#   4. otherwise runs the finals load first.
# It never fails the rebuild: on a timeout it logs and exits 0.
# =============================================================================
set -u

MARK_DIR="${INGEST_MARK_DIR:-/data/ingest_marks}"
GRACE_S="${INGEST_GRACE_S:-60}"
WAIT_S="${INGEST_WAIT_S:-7200}"
POLL_S="${INGEST_POLL_S:-30}"
STALE_S="${INGEST_STALE_S:-21600}"
FINALS_CMD="${INGEST_FINALS_CMD:-sh /app/operations/jobs/with_mark.sh nightly-finals sh /app/operations/jobs/with_retry.sh 6 sh /app/operations/jobs/nightly_finals.sh}"

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
