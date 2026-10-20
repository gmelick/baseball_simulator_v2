# The project-wide code clean-up: plan, rules and session briefs (SIM-493), 2026-10-19

## 1. What this is

The code-hygiene refactor (SIM-493) asks for four things: split the oversized files, name
the scripts by purpose, move run outputs out of `scripts/`, and add shared helpers. On
2026-10-19 the owner widened it: review and clean up **all** code, with comments, refactors
and any other change that makes the code clearer. The file clean-up of the same day
(`CHANGES.md` 2026-10-19) was the first part. This document is the plan for the rest, the
rules every change follows, and the brief of each session that does the work.

The work runs in three phases:

- **Phase A, the groundwork** (branch `sim493-groundwork`, done in one session): the
  fingerprint tool, the pins removed, the run outputs moved, the scripts and the tests
  renamed, the shared helpers. §3 records what it did.
- **Phase B, five sessions in parallel**, each in its own worktree off `sim493-groundwork`:
  simulation (B1), pipeline (B2), API and frontend (B3), similarity and betting (B4),
  scripts and the test harness (B5). §7 to §11 are their briefs.
- **Phase C, the integration** (branch `sim493-integration`): merge the five branches,
  remove the transitional re-exports, run every gate and the 45 × 130 lane, deploy with the
  owner's OK. §12.

The owner's decisions in planning (2026-10-19):

1. Groundwork first, then one session per area, then an integration pass.
2. Test files are named by subject, never by ticket.
3. A bug the review finds becomes a new `BACKLOG.xlsx` ticket. The refactor itself changes
   no behaviour.

**Timing.** The joint weight fit (SIM-548) is paused and runs no arm. The fingerprint (§4)
proves the simulator's output is byte-identical, so the fit's finished design runs stay
valid in substance.

## 2. The rules every change follows

1. **Behaviour-neutral, proved.** The same seeded games give byte-identical results: every
   pitch, the box score and the linescore. The RNG call order does not change. Each area
   adds its own neutral check (its brief names it). A change the fingerprint cannot cover
   (a log line, an error text) is listed in the session's `CHANGES.md` entry.
2. **A bug found is ticketed, not fixed.** Add a `BACKLOG.xlsx` row: the evidence, the
   definition of done and a proposed fix. Re-read the subtitle row right before you write,
   then bump the next free ID; another session may have filed one meanwhile. Check for
   `~$BACKLOG.xlsx` first (the lock file of an open workbook; the git-stash trap in the
   memory index).
3. **The comment standard (CLAUDE.md §14).**
   - A comment or docstring says what the code does now and why, in the present tense.
   - History goes: deleted code, the old behaviour, "was X", dated narrative, sprint and
     part labels. Git and `CHANGES.md` keep it.
   - A ticket tag stays only where it points at a ruling or a measurement that justifies a
     value. Write the work first: "(the fence certification, SIM-478)", never "SIM-478"
     alone. Drop a tag the git history already records.
   - Settle the two meanings of `SIM-421` (CLAUDE.md §7): the prop-market work keeps the
     tag; the 2026-05 realism batch says what it was in words.
   - Runtime strings (log text, error text, report fields, test messages) stay unless they
     are wrong. Do not edit `db/migrations/`.
4. **One home per name.** A split moves a name; callers in the same area import it from its
   new home. A caller OUTSIDE the area keeps working through an explicit re-export in the
   old module (never `import *`), marked `# re-export: removed in the integration pass`.
   Phase C re-points those callers and deletes the marked lines. These stay exactly as
   they are:
   - the dotted factory strings `simulation.batch_runner:rng_driven_machine_factory` and
     `simulation.production_factory:production_machine_factory` (they are part of the
     result and prop-set cache keys);
   - literal logger names (`"engine_artifacts"`, `"live_ingestion"`,
     `"player_profile_computor"`, `"api.routes.betting"`, ...; tests catch them by name);
   - the `python -m` entry points and their `__main__` blocks;
   - a `logging.basicConfig` call at import time;
   - the singleton objects other modules hold (`connection_manager`, `odds_router`): a
     re-export hands out the same object, never a new one.
5. **A big class splits into mixins, not free functions.** Tests and scripts replace methods
   on instances, subclass `StateMachine` (13 times) and `FullPoolSampler` (8 times), and
   read method source. A mixin keeps every method on the instance; the class keeps its name
   and its module. Every draw keeps sharing `self.rng` in today's order. Shared mutable
   module state stays with its readers (`ARSENAL_SCALE` in `pitcher_similarity.py`).
6. **Tests that pin code move with the code, in the same commit.**
   - A patch target is re-pointed to the module whose globals the moved code reads. A patch
     on an imported module's attribute (`time.sleep`, `duckdb.connect`,
     `psycopg2.extras.execute_batch`) survives a move when the moved code calls through the
     attribute (`psycopg2.extras.execute_batch(...)`, not a bare imported name).
   - A source-text pin (a file read by path, `inspect.getsource` of a module or class, a
     `def`-marker slice, a call count) is re-pointed to the new file, or rewritten to test
     the behaviour. `inspect.getsource(Class)` returns the class body only, without its
     mixins; `getsource(Class.method)` follows the method.
   - A pin is never weakened without a line in the session's `CHANGES.md` entry.
   - A session may edit another area's test only for a re-point its own move forces, in a
     small separate commit.
7. **Dead code goes.** Delete code with no caller in the program, the scripts or the config:
   grep the dotted name, `python -m`, the Makefile, compose and ofelia. Delete the tests that
   test only the deleted code. List each deletion in the `CHANGES.md` entry. Known
   candidates: `api/websocket/schemas.py`, `similarity/registry.py`, and the sampler's
   `runner_rate`, `fielder_quality`, `_steal_feat_cols` and `_FIELDER_BB_FEATURES`.
8. **Each split carries its config edits:** the coverage omit list in `pyproject.toml` (a
   split of an omitted file, such as `api/routes/games.py` or `api/main.py`, omits its new
   modules too), the mypy overrides, the AST guard lists (the pickle guard in
   `test_cache_codec.py`), the session's `docs/technical/` file and its CLAUDE.md §5 bullet.
9. **The size bar.** No program, script or test file stays over 2,000 lines. Every file over
   1,500 lines is listed in the session's `CHANGES.md` entry with the reason it stays whole.
   Probes of closed tickets (`scripts/probes/`) get comments and names only.
10. **The shared machine.** At most two heavy containers run at once across all sessions;
    the live app holds 6.7 GB. Heavy runs: the full unit lane, a production fingerprint, a
    bundle build, a profile build. Run `docker ps` before you start one and wait when two
    app-image containers already run. The host blue-screens: commit and push often.

## 3. Phase A: what the groundwork did

| Slice | Commit | What |
|---|---|---|
| A1 | `9a2a0de` | `scripts/behaviour_fingerprint.py` (§4) and the baselines on `fe81d79` |
| A2 | `dded0f0` | Citations name a section or a constant, never a line: "CLAUDE.md §11", `scripts/sim_stats.py::_MLB_2025`. The two tests that required a ticket comment in source test the content instead. |
| A3 + A4 | `3262c0a` | Scripts named by purpose; one-off probes in `scripts/probes/`; inputs in `scripts/data/`; committed run records in `docs/audit/records/`; run outputs in `results/` (git- and Docker-ignored). One test-side script loader, `tests/script_loader.py`. Table in §13.1. |
| A5 | `24707d3` | 237 test files named by subject. Table in §13.2. |
| A6 | `b459d53` and the next commit | `simulation/flags.py`, the registry of the `SIM_*` flags (`b459d53`), and `db/introspect.py`, the existence probe. |
| A7 | (see `git log`) | The coverage omit list drops two deleted files; `make test-unit` measures the five CI packages. |

**The flag registry.** `simulation/flags.py` holds one row per `SIM_*` flag: name, type,
code default, owner and meaning. Read a flag only through `read_bool`, `read_float`,
`read_int`, `read_str` or `actor_power_overrides`. An on/off flag accepts `1 true yes on`
and `"" 0 false no off`; anything else raises `FlagValueError`. A number that does not
parse keeps the default and logs a warning. `tests/unit/test_flag_registry.py` fails when
code reads an unregistered `SIM_*` name or compose sets one. A new flag is a new row.

**The existence probe.** `db/introspect.py`: `duck_columns`, `duck_table_exists` and
`duck_relation_readable` for DuckDB; `pg_column_exists` and `pg_relation_exists` for a
psycopg2 cursor; `pg_column_exists_async` for asyncpg. The names go into the SQL as
literals (each checked to be a plain identifier), so a test fake that matches on
`information_schema` keeps working.

## 4. The fingerprint

`scripts/behaviour_fingerprint.py` runs seeded games, records every pitch, and writes one
SHA-256 hash per game. Compare two records with `compare`; it exits 1 on any difference.

| Mode | What it runs | Where |
|---|---|---|
| `nodb` | 200 games of the no-database factory | host or container |
| `synthetic` | 200 games of the production factory over the in-memory bundle, with the flags unset and with the app's compose flags (less `SIM_PITCH_CELL_INDEX`, which that bundle cannot serve) | host or container |
| `production` | The 45 balanced games × 4 seeds, the production factory, the compose flags, a pinned bundle | the app container |
| `bundle` | The arrays of a built bundle, by content | wherever the bundle is |

The baselines (master `fe81d79`, 2026-10-19) are in the MAIN checkout's
`C:/Users/grego/Documents/baseball_simulator_v2/results/sim493/`:
`fp_base_nodb.json` and `fp_base_synthetic.json` (host Python 3.13.7), and
`fp_base_production_cached.json` (the container). The production mode reads the pinned
bundle copy `/data/sim493/bundle_pin` (a copy of version `20261019T081435Z`) and the games'
cached inputs `results/sim493/production_kwargs.json`, so the nightly jobs cannot move it.
Two runs of the base gave identical records. Compare a record only with one from the same
environment (the record names its Python and numpy).

From a worktree (`$WT`, Windows paths), the production run mounts the main checkout's
`results/` so it finds the cache and the baselines:

```bash
MSYS_NO_PATHCONV=1 docker compose -f "C:/Users/grego/Documents/baseball_simulator_v2/docker-compose.yml" --project-directory "C:/Users/grego/Documents/baseball_simulator_v2" run --rm --no-deps -T -v "$WT:/app" -v "$WT/api:/app/api" -v "$WT/pipeline:/app/pipeline" -v "$WT/similarity:/app/similarity" -v "$WT/simulation:/app/simulation" -v "$WT/db:/app/db" -v "C:/Users/grego/Documents/baseball_simulator_v2/results:/app/results" app python scripts/behaviour_fingerprint.py production --bundle-dir /data/sim493/bundle_pin --kwargs-cache results/sim493/production_kwargs.json --out results/sim493/fp_<area>_production.json
```

Then `python scripts/behaviour_fingerprint.py compare results/sim493/fp_base_production_cached.json results/sim493/fp_<area>_production.json`.
The host modes run from the worktree with `--out` pointing into the main checkout's
`results/sim493/`.

## 5. The worktree test recipe (every session)

A worktree has no `.env`, so run the MAIN checkout's compose file and mount the worktree
over `/app` AND over each of the five code folders. Compose's own mounts of the main
checkout's code folders sit on top of a single `/app` mount, so without the five `-v`
lines the container runs master's code. With `MSYS_NO_PATHCONV=1` the host side of every
path is a Windows path (`C:/Users/grego/...`).

```bash
MSYS_NO_PATHCONV=1 docker compose -f "C:/Users/grego/Documents/baseball_simulator_v2/docker-compose.yml" --project-directory "C:/Users/grego/Documents/baseball_simulator_v2" run --rm --no-deps -T -v "$WT:/app" -v "$WT/api:/app/api" -v "$WT/pipeline:/app/pipeline" -v "$WT/similarity:/app/similarity" -v "$WT/simulation:/app/simulation" -v "$WT/db:/app/db" app python -m pytest tests/unit -m "not slow" -p no:cacheprovider --timeout=60 --tb=native
```

- Prove the mount before you trust a result: `grep -c <a new name> <file>` inside the
  container.
- When collection dies with exit 139 more than once, run the lane as two halves:
  `--ignore-glob='tests/unit/test_[n-z]*'`, then `--ignore-glob='tests/unit/test_[a-m]*'`.
  A segfault is a retry, not a failure (CLAUDE.md §2a).
- Do not add `-q`: `pyproject.toml` already carries it.
- The host Python lacks `sqlglot`, so a test that imports `api.main` fails there; the
  container is the gate. Pure simulation tests run on the host.
- `ruff check .`, `ruff format --check .` and `mypy similarity/ pipeline/ api/` run on the
  host (ruff 0.16.7) or in the container.

## 6. What every session does

1. Read this document, CLAUDE.md §2a, §7 and §14, and the memory index.
2. Split its large files by responsibility (its brief names them).
3. Read every file it owns, with the file's tests, and apply the comment standard (rule 3).
4. Delete dead code (rule 7), merge duplicate helpers, rename unclear private names, and
   rename the test classes and functions that carry a ticket number (a CI list that names a
   test changes in the same commit).
5. Run the gates: its share of the unit lane, the regression lane when it touched an engine,
   ruff, the format check and mypy, and its neutral check.
6. Write its `CHANGES.md` entry (prepended; another session's entry may land first, so keep
   both), its `docs/technical/` file and its CLAUDE.md §5 bullet.
7. Commit as it goes; push its branch to GitHub after each green slice.

A session must NOT merge into master, deploy, restart the app, run a rebuild that publishes
a bundle, edit the CI mypy scope (Phase C does), or edit another session's area beyond a
forced re-point (rule 6). It files a ticket for every bug it finds (rule 2). It ends by
reporting: the branch, the commits, the line counts before and after, the files over 1,500
lines, the deletions, the tickets filed, and the gates with their results.

## 7. B1, Simulation

**Owns** `simulation/` (30 files) and the tests in §13.3 marked B1.

- `sim_loop.py` (3,754 lines) becomes the package `simulation/loop/` of mixins on
  `StateMachine`: `values` (the constants, `spawn_rng_streams`, `times_through_order`,
  `FieldingSignal`, `StealResolution`, `RunningGameTally`, `CountAdvance`,
  `advance_count`), `boxscore` (`PlayerStatLine`, `BoxScore`, `GameSimResult`), `in_play`
  (the fielding draw, the transition and the advancement draws), `running_game` (the steal,
  pickoff and got-away resolution, the draws and the staging), `terminals` (walk,
  intentional walk, strikeout, dropped third strike, `_force_on_reach`), `manager`
  (leverage, the intentional walk, the hooks, the pitching change, `_pick_reliever`) and
  `credit` (`_box_line`, `_accumulate_pa`, the three canonical sets). The core stays in
  `sim_loop.py`: `__init__`, `step_pitch`, `_full_pool_outcome`, the run ledger
  (`_snapshot_bases`, `_check_bases`, `_commit_run_delta`), `_record_outs` placed AFTER
  `step_pitch`, the end-of-PA and half-inning methods, `simulate_game`,
  `_place_ghost_runner` and the game-over predicates. That keeps four source-reading tests
  green (`test_game_page_whatif.py`, `test_run_ledger.py`, `test_pitcher_outs_credit.py`,
  the `detect_source` file tokens). `_move_runner` stops calling `StateMachine._clear_base`
  by class name. `simulation/sim_loop.py` re-exports every name an outside caller imports,
  explicitly (`Team` and the underscored names included).
- `full_pool_sampler.py` (3,715) becomes `simulation/sampler/` mixins on
  `FullPoolSampler`: `kernels` (`_group_rows`, `_count_cell`, `_repower`,
  `_repower_neutral`, `_raise_neutral`, `_PITCH_FEATURE_WEIGHTS`), `base` (`_pool_meta`,
  the as-of cutoff, the actor matrices, `_emb_z`, `_steal_actor_factor`, the draw-neutral
  helpers), `pitch` (the pitch factors, `new_half_inning`, `new_plate_appearance`, `draw`,
  the cell index, fatigue), `pitch_result` (the split, receiving, the born-ball and
  last-pitch readers), `fielding`, `fence` (fence, carry, the wall-margin band), `steal`
  (steal, pickoff, `steal_weights`), `advancement` and `manager` (the change and relief
  draws). The 400-line `__init__` becomes one `_init_*()` call per mixin, in today's
  order; mutable caches stay instance attributes. `_pitcher_prof_score` (cached, None for an
  all-zero row) and `_pitcher_prof_scores` (uncached, the zeros) behave differently: keep
  both. `test_pool_cutoff.py` counts `self._apply_asof(` calls in the file: re-point it to
  the package. Keep `_POS_NUM_TO_NAME` and `sim_loop._POS_NUM_TO_STR` as one table.
- Review the other 28 files. `batch_runner.py` (1,320): the cache classes, the
  shared-memory block and `BatchRunner` may split; `br._MP_START_METHOD` and
  `_PROP_SET_CACHE_EPOCH` stay module globals that tests patch. `sim_kwargs.py` (1,035): each
  patched name (`open_sim_duckdb`, `resolve_park_factor_onto_state`, `resolve_asof_ymd`,
  `_PARK_FACTORS_CHECKED_AT`) stays in its caller's namespace. `lineup_resolver.py` (1,008).
- Make `simulation/` pass mypy at today's settings (`mypy simulation/`); Phase C adds it to
  CI.
- **Neutral check:** the fingerprint, all three game modes, equal to the baselines; the perf
  benches (`tests/performance`, the per-pitch budget) no slower beyond noise.

## 8. B2, Pipeline

**Owns** `pipeline/`, `db/*.py` (not `db/migrations/`) and the tests marked B2.

- **The profile computor** (`pipeline/batch/player_profile_computor.py`, 8,198): modules for
  the SQL constants and builders, geometry and leverage, the GMM block (`_fit_gmm_batch` is
  a process-pool target: keep `basicConfig` reachable in workers), the pool metadata and
  `LeagueAverageProfiles`; mixins on `PlayerProfileComputor` for pitcher, batter, baserunner
  and steal, manager and bullpen, catcher, fielder, park factors, the sim pools and the
  infrastructure. Twelve tests slice the file between `def` markers and
  `test_one_cutoff_per_table.py` reads `inspect.getsource(PlayerProfileComputor)` for the
  eight stamped tables: re-point them (rule 6). `run()` stays where the
  `build_run_expectancy_matrix` patch reaches it.
- **The artifact builder** (`pipeline/batch/engine_artifacts.py`, 3,794): one module per
  artifact under `pipeline/batch/artifacts/`, the pool dataclasses and the reference
  tables. The actor-matrix cluster stays (tests patch `_ENGINE_LOADER`,
  `concentration_report`, `_matrix_from_queries`). `EngineArtifacts.load` (656 lines)
  becomes one loader per artifact. The 8 `np.load(allow_pickle=False)` calls stay countable
  (`test_bundle_no_pickle.py`).
- **The historical loader** (`pipeline/etl/etl_historical_loader.py`, 3,688): row building,
  Savant parsing, venues, schedule helpers and a writes mixin. `_connect`, `_http_get`,
  `_fetch_once`, `_fetch_game_pitches` and `_ensure_game_player_stats` stay where tests
  patch them; `INSERT_SQL` and the single `urlopen(` stay in the file.
- **The live pipeline** (`pipeline/live/live_ingestion_pipeline.py`, 3,535): odds rows, the
  mock odds, the odds routes, the game-state builder, the WebSocket parts and two odds
  mixins. `create_app`, `_sync_live_games` and the `connection_manager` singleton stay;
  `pipeline/odds_provider.py` imports `MockOddsAPI` lazily from here.
- **The BettingPros provider** (2,173): rules, parsing, schedule, markets, HTTP and three
  mixins; `_choose_row` stays where `GRADED_BOOK_PREFERENCE` is patched.
- Merge the two `_env_float` copies in `pipeline/backup/` into `pipeline/backup/common.py`.
- **Neutral checks:** a bundle built on the base code (a worktree of `fe81d79`) and on the
  new code from the same DuckDB file, back to back, equal under the fingerprint's `bundle`
  mode; a profile build `--seasons 2025 2026 --asof 2026-10-01` into two COPIES of the
  DuckDB file (base and new code), every table equal by content without the build-time
  stamps; the loader's rows for 200 final games of 2025, built from feeds fetched once into
  `results/`, equal by hash; after the deploy, the live service's log shows its normal cycle
  for 15 minutes.

## 9. B3, API and frontend

**Owns** `api/`, `frontend/src` and the tests marked B3.

- `api/routes/games.py` (3,475): `games_slate`, `games_feed`, `games_runs` (where
  `runs/latest` stays before `runs/{run_id}`) and `games_models`. `games.py` keeps the
  aggregator `router` with its `Depends(require_auth)`, the sim seam, `_resolve_state_or_error`
  (betting imports it), the replay cluster and every route that runs a simulation. The
  patch targets on `games` (`resolve_game_state` 26 times, `sim_store`, `record_game`,
  `_build_prop_set`, ...) must keep reaching the code that calls them.
  `test_sim_kwargs.py` counts the sim-kwarg calls in `games.py` (8) and `betting.py` (1):
  re-point it if a route moves.
- `api/routes/betting.py` (2,257): lines and CLV, the card, the stored odds and the pricing;
  `betting.py` keeps `_summary_and_winprob`, edges and signals.
- `api/schemas.py` (1,498) becomes a package whose `__init__` re-exports `__all__`.
- `api/main.py` (1,085): `api/env_check.py`, a middleware module and `api/startup.py` for the
  startup steps no test patches. `lifespan`, `create_app` and the patched steps
  (`open_pg_pool`, `make_pg_name_resolver`, `open_redis_cache`, `_background_prewarm`, the
  park-factor prime, `app.state.replay_duckdb =`) stay.
- **The OpenAPI contract does not change.** A sub-router is a bare `APIRouter()`: no prefix,
  no tags, no dependencies (FastAPI appends a sub-router's tags to the aggregator's, so a
  repeated tag changes the snapshot). Handler names and paths stay, so the operationIds
  stay. Each models module imports every model its annotations name (the modules use
  `from __future__ import annotations`).
- Review the frontend: `src/api/games.ts` (750), `WhatIfPanel`, `CardDetail`, `GamePage`
  and the rest. `src/api/schema.d.ts` is generated: never edit it by hand.
- `api/auth._env_flag` reads `AUTH_DISABLED`; keep its rule (anything not off is on) unless
  you file a ticket to change it.
- **Neutral checks:** `tests/unit/test_openapi_snapshot.py` passes with no regeneration; the
  read routes' JSON for 3 fixed games equal on a worktree app (the :8010 recipe in the
  memory index) and on the live app; the frontend type-check, lint, build and Playwright e2e.

## 10. B4, Similarity and betting

**Owns** `similarity/`, `betting/` and the tests marked B4.

- `similarity/similarity_calibration.py` (2,139): `calibration/fitting.py`, the report, and
  one calibrator mixin per engine. Drop its mypy `ignore_errors` if it can; the same for
  `similarity_diagnostics.py`. The module logs as `"similarity_calibration"` (four caplog
  tests).
- `similarity/engines/pitcher_similarity.py` (2,182) may stay whole: `apply_calibration`
  rewrites the module global `ARSENAL_SCALE` and three readers use it. At most move the
  arsenal cache and its serialisation (`_compute_w2_chunk` is a process-pool target). Record
  the reason either way.
- Review the fielder (1,701), batter (1,440) and other engines, `similarity_diagnostics.py`,
  the backtester and `betting/`. The eight engines' `_load_profiles` share one pattern; a
  helper is welcome if it keeps each engine's SQL readable.
- Make `betting/` pass mypy; Phase C adds it to CI.
- **Neutral checks:** the regression lane (`tests/regression`); the actor score matrices
  built on the base code and on the new code from the same DuckDB file, equal by content.

## 11. B5, Scripts and the test harness

**Owns** `scripts/`, `tests/acceptance`, `tests/regression`, `tests/integration`,
`tests/performance`, the conftest files, `tests/fixtures`, `tests/script_loader.py` and the
tests marked B5.

- `scripts/accuracy_comparison.py` (4,428) by stage: game selection, the run, pricing,
  grading and the report. Keep its logger name and every name its eight test files read.
- `scripts/rebuild_pitch_pool.py` (2,283) and `scripts/rebuild_steal_pool.py` (1,816) by
  step.
- Point the remaining one-file script loaders in the tests (`test_accuracy_comparison*.py`,
  `test_pair_accuracy.py`, `test_check_file_integrity.py`, ...) at `tests/script_loader.py`.
- Rename the acceptance tests' functions (`test_runs_band_sim450` and the rest) and update
  the CI lists that name them (`ci.yml` acceptance-arithmetic, `acceptance-nightly.yml`).
- Rewrite the acceptance docstrings' round-by-round history (rule 3); the citations already
  name sections, not lines.
- Split `test_backend_assorted_guards.py`, `test_profile_indexes_and_etl_fallbacks.py`,
  `test_odds_dedup_and_etl_errors.py` and `test_secondary_engines.py` by subject, each part
  beside the tests of its subject.
- Route the scripts' `SIM_*` reads through `simulation.flags`.
- **Neutral checks:** the accuracy comparison on 20 games of 2024, base code against new
  code, equal report JSON (the timing fields aside); `scripts/sim_stats.py` on ten games,
  equal JSON; the acceptance arithmetic lane.

## 12. Phase C, the integration

1. Branch `sim493-integration` from `sim493-groundwork`; merge B1 to B5, each by its full
   ref `refs/heads/...`; resolve `CHANGES.md`, CLAUDE.md and `pyproject.toml`.
2. Re-point every caller to the name's new home; delete every
   `# re-export: removed in the integration pass` line.
3. Add `simulation/` and `betting/` to mypy in CI and in `make type-check`.
4. Merge master in if it moved.
5. Gates: the unit, slow, regression and integration lanes (integration on the host Python);
   ruff, the format check and mypy; the frontend checks and e2e; the perf lane under
   `PERF_STRICT=1`; the fingerprint, all modes, equal to the baselines; the 45 × 130 lane on
   the balanced set (detached, about 2.5 h), reading the six non-passes of the 2026-10-07
   lane at like sizes.
6. Records: the `CHANGES.md` close entry, CLAUDE.md, `docs/technical/README.md` and the
   cheat sheet, `BACKLOG.xlsx` (the SIM-493 row deleted, the new tickets checked), the
   memory index.
7. With the owner's OK: merge into master and push; rebuild the app image (`scripts/` is
   baked in); recreate `app`, `jobs` and `live` outside 10:00–11:00 UTC with no `.running`
   mark; `docker builder prune -f`; the live check (`/simulate` n=100 in 25–36 s, a game
   page, the slate); delete the branches, the worktrees and `/data/sim493`.

## 13. Appendix

### 13.1 The script moves (A3 + A4)

| Old path | New path |
|---|---|
| `scripts/sim518_pair_accuracy.py` | `scripts/pair_accuracy.py` |
| `scripts/clv_backtest.py` | `scripts/accuracy_comparison.py` |
| `scripts/sim523_game_set.py` | `scripts/balanced_game_set.py` |
| `scripts/sim478_fence_check.py` | `scripts/fence_check.py` |
| `scripts/sim553_rebuild_pitch_pool.py` | `scripts/rebuild_pitch_pool.py` |
| `scripts/sim554_rebuild_steal_pool.py` | `scripts/rebuild_steal_pool.py` |
| `scripts/sim548_market_skill.py` | `scripts/market_skill.py` |
| `scripts/sim548_calibrate_markets.py` | `scripts/calibrate_markets.py` |
| `scripts/sim548_design.py` | `scripts/designed_experiment.py` |
| `scripts/sim548_offline_fit.py` | `scripts/offline_draw_fit.py` |
| `scripts/sim554_running_game_replay.py` | `scripts/running_game_replay.py` |
| `scripts/sim545_boxscore_audit.py` | `scripts/boxscore_audit.py` |
| `scripts/sim548_accuracy_split.sh` | `scripts/accuracy_split_arm.sh` |
| `scripts/sim548_baseline_1000.sh` | `scripts/accuracy_baseline_1000.sh` |
| `scripts/sim548_design_run.sh` | `scripts/designed_experiment_run.sh` |
| `scripts/sim427_manager_probe.py` | `scripts/probes/manager_probe.py` |
| `scripts/sim427_manager_recompute.py` | `scripts/probes/manager_profile_recompute.py` |
| `scripts/sim427_pen_check.py` | `scripts/probes/bullpen_check.py` |
| `scripts/sim478_wall_zone_probe.py` | `scripts/probes/wall_zone_probe.py` |
| `scripts/sim518_fatigue_probe.py` | `scripts/probes/fatigue_probe.py` |
| `scripts/sim518_fatigue_scan.py` | `scripts/probes/fatigue_kernel_scan.py` |
| `scripts/sim523_concentration.py` | `scripts/probes/actor_concentration_check.py` |
| `scripts/sim523_fit_probe.py` | `scripts/probes/conditional_rate_probe.py` |
| `scripts/sim523_receiving_probe.py` | `scripts/probes/catcher_receiving_probe.py` |
| `scripts/sim531_runner_recompute.py` | `scripts/probes/runner_profile_recompute.py` |
| `scripts/sim532_fielder_recompute.py` | `scripts/probes/fielder_profile_recompute.py` |
| `scripts/sim533_arm_angle_probe.py` | `scripts/probes/arm_angle_probe.py` |
| `scripts/sim549_rescore_runlines.py` | `scripts/probes/rescore_runlines.py` |
| `scripts/sim550_arm_recompute.py` | `scripts/probes/outfield_arm_recompute.py` |
| `scripts/sim554_running_game_census.py` | `scripts/probes/running_game_census.py` |
| `scripts/sim555_book_probe.py` | `scripts/probes/odds_book_census.py` |
| `scripts/sim555_book_sharpness.py` | `scripts/probes/book_sharpness.py` |
| `scripts/sim555_rescore_reports.py` | `scripts/probes/rescore_graded_book.py` |
| `scripts/sim555_retire_consensus_rows.py` | `scripts/probes/retire_consensus_odds.py` |
| `scripts/sim555_odds_census.sql` | `scripts/probes/odds_census.sql` |
| `scripts/sim559_backfill_start_positions.py` | `scripts/probes/backfill_start_positions.py` |
| `scripts/steal_weight_volume_probe.py` | `scripts/probes/steal_weight_volume_probe.py` |
| `scripts/backfill_odds_hash.py` | `scripts/probes/backfill_odds_hash.py` |
| `scripts/check_bat_side_coverage.py` | `scripts/probes/check_bat_side_coverage.py` |
| `scripts/run_index_acceptance.py` | `scripts/probes/run_index_acceptance.py` |
| `scripts/sim523_game_set.json` | `scripts/data/balanced_game_set.json` |
| `scripts/sim548_games_2024_chunk2.txt` | `scripts/data/games_2024_chunk2.txt` |
| `scripts/sim548_games_2024_chunk3.txt` | `scripts/data/games_2024_chunk3.txt` |
| `scripts/sim548_games_2024_chunk4.txt` | `scripts/data/games_2024_chunk4.txt` |
| `scripts/sim548_games_2024_design.txt` | `scripts/data/games_2024_design.txt` |
| `scripts/sim478_lane.txt` | `docs/audit/records/sim478_lane.txt` |
| `scripts/sim518_fatigue_scan.json` | `docs/audit/records/sim518_fatigue_scan.json` |
| `scripts/sim548_accuracy_pair_split.json` | `docs/audit/records/sim548_accuracy_pair_split.json` |
| `scripts/sim548_accuracy_pair_split.sim549.json` | `docs/audit/records/sim548_accuracy_pair_split.sim549.json` |
| `scripts/sim548_accuracy_pair_split.sim549.txt` | `docs/audit/records/sim548_accuracy_pair_split.sim549.txt` |
| `scripts/sim548_accuracy_pair_split.txt` | `docs/audit/records/sim548_accuracy_pair_split.txt` |
| `scripts/sim548_baseline_1000_skill.json` | `docs/audit/records/sim548_baseline_1000_skill.json` |
| `scripts/sim548_baseline_1000_skill.sim555.json` | `docs/audit/records/sim548_baseline_1000_skill.sim555.json` |
| `scripts/sim548_baseline_1000_skill.sim555.txt` | `docs/audit/records/sim548_baseline_1000_skill.sim555.txt` |
| `scripts/sim548_baseline_1000_skill.txt` | `docs/audit/records/sim548_baseline_1000_skill.txt` |
| `scripts/sim548_lane_production_20260915.txt` | `docs/audit/records/sim548_lane_production_20260915.txt` |
| `scripts/sim548_lane_production_20260915_timedout.txt` | `docs/audit/records/sim548_lane_production_20260915_timedout.txt` |
| `scripts/sim548_market_calibration_2024split.json` | `docs/audit/records/sim548_market_calibration_2024split.json` |
| `scripts/sim548_market_calibration_2024split.txt` | `docs/audit/records/sim548_market_calibration_2024split.txt` |
| `scripts/sim548_offline_pitch_20260915_partial57.txt` | `docs/audit/records/sim548_offline_pitch_20260915_partial57.txt` |
| `scripts/sim548_offline_pitch_v1_partial_20260915.txt` | `docs/audit/records/sim548_offline_pitch_v1_partial_20260915.txt` |
| `scripts/sim548_offline_smoke.json` | `docs/audit/records/sim548_offline_smoke.json` |
| `scripts/sim548_offline_smoke_settings.json` | `docs/audit/records/sim548_offline_smoke_settings.json` |
| `scripts/sim548_offline_v2_20260915.json` | `docs/audit/records/sim548_offline_v2_20260915.json` |
| `scripts/sim548_offline_v2_20260915.txt` | `docs/audit/records/sim548_offline_v2_20260915.txt` |
| `scripts/sim548_skill_first250.json` | `docs/audit/records/sim548_skill_first250.json` |
| `scripts/sim548_skill_first500.json` | `docs/audit/records/sim548_skill_first500.json` |
| `scripts/sim555_book_sharpness.txt` | `docs/audit/records/sim555_book_sharpness.txt` |
| `scripts/steal_weight_volume_probe_2025.json` | `docs/audit/records/steal_weight_volume_probe_2025.json` |
| `scripts/steal_weight_volume_probe_2025.txt` | `docs/audit/records/steal_weight_volume_probe_2025.txt` |
| `scripts/steal_weight_volume_probe_2026.json` | `docs/audit/records/steal_weight_volume_probe_2026.json` |
| `scripts/steal_weight_volume_probe_2026.txt` | `docs/audit/records/steal_weight_volume_probe_2026.txt` |

The untracked run outputs moved from `scripts/` to `results/sim427/`, `results/sim518/`,
`results/sim548/` (the accuracy reports, the baselines, the design runs, the offline-fit
arrays and the logs) and `results/steal_weight_volume/` (the probe's arrays).

### 13.2 The test renames (A5)

| Old path | New name |
|---|---|
| `tests/acceptance/test_band_arithmetic_sim450.py` | `test_band_arithmetic.py` |
| `tests/acceptance/test_production_config_bands_sim450.py` | `test_production_bands.py` |
| `tests/acceptance/test_sim553_pool_label_check.py` | `test_pool_label_check.py` |
| `tests/integration/test_api_e2e_sim371.py` | `test_api_end_to_end.py` |
| `tests/integration/test_sim438_live_game_upsert.py` | `test_live_game_upsert.py` |
| `tests/integration/test_stress_concurrency_sim404.py` | `test_batch_runner_stress.py` |
| `tests/performance/bench_api_simulate_sim372.py` | `bench_api_simulate.py` |
| `tests/unit/test_api_auth_session_sim389.py` | `test_api_auth_session.py` |
| `tests/unit/test_api_betting_sim36x.py` | `test_api_betting.py` |
| `tests/unit/test_api_data_health_sim417.py` | `test_api_data_health.py` |
| `tests/unit/test_api_errors_sim416.py` | `test_api_error_envelope.py` |
| `tests/unit/test_api_games_card_sim366.py` | `test_api_game_card.py` |
| `tests/unit/test_api_plays_pagination_sim415.py` | `test_api_plays_pagination.py` |
| `tests/unit/test_api_realism_wiring_sim411_425b.py` | `test_api_realism_wiring.py` |
| `tests/unit/test_api_ws_schemas_sim385.py` | `test_api_websocket_schemas.py` |
| `tests/unit/test_backend_sim101_to_106_148_153.py` | `test_backend_assorted_guards.py` |
| `tests/unit/test_backend_sim311.py` | `test_game_state_contract.py` |
| `tests/unit/test_backend_sim316.py` | `test_state_machine.py` |
| `tests/unit/test_backend_sim319.py` | `test_fielding_and_baserunning_steps.py` |
| `tests/unit/test_backend_sim320.py` | `test_simulate_game_driver.py` |
| `tests/unit/test_backend_sim327.py` | `test_game_sim_summary.py` |
| `tests/unit/test_backend_sim328.py` | `test_player_stat_lines.py` |
| `tests/unit/test_backend_sim329.py` | `test_prop_distributions.py` |
| `tests/unit/test_backend_sim330.py` | `test_win_probability.py` |
| `tests/unit/test_backend_sim331.py` | `test_play_snapshots.py` |
| `tests/unit/test_backend_sim332.py` | `test_batch_runner.py` |
| `tests/unit/test_baseball_analyst_sim202.py` | `test_simulation_constants.py` |
| `tests/unit/test_baseball_analyst_sim312.py` | `test_run_resolution.py` |
| `tests/unit/test_baseball_analyst_sim323.py` | `test_manager_decisions.py` |
| `tests/unit/test_baseball_analyst_sim324.py` | `test_baseball_sniff.py` |
| `tests/unit/test_baseball_analyst_sim349.py` | `test_situational_decisions.py` |
| `tests/unit/test_bet_signal_sim369.py` | `test_bet_signal.py` |
| `tests/unit/test_betting_sim339.py` | `test_clv_engine.py` |
| `tests/unit/test_betting_sim367.py` | `test_clv_engine_run_line.py` |
| `tests/unit/test_bettingpros_odds_provider_sim405.py` | `test_bettingpros_provider.py` |
| `tests/unit/test_boxscore_ext_sim365.py` | `test_boxscore_extended.py` |
| `tests/unit/test_clv_backtest.py` | `test_accuracy_comparison_markets.py` |
| `tests/unit/test_data_engineer_sim051.py` | `test_pool_pull_relative_spray.py` |
| `tests/unit/test_data_engineer_sim074.py` | `test_barrel_definition.py` |
| `tests/unit/test_data_engineer_sim076_sim095.py` | `test_pool_recency_and_incremental_rebuild.py` |
| `tests/unit/test_data_engineer_sim085_to_091.py` | `test_profile_indexes_and_etl_fallbacks.py` |
| `tests/unit/test_data_engineer_sim090.py` | `test_etl_connection_pool.py` |
| `tests/unit/test_data_engineer_sim092_sim093.py` | `test_odds_dedup_and_etl_errors.py` |
| `tests/unit/test_data_engineer_sim115.py` | `test_pool_index_prune.py` |
| `tests/unit/test_data_engineer_sim157.py` | `test_backfill_odds_hash.py` |
| `tests/unit/test_data_engineer_sim162.py` | `test_league_average_profiles.py` |
| `tests/unit/test_data_engineer_sim336.py` | `test_park_factors.py` |
| `tests/unit/test_data_engineer_sim337.py` | `test_pool_index_reconcile.py` |
| `tests/unit/test_data_engineer_sim340.py` | `test_odds_provider_ingestion.py` |
| `tests/unit/test_engine_artifacts_realism_sim411_413_425b.py` | `test_engine_artifacts_realism.py` |
| `tests/unit/test_engine_artifacts_shared_sim403b.py` | `test_engine_artifacts_shared_memory.py` |
| `tests/unit/test_etl_lineups_sim409.py` | `test_etl_starting_lineups.py` |
| `tests/unit/test_etl_schedule_filter_sim405fix.py` | `test_etl_schedule_final_filter.py` |
| `tests/unit/test_fielder_map_sim363.py` | `test_fielder_map.py` |
| `tests/unit/test_full_pool_sampler_sim430.py` | `test_sampler_hot_path_cache.py` |
| `tests/unit/test_line_movement_sim368.py` | `test_line_movement.py` |
| `tests/unit/test_linescore_sim362.py` | `test_linescore.py` |
| `tests/unit/test_live_pipeline_sim348.py` | `test_live_pipeline.py` |
| `tests/unit/test_manager_usage_ungate_sim427.py` | `test_manager_usage_score.py` |
| `tests/unit/test_metrics_sim374.py` | `test_metrics_endpoint.py` |
| `tests/unit/test_ml_engines_sim041.py` | `test_pitch_pitch_engine.py` |
| `tests/unit/test_ml_engines_sim042.py` | `test_batted_ball_engine.py` |
| `tests/unit/test_ml_engines_sim048.py` | `test_engine_registry.py` |
| `tests/unit/test_ml_engines_sim066_071.py` | `test_secondary_engines.py` |
| `tests/unit/test_ml_engines_sim072.py` | `test_catcher_engine_throwing.py` |
| `tests/unit/test_ml_engines_sim075.py` | `test_arsenal_cache_vectorized.py` |
| `tests/unit/test_ml_engines_sim076_harness.py` | `test_recency_walk_forward.py` |
| `tests/unit/test_ml_engines_sim150.py` | `test_engine_calibration_regression.py` |
| `tests/unit/test_ml_engines_sim220.py` | `test_probabilistic_backtester.py` |
| `tests/unit/test_ml_engines_sim322.py` | `test_arsenal_w2_standardization.py` |
| `tests/unit/test_ml_engines_sim346.py` | `test_pitcher_engine_calibration.py` |
| `tests/unit/test_ml_engines_sim361.py` | `test_calibration_report_json.py` |
| `tests/unit/test_ml_engines_sim406.py` | `test_calibration_fit_and_apply.py` |
| `tests/unit/test_ml_engines_sim407.py` | `test_validate_props.py` |
| `tests/unit/test_odds_provider_sim370.py` | `test_odds_provider_seam.py` |
| `tests/unit/test_perf_eng_sim113.py` | `test_gmm_batch_pipeline.py` |
| `tests/unit/test_perf_eng_sim158.py` | `test_index_acceptance_harness.py` |
| `tests/unit/test_perf_eng_sim333.py` | `test_shared_memory_attach.py` |
| `tests/unit/test_perf_eng_sim334.py` | `test_situation_metadata_store.py` |
| `tests/unit/test_perf_eng_sim360.py` | `test_persistent_process_pool.py` |
| `tests/unit/test_perf_eng_sim377.py` | `test_batch_runner_hit_rate.py` |
| `tests/unit/test_pitcher_decisions_sim364.py` | `test_pitcher_decisions.py` |
| `tests/unit/test_profile_index_recreate_sim419.py` | `test_profile_index_recreate.py` |
| `tests/unit/test_qa_sim325.py` | `test_replay_chi_squared.py` |
| `tests/unit/test_qa_sim326.py` | `test_invalid_state_detection.py` |
| `tests/unit/test_qa_sim347.py` | `test_batch_runner_concurrency.py` |
| `tests/unit/test_realism_consumers_sim411_413_425b.py` | `test_realism_consumers.py` |
| `tests/unit/test_sim402_prewarm.py` | `test_worker_prewarm.py` |
| `tests/unit/test_sim421_backtest_props.py` | `test_accuracy_comparison_props.py` |
| `tests/unit/test_sim421_game_markets.py` | `test_odds_game_markets.py` |
| `tests/unit/test_sim421_odds_vocabulary.py` | `test_odds_vocabulary.py` |
| `tests/unit/test_sim421_prop_wrappers.py` | `test_prop_wrappers.py` |
| `tests/unit/test_sim421_runner_run_credit.py` | `test_runner_run_credit.py` |
| `tests/unit/test_sim421_segment_markets.py` | `test_segment_markets.py` |
| `tests/unit/test_sim427_bullpen_usage.py` | `test_bullpen_usage.py` |
| `tests/unit/test_sim427_game_bullpen.py` | `test_game_bullpen_listing.py` |
| `tests/unit/test_sim427_relief_draw.py` | `test_relief_draw.py` |
| `tests/unit/test_sim430_forkserver.py` | `test_worker_forkserver.py` |
| `tests/unit/test_sim430_pitcher_sim_matrix.py` | `test_pitcher_sim_matrix.py` |
| `tests/unit/test_sim432_calibration_reconciliation.py` | `test_calibration_reconciliation.py` |
| `tests/unit/test_sim433_bullpen_availability.py` | `test_bullpen_availability.py` |
| `tests/unit/test_sim434_manager_model.py` | `test_manager_model.py` |
| `tests/unit/test_sim435_historical_odds.py` | `test_historical_odds_load.py` |
| `tests/unit/test_sim440_reload_game.py` | `test_etl_reload_game.py` |
| `tests/unit/test_sim441_etl_hardening.py` | `test_etl_hardening.py` |
| `tests/unit/test_sim441_manager_def_sub.py` | `test_manager_defensive_subs.py` |
| `tests/unit/test_sim443_db_dsn.py` | `test_db_dsn.py` |
| `tests/unit/test_sim443_gen_secrets.py` | `test_gen_secrets.py` |
| `tests/unit/test_sim443_no_default_password.py` | `test_no_default_password.py` |
| `tests/unit/test_sim443_shell_guards.py` | `test_shell_dsn_guards.py` |
| `tests/unit/test_sim443_sql_console_pool.py` | `test_sql_console_pool.py` |
| `tests/unit/test_sim443c_route_auth_coverage.py` | `test_route_auth_coverage.py` |
| `tests/unit/test_sim443f_cache_codec.py` | `test_cache_codec.py` |
| `tests/unit/test_sim446_sweep_streaming.py` | `test_sweep_streaming.py` |
| `tests/unit/test_sim447_venue_resolution.py` | `test_venue_resolution.py` |
| `tests/unit/test_sim449_sim_kwargs.py` | `test_sim_kwargs.py` |
| `tests/unit/test_sim451_filter_cells.py` | `test_filter_cells.py` |
| `tests/unit/test_sim455_matchup_cache.py` | `test_matchup_weight_cache.py` |
| `tests/unit/test_sim467_cell_index.py` | `test_pitch_cell_index.py` |
| `tests/unit/test_sim474_steal_draw.py` | `test_steal_draw.py` |
| `tests/unit/test_sim478_carry_offset_and_margin.py` | `test_fence_carry_and_margin.py` |
| `tests/unit/test_sim478_fence_check.py` | `test_fence_check.py` |
| `tests/unit/test_sim478_fence_season_groups.py` | `test_fence_season_groups.py` |
| `tests/unit/test_sim478_geometry_builder.py` | `test_park_geometry_builder.py` |
| `tests/unit/test_sim478_harness_counters.py` | `test_fence_counters.py` |
| `tests/unit/test_sim483_steal_run_credit.py` | `test_steal_run_credit.py` |
| `tests/unit/test_sim484_dropped_third_strike_credits.py` | `test_dropped_third_strike_credits.py` |
| `tests/unit/test_sim486_synthetic_bundle.py` | `test_synthetic_bundle.py` |
| `tests/unit/test_sim498_rng_streams.py` | `test_rng_streams.py` |
| `tests/unit/test_sim499_run_ledger.py` | `test_run_ledger.py` |
| `tests/unit/test_sim500_base_invariants.py` | `test_base_invariants.py` |
| `tests/unit/test_sim501_profile_code_sets.py` | `test_profile_code_sets.py` |
| `tests/unit/test_sim501a_out_label.py` | `test_out_label.py` |
| `tests/unit/test_sim502_play_events.py` | `test_play_events.py` |
| `tests/unit/test_sim506_steal_labels.py` | `test_steal_labels.py` |
| `tests/unit/test_sim509_hit_by_pitch.py` | `test_hit_by_pitch.py` |
| `tests/unit/test_sim510_transition_pool.py` | `test_transition_pool.py` |
| `tests/unit/test_sim511_512_transition_draw.py` | `test_transition_draw.py` |
| `tests/unit/test_sim515_ibb_rates.py` | `test_ibb_rates.py` |
| `tests/unit/test_sim517_receiving_consumers.py` | `test_receiving_kernel_consumers.py` |
| `tests/unit/test_sim517_uncaught_k3.py` | `test_uncaught_strike_three.py` |
| `tests/unit/test_sim518_conditioning.py` | `test_draw_conditioning.py` |
| `tests/unit/test_sim518_fatigue_scan.py` | `test_fatigue_kernel_scan.py` |
| `tests/unit/test_sim518_pair_accuracy.py` | `test_pair_accuracy.py` |
| `tests/unit/test_sim519_card_odds.py` | `test_slate_card_odds.py` |
| `tests/unit/test_sim519_game_feed.py` | `test_game_feed.py` |
| `tests/unit/test_sim519_last_seen.py` | `test_odds_last_seen.py` |
| `tests/unit/test_sim519_lineup_writer.py` | `test_published_lineup_writer.py` |
| `tests/unit/test_sim519_live_bridge.py` | `test_live_bridge.py` |
| `tests/unit/test_sim519_retry_wrapper.py` | `test_retry_wrapper.py` |
| `tests/unit/test_sim519_schedule_client.py` | `test_schedule_client.py` |
| `tests/unit/test_sim519_sim_jobs.py` | `test_sim_jobs.py` |
| `tests/unit/test_sim519_slate_endpoint.py` | `test_slate_endpoint.py` |
| `tests/unit/test_sim523_actor_matrices.py` | `test_actor_matrices.py` |
| `tests/unit/test_sim523_fence_stage.py` | `test_fence_stage.py` |
| `tests/unit/test_sim523_fielding_split.py` | `test_fielding_draw.py` |
| `tests/unit/test_sim523_fit_powers.py` | `test_actor_powers.py` |
| `tests/unit/test_sim523_manager_draw.py` | `test_manager_draw.py` |
| `tests/unit/test_sim523_part_g.py` | `test_profile_data_additions.py` |
| `tests/unit/test_sim523_receiving_ratio.py` | `test_receiving_ratio.py` |
| `tests/unit/test_sim523_result_split.py` | `test_pitch_result_split.py` |
| `tests/unit/test_sim524_park_snapshot.py` | `test_park_factor_snapshot.py` |
| `tests/unit/test_sim525_backup_data.py` | `test_backup_data_stores.py` |
| `tests/unit/test_sim525_backup_drill.py` | `test_backup_drill.py` |
| `tests/unit/test_sim525_backup_gauge.py` | `test_backup_age_gauge.py` |
| `tests/unit/test_sim525_backup_status.py` | `test_backup_status.py` |
| `tests/unit/test_sim525_pg_backup_sh.py` | `test_pg_backup_script.py` |
| `tests/unit/test_sim525_schedule.py` | `test_backup_schedule.py` |
| `tests/unit/test_sim528_savant_loader.py` | `test_savant_loader.py` |
| `tests/unit/test_sim529_batter_physical.py` | `test_batter_physical_features.py` |
| `tests/unit/test_sim531_lead_distance.py` | `test_lead_distance.py` |
| `tests/unit/test_sim532_fielder_oaa_jump.py` | `test_fielder_oaa_jump_model.py` |
| `tests/unit/test_sim532_fielder_profile.py` | `test_fielder_oaa_jump_profile.py` |
| `tests/unit/test_sim532_fielder_recompute.py` | `test_fielder_profile_recompute.py` |
| `tests/unit/test_sim532_savant_oaa_jump_boards.py` | `test_savant_oaa_jump_boards.py` |
| `tests/unit/test_sim533_arm_angle_decision.py` | `test_arm_angle_decision.py` |
| `tests/unit/test_sim534_point_in_time.py` | `test_point_in_time_batter.py` |
| `tests/unit/test_sim535_pool_cutoff.py` | `test_pool_cutoff.py` |
| `tests/unit/test_sim537_baserunner_catcher_fielder_point_in_time.py` | `test_point_in_time_runner_catcher_fielder.py` |
| `tests/unit/test_sim537_point_in_time.py` | `test_point_in_time_pitcher_manager.py` |
| `tests/unit/test_sim538_accuracy_comparison.py` | `test_accuracy_comparison.py` |
| `tests/unit/test_sim539_minimum_sample_size.py` | `test_accuracy_minimum_sample.py` |
| `tests/unit/test_sim540_hypothetical_return.py` | `test_accuracy_hypothetical_return.py` |
| `tests/unit/test_sim542_fielder_placeholder_schema.py` | `test_fielder_placeholder_schema.py` |
| `tests/unit/test_sim545_boxscore_ingest.py` | `test_boxscore_ingest.py` |
| `tests/unit/test_sim545_boxscore_reader.py` | `test_boxscore_reader.py` |
| `tests/unit/test_sim546_closing_rows.py` | `test_odds_closing_rows.py` |
| `tests/unit/test_sim546_inning_grid.py` | `test_inning_grid.py` |
| `tests/unit/test_sim546_segment_edges.py` | `test_segment_edges.py` |
| `tests/unit/test_sim548_calibration.py` | `test_market_calibration.py` |
| `tests/unit/test_sim548_design.py` | `test_designed_experiment.py` |
| `tests/unit/test_sim548_instruments.py` | `test_accuracy_fit_instruments.py` |
| `tests/unit/test_sim548_sit_sigma_env.py` | `test_situation_sigma_env.py` |
| `tests/unit/test_sim549_live_run_lines.py` | `test_live_run_lines.py` |
| `tests/unit/test_sim549_rescore.py` | `test_rescore_runlines.py` |
| `tests/unit/test_sim550_arm_block.py` | `test_outfield_arm_block.py` |
| `tests/unit/test_sim550_arm_model.py` | `test_outfield_arm_model.py` |
| `tests/unit/test_sim550_arm_recompute.py` | `test_outfield_arm_recompute.py` |
| `tests/unit/test_sim551_one_cutoff_per_table.py` | `test_one_cutoff_per_table.py` |
| `tests/unit/test_sim552_simulate_game_requires_lineup.py` | `test_simulate_game_requires_lineup.py` |
| `tests/unit/test_sim553_foul_tip_strike_three.py` | `test_foul_tip_strike_three.py` |
| `tests/unit/test_sim553_pool_chain.py` | `test_pool_chain.py` |
| `tests/unit/test_sim554_no_pitch_readers.py` | `test_no_pitch_readers.py` |
| `tests/unit/test_sim554_replay_instrument.py` | `test_running_game_replay.py` |
| `tests/unit/test_sim554_run_book_scripts.py` | `test_steal_pool_rebuild_scripts.py` |
| `tests/unit/test_sim554_running_game_on_the_pitch.py` | `test_running_game_on_the_pitch.py` |
| `tests/unit/test_sim554_steal_pool_data.py` | `test_steal_pool_data.py` |
| `tests/unit/test_sim554_steal_weights_seam.py` | `test_steal_weights_seam.py` |
| `tests/unit/test_sim555_live_pages.py` | `test_odds_live_pages.py` |
| `tests/unit/test_sim555_loader_retry.py` | `test_odds_loader_retry.py` |
| `tests/unit/test_sim555_matcher.py` | `test_odds_matcher.py` |
| `tests/unit/test_sim555_one_book_per_row.py` | `test_odds_one_book_per_row.py` |
| `tests/unit/test_sim555_provider_retry.py` | `test_odds_provider_retry.py` |
| `tests/unit/test_sim555_readers.py` | `test_odds_readers.py` |
| `tests/unit/test_sim555_scripts.py` | `test_odds_reload_scripts.py` |
| `tests/unit/test_sim555_writers.py` | `test_odds_writers.py` |
| `tests/unit/test_sim557_outs_identity.py` | `test_outs_identity.py` |
| `tests/unit/test_sim557_pitcher_outs_credit.py` | `test_pitcher_outs_credit.py` |
| `tests/unit/test_sim557_play_pitcher_decisions.py` | `test_play_pitcher_decisions.py` |
| `tests/unit/test_sim557_smoke_gate.py` | `test_smoke_outs_gate.py` |
| `tests/unit/test_sim558_starter_resolution.py` | `test_starter_resolution.py` |
| `tests/unit/test_sim559_incomplete_lineup_api.py` | `test_incomplete_lineup_api.py` |
| `tests/unit/test_sim559_starting_position.py` | `test_starting_position.py` |
| `tests/unit/test_sim560_projections_pool.py` | `test_projections_on_pool.py` |
| `tests/unit/test_sim561_replay_store.py` | `test_replay_store.py` |
| `tests/unit/test_sim563_bundle_versions.py` | `test_bundle_versions.py` |
| `tests/unit/test_sim563_ingest_wait.py` | `test_ingest_wait.py` |
| `tests/unit/test_sim563_worker_and_boot.py` | `test_bundle_worker_and_boot.py` |
| `tests/unit/test_sim565_bundle_no_pickle.py` | `test_bundle_no_pickle.py` |
| `tests/unit/test_sim566_read_only_routes.py` | `test_read_only_routes.py` |
| `tests/unit/test_sim567_odds_provider_pin.py` | `test_odds_provider_pin.py` |
| `tests/unit/test_sim568_offsite.py` | `test_backup_offsite.py` |
| `tests/unit/test_sim569_trusted_proxy.py` | `test_trusted_proxy.py` |
| `tests/unit/test_sim570_nginx_resolve.py` | `test_nginx_resolve.py` |
| `tests/unit/test_sim571_pitch_result_vocabulary.py` | `test_pitch_result_vocabulary.py` |
| `tests/unit/test_sim572_game_type_filter.py` | `test_game_type_filter.py` |
| `tests/unit/test_sim573_openapi_snapshot.py` | `test_openapi_snapshot.py` |

### 13.3 The test files by area

A test belongs to the area of the module it tests. The list is the default; a session that
must change a test outside its area does so in a small separate commit (rule 6).

**B1** (78): `test_actor_matrices.py`, `test_base_invariants.py`, `test_baseball_sniff.py`, `test_batch_runner.py`, `test_batch_runner_concurrency.py`, `test_batch_runner_hit_rate.py`, `test_boxscore_extended.py`, `test_cache_codec.py`, `test_draw_conditioning.py`, `test_dropped_third_strike_credits.py`, `test_engine_artifacts_shared_memory.py`, `test_fence_carry_and_margin.py`, `test_fence_season_groups.py`, `test_fence_stage.py`, `test_fielder_map.py`, `test_fielding_and_baserunning_steps.py`, `test_fielding_draw.py`, `test_flag_registry.py`, `test_foul_tip_strike_three.py`, `test_game_page_grading.py`, `test_game_page_whatif.py`, `test_game_sim_summary.py`, `test_game_state_contract.py`, `test_hit_by_pitch.py`, `test_ibb_rates.py`, `test_inning_grid.py`, `test_invalid_state_detection.py`, `test_linescore.py`, `test_lineup_resolver.py`, `test_manager_decisions.py`, `test_manager_draw.py`, `test_manager_model.py`, `test_matchup_weight_cache.py`, `test_outs_identity.py`, `test_park_factor_snapshot.py`, `test_persistent_process_pool.py`, `test_pitch_result_split.py`, `test_pitcher_decisions.py`, `test_pitcher_outs_credit.py`, `test_pitcher_sim_matrix.py`, `test_play_pitcher_decisions.py`, `test_play_recorder.py`, `test_play_snapshots.py`, `test_player_stat_lines.py`, `test_pool_chain.py`, `test_pool_cutoff.py`, `test_projections_on_pool.py`, `test_prop_distributions.py`, `test_prop_wrappers.py`, `test_realism_consumers.py`, `test_receiving_kernel_consumers.py`, `test_receiving_ratio.py`, `test_relief_draw.py`, `test_replay_chi_squared.py`, `test_replay_store.py`, `test_rng_streams.py`, `test_run_ledger.py`, `test_run_resolution.py`, `test_runner_run_credit.py`, `test_running_game_on_the_pitch.py`, `test_sampler_hot_path_cache.py`, `test_segment_edges.py`, `test_shared_memory_attach.py`, `test_simulate_game_driver.py`, `test_simulate_game_requires_lineup.py`, `test_simulation_constants.py`, `test_situation_sigma_env.py`, `test_situational_decisions.py`, `test_starter_resolution.py`, `test_state_machine.py`, `test_steal_draw.py`, `test_steal_run_credit.py`, `test_steal_weights_seam.py`, `test_synthetic_bundle.py`, `test_transition_draw.py`, `test_win_probability.py`, `test_worker_forkserver.py`, `test_worker_prewarm.py`

**B2** (68): `test_actor_powers.py`, `test_db_introspect.py`, `test_sim_store.py`, `test_backend_assorted_guards.py`, `test_backup_age_gauge.py`, `test_backup_data_stores.py`, `test_backup_offsite.py`, `test_backup_status.py`, `test_barrel_definition.py`, `test_bettingpros_provider.py`, `test_bullpen_availability.py`, `test_bullpen_usage.py`, `test_bundle_no_pickle.py`, `test_bundle_versions.py`, `test_db_dsn.py`, `test_engine_artifacts_realism.py`, `test_etl_connection_pool.py`, `test_etl_hardening.py`, `test_etl_historical_loader.py`, `test_etl_reload_game.py`, `test_etl_schedule_final_filter.py`, `test_etl_starting_lineups.py`, `test_fence_check.py`, `test_fielder_oaa_jump_profile.py`, `test_fielder_placeholder_schema.py`, `test_gmm_batch_pipeline.py`, `test_historical_odds_load.py`, `test_league_average_profiles.py`, `test_live_bridge.py`, `test_live_pipeline.py`, `test_live_pipeline_bugs.py`, `test_manager_defensive_subs.py`, `test_odds_dedup_and_etl_errors.py`, `test_odds_last_seen.py`, `test_odds_loader_retry.py`, `test_odds_matcher.py`, `test_odds_one_book_per_row.py`, `test_odds_provider_ingestion.py`, `test_odds_provider_retry.py`, `test_odds_provider_seam.py`, `test_odds_vocabulary.py`, `test_odds_writers.py`, `test_one_cutoff_per_table.py`, `test_out_label.py`, `test_outfield_arm_block.py`, `test_park_factors.py`, `test_park_geometry_builder.py`, `test_play_events.py`, `test_player_profile_computor.py`, `test_point_in_time_batter.py`, `test_point_in_time_pitcher_manager.py`, `test_point_in_time_runner_catcher_fielder.py`, `test_pool_pull_relative_spray.py`, `test_pool_recency_and_incremental_rebuild.py`, `test_profile_code_sets.py`, `test_profile_data_additions.py`, `test_profile_index_recreate.py`, `test_profile_indexes_and_etl_fallbacks.py`, `test_published_lineup_writer.py`, `test_savant_loader.py`, `test_savant_oaa_jump_boards.py`, `test_schedule_client.py`, `test_starting_position.py`, `test_steal_labels.py`, `test_steal_pool_data.py`, `test_transition_pool.py`, `test_uncaught_strike_three.py`, `test_venue_resolution.py`

**B3** (31): `test_api_auth.py`, `test_api_auth_session.py`, `test_api_betting.py`, `test_api_data_health.py`, `test_api_error_envelope.py`, `test_api_game_card.py`, `test_api_games.py`, `test_api_games_plays.py`, `test_api_main_wiring.py`, `test_api_plays_pagination.py`, `test_api_realism_wiring.py`, `test_api_schemas.py`, `test_api_state.py`, `test_api_websocket_schemas.py`, `test_bundle_worker_and_boot.py`, `test_game_feed.py`, `test_game_type_filter.py`, `test_incomplete_lineup_api.py`, `test_metrics_endpoint.py`, `test_openapi_snapshot.py`, `test_pitch_result_vocabulary.py`, `test_read_only_routes.py`, `test_route_auth_coverage.py`, `test_sim_jobs.py`, `test_similarity_endpoint.py`, `test_similarity_explorer_adapters.py`, `test_slate_card_odds.py`, `test_slate_endpoint.py`, `test_sql_console_pool.py`, `test_sql_safety.py`, `test_trusted_proxy.py`

**B4** (31): `test_clv_engine.py`, `test_arsenal_cache_vectorized.py`, `test_arsenal_w2_standardization.py`, `test_baserunner_steal_engine.py`, `test_batted_ball_engine.py`, `test_batter_physical_features.py`, `test_batter_similarity.py`, `test_bet_signal.py`, `test_calibration_fit_and_apply.py`, `test_calibration_report_json.py`, `test_catcher_engine_throwing.py`, `test_clv_engine_run_line.py`, `test_engine_build_smoke.py`, `test_engine_calibration_regression.py`, `test_engine_registry.py`, `test_fielder_oaa_jump_model.py`, `test_fielder_similarity.py`, `test_lead_distance.py`, `test_line_movement.py`, `test_live_run_lines.py`, `test_manager_usage_score.py`, `test_odds_live_pages.py`, `test_outfield_arm_model.py`, `test_pitch_pitch_engine.py`, `test_pitcher_engine_calibration.py`, `test_pitcher_similarity.py`, `test_probabilistic_backtester.py`, `test_recency_walk_forward.py`, `test_secondary_engines.py`, `test_situation_metadata_store.py`, `test_situation_similarity.py`

**B5** (76): `test_accuracy_comparison.py`, `test_accuracy_comparison_markets.py`, `test_accuracy_comparison_props.py`, `test_accuracy_fit_instruments.py`, `test_accuracy_hypothetical_return.py`, `test_accuracy_minimum_sample.py`, `test_arm_angle_decision.py`, `test_backfill_odds_hash.py`, `test_backup_drill.py`, `test_backup_schedule.py`, `test_boxscore_ingest.py`, `test_boxscore_reader.py`, `test_calibration_reconciliation.py`, `test_check_file_integrity.py`, `test_designed_experiment.py`, `test_docs_alembic_head.py`, `test_docs_duckdb_version.py`, `test_fatigue_kernel_scan.py`, `test_fence_counters.py`, `test_fielder_profile_recompute.py`, `test_filter_cells.py`, `test_game_bullpen_listing.py`, `test_gen_secrets.py`, `test_index_acceptance_harness.py`, `test_ingest_wait.py`, `test_market_calibration.py`, `test_nginx_resolve.py`, `test_no_default_password.py`, `test_no_pitch_readers.py`, `test_odds_closing_rows.py`, `test_odds_game_markets.py`, `test_odds_provider_pin.py`, `test_odds_readers.py`, `test_odds_reload_scripts.py`, `test_outfield_arm_recompute.py`, `test_pair_accuracy.py`, `test_pg_backup_script.py`, `test_pitch_cell_index.py`, `test_pool_index_prune.py`, `test_pool_index_reconcile.py`, `test_rescore_runlines.py`, `test_retry_wrapper.py`, `test_running_game_replay.py`, `test_segment_markets.py`, `test_shell_dsn_guards.py`, `test_sim_kwargs.py`, `test_smoke_outs_gate.py`, `test_steal_pool_rebuild_scripts.py`, `test_steal_weight_volume_probe.py`, `test_sweep_streaming.py`, `test_validate_props.py`, `tests/__init__.py`, `tests/acceptance/__init__.py`, `tests/acceptance/bands.py`, `tests/acceptance/conftest.py`, `tests/acceptance/test_band_arithmetic.py`, `tests/acceptance/test_pool_label_check.py`, `tests/acceptance/test_production_bands.py`, `tests/conftest.py`, `tests/fixtures/mlb_game_feed/record_fixtures.py`, `tests/integration/__init__.py`, `tests/integration/conftest.py`, `tests/integration/test_api_end_to_end.py`, `tests/integration/test_batch_runner_stress.py`, `tests/integration/test_etl_flow.py`, `tests/integration/test_live_game_upsert.py`, `tests/integration/test_live_pipeline_upsert.py`, `tests/integration/test_schema_migrations.py`, `tests/performance/__init__.py`, `tests/performance/bench_api_simulate.py`, `tests/performance/bench_simulation.py`, `tests/regression/__init__.py`, `tests/regression/conftest.py`, `tests/regression/regression_config.py`, `tests/regression/test_engine_regression.py`, `tests/script_loader.py`
