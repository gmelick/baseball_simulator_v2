# Code review path

This page gives you an order to read the whole codebase in. The repository holds about
790 source files. You do not need to open them at random. Read them in two passes:

1. **The tour** (about an hour). Follow one simulation request from the button on the
   game page to the numbers on the screen. You open one function in each layer and
   skip the rest. After the tour you know where every layer sits.
2. **The deep review** (fifteen stages). Read the code from the core outward. Stages 1
   to 3 are the simulator itself: one pitch, and the draws that decide it. Each later
   stage answers one question about the core: where its data comes from, or where
   its output goes.

The order runs from the core outward, so an early stage sometimes calls code that a later
stage explains: the loop calls the sampler, and the sampler reads the bundle. The tour and
the loop cheat sheet tell you enough about that code to read on. Stages 13 to 15 are
independent of each other: read those in any order.

## How to use this page

- **Keep three pages open.** This page says what to look for. The stage's section of
  the [file index](file-index.md) lists every file of the stage, with its size and the
  tests that import it. The area's reference page (linked in each stage) explains each
  function.
- **Read the test after the module.** The file index's "Tested by" column names the most
  specific tests. A test states the behaviour the code must keep, in a form you can run.
- **Keep the [loop cheat sheet](sim-loop-cheat-sheet.md) open for stages 2 to 5.** It
  shows every draw, its filters, its weights and their production values on one page.
- **Links open the file.** In the Claude app, a link opens the file in the file pane. In
  VS Code and on GitHub, a link opens the file too. The MkDocs website does not serve
  source files, so a source link there goes nowhere: read the source in your editor.
- **Track your progress.** Tick a box in the list below as you finish a stage. The boxes
  are plain Markdown (`- [x]`), so an edit to this file keeps them.

## Progress

- [ ] The tour
- [ ] 1. The game's vocabulary
- [ ] 2. One pitch: the loop
- [ ] 3. The draws: the sampler
- [ ] 4. The sampler's data: the bundle
- [ ] 5. The similarity scores
- [ ] 6. Profiles and pools: the DuckDB layer
- [ ] 7. Getting data in: the raw tables
- [ ] 8. Running many games and reading the results
- [ ] 9. Odds and betting
- [ ] 10. The API
- [ ] 11. Game day: the live service
- [ ] 12. The frontend
- [ ] 13. Operations: the stack, the nightly jobs, the backups
- [ ] 14. Measurement and maintenance tools
- [ ] 15. Tests and CI

The [file index](file-index.md) gives each stage's exact file count and line count. The
heart of the system, stages 1 to 4, is about 15,000 lines. The tests are about 125,000.

---

## The tour: one simulation request, end to end

Open each function, read its docstring and its body, and move on. Do not follow the
calls yet. The deep review does that.

1. **The button.** The game page asks for a run of 100 games:
   `startSimRun` in [frontend/src/api/games.ts](../../frontend/src/api/games.ts) sends
   `POST /api/games/{game_pk}/simulate`.
2. **The route.** `start_simulation_run` in
   [api/routes/games.py](../../api/routes/games.py) checks the login and
   hands the request to the run-job registry.
3. **The run job.** `SimJobRegistry.submit` and `SimJobRegistry._play` in
   [api/sim_jobs.py](../../api/sim_jobs.py) record a durable row in `sim.sim_runs`,
   queue the job, and run it on a thread so the API stays responsive.
4. **The runner.** `BatchRunner.run_job` in
   [simulation/batch_runner.py](../../simulation/batch_runner.py) splits the 100 games
   across a pool of worker processes. Each worker plays one game in `_run_one`.
5. **The worker's sampler.** `_build_full_pool_sampler` in
   [simulation/production_factory.py](../../simulation/production_factory.py) builds
   the sampler once per worker, from the bundle of pools and score matrices on disk
   (`EngineArtifacts.load` in
   [pipeline/batch/engine_artifacts.py](../../pipeline/batch/engine_artifacts.py)).
6. **One game.** `simulate_game` in
   [simulation/sim_loop.py](../../simulation/sim_loop.py) sets up the two lineups and
   calls `step_pitch` until the game ends.
7. **One pitch.** `StateMachine.step_pitch` in the same file runs the decisions of one
   pitch in order: the pitching change, the pickoff, the pitch, its result, the steal,
   then the play that ends the plate appearance.
8. **One draw.** `FullPoolSampler.draw` in
   [simulation/sampler/pitch.py](../../simulation/sampler/pitch.py) picks one real
   pitch from the pool of real pitches, weighted by how much the real pitcher, batter
   and situation resemble the live ones. The drawn row IS the pitch.
9. **The summary.** `GameSimSummary.from_results` in
   [simulation/results.py](../../simulation/results.py) folds the 100 games into win
   rates, score distributions and the inning-by-inning scores.
10. **The props and the markets.** `PropDistributionSet` in
    [simulation/prop_distributions.py](../../simulation/prop_distributions.py) turns
    the box scores into a distribution per player statistic.
    `market_probability` in
    [simulation/game_market_distributions.py](../../simulation/game_market_distributions.py)
    prices each game market from the inning-by-inning scores.
11. **The edge.** `get_game_edges` in [api/routes/betting.py](../../api/routes/betting.py)
    compares the model's probabilities with the bookmaker's prices through
    [betting/clv_engine.py](../../betting/clv_engine.py).
12. **The screen.** The game page shows the run in
    [SimulationCard.tsx](../../frontend/src/components/games/SimulationCard.tsx) and
    the prices in [BettingCard.tsx](../../frontend/src/components/games/BettingCard.tsx).

Then read the [architecture overview](architecture.md). It names the services, the data
stores and the nightly jobs that the tour did not touch.

---

## The deep review

Each stage has the same parts: what the stage does, the files to read first and in what
order, the questions to check, and the tests that pin the behaviour.

<a id="stage-1"></a>

## 1. The game's vocabulary

**What it does.** These files define the words every other file uses: the game state,
the result of one pitch, the bases, the count and the switches that turn features on
and off.

**Read in this order.**

1. [simulation/game_state.py](../../simulation/game_state.py): `Bases`, `GameState` and
   `PlayResult`. `Bases.assert_transition` checks that no runner appears or vanishes
   on a play.
2. [simulation/loop/values.py](../../simulation/loop/values.py): the count machine
   `advance_count`, the random streams `spawn_rng_streams`, and the small value
   objects the loop passes around.
3. [simulation/constants.py](../../simulation/constants.py): the shared constants.
4. [simulation/flags.py](../../simulation/flags.py): the registry of every `SIM_*`
   switch, with its type, its default and its meaning. Code reads a switch only through
   `read_bool`, `read_int`, `read_float` or `read_str`.

**Check.**

- Does `GameState` hold everything a pitch needs, and nothing a pitch does not use?
- Does `advance_count` match the rules of baseball for every count and pitch class?
- Does every switch the code reads have a row in the registry, with a default that
  matches what production runs (the `app` service of `docker-compose.yml`)?

**Tests.** `test_game_state_contract.py`, `test_base_invariants.py`,
`test_flag_registry.py`, `test_rng_streams.py`.

**Reference.** [Simulation](simulation.md).

<a id="stage-2"></a>

## 2. One pitch: the loop

**What it does.** The state machine plays one pitch at a time. Around the pitch it runs
the manager's decisions and the running game. On the last pitch of a plate appearance
it resolves the ball in play, the walk or the strikeout, then credits the box score.

**Read in this order.**

1. [simulation/sim_loop.py](../../simulation/sim_loop.py): `StateMachine.step_pitch`
   first, then `_commit_run_delta` (the one place a play becomes runs and outs),
   `_record_outs` (the one writer of a pitcher's outs), `_end_of_pa`,
   `advance_half_inning` and `simulate_game`.
2. [simulation/loop/terminals.py](../../simulation/loop/terminals.py): the walk, the hit
   by pitch, the intentional walk, the strikeout and the dropped third strike.
3. [simulation/loop/running_game.py](../../simulation/loop/running_game.py): the
   pickoff before the pitch, the steal after the pitch's result, and the runner's
   advance on a pitch that gets away.
4. [simulation/loop/in_play.py](../../simulation/loop/in_play.py): the ball in play,
   applied as the drawn real play's whole base-out change, then the extra-base draws.
5. [simulation/loop/manager.py](../../simulation/loop/manager.py): the pitching change
   and the intentional walk.
6. [simulation/loop/credit.py](../../simulation/loop/credit.py) and
   [simulation/loop/boxscore.py](../../simulation/loop/boxscore.py): the box score.
7. [simulation/run_resolution.py](../../simulation/run_resolution.py) (`resolve_runs`)
   and [simulation/start_state.py](../../simulation/start_state.py) (a game that starts
   at a real plate appearance, for the "what if" feature).

**Check.**

- **The architecture rule (owner rulings 2026-08-10 and 2026-08-29).** Every decision is
  a similarity-weighted draw from a filtered pool of real plays, and the drawn row IS
  the play. Look for any formula that decides an outcome, or any change made to a drawn
  play after the draw. Either one breaks the rule.
- Does every resolver take a snapshot of the bases BEFORE it changes them, and route the
  play through `_commit_run_delta`?
- Does a pickoff that ends the inning produce a "no pitch" result, and does every reader
  of a pitch result skip it (`PlayResult.no_pitch`)?
- Do the outs on the pitchers' lines equal the outs played in every game?
- Do the extra-inning ghost runner and the walk-off end the game at the right moment?

**Tests.** `test_state_machine.py`, `test_run_ledger.py`, `test_pitcher_outs_credit.py`,
`test_outs_identity.py`, `test_running_game_on_the_pitch.py`, `test_no_pitch_readers.py`,
`test_dropped_third_strike_credits.py`, `test_simulate_game_driver.py`.

**Reference.** [Simulation](simulation.md) and the
[loop cheat sheet](sim-loop-cheat-sheet.md).

<a id="stage-3"></a>

## 3. The draws: the sampler

**What it does.** The sampler makes every draw the loop asks for. For each draw it
filters a pool of real plays to the rows that fit the live situation (the "cell"),
weights each row by how much its pitcher, batter and other actors resemble the live
ones, and picks one row at random by that weight.

**Read in this order.**

1. [simulation/full_pool_sampler.py](../../simulation/full_pool_sampler.py): the
   constructor. It calls one `_init_*` method per draw module.
2. [simulation/sampler/base.py](../../simulation/sampler/base.py) and
   [simulation/sampler/kernels.py](../../simulation/sampler/kernels.py): the shared
   attributes and the weight functions.
3. [simulation/sampler/pitch.py](../../simulation/sampler/pitch.py): `new_half_inning`,
   `new_plate_appearance` and `draw`, the pitch draw through the cell index.
4. [simulation/sampler/pitch_result.py](../../simulation/sampler/pitch_result.py): the
   second half of the pitch, the result of the drawn pitch.
5. [simulation/sampler/fielding.py](../../simulation/sampler/fielding.py) and
   [simulation/sampler/fence.py](../../simulation/sampler/fence.py): the batted-ball
   draw (`battedball_draw`) and the check of each air ball against the live park's
   fence.
6. [simulation/sampler/steal.py](../../simulation/sampler/steal.py): `pickoff_draw` and
   `steal_draw`.
7. [simulation/sampler/advancement.py](../../simulation/sampler/advancement.py): the
   extra-base decisions.
8. [simulation/sampler/manager.py](../../simulation/sampler/manager.py):
   `pitching_change_draw` and `relief_arm_draw`.
9. [simulation/synthetic_bundle.py](../../simulation/synthetic_bundle.py): the
   in-memory bundle the tests draw from. It shows the shape of every pool in a few
   hundred lines.

**Check.**

- Does each draw filter only on hard facts of the situation (the count, the outs, the
  bases, the batter's hand, the class of the pitch), and weight only by similarity?
- Does a cell with few rows still answer from its own rows, with no hidden fallback?
- Does each weight follow the order the owner set for the pitch draws: pitcher first,
  then batter, then recency, then catcher receiving last?
- Are the weights that are built but OFF really off in production? The cheat sheet's
  summary table lists them.
- Does every power read 1 today (owner ruling 2026-09-16), so the sampler skips the
  power step?

**Tests.** `test_pitch_cell_index.py`, `test_pitch_result_split.py`,
`test_fielding_draw.py`, `test_fence_stage.py`, `test_fence_carry_and_margin.py`,
`test_steal_draw.py`, `test_transition_draw.py`, `test_manager_draw.py`,
`test_relief_draw.py`, `test_draw_conditioning.py`, `test_synthetic_bundle.py`.

**Reference.** [Simulation](simulation.md) and the
[loop cheat sheet](sim-loop-cheat-sheet.md).

<a id="stage-4"></a>

## 4. The sampler's data: the bundle

**What it does.** The sampler never opens a database. It reads a folder of arrays on
disk: the engine-artifact bundle. A nightly job builds the bundle from DuckDB, writes
it as a new version, and moves a link to it in one step. The app pins the version it
booted with and restarts itself when the link moves.

**Read in this order.**

1. [pipeline/batch/engine_artifacts.py](../../pipeline/batch/engine_artifacts.py): the
   builder's command line, `EngineArtifacts` and `EngineArtifacts.load`.
2. [pipeline/batch/artifacts/](../../pipeline/batch/artifacts/): one module per
   artifact. Start with `common.py` and `pools.py`, then the pool you care about
   (`pitch_pool.py`, `battedball_pool.py`, `steal_pool.py`, ...).
3. [pipeline/batch/bundle_versions.py](../../pipeline/batch/bundle_versions.py): the
   versioned link, the publish step and the pruning.
4. [pipeline/batch/pool_chain.py](../../pipeline/batch/pool_chain.py): the check that
   the pool's labels agree with real plate appearances.

**Check.**

- Does every array the sampler reads have one writer here, with a test that reads it
  back?
- Does the bundle load without pickle (`allow_pickle=False`)?
- Can a failed build leave the link pointing at a half-written version? It must not.

**Tests.** `test_bundle_versions.py`, `test_bundle_no_pickle.py`,
`test_bundle_worker_and_boot.py`, `test_engine_artifacts_realism.py`,
`test_actor_matrices.py`, `test_pool_chain.py`.

**Reference.** [Pipeline: the nightly builds](pipeline-batch.md).

<a id="stage-5"></a>

## 5. The similarity scores

**What it does.** Eleven engines score how alike two players, or two situations, are.
Each score runs from 0 to 1. The nightly build turns the scores of the actors a draw
uses into matrices in the bundle; the sampler raises a score to a power and uses it as
a weight.

**Read in this order.**

1. [similarity/engines/rbf_kernel.py](../../similarity/engines/rbf_kernel.py): the
   weighted bell-curve similarity that six engines share.
2. [similarity/engines/pitcher_similarity.py](../../similarity/engines/pitcher_similarity.py)
   and [arsenal_cache.py](../../similarity/engines/arsenal_cache.py): the pitcher
   engine, which compares two pitchers' whole arsenals.
3. The other engines, in the order the draws use them: batter, fielder, catcher, steal
   runner, pitcher against the run, advancement runner, manager, pitch-to-pitch. The
   situation and batted-ball engines feed no draw today.
4. [similarity/similarity_calibration.py](../../similarity/similarity_calibration.py)
   and [similarity/calibration/](../../similarity/calibration/): the fit behind
   `make calibrate`.
5. [similarity/backtesting/](../../similarity/backtesting/): the backtester.

**Check.**

- Is every score bounded in [0, 1], symmetric, and equal to 1 for a player compared
  with himself?
- Does each engine shrink a thin profile toward the league or position average?
- Does the pitcher engine use the linear form `exp(-W2 / 4.10)`, not the squared form?
  (W2 is the distance between two pitchers' arsenals.)

**Tests.** `tests/regression/test_engine_regression.py` (the invariants above),
`test_pitcher_similarity.py`, `test_batter_similarity.py`, `test_fielder_similarity.py`,
`test_weighted_rbf_kernel.py`, `test_actor_powers.py`.

**Reference.** [Similarity engines](similarity.md).

<a id="stage-6"></a>

## 6. Profiles and pools: the DuckDB layer

**What it does.** The profile computor reads the raw pitches and writes the player
profiles (`derived.*`) and the pools of real plays (`sim.*`) to the DuckDB file. The
bundle of stage 4 is built from these tables.

**Read in this order.**

1. [pipeline/batch/player_profile_computor.py](../../pipeline/batch/player_profile_computor.py):
   the command line and the order of the steps.
2. [pipeline/batch/profiles/](../../pipeline/batch/profiles/): one module per profile
   (pitcher with its arsenal model in `gmm.py`, batter, fielder, catcher, baserunner,
   manager, park), then `pools.py` and `pool_meta.py` for the pools.
3. [db/schemas/02_duckdb_schema.sql](../../db/schemas/02_duckdb_schema.sql) and the
   numbered files under [db/migrations/duckdb/](../../db/migrations/duckdb/).

**Check.**

- Does every profile table carry one cutoff date (`asof_date`), so no row sees data
  from after the game it describes?
- Does a partial rebuild refuse to leave a table stamped at two dates?
- Does each pool row carry the facts the sampler filters on?

**Tests.** `test_player_profile_computor.py`, `test_one_cutoff_per_table.py`,
`test_point_in_time_batter.py`, `test_pool_cutoff.py`,
`test_pool_recency_and_incremental_rebuild.py`, `test_league_average_profiles.py`.

**Reference.** [Pipeline: the nightly builds](pipeline-batch.md) and
[Database](database.md).

<a id="stage-7"></a>

## 7. Getting data in: the raw tables

**What it does.** The loaders fetch each game from the MLB Stats API, and the
leaderboards from Baseball Savant, and write them to the Postgres `raw.*` tables.

**Read in this order.**

1. [db/schemas/01_postgres_schema.sql](../../db/schemas/01_postgres_schema.sql): the
   raw tables.
2. [pipeline/etl/etl_historical_loader.py](../../pipeline/etl/etl_historical_loader.py)
   and [pipeline/etl/historical/](../../pipeline/etl/historical/): one game, from the
   feed to the rows.
3. [pipeline/statcast_events.py](../../pipeline/statcast_events.py): the event and
   pitch-result code sets.
4. [pipeline/etl/play_events.py](../../pipeline/etl/play_events.py) and
   [pipeline/etl/boxscore_ingest.py](../../pipeline/etl/boxscore_ingest.py): the
   non-pitch events and the official box score.
5. The Savant and sprint-speed loaders, then
   [db/migrations/versions/](../../db/migrations/versions/) (Alembic).

**Check.**

- Is the out on a play read from the events, not from a column that lags by one play?
- Is each starter's position the one he started at, not the one he ended at?
- Does a reload of one game replace its rows cleanly (the loaders are idempotent per
  game)?

**Tests.** `test_etl_historical_loader.py`, `test_out_label.py`, `test_play_events.py`,
`test_boxscore_ingest.py`, `test_starting_position.py`, `test_etl_reload_game.py`,
`test_migration_chain.py`.

**Reference.** [Pipeline: getting data in](pipeline-ingest.md) and
[Database](database.md).

<a id="stage-8"></a>

## 8. Running many games and reading the results

**What it does.** The runner plays 100 games on a pool of worker processes. The output
code turns the games into win probabilities, a distribution for each player statistic
(the props) and a price for each game market.

**Read in this order.**

1. [simulation/sim_kwargs.py](../../simulation/sim_kwargs.py) and
   [simulation/lineup_resolver.py](../../simulation/lineup_resolver.py): what a game
   needs, read from the database.
2. [simulation/production_factory.py](../../simulation/production_factory.py) and
   [simulation/batch_runner.py](../../simulation/batch_runner.py): the per-worker
   sampler and the worker pool.
3. [simulation/results.py](../../simulation/results.py),
   [simulation/prop_distributions.py](../../simulation/prop_distributions.py),
   [simulation/game_market_distributions.py](../../simulation/game_market_distributions.py)
   and [simulation/win_probability.py](../../simulation/win_probability.py).
4. [simulation/whatif.py](../../simulation/whatif.py),
   [simulation/play_recorder.py](../../simulation/play_recorder.py) and
   [db/sim_store.py](../../db/sim_store.py): the "what if" changes and the stored
   replay of one simulated game.

**Check.**

- Does a seed reproduce the same 100 games, whatever the number of workers?
- Does a worker read the bundle version the app booted with?
- Does every number that reaches a user pass through calibration?

**Tests.** `test_batch_runner.py`, `test_worker_forkserver.py`, `test_lineup_resolver.py`,
`test_game_sim_summary.py`, `test_prop_distributions.py`, `test_segment_markets.py`,
`test_win_probability.py`, `test_game_page_whatif.py`, `test_replay_store.py`.

**Reference.** [Simulation](simulation.md).

<a id="stage-9"></a>

## 9. Odds and betting

**What it does.** The odds providers fetch the bookmakers' prices and store one row per
book. The betting code removes the bookmaker's margin from a price, compares the fair
price with the model's probability, and reports the edge and the expected value.

**Read in this order.**

1. [pipeline/odds_provider.py](../../pipeline/odds_provider.py): the market vocabulary
   and the provider interface.
2. [pipeline/bettingpros_odds_provider.py](../../pipeline/bettingpros_odds_provider.py)
   and [pipeline/bettingpros/](../../pipeline/bettingpros/): the vendor's provider.
3. [betting/clv_engine.py](../../betting/clv_engine.py): implied probability, margin
   removal, edge, expected value and closing line value.
4. [betting/bet_signal.py](../../betting/bet_signal.py) and
   [betting/line_movement.py](../../betting/line_movement.py).

**Check.**

- Does every stored price name its book, and does each reader pick one book?
- Is the margin removed before an edge is computed?
- Does any number reach a bet signal before it is calibrated? (Owner ruling
  2026-09-08: no betting-value measurement until every graded band is green.)

**Tests.** `test_clv_engine.py`, `test_clv_engine_run_line.py`, `test_bet_signal.py`,
`test_odds_one_book_per_row.py`, `test_odds_readers.py`, `test_bettingpros_provider.py`.

**Reference.** [Betting](betting.md) and [Pipeline: getting data in](pipeline-ingest.md).

<a id="stage-10"></a>

## 10. The API

**What it does.** The FastAPI app boots, builds the similarity engines, starts the
worker pool and serves the routes the frontend calls. Every route requires a login.

**Read in this order.**

1. [api/main.py](../../api/main.py) and [api/startup.py](../../api/startup.py): the boot
   order.
2. [api/auth.py](../../api/auth.py): the login.
3. [api/state.py](../../api/state.py), [api/sim_jobs.py](../../api/sim_jobs.py) and
   [api/bundle_watch.py](../../api/bundle_watch.py): what the app holds while it runs.
4. [api/routes/games.py](../../api/routes/games.py) and its siblings `games_*.py`, then
   [api/routes/betting.py](../../api/routes/betting.py) and its siblings `betting_*.py`.
5. [api/schemas/](../../api/schemas/): the response shapes.
6. The Data Lab routes (`sql_*`, `analytics`, `players`, `similarity*`).

**Check.**

- Does every route require a login? A test walks the whole route table.
- Does the SQL console run only on the read-only database pool?
- Does a change to a route ship a fresh API snapshot (`frontend/openapi.json`)?

**Tests.** `test_route_auth_coverage.py`, `test_api_auth.py`, `test_api_games.py`,
`test_api_betting.py`, `test_sim_jobs.py`, `test_sql_safety.py`,
`test_read_only_routes.py`, `test_openapi_snapshot.py`.

**Reference.** [API](api.md).

<a id="stage-11"></a>

## 11. Game day: the live service

**What it does.** The `live` service follows today's games. It treats the MLB WebSocket
as a signal that something changed and re-fetches the state over REST. It writes each
posted lineup, so a game can be simulated before first pitch, and pushes updates
through Redis.

**Read in this order.**

1. [pipeline/mlb_schedule.py](../../pipeline/mlb_schedule.py),
   [pipeline/mlb_game_feed.py](../../pipeline/mlb_game_feed.py) and
   [api/league_feed.py](../../api/league_feed.py): the schedule and the game feed.
2. [pipeline/live/run_live.py](../../pipeline/live/run_live.py) and
   [pipeline/live/live_ingestion_pipeline.py](../../pipeline/live/live_ingestion_pipeline.py):
   the service and its loop.
3. [pipeline/live/ingestion/](../../pipeline/live/ingestion/): the WebSocket, the game
   state builder and the odds cycles.
4. [pipeline/live/lineup_writer.py](../../pipeline/live/lineup_writer.py) and
   [pipeline/live/broadcast.py](../../pipeline/live/broadcast.py).

**Check.**

- Does the service rebuild its state from REST after every signal, never from the
  WebSocket message itself?
- Does a game that started with no stored lineup get its lineup from the feed?

**Tests.** `test_schedule_client.py`, `test_game_feed.py`, `test_live_pipeline.py`,
`test_published_lineup_writer.py`, `test_live_bridge.py`.

**Reference.** [Pipeline: getting data in](pipeline-ingest.md).

<a id="stage-12"></a>

## 12. The frontend

**What it does.** The React app shows the day's slate, the game page (the real game,
the simulation, the props, the prices and the "what if" panel), the Data Lab and the
similarity explorer.

**Read in this order.**

1. [frontend/src/main.tsx](../../frontend/src/main.tsx) and
   [frontend/src/App.tsx](../../frontend/src/App.tsx): the entry point and the routes.
2. [frontend/src/api/](../../frontend/src/api/): the API client. `schema.d.ts` is
   generated from the API snapshot, but the clients declare their response shapes by
   hand: no module imports `typed.ts`.
3. [DaySummaryPage.tsx](../../frontend/src/pages/DaySummaryPage.tsx) and the slate
   components, then [GamePage.tsx](../../frontend/src/pages/GamePage.tsx) and the game
   components.
4. The Data Lab pages and the similarity pages.

**Check.**

- Does each hand-written response shape in `frontend/src/api/` match the API snapshot?
  Nothing checks this automatically, so a contract change can drift unseen.
- Does the page show a clear state for loading, empty and error?

**Tests.** The Playwright specs under [frontend/e2e/](../../frontend/e2e/), and the CI
frontend job (lint, type check, build).

**Reference.** [Frontend](frontend.md).

<a id="stage-13"></a>

## 13. Operations: the stack, the nightly jobs, the backups

**What it does.** Docker Compose runs the services. A scheduler runs the nightly data
jobs inside the `jobs` container. Three stores back up every night, and a copy goes
off the machine.

**Read in this order.**

1. [docker-compose.yml](../../docker-compose.yml), [Dockerfile](../../Dockerfile) and
   [Makefile](../../Makefile).
2. [deploy/ofelia/config.ini](../../deploy/ofelia/config.ini): the schedule. Then the
   shell jobs it runs (`scripts/nightly_*.sh`) and their wrappers
   (`with_mark.sh`, `with_retry.sh`, `wait_for_ingest.sh`).
3. [pipeline/backup/](../../pipeline/backup/) and [db/backup/](../../db/backup/).
4. [db/roles/](../../db/roles/) and [deploy/](../../deploy/).

**Check.**

- Does every job wait for the jobs it depends on, and leave a mark while it writes?
- Does a backup set count as complete only once its manifest lands?
- Does no password have a default anywhere?

**Tests.** `test_backup_data_stores.py`, `test_backup_offsite.py`,
`test_backup_schedule.py`, `test_retry_wrapper.py`, `test_ingest_wait.py`,
`test_no_default_password.py`, `test_nginx_resolve.py`.

**Reference.** [Architecture](architecture.md), the operator's manual
([WORKFLOW.md](../../WORKFLOW.md)) and [deploy/README.md](../../deploy/README.md).

<a id="stage-14"></a>

## 14. Measurement and maintenance tools

**What it does.** The scripts measure the simulator against real play, rebuild the
pools, and backfill data. Most of them are run by hand.

**Read in this order.**

1. [scripts/sim_stats.py](../../scripts/sim_stats.py): the short smoke run of the
   simulator, and the gate it applies.
2. [scripts/balanced_game_set.py](../../scripts/balanced_game_set.py): the 45 games the
   acceptance lane grades on.
3. [scripts/accuracy_comparison.py](../../scripts/accuracy_comparison.py) with
   [scripts/accuracy/](../../scripts/accuracy/), and
   [scripts/pair_accuracy.py](../../scripts/pair_accuracy.py): the model against the
   closing line.
4. [scripts/designed_experiment.py](../../scripts/designed_experiment.py): the one fit
   of every weight together.
5. The pool rebuilds, the loaders and the probes, as you need them.

**Check.**

- Does each tool say what it measures, on how many games, and what a pass looks like?
- Does a rebuild refuse to run against a table it cannot handle, before it deletes?

**Tests.** `test_accuracy_comparison.py`, `test_pair_accuracy.py`,
`test_designed_experiment.py`, `test_smoke_outs_gate.py`, `test_steal_pool_rebuild.py`,
`test_fence_check.py`.

**Reference.** [Scripts](scripts.md).

<a id="stage-15"></a>

## 15. Tests and CI

**What it does.** The tests run in lanes: unit, regression invariants, acceptance,
integration, performance and the frontend's end-to-end specs. CI runs them on every
push.

**Read in this order.**

1. [tests/conftest.py](../../tests/conftest.py): the switches the unit lane pins, and
   the shared fixtures.
2. [tests/acceptance/bands.py](../../tests/acceptance/bands.py): the bands the
   simulator's frequencies must fall in, against the pool's own totals.
3. [tests/regression/test_engine_regression.py](../../tests/regression/test_engine_regression.py):
   the engine invariants.
4. [.github/workflows/ci.yml](../../.github/workflows/ci.yml): which job runs which lane.

**Check.**

- Does each lane prove what its name says?
- Does a test that reads a document or a source file say why?

**Reference.** [Tests and CI](tests.md).
