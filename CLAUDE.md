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

- **Phases 1–5 are COMPLETE and CI-green on Python 3.11.15.** Unit+regression suite: **1814 pass / 1
  skip / 0 fail @ 89% coverage**; 8 CI jobs + weekly perf/integration all green.
- **Phase 6 (Frontend Build) is OPEN.** A full 9-agent program audit filed **43 tickets, SIM-378 →
  SIM-420**. **Next free ticket ID: SIM-421.**
- The `api/` layer serves the full backend surface; the **frontend is greenfield** (`frontend/`
  component dirs are empty). Start Phase 6 at the React-vs-vanilla ADR (SIM-378) — do NOT build
  components before the P0 kickoff gates land.
- Canonical git repo: this directory. Primary shell: **Windows Command Prompt (cmd.exe)**.

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
- `simulation/` — `sim_loop.py` (the simulator, ~2.7k lines, biggest file), `game_state.py`, `results.py`,
  `batch_runner.py` (ProcessPool runner), `production_factory.py`, `lineup_resolver.py`, `linescore.py`,
  `pitcher_decisions.py`, `play_recorder.py`, `prop_distributions.py`, `win_probability.py`, `snapshots.py`,
  `score_fusion.py`, `fingerprints.py`, `validation/replay_chi_squared.py`.
- `similarity/` — `engines/` (the 11 engines), `similarity_calibration.py`, `backtesting/` (backtester +
  walk-forward), `registry.py`.
- `betting/` — `clv_engine.py`, `bet_signal.py`, `line_movement.py`.
- `pipeline/` — `etl/` (historical loader), `live/live_ingestion_pipeline.py` (MLB WS + REST + odds),
  `batch/player_profile_computor.py` + `play_pool_cache.py`, `odds_provider.py`.
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
- **Ticketing:** every change maps to a `SIM-NNN` ticket. Next free ID is tracked in `BACKLOG.md` /
  `backlog.xlsx` (currently **SIM-421**). The `backlog.xlsx` sheets: `Full Backlog` (authoritative, ends
  SIM-420), per-phase `* Gate`/`* Build` sheets.
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

⚠ Fix these early in Phase 6 (tickets in parens):
- The "gold-standard" CLV is computed off an **uncalibrated** win prob — `betting.py` calls
  `win_probability()` without threading `app.state.calibration_map` (**SIM-387**).
- `require_api_key` is defined but applied to **zero** routes; dev CORS is `*`+credentials (**SIM-389**).
- `SIM_RUNNER_WORKERS=1` serializes `/simulate`; the lifespan runner is built without `shared_arrays=`
  (**SIM-403**).
- `GameState.park` is a **dead field** (run environment is park-blind); no home-field edge; pitcher
  throwing-hand unused in the batted-ball matchup (**SIM-411/412/413**).
- The `/metrics` p95 gauge is an unwired placeholder (**SIM-410**).
- Pre-existing Phase-6 tickets SIM-127–131 cite **phantom parent tickets** SIM-108/109/112/122–126 that
  don't exist (**SIM-382** backfills them).

**Live-environment verification debt** (code-complete, only mock/unit-verified — confirm on a staging
bring-up): real-DB `/simulate` 2s/30s SLA (SIM-402), real odds provider (SIM-405), a fitted
`CalibrationReport` (SIM-406), the DuckDB-profile 11-engine build (SIM-408), and a full `docker compose
up` of nginx+app+monitoring.

## 12. Phase roadmap

| Phase | Name | Status |
|------|------|--------|
| 1 | Data Infrastructure & Pipeline | ✅ Complete |
| 2 | Similarity Engine Suite (11 engines) | ✅ Complete |
| 3 | Play Pool Architecture | ✅ Complete |
| 4 | Core Simulation Loop | ✅ Complete |
| 5 | Simulation Runner & Backend API | ✅ Complete (CI-green on 3.11.15) |
| 6 | **Frontend Build** | 🚀 **OPEN** — 43 tickets SIM-378→420 |
| 7 | Integration, Testing & Deployment | Not started |

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
- `CHANGES.md` — the running changelog (chronological; append new entries at the end).
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
