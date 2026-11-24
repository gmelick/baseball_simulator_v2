# The code laid out by the steps the program runs (SIM-647) — plan

Status: IN PROGRESS. Every owner decision of section 8 is taken. Part 1 is deployed, Steps A
to C (2026-11-24, master 0f0fd61). Part 2's Steps A to C are committed on branch
`sim647-part2-ingest` (2026-11-24), not yet merged or deployed. Parts 3 to 7 have not started.

## 1. Why

The betting folder work (SIM-645, SIM-646) put the odds code in one folder, one file per step,
named for what the step does. The owner wants the same layout across the whole code base, so a
reader sees how the program runs from the folders alone. File sizes do not matter.

The folders today follow how the code grew:

- `pipeline/` (73 files, 27,692 lines) holds five stages: the raw data, the player profiles,
  the play pools, the bundle, the game-day service and the backups.
- `simulation/` (46 files, 18,765 lines) holds three jobs: one game (the loop and the draws),
  the batch of 100 games, and the numbers made from the batch.
- `scripts/` holds 18 files that the nightly jobs or `make` run. The images bake `scripts/` in;
  the containers do not mount it. A stale baked script nearly broke the nightly odds pass
  after SIM-645.
- Eleven files call the MLB Stats API, each with its own request code: `api/league_feed.py`,
  `betting/bettingpros/match_games.py`, `pipeline/mlb_schedule.py`,
  `pipeline/etl/etl_historical_loader.py`, `pipeline/etl/historical/venues.py`,
  `pipeline/etl/boxscore_ingest.py`, `pipeline/etl/mlb_venue_dimensions.py`,
  `pipeline/etl/venue_backfill_job.py`, `pipeline/live/bullpen_availability_ingest.py`,
  `pipeline/live/lineup_writer.py` and `pipeline/live/ingestion/constants.py`.
- `api/routes/games.py` (1,674 lines) holds the code that starts a simulation run beside the
  routes.
- 309 test files sit in one flat `tests/unit/` folder.

## 2. The rules (from the betting folder)

1. **One top-level folder per step of the program, in run order.** The folder name says the
   step. Python forbids a digit at the start of a package name, so `docs/technical/architecture.md`
   and each folder's `__init__.py` docstring state the order.
2. **One file per step inside a folder, named for what it does** (`load_games.py`,
   `build_pools.py`, `run_batch.py`). A file may be any size.
3. **No shim.** Every import names the new home (CLAUDE.md section 5).
4. **One copy of each step.** A step written twice is merged in the part's Step B.
5. **Three steps per part.** Step A moves code and changes no behaviour. Step B changes
   behaviour. Step C cleans up the code (rule 8). A reviewer reads each step apart.
6. **One branch per part, merged and deployed before the next part starts** (CLAUDE.md
   section 7). A part's tests move with it, into `tests/unit/<folder>/`.
7. **A mixin split stays as it is in Step A.** The loop, the sampler, the profile build and the
   historical loader split one class across files by step. They already follow rule 2. Turning
   a mixin into plain functions is a behaviour risk and is not in this plan.
8. **Step C, the clean-up (owner directive 2026-11-24).** The same pass the betting code had
   (SIM-642, SIM-643):
   - **The function review.** Each function and class in the part gets a one-line reason
     to exist on its own. Two functions that do one job become one. A helper with one caller
     moves into its caller when that reads more simply. A dead function is deleted.
   - **The comment rewrite.** Every module docstring, docstring and comment is rewritten in
     plain, short language (CLAUDE.md section 14). A docstring says what the function does
     and what it returns. A comment says why, never what the next line already says. Ticket
     history goes to `CHANGES.md`, not into the code.
   - The comment rewrite changes no behaviour, and the AST comparison of rule 5 proves it.
     A merge in the function review is a behaviour change: it has its own commit and its
     own tests.

## 3. The target layout

In run order:

| Folder | Its job |
|---|---|
| `ingest/` | Get the real data into Postgres `raw.*`: the MLB Stats API, Baseball Savant, the box scores, the bullpens, the venues. |
| `profiles/` | Build each player's season profile in DuckDB `derived.*`. |
| `pools/` | Build the play pools in DuckDB `sim.*`, and check their labels. |
| `similarity/` | Score how alike two players are, fit the engines' constants, build the score tables the draws read. |
| `bundle/` | Package the pools and the score tables into the versioned bundle, and load it in each worker. |
| `simulation/` | Play one game: the draws and the game loop. |
| `runs/` | Run many games of one matchup: the lineup, the inputs, the workers, the batch. |
| `outputs/` | Turn the runs into numbers: the summary, the props, the market prices, the win probability, the recorded plays. |
| `betting/` | The odds and the pricing (done, SIM-645). |
| `live/` | Game day: follow the games, build each game's state, the lineups, the odds hook, the broadcast. |
| `api/` | Serve the screens. |
| `validation/` | Measure the model: the accuracy comparison, prop validation, the replay test, the lanes' tools, the weight fit. |
| `operations/` | Keep the stack running: the nightly jobs, the backups, the secrets. |
| `db/` | The schemas, the migrations, the roles and the database helpers (unchanged in shape). |
| `scripts/` | Developer tools and one-off probes only. |

## 4. The file map

"B" marks a change that lands in the part's Step B. Every other row is a Step A move. A name
in brackets is the class or function that moves.

### Part 1 — `operations/` and the production files out of `scripts/`

| Today | Moves to |
|---|---|
| `scripts/nightly_ingest.sh`, `nightly_finals.sh`, `nightly_rebuild.sh`, `nightly_closing_lines.sh`, `weekly_refresh.sh` | `operations/jobs/` (same names) |
| `scripts/wait_for_ingest.sh`, `with_mark.sh`, `with_retry.sh` | `operations/jobs/` |
| `pipeline/backup/data_stores.py` | `operations/backup/back_up_data_stores.py` |
| `pipeline/backup/offsite.py` | `operations/backup/copy_offsite.py` |
| `pipeline/backup/status.py` | `operations/backup/backup_status.py` |
| `pipeline/backup/common.py` | `operations/backup/common.py` |
| `scripts/backup_drill.py` | `operations/backup/backup_drill.py` |
| `db/backup/pg_backup.sh`, `pg_restore.sh` | `operations/backup/postgres/` (as built: the `db` container mounts only this folder) |
| `scripts/gen_secrets.py` | `operations/gen_secrets.py` |
| `pipeline/db_dsn.py` | `db/dsn.py` |

The config that names these paths: `deploy/ofelia/config.ini` (11 lines), the Makefile, the
compose file (a new `./operations` mount in `app`, `live` and `jobs`; the `db` service's backup
mount moves to `./operations/backup/postgres`), the Dockerfile's `COPY` lines. The tests move to
`tests/unit/operations/`.

The deploy (as built): merge and recreate in one window, after about 11:00 UTC and before the
07:00 UTC finals, with no `.running` mark. The mounted `pipeline/` loses `backup/` the moment the
host checkout moves, so the old scheduler entries for the two backup jobs fail until the
recreate. Rebuild `app`, `live` and `jobs`; recreate them, `db` (a short Postgres restart) and the
scheduler.

### Part 2 — `ingest/`

| Today | Moves to |
|---|---|
| `pipeline/mlb_schedule.py` | `ingest/mlb/schedule.py` |
| `pipeline/mlb_game_feed.py` | `ingest/mlb/game_feed.py` |
| `api/league_feed.py` | `ingest/mlb/client.py` (B: the one MLB Stats API client; the eleven callers of section 1 use it) |
| `pipeline/etl/etl_historical_loader.py` | `ingest/games/load_games.py` |
| `pipeline/etl/historical/base.py` | `ingest/games/base.py` |
| `pipeline/etl/historical/schedule.py` | `ingest/games/date_windows.py` |
| `pipeline/etl/historical/game_values.py` | `ingest/games/game_values.py` |
| `pipeline/etl/historical/rows.py` | `ingest/games/pitch_rows.py` |
| `pipeline/etl/historical/reference_rows.py` | `ingest/games/reference_rows.py` |
| `pipeline/etl/historical/venues.py` | `ingest/games/venues.py` |
| `pipeline/etl/historical/writes.py` | `ingest/games/write_rows.py` |
| `pipeline/etl/play_events.py` | `ingest/games/play_events.py` |
| `pipeline/etl/coercion.py` | `ingest/games/coercion.py` |
| `scripts/reload_games.py`, `scripts/resumable_sweep.py` | `ingest/games/reload_games.py`, `ingest/games/resumable_sweep.py` |
| `pipeline/etl/boxscore_ingest.py` | `ingest/load_boxscores.py` |
| `scripts/load_official_boxscores.py` | merged into `ingest/load_boxscores.py` (B: one box-score load path) |
| `pipeline/live/bullpen_availability_ingest.py` | `ingest/load_bullpen_availability.py` |
| `pipeline/etl/mlb_venue_dimensions.py` | `ingest/venues/load_venue_dimensions.py` |
| `pipeline/etl/venue_backfill_job.py` | `ingest/venues/backfill_venues.py` (or deleted: the venue backfill ticket, SIM-625) |
| `pipeline/etl/savant_boards.py` | `ingest/savant/boards.py` |
| `pipeline/etl/savant_loader.py` | `ingest/savant/load_leaderboards.py` |
| `pipeline/etl/savant_pitch_tracking_loader.py` | `ingest/savant/load_pitch_tracking.py` |
| `pipeline/etl/etl_sprint_speed_loader.py` | `ingest/savant/load_sprint_speed.py` |
| `pipeline/etl/historical/savant.py` | `ingest/savant/page_json.py` |
| `pipeline/statcast_events.py` | `ingest/statcast_events.py` |

Step B also moves the schedule read of `betting/bettingpros/match_games.py` onto
`ingest/mlb/client.py`.

The client's design (2026-11-24). Today three callers carry their own copy of a retry
policy: the game loader (ten retries, a linear wait, `Retry-After` replaces the wait), the
box-score load (four attempts, a doubling wait) and the odds provider (an env-set count, a
doubling wait with a cap, `Retry-After` as a floor). The rest retry nothing.

- **One sync GET** in `ingest/mlb/client.py`, built from the game loader's (`urllib`, the
  `HttpError` type, the case-insensitive response). It takes a timeout, headers and a
  retry policy per caller. Every sync caller uses it, the BettingPros reads included.
- **One retry shape:** the wait doubles from the caller's first wait, a numeric
  `Retry-After` sets a floor, and a cap ends the growth. 408, 425, 429, every 5xx and every
  connection error retry; any other status fails at once. Each caller keeps its own count
  and first wait.
- **One async reader**, the app's `LeagueFeed`, serves the live service's feed and lineup
  reads and the venue backfill.
- `MLB_BASE` and the URL of each endpoint (schedule, feed, box score, people, venues,
  coaches, roster) live in the client once. The check: a row comparison at scale, as in SIM-645. Record every MLB
response of 300+ games with the old code, replay them through the new code, and compare the
rows each loader writes.

### Part 3 — `profiles/`, `pools/` and `bundle/`

| Today | Moves to |
|---|---|
| `pipeline/batch/player_profile_computor.py` | `profiles/build_profiles.py` |
| `pipeline/batch/profiles/base.py`, `constants.py`, `sql.py`, `defense.py` | `profiles/` (same names) |
| `pipeline/batch/profiles/pitcher.py`, `batter.py`, `fielder.py`, `fielder_plays.py`, `catcher.py`, `baserunner.py`, `manager.py`, `leverage.py`, `league_averages.py` | `profiles/` (same names) |
| `pipeline/batch/profiles/gmm.py` | `profiles/arsenal_fit.py` |
| `pipeline/batch/profiles/park.py` | `profiles/park_factors.py` |
| `pipeline/batch/profiles/pools.py` | `pools/` split one file per pool: `build_pitch_pool.py`, `build_batted_ball_pool.py`, `build_steal_pool.py`, `build_advancement_pool.py`, `build_pitching_change_pool.py`, and `build_pools.py` for the order |
| `pipeline/batch/profiles/pool_meta.py` | `pools/pool_metadata.py` |
| `pipeline/batch/pool_chain.py` | `pools/check_pool_labels.py` |
| `pipeline/batch/engine_artifacts.py` | split: `bundle/build_bundle.py` (the nightly build), `bundle/load_bundle.py` (the per-worker loader), and `similarity/build_score_tables.py` (`build_actor_sim_matrices`) |
| `pipeline/batch/bundle_versions.py` | `bundle/versions.py` |
| `pipeline/batch/artifacts/common.py` | `bundle/common.py` |
| `pipeline/batch/artifacts/pools.py` | `bundle/worker_pools.py` |
| `pipeline/batch/artifacts/pitch_pool.py`, `battedball_pool.py`, `steal_pool.py`, `advancement_pool.py` | `bundle/export_pitch_pool.py`, `export_batted_ball_pool.py`, `export_steal_pool.py`, `export_advancement_pool.py` |
| `pipeline/batch/artifacts/manager_pool.py` | `bundle/export_pitching_change_pool.py` |
| `pipeline/batch/artifacts/pitcher_sim.py` | `bundle/export_pitcher_scores.py` |
| `pipeline/batch/artifacts/actor_embeddings.py` | `bundle/export_actor_embeddings.py` |
| `pipeline/batch/artifacts/park_geometry.py` | `bundle/park_geometry.py` |
| `pipeline/batch/artifacts/receiving.py` | `bundle/catcher_receiving.py` |
| `pipeline/batch/artifacts/reference.py` | `bundle/reference_tables.py` |
| `simulation/synthetic_bundle.py` | `bundle/synthetic_bundle.py` |

The nightly shell jobs call `python -m pipeline.batch.player_profile_computor` and
`python -m pipeline.batch.engine_artifacts`; both change. The check: one profile build and one
bundle build on a frozen DuckDB copy with the old code and the new code; the tables and the
bundle's arrays must be byte-identical.

### Part 4 — `simulation/`, `runs/` and `outputs/`

| Today | Moves to |
|---|---|
| `simulation/game_state.py`, `flags.py` | `simulation/` (same names) |
| `simulation/constants.py` | `simulation/vocabulary.py` |
| `simulation/full_pool_sampler.py` | `simulation/draws/sampler.py` |
| `simulation/sampler/base.py`, `kernels.py`, `pitch.py`, `pitch_result.py`, `fielding.py`, `fence.py`, `advancement.py`, `steal.py` | `simulation/draws/` (same names) |
| `simulation/sampler/manager.py` | `simulation/draws/pitching_change.py` |
| `simulation/filter_cells.py` | `simulation/draws/filter_cells.py` |
| `simulation/sim_loop.py` | `simulation/game/play_game.py` |
| `simulation/loop/base.py`, `values.py`, `running_game.py`, `manager.py`, `credit.py` | `simulation/game/` (same names) |
| `simulation/loop/terminals.py` | `simulation/game/plate_appearance_ends.py` |
| `simulation/loop/in_play.py` | `simulation/game/ball_in_play.py` |
| `simulation/loop/boxscore.py` | `simulation/game/box_score.py` |
| `simulation/run_resolution.py` | `simulation/game/run_value.py` |
| `simulation/lineup_resolver.py` | `runs/resolve_lineup.py` |
| `pipeline/bullpen_usage.py` | `runs/resolve_pen.py` |
| `simulation/sim_kwargs.py` | `runs/build_game_inputs.py` |
| `simulation/start_state.py` | `runs/start_from_play.py` |
| `simulation/whatif.py` | `runs/what_if.py` |
| `simulation/production_factory.py` | `runs/make_workers.py` |
| `simulation/batch_runner.py` | `runs/run_batch.py` |
| the simulation seam in `api/routes/games.py` | `runs/simulate_matchup.py` (B: the route keeps only the request and the response) |
| `simulation/results.py` | `outputs/summarize_runs.py` |
| `simulation/linescore.py`, `pitcher_decisions.py`, `prop_distributions.py`, `win_probability.py`, `cache_codec.py` | `outputs/` (same names) |
| `simulation/game_market_distributions.py` | `outputs/game_market_prices.py` |
| `simulation/play_recorder.py` | `outputs/record_plays.py` |
| `simulation/snapshots.py` | `outputs/field_snapshots.py` |

The tests that read the loop's source (the run ledger and the outs writer, CLAUDE.md
section 15) follow the new file names. The check: `scripts/behaviour_fingerprint.py` reads the
same fingerprint before and after; the unit, regression and acceptance-arithmetic lanes pass.

### Part 5 — `similarity/`

| Today | Moves to |
|---|---|
| `similarity/engines/<actor>_similarity.py` (11 files) | `similarity/engines/<actor>.py` |
| `similarity/engines/rbf_kernel.py`, `arsenal_cache.py` | `similarity/engines/kernel.py`, `arsenal_cache.py` |
| `similarity/similarity_calibration.py`, `similarity/calibration/*` | `similarity/fit_constants/` (one file per engine, as today) |
| `scripts/fit_calibration.py` | `similarity/fit_constants/run_fit.py` |
| `similarity/similarity_diagnostics.py` | `similarity/check_scores.py` |
| `similarity/backtesting/*` | `validation/backtesting/` (part 7) |

Step B: the engine constants the draws read (SIM-587) come from one place. The check: the
regression lane, and the score tables built on a frozen DuckDB copy are byte-identical.

### Part 6 — `live/` and `api/`

| Today | Moves to |
|---|---|
| `pipeline/live/run_live.py`, `broadcast.py` | `live/` (same names) |
| `pipeline/live/live_ingestion_pipeline.py` | `live/follow_games.py` |
| `pipeline/live/ingestion/base.py` | `live/base.py` |
| `pipeline/live/ingestion/constants.py` | `live/settings.py` |
| `pipeline/live/ingestion/mlb_websocket.py` | `live/mlb_socket.py` |
| `pipeline/live/ingestion/game_state_builder.py` | `live/build_game_state.py` |
| `pipeline/live/ingestion/game_odds.py`, `prop_odds.py`, `odds_cycle.py` | `live/odds_hook.py` (one file) |
| `pipeline/live/lineup_writer.py` | `live/write_lineups.py` |
| `api/main.py`, `startup.py`, `env_check.py`, `middleware.py`, `errors.py`, `auth.py`, `state.py`, `bundle_watch.py`, `serialization.py`, `sim_jobs.py` | `api/app/` (same names) |
| `api/routes/games.py`, `games_slate.py`, `games_feed.py`, `games_runs.py`, `games_models.py` | `api/routes/game.py`, `slate.py`, `game_feed.py`, `game_runs.py`, `game_models.py` |
| `api/routes/ai_assistant.py`, `analytics.py`, `data_health.py`, `players.py`, `schema_introspect.py`, `sql_runner.py`, `sql_safety.py` | `api/routes/data_lab/` |
| `api/routes/similarity.py`, `similarity_explorer.py` | `api/routes/similarity/` |
| the other routes and `api/schemas/` | unchanged |

Part 2's review left two twins for this part: the live service's game-status map duplicates
`ingest/games/game_values._map_game_status` (but ignores `codedGameState`), and its lineup
writer builds a `raw.players` row the way the game loader's `_ensure_players` does, with a
different position source and conflict rule. A merge changes what the live service writes, so
it needs the owner's ruling.

The live service's entry point changes (`python -m live.run_live`); the compose file's `live`
command follows. The API snapshot is regenerated in the image (CLAUDE.md section 15). The check:
the unit lane, the API snapshot, and a live check on the next game day.

### Part 7 — `validation/` and the rest of `scripts/`

| Today | Moves to |
|---|---|
| `scripts/accuracy_comparison.py`, `scripts/accuracy/*`, `pair_accuracy.py`, `market_skill.py`, `calibrate_markets.py` | `validation/accuracy/` |
| `scripts/designed_experiment.py`, `designed_experiment_run.sh`, `offline_draw_fit.py`, `accuracy_baseline_1000.sh`, `accuracy_split_arm.sh`, `scripts/data/games_2024_*.txt` | `validation/weight_fit/` |
| `scripts/validate_props.py`, `simulation/prop_validation.py` | `validation/props/` (B: one copy of the prop ground-truth readers, SIM-618) |
| `simulation/validation/replay_chi_squared.py` | `validation/replay_chi_squared.py` |
| `scripts/sim_stats.py`, `balanced_game_set.py` (+ `scripts/data/balanced_game_set.json`), `fence_check.py`, `pool_window_census.py`, `boxscore_audit.py`, `running_game_replay.py`, `measure_filter_cells.py` | `validation/` |
| `similarity/backtesting/*` | `validation/backtesting/` |
| `scripts/behaviour_fingerprint.py`, `check_file_integrity.py`, `docs_file_index.py`, `export_openapi.py`, `trace_game.py`, `genexpr_crash_probe.py`, `rebuild_pitch_pool.py` (+ package), `rebuild_steal_pool.py` (+ package), `probes/` | stay in `scripts/` |
| `tests/unit/*` not yet moved | `tests/unit/<folder>/` |

## 5. What every part touches

- **The compose file:** one mount per new folder in `app`, `live` and `jobs`; the old mount
  goes when its folder empties. About twelve mounts at the end, from six today.
- **The Dockerfile:** one `COPY` line per folder.
- **CI and the tools:** the mypy scope (`ci.yml` and the Makefile), the coverage flags, the
  `pyproject.toml` omit lines, the file-index stages (`scripts/docs_file_index.py` `STAGES`).
- **The docs:** the `docs/technical/` page of the area is rewritten from the code; the file
  index is regenerated; CLAUDE.md sections 2a, 4, 5 and 8 name the new paths.
- **The backlog:** the rows that name a moved file get the new path in the same commit.
- **The deploy:** rebuild `app`, `live` and `jobs`, then recreate them after about 11:00 UTC;
  recreate the scheduler when `config.ini` changes (parts 1, 2 and 3).

## 6. The checks of every part

- ruff, the format check and mypy over the new scope.
- The container unit lane in two halves, with the worktree's code folders mounted over the
  service's own mounts.
- Step A: the AST comparison of each moved function (its code with docstrings and imports
  removed) against the old one.
- The part's own output check (sections 4 and 7): the row comparison, the byte comparison or
  the behaviour fingerprint.
- Step C: the AST comparison (docstrings removed) for the comment-rewrite commit; for the
  function-review commit, the part's tests plus a test for each merged function. The function
  review's table (each function, its reason, and kept, merged or deleted) goes in the
  `CHANGES.md` entry.
- An independent QA pass over the diff.

## 7. Order and sequencing

The parts run in the order of section 4. Part 1 is first because it is small and ends the
baked-script trap. Part 2 is next because it is the one with the most duplicated code. A part's
branch lives a few days at most. Open defect tickets that edit a part's files land before that
part starts, or after it merges. The weight fit (SIM-548) is paused; no part changes a computed
number, so the order does not touch the fit.

## 8. Owner decisions

1. **Flat folders (decided, 2026-11-23).** Each step is a top-level folder (`ingest/`,
   `runs/`). The imports stay short; the compose file carries about twelve mounts. The owner
   chose this over one parent package (`mlb/ingest/`).
2. **The priority: P2 (decided, 2026-11-23).** The ticket sits after the model defects,
   because each part touches files those tickets edit.
3. **The frontend stays out (decided, 2026-11-23).** Its folders already follow the screens
   (`components/slate`, `components/games`, `components/lab`).
4. **The file names: section 4's names for now (decided, 2026-11-23).** The owner reviews them
   and names any change; the plan's file map then follows.

## 9. Out of scope

- Turning the mixin splits into plain functions (rule 7).
- The frontend.
- Any change to a computed number. A defect found during a move is filed as its own ticket,
  unless it is a duplicate step that Step B of the part merges.
