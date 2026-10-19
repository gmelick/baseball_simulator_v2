# MLB Baseball Simulation Platform — End-to-End Workflow

*Last updated: 2026-10-09 (Alembic head 0030; DuckDB migration 0032; the 2026-06-04 Phase-7 refresh: Python 3.13 · DuckDB v32 · full API surface live)*

This document is the operator's manual.  It describes how to run the
platform end-to-end from a clean checkout, and how to confirm each
component is healthy.  Every command has a paired "what good looks
like" check so you can verify the state, not just whether the command
exited zero.

> **Shell.** All commands below are formatted for **Windows Command
> Prompt (cmd.exe)** — the user's primary shell.  PowerShell users can
> substitute `$env:VAR=…` for `set VAR=…` and `Invoke-WebRequest` for
> `curl`; bash users can substitute `export VAR=…` for `set VAR=…` and
> forward slashes for paths.

> **Phase note.** As of 2026-06-06 the platform is at **Phase 7 — live
> bring-up (largely complete)**; Phases 1–6 are COMPLETE and CI-green
> (Python 3.13 / numpy 2.x; 89% coverage; DuckDB v32 / Alembic 0030).  The
> full API surface (games, simulate, betting, WebSocket, odds, similarity,
> metrics) is live.  Calibration is LIVE (SIM-432; win-prob map = fitted
> reliability-curve), the full-pool sampler + all realism flags are ON in
> production (§1.8), and the SIM-435 odds backfill + SIM-429 CLV backtest
> chain is shipped + measured (§1.9).

---

## API endpoint reference (currently shipping)

The full Phase-5 API surface is live: the games router (`/api/games/*`,
including `/simulate`), the betting/CLV surface, the typed WebSocket
channel, the odds path, similarity, and metrics.  The **pitcher
distribution** similarity endpoint (per `api/routes/similarity.py`) is:

```
GET /api/similarity/pitcher/{pitcher_id}/{season}
  ?bins=20         (optional, 4-100, default 20)
  &top_n=20        (optional, 1-200, default 20)
```

**Note the path style.**  `pitcher_id` and `season` are *path* segments,
not query parameters.  An earlier draft of this doc had `/v1/similarity/pitcher?player_id=…`
which returns `{"detail":"Not Found"}` because that route doesn't exist.

Other endpoints (`/health`, `/ready`, `/`) are operational probes
defined in `api/main.py`.

---

## Part 1 — End-to-end workflow

### 1.1 Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker Desktop | latest | Required by `make dev`.  Ensure WSL2 backend is healthy. |
| Python | 3.13 | `pyproject.toml` pins 3.13 (SIM-431); CI + Docker + local unified. |
| Git for Windows | any | Includes Git Bash if you prefer bash semantics. |
| Make for Windows | GNU | `choco install make` or use the WSL2 alternative. |
| `curl` | any | Ships with Windows 10+. |
| `jq` (optional) | any | `choco install jq` — pretty-prints JSON. |

### 1.2 First-time setup

```bat
git clone <repo-url>
cd baseball_simulator_v2
copy .env.example .env
py scripts\gen_secrets.py --append .env
make dev-bg
make migrate
make test
```

`gen_secrets.py` fills every secret `.env` lacks: the three Postgres
passwords, the Redis password, the Grafana admin password, `SECRET_KEY`,
`AUTH_PASSWORD` and `API_KEYS` (SIM-443). It prints key names only, never a
value. Every compose command fails, naming the variable, until each is set.
A fresh Postgres volume creates the two app roles on first start
(`db/roles/README.md`); on an existing volume run `make db-roles`.

Postgres, Redis, the app (:8000), Prometheus and Grafana listen on
`127.0.0.1` only; another machine on the LAN reaches the site through nginx
on :80. Every `/api/` route needs a credential: below, `%API_KEY%` is one of
the comma-separated `API_KEYS` values in `.env` (`set API_KEY=<one key>`).
`/health` and `/ready` stay open.

**What good looks like.**
- `make migrate` ends with `alembic current` printing the head revision — the newest file under `db/migrations/versions/` (`0030` as of 2026-10-09).
- `make test` exits 0 (unit suite green at 89% coverage; ~22 slow/skipped).

### 1.3 Local Python development (without Docker)

```bat
py -3.13 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest
```

You will still need PostgreSQL and Redis running somewhere — either
via `docker compose up db redis` or installed locally.

### 1.4 Loading data

Host tools read `BASEBALL_DB_DSN`; none has a built-in default. Use the app
role and replace the `<...>` placeholder with the value from `.env` (cmd
reads a bare `<` as a redirect).

```bat
set BASEBALL_DB_DSN=postgresql://baseball_app:<BASEBALL_APP_DB_PASSWORD from .env>@localhost:5432/baseball_sim

python pipeline\etl\etl_historical_loader.py --season 2024 --dsn %BASEBALL_DB_DSN%
python pipeline\etl\etl_sprint_speed_loader.py --season 2024
python pipeline\etl\venue_backfill_job.py
python pipeline\etl\opening_line_job.py
```

After raw load, materialize derived profiles + simulation pools into DuckDB:

```bat
python pipeline\batch\player_profile_computor.py ^
    --seasons 2022 2023 2024 ^
    --full-rebuild ^
    --dsn %BASEBALL_DB_DSN% ^
    --duckdb-path db\schemas\baseball_simulator.duckdb
```

### 1.5 Live ingestion pipeline

```bat
set LIVE_GAME_PKS=745000,745001
python pipeline\live\live_ingestion_pipeline.py
```

### 1.6 Hitting the API

With `make dev-bg` running:

```bat
:: Liveness / readiness
curl http://localhost:8000/health
curl http://localhost:8000/ready

:: OpenAPI surface
curl http://localhost:8000/openapi.json > openapi.json
:: With jq:
::   curl http://localhost:8000/openapi.json | jq ".paths | keys"

:: Pitcher similarity distribution (CORRECT ENDPOINT)
:: Path: /api/similarity/pitcher/{pitcher_id}/{season}
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/similarity/pitcher/605400/2024"

:: With histogram + top-N tuning:
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/similarity/pitcher/605400/2024?bins=30&top_n=10"
```

> **cmd URL gotcha.**  `&` is a command separator in cmd.exe.  Either
> escape as `^&` or wrap the URL in double quotes (the doc uses
> double-quoted URLs throughout).

### 1.7 Run a simulation

The simulation loop is live (Phase 4 complete).  Kick off a 100-iteration
run for a game:

```bat
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/games/745000/simulate"
:: Returns win probabilities, the score spread and every iteration's score.
```

At 6 workers (forkserver + a 10 GB app `mem_limit`, SIM-430), an n=100
batch runs in ~38 s with no OOM.

**One game, pitch by pitch (SIM-561).** The game page's "Simulate a game"
button, or this call, plays ONE game and stores it in the replay file
(`/data/replay.duckdb`, which only the app opens):

```bat
curl -X POST -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/games/745000/sample-game"
:: Returns {game_pk, run_id, base_seed}; add ?base_seed=N to replay a game.
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/games/745000/linescore"
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/games/745000/plays"
:: Each play carries its inning, half, outs, batter, pitcher and the score after;
:: "names" maps the player ids to names.
```

Loading projections (`/boxscore`) also stores its run's first game. The replay
reads serve the newest stored game, and the file keeps the newest 5 per game.
**What good looks like:** the boot log says `SIM-561: replay store OPEN at
/data/replay.duckdb`; a sample game takes about 5 s.

### 1.8 Production simulation flags

The simulation path is the **full-pool similarity sampler** — the only
path since SIM-486 deleted the per-tile fallback (2026-09-06) — and every
realism factor is a DRAW WEIGHT with a fitted bandwidth, **ON** in the
docker-compose `app` env.  The same knobs are **pinned OFF in
`tests/conftest.py`** so the unit suite exercises the byte-identical
baseline (each knob's own tests opt back in explicitly).  Do not change
them ad hoc — they are a validated set.

| Flag | Ticket | Default in compose | What it does |
|---|---|---|---|
| `SIM_MANAGER` | SIM-434/427 | `1` | Manager starter-pull + reliever-selection decisions (fatigue/leverage/TTO).  Enabled + validated 2026-06-04 (pitchers/game 2→9, runs unchanged). |
| `SIM_BB_PLATOON` | SIM-413 | `1` | Batted-ball draw reweight by pitcher hand (L/R platoon). |
| `SIM_HOME_OFF_WEIGHT` | SIM-491/476 | `0.0` | The home-field draw weight on batted-ball rows whose batting side mismatches the live one (`1.0` = off; `0.0` = hard conditioning, the owner ruling). |
| `SIM_PARK_KERNEL_SIGMA` | SIM-491/476 | `0.02` | The park kernel bandwidth over the venue run factor (`0` = off). |
| `SIM_ACTOR_POWER_FIELDER` | SIM-523 | `1.2` | The fielder factor: the live defender's engine score against the row's own fielder, raised to this power (`0` = off). The bell-curve kernels are retired (2026-09-09). |
| `SIM_ACTOR_POWER_BATTER` / `_RUNNER_STEAL` / `_RUNNER_ADV` / `_CATCHER_THROWING` / `_PITCHER_STEAL` | SIM-523 | `1.0` / `12` / `20` / `2` / `12` | The other actor factors: each engine's 0-to-1 score matrix at its fitted power (`0` = off). |
| `SIM_BB_BATTER_POWER` | SIM-523 | `4` | The fielding draw's batter factor: the batter engine's score raised to this power. |
| `SIM_PITCH_RESULT_SPLIT` + `SIM_PITCH_PITCHER_POWER` / `SIM_RESULT_PITCHER_POWER` / `SIM_RESULT_BATTER_POWER` | SIM-523 | `0` + `1.0` / `1.0` / `1.0` | The pitch draw and the pitch-result draw as two draws with the pitcher and batter powers. Built and fitted (16 / 16 / 8) but OFF: at those powers the balanced lane reds strikeouts −2.4% (SIM-527). |
| `SIM_CATCHER_RECEIVING` | SIM-523 | `0` | The catcher receiving ratio on taken pitches (enable = SIM-526). |
| `SIM_GOT_AWAY` | SIM-517 | `1` | Honor the drawn pitch row's got-away fact (passed ball / wild pitch / uncaught third strike). |
| `SIM_STEAL_PITCH_CLASS` | SIM-554 | `1` | The running game on the pitch: the pickoff draw before the pitch, the steal draw after it among real pitches of the same class. `0` = the single pre-pitch draw. A bundle exported before DuckDB migration 0031, or one with any pitch row that has no class, also runs the single draw. Not pinned in `tests/conftest.py`: the unit lane's samplers run the new order. |

The old post-draw flips (`SIM_PARK_FACTOR`, `SIM_FIELDER_RBF`,
`SIM_FRAMING`, `SIM_HOME_FIELD_BIAS`) and the `SIM_FULL_POOL` switch no
longer exist.  Set a kernel to `0` (or the home weight to `1.0`) to turn
just that effect off.

### 1.9 Calibration, odds backfill, and the CLV backtest

The calibration → odds → CLV chain (the gold-standard edge-validation
loop).  All run inside the `app` container against the live Postgres +
DuckDB:

```bat
:: Fit /data/calibration.json (arsenal W2 + per-engine sigmas + win-prob
:: map). The API loads this at boot — see CALIBRATION_REPORT_PATH.
make calibrate
::   smoke run:  make calibrate FLAGS="--seasons 2024 --validate"

:: Validate win-prob + prop PMFs against real Final games; --write-calibration
:: fits the win-prob reliability curve back into the report.
make validate-props FLAGS="--seasons 2024 --max-games 50"

:: SIM-435 / SIM-555: backfill OPENING + CLOSING odds for Final games into
:: raw.game_odds / raw.prop_odds, one row per book ('bp:<id>'), each row checked
:: by the load guard (the closing lines the accuracy comparison scores against).
:: Network-bound. Name the provider in FLAGS: --provider bettingpros for real
:: lines, --provider mock for the wiring smoke (the loader refuses the mock
:: unasked). The app container reads ODDS_PROVIDER and ODDS_API_KEY from the
:: host's .env through env_file (bettingpros since 2026-10-09). A variable set
:: only in your host shell never reaches the container. --book NAME
:: (draftkings, bp:12, ...) loads one book.
make load-historical-odds FLAGS="--seasons 2024 --provider bettingpros"
::   smoke run:  make load-historical-odds FLAGS="--seasons 2024 --max-games 5 --provider bettingpros --book draftkings"
```

**SIM-429 CLV backtest (`scripts/clv_backtest.py`).**  Scores the model's
entry prices against the closing line — the project's gold-standard edge
metric.  The slate is fanned out **over games** (`--workers N`, default 6,
each a `forkserver` worker holding its own ~373 MB sampler — ~2.2 GB at 6
workers), giving ~6× throughput; `--workers 1` is the serial byte-identical
reference.  A single game can't go below ~30 s (core-bound at ~6).

> **`scripts/` is NOT bind-mounted into the `app` container** (only
> `api/`, `pipeline/`, `similarity/`, `simulation/`, `db/` are).  The make
> targets above run scripts that are already baked into the image, but a
> *new or edited* script needs an explicit `-v` mount (or a rebuild).
> Mount it for the CLV backtest:

```bat
:: Full 2024 season, 6 games at once (PowerShell/bash use $PWD; cmd uses %CD%):
docker compose run --rm -v "%CD%\scripts:/app/scripts" app ^
    python scripts/clv_backtest.py --seasons 2024 --workers 6 --iterations 100

:: Smoke / byte-identical serial reference (no pool):
docker compose run --rm -v "%CD%\scripts:/app/scripts" app ^
    python scripts/clv_backtest.py --seasons 2024 --max-games 2 --workers 1
```

> bash/PowerShell users: substitute `-v "$PWD/scripts:/app/scripts"` for
> the `-v "%CD%\scripts:..."` mount above.

**What good looks like.**  The backtest writes a CLV scoreboard JSON
report.  First full result (120 games, 2024): ~49% beat-close — i.e. **no
demonstrable edge yet**; the gold-standard loop works end-to-end, the
model still needs the run-conversion / calibration work to develop an edge.

---

## Part 2 — Per-component health checks

### 2.1 PostgreSQL (raw + sim schemas)

```bat
docker compose exec db pg_isready -U %POSTGRES_USER% -d %POSTGRES_DB%
:: The migrate service is the one service that logs in as the owner (SIM-443b).
docker compose run --rm migrate alembic current

docker compose exec db psql -U %POSTGRES_USER% -d %POSTGRES_DB% -c "SELECT season, COUNT(*) AS pitch_count FROM raw.pitches GROUP BY season ORDER BY season;"

:: SIM-160 bat-side coverage audit:
set BASEBALL_DB_DSN=postgresql://baseball_app:<BASEBALL_APP_DB_PASSWORD from .env>@localhost:5432/baseball_sim
python scripts\check_bat_side_coverage.py --out docs\data_quality\2026-05-20-bat-side-coverage.md
echo %ERRORLEVEL%
```

**What good looks like.**  ~700 000 rows per fully-loaded season; `alembic current` prints the newest revision under `db/migrations/versions/` (`0030` as of 2026-10-09); `check_bat_side_coverage.py` exits 0.

### 2.2 DuckDB analytical layer

```bat
type db\schemas\duckdb_schema_version.txt
:: Expected: 32

duckdb db\schemas\baseball_simulator.duckdb -c "SELECT * FROM migration_history ORDER BY applied_at;"

duckdb db\schemas\baseball_simulator.duckdb -c "SELECT player_id, season, framing_runs, blocking_runs, cs_rate, steal_attempt_rate_against, below_minimum_sample FROM derived.catcher_season_metrics WHERE player_id = 592663 ORDER BY season;"
```

**What good looks like.**  `steal_attempt_rate_against` ~ 0.03 for Realmuto.

### 2.3 Similarity engines

```bat
pytest tests\unit\test_engine_build_smoke.py -v
python similarity\engines\pitcher_similarity.py
python similarity\engines\catcher_similarity.py
pytest tests\unit\test_ml_engines_sim041.py tests\unit\test_ml_engines_sim042.py -v
pytest tests\regression\ -v --timeout=60
```

If regression-gate fails after intentional engine change:

```bat
python tests\regression\generate_fixtures.py --force
```

### 2.4 Live ingestion pipeline

```bat
pytest "tests\unit\test_live_pipeline_bugs.py::TestSIM132MockOddsVig" -v

docker compose exec db psql -U %POSTGRES_USER% -d %POSTGRES_DB% -c "SELECT COUNT(*) AS null_hashes FROM raw.game_odds WHERE odds_hash IS NULL;"
docker compose exec db psql -U %POSTGRES_USER% -d %POSTGRES_DB% -c "SELECT game_pk, source, odds_hash, COUNT(*) FROM raw.game_odds GROUP BY 1,2,3 HAVING COUNT(*) > 1 LIMIT 5;"

:: If either gate fails, run SIM-157 backfill:
set BASEBALL_DB_DSN=postgresql://baseball_app:<BASEBALL_APP_DB_PASSWORD from .env>@localhost:5432/baseball_sim
python scripts\backfill_odds_hash.py
```

### 2.5 FastAPI app

```bat
curl http://localhost:8000/health
:: Expected: {"status":"ok"}

curl http://localhost:8000/ready
:: Expected: {"status":"ready"}

:: Pitcher similarity (CORRECT path-style URL):
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/similarity/pitcher/605400/2024"

:: Surface inventory:
curl http://localhost:8000/openapi.json > openapi.json
:: Lists the full surface: /health, /ready, /api/games/* (incl. /simulate),
:: the betting/CLV routes, /api/similarity/pitcher/{pitcher_id}/{season},
:: /metrics, and the WebSocket channel.
```

### 2.6 Redis cache

```bat
docker compose exec redis redis-cli ping
:: Expected: PONG
```

The similarity endpoint caches responses under
`simviz:pitcher:{id}:{season}:bins={b}:top_n={n}` (24h TTL).

### 2.7 Index acceptance (SIM-085 / SIM-089 / SIM-158)

```bat
set BASEBALL_DB_DSN=postgresql://staging-host:5432/baseball
python scripts\run_index_acceptance.py ^
    --season 2024 ^
    --pitcher-id 605400 ^
    --out docs\perf\2026-05-13-index-acceptance.md
echo %ERRORLEVEL%
```

### 2.8 Tests, lint, types

```bat
make lint
make format
make type-check

make test-unit
make test-regression
make test-integration
```

**Local-dev dep gotchas.**  A handful of test modules need optional deps:
`POT` (pitcher engine W₂), `scipy` (situation engine), `pybaseball`
(ETL loader), `pytest-asyncio` (integration).  `pip install -r requirements-dev.txt` covers all.

**Python 3.13.**  `POT>=0.9.5` and `faiss-cpu>=1.9` both ship cp313
manylinux wheels, so the pitcher engine (W₂) and FAISS engine run with
full coverage on 3.13 — no source build required (SIM-431).

### 2.9 CI pipelines

- `.github\workflows\ci.yml` — ruff, mypy, unit, regression, Docker build on every PR.
- `.github\workflows\integration-weekly.yml` — testcontainers integration suite, Monday 03:00 UTC.
- `.github\workflows\docker-release.yml` — push image to `ghcr.io` on main.

### 2.10 Backups and restore

The scheduled backups (SIM-525) copy the three data stores to a host folder every day. A *store* is one of the three: the Postgres statistics database, the DuckDB analytics file (`/data/baseball_sim.duckdb`) and the engine-artifact bundle (the published version under `/data/play_pool/engine_artifacts`). A *set* is one store's backup from one run.

**The folder.**  `BACKUP_DIR` in `.env` names it: `C:/Users/grego/Documents/baseball_backups`. Compose mounts it at `/backups`: read-write in the `db` and `jobs` containers, read-only in the app. The folder is outside Docker's disk image, on the laptop's only disk, C:. Each store keeps its newest 3 complete sets (`BACKUP_KEEP=3` in `.env`). Without `BACKUP_DIR`, compose uses `.\backups` in the repo, which git and Docker ignore.

```
BACKUP_DIR\
  postgres\<TS>\     dump\ (pg_dump directory format), counts_before.tsv, counts_after.tsv, manifest.json
  duckdb\<TS>\       baseball_sim.duckdb [+ .wal], data_files\ (calibration.json, prop_validation.json, ingest_marks\*.last), manifest.json
  bundle\<TS>\       <build id>\ (a copy of the published version), manifest.json
  drills\<TS>.json   one report per restore drill
```

`<TS>` is the UTC start time of the run (`YYYYmmddTHHMMSSZ`). A run writes each set as `<TS>.partial`, syncs every file to disk, writes `manifest.json` last and then renames the folder. A set without `manifest.json` never counts. The next run deletes every `.partial` folder it finds. Each manifest records the bytes and the sha256 of every file.

**What the sets protect against.**  They protect against the loss or a reset of Docker's disk image, `docker compose down -v` and `make nuke`, a bad migration or rebuild, a writer that dies in the middle of a rebuild, and accidental deletion. They do NOT protect against the laptop's disk dying or the loss of the laptop on their own: the sets sit on the same disk. The off-site copy (SIM-568) covers that loss (see **The off-site copy** below).

**The off-site copy.**  A third job copies the newest complete sets of each store into a OneDrive folder, and the OneDrive client uploads them off the machine. `OFFSITE_BACKUP_DIR` in `.env` names the folder: `C:/Users/grego/OneDrive/Documents/baseball_backups`. Compose mounts it at `/offsite`: read-write in the `jobs` container, read-only in the app. Without the value, compose uses `.\backups_offsite` in the repo, which is NOT off the machine. Each store keeps its newest 3 sets there (`OFFSITE_KEEP`, else `BACKUP_KEEP`, else 3). The folder has the same store folders as `BACKUP_DIR`, and each set in it is a byte-for-byte copy of the local set. The copy writes no `.partial` folder: it writes each file straight into `<store>\<TS>\` and `manifest.json` last, so a folder with no manifest is not a complete set. The design and the run book are in `docs\audit\2026-10-17-sim568-offsite-copy.md`.

What OneDrive does and does not do for you:

- The copy is off the machine only once the OneDrive client has uploaded it. The job cannot see the upload: the OneDrive tray icon and Explorer's status column are the truth.
- The folder is a cloud-files folder. Storage Sense (the Windows disk clean-up) can turn its files into online-only placeholders. A read of a placeholder through the Docker mount downloads the file.
- Never point `OFFSITE_BACKUP_DIR` at `BACKUP_DIR`. The copy refuses to run when the two are one folder.

**The schedule.**  Ofelia runs two jobs at 10:00 UTC, after the nightly rebuild (08:00 UTC, done at about 09:30), and the off-site copy at 10:30 UTC. The closing-lines pass also starts at 10:30 UTC, in the app container, so it never shares a container with the copy. Each job sets `no-overlap`.

| Job | Container and user | What it does |
|---|---|---|
| `nightly-backup-postgres` (the Postgres job) | `db`, the `postgres` OS user | Runs `db/backup/pg_backup.sh` over the unix socket, with no password. It counts every `raw`, `sim` and `public` table, dumps the database (`pg_dump -Fd -j 4 -Z 3`), checks the dump with `pg_restore --list` and counts every table again. The live service can write during the dump, so a restore checks each count against the two counts. A lock folder (`postgres\.lock`) stops a second run. |
| `nightly-backup-data` (the data job) | `jobs`, `appuser` | Runs `with_mark.sh nightly-backup with_retry.sh 2 python -m pipeline.backup.data_stores`. It copies the DuckDB file under a read-only handle and requires the same table counts on the copy. It copies the small files into the same set. It copies the bundle version without the app's `.in_use.*` marks. |
| `nightly-backup-offsite` (the off-site copy, 10:30 UTC) | `jobs`, `appuser` | Runs `with_mark.sh nightly-backup-offsite with_retry.sh 2 python -m pipeline.backup.offsite`. It copies the newest 3 complete sets of each store from `/backups` to `/offsite`, file by file, the oldest set first. It checks each file's sha256 against the set's manifest, then copies the manifest last. It skips a set that is already off-site. |

The data job waits 60 s. Then it waits while a `nightly-rebuild`, `nightly-finals` or `weekly-refresh` `.running` mark younger than 6 h exists in `/data/ingest_marks`, for up to 2 h. The rebuild runs inside `with_mark.sh`, so it leaves that mark while it writes the DuckDB file. When a writer still holds the file, the open retries every 60 s for 30 min. When either wait runs out, the data job skips the DuckDB store, still backs up the bundle and exits 1. A crash retry within one hour skips a store that already finished and reuses its set id, so the duckdb and bundle sets of one night share one `<TS>`. The data job does not back up `/data/replay.duckdb` (the app holds it writable, and its replays are disposable) or the `calibration.json.bak_*` files.

The two 10:00 jobs guard the free space on `/backups` (the host disk C:). They need the larger of 10 GiB and 1.5 times the newest set. When the disk is short, they delete the oldest set while more than 2 remain. Otherwise they refuse with exit 2. A failed run prunes nothing.

One off-site copy runs at a time. A run takes the copy lock `/data/ingest_marks/offsite-copy.lock` before it waits and deletes it at its end; a second run refuses with exit 2. The copy then waits while the data job's `nightly-backup.running` mark is younger than 6 h, or while the Postgres job's lock (`postgres\.lock\owner`) holds a start younger than 6 h. It polls every 60 s for up to 2 h. When the wait runs out, it logs that and copies anyway: a complete set is always safe to copy. A crash retry keeps each file that already matches and copies the rest. Before it writes, the copy needs the larger of 10 GiB and 1.5 times the bytes it will copy free on C:, else it refuses with exit 2. It cannot see the OneDrive quota. Each store prunes only after that store copied cleanly. It never prunes a set among the newest 3 local sets, and a pruned set loses its manifest first. A failed store prunes nothing; the other stores still copy.

**Back up now.**  Run these from cmd.exe: Git Bash rewrites the container paths (CLAUDE.md §2a).

```bat
make backup
:: The Postgres step prints:  BACKUP OK postgres <TS> <bytes> bytes <seconds> s
:: The data step prints:      BACKUP OK duckdb <TS> ...  and  BACKUP OK bundle <TS> ...
make backup-status
```

`make backup` runs the two steps one after the other. Its data step passes `--new-set`, so it always makes a fresh set, even within an hour of the scheduled run. On the first run (2026-10-15) the data step took 308 s: a 3.6 GiB DuckDB copy and a 561 MiB bundle copy.

```bat
make backup-offsite
:: Prints, per set:  COPY OK <store> <TS> <bytes> bytes <seconds> s
::               or  COPY SKIP <store> <TS> (already off-site)
docker compose exec -T jobs python -m pipeline.backup.offsite --dry-run
:: Prints WOULD COPY / WOULD SKIP / WOULD PRUNE lines and writes nothing.
```

`make backup-offsite` runs the off-site copy now, inside `with_mark.sh nightly-backup-offsite`. It refuses with exit 2 while another copy runs: a `nightly-backup-offsite.running` mark under 6 h old, or a live copy lock. Do not run it while `make backup` runs: the backup can prune a local set that the copy reads, and that store then fails for the run.

**Check last night's run.**

```bat
make backup-status
:: Expected: postgres, duckdb and bundle, each under 24 h old, with 3 sets each.
:: The second block is the off-site copy, one line per store:
::   postgres local <TS>  off-site <TS>  age 0.5 h  2.7 GiB  (3 sets)  MIRRORED

type C:\Users\grego\Documents\baseball_backups\postgres\<TS>\manifest.json
type C:\Users\grego\Documents\baseball_backups\duckdb\<TS>\manifest.json

docker compose exec -T jobs cat /data/ingest_marks/nightly-backup.last
:: Expected: <UTC date> 0 <UTC timestamp>   (the middle field is the data job's exit code)
docker compose exec -T jobs cat /data/ingest_marks/nightly-backup-offsite.last
:: Expected: the same shape; the middle field is the off-site copy's exit code

docker compose logs scheduler | findstr nightly-backup
:: (this also matches nightly-backup-offsite)

set API_KEY=<one key from API_KEYS in .env>
curl -s -H "X-API-Key: %API_KEY%" http://localhost:8000/metrics | findstr backup_age
:: baseball_sim_backup_age_hours{store="postgres"} 1.2   (and duckdb, bundle)
:: baseball_sim_offsite_backup_age_hours{store="postgres"} 0.7   (and duckdb, bundle)
```

**What good looks like.**  Each store's newest set is under 24 h old, its folder holds `manifest.json`, and no `.partial` folder is left. `nightly-backup.last` reads exit 0. The gauge `baseball_sim_backup_age_hours{store}` reads the hours since each store's newest complete set, and -1 for a store with no set. The app reads the folder again at most every 600 s.

For the off-site copy: each store's line in the second block of `make backup-status` ends `MIRRORED`, and `nightly-backup-offsite.last` reads exit 0. The gauge `baseball_sim_offsite_backup_age_hours{store}` reads the hours since each store's newest complete off-site copy: under 24, and -1 before the first copy. A background task in the app reads the off-site folder at most every 600 s; a scrape never reads it. On the host, Explorer's status column shows every file of the newest sets as synced. Only that shows the copy is off the machine.

**The restore drill.**  The drill proves that a set restores. It never touches the live database.

```bat
make backup-drill
make backup-drill SET=<TS>
make backup-drill OFFSITE=1
```

`make backup-drill` runs `py scripts/backup_drill.py --set latest` on the host. The drill refuses to start while any `sim525_*` container or volume exists. It starts a scratch Postgres cluster (`postgres:15-alpine`, container `sim525_drill_pg`, no network, no port, the named volume `sim525_drill_pg`). It runs the real `pg_restore.sh` against the scratch cluster. It requires exit 0, no "errors ignored" line and `RESTORE OK` as the last line. Then it runs `--verify-set <TS>` in the jobs container on the newest set id that is complete in both `duckdb\` and `bundle\`: the DuckDB copy's counts and hashes, and a load of the bundle copy. It removes the scratch container and volume, and writes `BACKUP_DIR\drills\<TS>.json`. Postgres refuses a Windows folder as its data directory, so the scratch cluster lives on a Docker volume: it takes about 20 GB inside Docker's disk image while it runs. Check that the disk image has room first; `docker builder prune -f` frees the build cache.

`make backup-drill OFFSITE=1` runs the same drill from the off-site copy (`--offsite`); only the value 1 turns it on. It reads the folder that `OFFSITE_BACKUP_DIR` names, from the shell environment first and then `.env`, as compose reads it. The data verify runs with `--backups-root /offsite`. The report says `"source": "offsite"` and lands in the off-site folder's `drills\`. A drill from online-only placeholders downloads the set first, so it takes longer and needs the OneDrive client online.

**The restore run book, in short.**  The full run book is `docs\audit\2026-10-14-sim525-backups-plan.md` §6. Pick the set with `make backup-status`. To restore from the off-site copy on this machine, first copy the set folder (for example `postgres\<TS>\`) from the OneDrive folder back into `BACKUP_DIR`: the `db` container has no `/offsite` mount. On a new machine, point `BACKUP_DIR` at the synced OneDrive folder instead. `docs\audit\2026-10-17-sim568-offsite-copy.md` §7 holds both cases.

```bat
:: Postgres: the target database must be empty (a new postgres_data volume).
:: The script refuses a populated database with exit 3 ("target database is not empty").
make restore-postgres SET=<TS>
:: Expected last line: RESTORE OK /backups/postgres/<TS>
make db-roles
docker compose run --rm migrate alembic current
:: Expected: alembic prints 0030 (head)

:: DuckDB: checks the size and sha256, moves a live .wal aside, replaces the file.
docker compose stop scheduler
docker compose exec -T jobs python -m pipeline.backup.data_stores --restore-duckdb <TS> --yes
docker compose restart app
docker compose start scheduler

:: The bundle: copies the version back and publishes it; the app restarts itself on the link move.
docker compose exec -T jobs python -m pipeline.backup.data_stores --restore-bundle <TS> --yes
```

`pg_restore.sh` drops the empty `raw` and `sim` schemas that the image's init creates. Then it restores with `pg_restore -j 4 --no-owner --no-acl --exit-on-error` and checks every table's count against the set's two counts. The restore carries no owners or grants, so `make db-roles` puts the grants back. `--restore-bundle` refuses a version folder that exists already; `--force` copies over it. No command restores the small files: copy them from the DuckDB set's `data_files\` folder into `/data` by hand.

---

## Part 3 — End-to-end run (sprint-kickoff health checklist)

Run this from a clean checkout to confirm the full system boots and
every component is exercised once:

```bat
git clean -fdx -e .env -e db\schemas\baseball_simulator.duckdb
copy .env.example .env
py scripts\gen_secrets.py --append .env
make dev-bg
make migrate
make test
make test-integration

pytest tests\unit\test_engine_build_smoke.py -v

curl http://localhost:8000/health
curl http://localhost:8000/ready

:: SIM-443c: every data route needs a key. Use one value of API_KEYS in .env.
set API_KEY=<one API_KEYS value from .env>
:: CORRECT URL (path-style, /api/similarity, no /v1):
curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/similarity/pitcher/605400/2024?bins=20&top_n=10"

set BASEBALL_DB_DSN=postgresql://baseball_app:<BASEBALL_APP_DB_PASSWORD from .env>@localhost:5432/baseball_sim
python scripts\check_bat_side_coverage.py --out docs\data_quality\2026-05-20-bat-side-coverage.md
python scripts\run_index_acceptance.py --season 2024 --pitcher-id 605400 ^
    --out docs\perf\2026-05-13-index-acceptance.md

make down
```

### 3.1 Single-line `&&`-chained equivalent

```bat
git clean -fdx -e .env -e db\schemas\baseball_simulator.duckdb && copy .env.example .env && py scripts\gen_secrets.py --append .env && make dev-bg && make migrate && make test && pytest tests\unit\test_engine_build_smoke.py -v && curl http://localhost:8000/health && curl http://localhost:8000/ready && set "API_KEY=<one API_KEYS value from .env>" && curl -H "X-API-Key: %API_KEY%" "http://localhost:8000/api/similarity/pitcher/605400/2024" && python scripts\check_bat_side_coverage.py --out docs\data_quality\2026-05-20-bat-side-coverage.md && python scripts\run_index_acceptance.py --season 2024 --pitcher-id 605400 --out docs\perf\2026-05-13-index-acceptance.md && make down
```

### 3.2 Persisting `BASEBALL_DB_DSN`

```bat
setx BASEBALL_DB_DSN "postgresql://baseball_app:<BASEBALL_APP_DB_PASSWORD from .env>@localhost:5432/baseball_sim"
:: Open a NEW cmd window after setx — current windows don't see the new value.
echo %BASEBALL_DB_DSN%
```

---

## Part 4 — Where to look when something breaks

| Symptom | Where to look |
|---|---|
| `{"detail":"Not Found"}` on similarity curl | URL is wrong — use `/api/similarity/pitcher/{id}/{season}`, NOT `/v1/similarity/pitcher?…`. |
| `make migrate` fails | `db\migrations\versions\` — chain integrity; `alembic current` output. |
| Engine builds but scores degenerate | `similarity\similarity_diagnostics.py` reports COLLAPSED / NO_SPREAD flags. |
| Regression-gate fails after engine change | `python tests\regression\generate_fixtures.py --force` then commit. |
| API returns 5xx on similarity | `docker compose logs app` — engine load errors surface first. |
| Live pipeline misses pitches | Check `raw.etl_errors` — SIM-093 audits skipped rows. |
| Vig flake | Fixed in SIM-159; check `_VIG_LOWER`/`_VIG_UPPER` in `test_live_pipeline_bugs.py`. |
| Mock odds returns NULL hash | Run `python scripts\backfill_odds_hash.py`. |
| DuckDB schema mismatch | Re-apply `db\migrations\duckdb\*.sql` in numbered order (through `0032_*`) and bump `duckdb_schema_version.txt` to match the latest migration (currently `32`). On the live DuckDB, apply 0031 only through `scripts/sim554_rebuild_steal_pool.py --apply-migration` (app stopped): it applies the migration inside the steal-pool rebuild's transaction, and the old pool builder fails on a migrated table. |
| `curl` truncates URL | Escape `&` as `^&` or wrap the URL in double quotes. |
| `set VAR=value` doesn't persist | Use `setx VAR "value"` and open a new cmd window. |
| `make test` shows 21 errors | Integration tests can't reach Docker daemon — fixed in conftest.py; rebuild image with `make build`. |
| Pitcher engine smoke fails with `import ot` | POT isn't installed — `pip install -r requirements.txt` (pins `POT>=0.9.5`, which ships a cp313 wheel). |
| `asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "baseball_user"` (or `"baseball_app"` / `"baseball_ro"`) even though `.env` and `docker compose exec db env` agree | **Stale `postgres_data` volume.**  `POSTGRES_PASSWORD` only takes effect on the volume's first init.  Reset the passwords in-band from `.env` over the db container's socket (no password needed):<br>`make db-roles ROTATE_ADMIN_PASSWORD=1`<br>It sets `baseball_user`'s password from `POSTGRES_PASSWORD` and both app roles' passwords from `.env`, and re-applies the grants.<br>Nuclear option (loses all data): `docker compose down -v && docker compose up -d db && make migrate`. Take a backup first (`make backup`); the restore run book in §2.10 brings the data back from a set. |
| `required variable POSTGRES_PASSWORD is missing a value` (or another secret) on any `docker compose` or `make` command | `.env` lacks a secret (SIM-443): `py scripts\gen_secrets.py --append .env`, then `py scripts\gen_secrets.py --check .env` exits 0. A worktree needs its own `.env`. |
| `401 Unauthorized` from an `/api/` route | Every route needs a credential in every environment (SIM-443c): `X-API-Key: <key>` or `Authorization: Bearer <key>` with a key from `API_KEYS`, or the browser's login with `AUTH_PASSWORD`. `curl http://localhost:8000/health` shows `"auth_enforced"`. |
| The SQL console answers 503 | The read-only pool is missing or refused (SIM-443e). `curl http://localhost:8000/health` shows `"sql_console"`; `docker compose logs app \| findstr SIM-443e` names the reason. Run `make db-roles` if `baseball_ro` does not exist yet. |
| `permission denied for table ...` from the app or a loader | The table is outside `raw`/`sim`, or a new schema has no grants: add its lines to `db\roles\setup_roles.sql`, then `make db-roles`. |
| Ofelia logs `No such container: baseball_simulator_v2-jobs-1` | The nightly data jobs exec into the `jobs` container (SIM-443b): `docker compose up -d jobs`, then `docker compose ps jobs` shows it healthy. A worktree's or a renamed project's container has another name: edit the `container =` lines in `deploy\ofelia\config.ini`. |
| A store is lost or corrupt (the Postgres database, `/data/baseball_sim.duckdb` or the published bundle) | Restore it from its newest good set with the scheduled backups (SIM-525): the restore run book in §2.10, in full in `docs\audit\2026-10-14-sim525-backups-plan.md` §6. `make backup-status` lists the sets. To test a set before you restore it, run `make backup-drill SET=<TS>`. |
| A backup set is missing this morning (`make backup-status` shows a store over 24 h old, or the gauge `baseball_sim_backup_age_hours` reads over 24 or -1) | 1. `docker compose logs scheduler \| findstr nightly-backup` shows each job's output and exit. An exec error means the `db` or `jobs` container was stopped at 10:00 UTC. 2. The Postgres job: exit 2 is a refusal before any write (the backup root is missing or read-only, the free space is short, or another run holds `postgres\.lock`); exit 1 is a failed dump, and the job prunes nothing. 3. The data job: `docker compose exec -T jobs cat /data/ingest_marks/nightly-backup.last` holds its exit code in the middle field. Exit 2 is a refusal (the root or the free space). Exit 1 means a store failed. One cause: a writer mark such as `nightly-rebuild.running` stayed fresh for 2 h, or a writer held the DuckDB file for 30 min, so the job skipped the DuckDB store. 4. A `<TS>.partial` folder is a run that died; the next run deletes it. 5. With no `BACKUP_DIR` in `.env`, the sets go to `.\backups` in the repo. Then run `make backup` from cmd.exe. |

---

## Companion documents

- `BACKLOG.xlsx` — the open tickets, ranked by priority.
- `CHANGES.md` — every shipped change with per-ticket detail.
- `agent_team.md` — 9-agent role definitions.
- `docs\technical\README.md` — the per-subsystem code reference.
- `docs\archive\architecture\2026-05-20-play-pool.md` — Phase 3 sampler spec.
- `docs\similarity_visualization_spec.md` — similarity-explorer design + endpoint contract.
- `docs\perf\2026-05-13-index-acceptance.md` — SIM-158 acceptance harness.
- `docs\data_quality\2026-05-20-bat-side-coverage.md` — SIM-160 audit.
