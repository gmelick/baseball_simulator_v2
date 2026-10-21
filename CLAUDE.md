# CLAUDE.md — MLB Baseball Simulation Platform

> Project guide for Claude Code. Read this file first. This is a pitch-by-pitch Monte-Carlo
> simulator of MLB games, run as a **sports-trading hedge fund**: the product is player-prop
> prediction and betting-edge validation, benchmarked against **the closing line** (the
> market's last price before the game): the accuracy comparison scores the model's
> probabilities against the closing line's. A **9-agent team** does the work, with an
> independent QA cross-validation pass.

---

## 1. Purpose & goals

- **What it is:** the platform ingests real Statcast pitch-by-pitch data and builds **11
  similarity engines** across player and situation dimensions. A stochastic pitch-by-pitch
  simulator plays 100 independent games per request. Every decision in a game is a draw from a
  pool of real plays, weighted by similarity. The outputs feed win probabilities, a probability
  distribution (PMF) for each player statistic, box scores, and the betting surface (edges,
  expected value, closing line value).
- **Primary use case:** predict player props (pitcher strikeouts, batter hits / home runs /
  total bases, and the rest of the fifteen markets) and find +EV betting edges. The model is
  judged by the accuracy comparison (`scripts/accuracy_comparison.py`): its probabilities and
  the de-vigged closing line's, scored with a proper scoring rule (Brier, log loss) on the same
  outcomes. Closing line value (CLV: how a bet's price compares with the closing price)
  describes the market; the model is not judged by it (owner ruling 2026-09-28). Every model
  number must be *calibrated*: an uncalibrated number must not reach a user.
- **End state:** a live game dashboard (the day slate → the game cards → the game page with the
  real game, the simulation, the props, the prices and a "what if" panel) on top of the API.

## 2. Current status (read this)

- **Phases 1–6 are complete; Phase 7 is largely complete** (§12). The platform runs on Python
  3.13 and numpy 2.x. The frontend is React 18 + Vite + TypeScript. DuckDB schema **v32**,
  Alembic head **0030** (§7, `db/migrations/README.md`).
- **The simulator has one path: the full-pool similarity sampler** (SIM-486). The no-DB tests
  and the batch runner's no-DB factory build the same `StateMachine` over the in-memory bundle
  `simulation/synthetic_bundle.py`. A missing artifact bundle fails the worker loudly; there is
  no fallback simulator.
- **The production flags live in the `app` service `environment:` of `docker-compose.yml`.**
  `simulation/flags.py` registers every `SIM_*` flag with its code default. The loop cheat
  sheet (`docs/technical/sim-loop-cheat-sheet.md`) lists each draw's weights with the
  production value, the code default and the fitted value.
- **The unit lane runs the plain baseline.** `tests/conftest.py` pins most simulation flags
  OFF (the manager, the pitching-change draw, the platoon, the fielding filters, the fence
  stage, the kernels, the split), so the unit lane stays byte-identical whatever `.env` holds.
  It leaves `SIM_STEAL_PITCH_CLASS` unpinned on purpose. It hard-pins `ODDS_PROVIDER=mock`,
  removes `ODDS_API_KEY`, and defaults `AUTH_DISABLED=1`.
- **Calibration is live.** The app applies `/data/calibration.json` at boot. `make calibrate`
  drops the win-probability reliability curve; §2b says how to restore it.
- **The grade.** The latest 45 × 130 acceptance lane (`CHANGES.md`, the clean-up's close entry
  of 2026-10-20) reads 97 passed and 7 failed: walks per plate appearance +3.4%, doubles per
  ball in play +4.7%, steal attempts at second −8.6% and at third −16.5% (the steal-weight
  volume deficit, SIM-556), double plays per opportunity −5.0% (0.00003 past its floor), the
  underpowered home-win share, and a stale park-factor pin in `tests/acceptance/bands.py`.
  Strikeouts and hit by pitch pass. No betting value is measured until every band is green
  (§2b).
- **The open tickets are the rows of `BACKLOG.xlsx`** (§2b). The code clean-up (SIM-493)
  filed 49 defects, SIM-575 to SIM-623; the ten P2 rows (SIM-575 to SIM-584) change what
  production computes today.
- **The documentation.** `docs/technical/` is the code reference: the architecture, the code
  review path, the generated file index, the loop cheat sheet and one page per area
  (`docs/technical/README.md`). `WORKFLOW.md` is the operator's manual. `docs/README.md` maps
  every document. The dated status logs of 2026-05 to 2026-10 are in
  `docs/archive/CLAUDE-md-history.md`.

## 2a. Operational caveats (Windows + Docker)

- **Every compose command needs the five passwords in `.env` (SIM-443b).** The compose file
  has no default password: `POSTGRES_PASSWORD`, `BASEBALL_APP_DB_PASSWORD`,
  `BASEBALL_RO_DB_PASSWORD`, `REDIS_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` use `${VAR:?}`, so a
  missing one fails `docker compose` at once, `make test-unit` included. A worktree needs its
  own `.env`; `python scripts/gen_secrets.py --append .env` adds any secret that is missing and
  never changes one that has a value.
- **The app, live and jobs services blank three passwords over their `env_file`.** They set
  `POSTGRES_PASSWORD`, `BASEBALL_RO_DB_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` to `""`, so the
  owner's password never reaches a serving container. The app logs in as `baseball_app`, which
  cannot create or alter a table.
- **`make migrate` runs the `migrate` service**, the only service that logs in as the cluster
  owner. Run another Alembic command through it: `docker compose run --rm migrate alembic current`.
- **The nightly data jobs exec into the `jobs` container.** Ofelia runs `nightly-finals`,
  `weekly-refresh`, `nightly-rebuild`, `nightly-backup-data` and `nightly-backup-offsite` with
  `docker exec` into `baseball_simulator_v2-jobs-1` as `appuser` (an exec with no `user =`
  line runs as root). A stopped `jobs` container fails all five; Ofelia logs the exec error.
  The Sunday 08:00 pair shares the container's memory cap (`JOBS_MEM_LIMIT`, default 10g).
  Recreate the scheduler after an edit of `deploy/ofelia/config.ini`.
- **The deploy window.** The off-site copy runs from 10:30 to about 10:45 UTC. Recreate `jobs`,
  `app` or `scheduler` only after about 11:00 UTC, and only when `/data/ingest_marks/` holds no
  `.running` mark.
- **A rebuild runs while the app serves (SIM-524, SIM-563).** The app holds no handle on the
  DuckDB analytics file; it pins the bundle version it booted with and restarts itself when
  the nightly build moves the bundle link (`api/bundle_watch.py`). A DuckDB writer still needs
  the file to itself: never run a second rebuild during the nightly one.
- **The backup sets land in `BACKUP_DIR` (SIM-525).** `.env` sets
  `BACKUP_DIR=C:/Users/grego/Documents/baseball_backups`, a host folder outside Docker's disk
  image; compose mounts it at `/backups`. Without the value, compose uses `./backups`, which git
  and docker ignore. Run `make backup` from cmd.exe: Git Bash rewrites the container paths in its
  `docker compose exec` lines. The data job skips a store that has a set under an hour old;
  `--new-set` turns the skip off, and `make backup` passes it. The restore drill
  (`make backup-drill`) needs about 20 GB inside Docker's disk image while it runs.
- **The off-site copy lands in a OneDrive folder (SIM-568).** `.env` sets
  `OFFSITE_BACKUP_DIR=C:/Users/grego/OneDrive/Documents/baseball_backups`; compose mounts it at
  `/offsite`. Without the value, compose uses `./backups_offsite`, which is NOT off the machine.
  The copy is off the machine only once the OneDrive client has uploaded it; the tray icon and
  Explorer's status column are the truth. Storage Sense (the Windows disk clean-up) can turn
  the folder's files into online-only placeholders, and a read through the Docker mount then
  downloads the file. Never point `OFFSITE_BACKUP_DIR` at `BACKUP_DIR`. Do
  not run `make backup` and `make backup-offsite` at the same time.
- **Prometheus needs `deploy/monitoring/secrets/app_api_key` (SIM-443c).** The file holds one
  `API_KEYS` value from `.env` and is gitignored. Without it the `baseball-sim-app` target shows
  DOWN. See `deploy/monitoring/secrets/README.md`.
- **`scripts/` is NOT volume-mounted** into the running app container; only `api/`,
  `pipeline/`, `similarity/`, `simulation/` and `db/` are. Edits to `scripts/` reach the
  running container only after `docker compose build app` and `docker compose up -d app`.
  Edits to the mounted folders need only `docker compose restart app`. Run a script that is
  not in the image yet with `docker compose run --rm -v "$PWD:/app" app python scripts/<x>.py`.
- **`betting/` is NOT mounted either, and the app hot-reloads `api/`.** An edit to a mounted
  `api/` file that imports a NEW `betting/` name breaks the reload: the app fails to import
  until the image is rebuilt. Rebuild first (`docker compose build app`, then
  `docker compose up -d app`), then edit `api/`.
- **The container test and lint targets run the checkout (SIM-567).** The seven Makefile
  targets (`test`, `test-unit`, `test-regression`, `test-integration`, `lint`, `format`,
  `type-check`) mount the checkout over `/app`. `make test-unit TESTS="tests/unit/test_x.py"`
  names the files. An ad-hoc `docker compose run app pytest` with no `-v` runs the image's old
  copy. From a worktree, `make` reads the worktree's own compose file and `.env`; run these
  targets from the main checkout. The app image has no git, so the file-index guard skips there;
  CI runs it. With no `ODDS_API_KEY` the vendor's provider sends no BettingPros request, but its
  MLB Stats API reads (`_mlb_get`) still try the network; it returns empty rows and raises no
  error.
- **The live service runs in its own compose service, `live`, and publishes on Redis
  (SIM-519).** Never set `LIVE_PIPELINE_ENABLED` in the app: the `live` service must be the one publisher.
  Every "latest run" read takes only `sim.sim_runs` rows with `kind = 'pregame'`, so the
  what-if runs never stand in for a pregame run (SIM-562).
- **The app holds `/data/replay.duckdb` writable (SIM-561).** No other process can open it
  while the app runs, even read-only; read a stored game through the API (`/plays`,
  `/linescore`). Stop the app to inspect the file. The file keeps the 5 newest runs per game.
- **Git Bash on Windows mangles container paths.** A `docker compose exec` or `docker compose
  run` command from Git Bash that names a Linux path like `/app/scripts/foo.py` reaches Docker
  as `C:/Program Files/Git/app/scripts/foo.py`. Prefix with `MSYS_NO_PATHCONV=1`. The tell: the
  error names `C:/Program Files/Git/`.
- **Avoid rebuilding the app image repeatedly.** Each build adds to the Docker build cache on
  C:, and the cache once filled the disk. `docker builder prune -f` reclaims the space.
- **Never run interpreter-wide tracemalloc (`PYTHONTRACEMALLOC=1`) on the live multi-GB boot.**
  It wedged Docker Desktop on 2026-06-01. Profile with memray or py-spy in a memory-capped
  container.
- **The stochastic segfault (the SIM-445 crash class).** A process in the app image dies now
  and then with `Fatal Python error: Segmentation fault` (exit 139), at a random point in plain
  Python code. Known sites: the ETL sweep, the container unit lane (a rerun passes) and the
  actor-matrix builds. The crashes cluster on rebuild days, minutes after a DuckDB write.
  **The shape of the code is NOT the trigger**: `scripts/genexpr_crash_probe.py` ran every
  synthetic shape clean over 6.7 million evaluations. A sweep that rewrote the hot-path
  generators fixes nothing proven and was not landed (owner decision 2026-10-07). Treat a
  segfault as a retry, not a failure; `scripts/with_retry.sh` reruns a data job after one. On
  the day it recurs, run the probe first. The root cause is open, and no ticket tracks it.
- Canonical git repo: this directory. Primary shell: **Windows Command Prompt (cmd.exe)**;
  development and tests run through Docker (`docker compose run --rm app ...`).

## 2b. Standing owner rulings, the model, and the data-run traps

**The open-ticket list is `BACKLOG.xlsx` (owner decision 2026-09-10):** one tab, one row per
open ticket, ranked by priority, with a plain-English description, a definition of done and a
proposed solution. **The subtitle row (row 2) carries ONLY the updated date, the two pointers
and the next free ticket ID (owner ruling 2026-09-12).** A closure, a status change or a
data-run result goes to `CHANGES.md`, and a ticket's own row carries its status while it is
open. A closed ticket's row is deleted; `CHANGES.md` is its record. The closed-ticket narrative
of the old `BACKLOG.md` is frozen at `docs/archive/BACKLOG-history.md`.

### The rulings that govern all new work

- **The architecture rule, three clauses.** (1) Every decision is a similarity-weighted draw
  from a hard-filtered pool — never a hand-tuned formula (owner ruling 2026-08-10). (2) The
  drawn row IS the play: no post-draw adjustment of any kind; every factor is a draw WEIGHT, or
  OFF (owner ruling 2026-08-29). (3) Every change lands ON at its best-known default, and no
  change waits on a per-change accuracy run (owner ruling 2026-09-16). The paired accuracy
  comparison on 250 games resolves only large effects (the strikeout market needs 83,000
  pitcher-games to resolve a 0.0005 Brier effect; 250 games give 482). Per change, the gates
  are cheap: the unit and regression lanes, and a ten-game smoke of the affected channels
  (`scripts/sim_stats.py`) to catch a collapse. "OFF until its weight lands" means OFF until
  its best-known default exists.
- **Every similarity power is 1 (owner ruling 2026-09-16)** until the designed experiment fits
  the tunable parameters together. The weights are fitted TOGETHER, once, when the owner calls
  the model final: one designed experiment over every factor (`scripts/designed_experiment.py`;
  each arm flips several factors, so 16–20 arms read every factor's effect and the pairwise
  interactions; about three days of unattended compute). The fitted values (batter 1.0,
  fielder 1.2, steal runner 12, advancement runner 20, catcher throwing 2, pitcher against the
  run 12; the split's 16 / 16 / 8; the fielding draw's batter 4; manager usage 4) are its
  starting points. At power 1 the sampler skips the power step.
- **No betting-value measurement until everything is green (owner ruling 2026-09-08).** The
  closing-line-value re-measure, and any edge read built on it, wait until every band the
  lanes grade is inside its range. The strikeout-prop refit may proceed.
- **The play-picker design (owner rulings 2026-09-08; plan
  `docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md`).** The weight order in the pitch
  draws is pitcher first, batter, recency, catcher receiving last. Every actor factor is its
  engine's 0-to-1 score, emitted nightly as a matrix and looked up at draw time. The loop is the
  manager's decisions per plate appearance, then per pitch the pickoff draw, the pitch draw, the
  pitch-result draw (the batted ball is born there) and the steal draw, then the park geometry,
  the fielding draw and the advancement draws. Catcher receiving is a mass-preserving
  ball-strike ratio on taken pitches that ships OFF (enable = SIM-526). A concentration check
  (no factor may put more than its natural share of a draw on the live player's own team)
  gates a strict build at a 3.0 own-staff ratio.
- **The grade is POOL TOTALS (owner ruling 2026-08-20).** The simulator's frequencies certify
  against the play pool's own per-opportunity totals (`tests/acceptance/bands.py`
  `POOL_REFERENCES`, 16 bands); runs and the home-win share stay game-graded. The pool totals
  cannot see a labelling defect, because they read the same labels: the pool's label check
  (`pipeline/batch/pool_chain.py`, `tests/acceptance/test_pool_label_check.py`,
  `scripts/pool_window_census.py --strict`) fails when the pool's count chain drifts more than
  0.5% from real plate appearances.
- **The balanced certifying set (owner ruling 2026-09-09).** The lane grades on 45 games from
  three full-day slates (every team three times), chosen so every graded channel's
  actor-matched expectation sits within 0.6% of the pool's totals
  (`scripts/balanced_game_set.py`). The default lane is 45 × 130 = 5,850 game-sims. The
  home-win channel is underpowered below 13,365 game-sims.
- **The pool window (owner ruling 2026-08-20):** the last three COMPLETED seasons plus the
  current one (`RECENCY_FLOOR_SEASONS = 4`; 2023–2026 today).
- **The slate is schedule-driven (owner ruling 2026-08-29).** The frontend day slate reads the
  MLB Stats API for which games exist; the database adds our simulations, projections and odds
  on top.
- **Grade props on the official box score, never on a derivation from the pitches (owner
  decision 2026-09-12).** `raw.game_player_stats` is one row per player who appeared; no row
  means the player did not play, and the book voids the bet. The event-label reader is the
  fallback only.
- **The closing line is the benchmark (owner decision 2026-09-10)**, because the simulator has a
  probability only once both lineups post, and nearly every game has a closing line. **Closing
  line value is not a metric the model is judged by (owner ruling 2026-09-28):** it describes
  the market. The open owner-decision ticket on four similarity points (SIM-611) asks how the
  project's framing should read.
- **Every market the book posts is stored and priced (owner ruling 2026-09-12):** fifteen
  player-prop markets and fifteen game markets (`pipeline/odds_provider.py`), one row per book.
- **Sample hundreds of games when validating ETL work, never dozens.** Two adversarial review
  rounds found four defects each, all from real payloads at scale, none from reading code. A
  70-game sample reported "100%" on a metric that 950 games disproved.

### What production runs

The loop cheat sheet holds the detail; these are the facts a change most often touches.

- **The pitching change is a draw** from the change opportunity pool, weighted by the live
  manager's usage score; the pen is the MLB box's per-game listing (`SIM_BULLPEN_SOURCE=box`),
  and the entering arm is a second draw over that pen (SIM-427).
- **The running game on the pitch (SIM-554).** On a pitch with a stealable runner the loop
  draws the pickoff BEFORE the pitch, then the pitch and its result, then the steal AFTER the
  result, among real pitches of the same base, outs, count and class. A pickoff that makes the
  third out returns a no-pitch result (`PlayResult.no_pitch`, `pa_voided =
  "pickoff_third_out"`): every reader of a per-pitch result must skip it. With two outs, a third
  strike the catcher holds voids a staged steal; on a dropped-third-strike reach, the got-away
  advance runs first (owner decisions 2026-10-01). The switch is `SIM_STEAL_PITCH_CLASS`
  (default 1). At 0, or on a bundle exported before DuckDB migration 0031 or holding an
  unclassed pitch row, the loop runs the single pre-pitch steal draw instead.
- **The pitch is two draws** (`SIM_PITCH_RESULT_SPLIT=1`): the pitch through the cell index,
  then its result. The fielding draw filters by the born ball's class, weights by the born
  ball's kernel, the batter and fielder matrices and the park in the wall zone only, then runs
  the fence stage against the live park's fence (`park_geometry.json`, certified by
  `scripts/fence_check.py`).
- **Every out is on a pitcher's line.** The run ledger (`_commit_run_delta`) is the one place a
  play becomes runs and outs, and `_record_outs` is the one writer of a pitcher's outs.
  `scripts/sim_stats.py` exits 1 when the pitchers' outs differ from the outs played.
- **Built and OFF:** catcher receiving (`SIM_CATCHER_RECEIVING`, SIM-526), fatigue's
  pitch-count term, the pitch similarity on the fielding draw, the batting-side weight on the
  pitch draw and the reliever-hand weight. The cheat sheet's §4 lists their switches.
- **The lineup's position is the one each starter STARTED at** (SIM-559); each side's starter
  comes from the official box (SIM-558). The API answers 503 with Retry-After for a known game
  whose lineup has no pitcher or no batting order.

### Data runs and their traps

- **The profile recompute.** The nightly rebuild recomputes the current season's profiles and
  pools, then publishes a new bundle. A bare `player_profile_computor` run also builds the
  current season only. A full recompute must pass `--seasons 2017 … 2026 --full-rebuild`, then
  verify `sim.pool_build_metadata.builder_version` (`sim554.1` today), rebuild the bundle and
  refit the calibration.
- **One cutoff per profile table (SIM-551).** Eight `derived.*` profile tables carry
  `asof_date`, and every engine refuses a table stamped at two dates. The computor refuses such
  a rebuild before it writes. A partial recompute must include the season in progress.
- **`make calibrate` DROPS the win-probability reliability curve** from
  `/data/calibration.json`. Write it back from `/data/prop_validation.json` through
  `write_reliability_curve_to_calibration_report` (the one-liner is in `WORKFLOW.md` §6), or
  the app boots with uncalibrated win probabilities.
- **The change pool must carry `game_ymd`**, or the backtest cutoff (SIM-535) refuses every
  backtest game.
- **Migration 0031 applies only through `scripts/rebuild_steal_pool.py --apply-migration`**,
  which writes the pickoff rows in the same transaction. Never apply it as a separate step; the
  new code refuses an un-migrated table before it deletes anything.
- **A full re-sweep of `raw.pitches` takes about 6 hours** (`scripts/resumable_sweep.py`).

## 3. Tech stack

Python 3.13 · NumPy 2.x / pandas / SciPy · FastAPI · Pydantic v2 · PostgreSQL (asyncpg, SQLAlchemy,
Alembic) · **DuckDB** (in-process, no container; the postgres extension) · Redis · scikit-learn
(the arsenal mixture models) · POT (the Wasserstein distance) · FAISS (the pitch-to-pitch and
batted-ball engines) · sqlglot (the SQL console's parser) · the Anthropic SDK (the Data Lab
assistant) · pybaseball · Docker Compose · nginx · Ofelia (the job scheduler) · Prometheus +
Grafana · pytest (asyncio, cov, timeout, benchmark, mock, hypothesis) · ruff · mypy.
**Frontend:** React 18 + Vite + TypeScript, Playwright e2e (the decision record:
`docs/architecture/2026-09-02-adr-frontend-framework.md`).

## 4. Architecture

```
Data sources: MLB Stats API (schedule, feed, box) · Baseball Savant · BettingPros (odds)
  → Postgres raw.* (facts) + sim.* (runs, live state)    ← jobs container nightly; live service on game day
  → DuckDB derived.* profiles + sim.* play pools         ← nightly profile computor
  → 11 similarity engines → nightly actor score matrices
  → the engine-artifact bundle (versioned link)          ← nightly bundle build
  → FullPoolSampler + StateMachine (simulation/)         ← loaded once per forkserver worker
  → BatchRunner (100 games) → results, prop PMFs, market prices, win probability
  → FastAPI (api/) + Redis cache + replay file           → nginx → React frontend
```

`docs/technical/architecture.md` describes the services, the data stores, the process model
and the three paths (a simulation request, game day, the nightly builds).

## 5. Repository map

- `simulation/` — the simulator. `sim_loop.py` holds `StateMachine`'s core and `simulate_game`;
  its mixins live in `loop/`. `full_pool_sampler.py` holds `FullPoolSampler`'s constructor; its
  draws live in `sampler/`, one module per draw. Then the runner (`batch_runner.py`,
  `production_factory.py`, `lineup_resolver.py`, `sim_kwargs.py`), the outputs (`results.py`,
  `prop_distributions.py`, `game_market_distributions.py`, `win_probability.py`) and the "what
  if" (`start_state.py`, `whatif.py`). `flags.py` registers every `SIM_*` flag.
- `similarity/` — `engines/` (the 11 engines), `calibration/` and `similarity_calibration.py`
  (`make calibrate`'s fit), `backtesting/`. `api/state.py`'s `ENGINE_REGISTRY` builds the engines.
- `pipeline/` — `etl/` (the historical and Savant loaders), `live/` (the game-day service),
  `batch/` (the profile computor in `profiles/`, the bundle builder in `artifacts/`, the
  versioned link), `backup/`, the odds providers (`odds_provider.py`, `bettingpros/`) and the
  schedule and feed parsers.
- `betting/` — `clv_engine.py`, `bet_signal.py`, `line_movement.py`.
- `api/` — `main.py` and `startup.py` (the boot), `auth.py`, `state.py`, `sim_jobs.py`,
  `bundle_watch.py`, `routes/` (`games*.py`, `betting*.py`, the Data Lab routes), `schemas/`.
- `db/` — `schemas/`, `migrations/` (Alembic `versions/` and DuckDB `duckdb/`), `sim_store.py`
  (the replay file), `introspect.py` (the one table / column existence probe), `roles/`,
  `backup/`.
- `frontend/` — the React app; `frontend/openapi.json` is the API snapshot and
  `src/api/schema.d.ts` is generated from it.
- `scripts/` — the nightly shell jobs, the loaders, the pool rebuilds (with their step packages),
  the measurement tools (with `accuracy/`), and the one-off probes in `scripts/probes/`. Every
  run output goes to `results/` (ignored by git and Docker); a record a doc cites is committed
  to `docs/audit/records/`.
- `tests/` — `unit/`, `regression/`, `acceptance/`, `integration/`, `performance/`.
- `deploy/` — nginx, Prometheus + Grafana, the Ofelia schedule. `docs/` — see `docs/README.md`.

**Import a name from its home module.** After the code clean-up (SIM-493) no module re-exports
a moved name. `docs/technical/file-index.md` lists every source file with its purpose, and each
area page in `docs/technical/` maps its modules' functions and callers.

## 6. The 9-agent team (see `agent_team.md` for full scopes)

1. **Product Manager** — requirements, backlog, phase sequencing, prioritization.
2. **Baseball Analyst** — domain validation, the draws' filters, run-environment realism.
3. **ML / Modeling Engineer** — the 11 engines, the weights, calibration, the designed experiment.
4. **Data Engineer** — Postgres/DuckDB schema, ETL, live ingestion, nightly builds, migrations, backups.
5. **Backend Developer** — the simulation loop, the sampler, the runner, FastAPI, Redis.
6. **Performance Engineer** — throughput SLA (2 s/game, 30 s/100-game batch), the worker pool, memory.
7. **UX Designer** — the frontend and its design system.
8. **Betting / Markets Analyst** — CLV framework, odds, edges, props, the accuracy comparison.
9. **QA / DevOps** — tests, CI/CD, Docker, deployment, monitoring; the independent cross-validation pass.

**Invoke a role by name** (e.g. "Baseball Analyst: review the steal draw's filters"). The PM
consolidates; QA cross-validates and never self-certifies its own work.

## 7. Development workflow & conventions

- **Sprint workflow:** for each sprint, role agents implement their owned tickets (partition by
  file ownership to avoid concurrent edits to the same file), then an **independent QA
  cross-validation pass** runs the full suite. Record the work in `CHANGES.md` (newest entry
  first, per-agent detail); delete the closed tickets' rows and add the newly filed ones in
  `BACKLOG.xlsx` (§2b).
- **Branches (owner ruling 2026-10-07).** A branch is a named line of work off `master`. A
  **local branch** exists only in the repository on this laptop. A **GitHub branch** is a copy
  that exists only after a push; the two copies sync only on a push or a fetch. A **worktree**
  (`.claude/worktrees/<name>`) is a second folder with its own branch checked out. Uncommitted
  changes in a worktree are in neither git's history nor GitHub. Every branch has one lifecycle:
  1. Create one branch off `master` for one ticket.
  2. Commit as you go. Do not end a session with uncommitted work in a worktree.
  3. Push the branch to GitHub when the work outlives the session. The push is the backup,
     and this host blue-screens often.
  4. Merge into `master` within days. A branch that must wait takes in `master` regularly.
  5. Delete the branch on the day it merges: the local copy, the GitHub copy and its worktree.
     A merged branch loses nothing when you delete it, because its commits live in `master`.

  Abandoned or superseded work: delete the branch. To keep it for reference, tag it first
  (`git tag archive/<branch> <branch>`). Two counts give a branch's state. **Ahead**
  (`git rev-list --count master..<branch>`) counts the commits `master` lacks: the real work.
  **Behind** counts the commits the branch lacks. A merged branch is 0 ahead, and its behind
  count means nothing. When every line of `git cherry master <branch>` starts with `-`, each
  commit is already on `master` by another route, so the branch is merged in content. An
  unmerged branch that falls far behind is a risk: `wave1-remediation` sat 11 weeks and then
  conflicted with `master` in 12 files. A Code session owns its worktree and its branch. Archive
  the session first; do not delete the branch of a session that is not archived.
- **TDD:** tests first, then implementation. Unit tests use the `__new__` constructor bypass
  and in-memory data (no live DB) — see `tests/conftest.py`.
- **Ticketing:** every change maps to a `SIM-NNN` ticket. The next free ID lives in the
  subtitle row of `BACKLOG.xlsx` (row 2); read it there, never from a copy in a document. File
  the ticket when you use the number, not after. NOTE: the in-code `SIM-421` tag has TWO
  meanings; read the tag's own words to tell them apart. (a) A realism-work batch (2026-05) was
  tagged `SIM-421` before the full-pool epic was filed. Its tags talk about per-batter hands,
  the starters carried onto the state, the outcome-type fix and the replay proportions; they
  live in the simulation loop, `game_state.py`, `lineup_resolver.py`, `sim_kwargs.py`,
  `validation/replay_chi_squared.py`, the bundle builder and the profile computor. (b) `SIM-421`
  the ticket is the prop-market work. Its tags talk about prop markets, odds, the box score and
  the per-player prop lines; they live in `simulation/prop_distributions.py`, the odds
  providers, the live odds cycles, `pipeline/etl/opening_line_job.py`,
  `scripts/load_historical_odds.py`, the accuracy comparison, the props route and schema,
  migration 0022, and any `SIM-421 fix` on a box-line credit in the loop. Reconcile a realism
  tag if you touch it.
- **Migrations (mandatory):** every Postgres schema change ships an Alembic migration in
  `db/migrations/versions/`; every DuckDB schema change ships a numbered SQL file in
  `db/migrations/duckdb/` AND increments `db/schemas/duckdb_schema_version.txt`, and the same
  change goes into `db/schemas/02_duckdb_schema.sql`. The living docs state the current
  versions as `Alembic head **NNNN**` and `DuckDB schema **vNN**`; two doc-guard tests fail
  until they match the newest files.
- **Regression gate:** `tests/regression/` holds INVARIANT tests — properties an engine's scores
  must satisfy whatever the weights and data are: bounded [0, 1], symmetric, sorted,
  self-excluded, finite sub-scores, weights summing to 1.0. Run it after any engine refactor.
  **There are no golden-file snapshots, and do not add any (owner ruling 2026-09-10).** The
  model is under continuous deliberate change, so a score moving is the normal case rather than
  a signal; the snapshots failed on every intentional tune, and "regenerate the fixture" became
  a ritual that teaches people to regenerate without reading. Behaviour is graded downstream by
  the acceptance lane against the play pool's own totals — an outcome test, not a memory of last
  week's numbers.
- **Documentation:** the living docs describe the code as it is (history goes to `CHANGES.md`).
  A doc that no longer matches the code is rewritten from the code, not patched (owner
  directive 2026-10-21). A new, moved or deleted source file needs
  `python scripts/docs_file_index.py`; `tests/unit/test_docs_file_index.py` fails until the
  index matches, and it also holds the review path's stage headings equal to the script's
  `STAGES`. The file index shows each file's first docstring sentence, so fix a poor
  description in the docstring. `docs/technical/README.md` says what to update when.
- **Secrets:** never commit credentials; the DSN is read from `BASEBALL_DB_DSN`, and no script
  has a default (`pipeline/db_dsn.require_dsn`). There is a CI `secrets-check` job and a
  `file-integrity` guard (`scripts/check_file_integrity.py`, ast.parse + null-byte scan).

## 8. Commands (run from the repo root; Windows cmd.exe)

The `Makefile` wraps Docker (no local Python install needed for these):

```
make dev / make dev-bg   # build + start all services (foreground / background)
make down                # stop + remove containers and networks
make migrate             # apply all Alembic migrations (the migrate service)
make gen-secrets         # add any missing secret to .env (never changes a set one)
make db-roles            # create/refresh the app + read-only roles (ROTATE_ADMIN_PASSWORD=1 resets the owner's)
make test                # full suite (unit + integration)
make test-unit           # unit tests (TESTS="tests/unit/test_x.py" names files)
make test-regression     # engine invariant gate (no snapshots — see §7)
make test-integration    # integration tests (testcontainers; they skip without a Docker socket)
make lint / make format  # ruff check / ruff format
make type-check          # mypy over the five program packages
make profile-computor    # rebuild the DuckDB profiles + pools
make engine-artifacts    # build + publish a new bundle version
make calibrate           # fit /data/calibration.json (drops the reliability curve — §2b)
make validate-props      # prop-PMF / win-prob validation (FLAGS="... --write-calibration" writes the curve)
make load-historical-odds FLAGS="--seasons 2024 --provider bettingpros"
make ingest-catch-up DAYS=14   # load the last N days' finals, crash-wrapped (Postgres only)
make live-logs           # follow the live service's log
make backup              # back up the three stores into BACKUP_DIR now (from cmd.exe)
make backup-offsite      # copy the newest sets into OFFSITE_BACKUP_DIR now (from cmd.exe)
make backup-status       # each store's newest set and its age, and the off-site mirror
make backup-drill        # restore the newest set into a scratch cluster and check it (SET=<ts>, OFFSITE=1)
make restore-postgres SET=<ts>  # restore one Postgres set (refuses a populated database)
```

Raw equivalents (target **Python 3.13**):

```
pytest tests/unit/ -m "not slow" --cov=similarity --cov=pipeline --cov=simulation --cov=betting --cov=api
ruff check .   &&   ruff format --check .
mypy similarity/ pipeline/ api/ simulation/ betting/   # CI scope; config in pyproject.toml
python scripts/docs_file_index.py                      # regenerate the file index (stdlib + git)
```

## 9. Testing & CI

- **CI = `.github/workflows/ci.yml`**, 12 jobs on every push: lint, type-check, unit-tests
  (with the 80% coverage gate), slow-tests, regression, e2e (the TestClient end-to-end file),
  acceptance-arithmetic, secrets-check, file-integrity, frontend, frontend-e2e and
  docker-build-check. The other workflows: `integration-weekly.yml` (testcontainers),
  `perf-weekly.yml` (hard-gates the `/simulate` SLA under `PERF_STRICT`),
  `acceptance-nightly.yml`, `docker-release.yml` and `frontend-release.yml`.
  `docs/technical/tests.md` maps each lane to its job.
- **CI Python is 3.13.x**, matching Docker and local development; numpy is 2.x. The coverage
  gate is 80 (the 2026-10-20 lane measured 92.2%). CI uses `--tb=native` (a pytest
  `--tb=short` renderer bug — `tb_lineno=None` INTERNALERROR — can otherwise mask real failures).
- **Slow tests** are `@pytest.mark.slow`. The unit job runs `-m "not slow"` at `--timeout=30`;
  the `slow-tests` job runs `-m "slow"` at `--timeout=120`. A per-test
  `@pytest.mark.timeout(N)` overrides the CLI timeout.
- **Doc guards.** `test_docs_alembic_head.py` and `test_docs_duckdb_version.py` hold the
  version phrases to the newest migrations; `test_docs_file_index.py` holds the file index to
  the checkout; `test_openapi_snapshot.py` holds `frontend/openapi.json` to the API.
- **Coverage tip:** measure with `coverage run --parallel-mode` + `coverage combine` (NOT
  `--cov-append` across processes, which under-counts).

## 10. Established design decisions (do NOT relitigate without strong justification)

- GMM covariances stored in standardized space; arsenal W₂ calibrated to Statcast (~0.5–12, median ~2.84);
  linear-exponential `exp(-W₂/4.10)` (not squared). Target median similarity 0.50 across engines.
- EB_N_PRIOR=15 for the fielder engine (defensive metrics stabilize slowly); lower for pitcher/batter.
- Position-partitioned fielder engine (no cross-position scoring). Release-point sub-score excluded from
  the pitcher engine. Compositionally redundant batter features removed (ld_rate, iffb_rate, center_rate).
- DuckDB is in-process (no container). MLB WebSocket is treated as a pure change-signal; all state is
  re-fetched from REST. All `CREATE TABLE` use `IF NOT EXISTS`.
- Run-value constants: 0.75 runs/out saved (IF), 0.90 (OF), 0.25 runs/block, 0.125 runs/strike.

## 11. Defect magnitudes the acceptance bands are sized on

Open defects are rows in `BACKLOG.xlsx`. The Phase-5-close defect list, the 2026-05 live
bring-up record and the steal-volume history are in `docs/archive/CLAUDE-md-history.md`.

`tests/acceptance/bands.py` sizes two band floors on the magnitudes below, and the acceptance
tests cite them as "CLAUDE.md §11". `tests/unit/test_sim_kwargs.py` fails when this
section stops saying "Runs run ~7-8% low". Change that sentence only with a re-calibration.

- **Runs run ~7-8% low (down from ~12% pre-fix).** This was the run-conversion gap after the
  DP-rate fix of 2026-05-28. The gap closed with the 2026-08 data rebuild. A band, floor or
  calibration target that measures this gap must red on a 7% shortfall, so a regression reds.
- **home_win_pct stuck at the structural-only ~.510-.515** is the baseline before the home
  advantage landed. The batted-ball draw hard-conditions on the batting side
  (`SIM_HOME_OFF_WEIGHT=0.0`). The home_win_pct band must reject that baseline.
- **Read R-level moves only from a multi-game batch of at least 400 sims per game.** The
  per-channel breakouts of `scripts/sim_stats.py` are the lens, not the global R mean.

## 12. Phase roadmap

| Phase | Name | Status |
|------|------|--------|
| 1 | Data Infrastructure & Pipeline | ✅ Complete |
| 2 | Similarity Engine Suite (11 engines) | ✅ Complete |
| 3 | Play Pool Architecture | ✅ Complete |
| 4 | Core Simulation Loop | ✅ Complete |
| 5 | Simulation Runner & Backend API | ✅ Complete |
| 6 | Frontend Build | ✅ Complete |
| 7 | Integration, Testing & Deployment | **Largely complete.** The data foundation was rebuilt (2026-08); calibration is live; the live slate, the game page and its "what if" are deployed; the stack is hardened; nightly rebuilds, backups and the off-site copy run. **Open work:** the rows of `BACKLOG.xlsx` — the red bands (§2), the defects of the clean-up, and the designed experiment that fits the weights together. |

## 13. Key references

- `BACKLOG.xlsx` — the authoritative open-ticket list (§2b); verify a ticket's status here
  before acting on it. `docs/archive/BACKLOG-history.md` holds the frozen narrative of the
  closed tickets of the old `BACKLOG.md`.
- `CHANGES.md` — the change record (**newest entries at the top**; per-agent detail).
- `docs/technical/README.md` — the code reference: the architecture, the code review path,
  the file index, the loop cheat sheet and one page per area.
- `docs/technical/sim-loop-cheat-sheet.md` — the loop on one page: every draw, its hard filters,
  its weights and their production values, and the feature groups of each similarity score.
  Keep it current when a weight lands.
- `WORKFLOW.md` — the operator's manual (bring-up, the nightly jobs, health checks, backups).
- `agent_team.md` — full agent scopes and who to call.
- `docs/README.md` — every document, living or record.
- `docs/audit/2026-08-20-sim429-514-diagnosis-results.md` (why the model reads the way it does)
  and `docs/audit/2026-08-28-sim476-fit-plan.md` (the kernel fit).

## 14. Writing standard (applies to ALL prose: chat replies, docs, commit messages, code comments)

Write in **Simplified Technical English (ASD-STE100 style)** and follow **Zinsser's four principles**.
This is a hard requirement, not a preference. It applies to every summary, explanation, audit document,
ticket description, docstring, and inline comment.

**Simplified Technical English — the rules to keep:**

- **One idea per sentence.** Keep sentences to 20 words or fewer. Split a long sentence into two.
- **Use the active voice.** Write "the manager pulls the pitcher", not "the pitcher is pulled".
- **Use the present tense.** Write "the gate blocks the steal", not "the gate would block the steal".
- **Use one term for one thing, every time.** Do not switch between "green-light", "green", and
  "aggression rate" for the same value. Pick one name and keep it.
- **Define a technical term the first time you use it.** Then reuse the same term.
- **No unexplained jargon (owner directive 2026-09-08).** A term like "anisotropic kernel" or
  "Markov structure" gets a plain-English gloss the first time it appears in any summary ("a
  kernel with a separate bandwidth for each feature group"). If the gloss is longer than the term
  is useful, use the gloss alone and drop the term.
- **Describe the work, never a bare ticket number (owner directive 2026-09-08).** The owner does
  not memorize what each ticket is about. Write "the catcher receiving profile (SIM-517)", never
  "SIM-517" alone; in a list of open work, lead with the deliverable in plain words and put the
  ID in parentheses after it.
- **Say who does what.** Name the actor in every sentence. Avoid "it" and "this" with no clear subject.
- **Use simple words.** Write "use", not "utilize". Write "start", not "initiate".
- **Write one instruction per step.** Do not join two actions with "and" in a procedure step.
- **Do not use noun stacks.** Write "the rate of the steal attempt", not "steal attempt rate scaling".
- **Keep the article.** Write "the pool", not "pool".

**Zinsser's four principles — the test to apply before you send:**

1. **Clarity** — the reader must not have to read a sentence twice. If a sentence is unclear, the thought
   behind it is unclear. Fix the thought first.
2. **Simplicity** — cut every word that does no work. Delete "very", "quite", "in order to", "the fact
   that", "it should be noted that".
3. **Brevity** — say it once. Do not restate a point in a summary that the body already made.
4. **Humanity** — write to a person, not to a file. Say "you", say "I", and admit uncertainty plainly.

**Do not do these things:**

- Do not open with a windup ("It's worth noting that…"). Start with the point.
- Do not hedge to sound careful. Say "I did not verify this" instead of "this may potentially differ".
- Do not pad a list to look complete. A short list of real items beats a long list with filler.
- Do not use a term of art without its gloss ("anisotropic", "RBF", "Markov", "ECE", "CLV" on
  first use).

## 15. Working conventions for Claude Code

- Confirm a ticket's status in `BACKLOG.xlsx` before acting — it changes, and it is the single
  source of truth (§2b).
- Keep the agent-team rhythm: implement → independent QA cross-validation → run the full suite →
  record (`CHANGES.md`, `BACKLOG.xlsx`).
- Run `make test-unit` + `make lint` + `make type-check` before committing; run
  `make test-regression` after any engine/model change. Target Python 3.13 to match CI.
- Don't commit credentials; honor the migration and regression conventions above.
- Prefer surgical edits to the loop (`simulation/sim_loop.py` and `simulation/loop/`): the run
  ledger and the outs writer have tests that read their source.
- Follow the branch lifecycle in §7: one branch per ticket, commit before the session ends, and
  delete the branch on the day it merges.
- Ship a fresh API snapshot with every change to the API's contract (SIM-573): a route, a
  parameter, a field or a route's description. In the same commit, run
  `python scripts/export_openapi.py` in the app image with the checkout mounted, never on the
  host: the host's Pydantic 2.10 drops `additionalProperties: true`. Then run
  `cd frontend && npm run gen:api`. `tests/unit/test_openapi_snapshot.py` and a step of
  the CI frontend job fail on a stale `frontend/openapi.json` or `schema.d.ts`.
- Keep the docs current in the same commit as the code (§7, Documentation).
