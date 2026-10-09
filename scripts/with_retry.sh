#!/usr/bin/env sh
# =============================================================================
# scripts/with_retry.sh — run a command again after a crash (SIM-519 Part D).
#
#   sh scripts/with_retry.sh <max_attempts> <command...>
#
# The app image's Python dies now and then with a segmentation fault (the
# SIM-445 crash class, CLAUDE.md §2a). The loaders are idempotent per game, so a
# rerun resumes where the crash stopped. The wrapper reruns the command after a
# crash, up to <max_attempts> attempts, 30 seconds apart:
#
#   - exit 139 (SIGSEGV), or any non-zero exit whose output carries
#     "Fatal Python error"  -> a crash: wait and run again;
#   - any other non-zero exit -> a real failure: stop at once with that code;
#   - exit 0                  -> done.
#
# RETRY_SLEEP_S overrides the 30-second wait (the unit test sets 0).
# =============================================================================
set -u

if [ "$#" -lt 2 ]; then
  echo "usage: with_retry.sh <max_attempts> <command...>" >&2
  exit 2
fi
MAX="$1"
shift
SLEEP_S="${RETRY_SLEEP_S:-30}"
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

attempt=1
while :; do
  echo "[with-retry] attempt ${attempt}/${MAX}: $*"
  # Keep the command's output on screen and in a file the crash check reads.
  # The exit code of the command, not of tee, is the one that counts.
  { "$@" 2>&1; echo $? > "$LOG.code"; } | tee "$LOG"
  code="$(cat "$LOG.code")"
  rm -f "$LOG.code"
  echo "[with-retry] attempt ${attempt} exited ${code}"
  if [ "$code" -eq 0 ]; then
    exit 0
  fi
  crashed=0
  if [ "$code" -eq 139 ] || grep -q "Fatal Python error" "$LOG"; then
    crashed=1
  fi
  if [ "$crashed" -eq 0 ]; then
    echo "[with-retry] not a crash; stopping with exit ${code}" >&2
    exit "$code"
  fi
  if [ "$attempt" -ge "$MAX" ]; then
    echo "[with-retry] crashed ${attempt} times; giving up with exit ${code}" >&2
    exit "$code"
  fi
  attempt=$((attempt + 1))
  sleep "$SLEEP_S"
done
