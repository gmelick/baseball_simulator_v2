#!/usr/bin/env sh
# =============================================================================
# operations/jobs/with_retry.sh — run a command again after a crash.
#
#   sh operations/jobs/with_retry.sh <max_attempts> <command...>
#
# The app image's Python dies now and then with a segmentation fault
# (CLAUDE.md section 2a). The loaders skip what they already loaded, so a rerun
# resumes. The wrapper reruns the command up to <max_attempts> times, 30 s apart:
#
#   - exit 139, or a non-zero exit with "Fatal Python error" in the output:
#     a crash; wait and run again;
#   - any other non-zero exit: a real failure; stop at once with that code;
#   - exit 0: done.
#
# RETRY_SLEEP_S overrides the 30-second wait.
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
  # tee keeps the output on screen and in the file the crash check reads.
  # The file .code keeps the command's exit code, not tee's.
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
