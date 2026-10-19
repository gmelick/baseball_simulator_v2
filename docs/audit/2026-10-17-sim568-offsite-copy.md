# The off-site copy of the backup sets (SIM-568): the design, the run book, the restore from the copy — 2026-10-17

**Status:** BUILT, DEPLOYED and RUN 2026-10-17 (§10): every store is mirrored on OneDrive with
three sets, and the drill from the copy has run. The approved plan is
`C:/Users/grego/.claude/plans/implement-sim-568-keeping-a-sunny-lynx.md`. This document is the
design as built (§4), the run book (§6), the restore from the copy (§7) and, after the first
run, the run record (§10). The scheduled backups that this copy extends are in
`docs/audit/2026-10-14-sim525-backups-plan.md`.

Terms used in this document:

- A **store** is one of the three places that hold the platform's data: the Postgres database,
  the DuckDB analytics file and the engine-artifact bundle.
- A **set** is one complete backup of one store, taken at one time. `TS` is a set's id: the UTC
  time its run started, written `%Y%m%dT%H%M%SZ` (for example `20261017T100000Z`).
- A set is **complete** when its folder holds `manifest.json`. Every reader skips a folder
  without one.
- **The local folder** is `BACKUP_DIR` in `.env`, `C:/Users/grego/Documents/baseball_backups`.
  Compose mounts it at `/backups`.
- **The off-site folder** is `OFFSITE_BACKUP_DIR` in `.env`,
  `C:/Users/grego/OneDrive/Documents/baseball_backups`. Compose mounts it at `/offsite`.
- **The off-site copy** is the job that copies sets from the local folder to the off-site
  folder, and its module `pipeline/backup/offsite.py`.
- A set is **mirrored** when its off-site `manifest.json` equals the local one byte for byte and
  every file that the manifest lists has its size off-site.
- **The OneDrive client** is the OneDrive program on the laptop. It uploads the off-site folder
  to the owner's OneDrive account.
- A **placeholder** is an online-only OneDrive file: the name and the size stay on the disk, and
  the bytes live in the cloud. To **dehydrate** a file is to turn it into a placeholder. To
  **hydrate** it is to download its bytes again.

## 1. Why it matters

The scheduled backups (SIM-525) write a set of each store every night at 10:00 UTC into the
local folder. One night's sets take about 7.5 GB. On 2026-10-17 they were 2,897,058,654 bytes
(Postgres), 4,051,459,150 bytes (DuckDB) and 588,561,179 bytes (the bundle). Each store keeps 3
sets, so the local folder held about 22.4 GB that day.

The local folder sits on C:, the laptop's only disk. The sets survive the loss of Docker's disk
image. They do not survive the disk dying or the loss of the laptop.

The ticket's definition of done:

1. The newest complete set of each store also exists on another disk or machine.
2. The copy runs by itself after the 10:00 UTC backups.
3. One restore from that copy has been tested.

## 2. The owner's decisions

The owner took two decisions on 2026-10-15.

1. **The copy lives in the OneDrive folder `C:\Users\grego\OneDrive\Documents\baseball_backups`.**
   The OneDrive client uploads whatever lands there, so the cloud copy is the off-machine copy.
   The value is `OFFSITE_BACKUP_DIR` in `.env`, with forward slashes like `BACKUP_DIR`.
2. **The copy keeps 3 sets per store**, a mirror of the local retention. The setting is
   `OFFSITE_KEEP`; without it the copy reads `BACKUP_KEEP`, and without that it keeps 3. Three
   nights take about 22 GB of the OneDrive quota.

## 3. The host facts

These facts were verified on 2026-10-15, unless a line says 2026-10-17.

- `C:\Users\grego\OneDrive\Documents` is the OneDrive "Documents" known folder: Windows
  redirects the user's Documents folder into OneDrive there. It is a cloud-files folder with the
  "free up space" attribute, so synced files can later turn into placeholders. The repo is under
  the plain `C:\Users\grego\Documents`, not under OneDrive.
- The OneDrive client runs on a personal account. About 411 GB are synced, so the plan is
  presumably 1 TB with roughly 590 GB free. Nobody confirmed the plan's size.
- Docker bind-mounts the OneDrive folder. A read-only probe container listed it with the real
  file sizes.
- On a personal account, deleted files stay in OneDrive's recycle bin for 30 days. Most sources
  say that the recycle bin does not count against the quota. Microsoft's own text does not
  confirm it; §6 step 10 measures it.
- OneDrive refuses a few file names, `.lock` among them. The sets never hold one:
  `postgres/.lock` is the Postgres job's lock folder, and the copy never copies it. The copy's
  probe file has no leading dot for the same reason.
- C: had 122 GB free on 2026-10-15 and 118 GB free on 2026-10-17.
- On 2026-10-17 the off-site folder exists and is empty. Each store holds three local sets:
  `20261015T100000Z`, `20261016T100000Z` and `20261017T100000Z`. So the first run copies nine
  sets, about 22.4 GB, not one night's 7.5 GB.
- The nightly chain runs at 07:00 (the finals load), 08:00 (the rebuild, to about 09:30),
  10:00 (both backups) and 10:30 UTC (the closing-lines pass, and now the off-site copy). A
  recreate of the `jobs`, `app` or `scheduler` container inside that window kills a job.
- **Found on the first copy (2026-10-17): the Postgres sets were unreadable from the `jobs`
  container.** `pg_dump` creates its dump folder with mode 0700, owned by the `postgres` OS user
  (uid 70) of the `db` container. The copy runs as `appuser` (uid 1001) in the `jobs`
  container, and the bind mount keeps the Linux mode bits as metadata, so every Postgres set
  failed with "Permission denied" before any write. The fix: `db/backup/pg_backup.sh` now runs
  `chmod -R u=rwX,go=rX` on the set before the manifest lands (step 4b), and a test holds it.
  The three existing sets were opened by hand, as the `postgres` user in the `db` container.
  The host folder's own permissions guard the files; these bits only decide which container
  user may read them.

## 4. The design

**One rule: an off-site set is complete only when its manifest is there. The manifest is the
local set's own bytes, and the copy writes it last.**

### 4.1 Where it runs

The scheduler (Ofelia) runs the job `nightly-backup-offsite` at 10:30 UTC:

```
[job-exec "nightly-backup-offsite"]
schedule = 0 30 10 * * *
container = baseball_simulator_v2-jobs-1
user = appuser
no-overlap = true
command = sh /app/scripts/with_mark.sh nightly-backup-offsite sh /app/scripts/with_retry.sh 2 python -m pipeline.backup.offsite
```

- `with_mark.sh` leaves `/data/ingest_marks/nightly-backup-offsite.running` while the copy runs
  and `nightly-backup-offsite.last` at its end. The middle field of the `.last` mark is the exit
  code.
- `with_retry.sh 2` runs the copy a second time after a crash (exit 139 or "Fatal Python
  error", the SIM-445 crash class). The second run resumes the copy (§4.5).
- The `jobs` container mounts `/backups` and `/offsite`, both read-write. The app mounts
  `/offsite` read-only, for the off-site age gauge. The `db` container does not change: the copy
  reads `postgres/` through `/backups` in the `jobs` container, so Postgres never restarts for
  this work.
- The module is pure standard library. It imports `pipeline/backup/status.py` and
  `pipeline/backup/common.py` only, never duckdb. The helpers it shares with the data job moved
  from `data_stores.py` into `common.py` with the same names and bodies.

**Why 10:30 with a wait, and not a chain.** Ofelia has no job dependencies. The Postgres job
runs in another container. A copy chained after the data job would run inside the data job's
mark and retry. So the copy starts at a fixed time and waits (§4.4). The closing-lines pass also
starts at 10:30, but it runs in the app container, so the two never share a container.

**Why a job in the stack, and not a Windows task.** The job follows the SIM-525 pattern: the
`jobs` container, `with_mark.sh`, `with_retry.sh`, `make` targets, a gauge on `/metrics` and unit
tests with fake clocks. A `robocopy` task would run faster, but it would live in Windows Task
Scheduler, outside the repo's tests and run book. It would also check nothing against the
manifests. It is the fallback only if the first manual run cannot move one night's 7 GB through
the Docker file server in about 45 minutes.

### 4.2 The layout

The off-site folder has the same store folders as the local folder:

```
OFFSITE_BACKUP_DIR/
  postgres/<TS>/     the files of the local set, then manifest.json
  duckdb/<TS>/       the same
  bundle/<TS>/       the same
  drills/<TS>.json   the reports of the drills from the copy (the drill writes them, not the copy)
```

Each off-site set is a byte-for-byte copy of its local set. Its manifest is the local manifest,
byte for byte. So `python -m pipeline.backup.data_stores --verify-set <TS> --backups-root /offsite`
and the restore drill read the copy with no change. The copy never copies the local `drills/`
folder or `postgres/.lock`.

### 4.3 The completeness rule without a rename

The SIM-525 jobs write a set as `<TS>.partial` and rename it at the end. The copy cannot. The
OneDrive client holds a new file open while it uploads it, and a Windows folder rename fails
while a file inside the folder is open.

So the copy writes each file straight into `<store>/<TS>/` and writes `manifest.json` last. A
folder with no `manifest.json` reads as incomplete to every reader: `status.complete_sets`, the
drill's `complete_sets`, retention, `--status` and the gauge. A copy that dies leaves such a
folder, and the next run resumes it.

### 4.4 The copy lock, the wait, and why not `.partial`

**One copy runs at a time.** Before it waits, a run takes the copy lock
`/data/ingest_marks/offsite-copy.lock`. The run creates the file only when no file of that name
exists (`O_EXCL`), and writes its pid, its start time and its host into it. A second run, such
as a `make backup-offsite` during the 10:30 job, refuses with exit 2 before any write. The lock
is stale when it is older than `--lock-stale-s` (6 h), or when this host wrote it and its pid is
gone; a stale lock is removed and taken again. So the crash rerun of `with_retry.sh` takes over
the lock that the crashed run left. The run deletes the lock at its end, after a failure too.
When the mark folder cannot be written, the copy runs unlocked and logs that, as `with_mark.sh`
does.

On the first scheduled nights (2026-10-15 to 2026-10-17) the two backups wrote their manifests
by about 10:07 UTC. A slow night can run past 10:30. So the copy first waits, in one loop, while
either backup job runs:

- the data job's mark `/data/ingest_marks/nightly-backup.running` exists and is younger than
  6 h (`--stale-s 21600`); or
- the Postgres job's lock file `/backups/postgres/.lock/owner` holds a start time (its second
  field, in epoch seconds) younger than 6 h (`--lock-stale-s 21600`).

The loop polls every 60 s (`--poll-s 60`) for up to 2 h (`--wait-s 7200`), with no grace sleep
first (`--grace-s 0`). When the wait runs out, the copy logs it and goes on. It copies only
complete sets, and a complete set is always safe to copy. A set that completes later is copied
the next night; until then `--status` shows the store BEHIND.

The copy does not wait on a `postgres/*.partial` folder. A partial with no lock is a dead run:
the Postgres script's exit trap removes the lock and leaves the partial, and the next Postgres
run deletes it. A wait on it would idle for two hours for nothing.

### 4.5 The copy and the resume

Per store, the copy takes these steps.

1. **The wanted sets** are the newest `keep` complete local sets, the oldest first.
2. **The dead-folder clean** deletes every off-site folder of the store that ends in `.partial`
   or has no manifest, when its id is not wanted. A manifest-less folder of a wanted id is not
   deleted: the copy resumes it.
3. **The work list** is every wanted set that is not mirrored. The mirrored check reads the small
   off-site manifest and runs `stat` on each file. A placeholder reports its full size, so the
   check never downloads a large file.
4. **The copy of a set**, one set at a time, the oldest first:
   - The local manifest's file list is the set. A file that the manifest does not list is not
     copied.
   - Every local file must have the size that the manifest gives. Otherwise the store fails
     ("the local set is damaged") before the copy writes anything for that set.
   - An older off-site manifest of the set goes first, so the folder reads as incomplete during
     the copy.
   - An off-site file that already matches (its size, and its sha256 unless `--no-hash`) is
     kept. That is the resume after a crash.
   - Every other file is copied in 8 MiB reads. The copy hashes the source bytes as it reads
     them, flushes the new file to the disk through its own handle, and compares the source's
     sha256 with the manifest's. Then it reads the new file back and compares again (`--no-hash`
     skips the read-back). The copy uses plain reads and writes, not `shutil.copyfile`: its
     `sendfile` path over the Docker Desktop bind mount is unpredictable.
   - Any mismatch fails the store, and the set gets no manifest.
5. **The manifest** goes last. The copy writes the local manifest's bytes, unchanged, to
   `manifest.json.tmp`, flushes it, and moves it over `manifest.json` with `os.replace`. The move
   retries on `PermissionError` (OneDrive holds the folder) up to 10 times, 3 s apart. On give-up
   the copy deletes the `.tmp` and the store fails.
6. The copy prints `COPY OK <store> <TS> <bytes> bytes <seconds> s` for a copied set and
   `COPY SKIP <store> <TS> (already off-site)` for a mirrored one.

**Why the manifest is copied unchanged.** A half-written manifest would read as complete with
0 bytes, because `status._read_set` treats a manifest that does not parse as an empty one. The
mirrored check and the verify also compare bytes.

**What a resume costs.** After a crash, the rerun hashes every off-site file that has the right
size, so it reads back the bytes copied so far. Then it copies what is missing or wrong and
writes the manifest.

### 4.6 Retention, per store, the manifest first

After a store copied cleanly, the store keeps its newest `keep` complete off-site sets. The
copy never prunes a wanted set, that is a set among the newest `keep` local sets. Without that
rule, an off-site set newer than every local set would push a wanted set out, and the next run
would copy the wanted set back. For each set it prunes, the copy deletes `manifest.json` first
and then the folder (`rmtree(ignore_errors=True)`). So a half-deleted set never reads as
complete. A file that OneDrive still holds stays until the next run's dead-folder clean.

Retention runs per store, right after that store's copy, because a store that fails every night
must not stop the other stores' retention. A failed store prunes nothing.

### 4.7 The free-space guard, and the quota it cannot see

Once per run, before any write, the copy adds up the bytes of every work list. The disk under
`/offsite` must have the larger of `BACKUP_MIN_FREE_GB` (10 GiB) and `BACKUP_FREE_FACTOR` (1.5)
times those bytes free. Otherwise the copy refuses with exit 2. With nothing to copy, the guard
does not run.

`/offsite` is a bind mount of a folder on C:, so the guard reads C:. It cannot see the OneDrive
quota, and its log line says so ("the local disk, not the OneDrive quota"). A full quota stops
the OneDrive client's upload, not the copy. Only the host shows it: the tray icon and the
OneDrive storage figure.

The off-site folder is on C: too. Until the OneDrive client dehydrates its files, C: holds the
sets twice: about 22 GB in the local folder and about 22 GB in the off-site folder. The first
run on 2026-10-17 needs max(10 GiB, 1.5 × 22.4 GB), about 33.6 GB, against 118 GB free.

### 4.8 What OneDrive guarantees, and what it does not

What the copy relies on:

- The OneDrive client uploads whatever lands in the off-site folder. The cloud copy survives
  the loss of the laptop's disk and the loss of the laptop.
- A deleted file stays in OneDrive's recycle bin for 30 days.

What OneDrive does not do:

- **The copy is off the machine only once the upload ends, and the job cannot see the upload.**
  The OneDrive tray icon and Explorer's status column are the truth. One night's 7.5 GB takes
  20 to 50 minutes on a typical uplink (the plan's estimate, not measured).
- **The folder can dehydrate.** Storage Sense (the Windows disk clean-up) can turn the files
  into placeholders. A read of a placeholder through the Docker mount downloads the file. An
  offline or paused client can stall that read. So the app reads the off-site folder on a
  background task, never in a scrape (§4.9).
- **The copy mirrors deletions.** A set that the copy prunes, or a file that somebody deletes,
  leaves the cloud too. Only the recycle bin keeps it, for 30 days.
- **The quota is invisible to the job** (§4.7).

### 4.9 What reports the state

- `python -m pipeline.backup.offsite --status` (the second block of `make backup-status`)
  prints, per store, the newest local set, the newest off-site set with its age, size and number
  of sets, and a verdict: `MIRRORED` (the newest local set is complete and mirrored off-site),
  `BEHIND`, or `none` (no local set). `--json` prints the same as JSON.
- `--dry-run` prints `WOULD COPY`, `WOULD SKIP`, `WOULD PRUNE` and `WOULD DELETE` lines and
  writes nothing. It compares the two roots' real paths but writes no probe file. It takes no
  copy lock; when a live run holds it, the dry run logs "... a real run would refuse". It
  checks the two backup jobs once and does not wait; when one runs, it logs "... runs; a real
  run would wait". It runs the free-space guard, which only reads.
- The gauge `baseball_sim_offsite_backup_age_hours{store}` on `/metrics` reads the hours since
  each store's newest complete off-site set, and -1 when there is none. A background task in the
  app reads `OFFSITE_ROOT` at most every 600 s on a worker thread. The scrape reads only the
  stored value, so it reads -1 for every store until the first read and the first copy. The
  local gauge `baseball_sim_backup_age_hours` does not change.
- The mark `nightly-backup-offsite.last` holds the copy's exit code. The data job copies every
  `*.last` mark into its DuckDB set (`data_files/ingest_marks/`), so this mark rides into later
  DuckDB sets. That is harmless.

### 4.10 Exit codes and the command line

- **0:** every store copied or was already in sync. `--status` and `--dry-run` also exit 0.
- **1:** a store failed. The copy logs `FAIL <store>: ...` and still tries the other stores.
- **2:** a refusal before any write: a root is missing, the off-site root cannot be written, the
  two roots are one folder, another copy run holds the copy lock, or the free-space guard says
  no.

The two roots are one folder when somebody points `OFFSITE_BACKUP_DIR` at `BACKUP_DIR`. Two
mounts of one folder have different paths inside the container, so the copy writes the probe
file `offsite-probe-<pid>.tmp` into `/offsite`, refuses when the file shows under `/backups`,
and deletes it.

The flags: `--backups-root` (`BACKUP_ROOT`, default `/backups`), `--offsite-root`
(`OFFSITE_ROOT`, default `/offsite`), `--mark-dir` (`INGEST_MARK_DIR`, default
`/data/ingest_marks`), `--only {postgres,duckdb,bundle}` (repeatable), `--keep N`, `--no-hash`,
`--dry-run`, `--status [--json]`, and the waits `--grace-s 0`, `--wait-s 7200`, `--poll-s 60`,
`--stale-s 21600` and `--lock-stale-s 21600`. `--keep` beats `OFFSITE_KEEP`, which beats
`BACKUP_KEEP`; without all three the copy keeps 3, and 0 or less reads as 1.

`make backup-offsite` runs the copy by hand, inside `with_mark.sh nightly-backup-offsite`. Its
first line refuses with exit 2 while a `nightly-backup-offsite.running` mark under 6 hours old
exists. The copy lock (§4.4) refuses an overlap too, so a manual run never writes the set
folders that the scheduled run writes.

## 5. Edge cases the design covers

- A local set that completes after the copy started is copied the next night.
- Two copy runs at once, such as a `make backup-offsite` during the 10:30 job: the second run
  refuses with exit 2 before any write (§4.4).
- A crash in the middle of a copy leaves an off-site folder with no manifest. The next run, or
  the crash retry, hashes what exists, copies what is missing or wrong, and writes the manifest.
- OneDrive placeholders on a later night: the mirrored check runs `stat` on the files and
  compares the small manifest, which hydrates on read. A read error makes the set look
  incomplete, and the copy resumes it. Pruning deletes placeholders without a download.
- A destination file that OneDrive holds open fails that store for one night. The resume covers
  the next night.
- A `make backup` during a copy can prune the local set under it (`FileNotFoundError`). That
  store fails for one night. The wait covers the scheduled case.
- A full OneDrive quota stops the client's upload, not the copy. Only the host shows it.
- The read-back hash of a file just written may come from the container's page cache. So it
  proves that the bytes reached the Docker file server, not the disk. The drill from the copy
  (§6 step 8) is the stronger check.
- The gauge reads -1 for every store until the first run.

## 6. The run book

Run every `make` and `docker compose exec` line from cmd.exe: Git Bash rewrites the container
paths (CLAUDE.md §2a).

1. **Prepare the host.** The off-site folder exists (created 2026-10-17). Confirm that the
   OneDrive client runs and that Explorer shows the folder with a cloud status. Note the
   OneDrive storage figure and the free space on C:. Add this line to `.env`:

   ```
   OFFSITE_BACKUP_DIR=C:/Users/grego/OneDrive/Documents/baseball_backups
   ```

2. **Run the gates.** Run the unit tests with the whole-repo mount, then `ruff check .`,
   `ruff format --check .` and `mypy pipeline/ api/` in the same container shape. Then run
   `docker compose config --quiet`.
3. **Deploy in a quiet window.** No `/data/ingest_marks/*.running` mark may exist, and the next
   job must be more than 20 minutes away. The safe windows are about 07:15–07:40 UTC and after
   10:45 UTC. Once the copy is deployed, a later redeploy also waits until the day's
   `nightly-backup-offsite.last` mark is written.

   ```bat
   docker compose up -d --wait jobs app
   docker compose up -d --force-recreate scheduler
   docker compose logs scheduler | findstr offsite
   ```

   The app is recreated in about 75 s. The scheduler log lists the seventh job,
   `nightly-backup-offsite`.
4. **Probe the mount.**

   ```bat
   docker compose exec -T jobs sh -c "df -h /offsite && touch /offsite/probe.tmp && rm /offsite/probe.tmp"
   ```

   If the probe fails, stop and switch to the `robocopy` fallback (§4.1).
5. **Read the state before the first copy.**

   ```bat
   docker compose exec -T jobs python -m pipeline.backup.offsite --status
   docker compose exec -T jobs python -m pipeline.backup.offsite --dry-run
   ```

   Expect every store BEHIND. On 2026-10-17 the dry run lists nine `WOULD COPY` lines, about
   22.4 GB in all.
6. **The first copy, by hand.**

   ```bat
   make backup-offsite
   ```

   Expect nine `COPY OK` lines. The plan estimated 10 to 25 minutes for one night's 7.3 GB (a
   read, a write and a read-back through the Docker file server); nine sets take about three
   times as long. Watch `docker stats` on `jobs`. Then check:

   ```bat
   make backup-status
   :: The second block: three stores, each MIRRORED with 3 sets.
   docker compose exec -T jobs cat /data/ingest_marks/nightly-backup-offsite.last
   :: The middle field: 0.
   make backup-offsite
   :: Nine COPY SKIP lines, in seconds.
   set API_KEY=<one key from API_KEYS in .env>
   curl -s -H "X-API-Key: %API_KEY%" http://localhost:8000/metrics | findstr offsite_backup_age
   :: Within 600 s: three values under 1 h.
   ```

7. **Check the upload on the host.** Wait for the OneDrive client to upload about 22.4 GB; at
   the plan's 20 to 50 minutes per 7.3 GB, that takes one to three hours. Then:
   - Explorer's status column shows every file of the nine sets as synced.
   - `attrib` on the DuckDB copy shows no `O`: the file is still on the device, so the drill in
     step 8 reads it without a download.
   - The OneDrive storage figure moved by about 22 GB.
   - `onedrive.live.com` lists the manifests.
8. **The drill from the copy**, soon after step 7 and before any dehydration. The scratch
   cluster needs about 20 GB inside Docker's disk image; run `docker builder prune -f` first if
   the image is tight.

   ```bat
   make backup-drill OFFSITE=1
   :: Expected: Restore drill <TS>: PASS
   ```

   The Postgres restore takes about 6 minutes, and the data verify about 90 s through
   `/offsite`. The report lands in the off-site folder's `drills\`, with `"source": "offsite"`.
9. **Record the run.** Fill in §10 and `CHANGES.md` with the timings.
10. **The next morning, after 10:50 UTC.**
    - `make backup-status` shows three stores MIRRORED.
    - `nightly-backup-offsite.last` reads exit 0.
    - The off-site gauge reads under 24 h for every store.

    With three sets already off-site, this first scheduled run copies one new set per store and
    prunes the oldest off-site set of each store. Read the OneDrive storage figure after the
    upload. If it rose by about 7.5 GB, the recycle bin counts against the quota. If it stayed
    flat, it does not.

## 7. The restore from the copy

The off-site layout is the local layout, and each manifest is the local one. So every SIM-525
restore (`docs/audit/2026-10-14-sim525-backups-plan.md` §6) works on the copy once a container
can read it.

### 7.1 On a new machine

Use this when the laptop's disk died or the laptop is lost.

1. Install the OneDrive client and sign in to the owner's account. Let it sync
   `Documents\baseball_backups`, or download that folder from `onedrive.live.com`.
2. Make the sets local: right-click the folder and choose "Always keep on this device". A
   restore from placeholders stalls on downloads.
3. Clone the repo. Restore `.env` from its own off-machine copy: the sets do not hold `.env`
   (SIM-525 §4).
4. Set `BACKUP_DIR` in `.env` to the synced folder. Do not set `OFFSITE_BACKUP_DIR` to the same
   folder: the copy refuses to run when the two are one folder.
5. Keep the scheduler stopped until the restore ends. A 10:00 backup would write its
   `.partial` folders into the OneDrive folder, where the folder rename can fail.
6. Follow the full disaster run book, SIM-525 §6.1.
7. After the restore, point `BACKUP_DIR` back at a plain local folder, then start the scheduler.

### 7.2 On this machine

Use this when the local folder lost a set or holds a damaged one.

The `db` container mounts only `/backups`; it has no `/offsite` mount. So the Postgres restore
needs the set back in the local folder first:

```bat
robocopy "C:\Users\grego\OneDrive\Documents\baseball_backups\postgres\<TS>" "C:\Users\grego\Documents\baseball_backups\postgres\<TS>" /E
make restore-postgres SET=<TS>
```

`robocopy` downloads any placeholder as it reads it. Restore before the next 10:00 UTC run, or
stop the scheduler first: local retention keeps the newest 3 complete sets, so a run can prune
an older set that you copied back.

The DuckDB and bundle restores read any root, and the `jobs` container mounts `/offsite`. So
they can restore straight from the copy:

```bat
docker compose exec -T jobs python -m pipeline.backup.data_stores --restore-duckdb <TS> --yes --backups-root /offsite
docker compose exec -T jobs python -m pipeline.backup.data_stores --restore-bundle <TS> --yes --backups-root /offsite
```

The drill exercises the verify through `/offsite`, not these two restores. I did not test them
from the copy. A copy of the set folder back into the local folder, as for Postgres, works too.

## 8. Tests

- `tests/unit/test_sim568_offsite.py`: a run mirrors the newest set of every store; the
  manifest's file list is the copy list; a second run copies nothing; a crash in the middle of a
  copy leaves no manifest and no `.tmp`; a dead folder of a wanted set is resumed, and a
  same-size wrong file is copied again; a damaged complete copy loses its manifest before any
  rewrite and stays incomplete through a crash; dead folders of unwanted sets and stray
  `.partial` folders are deleted; `--keep 2` of five local sets copies the newest two; the
  `keep` precedence, with 0 read as 1; `--only postgres`; retention keeps the newest off-site
  sets, never prunes a set copied in the same run or a wanted set, and deletes the manifest
  before the folder; a pruned set with a held file stays incomplete until the next run; a
  failed store prunes nothing while the others copy and prune; the read-back hash catches a bad
  copy and `--no-hash` does not; a local set that differs from its manifest is refused before
  any write; a manifest path outside the set is refused; a missing root, an off-site root that
  cannot be written and one folder twice are refused, and the probe file is gone; the
  free-space guard refuses before any write; the wait holds on a fresh data job mark and on a
  fresh Postgres lock, ignores a stale mark, a stale lock and a lone `.partial`, and on a
  timeout logs and still copies; a live copy lock refuses a second run before any write, the
  lock spans the wait and goes after the run (a failed run too), a stale lock or a dead owner's
  lock is taken over, and a mark folder that cannot be written runs the copy unlocked; the
  manifest move retries on `PermissionError` and gives up
  after its tries; `--status` text and JSON with `mirrored`; a dry run writes nothing, writes no
  probe file and does not wait; `offsite` and `common` never import duckdb (a subprocess);
  `data_stores` reuses the `common` helpers.
- `tests/unit/test_sim525_backup_data.py` and `tests/unit/test_sim525_backup_status.py` run
  unchanged. They are the check on the move of the helpers into `common.py`.
- `tests/unit/test_sim525_pg_backup_sh.py`: the fake `pg_dump` now creates its dump folder 0700
  as the real one does, and a new test requires every folder and file of a complete set to be
  readable by another user (the fix of §3's permission finding).
- `tests/unit/test_sim525_schedule.py`: the third Ofelia job and its place after the data
  backup; the `jobs` mount (read-write), the app mount (read-only) and `OFFSITE_ROOT`; no `db`
  mount; no `${OFFSITE_BACKUP_DIR:?` (a worktree without the value still composes); the ignore
  lines; the `.env.example`
  lines; the `backup-offsite` target, its mark and its refusal while another copy runs;
  `backup-status` running `offsite --status`; `backup-drill` passing `--offsite` only for
  `OFFSITE=1`.
- `tests/unit/test_sim443_no_default_password.py`: the `jobs` container now runs five jobs, and
  the Ofelia file still holds no credential.
- `tests/unit/test_sim525_backup_gauge.py`: the off-site reader reads `OFFSITE_ROOT`; a missing
  root reads -1 everywhere; the fallback body carries the HELP and TYPE lines; the endpoint
  renders the stored reading and -1 before the first read; the scrape never reads the off-site
  folder; the refresh runs once per 600 s, one read at a time, and a failed or stalled read
  keeps the app up; the local gauge's three lines do not change.
- `tests/unit/test_sim525_backup_drill.py`: `--offsite` resolves the folder from
  `OFFSITE_BACKUP_DIR` (the shell first, then `.env`) and the verify runs with
  `--backups-root /offsite`; a `--backup-dir` equal to the off-site folder is accepted; any
  other folder still refuses; the report's `source`.
- `tests/unit/test_sim449_sim_kwargs.py` and four files under `tests/acceptance/`: the CLAUDE.md
  line citations moved with the new CLAUDE.md lines, to `CLAUDE.md:890` ("Runs run ~7-8% low")
  and `CLAUDE.md:893` ("stuck at the structural-only").

## 9. Open points

- The plan's timings for the copy and the upload are estimates. §10 records the measured ones.
- ~~Whether OneDrive's recycle bin counts against the quota~~ Answered 2026-10-18 (§10 item 8):
  it does not. The storage figure stayed at about 382 GB through the first off-site prune.
- The restore of the DuckDB and bundle stores straight from `/offsite` is not drilled (§7.2).

## 10. The run record (2026-10-17, UTC)

1. **The deploy, 15:20.** `OFFSITE_BACKUP_DIR` added to `.env`; `docker compose config --quiet`
   clean; `docker compose up -d --wait jobs app` (the app healthy in 75 s); the scheduler
   recreated with seven jobs, `nightly-backup-offsite` among them. The mount probe wrote and
   deleted a file under `/offsite`; `df` showed C: under both `/offsite` and `/backups`;
   `--status` read every store BEHIND; `--dry-run` listed nine sets, 22,387,851,562 bytes
   (20.8 GiB), and the guard asked for 31.3 GiB of 119.2 GiB free.
2. **The first copy, 15:22:04 to 15:34:58 (774 s).** The DuckDB sets copied and read back in
   174, 219 and 194 s (3.9 to 4.1 GB each), the bundle sets in 70, 58 and 58 s (588.5 MB each):
   about 22 MB/s of set data through the Docker file server. The Postgres store failed before
   any write with "Permission denied" (§3, the 0700 dump folder); the run exited 1 and pruned
   nothing. The lock and the running mark were gone at its end.
3. **The fix and the second copy, 15:35:26 to 15:43:53 (507 s).** After the script fix and the
   `chmod` of the three existing sets, the Postgres sets copied in 167, 168 and 169 s (2.9 GB
   each); the six other sets read `COPY SKIP`; exit 0; `nightly-backup-offsite.last` reads
   `2026-10-17 0 2026-10-17T15:43:53Z`. `--status`: postgres, duckdb and bundle MIRRORED, 3 sets
   each. A third run skipped all nine sets in 2 s.
4. **The gauge.** The first background read after the deploy saw an empty folder (-1); the read
   at about 15:31 saw two DuckDB sets (29.5 h, the second night's set) and no bundle; the read
   at about 15:41 saw DuckDB and bundle at 5.7 h, and Postgres still at its second set. Each
   reading aged by the seconds since its read between scrapes, as designed. The refresh is
   600 s, so a reading taken in the middle of a copy shows the sets complete at that moment.
   At 16:04 the two gauges agreed for every store: the local sets 5.94 to 5.96 h old, the
   off-site copies 5.97 to 5.99 h.
5. **The upload, done at 16:45:04.** Watched on the host by the files the OneDrive client turns
   into placeholders (the `ReparsePoint` attribute), with a poll every two minutes. The small
   files went first; the last file, a 3.77 GB DuckDB copy, became a placeholder at 16:45:04.
   From the first copied file (15:22) that is 1 h 23 min for 20.8 GiB, about 4.5 MB/s on
   average over Wi-Fi (a 30 s adapter sample read 5.0 MB/s, 40 Mbit/s). One night's 7.5 GB
   therefore reaches the cloud in about half an hour after the 10:30 copy. The §6 step 7
   estimate of 20 to 50 minutes per night's sets holds.
6. **The drill from the copy, 15:44:50 to 15:59:30 (881 s): PASS.** `make backup-drill
   OFFSITE=1` resolved the OneDrive folder from `.env`, started the scratch cluster with the
   Postgres set `20261017T100000Z` mounted from that folder, and restored it in 760 s; every one
   of the 36 tables landed at its exact count (the 10:00 dump ran with no live writer), and
   `RESTORE OK` was the last line. The data verify ran in a one-off `jobs` container with
   `--backups-root /offsite` in 117 s: the DuckDB copy's hashes and table counts match, and the
   bundle copy loaded with the production loader in 10.8 s. The scratch container and volume
   were removed. The report is `drills/20261017T154450Z.json` in the OneDrive folder, with
   `"source": "offsite"`.
7. **The baseline before the first scheduled run, 19:10 UTC.** The OneDrive folder holds nine
   set folders (the 2026-10-15, -16 and -17 sets of each store), 373 files, 20.85 GB, every file
   uploaded (a placeholder). The OneDrive client's registry key carries no quota figure, but it
   says the account's Microsoft 365 subscription is active with auto-renew (expiry in mid-2027),
   so the plan is the 1 TB one. The owner read the storage figure on the OneDrive settings page
   at about 19:40 UTC: **382.4 GB used of 1.0 TB (37%)**, with the nine sets (20.85 GB) uploaded.
   That is the before figure for the recycle-bin question (§9). The first scheduled run of
   2026-10-18 (10:30 UTC) copies one new set per store (about 7.5 GB) and prunes the 2026-10-15
   set of each store off-site (about 7.4 GB). If the figure after the upload reads about
   382 GB, the recycle bin does not count against the quota; if it reads about 390 GB, it does.
   The owner reads the figure again after the run; the run's readings go here as item 8.
8. **The first scheduled run (2026-10-18, a Sunday).** The scheduler started
   `nightly-backup-offsite` at 10:30:00 UTC and reported it finished at 10:37:49, "failed:
   false". The night's chain before it: the finals load 07:00, the Sunday sweep 08:00, the
   rebuild to 09:29, the data backup to 10:06. The copy found no job running, so it did not
   wait; the guard asked for 10.6 GiB of 101.0 GiB free (C: now holds the sets twice, local and
   off-site, until OneDrive dehydrates the copies). It copied the three new sets
   (`20261018T100000Z`: Postgres 2,897,521,568 bytes in 183 s, DuckDB 4,068,760,722 bytes in
   229 s, the bundle 588,584,471 bytes in 53 s), skipped the six sets already off-site, and
   pruned the `20261015T100000Z` set of each store off-site (10:33, 10:36, 10:37), the first
   off-site prune. `nightly-backup-offsite.last` reads `2026-10-18 0 2026-10-18T10:37:49Z`; no
   running mark and no copy lock were left. At 10:55 `--status` read every store MIRRORED with
   3 sets, both gauges agreed at 0.76 to 0.83 h, and the OneDrive folder held exactly the
   2026-10-16, -17 and -18 set of each store, every one with its manifest, and no 2026-10-15
   folder. The upload: 374 of 375 files were placeholders at 10:56; the last, the day's 3.79 GB
   DuckDB copy, became one at 11:03:39, 26 minutes after the copy ended, inside the half-hour
   estimate of item 5. The quota after the first prune: the owner read the OneDrive storage page
   at about 11:15 UTC, **about 382 GB used of 1.0 TB**, the same as the 382.4 GB before the run.
   The run added 7.6 GB and pruned 7.4 GB, so a pruned set in OneDrive's recycle bin does NOT
   count against the quota on this personal account. The standing footprint off-site is three
   nights' sets, about 21 GB, and the quota headroom is about 618 GB.
