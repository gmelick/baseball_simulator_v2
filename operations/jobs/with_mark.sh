#!/usr/bin/env sh
# =============================================================================
# operations/jobs/with_mark.sh — mark a job as running, run it, record its exit.
#
#   sh /app/operations/jobs/with_mark.sh <name> <command...>
#
# While the command runs, $INGEST_MARK_DIR/<name>.running holds its start
# epoch. When it ends, the mark goes and <name>.last holds one line:
#   <UTC date> <exit code> <UTC timestamp>
# The mark spans every attempt of a with_retry.sh inside it. The other jobs
# read the marks to order themselves: the rebuild waits for the finals load,
# and the data backup waits for the rebuild.
#
# The marks live in /data/ingest_marks by default. When that folder cannot be
# written, the command still runs, unmarked.
# =============================================================================
set -u

if [ "$#" -lt 2 ]; then
  echo "usage: with_mark.sh <name> <command...>" >&2
  exit 2
fi
NAME="$1"
shift
MARK_DIR="${INGEST_MARK_DIR:-/data/ingest_marks}"

marked=0
if mkdir -p "$MARK_DIR" 2>/dev/null && [ -w "$MARK_DIR" ]; then
  marked=1
  date -u +%s > "$MARK_DIR/$NAME.running"
else
  echo "[with-mark] cannot write $MARK_DIR; running $NAME unmarked" >&2
fi

"$@"
code=$?

if [ "$marked" -eq 1 ]; then
  echo "$(date -u +%F) $code $(date -u +%FT%TZ)" > "$MARK_DIR/$NAME.last"
  rm -f "$MARK_DIR/$NAME.running"
fi
exit "$code"
