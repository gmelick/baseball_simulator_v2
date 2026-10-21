# Operator's manual

This manual tells you how to run the platform on this laptop: how to bring the
stack up from a clean checkout, what runs each night, how to check each part,
how to load and rebuild data, how to back up and restore, and what to do when
something breaks.

The stack runs Python 3.13 in Docker. The analytics file is at
DuckDB schema **v32**, and the statistics database is at
Alembic head **0030**.

**Shell.** Every command here is for Windows Command Prompt (cmd.exe), the
primary shell. Two cmd rules matter:

- cmd reads a bare `<` as a redirect, so the placeholders below are words in
  capitals (`PASTE_ONE_KEY_HERE`), never `<...>`.
- `&` separates commands in cmd, so every URL with a query string is in double
  quotes.

**Git Bash rewrites container paths.** In Git Bash, a path such as
`/app/scripts/x.py` in a `docker compose exec` or `docker compose run` command
becomes `C:/Program Files/Git/app/scripts/x.py` before Docker sees it. The tell
is `C:/Program Files/Git/` in the error. Run those commands from cmd.exe, or
prefix them with `MSYS_NO_PATHCONV=1` in Git Bash.

---

## 1. The stack

[`docker-compose.yml`](docker-compose.yml) defines ten services.

| Service | What it is | Host port | Memory cap |
|---|---|---|---|
| `db` | PostgreSQL 15, the statistics database (`raw` and `sim` schemas) | `127.0.0.1:5432` | none |
| `redis` | Redis 7: the simulation cache and the live service's message bus | `127.0.0.1:6379` | 256 MB in Redis |
| `app` | The FastAPI application (uvicorn with `--reload`) and its simulation worker pool | `127.0.0.1:8000` | 16 GB |
| `live` | The live service: the schedule poll, the per-game push feeds, the posted-lineup writer, the pre-game odds | none (its own health check on 8001 inside) | 1 GB |
| `jobs` | An idle container the scheduler runs the nightly data jobs in, as `appuser` | none | `JOBS_MEM_LIMIT`, default 10 GB |
| `nginx` | The built frontend and the reverse proxy, the one service the LAN reaches | `0.0.0.0:80` | none |
| `migrate` | A one-shot service that runs `alembic upgrade head` as the database owner | none | none |
| `prometheus` | The metrics store | `127.0.0.1:9090` | none |
| `grafana` | The dashboards | `127.0.0.1:3000` | none |
| `scheduler` | Ofelia, the cron that runs the nightly jobs inside other containers | none | none |

Every service except `migrate` restarts by itself (`restart: unless-stopped`).
[`deploy/README.md`](deploy/README.md) explains nginx, the ports, the logins and
the environment tiers.

### Where the data lives

| Store | Where | Who writes it |
|---|---|---|
| The statistics database | the `postgres_data` volume | the loaders, the live service, the app's run records |
| The analytics file | `/data/baseball_sim.duckdb` on the `duckdb_data` volume | the nightly rebuild and the pool rebuild scripts |
| The engine-artifact bundle (the files the simulator draws from) | `/data/play_pool/engine_artifacts`, a link to one folder under `engine_artifacts.versions/` | the bundle build |
| The replay file (one simulated game per run, for the game page) | `/data/replay.duckdb` | the app only |
| The calibration report | `/data/calibration.json` | `make calibrate`, `make validate-props` |
| The job marks | `/data/ingest_marks/` | `scripts/with_mark.sh` |

### Which code a container runs

The `app` service mounts five folders from the checkout: `api/`, `pipeline/`,
`similarity/`, `simulation/` and `db/`. The `jobs` service mounts the same
five. `scripts/`, `betting/` and `tests/` are baked into the image at build
time.

- A change to a mounted folder reaches the app after `docker compose restart app`.
  uvicorn's `--reload` may restart it by itself, but a file event does not
  always cross the Windows bind mount: check the log.
- A change to `scripts/` or `betting/` reaches a container only after a rebuild:
  `docker compose build app`, then `docker compose up -d app` (and the same for
  `jobs`).
- A change to a service's `environment:` block in `docker-compose.yml` needs a
  recreate (`docker compose up -d app`). A restart keeps the old environment.
- Do not edit an `api/` file to import a new name from `betting/` before the
  image has that name. The reload then fails to import, and the app is down
  until you rebuild.
- Each image build adds to Docker's build cache on C:. The cache once filled the
  disk. `docker builder prune -f` frees it.

---

## 2. Bring the stack up from a clean checkout

### 2.1 What you need

| Tool | Notes |
|---|---|
| Docker Desktop | with the WSL2 back end. The app alone may use 16 GB, so give the WSL VM about 20 GB (`.wslconfig`). |
| Git for Windows | the checkout |
| GNU Make | the `make` targets (`choco install make`) |
| Python 3.13 with the `py` launcher | the Makefile's helper lines, `scripts/gen_secrets.py` and the restore drill run on the host |
| curl | ships with Windows |

### 2.2 The steps

```bat
git clone https://github.com/gmelick/baseball_simulator_v2.git
cd baseball_simulator_v2
copy .env.example .env
py scripts\gen_secrets.py --append .env
make dev-bg
make migrate
```

1. `gen_secrets.py` fills the eight secrets `.env` lacks: the three database
   passwords, the Redis and Grafana passwords, `SECRET_KEY`, `AUTH_PASSWORD`
   and `API_KEYS`. It never changes a secret that has a value, and it prints
   key names only. Every compose command fails, naming the variable, until
   each password is set.
2. Set `BACKUP_DIR` and `OFFSITE_BACKUP_DIR` in `.env` before the first night
   (section 8). Without them the backups land inside the repository.
3. `make dev-bg` builds the images and starts the stack in the background. The
   first build takes several minutes: it builds the Python image and the
   frontend.
4. `make migrate` applies every Alembic migration once the database is
   healthy.
5. A new database volume creates the two app roles on its first start
   ([`db/roles/README.md`](db/roles/README.md)). On an older volume, run
   `make db-roles`.
6. Give Prometheus its key: follow
   [`deploy/monitoring/secrets/README.md`](deploy/monitoring/secrets/README.md).

**What good looks like.**

```bat
docker compose ps
:: Expected: every service up; each one with a health check reads "(healthy)".
:: The scheduler has no health check. migrate is not listed: it runs only when called.
docker compose run --rm migrate alembic current
:: Expected: 0030 (head)
curl http://localhost:8000/health
:: Expected: {"status":"ok", ... "auth_enforced":true, "sql_console":{"ro_pool":true,"role":"baseball_ro"}}
```

Until the first data load and bundle build finish (section 2.3), the app boots
but cannot simulate: a simulation fails at its first game with no bundle.

### 2.3 The first data load

The order matters: each step reads what the one before it wrote. The game load
takes hours. A crash or a blue screen loses no data: the loader skips every game
already loaded, so a rerun resumes.

| Step | Command | What it fills |
|---|---|---|
| 1a. The completed seasons | `docker compose run -d --rm --name first-load app sh /app/scripts/with_retry.sh 6 python -m pipeline.etl.etl_historical_loader` | Every Final game from 2017 through last season: `raw.games`, `raw.pitches`, `raw.play_events`, the lineups, the official box score (`raw.game_player_stats`) and the bullpen listing (`raw.game_bullpen`). Follow it with `docker logs -f first-load`. |
| 1b. The current season | `docker compose run --rm app sh /app/scripts/with_retry.sh 6 sh /app/scripts/weekly_refresh.sh` | The same tables for this season's Final games. The bare loader of step 1a stops at last season. |
| 2. Sprint speed | `docker compose run --rm app python -m pipeline.etl.etl_sprint_speed_loader --seasons 2023 2024 2025 2026` | `raw.sprint_speed` |
| 3. The Savant leaderboards | `docker compose run --rm app python -m pipeline.etl.savant_loader --seasons 2023 2024 2025 2026` | the `raw.savant_*` tables |
| 4. The profiles and pools | `docker compose run --rm app python -m pipeline.batch.player_profile_computor --seasons 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026 --full-rebuild` | the `derived.*` profiles and the `sim.*` play pools in the analytics file |
| 5. The bundle | `make engine-artifacts` | a new bundle version; the app restarts itself on it |
| 6. The calibration | section 6 | `/data/calibration.json` |
| 7. The odds (optional) | `make load-historical-odds FLAGS="--seasons 2024 --provider bettingpros"` | `raw.game_odds`, `raw.prop_odds` (needs `ODDS_API_KEY`) |

The simulator's pools read the last three completed seasons plus the current
one, so steps 2 and 3 name those four seasons.

### 2.4 Host tools and the database

A script you run on the host (not in a container) reads `BASEBALL_DB_DSN`. No
script has a default. Use the app role:

```bat
set BASEBALL_DB_DSN=postgresql://baseball_app:PASTE_APP_PASSWORD@localhost:5432/baseball_sim
:: To keep it for new windows (open a NEW cmd window afterwards):
setx BASEBALL_DB_DSN "postgresql://baseball_app:PASTE_APP_PASSWORD@localhost:5432/baseball_sim"
```

`PASTE_APP_PASSWORD` is the value of `BASEBALL_APP_DB_PASSWORD` in `.env`. The
containers ignore this variable: compose builds their connection strings.

---

## 3. What runs each day

[`deploy/ofelia/config.ini`](deploy/ofelia/config.ini) holds the schedule. The
times are UTC; Eastern time is four hours earlier in summer and five in winter.

| Time (UTC) | Job | Runs in, as | What it does | What it waits for |
|---|---|---|---|---|
| 07:00 daily | `nightly-finals` | `jobs`, `appuser` | `scripts/nightly_finals.sh`: loads the Final games of the last three days (Eastern dates). Postgres only. | nothing |
| 08:00 Sunday | `weekly-refresh` | `jobs`, `appuser` | `scripts/weekly_refresh.sh`: sweeps the whole current season for a Final game the window missed. Postgres only. | nothing |
| 08:00 daily | `nightly-rebuild` | `jobs`, `appuser` | `scripts/nightly_rebuild.sh`: rebuilds the current season's profiles and pools, then builds and publishes a new bundle version. Ends near 09:30. | the finals load and the Sunday sweep (see below) |
| after the rebuild | the app's restart | `app` | When the bundle link moves, the app waits for its running simulation jobs (at most 10 minutes) and for any DuckDB writer, then restarts itself to load the new data ([`api/bundle_watch.py`](api/bundle_watch.py)). It is down for a few minutes. | the run jobs |
| 10:00 daily | `nightly-backup-postgres` | `db`, `postgres` | `db/backup/pg_backup.sh`: dumps the statistics database. | its own lock |
| 10:00 daily | `nightly-backup-data` | `jobs`, `appuser` | `python -m pipeline.backup.data_stores`: copies the analytics file, the bundle and the small `/data` files. | the rebuild, finals and sweep marks |
| 10:30 daily | `nightly-backup-offsite` | `jobs`, `appuser` | `python -m pipeline.backup.offsite`: copies the newest backup sets into the OneDrive folder. Takes about 10 to 25 minutes. | the data backup's mark and the Postgres backup's lock |
| 10:30 daily | `nightly-closing-lines` | `app`, root | `scripts/nightly_closing_lines.sh`: loads the closing odds of yesterday's and the day before's games, as the fallback to the live service. Runs only with `ODDS_PROVIDER=bettingpros` (the job sets it) and `ODDS_API_KEY` in `.env`. | nothing |

Each data job runs inside two wrappers:

- [`scripts/with_retry.sh`](scripts/with_retry.sh) reruns the command after a
  crash (exit 139, or "Fatal Python error" in its output), 30 seconds apart.
  Any other failure stops it at once.
- [`scripts/with_mark.sh`](scripts/with_mark.sh) writes
  `/data/ingest_marks/<job>.running` while the job runs, then
  `<job>.last` with one line: `<UTC date> <exit code> <UTC timestamp>`.

The waits read those marks. The rebuild waits 60 s, then waits while the finals
load or the Sunday sweep runs (at most 2 hours). When the finals load has not
run that day, the rebuild runs it first ([`scripts/wait_for_ingest.sh`](scripts/wait_for_ingest.sh)).
A mark older than 6 hours counts as a dead run's.

**The deploy window.** Recreate `jobs`, `app` or `scheduler` only after about
11:00 UTC, and only when `/data/ingest_marks/` holds no `.running` mark. A
stopped `jobs` container fails all five of its jobs; Ofelia logs the error.

**Check last night.**

```bat
docker compose exec -T jobs ls /data/ingest_marks
docker compose exec -T jobs cat /data/ingest_marks/nightly-finals.last
docker compose exec -T jobs cat /data/ingest_marks/nightly-rebuild.last
:: Expected: today's UTC date, then 0, then the end time.
docker compose logs --since 24h scheduler
```

---

## 4. Health checks

Every `/api/` route needs a credential. In the commands below, `%API_KEY%` is
one of the comma-separated values of `API_KEYS` in `.env`:

```bat
set API_KEY=PASTE_ONE_KEY_HERE
```

### 4.1 The app

```bat
curl http://localhost:8000/health
:: Expected: "status":"ok", "auth_enforced":true and "sql_console":{"ro_pool":true,...}
curl http://localhost:8000/ready
:: Expected: "status":"ready"; "checks" holds "postgres":"ok", "live_service":"ok",
:: "pitcher_engine":"ok". A failed Postgres or Redis check answers 503 "degraded".
docker compose logs app | findstr /C:"build_all_engines" /C:"replay store OPEN" /C:"watching"
:: Expected: "build_all_engines: 11/11 engines built.",
::           "SIM-561: replay store OPEN at /data/replay.duckdb.",
::           "SIM-563: watching /data/play_pool/engine_artifacts for a new bundle version."
```

### 4.2 The statistics database

```bat
docker compose exec db pg_isready -U baseball_user -d baseball_sim
:: Expected: accepting connections
docker compose run --rm migrate alembic current
:: Expected: 0030 (head)
docker compose exec db psql -U baseball_user -d baseball_sim -c "SELECT season, COUNT(*) FROM raw.games WHERE status = 'Final' GROUP BY season ORDER BY season;"
```

`alembic current` must print the newest file's revision under
`db/migrations/versions/`.

### 4.3 The analytics file

```bat
type db\schemas\duckdb_schema_version.txt
:: Expected: 32
docker compose exec -T app python -c "import duckdb; c = duckdb.connect('/data/baseball_sim.duckdb', read_only=True); print(c.execute('SELECT pool_name, season, builder_version FROM sim.pool_build_metadata ORDER BY 1, 2').fetchall())"
```

The version file must equal the newest file's number under
`db/migrations/duckdb/`. The builder version of each pool must be the one in
[`pipeline/batch/profiles/pool_meta.py`](pipeline/batch/profiles/pool_meta.py)
(`POOL_BUILDER_VERSION`).

A DuckDB writer opens the file only when no other process holds it, read-only
handles included. A short read like the one above is safe, but never during the
nightly rebuild (08:00 to about 09:30 UTC). The replay file
`/data/replay.duckdb` is different: the app holds it open for writing, so no
other process can open it while the app runs. Read a stored game through the
API (`/linescore`, `/plays`).

### 4.4 Redis, the live service and monitoring

```bat
docker compose exec redis redis-cli ping
:: Expected: PONG
docker compose ps live
:: Expected: running (healthy)
make live-logs
```

- Prometheus: `http://127.0.0.1:9090/targets` shows `baseball-sim-app` UP.
- Grafana: `http://127.0.0.1:3000`, folder "Baseball Sim", the platform dashboard.
- The app's metrics:

  ```bat
  curl -s -H "X-API-Key: %API_KEY%" http://localhost:8000/metrics | findstr baseball_sim_
  ```

  `baseball_sim_finals_age_hours` is the age of the newest Final game. The two
  backup gauges are in section 8.

### 4.5 A simulation

```bat
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/games/2026-09-27"
:: The day's games, from the league schedule.
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/games/GAME_PK/simulate?n_iterations=100"
:: Runs 100 games now and returns the summary: win shares, scores, the inning grids.
curl -X POST -H "X-API-Key: %API_KEY%" -H "Content-Type: application/json" -d "{\"n_iterations\": 100}" "http://localhost:8000/api/games/GAME_PK/simulate"
:: Queues a durable run and returns its run_id; poll it:
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/games/GAME_PK/simulate/runs/RUN_ID"
curl -X POST -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/games/GAME_PK/sample-game"
:: Plays ONE game and stores it in the replay file for /linescore and /plays.
```

A 100-game run takes about 25 to 40 seconds on this laptop. The full route list
is at `http://localhost:8000/docs`.

### 4.6 Checks before a commit

```bat
make test-unit
make lint
make type-check
make test-regression
```

These targets mount the checkout over the image's copy, so they test your
working tree. `make test-unit TESTS="tests/unit/test_x.py"` runs named files.
Run them from the main checkout: from a worktree, compose builds a separate
project. [`docs/technical/README.md`](docs/technical/README.md) leads to the
test lanes and the rest of the code reference.

---

## 5. Data loads and rebuilds

### 5.1 Catch up on missed games

```bat
make ingest-catch-up DAYS=14
```

It loads the Final games of the last 14 days through the crash wrapper. It
writes Postgres only, so it is safe while the app runs. The next nightly rebuild
picks the games up.

### 5.2 Rebuild the profiles and pools

The nightly rebuild already does the current season. A bare
`player_profile_computor` run also builds the current season only. For a full
recompute, name every season:

```bat
docker compose run --rm app python -m pipeline.batch.player_profile_computor --seasons 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026 --full-rebuild
make engine-artifacts
```

- **Include the season in progress.** Each profile table carries one cutoff
  date (`asof_date`), and the computor refuses a rebuild that would leave a
  table with two. A partial rebuild on a later day must therefore include the
  current season.
- **Rebuild the bundle after the profiles.** The simulator reads the bundle, not
  the analytics file. The app restarts itself when the new bundle version
  publishes.
- **Refit the calibration** after a full recompute (section 6).
- The app holds no handle on the analytics file, so a rebuild runs while the app
  serves. A second writer cannot: never run a rebuild during the nightly one.

### 5.3 The bundle

`make engine-artifacts` builds every part of the bundle (`--what all`). With no
`--out-dir`, the build copies the current version, builds into the copy and
then moves the link in one step. A failed build moves nothing. The build keeps
three versions plus any version a running app marks as in use. An explicit
`--out-dir` writes in place and publishes nothing. `FLAGS="--what pool"`
rebuilds the pools alone.

### 5.4 The pool rebuild scripts

Two scripts rebuild one pool end to end, with checks before and after:
[`scripts/rebuild_pitch_pool.py`](scripts/rebuild_pitch_pool.py) and
[`scripts/rebuild_steal_pool.py`](scripts/rebuild_steal_pool.py). Each one's
docstring is its run book. One rule: on a database without DuckDB migration
0031, apply it only through `rebuild_steal_pool.py --apply-migration`. That flag
applies the migration inside the rebuild's transaction. Never apply 0031 as a
separate step: the old pool builder fails on a migrated table.

### 5.5 Odds and box scores

```bat
make load-historical-odds FLAGS="--seasons 2024 --provider bettingpros"
make load-historical-odds FLAGS="--seasons 2024 --max-games 5 --provider bettingpros --book draftkings"
```

The loader writes the opening and closing lines of Final games, one row per
book, into `raw.game_odds` and `raw.prop_odds`. It needs `ODDS_API_KEY` in
`.env`. It refuses the mock provider unless you name it. The historical loader
writes each game's official box score as it loads the game;
[`scripts/load_official_boxscores.py`](scripts/load_official_boxscores.py)
backfills older games.

### 5.6 Validate a data change at scale

Check an ETL change on hundreds of games, never dozens. Two review rounds found
defects only in real payloads at scale; a 70-game sample once read "100%" on a
metric that 950 games disproved.

---

## 6. Calibration

The app loads `/data/calibration.json` once, at boot. The report calibrates the
similarity engines and carries the win-probability reliability curve. A missing
file leaves the engines on their defaults and the win probabilities
uncalibrated; the app still boots.

```bat
make calibrate
make validate-props FLAGS="--seasons 2024 --iterations 100 --write-calibration"
docker compose restart app
```

**The trap.** `make calibrate` rewrites the whole file, and it writes no
reliability curve. A run therefore DROPS the curve. Write the curve back before
the next boot: rerun `make validate-props` with `--write-calibration` (it
simulates real games, so it is slow), or reuse the last validation report:

```bat
docker compose exec -T app python -c "from simulation.prop_validation import PropValidationReport, write_reliability_curve_to_calibration_report as w; r = PropValidationReport.from_json(open('/data/prop_validation.json').read()); print(w(r, '/data/calibration.json'))"
:: Expected: True
docker compose restart app
```

---

## 7. Simulation flags

Every simulator setting is a `SIM_*` environment variable.

- The code default of each flag lives in
  [`simulation/flags.py`](simulation/flags.py), the registry of every flag.
- Production's values live in the `app` service's `environment:` block of
  `docker-compose.yml`. A change there needs `docker compose up -d app`.
- `tests/conftest.py` pins most simulation flags off, so the unit lane runs the
  plain baseline.
- [`docs/technical/sim-loop-cheat-sheet.md`](docs/technical/sim-loop-cheat-sheet.md)
  explains each draw, its weights and the production value of each flag.

Two owner rulings govern a change: every change lands ON at its best-known
default (owner ruling 2026-09-16), and every similarity power stays at 1 until
the sweep fits the weights together (owner ruling 2026-09-16). A change is gated
by the unit and regression lanes and a ten-game smoke of the affected channels
(`scripts/sim_stats.py`).

---

## 8. Backups, the off-site copy and the restore drill

The scheduled backups copy three stores every night: the statistics database,
the analytics file and the published bundle. A **set** is one backup of one
store.

### 8.1 Where the sets go

| `.env` key | Value on this laptop | Mounted at |
|---|---|---|
| `BACKUP_DIR` | `C:/Users/grego/Documents/baseball_backups` | `/backups`: read-write in `db` and `jobs`, read-only in `app` |
| `OFFSITE_BACKUP_DIR` | `C:/Users/grego/OneDrive/Documents/baseball_backups` | `/offsite`: read-write in `jobs`, read-only in `app` |

Without the keys, compose uses `.\backups` and `.\backups_offsite` inside the
repository. Git and Docker ignore both, and neither is off the machine.

```
BACKUP_DIR\
  postgres\<TS>\     dump\ (pg_dump directory format), counts_before.tsv, counts_after.tsv, manifest.json
  duckdb\<TS>\       baseball_sim.duckdb [+ .wal], data_files\ (calibration.json, prop_validation.json, ingest_marks\*.last), manifest.json
  bundle\<TS>\       <build id>\ (a copy of the published version), manifest.json
  drills\<TS>.json   one report per restore drill
```

`<TS>` is the run's UTC start, `YYYYmmddTHHMMSSZ`. A local set is written as
`<TS>.partial`, its `manifest.json` goes last, and then the folder is renamed. A
set without a manifest never counts, and the next run deletes every `.partial`
folder. Each store keeps 3 sets (`BACKUP_KEEP`). The off-site copy writes each
file straight into `<store>\<TS>\`, manifest last, and keeps 3 sets per store
(`OFFSITE_KEEP`, else `BACKUP_KEEP`). The replay file is not backed up.

**What the sets protect against.** The local sets survive a loss or a reset of
Docker's disk image, `docker compose down -v` and `make nuke`, a bad migration
or rebuild, a writer that dies in the middle of a rebuild, and an accidental
deletion. They sit on C:, the only disk, so they do not survive the loss of the
laptop. The off-site copy covers that loss once the OneDrive client has uploaded
it. The job cannot see the upload: the OneDrive tray icon and Explorer's status
column are the truth. Never point `OFFSITE_BACKUP_DIR` at `BACKUP_DIR`; the copy
refuses one folder twice.

**The free-space guard.** Each backup needs the larger of 10 GiB and 1.5 times
the newest set free. When the disk is short, it deletes the oldest set while
more than 2 remain; otherwise it refuses with exit 2.

### 8.2 Back up now, and check

Run these from cmd.exe. Do not run `make backup` and `make backup-offsite` at
the same time: the backup can prune a local set the copy is reading.

```bat
make backup
:: Expected: BACKUP OK postgres <TS> ...   then   BACKUP OK duckdb <TS> ...   and   BACKUP OK bundle <TS> ...
make backup-offsite
:: Expected per set: COPY OK <store> <TS> ...   or   COPY SKIP <store> <TS> (already off-site)
make backup-status
:: Expected: each store's newest set under 24 h old, 3 sets each; the off-site block ends MIRRORED per store.
docker compose exec -T jobs cat /data/ingest_marks/nightly-backup.last
docker compose exec -T jobs cat /data/ingest_marks/nightly-backup-offsite.last
:: Expected: today's UTC date, then 0, then the end time.
curl -s -H "X-API-Key: %API_KEY%" http://localhost:8000/metrics | findstr backup_age
:: baseball_sim_backup_age_hours{store="..."} and baseball_sim_offsite_backup_age_hours{store="..."}:
:: under 24; -1 means the store has no complete set.
```

`make backup` passes `--new-set`, so it always makes a fresh set.
`docker compose exec -T jobs python -m pipeline.backup.offsite --dry-run` prints
what the copy would do and writes nothing.

### 8.3 The restore drill

The drill proves a set restores. It never touches the live database.

```bat
make backup-drill
make backup-drill SET=20261015T100000Z
make backup-drill OFFSITE=1
```

[`scripts/backup_drill.py`](scripts/backup_drill.py) runs on the host. It
starts a scratch Postgres container (`sim525_drill_pg`, no network), restores
the set with the real `pg_restore.sh`, verifies the analytics-file and bundle
copies in the `jobs` container, removes the scratch container and writes a
report to `drills\<TS>.json`. It refuses to start while any `sim525_*` container
or volume exists. The scratch cluster lives on a Docker volume, so it needs
about 20 GB inside Docker's disk image while it runs. `OFFSITE=1` restores from
the off-site copy; a set that OneDrive made online-only downloads first.

### 8.4 Restore a store

The full run books are
[`docs/audit/2026-10-14-sim525-backups-plan.md`](docs/audit/2026-10-14-sim525-backups-plan.md)
§6 and, for the off-site copy,
[`docs/audit/2026-10-17-sim568-offsite-copy.md`](docs/audit/2026-10-17-sim568-offsite-copy.md)
§7. Pick the set with `make backup-status`. The `db` container has no
`/offsite` mount: to restore from the off-site copy, first copy the set folder
back into `BACKUP_DIR`.

```bat
:: The statistics database. The target must be empty (a new postgres_data volume);
:: the script refuses a populated database with exit 3.
make restore-postgres SET=PASTE_TS
:: Expected last line: RESTORE OK /backups/postgres/PASTE_TS
make db-roles
docker compose run --rm migrate alembic current
:: Expected: 0030 (head)

:: The analytics file: checks the size and sha256, moves a live .wal aside, replaces the file.
docker compose stop scheduler
docker compose exec -T jobs python -m pipeline.backup.data_stores --restore-duckdb PASTE_TS --yes
docker compose restart app
docker compose start scheduler

:: The bundle: copies the version back and publishes it; the app restarts itself.
docker compose exec -T jobs python -m pipeline.backup.data_stores --restore-bundle PASTE_TS --yes
```

The Postgres restore carries no owners or grants, so `make db-roles` puts the
grants back. `--restore-bundle` refuses a version folder that exists; `--force`
copies over it. No command restores the small files: copy them from the
analytics set's `data_files\` folder into `/data` by hand.

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `required variable POSTGRES_PASSWORD is missing a value` (or another secret) on any `docker compose` or `make` command | `.env` lacks a secret. Run `py scripts\gen_secrets.py --append .env`; then `py scripts\gen_secrets.py --check .env` exits 0. A worktree needs its own `.env`. |
| `password authentication failed for user "baseball_user"` (or `baseball_app`, `baseball_ro`) although `.env` looks right | The volume is older than the password: Postgres reads `POSTGRES_PASSWORD` only when it creates a volume. Run `make db-roles ROTATE_ADMIN_PASSWORD=1`; it resets all three passwords from `.env` over the socket. |
| `401 Unauthorized` from an `/api/` route | Send `X-API-Key: <key>` or `Authorization: Bearer <key>` with a key from `API_KEYS`, or log in in the browser. |
| The SQL console, the assistant or a Data Lab page answers 503 | The read-only pool is missing or refused. `/health` shows `"sql_console"`; `docker compose logs app \| findstr SIM-443e` names the reason. Run `make db-roles` if `baseball_ro` does not exist. |
| `permission denied for table ...` | The table is in a schema the roles do not cover. Add its lines to `db\roles\setup_roles.sql`, then run `make db-roles`. |
| The simulation fails with no bundle | No bundle is published yet. Run `make engine-artifacts`. |
| `Could not set lock on file "/data/baseball_sim.duckdb"` | Another process holds the analytics file. The nightly rebuild runs 08:00 to about 09:30 UTC; a script with a read-only handle also blocks a writer. Wait, or close the other process. |
| The app boots uncalibrated (identity win probabilities) | `/data/calibration.json` is missing, or `make calibrate` dropped the reliability curve. Section 6. |
| The site answers 502 for a few seconds after an app recreate, or a few minutes near 09:30 UTC | nginx re-resolves the app's new address within 10 s. Near 09:30 UTC the app restarts itself on the new bundle. Both are expected. |
| `Fatal Python error: Segmentation fault` (exit 139) in a container | A known, random crash of this image's Python. Treat it as a retry: the nightly jobs rerun by themselves (`with_retry.sh`), and the loaders resume. |
| Ofelia logs `No such container: baseball_simulator_v2-jobs-1` | The `jobs` container is stopped. Run `docker compose up -d jobs`; `docker compose ps jobs` shows it healthy. A stack under another project name needs its own `container =` lines in `deploy\ofelia\config.ini`. |
| A backup set is missing this morning (`make backup-status` shows a store over 24 h old, or a gauge reads -1) | 1. `docker compose logs scheduler \| findstr nightly-backup` shows each job's exit; an exec error means the `db` or `jobs` container was down at 10:00 UTC. 2. Exit 2 is a refusal before any write: the root is missing or read-only, the disk is short, or another run holds the lock. 3. The data job's exit 1 can mean a writer mark stayed fresh for 2 hours, or a writer held the analytics file for 30 minutes, so it skipped that store. 4. A `.partial` folder is a dead run's; the next run deletes it. Then run `make backup`. |
| The app fails to import after an `api/` edit | The edit imports a `betting/` name the image does not have. Rebuild: `docker compose build app`, then `docker compose up -d app`. |
| `C:/Program Files/Git/` in an error | Git Bash rewrote a container path. Use cmd.exe, or prefix the command with `MSYS_NO_PATHCONV=1`. |
| The disk fills during image builds | Run `docker builder prune -f`. |

**Do not** run interpreter-wide memory tracing (`PYTHONTRACEMALLOC=1`) on the
live app boot: it wedged Docker Desktop once. Profile memory in a separate,
memory-capped container.

---

## 10. Where to read more

- [`deploy/README.md`](deploy/README.md): nginx, the ports, the logins, the monitoring, the scheduler and the environment tiers.
- [`db/roles/README.md`](db/roles/README.md): the three database roles.
- [`docs/technical/README.md`](docs/technical/README.md): the code reference.
- [`docs/audit/2026-10-13-sim443-security-hardening.md`](docs/audit/2026-10-13-sim443-security-hardening.md): the security design and its cutover run book.
- `BACKLOG.xlsx`: the open tickets. `CHANGES.md`: what changed and when.
