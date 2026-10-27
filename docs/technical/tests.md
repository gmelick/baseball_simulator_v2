# Tests and CI

The test suite checks the platform in layers, from one function to the production
simulator on real data. It holds 341 tracked files under `tests/`: 282 unit-test
modules, the acceptance, regression, integration and performance lanes, their
conftests (the pytest setup files of a folder) and the recorded payloads they read.
Four Playwright specs (browser tests) under `frontend/e2e/` test the web app. Six
GitHub Actions workflows under `.github/workflows/` run the lanes.

## How it fits together

1. `pyproject.toml` configures pytest. It collects `tests/`, reads `test_*.py` and
   `bench_*.py`, runs async tests in auto mode, and rejects an unknown marker
   (`--strict-markers`) or config key (`--strict-config`).
2. [`tests/conftest.py`](../../tests/conftest.py) runs before every lane. At import it
   pins the production `SIM_*` flags to their OFF values, pins the odds provider to
   the mock, and turns the API login off. Before every test it restores a usable
   event loop.
3. The **unit lane** (`tests/unit/`) needs no database, no Redis and no network. It
   builds the production `StateMachine` and `FullPoolSampler` over an in-memory bundle
   ([`simulation/synthetic_bundle.py`](../../simulation/synthetic_bundle.py)), and fakes
   asyncpg, Redis and HTTP. Some tests open a real in-memory DuckDB.
4. The **regression lane** (`tests/regression/`) checks properties that every
   similarity engine's scores must keep for any weights and any data.
5. The **acceptance lane** (`tests/acceptance/`) has two halves. The band arithmetic
   (pure maths) runs on every push. The heavy half runs the production simulator on
   real data, by hand, with `SIM_ACCEPTANCE=1`.
6. The **integration lane** (`tests/integration/`) starts Postgres 15 and Redis 7 in
   throwaway containers (testcontainers), applies every Alembic migration, and tests
   the real writers against the real schema.
7. The **API end-to-end suite** drives the real routers through FastAPI's
   `TestClient` with mock data seams.
8. The **performance lane** (`tests/performance/`) times the hot paths with
   pytest-benchmark. Its thresholds are soft on a laptop and hard with
   `PERF_STRICT=1`.
9. The **frontend lanes** type-check, lint and build the web app, check the generated
   API types, and run the Playwright specs in three browsers against a mocked
   backend.
10. **CI** runs twelve jobs on every push (`ci.yml`), the integration and performance
    lanes weekly, and the acceptance lane on a manual dispatch.

## The lanes

| Lane | What runs | What it proves | Run it here | CI job |
|---|---|---|---|---|
| Unit (fast) | `tests/unit/ -m "not slow"` | Every module's rules, on fakes and the synthetic bundle | `make test-unit` | `unit-tests` (with the 80% coverage gate) |
| Unit (slow) | `tests/unit/ -m slow` (20 marks in 9 files) | Volume variants: whole-game batches, the worker pool, the replay harness | `make test-unit` runs them too | `slow-tests` |
| Regression | `tests/regression/` | Score invariants of five similarity engines | `make test-regression` | `regression` |
| Acceptance arithmetic | `tests/acceptance/test_band_arithmetic.py` | The band rule that decides red or green, and its flag parity with compose | `make test-unit TESTS=tests/acceptance/test_band_arithmetic.py` | `acceptance-arithmetic` |
| Acceptance (heavy) | `tests/acceptance/ -m acceptance` with `SIM_ACCEPTANCE=1` | The production simulator against the play pool's own totals, and the pool's labels against real plate appearances | the command in [The acceptance lane](#the-acceptance-lane) | `acceptance` in `acceptance-nightly.yml` (manual dispatch) |
| Integration | `tests/integration/ -m integration` | The live and ETL writers and the migration chain on real Postgres and Redis | host Python with Docker Desktop (see below) | `integration` in `integration-weekly.yml` (Mondays 03:00 UTC) |
| API end-to-end | `tests/integration/test_api_end_to_end.py` | Cross-endpoint flows, the WebSocket and replay reproducibility | `make test-unit TESTS=tests/integration/test_api_end_to_end.py` | `e2e` |
| Batch-runner stress | `tests/integration/test_batch_runner_stress.py` (all `@slow`) | The warm worker pool under load: no re-fork, no leak | `make test` | none |
| Performance | `tests/performance --benchmark-only` | Hot-path timings and the `/simulate` latency SLA | `pytest tests/performance --benchmark-only` | `benchmark` in `perf-weekly.yml` (Mondays 04:00 UTC) |
| Frontend checks | `npm run gen:api`, `type-check`, `lint`, `build` in `frontend/` | Types, lint, a clean production build, current API types | the same npm commands | `frontend` |
| Frontend E2E | `npm run e2e` in `frontend/` | Rendering, routing and state on chromium, firefox and webkit | `npm run e2e:install` once, then `npm run e2e` | `frontend-e2e` |

## Running the lanes on this machine

The Makefile wraps each Python lane in `docker compose run --rm -v "$(CURDIR):/app" app`.

| Target | Command inside the app container |
|---|---|
| `make test` | `pytest tests/ -v --tb=short --timeout=60` with coverage of the five program packages |
| `make test-unit` | `pytest $(TESTS) -v --tb=short --timeout=30` with coverage; `TESTS` defaults to `tests/unit/` |
| `make test-regression` | `pytest tests/regression/ -v --tb=short --timeout=60 -m regression` |
| `make test-integration` | `pytest tests/integration/ -v --tb=short --timeout=120 -m integration` |
| `make lint` / `make format` | `ruff check . --output-format=full` / `ruff format .` |
| `make type-check` | `mypy similarity/ pipeline/ api/ simulation/ betting/ --ignore-missing-imports --python-version 3.13` |

- **The mount.** The `-v "$(CURDIR):/app"` mount puts the whole checkout over the
  image's copy, so a run tests the checkout's `tests/`, `scripts/` and
  `pyproject.toml`, not the last image build. Compose itself mounts only `api/`,
  `pipeline/`, `similarity/`, `simulation/` and `db/`. A bare
  `docker compose run app pytest` without the mount still runs the image's copy.
- **Name the files.** `make test-unit TESTS="tests/unit/test_x.py tests/unit/test_y.py"`
  runs only those files.
- **`make test-unit` runs the slow tests.** It passes no `-m` filter, so the `@slow`
  tests run at the 30-second timeout unless their own timeout mark overrides it. CI
  splits them into the `slow-tests` job.
- **Run from the main checkout.** From a worktree, `make` reads the worktree's own
  compose file and `.env`, and that compose project has no app image yet.
- **The integration lane needs the host.** The app container has no Docker socket, so
  inside it the conftest's `docker version` probe fails and every integration test
  skips. Run the lane on the host's Python with Docker Desktop running:
  `pytest tests/integration/ -m integration --timeout=120`.
- **The frontend lanes run in `frontend/`:** `npm ci`, then `npm run type-check`,
  `npm run lint`, `npm run build`, `npm run gen:api`, `npm run e2e:install` and
  `npm run e2e`.

### Markers and timeouts

| Marker | Where it is registered | Meaning |
|---|---|---|
| `unit` | `pyproject.toml` | No external services |
| `integration` | `pyproject.toml` | Needs the testcontainers Postgres and Redis |
| `slow` | `pyproject.toml` | Over about a second; the fast unit job excludes it |
| `benchmark` | `pyproject.toml` | Uses pytest-benchmark |
| `regression` | `pyproject.toml` | The engine invariant gate |
| `acceptance` | `tests/acceptance/conftest.py` (`pytest_configure`) | The heavy acceptance lane |

A test's own `@pytest.mark.timeout(N)` beats the command-line timeout:
`test_filter_cells.py` (300 s), `test_replay_chi_squared.py` (300 s, 1,800 games),
`test_invalid_state_detection.py` (120 s, the 5,000-game run), `test_worker_prewarm.py`
(60 s) and the module mark of `test_pool_label_check.py` (300 s).

### Coverage

`[tool.coverage.run]` in `pyproject.toml` measures `similarity`, `pipeline`,
`simulation`, `betting` and `api`, and omits the command-line, diagnostic and
live-database modules that the unit lane only touches through mocks. The CI
`unit-tests` job reads the line rate from `coverage.xml` and fails below 80%.

## Shared configuration and helpers

### `tests/conftest.py`

[Source](../../tests/conftest.py) · 382 lines · Tests: [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py), [test_odds_provider_pin.py](../../tests/unit/test_odds_provider_pin.py)

The root conftest. It sets the environment at import time, for the whole process, so
the unit lane's output stays byte-identical (the same, bit for bit) whatever the
stack's `.env` holds. A test that wants a feature on sets the sampler or machine
attribute directly, or uses `monkeypatch`.

| Setting | Value | Why |
|---|---|---|
| `SIM_MANAGER`, `SIM_MANAGER_DRAW` | `0` | No manager and no pitching change; the manager tests opt in |
| `SIM_ACTOR_POWER_MANAGER_USAGE` and the six `SIM_RELIEF_*` weights | their OFF values | The pool's own change rate and the positional reliever pick |
| `SIM_BULLPEN_SOURCE` | `synthetic` | The no-DB seam's pen, not the MLB box listing |
| `SIM_BB_PLATOON`, `SIM_CATCHER_RECEIVING`, `SIM_GOT_AWAY` | `0` | Off; the tests opt in |
| `SIM_HOME_OFF_WEIGHT`, `SIM_PITCH_HOME_OFF_WEIGHT` | `1.0` | Skips the home-field weight exactly |
| `SIM_PARK_KERNEL_SIGMA`, `SIM_BB_PITCH_SIGMA`, `SIM_FATIGUE_PC_SIGMA`, `SIM_FATIGUE_TTO_SIGMA` | `0` | The kernels are off |
| `SIM_PITCH_CELL_INDEX`, `SIM_PITCH_RESULT_SPLIT`, `SIM_BB_BORN_SIGMA` | `0` | One whole-pool draw per pitch |
| `SIM_PITCH_PITCHER_POWER` | `1.0` | The raw score, no power step |
| `SIM_BB_CLASS_FILTER`, `SIM_PARK_WALL_ZONE_ONLY`, `SIM_FENCE_STAGE`, `SIM_CARRY_OFFSET`, `SIM_BB_BORN_PER_FEATURE`, `SIM_BB_MARGIN_BAND` | `0` | The fielding draw's filters and the fence stage are off |
| `SIM_RESTART_ON_NEW_BUNDLE`, `SIM_BOOT_LOCK_WAIT_S` | `0` | A test never restarts PID 1 and never waits for a DuckDB writer |
| `BASEBALL_ENGINE_ARTIFACT_DIR` | removed | A test never reads a bundle version a lifespan pinned |
| `AUTH_DISABLED` | `1` (a default, not a pin) | Route tests need no login; two auth test files delete it |
| `ODDS_PROVIDER` | `mock` (a hard pin) | The stack's `.env` names the vendor; the pin keeps every test off the network |
| `ODDS_API_KEY` | removed | A test that still builds the vendor's provider sends no BettingPros request |

`SIM_STEAL_PITCH_CLASS` (the running game on the pitch, SIM-554) is NOT pinned, on
purpose: the unit lane builds its samplers directly, so the no-database tests run
the production order of the pickoff and steal draws.

| Fixture | What it gives |
|---|---|
| `sample_game_pk`, `sample_player_ids` | Stable fake identifiers |
| `mock_db_pool` | An `AsyncMock` asyncpg pool; `fetch` returns `[]`, `execute` returns `"UPDATE 0"` |
| `mock_redis` | An `AsyncMock` Redis client; every read misses |
| `mock_http_session` | A mock aiohttp session whose `.get()` returns an async-context response |
| `sample_feed`, `sample_game_state` | A minimal MLB live feed and the game state the live builder makes from it |
| `_ensure_current_event_loop` (autouse) | A usable current event loop before every test. pytest-asyncio leaves the loop `None` after an async test, and a later sync test would fail only in a full run |

**Watch out:** `tests/unit/test_no_pitch_readers.py` reads this file's text and fails
if it pins `SIM_STEAL_PITCH_CLASS`.

### `tests/script_loader.py`

[Source](../../tests/script_loader.py) · 60 lines · Tests: used by about 45 test files

`scripts/` is not a package, so a test imports a script by its file path.

| Function | What it does | Called from |
|---|---|---|
| `load_script(name, module_name=None)` | Runs `scripts/<name>.py` or `scripts/probes/<name>.py` as a fresh module and registers it in `sys.modules` | The script tests |
| `scripts_on_path()` | Puts the repository root and `scripts/` on `sys.path`, so a stage package such as `scripts/accuracy/` imports by name | The script tests, `load_script` |
| `script_path(name)` | Finds the script in `scripts/` or `scripts/probes/` | `load_script` |

### `simulation/synthetic_bundle.py`

[Source](../../simulation/synthetic_bundle.py) · 688 lines · Tests: [test_synthetic_bundle.py](../../tests/unit/test_synthetic_bundle.py)

The in-memory engine-artifact bundle every no-DB test draws from. It builds a small
`EngineArtifacts` in the exact shape the production loader produces. The sampler,
the loop and the in-play path are the production code; only the rows are synthetic.
27 test files import it.

| Function | What it builds |
|---|---|
| `league_artifacts` | The league-average mix over every count and base-out cell, with the advancement draws on and a change pool, so a no-DB game is realistic baseball |
| `fixed_play_artifacts` | One event in every base-out cell and no advancement pools: a deterministic play for a unit test |
| `synthetic_artifacts` | The general builder the two above wrap (pitch mix per hand, batted-ball rows, steal and advancement pools, the change pool) |
| `synthetic_sampler` | A `FullPoolSampler` over a bundle (the league bundle by default) |
| `canonical_transition`, `inplay_rows`, `pitch_pool`, `battedball_pool`, `advancement_pools`, `steal_pools`, `change_pool` | The row builders |

`simulation.batch_runner.rng_driven_machine_factory` is the picklable no-DB factory
over `league_artifacts`. A `GameSpec` names it by dotted path; the batch-runner, API
and performance tests use it (24 files).

### `tests/fixtures/`

Recorded payloads, trimmed to the fields the parsers read. No test fetches them live.

| Folder | What it holds | Read by |
|---|---|---|
| `bettingpros/` (20 JSON files) | Vendor events and offers: moneyline, run line, total, team totals, first-five and first-inning markets, props with two pages | The odds-provider and odds-loader tests |
| `bullpen/mlb_roster_active_116.json` | One team's active roster | `test_bullpen_availability.py` |
| `mlb/boxscore_746437.json` | One official box score | `test_boxscore_ingest.py`, `test_boxscore_reader.py`, `test_accuracy_comparison_props.py`, `test_game_bullpen_listing.py` |
| `mlb_game_feed/` (4 feeds + `record_fixtures.py`) | Trimmed live feeds (a preview, two finals, one with every play); the script records new ones by hand | `test_game_feed.py`, `test_game_page_whatif.py`, `test_published_lineup_writer.py` |
| `mlb_schedule/` (8 JSON files) | League schedule responses: a normal day, an off day, a postponement, a cancellation, a suspension, two kinds of doubleheader, a preview | `test_schedule_client.py`, `test_slate_endpoint.py`, `test_published_lineup_writer.py` |

## The acceptance lane

The lane runs the simulator users get and grades it. Every other lane pins the
production flags OFF; this package turns them back on. It grades on the balanced
certifying set (owner ruling 2026-09-09): 45 games from three full-day slates, every
team three times, chosen by
[`scripts/balanced_game_set.py`](../../scripts/balanced_game_set.py) so that every graded
channel's expected rate sits within 0.6% of the pool's totals.

Run the certifying lane (45 games × 130 iterations = 5,850 game-sims, about 3.7 hours):

```
MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD:/app" \
    -e SIM_ACCEPTANCE=1 -e SIM_ACCEPTANCE_TIMEOUT=14400 \
    app pytest tests/acceptance -v -rxX
```

`SIM_ACCEPTANCE_GAMES` and `SIM_ACCEPTANCE_ITERS` shorten a run (a smoke run certifies
nothing). `SIM_ACCEPTANCE_JSON_OUT` writes a machine-readable record. The mount is
required: the lane parses `scripts/sim_stats.py`, which compose does not mount.

### `tests/acceptance/bands.py`

[Source](../../tests/acceptance/bands.py) · 1,412 lines · Tests: [test_band_arithmetic.py](../../tests/acceptance/test_band_arithmetic.py)

The reference rates and the band rule, as pure arithmetic. It imports nothing from
the simulator.

| Name | What it holds |
|---|---|
| `REFERENCES`, `CHANNELS` | The 13 game channels (R, H, HR, 2B, 3B, BB, K, SB, CS, DP, two reach-on-error rates, `home_win_pct`), each with its centre, floor and the documented defect it must detect |
| `evaluate`, `half_width`, `BandResult` | The game-band rule: `half_width = max(Z * sd / sqrt(n), floor)` with `Z = 4.0` and `sd` measured from the run. Four verdicts: UNDERPOWERED, UNRESOLVED, FAIL, PASS |
| `POOL_REFERENCES`, `evaluate_pool`, `PoolBandResult` | The 16 per-opportunity pool bands (owner ruling 2026-08-20): walks, intentional walks, strikeouts, hit by pitch and pitches per plate appearance; singles, doubles, triples, home runs and reach on error per ball in play; steal attempts at second and third, the steal success share, double plays per opportunity, called strikes on taken pitches, got-away pitches. Verdicts: PASS, RED, UNDERPOWERED |
| `SUPERSEDED_BY_POOL` | The 11 game channels that the pool bands replace as certification. They stay measured and printed. R and `home_win_pct` stay game-graded |
| `BALANCED_GAME_ORDER`, `ACCEPTANCE_PARK_FACTORS`, `MAX_PREFIX_PARK_BIAS` | The 45 game keys in a park-balanced order, so every prefix of four or more games stays within 2% of the set's mean park factor |
| `required_sims`, `binding_requirement`, `detection_power` | The run size each channel needs (the box channels need 5,648 game-sims; `home_win_pct` needs 13,365 decisive games) |

**Watch out:** a floor's citation must name a file and a section ("CLAUDE.md §11"),
never a line number; `test_band_arithmetic.py` enforces this.

### `tests/acceptance/conftest.py`

[Source](../../tests/acceptance/conftest.py) · 954 lines · Tests: [test_band_arithmetic.py](../../tests/acceptance/test_band_arithmetic.py)

| Name | What it does |
|---|---|
| `PRODUCTION_FLAGS` | The production configuration: the inverse of the root pins. Each `SIM<ticket>_LANE_*` variable sets one flag for a run that measures another arm |
| `production_flags` (package-scoped, autouse) | Writes `PRODUCTION_FLAGS` into the environment and asserts them. Package scope sets them before the run fixture and removes them before `tests/unit` runs |
| `preconditions` | Fails (does not skip) when `SIM_ACCEPTANCE=1` asked for the lane and the bundle, DuckDB or Postgres is missing |
| `acceptance_run` | Resolves each game's state, runs the production factory serially, and wraps the measured methods with counting probes |
| `check_running_game_order` | Fails when `SIM_STEAL_PITCH_CLASS` and the loop's actual pickoff and steal order disagree (an old bundle runs the single pre-pitch draw) |
| `pytest_configure` | Registers the `acceptance` marker |
| `DEFAULT_GAMES`, `DEFAULT_ITERS` | 45 and 130 |

### `tests/acceptance/test_band_arithmetic.py`

[Source](../../tests/acceptance/test_band_arithmetic.py) · 1,494 lines · 58 tests · no database

It proves the band rule itself: a band can fail, a correct model passes at the
required size, a short run reports UNDERPOWERED, the R band reds on the documented
7% run shortfall, `home_win_pct` reds on the structural-only baseline, and the
balanced order keeps every prefix park-balanced. It checks `bands.py`'s restated MLB
constants against `scripts/sim_stats.py`, and checks that `PRODUCTION_FLAGS` and the
`app` environment in `docker-compose.yml` agree (the split, the fatigue weights, the
manager draw, the running game).

### `tests/acceptance/test_production_bands.py`

[Source](../../tests/acceptance/test_production_bands.py) · 720 lines · 42 tests · skips without `SIM_ACCEPTANCE=1`

The heavy half. It asserts each game band and pool band, that the production flags
are on, that the run is large enough, that the game set does not bias the run
environment, that the realism inputs reach the simulator, and that each measured
method really ran (`_full_pool_outcome`, `_full_pool_fielding`,
`_resolve_in_play_transition`, `_commit_run_delta`, `_steal_opportunity_draw`).

### `tests/acceptance/test_pool_label_check.py`

[Source](../../tests/acceptance/test_pool_label_check.py) · 259 lines · 4 tests · needs DuckDB and Postgres

The pitch pool's count chain against real plate appearances (the foul-tip coding fix,
SIM-553). It counts strikeouts, walks, hit by pitch and pitches per plate appearance
from `raw.pitches` and fails when the chain drifts more than 0.5%
(`pipeline.batch.pool_chain.LABEL_CHECK_TOLERANCE`). The pool bands cannot see a
labelling defect, because they read the same labels.

## The regression lane

### `tests/regression/test_engine_regression.py`

[Source](../../tests/regression/test_engine_regression.py) · 443 lines · with [conftest.py](../../tests/regression/conftest.py) (323 lines) and [regression_config.py](../../tests/regression/regression_config.py) (52 lines)

The engine invariant gate. The conftest builds five engines through `__new__` with
seeded synthetic profiles (seed 2026, no DuckDB): the steal runner, the catcher, the
pitcher against the run, the manager and the situation engine. For each engine the
tests assert that scores lie in [0, 1], that scoring is symmetric (within
`SYMMETRY_TOLERANCE`, 1e-9), that results sort descending and exclude the query
profile, that every sub-score is present and finite, and that identical profiles
score near 1. The situation tests check the KD-tree query instead (sorted distances,
clipped score difference, batch equals single).

There are no golden-file snapshots and no asserted weight splits (owner ruling
2026-09-10). A score that moves is the normal case; the acceptance lane grades
behaviour.

## The integration lane

### `tests/integration/conftest.py`

[Source](../../tests/integration/conftest.py) · 274 lines

Session-scoped testcontainers fixtures: `pg_container` (postgres:15-alpine, with
`alembic upgrade head` applied), `pg_engine`, `pg_connection`, `redis_container`
(redis:7-alpine), `redis_client`, `async_redis_client` and an asyncpg fixture. A
`docker version` probe turns a missing daemon into one clear skip per test.

| File | Marker | What it proves |
|---|---|---|
| [test_schema_migrations.py](../../tests/integration/test_schema_migrations.py) | `integration` | `alembic upgrade head` builds exactly the canonical `raw.*` and `sim.*` tables, the lineup upsert's partial unique index, and the head revision |
| [test_etl_flow.py](../../tests/integration/test_etl_flow.py) | `integration` | The ETL freshness writes to `raw.etl_data_freshness` |
| [test_live_game_upsert.py](../../tests/integration/test_live_game_upsert.py) | `integration` | The live pipeline's game upsert supplies the season its foreign keys need |
| [test_live_pipeline_upsert.py](../../tests/integration/test_live_pipeline_upsert.py) | `integration` | The live lineup-state upsert deduplicates and stays idempotent |
| [test_api_end_to_end.py](../../tests/integration/test_api_end_to_end.py) | none (one test `slow`) | The real games, betting and WebSocket routers through `TestClient`, a fake asyncpg pool and the no-DB factory: cross-endpoint consistency and replay reproducibility |
| [test_batch_runner_stress.py](../../tests/integration/test_batch_runner_stress.py) | `slow` on every class | A real multi-worker pool: the same warm pool across requests, no worker leak |

**Watch out:** the last two files carry no `integration` marker, so
`make test-integration` and the weekly job skip them. CI runs the end-to-end file in
the `e2e` job; no CI job runs the stress file.

## The performance lane

| File | What it times | Threshold |
|---|---|---|
| [bench_simulation.py](../../tests/performance/bench_simulation.py) (635 lines) | The pitcher engine query, the arsenal cache lookup, one GMM fit, one `step_pitch`, the batch-runner throughput | p50 ≤ 5 ms and p99 ≤ 20 ms per query; ≤ 0.1 ms per lookup; ≤ 1.23 ms per pitch; ≤ 0.37 s per game |
| [bench_api_simulate.py](../../tests/performance/bench_api_simulate.py) (343 lines) | `GET /api/games/{game_pk}/simulate` through `TestClient` over the no-DB factory | one game < 2 s; a 100-iteration batch < 30 s |

By default a missed target prints a warning and the suite stays green. With
`PERF_STRICT=1` the targets fail. The per-pitch target also needs
`PERF_STRICT_SANDBOX=1`. The weekly job sets both and also fails when the suite's peak
memory exceeds 1.5 GB.

## The frontend lanes

[`frontend/playwright.config.ts`](../../frontend/playwright.config.ts) builds the app,
serves it on port 4173, and runs every spec in chromium, firefox and webkit. Each spec
mocks the backend with `page.route`, so no API or database runs.

| Spec | What it checks |
|---|---|
| [smoke.spec.ts](../../frontend/e2e/smoke.spec.ts) | Rendering, routing and the login gate |
| [slate.spec.ts](../../frontend/e2e/slate.spec.ts) | The day slate: card states, sort, the live card's field, box-score tabs, settled lines, lineups, the calendar, the degraded banner, the phone layout |
| [gamepage.spec.ts](../../frontend/e2e/gamepage.spec.ts) | The game page: the real game, the what-if panel and its two runs, the collapsible panels, the betting card's grading |
| [simulation.spec.ts](../../frontend/e2e/simulation.spec.ts) | One simulation run: queue, progress, cancel, the projections it feeds, the countdown on an unpublished lineup |

The `frontend` job first runs `npm run gen:api` and fails when
`src/api/schema.d.ts` differs from a fresh generation from `frontend/openapi.json`.

## Tests that read documents or configuration

These tests fail when prose or configuration drifts from the code. Update the text
in the same commit as the change.

| Test | What it holds |
|---|---|
| [test_docs_alembic_head.py](../../tests/unit/test_docs_alembic_head.py) | `CLAUDE.md`, `WORKFLOW.md`, `docs/technical/architecture.md` and `docs/technical/database.md` state the newest Alembic migration as `Alembic head **NNNN**`; `WORKFLOW.md`'s `alembic current` check shows it |
| [test_docs_duckdb_version.py](../../tests/unit/test_docs_duckdb_version.py) | The same four documents state the version in `db/schemas/duckdb_schema_version.txt` as `DuckDB schema **vNN**`, and the version file equals the newest DuckDB migration |
| [test_docs_file_index.py](../../tests/unit/test_docs_file_index.py) | `docs/technical/file-index.md` lists every source file in its review stage; the fix is `python scripts/docs_file_index.py`. It skips without `git` (the app image has none), so CI runs it |
| [test_sim_kwargs.py](../../tests/unit/test_sim_kwargs.py) (`test_the_cited_section_still_says_it`) | `CLAUDE.md` §11 still says "Runs run ~7-8% low" |
| [test_arm_angle_decision.py](../../tests/unit/test_arm_angle_decision.py) | `docs/technical/similarity.md` (inside the pitcher engine's section) and the loop cheat sheet carry the arm-angle decision (SIM-533) |
| [test_openapi_snapshot.py](../../tests/unit/test_openapi_snapshot.py) | `frontend/openapi.json` equals the app's OpenAPI schema, and `schema.d.ts` covers its routes |
| [test_flag_registry.py](../../tests/unit/test_flag_registry.py) | Every `SIM_*` name the API or simulator reads, and every `SIM_*` key in the compose `app` environment, is in `simulation/flags.py` |
| [test_script_flag_names.py](../../tests/unit/test_script_flag_names.py) | Every `SIM_*` name in `scripts/` (less `scripts/probes/`) is registered |
| [test_band_arithmetic.py](../../tests/acceptance/test_band_arithmetic.py) | Floor citations name a file and section; the lane's flags match compose |
| [test_no_default_password.py](../../tests/unit/test_no_default_password.py) | No tracked file holds the retired default password; compose publishes the internal ports on loopback and reads every secret with no default; the scheduler INI holds no DSN; the nightly shell scripts need `BASEBALL_DB_DSN` |
| [test_secrets_baseline.py](../../tests/unit/test_secrets_baseline.py) | `.env.example` documents the required variables; `.gitignore` keeps `.env` out |
| [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py) | The three Ofelia backup jobs (10:00 and 10:30 UTC) and the compose mounts of the backup folders |
| [test_nginx_resolve.py](../../tests/unit/test_nginx_resolve.py) | nginx re-resolves the app's name while it runs |
| [test_migration_chain.py](../../tests/unit/test_migration_chain.py), [test_schema_pitch_indexes.py](../../tests/unit/test_schema_pitch_indexes.py) | The first eleven Alembic migrations chain; the `raw.pitches` indexes match their migrations |

### Tests that read source code

| Test | What it holds |
|---|---|
| [test_run_ledger.py](../../tests/unit/test_run_ledger.py) | Each resolver snapshots the bases before it changes them (`inspect.getsource` of each method) |
| [test_pitcher_outs_credit.py](../../tests/unit/test_pitcher_outs_credit.py) | `_record_outs` is the one writer of a box line's `outs_recorded`, across `sim_loop.py` and every `simulation/loop/` module |
| [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py) | Every reader of a per-pitch result skips a no-pitch result |
| [test_bundle_no_pickle.py](../../tests/unit/test_bundle_no_pickle.py) | Every `np.load` in the bundle loader passes `allow_pickle=False` |
| [test_route_auth_coverage.py](../../tests/unit/test_route_auth_coverage.py) | Every non-exempt route of the real app requires a login |

## Unit tests by code area

The tables group the 282 modules by the code they cover. "slow" marks a module with
`@pytest.mark.slow` tests.

### The simulation loop (`simulation/sim_loop.py`, `simulation/loop/`)

| File | What it pins |
|---|---|
| [test_state_machine.py](../../tests/unit/test_state_machine.py) | The count machine and the half-inning roll |
| [test_simulate_game_driver.py](../../tests/unit/test_simulate_game_driver.py) | The full-game driver: regulation, walk-off, extra innings, the ghost runner, seeding |
| [test_simulate_game_requires_lineup.py](../../tests/unit/test_simulate_game_requires_lineup.py) | A game with no batting order is refused before the first pitch |
| [test_game_state_contract.py](../../tests/unit/test_game_state_contract.py), [test_base_invariants.py](../../tests/unit/test_base_invariants.py) | The `GameState` / `PlayResult` contract; the base guards and runner conservation |
| [test_run_ledger.py](../../tests/unit/test_run_ledger.py), [test_run_resolution.py](../../tests/unit/test_run_resolution.py) | The run ledger takes both base-out states; the event vocabulary and the run-expectancy delta (RE24) |
| [test_simulation_constants.py](../../tests/unit/test_simulation_constants.py) | The outcome vocabulary and the defensive run constants |
| [test_rng_streams.py](../../tests/unit/test_rng_streams.py) | The loop's and the sampler's random streams are independent |
| [test_fielding_and_baserunning_steps.py](../../tests/unit/test_fielding_and_baserunning_steps.py) | The fielding step, the baserunning and steal step, the dropped third strike |
| [test_running_game_on_the_pitch.py](../../tests/unit/test_running_game_on_the_pitch.py) | The pickoff before the pitch, the steal after its result, the void and got-away rules (SIM-554) |
| [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py) | Every reader handles a pickoff third out with no pitch |
| [test_dropped_third_strike_credits.py](../../tests/unit/test_dropped_third_strike_credits.py), [test_steal_run_credit.py](../../tests/unit/test_steal_run_credit.py), [test_runner_run_credit.py](../../tests/unit/test_runner_run_credit.py) | Box credits: the dropped-third-strike reach, a steal run (no RBI, earned), each runner's R |
| [test_player_stat_lines.py](../../tests/unit/test_player_stat_lines.py), [test_boxscore_extended.py](../../tests/unit/test_boxscore_extended.py) | Per-player stat lines and the extended box fields |
| [test_pitcher_outs_credit.py](../../tests/unit/test_pitcher_outs_credit.py), [test_outs_identity.py](../../tests/unit/test_outs_identity.py), [test_play_pitcher_decisions.py](../../tests/unit/test_play_pitcher_decisions.py) | Every out on a pitcher's line (SIM-557); the play names its pitcher for the win module |
| [test_manager_decisions.py](../../tests/unit/test_manager_decisions.py), [test_manager_model.py](../../tests/unit/test_manager_model.py), [test_situational_decisions.py](../../tests/unit/test_situational_decisions.py) | Leverage and the intentional walk; the pitch count and the `SIM_MANAGER` gate; whole games with and without a manager profile |
| [test_matchup_weight_cache.py](../../tests/unit/test_matchup_weight_cache.py) | The sampler's weights refresh on three separate keys |
| [test_receiving_kernel_consumers.py](../../tests/unit/test_receiving_kernel_consumers.py) | The got-away pitch: read from the drawn row, resolved in the loop |
| [test_synthetic_bundle.py](../../tests/unit/test_synthetic_bundle.py) | The in-memory bundle's shape |
| [test_baseball_sniff.py](../../tests/unit/test_baseball_sniff.py) (slow), [test_invalid_state_detection.py](../../tests/unit/test_invalid_state_detection.py) (slow) | Whole games produce sane baseball and never an invalid state |

### The sampler (`simulation/full_pool_sampler.py`, `simulation/sampler/`)

| File | What it pins |
|---|---|
| [test_pitch_cell_index.py](../../tests/unit/test_pitch_cell_index.py) | The pitch draw's cell index (runners, outs, count, score band, side) and its widening |
| [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py) | The pitch / pitch-result split: the pitch first, then its result |
| [test_actor_matrices.py](../../tests/unit/test_actor_matrices.py), [test_actor_powers.py](../../tests/unit/test_actor_powers.py) | The actor score matrices and their powers |
| [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py) | Fatigue, the batting side and the drawn-pitch weights, and the pool columns they read |
| [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py), [test_transition_draw.py](../../tests/unit/test_transition_draw.py), [test_realism_consumers.py](../../tests/unit/test_realism_consumers.py) | The fielding draw's filters and weights; the transition draw and the five advancement draws; the platoon, park and fielder inputs |
| [test_fence_stage.py](../../tests/unit/test_fence_stage.py), [test_fence_carry_and_margin.py](../../tests/unit/test_fence_carry_and_margin.py), [test_fence_season_groups.py](../../tests/unit/test_fence_season_groups.py) | The park geometry, the carry model, the fence stage and its amendments (SIM-478) |
| [test_steal_draw.py](../../tests/unit/test_steal_draw.py), [test_steal_weights_seam.py](../../tests/unit/test_steal_weights_seam.py) | The steal opportunity draw and its weights as a seam |
| [test_manager_draw.py](../../tests/unit/test_manager_draw.py), [test_relief_draw.py](../../tests/unit/test_relief_draw.py) | The pitching change as a draw; the reliever draw over the real pen (SIM-427) |
| [test_receiving_ratio.py](../../tests/unit/test_receiving_ratio.py) | The catcher receiving ratio on taken pitches (built, off) |
| [test_situation_sigma_env.py](../../tests/unit/test_situation_sigma_env.py), [test_sampler_hot_path_cache.py](../../tests/unit/test_sampler_hot_path_cache.py) | The situation bandwidth's variable; the per-plate-appearance cache |
| [test_pool_cutoff.py](../../tests/unit/test_pool_cutoff.py) | The pools offer no play from after the simulated game's date |

### The runner and its outputs

| File | What it pins |
|---|---|
| [test_batch_runner.py](../../tests/unit/test_batch_runner.py) (slow), [test_batch_runner_concurrency.py](../../tests/unit/test_batch_runner_concurrency.py) (slow), [test_batch_runner_hit_rate.py](../../tests/unit/test_batch_runner_hit_rate.py) | The batch runner, its concurrency, the `_` prefix on factory-only kwargs |
| [test_persistent_process_pool.py](../../tests/unit/test_persistent_process_pool.py) (slow), [test_shared_memory_attach.py](../../tests/unit/test_shared_memory_attach.py) (slow), [test_engine_artifacts_shared_memory.py](../../tests/unit/test_engine_artifacts_shared_memory.py) | The warm worker pool and the shared-memory attach of the bundle arrays |
| [test_worker_forkserver.py](../../tests/unit/test_worker_forkserver.py), [test_worker_prewarm.py](../../tests/unit/test_worker_prewarm.py) (slow) | The forkserver start method; the worker prewarm contracts |
| [test_bundle_worker_and_boot.py](../../tests/unit/test_bundle_worker_and_boot.py) | Workers read only the bundle; the app restarts on a new version and waits for a DuckDB writer at boot |
| [test_cache_codec.py](../../tests/unit/test_cache_codec.py) | The Redis cache stores typed JSON, never a pickle |
| [test_sim_kwargs.py](../../tests/unit/test_sim_kwargs.py), [test_api_realism_wiring.py](../../tests/unit/test_api_realism_wiring.py), [test_park_factor_snapshot.py](../../tests/unit/test_park_factor_snapshot.py) | The one `simulate_game` kwargs builder; the defense and park inputs; the app holds no DuckDB handle |
| [test_lineup_resolver.py](../../tests/unit/test_lineup_resolver.py), [test_starter_resolution.py](../../tests/unit/test_starter_resolution.py), [test_fielder_map.py](../../tests/unit/test_fielder_map.py) | The lineup resolver; the starter from the official box (SIM-558); the per-position fielder map |
| [test_game_sim_summary.py](../../tests/unit/test_game_sim_summary.py), [test_win_probability.py](../../tests/unit/test_win_probability.py) | The multi-game summary; the calibrated win probability |
| [test_prop_distributions.py](../../tests/unit/test_prop_distributions.py), [test_prop_wrappers.py](../../tests/unit/test_prop_wrappers.py) | The fifteen prop distributions |
| [test_segment_markets.py](../../tests/unit/test_segment_markets.py), [test_inning_grid.py](../../tests/unit/test_inning_grid.py), [test_linescore.py](../../tests/unit/test_linescore.py) | Every game market from the inning grid; the grid; the linescore |
| [test_pitcher_decisions.py](../../tests/unit/test_pitcher_decisions.py) | The win, loss and save |
| [test_play_recorder.py](../../tests/unit/test_play_recorder.py), [test_play_snapshots.py](../../tests/unit/test_play_snapshots.py) | One game's ordered play stream; the field and play-by-play snapshots |
| [test_replay_chi_squared.py](../../tests/unit/test_replay_chi_squared.py) (slow) | The historical-replay harness and its chi-squared fit |
| [test_flag_registry.py](../../tests/unit/test_flag_registry.py) | The `SIM_*` flag registry is whole |

### The similarity engines and calibration (`similarity/`)

| File | What it pins |
|---|---|
| [test_pitcher_similarity.py](../../tests/unit/test_pitcher_similarity.py), [test_pitcher_similarity_contract.py](../../tests/unit/test_pitcher_similarity_contract.py), [test_pitcher_engine_calibration.py](../../tests/unit/test_pitcher_engine_calibration.py) | The pitcher engine, its result shape, its arsenal calibration |
| [test_arsenal_cache_vectorized.py](../../tests/unit/test_arsenal_cache_vectorized.py), [test_arsenal_w2_standardization.py](../../tests/unit/test_arsenal_w2_standardization.py) | The arsenal distance cache; the W₂ (Wasserstein-2) distance on one scale |
| [test_arm_angle_decision.py](../../tests/unit/test_arm_angle_decision.py) | The arm angle and spin shape are not pitcher features (SIM-533) |
| [test_batter_similarity.py](../../tests/unit/test_batter_similarity.py), [test_batter_physical_features.py](../../tests/unit/test_batter_physical_features.py) | The batter engine; its physical group and the arm measurements |
| [test_fielder_similarity.py](../../tests/unit/test_fielder_similarity.py), [test_fielder_oaa_jump_model.py](../../tests/unit/test_fielder_oaa_jump_model.py), [test_outfield_arm_model.py](../../tests/unit/test_outfield_arm_model.py) | The fielder engine; outs above average and the outfield jump; the outfield arm group |
| [test_baserunner_extra_base_similarity.py](../../tests/unit/test_baserunner_extra_base_similarity.py), [test_baserunner_steal_engine.py](../../tests/unit/test_baserunner_steal_engine.py), [test_baserunner_steal_scores.py](../../tests/unit/test_baserunner_steal_scores.py), [test_weighted_rbf_kernel.py](../../tests/unit/test_weighted_rbf_kernel.py) | The two runner engines and the shared weighted RBF kernel (a similarity that falls off as a bell curve with distance) |
| [test_catcher_engine_throwing.py](../../tests/unit/test_catcher_engine_throwing.py), [test_catcher_similarity_scores.py](../../tests/unit/test_catcher_similarity_scores.py) | The catcher engine |
| [test_pitcher_steal_similarity.py](../../tests/unit/test_pitcher_steal_similarity.py) | The pitcher-against-the-run engine |
| [test_manager_similarity.py](../../tests/unit/test_manager_similarity.py), [test_manager_usage_score.py](../../tests/unit/test_manager_usage_score.py) | The manager engine and its usage columns |
| [test_situation_similarity.py](../../tests/unit/test_situation_similarity.py), [test_situation_engine_queries.py](../../tests/unit/test_situation_engine_queries.py), [test_situation_metadata_store.py](../../tests/unit/test_situation_metadata_store.py) | The situation engine's KD-tree and its columnar metadata |
| [test_pitch_pitch_engine.py](../../tests/unit/test_pitch_pitch_engine.py), [test_batted_ball_engine.py](../../tests/unit/test_batted_ball_engine.py) | The two FAISS nearest-neighbour engines |
| [test_engine_build_smoke.py](../../tests/unit/test_engine_build_smoke.py), [test_engine_calibration_regression.py](../../tests/unit/test_engine_calibration_regression.py) | Each engine's `build()`; the RBF sigma fit and the FAISS lookup |
| [test_calibration_fit_and_apply.py](../../tests/unit/test_calibration_fit_and_apply.py), [test_calibration_report_json.py](../../tests/unit/test_calibration_report_json.py), [test_calibration_reconciliation.py](../../tests/unit/test_calibration_reconciliation.py) | The calibration report reaches every engine; its JSON form and the win-probability map; the fit runs on the live schema |
| [test_probabilistic_backtester.py](../../tests/unit/test_probabilistic_backtester.py), [test_recency_walk_forward.py](../../tests/unit/test_recency_walk_forward.py) | The backtester's calibration metrics; the recency walk-forward |

### The profiles and pools (`pipeline/batch/`)

| File | What it pins |
|---|---|
| [test_player_profile_computor.py](../../tests/unit/test_player_profile_computor.py), [test_league_average_profiles.py](../../tests/unit/test_league_average_profiles.py) | The profile computor's helpers; the league-average rows |
| [test_one_cutoff_per_table.py](../../tests/unit/test_one_cutoff_per_table.py), [test_delete_seasons_coverage.py](../../tests/unit/test_delete_seasons_coverage.py), [test_profile_index_recreate.py](../../tests/unit/test_profile_index_recreate.py) | One `asof_date` per profile table (SIM-551); a full rebuild deletes every season-keyed table; the index recreate |
| [test_point_in_time_batter.py](../../tests/unit/test_point_in_time_batter.py), [test_point_in_time_pitcher_manager.py](../../tests/unit/test_point_in_time_pitcher_manager.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/test_point_in_time_runner_catcher_fielder.py) | No profile reads data dated after its cutoff |
| [test_gmm_batch_pipeline.py](../../tests/unit/test_gmm_batch_pipeline.py), [test_arsenal_fit_reproducible.py](../../tests/unit/test_arsenal_fit_reproducible.py), [test_barrel_definition.py](../../tests/unit/test_barrel_definition.py) | The pitch-mix model (GMM) batch; the same pitches in any stored order give the same arsenal (SIM-580); the barrel definition |
| [test_profile_code_sets.py](../../tests/unit/test_profile_code_sets.py), [test_out_label.py](../../tests/unit/test_out_label.py), [test_steal_labels.py](../../tests/unit/test_steal_labels.py) | The pitch-code sets, the out label and the steal labels agree across the profile SQL and the pool build |
| [test_foul_tip_strike_three.py](../../tests/unit/test_foul_tip_strike_three.py), [test_hit_by_pitch.py](../../tests/unit/test_hit_by_pitch.py) | A two-strike foul tip is strike three (SIM-553); a hit by pitch is its own outcome |
| [test_fielder_oaa_jump_profile.py](../../tests/unit/test_fielder_oaa_jump_profile.py), [test_fielder_placeholder_schema.py](../../tests/unit/test_fielder_placeholder_schema.py), [test_outfield_arm_block.py](../../tests/unit/test_outfield_arm_block.py) | The fielder profile's Savant columns, its temp-table placeholders, the outfield arm block |
| [test_lead_distance.py](../../tests/unit/test_lead_distance.py) | Savant's lead and hold measures in the steal and baserunning models (SIM-531) |
| [test_manager_defensive_subs.py](../../tests/unit/test_manager_defensive_subs.py), [test_uncaught_strike_three.py](../../tests/unit/test_uncaught_strike_three.py) | Late defensive subs count plate appearances; the catcher's uncaught third strikes |
| [test_park_factors.py](../../tests/unit/test_park_factors.py), [test_park_geometry_builder.py](../../tests/unit/test_park_geometry_builder.py) | Park factors and their splits; the version-2 park geometry |
| [test_pool_pull_relative_spray.py](../../tests/unit/test_pool_pull_relative_spray.py), [test_pool_recency_and_incremental_rebuild.py](../../tests/unit/test_pool_recency_and_incremental_rebuild.py) | The handedness-corrected spray angle; recency weight and the incremental rebuild |
| [test_pool_chain.py](../../tests/unit/test_pool_chain.py) | The count chain of the pitch pool and the label check |
| [test_ibb_rates.py](../../tests/unit/test_ibb_rates.py) | The measured intentional-walk rate table |
| [test_transition_pool.py](../../tests/unit/test_transition_pool.py), [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_profile_data_additions.py](../../tests/unit/test_profile_data_additions.py) | The transition columns and the advancement pool; the steal pool's pitch class and pickoff rows; the fielding chain and sprint speed |

### The bundle (`pipeline/batch/engine_artifacts.py`, `pipeline/batch/artifacts/`)

| File | What it pins |
|---|---|
| [test_engine_artifacts_realism.py](../../tests/unit/test_engine_artifacts_realism.py) | The batted-ball artifact's realism columns |
| [test_pitcher_sim_matrix.py](../../tests/unit/test_pitcher_sim_matrix.py) | The dense pitcher similarity matrix |
| [test_bundle_no_pickle.py](../../tests/unit/test_bundle_no_pickle.py) | The bundle loads with `allow_pickle=False` (SIM-565) |
| [test_bundle_versions.py](../../tests/unit/test_bundle_versions.py) | A build publishes a new version behind a link and moves the link in one step (SIM-563) |

### Data ingestion (`pipeline/etl/`, `pipeline/statcast_events.py`)

| File | What it pins |
|---|---|
| [test_etl_historical_loader.py](../../tests/unit/test_etl_historical_loader.py), [test_etl_connection_pool.py](../../tests/unit/test_etl_connection_pool.py), [test_etl_ensure_rows.py](../../tests/unit/test_etl_ensure_rows.py) | The historical loader's row builders, its connection pool, its prerequisite writers |
| [test_etl_error_ledger.py](../../tests/unit/test_etl_error_ledger.py), [test_etl_hardening.py](../../tests/unit/test_etl_hardening.py), [test_etl_reload_game.py](../../tests/unit/test_etl_reload_game.py) | The error ledger; per-game isolation and the circuit breaker; the corrective reload |
| [test_etl_schedule_final_filter.py](../../tests/unit/test_etl_schedule_final_filter.py), [test_etl_starting_lineups.py](../../tests/unit/test_etl_starting_lineups.py), [test_starting_position.py](../../tests/unit/test_starting_position.py) | Only final games load; the starting lineup rows; each starter's starting position (SIM-559) |
| [test_release_speed_bounds.py](../../tests/unit/test_release_speed_bounds.py), [test_play_events.py](../../tests/unit/test_play_events.py) | The 50–110 mph bounds; the non-pitch play events |
| [test_venue_resolution.py](../../tests/unit/test_venue_resolution.py), [test_venue_id_fallback.py](../../tests/unit/test_venue_id_fallback.py) | Venue resolution and the NULL venue |
| [test_boxscore_ingest.py](../../tests/unit/test_boxscore_ingest.py), [test_boxscore_reader.py](../../tests/unit/test_boxscore_reader.py), [test_game_bullpen_listing.py](../../tests/unit/test_game_bullpen_listing.py) | The official box score: parser, reader, the per-game bullpen listing |
| [test_bullpen_usage.py](../../tests/unit/test_bullpen_usage.py), [test_bullpen_availability.py](../../tests/unit/test_bullpen_availability.py) | The pen from the box; roster and injured-list availability |
| [test_savant_loader.py](../../tests/unit/test_savant_loader.py), [test_savant_oaa_jump_boards.py](../../tests/unit/test_savant_oaa_jump_boards.py) | The Savant loader's season guards; the outs-above-average and jump boards |
| [test_db_dsn.py](../../tests/unit/test_db_dsn.py) | The connection string has no default password |

### The live service and the slate (`pipeline/live/`, `pipeline/mlb_schedule.py`, `pipeline/mlb_game_feed.py`)

| File | What it pins |
|---|---|
| [test_live_pipeline.py](../../tests/unit/test_live_pipeline.py), [test_live_pipeline_bugs.py](../../tests/unit/test_live_pipeline_bugs.py), [test_live_pipeline_setup.py](../../tests/unit/test_live_pipeline_setup.py) | The live ingestion pipeline |
| [test_live_bridge.py](../../tests/unit/test_live_bridge.py), [test_live_connection_manager.py](../../tests/unit/test_live_connection_manager.py), [test_live_game_state_builder.py](../../tests/unit/test_live_game_state_builder.py), [test_live_resimulate_cooldown.py](../../tests/unit/test_live_resimulate_cooldown.py) | The Redis bridge and heartbeat, the WebSocket fan-out, the game-state builder, the resimulate cooldown |
| [test_published_lineup_writer.py](../../tests/unit/test_published_lineup_writer.py) | The published-lineup writer |
| [test_schedule_client.py](../../tests/unit/test_schedule_client.py), [test_game_feed.py](../../tests/unit/test_game_feed.py) | The schedule client; the game feed parser and route |

### Odds and betting (`pipeline/odds_provider.py`, `pipeline/bettingpros/`, `betting/`)

| File | What it pins |
|---|---|
| [test_odds_provider_seam.py](../../tests/unit/test_odds_provider_seam.py), [test_odds_provider_pin.py](../../tests/unit/test_odds_provider_pin.py), [test_odds_provider_ingestion.py](../../tests/unit/test_odds_provider_ingestion.py), [test_odds_provider_retry.py](../../tests/unit/test_odds_provider_retry.py) | The provider seam, the test lane's pin, the prop ingestion, the retry |
| [test_bettingpros_provider.py](../../tests/unit/test_bettingpros_provider.py), [test_historical_odds_load.py](../../tests/unit/test_historical_odds_load.py), [test_odds_loader_retry.py](../../tests/unit/test_odds_loader_retry.py) | The BettingPros provider, the historical loader, its retry option |
| [test_odds_vocabulary.py](../../tests/unit/test_odds_vocabulary.py), [test_odds_game_markets.py](../../tests/unit/test_odds_game_markets.py), [test_odds_book_vocabulary.py](../../tests/unit/test_odds_book_vocabulary.py) | The fifteen prop markets, the fifteen game markets, the book labels |
| [test_odds_one_book_per_row.py](../../tests/unit/test_odds_one_book_per_row.py), [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_odds_readers.py](../../tests/unit/test_odds_readers.py), [test_odds_matcher.py](../../tests/unit/test_odds_matcher.py) | One book per odds row (SIM-555): the guard, the writers, the readers, the played-schedule matcher |
| [test_odds_hash_dedup.py](../../tests/unit/test_odds_hash_dedup.py), [test_odds_last_seen.py](../../tests/unit/test_odds_last_seen.py), [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_backfill_odds_hash.py](../../tests/unit/test_backfill_odds_hash.py) | Quote dedup, the last-seen stamp, the live closing rows, the hash backfill |
| [test_clv_engine.py](../../tests/unit/test_clv_engine.py), [test_clv_engine_run_line.py](../../tests/unit/test_clv_engine_run_line.py), [test_bet_signal.py](../../tests/unit/test_bet_signal.py), [test_line_movement.py](../../tests/unit/test_line_movement.py) | Prices, no-vig, edge and closing line value; the run line; the bet gate and stake; line movement |

### The API (`api/`) and the replay store (`db/sim_store.py`)

| File | What it pins |
|---|---|
| [test_api_main_wiring.py](../../tests/unit/test_api_main_wiring.py), [test_api_state.py](../../tests/unit/test_api_state.py), [test_api_error_envelope.py](../../tests/unit/test_api_error_envelope.py), [test_api_schemas.py](../../tests/unit/test_api_schemas.py) | Routers, lifespan, CORS, rate limit; the boot builders; the error envelope; the response models |
| [test_api_auth.py](../../tests/unit/test_api_auth.py), [test_api_auth_session.py](../../tests/unit/test_api_auth_session.py), [test_route_auth_coverage.py](../../tests/unit/test_route_auth_coverage.py), [test_trusted_proxy.py](../../tests/unit/test_trusted_proxy.py) | CORS and latency middleware; the session login; login on every route; the client address behind nginx |
| [test_openapi_snapshot.py](../../tests/unit/test_openapi_snapshot.py) | The frontend's API snapshot is current (SIM-573) |
| [test_api_games.py](../../tests/unit/test_api_games.py), [test_slate_endpoint.py](../../tests/unit/test_slate_endpoint.py), [test_incomplete_lineup_api.py](../../tests/unit/test_incomplete_lineup_api.py) | The game routes; the schedule-driven slate; 503 for an unplayable lineup |
| [test_api_games_plays.py](../../tests/unit/test_api_games_plays.py), [test_api_plays_pagination.py](../../tests/unit/test_api_plays_pagination.py), [test_api_game_card.py](../../tests/unit/test_api_game_card.py), [test_replay_store.py](../../tests/unit/test_replay_store.py), [test_sim_store.py](../../tests/unit/test_sim_store.py) | The replay routes, their pagination, the game card, the replay file, the sim store and its migrations |
| [test_projections_on_pool.py](../../tests/unit/test_projections_on_pool.py), [test_sim_jobs.py](../../tests/unit/test_sim_jobs.py), [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py) | Projections on the worker pool; the run jobs; the what-if from a real plate appearance |
| [test_api_betting.py](../../tests/unit/test_api_betting.py), [test_segment_edges.py](../../tests/unit/test_segment_edges.py), [test_live_run_lines.py](../../tests/unit/test_live_run_lines.py), [test_odds_live_pages.py](../../tests/unit/test_odds_live_pages.py), [test_slate_card_odds.py](../../tests/unit/test_slate_card_odds.py), [test_game_page_grading.py](../../tests/unit/test_game_page_grading.py) | The betting routes: edges and signals for every market, run lines by shape, one book per row, the card odds, grading on the real final |
| [test_api_data_health.py](../../tests/unit/test_api_data_health.py), [test_read_only_routes.py](../../tests/unit/test_read_only_routes.py), [test_sql_console_pool.py](../../tests/unit/test_sql_console_pool.py), [test_sql_safety.py](../../tests/unit/test_sql_safety.py) | The Data Lab: freshness, the read-only pool, the SQL console guard |
| [test_game_type_filter.py](../../tests/unit/test_game_type_filter.py), [test_pitch_result_vocabulary.py](../../tests/unit/test_pitch_result_vocabulary.py) | The Data Lab's regular-season default (SIM-572) and its pitch-result codes (SIM-571) |
| [test_similarity_endpoint.py](../../tests/unit/test_similarity_endpoint.py), [test_similarity_explorer_adapters.py](../../tests/unit/test_similarity_explorer_adapters.py) | The pitcher similarity explorer and the engine adapters |
| [test_metrics_endpoint.py](../../tests/unit/test_metrics_endpoint.py), [test_backup_age_gauge.py](../../tests/unit/test_backup_age_gauge.py) | `/metrics` and the backup-age gauges |

### The database schema (`db/`)

| File | What it pins |
|---|---|
| [test_migration_chain.py](../../tests/unit/test_migration_chain.py), [test_schema_pitch_indexes.py](../../tests/unit/test_schema_pitch_indexes.py) | The Alembic chain; the `raw.pitches` indexes |
| [test_pool_index_prune.py](../../tests/unit/test_pool_index_prune.py), [test_pool_index_reconcile.py](../../tests/unit/test_pool_index_reconcile.py) | DuckDB migrations 0005 and 0006 on the pool indexes |
| [test_db_introspect.py](../../tests/unit/test_db_introspect.py) | The table and column existence probe |
| [test_docs_alembic_head.py](../../tests/unit/test_docs_alembic_head.py), [test_docs_duckdb_version.py](../../tests/unit/test_docs_duckdb_version.py), [test_docs_file_index.py](../../tests/unit/test_docs_file_index.py) | The documents cite the current schema versions; the file index is current |

### Operations, backups and deployment

| File | What it pins |
|---|---|
| [test_backup_data_stores.py](../../tests/unit/test_backup_data_stores.py), [test_backup_status.py](../../tests/unit/test_backup_status.py), [test_backup_offsite.py](../../tests/unit/test_backup_offsite.py) | The DuckDB and bundle backups, the set reader, the off-site copy |
| [test_backup_drill.py](../../tests/unit/test_backup_drill.py), [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py), [test_pg_backup_script.py](../../tests/unit/test_pg_backup_script.py) | The restore drill; the scheduler wiring; the Postgres dump and restore scripts |
| [test_ingest_wait.py](../../tests/unit/test_ingest_wait.py), [test_retry_wrapper.py](../../tests/unit/test_retry_wrapper.py), [test_shell_dsn_guards.py](../../tests/unit/test_shell_dsn_guards.py) | The rebuild waits for the finals; the crash-retry wrapper; the shell scripts need `BASEBALL_DB_DSN` |
| [test_no_default_password.py](../../tests/unit/test_no_default_password.py), [test_secrets_baseline.py](../../tests/unit/test_secrets_baseline.py), [test_gen_secrets.py](../../tests/unit/test_gen_secrets.py) | No default password; the secrets baseline; the secrets generator |
| [test_nginx_resolve.py](../../tests/unit/test_nginx_resolve.py), [test_check_file_integrity.py](../../tests/unit/test_check_file_integrity.py), [test_sweep_streaming.py](../../tests/unit/test_sweep_streaming.py) | nginx re-resolves the app; the file-integrity guard; the sweep wrapper streams its child's output |

**Watch out:** the five shell-script tests (`test_ingest_wait.py`, `test_retry_wrapper.py`,
`test_shell_dsn_guards.py`, `test_pg_backup_script.py`, part of
`test_backup_schedule.py`) skip on Windows or without a POSIX `sh`. They run in the
container and in CI.

### Scripts and measurement tools (`scripts/`)

| File | What it pins |
|---|---|
| [test_accuracy_comparison.py](../../tests/unit/test_accuracy_comparison.py), [test_accuracy_comparison_markets.py](../../tests/unit/test_accuracy_comparison_markets.py), [test_accuracy_comparison_props.py](../../tests/unit/test_accuracy_comparison_props.py) | The sim-vs-closing-line accuracy comparison, its market map, its prop grading on the box score |
| [test_accuracy_hypothetical_return.py](../../tests/unit/test_accuracy_hypothetical_return.py), [test_accuracy_minimum_sample.py](../../tests/unit/test_accuracy_minimum_sample.py), [test_accuracy_fit_instruments.py](../../tests/unit/test_accuracy_fit_instruments.py) | The opt-in dollar return; the minimum sample sizes; the joint-fit instruments |
| [test_pair_accuracy.py](../../tests/unit/test_pair_accuracy.py), [test_designed_experiment.py](../../tests/unit/test_designed_experiment.py), [test_market_calibration.py](../../tests/unit/test_market_calibration.py) | Pairing two reports; the designed experiment; the market calibration fit |
| [test_validate_props.py](../../tests/unit/test_validate_props.py) | Prop validation and the win-probability reliability curve |
| [test_book_sharpness.py](../../tests/unit/test_book_sharpness.py), [test_odds_book_census.py](../../tests/unit/test_odds_book_census.py), [test_odds_census_sql.py](../../tests/unit/test_odds_census_sql.py), [test_rescore_graded_book.py](../../tests/unit/test_rescore_graded_book.py), [test_rescore_runlines.py](../../tests/unit/test_rescore_runlines.py), [test_retire_consensus_odds.py](../../tests/unit/test_retire_consensus_odds.py) | The odds re-load scripts (SIM-555) and the run-line re-score |
| [test_fence_check.py](../../tests/unit/test_fence_check.py), [test_fence_counters.py](../../tests/unit/test_fence_counters.py) | The fence certification metrics; the harness's fence counters |
| [test_filter_cells.py](../../tests/unit/test_filter_cells.py) (slow), [test_fatigue_kernel_scan.py](../../tests/unit/test_fatigue_kernel_scan.py) | The filter-cell measurement; the fatigue-kernel scan |
| [test_running_game_census.py](../../tests/unit/test_running_game_census.py), [test_running_game_replay.py](../../tests/unit/test_running_game_replay.py), [test_steal_weight_volume_probe.py](../../tests/unit/test_steal_weight_volume_probe.py), [test_steal_pool_rebuild.py](../../tests/unit/test_steal_pool_rebuild.py) | The running-game census and replay; the steal-weight probe; the steal-pool rebuild |
| [test_fielder_profile_recompute.py](../../tests/unit/test_fielder_profile_recompute.py), [test_outfield_arm_recompute.py](../../tests/unit/test_outfield_arm_recompute.py) | The two fielder recompute scripts |
| [test_smoke_outs_gate.py](../../tests/unit/test_smoke_outs_gate.py) | `scripts/sim_stats.py` exits 1 when the pitchers' outs differ from the outs played |
| [test_index_acceptance_harness.py](../../tests/unit/test_index_acceptance_harness.py), [test_script_flag_names.py](../../tests/unit/test_script_flag_names.py) | The index-acceptance harness; the scripts' flag names |

## CI workflows

### `.github/workflows/ci.yml`

[Source](../../.github/workflows/ci.yml) · 750 lines

Runs on every push to any branch, and on a pull request to `main` or `release/**`.
Python jobs use 3.13 and `pip install -r requirements-dev.txt`; the frontend jobs use
Node 20 and `npm ci`. A newer run on the same ref cancels an older one.

| Job | What it runs | Needs |
|---|---|---|
| `lint` | `ruff check .` and `ruff format --check .` with ruff 0.16.7 | — |
| `type-check` | `mypy similarity/ pipeline/ api/ simulation/ betting/` | — |
| `unit-tests` | the fast unit lane at `--timeout=30` with `--tb=native`, then the 80% coverage gate | — |
| `slow-tests` | `tests/unit/ -m slow` at `--timeout=120` | — |
| `regression` | `tests/regression/` at `--timeout=60` | `unit-tests` |
| `e2e` | `tests/integration/test_api_end_to_end.py` at `--timeout=120` | `unit-tests` |
| `acceptance-arithmetic` | `tests/acceptance/test_band_arithmetic.py`, then a roster check | — (on purpose) |
| `secrets-check` | no committed `.env`; no literal credential, retired password or DSN password | — |
| `file-integrity` | `python scripts/check_file_integrity.py` (NUL bytes, unparseable source) | — |
| `frontend` | `npm run gen:api` + `git diff --exit-code`, `type-check`, `lint`, `build` | — |
| `frontend-e2e` | `npm run e2e:install`, `npm run e2e` | `frontend` |
| `docker-build-check` | builds the `runtime` image target, no push | `lint`, `unit-tests`, `secrets-check` |

The roster check reads the JUnit report and fails unless each of 27 named tests ran.
pytest exits 0 when every test skips, so the exit code alone proves nothing. Adding a
test needs no roster edit; renaming or deleting a named one does.

**Watch out:** the default branch is `master`, so the `pull_request` trigger (on
`main`) never fires; every push still runs the workflow. The `--tb=native` flag works
around a pytest `--tb=short` renderer crash that can hide a real failure.

### `.github/workflows/acceptance-nightly.yml`

[Source](../../.github/workflows/acceptance-nightly.yml) · 343 lines · manual dispatch only

Runs the heavy acceptance lane on the runner named by the repository variable
`ACCEPTANCE_RUNNER`, else `ubuntu-latest`. A precondition step fails (never skips)
when the DuckDB file, the bundle manifests or Postgres is missing, so on a hosted
runner the job goes red within a minute. It has no schedule: a schedule against a
label no runner carries queues silently and is cancelled after a day. The file's
header lists the five conditions to meet before a schedule returns.

**Watch out:** the workflow is behind the lane. Its `games` input says "max 12" and
defaults to 12 (the lane's default is the 45-game set), and its roster names three
tests that `test_production_bands.py` no longer holds
(`test_full_pool_out_advancement_runs_sim450`,
`test_full_pool_steal_decision_runs_sim450`, `test_reach_on_error_band_sim450`), so a
dispatch fails its own roster check. Run the lane from the command line until the
workflow is updated.

### The weekly and release workflows

| Workflow | Trigger | What it runs |
|---|---|---|
| [integration-weekly.yml](../../.github/workflows/integration-weekly.yml) | Mondays 03:00 UTC, or by hand | `pytest tests/integration/ -m integration --timeout=120` on a hosted runner with Docker |
| [perf-weekly.yml](../../.github/workflows/perf-weekly.yml) | Mondays 04:00 UTC, or by hand | `pytest tests/performance --benchmark-only` with `PERF_STRICT=1` and `PERF_STRICT_SANDBOX=1`, then a 1.5 GB peak-memory check |
| [docker-release.yml](../../.github/workflows/docker-release.yml) | a push to `main`, or a published release | builds and pushes the API image to the GitHub container registry |
| [frontend-release.yml](../../.github/workflows/frontend-release.yml) | a push to `main`, or a published release | builds and pushes the frontend image |

**Watch out:** the two release workflows trigger on `main`, so a push to `master`
never runs them; only a published release does.
