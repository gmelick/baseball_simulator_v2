#!/usr/bin/env sh
# =============================================================================
# SIM-525: the nightly Postgres backup (the scheduled backups of the data stores).
#
# The script dumps the database in directory format, the only pg_dump format
# that restores in parallel. It runs inside the db container as the postgres
# OS user and connects over the unix socket as POSTGRES_USER, so it needs no
# password. It never echoes a command (no shell trace).
#
# One run writes one set:
#
#   $BACKUP_ROOT/postgres/<TS>/
#       dump/              pg_dump -Fd (toc.dat + one .dat.gz per table)
#       counts_before.tsv  'schema.table<TAB>count', taken just before the dump
#       counts_after.tsv   the same, taken just after the dump
#       manifest.json      written LAST; a set without it is not complete
#
# TS is the UTC start time, %Y%m%dT%H%M%SZ. The set is built as <TS>.partial
# and renamed only after every file and the manifest are synced. The next run
# deletes every '*.partial' it finds. The live service may write during the
# dump, so the restore checks each table's count against the two counts.
#
# Callers:
#   Ofelia, nightly at 10:00 UTC:  sh /opt/baseball/backup/pg_backup.sh
#   By hand:                       make backup
#
# Exit codes: 0 the set is complete; 1 the backup failed (nothing is pruned);
# 2 a refusal before any write (the root is missing or not writable, a bad
# setting, not enough free space, or another backup run holds the lock).
#
# The lock: one run at a time. The run creates the folder
# $BACKUP_ROOT/postgres/.lock (mkdir is atomic) and writes its pid and start
# epoch into .lock/owner. A second run (say `make backup` during the 10:00
# job) refuses with exit 2 and deletes nothing. A lock is stale when its pid
# is gone or it is older than BACKUP_LOCK_STALE_S; the next run removes it.
#
# Settings (environment, with defaults):
#   BACKUP_ROOT=/backups  BACKUP_KEEP=3  PG_DUMP_JOBS=4  PG_DUMP_COMPRESS=3
#   POSTGRES_USER=baseball_user  POSTGRES_DB=baseball_sim
#   BACKUP_MIN_FREE_GB=10  BACKUP_FREE_FACTOR_TENTHS=15 (1.5 x the newest set)
#   BACKUP_LOCK_STALE_S=21600 (6 h)
# =============================================================================
set -eu

root="${BACKUP_ROOT:-/backups}"
keep="${BACKUP_KEEP:-3}"
dump_jobs="${PG_DUMP_JOBS:-4}"
dump_compress="${PG_DUMP_COMPRESS:-3}"
pg_user="${POSTGRES_USER:-baseball_user}"
pg_db="${POSTGRES_DB:-baseball_sim}"
min_free_gb="${BACKUP_MIN_FREE_GB:-10}"
free_factor_tenths="${BACKUP_FREE_FACTOR_TENTHS:-15}"
lock_stale_s="${BACKUP_LOCK_STALE_S:-21600}"

log() { echo "[pg-backup $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
err() { echo "[pg-backup $(date -u '+%Y-%m-%dT%H:%M:%SZ')] ERROR: $*" >&2; }
fail() { err "$*"; exit 1; }
refuse() { err "$*"; exit 2; }

usage() {
    echo "usage: sh pg_backup.sh   (settings come from the environment; see the header)"
}

case "${1:-}" in
    "") ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

# Each numeric setting must be a whole number.
for pair in "BACKUP_KEEP=$keep" "PG_DUMP_JOBS=$dump_jobs" "PG_DUMP_COMPRESS=$dump_compress" \
    "BACKUP_MIN_FREE_GB=$min_free_gb" "BACKUP_FREE_FACTOR_TENTHS=$free_factor_tenths"     "BACKUP_LOCK_STALE_S=$lock_stale_s"; do
    case "${pair#*=}" in
        "" | *[!0-9]*) refuse "${pair%%=*} must be a whole number, not '${pair#*=}'" ;;
    esac
done
[ "$keep" -ge 1 ] || refuse "BACKUP_KEEP must be at least 1"

psql_q() {
    psql -q -X --no-password -v ON_ERROR_STOP=1 -At -U "$pg_user" -d "$pg_db" "$@"
}

# JSON string body: escape the backslash and the double quote.
json_esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# The complete sets of the store, oldest first (the TS names sort by time).
complete_sets() {
    for d in "$store"/*; do
        [ -d "$d" ] || continue
        name="${d##*/}"
        case "$name" in *.partial) continue ;; esac
        [ -f "$d/manifest.json" ] && echo "$name"
    done | LC_ALL=C sort
}

# The bytes a set's manifest records; the folder's size when it records none.
set_bytes() {
    b=$(sed -n 's/^[[:space:]]*"bytes":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$1/manifest.json" | head -n 1)
    if [ -z "$b" ]; then
        b=$(( $(du -sk "$1" | awk '{print $1}') * 1024 ))
    fi
    echo "$b"
}

# Flush every file and folder of one tree to disk. A bare `sync` flushes every
# filesystem the container sees and can stall for minutes on a bind mount, so
# the script names the files (`sync FILE...` fsyncs each one).
sync_tree() { find "$1" -exec sync {} +; }

# The free KiB on the backup root (the bind mount reports the host disk).
free_kb() { df -Pk "$root" | awk 'NR == 2 {print $4}'; }

TABLES_SQL="select n.nspname || '.' || c.relname
from pg_catalog.pg_class c
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where c.relkind = 'r' and n.nspname in ('raw', 'sim', 'public')
order by 1"

# Write 'schema.table<TAB>count' for every table of raw, sim and public, sorted.
take_counts() {
    out="$1"
    tables=$(psql_q -c "$TABLES_SQL") || fail "cannot list the tables"
    : > "$out.unsorted"
    for t in $tables; do
        schema="${t%%.*}"
        rel="${t#*.}"
        n=$(psql_q -c "select count(*) from \"$schema\".\"$rel\"") || fail "cannot count $t"
        printf '%s\t%s\n' "$t" "$n" >> "$out.unsorted"
    done
    LC_ALL=C sort "$out.unsorted" > "$out"
    rm -f "$out.unsorted"
}

# --- 1. The root must exist and be writable. ---------------------------------
if [ ! -d "$root" ] || [ ! -w "$root" ]; then
    refuse "the backup root $root is missing or not writable"
fi
store="$root/postgres"
mkdir -p "$store" || refuse "cannot create $store"

# --- 1b. The lock: one backup run at a time. ----------------------------------
lock="$store/.lock"
take_lock() {
    mkdir "$lock" 2>/dev/null || return 1
    printf '%s %s\n' "$$" "$(date -u '+%s')" > "$lock/owner"
}
if ! take_lock; then
    owner_pid=""
    owner_epoch=""
    if [ -f "$lock/owner" ]; then
        read -r owner_pid owner_epoch < "$lock/owner" || :
    fi
    case "$owner_epoch" in "" | *[!0-9]*) owner_epoch=0 ;; esac
    age_s=$(( $(date -u '+%s') - owner_epoch ))
    alive=0
    case "$owner_pid" in
        "" | *[!0-9]*) ;;
        *) if kill -0 "$owner_pid" 2>/dev/null; then alive=1; fi ;;
    esac
    if [ "$alive" = "1" ] && [ "$age_s" -lt "$lock_stale_s" ]; then
        refuse "another backup run (pid $owner_pid, ${age_s} s old) holds $lock; nothing deleted"
    fi
    log "removing a stale lock (pid ${owner_pid:-unknown}, ${age_s} s old)"
    rm -rf "$lock"
    take_lock || refuse "another backup run took $lock; nothing deleted"
fi
trap 'rm -rf "$lock"' EXIT
trap 'exit 1' INT TERM HUP

# --- 2. Delete the sets a dead run left behind. ------------------------------
for d in "$store"/*.partial; do
    [ -e "$d" ] || continue
    log "deleting the incomplete set ${d##*/}"
    rm -rf "$d"
done

# --- 3. The free-space guard. -------------------------------------------------
while :; do
    newest=$(complete_sets | tail -n 1)
    newest_bytes=0
    if [ -n "$newest" ]; then
        newest_bytes=$(set_bytes "$store/$newest")
    fi
    need_kb=$(( min_free_gb * 1048576 ))
    factor_kb=$(( newest_bytes * free_factor_tenths / 10 / 1024 ))
    if [ "$factor_kb" -gt "$need_kb" ]; then
        need_kb="$factor_kb"
    fi
    have_kb=$(free_kb) || refuse "cannot read the free space on $root"
    case "$have_kb" in
        "" | *[!0-9]*) refuse "cannot read the free space on $root" ;;
    esac
    if [ "$have_kb" -ge "$need_kb" ]; then
        break
    fi
    count=$(complete_sets | wc -l)
    if [ "$count" -gt 2 ]; then
        oldest=$(complete_sets | head -n 1)
        log "free space ${have_kb} KiB is under ${need_kb} KiB: deleting the oldest set $oldest"
        rm -rf "${store:?}/$oldest"
        continue
    fi
    refuse "free space ${have_kb} KiB on $root is under the ${need_kb} KiB the guard needs"
done

# --- 4. Dump. -----------------------------------------------------------------
start_s=$(date -u '+%s')
ts=$(date -u '+%Y%m%dT%H%M%SZ')
final="$store/$ts"
partial="$final.partial"
[ ! -e "$final" ] || fail "the set $ts exists already"
mkdir -p "$partial" || fail "cannot create $partial"
log "start: database $pg_db, set $ts, $dump_jobs jobs, compression $dump_compress"

server_version=$(psql_q -c "show server_version") || fail "cannot read the server version"
client_version=$(pg_dump --version) || fail "cannot read the pg_dump version"

take_counts "$partial/counts_before.tsv"
log "counted $(wc -l < "$partial/counts_before.tsv" | tr -d ' ') tables before the dump"

pg_dump -Fd -j "$dump_jobs" -Z "$dump_compress" -U "$pg_user" -d "$pg_db" -f "$partial/dump" \
    || fail "pg_dump failed; the set stays incomplete"
pg_restore --list "$partial/dump" > /dev/null || fail "pg_restore --list cannot read the dump"
log "the dump reads back (pg_restore --list)"

take_counts "$partial/counts_after.tsv"
alembic_head=$(psql_q -c "select version_num from public.alembic_version") \
    || fail "cannot read the alembic head"

# --- 5. The manifest, written last. -------------------------------------------
files_list="$partial/.files.list"
files_part="$partial/.files.part"
(cd "$partial" && find . -type f ! -name manifest.json ! -name 'manifest.json.tmp' \
    ! -name '.files.*' | sed 's|^\./||' | LC_ALL=C sort) > "$files_list" \
    || fail "cannot list the set's files"
: > "$files_part"
total=0
n_files=0
while IFS= read -r p; do
    [ -n "$p" ] || continue
    # stat, never wc: busybox wc reads the whole file in small chunks, and over
    # the Docker Desktop bind mount a gigabyte then takes minutes (measured
    # 2026-10-15: 53 s for 94 MB; sha256sum reads the same file in 2.7 s).
    size=$(stat -c %s "$partial/$p") || fail "cannot size $p"
    sum=$(sha256sum "$partial/$p" | awk '{print $1}') || fail "cannot hash $p"
    total=$(( total + size ))
    if [ "$n_files" -gt 0 ]; then
        printf ',\n' >> "$files_part"
    fi
    printf '    {"path": "%s", "bytes": %s, "sha256": "%s"}' \
        "$(json_esc "$p")" "$size" "$sum" >> "$files_part"
    n_files=$(( n_files + 1 ))
done < "$files_list"

sync_tree "$partial" || fail "cannot sync the set to disk"
seconds=$(( $(date -u '+%s') - start_s ))
created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
host=$(uname -n)
{
    printf '{\n'
    printf '  "store": "postgres",\n'
    printf '  "set": "%s",\n' "$ts"
    printf '  "created_at": "%s",\n' "$created_at"
    printf '  "seconds": %s.0,\n' "$seconds"
    printf '  "bytes": %s,\n' "$total"
    printf '  "host": "%s",\n' "$(json_esc "$host")"
    printf '  "tool": "db/backup/pg_backup.sh",\n'
    printf '  "database": "%s",\n' "$(json_esc "$pg_db")"
    printf '  "server_version": "%s",\n' "$(json_esc "$server_version")"
    printf '  "client_version": "%s",\n' "$(json_esc "$client_version")"
    printf '  "format": "directory",\n'
    printf '  "jobs": %s,\n' "$dump_jobs"
    printf '  "compress": %s,\n' "$dump_compress"
    printf '  "alembic_head": "%s",\n' "$(json_esc "$alembic_head")"
    printf '  "pg_restore_list_ok": true,\n'
    printf '  "counts_files": ["counts_before.tsv", "counts_after.tsv"],\n'
    printf '  "files": [\n'
    cat "$files_part"
    printf '\n  ]\n}\n'
} > "$partial/manifest.json.tmp" || fail "cannot write the manifest"
rm -f "$files_list" "$files_part"
mv "$partial/manifest.json.tmp" "$partial/manifest.json" || fail "cannot write the manifest"
sync_tree "$partial" || fail "cannot sync the manifest to disk"
mv "$partial" "$final" || fail "cannot rename $partial"
sync "$store" || fail "cannot sync $store"

# --- 6. Retention: keep the newest BACKUP_KEEP complete sets. -----------------
count=$(complete_sets | wc -l)
excess=$(( count - keep ))
if [ "$excess" -gt 0 ]; then
    for old in $(complete_sets | head -n "$excess"); do
        [ "$old" = "$ts" ] && continue
        log "retention: deleting the set $old"
        rm -rf "${store:?}/$old"
    done
fi

echo "BACKUP OK postgres $ts $total bytes $seconds s"
