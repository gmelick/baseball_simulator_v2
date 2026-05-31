# CLAUDE.md — MLB Baseball Simulation Platform

> Project guide for Claude Code. This is a pitch-by-pitch Monte-Carlo MLB game simulator built and run
> as a **sports-trading hedge fund**: the product is player-prop prediction and betting-edge validation,
> anchored to **Closing Line Value (CLV)** as the gold-standard metric. Work is executed by a **9-agent
> team** with cross-validation. Read this file first, then `docs/HANDOFF_PHASE6.md` before starting work.

---

## 1. Purpose & goals

- **What it is:** ingests real Statcast pitch-by-pitch data, builds **11 similarity engines** across
  player/situation dimensions, and runs a stochastic pitch-by-pitch game simulator that produces 100
  independent game iterations per request. Outputs feed win probabilities, per-player prop distributions
  (PMFs), boxscores, and a betting/CLV surface.
- **Primary use case:** predict player props (pitcher Ks, batter hits/HR/TB, etc.) and identify +EV
  betting edges, validated against CLV. Every model number must ultimately be *calibrated* — uncalibrated
  numbers must not reach users.
- **End state:** a live game dashboard (day slate → 3-state game cards → game page with play-by-play,
  per-player projections, linescore, field graphic, managerial override) on top of the completed API.

## 2. Current status (read this)

- **Phases 1–5 + Phase 6 Frontend Build (SIM-378→401 + hardening 415→420) are COMPLETE and CI-green
  on Python 3.11.15.** Unit suite green (the unit lane runs the per-tile path; see below).
- **2026-05-28 closure batch — SIX P1/P2 tickets closed in one day:**
  - SIM-403 — real parallelism (worker-count fix: `SIM_RUNNER_WORKERS` unset → `default_max_workers()`)
  - **SIM-403b** — `EngineArtifacts.{extract,attach}_shared_views` for zero-copy across workers
    via `multiprocessing.shared_memory` (publishes 41 arrays = ~166 MB in the lifespan)
  - SIM-404 — stress/concurrency/leak suite (5 slow-marked integration tests)
  - SIM-409 — `LineupNotIngestedError` → 503 + `Retry-After: 900`; `lineup_ready: bool | None` on `GameCard`
  - SIM-414 — W/L/S + ER + per-runner R reconciliation (sub-5-IP starter rule, inning-reconstruction
    unearned runs, walk-forced R credit)
  - **SIM-412** — home-field run advantage (`_apply_home_field_bias` flips HOME batted-ball outs
    to singles at default 0.025; env override `SIM_HOME_FIELD_BIAS`).  Tuning note: 4×400-sim
    harness run shows current default slightly overshoots (delta R = +0.198 vs target +0.13);
    a future tweak to ~0.017 would land closer.
- **Similarity-engine-wiring / full-pool realism epic (SIM-422→429) — LANDED on `master`.** The
  simulator scores the **entire same-hand play pool** by the applicable similarity engines (no top-K;
  the batter's hand is the only hard filter — the pitcher hand self-zeroes via the pitcher engine)
  and is the **production default** (`SIM_FULL_POOL=1` in the docker-compose `app` env; per-tile
  path is the fallback and unit-test default, pinned off in `tests/conftest.py`).
- **DP-rate bug fix propagated 2026-05-28:** the player-profile computor's
  `dp_turned = outs_on_pitch >= 2` always-False bug was fixed and the 5.7-hour 2017-2025
  recompute completed.  Per-season DP rates now 42-48% (was 0.0).  Actor embeddings rebuilt
  (`fielder_emb` = 11346 × 51 features).  Box output now MLB-realistic: H/HR/2B/BB/K within
  ~3-5% of MLB-2023, steals match MLB volume.  **Runs run ~7-8% low** (down from ~12% pre-fix) —
  remaining hits→runs *conversion* residual lives in batted-ball-with-RISP / sequencing
  (see §11). **Next free ticket ID: SIM-431** (SIM-430 = the full-pool `/simulate`
  throughput / 2s-30s SLA perf gap, filed 2026-05-30 off the SIM-402 live re-measure).

- **SIM-402 — CLOSED 2026-05-30 (code complete + re-measured live); the residual throughput
  gap is spun off to SIM-430.** Live API probed at
  `http://localhost:8000/api/games/{pk}/simulate`.
  - **Cold-worker fix shipped.** `production_machine_factory` passes `fingerprint_deriver=None`
    on the full-pool path (the deriver is unused there but `_default_deriver_builder` did 3 eager
    per-seed disk loads), and a BACKGROUND pre-warm (`BatchRunner.prewarm()` +
    `production_factory.warm_worker_cache()`, lifespan-gated on `SIM_FULL_POOL`, bounded-concurrency
    + a `_get_pool` lock) populates each worker's per-process full-pool cache off the request path.
    This eliminated the n=10 ≈ **498-507s** cold-fan-out stall (a fresh n-iteration request used to
    spread games one-per-worker, so the per-worker cache never warmed in time and every worker paid
    the full artifact-load + per-hand-precompute on the request path).  +13 unit tests
    (`tests/unit/test_sim402_prewarm.py`); ruff + mypy clean.
  - **Live re-measure (2026-05-30, all-seasons DuckDB, 1 worker):** warm n=1 ≈ **2.2-2.3s**,
    n=100 ≈ **215s** serial.  The 2s-game / 30s-batch SLA is **NOT met** on the full-pool path —
    per-game cost is ~2.2s and the n-iteration fan-out does not parallelize at 1 worker.
  - **`SIM_RUNNER_WORKERS=10` is non-viable on this 15.5 GiB host:** a pre-warm worker is
    OOM-killed → the ProcessPool deadlocks → every `/simulate` hangs >400s (the 10-worker
    re-measure returned all-n TimeoutError).  The host `.env` is pinned to **1 worker**, with the
    reason documented inline there.
  - **Remaining work → SIM-430** (new perf ticket): cut the full-pool per-game cost and/or give
    `/simulate` a fan-out that scales without OOM (lighter per-worker footprint or intra-request
    game batching).

## 2a. Operational caveats (Windows + Docker)

- **`scripts/` is NOT volume-mounted** into the running app container; only `api/`, `pipeline/`,
  `similarity/`, `simulation/`, `db/` are (see `docker-compose.yml` line ~110).  Edits to
  `scripts/` are picked up by the running container only after `docker compose build app` +
  `docker compose up -d app` (recreate).  Edits to the mounted dirs are picked up by
  `docker compose restart app` alone.
- **Git Bash on Windows mangles container paths.** Any `docker compose exec` / `docker compose run`
  command from Git Bash that uses a Linux container path like `/app/scripts/foo.py` gets translated
  to `C:/Program Files/Git/app/scripts/foo.py` before Docker sees it.  Prefix with
  `MSYS_NO_PATHCONV=1` to disable the translation.  Tell: the error message includes
  `C:/Program Files/Git/`.
- **Open follow-ons (tracked, blocked on data/infra, not shipped hollow):** SIM-427 engine-backed
  manager (needs a per-(team,season) bullpen roster built from raw Statcast — no role/team source in
  `derived.*`); SIM-425b Fielder RBF (needs per-row fielder identity baked into the batted-ball
  artifact → a play-pool rebuild); SIM-411 park factor + SIM-413 pitcher-hand platoon (both also
  blocked on a play-pool rebuild — engine artifact has no `venue_id` / `p_throws` per row);
  SIM-429 granular run-conversion calibration + the CLV backtest (the larger sim harness landed
  2026-05-28 as `scripts/sim_stats.py` v2 — defaults to 200 sims/game, reports per-channel + home/
  away splits + R standard error; calibration sweeps + CLV backtest pending the live-odds path).
- **Live-env verification debt — largely retired 2026-05-30.**  `docker compose up`
  (nginx+app+monitoring) runs; the 2026-05-29 bring-up fixed a `/dev/shm` overflow
  (`shm_size: 1gb` on the `app` service) and made the pre-warm a BACKGROUND task with
  bounded-concurrency warming + a `_get_pool` lock (a blocking pre-warm hung startup ~22-30 min —
  `asyncio.wait_for` can't interrupt a multiprocessing-blocked thread — and warming all 10 workers
  at once OOM-killed one).
  - **SIM-402 CLOSED** — re-measured live; the 2s/30s SLA is not met and the throughput gap is now
    **SIM-430** (see §2).
  - **SIM-408 CLOSED** — the engine↔DuckDB schema divergence (was **only 7/11 engines build**:
    catcher/manager/baserunner_steal/pitcher_steal failing, situation indexing 0 rows) was
    reconciled and a full all-seasons (2017-2026) profile rebuild ran; the live app now logs
    `build_all_engines: 11/11`.  See §11 for what was trimmed/built per engine.
  - **Still open (now UNBLOCKED by the 11/11 real-data build — the next work):** SIM-406 (a fitted
    `CalibrationReport` over real data, applied to ALL engines) and SIM-407 (prop-PMF +
    win-probability validation + the win-prob reliability-curve fit) — **both CLOSED 2026-05-30**.
- Canonical git repo: this directory. Primary shell: **Windows Command Prompt (cmd.exe)**;
  development + tests run through Docker (`docker compose run --rm app ...`).

## 3. Tech stack

Python 3.11+ · FastAPI · Pydantic v2 · PostgreSQL (async SQLAlchemy + Alembic) · **DuckDB** (in-process,
no container — postgres extension) · Redis · scikit-learn (GMMs) · **FAISS** · NumPy/pandas · scipy · POT
(Wasserstein) · pybaseball · Docker / docker-compose · nginx · Prometheus + Grafana · pytest (+asyncio,
cov, timeout, benchmark, mock, hypothesis) · ruff (lint+format) · mypy. Frontend framework (React vs
vanilla-JS + WebSocket) is the Phase-6 kickoff decision (SIM-378).

## 4. Architecture (layered)

```
Data sources (MLB Stats API REST+WS · Statcast/pybaseball)
  → Data layer: PostgreSQL raw.* + DuckDB derived.*/sim.* ; ETL + nightly profile pre-compute (pipeline/)
  → 11 similarity engines (similarity/engines/) : GMM-W2 pitcher, RBF batter/fielder/baserunner/
    catcher/pitcher-steal/manager, KDTree situation, FAISS pitch-to-pitch + batted-ball
  → Play pool (sim.pitch_pool / sim.outcome_pool + FAISS tiles)  [Phase 3]
  → Full-pool similarity sampler (simulation/full_pool_sampler.py over the SIM-422 engine-artifact
    bundle) : scores the WHOLE same-hand pool by the applicable engines (factorized weights:
    f_pitcher·f_batter·f_situation·recency; count-bucketed pitch draw) — the PRODUCTION path
    (SIM_FULL_POOL=1). The per-tile FAISS k-NN sampler is the fallback / unit-test path.  [SIM-422→429]
  → Core sim loop (simulation/sim_loop.py) : 8-step pitch-by-pitch state machine + manager/situational
    decisions → GameSimResult                                     [Phase 4]
  → Runner + API (simulation/batch_runner.py, api/) : 100-iteration ProcessPool runner, REST + WebSocket,
    Redis cache, persistence (DuckDB v10 / Alembic 0014), betting/CLV surface, auth/rate-limit/CORS,
    nginx, Prometheus/Grafana                                     [Phase 5 — COMPLETE]
  → Frontend (frontend/)                                          [Phase 6 — OPEN]
```

## 5. Repository map

- `api/` — FastAPI app. `main.py` (create_app + lifespan), `routes/` (games, betting, metrics, similarity),
  `schemas.py` (Pydantic), `serialization.py` (numpy-safe `to_jsonable`), `auth.py`, `state.py` (engine build).
- `simulation/` — `sim_loop.py` (the simulator, biggest file; full-pool draw + engine-backed
  advancement/steal/framing live here), `full_pool_sampler.py` (SIM-423 full-pool similarity sampler:
  count-bucket CDFs, batted-ball draw, `runner_rate`/`catcher_framing`), `matchup_provider.py` (SIM-421
  fork-safe deriver/centroid provider), `game_state.py` (carries bat/throw hands + per-team
  pitcher/catcher ids), `results.py`, `batch_runner.py` (ProcessPool runner), `production_factory.py`
  (builds the full-pool sampler from disk per worker when `SIM_FULL_POOL` is set), `lineup_resolver.py`
  (also resolves the per-team catcher via the SIM-363 defense map), `linescore.py`, `pitcher_decisions.py`,
  `play_recorder.py`, `prop_distributions.py`, `win_probability.py`, `snapshots.py`, `score_fusion.py`,
  `fingerprints.py`, `validation/replay_chi_squared.py`.
- `similarity/` — `engines/` (the 11 engines), `similarity_calibration.py`, `backtesting/` (backtester +
  walk-forward), `registry.py`.
- `betting/` — `clv_engine.py`, `bet_signal.py`, `line_movement.py`.
- `pipeline/` — `etl/` (historical loader), `live/live_ingestion_pipeline.py` (MLB WS + REST + odds),
  `batch/player_profile_computor.py` + `play_pool_cache.py` (normalized tiles + persisted norms/centroids)
  + `engine_artifacts.py` (SIM-422 builder + per-worker loader for the full-pool bundle: hand pools,
  pitcher×pitcher sim, batter/catcher/fielder/baserunner/manager embeddings, batted-ball pools),
  `odds_provider.py`.
- `db/` — `migrations/` (Alembic, head 0014) + `migrations/duckdb/` (numbered SQL, schema v10) +
  `schemas/duckdb_schema_version.txt`.
- `tests/` — `unit/`, `regression/` (golden-file engine-drift gate), `integration/` (E2E TestClient),
  `performance/` (pytest-benchmark). `conftest.py` has shared fixtures + the event-loop guard.
- `deploy/` — nginx + Prometheus/Grafana. `frontend/` — greenfield (empty component dirs today).
- `docs/` — `HANDOFF_PHASE*.md`, `SPRINT_*.md`, `audit/`, `architecture/`. Root: `BACKLOG.md`,
  `CHANGES.md`, `agent_team.md`, `README.md`, `WORKFLOW.md`, `PRODUCT_GUIDE.md`, `backlog.xlsx`.

## 6. The 9-agent team (see `agent_team.md` for full scopes)

1. **Product Manager** — requirements, backlog, phase sequencing, prioritization.
2. **Baseball Analyst** — domain validation, feature selection, run-environment realism, manager logic.
3. **ML / Modeling Engineer** — the 11 engines, GMM/RBF/FAISS math, calibration, backtesting/ablation.
4. **Data Engineer** — Postgres/DuckDB schema, ETL, live ingestion, nightly profiles, migrations.
5. **Backend Developer** — sim-loop wiring, FastAPI, WebSocket, Redis, the runner.
6. **Performance Engineer** — throughput SLA (2s/game, 30s/100-game batch), FAISS tuning, vectorization.
7. **UX Designer** — frontend wireframes, design system, components (owns Phase 6 build design).
8. **Betting / Markets Analyst** — CLV framework, odds integration, edge/+EV identification, props.
9. **QA / DevOps** — tests, CI/CD, Docker, deployment, monitoring; the independent cross-validation pass.

**Invoke a role by name** (e.g. "Baseball Analyst: review the manager pull-timing logic"). The PM
consolidates; QA cross-validates and never self-certifies its own work.

## 7. Development workflow & conventions

- **Sprint workflow:** for each sprint, role agents implement their owned tickets (partition by file
  ownership to avoid concurrent edits to the same file), then an **independent QA cross-validation pass**
  runs the full suite. Document in `CHANGES.md` (grows, per-agent detail), trim `BACKLOG.md` to one-line
  rows under a sprint banner, add `docs/SPRINT_<date>_<name>.md`, and regenerate `backlog.xlsx`.
- **TDD:** tests first, then implementation (Backend Developer convention). Unit tests use the `__new__`
  constructor-bypass + in-memory mock pattern (no live DB) — see `tests/conftest.py`.
- **Ticketing:** every change maps to a `SIM-NNN` ticket. Next free ID is tracked in `BACKLOG.md`
  (currently **SIM-431**; SIM-430 is the full-pool `/simulate` throughput / 2s-30s SLA perf ticket
  filed 2026-05-30, and the SIM-422→429 full-pool epic is filed there under its own banner). NOTE: a
  realism-work batch was tagged `SIM-421` *in code comments* before the epic was filed — `SIM-421` the
  ticket is the P3 book-offered-market projection, so treat in-code `SIM-421` tags as the realism work
  and reconcile if you touch them.
- **Migrations (mandatory):** every Postgres schema change ships an Alembic migration in
  `db/migrations/versions/`; every DuckDB schema change ships a numbered SQL file in
  `db/migrations/duckdb/` AND increments `db/schemas/duckdb_schema_version.txt`. *Gotcha:* a past sprint
  bumped a DuckDB migration but forgot the version file + its sanity test — always verify
  version-file == latest-migration-number after a DuckDB schema ticket.
- **Regression gate:** `tests/regression/` holds golden-file + property tests detecting engine drift.
  Regenerate fixtures with `python tests/regression/generate_fixtures.py --force` (only when a model
  change is intentional). After any engine refactor, run the regression suite — a past columnarization
  silently broke the situation-engine golden files.
- **Secrets:** never commit credentials; the DSN is read from `BASEBALL_DB_DSN`. There is a CI
  `secrets-check` job and a `file-integrity` guard (`scripts/check_file_integrity.py`, ast.parse +
  null-byte scan).

## 8. Commands (run from the repo root; Windows cmd.exe)

The `Makefile` wraps Docker (no local Python install needed):

```
make dev               # build + start all services (db, redis, app) foreground
make down              # stop + remove containers/networks
make migrate           # apply all Alembic migrations (db must be healthy)
make test              # full suite (unit + integration)
make test-unit         # unit tests only (no Docker)
make test-regression   # golden-file engine-drift gate
make test-integration  # testcontainers (Postgres + Redis)
make lint              # ruff check
make format            # ruff format
make type-check        # mypy
make profile-computor  # nightly: rebuild DuckDB profiles + sim pools
make play-pool-cache   # nightly (after profile-computor): materialize FAISS tiles
```

Raw equivalents (if running Python directly, target **Python 3.11**):

```
pytest tests/unit/ -m "not slow" --cov=similarity --cov=pipeline --cov=simulation --cov=betting --cov=api
ruff check .   &&   ruff format --check .
mypy similarity/ pipeline/ api/        # CI scope; config in pyproject.toml; pin mypy>=1.8,<2
```

## 9. Testing & CI

- **CI = `.github/workflows/ci.yml`**, 8 jobs on every push/PR: lint (ruff), type-check (mypy),
  **unit-tests + 80% coverage gate**, regression, e2e (SIM-371 TestClient), secrets-check, file-integrity,
  docker-build-check. Plus weekly `integration-weekly.yml` (testcontainers) and a perf job that hard-gates
  the `/simulate` SLA under `PERF_STRICT`. `docker-release.yml` pushes the API image to ghcr on main.
- **CI Python is 3.11.x.** The coverage gate is 80 (currently met at 89%). CI uses `--tb=native`
  (a pytest `--tb=short` renderer bug — `tb_lineno=None` INTERNALERROR — can otherwise mask real failures).
- **Slow tests** (~15) are `@pytest.mark.slow` and currently run in the default unit lane at
  `--timeout=30`; SIM-418 will split them into a dedicated lane. A per-test `@pytest.mark.timeout(N)`
  overrides the CLI timeout (used for the 5000-game exhaustive test).
- **Coverage tip:** measure with `coverage run --parallel-mode` + `coverage combine` (NOT `--cov-append`
  across processes, which under-counts).

## 10. Established design decisions (do NOT relitigate without strong justification)

- GMM covariances stored in standardized space; arsenal W₂ calibrated to Statcast (~0.5–12, median ~2.84);
  linear-exponential `exp(-W₂/4.10)` (not squared). Target median similarity 0.50 across engines.
- EB_N_PRIOR=15 for the fielder engine (defensive metrics stabilize slowly); lower for pitcher/batter.
- Position-partitioned fielder engine (no cross-position scoring). Release-point sub-score excluded from
  the pitcher engine. Compositionally redundant batter features removed (ld_rate, iffb_rate, center_rate).
- DuckDB is in-process (no container). MLB WebSocket is treated as a pure change-signal; all state is
  re-fetched from REST. All `CREATE TABLE` use `IF NOT EXISTS`.
- Run-value constants: 0.75 runs/out saved (IF), 0.90 (OF), 0.25 runs/block, 0.125 runs/strike.

## 11. Known defects / dead-wiring + verification debt (from the Phase-5-close audit)

The audit-era list of issues (kept here for historical context; tickets marked ✓ have closed):
- ✓ The "gold-standard" CLV is computed off an **uncalibrated** win prob — `betting.py` calls
  `win_probability()` without threading `app.state.calibration_map` (**SIM-387** — closed).
- ✓ `require_api_key` is defined but applied to **zero** routes; dev CORS is `*`+credentials
  (**SIM-389** — closed).
- ✓ `SIM_RUNNER_WORKERS=1` serializes `/simulate`; the lifespan runner is built without
  `shared_arrays=` (**SIM-403** worker-count fix + **SIM-403b** `EngineArtifacts.{extract,attach}_shared_views`
  zero-copy across workers — both closed 2026-05-28).
- `GameState.park` is a **dead field** (run environment is park-blind) (**SIM-411** — open, blocked
  on play-pool rebuild for venue_id per-row); pitcher throwing-hand unused in the batted-ball matchup
  (**SIM-413** — open, same blocker).
- ✓ Home-field run advantage missing — `home_win_pct` stuck at the structural-only ~.510-.515
  (**SIM-412** — closed 2026-05-28; `_apply_home_field_bias` flips a small fraction of HOME
  batted-ball outs to singles, default 0.025 calibrated to MLB ~.535-.540; env override
  `SIM_HOME_FIELD_BIAS`).
- ✓ The `/metrics` p95 gauge is an unwired placeholder (**SIM-410** — closed).
- ✓ Pre-existing Phase-6 tickets SIM-127–131 cite **phantom parent tickets** SIM-108/109/112/122–126
  (**SIM-382** backfill — closed).
- ✓ Walk-forced runs missed in per-runner R, ER under-counting, sub-5-IP starter winners
  (**SIM-414a/b/c** — closed 2026-05-28; `_resolve_walk` records forced advances,
  `_half_inning_error_outs_lost` inning-reconstruction for ER, `STARTER_WIN_MIN_OUTS=15` reassignment
  in `pitcher_decisions.py`).
- ✓ Lineup ingestion silent 500s on scheduled games whose lineup hasn't been published
  (**SIM-409** — closed 2026-05-28; `LineupNotIngestedError` → 503 + `Retry-After: 900`;
  `lineup_ready: bool | None` field on `GameCard`).

**Live-environment verification debt** — mostly retired over the 2026-05-29/30 live bring-up.
`docker compose up` of nginx+app+monitoring runs. (SIM-405 real odds provider, SIM-410 p95 timing,
and the SIM-403 worker-count fix closed earlier.) **2026-05-29 → 2026-05-30 update:**
- **SIM-402 — CLOSED.** `/dev/shm` overflow + pre-warm hang/OOM fixed (`shm_size: 1gb`; pre-warm is
  a BACKGROUND task with bounded-concurrency warming + a `_get_pool` lock; healthcheck `start_period`
  180s). Re-measured live (all-seasons DuckDB, 1 worker): n=1 ≈ 2.2-2.3s, n=100 ≈ 215s — the 2s/30s
  SLA is **not met** on the full-pool path, and 10 workers OOM-deadlock on this 15.5 GiB host. The
  throughput gap is now **SIM-430**; the host `.env` is pinned to 1 worker. See §2.
- **SIM-408 — CLOSED.** The engine↔DuckDB schema divergence (was **7/11**: catcher / manager /
  baserunner_steal / pitcher_steal failing, situation indexing 0 rows) was reconciled via the TRIM
  approach and a full all-seasons (2017-2026) profile rebuild — the live app now logs
  `build_all_engines: 11/11`. What changed, per engine: situation now reads a new
  `derived.at_bat_situations` table (+ a fixed park-factor join `pf.factor_type='R'`/`regressed_factor`)
  and raises on a zero-row index; baserunner_steal + pitcher_steal read new metrics tables with the
  biomech (jump/delivery/pickoff) features trimmed (pitcher_steal is now outcome-only); catcher
  derives its rates from existing count columns + two new shadow/heart zone-framing columns (the
  Offense + exchange_time sub-scores were trimmed, weights renormalized); manager's computor was
  rewritten to the engine's usage/aggression/platoon vocabulary with the USAGE sub-score gated NULL
  on SIM-427. Shipped as DuckDB migration `0011` (non-destructive — CREATE new tables +
  `ALTER ... ADD COLUMN IF NOT EXISTS`); schema version 10 → 11. Diagnosis +
  reconciliation map in `docs/audit/2026-05-29-sim408-engine-schema-divergence.md` and
  `docs/audit/2026-05-29-sim408-reconciliation-plan.md`.
- **Still open (now UNBLOCKED by the 11/11 real-data build — the next work):** a fitted
  `CalibrationReport` over real data, **applied to all 8 similarity-score engines** (SIM-406 —
  CLOSED 2026-05-30: `apply_calibration` on every RBF engine + the calibrator extended to the 4
  SIM-408-era engines + `scripts/fit_calibration.py`/`make calibrate`/nightly persist to
  `CALIBRATION_REPORT_PATH`), and prop-PMF validation + the win-prob reliability-curve fit (SIM-407,
  still open).

**Full-pool realism residual (SIM-422→429, the production path):** box rate stats (H/HR/2B/BB/K) are
within ~4% of MLB and steals match MLB volume, but **runs sit ~10-12% low** — a hits→runs *conversion*
gap, not a rate-stat or baserunning-aggression problem (advancement rates are already MLB-realistic; a
global advancement multiplier `SIM_RUN_CALIB` was investigated and rejected as the wrong lever). The
gap lives in batted-ball-with-RISP / sequencing. One concrete contributor identified + fixed: the
batted-ball draw conditions only softly on base-out, so ~55% of drawn double-play events landed with no
runner to double off — `_full_pool_fielding` now records a 2nd out only when a forceable runner exists
(else a 1-out field_out). The harness for the next calibration pass landed 2026-05-28 as
`scripts/sim_stats.py` v2 (defaults to 200 iters/game, reports per-channel + per-half home/away
splits + R standard error so a calibration sweep can target the right channel). Remaining
conversion gap → granular per-channel calibration on this larger harness (SIM-429 follow-on).
*Validation caveat:* run a multi-game × ≥400-sim batch before reading R-level moves; the per-channel
breakouts (RISP, advancement, DP rate) are the right lens, not the global R mean.

## 12. Phase roadmap

| Phase | Name | Status |
|------|------|--------|
| 1 | Data Infrastructure & Pipeline | ✅ Complete |
| 2 | Similarity Engine Suite (11 engines) | ✅ Complete |
| 3 | Play Pool Architecture | ✅ Complete |
| 4 | Core Simulation Loop | ✅ Complete |
| 5 | Simulation Runner & Backend API | ✅ Complete (CI-green on 3.11.15) |
| 6 | **Frontend Build + P1 backend prerequisites** | ✅ **Code-complete** — SIM-378→401 + 415→420 + 414 closed; SIM-402 + 406 + 407 + 408 closed 2026-05-30 |
| 7 | Integration, Testing & Deployment | Live-env bring-up DONE 2026-05-30 (SIM-402 + 406 + 407 + 408 closed). Remaining: SIM-430 (full-pool `/simulate` throughput); the SIM-407 prop-pair population over a live box-score source + a full multi-season `validate-props --write-calibration` run are live follow-ups (code path in place) |

**Realism sub-track (interleaved, landed on `master`):** the SIM-422→429 full-pool similarity-wiring
epic replaced the per-tile k-NN draw with whole-pool engine-weighted sampling and made it the
production default — see §2/§11. This is independent of the frontend critical path below.

**Phase 6 critical path:** SIM-378 (React-vs-vanilla ADR) → 379/380/381 (scaffold + design system +
API→UI serving) + 382/383/384/385/387/389 (backfill deps; enriched games list+records; aggregate card +
status enum; typed WebSocket schema; calibration-wiring fix; auth enforcement) → 386 (live read path) →
391/392 (Day Summary + 3-state cards + linescore/field graphics) → 393/394 (game page + boxscore) →
395/396/397/398 (betting card + CLV chart + override v1 then v2). The data/ML/perf prerequisite track
(402–409) runs alongside and must be live-env verified before its numbers reach users.

## 13. Key references (read before working)

- `docs/HANDOFF_PHASE6.md` — Phase 6 onboarding (what Phase 5 leaves you, scope, risks, how to start).
- `docs/audit/2026-09-02-phase6-prioritized-tickets.md` — the full tiered 43-ticket list + sprint plan.
- `docs/audit/2026-09-02-phase5-close-program-audit.md` — the audit narrative + findings.
- `BACKLOG.md` / `backlog.xlsx` — authoritative ticket status (verify before acting on any ticket).
- `CHANGES.md` — the running changelog (**newest entries prepended at the top**; per-agent detail).
- `agent_team.md` — full agent scopes + the cross-agent collaboration map.
- `WORKFLOW.md` — the operator's manual (clean-checkout bring-up, health checks).

## 14. Working conventions for Claude Code

- Confirm a ticket's status in `BACKLOG.md`/`backlog.xlsx` before acting — they change.
- Keep the agent-team rhythm: implement → independent QA cross-validation → run the full suite → document
  (CHANGES/BACKLOG/SPRINT) → regenerate `backlog.xlsx`.
- Run `make test-unit` + `make lint` + `make type-check` before committing; run `make test-regression`
  after any engine/model change. Target Python 3.11 to match CI.
- Don't commit credentials; honor the migration + regression conventions above.
- Prefer surgical edits to `simulation/sim_loop.py` (it's the largest, most-touched file).
