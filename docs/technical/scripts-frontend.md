# Operational Scripts and Frontend

The scripts that are still run operationally (not one-off probes), plus a map of the frontend structure.

*18 files documented — use the page outline (right sidebar) to jump to one.*

### `scripts/nightly_ingest.sh`

Nightly data-ingestion chain: loads newly-Final games for the current season, rebuilds the DuckDB derived profiles and sim pools, then rebuilds the engine-artifact bundle the production simulator reads. Runs as a fresh container off the app image, scheduled by the Ofelia cron service.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `main body (POSIX sh, no functions)` | Runs 3 steps in order: (1) a Python heredoc calling HistoricalDataLoader.refresh_seasons(YEAR, YEAR), exiting non-zero if any game failed, (2) player_profile_computor --seasons YEAR, (3) engine_artifacts --what all. | `deploy/ofelia/config.ini job-run nightly-ingest, cron 0 0 7 * * * UTC, via the docker-compose scheduler service/profile`<br>`docker-compose.yml documents the chain`<br>`an operator running it by hand: docker compose run --rm app sh /app/scripts/nightly_ingest.sh` | `pipeline.etl.etl_historical_loader.HistoricalDataLoader.refresh_seasons`<br>`pipeline.batch.player_profile_computor (module main)`<br>`pipeline.batch.engine_artifacts (module main)` |

**Depends on:** `pipeline/etl/etl_historical_loader.py`, `pipeline/batch/player_profile_computor.py`, `pipeline/batch/engine_artifacts.py`

**Used by:** `deploy/ofelia/config.ini`, `docker-compose.yml scheduler service`

**Environment flags read here:** `BASEBALL_DB_DSN (defaults to the in-container db:5432 DSN)`, `BASEBALL_DUCKDB_PATH (defaults to /data/baseball_sim.duckdb)`

> **Notes for anyone changing this file:** set -eu means step 1 must exit non-zero on any per-game load failure, or the chain would rebuild profiles/artifacts on an incomplete season -- this is intentional (SIM-441 comment). The heredoc delimiter is quoted (PY in single quotes) on purpose so ${YEAR} is NOT shell-interpolated into the Python code; the year instead crosses the boundary through os.environ['YEAR']. This script is not itself resumable the way resumable_sweep.py is -- a mid-chain crash needs a manual re-run of the remaining steps.

---

### `scripts/fit_calibration.py`

Fits a CalibrationReport (per-engine RBF and arsenal sigmas) over the real DuckDB player profiles and writes it to /data/calibration.json, which the API loads at boot to calibrate every similarity engine's score curve. Wired to make calibrate.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_seasons(duckdb_path, requested)` | Returns explicit --seasons, else every distinct season found in derived.batter_season_metrics, else a hardcoded default list. | — | — |
| `sample_arsenal_distances(duckdb_path, seasons, n_pairs, seed)` | Builds the pitcher similarity engine and samples random same-hand pairwise arsenal W2 (Wasserstein-2) distances to anchor the ARSENAL_SCALE constant, avoiding the O(N^2) full pairwise cache. | — | `similarity.engines.pitcher_similarity.PitcherSimilarityEngine.build`<br>`similarity.engines.pitcher_similarity.ArsenalSimilarity.distance` |
| `validate_engine_medians(duckdb_path, seasons, report)` | Best-effort sanity check: builds the batter engine, applies the fitted report, and logs whether the median similarity over a random query sample lands near the 0.50 target. | — | `similarity.engines.batter_similarity.BatterSimilarityEngine build/apply_calibration/query` |
| `main(argv)` | CLI entry: fits the report via SimilarityCalibrator.calibrate_from_population, writes CalibrationReport.to_json() to --output, and optionally runs the median validation. | `Makefile make calibrate target -> docker compose run --rm app python scripts/fit_calibration.py $(FLAGS)` | `similarity.similarity_calibration.SimilarityCalibrator.calibrate_from_population`<br>`similarity.similarity_calibration.CalibrationReport.to_json/summary` |

**Depends on:** `similarity/similarity_calibration.py (CalibrationReport, SimilarityCalibrator)`, `similarity/engines/pitcher_similarity.py`, `similarity/engines/batter_similarity.py`, `derived.batter_season_metrics table (DuckDB)`

**Used by:** `api boot/lifespan, which reads the written /data/calibration.json to apply calibration to all 8 similarity-score engines and derive the win-prob CalibrationMap`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH (default /data/baseball_sim.duckdb)`, `CALIBRATION_REPORT_PATH (default /data/calibration.json)`

> **Notes for anyone changing this file:** Must run AFTER profile-computor (it reads derived.* season-metrics tables). The arsenal W2 sampling step is the slow part (builds the full pitcher engine); skip it with --no-arsenal to keep the locked 4.10 default. This is SIM-406/432, the calibration/schema-reconciliation work; per CLAUDE.md it has been live in production since 2026-06-01.

---

### `scripts/validate_props.py`

Offline job (SIM-407) that replays real completed games through the production simulator N times, compares the simulator's win probabilities and per-player prop PMFs against what actually happened, and writes a PropValidationReport. With --write-calibration it fits the win-probability reliability curve back into the CalibrationReport used at API boot.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_fetch_final_games(dsn, seasons, max_games)` | Reads completed (Final) games and their real scores from raw.games (home_score_final/away_score_final). | — | — |
| `_fetch_pa_events(pool, game_pk)` | Reads one (batter, pitcher, events) row per completed plate appearance from raw.pitches -- the real prop ground truth. | — | — |
| `_collect_game_results(state, n_iter, base_seed)` | Replays one resolved game N times via record_game_plays under the production factory, collecting one GameSimResult per iteration; run via asyncio.to_thread since it is sync/CPU-bound. | `run()` | `api.routes.games._sim_kwargs_from_state`<br>`simulation.batch_runner.derive_seed`<br>`simulation.play_recorder.record_game_plays` |
| `run(args)` | Orchestrates the whole validation: checks the park-factor DuckDB source (refuses to run park-blind unless --allow-neutral-parks), resolves each game's state and park factor, replays it, builds win-prob and prop PMFs, pairs them against real outcomes, and writes the report. | `Makefile make validate-props target` | `api.routes.games._resolve_state_or_error`<br>`simulation.sim_kwargs.open_sim_duckdb/resolve_park_factor_onto_state`<br>`simulation.win_probability.win_probability`<br>`simulation.prop_distributions.PropDistributionSet.from_results`<br>`simulation.prop_validation.build_validation_report/pair_props_for_validation/real_props_from_pa_events/write_reliability_curve_to_calibration_report` |

**Depends on:** `simulation/prop_distributions.py`, `simulation/prop_validation.py`, `simulation/sim_kwargs.py`, `simulation/win_probability.py`, `simulation/play_recorder.py`, `simulation/batch_runner.py`, `api/routes/games.py (_resolve_state_or_error, _sim_kwargs_from_state)`, `raw.games / raw.pitches tables`

**Used by:** `writes /data/prop_validation.json; with --write-calibration also updates /data/calibration.json, which the API re-reads at its next boot`

**Environment flags read here:** `BASEBALL_DB_DSN`, `CALIBRATION_REPORT_PATH`, `PROP_VALIDATION_PATH`, `BASEBALL_DUCKDB_PATH`

> **Notes for anyone changing this file:** Only H/HR/TB (batter) and K/BB (pitcher) props are validated -- RBI/ER/OUTS are intentionally NOT derived because raw.pitches.events cannot recover them exactly, and doing so would corrupt the calibration (see the module docstring). SIM-452 precondition: the job exits with code 2 if the sim DuckDB (which holds the venue park factors) will not open, unless --allow-neutral-parks is passed; this exists because an earlier version silently fit a park-blind reliability curve.

---

### `scripts/load_historical_odds.py`

Offline backfill (SIM-435) that fetches OPENING and CLOSING betting lines (game moneyline/runline/total plus 7 player props) for every completed game and writes them into raw.game_odds / raw.prop_odds, so the CLV backtest has entry and closing lines to score against.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_fetch_final_games / _fetch_lineup_players` | Reads Final games and, per game, the (player_id, is_pitcher) pairs from raw.game_lineups to route pitcher vs batter prop markets. | — | — |
| `_load_game_odds(provider, persist, game_pk)` | Fetches and persists opening/closing prices for moneyline/runline/total for one game. | — | `pipeline.odds_provider.get_odds_provider(...).get_odds` |
| `_load_prop_odds(provider, persist, game_pk, players)` | Fetches and persists opening/closing prop lines (strikeouts/earned_runs/walks for pitchers; hits/home_runs/total_bases/rbis for batters) per player. | — | `pipeline.odds_provider.get_odds_provider(...).get_prop_odds` |
| `_build_persisters(dsn, pool)` | Constructs a LiveIngestionPipeline WITHOUT starting it, attaches its own asyncpg pool, and returns its _persist_odds/_persist_prop_odds bound methods so writes reuse the exact odds_hash dedup and ON-CONFLICT write path the live pipeline uses. | — | `pipeline.live.live_ingestion_pipeline.LiveIngestionPipeline._persist_odds`<br>`pipeline.live.live_ingestion_pipeline.LiveIngestionPipeline._persist_prop_odds` |
| `run(args)` | Top-level orchestration: fetch games, fetch/persist game odds, fetch lineup then fetch/persist prop odds per game, log totals. | `Makefile make load-historical-odds target` | — |

**Depends on:** `pipeline/odds_provider.py (get_odds_provider)`, `pipeline/bettingpros_odds_provider.py`, `pipeline/live/live_ingestion_pipeline.py`, `raw.games / raw.game_lineups / raw.game_odds / raw.prop_odds tables`

**Used by:** `scripts/clv_backtest.py reads the raw.game_odds / raw.prop_odds rows this script writes`

**Environment flags read here:** `BASEBALL_DB_DSN`, `REDIS_URL (a placeholder value only -- start() is never called, so Redis is never touched)`, `ODDS_PROVIDER (read inside pipeline.odds_provider.get_odds_provider)`, `ODDS_API_KEY (read by the BettingPros provider)`

> **Notes for anyone changing this file:** Re-runs are idempotent via the odds_hash ON CONFLICT DO NOTHING dedup shared with the live pipeline. With ODDS_PROVIDER unset it falls back to the deterministic MockOddsAPI, useful for a no-network wiring smoke test. Network-bound (one provider request per game/market), so cap smoke runs with --max-games.

---

### `scripts/clv_backtest.py`

The SIM-429 CLV (Closing Line Value) backtest scoreboard, the platform's gold-standard validation. For a slate of completed games with ingested odds, it runs N-iteration sims, picks the +EV side at the opening line for each market, and measures whether that pick's implied win probability improved by the closing line (beat_close_rate). Runs across-games in parallel via a forkserver ProcessPoolExecutor for season-scale throughput.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `evaluate_two_way_market(...)` | PURE: builds both sides' EdgeReports on the opening line, picks the larger positive-edge side above min_edge, and scores its entry-to-close CLV. No DB, sim, or RNG involved. | `score_game_markets`<br>`score_prop_markets`<br>`tests/unit/test_clv_backtest.py` | `betting.clv_engine.clv_from_odds` |
| `aggregate_scoreboard(bets)` | PURE: rolls a list of BetRecord into an overall row plus one row per market key, sorted by trust tier, each carrying beat_close_rate / mean_clv_prob / mean_model_edge. | `run()`<br>`tests/unit/test_clv_backtest.py` | — |
| `score_game_markets(game_pk, win_prob, summary, odds, min_edge)` | Scores moneyline/total/runline for one game by wiring the sim output into evaluate_two_way_market via per-market edge-report closures. | — | `betting.clv_engine.moneyline_edge_report/total_over_under_edge_report/run_line_edge_report` |
| `score_prop_markets(game_pk, pset, prop_odds, min_edge)` | Scores every player-prop market (mapped through PROP_VOCAB_MAP) against the model's PropDistribution for that player/stat. | — | `betting.clv_engine.prop_edge_report`<br>`simulation.prop_distributions.PropDistributionSet.get` |
| `_score_one_game(pool, game_pk, ...)` | The entire per-game pipeline shared by BOTH the serial and parallel execution paths: read odds, resolve GameState, resolve park factor, replay iterations, build WinProbability and PropDistributionSet, score markets. | `_run_serial`<br>`_process_one_game` | `api.routes.games._resolve_state_or_error`<br>`simulation.sim_kwargs.resolve_park_factor_onto_state`<br>`simulation.win_probability.win_probability`<br>`simulation.results.GameSimSummary.from_results`<br>`_collect_game_results`<br>`score_game_markets`<br>`score_prop_markets` |
| `_process_one_game(game_pk, params)` | Module-level, picklable across-process worker function mapped over the ProcessPoolExecutor; lazily inits one event loop, one asyncpg pool and one DuckDB connection per forkserver worker (amortized), then drives _score_one_game. | `_run_parallel via ProcessPoolExecutor.submit` | `_worker_lazy_init`<br>`_score_one_game` |
| `run(args)` | Top-level orchestration: opens the parent's sim DuckDB (the SIM-452 park-factor precondition), fetches the game list, dispatches to _run_serial or _run_parallel, aggregates the scoreboard, prints it, and writes the JSON report with a documented non-zero exit code for every abandoned-run class (SIM-454). | `main()` | — |

**Depends on:** `betting/clv_engine.py (EdgeReport, MarketSide, OddsQuote, TwoWayMarket, clv_from_odds, moneyline_edge_report, prop_edge_report, run_line_edge_report, total_over_under_edge_report)`, `simulation/sim_kwargs.py (open_sim_duckdb, resolve_park_factor_onto_state, UnresolvedParkFactorError)`, `simulation/win_probability.py`, `simulation/results.py (GameSimSummary)`, `simulation/prop_distributions.py`, `simulation/play_recorder.py (record_game_plays)`, `simulation/batch_runner.py (derive_seed)`, `simulation/production_factory.py (warm_worker_cache)`, `api/routes/games.py (_resolve_state_or_error, _sim_kwargs_from_state)`, `raw.games / raw.game_odds / raw.prop_odds tables`

**Used by:** `operator CLI only; no other module imports this script. tests/unit/test_clv_backtest.py loads the pure functions by file path via importlib since scripts/ is not a Python package`

**Environment flags read here:** `BASEBALL_DB_DSN`, `BASEBALL_DUCKDB_PATH`, `CLV_BACKTEST_PATH`, `SIM_MP_START_METHOD (default forkserver -- the multiprocessing start method for the across-games pool)`

> **Notes for anyone changing this file:** Per CLAUDE.md this is the CLV re-measure gate: the owner ruling (2026-09-08/09) says no betting-value measurement runs until every acceptance band is green -- as of 2026-09-10 that gate is not fully open yet (the pitch/pitch-result split's strikeout shortfall, SIM-527, is still red). The last completed CLV read (about 49 percent beat-close, from before the 2026-08 data rebuild) is stale and must be re-run once the gate clears. Exit codes are meaningful and documented (EXIT_OK=0 through EXIT_INFRASTRUCTURE=6): a run that scores 0 games always exits non-zero (EXIT_NOTHING_SCORED=5) rather than looking like a healthy empty run, a deliberate SIM-454 fix for a prior silent-no-op bug class. --workers 1 is the serial fallback and byte-identical verification reference; --workers 6 (default) is about 6x throughput via one whole game per forkserver worker (about 373 MB cache each).

---

### `scripts/sim_stats.py`

Scaled Monte-Carlo box-score harness (SIM-429 follow-on, v2) that runs N iterations per game through the production simulator and reports per-channel statistics (R/H/HR/2B/3B/BB/K/SB/CS, home-vs-away splits, DP rate) against the project's own MLB-2025 baseline, so a calibration sweep can target the actual residual channel instead of guessing.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_resolve(game_pk, duck)` | Resolves the GameState and fills in the venue park factor plus venue id (the harness must do this itself, the same way the API endpoint does). | — | `simulation.lineup_resolver.resolve_game_state`<br>`simulation.sim_kwargs.resolve_park_run_factor/resolve_venue_id` |
| `_game_summary(result, home_ids, away_ids)` | Aggregates one iteration's boxscore into both-teams totals plus home/away splits (for the SIM-412 home-field-bias validation). | — | — |
| `_aggregate(per_game)` | Rolls up every game's per-iteration summaries into per-channel means plus the R standard error, the precision read that says whether the iteration count is enough. | — | — |
| `main()` | CLI entry: prints every realism env flag's current value, resolves each game, runs --iters sims per game via simulate_game, prints the per-channel report against the MLB-2025 baseline, and optionally writes a JSON dump. | — | `simulation.production_factory.production_machine_factory`<br>`simulation.sim_loop.simulate_game`<br>`simulation.sim_kwargs.sim_kwargs_from_state`<br>`simulation.batch_runner.GameSpec` |

**Depends on:** `simulation/batch_runner.py (GameSpec)`, `simulation/lineup_resolver.py`, `simulation/production_factory.py`, `simulation/sim_kwargs.py`, `simulation/sim_loop.py (BoxScore, simulate_game)`

**Used by:** `operator CLI only -- used ad hoc during calibration and realism sweeps, referenced throughout docs/audit/*.md as the standard measurement tool`

**Environment flags read here:** `BASEBALL_DB_DSN`, `SIM_MANAGER`, `SIM_BB_PLATOON`, `SIM_HOME_OFF_WEIGHT`, `SIM_PARK_KERNEL_SIGMA`, `SIM_CATCHER_RECEIVING`, `SIM_GOT_AWAY`, `SIM_PITCH_RESULT_SPLIT`, `SIM_PITCH_PITCHER_POWER`, `SIM_RESULT_PITCHER_POWER`, `SIM_RESULT_BATTER_POWER`, `SIM_BB_BORN_SIGMA`, `SIM_BB_CLASS_FILTER`, `SIM_PARK_WALL_ZONE_ONLY`, `SIM_FENCE_STAGE`, `SIM_MANAGER_DRAW`, `SIM_PARK_FACTOR (referenced in the module docstring's usage example)`

> **Notes for anyone changing this file:** This is v2, replacing an earlier 4-game x 25-iter harness that was too noisy (about +/-0.2 R variance) to read per-channel calibration moves cleanly; v2 defaults to 200 iters per game. It deliberately prints every realism flag plus the resolved park factor and defense-map sizes so a neutral no-op configuration can never silently read as a measured 'no effect' (the SIM-449 concern). The MLB-2025 baseline (_MLB_2025 dict, _MLB_HOME_WIN_PCT) is this project's own ingested 2025 season (2,430 games), per the SIM-508 owner decision, not an external source.

---

### `scripts/check_file_integrity.py`

Pure-stdlib pre-commit hook and CI guard (SIM-315) that scans .py files for two classes of file-bridge corruption -- embedded NUL bytes and truncated/un-parseable source (caught via ast.parse) -- so a silently-corrupted file is caught before it is committed.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `iter_python_files(paths)` | Yields .py files under the given file/directory arguments, pruning EXCLUDED_DIRS (.git, __pycache__, venvs, caches, node_modules, build/dist). | — | — |
| `check_file(path)` | Returns an Offender if the file contains a NUL byte, fails UTF-8 decoding, or fails ast.parse (the truncation signature). | — | — |
| `main(argv)` | Runs check_file over every discovered path, prints a FAIL/OK report, and returns exit code 1 if any offender was found. | `.pre-commit-config.yaml (entry: python scripts/check_file_integrity.py)`<br>`.github/workflows/ci.yml (the file-integrity CI job, around line 582)`<br>`tests/unit/test_check_file_integrity.py` | — |

**Depends on:** `Python stdlib only (ast, os, argparse) -- no project imports`

**Used by:** `.pre-commit-config.yaml`, `.github/workflows/ci.yml`

> **Notes for anyone changing this file:** With no path arguments it walks the whole repository; pre-commit and CI instead pass the explicit list of changed files. Guards against a real observed failure mode: the OneDrive/Cowork file bridge silently truncating or NUL-corrupting files mid-write, which can look like a plausible diff yet fail to import.

---

### `scripts/export_openapi.py`

SIM-420 -- exports the FastAPI app's OpenAPI schema to frontend/openapi.json, the snapshot the frontend's typed API client (openapi-typescript) is generated from.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `main()` | Imports api.main.app, calls app.openapi(), and writes pretty-printed sorted-key JSON to frontend/openapi.json. | `an operator running it by hand whenever the API contract changes; frontend/src/api/typed.ts's file header documents this as the required first step before npm run gen:api` | `api.main.app.openapi()` |

**Depends on:** `api/main.py (create_app / the FastAPI app instance)`

**Used by:** `frontend/openapi.json feeds npm run gen:api (openapi-typescript) which writes frontend/src/api/schema.d.ts, which frontend/src/api/typed.ts re-exports from`

> **Notes for anyone changing this file:** Needs no server or live DB -- create_app only registers routes. Must be followed by cd frontend && npm run gen:api and a commit of the regenerated schema.d.ts so the frontend's typed client never silently drifts from the backend contract.

---

### `scripts/rebuild_pools.py`

Lightweight operational tool that rebuilds ONLY sim.pitch_pool and sim.outcome_pool for the given seasons, skipping the expensive full engine-profile recompute -- used when only the play pools need to be re-materialized after a pool-affecting fix.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `module-level script body` | Connects a PlayerProfileComputor directly (bypassing its normal orchestration) and calls _build_pitch_pool(seasons, incremental=False) then _build_outcome_pool(seasons, incremental=False) for the seasons given on argv. | — | `pipeline.batch.player_profile_computor.PlayerProfileComputor._connect/_build_pitch_pool/_build_outcome_pool/_close` |

**Depends on:** `pipeline/batch/player_profile_computor.py`

**Used by:** `operator CLI only: python scripts/rebuild_pools.py [season ...] (default season 2026)`

**Environment flags read here:** `BASEBALL_DB_DSN (required, no default -- raises KeyError if unset)`, `BASEBALL_DUCKDB_PATH (default /data/baseball_sim.duckdb)`

> **Notes for anyone changing this file:** Bypasses the computor's normal connect-then-orchestrate flow by calling the private _build_pitch_pool/_build_outcome_pool methods directly with incremental=False, so it always fully replaces the named seasons' pool rows rather than incrementally updating them. Does not rebuild the engine-artifact bundle afterward -- a caller must separately run pipeline.batch.engine_artifacts --what pool to make the new pool rows visible to the production sampler.

---

### `scripts/resumable_sweep.py`

Drives the corrective ETL reload sweep to completion across a stochastic CPython interpreter crash (a since-mostly-fixed inline-cache corruption bug, SIM-445/446) by re-launching HistoricalDataLoader.refresh_seasons as a subprocess and recording every successfully-loaded game_pk to a progress file, so a crash only costs lost progress, not correctness.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_run_attempt(season, progress, native, quiet)` | Launches the sweep child as a subprocess (a fault kills the interpreter outright so it cannot be caught in-process), streams its output, and detects completion/failure markers. | — | `subprocess.Popen running the embedded _CHILD script, which calls pipeline.etl.etl_historical_loader.HistoricalDataLoader.refresh_seasons` |
| `main()` | Loops seasons and attempts, retrying after a crash or a partial-failure completion, stopping immediately if an attempt makes zero forward progress (a deterministic defect, not a transient fault), and reports total games loaded at the end. | `operator CLI: python scripts/resumable_sweep.py --season YEAR [--end-season Y2] [--max-attempts N] [--in-docker] [--quiet]` | — |

**Depends on:** `pipeline/etl/etl_historical_loader.py (HistoricalDataLoader.refresh_seasons, _dispatch_game)`, `.env file for the DSN and DB_HOST_PORT`, `.sweep_progress/<season>.txt progress files`

**Used by:** `operator CLI only -- the standard way to run a full multi-season re-sweep; CLAUDE.md records the 2026-08-13 SIM-488 re-sweep of 22,533 games as having used this tool`

**Environment flags read here:** `BASEBALL_DB_DSN (rewritten by the child to 127.0.0.1:DB_HOST_PORT unless --in-docker)`

> **Notes for anyone changing this file:** The wrapper is NOT the primary fix for the underlying crash -- SIM-446 (switching the ETL's HTTP transport to stdlib urllib) is -- but it is kept as a belt-and-braces safety net because the crash's root cause was never proven, only its trigger (the HTTP transport) was found by elimination. Correctness relies on reload_game's own delete-then-reinsert-in-one-transaction design plus its shrink guard, which make retries safe. Progress is recorded only when a game actually LOADED (the summary's loaded counter increased), never merely because _dispatch_game returned, since that method deliberately swallows per-game exceptions.

---

### `scripts/reload_games.py`

Corrective reload tool for specific games or every discovered ETL gap. Distinguishes three classes of incomplete load (STALE rows with no ledger entry, FAILED ledger rows for Final games, and correctly-empty NOT-PLAYED games) and reloads only the first two, since the third needs no fix.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_discover(dsn, season)` | Runs the STALE / FAILED / NEVER-LOADED / NOT-PLAYED SQL queries against Postgres and returns the deduplicated reload targets plus the not-played list. | — | — |
| `main()` | Either reloads explicit --game-pk values or discovered targets via HistoricalDataLoader.reload_game, reporting before/after row counts, then runs a per-season ledger-vs-pitches reconciliation. | — | `pipeline.etl.etl_historical_loader.HistoricalDataLoader.reload_game` |
| `_refresh_venues(dsn, dry_run)` | Re-fetches raw.venues rows whose venue_name is blank (a SIM-447 dimension-key typo) in place via _ensure_venue(force=True), since venue rows cannot be deleted and reloaded (raw.pitches holds a foreign key to them). | — | `pipeline.etl.etl_historical_loader.HistoricalDataLoader._ensure_venue` |

**Depends on:** `pipeline/etl/etl_historical_loader.py (HistoricalDataLoader.reload_game, _ensure_venue)`, `.env file`, `raw.pitches / raw.etl_game_ingest / raw.games / raw.venues tables`

**Used by:** `operator CLI only: python scripts/reload_games.py [--dry-run|--game-pk ...|--season Y|--allow-shrink|--refresh-venues]`

**Environment flags read here:** `BASEBALL_DB_DSN (read from .env, rewritten to the host-published port)`

> **Notes for anyone changing this file:** reload_game is a delete-then-reinsert inside one transaction with a shrink guard (ReloadShrinkError) that refuses to replace a game with fewer rows unless --allow-shrink is passed -- this is the safety net that makes the tool safe to interrupt and re-run. The STALE case (rows exist but no raw.etl_game_ingest row) is the dangerous one: it looks loaded but actually carries pre-fix parser bugs, and is exactly what a naive sweep wrapper could leave behind if it recorded progress on dispatch-return instead of on an actual load.

---

### `scripts/sim515_build_ibb_rates.py`

Applies DuckDB migration 0020 and builds sim.ibb_rates -- the per-cell (runner state x outs x late x close) intentional-walk rate table the simulator draws from per plate appearance (SIM-515, replacing an older hardcoded 2.64x IBB formula).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `main()` | Applies migration 0020, calls PlayerProfileComputor._build_ibb_rates(seasons), then prints the built cells (sorted by issued count) and the implied league IBB-per-team-game rate as a sanity check against MLB. | `operator CLI only, run detached in the app container with scripts/ bind-mounted` | `pipeline.batch.player_profile_computor.PlayerProfileComputor._build_ibb_rates` |

**Depends on:** `pipeline/batch/player_profile_computor.py (_build_ibb_rates)`, `db/migrations/duckdb/0020_sim515_ibb_rates.sql`

**Used by:** `simulation/full_pool_sampler.py's per-PA IBB draw reads the sim.ibb_rates table this script builds`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH (default /data/baseball_sim.duckdb)`, `BASEBALL_DB_DSN (required, no default)`

> **Notes for anyone changing this file:** The season window passed via --seasons MUST match the pool's recency-floor window (the last three completed seasons plus the current one, per the 2026-08-20 owner ruling) -- this is the live mechanism CLAUDE.md section 2b names for the SIM-515 IBB-rate table. scripts/ is not baked into the running image, so it must be run with -v "$PWD/scripts:/app/scripts" bind-mounted in.

---

### `scripts/sim518_rebuild_pools.py`

End-to-end pool-only rebuild (SIM-469/SIM-518): applies DuckDB migration 0023, rebuilds sim.pitch_pool with three new conditioning columns (bat_home, pitcher_pitch_count, times_through_order), verifies the rebuild against an independently-recomputed sample, re-exports the engine-artifact pools, and verifies the loader round-trip -- about 2 hours detached versus the 5.7-hour full profile recompute.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_season_counts(con)` | Returns per-season sim.pitch_pool row counts, used to verify the rebuild changed no row count. | — | — |
| `main()` | Runs 5 numbered steps in order: apply the migration, rebuild the pitch pool via PlayerProfileComputor._build_pitch_pool, verify (row counts plus an independent correlated-subquery recomputation of the 3 new columns on a random game sample), re-export the artifacts, and check the loader round-trip on the exported EngineArtifacts bundle. | `operator CLI, run detached with scripts/ bind-mounted; CLAUDE.md records it as having run on 2026-09-09 as part of the SIM-523 part-G rebuild chain` | `pipeline.batch.player_profile_computor.PlayerProfileComputor._build_pitch_pool`<br>`pipeline.batch.player_profile_computor.POOL_BUILDER_VERSION`<br>`pipeline.batch.engine_artifacts.main (--what pool)`<br>`pipeline.batch.engine_artifacts.EngineArtifacts.load` |

**Depends on:** `pipeline/batch/player_profile_computor.py`, `pipeline/batch/engine_artifacts.py`, `db/migrations/duckdb/0023_sim518_pitch_pool_conditioning.sql`

**Used by:** `simulation/full_pool_sampler.py's draw-conditioning weights (SIM_FATIGUE_PC_SIGMA / SIM_FATIGUE_TTO_SIGMA / SIM_PITCH_HOME_OFF_WEIGHT), still gated OFF pending their fit, read the columns this rebuild adds`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH (default /data/baseball_sim.duckdb)`, `BASEBALL_ENGINE_ARTIFACT_DIR (default /data/play_pool/engine_artifacts)`, `SIM518_SAMPLE_GAMES (default 500 -- size of the independent-verification game sample)`, `BASEBALL_DB_DSN`

> **Notes for anyone changing this file:** Asserts POOL_BUILDER_VERSION is one of (sim518.1, sim523g.1) -- this ties the script to a specific pool-builder version chain; the live version has since moved past sim518.1 to sim523g.1 (see scripts/sim523_part_g_rebuild.py), which this script tolerates but does not itself produce. Elsewhere documented as needing to run with the app STOPPED, because the forkserver holds a DuckDB writer lock (the still-open SIM-524 ticket).

---

### `scripts/sim523_part_g_rebuild.py`

The current, most complete data-rebuild chain for the play-picker redesign (SIM-523 part G): adds sprint speed to the batter/fielder profiles, rebuilds the pitch pool via sim518_rebuild_pools.py's steps, rebuilds the outcome pool with the current sim523g.1 builder (adding the fielding-chain putout/assist masks), and re-exports the batted-ball pool and actor embeddings.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `step_migration_and_speed()` | Applies DuckDB migration 0024 and loads sprint speed from raw.sprint_speed into derived.baserunner_season_metrics and derived.fielder_season_metrics via the Postgres attachment. | — | — |
| `step_sim469()` | Runs the SIM-469 pitch-pool rebuild by re-using scripts/sim518_rebuild_pools.py's steps as-is. | — | — |
| `step_outcome_pool()` | Rebuilds sim.outcome_pool for the window seasons with the sim523g.1 builder, verifying row counts and that putout/assist masks are set correctly on out plays and double plays. | — | `pipeline.batch.player_profile_computor.PlayerProfileComputor._build_outcome_pool` |
| `step_round_trip()` | Re-exports the batted-ball pool and actor embeddings, then verifies BattedBallPool.fielders/putout_mask/assist_mask are present at the expected rates. | — | `pipeline.batch.engine_artifacts.main` |
| `main()` | Runs the four steps in order, logging progress with timestamps. | `operator CLI, run with the app STOPPED (docker compose stop app, then this script, then docker compose up -d app); CLAUDE.md records this as builder sim523g.1, run 2026-09-09` | — |

**Depends on:** `scripts/sim518_rebuild_pools.py (re-run as one step)`, `pipeline/batch/player_profile_computor.py`, `pipeline/batch/engine_artifacts.py`, `db/migrations/duckdb/0024_sim523_part_g_data_adds.sql`, `raw.sprint_speed table`

**Used by:** `the sim523g.1-built pools are what production reads today (see the POOL_BUILDER_VERSION check in sim518_rebuild_pools.py); the fielder/baserunner sprint-speed columns feed the SIM-521 fielder-arm-features and steal/advancement actor-matrix work`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH`, `BASEBALL_ENGINE_ARTIFACT_DIR`, `BASEBALL_DB_DSN`

> **Notes for anyone changing this file:** This is the CURRENT full data-rebuild chain -- it produces sim523g.1, the pool builder version production actually runs today. Must run with the app container stopped, because the DuckDB forkserver holds a writer lock that blocks concurrent rebuilds (tracked as the still-open SIM-524 ticket). A future season/window extension would re-run this chain, or its constituent steps, again.

---

### `scripts/sim523_game_set.py`

Generates the BALANCED certifying game set (owner ruling 2026-09-09) used by the acceptance test lane: picks whole-day slates from real MLB schedule dates (every team appears once per date) whose starting pitchers' and batters' own pool rates sit within 0.6 percent of the full pool's own totals across 8 outcome channels, so the certification lane grades on games that are statistically representative rather than hand-picked.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_slates(conn)` | Finds every regular-season date in the pool window with exactly 15 Final games, every home team distinct, and ingested starting lineups. | — | — |
| `PoolRates (class)` | Holds the pool's own recency-weighted per-channel rates (K/BB/HBP per plate appearance; singles/doubles/triples/home-runs/reach-on-error per ball in play) that candidate slates are scored against. | — | — |
| `_game_expectation(...)` | Computes one game's actor-matched expected rate per channel from its two starting pitchers' and eighteen starting batters' own pool rows, weighted by expected plate appearances. | — | — |
| `_balanced_order(games)` | Orders the picked games so extremes are paired, matching what tests/acceptance/bands.py requires for a stable, low-variance-per-block lane ordering. | — | — |
| `main()` | CLI entry: scores every candidate date, picks the best --slates dates from distinct seasons minimizing the largest relative channel deviation (ties broken on park balance), and prints/writes the game keys plus the expectation table, ready to paste into bands.py. | `an operator running it to regenerate the certifying set; its output is embedded as constants in tests/acceptance/bands.py` | — |

**Depends on:** `raw.games / raw.game_lineups tables (via asyncpg)`, `sim.pitch_pool / sim.outcome_pool (via duckdb)`, `derived.park_factors (regressed run factor)`

**Used by:** `tests/acceptance/bands.py -- the 45-game balanced set constant (around line 810) cites this script as its generator`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH`, `BASEBALL_DB_DSN`

> **Notes for anyone changing this file:** This is the generator behind the acceptance suite's default 45-game by 130-iteration lane (5,850 game-sims) that replaced the older fixed 12-game set and absorbed the SIM-497a/b date-range lane. Re-run this whenever the pool window advances (a new season becomes available) or the channel list changes, since the constants embedded in bands.py would otherwise drift from the live pool's actual composition.

---

### `scripts/sim523_concentration.py`

Runs the SIM-523 part-F concentration check at given actor-factor powers: measures, per actor score matrix, how much of the draw weight a live player's own team disproportionately receives (the own-staff ratio), and passes or fails against a strict ratio ceiling. This is the gate that must pass before a strict artifact build ships fitted actor powers.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `main()` | Parses --powers (name=power pairs) and --strict-ratio, calls concentration_report over the manifest's seasons, prints a per-matrix table (median/p90/max own-staff ratio, effective sample share), and exits non-zero if the worst p90 ratio exceeds --strict-ratio. | `an operator, run before shipping a strict actor-power build` | `pipeline.batch.engine_artifacts.concentration_report` |

**Depends on:** `pipeline/batch/engine_artifacts.py (concentration_report -- the same function the artifact builder itself calls to gate a --what actors_sim strict build)`

**Used by:** `operator CLI / release-gating step before a fitted-power artifact build ships`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH`

> **Notes for anyone changing this file:** Implements the owner's concentration rule from CLAUDE.md section 2b: no actor factor may put more than its natural share of a draw on the live player's own team, checked at a 3.0 own-staff ratio ceiling. The same concentration_report function is called internally by pipeline/batch/engine_artifacts.py's own build path, so this script is mainly useful for probing a candidate power set BEFORE committing it to the build config.

---

### `scripts/measure_filter_cells.py`

SIM-451 -- measures how full the 2,880 pitch-draw hard-filter cells (base occupancy by outs by count by score-band by batting-side) actually are on the live pools, reports the share of ALL draws that land in an under-full cell for candidate MIN_CELL thresholds, and measures the effect of each widening-ladder relaxation step -- the evidence base for tuning the sampler's hard-filter cell index.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `occupancy_stats / cell_occupancy` | Computes the 2,880-length occupancy vector (draws per cell) from the live pitch pool for a given season window. | — | — |
| `widening_recovery(...)` | For each step of the fixed widening ladder (relax score band, then home/away, then count), reports the under-full draw share that survives and the share recovered -- the evidence for whether the decided relaxation order is the right one. | — | — |
| `health_report(...)` | Assembles season row counts, half-inning join coverage, per-window occupancy stats, and the widening ladder into one report, with a coverage_ok gate that is false on low join coverage or a season the pitch pool is missing. | — | — |
| `run(args)` | CLI entry that opens the DuckDB (and optionally Postgres for join coverage), runs the measurement across each requested pool-window configuration, and prints the report. | `operator CLI: python scripts/measure_filter_cells.py` | `simulation.filter_cells -- the shared cell-id algebra (base/outs/count/score-band/side encoding)` |

**Depends on:** `simulation/filter_cells.py -- the ONE shared definition of the cell algebra, also used by simulation/full_pool_sampler.py's live cell index`

**Used by:** `simulation/filter_cells.py's own header names this script as the origin of the SIM-451 measurement; simulation/full_pool_sampler.py's SIM_PITCH_MIN_CELL default is set from its results`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH`, `BASEBALL_DB_DSN`

> **Notes for anyone changing this file:** Per CLAUDE.md's open-work board, the cell-occupancy census re-run (SIM-451) is still an open item -- this is the tool that re-run would use. The cell algebra lives in simulation/filter_cells.py specifically so this script and the production sampler can never drift apart; per that module's own comment, a cell id computed under different edges is NOT comparable with a prior SIM-451 report, so nothing in the shared algebra may change without re-running this measurement. Heavily unit-tested (tests/unit/test_sim451_filter_cells.py, 1000+ lines) despite being a measurement rather than a rebuild script.

---

### `frontend/src/`

The React 18 + Vite + TypeScript frontend (Phase 6, ADR-001). src/api/ holds one hand-written REST client per backend route group (games.ts, betting.ts, players.ts, similarity.ts, lab.ts, auth.ts, assistant.ts) plus a generated schema.d.ts (from scripts/export_openapi.py plus openapi-typescript) and typed.ts, which re-exports the generated response types as the single source of truth so the frontend can never silently drift from the backend contract. src/pages/ holds one component per route (day summary, game page, player detail, betting/props, plus a DataLabLayout with a SQL console, an engine explorer and an AI assistant page for the internal Data Lab tooling); src/components/ holds the reusable pieces grouped by feature (games, charts, graphics, similarity, lab, auth, ui); src/hooks/ holds cross-cutting stateful logic (useAsync for REST loading state, useGameSocket for the live per-game WebSocket); and src/contexts/AuthContext.tsx holds app-wide auth state. The app talks to the backend two ways: plain REST calls (the clients in src/api/*.ts hitting FastAPI routes) for on-demand reads and mutations, and a typed WebSocket (useGameSocket, connecting to /ws/games/{gamePk}) for live per-game push updates (game_state_update, resim_pending, ping/pong) that keep a game page's live state current without polling.

**Depends on:** `scripts/export_openapi.py (produces frontend/openapi.json, the source for schema.d.ts)`, `api/main.py and api/routes/*.py (the REST and WebSocket surface this frontend consumes)`

> **Notes for anyone changing this file:** Component-level detail is out of scope for this pass; this entry only maps the top-level folder structure and the REST-plus-WebSocket integration seam. The typed API surface (typed.ts) is regenerated, not hand-maintained -- any API contract change should flow scripts/export_openapi.py, then npm run gen:api, then a commit of the updated schema.d.ts, before frontend code depends on the new shape.

---
