# Operations

The `operations/` folder keeps the stack running. It holds the nightly shell jobs and
their wrappers, the backups of the three data stores with their off-site copy, and the
secrets generator. No file here serves a user request; the API imports only
`operations.backup.backup_status`, for its backup-age gauges. The scheduler, the `make`
targets and the operator run the rest.

| Part | Folder |
|---|---|
| The nightly shell jobs and their wrappers | [`operations/jobs/`](../../operations/jobs/) |
| The backups, the off-site copy and the restore drill | [`operations/backup/`](../../operations/backup/) |
| The two Postgres backup scripts the `db` container runs | [`operations/backup/postgres/`](../../operations/backup/postgres/) |
| The secrets generator | [`operations/gen_secrets.py`](../../operations/gen_secrets.py) |

The run book (how to run, check and recover each job) is the operator's manual,
[WORKFLOW.md](../../WORKFLOW.md). The code the jobs call is on
[Nightly builds](pipeline-batch.md) and [Data ingestion](pipeline-ingest.md).

## How it fits together

**Where each file runs.**

1. **The `jobs` container.** The scheduler, Ofelia (a cron for Docker containers), runs
   the shell jobs and the two Python backup jobs there as `appuser`. Its jobs live in
   [`deploy/ofelia/config.ini`](../../deploy/ofelia/config.ini).
2. **The `app` container.** The nightly odds pass runs there, because only `app` reads the
   vendor key from `.env`. `make ingest-catch-up` also starts a fresh `app` container.
3. **The `db` container.** The Postgres backup and restore scripts run there as the
   `postgres` OS user. Compose mounts `operations/backup/postgres/` at
   `/opt/baseball/backup`.
4. **The host.** The restore drill and the secrets generator run with the Windows `py`
   launcher. Both use the standard library only.

**The mount.** [docker-compose.yml](../../docker-compose.yml) mounts `./operations` at
`/app/operations` in the `app`, `live` and `jobs` services. An edit to a job or a backup
module reaches those containers without an image rebuild. A scheduled job runs the
mounted copy at its next start.

**The nightly order (UTC).** Ofelia has no job dependencies, so the jobs order themselves
through marks. Every scheduled job in `jobs` runs inside `with_mark.sh`. That wrapper
writes `/data/ingest_marks/<job>.running` while the job runs, and `<job>.last` when it
ends. `with_retry.sh` reruns a job after a crash.

1. **07:00** — `nightly-finals` runs `nightly_finals.sh`: the last three days' Final games
   into Postgres.
2. **08:00** — `nightly-rebuild` runs `nightly_rebuild.sh`. It waits for the finals load
   (`wait_for_ingest.sh`), rebuilds the current season's profiles and pools, then builds
   and publishes a new engine-artifact bundle. The app sees the new bundle and restarts
   itself.
3. **08:00 on Sunday** — `weekly-refresh` runs `weekly_refresh.sh`: a sweep of the whole
   current season for any Final game the three-day window missed.
4. **10:00** — the backups. `nightly-backup-postgres` runs
   `operations/backup/postgres/pg_backup.sh` in the `db` container. `nightly-backup-data`
   runs `python -m operations.backup.back_up_data_stores` in `jobs`; it waits while the
   rebuild, the finals load or the Sunday sweep runs.
5. **10:30** — `nightly-backup-offsite` runs `python -m operations.backup.copy_offsite`. It
   waits for both backups, then copies the newest complete sets into the OneDrive folder.
   `nightly-closing-lines` runs `nightly_closing_lines.sh` in the `app` container.

`nightly_ingest.sh` runs the finals sweep, the profile build and the bundle build in one
command, for a run by hand. The scheduler does not use it.

**The backup sets.** Three stores are backed up nightly: the Postgres statistics database,
the DuckDB analytics file and the bundle. A *set* is one backup of one store: a folder
named by the run's UTC start time (`%Y%m%dT%H%M%SZ`). A set is complete only when its
`manifest.json` exists; the writer writes the manifest last. The sets land in `/backups`,
the host folder `BACKUP_DIR`. The off-site copies land in `/offsite`, the host folder
`OFFSITE_BACKUP_DIR` (a OneDrive folder).

**The `make` targets.**

| Target | What it runs |
|---|---|
| `make ingest-catch-up DAYS=14` | `nightly_finals.sh` in a fresh `app` container, inside `with_mark.sh nightly-finals` and `with_retry.sh 6` |
| `make backup` | `pg_backup.sh` in `db`, then `back_up_data_stores --new-set` in `jobs` inside `with_mark.sh nightly-backup` |
| `make backup-offsite` | `copy_offsite` in `jobs`, inside `with_mark.sh nightly-backup-offsite`; it refuses while another copy's mark is under 6 hours old |
| `make backup-status` | `back_up_data_stores --status` and `copy_offsite --status` in `jobs` |
| `make backup-drill` | `backup_drill.py` on the host (`SET=<ts>` picks a set; `OFFSITE=1` restores from the off-site copy) |
| `make restore-postgres SET=<ts>` | `pg_restore.sh` in `db` |
| `make gen-secrets` | `gen_secrets.py --append .env` on the host |

Run `make backup`, `make backup-offsite` and `make backup-status` from cmd.exe. Git Bash
rewrites the container paths in their `docker compose exec` lines.

---

## The nightly shell jobs and their wrappers

All of these are POSIX `sh` scripts that run in the Debian app image. Each job stops at
once without `BASEBALL_DB_DSN`. [test_shell_dsn_guards.py](../../tests/unit/operations/test_shell_dsn_guards.py)
and [test_no_default_password.py](../../tests/unit/test_no_default_password.py) hold that rule.

### `operations/jobs/nightly_finals.sh`

[Source](../../operations/jobs/nightly_finals.sh) · 51 lines · Tests: [test_shell_dsn_guards.py](../../tests/unit/operations/test_shell_dsn_guards.py), [test_no_default_password.py](../../tests/unit/test_no_default_password.py)

Loads every game that became Final from today minus `DAYS` (default 3) to today, with
"today" in Eastern time, the league's day. It calls `HistoricalDataLoader.load_date_range`,
which skips games already loaded. It writes Postgres only, so it runs while the app
serves. It exits 1 when any game failed.

Run: the scheduler at 07:00 UTC, as `with_mark.sh nightly-finals with_retry.sh 6 nightly_finals.sh`. By hand: `make ingest-catch-up DAYS=14`.

### `operations/jobs/weekly_refresh.sh`

[Source](../../operations/jobs/weekly_refresh.sh) · 29 lines · Tests: [test_shell_dsn_guards.py](../../tests/unit/operations/test_shell_dsn_guards.py), [test_no_default_password.py](../../tests/unit/test_no_default_password.py)

Calls `HistoricalDataLoader.refresh_seasons(year, year)` on the current season. It catches
a Final game that the three-day window missed. It writes Postgres only. It exits 1 when
any game failed.

Run: the scheduler on Sunday at 08:00 UTC, through `with_mark.sh weekly-refresh` and `with_retry.sh 6`.

### `operations/jobs/nightly_rebuild.sh`

[Source](../../operations/jobs/nightly_rebuild.sh) · 35 lines · Tests: [test_ingest_wait.py](../../tests/unit/operations/test_ingest_wait.py), [test_backup_schedule.py](../../tests/unit/operations/test_backup_schedule.py), [test_shell_dsn_guards.py](../../tests/unit/operations/test_shell_dsn_guards.py)

Rebuilds the current season's profiles and the engine-artifact bundle, in three steps:

1. `wait_for_ingest.sh`.
2. `python -m pipeline.batch.player_profile_computor --seasons <year>`.
3. `python -m pipeline.batch.engine_artifacts --what all`.

The profile step writes DuckDB and needs its write lock; the bundle step opens it
read-only. The app holds no handle on that file, so the job runs beside the app. The
bundle step publishes a new bundle version, and the app restarts itself on the new
version (`api/bundle_watch.py`).

Run: the scheduler at 08:00 UTC, through `with_mark.sh nightly-rebuild` and `with_retry.sh 3`. By hand: `docker compose run --rm app sh /app/operations/jobs/nightly_rebuild.sh`.

### `operations/jobs/nightly_ingest.sh`

[Source](../../operations/jobs/nightly_ingest.sh) · 75 lines · Tests: [test_etl_hardening.py](../../tests/unit/test_etl_hardening.py), [test_shell_dsn_guards.py](../../tests/unit/operations/test_shell_dsn_guards.py)

The whole chain in one run by hand: `refresh_seasons` for the current season, then the
profile computor, then the bundle build. It stops before the rebuild when any game
failed, so it never rebuilds on a season it knows is incomplete. The scheduler does not
run this script; it runs the three jobs above instead.

Run: `docker compose run --rm app sh /app/operations/jobs/nightly_ingest.sh`.

**Watch out:** the Python heredoc's delimiter is quoted (`'PY'`), so the shell does not
expand `${YEAR}` inside it. The year crosses into Python through the environment
(`os.environ["YEAR"]`).

### `operations/jobs/nightly_closing_lines.sh`

[Source](../../operations/jobs/nightly_closing_lines.sh) · 64 lines · Tests: [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_shell_dsn_guards.py](../../tests/unit/operations/test_shell_dsn_guards.py)

The nightly odds pass. It runs the historical odds loader,
`python -m betting.historical_loader --line-types opening closing`, over the games of
yesterday and the day before. The dates are UTC dates, so the pass does not miss a late
West Coast game. A closing row is the last price a book posted before first pitch. The
live service stamps each game's closing rows at first pitch; this pass is the fallback
and the reconciliation. The live service stores no opening row, so this pass is the only
source of an opening line: BettingPros returns each market's opener with every offer.
Without `ODDS_API_KEY`, the script exits 1 before it loads anything.

Run: the scheduler at 10:30 UTC, inside the running `app` container (`sh /app/operations/jobs/nightly_closing_lines.sh`). The `app` container reads `ODDS_API_KEY` from `.env`. The loader is on [the betting page](betting.md#bettinghistorical_loaderpy).

### `operations/jobs/with_retry.sh`

[Source](../../operations/jobs/with_retry.sh) · 57 lines · Tests: [test_retry_wrapper.py](../../tests/unit/operations/test_retry_wrapper.py), [test_backup_schedule.py](../../tests/unit/operations/test_backup_schedule.py)

`with_retry.sh <max_attempts> <command...>` runs the command again after a crash, up to
`max_attempts` times, 30 seconds apart (`RETRY_SLEEP_S`). A crash is exit 139 (a
segmentation fault) or output that holds "Fatal Python error". That is the stochastic
interpreter crash of the app image (the SIM-445 crash class, CLAUDE.md §2a). Any other
non-zero exit stops the wrapper at once with that code. The loaders are idempotent per
game, so a rerun resumes where the crash stopped.

### `operations/jobs/with_mark.sh`

[Source](../../operations/jobs/with_mark.sh) · 45 lines · Tests: [test_ingest_wait.py](../../tests/unit/operations/test_ingest_wait.py), [test_backup_schedule.py](../../tests/unit/operations/test_backup_schedule.py)

`with_mark.sh <name> <command...>` writes `$INGEST_MARK_DIR/<name>.running` (default
`/data/ingest_marks`) with the start time. It runs the command, then removes the mark and
writes `<name>.last` as one line: the UTC date, the exit code and the UTC timestamp. The
mark spans every attempt of a `with_retry.sh` inside it. When the folder cannot be
written, the command still runs, unmarked.

### `operations/jobs/wait_for_ingest.sh`

[Source](../../operations/jobs/wait_for_ingest.sh) · 62 lines · Tests: [test_ingest_wait.py](../../tests/unit/operations/test_ingest_wait.py)

The rebuild's first step. It makes sure the finals load ran today before the rebuild
reads Postgres. It waits `INGEST_GRACE_S` (60 s), so a job fired in the same second can
write its mark. Then it waits while a fresh `nightly-finals` or `weekly-refresh` mark
exists: at most `INGEST_WAIT_S` (2 hours), and a mark older than 6 hours is stale. When
`nightly-finals.last` shows today, the rebuild goes on. When it does not, the script runs
the finals load first. It never fails the rebuild.

---

## The backups

### `operations/backup/__init__.py`

[Source](../../operations/backup/__init__.py) · 16 lines · Tests: none

The package docstring: which module does what.

### `operations/backup/backup_status.py`

[Source](../../operations/backup/backup_status.py) · 121 lines · Tests: [test_backup_status.py](../../tests/unit/operations/test_backup_status.py), [test_backup_age_gauge.py](../../tests/unit/operations/test_backup_age_gauge.py)

Which sets exist and how old they are. It is pure standard library, because the API
imports it.

| Function / class | What it does | Called from |
|---|---|---|
| `SetInfo` | One complete set: store, set id, path, creation time, bytes. | the backup modules |
| `complete_sets` / `newest_complete_set` / `newest_complete_sets` | Every complete set of a store (oldest first); the newest; the newest of each store (`postgres`, `duckdb`, `bundle`). | `back_up_data_stores`, `copy_offsite`, `api/routes/metrics.py` |
| `age_hours` | Hours since a set's creation; −1.0 when there is none. | `api/routes/metrics.py` (the gauges `baseball_sim_backup_age_hours` and `baseball_sim_offsite_backup_age_hours`) |

### `operations/backup/common.py`

[Source](../../operations/backup/common.py) · 91 lines · Tests: [test_backup_offsite.py](../../tests/unit/operations/test_backup_offsite.py)

The helpers both backup jobs share, pure standard library: `env_float`, `human_bytes`,
`sha256_file` (in 8 MiB reads), `fsync_path`, `copy_file_synced`, and `fresh_marks` (the
jobs whose `.running` mark exists and is younger than the stale limit).

### `operations/backup/back_up_data_stores.py`

[Source](../../operations/backup/back_up_data_stores.py) · 915 lines · Tests: [test_backup_data_stores.py](../../tests/unit/operations/test_backup_data_stores.py), [test_backup_schedule.py](../../tests/unit/operations/test_backup_schedule.py), [test_backup_drill.py](../../tests/unit/operations/test_backup_drill.py)

Backs up, verifies and restores the DuckDB file and the bundle. The module docstring is
the design. Exit codes: 0 every store succeeded; 1 a store failed (the job still tried
the other); 2 a refusal before any write.

| Function / class | What it does | Called from |
|---|---|---|
| `run_backup` | Refuses when the backup root is missing or read-only; deletes dead `*.partial` sets; checks free space; waits for the writer jobs; backs up each store under one set id; prunes each store to `BACKUP_KEEP` (3) sets only when every store succeeded. A crash retry within an hour skips a store that already has its set (`recent_sets`); `--new-set` turns that off. | `main` |
| `wait_for_marks` | Waits while the rebuild, the finals load or the Sunday sweep runs (their `.running` marks), up to two hours. | `run_backup` |
| `backup_duckdb` | Opens the file read-only, counts every table, copies the file and its `.wal` while it holds the handle, then opens the copy and requires the same counts. Adds the small files (`backup_small_files`: the calibration report, the prop validation, the `*.last` marks). | `run_backup` |
| `backup_bundle` | Copies the published version behind the link into `bundle/<set>/<build id>/`, without the in-use markers. | `run_backup` |
| `free_space_ok` | Needs max(`BACKUP_MIN_FREE_GB` 10 GiB, `BACKUP_FREE_FACTOR` 1.5 × the newest set) free. When short, deletes the oldest set while more than two remain, then refuses. | `run_backup` |
| `verify_set` / `check_hashes` | Checks every file's size and sha256, then the DuckDB copy's counts or a bundle load. | `main --verify-set`, `operations/backup/backup_drill.py` |
| `restore_duckdb` / `restore_bundle` | Puts a DuckDB set back with `os.replace`; copies a bundle set back as a new version and publishes it. Both need `--yes`. | `main` |
| `status_rows` / `print_status` | The newest set of each store, as text or JSON. | `main --status`, `make backup-status` |

### `operations/backup/copy_offsite.py`

[Source](../../operations/backup/copy_offsite.py) · 991 lines · Tests: [test_backup_offsite.py](../../tests/unit/operations/test_backup_offsite.py), [test_backup_schedule.py](../../tests/unit/operations/test_backup_schedule.py)

The off-site copy (SIM-568). It copies the newest complete sets of each store from
`/backups` to `/offsite`. The OneDrive client uploads them, and this module cannot see
that upload. Pure standard library. The exit codes are those of `back_up_data_stores`.

| Function / class | What it does | Called from |
|---|---|---|
| `run_copy` / `_copy_locked` | Refuses when a root is missing, the off-site root is read-only, or the two roots are one folder; takes the copy lock; waits for the backup jobs; checks free space once; per store, deletes dead off-site folders, copies each wanted set that is not mirrored (oldest first), then prunes. | `main` |
| `take_lock` / `lock_holder` / `release_lock` | One copy run at a time (`offsite-copy.lock` in the mark folder). | `run_copy` |
| `busy_reasons` / `wait_until_quiet` | The data job's `nightly-backup` mark and the Postgres job's lock; waits up to two hours, then copies anyway (a complete set is always safe to copy). | `_copy_locked` |
| `copy_set` | Copies one set file by file, the manifest last, with no `.partial` folder and no rename (OneDrive holds new files open). A rerun keeps each file that already matches. | `mirror_store` |
| `is_mirrored` | True when the off-site manifest equals the local one byte for byte and every listed file has its size. | `plan_store`, `status_rows` |
| `prune_offsite` | Keeps the newest `keep` complete off-site sets and never prunes a set the local side still wants. | `mirror_store` |
| `resolve_keep` | `--keep`, else `OFFSITE_KEEP`, else `BACKUP_KEEP`, else 3. | `main` |

**Watch out:** `backup_status.py`, `common.py` and `copy_offsite.py` import no duckdb, and
their docstrings forbid it: the API imports `backup_status.py`. The API reads the backup
root from `BACKUPS_ROOT`; the backup jobs read `BACKUP_ROOT`. Both default to `/backups`.

### `operations/backup/backup_drill.py`

[Source](../../operations/backup/backup_drill.py) · 535 lines · Tests: [test_backup_drill.py](../../tests/unit/operations/test_backup_drill.py), [test_backup_schedule.py](../../tests/unit/operations/test_backup_schedule.py)

The restore drill. It proves a backup set restores, without touching the live database:

1. It starts a scratch Postgres cluster with no network.
2. It runs the real `operations/backup/postgres/pg_restore.sh` against that cluster.
3. It verifies the DuckDB and bundle copies in the `jobs` container
   (`python -m operations.backup.back_up_data_stores --verify-set`).
4. It removes the scratch cluster, whatever happened.

It writes `<backup dir>/drills/<start>.json`. `--offsite` restores from the OneDrive copy
instead. It exits 0 on a pass, 1 when a step fails, 2 when it refuses to start. It uses
the standard library only. It needs about 20 GB free in Docker's disk image while it runs.

Run on the host: `make backup-drill` or `make backup-drill OFFSITE=1`.

### `operations/backup/postgres/`

[pg_backup.sh](../../operations/backup/postgres/pg_backup.sh) · 305 lines · [pg_restore.sh](../../operations/backup/postgres/pg_restore.sh) · 147 lines · Tests: [test_pg_backup_script.py](../../tests/unit/operations/test_pg_backup_script.py), [test_backup_drill.py](../../tests/unit/operations/test_backup_drill.py), [test_backup_schedule.py](../../tests/unit/operations/test_backup_schedule.py)

The two scripts run inside the `db` container as the `postgres` OS user over the unix
socket, so they need no password. Compose mounts this folder read-only at
`/opt/baseball/backup`.

| Script | What it does | Called from |
|---|---|---|
| `pg_backup.sh` | Dumps the database in directory format (`pg_dump -Fd`, the format that restores in parallel) into `$BACKUP_ROOT/postgres/<TS>/`, with the row count of every table just before and just after the dump. It builds the set as `<TS>.partial`, opens its permissions so the jobs container can read it, writes `manifest.json` last, then renames the folder. It keeps the newest `BACKUP_KEEP` (3) complete sets. A lock folder allows one run at a time; a free-space guard needs the larger of 10 GiB and 1.5 × the newest set. Exit 2 is a refusal before any write. | the Ofelia job `nightly-backup-postgres` at 10:00 UTC; `make backup` |
| `pg_restore.sh <set>` | Restores one set into an EMPTY database. It waits for the server, then drops the empty `raw` and `sim` schemas without CASCADE. A populated database fails the drop, and the script exits 3 before it restores anything. It runs `pg_restore` in parallel with `--exit-on-error`, then checks each table's count against the two counts in the set. | `make restore-postgres SET=<ts>`; the restore drill (`operations/backup/backup_drill.py`) |

**Watch out:** after a restore, run `make db-roles` (the grants), then
`docker compose run --rm migrate alembic current` (expect the head).

---

## The secrets generator

### `operations/gen_secrets.py`

[Source](../../operations/gen_secrets.py) · 208 lines · Tests: [test_gen_secrets.py](../../tests/unit/operations/test_gen_secrets.py)

Generates the stack's eight secrets: the three Postgres role passwords, the Redis
password, the Grafana admin password, the session-signing key, the login password and one
API key. Each value is `secrets.token_urlsafe(32)`. `--append .env` adds only the missing
or blank keys and never changes a value. `--check .env` exits 1 when a secret is missing.
It prints key names only, never a value. It uses the standard library only.

Run on the host: `make gen-secrets` (`py operations/gen_secrets.py --append .env`).

The code reads the database address through `db/dsn.py`, which carries no default
password; see [the database page](database.md#dbdsnpy).
