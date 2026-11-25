# Data ingestion

The `ingest/` folder brings the real data into the Postgres `raw.*` tables. It is the
first step of the program. It reads two sources. The MLB Stats API is the league's public
service for schedules, game feeds and box scores. Baseball Savant is the league's Statcast
site. The nightly builds read these tables next ([Player profiles](profiles.md), [Play pools](pools.md)).
The table layouts are on the [database page](database.md).

Two other parts also write `raw.*` tables, and both read the league through this folder's
client. The odds code lives in `betting/` ([Betting](betting.md)). The game-day service
lives in `pipeline/live/` ([Live service](live.md)).

| Part | Folder or file |
|---|---|
| The MLB Stats API client, the schedule parser and the game-feed parser | [`ingest/mlb/`](../../ingest/mlb/) |
| The game loader: each finished game's pitches, plays, lineups and venue | [`ingest/games/`](../../ingest/games/) |
| The official box score and the box's bullpen listing | [`ingest/load_boxscores.py`](../../ingest/load_boxscores.py) |
| Which pitchers each team can use in each game | [`ingest/load_bullpen_availability.py`](../../ingest/load_bullpen_availability.py) |
| The park dimensions, and the venues the schedule left empty | [`ingest/venues/`](../../ingest/venues/) |
| The Savant leaderboards, the per-pitch tracking and the sprint speeds | [`ingest/savant/`](../../ingest/savant/) |
| The Statcast event and pitch-result code sets | [`ingest/statcast_events.py`](../../ingest/statcast_events.py) |

## The terms

| Term | Meaning |
|---|---|
| **Final game** | A game whose schedule entry reads `abstractGameState = "Final"`. Only a Final game has full pitch-by-pitch data, so the loaders load no other game. |
| **The feed** | One game's live feed, `/api/v1.1/game/{game_pk}/feed/live`: every play, every pitch and both box scores. |
| **The box** | The official box score: one line per player who appeared, and the arms each side lists. A sportsbook grades a player prop on it. |
| **Transient failure** | A failed read that may succeed later: a time-out, a dropped connection, a truncated body, or HTTP 408, 425, 429 or any 5xx. |
| **The ingest ledger** | `raw.etl_game_ingest`: one row per game the loader finished, with its outcome, `loaded` or `empty`. |
| **The error ledger** | `raw.etl_errors`: one row per pitch the loader skipped, with the reasons. |

## How it fits together

**The one MLB client.** Every program read of the MLB Stats API goes through
[`ingest/mlb/client.py`](../../ingest/mlb/client.py).

1. A sync caller calls `get` or `get_json`. The GET runs on the standard library's `urllib`.
2. A transient failure retries by the caller's `RetryPolicy`. Any other failure raises at once.
3. The wait before retry *n* is `first_wait_s × 2^(n−1)`. A numeric `Retry-After` on a
   429 or 503 sets a floor. `cap_s` (60 seconds by default) sets a ceiling.
4. Each retry logs one INFO line, without the query string.
5. After the last retry, the original error reaches the caller unchanged.
6. An async caller uses `LeagueFeed`, which holds one `aiohttp` session. It never retries.

Each caller sets its own policy:

| Caller | Retries | First wait | Time-out per attempt |
|---|---|---|---|
| The game loader (`load_games._http_get`) | 9 (ten attempts) | 2 s | 30 s |
| The box-score backfill (`BoxscoreIngest`) | 3 (`max_attempts` 4) | 1 s | 15 s |
| The bullpen availability load | 3 (`DEFAULT_POLICY`) | 1 s | 15 s |
| The venue-dimension load | 1 (a failed season keeps the last copy) | 2 s | 30 s |
| The Savant loaders (`fetch_csv`) | 2 | 2 s | 180 s |
| BettingPros ([`betting/bettingpros/match_games.py`](../../betting/bettingpros/match_games.py)) | its own setting | its own setting | its own setting |
| `LeagueFeed`: the app's slate and game page, the live service, the venue backfill | none | — | 5 s (`SLATE_FEED_TIMEOUT_S`); 10 s in the live service and the venue backfill |

**1. The nightly finals and the weekly refresh.**

1. The `nightly-finals` job runs [`operations/jobs/nightly_finals.sh`](../../operations/jobs/nightly_finals.sh)
   at 07:00 UTC. The script calls `HistoricalDataLoader.load_date_range` over the last
   three days in Eastern time.
2. On Sunday at 08:00 UTC, the `weekly-refresh` job runs
   [`weekly_refresh.sh`](../../operations/jobs/weekly_refresh.sh). It calls
   `refresh_seasons` on the current season.
3. The loader reads the schedule. `load_date_range` asks for windows of at most 364 days.
4. `_load_schedule_dates` skips a rescheduled or resumed entry, and every game that is not Final.
5. `_dispatch_game` skips a game already loaded and loads the rest. Five failures in a row stop the run.
6. `_fetch_game_pitches` reads the feed with `hydrate=alignment`, and both teams' coaches.
   It returns one dict per pitch and collects the non-pitch events.
7. `_ensure_prerequisites` writes every parent row the pitches need, in foreign-key
   order: `raw.venues`, `raw.teams`, `raw.players`, `raw.managers`, `raw.games`.
8. The same step writes `raw.game_lineups`, the box (`raw.game_player_stats`) and the
   box's bullpen listing (`raw.game_bullpen`), from the feed in hand.
9. `_process_and_insert` builds and checks each pitch row. One transaction writes
   `raw.pitches`, `raw.play_events`, the error ledger and the ingest ledger.
10. `_log_freshness` updates `raw.etl_data_freshness`.

The operations page describes the two jobs and their wrappers ([Operations](operations.md)).

**2. The box-score backfill.** `python -m ingest.load_boxscores` loads the box and the
bullpen listing of every Final game in `raw.games` that lacks them. It reads
`/game/{game_pk}/boxscore` once per game. The game loader writes the same two tables, so
the backfill only fills a gap or refreshes the rows.

**3. The corrective reload.** The ordinary load cannot repair a loaded game: its insert
skips every row that exists. After a parser change, an operator runs one of two tools.
`ingest/games/reload_games.py` reloads named games, or every gap it finds.
`ingest/games/resumable_sweep.py` reloads whole seasons and restarts after a crash. Both
call `reload_game`, which replaces a game's rows in one transaction.

**4. The reference loaders, run by hand.** Five loaders have a command-line entry point
and no scheduler job: the Savant leaderboards, the per-pitch tracking, the sprint speeds,
the bullpen availability and the venue backfill. The bundle build calls one more
loader: `write_venue_dimensions` refreshes the published fence distances.

**Docker.** [docker-compose.yml](../../docker-compose.yml) mounts `./ingest` into the
`app`, `live` and `jobs` services, and the image copies it too. An edit reaches those
containers without an image rebuild. A scheduled job runs the mounted copy at its next start.

### Who runs each loader, and what it writes

| Loader | Run by | Writes |
|---|---|---|
| `HistoricalDataLoader` ([`ingest/games/load_games.py`](../../ingest/games/load_games.py)) | the `nightly-finals` job (`load_date_range`); the `weekly-refresh` job (`refresh_seasons`); `make ingest-catch-up DAYS=14`; `reload_games.py`; `resumable_sweep.py` | `raw.venues`, `raw.teams`, `raw.players`, `raw.managers`, `raw.games`, `raw.game_lineups`, `raw.game_player_stats`, `raw.game_bullpen`, `raw.pitches`, `raw.play_events`, `raw.etl_errors`, `raw.etl_game_ingest`, `raw.etl_data_freshness` |
| The box-score backfill (`python -m ingest.load_boxscores`) | by hand | `raw.game_player_stats`, `raw.game_bullpen` |
| `BullpenAvailabilityIngest` (`python -m ingest.load_bullpen_availability`) | by hand | `raw.game_bullpen_availability` |
| `VenueBackfillJob` (`python -m ingest.venues.backfill_venues`) | by hand | `raw.games.venue_id`, `raw.pipeline_run_log` |
| `write_venue_dimensions` | the bundle build (`build_park_geometry`) | `venue_dimensions.json` in the bundle folder; no table |
| The leaderboard loader (`python -m ingest.savant.load_leaderboards`) | by hand | the twelve `raw.savant_*` board tables |
| The pitch-tracking loader (`python -m ingest.savant.load_pitch_tracking`) | by hand | `raw.savant_pitch_tracking` |
| `SprintSpeedLoader` (`python -m ingest.savant.load_sprint_speed`) | by hand | `raw.sprint_speed` |

Every loader that writes Postgres reads its address from `BASEBALL_DB_DSN`. No loader has
a default password ([the database page](database.md#dbdsnpy)).

---

## The package

### `ingest/__init__.py`

[Source](../../ingest/__init__.py) · 13 lines · Tests: none

The package docstring: one line per part, and the program's next steps. Import every
name from its own module.

---

## The MLB Stats API: `ingest/mlb/`

### `ingest/mlb/client.py`

[Source](../../ingest/mlb/client.py) · 261 lines · Tests: [test_mlb_client.py](../../tests/unit/ingest/test_mlb_client.py), [test_etl_hardening.py](../../tests/unit/ingest/test_etl_hardening.py), [test_savant_loader.py](../../tests/unit/ingest/test_savant_loader.py), [test_boxscore_ingest.py](../../tests/unit/ingest/test_boxscore_ingest.py), [test_park_geometry_builder.py](../../tests/unit/bundle/test_park_geometry_builder.py)

The one MLB Stats API client. It holds the endpoint addresses, the sync GET with its
retry rule, and the async reader. The Savant and BettingPros reads use the same sync GET.

| Function / class | What it does | Called from |
|---|---|---|
| `MLB_BASE`, `API_V1`, `SCHEDULE_URL`, `GAME_FEED_URL`, `PEOPLE_URL`, `VENUES_URL`, `VENUE_URL`, `COACHES_URL` | The endpoint addresses. | every reader of the league |
| `RetryPolicy` | How a read retries: `retries` after the first attempt, `first_wait_s` and `cap_s`. `wait_s` gives the wait before one retry. | each caller (the policy table above) |
| `DEFAULT_POLICY` | Three retries, the first after one second. | `load_bullpen_availability.py` |
| `is_transient` | True for HTTP 408, 425, 429 or any 5xx, and for any `OSError` or `http.client.HTTPException`. | `get` |
| `retry_after_s` | The numeric `Retry-After` of a 429 or 503, in seconds, else None. | `RetryPolicy.wait_s` |
| `log_target` | A URL's host and path, without the query, for a log line. | `get`; `betting/bettingpros/match_games.py` |
| `get` / `get_json` | One sync GET with the retry rule. `get` returns a `Response` (status, headers, decoded text, URL); `get_json` returns the parsed body. A list value repeats its key in the query. `describe` cleans an error text: the odds reads take their key out. | `load_games._http_get`, `BoxscoreIngest._mlb_get`, `BullpenAvailabilityIngest._mlb_get`, `fetch_venue_dimensions`, `load_leaderboards.fetch_csv`, `match_games.TransportMixin._http_get_json` |
| `LeagueFeed` | The async reader over one lazy `aiohttp` session. `get_json` reads any address; `schedule_payload` reads the schedule; `game_feed_payload` reads one feed with `hydrate=alignment`. A non-2xx status raises `aiohttp.ClientResponseError`. | `api/main.py` (`app.state.league_feed`, for the slate and the game page), `LiveIngestionPipeline.start`, `VenueBackfillJob.run` |

**Flags:** `SLATE_FEED_TIMEOUT_S` (the `LeagueFeed` time-out when the caller sets none; default 5).

**Watch out:** `get` with no `policy` makes ONE attempt: `RetryPolicy()` has zero
retries. `LeagueFeed` never retries, so its caller handles a failure. Keep the sync GET
on `urllib`: the long reload sweeps crashed with a memory fault on `requests`. `urllib`
asks for an uncompressed body, so the client has no gunzip step.
[test_etl_hardening.py](../../tests/unit/ingest/test_etl_hardening.py) (`TestHttpHygiene`)
fails when `load_games.py` or a Savant module opens a URL of its own. It also fails when
`load_games.py` or the client imports `requests`.

### `ingest/mlb/schedule.py`

[Source](../../ingest/mlb/schedule.py) · 335 lines · Tests: [test_schedule_client.py](../../tests/unit/ingest/test_schedule_client.py), [test_odds_matcher.py](../../tests/unit/test_odds_matcher.py)

The one parser of the league schedule, and the one status mapper. The day slate takes its
games from the schedule, not from the database (owner ruling 2026-08-29). The parser is
pure. The tests run it on recorded responses in `tests/fixtures/mlb_schedule/`.

| Function / class | What it does | Called from |
|---|---|---|
| `ScheduleGame` / `ScheduleTeam` / `SchedulePlayer` | One game as the slate needs it: the teams, their records and scores, the probable pitchers, the posted lineups, the start, the state, the venue, the double-header fields and a live game's inning. `lineups_posted` is true when both batting orders are in. | the slate route, `pipeline/live/lineup_writer.py`, the live service |
| `parse_schedule` / `parse_game` | One schedule response to `ScheduleGame` records. `parse_schedule` logs and skips a malformed entry, so one bad entry never fails the day. | `parse_schedule`: `api/routes/games_slate.py`. `parse_game`: `parse_schedule`, `lineup_writer.py`, `LiveIngestionPipeline._upsert_schedule_fields` |
| `card_state_of` / `card_state` / `card_state_from_raw` | The four card states the slate draws: `scheduled`, `live`, `final`, `postponed`. `card_state_from_raw` maps a stored `raw.games.status` when the schedule read fails. | `card_state_of`: `game_feed.feed_state`. `card_state`: `games_slate.py`, `lineup_writer.py`. `card_state_from_raw`: `games_slate.py` |
| `schedule_params` | The query of one schedule request: every type in `GAME_TYPES`, the date window and the hydrate. | `LeagueFeed.schedule_payload`, `LiveIngestionPipeline._sync_live_games` |
| `is_postponed` / `schedule_entries` / `played_index` | Whether an entry is postponed; every `(date, game)` entry of one answer; the PLAYED entry of a game the schedule lists twice (a postponed game made up later). | `betting/bettingpros/match_games.py`; `schedule_entries` and `played_index` also `BullpenAvailabilityIngest._resolve_game_meta` |
| `int_or_none` | A value as an int, or None when it is missing, blank or not a number. | `game_feed.py` |

**Watch out:** the league marks a postponed game `abstractGameState = "Final"`, so
`card_state_of` reads the coded state first. The card shows a suspended game as
postponed, but `is_postponed` does not: MLB marks both entries of a suspended game
"Final". `GAME_TYPES` here holds `C`; the game loader's list leaves `C` out, because the
`raw.games` CHECK refuses it.

### `ingest/mlb/game_feed.py`

[Source](../../ingest/mlb/game_feed.py) · 688 lines · Tests: [test_game_feed.py](../../tests/unit/ingest/test_game_feed.py), [test_starting_position.py](../../tests/unit/ingest/test_starting_position.py), [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py)

The league's per-game feed, turned into plain dicts for the game page. The parsers are
pure. The tests run them on trimmed recorded feeds in `tests/fixtures/mlb_game_feed/`. A
field the feed lacks reads None, and the page hides it.

| Function / class | What it does | Called from |
|---|---|---|
| `parse_game_feed` | The slate card's detail: the linescore, both box scores, the posted lineups and the probable pitchers. A live game adds the count, the runners, the fielders, the pitch count and the last play. | `api/routes/games_feed.py` (`/feed`), `api/routes/games_runs.py`, `api/routes/betting.py` |
| `feed_state` | A feed's card state, through the schedule's mapper. | `api/routes/games_feed.py` |
| `starting_position` | The position a player held when he entered: the first entry of `allPositions`, not the box's `position`, which is his LAST position. A two-way starter who pitches and bats as the designated hitter reads `P`. | `game_values._build_starting_lineup_rows`, this module's box and lineup rows, `scripts/probes/backfill_start_positions.py` |
| `parse_plays` / `finished_flags` | The real game's plate appearances in order, with a name map. `finished_flags` keeps a batter's turn open when his half ends on an out on the bases. | `parse_plays`: `api/routes/games_feed.py` (`/feed/plays`) |
| `state_at_pa` | The game as it stood at the start of one plate appearance: lineups, defense, runners, score, pitch counts, bench and pen. The what-if runs start here; `simulation/start_state.py` reads the result. It raises `PlateAppearanceNotFound` before the lineups or the starting pitchers are posted. | `api/routes/games_feed.py` (`/feed/state`), `api/routes/games_runs.py` |

**Watch out:** `state_at_pa` needs the feed read with `hydrate=alignment`. Only then does
each pitch carry the fielding side's nine. `LeagueFeed.game_feed_payload` adds it.

---

## The game loader: `ingest/games/`

`HistoricalDataLoader` loads finished games, pitch by pitch. The class lives in
`load_games.py`, beside the feed parser and the two HTTP seams. It mixes in
`ReferenceRowsMixin` and `WritesMixin`, and both share the state that `LoaderBase`
declares. Each other module holds one job. The sections follow one game through the loader.

### `ingest/games/load_games.py`

[Source](../../ingest/games/load_games.py) · 1415 lines · Tests: [test_etl_historical_loader.py](../../tests/unit/ingest/test_etl_historical_loader.py), [test_etl_reload_game.py](../../tests/unit/ingest/test_etl_reload_game.py), [test_etl_ensure_rows.py](../../tests/unit/ingest/test_etl_ensure_rows.py), [test_etl_hardening.py](../../tests/unit/ingest/test_etl_hardening.py), [test_etl_connection_pool.py](../../tests/unit/ingest/test_etl_connection_pool.py), [test_etl_error_ledger.py](../../tests/unit/ingest/test_etl_error_ledger.py), [test_boxscore_ingest.py](../../tests/unit/ingest/test_boxscore_ingest.py), [test_play_events.py](../../tests/unit/ingest/test_play_events.py), [test_venue_resolution.py](../../tests/unit/ingest/test_venue_resolution.py), [test_mlb_client.py](../../tests/unit/ingest/test_mlb_client.py), [test_secrets_baseline.py](../../tests/unit/test_secrets_baseline.py), [test_etl_flow.py](../../tests/integration/test_etl_flow.py)

The loader of finished games. The tests patch `_connect`, `_http_get` and
`_fetch_game_pitches` by name, so those names stay in this file.

| Function / class | What it does | Called from |
|---|---|---|
| `HistoricalDataLoader.load_date_range` | Loads every Final game between two dates. It reads the schedule in windows of at most 364 days, and warns when a window's answer stops short. `reload=True` sends every game through `reload_game`. | `operations/jobs/nightly_finals.sh` |
| `HistoricalDataLoader.refresh_seasons` | Loads every Final game of a range of seasons, one schedule read per season. The default end is last season. `reload=True` re-ingests. | `operations/jobs/weekly_refresh.sh`, `resumable_sweep.py`, this module's `__main__` |
| `HistoricalDataLoader.load_game` | Loads one game. The pitch insert skips a row that exists. | `_dispatch_game` |
| `HistoricalDataLoader.reload_game` | Deletes and re-inserts one game's pitch rows and play events in one transaction. It refuses to store fewer rows than it deleted (`ReloadShrinkError`), unless `allow_shrink=True`. | `_dispatch_game`, `reload_games.py` |
| `HistoricalDataLoader._load_schedule_dates` | Sends every Final game of the schedule's dates to `_dispatch_game`. It skips the entry of a rescheduled or resumed game. | `load_date_range`, `refresh_seasons` |
| `HistoricalDataLoader._dispatch_game` | Loads or reloads one game, and adds its outcome to the run's counts. It contains one game's failure. `ETL_CONSECUTIVE_FAILURE_LIMIT` failures in a row raise `RuntimeError`. | `_load_schedule_dates`; `resumable_sweep.py` wraps it |
| `HistoricalDataLoader._ensure_prerequisites` | Writes every parent row of `raw.pitches` in foreign-key order, then the starting lineups and the box. Each step asks the database which ids are missing and fetches only those. | `_load_game` |
| `HistoricalDataLoader._ensure_venue` | Upserts a missing venue-season. The fence distances and heights come from Savant's park-factor page, else its venue page. The location, surface and roof come from the MLB venue endpoint. `force=True` corrects a stored row. | `_ensure_prerequisites`, `ReferenceRowsMixin._ensure_teams`, `reload_games.py --refresh-venues` |
| `HistoricalDataLoader._ensure_players` | Upserts every player of the game's box. The person record comes from the feed, then the box, then `/people/{id}`. A player whose batting or throwing hand stays unknown is skipped, never guessed. | `_ensure_prerequisites` |
| `HistoricalDataLoader._ensure_game_player_stats` | Writes the box and the bullpen listing from the feed in hand, in one transaction. A failure logs a warning and never fails the game load. | `_ensure_prerequisites` |
| `_fetch_game_pitches` | Reads the feed with `hydrate=alignment`, both teams' coaches on the game date, and each new batter's person record. It tracks the outs and the bases entering each play, the extra-inning runner included. It returns one dict per pitch; the managers and the non-pitch events ride on the returned feed dict. | `_load_game` |
| `_http_get` / `_connect` | One GET through the MLB client with the loader's policy: ten attempts, the first retry after two seconds. `_connect` parses the JSON. | every read of the loader |

**Flags:** `BASEBALL_DB_DSN` (required). `ETL_CONSECUTIVE_FAILURE_LIMIT` (default 5).
`ETL_DB_POOL_MIN` and `ETL_DB_POOL_MAX` (default 1 and 8).

**Watch out:** `load_game` cannot repair loaded data. Its insert carries
`ON CONFLICT ... DO NOTHING`, so a rerun after a parser fix writes nothing. Use
`reload_game`, `refresh_seasons(reload=True)` or `load_date_range(reload=True)`. A range
load skips a game with a pitch row, or with a `loaded` or `empty` outcome in the ingest
ledger.

### `ingest/games/date_windows.py`

[Source](../../ingest/games/date_windows.py) · 43 lines · Tests: [test_etl_hardening.py](../../tests/unit/ingest/test_etl_hardening.py), [test_etl_reload_game.py](../../tests/unit/ingest/test_etl_reload_game.py), [test_etl_schedule_final_filter.py](../../tests/unit/ingest/test_etl_schedule_final_filter.py)

The schedule helpers of a range load.

| Function / class | What it does | Called from |
|---|---|---|
| `_date_chunks` | Splits an inclusive date range into windows of at most 364 days. | `load_date_range` |
| `_schedule_game_is_final` | True for a schedule entry that reads `abstractGameState = "Final"`. | `_load_schedule_dates` |
| `_CONSECUTIVE_FAILURE_LIMIT` | The failures in a row that stop a run (`ETL_CONSECUTIVE_FAILURE_LIMIT`, default 5). | `_dispatch_game` |

**Watch out:** the schedule endpoint cuts a window wider than a year and still answers
HTTP 200. A cut answer looks like a stretch with no games.

### `ingest/games/venues.py`

[Source](../../ingest/games/venues.py) · 84 lines · Tests: [test_venue_resolution.py](../../tests/unit/ingest/test_venue_resolution.py)

The venue where a game was PLAYED.

| Function / class | What it does | Called from |
|---|---|---|
| `_resolve_venue` / `_resolve_venue_id` | The venue's `(id, name)` from `gameData.venue`, else from `_VENUE_OVERRIDES`, the neutral-site games whose feed has none. Else it raises `MissingVenueError` with the steps to fix it. | `_resolve_venue`: `_fetch_game_pitches`. `_resolve_venue_id`: `_ensure_prerequisites`, `_ensure_game` |
| `_first_nonblank` | The first candidate that is a non-blank string. | `_ensure_venue` |

**Watch out:** never fall back to `teams.home.venue`. For a neutral-site game it is the
home team's regular park. A wrong venue id passes the NOT NULL and the foreign key, and it
distorts that park's park factor with no error.

### `ingest/games/game_values.py`

[Source](../../ingest/games/game_values.py) · 165 lines · Tests: [test_etl_historical_loader.py](../../tests/unit/ingest/test_etl_historical_loader.py), [test_etl_hardening.py](../../tests/unit/ingest/test_etl_hardening.py), [test_etl_starting_lineups.py](../../tests/unit/ingest/test_etl_starting_lineups.py), [test_starting_position.py](../../tests/unit/ingest/test_starting_position.py)

The values the loader reads from the feed.

| Function / class | What it does | Called from |
|---|---|---|
| `_manager_from_coaches` | A team's manager from its coaches: the acting manager, then the manager, then the bench coach. With none of them it returns `("", "")`. | `_fetch_game_pitches` |
| `_parse_height` | A height string (`6' 2"` or `6-2`) in inches, or None. | `_ensure_players`; `pipeline/live/lineup_writer.py` |
| `_parse_wind` | The feed's wind string (`"8 mph, L To R"`) as `(speed, direction)`. `"Calm"` reads `(0, "Calm")`. | `_ensure_game` |
| `_map_game_status` | The feed's state as a `raw.games.status` value. The coded states D, T and C win over the abstract state. | `_ensure_game` |
| `_build_starting_lineup_rows` | Both starting lineups from the feed's box, as `raw.game_lineups` tuples. Each starter sits at the position he held at first pitch (`starting_position`). The starting pitcher joins with a NULL batting order when he does not bat. | `_ensure_game_lineups`; `pipeline/live/lineup_writer.py`; `api/routes/games_feed.py` |

**Watch out:** `_manager_from_coaches` has no fallback to the first coach listed. That
coach is usually a pitching coach, and a wrong id reaches every pitch row, `raw.games` and
`raw.managers`.

### `ingest/games/play_events.py`

[Source](../../ingest/games/play_events.py) · 365 lines · Tests: [test_play_events.py](../../tests/unit/ingest/test_play_events.py)

The non-pitch events a pitch row cannot hold: pickoff throws and their outcomes,
step-offs, balks and intentional walks. An intentional walk has no pitch, so without this
module the database holds no intentional walk. The module is pure: no network, no database.

| Function / class | What it does | Called from |
|---|---|---|
| `extract_play_events` | One `raw.play_events` row per non-pitch event of one play. It joins a pickoff throw to its outcome, and names the pitcher who threw, a change in the middle of the plate appearance included. It builds an intentional walk's row from the play's result. | `load_games._fetch_game_pitches` |

**Watch out:** each signal lives in an unexpected place in the feed. A pickoff throw is
`type == "pickoff"` with an empty event type. A pickoff OUT lives on the runner entry. The
caller passes the bases, the outs and the score entering the play: the module cannot
derive them.

### `ingest/games/base.py`

[Source](../../ingest/games/base.py) · 110 lines · Tests: [test_etl_connection_pool.py](../../tests/unit/ingest/test_etl_connection_pool.py)

`LoaderBase`: the state every part of the loader shares. The state is the DSN, the
connection pool, the pool's lock and the count of games that failed in a row.

| Function / class | What it does | Called from |
|---|---|---|
| `LoaderBase._ensure_pool` | Builds the `ThreadedConnectionPool` once, on the first database use, under a lock. | `_get_conn` |
| `LoaderBase._get_conn` | Lends a pooled connection for one block, then returns it open. The caller commits or rolls back. | every database step |
| `LoaderBase._exists_once` | Whether a table or a column exists, asked once per loader and cached. It logs an absence once. | `_ensure_game_player_stats`, `_ensure_game_lineups`, `_write_play_events` |
| `LoaderBase.close` | Closes every pooled connection. | the two nightly jobs, `reload_games.py`, `resumable_sweep.py` |

**Watch out:** the `_exists_once` cache assumes that no migration runs during a sweep.

### `ingest/games/reference_rows.py`

[Source](../../ingest/games/reference_rows.py) · 405 lines · Tests: through `HistoricalDataLoader`: [test_etl_ensure_rows.py](../../tests/unit/ingest/test_etl_ensure_rows.py), [test_etl_historical_loader.py](../../tests/unit/ingest/test_etl_historical_loader.py), [test_etl_hardening.py](../../tests/unit/ingest/test_etl_hardening.py)

`ReferenceRowsMixin`: the reference rows a game needs before its pitches.

| Function / class | What it does | Called from |
|---|---|---|
| `_ensure_teams` | Upserts each missing team-season from the feed. It first makes sure the team's home park exists. | `_ensure_prerequisites` |
| `_ensure_managers` | Upserts both managers. A new manager closes the incumbent's active stint on the game date. A manager seen again in the dugout is active again. | `_ensure_prerequisites` |
| `_ensure_game` | Upserts the `raw.games` row: the venue, the teams, the type, the status, the weather, the final score, the inning scores and the winning, losing and save pitchers. The upsert always runs, so a re-fetch fills in a final score. | `_ensure_prerequisites` |
| `_ensure_game_lineups` | Inserts both starting lineups. It first deletes the game's non-box rows (`source <> 'box'`), so the final box replaces a published or projected lineup. | `_ensure_prerequisites` |

### `ingest/games/pitch_rows.py`

[Source](../../ingest/games/pitch_rows.py) · 409 lines · Tests: [test_etl_historical_loader.py](../../tests/unit/ingest/test_etl_historical_loader.py), [test_etl_hardening.py](../../tests/unit/ingest/test_etl_hardening.py), [test_etl_reload_game.py](../../tests/unit/ingest/test_etl_reload_game.py), [test_release_speed_bounds.py](../../tests/unit/ingest/test_release_speed_bounds.py)

One `raw.pitches` row: the required columns, the name map, the row builder and the
two-tier check.

| Function / class | What it does | Called from |
|---|---|---|
| `COLUMN_RENAME` / `HALF_INNING_MAP` | The parser's names to the `raw.pitches` column names; the feed's half-inning name to `Top` or `Bot`. | `_build_row_dict` |
| `_build_row_dict` | One parsed pitch to a dict keyed by the columns. An empty string becomes None, and each value takes its column's type. | `_process_and_insert` |
| `_validate_row` | The two-tier check. A hard error skips the row and logs it to the error ledger. A warning keeps the row with `data_quality_flag = TRUE`. | `_process_and_insert` |
| `ALWAYS_REQUIRED` / `IN_PLAY_REQUIRED` | The columns every pitch must fill; the columns an in-play pitch (`X`, `D` or `E`) must fill. | `_validate_row` |

**Watch out:** the hard errors mirror the database's CHECK ranges. A value outside a CHECK
would fail the whole game's insert, so it is a hard error, never a warning. The warning
bands must match the `raw.flag_pitch_quality()` trigger. A change to a band needs a
migration and an edit of `db/schemas/01_postgres_schema.sql`.

### `ingest/games/write_rows.py`

[Source](../../ingest/games/write_rows.py) · 528 lines · Tests: [test_etl_historical_loader.py](../../tests/unit/ingest/test_etl_historical_loader.py), [test_etl_reload_game.py](../../tests/unit/ingest/test_etl_reload_game.py); through `HistoricalDataLoader`: [test_etl_error_ledger.py](../../tests/unit/ingest/test_etl_error_ledger.py)

`WritesMixin`: a game's rows and the run's records.

| Function / class | What it does | Called from |
|---|---|---|
| `_game_already_loaded` | True when the game has a pitch row, or a `loaded` or `empty` outcome in the ingest ledger. | `_dispatch_game` |
| `_process_and_insert` | Builds and checks each pitch row, then writes the game. It returns the rows inserted, skipped and flagged. | `_load_game` |
| `_batch_insert` | One transaction per game: the optional delete, the play events, the pitch rows in 500-row batches, the error ledger and the ingest outcome. A count before and after measures the rows written. | `_process_and_insert` |
| `_write_play_events` / `_write_error_ledger` / `_write_ingest_outcome` | The three writes on the caller's cursor, inside the game's transaction. The outcome `empty` marks a game with no pitch row, so the nightly run stops re-fetching it. | `_batch_insert` |
| `_log_freshness` | Each pitcher's and batter's last loaded game in `raw.etl_data_freshness`. An older game never overwrites a newer one. | `_process_and_insert` |
| `ReloadShrinkError` | A reload would store fewer rows than it deleted. It raises before the commit, so the delete rolls back. | `_batch_insert` |

**Watch out:** the reload path never reads the ingest ledger, so a reload still reaches a
game marked `empty`.

### `ingest/games/coercion.py`

[Source](../../ingest/games/coercion.py) · 52 lines · Tests: [test_etl_historical_loader.py](../../tests/unit/ingest/test_etl_historical_loader.py)

`to_float`, `to_int`, `to_bool` and `to_str`: the shared rule that an empty string, None
and a NaN float all read as None. The pitch-row builder, the venue resolver and the three
Savant loaders use them.

**Watch out:** `_opt_int` in `ingest/load_bullpen_availability.py` and `_opt_float` in
`betting/bettingpros/fetch_odds.py` do not treat `""` as missing. Folding them in here
changes their behaviour.

### `ingest/games/reload_games.py`

[Source](../../ingest/games/reload_games.py) · 366 lines · Tests: none

Reloads named games, or every gap it finds in the loaded games. It sorts the gaps into
four classes:

- **STALE**: pitch rows but no ingest-ledger row. The game looks loaded, but its rows come
  from an older parser.
- **FAILED**: a ledger row that is not `loaded`, for a game whose status is `Final`.
- **NEVER LOADED**: a Final game with neither pitch rows nor a ledger row.
- **NOT PLAYED**: an `empty` outcome for a cancelled or postponed game. Zero pitches is
  the correct answer.

The script reloads the first three classes and lists the fourth. Each game reports its
rows before and after. The run ends with a per-season check of the ledger against
`raw.pitches`.

| Function / class | What it does | Called from |
|---|---|---|
| `_discover` | The games to reload, each with its reason, and the games never played. | `main` |
| `_refresh_venues` | Re-fetches every `raw.venues` row with a blank name, through `_ensure_venue(force=True)`. | `main` (`--refresh-venues`) |
| `_reconcile` | Per season, the ledger's loaded games against the distinct games in `raw.pitches`. More games in `raw.pitches` means stale games remain. | `main` |
| `_load_env` / `_host_dsn` | Reads `BASEBALL_DB_DSN` from the repository's `.env`, and points it at the host's published port (`DB_HOST_PORT`, default 5432). | `main`; the child process of `resumable_sweep.py` |

Run on the host: `python ingest/games/reload_games.py --dry-run`, then without
`--dry-run`. `--game-pk` names games; `--season` limits the search to one season;
`--allow-shrink` allows a smaller parse; `--refresh-venues` repairs blank venue names. The
exit code is 1 when a game still fails, a named game is unknown, or a venue name stays blank.

**Watch out:** the script reads the DSN from `.env`, not from the environment.
`.env.example` carries the DSN line commented out, so add a host DSN line before a run.

### `ingest/games/resumable_sweep.py`

[Source](../../ingest/games/resumable_sweep.py) · 257 lines · Tests: [test_sweep_streaming.py](../../tests/unit/ingest/test_sweep_streaming.py)

Drives a corrective reload of whole seasons to its end. The app image's interpreter
crashes now and then (the SIM-445 crash class, CLAUDE.md §2a), and the sweep survives that
crash. It runs `refresh_seasons(season, season, reload=True)` in a child process. The child
records each game that LOADED in `.sweep_progress/<season>.txt`, which git ignores. After
a crash the driver starts a new child, which skips the recorded games. A crash cannot
corrupt a game, because each reload is one transaction. A full ten-season sweep takes
about six hours.

| Function / class | What it does | Called from |
|---|---|---|
| `_run_attempt` | Runs one child and streams its output. It returns the exit code, whether the child finished the season, and the child's failed games. | `main` |
| `main` | Per season, runs attempts until the season completes with no failed game. An attempt that loads no new game stops the run with exit 1. | the operator |

Run on the host: `python ingest/games/resumable_sweep.py --season 2017 --end-season 2026`.
`--in-docker` uses the compose DSN instead of `127.0.0.1:DB_HOST_PORT`. `--quiet` keeps the
summaries, the crash text and a heartbeat every 25 games. `--max-attempts` defaults to 500
per season.

**Watch out:** the child reads the DSN from `.env` through `reload_games._load_env`, so
the `.env` trap above applies. The child records a game only when the run's loaded count
rises. `_dispatch_game` contains a failure, so its return does not prove the game loaded.

---

## The box score and the bullpens

### `ingest/load_boxscores.py`

[Source](../../ingest/load_boxscores.py) · 747 lines · Tests: [test_boxscore_ingest.py](../../tests/unit/ingest/test_boxscore_ingest.py), [test_game_bullpen_listing.py](../../tests/unit/ingest/test_game_bullpen_listing.py), [test_mlb_client.py](../../tests/unit/ingest/test_mlb_client.py), [test_boxscore_reader.py](../../tests/unit/test_boxscore_reader.py), [test_accuracy_comparison_props.py](../../tests/unit/test_accuracy_comparison_props.py)

The official box, the truth a sportsbook grades a player prop on (owner decision
2026-09-12). It writes one row per player who appeared. A player who did not play has no
row, so the book voids his bet. The same box gives the bullpen listing: the simulator's pen
for the game.

| Function / class | What it does | Called from |
|---|---|---|
| `parse_boxscore` | PURE: a box's `teams` block to one `PlayerGameStats` per player whose batting or pitching block is not empty. The box endpoint and the feed share this shape. | `HistoricalDataLoader._ensure_game_player_stats`, `load_game` (the backfill) |
| `parse_bullpen_listing` | PURE: the arms the box lists per side, as `BullpenListing` rows. The first pitcher reads `started`, the other pitchers `pitched`, the rest of the pen `bullpen`. | the same two |
| `persist` / `persist_sync` | Upsert the player rows through asyncpg, or on a psycopg2 cursor. A conflict on `(game_pk, player_id)` refreshes the row. | `load_game`; `_ensure_game_player_stats` |
| `persist_bullpen` / `persist_bullpen_sync` | The same for the listing, keyed on `(game_pk, pitcher_id)`. | `load_game`; `_ensure_game_player_stats` |
| `side_team_id` | A side's team id from the box, else the caller's id from `raw.games`. | the two parsers; `scripts/probes/backfill_start_positions.py` |
| `BoxscoreIngest` | Fetches `/game/{game_pk}/boxscore` through `_mlb_get`, the network seam the tests stub. It makes four attempts, the wait doubling from one second. | `load_game`; `scripts/probes/backfill_start_positions.py` |
| `run` / `main` | The backfill. `--only-missing` (the default) skips a game that has rows. `--bullpen-only` writes the listing alone, for the games that lack it. Five failures in a row stop the run (`--max-consecutive-failures`; 0 never stops). | the operator |

Run in the app container: `python -m ingest.load_boxscores --seasons 2024`, or
`--game-pks 746437 746438`. The exit code is 0 on success, and 2 with no `--seasons` and
no `--game-pks`. It is 1 when the run stops early, or writes nothing while a game fails.

**Watch out:** the backfill reads only games that `raw.games` holds as Final with a final
score. Inside the game loader, a savepoint guards the bullpen listing: a bad listing never
costs the box rows their commit.

### `ingest/load_bullpen_availability.py`

[Source](../../ingest/load_bullpen_availability.py) · 670 lines · Tests: [test_bullpen_availability.py](../../tests/unit/ingest/test_bullpen_availability.py), [test_mlb_client.py](../../tests/unit/ingest/test_mlb_client.py)

Writes `raw.game_bullpen_availability`: one row per pitcher on each team's roster, used or
not, with whether he could pitch and why. The manager usage profile
(`profiles/manager.py`) reads it. A reliever the manager held back differs
from one who could not pitch. Each arm's rest and recent workload come from `raw.pitches`,
through `compute_bullpen_workload` in this module. The roster and the injured list (IL) come from the
MLB roster endpoint.

| Function / class | What it does | Called from |
|---|---|---|
| `BullpenAvailabilityIngest.decide` | The first rule that holds: off the active roster or on the IL gives `IL`; pitched the day before gives `rest`; `REST_PITCHES_3D` (45) or more pitches in 3 days gives `recent_use`; else the arm is available, `active`. | `build_rows` |
| `BullpenAvailabilityIngest.build_rows` | One row per roster pitcher of each team. An arm who pitched but is missing from the roster read still gets a row (`source = 'workload'`). | `ingest_game` |
| `BullpenAvailabilityIngest.parse_roster` / `_fetch_team_pitchers` | A team's pitchers on a date: the active roster, plus the IL arms from the full-season roster. | `ingest_game` |
| `BullpenAvailabilityIngest._resolve_game_meta` | The game's official date and both teams, from the schedule's played entry. | `ingest_game` |
| `BullpenAvailabilityIngest.ingest` / `ingest_game` | Builds every arm's appearance timeline once, then resolves and upserts each game. A failed game logs a warning, and the run goes on. | `main` |
| `_rest_as_of` | The rest of an arm who did not pitch in the game, from his appearance timeline. | `build_rows` |
| `compute_bullpen_workload` | Each arm's rest and pitches in the last 3 days per game, from `raw.pitches` (no network). | `_load_workload_records` (for `main`) |

Run: `python -m ingest.load_bullpen_availability --seasons 2024 --duckdb /data/baseball_sim.duckdb`.
The load reads the network for every game, so a season is slow.

**Watch out:** `REST_PITCHES_3D` (45) is a guess, not a fit. `AvailabilityRow.is_starter`
is always False. The date is the schedule's `officialDate`, never the UTC `gameDate`. A
western night game's UTC date is the next day, so the roster read would take the wrong day.

---

## The venues: `ingest/venues/`

### `ingest/venues/load_venue_dimensions.py`

[Source](../../ingest/venues/load_venue_dimensions.py) · 152 lines · Tests: [test_park_geometry_builder.py](../../tests/unit/bundle/test_park_geometry_builder.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py)

The MLB API's published fence distances. They are the prior of a park's fence line where
the pool's own balls are too few (the fence certification, SIM-478). The bundle keeps the
last answer in `venue_dimensions.json`.

| Function / class | What it does | Called from |
|---|---|---|
| `fetch_venue_dimensions` | One season's venues with their five published distances, the off-alley points, the roof and the turf. | `write_venue_dimensions` |
| `write_venue_dimensions` | Fetches every season of the window and merges the answers over the stored copy; a venue's latest season wins. A failed season keeps the last copy. | `bundle/park_geometry.py` (`build_park_geometry`) |
| `read_venue_dimensions` | The stored copy, or `{}`. | `write_venue_dimensions` |

**Watch out:** a broken answer can raise `http.client.HTTPException`, which is not an
`OSError`. `write_venue_dimensions` catches it by name.

### `ingest/venues/backfill_venues.py`

[Source](../../ingest/venues/backfill_venues.py) · 332 lines · Tests: [test_venue_id_fallback.py](../../tests/unit/ingest/test_venue_id_fallback.py), [test_etl_flow.py](../../tests/integration/test_etl_flow.py)

`VenueBackfillJob.run` fills a `raw.games.venue_id` that the schedule left empty. The live
service writes a game row as soon as the schedule lists the game. Some early entries have
no venue yet. The job asks the schedule (`hydrate=venue`) for each such game through
`LeagueFeed`. It writes the venue when `raw.venues` holds it, and logs the run in
`raw.pipeline_run_log`. A second run changes nothing.

Run: `python -m ingest.venues.backfill_venues`, or with `--dry-run`.

**Watch out:** `schedule_venue_backfill_job` registers the job on an APScheduler instance,
but nothing in the stack calls it. A venue missing from `raw.venues` gets a warning and no
write; load the venue first.

---

## Baseball Savant: `ingest/savant/`

### `ingest/savant/boards.py`

[Source](../../ingest/savant/boards.py) · 517 lines · Tests: [test_savant_loader.py](../../tests/unit/ingest/test_savant_loader.py), [test_savant_oaa_jump_boards.py](../../tests/unit/ingest/test_savant_oaa_jump_boards.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py), [test_point_in_time_batter.py](../../tests/unit/profiles/test_point_in_time_batter.py), [test_arm_angle_decision.py](../../tests/unit/test_arm_angle_decision.py), [test_outfield_arm_recompute.py](../../tests/unit/test_outfield_arm_recompute.py)

The registry of the twelve Savant leaderboards. Each `SavantBoard` names its address, how
its query names the season, its target table and its columns. Each board writes
`raw.savant_<name>`:

- the batter's swing: `bat_tracking`, `swing_path`, `batting_stance`;
- fielding and throwing: `arm_strength`, `poptime`, `catcher_throwing`,
  `first_base_receiving`, `outs_above_average`, `outfield_jump`;
- the running game: `basestealing`, `pitcher_running_game`;
- both of the last two groups: `baserunning`.

| Function / class | What it does | Called from |
|---|---|---|
| `SavantBoard` | One board: the address, the season style, the table, the columns, the player column, the optional season column, the extra parameters, the split pulls and the date control. | `load_leaderboards.py` |
| `SavantBoard.season_params` | The season part of the query, in the board's style: `year`, `camel`, `snake`, `bracket` or `startyear`. | `load_leaderboards.build_url` |
| `SavantBoard.target_columns` | The table's columns in upsert order: the key, then the board's columns. | `load_leaderboards.upsert_sql` |
| `BOARDS` | The registry, keyed by board name. | `load_leaderboards.py`, `scripts/probes/outfield_arm_recompute.py` |
| `BATTER_BOARDS` / `FIELDING_BOARDS` / `RUNNING_BOARDS` | The groups that `--boards` accepts as `batter`, `fielding` and `running`. | `load_leaderboards.resolve_boards` |
| `PROBE_SEASON` | 1990, a season no board holds. | `load_leaderboards.probe_season_param` |

**Watch out:** Savant answers a season parameter it does not know with HTTP 200 and the
CURRENT season's rows. Each board pulls at the smallest minimum its endpoint honours,
never the qualified default (owner ruling 2026-09-16). The run-value boards ignore `n=0`,
so they use `n=1`. `outs_above_average` is pulled once per position and takes the position
from the query. `baserunning` pins `type=Run`, the runner's view; the fielder's view needs
its own pull. The stance board lives at `/visuals/batting-stance`, not under `/leaderboard/`.

### `ingest/savant/load_leaderboards.py`

[Source](../../ingest/savant/load_leaderboards.py) · 457 lines · Tests: [test_savant_loader.py](../../tests/unit/ingest/test_savant_loader.py), [test_savant_oaa_jump_boards.py](../../tests/unit/ingest/test_savant_oaa_jump_boards.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py), [test_point_in_time_batter.py](../../tests/unit/profiles/test_point_in_time_batter.py)

One loader for every board. It fetches the CSV, checks the season, coerces each row,
drops the players the database does not know, and upserts the rest. `main` writes every
board and season of a run in one transaction.

| Function / class | What it does | Called from |
|---|---|---|
| `build_url` | The address of one pull: the board's extra parameters, its season style, the split value and the optional as-of window. | `load_board_season`, `probe_season_param`; `scripts/probes/outfield_arm_recompute.py` |
| `fetch_csv` | One GET through the MLB client (three minutes per attempt; two retries, 2 s then 4 s apart). It removes a leading byte-order mark. | `load_board_season`, `probe_season_param`, `load_pitch_tracking.load_day`, `SprintSpeedLoader._fetch_csv`, `scripts/probes/outfield_arm_recompute.py` |
| `parse_rows` | The CSV body to rows. An HTML page raises `SavantFetchError`. | `load_board_season`, `probe_season_param`, `load_pitch_tracking.load_day`, the probe |
| `probe_season_param` | Asks the board for 1990. Any row raises `SeasonParamError`. | `main` |
| `check_row_seasons` | On a board with a season column, every row must carry the season asked for. | `load_board_season`, the probe |
| `coerce_row` | One CSV row to one upsert payload; a blank reads None, never 0. An as-of board stamps the cutoff: the as-of date, else the earlier of the season's last day and today. | `load_board_season` |
| `upsert_sql_for` / `upsert_sql` | The one Savant upsert builder: insert with `scraped_at = NOW()`, and on a key conflict overwrite every column outside the key. `upsert_sql` keys a board on the player, the season, the split and the cutoff. | `load_board_season`; `upsert_sql_for` also `load_pitch_tracking.py`, `load_sprint_speed.py` |
| `filter_to_known_players` | Drops the rows whose player is not in `raw.players` (the foreign key). | `load_board_season`, `SprintSpeedLoader` |
| `load_board_season` | Fetches, checks, coerces and upserts one board for one season, one pull per split. | `main` |
| `resolve_boards` | The named boards and groups, each once. An unknown name exits. | `main` |

Run: `python -m ingest.savant.load_leaderboards --boards all --seasons 2023 2024 2025 2026`.
`--dry-run` fetches and counts without writing. `--asof` pulls a season up to a date, on
the boards with a date control only. `--skip-probe` skips the season probe.

**Flags:** `BASEBALL_DB_DSN` (required unless `--dry-run`).

**Watch out:** several boards answer a rejected parameter with the rendered HTML page and
HTTP 200. Parsed as CSV, that page reads as an empty season; `parse_rows` refuses it.

### `ingest/savant/load_pitch_tracking.py`

[Source](../../ingest/savant/load_pitch_tracking.py) · 268 lines · Tests: [test_point_in_time_batter.py](../../tests/unit/profiles/test_point_in_time_batter.py)

The per-pitch tracking measurements from Savant's search export, one day per request,
into `raw.savant_pitch_tracking`. The measurements are bat speed, swing length, attack
angle and direction, swing path tilt, the two intercepts, arm angle and hyper speed.
Per-pitch rows let the batter profile average up to a cutoff date. A backtest then never
reads a season average that holds the future.

| Function / class | What it does | Called from |
|---|---|---|
| `build_url` | The export address for one day's regular-season pitches. | `load_day` |
| `load_day` / `check_day` | Loads one day. A day at the 25,000-row cap raises `TruncatedDayError`; a row from another date raises too. | `main` |
| `season_days` / `loaded_days` | The days of a season that have games in `raw.pitches`, else the calendar window; the days already loaded, for `--resume`. | `main` |

Run: `python -m ingest.savant.load_pitch_tracking --seasons 2024`, with `--resume` to skip
the loaded days, or `--start` and `--end` for a window.

**Watch out:** the export stops at 25,000 rows and says nothing, so the loader asks for one
day at a time. The bat-tracking columns start in 2024: a 2023 day brings only `arm_angle`
and `hyper_speed`. `main` commits every ten days, and it commits the fetched days on an error.

### `ingest/savant/load_sprint_speed.py`

[Source](../../ingest/savant/load_sprint_speed.py) · 194 lines · Tests: [test_savant_loader.py](../../tests/unit/ingest/test_savant_loader.py)

`SprintSpeedLoader` fetches Savant's sprint-speed leaderboard per season and upserts
`raw.sprint_speed`. Each row holds one player-season: the speed, the runs, the bolts and
the home-to-first time. `refresh_seasons` loads every season in one transaction. The
loader reads through `fetch_csv`, and raises `ValueError` on an empty body or a missing
column. It drops the players missing from `raw.players`.

Run: `python -m ingest.savant.load_sprint_speed --seasons 2023 2024 2025`.

**Watch out:** `min=0` asks for every runner, but Savant keeps its own floor of 5 runs. The
table keeps `competitive_runs`, so a reader sets its own minimum.

### `ingest/savant/page_json.py`

[Source](../../ingest/savant/page_json.py) · 46 lines · Tests: [test_etl_hardening.py](../../tests/unit/ingest/test_etl_hardening.py)

`_parse_savant_embedded_json` reads the JSON literal that a Savant HTML page embeds
(`var data = ...`). It raises `SavantScrapeError` when a marker is missing or the slice is
not JSON. The game loader's venue step reads the fence dimensions with it.

---

## The Statcast code sets

### `ingest/statcast_events.py`

[Source](../../ingest/statcast_events.py) · 474 lines · Tests: [test_out_label.py](../../tests/unit/ingest/test_out_label.py), [test_pitch_result_vocabulary.py](../../tests/unit/test_pitch_result_vocabulary.py), [test_profile_code_sets.py](../../tests/unit/profiles/test_profile_code_sets.py), [test_foul_tip_strike_three.py](../../tests/unit/test_foul_tip_strike_three.py), [test_steal_labels.py](../../tests/unit/test_steal_labels.py), [test_transition_pool.py](../../tests/unit/test_transition_pool.py)

The one place that decides what a `raw.pitches` row means. Every out label derives from
`events`, the plate-appearance result on its last pitch, never from `outs_on_pitch`. The
module also holds the pitch-result code sets over the `type` column. It imports nothing,
so the API reads it without the profile computor's imports.

| Function / class | What it does | Called from |
|---|---|---|
| `PLAY_OUTS_BY_EVENT` / `FIELDING_OUT_EVENTS` / `play_outs` / `sql_fielding_out` | How many outs a play recorded (question 1). `play_outs` adds the hidden runner out when the type is `X`. | `play_outs`: `profiles/fielder_plays.py`. `sql_fielding_out`: `profiles/sql.py` |
| `BATTER_RETIRED_EVENTS` / `batter_retired` / `sql_batter_retired` | Whether the batter was retired (question 2). The batter reaches on `force_out` and on both fielder's-choice events. | `batter_retired`: `fielder_plays.py`. `sql_batter_retired`: `profiles/sql.py` |
| `sql_play_outs` / `sql_cs_out` / `sql_outs_recorded` | The outs on a row: the event's outs and a hidden runner out; then a caught stealing in the middle of a plate appearance. `sql_outs_recorded` is the innings-pitched building block. | `sql_play_outs`: `pools/build_batted_ball_pool.py`. `sql_outs_recorded`: `profiles/sql.py`, `scripts/boxscore_audit.py` |
| `sql_runner_dest` / `sql_batter_dest` | Each runner's and the batter's base after the play: 0 is out, 4 is scored. | `pools/build_batted_ball_pool.py` |
| `sql_steal_attempt` / `sql_steal_success` | The one steal label every site reuses. | `profiles/baserunner.py`, `catcher.py`, `manager.py`; `pools/build_steal_pool.py`; `sql_steal_success` also `scripts/boxscore_audit.py` |
| `BALL_TYPES`, `CALLED_STRIKE_TYPES`, `WHIFF_TYPES`, `FOUL_TYPES`, `STRIKE_THREE_FOUL_TYPES`, `NON_SWING_TYPES`, `IN_PLAY_TYPES`, `FOUL_TIP_TYPES`, `SAVANT_WHIFF_TYPES` | The pitch-result code sets. `STRIKE_THREE_FOUL_TYPES` marks a two-strike foul tip or foul bunt as strike three. | `profiles/sql.py`; `STRIKE_THREE_FOUL_TYPES` also `scripts/rebuild_pitch_pool.py`, `scripts/pitch_pool_rebuild/checks.py` |
| `sql_in_play` / `sql_swing` / `sql_savant_whiff` / `sql_called_strike` / `sql_csw` / `sql_list` | Row filters built from those sets. The model's whiff counts a foul tip as contact; Savant's whiff, which the Data Lab shows, counts it as a whiff. | `api/routes/analytics.py`, `api/routes/ai_assistant.py`; `sql_in_play` and `sql_list` also `profiles/sql.py`; `sql_list` also `pools/check_pool_labels.py` |

**Watch out:** [test_out_label.py](../../tests/unit/ingest/test_out_label.py) fails when
any profile site reads `outs_on_pitch`. Filter on `type`, never on `description`, which is
display text. A new event goes into `PLAY_OUTS_BY_EVENT` and into the test's
`MEASURED_VOCABULARY` together.
