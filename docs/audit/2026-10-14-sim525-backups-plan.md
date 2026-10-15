# The scheduled backups of the three data stores (SIM-525): the design, the run books, the restore drill — 2026-10-14

**Status:** PLANNED and BUILT 2026-10-14 (branch `sim-525-backups`: commit `179120a` holds the code, and commit `9122237`
sizes the dump files with `stat`, §10). **DEPLOYED
2026-10-15 at 04:56 UTC:** the live stack runs the code, and the scheduler holds the two backup
jobs. The first backup ran by hand at 04:58 UTC. The restore drill ran the same day (§10). The
approved plan is `C:/Users/grego/.claude/plans/implement-sim-525-modular-bumblebee.md`. This
document is the design as built, the two run books and the run record.

Terms used in this document:

- A **store** is one of the three places that hold the platform's data: the Postgres database,
  the DuckDB analytics file and the engine-artifact bundle.
- A **set** is one complete backup of one store, taken at one time.
- **The Postgres job** is the scheduled backup of the Postgres store. **The data job** is the
  scheduled backup of the DuckDB store and the bundle store.
- `TS` is a set's id: the UTC time its run started, written `%Y%m%dT%H%M%SZ`
  (for example `20261015T045846Z`).

## 1. Why it matters

The platform keeps its data in three stores. Before this ticket, none of them had a backup.

| Store | Where it lives | Size on 2026-10-14 | Cost to rebuild |
|---|---|---|---|
| Postgres `baseball_sim` (the statistics database) | the volume `postgres_data`; the `raw` schema holds 17 GB (prop odds 8.8 GB, pitches 6.5 GB) | 18 GB | the 6-hour re-sweep of every season, then the odds reload, which takes days |
| The DuckDB analytics file `/data/baseball_sim.duckdb` (the profiles and the play pools) | the volume `duckdb_data` | 3.6 GiB | the 1.5-hour nightly recompute, which reads Postgres |
| The engine-artifact bundle `/data/play_pool/engine_artifacts` (a link to `engine_artifacts.versions/<build id>/`) | the volume `duckdb_data` | 561 MiB per version, three versions kept | the same recompute |

Every volume lives inside Docker Desktop's disk image on C:. That image filled to 100% once.
`make nuke`, a Docker Desktop reset, a corrupt disk image or a writer that dies in the middle
of a rebuild loses a store. The first three lose all three stores at once.

The laptop has one disk, C:. The backup folder is on that disk, outside Docker's disk image.
The sets survive the loss of the disk image. They do not survive the loss of the disk.

## 2. The owner's decisions

The owner took three decisions on 2026-10-14.

1. **The sets go to the host folder `C:/Users/grego/Documents/baseball_backups`.** The value is
   `BACKUP_DIR` in `.env`. The folder was created on 2026-10-15. It is outside Docker's disk
   image, on the laptop's only disk, C:.
2. **Each store keeps 3 daily sets** (`BACKUP_KEEP=3`).
3. **The documents say what the sets protect against, and what they do not.**

The sets protect against:

- the loss or the reset of Docker's disk image;
- `docker compose down -v` and `make nuke`, which delete the volumes;
- a bad migration or a bad rebuild;
- a writer that dies in the middle of a rebuild;
- an accidental deletion.

The sets do not protect against:

- the laptop's disk dying;
- the loss of the laptop.

An off-machine copy closes that gap. It is a follow-up ticket (§9).

## 3. The design

**One rule: each store backs up on its own, into its own folder, with its own manifest and its
own retention.** A failure in one store never spoils the set of another store.

### 3.1 The layout

```
BACKUP_DIR/
  postgres/<TS>/          dump/              pg_dump directory format (-Fd -j 4 -Z 3)
                          counts_before.tsv  'schema.table<TAB>count' just before the dump
                          counts_after.tsv   the same, just after the dump
                          manifest.json
  duckdb/<TS>/            baseball_sim.duckdb [+ baseball_sim.duckdb.wal]
                          data_files/        calibration.json, prop_validation.json,
                                             ingest_marks/*.last
                          manifest.json
  bundle/<TS>/            <build id>/        a copy of the published bundle version
                          manifest.json
  drills/<TS>.json        the restore drill reports
```

Each manifest records the store, the set id, the creation time, the seconds the run took, the
total bytes, the host and a size and a sha256 for every file of the set. The Postgres manifest
adds the server and client versions, the dump settings and the Alembic head. The DuckDB manifest
adds the DuckDB version, whether the WAL was copied, every table's count and the list of the
small files. The bundle manifest adds the build id and the time the version was published.

### 3.2 Where each part runs

The scheduler (Ofelia) runs two jobs at 10:00 UTC. Each job sets `no-overlap = true`, so a slow
run never starts twice.

- **The Postgres job** (`nightly-backup-postgres`) execs `sh /opt/baseball/backup/pg_backup.sh`
  into the `db` container as the `postgres` OS user. The script logs in over the unix socket.
  The socket is `trust`, so the job needs no password. The client is the server's own, so the
  two versions always match (15.18). Compose mounts `./db/backup` read-only at
  `/opt/baseball/backup`. The `db` service has no `env_file`, and an Ofelia exec passes no
  environment, so the `db` service passes `BACKUP_KEEP` and `BACKUP_MIN_FREE_GB` from `.env`
  itself.
- **The data job** (`nightly-backup-data`) execs
  `sh /app/scripts/with_mark.sh nightly-backup sh /app/scripts/with_retry.sh 2 python -m pipeline.backup.data_stores`
  into the `jobs` container as `appuser`. `with_mark.sh` leaves
  `/data/ingest_marks/nightly-backup.running` while the job runs and `nightly-backup.last` at its
  end. `with_retry.sh 2` gives the job two attempts: it runs the command again after a crash
  (exit 139 or "Fatal Python error"), 30 seconds later, and stops at once on any other failure.
- **The rebuild job** (08:00 UTC) now runs inside `with_mark.sh` too. It leaves
  `/data/ingest_marks/nightly-rebuild.running` while it writes the DuckDB file. The data job
  waits for that mark.

The Postgres dump runs while the app serves. The DuckDB copy runs while the app serves, because
the app holds no handle on the DuckDB file (SIM-524).

### 3.3 The Postgres store: the dump and the count bracket

The script takes these steps, in order.

1. It refuses (exit 2) when `/backups` is missing or cannot be written, or when a setting is not
   a whole number.
2. It takes the lock: it creates the folder `postgres/.lock` (`mkdir` is atomic) and writes its
   pid and start time into `.lock/owner`. A second run refuses with exit 2 and deletes nothing.
   A lock whose pid is gone, or that is older than 6 hours, is stale; the next run removes it.
3. It deletes every `postgres/*.partial` folder.
4. It runs the free-space guard (§3.7).
5. It counts every table of the `raw`, `sim` and `public` schemas with an exact `count(*)` and
   writes `counts_before.tsv`.
6. It runs `pg_dump -Fd -j 4 -Z 3`. The directory format is the only format that restores in
   parallel. Postgres 15 compresses with gzip only.
7. It runs `pg_restore --list` on the dump. That is the structural check: the dump's table of
   contents must read back.
8. It counts every table again and writes `counts_after.tsv`. It reads the Alembic head.
9. It hashes every file, writes `manifest.json` last, and renames the set (§3.6).
10. It keeps the newest `BACKUP_KEEP` complete sets (§3.8).

**Why two counts.** `pg_dump` reads one consistent snapshot. The live service and the app can
write to the database during the dump, so the snapshot's count of a table lies somewhere between
the count before and the count after. The restore reads each restored table's count and requires
it to lie inside that bracket. A table that only gains rows, or only loses rows, during the dump
always passes. A table that gains and loses rows during the dump can fall outside the bracket;
the restore then exits 1 and names the table.

### 3.4 The DuckDB store: the read-only handle and the WAL sidecar

The data job takes these steps for the DuckDB store.

1. **The writer wait.** It sleeps 60 seconds of grace, so a job that the scheduler starts in the
   same second has time to write its mark. Then it waits while any of the marks
   `nightly-rebuild.running`, `nightly-finals.running` or `weekly-refresh.running` is younger than
   6 hours. It checks every 60 seconds, for up to 2 hours. An older mark was left by a job that
   died, and the wait ignores it. When the wait gives up, the job skips the DuckDB store, still
   backs up the bundle, and exits 1.
2. **The read-only open.** It opens `/data/baseball_sim.duckdb` read-only. When another process
   holds the file, DuckDB raises a lock error. The job retries every 60 seconds for 30 minutes.
   Then it skips the DuckDB store and the run exits 1. Any other error fails the store at once.
3. **The copy, with the handle held.** While it holds the read-only handle, the job takes every
   table's `count(*)` and copies the file. When the sidecar `baseball_sim.duckdb.wal` exists, the
   job copies it too. It flushes each copy to the disk (fsync) and closes the handle.
4. **The check.** It opens the copy read-only and requires every table's count to equal the
   source's.
5. **The small files.** It copies `calibration.json`, `prop_validation.json` and every
   `ingest_marks/*.last` file into `data_files/`. It skips the `.running` marks: they describe a
   job, not data.
6. It hashes every file and writes the manifest last (§3.6).

**Why the copy is consistent.** A DuckDB writer opens the file only when no other process holds
it, read-only handles included. So while the data job holds its read-only handle, no writer can
change the file, and the bytes the job copies are one state of the database. The WAL (the
write-ahead log, the file of changes not yet written into the main file) belongs to that same
state. A copy with its WAL opens and replays the WAL, so the job copies both.

**Why the wait comes first.** The rebuild does not retry its own connect. A read-only handle
that the data job holds makes the rebuild's writer fail at once. So the data job never opens the
file while a rebuild mark is fresh.

### 3.5 The bundle store

The data job resolves the link `/data/play_pool/engine_artifacts` to the published version. It
copies that version with `shutil.copytree` into `bundle/<TS>/<build id>/`, without the app's
`.in_use.*` marks. It flushes every file. The manifest records a sha256 per file, the build id,
the source path and the time the version was published.

### 3.6 The `.partial` rule

Every set is written as `<TS>.partial`. Every file is flushed to the disk. `manifest.json` is
written last. Then the folder is renamed to `<TS>`.

The next run of the same store deletes every leftover `.partial` folder. Each job sets
`no-overlap`, so a `.partial` folder at the start of a run always belongs to a run that died: a
crash, or one of the host's frequent reboots.

A set without `manifest.json` is never counted. The status reader, retention, the free-space
guard, the restore, the drill and the gauge all skip it.

### 3.7 The free-space guard

Before it writes a set, each job checks the free space on `/backups`. `/backups` is a bind mount,
so it reports the host disk C:. The guard never reads `/data`, whose sparse disk image reports
far more free space than C: has.

- The room needed is the larger of 10 GiB (`BACKUP_MIN_FREE_GB`) and 1.5 times the size of the
  store's newest complete set.
- When the disk is short, the guard deletes the oldest complete set while more than 2 sets
  remain, and checks again.
- When the disk is still short with 2 sets or fewer, the job refuses with exit 2 and writes
  nothing.

### 3.8 Retention

Each store keeps its newest `BACKUP_KEEP` complete sets (3). A run never deletes the set it just
made. A failed run prunes nothing. The data job prunes only when both of its stores succeeded.

### 3.9 The rerun window

A crash retry of the data job must not make a second set the same night. So a run first reads
each store's newest complete set. When that set started less than 1 hour ago
(`--rerun-window-s 3600`), the run skips the store. It writes the other store's set under the
same set id. So one night makes one set per store, and the DuckDB set and the bundle set share
one `TS`.

`--new-set` turns the window off. The manual `make backup` passes it, so a manual run always
makes a fresh set.

## 4. What is NOT backed up, and why

- **`/data/replay.duckdb`**, the store of the replayed games on the game page. The app holds it
  writable with a live WAL, so no other process can open it. It holds only disposable replays.
- **`calibration.json.bak_*`**, the old copies of the calibration report. The current
  `calibration.json` is in every DuckDB set.
- **The older bundle versions.** The bundle set holds the published version only.
- **Redis**, the cache. Every value in it is rebuilt on demand.
- **The Postgres roles and their passwords.** `pg_dump` dumps one database, not the cluster's
  roles. On a fresh volume the image creates the owner role from `.env`, and `setup_roles.sh`
  creates the app role and the read-only role from `.env`; `make db-roles` resets their
  passwords from `.env` on every run.
- **The `.env` file and its secrets.** The roles above come from it. Keep a copy of `.env`
  outside Docker and off the laptop, for example in a password manager.
- **Prometheus and Grafana data.** They hold metrics history only.

## 5. The backup run book

### 5.1 By the scheduler

Nothing to do. At 10:00 UTC each day:

- `nightly-backup-postgres` writes one set into `postgres/`;
- `nightly-backup-data` writes one set into `duckdb/` and one into `bundle/`, and leaves
  `/data/ingest_marks/nightly-backup.last`.

The rebuild ends near 09:30 UTC. The closing-lines pass runs at 10:30 UTC inside the app
container. The backup window sits between them.

### 5.2 By hand

Run this from cmd.exe. Git Bash rewrites the container paths (CLAUDE.md §2a).

```
make backup
```

The target runs two steps, one after the other:

```
docker compose exec -T -u postgres db sh /opt/baseball/backup/pg_backup.sh
docker compose exec -T jobs sh /app/scripts/with_mark.sh nightly-backup python -m pipeline.backup.data_stores --new-set
```

Each step prints one line per store on success:

```
BACKUP OK postgres <TS> <bytes> bytes <seconds> s
BACKUP OK duckdb <TS> <bytes> bytes <seconds> s
BACKUP OK bundle <TS> <bytes> bytes <seconds> s
```

The exit codes: 0 every store succeeded; 1 a store failed (the data job still tries the other
store); 2 a refusal before any write.

### 5.3 How to read a set

- A set is complete when `manifest.json` exists in it. A `<TS>.partial` folder is a run in
  progress or a dead run.
- `manifest.json` lists every file with its size and sha256, and the total bytes and seconds.
- In a Postgres set, `counts_before.tsv` and `counts_after.tsv` hold one line per table:
  `schema.table`, a tab, the count.
- In a DuckDB set, the manifest's `counts` holds every table's count, and `wal_included` says
  whether the WAL was copied.
- In a bundle set, the manifest's `build_id` names the folder that holds the copy.

### 5.4 `make backup-status`

```
make backup-status
```

The target runs `python -m pipeline.backup.data_stores --status` in the `jobs` container. It
prints, for each store, the newest complete set, its creation time, its age in hours, its size
and the number of complete sets. `--json` prints the same as JSON.

### 5.5 The gauge

`/metrics` carries `baseball_sim_backup_age_hours{store="postgres"|"duckdb"|"bundle"}`: the hours
since the newest complete set of each store. It reads -1 when a store has no complete set, or
when the folder is missing or cannot be read. The app reads the folder `/backups`, which compose
mounts read-only (`BACKUPS_ROOT`). It reads the folder at most once every 600 seconds. Both
renderers of `/metrics` (with and without `prometheus_client`) carry the gauge. A healthy stack
reads each store under 24 hours, shortly after 10:00 UTC each day.

## 6. The restore run book

A restore replaces live data. Read each step before you run it.

### 6.1 The full disaster: Docker's disk image is gone

1. Bring up a fresh `db` on a fresh volume: `docker compose up -d --wait db`. The image's init
   runs `db/roles/setup_roles.sh`, which creates the two roles and the empty `raw` and `sim`
   schemas.
2. Run `make db-roles`. It creates or refreshes the app role and the read-only role.
3. Run `make restore-postgres SET=<TS>`, with the `TS` of the newest Postgres set.
   The target runs `sh /opt/baseball/backup/pg_restore.sh /backups/postgres/<TS>` in the `db`
   container as the `postgres` OS user. The script takes four steps:
   - **the connection probe:** `select 1`, retried every 2 seconds for up to 30 seconds; a
     server that never answers exits 1, never 3;
   - **the empty-target guard:** `DROP SCHEMA IF EXISTS raw; DROP SCHEMA IF EXISTS sim;`
     WITHOUT `CASCADE`. On the fresh cluster the drop removes the two empty schemas, and the dump
     creates them again. On a populated database the drop fails with "depend on", and the script
     refuses with exit 3, "target database is not empty", before it restores anything;
   - **the restore:** `pg_restore -j 4 --no-owner --no-acl --exit-on-error`, so the first error
     stops the restore instead of being ignored;
   - **the count check:** each table of `counts_before.tsv` must lie inside its bracket. The
     script prints one `count <table> live=<n> bracket=[<lo>,<hi>] OK|FAIL` line per table.
   The last line reads `RESTORE OK <set dir>`.
4. Run `make db-roles` again. The restore drops every grant (`--no-acl`); this step applies them
   again.
5. Run `docker compose run --rm migrate alembic current`. Expect `0030 (head)`.
6. Start the jobs container: `docker compose up -d jobs`.
7. Restore the DuckDB store and the bundle store (§6.2, §6.3). In this case, skip the scheduler
   steps and the app restart of §6.2, because neither runs yet. Step 8 starts them.
8. Start the rest of the stack.

### 6.2 The DuckDB store

1. Stop the scheduler, so no rebuild starts: `docker compose stop scheduler`.
2. Run, in the `jobs` container:
   `docker compose exec -T jobs python -m pipeline.backup.data_stores --restore-duckdb <TS> --yes`.
   The module checks the copy's size and sha256 against the manifest. It copies the file to
   `/data/baseball_sim.duckdb.restore-tmp`. It moves a live `.wal` aside to
   `baseball_sim.duckdb.wal.pre-restore-<time>`: the old file's WAL corrupts the restored file.
   Then it moves the copy into place with `os.replace`. A set with a WAL puts that WAL in place too.
3. Restart the app: `docker compose restart app`. A process that kept the old file open reads
   the old file until it restarts.
4. Start the scheduler: `docker compose start scheduler`.

Without `--yes`, the module refuses with exit 2.

### 6.3 The bundle store

Run, in the `jobs` container:
`docker compose exec -T jobs python -m pipeline.backup.data_stores --restore-bundle <TS> --yes`.

The module checks every file's sha256. It copies the set into
`/data/play_pool/engine_artifacts.versions/<build id>/` and publishes that version through
`bundle_versions.publish_bundle_version`. The app's bundle watcher sees the link move and
restarts the app (SIM-563). When the version folder already exists, the module refuses; pass
`--force` to copy over it.

### 6.4 The partial cases

- **Only the DuckDB file or only the bundle is bad** (a writer died mid-rebuild, or a bad
  rebuild). Restore that store alone (§6.2 or §6.3). The DuckDB store can also come back from
  the nightly recompute in 1.5 hours, from Postgres.
- **The Postgres database is damaged but the volume is alive** (a bad migration). The restore
  never empties a populated database: it refuses with exit 3. Stop the writers
  (`docker compose stop scheduler live app jobs`). Remove the db container
  (`docker compose rm -sf db`). Remove its volume
  (`docker volume rm baseball_simulator_v2_postgres_data`). Then follow §6.1 from step 1. Every row written after the set's time is lost; the nightly finals load and
  `make ingest-catch-up DAYS=<n>` load the missed games again.
- **Only the small files are bad.** Copy the file from the newest DuckDB set's `data_files/` into
  `/data`. After a restore of `calibration.json`, restart the app.

## 7. The restore drill

The drill proves that a set restores, without touching the live database.

```
make backup-drill            (the newest set)
make backup-drill SET=<TS>   (one named Postgres set)
```

The target runs `py scripts/backup_drill.py --set <TS or latest>` on the host. The drill uses
only Python's standard library and calls `docker` with a list of arguments, so Git Bash cannot
rewrite its paths. It takes these steps.

1. It refuses (exit 2) while any `sim525_*` container or volume exists, when no complete set
   exists, or when `--backup-dir` names a folder other than the one the `jobs` container reads.
2. It picks the Postgres set: `--set`, or the newest complete one. It picks the data set: the
   newest `TS` complete in BOTH `duckdb/` and `bundle/`.
3. It starts `postgres:15-alpine` as `sim525_drill_pg`, with `POSTGRES_HOST_AUTH_METHOD=trust`,
   `--network none`, no published port, the named volume `sim525_drill_pg` as its data
   directory, the set mounted read-only at `/restore` and `db/backup` read-only at
   `/opt/baseball/backup`.
4. It waits up to 120 seconds for `pg_isready` over `127.0.0.1`. The image first runs a
   temporary init server on the socket only; the loopback probe answers only when the final
   server is up.
5. It runs the real `pg_restore.sh` against `/restore`. It requires exit 0, no
   "errors ignored" line and `RESTORE OK` as the last line.
6. It runs `docker compose run --rm -T jobs python -m pipeline.backup.data_stores --verify-set <TS>`.
   The verify checks every file's size and sha256 in both sets, opens the DuckDB copy and compares
   its counts with the manifest's, and loads the bundle copy with `EngineArtifacts.load`.
7. It removes the container and the volume, whatever happened before. `--keep-cluster` keeps
   them for inspection.
8. It writes `BACKUP_DIR/drills/<TS>.json` and prints a report.

**How long and what it costs.** The plan estimated 30 to 60 minutes; §10 records the measured
time. Each step has a timeout of 2 hours (`--timeout-s 7200`). Postgres refuses a Windows bind
mount as its data directory, so the scratch cluster lives on a Docker volume. That volume takes
about 20 GB inside Docker's disk image while the drill runs. Run `docker builder prune -f` first
when the disk image is near full.

**How to read the report.** The console prints `Restore drill <TS>: PASS` or `FAIL`, then one
line per part (`postgres` and `data`) with its set, its verdict, its exit code and its seconds,
then the path of the report. The JSON report holds `started_at`, `finished_at`, `ok`, and for
each part `set`, `seconds`, `ok`, `exit_code` and `output_tail` (the last 40 lines of the step's
output). The drill exits 0 on a pass and 1 on a failure.

## 8. Tests

- `tests/unit/test_sim525_backup_data.py`: the DuckDB and bundle backups. A DuckDB round trip with
  a count compare, the WAL sidecar, the bundle copy without the in-use marks, retention, the
  `.partial` cleanup, the writer wait, the lock retry, the free-space guard, the rerun window,
  the verify and the two restores.
- `tests/unit/test_sim525_backup_status.py`: the reader of the sets (`pipeline/backup/status.py`).
  A set without a manifest is not complete.
- `tests/unit/test_sim525_pg_backup_sh.py`: the real `pg_backup.sh` and `pg_restore.sh` under
  `sh`, with fake `pg_dump`, `psql`, `pg_restore` and `df` binaries first on PATH. The rename, the
  manifest, the lock, the refusal on low space, the empty-target guard and the count bracket. It
  skips on Windows; CI and the container run it.
- `tests/unit/test_sim525_backup_drill.py`: `scripts/backup_drill.py` with a fake runner. The set
  pickers, the `docker run` arguments, the loopback ready probe, the restore verdict, the cleanup
  after a failure, the refusals and the streaming timeout.
- `tests/unit/test_sim525_backup_gauge.py`: the gauge on `/metrics`, -1 with no set, and the
  600-second cache.
- `tests/unit/test_sim525_schedule.py`: the wiring. The two Ofelia jobs and `no-overlap`, the
  rebuild inside `with_mark.sh`, the compose mounts, the Makefile targets and the ignore files.
- `tests/unit/test_sim443_no_default_password.py` (amended): the Ofelia file holds no credential;
  the `jobs` container now runs four jobs, and the Postgres job runs as `postgres`.

## 9. Follow-ups

- **An off-machine copy of the backup sets** (P3). The sets share the
  laptop's one disk. A copy on another machine or a cloud store protects against the disk dying
  and the laptop being lost.
- **Pin `ODDS_PROVIDER=mock` in `tests/conftest.py`** (P3). The value
  `bettingpros` in `.env` reaches `docker compose run app pytest` and fails 12 unrelated tests
  (§10).
- **A Grafana panel for the backup-age gauge.** The gauge is on `/metrics`; no dashboard shows it
  yet, and no alert fires when a store passes 24 hours.
- **The first scheduled run.** Read `make backup-status` after 10:00 UTC on a day after the
  deploy: three stores under 24 hours, `nightly-backup.last` written.

## 10. The run record (2026-10-15, UTC)

**04:56, the deploy from this checkout.** `BACKUP_DIR` was written to `.env`.
`docker compose config --quiet` passed. `docker compose up -d --wait db jobs app` recreated the
three services. Postgres restarted for about ten seconds. The live service logged one DNS error
during that restart and reconnected; its Redis heartbeat advanced afterwards. The app, `db` and
`jobs` were healthy inside a minute. `docker compose up -d --force-recreate scheduler` registered
six jobs: the four old ones, `nightly-backup-postgres` and `nightly-backup-data`. Inside both the
`db` and the `jobs` container, `df /backups` reads the host disk C: (125 GB free), as the
free-space guard needs.

**04:58, the first backup by hand.** Both steps ran in parallel, as the schedule runs them.

- The data job wrote the set `20261015T045846Z`. The DuckDB copy: 3,839,122,442 bytes (3.6 GiB)
  in 201 s, with equal counts on the copy. The bundle (build `20261014T081352Z`): 588,464,368
  bytes in 46 s. The whole run took 308 s, with the 60 s grace.
- The Postgres job's first attempt (set `20261015T045845Z`) dumped in four minutes and then
  sat in the file-sizing step: busybox `wc -c` read 94 MB in 53 s over the bind mount. It was
  stopped at 05:13 and its partial set removed; the script now sizes each file with `stat`
  (commit `9122237`). The fixed script wrote the set `20261015T051313Z` at 05:13:
  2,895,149,569 bytes (2.7 GiB, 39 files) in 267 s.

**The negative checks.** Each one behaved as designed.

- A planted `duckdb/20261015T000000Z.partial` was deleted on the next run. That run skipped both
  stores, because their sets were under an hour old, and exited 0.
- A fresh `nightly-rebuild.running` mark made the data job wait. The job gave up after the
  5-second test window, skipped the DuckDB store and exited 1. No `.partial` folder was left.
- `--status` listed the sets.
- `pg_restore.sh` run against the live database refused with exit 3 ("target database is not
  empty") before any restore step, and `raw.games` still held its 23,175 rows.

**The restore drill.** `make backup-drill` ran from 05:19 to 05:27 and passed in 450 s. The
scratch cluster restored the set `20261015T051313Z` in 349 s of `pg_restore -j 4`. Every one of
the 36 tables landed inside its count bracket: 34 exact, and `raw.game_odds` and `raw.prop_odds`
moved by 27 and 8 rows during the dump, because the live service writes odds. `RESTORE OK` was
the last line, and no error was ignored. The data set `20261015T045846Z` verified in 84 s: the
DuckDB copy's counts and hashes match, and the bundle copy loaded with the production loader in
10 s. The drill removed the container and the volume. The report is
`BACKUP_DIR/drills/20261015T051919Z.json`.

**The unit lane in the container.** 6,239 tests passed. `ruff` and `mypy` are clean. 12 tests
failed in `test_data_engineer_sim340.py`, `test_live_pipeline_sim348.py` and
`test_sim421_odds_vocabulary.py`. The cause is `ODDS_PROVIDER=bettingpros`, which reaches
`docker compose run app pytest` from `.env`. The 12 tests pass with `ODDS_PROVIDER=mock`. That is
an old gap in the tests' isolation, not a defect of this ticket (§9).

**How it was built.** A workflow of Opus 5.5 agents built five components on separate files. An
independent reviewer read each component. The reviews found defects in four, and each was
repaired:

- a crash retry of the data job made a second set the same day (now the rerun window, §3.9);
- the restore read a failed `psql` as a populated target, and the Postgres job had no lock (now
  the connection probe and the `postgres/.lock` folder);
- the `db` service passed no `BACKUP_KEEP` to the Postgres job;
- the drill's ready probe could catch the image's temporary init server, the drill passed
  `latest` to the verify without resolving it, and its streaming timeout never fired while a
  command printed nothing.

Then one integration agent ran the unit lane.
