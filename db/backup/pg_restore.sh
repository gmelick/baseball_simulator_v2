#!/usr/bin/env sh
# =============================================================================
# SIM-525: restore one Postgres backup set into an EMPTY database.
#
#   sh /opt/baseball/backup/pg_restore.sh <set dir>
#
# The script runs as the postgres OS user over the unix socket, like
# pg_backup.sh. It never echoes a command (no shell trace). It takes three steps:
#
#   0. The connection probe: 'select 1', retried every 2 s for up to
#      PG_CONNECT_WAIT_S. A server that never answers exits 1 ('cannot
#      connect'), never 3: a dead or restarting server is not a full one.
#   1. The empty-target guard: 'DROP SCHEMA IF EXISTS raw' and '... sim',
#      WITHOUT CASCADE, in one psql call. The image's init pre-creates both
#      schemas empty, so on a fresh cluster the drop removes them and the dump
#      creates them again. On a populated database the drop fails with
#      'depend on', and the script refuses (exit 3) before it restores
#      anything. Any other failure of the drop exits 1.
#   2. pg_restore in parallel with --exit-on-error, so an error stops the
#      restore instead of being ignored.
#   3. The count check (COUNT_CHECK=1): the live count of every table in
#      counts_before.tsv must lie between its count before and after the dump.
#
# After a restore, the run book runs `make db-roles` (the grants) and
# `docker compose run --rm migrate alembic current` (expect the head).
#
# Exit codes: 0 restored and every count passes; 1 no connection, or the
# restore or a count failed; 2 a usage error or an incomplete set; 3 the target is not empty.
#
# Settings (environment, with defaults):
#   PG_RESTORE_JOBS=4  POSTGRES_USER=baseball_user  POSTGRES_DB=baseball_sim
#   COUNT_CHECK=1  PG_CONNECT_WAIT_S=30
# =============================================================================
set -eu

restore_jobs="${PG_RESTORE_JOBS:-4}"
pg_user="${POSTGRES_USER:-baseball_user}"
pg_db="${POSTGRES_DB:-baseball_sim}"
count_check="${COUNT_CHECK:-1}"
connect_wait_s="${PG_CONNECT_WAIT_S:-30}"

log() { echo "[pg-restore $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
err() { echo "[pg-restore $(date -u '+%Y-%m-%dT%H:%M:%SZ')] ERROR: $*" >&2; }

usage() {
    echo "usage: sh pg_restore.sh <set dir>   (a folder holding manifest.json and dump/)"
}

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    usage >&2
    exit 2
fi
case "$1" in
    -h | --help) usage; exit 0 ;;
esac
case "$restore_jobs" in
    "" | *[!0-9]*) err "PG_RESTORE_JOBS must be a whole number"; exit 2 ;;
esac
case "$connect_wait_s" in
    "" | *[!0-9]*) err "PG_CONNECT_WAIT_S must be a whole number"; exit 2 ;;
esac

set_dir="${1%/}"
if [ ! -f "$set_dir/manifest.json" ] || [ ! -f "$set_dir/dump/toc.dat" ]; then
    err "$set_dir is not a complete set (it needs manifest.json and dump/toc.dat)"
    exit 2
fi

psql_q() {
    psql -q -X --no-password -v ON_ERROR_STOP=1 -At -U "$pg_user" -d "$pg_db" "$@"
}

# --- 0. The connection probe. ---------------------------------------------------
waited_s=0
until probe_err=$(psql_q -c "select 1" 2>&1 >/dev/null); do
    if [ "$waited_s" -ge "$connect_wait_s" ]; then
        err "cannot connect to database $pg_db after ${waited_s} s; nothing restored: $probe_err"
        exit 1
    fi
    sleep 2
    waited_s=$(( waited_s + 2 ))
done

# --- 1. The empty-target guard. -----------------------------------------------
log "checking that database $pg_db is empty"
if ! drop_err=$(psql_q -c "DROP SCHEMA IF EXISTS raw; DROP SCHEMA IF EXISTS sim;" 2>&1 >/dev/null); then
    case "$drop_err" in
        *"depend on"*)
            err "target database is not empty: schema raw or sim holds objects; nothing restored"
            exit 3
            ;;
    esac
    err "the empty-target guard failed; nothing restored: $drop_err"
    exit 1
fi

# --- 2. Restore. ---------------------------------------------------------------
start_s=$(date -u '+%s')
log "restoring $set_dir with $restore_jobs jobs"
if ! pg_restore -j "$restore_jobs" --no-owner --no-acl --exit-on-error \
    -U "$pg_user" -d "$pg_db" "$set_dir/dump"; then
    err "pg_restore failed"
    exit 1
fi
log "pg_restore finished in $(( $(date -u '+%s') - start_s )) s"

# --- 3. The count check. -------------------------------------------------------
if [ "$count_check" = "1" ]; then
    before="$set_dir/counts_before.tsv"
    after="$set_dir/counts_after.tsv"
    if [ ! -f "$before" ] || [ ! -f "$after" ]; then
        err "the set has no counts files; set COUNT_CHECK=0 to skip the check"
        exit 1
    fi
    tab=$(printf '\t')
    # One line per table: name, count before, count after (before when absent).
    awk -F'\t' 'NR == FNR { a[$1] = $2; next }
        $1 != "" { print $1 "\t" $2 "\t" (($1 in a) ? a[$1] : $2) }' "$after" "$before" \
        | {
            fails=0
            while IFS="$tab" read -r t b a; do
                [ -n "$t" ] || continue
                if [ "$b" -le "$a" ]; then lo="$b"; hi="$a"; else lo="$a"; hi="$b"; fi
                schema="${t%%.*}"
                rel="${t#*.}"
                if ! live=$(psql_q -c "select count(*) from \"$schema\".\"$rel\""); then
                    echo "count $t live=unreadable bracket=[$lo,$hi] FAIL"
                    fails=$(( fails + 1 ))
                    continue
                fi
                if [ "$live" -ge "$lo" ] && [ "$live" -le "$hi" ]; then
                    echo "count $t live=$live bracket=[$lo,$hi] OK"
                else
                    echo "count $t live=$live bracket=[$lo,$hi] FAIL"
                    fails=$(( fails + 1 ))
                fi
            done
            if [ "$fails" -gt 0 ]; then
                err "$fails table(s) failed the count check"
                exit 1
            fi
        } || exit 1
else
    log "the count check is off (COUNT_CHECK=$count_check)"
fi

echo "RESTORE OK $set_dir"
