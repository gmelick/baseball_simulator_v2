# Code Review Checklist - MLB Baseball Simulation Platform

> Generated 2026-06-08. Every source file with the functions/methods inside it, ordered by the typical run lifecycle: offline data prep -> engine build -> API boot -> request routing -> simulation -> results -> betting -> ops scripts. Tick each box once you've confirmed it behaves as expected.

**Coverage:** 80 source files, 1662 symbols. Excludes tests/ (unit/integration/regression/perf), db/migrations/, and the React frontend/ - ask if you want those enumerated too.

**How to read a row:** ` symbol _(kind)_ - what it does `. Methods are shown as `Class.method`. Start at Phase 0 if reviewing the data->model->serving path end to end; jump to Phase 6 (`sim_loop.py`) if you only care about the game engine itself.

## Phases
- **Phase 0 - Offline: Raw data ingestion (ETL + live feed + odds)** - 9 files, 193 symbols
- **Phase 1 - Offline: Profile & artifact precompute (nightly)** - 3 files, 114 symbols
- **Phase 2 - Similarity engines + calibration (built at API boot)** - 16 files, 500 symbols
- **Phase 3 - API application boot (lifespan -> engine state)** - 7 files, 121 symbols
- **Phase 4 - API request routing** - 7 files, 97 symbols
- **Phase 5 - Simulation setup (per /simulate request)** - 6 files, 145 symbols
- **Phase 6 - Simulation execution (per game iteration)** - 6 files, 174 symbols
- **Phase 7 - Results, props & aggregation** - 9 files, 158 symbols
- **Phase 8 - Betting / CLV surface** - 3 files, 52 symbols
- **Phase 9 - Validation, calibration & ops scripts** - 14 files, 108 symbols

---

## Phase 0 - Offline: Raw data ingestion (ETL + live feed + odds)

### `pipeline/etl/coercion.py`  _(4)_
_SIM-437 shared empty/missing -> None type-coercion helpers (imported by both ETL loaders)._

- [ ] `to_float` _(function)_ - Convert a feed value to float; return None for empty/missing/NaN.
- [ ] `to_int` _(function)_ - Convert a feed value to int via to_float; return None if conversion fails.
- [ ] `to_bool` _(function)_ - Convert various feed types to bool (str 'true'/'1'/'yes' -> True).
- [ ] `to_str` _(function)_ - Convert a feed value to a stripped string; return None if empty.

### `pipeline/etl/etl_historical_loader.py`  _(43)_
_MLB Statcast pitch-by-pitch ETL loader to PostgreSQL raw.pitches_

- [X] `_build_row_dict` _(function)_ - Rename API fields and coerce types (via pipeline/etl/coercion) to match raw.pitches schema
- [X] `ValidationResult` _(class)_ - Dataclass holding hard_errors and warnings from pitch validation
- [X] `ValidationResult.is_valid` _(method)_ - Property: True if hard_errors list is empty
- [ ] `_validate_row` _(function)_ - Two-tier validation: hard errors skip row; warnings flag it TRUE
- [ ] `_connect` _(function)_ - HTTP GET with retry backoff; raise on MAX_API_RETRIES exceeded
- [ ] `_fetch_game_pitches` _(function)_ - Fetch feed/live + coaches for game_pk; return (pitch_rows, game_dict)
- [ ] `_parse_height` _(function)_ - Convert MLB API height string '6\' 2\"' to total inches or None
- [ ] `_map_game_status` _(function)_ - Map MLB API abstractGameState + codedGameState to raw.games enum
- [ ] `_schedule_game_is_final` _(function)_ - True iff schedule entry's abstractGameState is 'Final'
- [ ] `_build_starting_lineup_rows` _(function)_ - Build raw.game_lineups tuples from feed/live boxscore starters
- [ ] `HistoricalDataLoader` _(class)_ - Main ETL class: fetch MLB Statcast data and load to PostgreSQL
- [ ] `HistoricalDataLoader.__init__` _(method)_ - Initialize loader with DSN, CSV output flags, lazy pool creation
- [ ] `HistoricalDataLoader._ensure_pool` _(method)_ - Create ThreadedConnectionPool once on first DB access
- [ ] `HistoricalDataLoader._get_conn` _(method)_ - Context manager to borrow pooled connection and return on exit
- [ ] `HistoricalDataLoader.close` _(method)_ - Close pool and all pooled connections; safe to call multiple times
- [ ] `HistoricalDataLoader.load_game` _(method)_ - Fetch and load single game; return summary dict of counts
- [ ] `HistoricalDataLoader.load_date_range` _(method)_ - Incremental loader for all games between two dates
- [ ] `HistoricalDataLoader.refresh_seasons` _(method)_ - Full historical backfill for year range, skipping already-loaded games
- [ ] `HistoricalDataLoader.backfill_lineups_and_scores` _(method)_ - SIM-409: re-fetch Final games to populate raw.game_lineups + scores
- [ ] `HistoricalDataLoader._ensure_prerequisites` _(method)_ - Guarantee all raw.pitches FK parents exist before insert
- [ ] `HistoricalDataLoader._ensure_venue` _(method)_ - Upsert raw.venues; fetch missing venue dimensions/roof/surface
- [ ] `HistoricalDataLoader._ensure_teams` _(method)_ - Upsert raw.teams for missing home/away teams
- [ ] `HistoricalDataLoader._ensure_players` _(method)_ - Upsert raw.players for all boxscore batters/pitchers/fielders
- [ ] `HistoricalDataLoader._ensure_managers` _(method)_ - Upsert raw.managers for home/away game managers per season
- [ ] `HistoricalDataLoader._ensure_game` _(method)_ - Upsert raw.games with final scores, linescore, pitcher decisions
- [ ] `HistoricalDataLoader._ensure_game_lineups` _(method)_ - Insert starting-lineup rows to raw.game_lineups (SIM-409)
- [ ] `HistoricalDataLoader._game_already_loaded` _(method)_ - Return True if any raw.pitches rows exist for game_pk
- [ ] `HistoricalDataLoader._process_and_insert` _(method)_ - Validate, build, and batch-insert pitch rows; return counts
- [ ] `HistoricalDataLoader._log_etl_errors` _(method)_ - SIM-093: bulk-insert skipped pitches to raw.etl_errors audit table
- [ ] `HistoricalDataLoader.reprocess_errored_games` _(method)_ - Return game_pks with raw.etl_errors on/after date for re-ingest
- [ ] `HistoricalDataLoader._batch_insert` _(method)_ - Insert rows in BATCH_SIZE chunks; return total inserted count
- [ ] `HistoricalDataLoader._log_freshness` _(method)_ - Upsert raw.etl_data_freshness with last loaded date per pitcher/batter
- [ ] `HistoricalDataLoader.quality_report` _(method)_ - Print summary of data_quality_flag=TRUE rows per pitcher or date
- [ ] `BATCH_SIZE` _(constant)_ - Rows per executemany call to DB
- [ ] `MAX_API_RETRIES` _(constant)_ - Max retry attempts for MLB Stats API calls
- [ ] `RETRY_BACKOFF_S` _(constant)_ - Seconds between API retries (multiplied by attempt count)
- [ ] `ETL_DB_POOL_MIN` _(constant)_ - Minimum pooled DB connections via ETL_DB_POOL_MIN env var
- [ ] `ETL_DB_POOL_MAX` _(constant)_ - Maximum pooled DB connections via ETL_DB_POOL_MAX env var
- [ ] `GAME_TYPES` _(constant)_ - MLB game type codes (R, F, D, L, W, C, P)
- [ ] `ALWAYS_REQUIRED` _(constant)_ - Columns that must be non-null on every pitch row
- [ ] `IN_PLAY_REQUIRED` _(constant)_ - Columns required when type == 'X' (ball in play)
- [ ] `COLUMN_RENAME` _(constant)_ - Map CSV/API field names to PostgreSQL raw.pitches column names
- [ ] `HALF_INNING_MAP` _(constant)_ - Normalize inning_topbot from API 'top'/'bottom' to schema 'Top'/'Bot'

### `pipeline/etl/etl_sprint_speed_loader.py`  _(7)_
_Fetches Baseball Savant sprint speed leaderboard CSV and upserts to raw.sprint_speed._

- [ ] `SprintSpeedLoader` _(class)_ - Fetch + upsert sprint speed data from Baseball Savant.
- [ ] `SprintSpeedLoader.__init__` _(method)_ - Initialize loader with DSN and optional batch size.
- [ ] `SprintSpeedLoader.refresh_seasons` _(method)_ - Fetch each season's CSV and upsert into raw.sprint_speed.
- [ ] `SprintSpeedLoader._refresh_one_season` _(method)_ - Fetch, parse, and upsert sprint speed data for a single season.
- [ ] `SprintSpeedLoader._fetch_csv` _(method)_ - Hit Savant sprint_speed endpoint with CSV output.
- [ ] `SprintSpeedLoader._parse_csv` _(method)_ - Yield one upsert-ready dict per CSV row.
- [ ] `SprintSpeedLoader._filter_to_known_players` _(method)_ - Drop rows whose player_id is not in raw.players (FK protection).

### `pipeline/etl/venue_backfill_job.py`  _(13)_
_Backfill NULL venue_id rows on raw.games from MLB schedule API._

- [ ] `VenueBackfillJob` _(class)_ - Idempotent backfill of NULL venue_id rows on raw.games.
- [ ] `VenueBackfillJob.__init__` _(method)_ - Initialize with DSN, dry-run flag, and HTTP timeout.
- [ ] `VenueBackfillJob._connect` _(method)_ - Create asyncpg pool.
- [ ] `VenueBackfillJob._close` _(method)_ - Close asyncpg pool.
- [ ] `VenueBackfillJob.run` _(method)_ - Execute the backfill pass; return summary dict.
- [ ] `VenueBackfillJob._fetch_null_venue_games` _(method)_ - SELECT games with NULL venue_id from raw.games.
- [ ] `VenueBackfillJob._update_venue` _(method)_ - UPDATE a game with its venue_id.
- [ ] `VenueBackfillJob._start_log_row` _(method)_ - Insert a 'running' log row and return its id.
- [ ] `VenueBackfillJob._finish_log_row` _(method)_ - UPDATE log row with final status and summary.
- [ ] `VenueBackfillJob._fetch_venue_id` _(method)_ - Fetch venue_id for a game from MLB schedule API.
- [ ] `schedule_venue_backfill_job` _(function)_ - Register backfill job with APScheduler for 6-hourly execution.
- [ ] `_parse_args` _(function)_ - Parse command-line arguments.
- [ ] `_main` _(function)_ - CLI entry point.

### `pipeline/etl/opening_line_job.py`  _(14)_
_Nightly job capturing opening betting lines for games in 7-day lookahead._

- [ ] `OpeningLineJob` _(class)_ - Capture opening lines for all games in 7-day lookahead.
- [ ] `OpeningLineJob.__init__` _(method)_ - Initialize with DSN, lookahead days, and dry-run flag.
- [ ] `OpeningLineJob._connect` _(method)_ - Create asyncpg pool.
- [ ] `OpeningLineJob._close` _(method)_ - Close asyncpg pool.
- [ ] `OpeningLineJob.run` _(method)_ - Execute nightly opening line capture; return summary dict.
- [ ] `OpeningLineJob._fetch_upcoming_games` _(method)_ - Query MLB schedule for all games in lookahead window.
- [ ] `OpeningLineJob._has_opening_line` _(method)_ - Check if raw.game_odds has a line_type='opening' row.
- [ ] `OpeningLineJob._fetch_current_odds` _(method)_ - Fetch game-level odds from configured provider.
- [ ] `OpeningLineJob._store_opening_line` _(method)_ - Insert raw.game_odds row with line_type='opening'.
- [ ] `OpeningLineJob._capture_prop_opening_lines` _(method)_ - Store opening prop lines for announced starters.
- [ ] `OpeningLineJob._start_log_row` _(method)_ - Insert 'running' log row and return its id.
- [ ] `OpeningLineJob._finish_log_row` _(method)_ - UPDATE log row with final status and summary.
- [ ] `schedule_opening_line_job` _(function)_ - Register opening line job with APScheduler for daily 08:00 ET execution.
- [ ] `_main` _(function)_ - Standalone entry point for manual backfill.

### `pipeline/odds_provider.py`  _(13)_
_SIM-370 odds/prop provider abstraction seam for swapping real sources._

- [ ] `OddsProvider` _(class)_ - Protocol interface for odds/prop sources consumed by live pipeline.
- [ ] `OddsProvider.get_odds` _(method)_ - Return game-level betting lines for a game_pk.
- [ ] `OddsProvider.get_prop_odds` _(method)_ - Return a single player-prop quote.
- [ ] `RealOddsAPIProvider` _(class)_ - SIM-370 template stub for real odds/prop provider integration.
- [ ] `RealOddsAPIProvider.__init__` _(method)_ - Initialize with optional API key.
- [ ] `RealOddsAPIProvider._not_configured` _(method)_ - Return error message for unimplemented provider.
- [ ] `RealOddsAPIProvider.get_odds` _(method)_ - Raise not-configured error.
- [ ] `RealOddsAPIProvider.get_prop_odds` _(method)_ - Raise not-configured error.
- [ ] `_make_mock_provider` _(function)_ - Lazy factory for default mock provider.
- [ ] `register_odds_provider` _(function)_ - Register named provider factory for ODDS_PROVIDER env var.
- [ ] `_make_bettingpros_provider` _(function)_ - Lazy factory for SIM-405 BettingPros provider.
- [ ] `available_providers` _(function)_ - Return sorted list of registered provider names.
- [ ] `get_odds_provider` _(function)_ - Return configured OddsProvider via ODDS_PROVIDER env var.

### `pipeline/bettingpros_odds_provider.py`  _(15)_
_Real SIM-405 odds/prop provider backed by BettingPros v3 API._

- [ ] `_normalize_name` _(function)_ - Lower-case, strip accents/non-alphanumerics, collapse spaces.
- [ ] `BettingProsOddsProvider` _(class)_ - Real odds provider backed by BettingPros v3 API.
- [ ] `BettingProsOddsProvider.__init__` _(method)_ - Initialize with optional API key and book preference.
- [ ] `BettingProsOddsProvider._http_get_json` _(method)_ - GET JSON via urllib with optional headers.
- [ ] `BettingProsOddsProvider._bp_get` _(method)_ - GET BettingPros v3 endpoint (network seam).
- [ ] `BettingProsOddsProvider._mlb_get` _(method)_ - GET MLB Stats API endpoint (network seam).
- [ ] `BettingProsOddsProvider._resolve_game_meta` _(method)_ - Resolve game_pk to (date, home_name, away_name).
- [ ] `BettingProsOddsProvider._resolve_event` _(method)_ - Resolve game_pk to matching BettingPros event dict.
- [ ] `BettingProsOddsProvider._resolve_player_name` _(method)_ - Resolve MLB player_id to normalized full name.
- [ ] `BettingProsOddsProvider._pick_line` _(method)_ - Return (american_cost, line) for selection at requested line_type.
- [ ] `BettingProsOddsProvider.get_odds` _(method)_ - Return game-level lines in MockOddsAPI dict shape.
- [ ] `BettingProsOddsProvider.get_prop_odds` _(method)_ - Return single player-prop quote in MockOddsAPI shape.
- [ ] `BettingProsOddsProvider._selections` _(method)_ - Return all selections of (single) offer for a game-level market.
- [ ] `BettingProsOddsProvider._find_player_offer` _(method)_ - Return prop offer whose participant matches player_name.
- [ ] `_opt_float` _(function)_ - Coerce value to float or None.

### `pipeline/live/live_ingestion_pipeline.py`  _(63)_
_Live MLB data ingestion pipeline â€” WebSocket polling, game state building, odds fetch, simulation signal._

- [ ] `SimulationCallback` _(constant)_ - Type alias for async simulation callback signature.
- [ ] `MockOddsAPI` _(class)_ - Generates deterministic mock betting lines seeded on game_pk.
- [ ] `MockOddsAPI._prob_to_american` _(method)_ - Converts probability to American odds format.
- [ ] `MockOddsAPI.get_odds` _(method)_ - Returns mock moneyline/runline/total odds for a game.
- [ ] `MockOddsAPI.get_prop_odds` _(method)_ - Returns deterministic mock player prop lines for confirmed markets.
- [ ] `get_game_odds` _(function)_ - FastAPI endpoint returning mock odds for a single game.
- [ ] `get_todays_odds` _(function)_ - FastAPI endpoint returning mock odds for all games scheduled today.
- [ ] `GameStateBuilder` _(class)_ - Transforms MLB feed/live JSON into game_state JSONB structure.
- [ ] `GameStateBuilder.__init__` _(method)_ - Initializes builder with database pool and incremental parse caches.
- [ ] `GameStateBuilder.build` _(method)_ - Main entry point â€” parses feed/live JSON into complete game_state dict.
- [ ] `GameStateBuilder._parse_roster` _(method)_ - Extracts lineup, bullpen, and bench lists with stats from boxscore.
- [ ] `GameStateBuilder._batch_days_rest` _(method)_ - Fetches days_rest for all pitchers in one batch DB query.
- [ ] `GameStateBuilder._infer_role` _(method)_ - Classifies pitcher role (SP/Opener/MRP/RP) from in-game stats.
- [ ] `GameStateBuilder._parse_play_history` _(method)_ - Converts allPlays into at-bat-level log; incremental parse per SIM-101.
- [ ] `GameStateBuilder._build_history_entry` _(method)_ - Extracts at-bat summary from single play dict (pitches, event, RBI).
- [ ] `GameStateBuilder._parse_linescore` _(method)_ - Extracts inning-by-inning scoring and final stats.
- [ ] `MLBGameWebSocket` _(class)_ - Subscribes to MLB gameday push feed with exponential backoff reconnect.
- [ ] `MLBGameWebSocket.__init__` _(method)_ - Initializes WebSocket client for a game with update callback.
- [ ] `MLBGameWebSocket.start` _(method)_ - Starts the WebSocket connection loop as an async task.
- [ ] `MLBGameWebSocket.stop` _(method)_ - Stops the WebSocket connection loop and cancels the task.
- [ ] `MLBGameWebSocket._run` _(method)_ - Main loop â€” connects to MLB WS, fires update callback on messages, reconnects on error.
- [ ] `ConnectionManager` _(class)_ - Manages frontend WebSocket clients subscribed to individual game channels.
- [ ] `ConnectionManager.__init__` _(method)_ - Initializes empty subscription dict mapping game_pk to client sets.
- [ ] `ConnectionManager.connect` _(method)_ - Accepts and registers a frontend WebSocket client for a game.
- [ ] `ConnectionManager.disconnect` _(method)_ - Unregisters a frontend WebSocket client and removes dead connections.
- [ ] `ConnectionManager.broadcast` _(method)_ - Sends JSON payload to all clients watching a game (iteration-safe).
- [ ] `ConnectionManager.subscriber_count` _(method)_ - Returns the number of frontend clients subscribed to a game.
- [ ] `LiveIngestionPipeline` _(class)_ - Orchestrates full live data ingestion â€” polling, WS, DB writes, broadcast.
- [ ] `LiveIngestionPipeline.__init__` _(method)_ - Initializes pipeline with DSN, Redis URL, simulation callback, odds provider.
- [ ] `LiveIngestionPipeline.start` _(method)_ - Creates DB pool, Redis connection, HTTP session; starts schedule poller.
- [ ] `LiveIngestionPipeline._hydrate_completed_games` _(method)_ - Loads today's Final game_pks on boot to skip redundant upserts (SIM-105).
- [ ] `LiveIngestionPipeline.stop` _(method)_ - Stops poller, closes WebSocket clients and connections.
- [ ] `LiveIngestionPipeline._schedule_poller` _(method)_ - Polls MLB schedule every 30s to discover newly-live and finished games.
- [ ] `LiveIngestionPipeline._sync_live_games` _(method)_ - Fetches today's schedule, spins up/tears down WS per game status.
- [ ] `LiveIngestionPipeline._start_watching` _(method)_ - Creates lock, builder, and MLBGameWebSocket for a newly-detected game.
- [ ] `LiveIngestionPipeline._get_or_create_builder` _(method)_ - Returns cached GameStateBuilder for a game or creates one.
- [ ] `LiveIngestionPipeline._refresh_game_state` _(method)_ - Core update cycle â€” fetches feed, builds state, upserts DB, broadcasts, signals resim.
- [ ] `LiveIngestionPipeline._fetch_feed` _(method)_ - Fetches feed/live from MLB API; falls back to Redis cache on error.
- [ ] `LiveIngestionPipeline._odds_provider` _(method)_ - Lazily resolves active odds provider (env-selected, defaults to mock).
- [ ] `LiveIngestionPipeline._fetch_odds` _(method)_ - Returns game odds from configured provider.
- [ ] `LiveIngestionPipeline._collect_prop_player_ids` _(method)_ - Extracts pitcher and batter IDs eligible for prop pricing.
- [ ] `LiveIngestionPipeline._fetch_prop_odds` _(method)_ - Returns multi-book prop quotes for players via the configured provider.
- [ ] `LiveIngestionPipeline._persist_prop_odds_cycle` _(method)_ - Self-throttled prop fetch/persist cycle at PROP_FETCH_CADENCE_S interval.
- [ ] `LiveIngestionPipeline.capture_opening_prop_lines` _(method)_ - Captures opening prop lines for CLV calculation (SIM-340 opening-line hook).
- [ ] `LiveIngestionPipeline._should_resimulate` _(method)_ - Detects plate appearance completion from feed; guards against double-trigger.
- [ ] `LiveIngestionPipeline._signal_resimulation` _(method)_ - Invokes simulation callback or logs resim signal for Phase 5 integration.
- [ ] `LiveIngestionPipeline._upsert_lineup_state` _(method)_ - Upserts sim.lineup_state with built game_state JSONB.
- [ ] `LiveIngestionPipeline._upsert_game_record` _(method)_ - Upserts raw.games with schedule API game dict (handles Preview/Live/Final).
- [ ] `LiveIngestionPipeline._odds_hash` _(method)_ - Generates deterministic SHA-256 fingerprint of odds payload for dedup.
- [ ] `LiveIngestionPipeline._persist_odds` _(method)_ - Inserts raw.game_odds with CLV columns; deduped by odds_hash.
- [ ] `LiveIngestionPipeline._prop_odds_hash` _(method)_ - Generates deterministic SHA-256 fingerprint of prop payload for dedup.
- [ ] `LiveIngestionPipeline._persist_prop_odds` _(method)_ - Inserts raw.prop_odds with all CLV columns; deduped by odds_hash.
- [ ] `LiveIngestionPipeline.mark_closing_lines` _(method)_ - Designates most recent pre-pitch game-odds snapshot as 'closing' for CLV.
- [ ] `LiveIngestionPipeline.mark_closing_prop_lines` _(method)_ - Designates most recent pre-pitch prop-odds per player/stat/book as 'closing'.
- [ ] `LiveIngestionPipeline._cache_to_redis` _(method)_ - Writes game_feed and game_state to Redis with TTL (fallback + resim endpoint).
- [ ] `LiveIngestionPipeline.live_game_pks` _(method)_ - Property returning list of currently-watched game_pks.
- [ ] `LiveIngestionPipeline.is_watching` _(method)_ - Returns True if pipeline is watching a game_pk.
- [ ] `game_state_ws` _(function)_ - WebSocket endpoint at /ws/games/{game_pk}; handles ping/pong and broadcast receives.
- [ ] `create_app` _(function)_ - Factory for FastAPI app with pipeline lifespan, routers, status and resimulate endpoints.
- [ ] `lifespan` _(function)_ - FastAPI lifespan context â€” starts pipeline on app boot, stops on shutdown.
- [ ] `pipeline_status` _(function)_ - FastAPI endpoint returning live game list and count.
- [ ] `manual_resimulate` _(function)_ - FastAPI endpoint triggering manual resimulation with per-game cooldown (SIM-104).
- [ ] `run` _(function)_ - Standalone runner â€” creates app and starts Uvicorn server.

### `pipeline/live/bullpen_availability_ingest.py`  _(21)_
_Ingest per-game bullpen availability (rest, IL, recent workload status)._

- [ ] `PitcherStatus` _(class)_ - Dataclass for one pitcher's MLB-roster status.
- [ ] `AvailabilityRow` _(class)_ - Dataclass for resolved per-game availability decision.
- [ ] `AvailabilityRow.as_tuple` _(method)_ - Return positional tuple matching INSERT column order.
- [ ] `_GameMeta` _(class)_ - Dataclass holding game date and team identifiers.
- [ ] `BullpenAvailabilityIngest` _(class)_ - Fetch + parse + persist per-game bullpen availability.
- [ ] `BullpenAvailabilityIngest._http_get_json` _(method)_ - GET JSON from a URL via urllib.
- [ ] `BullpenAvailabilityIngest._mlb_get` _(method)_ - GET an MLB Stats API endpoint (network seam).
- [ ] `BullpenAvailabilityIngest._resolve_game_meta` _(method)_ - Resolve game_pk to (date, home_team_id, away_team_id).
- [ ] `BullpenAvailabilityIngest._is_il_status` _(method)_ - Return True if roster status.code denotes injured list.
- [ ] `BullpenAvailabilityIngest.parse_roster` _(method)_ - Parse MLB active-roster payload to {pitcher_id: PitcherStatus}.
- [ ] `BullpenAvailabilityIngest._fetch_team_pitchers` _(method)_ - Fetch + parse team's pitching staff status for a date.
- [ ] `BullpenAvailabilityIngest.decide` _(method)_ - Resolve (available, reason) from roster status + workload.
- [ ] `BullpenAvailabilityIngest.build_rows` _(method)_ - Build one availability row per pitcher on each team's full roster.
- [ ] `BullpenAvailabilityIngest.ingest_game` _(method)_ - Resolve + persist availability for one game.
- [ ] `BullpenAvailabilityIngest.ingest` _(method)_ - Ingest availability for every game in workload records.
- [ ] `BullpenAvailabilityIngest._persist` _(method)_ - UPSERT availability rows into raw.game_bullpen_availability.
- [ ] `_opt_int` _(function)_ - Coerce value to int or None; handles NaN.
- [ ] `_as_date` _(function)_ - Coerce various date formats to datetime.date or None.
- [ ] `_rest_as_of` _(function)_ - Compute rest state as of a game_date from appearance timeline.
- [ ] `_load_workload_records` _(function)_ - Run workload query and return records from PlayerProfileComputor.
- [ ] `main` _(function)_ - CLI entrypoint for data-gated ingestion.

---

## Phase 1 - Offline: Profile & artifact precompute (nightly)

### `pipeline/batch/player_profile_computor.py`  _(61)_
_Step 1.4 nightly batch: computes pitcher/batter/fielder profiles and defensive metrics._

- [ ] `_barrel_case_sql` _(function)_ - Return DuckDB SQL boolean expression for Statcast barrel detection.
- [ ] `compute_leverage_index` _(function)_ - Compute simplified leverage index based on inning, score, runners, outs.
- [ ] `_hc_to_feet` _(function)_ - Convert hc units to feet.
- [ ] `_euclidean_hc` _(function)_ - Compute euclidean distance in hit-chart coordinate space.
- [ ] `_throw_time` _(function)_ - Estimate throw travel time given distance and velocity.
- [ ] `_estimate_hang_time` _(function)_ - Estimate batted ball hang time from launch speed and angle.
- [ ] `_estimate_gb_travel_time` _(function)_ - Estimate ground ball travel time accounting for friction deceleration.
- [ ] `_classify_direction_of` _(function)_ - Classify fielder movement direction relative to their position.
- [ ] `_sigmoid` _(function)_ - Standard sigmoid function for curve fitting.
- [ ] `_fit_logistic_model` _(function)_ - Fit logistic regression with standard scaling, returning model or None.
- [ ] `_predict_proba` _(function)_ - Predict probability using fitted logistic model with stored scaler.
- [ ] `build_run_expectancy_matrix` _(function)_ - Build 24-state run expectancy matrix from play-by-play data.
- [ ] `_quick_auc` _(function)_ - Fast AUC approximation without sklearn.metrics import.
- [ ] `_fit_gmm_for_pitcher` _(function)_ - Fit Gaussian Mixture Model to pitcher's pitch feature vectors.
- [ ] `_resolve_gmm_workers` _(function)_ - Size GMM fit pool to CPU cores minus one, never more than tasks.
- [ ] `_chunk_tasks` _(function)_ - Round-robin tasks into at most n_chunks non-empty lists.
- [ ] `_fit_gmm_batch` _(function)_ - Fit GMMs for a chunk of pitchers in one worker process.
- [ ] `_flush_gmm_results` _(function)_ - Write all GMM results in bulk set-based statements.
- [ ] `_label_component` _(function)_ - Generate human-readable label for GMM component based on velocity/movement.
- [ ] `recency_weight` _(function)_ - Compute sampling recency weight for a row vs reference season.
- [ ] `_recency_weight_sql` _(function)_ - Return SQL CASE expression mirroring recency_weight logic.
- [ ] `_canonical_ref_season` _(function)_ - Determine single canonical recency reference shared by all sim pools.
- [ ] `_source_state` _(function)_ - Return MAX(game_date) and COUNT(*) of usable source rows for season.
- [ ] `_seasons_needing_rebuild` _(function)_ - Return subset of seasons whose source data changed since last build.
- [ ] `_record_pool_build` _(function)_ - Upsert per-pool per-season build watermark and recency reference.
- [ ] `_ensure_index_if_not_exists` _(function)_ - Inject IF NOT EXISTS into CREATE INDEX statement for idempotency.
- [ ] `PlayerProfileComputor.__init__` _(method)_ - Initialize computor with PostgreSQL DSN and DuckDB path.
- [ ] `PlayerProfileComputor._connect` _(method)_ - Connect to DuckDB and attach PostgreSQL extension.
- [ ] `PlayerProfileComputor._close` _(method)_ - Close DuckDB connection.
- [ ] `PlayerProfileComputor._run_schema_ddl` _(method)_ - Create all DuckDB tables via schema SQL file, idempotent.
- [ ] `PlayerProfileComputor.run` _(method)_ - Execute full Step 1.4 nightly pre-computation job.
- [ ] `PlayerProfileComputor._delete_seasons` _(method)_ - Delete existing rows for seasons from all derived and sim tables.
- [ ] `PlayerProfileComputor._recreate_indexes` _(method)_ - Idempotently recreate dropped secondary indexes.
- [ ] `PlayerProfileComputor._compute_park_factors` _(method)_ - Compute park factors per venue/season with Bayesian shrinkage.
- [ ] `PlayerProfileComputor._compute_pitcher_profiles` _(method)_ - Compute pitcher command metrics and fit GMM per pitcher/season.
- [ ] `PlayerProfileComputor._compute_batter_profiles` _(method)_ - Compute batter discipline, batted ball, and platoon split metrics.
- [ ] `PlayerProfileComputor._compute_baserunner_profiles` _(method)_ - Compute baserunner advancement, stealing, and sprint speed metrics.
- [ ] `PlayerProfileComputor._build_baserunner_steal_metrics` _(method)_ - Build SIM-408 steal-engine baserunner metrics table.
- [ ] `PlayerProfileComputor._build_pitcher_steal_metrics` _(method)_ - Build SIM-408 pitcher steal-engine metrics table.
- [ ] `PlayerProfileComputor._compute_bullpen_workload` _(method)_ - Compute per-game bullpen rest and workload (not derived in nightly).
- [ ] `PlayerProfileComputor._compute_manager_profiles` _(method)_ - Compute manager decision tendencies per season (positioning, tactics).
- [ ] `PlayerProfileComputor._compute_catcher_framing` _(method)_ - Compute catcher framing value via logistic regression on called strikes.
- [ ] `PlayerProfileComputor._compute_catcher_blocking` _(method)_ - Compute catcher blocking value from wild pitch and passed ball prevention.
- [ ] `PlayerProfileComputor._compute_catcher_throwing` _(method)_ - Compute catcher throwing arm strength and success rates on steal attempts.
- [ ] `PlayerProfileComputor._compute_outfield_catch_probability` _(method)_ - Compute outfield catch probability model via logistic regression.
- [ ] `PlayerProfileComputor._compute_outfield_arm_metrics` _(method)_ - Compute outfield throwing runs via distance/speed modeling.
- [ ] `PlayerProfileComputor._compute_infield_oaa` _(method)_ - Compute infield OAA (outs above average) from positioning/routing.
- [ ] `PlayerProfileComputor._compute_dp_metrics` _(method)_ - Compute double-play success and opportunity rates per fielder.
- [ ] `PlayerProfileComputor._compute_bunt_defense` _(method)_ - Compute bunt defense value (pitcher + corner infielders).
- [ ] `PlayerProfileComputor._compute_first_base_scooping` _(method)_ - Compute first baseman scooping ability on low throws.
- [ ] `PlayerProfileComputor._compute_error_decomposition` _(method)_ - Decompose fielding errors into preventable vs. unavoidable.
- [ ] `PlayerProfileComputor._ensure_fielder_temp_tables_exist` _(method)_ - Create temporary tables for per-play fielder detail (outfield/infield/DP).
- [ ] `PlayerProfileComputor._aggregate_fielder_season_metrics` _(method)_ - Aggregate per-play fielder detail into derived.fielder_season_metrics.
- [ ] `PlayerProfileComputor._ensure_catcher_temp_tables_exist` _(method)_ - Create temporary catcher temp table for per-pitch framing/blocking.
- [ ] `PlayerProfileComputor._aggregate_catcher_season_metrics` _(method)_ - Aggregate per-pitch catcher detail into derived.catcher_season_metrics.
- [ ] `PlayerProfileComputor._build_pitch_pool` _(method)_ - Build sim.pitch_pool from quality-filtered pitches with incremental rebuild.
- [ ] `PlayerProfileComputor._build_outcome_pool` _(method)_ - Build sim.outcome_pool from in-play batted balls with incremental rebuild.
- [ ] `PlayerProfileComputor._build_stolen_base_pool` _(method)_ - Build sim.stolen_base_pool denormalizing from derived metrics.
- [ ] `PlayerProfileComputor._build_at_bat_situations` _(method)_ - Build derived.at_bat_situations with per-PA situation facts for KDTree.
- [ ] `LeagueAverageProfiles.__init__` _(method)_ - Initialize league-average profiles builder with DuckDB path.
- [ ] `LeagueAverageProfiles.compute` _(method)_ - Compute aggregate league-average profiles for fallback scoring.

### `pipeline/batch/play_pool_cache.py`  _(36)_
_SIM-301: Nightly FAISS tile serializer for pitch and batted-ball pools._

- [ ] `TileKey` _(class)_ - Dataclass identifying one tile on disk (pool, season, bat_hand, pitcher_id).
- [ ] `TileKey.dir_path` _(method)_ - Return directory path for this tile.
- [ ] `TileKey.faiss_path` _(method)_ - Return FAISS index file path for this tile.
- [ ] `TileKey.meta_path` _(method)_ - Return metadata JSON file path for this tile.
- [ ] `TileKey.rowids_path` _(method)_ - Return row-ID mapping file path for this tile.
- [ ] `TileKey.label` _(method)_ - Return human-readable label for this tile.
- [ ] `TilePayload` _(class)_ - Dataclass holding materialized tile vectors, rowids, metadata before serialization.
- [ ] `BuildResult` _(class)_ - Dataclass holding tile rebuild counts and lists for observability.
- [ ] `_table_columns` _(function)_ - Return set of column names from a DuckDB sim table.
- [ ] `_select_bat_hand_column` _(function)_ - Pick batter-handedness column: bat_hand if present, else stand.
- [ ] `_select_watermark_expr` _(function)_ - Build SQL expression yielding per-group freshness watermark as ISO string.
- [ ] `_plan_pitch_tiles` _(function)_ - Return (season, pitcher_id, bat_hand) -> watermark map and bat_hand column name.
- [ ] `_plan_battedball_tiles` _(function)_ - Return (season, bat_hand) -> watermark map for batted-ball pool.
- [ ] `_apply_recency_boost` _(function)_ - Duplicate recent-season rows before indexing for recency weighting.
- [ ] `_fetch_pitch_group` _(function)_ - Stream one tile's pitch rows out of sim.pitch_pool.
- [ ] `_build_pitch_payload` _(function)_ - Materialize one pitch tile into vectors, rowids, metadata.
- [ ] `_build_battedball_payload` _(function)_ - Materialize one batted-ball tile into vectors, rowids, metadata.
- [ ] `_read_meta` _(function)_ - Load metadata JSON from disk, return None on error.
- [ ] `is_tile_stale` _(function)_ - Determine if on-disk tile must be rebuilt (rule 1-4).
- [ ] `_atomic_write_bytes` _(function)_ - Write bytes atomically: tmpfile, fsync, os.replace.
- [ ] `_write_faiss_index` _(function)_ - Serialize FAISS index atomically; return on-disk byte size.
- [ ] `_build_faiss_index` _(function)_ - Build flat-L2 FAISS index over normalized vectors.
- [ ] `_cf` _(function)_ - Coerce DuckDB scalar to JSON-safe finite float.
- [ ] `_fit_pool_norm` _(function)_ - Fit (mean, std) over whole pool for feature columns via DuckDB.
- [ ] `_normalize_for_index` _(function)_ - Apply z-score normalization and sqrt-weight scaling to tile vectors.
- [ ] `_compute_pitch_centroids` _(function)_ - Compute per-(season, pitcher, hand) raw pitch centroids from pool.
- [ ] `_compute_battedball_centroids` _(function)_ - Compute per-(season, hand) raw batted-ball centroids from pool.
- [ ] `write_tile` _(function)_ - Build FAISS index, rowids, meta and write all three atomically.
- [ ] `_build_pitch_pool` _(function)_ - Build every pitch tile: standalone + pitcher_id=0 fallback tiles.
- [ ] `_build_battedball_pool` _(function)_ - Build batted-ball tiles, one per (season, bat_hand).
- [ ] `build_play_pool_cache` _(function)_ - Build (only stale/missing) play-pool tiles into pool_dir.
- [ ] `_f` _(function)_ - Coerce DuckDB scalar to NaN-friendly float.
- [ ] `_now_iso` _(function)_ - Return current UTC timestamp as ISO-8601 string.
- [ ] `_epoch_iso` _(function)_ - Return epoch (1970-01-01) as ISO-8601 string.
- [ ] `_parse_args` _(function)_ - Parse CLI arguments for play_pool_cache module.
- [ ] `main` _(function)_ - CLI entry point for play-pool cache builder.

### `pipeline/batch/engine_artifacts.py`  _(17)_
_SIM-422 fork-safe engine-artifact bundle builder & per-worker loader_

- [ ] `last_n_seasons` _(function)_ - Query database for sorted list of most recent N seasons
- [ ] `build_pitch_pool_artifact` _(function)_ - Write per-hand pitch pool as geometry/situation/metadata disk files
- [ ] `build_battedball_pool_artifact` _(function)_ - Write per-hand batted-ball pool with geometry/situation/realism metadata
- [ ] `build_pitcher_sim_matrix` _(function)_ - Export pitcherÃ—pitcher similarity as dense matrix + dict-of-dicts npz
- [ ] `build_actor_embeddings` _(function)_ - Export per-actor per-season embeddings with global mean/std normalization
- [ ] `HandPool` _(class)_ - One batter-hand's resident pitch-pool candidate matrix for full-pool scoring
- [ ] `HandPool.n` _(method)_ - Property returning number of pitches in this hand's pool
- [ ] `BattedBallPool` _(class)_ - One batter-hand's batted-ball pool with optional realism fact columns
- [ ] `BattedBallPool.n` _(method)_ - Property returning number of batted balls in this hand's pool
- [ ] `EngineArtifacts` _(class)_ - Per-worker loader for SIM-422 bundle with pools, pitcher-sim, actor embeddings
- [ ] `EngineArtifacts.__init__` _(method)_ - Initialize bundle with pools, pitcher-sim, seasons, actor embeddings
- [ ] `EngineArtifacts.extract_shared_arrays` _(method)_ - Extract shareable numpy arrays keyed by flat name for multiprocessing.shared_memory
- [ ] `EngineArtifacts.attach_shared_views` _(method)_ - In-place replace arrays with zero-copy shared-memory views from parent
- [ ] `EngineArtifacts.load` _(method)_ - Load entire bundle from disk with optional shared-memory view attachment
- [ ] `EngineArtifacts.load._take` _(function)_ - Prefer shared view or load fresh numpy array from disk path
- [ ] `EngineArtifacts.load._bb_take` _(function)_ - Load optional numeric batted-ball column with fallback for legacy artifacts
- [ ] `main` _(function)_ - CLI entry point for building engine-artifact bundle with configurable scope

---

## Phase 2 - Similarity engines + calibration (built at API boot)

### `similarity/registry.py`  _(11)_
_Unified registry for discovering and constructing all 11 similarity engines._

- [ ] `EngineSpec` _(class)_ - Immutable metadata describing a similarity engine with lazy class import.
- [ ] `EngineSpec.engine_class` _(method)_ - Lazy-import and return the engine class.
- [ ] `SimilarityEngineRegistry` _(class)_ - Unified discovery and construction interface for all 11 engines.
- [ ] `SimilarityEngineRegistry.list_engines` _(method)_ - Return all canonical engine names in order.
- [ ] `SimilarityEngineRegistry.get_spec` _(method)_ - Return EngineSpec metadata for a named engine.
- [ ] `SimilarityEngineRegistry.get_class` _(method)_ - Resolve name to engine class (lazy import).
- [ ] `SimilarityEngineRegistry.create` _(method)_ - Instantiate an engine by name with kwargs.
- [ ] `list_engines` _(function)_ - Module-level alias for SimilarityEngineRegistry.list_engines.
- [ ] `get_spec` _(function)_ - Module-level alias for SimilarityEngineRegistry.get_spec.
- [ ] `get_class` _(function)_ - Module-level alias for SimilarityEngineRegistry.get_class.
- [ ] `create` _(function)_ - Module-level alias for SimilarityEngineRegistry.create.

### `similarity/engines/pitcher_similarity.py`  _(65)_
_Pitcher-to-pitcher similarity engine using Wasserstein-2 optimal transport and RBF command kernel._

- [ ] `_require_pot` _(function)_ - Raise clean error when POT library is missing but required for W2 distance.
- [ ] `arsenal_scale_from_gamma` _(function)_ - Convert calibrated squared-exponential gamma into engine's linear arsenal scale.
- [ ] `GMMComponent` _(class)_ - Dataclass holding one Gaussian component with weight, mean, covariance, and sample size.
- [ ] `GMMModel` _(class)_ - Full Gaussian Mixture Model for a pitcher-season with multiple components and deserialization.
- [ ] `GMMModel.from_json` _(method)_ - Deserialize GMM from JSON, restoring covariances from standardized to original units.
- [ ] `PitcherProfile` _(class)_ - Complete pitcher profile with GMM, command vector, EB shrinkage alpha, and metadata.
- [ ] `SimilarityResult` _(class)_ - Single similarity query result with pitcher ID, season, score, and sub-scores.
- [ ] `ArsenalSimilarity` _(class)_ - Computes Wasserstein-2 distance and similarity scores between pitcher GMMs.
- [ ] `ArsenalSimilarity.bures_wasserstein_sq` _(method)_ - Compute squared Bures-Wasserstein distance between two Gaussian components.
- [ ] `ArsenalSimilarity._build_cost_matrix` _(method)_ - Build cost matrix of squared Bures distances between all component pairs.
- [ ] `ArsenalSimilarity.distance` _(method)_ - Solve optimal transport problem to compute W2 distance between two GMMs.
- [ ] `ArsenalSimilarity._greedy_transport` _(method)_ - Approximate optimal transport via greedy nearest-component matching when POT unavailable.
- [ ] `ArsenalSimilarity.score` _(method)_ - Convert W2 distance to similarity score using linear exponential transform.
- [ ] `RBFSimilarity` _(class)_ - Gaussian RBF kernel scorer with dimensionality-invariant scaling for command metrics.
- [ ] `RBFSimilarity.__init__` _(method)_ - Initialize RBF kernel with sigma and derive gamma parameter.
- [ ] `RBFSimilarity.score` _(method)_ - Compute RBF similarity between two command vectors.
- [ ] `RBFSimilarity.score_batch` _(method)_ - Vectorized RBF similarity between one query and array of candidates.
- [ ] `EmpiricalBayesShrinkage` _(class)_ - Shrinks pitcher feature vectors toward league average based on sample size.
- [ ] `EmpiricalBayesShrinkage.__init__` _(method)_ - Initialize shrinkage with prior sample size.
- [ ] `EmpiricalBayesShrinkage.alpha` _(method)_ - Compute shrinkage weight as proportion of own data versus prior.
- [ ] `EmpiricalBayesShrinkage.shrink` _(method)_ - Blend pitcher's own vector with league average using shrinkage weight.
- [ ] `standardize_gmm` _(function)_ - Standardize GMM components from original feature units into z-score space.
- [ ] `enforce_min_cluster_size` _(function)_ - Merge small GMM components into larger neighbors to prevent outlier dominance.
- [ ] `FeatureNormalizer` _(class)_ - Z-score normalizer for command feature vectors fit on pitcher population.
- [ ] `FeatureNormalizer.fit` _(method)_ - Compute population statistics from all pitcher command vectors.
- [ ] `FeatureNormalizer.normalize_command` _(method)_ - Z-score normalize a single command vector.
- [ ] `_cache_key` _(function)_ - Generate canonical symmetric cache key for pitcher pair comparisons.
- [ ] `ArsenalCache` _(class)_ - Lazy and batch-precomputable cache for pairwise W2 arsenal distances with matrix backing.
- [ ] `ArsenalCache.__init__` _(method)_ - Initialize empty cache with dict and optional dense matrix store.
- [ ] `ArsenalCache.get` _(method)_ - Look up cached W2 distance between two pitchers.
- [ ] `ArsenalCache.put` _(method)_ - Store a W2 distance in cache and invalidate dense matrix view.
- [ ] `ArsenalCache.build_matrix` _(method)_ - Materialize dense symmetric W2 distance matrix for vectorized lookups.
- [ ] `ArsenalCache.has_matrix` _(method)_ - Check if precomputed matrix covering exact profile list exists.
- [ ] `ArsenalCache.row_distances` _(method)_ - Return all W2 distances from query pitcher to profile list as NumPy array.
- [ ] `ArsenalCache.get_or_compute` _(method)_ - Return cached W2 distance or compute and cache on miss.
- [ ] `ArsenalCache.precompute` _(method)_ - Batch-fill cache for all pairs with optional multiprocessing.
- [ ] `ArsenalCache.size` _(method)_ - Return number of cached distances.
- [ ] `ArsenalCache.finite_distances` _(method)_ - Return all cached finite W2 distances as 1D NumPy array for calibration.
- [ ] `ArsenalCache.save` _(method)_ - Persist cache to disk via pickle.
- [ ] `ArsenalCache.load` _(method)_ - Load cache from disk, merging with existing entries.
- [ ] `_serialize_gmm` _(function)_ - Serialize GMM to plain dict for cross-process pickling in multiprocessing.
- [ ] `_deserialize_gmm` _(function)_ - Reconstruct minimal GMMModel from serialized data in worker process.
- [ ] `_compute_w2_chunk` _(function)_ - Worker function computing W2 distances for a chunk of pitcher pairs.
- [ ] `HandednessPartition` _(class)_ - Stores profiles for one handedness with pre-built vectorized RBF and arsenal matrices.
- [ ] `HandednessPartition.__init__` _(method)_ - Initialize partition for 'L' or 'R' handedness.
- [ ] `HandednessPartition.build` _(method)_ - Populate partition and build normalized command/EB-alpha matrices.
- [ ] `HandednessPartition.score_all` _(method)_ - Score query pitcher against every profile in partition, vectorized.
- [ ] `PitcherSimilarityEngine` _(class)_ - Main engine for exhaustive pitcher-to-pitcher similarity scoring with caching.
- [ ] `PitcherSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path and build scoring components.
- [ ] `PitcherSimilarityEngine.apply_calibration` _(method)_ - Wire calibration report into engine's arsenal W2-to-score transform.
- [ ] `PitcherSimilarityEngine.arsenal_scale` _(method)_ - Property returning currently-in-force W2 scale.
- [ ] `PitcherSimilarityEngine.build` _(method)_ - Load profiles, apply shrinkage, standardize GMMs, build matrices.
- [ ] `PitcherSimilarityEngine.precompute_arsenal_cache` _(method)_ - Batch-precompute all W2 distances with optional multiprocessing and persistence.
- [ ] `PitcherSimilarityEngine._load_league_averages` _(method)_ - Load league-average command profiles for shrinkage fallback.
- [ ] `PitcherSimilarityEngine._load_profiles` _(method)_ - Load pitcher profiles and GMM models from DuckDB.
- [ ] `PitcherSimilarityEngine._apply_shrinkage` _(method)_ - Apply empirical Bayes shrinkage to all command vectors.
- [ ] `PitcherSimilarityEngine._standardize_arsenals` _(method)_ - Compute population statistics and standardize all GMM components to z-space.
- [ ] `PitcherSimilarityEngine.query` _(method)_ - Score query pitcher against all same-handedness profiles, exhaustive.
- [ ] `PitcherSimilarityEngine.query_pair` _(method)_ - Compute similarity between two specific pitcher-seasons.
- [ ] `PitcherSimilarityEngine._score_pair` _(method)_ - Compute all sub-scores and composite between two pitchers.
- [ ] `PitcherSimilarityEngine.get_profile` _(method)_ - Retrieve loaded pitcher profile by ID and season.
- [ ] `PitcherSimilarityEngine.profile_count` _(method)_ - Property returning number of loaded profiles.
- [ ] `PitcherSimilarityEngine.profile_ids` _(method)_ - List all pitcher-season pairs in engine.
- [ ] `PitcherSimilarityEngine.arsenal_cache_size` _(method)_ - Property returning number of cached arsenal distances.
- [ ] `build_similarity_matrix` _(function)_ - Build symmetric NxN similarity matrix for batch pitcher comparisons.

### `similarity/engines/batter_similarity.py`  _(38)_
_Computes composite similarity scores [0, 1] between MLB batter-season profiles_

- [ ] `BatterProfile` _(class)_ - Complete batter-season profile with feature vectors for similarity scoring
- [ ] `SimilarityResult` _(class)_ - One entry in similarity query output with composite and sub-scores
- [ ] `WeightedRBFSimilarity` _(class)_ - Gaussian RBF kernel with per-feature reliability weights and dimensionality-invariant scaling
- [ ] `WeightedRBFSimilarity.__init__` _(method)_ - Initialize RBF scorer with sigma and normalized reliability weights
- [ ] `WeightedRBFSimilarity.score` _(method)_ - Compute weighted RBF similarity between two vectors
- [ ] `WeightedRBFSimilarity.score_batch` _(method)_ - Compute weighted RBF between one query and array of candidate vectors
- [ ] `EmpiricalBayesShrinkage` _(class)_ - Shrinks raw feature vectors toward league average based on sample size
- [ ] `EmpiricalBayesShrinkage.__init__` _(method)_ - Initialize with shrinkage prior strength parameter
- [ ] `EmpiricalBayesShrinkage.alpha` _(method)_ - Compute shrinkage weight alpha from sample size
- [ ] `EmpiricalBayesShrinkage.shrink` _(method)_ - Shrink raw vector toward league average using computed alpha
- [ ] `FeatureNormalizer` _(class)_ - Z-score normalizer fit on population of batter profiles
- [ ] `FeatureNormalizer.fit` _(method)_ - Fit z-score normalization parameters separately for each feature group
- [ ] `FeatureNormalizer._normalize` _(method)_ - Apply z-score normalization with NaN-to-neutral handling
- [ ] `FeatureNormalizer.normalize_discipline` _(method)_ - Normalize discipline feature vector using fitted parameters
- [ ] `FeatureNormalizer.normalize_batted_ball` _(method)_ - Normalize batted ball feature vector using fitted parameters
- [ ] `FeatureNormalizer.normalize_power` _(method)_ - Normalize power feature vector using fitted parameters
- [ ] `FeatureNormalizer.normalize_platoon_l` _(method)_ - Normalize vs-LHP platoon feature vector using fitted parameters
- [ ] `FeatureNormalizer.normalize_platoon_r` _(method)_ - Normalize vs-RHP platoon feature vector using fitted parameters
- [ ] `bats_penalty` _(function)_ - Compute multiplicative penalty for batter handedness mismatch
- [ ] `bats_penalty_vector` _(function)_ - Vectorized version of bats_penalty for batch scoring
- [ ] `BatterPartition` _(class)_ - Stores batter profiles with pre-built normalized matrices for vectorized RBF scoring
- [ ] `BatterPartition.__init__` _(method)_ - Initialize empty partition with no profiles or matrices
- [ ] `BatterPartition.build` _(method)_ - Build normalized feature matrices from profiles for vectorized batch scoring
- [ ] `BatterPartition.score_all` _(method)_ - Score query batter against ALL partition profiles with optional platoon weighting
- [ ] `BatterSimilarityEngine` _(class)_ - Exhaustive batter-to-batter similarity engine with cross-season platoon-aware scoring
- [ ] `BatterSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path and build RBF/shrinkage components
- [ ] `BatterSimilarityEngine.apply_calibration` _(method)_ - SIM-406: replace RBF sigmas and weights from fitted calibration report
- [ ] `BatterSimilarityEngine.build` _(method)_ - Load all batter profiles, apply shrinkage, fit normalizer and build partition
- [ ] `BatterSimilarityEngine._load_league_averages` _(method)_ - Load league average profiles from DuckDB for shrinkage fallback
- [ ] `BatterSimilarityEngine._load_profiles` _(method)_ - Load batter-season profiles from DuckDB with graceful optional columns
- [ ] `BatterSimilarityEngine._apply_shrinkage` _(method)_ - Apply empirical Bayes shrinkage to all feature vectors
- [ ] `BatterSimilarityEngine.query` _(method)_ - Score query batter exhaustively against all profiles with optional handedness context
- [ ] `BatterSimilarityEngine.query_pair` _(method)_ - Compute similarity between two specific batter-seasons
- [ ] `BatterSimilarityEngine._score_pair` _(method)_ - Compute sub-scores and composite between two profiles with platoon weighting
- [ ] `BatterSimilarityEngine.get_profile` _(method)_ - Retrieve BatterProfile by ID and season
- [ ] `BatterSimilarityEngine.profile_count` _(method)_ - Return count of loaded batter-season profiles
- [ ] `BatterSimilarityEngine.profile_ids` _(method)_ - Return list of all (batter_id, season) tuples in engine
- [ ] `build_similarity_matrix` _(function)_ - Build symmetric NÃ—N similarity matrix for list of batter-season tuples

### `similarity/engines/fielder_similarity.py`  _(69)_
_Fielder-to-fielder defensive similarity scoring with RBF kernels and Empirical Bayes._

- [ ] `INFIELD_POSITIONS` _(constant)_ - Set of infield defensive positions (1B, 2B, 3B, SS)
- [ ] `OUTFIELD_POSITIONS` _(constant)_ - Set of outfield defensive positions (LF, CF, RF)
- [ ] `ALL_POSITIONS` _(constant)_ - Union of infield and outfield positions
- [ ] `IF_RANGE_FEATURES` _(constant)_ - Infield range feature names and reliability weights for OAA directional breakdown
- [ ] `IF_DP_FEATURES` _(constant)_ - Infield double-play feature names and weights (DP above expected, attempt/success rates)
- [ ] `IF_PIVOT_FEATURES` _(constant)_ - Middle-infield (2B/SS) pivot-specific feature names and weights
- [ ] `IF_ERROR_FEATURES` _(constant)_ - Infield fielding and throwing error rate feature names and weights
- [ ] `IF_SPECIALTY_FEATURES` _(constant)_ - Infield specialty features: bunt defense and scoop success rates
- [ ] `OF_RANGE_FEATURES` _(constant)_ - Outfield range feature names and weights (OAA directional + catch pct)
- [ ] `OF_ARM_FEATURES` _(constant)_ - Outfield arm features: hold rate, thrown-out rate, advancement prevention, arm runs
- [ ] `OF_STAR_FEATURES` _(constant)_ - Outfield star play success rates by difficulty bucket (5/4-star, routine)
- [ ] `OF_ERROR_FEATURES` _(constant)_ - Outfield fielding and throwing error rate feature names and weights
- [ ] `WEIGHT_IF_RANGE` _(constant)_ - Infield composite weight for range sub-score (45%)
- [ ] `WEIGHT_IF_DP` _(constant)_ - Infield composite weight for double-play sub-score (30%)
- [ ] `WEIGHT_IF_ERRORS` _(constant)_ - Infield composite weight for error sub-score (15%)
- [ ] `WEIGHT_IF_SPECIALTY` _(constant)_ - Infield composite weight for specialty sub-score (10%)
- [ ] `WEIGHT_OF_RANGE` _(constant)_ - Outfield composite weight for range sub-score (40%)
- [ ] `WEIGHT_OF_ARM` _(constant)_ - Outfield composite weight for arm sub-score (30%)
- [ ] `WEIGHT_OF_STARS` _(constant)_ - Outfield composite weight for star plays sub-score (15%)
- [ ] `WEIGHT_OF_ERRORS` _(constant)_ - Outfield composite weight for error sub-score (15%)
- [ ] `RBF_SIGMA_IF_RANGE` _(constant)_ - RBF bandwidth parameter for infield range similarity kernel
- [ ] `RBF_SIGMA_IF_DP` _(constant)_ - RBF bandwidth parameter for infield double-play similarity kernel
- [ ] `RBF_SIGMA_IF_ERRORS` _(constant)_ - RBF bandwidth parameter for infield error similarity kernel
- [ ] `RBF_SIGMA_IF_SPECIALTY` _(constant)_ - RBF bandwidth parameter for infield specialty similarity kernel
- [ ] `RBF_SIGMA_OF_RANGE` _(constant)_ - RBF bandwidth parameter for outfield range similarity kernel
- [ ] `RBF_SIGMA_OF_ARM` _(constant)_ - RBF bandwidth parameter for outfield arm similarity kernel
- [ ] `RBF_SIGMA_OF_STARS` _(constant)_ - RBF bandwidth parameter for outfield star plays similarity kernel
- [ ] `RBF_SIGMA_OF_ERRORS` _(constant)_ - RBF bandwidth parameter for outfield error similarity kernel
- [ ] `EB_N_PRIOR` _(constant)_ - Empirical Bayes prior sample size for defensive metric shrinkage
- [ ] `MIN_FIELDER_BATTED_BALLS` _(constant)_ - Minimum batted ball sample threshold for profile inclusion
- [ ] `FielderProfile` _(class)_ - Dataclass holding fielder-position-season profile with feature vectors
- [ ] `SimilarityResult` _(class)_ - Frozen dataclass with one similarity result entry: player, position, season, scores
- [ ] `WeightedRBFSimilarity` _(class)_ - Gaussian RBF kernel with per-feature reliability weights and batch scoring
- [ ] `WeightedRBFSimilarity.__init__` _(method)_ - Initialize RBF kernel with sigma and normalized reliability weights
- [ ] `WeightedRBFSimilarity.score` _(method)_ - Compute weighted RBF similarity between two feature vectors
- [ ] `WeightedRBFSimilarity.score_batch` _(method)_ - Vectorized RBF similarity computation against array of candidate vectors
- [ ] `EmpiricalBayesShrinkage` _(class)_ - Shrinks raw feature vectors toward positional average based on sample size
- [ ] `EmpiricalBayesShrinkage.__init__` _(method)_ - Initialize Empirical Bayes shrinkage with prior sample size parameter
- [ ] `EmpiricalBayesShrinkage.alpha` _(method)_ - Compute shrinkage weight alpha as n / (n + n_prior)
- [ ] `EmpiricalBayesShrinkage.shrink` _(method)_ - Apply Empirical Bayes shrinkage to raw vector toward positional average
- [ ] `FeatureNormalizer` _(class)_ - Z-score normalizer with per-position parameters for all feature types
- [ ] `FeatureNormalizer.fit` _(method)_ - Fit Z-score normalization parameters per position from all fielder profiles
- [ ] `FeatureNormalizer._normalize` _(method)_ - Apply Z-score normalization using cached mean/std parameters per feature type
- [ ] `FeatureNormalizer.normalize_range` _(method)_ - Z-score normalize range feature vector for a position
- [ ] `FeatureNormalizer.normalize_error` _(method)_ - Z-score normalize error feature vector for a position
- [ ] `FeatureNormalizer.normalize_dp` _(method)_ - Z-score normalize double-play feature vector for a position
- [ ] `FeatureNormalizer.normalize_specialty` _(method)_ - Z-score normalize specialty feature vector for a position
- [ ] `FeatureNormalizer.normalize_arm` _(method)_ - Z-score normalize arm feature vector for a position
- [ ] `FeatureNormalizer.normalize_star` _(method)_ - Z-score normalize star plays feature vector for a position
- [ ] `PositionPartition` _(class)_ - Stores normalized feature matrices and profiles for single position, vectorized scoring
- [ ] `PositionPartition.__init__` _(method)_ - Initialize empty position partition with cached feature matrices
- [ ] `PositionPartition.build` _(method)_ - Populate partition with normalized feature matrices and EB alphas from profiles
- [ ] `PositionPartition.score_all` _(method)_ - Score query against all profiles in partition, return full sorted result list
- [ ] `FielderSimilarityEngine` _(class)_ - Main similarity engine: loads, shrinks, normalizes profiles, provides exhaustive queries
- [ ] `FielderSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path and build per-position RBF kernel scorers
- [ ] `FielderSimilarityEngine.apply_calibration` _(method)_ - Rebuild RBF scorers from fitted calibration report (SIM-406)
- [ ] `FielderSimilarityEngine.build` _(method)_ - Load profiles, apply shrinkage, fit normalizer, build position partitions
- [ ] `FielderSimilarityEngine._load_positional_averages` _(method)_ - Load league positional average vectors per position-season from DuckDB
- [ ] `FielderSimilarityEngine._load_profiles` _(method)_ - Load fielder-position-season profiles from derived.fielder_season_metrics table
- [ ] `FielderSimilarityEngine._apply_shrinkage` _(method)_ - Apply Empirical Bayes shrinkage to all profiles using positional averages
- [ ] `FielderSimilarityEngine._get_rbfs_for_position` _(method)_ - Return tuple of RBF scorers for range, secondary, tertiary, error by position
- [ ] `FielderSimilarityEngine.query` _(method)_ - Score query fielder against all same-position profiles, optionally limit top-n
- [ ] `FielderSimilarityEngine.query_pair` _(method)_ - Compute similarity between two specific fielder-position-season profiles
- [ ] `FielderSimilarityEngine._score_pair` _(method)_ - Compute composite and sub-score similarities between two profiles
- [ ] `FielderSimilarityEngine.get_profile` _(method)_ - Retrieve FielderProfile by player_id, position, season tuple
- [ ] `FielderSimilarityEngine.profile_count` _(method)_ - Return total number of fielder profiles loaded in engine
- [ ] `FielderSimilarityEngine.profile_ids` _(method)_ - Return list of all (player_id, position, season) keys in engine
- [ ] `FielderSimilarityEngine.profile_ids_for_position` _(method)_ - Return list of profile keys for a specific defensive position
- [ ] `build_similarity_matrix` _(function)_ - Build symmetric NÃ—N similarity matrix for list of fielder-position-season triples

### `similarity/engines/catcher_similarity.py`  _(35)_
_Catcher-to-catcher similarity engine with framing, blocking, throwing execution, and deterrence._

- [ ] `WeightedRBFSimilarity` _(class)_ - RBF kernel with per-feature reliability weights for dimensionality-invariant scoring.
- [ ] `WeightedRBFSimilarity.__init__` _(method)_ - Initialize RBF with sigma and normalize reliability weights to sum 1.0.
- [ ] `WeightedRBFSimilarity.score` _(method)_ - Compute weighted RBF similarity between two catcher vectors.
- [ ] `WeightedRBFSimilarity.score_batch` _(method)_ - Vectorized weighted RBF between query and array of candidates.
- [ ] `EmpiricalBayesShrinkage` _(class)_ - Shrinks catcher feature vectors toward league average by sample size.
- [ ] `EmpiricalBayesShrinkage.__init__` _(method)_ - Initialize shrinkage with prior sample size for defensive metrics.
- [ ] `EmpiricalBayesShrinkage.alpha` _(method)_ - Compute shrinkage weight as proportion of own data versus prior.
- [ ] `EmpiricalBayesShrinkage.shrink` _(method)_ - Blend catcher's own vector with league average using shrinkage weight.
- [ ] `FeatureNormalizer` _(class)_ - Z-score normalizer per feature group fit on catcher population.
- [ ] `FeatureNormalizer.fit` _(method)_ - Compute population statistics for each catcher feature group.
- [ ] `FeatureNormalizer._norm` _(method)_ - Z-score normalize a vector using stored mean and std.
- [ ] `FeatureNormalizer.normalize_framing` _(method)_ - Normalize framing feature vector.
- [ ] `FeatureNormalizer.normalize_blocking` _(method)_ - Normalize blocking feature vector.
- [ ] `FeatureNormalizer.normalize_throwing` _(method)_ - Normalize throwing execution feature vector.
- [ ] `FeatureNormalizer.normalize_deterrence` _(method)_ - Normalize throwing deterrence feature vector.
- [ ] `CatcherProfile` _(class)_ - Complete catcher-season profile with four defensive feature groups.
- [ ] `SimilarityResult` _(class)_ - Single catcher similarity result with all four sub-scores.
- [ ] `CatcherPartition` _(class)_ - Stores all catcher profiles with pre-built normalized feature matrices.
- [ ] `CatcherPartition.__init__` _(method)_ - Initialize single partition for all catchers.
- [ ] `CatcherPartition._zero_vec` _(method)_ - Create zero vector of specified dimension for missing profiles.
- [ ] `CatcherPartition.build` _(method)_ - Populate partition and build normalized feature matrices.
- [ ] `CatcherPartition.score_all` _(method)_ - Score query catcher against all profiles, vectorized.
- [ ] `CatcherSimilarityEngine` _(class)_ - Main engine for exhaustive catcher-to-catcher similarity scoring.
- [ ] `CatcherSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path and build four RBF scorers.
- [ ] `CatcherSimilarityEngine.apply_calibration` _(method)_ - Wire calibration report into four defensive RBF scorers.
- [ ] `CatcherSimilarityEngine.build` _(method)_ - Load profiles, apply shrinkage, fit normalizer, build partition.
- [ ] `CatcherSimilarityEngine._load_league_averages` _(method)_ - Load league-average catcher profiles by season.
- [ ] `CatcherSimilarityEngine._load_profiles` _(method)_ - Load catcher profiles from DuckDB, deriving rates from counts.
- [ ] `CatcherSimilarityEngine._apply_shrinkage` _(method)_ - Apply empirical Bayes shrinkage to all feature groups.
- [ ] `CatcherSimilarityEngine.query` _(method)_ - Score query catcher against all profiles, exhaustive.
- [ ] `CatcherSimilarityEngine.query_pair` _(method)_ - Compute similarity between two specific catcher-seasons.
- [ ] `CatcherSimilarityEngine.get_profile` _(method)_ - Retrieve loaded catcher profile by ID and season.
- [ ] `CatcherSimilarityEngine.profile_count` _(method)_ - Property returning number of loaded profiles.
- [ ] `CatcherSimilarityEngine.profile_ids` _(method)_ - List all catcher-season pairs in engine.
- [ ] `build_similarity_matrix` _(function)_ - Build symmetric NxN similarity matrix for batch catcher comparisons.

### `similarity/engines/situation_similarity.py`  _(28)_
_Situation-to-situation KDTree engine for finding nearest historical game states._

- [ ] `SituationVector` _(class)_ - Frozen dataclass holding 11-dimensional game state vector for KDTree query.
- [ ] `SituationVector.to_array` _(method)_ - Convert situation to NumPy array with clipped score differential.
- [ ] `NearestSituation` _(class)_ - Single K-nearest-neighbor result with play ID, game metadata, and distance.
- [ ] `ColumnarSituationMeta` _(class)_ - Columnar NumPy store of per-row situation metadata as parallel arrays.
- [ ] `ColumnarSituationMeta.__post_init__` _(method)_ - Freeze arrays to read-only for safe cross-process sharing.
- [ ] `ColumnarSituationMeta.empty` _(method)_ - Create empty columnar store with zero rows.
- [ ] `ColumnarSituationMeta.from_columns` _(method)_ - Build columnar store from per-field Python lists.
- [ ] `ColumnarSituationMeta.__len__` _(method)_ - Return number of rows in store.
- [ ] `ColumnarSituationMeta.row` _(method)_ - Reconstruct single NearestSituation at given index with optional distance.
- [ ] `ColumnarSituationMeta.__getitem__` _(method)_ - Get reconstructed NearestSituation at integer index.
- [ ] `ColumnarSituationMeta.__iter__` _(method)_ - Iterate over reconstructed NearestSituation rows.
- [ ] `SituationNormalizer` _(class)_ - Z-score normalizer for situation feature vectors with importance weighting.
- [ ] `SituationNormalizer.fit` _(method)_ - Compute mean and std from full historical situation matrix.
- [ ] `SituationNormalizer.normalize` _(method)_ - Normalize single situation vector and apply importance weights.
- [ ] `SituationNormalizer.normalize_batch` _(method)_ - Normalize batch of situation vectors.
- [ ] `SituationSimilarityEngine` _(class)_ - KDTree engine for K-nearest-neighbor lookup of historical situations.
- [ ] `SituationSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path.
- [ ] `SituationSimilarityEngine.build` _(method)_ - Load historical situations and build KDTree index.
- [ ] `SituationSimilarityEngine._load_situations` _(method)_ - Load situations from DuckDB into matrix and columnar metadata.
- [ ] `SituationSimilarityEngine.query` _(method)_ - Find K nearest historical situations to query vector.
- [ ] `SituationSimilarityEngine.query_batch` _(method)_ - Batch query: find K nearest for each situation in list.
- [ ] `SituationSimilarityEngine._row_from_meta` _(method)_ - Reconstruct NearestSituation at index with query-time distance.
- [ ] `SituationSimilarityEngine.index_size` _(method)_ - Property returning number of indexed situations.
- [ ] `SituationSimilarityEngine.is_built` _(method)_ - Check if KDTree has been successfully built.
- [ ] `SituationSimilarityEngine.situation_count_by_outs` _(method)_ - Return distribution of situations by outs (0, 1, 2).
- [ ] `SituationSimilarityEngine.situation_count_by_inning` _(method)_ - Return distribution of situations by inning.
- [ ] `sorted_dict` _(function)_ - Return dictionary sorted by keys.
- [ ] `build_coverage_report` _(function)_ - Generate human-readable coverage summary by (inning, outs) combination.

### `similarity/engines/manager_similarity.py`  _(33)_
_Manager-to-manager similarity engine with pitcher usage, offensive aggressiveness, and platoon sub-scores._

- [ ] `WeightedRBFSimilarity` _(class)_ - RBF kernel with per-feature reliability weights for dimensionality-invariant scoring.
- [ ] `WeightedRBFSimilarity.__init__` _(method)_ - Initialize RBF with sigma and normalize reliability weights to sum 1.0.
- [ ] `WeightedRBFSimilarity.score` _(method)_ - Compute weighted RBF similarity between two manager vectors.
- [ ] `WeightedRBFSimilarity.score_batch` _(method)_ - Vectorized weighted RBF between query and array of candidate managers.
- [ ] `EmpiricalBayesShrinkage` _(class)_ - Shrinks manager feature vectors toward league average by sample size.
- [ ] `EmpiricalBayesShrinkage.__init__` _(method)_ - Initialize shrinkage with large prior for behavioral metric stabilization.
- [ ] `EmpiricalBayesShrinkage.alpha` _(method)_ - Compute shrinkage weight as proportion of own data versus prior.
- [ ] `EmpiricalBayesShrinkage.shrink` _(method)_ - Blend manager's own vector with league average using shrinkage weight.
- [ ] `FeatureNormalizer` _(class)_ - Z-score normalizer per feature group fit on manager population.
- [ ] `FeatureNormalizer.fit` _(method)_ - Compute population statistics for each manager feature group.
- [ ] `FeatureNormalizer._norm` _(method)_ - Z-score normalize a vector using stored mean and std.
- [ ] `FeatureNormalizer.normalize_usage` _(method)_ - Normalize pitcher usage feature vector.
- [ ] `FeatureNormalizer.normalize_aggression` _(method)_ - Normalize offensive aggressiveness feature vector.
- [ ] `FeatureNormalizer.normalize_platoon` _(method)_ - Normalize platoon/matchup management feature vector.
- [ ] `ManagerProfile` _(class)_ - Manager-season profile with three behavioral feature groups.
- [ ] `SimilarityResult` _(class)_ - Single manager similarity result with all three sub-scores.
- [ ] `ManagerPartition` _(class)_ - Stores all manager profiles with pre-built normalized feature matrices.
- [ ] `ManagerPartition.__init__` _(method)_ - Initialize single partition for all managers.
- [ ] `ManagerPartition.build` _(method)_ - Populate partition and build normalized feature matrices.
- [ ] `ManagerPartition.score_all` _(method)_ - Score query manager against all profiles, vectorized.
- [ ] `ManagerSimilarityEngine` _(class)_ - Main engine for exhaustive manager-to-manager similarity scoring.
- [ ] `ManagerSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path and build three RBF scorers.
- [ ] `ManagerSimilarityEngine.apply_calibration` _(method)_ - Wire calibration report into three behavioral RBF scorers.
- [ ] `ManagerSimilarityEngine.build` _(method)_ - Load profiles, apply shrinkage, fit normalizer, build partition.
- [ ] `ManagerSimilarityEngine._load_league_averages` _(method)_ - Load league-average manager profiles by season.
- [ ] `ManagerSimilarityEngine._load_profiles` _(method)_ - Load manager profiles from DuckDB.
- [ ] `ManagerSimilarityEngine._apply_shrinkage` _(method)_ - Apply empirical Bayes shrinkage to all feature groups.
- [ ] `ManagerSimilarityEngine.query` _(method)_ - Score query manager against all profiles, exhaustive.
- [ ] `ManagerSimilarityEngine.query_pair` _(method)_ - Compute similarity between two specific manager-seasons.
- [ ] `ManagerSimilarityEngine.get_profile` _(method)_ - Retrieve loaded manager profile by ID and season.
- [ ] `ManagerSimilarityEngine.profile_count` _(method)_ - Property returning number of loaded profiles.
- [ ] `ManagerSimilarityEngine.profile_ids` _(method)_ - List all manager-season pairs in engine.
- [ ] `build_similarity_matrix` _(function)_ - Build symmetric NxN similarity matrix for batch manager comparisons.

### `similarity/engines/baserunner_similarity.py`  _(36)_
_Baserunner extra-base similarity engine with RBF scoring and Empirical Bayes shrinkage_

- [ ] `BaserunnerProfile` _(class)_ - Dataclass storing complete baserunner-season profile for similarity scoring
- [ ] `SimilarityResult` _(class)_ - Frozen dataclass holding one similarity query result with component scores
- [ ] `WeightedRBFSimilarity` _(class)_ - Gaussian RBF kernel with reliability weights and dimensionality-invariant scaling
- [ ] `WeightedRBFSimilarity.__init__` _(method)_ - Initialize RBF scorer with sigma and normalized reliability weights
- [ ] `WeightedRBFSimilarity.score` _(method)_ - Compute single RBF similarity score between two normalized feature vectors
- [ ] `WeightedRBFSimilarity.score_batch` _(method)_ - Vectorized batch RBF scoring of query against multiple candidate vectors
- [ ] `EmpiricalBayesShrinkage` _(class)_ - Empirical Bayes shrinkage estimator with configurable prior strength
- [ ] `EmpiricalBayesShrinkage.__init__` _(method)_ - Initialize shrinkage estimator with prior sample count
- [ ] `EmpiricalBayesShrinkage.alpha` _(method)_ - Compute shrinkage weight alpha from sample count and prior
- [ ] `EmpiricalBayesShrinkage.shrink` _(method)_ - Apply EB shrinkage to feature vector toward league average
- [ ] `FeatureNormalizer` _(class)_ - Z-score normalizer fit on population of baserunner profiles
- [ ] `FeatureNormalizer.fit` _(method)_ - Fit normalizer means and stds from list of baserunner profiles
- [ ] `FeatureNormalizer._fit_group` _(function)_ - Compute mean and std from array of vectors, replacing zero stds with 1
- [ ] `FeatureNormalizer._normalize` _(method)_ - Apply z-score normalization with NaN handling to feature vector
- [ ] `FeatureNormalizer.normalize_speed` _(method)_ - Normalize speed feature vector using fitted parameters
- [ ] `FeatureNormalizer.normalize_aggression` _(method)_ - Normalize aggression feature vector using fitted parameters
- [ ] `FeatureNormalizer.normalize_success` _(method)_ - Normalize success feature vector using fitted parameters
- [ ] `BaserunnerPartition` _(class)_ - Stores profiles with pre-built normalized matrices for vectorized batch scoring
- [ ] `BaserunnerPartition.__init__` _(method)_ - Initialize empty partition with placeholder for matrices
- [ ] `BaserunnerPartition.build` _(method)_ - Build normalized feature matrices for all profiles using normalizer
- [ ] `BaserunnerPartition.score_all` _(method)_ - Compute RBF scores against all candidates; apply confidence discount and composite
- [ ] `BaserunnerSimilarityEngine` _(class)_ - Main engine scoring baserunners exhaustively against all profiles
- [ ] `BaserunnerSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path and build RBF/shrinkage components
- [ ] `BaserunnerSimilarityEngine.apply_calibration` _(method)_ - Rebuild RBF scorers from calibration report (SIM-406)
- [ ] `BaserunnerSimilarityEngine.build` _(method)_ - Load profiles and league averages from DuckDB; apply shrinkage; build matrices
- [ ] `BaserunnerSimilarityEngine._load_league_averages` _(method)_ - Load per-season league average feature vectors from DuckDB
- [ ] `BaserunnerSimilarityEngine._load_profiles` _(method)_ - Load all baserunner profiles and construct BaserunnerProfile objects
- [ ] `BaserunnerSimilarityEngine._load_profiles._v` _(function)_ - Convert list of values to float64 array, replacing None with 0
- [ ] `BaserunnerSimilarityEngine._apply_shrinkage` _(method)_ - Apply EB shrinkage to speed, aggression, success vectors for all profiles
- [ ] `BaserunnerSimilarityEngine.query` _(method)_ - Score query baserunner against all profiles; return top-N sorted by score
- [ ] `BaserunnerSimilarityEngine.query_pair` _(method)_ - Compute similarity between two specific baserunner-season tuples
- [ ] `BaserunnerSimilarityEngine._score_pair` _(method)_ - Compute normalized sub-scores and confidence-discounted composite between profiles
- [ ] `BaserunnerSimilarityEngine.get_profile` _(method)_ - Retrieve BaserunnerProfile by player_id and season
- [ ] `BaserunnerSimilarityEngine.profile_count` _(method)_ - Return count of loaded profiles
- [ ] `BaserunnerSimilarityEngine.profile_ids` _(method)_ - Return list of all (player_id, season) tuples in engine
- [ ] `build_similarity_matrix` _(function)_ - Build symmetric NÃ—N similarity matrix from list of runner tuples

### `similarity/engines/baserunner_steal_similarity.py`  _(42)_
_Baserunner stolen-base similarity scoring engine with RBF kernels and Empirical Bayes shrinkage_

- [ ] `TENDENCY_FEATURES` _(constant)_ - Defines steal-attempt-rate features with reliability weights.
- [ ] `SUCCESS_FEATURES` _(constant)_ - Defines steal-success-rate features with reliability weights.
- [ ] `WEIGHT_TENDENCY` _(constant)_ - Sub-score weight for steal tendency (~0.6154).
- [ ] `WEIGHT_SUCCESS` _(constant)_ - Sub-score weight for steal success (~0.3846).
- [ ] `RBF_SIGMA_TENDENCY` _(constant)_ - RBF bandwidth parameter for tendency scoring (1.0500).
- [ ] `RBF_SIGMA_SUCCESS` _(constant)_ - RBF bandwidth parameter for success scoring (1.0200).
- [ ] `EB_N_PRIOR` _(constant)_ - Empirical Bayes prior sample size (20 attempts).
- [ ] `MIN_STEAL_ATTEMPTS` _(constant)_ - Minimum steal attempts required for profile inclusion (10).
- [ ] `BaserunnerStealProfile` _(class)_ - Dataclass storing player-season steal profile with feature vectors.
- [ ] `SimilarityResult` _(class)_ - Frozen dataclass for one similarity query result entry.
- [ ] `WeightedRBFSimilarity` _(class)_ - Gaussian RBF kernel with per-feature reliability weight normalization.
- [ ] `WeightedRBFSimilarity.__init__` _(method)_ - Initialize RBF kernel with sigma and normalized reliability weights.
- [ ] `WeightedRBFSimilarity.score` _(method)_ - Compute single weighted RBF similarity score between two vectors.
- [ ] `WeightedRBFSimilarity.score_batch` _(method)_ - Vectorized RBF similarity scoring of query against multiple candidates.
- [ ] `EmpiricalBayesShrinkage` _(class)_ - Implements empirical Bayes shrinkage toward league averages.
- [ ] `EmpiricalBayesShrinkage.__init__` _(method)_ - Initialize shrinkage with prior sample size parameter.
- [ ] `EmpiricalBayesShrinkage.alpha` _(method)_ - Compute shrinkage coefficient alpha from sample count.
- [ ] `EmpiricalBayesShrinkage.shrink` _(method)_ - Shrink raw feature vector toward league average using alpha weight.
- [ ] `FeatureNormalizer` _(class)_ - Dataclass storing z-score normalization means and standard deviations.
- [ ] `FeatureNormalizer.fit` _(method)_ - Compute z-score mean and std from profiles for both feature groups.
- [ ] `FeatureNormalizer._fit` _(function)_ - Inner helper to compute mean and std from array of vectors.
- [ ] `FeatureNormalizer._norm` _(method)_ - Apply z-score normalization to vector, handling NaNs.
- [ ] `FeatureNormalizer.normalize_tendency` _(method)_ - Apply z-score normalization to tendency feature vector.
- [ ] `FeatureNormalizer.normalize_success` _(method)_ - Apply z-score normalization to success feature vector.
- [ ] `StealPartition` _(class)_ - Manages vectorized batch scoring across all steal profiles.
- [ ] `StealPartition.__init__` _(method)_ - Initialize empty partition with profile storage and matrices.
- [ ] `StealPartition.build` _(method)_ - Build normalized feature matrices and EB-alpha weights from profiles.
- [ ] `StealPartition.score_all` _(method)_ - Score query profile against all partition profiles, return filtered results.
- [ ] `BaserunnerStealSimilarityEngine` _(class)_ - Main stolen-base similarity engine: builds index and scores queries.
- [ ] `BaserunnerStealSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path and RBF/shrinkage components.
- [ ] `BaserunnerStealSimilarityEngine.apply_calibration` _(method)_ - Update RBF sigma parameters from CalibrationReport (SIM-406).
- [ ] `BaserunnerStealSimilarityEngine.build` _(method)_ - Load profiles from DuckDB, apply shrinkage, fit normalizer, build index.
- [ ] `BaserunnerStealSimilarityEngine._load_league_averages` _(method)_ - Query DuckDB for league average steal profiles by season.
- [ ] `BaserunnerStealSimilarityEngine._load_profiles` _(method)_ - Query DuckDB baserunner_steal_metrics table, construct BaserunnerStealProfile objects.
- [ ] `BaserunnerStealSimilarityEngine._load_profiles._v` _(function)_ - Inner helper to convert values to float64 numpy array, replacing None with 0.0.
- [ ] `BaserunnerStealSimilarityEngine._apply_shrinkage` _(method)_ - Apply empirical Bayes shrinkage to all profiles using league averages.
- [ ] `BaserunnerStealSimilarityEngine.query` _(method)_ - Score player-season against all profiles, optionally return top-N results.
- [ ] `BaserunnerStealSimilarityEngine.query_pair` _(method)_ - Compute similarity between two specific player-season pairs.
- [ ] `BaserunnerStealSimilarityEngine.get_profile` _(method)_ - Retrieve BaserunnerStealProfile for given player-season.
- [ ] `BaserunnerStealSimilarityEngine.profile_count` _(method)_ - Property returning count of profiles in index.
- [ ] `BaserunnerStealSimilarityEngine.profile_ids` _(method)_ - Return list of all (player_id, season) tuples in index.
- [ ] `build_similarity_matrix` _(function)_ - Construct symmetric NÃ—N similarity matrix for given runner-season list.

### `similarity/engines/pitcher_steal_similarity.py`  _(38)_
_Pitcher steal-prevention similarity scoring engine with RBF-based matching._

- [ ] `OUTCOME_FEATURES` _(constant)_ - Feature definitions with weights for steal-prevention outcome scoring.
- [ ] `WEIGHT_OUTCOME` _(constant)_ - Sub-score weight for outcome features (normalized to 1.0).
- [ ] `RBF_SIGMA_OUTCOME` _(constant)_ - RBF bandwidth parameter for outcome feature similarity.
- [ ] `EB_N_PRIOR` _(constant)_ - Empirical Bayes prior baserunner event count for stabilization.
- [ ] `MIN_BASERUNNER_EVENTS` _(constant)_ - Minimum innings with runners on for pitcher inclusion.
- [ ] `PitcherStealProfile` _(class)_ - Pitcher-season profile for steal-prevention similarity scoring.
- [ ] `SimilarityResult` _(class)_ - One entry in similarity query output with scores and metadata.
- [ ] `WeightedRBFSimilarity` _(class)_ - RBF kernel-based similarity scorer with feature-weighted distances.
- [ ] `WeightedRBFSimilarity.__init__` _(method)_ - Initialize RBF scorer with sigma and feature reliability weights.
- [ ] `WeightedRBFSimilarity.score` _(method)_ - Compute RBF similarity score between two feature vectors.
- [ ] `WeightedRBFSimilarity.score_batch` _(method)_ - Compute RBF similarity scores for query against candidate matrix.
- [ ] `EmpiricalBayesShrinkage` _(class)_ - Empirical Bayes shrinkage estimator using sample-size credibility.
- [ ] `EmpiricalBayesShrinkage.__init__` _(method)_ - Initialize shrinkage estimator with prior event count.
- [ ] `EmpiricalBayesShrinkage.alpha` _(method)_ - Compute credibility weight (data weight) for shrinkage formula.
- [ ] `EmpiricalBayesShrinkage.shrink` _(method)_ - Apply Empirical Bayes shrinkage to raw feature vector.
- [ ] `FeatureNormalizer` _(class)_ - Standardizes features using population mean and standard deviation.
- [ ] `FeatureNormalizer.fit` _(method)_ - Compute outcome feature normalization parameters from pitcher profiles.
- [ ] `FeatureNormalizer._fit` _(function)_ - Helper to compute mean and std from feature matrix.
- [ ] `FeatureNormalizer._norm` _(method)_ - Apply (v - mean) / std normalization with NaN handling.
- [ ] `FeatureNormalizer.normalize_outcome` _(method)_ - Normalize outcome feature vector using fitted parameters.
- [ ] `PitcherStealPartition` _(class)_ - Scoring partition: stores and scores all profiles against query.
- [ ] `PitcherStealPartition.__init__` _(method)_ - Initialize empty partition with profile storage and cache.
- [ ] `PitcherStealPartition.build` _(method)_ - Cache normalized profiles and EB alphas for batch scoring.
- [ ] `PitcherStealPartition.score_all` _(method)_ - Score query pitcher against all candidates, excluding self-matches.
- [ ] `PitcherStealSimilarityEngine` _(class)_ - Main engine: builds profiles, applies shrinkage, scores pitcher similarity.
- [ ] `PitcherStealSimilarityEngine.__init__` _(method)_ - Initialize engine with DuckDB path and empty state.
- [ ] `PitcherStealSimilarityEngine.apply_calibration` _(method)_ - Update RBF sigma from calibration report (SIM-406).
- [ ] `PitcherStealSimilarityEngine.build` _(method)_ - Load profiles, apply shrinkage, normalize features, build partition.
- [ ] `PitcherStealSimilarityEngine._load_league_averages` _(method)_ - Load seasonal outcome league averages from derived.league_averages.
- [ ] `PitcherStealSimilarityEngine._load_profiles` _(method)_ - Load pitcher-steal profiles from derived.pitcher_steal_metrics.
- [ ] `PitcherStealSimilarityEngine._load_profiles._v` _(function)_ - Helper to convert feature values to numpy array, replacing None with 0.0.
- [ ] `PitcherStealSimilarityEngine._apply_shrinkage` _(method)_ - Apply EB shrinkage to each pitcher profile using league average.
- [ ] `PitcherStealSimilarityEngine.query` _(method)_ - Return top-n similar pitchers for a given pitcher-season.
- [ ] `PitcherStealSimilarityEngine.query_pair` _(method)_ - Compute pairwise similarity score between two pitcher-seasons.
- [ ] `PitcherStealSimilarityEngine.get_profile` _(method)_ - Retrieve pitcher-season profile or None if not found.
- [ ] `PitcherStealSimilarityEngine.profile_count` _(method)_ - Property returning total number of loaded pitcher-season profiles.
- [ ] `PitcherStealSimilarityEngine.profile_ids` _(method)_ - Return list of all (pitcher_id, season) tuples in engine.
- [ ] `build_similarity_matrix` _(function)_ - Construct symmetric similarity matrix for pitcher_ids list.

### `similarity/engines/pitch_pitch_similarity.py`  _(17)_
_Pitch-to-pitch FAISS nearest-neighbor engine for finding similar pitches by physics._

- [ ] `PitchVector` _(class)_ - Query vector for pitch-to-pitch FAISS index.
- [ ] `PitchVector.to_array` _(method)_ - Convert query vector to numpy array.
- [ ] `NearestPitch` _(class)_ - One entry in pitch K-nearest-neighbor query output.
- [ ] `PitchNormalizer` _(class)_ - Z-score normalizer for pitch feature vectors.
- [ ] `PitchNormalizer.fit` _(method)_ - Fit normalization parameters from pitch pool.
- [ ] `PitchNormalizer.normalize` _(method)_ - Z-score normalize single pitch vector.
- [ ] `PitchNormalizer.normalize_batch` _(method)_ - Z-score normalize batch of pitch vectors.
- [ ] `PitchPitchSimilarityEngine` _(class)_ - Main pitch-to-pitch FAISS engine for nearest-neighbor search.
- [ ] `PitchPitchSimilarityEngine.build` _(method)_ - Load pitch pool and build FAISS index.
- [ ] `PitchPitchSimilarityEngine._load_pool` _(method)_ - Load pitch pool from DuckDB and return feature matrix.
- [ ] `PitchPitchSimilarityEngine._apply_recency_boost` _(method)_ - Replicate recent seasons in index for recency weighting.
- [ ] `PitchPitchSimilarityEngine._build_faiss_index` _(method)_ - Build and return FAISS index from scaled vectors.
- [ ] `PitchPitchSimilarityEngine.query` _(method)_ - Return K nearest pitches to query vector.
- [ ] `PitchPitchSimilarityEngine.query_batch` _(method)_ - Batch query path for multiple pitch vectors.
- [ ] `PitchPitchSimilarityEngine.index_size` _(method)_ - Return count of indexed pitches.
- [ ] `PitchPitchSimilarityEngine.is_built` _(method)_ - Check if index has been built.
- [ ] `_f` _(function)_ - Coerce DuckDB Decimals/None to float with NaN handling.

### `similarity/engines/batted_ball_similarity.py`  _(20)_
_Batted-ball-to-batted-ball FAISS engine for outcome prediction via launch conditions._

- [ ] `BattedBallVector` _(class)_ - Query vector for batted-ball FAISS index.
- [ ] `BattedBallVector.to_array` _(method)_ - Convert to numpy array.
- [ ] `NearestBattedBall` _(class)_ - One entry in batted-ball K-nearest-neighbor output.
- [ ] `BattedBallNormalizer` _(class)_ - Z-score normalizer for batted-ball vectors.
- [ ] `BattedBallNormalizer.fit` _(method)_ - Fit normalization parameters.
- [ ] `BattedBallNormalizer.normalize` _(method)_ - Z-score normalize single vector.
- [ ] `BattedBallNormalizer.normalize_batch` _(method)_ - Z-score normalize batch of vectors.
- [ ] `BattedBallSimilarityEngine` _(class)_ - Main batted-ball FAISS engine for outcome prediction.
- [ ] `BattedBallSimilarityEngine.build` _(method)_ - Load outcome pool and build FAISS index.
- [ ] `BattedBallSimilarityEngine._select_spray_column` _(method)_ - Detect SIM-051 spray column or fall back to raw.
- [ ] `BattedBallSimilarityEngine._load_pool` _(method)_ - Load outcome pool from DuckDB.
- [ ] `BattedBallSimilarityEngine._apply_recency_boost` _(method)_ - Replicate recent seasons for recency weighting.
- [ ] `BattedBallSimilarityEngine._build_faiss_index` _(method)_ - Build FAISS index from vectors.
- [ ] `BattedBallSimilarityEngine.query` _(method)_ - Return K nearest batted balls.
- [ ] `BattedBallSimilarityEngine.query_batch` _(method)_ - Batch query for multiple vectors.
- [ ] `BattedBallSimilarityEngine.outcome_distribution` _(method)_ - Return outcome probability distribution from K neighbors.
- [ ] `BattedBallSimilarityEngine.index_size` _(method)_ - Return count of indexed batted balls.
- [ ] `BattedBallSimilarityEngine.is_built` _(method)_ - Check if index is built.
- [ ] `BattedBallSimilarityEngine.spray_column_used` _(method)_ - Return spray column name used by loader.
- [ ] `_f` _(function)_ - Coerce values to float with NaN handling.

### `similarity/similarity_calibration.py`  _(28)_
_Empirical calibration of tunable similarity engine constants from population data._

- [ ] `calibrate_sigma` _(function)_ - Find RBF sigma that produces target median similarity score across population.
- [ ] `calibrate_arsenal_gamma` _(function)_ - Find ARSENAL_GAMMA for squared-exponential arsenal W2 distance scoring.
- [ ] `calibrate_eb_prior` _(function)_ - Estimate Empirical Bayes prior strength from variance decomposition.
- [ ] `calibrate_reliability_weights` _(function)_ - Compute per-feature reliability weights via split-half correlation.
- [ ] `CalibrationReport` _(class)_ - Holds all calibrated parameters with their derivation context.
- [ ] `CalibrationReport.SCHEMA_VERSION` _(constant)_ - Bumped if persisted layout changes incompatibly.
- [ ] `CalibrationReport._encode_value` _(method)_ - Convert field value into JSON-safe, round-trippable form.
- [ ] `CalibrationReport._decode_value` _(method)_ - Inverse of _encode_value for reconstructing objects from JSON.
- [ ] `CalibrationReport.to_dict` _(method)_ - Lossless, round-trippable dict of EVERY field.
- [ ] `CalibrationReport.to_json` _(method)_ - Serialize to JSON string round-trippable via from_json.
- [ ] `CalibrationReport.from_dict` _(method)_ - Reconstruct CalibrationReport from to_dict payload dict.
- [ ] `CalibrationReport.from_json` _(method)_ - Reconstruct CalibrationReport from to_json string.
- [ ] `CalibrationReport.summary` _(method)_ - Return formatted string report of all calibrated parameters.
- [ ] `CalibrationReport.equals` _(method)_ - Field-by-field equality handling NDArray fields correctly.
- [ ] `SimilarityCalibrator` _(class)_ - Orchestrates calibration of all similarity engine constants.
- [ ] `SimilarityCalibrator.__init__` _(method)_ - Initialize calibrator with path to DuckDB database.
- [ ] `SimilarityCalibrator.calibrate_from_population` _(method)_ - Tier 1: derive all parameters from population of profiles.
- [ ] `SimilarityCalibrator._calibrate_batter_params` _(method)_ - Load batter profiles and calibrate all batter constants.
- [ ] `SimilarityCalibrator._calibrate_pitcher_params` _(method)_ - Load pitcher profiles and calibrate command-RBF sigma.
- [ ] `SimilarityCalibrator._calibrate_arsenal_params` _(method)_ - Calibrate arsenal W2->score anchor (SIM-346).
- [ ] `SimilarityCalibrator._calibrate_fielder_params` _(method)_ - Tier 1 calibration for fielder similarity engine parameters.
- [ ] `SimilarityCalibrator._calibrate_baserunner_params` _(method)_ - Tier 1 calibration for baserunner extra-base similarity.
- [ ] `SimilarityCalibrator._zscore_matrix` _(method)_ - Z-score columns with std==0 mapped to 1.0.
- [ ] `SimilarityCalibrator._fit_sigma` _(method)_ - Calibrate RBF sigma returning 0.0 for uncalibratable matrices.
- [ ] `SimilarityCalibrator._calibrate_catcher_params` _(method)_ - SIM-406: calibrate catcher engine's four defensive RBF sigmas.
- [ ] `SimilarityCalibrator._calibrate_baserunner_steal_params` _(method)_ - SIM-406: calibrate stolen-base engine's tendency and success sigmas.
- [ ] `SimilarityCalibrator._calibrate_pitcher_steal_params` _(method)_ - SIM-406: calibrate pitcher steal-prevention engine outcome sigma.
- [ ] `SimilarityCalibrator._calibrate_manager_params` _(method)_ - SIM-406: calibrate manager engine's usage, aggression, platoon sigmas.

### `similarity/similarity_diagnostics.py`  _(19)_
_Sanity-check suite validating similarity engine output distributions and pathological patterns._

- [ ] `ScoreDistribution` _(class)_ - Statistics for one score dimension with anomaly flags.
- [ ] `ScoreDistribution.from_array` _(method)_ - Construct distribution from values array.
- [ ] `ScoreDistribution.format_row` _(method)_ - Format distribution as human-readable table row.
- [ ] `DiagnosticReport` _(class)_ - Full diagnostic output for one engine.
- [ ] `run_batter_diagnostics` _(function)_ - Run diagnostics on built BatterSimilarityEngine.
- [ ] `run_pitcher_diagnostics` _(function)_ - Run diagnostics on built PitcherSimilarityEngine.
- [ ] `run_fielder_diagnostics` _(function)_ - Run diagnostics on built FielderSimilarityEngine per position.
- [ ] `run_baserunner_diagnostics` _(function)_ - Run diagnostics on built BaserunnerSimilarityEngine.
- [ ] `run_generic_diagnostics` _(function)_ - Run diagnostics on any RBF engine with standard interface.
- [ ] `_check_dimensional_balance` _(function)_ - Verify sub-score medians are in comparable ranges.
- [ ] `_check_cross_season` _(function)_ - Check same-player cross-season pairs score above median.
- [ ] `_check_cross_season_fielder` _(function)_ - Check same-player same-position cross-season pairs.
- [ ] `_check_symmetry_batter` _(function)_ - Spot-check that score(A,B) == score(B,A) for random pairs.
- [ ] `_check_symmetry_pitcher` _(function)_ - Symmetry check for pitcher pairs.
- [ ] `_check_symmetry_fielder` _(function)_ - Symmetry check for same-position fielder pairs.
- [ ] `_run_synthetic_batter_test` _(function)_ - Build synthetic batter engine and run diagnostics.
- [ ] `_run_synthetic_pitcher_test` _(function)_ - Build synthetic pitcher engine and run diagnostics.
- [ ] `_run_synthetic_fielder_test` _(function)_ - Build synthetic fielder engine and run diagnostics.
- [ ] `_run_synthetic_baserunner_test` _(function)_ - Build synthetic baserunner engine and run diagnostics.

### `similarity/backtesting/backtester.py`  _(14)_
_Probabilistic backtester scoring distributions against labels with ablation framework._

- [ ] `probs_from_dicts` _(function)_ - Stack outcome-probability dicts into probability matrix.
- [ ] `normalize_probs` _(function)_ - Row-normalize probability matrix to valid distributions.
- [ ] `_validate` _(function)_ - Common shape/range validation for metric functions.
- [ ] `_confidence_and_correct` _(function)_ - Compute per-row predicted-class confidence and correctness.
- [ ] `_bin_edges` _(function)_ - Return equal-width confidence bin edges.
- [ ] `reliability_curve` _(function)_ - Compute binned reliability-diagram points.
- [ ] `expected_calibration_error` _(function)_ - Compute expected calibration error (binned, confidence-based).
- [ ] `brier_score` _(function)_ - Compute multi-class Brier score.
- [ ] `log_loss` _(function)_ - Compute multi-class cross-entropy with eps clipping.
- [ ] `evaluate_distributions` _(function)_ - Score predicted distributions against actual labels.
- [ ] `league_average_distribution` _(function)_ - Compute league-average baseline from training marginals.
- [ ] `_column` _(function)_ - Return column of df as list (agnostic to type).
- [ ] `_subset_indices` _(function)_ - Return indices of rows with wanted seasons.
- [ ] `walk_forward_ablation` _(function)_ - Walk-forward test with league-average ablation floor.

### `similarity/backtesting/recency_walk_forward.py`  _(7)_
_SIM-076 walk-forward harness validating recency weighting impact on out-of-sample fit._

- [ ] `walk_forward_folds` _(function)_ - Generate expanding-window walk-forward folds over seasons.
- [ ] `_stack_features` _(function)_ - Return feature matrix from frame/mapping feature column.
- [ ] `recency_weighted_prediction` _(function)_ - Predict outcome via recency-weighted k-NN average.
- [ ] `_mae` _(function)_ - Compute mean absolute error.
- [ ] `_rmse` _(function)_ - Compute root mean squared error.
- [ ] `walk_forward_recency_eval` _(function)_ - Run expanding-window folds and compare weighted vs unweighted error.
- [ ] `_subset` _(function)_ - Boolean-mask df into column dict for predictor.

---

## Phase 3 - API application boot (lifespan -> engine state)

### `api/main.py`  _(11)_
_FastAPI application entry point for MLB Baseball Simulation Platform_

- [ ] `_REQUIRED_ENV_VARS` _(constant)_ - List of required environment variables for basic operation
- [ ] `_REQUIRED_AUTH_ENV_VARS` _(constant)_ - List of auth-related environment variables required outside development
- [ ] `validate_environment` _(function)_ - Validate required environment variables at startup; raises RuntimeError if any missing
- [ ] `_background_prewarm` _(function)_ - Warm sim-runner workers asynchronously in background; never blocks startup
- [ ] `lifespan` _(function)_ - Manage app lifecycle: startup opens pools and engines; shutdown closes connections gracefully
- [ ] `_resim_signal` _(function)_ - Signal handler for re-simulation events; logs signal and stores latest game state
- [ ] `create_app` _(function)_ - Factory function that creates and configures the FastAPI application with all routers
- [ ] `_serve_spa` _(function)_ - SPA catch-all route handler; returns index.html for non-API client routes
- [ ] `health` _(function)_ - Liveness probe endpoint returning application status and environment
- [ ] `ready` _(function)_ - Readiness probe endpoint; checks Postgres, Redis, and engine health; returns 503 if degraded
- [ ] `root` _(function)_ - Root endpoint returning service info, phase, and documentation links

### `api/state.py`  _(24)_
_Boot-time FastAPI resource construction: engines, name resolver, similarity cache._

- [ ] `DEFAULT_DUCKDB_PATH` _(constant)_ - Default DuckDB database path for similarity engine profiles
- [ ] `build_pitcher_engine` _(function)_ - Construct and build PitcherSimilarityEngine from DuckDB, sync CPU+I/O work
- [ ] `ENGINE_REGISTRY` _(constant)_ - Stable name to (module, class) mapping for all 11 similarity engines
- [ ] `_default_engine_loader` _(function)_ - Lazy import engine class from module path for testability
- [ ] `build_all_engines` _(function)_ - Build all 11 similarity engines resilient to individual failures
- [ ] `CALIBRATION_REPORT_PATH_ENV` _(constant)_ - Environment variable name for persisted CalibrationReport JSON path
- [ ] `load_calibration_report` _(function)_ - Load CalibrationReport from JSON with best-effort graceful degradation
- [ ] `load_calibration_map` _(function)_ - Load calibration report and convert to CalibrationMap or identity default
- [ ] `apply_calibration_to_engines` _(function)_ - Wire fitted CalibrationReport into engines supporting RBF/arsenal scorers
- [ ] `NameResolver` _(class)_ - Protocol: async callable mapping player_ids to {id: full_name} dict
- [ ] `NameResolver.__call__` _(method)_ - Async method mapping iterable of player_ids to name dict
- [ ] `make_pg_name_resolver` _(function)_ - Build Postgres-backed NameResolver using ANY($1::int[]) batch query
- [ ] `make_pg_name_resolver._resolve` _(function)_ - Async closure resolving player IDs to names from raw.players table
- [ ] `CACHE_TTL_SECONDS` _(constant)_ - 24-hour TTL for similarity distribution cache entries
- [ ] `make_cache_key` _(function)_ - Build canonical Redis cache key for similarity-distribution payload
- [ ] `SimilarityCache` _(class)_ - Protocol: minimal async cache with get_json and set_json methods
- [ ] `SimilarityCache.get_json` _(method)_ - Async method retrieving JSON value from cache by key
- [ ] `SimilarityCache.set_json` _(method)_ - Async method storing JSON value with TTL in cache
- [ ] `RedisSimilarityCache` _(class)_ - Redis-backed SimilarityCache storing JSON strings with decode-miss handling
- [ ] `RedisSimilarityCache.__init__` _(method)_ - Initialize cache with asyncio Redis client
- [ ] `RedisSimilarityCache.get_json` _(method)_ - Retrieve and deserialize JSON from Redis, treating decode errors as cache miss
- [ ] `RedisSimilarityCache.set_json` _(method)_ - Serialize and store JSON to Redis with expiration time
- [ ] `open_redis_cache` _(function)_ - Open Redis client and wrap it in RedisSimilarityCache, with connection probe
- [ ] `open_pg_pool` _(function)_ - Open asyncpg pool with configurable size (default 2-10) against PostgreSQL DSN

### `api/errors.py`  _(1)_
_App-level exception handling with structured JSON error envelope and request ID correlation_

- [ ] `install_exception_handlers` _(function)_ - Register catch-all unhandled-exception handler on FastAPI app

### `api/auth.py`  _(18)_
_API security primitives including session tokens, rate limiting, and CORS origin resolution_

- [ ] `_is_development` _(function)_ - Check if ENVIRONMENT env var is set to development
- [ ] `_env_flag` _(function)_ - Parse a boolean environment variable with fallback default
- [ ] `_configured_api_keys` _(function)_ - Parse comma-separated API_KEYS env var into a set of non-empty keys
- [ ] `_session_ttl_seconds` _(function)_ - Return session lifetime in seconds from SESSION_TTL_HOURS env
- [ ] `_get_secret_key` _(function)_ - Get HMAC signing key from SECRET_KEY env with dev sentinel fallback
- [ ] `_get_auth_password` _(function)_ - Get shared browser login password from AUTH_PASSWORD env with dev default
- [ ] `_b64url_encode` _(function)_ - Encode bytes to base64url string without padding
- [ ] `_b64url_decode` _(function)_ - Decode base64url string to bytes with auto-padding
- [ ] `_mint_session_token` _(function)_ - Create HMAC-SHA256 signed session token with exp and iat claims
- [ ] `_verify_session_token` _(function)_ - Verify session token signature and expiry, returning payload or None
- [ ] `cookie_kwargs` _(function)_ - Build kwargs for set_cookie/delete_cookie with security settings
- [ ] `require_auth` _(function)_ - FastAPI dependency accepting valid session cookie or API key header
- [ ] `RateLimitMiddleware` _(class)_ - In-memory sliding-window rate limiter keyed by API key or client IP
- [ ] `RateLimitMiddleware.dispatch` _(method)_ - ASGI middleware dispatch that checks rate limits and rejects with 429
- [ ] `RateLimitMiddleware._client_key` _(method)_ - Static method that returns API-key-based or IP-based bucket key
- [ ] `LatencyMiddleware` _(class)_ - ASGI timing middleware that computes rolling p95 API latency
- [ ] `LatencyMiddleware.dispatch` _(method)_ - Record request wall-clock time and update rolling p95 gauge on app.state
- [ ] `resolve_cors_origins` _(function)_ - Resolve CORS allowlist from CORS_ORIGINS/FRONTEND_URL env or defaults

### `api/serialization.py`  _(2)_
_Recursive numpy-to-JSON converter for safe serialization of internal dataclass structures_

- [ ] `to_jsonable` _(function)_ - Recursively convert objects to JSON-native Python types (int, float, str, list, dict)
- [ ] `_jsonable_key` _(function)_ - Coerce mapping keys to JSON-safe values unwrapping numpy/enum to Python scalars

### `api/schemas.py`  _(58)_
_Pydantic v2 models for API response serialization, converting internal dataclasses to JSON-safe wire contracts_

- [ ] `_ApiModel` _(class)_ - Shared base class for all SIM-350 response models with strict validation
- [ ] `ConfidenceIntervalModel` _(class)_ - Response model for confidence intervals with point, bounds, and derived half_width
- [ ] `ConfidenceIntervalModel.from_dataclass` _(method)_ - Build from a simulation.results.ConfidenceInterval dataclass
- [ ] `GameSimSummaryModel` _(class)_ - Response model for game simulation summary with optional raw iteration arrays
- [ ] `GameSimSummaryModel.from_dataclass` _(method)_ - Build from GameSimSummary, optionally including raw score arrays
- [ ] `GameSimSummaryLite` _(class)_ - Array-free projection of GameSimSummaryModel for list endpoints
- [ ] `GameSimSummaryLite.from_dataclass` _(method)_ - Build array-free projection from GameSimSummary
- [ ] `PlayerStatLineModel` _(class)_ - Response model for individual player batting/pitching statistics
- [ ] `PlayerStatLineModel.from_dataclass` _(method)_ - Build from simulation.sim_loop.PlayerStatLine dataclass
- [ ] `BoxScoreModel` _(class)_ - Response model for game box score with per-player lines keyed by player ID
- [ ] `BoxScoreModel.from_dataclass` _(method)_ - Build from simulation.sim_loop.BoxScore dataclass
- [ ] `CalibrationMapModel` _(class)_ - Response model for CalibrationMap with serializable name field
- [ ] `CalibrationMapModel.from_dataclass` _(method)_ - Build from simulation.win_probability.CalibrationMap dataclass
- [ ] `WinProbabilityModel` _(class)_ - Response model for calibrated home/away win probability with confidence intervals
- [ ] `WinProbabilityModel.from_dataclass` _(method)_ - Build from simulation.win_probability.WinProbability dataclass
- [ ] `PlayerRefModel` _(class)_ - Response model for a player reference with ID and optional display label
- [ ] `PlayerRefModel.from_dataclass` _(method)_ - Build from simulation.snapshots.PlayerRef dataclass
- [ ] `_optional_player_ref` _(function)_ - Convert Optional[PlayerRef] to Optional[PlayerRefModel]
- [ ] `FieldSnapshotModel` _(class)_ - Response model for field state with positions, baserunners, and game context
- [ ] `FieldSnapshotModel.from_dataclass` _(method)_ - Build from simulation.snapshots.FieldSnapshot dataclass
- [ ] `PlayByPlayEntryModel` _(class)_ - Response model for a single pitch with outcome, contact, and event data
- [ ] `PlayByPlayEntryModel.from_dataclass` _(method)_ - Build from simulation.snapshots.PlayByPlayEntry dataclass
- [ ] `PlayByPlayModel` _(class)_ - Response model for full play-by-play with pagination support
- [ ] `PlayByPlayModel.from_dataclass` _(method)_ - Build from simulation.snapshots.PlayByPlay dataclass
- [ ] `StateAtPitchModel` _(class)_ - Response model for game state at a specific pitch with field snapshot
- [ ] `StateAtPitchModel.from_dataclass` _(method)_ - Build from simulation.snapshots.StateAtPitch dataclass
- [ ] `MetricDeltaModel` _(class)_ - Response model for baseline-vs-override metric delta
- [ ] `MetricDeltaModel.from_dataclass` _(method)_ - Build from simulation.snapshots.MetricDelta dataclass
- [ ] `OverrideDeltaModel` _(class)_ - Response model for baseline-vs-override comparison with metrics and description
- [ ] `OverrideDeltaModel.from_dataclass` _(method)_ - Build from simulation.snapshots.OverrideDelta dataclass
- [ ] `PropDistributionModel` _(class)_ - Response model for integer-support PMF of one player prop
- [ ] `PropDistributionModel.from_dataclass` _(method)_ - Build from simulation.prop_distributions.PropDistribution dataclass
- [ ] `PropDistributionSetModel` _(class)_ - Response model for all prop PMFs across all players in a run
- [ ] `PropDistributionSetModel.from_dataclass` _(method)_ - Build from simulation.prop_distributions.PropDistributionSet dataclass
- [ ] `InningLineModel` _(class)_ - Response model for one inning's run cells in the linescore
- [ ] `InningLineModel.from_dataclass` _(method)_ - Build from simulation.linescore.InningLine dataclass
- [ ] `LinescoreModel` _(class)_ - Response model for classic baseball linescore with runs/hits/errors and innings
- [ ] `LinescoreModel.from_dataclass` _(method)_ - Build from simulation.linescore.Linescore dataclass
- [ ] `LinescoreModel.from_jsonable` _(method)_ - Build from persisted to_jsonable dict with derived properties recomputed
- [ ] `PitcherDecisionsModel` _(class)_ - Response model for derived W/L/Save pitcher decisions in a game
- [ ] `PitcherDecisionsModel.from_dataclass` _(method)_ - Build from simulation.pitcher_decisions.PitcherDecisions dataclass
- [ ] `PitcherDecisionsModel.from_jsonable` _(method)_ - Build from persisted to_jsonable dict defensively coercing types
- [ ] `BoxscoreCardRowModel` _(class)_ - Response model for one player's prop means in the boxscore card
- [ ] `BoxscoreCardRowModel.from_prop_map` _(method)_ - Build from a prop_name->PropDistribution map extracting means
- [ ] `BoxscoreCardModel` _(class)_ - Response model for SIM-366 per-player boxscore-average card
- [ ] `BoxscoreCardModel.from_prop_set` _(method)_ - Build from PropDistributionSet extracting all players' prop means
- [ ] `CLVModel` _(class)_ - Response model for closing line value with entry/close fairness and beat_close flag
- [ ] `CLVModel.from_dataclass` _(method)_ - Build from betting.clv_engine.CLV dataclass
- [ ] `EdgeReportModel` _(class)_ - Response model for market edge/EV/CLV report for one market side
- [ ] `EdgeReportModel.from_dataclass` _(method)_ - Build from betting.clv_engine.EdgeReport dataclass
- [ ] `LiveGameStateResponse` _(class)_ - Response model for live in-progress game state from sim.lineup_state
- [ ] `PropEdgeResponse` _(class)_ - Response model for player prop PMF with optional over/under and edge report
- [ ] `BetSignalModel` _(class)_ - Response model for +EV bet recommendation with edge report
- [ ] `BetSignalModel.from_dataclass` _(method)_ - Build from betting.bet_signal.BetSignal dataclass
- [ ] `LineQuoteModel` _(class)_ - Response model for timestamped odds quote on one market side
- [ ] `LineQuoteModel.from_dataclass` _(method)_ - Build from betting.line_movement.LineQuote dataclass
- [ ] `LineMovementModel` _(class)_ - Response model for line-movement time-series with quotes and derived deltas
- [ ] `LineMovementModel.from_dataclass` _(method)_ - Build from betting.line_movement.LineMovement dataclass

### `api/websocket/schemas.py`  _(7)_
_Typed Pydantic models for WebSocket events broadcast to frontend clients_

- [ ] `WsEventType` _(class)_ - Discriminant enum for WebSocket event types
- [ ] `LiveGameState` _(class)_ - Response model for structured live game state from pipeline
- [ ] `LiveOdds` _(class)_ - Response model for odds snapshot included in game state update
- [ ] `GameStateUpdateEvent` _(class)_ - WebSocket event broadcast after every live-game state refresh
- [ ] `ResimPendingEvent` _(class)_ - WebSocket event broadcast when plate-appearance ends and re-sim queued
- [ ] `PingEvent` _(class)_ - Server-initiated keep-alive probe event
- [ ] `PongEvent` _(class)_ - Server response to client ping event

---

## Phase 4 - API request routing

### `api/routes/_common.py`  _(2)_
_Shared route helpers factored to eliminate code duplication across routers_

- [ ] `_get_pool` _(function)_ - Get asyncpg pool from app.state or raise 503 if unattached
- [ ] `_row_get` _(function)_ - Read column value from asyncpg Record or plain dict uniformly

### `api/routes/games.py`  _(44)_
_Game simulation endpoints (SIM-355/358/359/362/364/366/386/390)_

- [ ] `PRODUCTION_FACTORY_REF` _(constant)_ - Production machine-factory dotted ref string
- [ ] `resolve_factory_ref` _(function)_ - Resolve machine-factory dotted ref from request/env/default precedence
- [ ] `_get_sim_cache` _(function)_ - Get optional sim cache from app.state or None
- [ ] `_get_sim_duckdb` _(function)_ - Get optional sim DuckDB connection from app.state or None
- [ ] `_build_runner` _(function)_ - Get shared BatchRunner or build transient runner per request
- [ ] `GameCard` _(class)_ - Response model for one scheduled game row with SIM-383 enrichment
- [ ] `GamesOnDateResponse` _(class)_ - Response envelope for GET /api/games/{date} with game list
- [ ] `SimulateResponse` _(class)_ - Response envelope for GET /api/games/{game_pk}/simulate
- [ ] `SubstitutionSlot` _(class)_ - Single targeted player substitution at batting-order slot (SIM-388)
- [ ] `RosterOverride` _(class)_ - POST request body for /simulate/with_override (SIM-358/388)
- [ ] `WithOverrideResponse` _(class)_ - Response envelope for POST /simulate/with_override with baseline/override/delta
- [ ] `GameStatus` _(class)_ - 3-state game status enum (scheduled/live/final/postponed)
- [ ] `GameCardAggregateResponse` _(class)_ - Response for GET /api/games/{game_pk}/status aggregating identity/status/sim
- [ ] `_team_records_cte` _(function)_ - Build team-records CTE body with date cutoff expression for SIM-383
- [ ] `_sim_summary_lite_from_stored` _(function)_ - Rebuild GameSimSummaryLite from stored jsonable dict, best-effort
- [ ] `_sim_kwargs_from_state` _(function)_ - Build simulate_game kwargs dict from resolved GameState
- [ ] `_resolve_park_run_factor` _(function)_ - SIM-411: resolve venue run park-factor from Postgres+DuckDB sources
- [ ] `_apply_override` _(function)_ - Apply RosterOverride to base sim_kwargs (lineupsâ†’substitutionsâ†’pitcher/bat)
- [ ] `_resolve_state_or_error` _(function)_ - Resolve GameState via SIM-353, mapping lineup errors to HTTP errors
- [ ] `_run_batch` _(function)_ - Run Monte-Carlo batch sync (offloaded to worker thread)
- [ ] `_player_ref_from_jsonable` _(function)_ - Rebuild Optional[PlayerRef] from jsonable dict (None when empty)
- [ ] `_state_at_pitch_model_from_snapshot` _(function)_ - Rebuild StateAtPitchModel from stored jsonable StateAtPitch snapshot
- [ ] `_record_and_build` _(function)_ - Record one game and build play-stream/state/linescore/decisions artifacts
- [ ] `_persist_replay_artifacts` _(function)_ - Best-effort persist /plays+/state replay artifacts to DuckDB+Postgres
- [ ] `_resolve_lineup_best_effort` _(function)_ - Resolve ResolvedLineup for defense map or None on any failure (SIM-363)
- [ ] `_parse_date` _(function)_ - Parse YYYY-MM-DD path param, raise 422 on malformed date
- [ ] `_opt_str` _(function)_ - Get optional str from row column or None when absent/NULL
- [ ] `_opt_int` _(function)_ - Get optional int from row column or None when absent/NULL
- [ ] `_game_card` _(function)_ - Map raw.games row to GameCard with SIM-383 enrichment
- [ ] `get_games_on_date` _(function)_ - GET /api/games/{date}: list games scheduled on a date (SIM-355)
- [ ] `get_game_status_card` _(function)_ - GET /api/games/{game_pk}/status: aggregated identity/status/sim card
- [ ] `get_live_game_state` _(function)_ - GET /api/games/{game_pk}/live: read live in-progress game state (SIM-386)
- [ ] `simulate_game_endpoint` _(function)_ - GET /api/games/{game_pk}/simulate: Monte-Carlo simulation endpoint (SIM-355)
- [ ] `simulate_with_override_endpoint` _(function)_ - POST /api/games/{game_pk}/simulate/with_override: baseline vs override diff (SIM-358)
- [ ] `get_game_plays` _(function)_ - GET /api/games/{game_pk}/plays: persisted pitch-level play-by-play (SIM-357)
- [ ] `get_game_state_at_pitch` _(function)_ - GET /api/games/{game_pk}/state/{at_bat}/{pitch}: field snapshot at pitch
- [ ] `GameCardResponse` _(class)_ - Response envelope for linescore+pitcher decisions game card (SIM-362/364)
- [ ] `_load_game_card_or_error` _(function)_ - Load persisted game card from DuckDB, mapping no-store/no-card to errors
- [ ] `get_game_linescore` _(function)_ - GET /api/games/{game_pk}/linescore: per-inning R/H/E grid (SIM-362)
- [ ] `get_game_decisions` _(function)_ - GET /api/games/{game_pk}/decisions: W/L/Save pitcher decisions (SIM-364)
- [ ] `get_game_card` _(function)_ - GET /api/games/{game_pk}/card: combined linescore+decisions response
- [ ] `_build_prop_set` _(function)_ - Build PropDistributionSet from N-game boxscore batch (SIM-366)
- [ ] `get_game_boxscore` _(function)_ - GET /api/games/{game_pk}/boxscore: per-player prop means card (SIM-366)
- [ ] `get_player_prop_edge` _(function)_ - GET /api/games/{game_pk}/props/{player_id}/{prop}: PMF+edge report (SIM-390)

### `api/routes/betting.py`  _(16)_
_Betting API endpoints for market edges, bet signals, line movement, and CLV snapshots_

- [ ] `_mock_odds` _(function)_ - Get deterministic mock odds dict for a game and market type
- [ ] `_resolve_price` _(function)_ - Pick price from injected value or mock, returning (price, was_injected)
- [ ] `_safe_report` _(function)_ - Build EdgeReport skipping degenerate sim probabilities
- [ ] `_build_edge_reports` _(function)_ - Build EdgeReports for requested markets off sim and odds
- [ ] `_parse_markets` _(function)_ - Parse comma-separated markets query param with validation
- [ ] `_summary_and_winprob` _(function)_ - Resolve lineup, run/reuse sim, and return (summary, win_prob)
- [ ] `EdgesResponse` _(class)_ - Response envelope for GET /api/betting/games/{game_pk}/edges
- [ ] `SignalsResponse` _(class)_ - Response envelope for GET /api/betting/games/{game_pk}/signals
- [ ] `LineMovementResponse` _(class)_ - Response envelope for GET /api/betting/games/{game_pk}/line-movement
- [ ] `ClvSnapshotResponse` _(class)_ - Response envelope for GET /api/betting/games/{game_pk}/clv
- [ ] `_validate_market_type` _(function)_ - Validate market_type is one of moneyline/runline/total
- [ ] `_fetch_movements` _(function)_ - Acquire conn and run fetch_line_movement with pool-or-connection handling
- [ ] `get_game_edges` _(function)_ - SIM-367/339: Run sim and build per-market EdgeReports
- [ ] `get_game_signals` _(function)_ - SIM-369: Build EdgeReports and gate to ranked +EV BetSignals
- [ ] `get_game_line_movement` _(function)_ - SIM-368: Read persisted raw.game_odds history and build line-movement series
- [ ] `get_game_clv` _(function)_ - SIM-339/368: Projection of line-movement with only CLV-bearing series

### `api/routes/similarity.py`  _(19)_
_Similarity Score Explorer endpoints for pitcher distribution queries with caching_

- [ ] `get_pitcher_engine` _(function)_ - FastAPI dependency returning singleton PitcherSimilarityEngine from app.state
- [ ] `get_name_resolver` _(function)_ - FastAPI dependency returning NameResolver with fallback placeholder
- [ ] `get_similarity_cache` _(function)_ - FastAPI dependency returning Redis cache or None
- [ ] `_BinSpec` _(class)_ - Frozen dataclass for linear histogram bin edges and indexing
- [ ] `_BinSpec.linear` _(method)_ - Create BinSpec with even bin width
- [ ] `_BinSpec.index_for` _(method)_ - Compute bin index for a score with clamping and inclusive right edge
- [ ] `_BinSpec.edges` _(method)_ - Return (lo, hi) edges for a bin index
- [ ] `compute_score_summary` _(function)_ - Compute min/p25/median/p75/max/mean/std over full population
- [ ] `classify_diagnostic` _(function)_ - Classify engine output health (COLLAPSED/NO_SPREAD/HEALTHY)
- [ ] `_result_to_member` _(function)_ - Convert SimilarityResult to member dict with resolved name
- [ ] `build_bins` _(function)_ - Construct histogram bin payload from engine's scored results
- [ ] `build_top_n` _(function)_ - Extract top N members from results with resolved names
- [ ] `_Member` _(class)_ - Response model for pitcher appearance in bin/preview/top-N
- [ ] `_Bin` _(class)_ - Response model for histogram bin with preview and members
- [ ] `_ScoreSummary` _(class)_ - Response model for percentile and central tendency statistics
- [ ] `_Diagnostic` _(class)_ - Response model for engine health classification
- [ ] `_Query` _(class)_ - Response model for query parameters echo
- [ ] `SimilarityDistributionResponse` _(class)_ - Response envelope for similarity histogram endpoint
- [ ] `get_pitcher_similarity_distribution` _(function)_ - Return pitcher similarity distribution binned for histogram with caching

### `api/routes/metrics.py`  _(7)_
_Prometheus metrics endpoint with optional prometheus_client fallback to hand-rolled exporter_

- [ ] `record_sim_latency` _(function)_ - Record wall-clock latency of most recent simulation run
- [ ] `record_request` _(function)_ - Increment served-request counter on app.state
- [ ] `_pipeline_freshness_seconds` _(function)_ - Compute seconds since last re-sim signal or -1 if none observed
- [ ] `_collect` _(function)_ - Snapshot current scalar readings for both render paths
- [ ] `_metric_block` _(function)_ - Format metrics in Prometheus text exposition with HELP and TYPE lines
- [ ] `_render_fallback` _(function)_ - Render metrics in Prometheus text exposition format by hand
- [ ] `metrics` _(function)_ - Expose application metrics in Prometheus text exposition format

### `api/routes/data_health.py`  _(4)_
_Data freshness and health API for UI badge showing current ingest watermark and coverage_

- [ ] `SeasonCoverage` _(class)_ - Response model for per-season ingest coverage (games and pitches)
- [ ] `DataFreshnessResponse` _(class)_ - Response model for aggregate ingest freshness and per-season coverage
- [ ] `_iso` _(function)_ - Convert value to ISO-8601 string or None
- [ ] `get_data_freshness` _(function)_ - SIM-417: Return aggregate ingest watermark and per-season coverage

### `api/routes/auth.py`  _(5)_
_Browser session auth endpoints for login, status check, and logout_

- [ ] `LoginRequest` _(class)_ - Request model for POST /auth/login with password
- [ ] `AuthStatusResponse` _(class)_ - Response model for GET /auth/me with auth status and TTL
- [ ] `login` _(function)_ - POST /auth/login: Validate password and issue httpOnly session cookie
- [ ] `me` _(function)_ - GET /auth/me: Probe current session state without enforcing auth
- [ ] `logout` _(function)_ - POST /auth/logout: Clear session cookie

---

## Phase 5 - Simulation setup (per /simulate request)

### `simulation/production_factory.py`  _(18)_
_Production DuckDB/FAISS-backed machine factory with dependency injection seams._

- [ ] `_kwarg` _(function)_ - Read a sim_kwargs value with factory-only underscore-prefixed key support.
- [ ] `_default_sampler_builder` _(function)_ - Build the production PlayPoolSampler with DB connection and k-NN seeding.
- [ ] `_default_deriver_builder` _(function)_ - Build the FingerprintDeriver from on-disk artifacts or return None.
- [ ] `set_sampler_builder` _(function)_ - Install a sampler builder as active; return the previous one.
- [ ] `set_deriver_builder` _(function)_ - Install a fingerprint-deriver builder; return the previous one.
- [ ] `use_sampler_builder` _(class)_ - Context manager to temporarily inject a sampler builder with auto-restore.
- [ ] `use_sampler_builder.__init__` _(method)_ - Initialize the sampler-builder context manager.
- [ ] `use_sampler_builder.__enter__` _(method)_ - Enter the context and install the builder.
- [ ] `use_sampler_builder.__exit__` _(method)_ - Exit the context and restore the previous builder.
- [ ] `_attach_shared_tiles` _(function)_ - Attach SIM-333 shared-memory tiles zero-copy from the opaque segment registry.
- [ ] `_build_full_pool_sampler` _(function)_ - Build and cache the full-pool sampler or return None when disabled.
- [ ] `reset_caches` _(function)_ - Clear the per-process cached full-pool sampler for test isolation.
- [ ] `_warm_sampler` _(function)_ - Trigger lazy per-hand precomputes on a FullPoolSampler for worker warmth.
- [ ] `warm_worker_cache` _(function)_ - Populate this process's full-pool sampler cache for first-game responsiveness.
- [ ] `_manager_enabled` _(function)_ - Test whether SIM_MANAGER env flag enables the manager wiring.
- [ ] `_default_bullpen_for_spec` _(function)_ - Build a generic per-team bullpen from synthetic deterministic pitcher IDs.
- [ ] `set_bullpen_builder` _(function)_ - Install a bullpen builder as active; return the previous one.
- [ ] `production_machine_factory` _(function)_ - Build a real DuckDB/FAISS-backed StateMachine for a worker per-game.

### `simulation/lineup_resolver.py`  _(22)_
_Runtime lineup/substitution resolver from Postgres raw.game_lineups data._

- [ ] `LineupResolutionError` _(class)_ - Exception raised when a lineup cannot be resolved into a usable GameState.
- [ ] `LineupNotIngestedError` _(class)_ - Exception for transient lineup absence before MLB publishes the data.
- [ ] `LineupSlot` _(class)_ - One resolved occupant of one batting-order slot with substitution provenance.
- [ ] `LineupSlot.is_substitution` _(method)_ - Property: True when this occupant arrived via substitution, not the starter.
- [ ] `TeamLineup` _(class)_ - A resolved batting order for one side plus the current pitcher.
- [ ] `TeamLineup.batting_order_ids` _(method)_ - Property: ordered list of batter ids for GameState.*_lineup field.
- [ ] `ResolvedLineup` _(class)_ - Both sides' resolved lineups for a game ready to build a GameState.
- [ ] `_row_get` _(function)_ - Read a key from asyncpg Record or plain dict uniformly.
- [ ] `_occupant_takes_effect` _(function)_ - Test whether a lineup row's occupant has taken effect by as_of_at_bat.
- [ ] `_pick_team_slots` _(function)_ - Reduce one team's raw rows to ordered batting slots and current pitcher id.
- [ ] `resolve_lineup_from_rows` _(function)_ - Assemble a ResolvedLineup from raw raw.game_lineups rows (pure, no I/O).
- [ ] `build_game_state` _(function)_ - Build a fresh top-of-the-1st GameState from a resolved lineup.
- [ ] `build_team_defense_map` _(function)_ - Map one team's resolved lineup to {defensive-slot name: player_id}.
- [ ] `build_defense_map` _(function)_ - Map the fielding team's lineup to defensive-slot-name-to-player_id mapping.
- [ ] `fielding_side_for_half` _(function)_ - Determine which team fields for a given half (TOP->HOME, BOTTOM->AWAY).
- [ ] `build_defense_map_for_state` _(function)_ - Convenience: pick fielding side from state.half and build its defense map.
- [ ] `_normalize_side` _(function)_ - Coerce a side selector (Team/string/int) to a Team enum.
- [ ] `fetch_game_sides` _(function)_ - Read raw.games row mapping team ids to home/away sides.
- [ ] `fetch_lineup_rows` _(function)_ - Read all raw.game_lineups rows for a game (all sequences).
- [ ] `fetch_player_hands` _(function)_ - Read bats/throws for given players in one round trip.
- [ ] `resolve_lineup` _(function)_ - Read Postgres and return the resolved (substitution-applied) lineup.
- [ ] `resolve_game_state` _(function)_ - One-call convenience: Postgres game_pk to a fresh GameState.

### `simulation/matchup_provider.py`  _(14)_
_Production MatchupProfileProvider and tile-space normalization artifact loaders._

- [ ] `write_norm` _(function)_ - Persist fitted normalizer mean/std as JSON in the pool directory.
- [ ] `read_norm` _(function)_ - Load persisted (mean, std) normalizer stats or None if absent.
- [ ] `write_centroids` _(function)_ - Persist a per-matchup raw centroid map as JSON in the pool directory.
- [ ] `read_centroids` _(function)_ - Load a centroid map written by write_centroids (empty on miss).
- [ ] `pitch_key` _(function)_ - Build the canonical pitch-centroid lookup key from season/pitcher/bat_hand.
- [ ] `battedball_key` _(function)_ - Build the canonical batted-ball-centroid lookup key from season/bat_hand.
- [ ] `PrecomputedMatchupProvider` _(class)_ - Resolve a MatchupProfile from precomputed per-matchup centroids with fallback.
- [ ] `PrecomputedMatchupProvider.__init__` _(method)_ - Initialize the provider with pitch and batted-ball centroid maps.
- [ ] `PrecomputedMatchupProvider._pitch_centroid` _(method)_ - Look up pitch centroid with season/pitcher/hand fallback to global mean.
- [ ] `PrecomputedMatchupProvider._battedball_centroid` _(method)_ - Look up batted-ball centroid with fallback to global mean.
- [ ] `PrecomputedMatchupProvider.__call__` _(method)_ - Resolve a MatchupProfile from current matchup parameters.
- [ ] `load_pitch_norm` _(function)_ - Build the deriver's pitch normalizer from persisted stats or None.
- [ ] `load_battedball_norm` _(function)_ - Build the deriver's batted-ball normalizer from persisted stats or None.
- [ ] `load_provider` _(function)_ - Build a PrecomputedMatchupProvider from on-disk centroids or None on miss.

### `simulation/game_state.py`  _(29)_
_Mutable GameState and PlayResult dataclass contracts for SIM-311/SIM-310 spec._

- [ ] `Half` _(class)_ - IntEnum: which half of inning (TOP=0 away bats, BOTTOM=1 home bats).
- [ ] `Team` _(class)_ - IntEnum: the two sides (AWAY=0, HOME=1).
- [ ] `Bases` _(class)_ - Base-occupancy state with optional runner identities (SIM-311).
- [ ] `Bases.runners_state` _(method)_ - Property: 3-bit base-occupancy bitmask for run_resolution encoding.
- [ ] `Bases.occupancy` _(method)_ - Property: (on_1B, on_2B, on_3B) bools for occupancy checks.
- [ ] `Bases.count_on_base` _(method)_ - Property: count of runners currently on base (0-3).
- [ ] `Bases.clear` _(method)_ - Empty all bases on half-inning transition.
- [ ] `Bases.assert_consistent` _(method)_ - Lightweight invariant: each occupied base holds a non-negative runner id.
- [ ] `ManagerContext` _(class)_ - Manager/leverage context hook for spec Â§3 pre-pitch and Â§5.3 end-of-PA hooks.
- [ ] `GameState` _(class)_ - Mutable per-game state the simulation loop reads and commits (spec Â§9).
- [ ] `GameState.offense` _(method)_ - Property: the batting team (AWAY in top, HOME in bottom).
- [ ] `GameState.defense` _(method)_ - Property: the fielding team.
- [ ] `GameState.score_diff` _(method)_ - Property: offense score minus defense score (positive=offense leads).
- [ ] `GameState.runners_state` _(method)_ - Property: base-occupancy bitmask in RE24/run_resolution encoding.
- [ ] `GameState.sampler_prefilter` _(method)_ - Return (pitcher_id, bat_hand, season) tuple for sampler tile pre-filtering.
- [ ] `GameState.bat_hand_for` _(method)_ - Get a batter's hand for sampler pre-filter, resolving switch-hitter vs pitcher.
- [ ] `GameState.reset_count` _(method)_ - Zero the ball/strike count on new PA.
- [ ] `GameState.reset_outs` _(method)_ - Zero outs on half-inning transition.
- [ ] `GameState.add_ball` _(method)_ - Increment the ball count.
- [ ] `GameState.add_strike` _(method)_ - Increment the strike count.
- [ ] `GameState.record_out` _(method)_ - Record n outs.
- [ ] `GameState.add_runs` _(method)_ - Credit runs to a team (defaults to current offense).
- [ ] `GameState.is_half_inning_over` _(method)_ - Test whether outs reaches 3 to end the half-inning.
- [ ] `GameState.assert_count_valid` _(method)_ - Lightweight count invariants (balls/strikes within bounds).
- [ ] `GameState.assert_outs_valid` _(method)_ - Lightweight outs invariant (0-2 in-play, 0-3 transient).
- [ ] `GameState.assert_score_valid` _(method)_ - Lightweight invariant: scores are non-negative.
- [ ] `GameState.assert_invariants` _(method)_ - Run all lightweight committed-state invariants together.
- [ ] `PlayResult` _(class)_ - Structured result of one pitch (and resolved PA event on contact).
- [ ] `PlayResult.as_scaffold_dict` _(method)_ - Return the legacy scaffold simulate_pitch dict shape.

### `simulation/constants.py`  _(7)_
_Centralized run-value constants and Statcast event alias mapping_

- [ ] `RUN_VALUES` _(constant)_ - Dict mapping 12 standard plate-appearance outcomes to offensive run values
- [ ] `STATCAST_EVENT_ALIASES` _(constant)_ - Maps Statcast raw event strings to canonical RUN_VALUES keys
- [ ] `CANONICAL_OUTCOME_KEYS` _(constant)_ - Frozenset of all valid RUN_VALUES keys
- [ ] `DEFENSIVE_RUN_VALUES` _(constant)_ - Dict mapping defensive events to runs saved (OAA infield/outfield, blocking, framing)
- [ ] `UnknownEventError` _(class)_ - Exception raised for unknown event strings in strict mode
- [ ] `resolve_event_to_canonical` _(function)_ - Maps Statcast or canonical event strings to canonical RUN_VALUES keys, returns None for unknown
- [ ] `run_value_for_event` _(function)_ - Returns linear-weight run value for an event, with optional strict mode and default fallback

### `simulation/batch_runner.py`  _(55)_
_Parallel 100-iteration Monte-Carlo batch runner with shared-memory zero-copy attach._

- [ ] `default_max_workers` _(function)_ - Compute SIM-281 worker count: min(cpu-1, 10), floored at 1.
- [ ] `derive_seed` _(function)_ - Derive iteration i's per-game seed from base_seed for reproducibility.
- [ ] `GameSpec` _(class)_ - Fully-picklable description of one matchup to simulate (worker input).
- [ ] `GameSpec.cache_key_fields` _(method)_ - Return the hashable identity of this spec for cache-key generation.
- [ ] `_hashable` _(function)_ - Coerce a sim-kwarg value into hashable form for the cache key.
- [ ] `_resolve_dotted` _(function)_ - Resolve a pkg.module:callable dotted reference to the callable.
- [ ] `SharedArrayDescriptor` _(class)_ - Picklable descriptor for reconstructing a zero-copy numpy view over shared memory.
- [ ] `publish_shared_arrays` _(function)_ - Parent-side: copy each read-only array into named SharedMemory segment.
- [ ] `unlink_shared_segments` _(function)_ - Parent-side teardown: close and unlink every owned shared segment exactly once.
- [ ] `_worker_init` _(function)_ - Pool initializer: attach parent's shared-memory segments zero-copy per worker.
- [ ] `get_shared_view` _(function)_ - Worker-side accessor: the zero-copy view attached by _worker_init for name.
- [ ] `_prewarm_worker` _(function)_ - SIM-402 worker-side prewarm task: warm THIS worker's cache with semaphore gating.
- [ ] `_run_one` _(function)_ - Run ONE simulate_game per (spec, seed); mapped across N seeds in pool.
- [ ] `SimCache` _(class)_ - Tiny cache interface the runner depends on (Redis one impl).
- [ ] `SimCache.get` _(method)_ - Get a cached value or None on miss.
- [ ] `SimCache.set` _(method)_ - Cache a value with TTL in seconds.
- [ ] `SimCache.clear` _(method)_ - Clear all cached entries.
- [ ] `NullCache` _(class)_ - No-op cache: every get misses, every set is dropped.
- [ ] `NullCache.get` _(method)_ - Return None (cache miss).
- [ ] `NullCache.set` _(method)_ - Ignore set (no-op).
- [ ] `NullCache.clear` _(method)_ - Ignore clear (no-op).
- [ ] `InMemoryCache` _(class)_ - Process-local TTL cache (Redis fallback for sandbox/tests).
- [ ] `InMemoryCache.__init__` _(method)_ - Initialize the in-memory cache with max size and clock.
- [ ] `InMemoryCache.get` _(method)_ - Get cached value if present and not expired; evict on expiry.
- [ ] `InMemoryCache.set` _(method)_ - Cache a value with TTL, evicting oldest-inserted on size overflow.
- [ ] `InMemoryCache.clear` _(method)_ - Clear all cached entries.
- [ ] `RedisCache` _(class)_ - Redis-backed TTL cache (production backend); handles per-op failures gracefully.
- [ ] `RedisCache.__init__` _(method)_ - Initialize the Redis cache with a client and optional prefix.
- [ ] `RedisCache._k` _(method)_ - Build the full Redis key with the instance prefix.
- [ ] `RedisCache.get` _(method)_ - Get pickled value from Redis or None on miss/error.
- [ ] `RedisCache.set` _(method)_ - Set pickled value in Redis with TTL; swallows errors.
- [ ] `RedisCache.clear` _(method)_ - Delete all prefixed keys from Redis; swallows errors.
- [ ] `make_cache` _(function)_ - Choose a cache backend: Redis if reachable, else in-memory.
- [ ] `BatchResult` _(class)_ - Runner's return: the SIM-327 summary plus run provenance.
- [ ] `BatchRunner` _(class)_ - Run N iterations and aggregate to SIM-327 summary with persistent warm pool.
- [ ] `BatchRunner.__init__` _(method)_ - Initialize the batch runner with cache, worker count, and shared arrays.
- [ ] `BatchRunner.resolve_max_workers` _(method)_ - Compute worker count for this batch: override or SIM-281 ceiling, floored at 1.
- [ ] `BatchRunner._cache_key` _(method)_ - Key the sim cache on (spec + base seed + N).
- [ ] `BatchRunner._ensure_shared_published` _(method)_ - Publish read-only arrays into shared memory exactly once (idempotent).
- [ ] `BatchRunner._pool_kwargs` _(method)_ - Kwargs for ProcessPoolExecutor: initializer + initargs for SIM-333 attach seam.
- [ ] `BatchRunner._get_pool` _(method)_ - Return the warm ProcessPoolExecutor (SIM-360): lazy create, reuse, recreate on worker-count change.
- [ ] `BatchRunner.prewarm` _(method)_ - SIM-402: warm the sim machinery before first request.
- [ ] `BatchRunner.shared_registry` _(method)_ - Property: return the published shared registry (publishing if not yet done).
- [ ] `BatchRunner.close` _(method)_ - Shut warm pool down and release parent-owned shared segments.
- [ ] `BatchRunner.__enter__` _(method)_ - Context manager entry: return self.
- [ ] `BatchRunner.__exit__` _(method)_ - Context manager exit: call close.
- [ ] `BatchRunner.run` _(method)_ - Run the batch and return BatchResult (summary + provenance).
- [ ] `BatchRunner._execute` _(method)_ - Run N games: in-process when max_workers==1, else pooled.
- [ ] `BatchRunner._map_pool` _(method)_ - Submit one _run_one per seed and gather results in seed order.
- [ ] `rng_driven_machine_factory` _(function)_ - Build a no-sampler, rng-driven StateMachine for the worker (always-on path).
- [ ] `_CyclingResolver` _(class)_ - No-DB resolver: in-play is single hit_rate, else out (no sampler).
- [ ] `_CyclingResolver.__init__` _(method)_ - Initialize the resolver with rng and hit rate.
- [ ] `_CyclingResolver.resolve_fielding` _(method)_ - Resolve batted ball to single or field_out based on hit_rate.
- [ ] `_RngOutcomeStateMachine` _(class)_ - StateMachine that draws each pitch outcome from loop rng (no sampler).
- [ ] `_RngOutcomeStateMachine.step_pitch` _(method)_ - Draw pitch outcome from rng distribution; call parent step_pitch.

---

## Phase 6 - Simulation execution (per game iteration)

### `simulation/sim_loop.py`  _(91)_
_SIM-316 plate-appearance state machine with count/out/base/inning control_

- [ ] `strikes_bucket_foul_factor` _(function)_ - Return SIM-056 count-conditional foul multiplier for strike bucket
- [ ] `apply_count_foul_weighting` _(function)_ - Re-weight outcome distribution by count-conditional foul factor and renormalize
- [ ] `_env_flag` _(function)_ - Parse on/off environment flag (e.g. SIM_FULL_POOL) for boolean config
- [ ] `times_through_order` _(function)_ - Calculate how many times through lineup pitcher has faced batters
- [ ] `pitcher_fatigue` _(function)_ - Compute bounded fatigue index from pitch count, TTO, and rest days
- [ ] `tto_effectiveness` _(function)_ - Return effectiveness multiplier from times-through-order penalty decay
- [ ] `platoon_factor` _(function)_ - Compute platoon multiplier (same-hand vs opposite-hand pitcher matchup)
- [ ] `score_reliever` _(function)_ - Score candidate reliever for leverage-adjusted bullpen selection
- [ ] `_safe_float` _(function)_ - Safely coerce value to finite float with NaN/None fallback to default
- [ ] `FieldingSignal` _(class)_ - Frozen dataclass carrying resolved batted-ball fielding signal (hit/out/error)
- [ ] `StealResolution` _(class)_ - Frozen dataclass for steal attempt resolution (safe/caught outcome)
- [ ] `PlayResolver` _(class)_ - Injectable provider of engine-derived fielding and steal signals
- [ ] `PlayResolver.resolve_fielding` _(method)_ - Map sampled batted-ball event to FieldingSignal with result deltas
- [ ] `PlayResolver.resolve_steal` _(method)_ - Resolve steal attempt outcome from stolen_base_pool (default no-op)
- [ ] `_SampledStealPool` _(class)_ - In-memory stand-in for stolen_base_pool with historical success weights
- [ ] `_SampledStealPool.sample` _(method)_ - Draw safe/caught outcome from weighted historical steal results
- [ ] `PitchState` _(class)_ - SIM-303 scaffold: minimal pitch state for single-pitch simulation
- [ ] `CountAdvance` _(class)_ - Result of count advance on one pitch: new count and PA-terminal status
- [ ] `advance_count` _(function)_ - Advance count by pitch outcome; apply SIM-056 two-strike-foul absorbing rule
- [ ] `pitch_outcome_to_event` _(function)_ - Map non-contact terminal pitch outcome to PA-event string or None
- [ ] `PlateAppearanceSimulator` _(class)_ - SIM-303 scaffold: sample one pitch + batted-ball via PlayPoolSampler
- [ ] `PlateAppearanceSimulator.__init__` _(method)_ - Initialize simulator with sampler, k, rng, and optional FingerprintDeriver
- [ ] `PlateAppearanceSimulator._pitch_fingerprint` _(method)_ - Build 10-dim pitch query vector (SIM-317 real or stub hash)
- [ ] `PlateAppearanceSimulator._battedball_fingerprint` _(method)_ - Build 3-dim batted-ball query vector (EV/LA/spray; SIM-317 or stub)
- [ ] `PlateAppearanceSimulator.simulate_pitch` _(method)_ - Simulate one pitch: sample outcome, batted-ball if contact, return legacy dict
- [ ] `StateMachine` _(class)_ - SIM-316 GameState-driven plate-appearance and half-inning state machine
- [ ] `StateMachine.__init__` _(method)_ - Initialize machine with sampler, resolver, manager, bench, bullpen
- [ ] `StateMachine.step_pitch` _(method)_ - Advance game one pitch: readâ†’sample/classifyâ†’commitâ†’emit PlayResult
- [ ] `StateMachine._draw_reweighted_outcome` _(method)_ - SIM-318 Option-A: re-weight distribution, draw outcome from reweighted mix
- [ ] `StateMachine._accept_or_resample_foul` _(method)_ - SIM-318 Option-B: bias foul acceptance by count-conditional factor
- [ ] `StateMachine._jitter_query` _(method)_ - SIM-421: add Gaussian scatter to deriver CENTROID query per pitch
- [ ] `StateMachine._full_pool_outcome` _(method)_ - SIM-424: draw pitch outcome from full-pool sampler (Situation+Pitcher+Batter)
- [ ] `StateMachine._apply_framing` _(method)_ - SIM-428: nudge taken pitch (ballâ†”called_strike) by catcher framing delta
- [ ] `StateMachine._full_pool_fielding` _(method)_ - SIM-425: draw batted ball from full-pool sampler â†’ FieldingSignal
- [ ] `StateMachine._fielder_rbf_nudge` _(method)_ - SIM-425b: nudge singleâ†”out boundary by live defender quality vs pool fielder
- [ ] `StateMachine._tag_rate` _(method)_ - SIM-425: runner-on-3rd tag-up attempt rate from embedding or league default
- [ ] `StateMachine._full_pool_out_advancement` _(method)_ - SIM-425: advance runners on full-pool out (sac fly tag, productive ground)
- [ ] `StateMachine._commit_run_delta` _(method)_ - Resolve run value and base-out delta via resolve_runs; commit and record outs
- [ ] `StateMachine._run_calib` _(method)_ - Resolve SIM-429 run-conversion calibration multiplier from env or default
- [ ] `StateMachine._advance_rate` _(method)_ - Runner extra-base advance probability from embedding or Retrosheet constant
- [ ] `StateMachine._extra_advance` _(method)_ - Return 1 if runner takes extra base beyond hit value; else 0
- [ ] `StateMachine._advance_runners` _(method)_ - Advance runners on bases per hit value; return scored runs; mutate bases
- [ ] `StateMachine._resolve_steal_outcome` _(method)_ - Resolve safe/caught steal: move runner or record out; accumulate stats
- [ ] `StateMachine._move_runner` _(method)_ - Static helper: move runner from base to base (4 == home/off bases)
- [ ] `StateMachine._clear_base` _(method)_ - Static helper: clear runner from specified base
- [ ] `StateMachine._resolve_walk` _(method)_ - Resolve ball-4 walk: force runners and score forced run via resolve_runs
- [ ] `StateMachine._issue_intentional_walk` _(method)_ - Issue manager-signalled IBB without throwing pitch; roll over PA
- [ ] `StateMachine._resolve_strikeout` _(method)_ - Resolve strike-3 strikeout incl. SIM-056 dropped-third-strike edge
- [ ] `StateMachine._dropped_third_strike` _(method)_ - SIM-056 dropped-third-strike eligibility predicate + optional resolver roll
- [ ] `StateMachine._force_on_reach` _(method)_ - Place batter on 1B and force runners; return forced runs (walk/D3K shared)
- [ ] `StateMachine._apply_sac_fly_bias` _(method)_ - SIM-349: nudge fly-out toward sacrifice_fly when manager sac-fly intent set
- [ ] `StateMachine._apply_home_field_bias` _(method)_ - SIM-412: flip home-team batted-ball out to single with small probability
- [ ] `StateMachine._apply_park_factor` _(method)_ - SIM-411: nudge outâ†”hit by venue run park factor (hitter/pitcher park)
- [ ] `StateMachine._resolve_in_play` _(method)_ - Resolve in-play PA: batted-ball sample â†’ fielding â†’ baserunning (step 5/6/7)
- [ ] `StateMachine._end_of_pa` _(method)_ - Run end-of-PA: accumulate stats, advance batting order, reset/roll
- [ ] `StateMachine._box_line` _(method)_ - Lazily create boxscore and return player stat line by id
- [ ] `StateMachine._accumulate_pa` _(method)_ - SIM-328: attribute completed PA to batter and pitcher; track earned/unearned
- [ ] `StateMachine._current_batter_id` _(method)_ - Static helper: get id of batter who just completed THIS PA (SIM-328 attribution)
- [ ] `StateMachine.advance_half_inning` _(method)_ - Roll half-inning at 3 outs: clear bases, reset count, flip half, advance inning
- [ ] `StateMachine._record_outs` _(method)_ - Record n outs with guard against impossible (>3) total
- [ ] `StateMachine._advance_batting_order` _(method)_ - Advance batting team's lineup-slot pointer by one with wrap
- [ ] `StateMachine._set_half_matchup` _(method)_ - Re-point matchup at new half's offense/defense (SIM-421 pre-filter refresh)
- [ ] `StateMachine.compute_leverage` _(method)_ - Static helper: compute Leverage Index from inning/score/base-out state
- [ ] `StateMachine._tendency` _(method)_ - Read single manager tendency by name from profile or direct mapping
- [ ] `StateMachine._manager_rng` _(method)_ - Draw single [0,1) from machine rng for deterministic manager decision
- [ ] `StateMachine._pre_pitch_hook` _(method)_ - Pre-pitch manager decisions (Â§3): IBB, pitch-out, steal green-light
- [ ] `StateMachine._should_issue_ibb` _(method)_ - Decide intentional walk in close-and-late with RISP and first base open
- [ ] `StateMachine._maybe_hit_and_run` _(method)_ - Signal hit-and-run for THIS pitch when runner-on-1B, <2-outs, favorable count
- [ ] `StateMachine.stage_steal` _(method)_ - Stage steal for NEXT pitch (SIM-319 decision); resolve safe/caught outcome
- [ ] `StateMachine._sample_steal_success` _(method)_ - Draw safe/caught outcome from stolen_base_pool (Â§3 item 4)
- [ ] `StateMachine._full_pool_steal_decision` _(method)_ - SIM-426: stage steal from runner embedding rates (no manager dependence)
- [ ] `StateMachine._end_of_pa_hook` _(method)_ - End-of-PA manager hook (Â§5.3): starter pull, pinch-hit, sac-bunt, sac-fly
- [ ] `StateMachine._maybe_pull_starter` _(method)_ - Pull pitcher for reliever when pitch count clears floor or leverage rises
- [ ] `StateMachine._pick_reliever` _(method)_ - Score and pop reliever from bullpen by leverage/platoon/effectiveness/rest
- [ ] `StateMachine._maybe_pinch_hit` _(method)_ - Swap batter for bench player in high-leverage spot; advance lineup
- [ ] `StateMachine._maybe_sac_bunt` _(method)_ - Signal sac bunt for next PA when runner on, <2-outs, manager tendency fires
- [ ] `StateMachine._maybe_sac_fly_intent` _(method)_ - Flag sac-fly intent for NEXT PA when runner-on-3rd, <2-outs, run-needed
- [ ] `PlayerStatLine` _(class)_ - SIM-328: one player's batting and pitching stats for a single simulated game
- [ ] `PlayerStatLine.ip_outs` _(method)_ - Innings pitched as raw count of outs (thirds of inning)
- [ ] `PlayerStatLine.ip` _(method)_ - Innings pitched in x.1/x.2 notation (float): full + (remainder/10)
- [ ] `PlayerStatLine.ip_thirds` _(method)_ - Innings pitched as true decimal (outs/3) for rate stats and ERA
- [ ] `BoxScore` _(class)_ - Per-game accumulator of PlayerStatLine keyed by player_id (SIM-328)
- [ ] `BoxScore.line` _(method)_ - Return stat line for player_id, creating empty one if absent
- [ ] `BoxScore.batters` _(method)_ - Property: filter lines with any batting activity (AB/H/RBI)
- [ ] `BoxScore.pitchers` _(method)_ - Property: filter lines with any pitching activity (IP/K/BB/ER)
- [ ] `GameSimResult` _(class)_ - Final per-game result: scores, innings, state, walk-off/extra-innings flags
- [ ] `GameSimResult.winner` _(method)_ - Property: return winning Team or None on tie
- [ ] `_place_ghost_runner` _(function)_ - Seed extra-innings automatic runner on second base (Â§6.2)
- [ ] `_game_over_after_half` _(function)_ - SIM-320 game-over predicate: check regulation finish and walk-off logic
- [ ] `_is_walkoff_live` _(function)_ - True when run just scored is walk-off (bottom 9th+ with home leading)
- [ ] `simulate_game` _(function)_ - SIM-320: drive StateMachine to completed game; handle regulation/walk-off/extra-innings

### `simulation/full_pool_sampler.py`  _(24)_
_SIM-423 full-pool similarity-weighted pitch sampler with per-PA/half-inning weights._

- [ ] `FullPoolSampler` _(class)_ - Full-pool sampler: scores entire bat_hand pool by similarity engines; draws from full weighted distribution.
- [ ] `FullPoolSampler.__init__` _(method)_ - Initialize with engine artifacts, rng, and RBF/platoon parameters.
- [ ] `FullPoolSampler._pool_meta` _(method)_ - Per-pool one-time precompute: dense pitcher-sim + batter-embedding indices + count buckets.
- [ ] `FullPoolSampler._f_pitcher` _(method)_ - Build pitcher-similarity factor for the pool (or all ones on miss).
- [ ] `FullPoolSampler._batter_vecs_z` _(method)_ - Return cached z-scored batter-embedding matrix (SIM-430 constant, computed once).
- [ ] `FullPoolSampler._batter_affinity` _(method)_ - Per-embedding-row RBF affinity to batter_key, memoized by key (SIM-430).
- [ ] `FullPoolSampler._f_batter` _(method)_ - Build batter RBF factor via batter_affinity and pool batter indices.
- [ ] `FullPoolSampler._f_situation` _(method)_ - Build situation RBF factor over the full situation vector.
- [ ] `FullPoolSampler._f_situation_baseout` _(method)_ - Build base-out RBF factor (count handled by bucket, not this factor).
- [ ] `FullPoolSampler.new_half_inning` _(method)_ - Cache the half-inning-constant base (f_pitcher Ã— recency).
- [ ] `FullPoolSampler.new_plate_appearance` _(method)_ - Assemble per-PA matchup weight and split into 12 count-bucket CDFs.
- [ ] `FullPoolSampler.draw` _(method)_ - Count-conditioned draw of one pitch outcome (SIM-429).
- [ ] `FullPoolSampler._batter_aff` _(method)_ - Per-batter-embedding RBF affinity (delegates to _batter_affinity for memoization).
- [ ] `FullPoolSampler._bb_pool_bat_idx` _(method)_ - Build dense batter-embedding index for batted-ball pool rows (SIM-430: cached).
- [ ] `FullPoolSampler._bb_same_hand_mask` _(method)_ - SIM-413: boolean mask of batted-ball rows pitched by same hand as pitcher_throws.
- [ ] `FullPoolSampler.battedball_new_pa` _(method)_ - Assemble batted-ball weight CDF with optional SIM-413 platoon reweight.
- [ ] `FullPoolSampler.battedball_draw` _(method)_ - Draw one batted ball -> (event, result_hits, result_outs, launch_angle).
- [ ] `FullPoolSampler.last_battedball_fielder` _(method)_ - SIM-425b: (fielded_by_position, fielder_player_id, season) of last battedball_draw row.
- [ ] `FullPoolSampler.fielder_quality` _(method)_ - SIM-425b: fielder's outs-above-average or None when absent/legacy bundle.
- [ ] `FullPoolSampler.has_battedball` _(method)_ - Test whether the sampler has batted-ball pools available.
- [ ] `FullPoolSampler.runner_rate` _(method)_ - Return baserunner's raw advancement rate or None when absent.
- [ ] `FullPoolSampler._br_feat_idx` _(method)_ - Cached index of baserunner feature names to vector positions.
- [ ] `FullPoolSampler.catcher_framing` _(method)_ - Return catcher's per-taken-pitch called-strike delta (0.0 when absent).
- [ ] `FullPoolSampler._cat_feat_idx` _(method)_ - Cached index of catcher feature names to vector positions.

### `simulation/play_pool_sampler.py`  _(36)_
_SIM-302 read-side play-pool k-NN sampler with LRU tile cache and DuckDB payload fetch._

- [ ] `TileHandle` _(class)_ - A loaded tile: FAISS index + rowids + meta + resolved cache key + label.
- [ ] `TileHandle.n_vectors` _(method)_ - Property: number of vectors in this tile.
- [ ] `_pitch_dir` _(function)_ - Build the on-disk pitch-tile directory path.
- [ ] `_battedball_dir` _(function)_ - Build the on-disk batted-ball-tile directory path.
- [ ] `_faiss_path` _(function)_ - Build the on-disk FAISS index file path.
- [ ] `_meta_path` _(function)_ - Build the on-disk FAISS tile metadata file path.
- [ ] `_rowids_path` _(function)_ - Build the on-disk FAISS rowids numpy file path.
- [ ] `_read_meta` _(function)_ - Read and parse the JSON metadata file or return None.
- [ ] `_normalize_bat_hand` _(function)_ - Coerce to single uppercase L/R tile encoding (no S tile).
- [ ] `PlayPoolSampler` _(class)_ - Read-side play-pool sampler: LRU tile cache + k-NN + DuckDB outcome/recency fetch.
- [ ] `PlayPoolSampler.__init__` _(method)_ - Initialize with pool/DB paths, tile LRU cap, rng, and injectable fetch callables.
- [ ] `PlayPoolSampler.resident_count` _(method)_ - Property: number of tiles currently held in the LRU.
- [ ] `PlayPoolSampler._cache_get` _(method)_ - Get tile from LRU and mark most-recently-used.
- [ ] `PlayPoolSampler._cache_put` _(method)_ - Put tile in LRU and evict least-recently-used if over cap.
- [ ] `PlayPoolSampler.load_tile` _(method)_ - Resolve and load a tile (with pitcher_id=0 fallback for small tiles).
- [ ] `PlayPoolSampler._should_fall_back` _(method)_ - Test if specific pitch tile is missing or too small; return true to serve fallback.
- [ ] `PlayPoolSampler._load_from_disk` _(method)_ - Load tile from disk: FAISS index + rowids + metadata.
- [ ] `PlayPoolSampler.attach_shared_tile` _(method)_ - Build + cache TileHandle over shared-memory buffers (zero-copy attach).
- [ ] `PlayPoolSampler._knn` _(method)_ - Run k-NN search; return (positions, weights, distances) with distance->weight conversion.
- [ ] `PlayPoolSampler._apply_recency` _(method)_ - Multiply distance weights by per-row recency_weight and renormalize.
- [ ] `PlayPoolSampler._draw_one` _(method)_ - Draw one neighbor index via rng.choice(p=weights).
- [ ] `PlayPoolSampler.sample_pitch` _(method)_ - k-NN sample one historical pitch outcome from the pitch tile.
- [ ] `PlayPoolSampler.sample_batted_ball` _(method)_ - k-NN sample one batted-ball event or return outcome distribution.
- [ ] `PlayPoolSampler._event_distribution` _(method)_ - Collapse k neighbors' weights onto event types, normalized to probability dict.
- [ ] `PlayPoolSampler.reload_recent` _(method)_ - Re-stat resident tiles; evict + reload if on-disk build is newer.
- [ ] `PlayPoolSampler._tile_dir_for_handle` _(method)_ - Resolve the on-disk directory path for a TileHandle.
- [ ] `PlayPoolSampler._bat_hand_for_key` _(method)_ - Extract bat_hand from a cache_key tuple.
- [ ] `PlayPoolSampler._fetch_outcome` _(method)_ - Single-row outcome lookup (delegates to batch fetch).
- [ ] `PlayPoolSampler._fetch_outcomes` _(method)_ - Resolve row ids to outcome strings (injected or DuckDB).
- [ ] `PlayPoolSampler._duckdb_fetch` _(method)_ - Fetch outcome strings from read-only DuckDB connection.
- [ ] `PlayPoolSampler._fetch_recencies` _(method)_ - Resolve row ids to recency_weight values (injected or DuckDB).
- [ ] `PlayPoolSampler._duckdb_fetch_recency` _(method)_ - Fetch recency_weight from read-only DuckDB connection.
- [ ] `PlayPoolSampler._table_has_recency` _(method)_ - Test if table has recency_weight column (cached per table).
- [ ] `PlayPoolSampler.close` _(method)_ - Release DuckDB connection and drop resident tiles.
- [ ] `PlayPoolSampler.__enter__` _(method)_ - Context manager entry: return self.
- [ ] `PlayPoolSampler.__exit__` _(method)_ - Context manager exit: call close.

### `simulation/run_resolution.py`  _(8)_
_Context-aware run resolution via RE24 matrix and sampled deltas_

- [ ] `RE24_MATRIX` _(constant)_ - 24-state base-out run-expectancy matrix for ~2024 MLB run environment
- [ ] `OUTS_PER_INNING` _(constant)_ - Constant 3 defining outs that end a half-inning
- [ ] `re24_value` _(function)_ - Returns run expectancy for a base-out state, 0.0 for >=3 outs
- [ ] `re24_from_rows` _(function)_ - Builds RE24 matrix dict from (outs, runners_state, expected_runs) rows with validation
- [ ] `advance_state` _(function)_ - Advances base-out state by sampled result_hits/outs/runs deltas, returns new state
- [ ] `_popcount` _(function)_ - Counts number of runners on base from 3-bit runners_state bitmask
- [ ] `RunResolution` _(class)_ - Result of run resolution with runs value, method, RE deltas, and canonical event
- [ ] `resolve_runs` _(function)_ - Resolves run value via RE24 deltas or falls back to linear weight lookup

### `simulation/pitcher_decisions.py`  _(5)_
_W/L/Save pitcher attribution from ordered play stream_

- [ ] `SAVE_LEAD_CEILING` _(constant)_ - Standard MLB Rule 9.19 save-situation lead ceiling of 3 runs
- [ ] `STARTER_WIN_MIN_OUTS` _(constant)_ - SIM-414: MLB Rule 9.17(b) minimum 15 outs (5 IP) for starting pitcher to earn win
- [ ] `PitcherDecisions` _(class)_ - Frozen dataclass holding winning/losing/save pitcher ids and final scores
- [ ] `_defending_team` _(function)_ - Returns fielding team from game state (HOME if TOP, AWAY if BOTTOM)
- [ ] `decisions_from_plays` _(function)_ - Derives W/L/Save decisions from ordered play stream using lead-taking, starter rule, save heuristics

### `simulation/play_recorder.py`  _(10)_
_Non-invasive recording of ordered PlayResult stream from simulate_game_

- [ ] `DEFAULT_FACTORY_REF` _(constant)_ - Default machine factory dotted-ref for picklable no-DB rng-driven factory
- [ ] `RecordingMachine` _(class)_ - Delegating wrapper intercepts step_pitch to record PlayResults while forwarding all attributes
- [ ] `RecordingMachine.__init__` _(method)_ - Initializes wrapper with inner machine and empty recorded_plays list
- [ ] `RecordingMachine.step_pitch` _(method)_ - Delegates to inner machine, appends result to recorded_plays, returns unchanged
- [ ] `RecordingMachine.__getattr__` _(method)_ - Forwards attribute reads to inner machine, bypassing wrapper-owned attributes
- [ ] `RecordingMachine.__setattr__` _(method)_ - Keeps wrapper attributes local, forwards other writes to inner machine
- [ ] `RecordingStateMachine` _(class)_ - StateMachine subclass that records every pitch via step_pitch override
- [ ] `RecordingStateMachine.__init__` _(method)_ - Calls super init then creates empty recorded_plays list
- [ ] `RecordingStateMachine.step_pitch` _(method)_ - Calls super step_pitch, appends result to recorded_plays, returns result
- [ ] `record_game_plays` _(function)_ - Simulates one game with recording via factory ref, returns (GameSimResult, play list)

---

## Phase 7 - Results, props & aggregation

### `simulation/results.py`  _(6)_
_Multi-iteration aggregation contract and per-game result re-export_

- [ ] `ConfidenceInterval` _(class)_ - Frozen dataclass for two-sided confidence interval with point, bounds, level, and method
- [ ] `ConfidenceInterval.half_width` _(method)_ - Returns half the interval width (margin around point estimate)
- [ ] `GameSimSummary` _(class)_ - Aggregate of N per-game results with win rates, central tendency, and confidence intervals
- [ ] `GameSimSummary.from_results` _(method)_ - Builds aggregate from per-game results list with optional confidence level and timestamp
- [ ] `_proportion_ci` _(function)_ - Wald normal-approximation CI on a proportion, clamped to [0, 1]
- [ ] `_mean_ci` _(function)_ - Wald CI on sample mean using std with ddof=1, handles n < 2

### `simulation/linescore.py`  _(10)_
_Per-inning linescore and team R/H/E derivation from play stream_

- [ ] `HIT_EVENTS` _(constant)_ - Frozenset of event names counting as base hits (single/double/triple/home_run)
- [ ] `_is_hit` _(function)_ - Returns True if play is a base hit, tolerant of canonical-vs-raw naming
- [ ] `InningLine` _(class)_ - Frozen dataclass for one inning's away/home run cells with None for unplayed halves
- [ ] `InningLine.away_played` _(method)_ - Property returning True if top (away) half of inning was played
- [ ] `InningLine.home_played` _(method)_ - Property returning True if bottom (home) half of inning was played
- [ ] `Linescore` _(class)_ - Frozen dataclass for full linescore with per-inning run grid and team R/H/E totals
- [ ] `Linescore.n_innings` _(method)_ - Property returning number of innings played (>= 9)
- [ ] `Linescore.away_by_inning` _(method)_ - Property returning away run cells per inning with None for unplayed
- [ ] `Linescore.home_by_inning` _(method)_ - Property returning home run cells per inning with None for unplayed
- [ ] `linescore_from_plays` _(function)_ - Derives linescore from play stream with errors charged to fielding side

### `simulation/snapshots.py`  _(27)_
_Field state, play-by-play, state-at-pitch, and override delta contracts_

- [ ] `DEFENSE_POSITIONS` _(constant)_ - Tuple of 9 defensive positions in scorebook order (P, C, 1B...RF)
- [ ] `POSITION_NUMBER` _(constant)_ - Dict mapping position names to scorebook 1-indexed numbers
- [ ] `BASE_LABELS` _(constant)_ - Tuple of base labels (1B, 2B, 3B)
- [ ] `OVERRIDE_METRIC_FIELDS` _(constant)_ - Tuple of metric attribute names compared by OverrideDelta in display order
- [ ] `_label_for` _(function)_ - Resolves display label for player id from optional map, fallback to #<id>
- [ ] `PlayerRef` _(class)_ - Frozen dataclass for player reference with id and optional label
- [ ] `PlayerRef.of` _(method)_ - Class method building PlayerRef from id and optional labels map
- [ ] `FieldSnapshot` _(class)_ - Frozen dataclass for BaseballFieldGraphic state with positions, batter, runners, count
- [ ] `FieldSnapshot.occupied_bases` _(method)_ - Property returning tuple of occupied base labels
- [ ] `FieldSnapshot.runners_on` _(method)_ - Property returning count of runners on base (0-3)
- [ ] `FieldSnapshot.from_game_state` _(method)_ - Class method building snapshot from GameState with optional labels and defense_positions
- [ ] `PlayByPlayEntry` _(class)_ - Frozen dataclass for one pitch with sequence, at-bat, pitch numbers, outcome, and runs
- [ ] `PlayByPlayEntry.from_play_result` _(method)_ - Class method building entry from PlayResult with sequence and position indices
- [ ] `PlayByPlay` _(class)_ - Frozen dataclass for collection of pitch-level play-by-play entries
- [ ] `PlayByPlay.n_pitches` _(method)_ - Property returning total pitch count
- [ ] `PlayByPlay.n_plate_appearances` _(method)_ - Property returning count of distinct plate appearances
- [ ] `PlayByPlay.pitches_for_at_bat` _(method)_ - Returns ordered pitch entries for a given at-bat index
- [ ] `PlayByPlay.plate_appearances` _(method)_ - Property returning entries grouped into PAs (list of lists)
- [ ] `PlayByPlay.from_play_results` _(method)_ - Class method building play-by-play from flat pitch sequence, inferring PAs from pa_terminal
- [ ] `StateAtPitch` _(class)_ - Frozen dataclass for point-in-time field snapshot tagged with at-bat/pitch indices
- [ ] `StateAtPitch.from_game_state` _(method)_ - Class method building snapshot from GameState with optional sequence index
- [ ] `MetricDelta` _(class)_ - Frozen dataclass for one baseline-vs-override metric with baseline, override, delta
- [ ] `MetricDelta.delta` _(method)_ - Property returning override minus baseline
- [ ] `OverrideDelta` _(class)_ - Frozen dataclass for baseline-vs-override comparison with metrics dict and description
- [ ] `OverrideDelta.delta` _(method)_ - Returns override minus baseline delta for named metric
- [ ] `OverrideDelta.home_win_pct_delta` _(method)_ - Property returning change in home win probability
- [ ] `OverrideDelta.from_summaries` _(method)_ - Class method building from two summaries and optional metrics list

### `simulation/score_fusion.py`  _(18)_
_Cross-engine score fusion for per-pitch shaping signal (currently unwired in production)_

- [ ] `PITCH_DRAW_WEIGHTS` _(constant)_ - Per-engine weight dict for default per-pitch draw (pitcher 0.5, batter 0.3, situation 0.2)
- [ ] `BATTED_BALL_WEIGHTS` _(constant)_ - Per-engine weight dict for batted-ball draw (batter 0.55, situation 0.25, pitcher 0.2)
- [ ] `FIELDING_WEIGHTS` _(constant)_ - Per-engine weight dict for fielding step (fielder 0.7, catcher 0.3)
- [ ] `PROFILES` _(constant)_ - Named profiles bundling {weights + rule} (pitch_draw, batted_ball, fielding)
- [ ] `_registry_score_type` _(function)_ - Lazy-guards registry lookup for engine's score_type, returns None if unavailable
- [ ] `_coerce_score` _(function)_ - Extracts numeric score from raw output (number or .score/.distance attribute)
- [ ] `distance_to_affinity` _(function)_ - Maps distance to bounded [0,1] affinity via exp(-distance/scale)
- [ ] `_to_affinity` _(function)_ - Reduces engine output to comparable affinity, honoring score_type
- [ ] `EngineSignal` _(class)_ - Frozen dataclass for one engine's contribution (name, raw, score_type, scale)
- [ ] `EngineSignal.resolved_score_type` _(method)_ - Returns score_type, resolving via registry if needed
- [ ] `EngineSignal.affinity` _(method)_ - Returns comparable [0,1] affinity (NaN if missing)
- [ ] `FusionResult` _(class)_ - Frozen dataclass for fusion output with fused scalar, affinities, weights, rule, profile
- [ ] `_blend` _(function)_ - Blends per-engine affinities with weights under rule, redistributes missing weight
- [ ] `ScoreFusion` _(class)_ - Reusable cross-engine fuser bound to a profile (weights + rule)
- [ ] `ScoreFusion.__init__` _(method)_ - Initializes fuser with profile name or explicit weights/rule
- [ ] `ScoreFusion._normalize_signals` _(method)_ - Coerces various signal shapes into EngineSignals
- [ ] `ScoreFusion.fuse` _(method)_ - Fuses injected signals into FusionResult
- [ ] `fuse_scores` _(function)_ - One-shot cross-engine fusion functional entry point

### `simulation/fingerprints.py`  _(19)_
_Real query-fingerprint derivation from game state via similarity engines_

- [ ] `PITCH_FEATURE_NAMES` _(constant)_ - 10-dim pitch feature names in PITCH_FEATURES order
- [ ] `BATTED_BALL_FEATURE_NAMES` _(constant)_ - 3-dim batted-ball feature names in BATTED_BALL_FEATURES order
- [ ] `PITCH_FINGERPRINT_DIM` _(constant)_ - Integer 10 (dimension of pitch query vector)
- [ ] `BATTED_BALL_FINGERPRINT_DIM` _(constant)_ - Integer 3 (dimension of batted-ball query vector)
- [ ] `MatchupProfile` _(class)_ - Frozen dataclass holding arsenal, intended_location, batted_ball centroids
- [ ] `MatchupProfile.__post_init__` _(method)_ - Validates and coerces vector shapes, raises on dimension mismatch
- [ ] `MatchupProfileProvider` _(constant)_ - Callable type signature for resolving matchup geometry
- [ ] `_PitchNorm` _(class)_ - Dataclass holding mean/std normalization stats (None = unfitted)
- [ ] `FingerprintDeriver` _(class)_ - Derives 10-dim pitch and 3-dim batted-ball query vectors from game state
- [ ] `FingerprintDeriver.__init__` _(method)_ - Initializes deriver with profile provider and optional fusion/norm overrides
- [ ] `FingerprintDeriver.new_plate_appearance` _(method)_ - Clears per-PA matchup cache at plate-appearance boundary
- [ ] `FingerprintDeriver.cache_hits` _(method)_ - Property returning count of cache hits
- [ ] `FingerprintDeriver.cache_misses` _(method)_ - Property returning count of cache misses
- [ ] `FingerprintDeriver._matchup` _(method)_ - Resolves matchup geometry with per-PA caching
- [ ] `FingerprintDeriver.pitch_fingerprint` _(method)_ - Builds 10-dim pitch vector from state, optionally tilted by signals
- [ ] `FingerprintDeriver.battedball_fingerprint` _(method)_ - Builds 3-dim batted-ball vector from state, optionally tilted by signals
- [ ] `FingerprintDeriver._tilt_location` _(method)_ - Tilts (plate_x, plate_z) by shaping scalar in [0,1]
- [ ] `FingerprintDeriver._tilt_exit_velo` _(method)_ - Tilts contact-centroid exit velocity by shaping scalar
- [ ] `FingerprintDeriver._normalize` _(method)_ - Normalizes raw vector into engine's indexed space via z-score + sqrt-weight scale

### `simulation/win_probability.py`  _(10)_
_Calibrated game win probability via Beta smoothing and Wald CI_

- [ ] `JEFFREYS_ALPHA` _(constant)_ - Jeffreys prior pseudo-count 0.5 for Beta smoothing (default)
- [ ] `LAPLACE_ALPHA` _(constant)_ - Laplace add-one pseudo-count 1.0 for Beta smoothing
- [ ] `TieHandling` _(class)_ - Enum for tie handling policy: SPLIT (half win each) or DROP (condition on decisive)
- [ ] `CalibrationMap` _(class)_ - Frozen dataclass for monotone p->p calibration map (identity by default)
- [ ] `CalibrationMap.apply` _(method)_ - Applies calibration map to probability and clamps result to [0, 1]
- [ ] `CalibrationMap.from_report` _(method)_ - Builds map from CalibrationReport's reliability curve with monotone enforcement
- [ ] `IDENTITY_CALIBRATION` _(constant)_ - Module-level singleton identity calibration map
- [ ] `WinProbability` _(class)_ - Frozen dataclass for calibrated home/away win probability with CI and metadata
- [ ] `win_probability` _(function)_ - Turns sim run into calibrated home/away win probability with Beta smoothing
- [ ] `_smoothed_proportion_ci` _(function)_ - Wald CI on smoothed home-win proportion never exactly 0 or 1

### `simulation/prop_distributions.py`  _(24)_
_Prop-distribution aggregator for per-player over/under PMFs over N iterations_

- [ ] `PITCHER_PROPS` _(constant)_ - Tuple of pitcher props charged on defense (K, BB, ER, OUTS)
- [ ] `BATTER_PROPS` _(constant)_ - Tuple of batter props credited on offense (H, HR, RBI, TB)
- [ ] `ALL_PROPS` _(constant)_ - Concatenation of pitcher and batter props
- [ ] `TB_IS_LOWER_BOUND` _(constant)_ - Boolean False indicating TB is now exact (SIM-365)
- [ ] `_total_bases` _(function)_ - Computes exact total bases from h, b2, b3, hr fields
- [ ] `_PROP_EXTRACTORS` _(constant)_ - Dict mapping prop names to lambda extractors from PlayerStatLine
- [ ] `PropDistribution` _(class)_ - Frozen dataclass for one prop's integer-support PMF with support and probabilities
- [ ] `PropDistribution.pmf` _(method)_ - Returns PMF as plain {value: probability} dict
- [ ] `PropDistribution.prob` _(method)_ - Returns P(X == value), 0.0 for values that never occurred
- [ ] `PropDistribution.p_at_least` _(method)_ - Returns P(X >= line), inclusive over
- [ ] `PropDistribution.p_at_most` _(method)_ - Returns P(X <= line), inclusive under
- [ ] `PropDistribution.p_greater` _(method)_ - Returns P(X > line), strict over
- [ ] `PropDistribution.p_less` _(method)_ - Returns P(X < line), strict under
- [ ] `PropDistribution.p_push` _(method)_ - Returns P(X == line), nonzero only for integer line
- [ ] `PropDistribution.p_over` _(method)_ - Returns betting OVER probability (sportsbook convention)
- [ ] `PropDistribution.p_under` _(method)_ - Returns betting UNDER probability (sportsbook convention)
- [ ] `PropDistribution.mean_ci` _(method)_ - Returns Wald CI on the prop's sample mean
- [ ] `PropDistribution.from_samples` _(method)_ - Class method building PMF from iterable of per-iteration integer samples
- [ ] `PropDistributionSet` _(class)_ - Collection of all prop PMFs for all players over one Monte-Carlo run
- [ ] `PropDistributionSet.get` _(method)_ - Looks up player's props or one prop, None if absent
- [ ] `PropDistributionSet.player_ids` _(method)_ - Returns all player ids with at least one prop PMF, ascending
- [ ] `PropDistributionSet.__contains__` _(method)_ - Returns True if player_id has at least one prop PMF
- [ ] `PropDistributionSet.from_boxscores` _(method)_ - Class method building prop PMF set from N per-game BoxScores
- [ ] `PropDistributionSet.from_results` _(method)_ - Class method building from N GameSimResults via from_boxscores

### `simulation/prop_validation.py`  _(24)_
_Binary calibration metrics and prop-PMF validation for win prob and over/under_

- [ ] `_as_prob_outcome` _(function)_ - Validates and coerces (predicted prob, 0/1 outcome) pairs with clipping
- [ ] `_bin_edges` _(function)_ - Returns n_bins+1 linspace edges for binning probabilities
- [ ] `binary_reliability_curve` _(function)_ - Returns bin points with mean_pred, observed rate, count, gap for binary event
- [ ] `binary_ece` _(function)_ - Returns binary Expected Calibration Error (population-weighted mean gap)
- [ ] `binary_brier` _(function)_ - Returns Brier score (mean squared error of forecast vs 0/1 outcome)
- [ ] `binary_log_loss` _(function)_ - Returns binary cross-entropy with eps clipping for confident-wrong penalties
- [ ] `fit_reliability_curve` _(function)_ - Fits [[predicted_p, observed_p], ...] curve for CalibrationMap consumption
- [ ] `PropCalibration` _(class)_ - Frozen dataclass for one prop's over/under calibration metrics at a line
- [ ] `validate_prop_over_under` _(function)_ - Scores prop PMF p_over against realized over/under with binary calibration metrics
- [ ] `pit_values` _(function)_ - Deterministic mid-P Probability Integral Transform for discrete PMFs
- [ ] `pmf_coverage` _(function)_ - Returns empirical coverage and PIT mean for PMF central interval
- [ ] `real_props_from_pa_events` _(function)_ - Aggregates per-PA event labels into per-player prop totals (derivable subset only)
- [ ] `pair_props_for_validation` _(function)_ - Pairs sim PMF set with realized actuals, appends to prop_pairs_by_line
- [ ] `DERIVABLE_BATTER_PROPS` _(constant)_ - Tuple of batter props exactly recoverable from event label (H, HR, TB)
- [ ] `DERIVABLE_PITCHER_PROPS` _(constant)_ - Tuple of pitcher props exactly recoverable from event label (K, BB)
- [ ] `DEFAULT_PROP_LINES` _(constant)_ - Dict of default over/under lines per prop for binary calibration
- [ ] `PropValidationReport` _(class)_ - Dataclass aggregating win-prob calibration, fitted curve, and per-prop calibrations
- [ ] `PropValidationReport.to_dict` _(method)_ - Converts report to dict via asdict
- [ ] `PropValidationReport.to_json` _(method)_ - Serializes report to JSON string
- [ ] `PropValidationReport.from_dict` _(method)_ - Class method deserializing from dict (field-safe)
- [ ] `PropValidationReport.from_json` _(method)_ - Class method deserializing from JSON string
- [ ] `reliability_curve_for_calibration_report` _(function)_ - Extracts win-prob reliability curve in CalibrationReport shape
- [ ] `build_validation_report` _(function)_ - Assembles PropValidationReport from collected (prediction, actual) data
- [ ] `write_reliability_curve_to_calibration_report` _(function)_ - Writes fitted win-prob curve into existing CalibrationReport JSON file

### `simulation/validation/replay_chi_squared.py`  _(20)_
_E2E historical-replay and chi-squared goodness-of-fit harness for run distributions_

- [ ] `LEAGUE_PITCH_MODEL` _(constant)_ - Calibrated per-pitch probability model for no-DB reference replay
- [ ] `LEAGUE_INPLAY_MODEL` _(constant)_ - Calibrated in-play outcome probabilities (SIM-421 tuned)
- [ ] `_normalize` _(function)_ - Extracts normalized probability vector from model dict
- [ ] `HistoricalGame` _(class)_ - Frozen dataclass for one team-game with known actual run total and matchup keys
- [ ] `ChiSquaredResult` _(class)_ - Frozen dataclass for chi-squared GOF result with statistic, dof, p-value, bins, counts
- [ ] `ChiSquaredResult.passed` _(method)_ - Property returning True if p_value > alpha
- [ ] `_LeagueOutcomeMachine` _(class)_ - StateMachine drawing pitch outcomes from calibrated league model (no sampler)
- [ ] `_LeagueOutcomeMachine.__init__` _(method)_ - Initializes machine with pitch model and normalized probabilities
- [ ] `_LeagueOutcomeMachine.step_pitch` _(method)_ - Draws pitch outcome from league model, calls super with it
- [ ] `_LeagueInPlayResolver` _(class)_ - PlayResolver drawing in-play outcomes from calibrated league model
- [ ] `_LeagueInPlayResolver.__init__` _(method)_ - Initializes resolver with seeded rng and in-play model
- [ ] `_LeagueInPlayResolver.resolve_fielding` _(method)_ - Draws in-play event from league model, applies context filter for GIDP
- [ ] `_default_state_machine` _(function)_ - Builds calibrated no-DB league machine for one seeded replay
- [ ] `simulate_run_distribution` _(function)_ - Replays n_games and returns simulated per-team-game run totals (flat list)
- [ ] `replay_historical_games` _(function)_ - Replays each HistoricalGame's matchup and returns simulated run totals
- [ ] `bin_run_totals` _(function)_ - Histograms run totals into 0, 1, ..., max_bin+ bins
- [ ] `pool_low_expected_bins` _(function)_ - Pools tail bins until all expected counts meet minimum, returns pooled counts and labels
- [ ] `run_total_distribution` _(function)_ - Thin alias for bin_run_totals with 'run distribution' vocabulary
- [ ] `chi_squared_gof` _(function)_ - Runs chi-squared GOF comparing simulated vs reference run distributions
- [ ] `replay_and_test` _(function)_ - Top-level convenience: replays games and chi-squared-tests in one call

---

## Phase 8 - Betting / CLV surface

### `betting/clv_engine.py`  _(26)_
_CLV/edge engine: odds conversions, de-vig, edge/EV, and CLV calculations._

- [ ] `american_to_decimal` _(function)_ - Convert American odds to decimal odds.
- [ ] `decimal_to_american` _(function)_ - Convert decimal odds to American odds.
- [ ] `implied_prob_from_american` _(function)_ - Calculate raw implied probability from American odds.
- [ ] `american_to_implied_prob` _(constant)_ - Alias for implied_prob_from_american.
- [ ] `prob_to_american` _(function)_ - Convert probability to fair American odds.
- [ ] `fair_american_from_prob` _(constant)_ - Alias for prob_to_american.
- [ ] `devig_multiway` _(function)_ - Remove vig from N-way market by normalization.
- [ ] `devig_two_way` _(function)_ - Remove vig from two-way market (proportional method).
- [ ] `edge` _(function)_ - Calculate edge as sim probability minus fair probability.
- [ ] `expected_value` _(function)_ - Calculate EV per unit stake at offered American odds.
- [ ] `CLV` _(class)_ - Closing Line Value dataclass capturing entry vs close CLV.
- [ ] `CLV.beat_close` _(method)_ - Property: true if this entry beat the close.
- [ ] `clv_from_prob` _(function)_ - Build CLV from two no-vig fair probabilities.
- [ ] `clv_from_odds` _(function)_ - Build CLV from raw two-way American odds at entry/close.
- [ ] `MarketSide` _(class)_ - Enum for market sides: HOME, AWAY, OVER, UNDER.
- [ ] `OddsQuote` _(class)_ - Dataclass: one snapshot of two-way American odds.
- [ ] `TwoWayMarket` _(class)_ - Dataclass: entry and optional closing quotes for a market.
- [ ] `EdgeReport` _(class)_ - Dataclass: edge/EV/CLV report for one market side.
- [ ] `EdgeReport.positive_edge` _(method)_ - Property: true if edge > 0.
- [ ] `_build_edge_report` _(function)_ - Shared assembly of edge reports: de-vig, compute edge/EV/CLV.
- [ ] `moneyline_edge_report` _(function)_ - Edge/EV/CLV for a moneyline side from WinProbability.
- [ ] `prop_edge_report` _(function)_ - Edge/EV/CLV for a player prop from PropDistribution.
- [ ] `total_over_under_edge_report` _(function)_ - Edge/EV/CLV for a game total from raw score arrays.
- [ ] `_as_margin_array` _(function)_ - Coerce input to a 1-D float64 score-margin array.
- [ ] `spread_cover_prob` _(function)_ - Calculate sim cover probability of a spread at line.
- [ ] `run_line_edge_report` _(function)_ - Edge/EV/CLV for a run line/spread side.

### `betting/bet_signal.py`  _(12)_
_Bet-signal recommendation module: gate, size, and rank +EV bets._

- [ ] `DEFAULT_MIN_EDGE` _(constant)_ - Default minimum edge threshold (2%).
- [ ] `DEFAULT_MIN_EV` _(constant)_ - Default minimum EV threshold (0.0).
- [ ] `DEFAULT_KELLY_FRACTION` _(constant)_ - Default Kelly fraction multiplier (0.25).
- [ ] `DEFAULT_MAX_STAKE_FRACTION` _(constant)_ - Default max stake cap (0.05).
- [ ] `kelly_fraction_full` _(function)_ - Calculate full-Kelly fraction for win probability and odds.
- [ ] `stake_fraction` _(function)_ - Compute recommended stake as fraction of bankroll (fractional Kelly).
- [ ] `BetSignalConfig` _(class)_ - Dataclass: thresholds and sizing knobs for bet signals.
- [ ] `BetSignalConfig.__post_init__` _(method)_ - Validate Kelly and max stake fractions.
- [ ] `DEFAULT_CONFIG` _(constant)_ - Default BetSignalConfig instance.
- [ ] `BetSignal` _(class)_ - Dataclass: fireable +EV bet recommendation from EdgeReport.
- [ ] `_passes_gate` _(function)_ - Check if EdgeReport clears action gates (positive edge and +EV).
- [ ] `bet_signals_from_edges` _(function)_ - Filter, size, and rank EdgeReports into fireable BetSignals.

### `betting/line_movement.py`  _(14)_
_CLV/line-movement time-series surface: opening-to-closing quote sequence._

- [ ] `_MARKET_COLUMNS` _(constant)_ - Mapping of (market_type, side) to game_odds column names.
- [ ] `_MARKET_SIDES` _(constant)_ - Valid (market_type, side) pairings for each market.
- [ ] `_side_columns` _(function)_ - Return (this-side, other-side, line) columns for market/side.
- [ ] `LineQuote` _(class)_ - Dataclass: one timestamped snapshot of a market side.
- [ ] `LineQuote.from_american` _(method)_ - Build LineQuote from American odds, computing implied_prob.
- [ ] `LineMovement` _(class)_ - Dataclass: line-movement time-series and opening->closing summary.
- [ ] `LineMovement.has_movement` _(method)_ - Property: true if line moved (2+ quotes and non-zero delta).
- [ ] `LineMovement.beat_close` _(method)_ - Property: true if entry beat the close.
- [ ] `_coerce_quote` _(function)_ - Coerce raw row mapping or LineQuote into LineQuote.
- [ ] `_sort_key` _(function)_ - Order quotes by fetched_at with None timestamps first.
- [ ] `line_movement_from_quotes` _(function)_ - Build LineMovement from unordered quote sequence (pure).
- [ ] `_row_to_mapping` _(function)_ - Normalize asyncpg Record or dict to plain dict.
- [ ] `fetch_line_movement` _(function)_ - Read raw.game_odds and build per-side line-movement series.
- [ ] `_net_direction` _(function)_ - Calculate net steam direction for a side across pooled rows.

---

## Phase 9 - Validation, calibration & ops scripts

### `scripts/fit_calibration.py`  _(5)_
_Fit calibration report: population-fit engine sigmas and reliability curves._

- [ ] `resolve_seasons` _(function)_ - Return seasons to calibrate over (explicit or from DuckDB).
- [ ] `sample_arsenal_distances` _(function)_ - Build pitcher engine and sample W2 distances for calibration.
- [ ] `validate_engine_medians` _(function)_ - Best-effort validation: build engine and log median similarity.
- [ ] `parse_args` _(function)_ - Parse command-line arguments.
- [ ] `main` _(function)_ - Entry point: resolve seasons, fit, validate, persist report.

### `scripts/validate_props.py`  _(7)_
_Prop validation: compare sim predictions to real outcomes and fit curves._

- [ ] `_fetch_final_games` _(function)_ - Fetch Final game rows with home/away scores from Postgres.
- [ ] `_fetch_pa_events` _(function)_ - Fetch (batter, pitcher, events) tuples for completed PAs.
- [ ] `_collect_game_results` _(function)_ - Replay one game N times; return per-iteration results.
- [ ] `run` _(function)_ - Main: fetch games, resolve, replay, validate, fit, report.
- [ ] `_summary_text` _(function)_ - Format validation report as human-readable text.
- [ ] `parse_args` _(function)_ - Parse command-line arguments.
- [ ] `main` _(function)_ - Entry point: setup logging, parse args, run.

### `scripts/sim_stats.py`  _(9)_
_Scaled Monte-Carlo harness: per-game box-score and channel breakdowns._

- [ ] `_dsn` _(function)_ - Return Postgres DSN from env or default.
- [ ] `_resolve` _(function)_ - Async resolve game state from Postgres.
- [ ] `_sim_kwargs` _(function)_ - Extract sim_kwargs dict from resolved game state.
- [ ] `_per_team_box` _(function)_ - Aggregate one team's box from boxscore by lineup ids.
- [ ] `_game_summary` _(function)_ - Generate per-game summary with both-team totals and splits.
- [ ] `_mean_sd` _(function)_ - Calculate mean and standard deviation from list.
- [ ] `_aggregate` _(function)_ - Roll up per-game summaries to per-channel means.
- [ ] `_print_report` _(function)_ - Print aggregated sim stats report to stdout.
- [ ] `main` _(function)_ - Entry point: parse args, run sims, aggregate, report.

### `scripts/clv_backtest.py`  _(30)_
_CLV backtest scoreboard: replay sims and measure beat-close rate._

- [ ] `_pool_mp_context` _(function)_ - Return multiprocessing context for forkserver pool.
- [ ] `trust_label` _(function)_ - Return trust tier for a market key.
- [ ] `BetRecord` _(class)_ - Dataclass: one scored row of the backtest.
- [ ] `BetRecord.to_jsonable` _(method)_ - Convert to JSON-safe dict.
- [ ] `BetRecord.from_jsonable` _(method)_ - Rebuild from JSON-safe dict.
- [ ] `TwoWayPrices` _(class)_ - Dataclass: opening + closing two-way American odds.
- [ ] `_pick_side` _(function)_ - Pick the side with larger positive edge >= floor.
- [ ] `evaluate_two_way_market` _(function)_ - Pure: pick +EV side on opening line and score CLV (pure).
- [ ] `ScoreboardRow` _(class)_ - Dataclass: aggregated CLV stats for one group.
- [ ] `ScoreboardRow.to_jsonable` _(method)_ - Convert to JSON-safe dict.
- [ ] `_row_for` _(function)_ - Aggregate BetRecords into one ScoreboardRow.
- [ ] `aggregate_scoreboard` _(function)_ - Roll BetRecords into full scoreboard dict (pure).
- [ ] `format_scoreboard` _(function)_ - Render scoreboard as readable text table.
- [ ] `_fetch_final_games` _(function)_ - Fetch Final game pks for seasons from Postgres.
- [ ] `_fetch_game_odds` _(function)_ - Fetch opening/closing game odds for one game.
- [ ] `_fetch_prop_odds` _(function)_ - Fetch opening/closing prop odds for one game.
- [ ] `_game_prices` _(function)_ - Build TwoWayPrices for one game market from odds.
- [ ] `score_game_markets` _(function)_ - Score three game markets (ML/total/runline) for one game.
- [ ] `score_prop_markets` _(function)_ - Score player-prop markets for one game.
- [ ] `_collect_game_results` _(function)_ - Replay one game N times; return per-iteration results.
- [ ] `_score_one_game` _(function)_ - Resolve, replay, and score ONE game end-to-end.
- [ ] `_worker_lazy_init` _(function)_ - One-time per-worker setup: warm cache and create pool.
- [ ] `_process_one_game` _(function)_ - Module-level worker: score ONE game for ProcessPoolExecutor.
- [ ] `_Counters` _(class)_ - Dataclass: running tallies for the run summary.
- [ ] `_tally` _(function)_ - Fold one game's status into run counters.
- [ ] `_run_serial` _(function)_ - Serial fallback: score games in-process on single pool.
- [ ] `_run_parallel` _(function)_ - Across-games parallel: map games to ProcessPoolExecutor workers.
- [ ] `run` _(function)_ - Main orchestration: fetch games, score, aggregate, emit report.
- [ ] `parse_args` _(function)_ - Parse command-line arguments.
- [ ] `main` _(function)_ - Entry point: setup logging, parse args, run.

### `scripts/load_historical_odds.py`  _(9)_
_Historical odds backfiller: ingest opening+closing lines into raw tables._

- [ ] `_fetch_final_games` _(function)_ - Fetch Final game pks for seasons from Postgres.
- [ ] `_fetch_lineup_players` _(function)_ - Fetch (player_id, is_pitcher) for every player in game.
- [ ] `_has_line` _(function)_ - Check if game-odds dict has at least one resolved price.
- [ ] `_load_game_odds` _(function)_ - Fetch and persist opening/closing game lines for one game.
- [ ] `_load_prop_odds` _(function)_ - Fetch and persist opening/closing prop lines for one game.
- [ ] `_build_persisters` _(function)_ - Bind persist_game and persist_prop to live pipeline write path.
- [ ] `run` _(function)_ - Main: fetch games, iterate, load odds, report.
- [ ] `parse_args` _(function)_ - Parse command-line arguments.
- [ ] `main` _(function)_ - Entry point: setup logging, parse args, run.

### `scripts/rebuild_pools.py`  _(1)_
_Rebuild pitch and outcome pools for given seasons only._

- [ ] `main` _(function)_ - Connect, rebuild pitch/outcome pools for seasons, report.

### `scripts/perf_fullpool.py`  _(7)_
_SIM-423 perf gate: benchmark full-pool factorized weighted draw._

- [ ] `timeit` _(function)_ - Benchmark function, return best time in milliseconds.
- [ ] `f_geom` _(function)_ - Compute geometry similarity scores.
- [ ] `f_situation` _(function)_ - Compute situation similarity scores.
- [ ] `f_pitcher` _(function)_ - Gather pitcher similarity scores by index.
- [ ] `half_inning_base` _(function)_ - Compute base weights (geom * pitcher * recency).
- [ ] `pa_setup` _(function)_ - Compute PA weights with situation and batter factors.
- [ ] `per_pitch_draw` _(function)_ - Draw one pitch from CDF.

### `scripts/measure_knn.py`  _(4)_
_Measure sampler weight concentration and ESS over KNN tiles._

- [ ] `ess` _(function)_ - Calculate effective sample size from weights.
- [ ] `_weights` _(function)_ - Get recency-adjusted weights for a query.
- [ ] `_report` _(function)_ - Report ESS and weight concentration for a tile/query.
- [ ] `main` _(function)_ - Iterate pitchers and tiles, report ESS/concentration.

### `scripts/run_index_acceptance.py`  _(9)_
_SIM-158 index acceptance gates: verify query plans and latencies._

- [ ] `_build_situation_query` _(function)_ - Build EXPLAIN ANALYZE situation lookup with index-friendly predicates.
- [ ] `_mask_dsn_password` _(function)_ - Mask password in DSN for safe logging.
- [ ] `_extract_total_ms` _(function)_ - Extract Execution Time from EXPLAIN ANALYZE plan text.
- [ ] `_plan_uses_index` _(function)_ - Check if plan text uses expected index.
- [ ] `_plan_is_seq_scan` _(function)_ - Check if plan text has Seq Scan.
- [ ] `_run_gate` _(function)_ - Run EXPLAIN ANALYZE and return (passed, plan, ms).
- [ ] `_make_markdown` _(function)_ - Generate Markdown report of index acceptance results.
- [ ] `main_async` _(function)_ - Run gates and emit acceptance report.
- [ ] `main` _(function)_ - Entry point: parse args, run async main.

### `scripts/export_openapi.py`  _(1)_
_Export FastAPI OpenAPI schema to frontend/openapi.json for codegen._

- [ ] `main` _(function)_ - Export app OpenAPI schema to JSON with stable key ordering.

### `scripts/check_file_integrity.py`  _(5)_
_SIM-315 file-integrity guard: detect NUL bytes and truncated source._

- [ ] `Offender` _(class)_ - Dataclass: a single integrity violation for one file.
- [ ] `_is_excluded_dir` _(function)_ - Check if directory is in EXCLUDED_DIRS.
- [ ] `iter_python_files` _(function)_ - Yield .py files for given paths, walking directories.
- [ ] `check_file` _(function)_ - Check file for NUL bytes and parse errors.
- [ ] `main` _(function)_ - Entry point: scan files, report integrity issues.

### `scripts/check_bat_side_coverage.py`  _(9)_
_SIM-160 bat_hand coverage audit: check NULL rates per season._

- [ ] `_mask_dsn_password` _(function)_ - Mask password in DSN for safe logging.
- [ ] `SeasonCoverage` _(class)_ - Dataclass: bat_hand coverage stats for one season.
- [ ] `SeasonCoverage.null_pct` _(method)_ - Property: NULL percentage.
- [ ] `SeasonCoverage.switch_pct` _(method)_ - Property: switch hitter percentage.
- [ ] `SeasonCoverage.gate_passes` _(method)_ - Property: true if NULL rate <= budget.
- [ ] `_scan_coverage` _(function)_ - Query Postgres for per-season bat_hand coverage stats.
- [ ] `_make_markdown` _(function)_ - Generate Markdown coverage report.
- [ ] `main_async` _(function)_ - Scan coverage and emit report.
- [ ] `main` _(function)_ - Entry point: parse args, run async main.

### `scripts/backfill_odds_hash.py`  _(7)_
_SIM-157 odds_hash backfiller: populate hashes and collapse duplicates._

- [ ] `_connect` _(function)_ - Connect to Postgres.
- [ ] `_row_counts` _(function)_ - Return (total_rows, null_hash_rows).
- [ ] `_backfill_hashes` _(function)_ - Pass 1: populate odds_hash on legacy NULL rows.
- [ ] `_dedup_rows` _(function)_ - Pass 2: keep earliest row per (game_pk, source, odds_hash).
- [ ] `_validate` _(function)_ - SIM-157 acceptance gates: zero NULLs and duplicates.
- [ ] `main_async` _(function)_ - Connect, backfill, dedup, validate.
- [ ] `main` _(function)_ - Entry point: parse args, setup logging, run.

### `scripts/trace_game.py`  _(5)_
_Per-pitch sim trace: emit CSV of game state and outcome per pitch._

- [ ] `_dsn` _(function)_ - Return Postgres DSN from env or default.
- [ ] `_resolve_state` _(function)_ - Async resolve game state from Postgres.
- [ ] `_box_totals` _(function)_ - Aggregate (AB, H, HR, RBI) from boxscore.
- [ ] `_state_snap` _(function)_ - Capture game state snapshot.
- [ ] `main` _(function)_ - Resolve game, run sim with tracing, emit CSV.


