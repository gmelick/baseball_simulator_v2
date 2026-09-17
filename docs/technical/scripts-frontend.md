# Operational Scripts and Frontend

The scripts that are still run operationally (not one-off probes), plus a map of the frontend structure.

*20 files documented — use the page outline (right sidebar) to jump to one.*

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

Offline job (SIM-407) that replays real completed games through the production simulator N times, compares the simulator's win probabilities and per-player prop PMFs against what actually happened, and writes a PropValidationReport. With --write-calibration it fits the win-probability reliability curve back into the CalibrationReport used at API boot. Since SIM-545 the prop ground truth is the official box score (raw.game_player_stats) per game, every priced market, with the raw.pitches event label as the per-game fallback.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_fetch_final_games(dsn, seasons, max_games)` | Reads completed (Final) games and their real scores from raw.games (home_score_final/away_score_final). | — | — |
| `_fetch_pa_events(pool, game_pk)` | Reads one (batter, pitcher, events) row per completed plate appearance from raw.pitches -- the event-label prop ground truth, the FALLBACK since SIM-545. | `_pair_game_props` | — |
| `_fetch_official_boxscore(pool, game_pk)` | SIM-545: every raw.game_player_stats row for one game as plain dicts (the same reader scripts/clv_backtest.py carries -- scripts/ is not a package). An empty list means no rows; a MISSING table (migration 0023 not applied) warns once and also reads as empty, so the run falls back honestly. | `_pair_game_props` | — |
| `_pair_game_props(pool, game_pk, pset, prop_pairs_by_line, *, official_boxscore)` | SIM-545: pairs one game's PMFs against the best ground truth held -- the box score (every market, via real_props_from_boxscore_rows and the BOXSCORE_* prop tuples) when `official_boxscore` is set and the game has rows, else the event label (five props). Returns `(pairs_added, source)` so run() counts games per source. | `run()` | `_fetch_official_boxscore`<br>`_fetch_pa_events`<br>`simulation.prop_validation.pair_props_for_validation` |
| `_collect_game_results(state, n_iter, base_seed)` | Replays one resolved game N times via record_game_plays under the production factory, collecting one GameSimResult per iteration; run via asyncio.to_thread since it is sync/CPU-bound. | `run()` | `api.routes.games._sim_kwargs_from_state`<br>`simulation.batch_runner.derive_seed`<br>`simulation.play_recorder.record_game_plays` |
| `run(args)` | Orchestrates the whole validation: checks the park-factor DuckDB source (refuses to run park-blind unless --allow-neutral-parks), resolves each game's state and park factor, replays it, builds win-prob and prop PMFs, pairs them against real outcomes, and writes the report. | `Makefile make validate-props target` | `api.routes.games._resolve_state_or_error`<br>`simulation.sim_kwargs.open_sim_duckdb/resolve_park_factor_onto_state`<br>`simulation.win_probability.win_probability`<br>`simulation.prop_distributions.PropDistributionSet.from_results`<br>`simulation.prop_validation.build_validation_report/pair_props_for_validation/real_props_from_boxscore_rows/real_props_from_pa_events/write_reliability_curve_to_calibration_report` |

**Depends on:** `simulation/prop_distributions.py`, `simulation/prop_validation.py`, `simulation/sim_kwargs.py`, `simulation/win_probability.py`, `simulation/play_recorder.py`, `simulation/batch_runner.py`, `api/routes/games.py (_resolve_state_or_error, _sim_kwargs_from_state)`, `raw.games / raw.game_player_stats / raw.pitches tables`

**Used by:** `writes /data/prop_validation.json; with --write-calibration also updates /data/calibration.json, which the API re-reads at its next boot`

**Environment flags read here:** `BASEBALL_DB_DSN`, `CALIBRATION_REPORT_PATH`, `PROP_VALIDATION_PATH`, `BASEBALL_DUCKDB_PATH`

> **Notes for anyone changing this file:** Two ground-truth sources, chosen per game (SIM-545). `--official-boxscore` (the default) pairs every priced market -- batter H/HR/TB/RBI/1B/2B/3B/R/SB/HRR, pitcher K/BB/ER/OUTS/H_ALLOWED -- from the official box score; a game with no box-score rows, or every game under `--no-official-boxscore`, pairs only H/HR/TB and K/BB from the event label (RBI/ER/OUTS are still NOT derived from an event label, which cannot recover them exactly). The log and the summary header print how many games each source paired: a run that paired most games on the fallback is a five-prop validation, and it says so. SIM-452 precondition: the job exits with code 2 if the sim DuckDB (which holds the venue park factors) will not open, unless --allow-neutral-parks is passed; this exists because an earlier version silently fit a park-blind reliability curve.

---

### `scripts/load_historical_odds.py`

Offline backfill (SIM-435) that fetches OPENING and CLOSING betting lines (game moneyline/runline/total plus the 15 player-prop markets -- SIM-421 widened it from 7) for every completed game and writes them into raw.game_odds / raw.prop_odds, so the accuracy comparison has closing lines to score against. SIM-421 added a follow-up mode (`--no-game-odds --prop-stats ...`) that loads only the eight new markets for a season whose seven originals are already in. SIM-421 (2026-09-12): every game market the book posts is fetched by default (the fifteen `GAME_MARKET_TYPES`); `--game-markets` narrows the set the way `--prop-stats` does, e.g. a follow-up pass for the twelve segment / team markets only.
 `--skip-loaded-since <ISO timestamp>` resumes a run that died: it skips every game that already has a prop-odds row fetched at or after that instant (a naive timestamp is local time).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_fetch_final_games / _fetch_lineup_players` | Reads Final games and, per game, the (player_id, is_pitcher) pairs from raw.game_lineups to route pitcher vs batter prop markets. | — | — |
| `_load_game_odds(provider, persist, game_pk)` | Fetches and persists opening/closing prices for moneyline/runline/total for one game. | — | `pipeline.odds_provider.get_odds_provider(...).get_odds` |
| `_load_prop_odds(provider, persist, game_pk, players, *, prop_stats=PROP_STATS, line_types=LINE_TYPES)` | Fetches and persists the prop lines per player, routed by role through `_prop_stats_for_player`: the five PITCHER_PROP_STATS for a pitcher, the ten BATTER_PROP_STATS for a hitter (both imported from pipeline/odds_provider.py -- SIM-421), so `hits` (batter) and `hits_allowed` (pitcher) never cross. | `run()` | `pipeline.odds_provider.get_odds_provider(...).get_prop_odds`<br>`_prop_stats_for_player` |
| `_select_prop_stats(requested) / _select_line_types(requested)` | SIM-421: validate `--prop-stats` (any subset of the 15; canonical order kept) and `--line-types` (any subset of opening/current/closing; default opening + closing). An unknown value raises before ANY provider request, and parse_args turns it into exit code 2, so a typo never silently loads a partial season. | `parse_args`<br>`run()` | `pipeline.odds_provider.PROP_STATS` |
| `_configure_offline_cache()` | SIM-421: sets `ODDS_OFFERS_CACHE_TTL_S` to OFFLINE_OFFERS_CACHE_TTL_S (600 s) ONLY when the operator left it unset, before the provider is built (it reads the variable in its constructor). Safe here and not in the live pipeline because these games are over and their lines are final; it collapses a game's several hundred prop requests into one fetch per market. | `run()` | — |
| `_build_persisters(dsn, pool)` | Constructs a LiveIngestionPipeline WITHOUT starting it, attaches its own asyncpg pool, and returns its _persist_odds/_persist_prop_odds bound methods so writes reuse the exact odds_hash dedup and ON-CONFLICT write path the live pipeline uses. | — | `pipeline.live.live_ingestion_pipeline.LiveIngestionPipeline._persist_odds`<br>`pipeline.live.live_ingestion_pipeline.LiveIngestionPipeline._persist_prop_odds` |
| `run(args)` | Top-level orchestration: validate the market/line-type selection, set the offline cache time-to-live, fetch games, fetch/persist game odds (skipped under `--no-game-odds`), fetch lineup then fetch/persist prop odds per game, log totals. | `Makefile make load-historical-odds target` | `_select_prop_stats`<br>`_select_line_types`<br>`_configure_offline_cache` |

**Depends on:** `pipeline/odds_provider.py (get_odds_provider, PROP_STATS, PITCHER_PROP_STATS, BATTER_PROP_STATS)`, `pipeline/bettingpros_odds_provider.py`, `pipeline/live/live_ingestion_pipeline.py`, `raw.games / raw.game_lineups / raw.game_odds / raw.prop_odds tables`

**Used by:** `scripts/clv_backtest.py reads the raw.game_odds / raw.prop_odds rows this script writes`

**Environment flags read here:** `BASEBALL_DB_DSN`, `REDIS_URL (a placeholder value only -- start() is never called, so Redis is never touched)`, `ODDS_PROVIDER (read inside pipeline.odds_provider.get_odds_provider)`, `ODDS_API_KEY (read by the BettingPros provider)`, `ODDS_OFFERS_CACHE_TTL_S (SIM-421: set to 600 by this job when unset; an explicit value always wins)`

> **Notes for anyone changing this file:** Re-runs are idempotent via the odds_hash ON CONFLICT DO NOTHING dedup shared with the live pipeline. With ODDS_PROVIDER unset it falls back to the deterministic MockOddsAPI, useful for a no-network wiring smoke test. Network-bound, so cap smoke runs with --max-games. The SIM-421 follow-up pass for the eight new markets is `ODDS_PROVIDER=bettingpros ODDS_API_KEY=... python scripts/load_historical_odds.py --seasons 2024 --no-game-odds --prop-stats singles doubles triples runs stolen_bases hits_runs_rbis outs_recorded hits_allowed` -- it needs migration 0022 applied first (the seven-value CHECK constraint rejects the new strings; applied on the live database 2026-09-12), and it must be coordinated with the owner's wrong-game odds re-load (SIM-536), which the owner runs personally.

---

### `scripts/clv_backtest.py`

The SIM-538 sim-vs-closing-line accuracy comparison, the platform's gold-standard validation. For every completed game with a closing betting price, it compares the simulator's own probability against the market's (de-vigged) closing probability, scored against the real outcome with a proper scoring rule (Brier score, log loss). SIM-539 adds a stated minimum sample size per market row; SIM-540 adds an opt-in hypothetical dollar return, explicitly not a certified betting edge. Runs across-games in parallel via a forkserver ProcessPoolExecutor for season-scale throughput. SIM-541 (2026-09-11) deleted the file's original report, the SIM-429 CLV (Closing Line Value) scoreboard (entry price vs. closing price, no real outcome involved) -- the owner ruled the platform no longer measures the entry-to-close line move at all.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `score_game_accuracy(game_pk, win_prob, summary, odds, home_score, away_score)` | Scores moneyline/total/runline for one game on a FIXED reference side (home / over) against the closing line and the real final score -- never the side the model would bet, so the read never depends on the model's own pick. | `_score_one_game` | `betting.clv_engine.moneyline_edge_report/total_over_under_edge_report/run_line_edge_report` |
| `score_prop_accuracy(game_pk, pset, prop_odds, batter_actuals, pitcher_actuals, *, scorable_props)` | Scores every player-prop market the game's ground truth can grade, on the OVER side against the closing line and the real per-player total. `scorable_props` is REQUIRED and per game (SIM-545): BOXSCORE_SCORED_PROPS (all 15 model props, RBI and ER included) on the official box score, EVENT_LABEL_SCORED_PROPS (K/BB/H/HR/TB) on the event-label fallback -- so a fallback game never grades a prop its source cannot see. A player with no actual (he did not play -- the book voids the bet) and a push are skipped. | `_score_one_game` | `PROP_VOCAB_MAP`<br>`betting.clv_engine.prop_edge_report` |
| `PROP_VOCAB_MAP / MARKET_TRUST / TRUST_TIER_ORDER` | The 15-entry odds prop_stat -> model prop map (SIM-421; the batter `hits`->H and the pitcher `hits_allowed`->H_ALLOWED are kept apart), the per-market trust LABEL (a grouping carried over from the retired pool-totals view -- the market's own Brier row is the verdict; the eight new model props 1B/2B/3B/R/SB/HRR/OUTS/H_ALLOWED carry `unvalidated` until the comparison has scored them), and the one shared tier sort order. | `score_prop_accuracy`<br>`aggregate_accuracy_comparison`<br>`aggregate_hypothetical_return` | — |
| `_fetch_official_boxscore(pool, game_pk) / _fetch_prop_ground_truth(pool, game_pk)` | SIM-545: read every raw.game_player_stats row for one game (a MISSING table warns once and reads as empty) and pick the ground truth per game -- the official box score when the game has rows, else the event label -- as a `PropGroundTruth` (actuals + `source` + `scorable_props`). | `_score_one_game` | `simulation.prop_validation.real_props_from_boxscore_rows`<br>`_fetch_pa_events`<br>`simulation.prop_validation.real_props_from_pa_events` |
| `aggregate_accuracy_comparison(records, ...)` | PURE: rolls a list of AccuracyRecord into an overall row plus one row per market key, sorted by trust tier, each carrying a bootstrap 95% CI on the paired Brier/log-loss difference and (SIM-539) a stated minimum sample size. | `run()`<br>`tests/unit/test_sim538_accuracy_comparison.py` | — |
| `score_hypothetical_return(records, edge_threshold)` | SIM-540, PURE, opt-in: turns every AccuracyRecord whose sim/market probabilities disagree by at least edge_threshold into a ReturnRecord, priced on the simulator's favored side at the closing price. | `aggregate_hypothetical_return` | `betting.clv_engine.expected_value/american_to_decimal` |
| `aggregate_hypothetical_return(records, edge_threshold, ...)` | SIM-540, PURE: rolls the qualifying bets into mean/total model EV (the model grading its own confidence) and mean/total realized return (graded against the real outcome). | `run()`<br>`tests/unit/test_sim540_hypothetical_return.py` | `score_hypothetical_return` |
| `_score_one_game(pool, game_pk, ...)` | The entire per-game pipeline shared by BOTH the serial and parallel execution paths: read the closing odds, resolve GameState, resolve the park factor and the point-in-time cutoff, replay iterations, build WinProbability and PropDistributionSet, score accuracy records. Returns `(records, status, park_factor, ground_truth_source)` -- the fourth element (SIM-545) is `official_boxscore` / `event_label` / None (no props graded) and feeds the per-source game counters. | `_run_serial`<br>`_process_one_game` | `api.routes.games._resolve_state_or_error`<br>`simulation.sim_kwargs.resolve_park_factor_onto_state/resolve_asof_ymd`<br>`simulation.win_probability.win_probability`<br>`simulation.results.GameSimSummary.from_results`<br>`_collect_game_results`<br>`score_game_accuracy`<br>`_fetch_prop_ground_truth`<br>`score_prop_accuracy` |
| `_process_one_game(game_pk, params)` | Module-level, picklable across-process worker function mapped over the ProcessPoolExecutor; lazily inits one event loop, one asyncpg pool and one DuckDB connection per forkserver worker (amortized), then drives _score_one_game. | `_run_parallel via ProcessPoolExecutor.submit` | `_worker_lazy_init`<br>`_score_one_game` |
| `run(args)` | Top-level orchestration: opens the parent's sim DuckDB (the SIM-452 park-factor precondition), fetches the game list, dispatches to _run_serial or _run_parallel, prints the accuracy comparison (and, opt-in, the hypothetical return), and writes the JSON report with a documented non-zero exit code for every abandoned-run class (SIM-454). | `main()` | — |
| `_read_game_pks_file(path) / bundle_provenance(calibration_path)` | SIM-518: `--game-pks-file` restricts the run to a listed set of games (a JSON list or one per line) — the paired-arm subset run; `bundle_provenance` stamps `params.provenance` with the artifact bundle's manifest timestamps, the calibration file's SHA-256 and the fatigue bandwidths, so `scripts/sim518_pair_accuracy.py` can refuse to pair two reports that did not come from the same frozen inputs. | `run()` | — |

**Depends on:** `betting/clv_engine.py (MarketSide, OddsQuote, TwoWayMarket, american_to_decimal, expected_value, moneyline_edge_report, prop_edge_report, run_line_edge_report, total_over_under_edge_report)`, `simulation/sim_kwargs.py (open_sim_duckdb, resolve_park_factor_onto_state, resolve_asof_ymd, UnresolvedParkFactorError)`, `simulation/win_probability.py`, `simulation/results.py (GameSimSummary)`, `simulation/prop_distributions.py`, `simulation/prop_validation.py (binary_brier, binary_log_loss, real_props_from_boxscore_rows, real_props_from_pa_events, the BOXSCORE_* / DERIVABLE_* prop tuples)`, `simulation/play_recorder.py (record_game_plays)`, `simulation/batch_runner.py (derive_seed)`, `simulation/production_factory.py (warm_worker_cache)`, `api/routes/games.py (_resolve_state_or_error, _sim_kwargs_from_state)`, `raw.games / raw.game_odds / raw.prop_odds / raw.game_player_stats / raw.pitches tables`

**Used by:** `operator CLI only; no other module imports this script. tests/unit/test_sim538_accuracy_comparison.py, test_sim539_minimum_sample_size.py, test_sim540_hypothetical_return.py, test_sim421_backtest_props.py and test_clv_backtest.py load the pure functions by file path via importlib since scripts/ is not a Python package`

**Environment flags read here:** `BASEBALL_DB_DSN`, `BASEBALL_DUCKDB_PATH`, `CLV_BACKTEST_PATH`, `CALIBRATION_REPORT_PATH`, `SIM_MP_START_METHOD (default forkserver -- the multiprocessing start method for the across-games pool)`

> **Notes for anyone changing this file:** Per CLAUDE.md no betting-value measurement runs until every acceptance band is green (owner ruling 2026-09-08/09) -- as of 2026-09-10 that gate is not fully open yet (the pitch/pitch-result split's strikeout shortfall, SIM-527, is still red). SIM-540's hypothetical dollar return is a diagnostic, not a certified betting edge -- `report["hypothetical_return"]["certified"]` is always `False`, a machine-readable marker for a script or dashboard reading the JSON directly. Exit codes are meaningful and documented (EXIT_OK=0 through EXIT_INFRASTRUCTURE=6): a run that scores 0 games always exits non-zero (EXIT_NOTHING_SCORED=5) rather than looking like a healthy empty run, a deliberate SIM-454 fix for a prior silent-no-op bug class. --workers 1 is the serial fallback and byte-identical verification reference; --workers 6 (default) is about 6x throughput via one whole game per forkserver worker (about 373 MB cache each). SIM-545: the JSON report's counters block carries `games_official_boxscore` and `games_event_label`, and the console header prints the same split -- read them first: a run that graded most games on the event-label fallback is a five-prop report (K/BB/H/HR/TB), not a fifteen-prop one. The worker payload of a scored game carries a `ground_truth_source` key; a failed game's payload keeps the exact three-key shape tests/unit/test_sim449_sim_kwargs.py pins.

---

### `scripts/sim518_pair_accuracy.py`

**Purpose:** SIM-518 — pair two `clv_backtest.py` reports (an OFF arm and an ON arm run on the same games, seeds and frozen bundle) and read the PAIRED per-record difference of the simulator's own Brier and log-loss scores, per market, with the game-clustered bootstrap range and the SIM-539 minimum sample at a stated floor. The instrument that says whether a model change (here the pitcher-fatigue factor) made the served probabilities more or less accurate.

| Function | What it does | Called by | Depends on |
|---|---|---|---|
| `provenance_mismatches(a, b)` | Every reason two reports must not be paired: a different base seed, iteration count, season list, market set, calibration flag, artifact bundle (manifest timestamps), calibration hash, or game set. `--force` pairs anyway and records the confound. | `main()` | — |
| `pair_records(a, b)` | Joins the two reports' `accuracy_records` on (game_pk, market, player_id); raises when an outcome or a market price differs between the arms (the odds rows or the box changed between the runs); returns per-record Brier / log loss for each arm and the market, and the paired differences (B minus A). | `main()` | `simulation.prop_validation.OUTCOME_PROB_EPS` |
| `summarize(rows, ...)` | One bucket's read: n, games, the mean paired Brier and log-loss difference, the game-clustered 95% bootstrap range, the clustered SE, the mean probability shift, each arm's mean Brier lead over the market, and the SIM-539 minimum sample at the floor. Reuses the backtest's own `_bootstrap_paired_diff_ci` / `_clustered_se` / `_minimum_observations_clustered`. | `main()` | `scripts/clv_backtest.py (loaded by path)` |
| `_fetch_depth(dsn, game_pks)` | The pitcher's real pitch count and starter flag per (game, player) from `raw.game_player_stats`, for the prop buckets by pitch depth (<75 / 75-99 / 100+) and the deep-start game list (`--write-deep-start-list`, starts of 90+ pitches). | `main()` | `raw.game_player_stats` |

> **Note (2026-09-13):** the backtest's `params.provenance` stamp now also carries the change pool's manifest time (`manifest_mtimes.manager_pool`, compared by `provenance_mismatches`) and a `manager` block — the arm's `SIM_MANAGER`, `SIM_MANAGER_DRAW`, `SIM_BULLPEN_SOURCE`, `SIM_ACTOR_POWER_MANAGER_USAGE` and the six `SIM_RELIEF_*` values — so a report says which arm of the SIM-427 switch it measured.

**Used by:** `operator CLI only (the SIM-518 paired accuracy read). tests/unit/test_sim518_pair_accuracy.py loads it by path.`

---

### `scripts/load_official_boxscores.py`

The official box-score backfill (SIM-545): fetches the MLB Stats API box score for every completed game already in raw.games and stores one raw.game_player_stats row per player who appeared. The historical loader writes the box for every game it loads from now on; this script covers the games loaded before that hook existed. A sportsbook grades a prop against this record, so the validation lanes and the accuracy comparison read it as their prop ground truth.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `final_games_sql(seasons, max_games, game_pks, only_missing, missing_table=...)` | PURE SQL builder over raw.games (Final games, ordered by game_pk for reproducibility); `only_missing` adds a NOT EXISTS against `missing_table` (raw.game_player_stats by default; raw.game_bullpen under `--bullpen-only`) so a re-run skips games that already have rows. | `_fetch_final_games` | — |
| `load_game(ingest, conn, ref, bullpen_only=False)` | One game: fetch the box (`BoxscoreIngest.fetch_rows`, with season / date / team ids from raw.games), upsert the rows (`persist`, ON CONFLICT DO UPDATE), then — SIM-427 — the game's bullpen listing (`parse_bullpen_listing` -> `persist_bullpen`); `bullpen_only` (CLI `--bullpen-only`) skips the box rows and writes the listing alone, for the games whose box rows were loaded before the listing existed. Returns box rows written. | `run()` | `pipeline.etl.boxscore_ingest.BoxscoreIngest`<br>`pipeline.etl.boxscore_ingest.persist`<br>`pipeline.etl.boxscore_ingest.parse_bullpen_listing / persist_bullpen` |
| `run(args)` | The loop: one MLB request per game, `--sleep` seconds apart (default 0.25); a per-game failure is logged and counted, and five in a row stop the run (`--max-consecutive-failures`, default 5; 0 = never stop) because that pattern is an outage or a schema mismatch, not one bad game; a progress line every 100 games; a closing RunSummary (games fetched / rows written / games skipped / failures / aborted). Exit code 1 when the run stops early, or when it wrote nothing and at least one game failed (a wrapper never reads an empty run as a success); 2 with neither `--seasons` nor `--game-pks`; 0 otherwise. | `main()` | `_fetch_final_games`<br>`load_game` |

**Depends on:** `pipeline/etl/boxscore_ingest.py`, `asyncpg`, `MLB Stats API (/api/v1/game/{game_pk}/boxscore)`, `raw.games / raw.game_player_stats tables`

**Used by:** `operator CLI only (the SIM-545 data run). scripts/validate_props.py, scripts/clv_backtest.py and scripts/sim545_boxscore_audit.py read the rows it writes`

**Environment flags read here:** `BASEBALL_DB_DSN (the --dsn default)`

> **Notes for anyone changing this file:** Migration 0023 must be applied first, or every game fails with an undefined-table error (the loop keeps going and counts them -- read the summary). `--only-missing` is the default (`--no-only-missing` refreshes every selected game through the upsert). Cap a first run with `--max-games` as a smoke test. The full 2024 season ran on 2026-09-12 (migration 0023 applied): 2,472 games, 72,651 rows, zero failures, about 40 seconds per 100 games at `--sleep 0.2`; on every game the box-score runs per side equal the raw.games final score, and every Final 2024 game has rows. The other nine seasons (2017-2023, 2025, 2026) have NOT run yet -- about 2-3 hours at the default sleep for the roughly 20,000 remaining games. A numeric `Retry-After` header from MLB replaces the retry wait for that attempt (capped at 60 s, in `BoxscoreIngest._mlb_get`).

---

### `scripts/sim427_manager_recompute.py`

**Purpose:** SIM-427 part 4a — recompute the manager profiles (`derived.manager_season_metrics`) and the manager league-average row (`derived.league_averages`, entity `manager`) on their own, instead of the five-hour full profile rebuild. The app must be STOPPED (a DuckDB write). Applies the DuckDB migration `0027_sim537_pitcher_manager_asof.sql` first (idempotent; the live manager table lacked the point-in-time column the SIM-537 INSERT names), runs the computor's own `_compute_manager_profiles`, then `LeagueAverageProfiles.compute`, then verifies (rows per season, the manager league rows, the league means the plan's §2 table cites). Ran 2026-09-13: ten seasons, 14 s for the profiles, 10 manager league rows.

| Function | What it does | Called by | Depends on |
|---|---|---|---|
| `apply_migration(duckdb_path)` | Splits the migration file into statements (comment lines dropped first) and runs each on a writable connection. | `main()` | `db/migrations/duckdb/0027_sim537_pitcher_manager_asof.sql` |
| `verify(duckdb_path, seasons)` | Read-only: the `asof_date` column is present, rows per season (and how many below the minimum sample), the manager league rows per season with the four means the plan cites; raises when a requested season has no league row. | `main()` | — |

**Used by:** `operator CLI only (part 4a's data run). Then \`make calibrate\` and \`python -m pipeline.batch.engine_artifacts --what actors_sim --matrix manager_usage\` (read-only).`

---

### `scripts/sim551_batter_recompute.py`

**Purpose:** SIM-551 — rebuild the batter profiles (`derived.batter_season_metrics`) at ONE cutoff, on their own, after a partial rebuild left the table at two dates and the batter engine refused to build (`build_all_engines: 10/11`, 2026-09-11 → 09-16). The app must be STOPPED (a DuckDB write). Runs the computor's own `_compute_batter_profiles` for the seasons in one transaction (the seasons' old rows deleted first), recomputes ONLY the `batter` rows of `derived.league_averages` (the writer's other blocks would overwrite the runner rows the SIM-531 recompute wrote with their new keys), then verifies. Ran 2026-09-17: ten seasons in 11 s, one stamp on 7,885 rows, the batter engine built 4,371 profiles.

| Function | What it does | Called by | Depends on |
|---|---|---|---|
| `stamps(duckdb_path)` | Read-only: the distinct `asof_date` stamps on the table with their season range and row count — printed before and after the rebuild. | `main()` | — |
| `recompute_batter_league_averages(duckdb_path, seasons)` | The `batter` block of `LeagueAverageProfiles.compute`, alone (INSERT OR REPLACE on the batter rows for the seasons). | `main()` | — |
| `verify(duckdb_path, seasons)` | Read-only: rows per season with the physical block's coverage (a floor of 500 batter-seasons with a bat-speed figure on a completed season from 2023 on), exactly ONE stamp on the table, a batter league row per season, and a build of `BatterSimilarityEngine` over every season — the build the app runs at boot; raises on any problem. | `main()` | `similarity.engines.batter_similarity.BatterSimilarityEngine` |

**Used by:** `operator CLI only. Then \`docker compose start app\` (check the boot log for \`build_all_engines: 11/11\`) and \`python -m pipeline.batch.engine_artifacts --what actors_sim --matrix batter\` (read-only).`

---

### `scripts/sim427_manager_probe.py`

**Purpose:** SIM-427 part 4f — the manager probe: real games on the balanced 45-game set with ONE configuration of the pitching-change draw (an "arm") per process, paired by `report`. The reads: THE USAGE READ (pitchers per team-game, the starters' pitches at the pull — mean and spread across starter-games — the starters' outs, the half-boundary share of changes, against the official box score's numbers for the set's seasons from `raw.game_player_stats` and the pool's own change rates); THE MANAGER READ (the change rate per boundary with a starter on the mound, by manager tier — terciles of the live managers' own `starter_avg_pitch_count` — against the pool's own rate on those managers' starter rows: the per-opportunity conditional the manager power is fitted on); THE RELIEVER READ (the entering arm's high-leverage role share by inning tier, its rest at entry, its hand mix, against the pool's own incoming arms); THE COMPOSITION READ (the sim's pitch share by pitch-count band and times through the order against the pool's row share — the fatigue read of the SIM-518 plan); THE PREDICTION SHIFT (per starter-game strikeout mean and probability of clearing the closing line, arm minus baseline).

| Function / class | What it does | Called by | Depends on |
|---|---|---|---|
| `Arm.parse(text)` / `Arm.apply(machine)` | `off` = production today (the SIM-434 formula, the synthetic pen); otherwise `key=value` pairs: `draw` (0/1), `pen` (synthetic/box), `mgr` (the manager power), `role` (σ), `stuff` (power), `rest` (σ days), `p2d` (mismatch weight), `p3d` (σ pitches), `hand` (mismatch weight). `apply` sets the sampler weights on the process-cached sampler directly and `machine.manager_draw`; the pen source is the resolver's `SIM_BULLPEN_SOURCE`, set in this process's environment before the games are resolved. | `run()` | — |
| `install(machine, rec, state)` | Wraps the per-game machine's `_maybe_pull_starter` (boundaries visited and changes per (manager, starter-on-the-mound), the change's side, the entering arm's hand, role share and recent usage) and `_full_pool_outcome` (pitches per pitcher and the band × TTO composition). | `run()` | — |
| `_box_reference(seasons)` / `_pool_reference(fp, manager_ids)` / `_k_lines(game_pks)` | The three references: the box score's usage numbers for the seasons (the plan's 2024 constants stand in when the database is unreachable), the change pool's own rates (overall per role × boundary, per manager on starter rows, the incoming arms' role / rest / hand mix on changed rows), the closing strikeout lines. | `run()` | `raw.game_player_stats`<br>`raw.prop_odds`<br>`EngineArtifacts.change_pool / change_roles` |
| `report(args)` | Pairs the arm JSONs (the first is the baseline) and prints the five reads; `--scan-json scripts/sim518_fatigue_scan.json` supplies the pool's row share for the composition read. | `main()` | — |

**Used by:** `operator CLI only (part 4f's fits: the manager power ladder 0 / 1 / 2 / 4 / 8, then the reliever weights; the usage read is the first of the two flip conditions in the plan's §5).`

---

### `scripts/sim427_accuracy_pair.sh`

**Purpose:** SIM-427's paired accuracy run — the second flip condition of the plan's §5. Runs INSIDE the app container (`docker compose run -d ... app sh scripts/sim427_accuracy_pair.sh`): first the ON arm (`SIM_MANAGER_DRAW=1`, `SIM_BULLPEN_SOURCE=box`, manager power 4, role 0.1, rest 0.5, pitched-in-2-days 0.25, pitches-in-3-days 10 — the fitted values), then the OFF arm (the container's own env: production today), each a `scripts/clv_backtest.py --seasons 2024 --iterations 100 --workers 5 [--max-games N]` writing `scripts/sim427_accuracy_{on,off}.json` and its log; `sim427_accuracy_pair.log` carries the arms' start / exit lines. `SIM427_ITERS` / `SIM427_WORKERS` override the two numbers; `SIM427_MAX_GAMES` caps the run to the first N Final games by game_pk (a prefix spans the whole season — the first 250 cover 150 dates; the owner chose 250 on 2026-09-13 after the full season measured at 43 hours). Pair the reports with `scripts/sim518_pair_accuracy.py`. Five workers, not six: the Docker VM has 9.7 GiB and each worker holds ~1.2 GB beside the two Postgres containers.

**Used by:** `operator CLI only. RAN 2026-09-13 on the first 250 games of 2024 (the owner's choice over the 43-hour season): the draw read more accurate on the starter strikeout market (−0.0156 Brier, range [−0.0255, −0.0066]) and the moneyline (−0.0062), flat elsewhere — the second flip condition; the pairing is scripts/sim427_accuracy_pair.{txt,json}.`

---

### `scripts/sim518_accuracy_fatigue.sh`

**Purpose:** the fatigue plan's §6 grade — two paired accuracy arms INSIDE the app container on the flipped production (the manager draw, the real pen): the fatigue weight with the pitch-count term OFF and the times-through bandwidth at 0.5, then 0.7 (`SIM_FATIGUE_PC_SIGMA=0 SIM_FATIGUE_TTO_SIGMA=$SIGMA`), each `scripts/clv_backtest.py --seasons 2024 --iterations 100 --workers 5 --max-games 250` on the same games, seeds and bundle as the manager flip's ON arm (`scripts/sim427_accuracy_on.json`), which is their baseline. Writes `scripts/sim518_accuracy_tto{05,07}.json` and logs; `sim518_accuracy_fatigue.log` carries the start / exit lines. The same `SIM427_ITERS` / `SIM427_WORKERS` / `SIM427_MAX_GAMES` overrides.

**Used by:** `operator CLI only. RAN 2026-09-14 (2 h 22 + 2 h 30): flat on every market at both bandwidths (pooled −0.0004 / −0.0001 Brier; starter strikeouts −0.0033 / +0.0004); the 0.5 arm's three two-standard-error rows did not repeat at 0.7. The pairings are scripts/sim518_accuracy_pair_tto{05,07}.{txt,json}. The owner landed the weight at 0.5 the same day, so scripts/sim518_accuracy_tto05.json is production's accuracy baseline from then on.`

---

### `scripts/sim548_accuracy_split.sh`

**Purpose:** the accuracy-fit plan's Part A4 — the pitch / pitch-result split as one paired accuracy arm INSIDE the app container over production as the compose file sets it (fatigue 0.5 included): `SIM_PITCH_RESULT_SPLIT=1 SIM_PITCH_PITCHER_POWER=16 SIM_RESULT_PITCHER_POWER=16 SIM_RESULT_BATTER_POWER=8` (the 2026-09-09 fit; bandwidth 1.0 and the density correction at their defaults) on the same 250 games, seeds and bundle. Writes `scripts/sim548_accuracy_split.json`, `.run.log` and `sim548_accuracy_split.log` (start / exit). Pair against `scripts/sim518_accuracy_tto05.json` with `scripts/sim518_pair_accuracy.py` (baseline first). `scripts/clv_backtest.py` stamps the split's six settings into `params.provenance.split`.

**Used by:** `operator CLI only. LAUNCHED 2026-09-14 05:38 UTC (container sim548_split).`

---

### `scripts/sim548_baseline_1000.sh`

**Purpose:** the 1,000-game accuracy baseline of production (the owner's request of 2026-09-14: which markets the simulator is best and worst at against the closing line). Runs INSIDE the app container on the compose environment (the split ON at 16 / 16 / 8, the fatigue weight 0.5) over games 251–1000 of 2024 (the first 1,000 Final games by game id; the first 250 are already on record under the identical configuration as `scripts/sim548_accuracy_split.json`) as three 250-game chunks, each its own report (`scripts/sim548_baseline_2024_chunk{2,3,4}.json`; the lists `scripts/sim548_games_2024_chunk{2,3,4}.txt`), about 3 h 55 each. The app is stopped for the run (five workers and the warm app do not fit the 9.7 GiB VM together).

**Used by:** `operator CLI only. LAUNCHED 2026-09-14 16:51 UTC (container sim548_baseline).`

---

### `scripts/sim548_market_skill.py`

**Purpose:** the joint-fit plan's §5.1 — the per-market skill table of one production read, and the composite objective between two reads. Merges one or more backtest reports of ONE configuration on disjoint game sets (it refuses a provenance mismatch or an overlapping game set unless `--force`) and prints, per market: the records and games; the simulator's Brier, the closing line's and the base-rate forecast's; the skill (base minus Brier) for both; the gap (sim minus line) with a game-clustered bootstrap range and a plain verdict (beats / level with / behind the line); the bias, the spread and the AUC for both; the reliability by bin. With `--compare` (a second configuration on the same games) it prints the composite `Z = Σ w·(Δ/s)/√Σw²` over the markets that clear `--min-n`, with its range, and the markets worse or better beyond their ranges. `--weights` takes a JSON dict of market weights; `--json-out` writes everything.

| Function | What it does | Called by | Depends on |
|---|---|---|---|
| `load_reports(paths, force)` | Merges the reports' `accuracy_records`; checks the provenance key (seed, iterations, seasons, bundle timestamps, calibration hash, the fatigue / manager / split settings) and the game-set overlap. | `main` | — |
| `market_rows(records, min_n, n_boot, seed)` | The per-market statistics; the gap's range from `game_bootstrap` over per-game sums. | `main` | `auc`, `per_game_sums`, `game_bootstrap` |
| `composite(base, arm, min_n, n_boot, seed, weights)` | Pairs the two reads on (game, market, player); per market Δ, its bootstrap standard error and range; Z with its range; the guard lists. | `main` | `numpy` |

**Used by:** `operator CLI only. First run 2026-09-14 on the split arm's 250 games.`

---

### `scripts/sim548_calibrate_markets.py`

**Purpose:** the joint-fit plan's §5.4 / §7 — the per-market calibration layer. Fits, per market, a map from the simulator's probability to a calibrated one on the FIT reports' records and reads it on the CHECK reports' (never the same games): the two-parameter logistic map `logit(p') = a + b·logit(p)` (a shift and a shrink; `b < 1` = the simulator was over-spread) by maximum likelihood — Newton steps with step-halving, a convergence flag, and a fit that did not converge never passes — or `--method isotonic` (a monotone step map over the unique probabilities, so tied values cannot reorder it). A converged negative slope collapses to the fit set's base rate (a wrong-way market carries no information to keep). The moneyline is fitted on `sim_prob_raw` but every BEFORE figure is scored on the mapped `sim_prob` production shows; it is skipped when the records lack the raw value. Per market it prints a and b, the Brier / bias / spread before and after on the check set, the gap to the line before and after with ranges, the map's gain with its range, and PASS when the gain is clear of zero. `--write-calibration PATH` merges the passing maps into the calibration report under `market_calibration` (the app does not read that key yet). Reviewed and corrected 2026-09-15.

**Used by:** `operator CLI only. First read 2026-09-15 (fit games 1–500 of 2024, check 501–1000): the strikeout map (b 0.15) brings the market level with the line; the batter props pass; the totals do not.`

---

### `scripts/sim548_offline_fit.py`

**Purpose:** the joint-fit plan's §5.2 — the offline joint fit of the pitch draws' weights (v2, 2026-09-15). Builds the production sampler through the factory and takes whole STARTS of one season as the test set (`--starts`, every pitch of each; `--start-min-pitches`), then for each pitch sets the point-in-time cutoff to the day before its game, calls `new_half_inning` / `new_plate_appearance` exactly as the loop does (the row's own count, base-out state, batting side, pitch count and times through), and reads the count bucket's whole jar (`_pa_rows` / `_bucket_cdf`; `FullPoolSampler.result_weights` for the result draw). Three levels of score per setting: per pitch the six-outcome Brier (the single-draw path; the split anchored on the real pitch; the split through the pitch draw's own mixture with `--chain-k` anchors drawn from it — unbiased at every power); per plate appearance the twelve count buckets run as an absorbing chain to strikeout / walk / in play / hit by pitch, a four-way Brier (`--pa-chain-k` anchors per bucket for the split path); per start the sum of P(strikeout) against the real strikeouts — correlation, slope of real on predicted, bias, spread ratio (the market's own quantity). Also the per-pitcher discrimination read against each pitcher's season share from rows outside the test set, the effective rows and share of the ADMISSIBLE rows (with the 10th percentile), a design of ladders and pair grids over the eight pitch-draw weights with the interaction read per pair, a checkpoint that resumes only for the same test set and chain settings (`--checkpoint`), `--profile-season` (key the actors to another season's profile — the leak check) and `--holdout-season` (production and the top settings by the per-start strikeout read on fresh starts of another season).

**Used by:** `operator CLI only. v1 (per-pitch only) ran 2026-09-15 to 57 of 78 settings before a Docker restart (scripts/sim548_offline_pitch_v1_partial_20260915.txt); v2 launched 16:38 UTC on 120 starts of 2024, 78 settings (scripts/sim548_offline_v2_20260915.{txt,json,npz}).`

---

### `scripts/sim548_design.py`

**Purpose:** the joint-fit plan's §5.3 — the designed experiment on the full simulator. `--factors` names up to six weights with two levels (`[production, candidate]`, or `[low, high, mid]` when true centre arms are wanted); the script builds a full factorial up to four factors, or a 16-run fraction for five (resolution V, E=ABCD) or six (resolution IV, E=ABC, F=BCD), plus `--baseline-repeats` runs of production with seeds strided by the iteration count (the backtest seeds iteration i as base seed + i, so neighbouring seeds would share almost every iteration) — the noise floor — and `--centre-arms` true centre points at the mid levels (curvature = corners − centres, with a range; None without them). Each arm is one `clv_backtest.py` run on one game list (resumable: an arm whose report exists is skipped). The read, per market on that market's OWN common games (a sparse market no longer shrinks the others' game sets) and for the composite Z: the main effect of each factor, one column per alias group of two-way products (named `A×B=C×E`; a group aliased with a main effect is dropped and reported), the curvature, each with a game-clustered bootstrap range, the noise floor from the baselines, and the best corner. A fractional design with a missing factorial report, or any rank-deficient design, is refused by name. `--analyze-only` reads an existing `out-dir`. Reviewed and corrected 2026-09-15 (`docs/audit/2026-09-15-sim548-instruments-review.md`).

**Used by:** `operator CLI only. Built and corrected 2026-09-15; the first design (four factors, full 2^4, three baselines, two centre arms — the review's §4) waits on the v2 offline read.`

---

### `scripts/sim427_pen_check.py`

**Purpose:** SIM-427 part 4b's check — the real pen against reality, on hundreds of games (never dozens). Builds the pen the resolver builds (`pipeline.bullpen_usage.fetch_pen_for_game`) for a random sample of listed Final games and reads: the listing's coverage per season; the pen's size; the share of relievers who actually pitched (`raw.game_player_stats`, `played_pitch` and not `p_started`) that the pen contains, per side and overall, with every miss classified (not listed / removed by the rotation rule); the agreement with the older SIM-433 availability table both ways; the pen's rest mix. Ran 2026-09-13 on 400 games of 2023-2026: every Final game listed; 8.4 arms per pen; 99.1% of the relievers used are in the pen (96.9% of sides complete) after the opener refinement (`ROTATION_START_MIN_PITCHES`); the SIM-433 gap (71% / 70% raw) is by design — the pen keeps arms SIM-433 marks "rest", SIM-433 keeps rotation starters and today's starter.

| Function | What it does | Called by | Depends on |
|---|---|---|---|
| `main_async(args)` | The whole read: coverage SQL, the sample (`--seasons`, `--games`, `--seed`), per side the pen + usage, the relievers used, the SIM-433 rows, the printed summary. | `main()` | `pipeline.bullpen_usage.fetch_pen_for_game`<br>`raw.game_bullpen / raw.game_player_stats / raw.game_bullpen_availability` |

**Used by:** `operator CLI only (re-run after any change to the pen rule or a new listing backfill).`

---

### `scripts/sim545_boxscore_audit.py`

The validation study for the official box-score ground truth (SIM-545): derives every prop total the platform can build from its own data (raw.pitches event labels, runner flags and steal columns, plus the pickoff outs and intentional walks in raw.play_events), compares each with the official raw.game_player_stats row for the same games, and prints per stat the rows compared, the exact-match rate, the mean absolute difference, the signed bias and the ten worst players. Read-only against the database. Run it over hundreds of games, never dozens (CLAUDE.md §2b).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `pitch_rows_sql() / play_events_sql() / boxscore_rows_sql() / games_sql(seasons, max_games)` | PURE SQL builders: the raw.pitches read uses `pipeline.statcast_events.sql_outs_recorded` and `sql_steal_success` (the SIM-506 two-place steal rule), so the comparison measures the platform's OWN labels against the box. | `run()` | `pipeline.statcast_events` |
| `derive_totals(pitch_rows, play_event_rows=())` | PURE: per-batter H/HR/TB/1B/2B/3B/R/SB/RBI and per-pitcher K/BB/H_ALLOWED/ER/OUTS from the platform's rows; folds the play_events pickoff outs into OUTS and the intent_walk rows into the pitcher's BB (the box counts intentional walks; no pitch row carries one). | `run()` | — |
| `official_totals(box_rows)` | The official side, through the shared reader `simulation.prop_validation.real_props_from_boxscore_rows` -- the SAME function the lanes use, so the audit grades the reader too. | `run()` | `simulation.prop_validation.real_props_from_boxscore_rows` |
| `compare_totals(derived, official, stats) / format_report(...)` | PURE: per stat, rows compared, exact-match rate, mean absolute difference, signed bias (derived minus official) and the ten worst (player, game) pairs; `derived_only_keys` lists players the derivation credits but the box does not. | `run()` | — |

**Depends on:** `pipeline/statcast_events.py`, `simulation/prop_validation.py (real_props_from_boxscore_rows)`, `asyncpg`, `raw.pitches / raw.play_events / raw.game_player_stats / raw.games tables`

**Used by:** `operator CLI only; tests/unit/test_sim545_boxscore_reader.py drives the pure builders and the comparison on hand-built rows`

**Environment flags read here:** `BASEBALL_DB_DSN (the --dsn default)`

> **Notes for anyone changing this file:** The script ran against Postgres on 2026-09-12 over the full 2024 season (2,472 games; 51,679 batter lines, 21,144 pitcher lines): the derivation is exact on H/HR/TB/1B/2B/3B/RBI and H_ALLOWED and inexact on SB (99.08% of player-games match), R (99.97%), K (99.67%), BB (99.71%), OUTS (97.93%) and ER (87.57%) -- the full table, the known causes (K, OUTS, ER) and the open hypotheses (R, SB, BB) are in docs/audit/2026-09-12-sim545-boxscore-audit.md. That study is the evidence for grading props on the box, never the derivation. A non-zero bias on one stat is a finding about the DERIVATION (or the pitch sweep), not about the box score: the box is the reference. The two known semantic gaps to expect in the numbers: the derived BB includes the play_events intentional walks on purpose (so it can match the box), and OUTS credits pickoff outs from play_events -- both are places where raw.pitches alone under-counts.

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
