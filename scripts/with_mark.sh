#!/usr/bin/env sh
# =============================================================================
# scripts/with_mark.sh — mark an ingest job as running, run it, record its exit.
#
#   sh /app/scripts/with_mark.sh <name> <command...>
#
# While the command runs, $INGEST_MARK_DIR/<name>.running holds its start time.
# When it ends, the mark is removed and <name>.last holds one line:
#   <UTC date> <exit code> <UTC timestamp>
# The mark spans the whole command, so it covers every attempt of a
# with_retry.sh wrapper inside it. scripts/wait_for_ingest.sh reads the marks:
# the nightly rebuild waits for the finals load before it reads Postgres.
# The scheduler marks the profile + bundle rebuild (nightly-rebuild) and the
# data backup (nightly-backup) too. The data backup waits for the
# rebuild's mark, so it never opens the DuckDB file while the rebuild writes it.
#
# The marks live on the /data volume (default /data/ingest_marks). When that
# folder cannot be written, the command still runs, unmarked.
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
