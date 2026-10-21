# Data In: the Ingest Pipeline

This page covers the code that brings outside data into the Postgres `raw` schema. The
sources are the MLB Stats API (the league's public game, schedule and box-score service),
Baseball Savant (the league's Statcast leaderboards) and the BettingPros odds API. The
nightly builds ([pipeline-batch.md](pipeline-batch.md)) read these tables; the live service
also feeds the browsers through Redis. The table layouts are on [database.md](database.md).

## How it fits together

Four paths bring data in.

**1. The nightly finals load (the historical loader).**

1. The `nightly-finals` scheduler job runs [`scripts/nightly_finals.sh`](../../scripts/nightly_finals.sh)
   at 07:00 UTC. It calls `HistoricalDataLoader.load_date_range` for the last few days in
   Eastern time. The Sunday `weekly-refresh` job calls `refresh_seasons` for the current
   season.
2. `load_date_range` reads the MLB schedule in windows of at most 364 days and keeps only
   Final games (`historical/schedule.py`).
3. `_dispatch_game` skips a game that is already loaded (`_game_already_loaded`) and loads
   the rest. Five failures in a row stop the run.
4. `_fetch_game_pitches` reads the game's live feed and both teams' coaches. It returns one
   row dict per pitch and collects the non-pitch events (`play_events.extract_play_events`).
5. `_ensure_prerequisites` writes every parent row the pitches need, in foreign-key order:
   `raw.venues`, `raw.teams`, `raw.players`, `raw.managers`, `raw.games`, then
   `raw.game_lineups`. It also writes the official box score (`raw.game_player_stats`) and
   the box's bullpen listing (`raw.game_bullpen`) from the same payload.
6. `_process_and_insert` builds and validates each pitch row (`historical/rows.py`). It
   writes `raw.pitches`, `raw.play_events`, `raw.etl_errors`, `raw.etl_game_ingest` and
   `raw.etl_data_freshness` (`historical/writes.py`).

**2. The live service (game day).**

1. The compose `live` service runs `python -m pipeline.live.run_live`. It waits for Postgres
   and serves `LiveIngestionPipeline` on port 8001.
2. Every 30 seconds `_sync_live_games` reads the schedule from yesterday to tomorrow in
   Eastern time. It upserts each game into `raw.games`.
3. A Preview game gets its posted lineup (`lineup_writer.PublishedLineupWriter.on_preview`)
   and its pre-game odds (`_persist_pregame_odds`).
4. A game that turns Live gets its closing rows promoted (`_mark_closing_rows`), its first
   pitch time stamped (`on_game_live`) and a socket watcher (`MLBGameWebSocket`).
5. Each socket message is only a change signal. `_refresh_game_state` re-reads the full
   feed over REST, builds the game state (`GameStateBuilder.build`), writes
   `sim.lineup_state`, caches the feed in Redis and stores the odds on a one-minute cadence.
6. The service publishes each browser-bound message on the Redis channel
   `live:game:{game_pk}` (`broadcast.RedisBroadcaster`). The app's `RedisBridge` forwards
   it to the browsers that watch the game.

**3. The odds.** Every writer reads odds through one seam, `odds_provider.get_odds_provider`.
The provider returns one row per book. The load guard (`odds_row_guard.check_row`) refuses
a row that cannot be one bet. The kept rows go to `raw.game_odds` and `raw.prop_odds`. Four
writers use the seam: the live cycles, the nightly closing pass
([`scripts/load_historical_odds.py`](../../scripts/load_historical_odds.py), run by
`nightly_closing_lines.sh`), the opening-line job and the historical odds backfill.

**4. The reference loaders.** The Savant leaderboards, the per-pitch tracking export, the
sprint speeds, the venue backfill and the bullpen availability each have a command-line
entry point. No scheduler job runs them; an operator runs them by hand.

### Who runs each loader, and what it writes

| Loader | Runs by | Writes |
|---|---|---|
| `HistoricalDataLoader` | the `nightly-finals` and `weekly-refresh` jobs; `scripts/reload_games.py`; `scripts/resumable_sweep.py` | `raw.venues`, `raw.teams`, `raw.players`, `raw.managers`, `raw.games`, `raw.game_lineups`, `raw.game_player_stats`, `raw.game_bullpen`, `raw.pitches`, `raw.play_events`, `raw.etl_errors`, `raw.etl_game_ingest`, `raw.etl_data_freshness` |
| `BoxscoreIngest` | `scripts/load_official_boxscores.py` (backfill) | `raw.game_player_stats`, `raw.game_bullpen` |
| `savant_loader` | by hand | the twelve `raw.savant_*` board tables |
| `savant_pitch_tracking_loader` | by hand | `raw.savant_pitch_tracking` |
| `SprintSpeedLoader` | by hand | `raw.sprint_speed` |
| `VenueBackfillJob` | by hand | `raw.games.venue_id`, `raw.pipeline_run_log` |
| `OpeningLineJob` | by hand | `raw.game_odds`, `raw.prop_odds` (`line_type='opening'`), `raw.pipeline_run_log` |
| `BullpenAvailabilityIngest` | by hand | `raw.game_bullpen_availability` |
| `LiveIngestionPipeline` | the compose `live` service | `raw.games`, `raw.game_lineups` (published rows), `raw.game_odds`, `raw.prop_odds`, `sim.lineup_state`, Redis keys |

## The historical loader

### `pipeline/etl/etl_historical_loader.py`

[Source](../../pipeline/etl/etl_historical_loader.py) · 1825 lines · Tests: [test_etl_historical_loader.py](../../tests/unit/test_etl_historical_loader.py), [test_etl_reload_game.py](../../tests/unit/test_etl_reload_game.py), [test_etl_ensure_rows.py](../../tests/unit/test_etl_ensure_rows.py), [test_etl_hardening.py](../../tests/unit/test_etl_hardening.py), [test_etl_connection_pool.py](../../tests/unit/test_etl_connection_pool.py)

The loader of finished games. The file holds the HTTP plumbing, the feed parser and the
`HistoricalDataLoader` class. The class's other parts live in `pipeline/etl/historical/`.
The tests patch `_connect`, `_http_get`, `_fetch_once` and `_fetch_game_pitches` here, so
those names stay in this file.

| Function / class | What it does | Called from |
|---|---|---|
| `HistoricalDataLoader.load_date_range` | Loads every Final game in a date range. It cuts the range into schedule windows and warns when a window comes back short. `reload=True` sends every game through `reload_game`. | `scripts/nightly_finals.sh`, `scripts/nightly_ingest.sh` |
| `HistoricalDataLoader.refresh_seasons` | Loads every Final game of a range of seasons. The default end is last season. `reload=True` re-ingests. | `scripts/weekly_refresh.sh`, `scripts/resumable_sweep.py` |
| `HistoricalDataLoader.load_game` | Loads one game. The pitch insert skips a row that exists (`ON CONFLICT ... DO NOTHING`). | `_dispatch_game` |
| `HistoricalDataLoader.reload_game` | Deletes and re-inserts one game's pitch rows in one transaction. It refuses to store fewer rows than before (`ReloadShrinkError`) unless `allow_shrink=True`. | `_dispatch_game`, `scripts/reload_games.py` |
| `HistoricalDataLoader._dispatch_game` | Routes one game to a load or a reload and counts the outcome. It contains a single game's failure; `ETL_CONSECUTIVE_FAILURE_LIMIT` (default 5) failures in a row raise. | `load_date_range`, `refresh_seasons` |
| `_fetch_game_pitches` | Reads `feed/live?hydrate=alignment` and both teams' coaches. It tracks the pre-play outs and base state per play (the extra-innings runner included) and builds one dict per pitch. It caches each batter's hand across games. | `_load_game` |
| `HistoricalDataLoader._ensure_prerequisites` | Writes every parent row of `raw.pitches` in foreign-key order. Each step checks which ids are missing and fetches only those. | `_load_game` |
| `HistoricalDataLoader._ensure_venue` | Upserts a missing venue: its fence dimensions from a Savant page (the park-factors board, else the venue page), its location, surface and roof from the MLB venues endpoint. `force=True` corrects a stored row (`scripts/reload_games.py --refresh-venues`). | `_ensure_prerequisites` |
| `HistoricalDataLoader._ensure_players` | Upserts every player of the game's box, from the people endpoint for any missing one. | `_ensure_prerequisites` |
| `HistoricalDataLoader._ensure_game_player_stats` | Writes the official box score and the bullpen listing from the feed in hand. A failure logs a warning and never fails the game load. | `_ensure_prerequisites` |
| `_http_get` / `_fetch_once` | One GET with a bounded retry: 408, 425, 429, 500, 502, 503 and 504 retry (429 honours `Retry-After`); any other status raises at once. | every fetch |

**Flags:** `ETL_HTTP_TRANSPORT` (default `urllib`; `requests` turns on the pooled session for
a comparison). `ETL_CONSECUTIVE_FAILURE_LIMIT` (default 5). `BASEBALL_DB_DSN` (required).

**Watch out:** `load_game` cannot repair loaded data: its insert skips every existing row.
After a parser change, use `reload_game`, `refresh_seasons(reload=True)` or
`load_date_range(reload=True)`. Keep the default `urllib` transport: the `requests`
transport crashed the long sweeps with a memory fault, so the loader sends no
`Accept-Encoding` and has no gunzip branch (a test holds the two together). The loader
skips a game with a pitch row or an `'empty'` outcome in `raw.etl_game_ingest`.

### `pipeline/etl/historical/`

[Source](../../pipeline/etl/historical/) · 8 modules, 1852 lines · Tests: [test_etl_hardening.py](../../tests/unit/test_etl_hardening.py), [test_etl_starting_lineups.py](../../tests/unit/test_etl_starting_lineups.py), [test_starting_position.py](../../tests/unit/test_starting_position.py), [test_venue_resolution.py](../../tests/unit/test_venue_resolution.py), [test_etl_schedule_final_filter.py](../../tests/unit/test_etl_schedule_final_filter.py)

The parts of `HistoricalDataLoader`, one job per module. The class mixes in
`ReferenceRowsMixin`, `WritesMixin` and `LoaderBase`.

| Module | Lines | What it holds |
|---|---|---|
| [`base.py`](../../pipeline/etl/historical/base.py) | 84 | `LoaderBase`: the DSN, the lazy psycopg2 connection pool (`_ensure_pool`, `_get_conn`) and `close`. |
| [`game_values.py`](../../pipeline/etl/historical/game_values.py) | 168 | The feed values: the manager among the coaches (acting manager, then manager, then bench coach), the height, the wind, the game status, and `_build_starting_lineup_rows`, which puts each starter at the position he STARTED at (`allPositions[0]`; the starting-position repair, SIM-559). The live lineup writer reuses this builder. |
| [`reference_rows.py`](../../pipeline/etl/historical/reference_rows.py) | 420 | `ReferenceRowsMixin`: `_ensure_teams`, `_ensure_managers`, `_ensure_game` (the game row with its linescore and decisions) and `_ensure_game_lineups`. The lineup step first deletes the game's non-box rows (`source <> 'box'`), so the final box replaces a published or projected lineup. |
| [`rows.py`](../../pipeline/etl/historical/rows.py) | 429 | The pitch row: `_build_row_dict` maps the feed fields to `raw.pitches` columns; `_validate_row` sorts problems into hard errors (the row is skipped and logged to `raw.etl_errors`) and warnings (the row is kept with `data_quality_flag = TRUE`). |
| [`savant.py`](../../pipeline/etl/historical/savant.py) | 50 | `_parse_savant_embedded_json`: reads the JSON a Savant page embeds in its HTML; raises `SavantScrapeError` when the page has none. |
| [`schedule.py`](../../pipeline/etl/historical/schedule.py) | 51 | `_date_chunks` (windows of at most 364 days), `_schedule_game_is_final` and the failure limit. |
| [`venues.py`](../../pipeline/etl/historical/venues.py) | 95 | `_resolve_venue`: the venue a game was PLAYED at, with overrides for neutral-site games whose feed has none; `MissingVenueError` otherwise. |
| [`writes.py`](../../pipeline/etl/historical/writes.py) | 555 | `WritesMixin`: `_game_already_loaded`, `_process_and_insert` (500-row batches), the error ledger, the per-game outcome (`raw.etl_game_ingest`: `loaded` or `empty`), `_write_play_events`, `_log_freshness`, `reprocess_errored_games` and `quality_report`. |

**Watch out:** `_validate_row` mirrors the database's CHECK ranges. A value outside a CHECK
is a hard error, never a warning, because the insert would fail.

### `pipeline/etl/play_events.py`

[Source](../../pipeline/etl/play_events.py) · 403 lines · Tests: [test_play_events.py](../../tests/unit/test_play_events.py)

The non-pitch events a pitch row cannot hold: pickoff throws and their outcomes, step-offs,
balks and intentional walks. An intentional walk has no pitch at all, so without this module
the database would hold none. The module is pure: no network, no database.

| Function / class | What it does | Called from |
|---|---|---|
| `extract_play_events` | Returns one `raw.play_events` row per non-pitch event of one play. It joins a pickoff throw to its outcome by the play index, names the pitcher who threw (a mid-plate-appearance change included) and builds the intentional walk's row from the play's result. | `etl_historical_loader._fetch_game_pitches` |

**Watch out:** each signal lives in an unexpected place in the feed. A pickoff throw is
`type == "pickoff"` with an empty event type; a pickoff OUT lives on the runner entry. The
caller must pass the pre-play base state and score: the module cannot derive them.

### `pipeline/etl/boxscore_ingest.py`

[Source](../../pipeline/etl/boxscore_ingest.py) · 710 lines · Tests: [test_boxscore_ingest.py](../../tests/unit/test_boxscore_ingest.py), [test_game_bullpen_listing.py](../../tests/unit/test_game_bullpen_listing.py), [test_starting_position.py](../../tests/unit/test_starting_position.py), [test_boxscore_reader.py](../../tests/unit/test_boxscore_reader.py)

The official box score, the ground truth a sportsbook grades a player prop on (the official
box-score ground truth, SIM-545). One row per player who appeared; a player who did not play
has no row, so the book voids his bet.

| Function / class | What it does | Called from |
|---|---|---|
| `parse_boxscore` | PURE: turns a box score's `teams` block into one `PlayerGameStats` per player whose batting or pitching block is not empty. The box endpoint and the live feed share this shape. | `etl_historical_loader._ensure_game_player_stats`, `BoxscoreIngest.fetch_rows` |
| `persist_sync` / `persist` | Upsert the rows on a psycopg2 cursor / an asyncpg connection (`ON CONFLICT (game_pk, player_id) DO UPDATE`). | the loader; `scripts/load_official_boxscores.py` |
| `parse_bullpen_listing` / `persist_bullpen_sync` | The arms the box lists per side for `raw.game_bullpen`: `started` (the first pitcher), `pitched` and `bullpen`. This listing is the simulator's pen for the game (the real managers and pens, SIM-427). | the loader |
| `starting_position` / `parse_starting_positions` / `persist_starting_positions` | Each starter's position at the start of the game, and the repair of stored lineup rows. | `game_values._build_starting_lineup_rows`; `scripts/probes/backfill_start_positions.py` |
| `BoxscoreIngest` | Fetches `/game/{game_pk}/boxscore` (`_mlb_get`, the one network seam, with a short retry) and persists the rows. | `scripts/load_official_boxscores.py` |

### `pipeline/etl/coercion.py`

[Source](../../pipeline/etl/coercion.py) · 62 lines · Tests: [test_etl_historical_loader.py](../../tests/unit/test_etl_historical_loader.py)

`to_float`, `to_int`, `to_bool` and `to_str`: the shared "empty or missing becomes `None`"
rules. An empty string, `None` and a NaN float all read as `None`. The historical row
builder, the venue resolver and the Savant and sprint-speed loaders use them.

**Watch out:** the `_opt_int` helper in `bullpen_availability_ingest.py` and `_opt_float` in
`bettingpros/parsing.py` do not treat `""` as missing. Folding them in here changes behaviour.

## Baseball Savant and the other reference loaders

### `pipeline/etl/savant_boards.py`

[Source](../../pipeline/etl/savant_boards.py) · 555 lines · Tests: [test_savant_loader.py](../../tests/unit/test_savant_loader.py), [test_savant_oaa_jump_boards.py](../../tests/unit/test_savant_oaa_jump_boards.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py)

The registry of the twelve Savant leaderboards. Each `SavantBoard` names its address, how
the board spells the season, its target table and its columns. The boards and their tables:
`bat_tracking`, `swing_path`, `batting_stance` (the batter swing features), `arm_strength`,
`baserunning`, `poptime`, `catcher_throwing`, `first_base_receiving`, `outs_above_average`,
`outfield_jump` (the fielding boards), and `basestealing`, `pitcher_running_game` (the
running game). Each writes `raw.savant_<name>`. `BATTER_BOARDS`, `FIELDING_BOARDS` and
`RUNNING_BOARDS` are the groups the loader's `--boards` accepts.

| Function / class | What it does | Called from |
|---|---|---|
| `SavantBoard.season_params` | The season part of the query, in the board's style: `year`, `camel`, `snake`, `bracket` or `startyear`. | `savant_loader.build_url` |
| `BOARDS` | The registry, keyed by board name. | `savant_loader`, the profile recompute probes |

**Watch out:** Savant answers a season parameter it does not know with HTTP 200 and the
CURRENT season's rows. Each board pulls at the smallest minimum its endpoint honours, never
the qualified default (owner ruling 2026-09-16); the run-value boards ignore `n=0`, so they
use `n=1`. `outs_above_average` is pulled once per position and takes the position from
the query, not from the board's column.

### `pipeline/etl/savant_loader.py`

[Source](../../pipeline/etl/savant_loader.py) · 448 lines · Tests: [test_savant_loader.py](../../tests/unit/test_savant_loader.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py)

One loader for every board: fetch the CSV, coerce it, drop players the database does not
know, upsert. Run it as `python -m pipeline.etl.savant_loader --boards all --seasons ...`.

| Function / class | What it does | Called from |
|---|---|---|
| `probe_season_param` | Asks each board for 1990 (`PROBE_SEASON`). A board that returns rows ignored the season, and the load raises `SeasonParamError`. | `main` |
| `check_row_seasons` | Where a board returns a season column, every row must carry the season asked for. | `load_board_season` |
| `load_board_season` | Fetches, parses, coerces and upserts one board for one season. `--asof` keeps games up to a date on the boards that support it. | `main`; the profile recompute probes |
| `coerce_row` / `filter_to_known_players` | One CSV row to one upsert payload (a blank is `None`, never 0); drop rows whose player is not in `raw.players`. | `load_board_season` |

**Flags:** `BASEBALL_DB_DSN` (required unless `--dry-run`).

### `pipeline/etl/savant_pitch_tracking_loader.py`

[Source](../../pipeline/etl/savant_pitch_tracking_loader.py) · 283 lines · Tests: none

The per-pitch tracking measurements (bat speed, swing length, arm angle and others) from
Savant's search export, one day per request, into `raw.savant_pitch_tracking`. Per-pitch
rows let the batter profile aggregate up to a cutoff date, so a backtest never reads a
season average that contains the future.

| Function / class | What it does | Called from |
|---|---|---|
| `load_day` / `check_day` | Loads one day. The export truncates silently at 25,000 rows, so a day at the cap raises `TruncatedDayError`; a row from another date also raises. | `main` |
| `season_days` / `loaded_days` | The days of a season, and the days already loaded (for `--resume`). | `main` |

### `pipeline/etl/etl_sprint_speed_loader.py`

[Source](../../pipeline/etl/etl_sprint_speed_loader.py) · 297 lines · Tests: none

`SprintSpeedLoader` fetches Savant's sprint-speed leaderboard per season and upserts
`raw.sprint_speed`, one row per player and season. `refresh_seasons` is the entry point;
`_filter_to_known_players` drops a player missing from `raw.players`. Run it as
`python -m pipeline.etl.etl_sprint_speed_loader --seasons ...`.

### `pipeline/etl/mlb_venue_dimensions.py`

[Source](../../pipeline/etl/mlb_venue_dimensions.py) · 156 lines · Tests: [test_park_geometry_builder.py](../../tests/unit/test_park_geometry_builder.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py)

The MLB API's published fence distances, the prior of a park's fence line where the pool's
own balls are too few (the fence certification, SIM-478). The bundle keeps the last answer
in `venue_dimensions.json`.

| Function / class | What it does | Called from |
|---|---|---|
| `fetch_venue_dimensions` | One season's venues with their five published distances, over `urllib`. | `write_venue_dimensions` |
| `write_venue_dimensions` | Fetches every season of the window, merges the answers over the stored copy and writes it. A failed season keeps the last copy. | `pipeline/batch/artifacts/park_geometry.py` |
| `read_venue_dimensions` | The stored copy, or `{}`. | `pipeline/batch/artifacts/park_geometry.py` |

### `pipeline/etl/venue_backfill_job.py`

[Source](../../pipeline/etl/venue_backfill_job.py) · 349 lines · Tests: [test_venue_id_fallback.py](../../tests/unit/test_venue_id_fallback.py)

`VenueBackfillJob.run` fills a `raw.games.venue_id` the schedule left empty. It reads each
such game's schedule entry with `hydrate=venue`, updates the row when `raw.venues` holds the
venue, and logs a `raw.pipeline_run_log` row. It is idempotent. Run it as
`python -m pipeline.etl.venue_backfill_job`; `schedule_venue_backfill_job` registers it on
an APScheduler instance, but nothing in the stack calls that hook.

## Shared vocabularies and helpers

### `pipeline/statcast_events.py`

[Source](../../pipeline/statcast_events.py) · 493 lines · Tests: [test_out_label.py](../../tests/unit/test_out_label.py), [test_pitch_result_vocabulary.py](../../tests/unit/test_pitch_result_vocabulary.py), [test_steal_labels.py](../../tests/unit/test_steal_labels.py), [test_transition_pool.py](../../tests/unit/test_transition_pool.py), [test_profile_code_sets.py](../../tests/unit/test_profile_code_sets.py)

The one place that decides what a `raw.pitches` row means. Every out label derives from the
`events` column (the plate-appearance result on its last pitch), never from
`outs_on_pitch`. The module also holds the pitch-result code sets over the `type` column.

| Function / class | What it does | Called from |
|---|---|---|
| `PLAY_OUTS_BY_EVENT` / `FIELDING_OUT_EVENTS` | How many outs a play recorded (question 1). | the profile and pool SQL |
| `BATTER_RETIRED_EVENTS` / `batter_retired` / `sql_batter_retired` | Whether the batter was retired (question 2). The batter reaches on `force_out` and both fielder's-choice events. | the profile and pool SQL |
| `sql_play_outs` / `sql_outs_recorded` / `sql_cs_out` | The outs on a row: the event's outs, a hidden runner out, and a caught stealing in the middle of a plate appearance. | `pipeline/batch/profiles/` |
| `sql_runner_dest` / `sql_batter_dest` | Each runner's and the batter's base after the play (0 = out, 4 = scored), for the transition pools. | `pipeline/batch/profiles/pools.py` |
| `sql_steal_attempt` / `sql_steal_success` | The one steal label every site reuses. | the steal and advancement pool builders |
| `BALL_TYPES`, `CALLED_STRIKE_TYPES`, `WHIFF_TYPES`, `FOUL_TYPES`, `STRIKE_THREE_FOUL_TYPES`, `NON_SWING_TYPES`, `IN_PLAY_TYPES`, `FOUL_TIP_TYPES`, `SAVANT_WHIFF_TYPES` | The pitch-result code sets. `STRIKE_THREE_FOUL_TYPES` marks a two-strike foul tip or foul bunt as strike three (the foul-tip strike three, SIM-553). | the profiles, the pool build, the Data Lab routes |
| `sql_in_play` / `sql_swing` / `sql_savant_whiff` / `sql_called_strike` / `sql_csw` | Row filters built from those sets. The model's whiff counts a foul tip as contact; Savant's whiff (the Data Lab's) counts it as a whiff. | `api/routes/analytics.py`, `api/routes/ai_assistant.py`, the profiles |

**Watch out:** `tests/unit/test_out_label.py` fails if any profile site reads
`outs_on_pitch`. Filter on `type`, never on `description` (display text).

### `pipeline/bullpen_usage.py`

[Source](../../pipeline/bullpen_usage.py) · 303 lines · Tests: [test_bullpen_usage.py](../../tests/unit/test_bullpen_usage.py)

The pen's facts for the pitching-change draw and the reliever draw: each arm's rest
(capped at 5 days), whether he pitched in the last two days, his pitches over the last three,
and his role. One set of queries serves both the pool builder (in bulk) and the lineup
resolver (per game), so the pool and the live candidates share one definition.

| Function / class | What it does | Called from |
|---|---|---|
| `usage_for_appearances_sql` | Every appearance of the seasons with the arm's rest as of that game. | `fetch_game_context_sync` |
| `usage_for_candidates_sql` / `fetch_usage_for_candidates` | The rest of a list of arms as of a date. | `fetch_pen_for_game` |
| `rotation_starters_sql` | The team's rotation: the starters of its last 5 games who threw at least 45 pitches (an opener does not count). | `fetch_pen_for_game` |
| `pen_from_listing` | PURE: the pen = the arms the box lists as available, minus the rotation, minus today's starter. | `fetch_pen_for_game` |
| `fetch_pen_for_game` | The pen and each arm's usage for one game, or `None` when the box lists nothing. | `simulation/lineup_resolver.py` |
| `fetch_game_context_sync` | The builder's one Postgres read: each game's managers, each appearance's usage, each pitcher's hand. | `pipeline/batch/artifacts/manager_pool.py` |

**Watch out:** the windows end strictly before the game DATE, so the two games of a
double-header do not see each other's usage.

### `pipeline/db_dsn.py`

[Source](../../pipeline/db_dsn.py) · 65 lines · Tests: [test_db_dsn.py](../../tests/unit/test_db_dsn.py)

`require_dsn` returns `BASEBALL_DB_DSN` or raises an error that names the variable. The code
carries no default password. `dsn_label` gives `host[:port]/dbname` for a log line. The
scripts and the pool chain use them.

## The league schedule and game feed

### `pipeline/mlb_schedule.py`

[Source](../../pipeline/mlb_schedule.py) · 327 lines · Tests: [test_schedule_client.py](../../tests/unit/test_schedule_client.py), [test_slate_endpoint.py](../../tests/unit/test_slate_endpoint.py)

The one parser of the league schedule and the one status mapper. The day slate reads the
schedule for which games exist (owner ruling 2026-08-29).

| Function / class | What it does | Called from |
|---|---|---|
| `parse_schedule` / `parse_game` | One schedule response to `ScheduleGame` records (teams, probable pitchers, posted lineups, start time). PURE. | `api/league_feed.py`, `api/routes/games_slate.py`, `lineup_writer` |
| `card_state` / `card_state_of` / `card_state_from_raw` | The four card states the slate draws: `scheduled`, `live`, `final`, `postponed`. `card_state_from_raw` maps a stored `raw.games.status`. | the slate route, `mlb_game_feed.feed_state`, `lineup_writer` |
| `fetch_schedule_sync` | Reads the schedule over `urllib`. The historical loader reads the schedule through its own transport instead. | none today |

### `pipeline/mlb_game_feed.py`

[Source](../../pipeline/mlb_game_feed.py) · 681 lines · Tests: [test_game_feed.py](../../tests/unit/test_game_feed.py), [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py)

The league's per-game live feed turned into plain dicts for the game page. The parsers are
pure; the tests run them on recorded feeds in `tests/fixtures/mlb_game_feed/`.

| Function / class | What it does | Called from |
|---|---|---|
| `parse_game_feed` | The card detail: the linescore, both box scores, the posted lineups and, for a live game, the count, the runners, the fielders and the last play. | `api/routes/games_feed.py`, `api/routes/games_runs.py`, `api/routes/betting.py` |
| `parse_plays` / `finished_flags` | The real game's plate appearances in order, with a name map. | `api/routes/games_feed.py` |
| `state_at_pa` | The game as it stood at the start of one plate appearance: lineups, defense, runners, score, pitch counts, bench and pen. The "what if" runs start here (the game-page what-if, SIM-562); `simulation/start_state.py` takes the result. Raises `PlateAppearanceNotFound`. | `api/routes/games_feed.py` (`/feed/state`) |

## Odds

### `pipeline/odds_provider.py`

[Source](../../pipeline/odds_provider.py) · 797 lines · Tests: [test_odds_vocabulary.py](../../tests/unit/test_odds_vocabulary.py), [test_odds_provider_seam.py](../../tests/unit/test_odds_provider_seam.py), [test_odds_one_book_per_row.py](../../tests/unit/test_odds_one_book_per_row.py), [test_odds_readers.py](../../tests/unit/test_odds_readers.py)

The odds seam and the one source of the odds vocabulary. A provider is any object with the
`OddsProvider` methods; the mock and BettingPros providers register themselves by name.

| Function / class | What it does | Called from |
|---|---|---|
| `PROP_STATS` / `PITCHER_PROP_STATS` / `BATTER_PROP_STATS` | The fifteen prop markets: five pitcher markets (strikeouts, earned runs, walks, outs recorded, hits allowed) and ten batter markets (hits, home runs, total bases, RBIs, singles, doubles, triples, runs, stolen bases, hits + runs + RBIs). | every odds writer and reader |
| `PROP_STAT_TO_MODEL_PROP` | Each market's name in the simulator's prop output (`strikeouts` → `K`). `scripts/accuracy/records.py` keeps its own copy of the same pairs. | `scripts/probes/rescore_graded_book.py` |
| `GAME_MARKET_TYPES` / `GAME_MARKET_KIND` / `GAME_MARKET_SEGMENT` / `GAME_MARKET_SIDE` / `GAME_MARKET_NAMES` | The fifteen game markets: the full-game moneyline, run line and total; the first-inning and first-five moneyline, total and run line; each side's full-game and first-five team total; the first team to score; a run in the first inning. The kind says which columns a market fills (`moneyline`, `three_way`, `runline`, `total`, `yes_no`). | every odds writer and reader, `simulation/game_market_distributions.py` |
| `BOOK_NAMES` / `BOOK_KIND` / `GRADED_BOOK_PREFERENCE` / `BETTABLE_KINDS` | The books. A row names its book as `'bp:<id>'`. Only sportsbooks are bettable. The readers grade against the first book on the preference list that has a row: DraftKings, then BetMGM, FanDuel, theScore and seven more. | the readers, the BettingPros provider |
| `odds_rows_by_book` / `prop_rows_by_book` | Every book's rows of one market, through the provider's by-book method (or its one-row method, for a test fake). | the live cycles, the opening-line job, `scripts/load_historical_odds.py` |
| `get_odds_provider` / `register_odds_provider` | Pick the provider by name, else `ODDS_PROVIDER`, else `mock`. | the live pipeline, the opening-line job, the odds loader |

**Flags:** `ODDS_PROVIDER` (code default `mock`; `bettingpros` or `real` selects the
vendor). The compose stack reads it from `.env`; `tests/conftest.py` pins `mock`.

**Watch out:** a new prop market goes in four places together: the tuples here, a migration
that widens the `raw.prop_odds` CHECK, `MockOddsAPI`'s config and the BettingPros market
ids. Import-time guards and the vocabulary test fail when one is missing.

### `pipeline/odds_row_guard.py`

[Source](../../pipeline/odds_row_guard.py) · 460 lines · Tests: [test_odds_one_book_per_row.py](../../tests/unit/test_odds_one_book_per_row.py), [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_odds_book_census.py](../../tests/unit/test_odds_book_census.py)

The load guard: it refuses an odds row that no single book could post (one row per book,
SIM-555). Every writer calls it before it stores a row; the API's stored-odds read calls it
again. The module is pure.

| Function / class | What it does | Called from |
|---|---|---|
| `check_row` / `refuse_reason` | The rules, per market kind: a missing side; run-line spreads of different size; equal spreads priced like a pair; an over and an under at two lines; a first-five row that carries first-inning prices; a placeholder team-total line; a three-way row whose prices cannot be one market; a closing line stamped more than 15 minutes after the start; a price stamped no later than 15 minutes after a postponed game's original start. | the live cycles, the opening-line job, `scripts/load_historical_odds.py`, `api/routes/betting_stored_odds.py` |
| `RefusalTally` | Counts the refusals by rule, market and book, and warns when the refused share passes a limit. | the writers |
| `implied_probability` | The chance an American price stands for (+150 → 0.40). | `check_row`, the BettingPros twin rule |

### `pipeline/bettingpros_odds_provider.py`

[Source](../../pipeline/bettingpros_odds_provider.py) · 445 lines · Tests: [test_bettingpros_provider.py](../../tests/unit/test_bettingpros_provider.py), [test_odds_matcher.py](../../tests/unit/test_odds_matcher.py), [test_odds_provider_retry.py](../../tests/unit/test_odds_provider_retry.py), [test_odds_one_book_per_row.py](../../tests/unit/test_odds_one_book_per_row.py)

`BettingProsOddsProvider`, the real odds source over the BettingPros v3 API. It resolves an
MLB game to a vendor event by the date and the team nicknames, and an MLB player to a prop
offer by the normalized name. Its parts live in `pipeline/bettingpros/`.

| Function / class | What it does | Called from |
|---|---|---|
| `get_odds_by_book` / `get_prop_odds_by_book` | One row per book for one market and line type. An `opening` row is the opener's single row; any other line type gives one row per book with a usable line on every side, the vendor's blend (`bp:0`) included. Each row carries the vendor's stamp in `book_line_at`. | `odds_provider.odds_rows_by_book` / `prop_rows_by_book` |
| `get_odds` / `get_prop_odds` | The one-row methods. The default `book='consensus'` returns the first graded book's row; the blend and the other non-sportsbooks are never the default. | `GameOddsMixin._fetch_odds` (the live broadcast row) |

**Flags:** `ODDS_API_KEY` (the vendor key). `ODDS_OFFERS_CACHE_TTL_S` (default 30 seconds,
half the live cadence; 0 turns the caches off). `BETTINGPROS_MAX_RETRIES` (default 0) and
`BETTINGPROS_RETRY_WAIT_S` (default 2.0).

**Watch out:** leave `BETTINGPROS_MAX_RETRIES` unset in the app and the live service. A
retry waits with `time.sleep`, which stops the whole event loop. Only the offline loaders
turn retries on (`set_retry_policy`). A read that still fails becomes an empty result and
counts in `read_failures`.

### `pipeline/bettingpros/`

[Source](../../pipeline/bettingpros/) · 8 modules, 1907 lines · Tests: [test_odds_matcher.py](../../tests/unit/test_odds_matcher.py), [test_odds_game_markets.py](../../tests/unit/test_odds_game_markets.py), [test_odds_census_sql.py](../../tests/unit/test_odds_census_sql.py), [test_odds_loader_retry.py](../../tests/unit/test_odds_loader_retry.py)

The provider's parts. The class is `BettingProsOddsProvider(OfferRowsMixin, ProviderBase)`;
`OfferRowsMixin` extends `EventMatchMixin`, which extends `TransportMixin`.

| Module | Lines | What it holds |
|---|---|---|
| [`base.py`](../../pipeline/bettingpros/base.py) | 39 | `ProviderBase`: the settings and caches every mixin sees. |
| [`transport.py`](../../pipeline/bettingpros/transport.py) | 168 | `TransportMixin`: `_http_get_json` (the one read, with the retry policy: 429, 5xx, time-outs and dropped connections retry with a doubling wait up to 60 seconds), and the two network seams `_bp_get` and `_mlb_get` that the tests stub. |
| [`events.py`](../../pipeline/bettingpros/events.py) | 612 | `EventMatchMixin`: the game-to-event match (`_resolve_event`, `_match_by_start`, `_match_game_two`), the player-name lookup, the paged `/offers` read (at most 20 pages) and the caches. A found game or name is kept for the process; a failed lookup is kept for one cache lifetime. `forget_game` and `forget_failed_lookups` drop them. |
| [`schedule.py`](../../pipeline/bettingpros/schedule.py) | 155 | The schedule facts the match reads: the slates a game touched, its double-header number, its postponed start. The match reads the PLAYED entry, never a postponed one. |
| [`rows.py`](../../pipeline/bettingpros/rows.py) | 591 | `OfferRowsMixin`: one row per book (`_rows`, `_game_market_rows`, `_prop_market_rows`) and the first-five rules. A first-five entry that copies the book's first-inning prices (the "twin" rule), or a book on the dated exclusion list, gives no row. When the first-inning read fails, the first-five market gets no rows at all. |
| [`rules.py`](../../pipeline/bettingpros/rules.py) | 79 | The vendor-data rules: the dated first-five exclusions and the twin rule's price tolerance. |
| [`parsing.py`](../../pipeline/bettingpros/parsing.py) | 203 | One offer's sides, one book's usable line (an `is_off` or inactive line is not a quote), the shared opener, the vendor's stamp and the name key. |
| [`markets.py`](../../pipeline/bettingpros/markets.py) | 60 | The vendor market id of every game and prop market (`hits` is 287, `hits_allowed` 404), with import-time guards against the vocabulary. |

**Watch out:** game 2 of a straight double-header has a placeholder start, so the match takes
the later of the two teams' events on that date. A made-up game's rows carry
`postponed_start`, and the guard refuses a price stamped before it.

### `pipeline/etl/opening_line_job.py`

[Source](../../pipeline/etl/opening_line_job.py) · 652 lines · Tests: [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_etl_flow.py](../../tests/integration/test_etl_flow.py)

`OpeningLineJob.run` captures the opening lines of every game in the next seven days that has
none: the moneyline's opening rows (`_fetch_opening_rows`) and, once a starter is announced,
his five pitcher prop markets (`_capture_prop_opening_lines`). It stores every by-book
opening row the guard keeps, with `line_type='opening'`, and logs a `raw.pipeline_run_log`
row. Run it as `python -m pipeline.etl.opening_line_job`. `schedule_opening_line_job`
registers it on an APScheduler instance, but nothing in the stack calls that hook: the job
runs only by hand.

## The live service

### `pipeline/live/run_live.py`

[Source](../../pipeline/live/run_live.py) · 71 lines · Tests: none

The `live` service's entry point. `main` waits up to ten minutes for Postgres
(`wait_for_postgres`), builds the app with `publish_to_redis=True` and serves it on
`LIVE_PORT` (default 8001). The service passes no simulation callback, so a plate
appearance's end is logged, not simulated.

**Watch out:** never set `LIVE_PIPELINE_ENABLED` in the app while this service runs. Two
publishers would double-write every table.

### `pipeline/live/live_ingestion_pipeline.py`

[Source](../../pipeline/live/live_ingestion_pipeline.py) · 1147 lines · Tests: [test_live_pipeline.py](../../tests/unit/test_live_pipeline.py), [test_live_pipeline_setup.py](../../tests/unit/test_live_pipeline_setup.py), [test_odds_hash_dedup.py](../../tests/unit/test_odds_hash_dedup.py), [test_live_resimulate_cooldown.py](../../tests/unit/test_live_resimulate_cooldown.py), [test_odds_provider_ingestion.py](../../tests/unit/test_odds_provider_ingestion.py)

`LiveIngestionPipeline`: the day's games, their live state and their odds. It mixes in the
game-odds and prop-odds cycles from `pipeline/live/ingestion/`. The file also holds
`create_app` (the live service's app), the browser WebSocket route `game_state_ws` and the
`connection_manager` singleton.

| Function / class | What it does | Called from |
|---|---|---|
| `_sync_live_games` | The 30-second schedule poll over yesterday to tomorrow in Eastern time. Preview: pre-game odds and the posted lineup. Live: closing rows, first pitch, watcher, first refresh, and the feed lineup when none is stored. Final: stop the watcher and refresh once more. Every game: upsert `raw.games`. Then it writes the heartbeat. | `_schedule_poller` |
| `_refresh_game_state` | One update of one game: read the feed (Redis on failure), build the state, write `sim.lineup_state`, cache in Redis, run the odds cycles, broadcast, and signal the end of a plate appearance. A lock skips a signal while a refresh runs. | `MLBGameWebSocket`, `_sync_live_games` |
| `_persist_pregame_odds` | Before first pitch, stores every book's current lines at most every ten minutes, every minute inside 15 minutes of the start. It upserts the game row first (the foreign key). | `_sync_live_games` |
| `_mark_closing_rows` | The first time the poll sees a game Live, promotes each book's last pre-pitch row to `closing`. A failure is logged; the nightly closing pass repairs it. | `_sync_live_games` |
| `_should_resimulate` / `_signal_resimulation` | True once per completed plate appearance (`about.isComplete`). The signal calls the callback, or logs it when none is set. | `_refresh_game_state` |
| `_upsert_game_record` / `_upsert_schedule_fields` | The game row from the schedule entry, with the schedule's fields (Alembic 0029 columns). | `_sync_live_games` |
| `_write_preview_lineup` / `_write_live_lineup` | Hand a game to the published-lineup writer. | `_sync_live_games` |
| `create_app` | The live app: the pipeline's lifespan, the WebSocket and odds routers, `/health` (the heartbeat's age), `/api/pipeline/status` and a resimulate route with a 10-second cooldown per game. | `run_live.main` |

**Flags:** `ODDS_PROVIDER` (through the seam), `REDIS_URL`, `BASEBALL_DB_DSN`.

**Watch out:** the app mounts `ws_router` and `odds_router` from this module whether or not
the pipeline runs in its process.

### `pipeline/live/ingestion/`

[Source](../../pipeline/live/ingestion/) · 10 modules, 2592 lines · Tests: [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_live_game_state_builder.py](../../tests/unit/test_live_game_state_builder.py), [test_odds_last_seen.py](../../tests/unit/test_odds_last_seen.py), [test_live_connection_manager.py](../../tests/unit/test_live_connection_manager.py)

The live pipeline's parts.

| Module | Lines | What it holds |
|---|---|---|
| [`base.py`](../../pipeline/live/ingestion/base.py) | 137 | `PipelineBase`: the shared state, `_off_loop` (a vendor read on the pipeline's one worker thread, off the event loop), `_odds_provider` and `_guard_rows` (the load guard over one offer). |
| [`constants.py`](../../pipeline/live/ingestion/constants.py) | 74 | The settings: the MLB endpoints, the 30-second poll, the socket reconnect backoff (2 to 60 seconds), the Redis lifetimes (60 seconds live, an hour done), the 10-second resimulate cooldown, the 60-second odds cadence and the pre-game cadences. |
| [`game_odds.py`](../../pipeline/live/ingestion/game_odds.py) | 212 | `GameOddsMixin`: every book's row of all fifteen game markets, once a minute per game (`_persist_game_odds_cycle`), and `mark_closing_lines`. |
| [`prop_odds.py`](../../pipeline/live/ingestion/prop_odds.py) | 437 | `PropOddsMixin`: the players each market needs (a pitcher gets the pitcher markets, a hitter the batter markets), the prop cycle on the same cadence, `capture_opening_prop_lines` and `mark_closing_prop_lines`. |
| [`closing_rows.py`](../../pipeline/live/ingestion/closing_rows.py) | 296 | The closing-row rule: per (market, book) or (player, prop, book), the latest pre-pitch `current` row stamped no later than the start plus 15 minutes becomes `closing`; a key that holds a closing row is done. The row's hash is rewritten so the nightly loader's identical row deduplicates against it. |
| [`odds_rows.py`](../../pipeline/live/ingestion/odds_rows.py) | 376 | The insert statements and their arguments, the dedup hash (`ON CONFLICT (game_pk, source, odds_hash) DO NOTHING`; the live writers re-stamp `last_seen_at` instead), the pre-game cadence and the staleness test for a postponed or suspended game. |
| [`game_state_builder.py`](../../pipeline/live/ingestion/game_state_builder.py) | 499 | `GameStateBuilder.build`: one feed to the game-state JSON (inning, count, runners, lineups, bullpen with rest, bench, play history, linescore). It keeps each game's play history and parses only the new plays. |
| [`mlb_websocket.py`](../../pipeline/live/ingestion/mlb_websocket.py) | 166 | `MLBGameWebSocket` (one game's MLB push socket; any message is a change signal; it reconnects with backoff) and `ConnectionManager` (the browser sockets per game). |
| [`mock_odds.py`](../../pipeline/live/ingestion/mock_odds.py) | 343 | `MockOddsAPI`: deterministic lines seeded on the game, one book (`consensus`), for the tests and a stack without a vendor key. |
| [`odds_routes.py`](../../pipeline/live/ingestion/odds_routes.py) | 52 | `odds_router` at `/api/odds`: `/{game_pk}` and `/today/all`. Both serve mock lines and require a login. |

**Watch out:** the closing rows depend on the poll's own clock. A key with no closing row at
first pitch (a book that first posts in play) can take an in-play row on a later promotion.

### `pipeline/live/lineup_writer.py`

[Source](../../pipeline/live/lineup_writer.py) · 363 lines · Tests: [test_published_lineup_writer.py](../../tests/unit/test_published_lineup_writer.py)

`PublishedLineupWriter` makes an upcoming game simulable. The simulator resolves a game from
`raw.game_lineups`, and the historical loader writes that table only from a final box.

| Function / class | What it does | Called from |
|---|---|---|
| `on_preview` | When both lineups are posted, reads the feed once and writes the starting rows with `source = 'published'`. Each side's probable pitcher becomes its `P` row. It writes again only when the lineups or the probables change. | `_write_preview_lineup` |
| `on_live` | A game that started with no stored lineup gets its starting rows from the feed's box. | `_write_live_lineup` |
| `_projected_rows` | With `LIVE_PROJECTED_LINEUPS=1`, copies each team's last final lineup (against a starter of the same hand when one exists) with `source = 'projected'`. | `_on_preview` |
| `_ensure_players` | Adds a player the database does not know, from the people endpoint (the foreign key). | `_write_rows` |

**Flags:** `LIVE_PROJECTED_LINEUPS` (default 0; production 0).

**Watch out:** the final box is the authority. The writer never touches a game with box rows,
and the loader deletes the published and projected rows before its own insert.

### `pipeline/live/broadcast.py`

[Source](../../pipeline/live/broadcast.py) · 160 lines · Tests: [test_live_bridge.py](../../tests/unit/test_live_bridge.py)

The Redis bridge between the live service and the browsers. Nothing is stored on the
channel: a browser that connects late gets the next message.

| Function / class | What it does | Called from |
|---|---|---|
| `RedisBroadcaster` | Publishes each message on `live:game:{game_pk}`. | the live service's `create_app` |
| `RedisBridge` | The app side: one subscription per watched game, counted by reference; the last browser to leave ends it. | `api/startup.py` |
| `write_heartbeat` / `heartbeat_age_s` / `heartbeat_status` | `live:heartbeat` (the poll time) and `live:watching` (the live games), each with a 90-second lifetime. `/ready` reads `ok`, `stale` or `absent`. | `_sync_live_games`, `api/main.py` (`/ready`), `api/routes/metrics.py` |

### `pipeline/live/bullpen_availability_ingest.py`

[Source](../../pipeline/live/bullpen_availability_ingest.py) · 617 lines · Tests: [test_bullpen_availability.py](../../tests/unit/test_bullpen_availability.py)

`BullpenAvailabilityIngest` writes `raw.game_bullpen_availability`: one row per pitcher on
each team's full active roster, with whether he could pitch and the reason (`active`,
`IL`, `rest`, `recent_use`). It combines the MLB active-roster endpoint with the rest derived from
`raw.pitches`. The manager-usage profile reads it for the "chose not to use" signal. Run it
as `python -m pipeline.live.bullpen_availability_ingest --seasons ... --duckdb <file>`.

| Function / class | What it does | Called from |
|---|---|---|
| `build_rows` / `decide` | One row per roster pitcher; an injured-list status wins over rest. | `ingest_game` |
| `_rest_as_of` | The rest of an arm that did not pitch in the game, from his appearance timeline. | `build_rows` |
| `ingest` / `ingest_game` | Resolve and upsert every game of the workload records. | `main` |

**Watch out:** `REST_PITCHES_3D` (45) is an untuned placeholder. `AvailabilityRow.is_starter`
is always False.
