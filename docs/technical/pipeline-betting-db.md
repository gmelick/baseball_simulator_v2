# Data Pipeline, Betting, and Database

ETL and live ingestion, the nightly profile/artifact builders, the CLV and betting-signal engines, and the migration system.

*20 files documented — use the page outline (right sidebar) to jump to one.*

### `pipeline/statcast_events.py`

Canonical vocabulary and SQL/Python helper functions that translate raw.pitches' `events` column (and the `type`/steal columns) into out-counts, batter-retired booleans, runner/batter post-play destinations, and steal attempt/success labels. It is the single source of truth so this logic is not redefined differently in different SQL sites.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PLAY_OUTS_BY_EVENT / FIELDING_OUT_EVENTS / BATTER_RETIRED_EVENTS` | Module-level dict/frozensets: the full events vocabulary mapped to outs-recorded, and the separate set of events where the batter did not reach base. | — | — |
| `sql_play_outs(q)` | SQL CASE expression: outs recorded on an in-play row (the events term plus the 'hidden runner out' term for type='X' rows events misses). | `pipeline/batch/player_profile_computor.py` | `sql_event_outs`<br>`sql_hidden_runner_out` |
| `sql_outs_recorded(q)` | SQL CASE expression: total outs on a raw.pitches row (play outs plus a mid-PA caught-stealing out) — the innings-pitched building block. | `pipeline/batch/player_profile_computor.py` | `sql_play_outs`<br>`sql_cs_out` |
| `sql_cs_out(q)` | SQL CASE expression: a mid-plate-appearance caught-stealing out, structurally excluding rows whose `events` already counts a steal/pickoff out (no double-count). | `pipeline/batch/player_profile_computor.py` | — |
| `sql_runner_dest(base, q) / sql_batter_dest(q, batter_expr)` | SQL CASE expressions giving each pre-play runner's / the batter's post-play destination (0=out, 1-3=base, 4=scored), used to build the SIM-510 transition-encoded pools. | `pipeline/batch/player_profile_computor.py (_build_advancement_opportunity_pool / outcome pool)` | — |
| `sql_batter_retired(q) / batter_retired(event)` | SQL and Python versions of 'was the batter retired' (question 2, a different event set than FIELDING_OUT_EVENTS). | `pipeline/batch/player_profile_computor.py` | — |
| `sql_steal_attempt(base, q) / sql_steal_success(base, q)` | NULL-safe SQL booleans combining the sb_attempt_*/sb_success_* columns with events-based CS/SB labels — the one definition every steal-labeling site must reuse (SIM-506). | `pipeline/batch/player_profile_computor.py (steal/advancement pool builders)` | — |
| `play_outs(event, type_code) / sql_in_play(q)` | Python-side per-row out-count helper (for pandas paths) and the in-play row filter (type IN ('X','D','E')). | `pipeline/batch/player_profile_computor.py` | — |

**Used by:** `pipeline/batch/player_profile_computor.py`

> **Notes for anyone changing this file:** This is the ONLY place that should decide 'how many outs did this play record' / 'was the batter retired' / 'was a steal attempted' — a dedicated unit test (tests/unit/test_sim501a_out_label.py) fails if any other site reads outs_on_pitch directly instead of these events-based helpers. The two questions use DIFFERENT event sets on purpose (FIELDING_OUT_EVENTS includes force_out/fielders_choice_out; BATTER_RETIRED_EVENTS excludes them because the batter reached) — do not merge them. simulation/sim_loop.py intentionally carries its own, different `_OUT_EVENTS` list for a different question ('did the batter reach for the loop's own purposes'); that divergence is documented sim-scoped work, not a bug in this file.

---

### `pipeline/etl/coercion.py`

Shared 'empty/missing -> None' type-coercion helpers (to_float/to_int/to_bool/to_str) so both ETL loaders parse external feed values (empty strings, None, pandas NaN) identically instead of duplicating slightly-different logic (SIM-437 consolidation).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `to_float(v) / to_int(v)` | Coerce a feed value to float/int, treating '', None and NaN as missing (returns None). | `pipeline/etl/etl_historical_loader.py`<br>`pipeline/etl/etl_sprint_speed_loader.py` | — |
| `to_bool(v)` | Coerce common feed truthy strings ('true'/'1'/'yes') and other types to bool. | `pipeline/etl/etl_historical_loader.py` | — |
| `to_str(v)` | Coerce a value to a stripped string, or None if empty/whitespace-only. | `pipeline/etl/etl_historical_loader.py` | — |

**Used by:** `pipeline/etl/etl_historical_loader.py`, `pipeline/etl/etl_sprint_speed_loader.py`

> **Notes for anyone changing this file:** Deliberately does NOT absorb the differently-behaved `_opt_int`/`_opt_float` helpers in pipeline/live/bullpen_availability_ingest.py and pipeline/bettingpros_odds_provider.py — those do not treat '' as missing. Do not 'consolidate' those in a future cleanup; the module docstring calls this out explicitly as a behavior change, not a refactor.

---

### `pipeline/etl/venue_backfill_job.py`

SIM-086 idempotent job that finds raw.games rows with a NULL venue_id (early schedule entries that predate the MLB API publishing a venue) and fills them once the venue becomes known, guarding against an FK violation against raw.venues.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `VenueBackfillJob.run()` | Fetches every NULL-venue game, looks up its venue via the schedule API, updates the row, and writes a raw.pipeline_run_log summary. | `module __main__ (_main)`<br>`tests/unit/test_data_engineer_sim085_to_091.py` | `_fetch_null_venue_games`<br>`_fetch_venue_id`<br>`_update_venue`<br>`_start_log_row`<br>`_finish_log_row` |
| `VenueBackfillJob._fetch_venue_id(session, game_pk, game_date)` | GETs /api/v1/schedule?gamePk=...&hydrate=venue and returns the venue id if present. | `VenueBackfillJob.run` | — |
| `VenueBackfillJob._update_venue(game_pk, venue_id)` | Pre-checks raw.venues for the venue id (to avoid a raw FK exception) then UPDATEs raw.games.venue_id. | `VenueBackfillJob.run` | — |
| `schedule_venue_backfill_job(dsn, scheduler, interval_hours=6)` | Registers this job on an APScheduler instance every 6 hours by default. | — | — |

**Depends on:** `aiohttp`, `asyncpg`, `MLB Stats API (schedule endpoint)`

**Environment flags read here:** `BASEBALL_DB_DSN (CLI --dsn default)`

> **Notes for anyone changing this file:** GOTCHA: schedule_venue_backfill_job is a complete, tested scheduler hook, but grep across api/ finds nothing that calls it — api/main.py's lifespan never registers it. In the current codebase this job only runs if invoked manually (`python -m pipeline.etl.venue_backfill_job`) or wired into an external cron; it is not part of the live app's automatic behavior. Idempotent, so safe to run ad hoc.

---

### `pipeline/etl/opening_line_job.py`

SIM-138 nightly job that captures OPENING betting lines (game-level + pitcher props for announced starters) for every game in the next 7 days, via the SIM-370 odds-provider seam, because opening lines can never be recovered retroactively and CLV needs them as the entry-line anchor.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `OpeningLineJob.run()` | For every game in the lookahead window, checks for an existing opening-line row and, if absent, fetches + stores one (plus pitcher props once a starter is announced); writes a raw.pipeline_run_log summary. | `module __main__`<br>`tests/integration/test_etl_flow.py` | `_fetch_upcoming_games`<br>`_has_opening_line`<br>`_fetch_current_odds`<br>`_store_opening_line`<br>`_capture_prop_opening_lines` |
| `OpeningLineJob._fetch_current_odds(game_pk) / _capture_prop_opening_lines(game_pk, home_pitcher_id, away_pitcher_id)` | Delegate all odds/prop generation to pipeline.odds_provider.get_odds_provider() (mock by default) rather than hard-coding a source. The pitcher props captured for an announced starter are every market in PITCHER_PROP_TYPES (five since SIM-421: strikeouts, earned runs, walks, outs recorded, hits allowed). | — | `pipeline.odds_provider.get_odds_provider` |
| `PITCHER_PROP_TYPES / BATTER_PROP_TYPES` | Module constants; since SIM-421 they are aliases of `pipeline.odds_provider.PITCHER_PROP_STATS` / `BATTER_PROP_STATS` (the single source of the 15-market vocabulary), kept under their historical names. | — | `pipeline.odds_provider` |
| `schedule_opening_line_job(dsn, scheduler, hour=8, minute=0, timezone_str='America/New_York')` | Registers a daily 08:00 ET APScheduler cron job. | — | — |

**Depends on:** `pipeline/odds_provider.py (get_odds_provider, PITCHER_PROP_STATS, BATTER_PROP_STATS)`, `MLB Stats API (schedule endpoint)`, `asyncpg`

**Environment flags read here:** `ODDS_PROVIDER (via get_odds_provider)`, `BASEBALL_DB_DSN (CLI default)`

> **Notes for anyone changing this file:** Same gap as venue_backfill_job.py: schedule_opening_line_job is never called from api/main.py or any other production wiring found in the repo — it must be run manually or via an external cron entry today. Its own docstring stresses this job is time-critical: a missed day of opening-line capture is a permanent, unrecoverable data loss for the CLV pipeline.

---

### `pipeline/etl/etl_sprint_speed_loader.py`

Fetches Baseball Savant's Sprint Speed leaderboard CSV per season and upserts it into raw.sprint_speed (one row per player per season), FK-guarding against players not yet present in raw.players.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `SprintSpeedLoader.refresh_seasons(seasons)` | Public entry point: fetches + parses + upserts each requested season inside one connection/transaction. | `module __main__ CLI only` | `_refresh_one_season` |
| `SprintSpeedLoader._fetch_csv(season)` | GETs the Savant sprint_speed CSV endpoint with a custom User-Agent (Savant blocks the default urllib one) and retry/backoff. | `_refresh_one_season` | — |
| `SprintSpeedLoader._parse_csv(csv_text, season) / _filter_to_known_players(conn, rows)` | Parses the CSV into upsert-ready dicts and drops rows whose player_id is not yet in raw.players. | `_refresh_one_season` | `pipeline.etl.coercion.to_int`<br>`pipeline.etl.coercion.to_float` |

**Depends on:** `pipeline/etl/coercion.py`, `Baseball Savant leaderboard endpoint`, `psycopg2`

**Environment flags read here:** `BASEBALL_DB_DSN (CLI default)`

> **Notes for anyone changing this file:** No other module in the codebase imports SprintSpeedLoader — it is a standalone CLI script, and scripts/nightly_ingest.sh does NOT call it. CLAUDE.md's SIM-523 part G notes sprint speed was added 'live' into the profiles for the redesign; if sprint-speed columns look empty/stale, check whether this loader has actually been run for the seasons in question — nothing runs it automatically.

---

### `pipeline/etl/savant_loader.py` + `pipeline/etl/savant_boards.py`

The Baseball Savant leaderboard loader (SIM-528) and its registry: one `SavantBoard` per CSV endpoint (the URL, how the season is spelled, the target `raw.savant_*` table, the columns), pulled per season and upserted with an FK guard against players not yet in `raw.players`. The loader proves the season parameter was honoured before it writes (it asks every board for 1990 and expects zero rows).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BOARDS` | The registry: twelve boards — the batter swing/stance family (`bat_tracking`, `swing_path`, `batting_stance`; SIM-529), the fielding boards (`arm_strength`, `baserunning`, `poptime`, `catcher_throwing`, `first_base_receiving`; SIM-530; `outs_above_average` and `outfield_jump`; SIM-532, 2026-09-17) and the running-game boards (`basestealing`, `pitcher_running_game`; SIM-531). `BATTER_BOARDS` / `FIELDING_BOARDS` / `RUNNING_BOARDS` are the group aliases `--boards` accepts. SIM-532's two boards feed the fielder profile's six-column tail (`derived.fielder_season_metrics`: `savant_oaa`, `savant_oaa_per_100`, the three jump parts, `jump_plays`). `outs_above_average` → `raw.savant_outs_above_average`, keyed (player, season, POSITION): the loader pulls it SEVEN times a season, once per position (`split_param="pos"`, `pos=3..9`), and writes the position from the QUERY — the board's `primary_pos_formatted` is the player's primary position, not the pulled one. Its traps: the season parameter is the fifth style, `startyear` (`startYear=…&endYear=…`); its `year` column is BLANK on every row, so the per-row season check cannot run (`season_column=None`) and the 1990 probe (0 rows) is the only guard; the hand-split columns (`oaa_vs_lhh`, `oaa_vs_rhh`) are stored and read by nothing (they do not repeat year to year within a position, except weakly at shortstop). `outfield_jump` → `raw.savant_outfield_jump`, keyed (player, season): reaction, burst, route and jump in feet against the league, the feet covered, the plays scored (the jump's confidence basis). Both are pulled at `min=0`. | `resolve_boards`, `scripts`, the profile builders (by table) | — |
| `build_url / load_board_season` | Builds the CSV URL (the board's `extra` parameters + the season pair + `csv=true`) and fetches, parses, coerces and upserts one board-season. | `main` | `coercion.to_int / to_float / to_str` |
| `coerce_row` | One CSV row to one upsert payload: `_INT_TARGETS` names the whole-number columns (SIM-531 added `n_init`, `n_sb`, `n_pk` and `n_bk`; `n_cs` was already there from the catcher-throwing board); every other numeric column is a float; a blank is None, never 0.0. | `load_board_season` | — |

**Depends on:** `pipeline/etl/coercion.py`, `psycopg2`, the Savant CSV endpoints

**Environment flags read here:** `BASEBALL_DB_DSN (CLI default)`

> **Notes for anyone changing this file:** Owner ruling 2026-09-16 — every board is pulled at the SMALLEST minimum its endpoint honours, never at the qualified default (the registry docstring carries the per-board table): the bat-tracking family and the stance at `minSwings=0` (215 → 650 batters), `basestealing` / `pitcher_running_game` / `baserunning` at `n=1` (432 → 638 runners, 496 → 849 pitchers, 305 → 623 extra-base runners in 2024), `catcher_throwing` at `n=1` (66 → 94), `first_base_receiving` at `min=1` (42 → 152), `poptime` at `min2b=0 & min3b=0`; `arm_strength` honours no minimum (Savant's own floor is 50 throws). Two traps: `n=0` is IGNORED by the run-value boards (it serves the qualified default; the smallest honoured value is `n=1`, and a test forbids `n=0`), and a row count near the qualified figure in the log means a minimum crept back in. The boards carry no per-row date, so the profile builders season-shift their joins under a backtest cutoff (the SIM-537 rule). The `baserunning` board is the RUNNER's own side — reading it as a fielder's arm block is the SIM-550 defect.

---

### `pipeline/etl/play_events.py`

SIM-502 pure-function module that extracts non-pitch play events (pickoff throws/outs, stepoffs, balks, intentional walks) from one MLB `play` dict — events raw.pitches cannot represent because they produce no pitch row — and returns row dicts destined for raw.play_events.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `extract_play_events(play, *, game_pk, game_date, season, outs_before_play, runners_state_before, home_score_before, away_score_before)` | The sole public entry point: walks one play's events, resolves the actual thrower (handling a mid-PA pitching change), joins pickoff throws to their outcome events by playIndex, and synthesizes an intentional-walk row with no pitch events at all. | `pipeline/etl/etl_historical_loader.py (_fetch_game_pitches, per play)` | `_pickoff_runners`<br>`_base_of`<br>`_runners_state`<br>`_credits`<br>`_action_pickoff_kind` |

**Used by:** `pipeline/etl/etl_historical_loader.py`

> **Notes for anyone changing this file:** Extremely fiddly, well-documented MLB feed quirks that must not be 'simplified' without re-reading the inline comments: (1) a pickoff is written as TWO playEvents — a throw (type='pickoff', no details) and, if it resolves, a separate action event carrying the real outcome, joined only by playIndex; (2) home plate's outBase token is the string '4B', not 'home'; (3) an intentional walk carries NO playEvents at all, so its row uses a sentinel play_index=-1 built from `play.result`; (4) `runners_state_before` and the pre-play score MUST be threaded in by the caller (an extra-innings ghost runner or a home run 4 pitches later can silently poison a naively-derived value). This module is pure (no DB/network) specifically so tests can pin these payload shapes without a live stack.

---

### `pipeline/etl/boxscore_ingest.py`

The official per-player box score as the prop ground truth (SIM-545). A sportsbook grades a player prop against the official box score; the platform used to derive its "actuals" from raw.pitches event labels, which cover only H/HR/TB and K/BB and miss what lives in no pitch row (an intentional walk, a pickoff out). This module copies the MLB box score verbatim into raw.game_player_stats (Alembic 0023) — one row per (game_pk, player_id) for every player who APPEARED — so every prop the market offers has the same actual the book uses. A player who did not play gets NO row (the book voids his bet). Stdlib-only at import time; the parser is unit-tested on a captured payload with no live call and no DB.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `parse_boxscore(teams, *, game_pk, season, game_date, home_team_id=None, away_team_id=None)` | PURE: turns a box score's `teams` dict (the `/api/v1/game/{pk}/boxscore` response or the live feed's `liveData.boxscore` — the same shape) into one PlayerGameStats per player whose `stats.batting` OR `stats.pitching` dict is non-empty. `played_bat` / `played_pitch` read `gamesPlayed >= 1` per block (a two-way player keeps both); `batting_order` is MLB `battingOrder // 100`; `team_id` comes from the payload with the caller's raw.games ids as the fallback. | `pipeline/etl/etl_historical_loader.py (_ensure_game_player_stats)`<br>`scripts/load_official_boxscores.py`<br>`BoxscoreIngest.fetch_rows` | `BATTING_FIELDS`<br>`PITCHING_FIELDS` |
| `PlayerGameStats` | Frozen dataclass mirroring raw.game_player_stats; its field order IS the INSERT column order (`COLUMNS`), so the two must move together. | `parse_boxscore` | — |
| `BATTING_FIELDS / PITCHING_FIELDS` | The MLB field -> column maps (plateAppearances->pa, atBats->ab, ..., totalBases->tb; outs->p_outs, ..., battersFaced->p_batters_faced). `gamesStarted >= 1` becomes the `p_started` boolean apart. | `parse_boxscore` | — |
| `persist(conn, rows) (async, asyncpg) / persist_sync(cur, rows) (psycopg2)` | The two drivers over ONE upsert (`UPSERT_SQL_ASYNCPG` / `UPSERT_SQL_PSYCOPG2`): `INSERT ... ON CONFLICT (game_pk, player_id) DO UPDATE`, so a re-run refreshes a game's rows in place. `persist_sync` runs on the caller's cursor and the caller commits. | `scripts/load_official_boxscores.py (persist)`<br>`pipeline/etl/etl_historical_loader.py (persist_sync)` | — |
| `parse_bullpen_listing(teams, *, game_pk, home_team_id, away_team_id)` / `BullpenListing` / `persist_bullpen` / `persist_bullpen_sync` | SIM-427: the per-game BULLPEN LISTING from the same box `teams` block — one `BullpenListing(game_pk, pitcher_id, team_id, side, listed)` per pitcher the box names: `pitchers[0]` is `started`, the rest of `pitchers` `pitched`, the `bullpen` ids `bullpen` (a pitcher in both lists keeps the `pitchers` reading). Upserted into `raw.game_bullpen` (Alembic 0025; PRIMARY KEY game_pk, pitcher_id) by the two drivers (`BULLPEN_UPSERT_SQL_ASYNCPG` / `_PSYCOPG2`). This listing is the SIM-427 real pen's source: `pipeline/bullpen_usage.py` turns it into a pen. | `pipeline/etl/etl_historical_loader.py (after the box rows, best effort under a SAVEPOINT)`<br>`scripts/load_official_boxscores.py (--bullpen-only)` | — |
| `BoxscoreIngest` | The fetch + persist wrapper: `fetch_boxscore(game_pk)` -> `fetch_rows(...)` -> `persist(...)`. `_mlb_get` is the ONLY network seam (stdlib urllib; a bounded doubling retry on 429 / 5xx / timeouts, `max_attempts` 4 from `backoff` 1 s; any other 4xx is not retried), stubbed in unit tests. A numeric `Retry-After` header on a retried status replaces that attempt's wait, capped at `_MAX_RETRY_AFTER_S` (60 s); a header that is not a number (an HTTP date) falls back to the doubling wait (`_retry_after_seconds`). | `scripts/load_official_boxscores.py` | `parse_boxscore`<br>`persist`<br>`_retry_after_seconds` |

**Depends on:** `MLB Stats API (/api/v1/game/{game_pk}/boxscore)`, `asyncpg or psycopg2 (whichever driver the caller passes)`

**Used by:** `pipeline/etl/etl_historical_loader.py (the non-fatal per-game hook)`, `scripts/load_official_boxscores.py (the backfill CLI)`, `simulation/prop_validation.py reads the rows this module writes (real_props_from_boxscore_rows)`

**Environment flags read here:** `none (the callers pass the DSN)`

> **Notes for anyone changing this file:** (1) raw.game_player_stats.player_id has NO foreign key to raw.players ON PURPOSE (the migration's table comment says why): a box score can list a player the pitch sweep never wrote, and a missing ground-truth row silently voids a graded prop, which is worse than a dangling id — do not "fix" this by adding the FK. (2) The absence of a row is meaningful ("did not play"); never emit an all-zero row for a bench player, or every reader's DNP-skip rule breaks. (3) The historical loader's hook and the backfill CLI share the column list and the upsert through this module — add a column here (dataclass + BATTING_FIELDS/PITCHING_FIELDS + migration) and both writers pick it up.

---

### `pipeline/bullpen_usage.py`

SIM-427: the pen's FACTS from the official box score and the MLB box's per-game bullpen listing, for the pitching-change draw and the reliever draw. Two consumers share these queries so the pool rows and the live candidates carry the SAME definitions: the artifact builder (in bulk, every appearance in the pool window — the incoming arm's rest as of the game he entered, the game's two managers, every pitcher's throwing hand) and the lineup resolver (per game — the arms the box lists as available and their rest). The facts: `days_rest` (days since the arm's last appearance, capped at REST_CAP_DAYS = 5; nothing in the trailing LOOKBACK_DAYS = 30 reads as the cap; 1 = pitched yesterday — 0 never occurs because the windows are strictly before the game date), `pitched_2d` (an appearance on either of the two preceding days), `pitches_3d` (his pitches over the three preceding days), and the ROLE rule (an arm that STARTED any of the team's last ROTATION_GAMES = 5 games is a rotation starter and leaves the pen — the box's `bullpen` list carries the rotation's off-day starters). Every query has two spellings (asyncpg `$n`, psycopg2 `%s`) built by one function each.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `usage_for_appearances_sql(driver)` / `usage_for_candidates_sql(driver)` | Bulk (every appearance in the seasons) and per-game (a list of arms as of a date) rest / pitched-2d / pitches-3d from `raw.game_player_stats` (`played_pitch` rows strictly before the game date). | `fetch_game_context_sync`<br>`fetch_usage_for_candidates` | `raw.game_player_stats (Alembic 0023)` |
| `rotation_starters_sql(driver)` / `game_listing_sql(driver)` / `game_managers_sql(driver)` / `pitcher_throws_sql(driver)` | The team's rotation as of a date (the distinct starters of its last five games), the box listing for one game and team (`raw.game_bullpen`), the two managers of every game in the seasons (`raw.games`), every pitcher's throwing hand (`raw.players`). | `fetch_pen_for_game`<br>`fetch_game_context_sync` | — |
| `pen_from_listing(listing, rotation, exclude)` | PURE: the pen = the arms listed `bullpen` or `pitched` minus the rotation minus `exclude` (today's starter); the `pitched` arms first in listing order (the arms the manager used), then the rest — the stable order the positional pick reads. | `fetch_pen_for_game` | — |
| `fetch_pen_for_game(conn, *, game_pk, team_id, game_date, exclude)` (async) | The resolver's read: `(pen, {pitcher_id: ArmUsage})`, or None when the box lists nothing for the game (the caller falls back to the synthetic pen and says so). | `simulation/lineup_resolver._attach_bullpens` | `fetch_usage_for_candidates` |
| `fetch_game_context_sync(dsn, seasons)` | The builder's one Postgres read (psycopg2): `{game_pk: (home_manager_id, away_manager_id)}`, `{(game_pk, pitcher_id): (days_rest, pitched_2d, pitches_3d)}` and `{pitcher_id: throws code}` (THROWS_CODE: L 1, R 2, unknown 0). | `pipeline/batch/engine_artifacts.build_pitching_change_pool` | — |
| `ArmUsage` | One arm's recent usage as of a game date; `as_tuple()` is the `(days_rest, pitched_2d, pitches_3d)` triple the game state carries in `pitcher_recent_usage`. | `fetch_usage_for_candidates` | — |

**Depends on:** `raw.game_player_stats (SIM-545)`, `raw.game_bullpen (SIM-427, Alembic 0025)`, `raw.games`, `raw.players`

**Used by:** `simulation/lineup_resolver.py (the real pen per game)`, `pipeline/batch/engine_artifacts.py (the change pool's manager / rest / throws columns)`, `scripts/sim427_manager_probe.py (indirectly, through the resolver)`

**Environment flags read here:** `none (the callers pass the connection / DSN; the resolver's SIM_BULLPEN_SOURCE switch lives in simulation/lineup_resolver.py)`

> **Notes for anyone changing this file:** (1) Two games on one date (a doubleheader) do not see each other's usage — the windows are strictly before the game DATE, a known small gap; do not "fix" it by including the date without also ordering by game time on both sides. (2) The pool side and the live side MUST keep the same definitions (the fit compares one against the other); change a window constant here and both move together — that is why there is one module.

---

### `pipeline/etl/etl_historical_loader.py`

The main historical/nightly ETL: fetches MLB Stats API feed/live JSON per game, upserts every FK prerequisite (venues, teams, players, managers, the game row, lineups), validates and batch-inserts every pitch into PostgreSQL raw.pitches (plus non-pitch events via play_events.py into raw.play_events), and offers a corrective 'reload' path that can overwrite already-ingested games.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `HistoricalDataLoader.load_game(game_pk, season) / reload_game(game_pk, season, allow_shrink=False)` | Public per-game entry points. load_game's INSERT uses ON CONFLICT DO NOTHING (cannot repair bad data); reload_game DELETEs and re-inserts the game's rows in one transaction — the only supported way to correct already-ingested pitch data. | `scripts/reload_games.py`<br>`scripts/resumable_sweep.py`<br>`scripts/native_sweep_probe.py`<br>`tests/unit/test_sim440_reload_game.py` | `_fetch_game_pitches`<br>`_ensure_prerequisites`<br>`_process_and_insert` |
| `HistoricalDataLoader.refresh_seasons(start_year, end_year, reload=False)` | Full historical backfill / nightly incremental updater: walks the MLB schedule for each season, skips non-Final games, and routes each game through _dispatch_game so one bad feed cannot abort a multi-thousand-game run. | `scripts/nightly_ingest.sh (step 1/3, current-year incremental)`<br>`module __main__` | `_dispatch_game` |
| `HistoricalDataLoader.load_date_range(start_date, end_date, reload=False)` | Incremental loader over an arbitrary date window, chunked to <=365 days to work around the MLB schedule API's silent 365-day clamp. | — | `_dispatch_game (via internal chunk loop)` |
| `HistoricalDataLoader._dispatch_game(game_pk, season, ..., reload, summary)` | Routes one game to load/reload, tallies the outcome, and re-raises only after ETL_CONSECUTIVE_FAILURE_LIMIT (default 5) consecutive per-game failures — the distinction between 'one bad feed' (survivable) and 'the API is down' (must fail loudly). | `refresh_seasons`<br>`load_date_range` | — |
| `HistoricalDataLoader._ensure_prerequisites(game_pk, game_dict)` | Upserts every FK parent of raw.pitches in dependency order (venues -> teams -> players -> managers -> game -> lineups), each step doing one batch existence check before hitting the MLB API for missing ids only; then writes the official box score (SIM-545, non-fatal). | `_load_game` | `_ensure_venue`<br>`_ensure_teams`<br>`_ensure_players`<br>`_ensure_managers`<br>`_ensure_game`<br>`_ensure_game_lineups`<br>`_ensure_game_player_stats` |
| `HistoricalDataLoader._ensure_game_player_stats(game_pk, season, game_date, game_dict, home_team_id, away_team_id)` | SIM-545: parses the feed's in-hand `liveData.boxscore.teams` block through `pipeline.etl.boxscore_ingest.parse_boxscore` (no extra HTTP call) and upserts one `raw.game_player_stats` row per player who appeared, on the loader's own pooled psycopg2 connection. Probes `to_regclass('raw.game_player_stats')` ONCE per run (a tri-state cache like the play_events one) and warns instead of writing when Alembic 0023 is not applied. Non-fatal by design: any failure logs a warning and never fails the game load. Since SIM-427 it also writes the game's bullpen listing (`parse_bullpen_listing` -> `persist_bullpen_sync`) after the box rows, under `SAVEPOINT sim427_bullpen`, so a missing `raw.game_bullpen` table (probed once per run, `_game_bullpen_table_exists`) or a bad row never loses the box rows. | `_ensure_prerequisites` | `pipeline.etl.boxscore_ingest.parse_boxscore`<br>`pipeline.etl.boxscore_ingest.persist_sync`<br>`pipeline.etl.boxscore_ingest.parse_bullpen_listing / persist_bullpen_sync` |
| `HistoricalDataLoader._batch_insert(rows, replace_game_pk=None, allow_shrink=False, error_rows=None, ...)` | The single transaction for one game's write: optional DELETE (reload), raw.play_events write, raw.pitches insert, the raw.etl_errors ledger, and the raw.etl_game_ingest outcome all commit or roll back together; measures rows ACTUALLY written via COUNT rather than trusting len(rows), because ON CONFLICT DO NOTHING can silently write zero. | `_process_and_insert` | `_write_play_events`<br>`_write_error_ledger`<br>`_write_ingest_outcome` |
| `_build_row_dict(raw, game_pk, season, game_date) / _validate_row(row)` | Module-level: rename+coerce one pitch's raw feed fields to schema column names, then two-tier validate (hard error -> row skipped + ledger entry; warning -> inserted with data_quality_flag=TRUE). | `_process_and_insert` | `pipeline.etl.coercion.to_*` |
| `_fetch_game_pitches(game_pk, batter_hand_cache)` | Module-level: the entire feed/live parse — managers, per-pitch fields, substitution flags, mid-PA SB/WP detection, extra-innings ghost-runner placement, and non-pitch event extraction via play_events.extract_play_events. | `_load_game` | `_connect`<br>`_resolve_venue`<br>`pipeline.etl.play_events.extract_play_events` |
| `_resolve_venue(game_pk, game_data)` | Resolves a game's true venue, raising MissingVenueError rather than silently falling back to teams.home.venue (which would misattribute a neutral-site game to the home team's regular park and corrupt that park's park factor). | `_fetch_game_pitches`<br>`_ensure_prerequisites (via _resolve_venue_id)` | — |
| `_http_get / _fetch_once / _urllib_fetch / _requests_fetch` | The HTTP transport seam. Default transport is stdlib urllib (ETL_HTTP_TRANSPORT env); `requests` is an opt-in alternate kept only for A/B comparison. | `_connect` | — |

**Depends on:** `pipeline/etl/coercion.py`, `pipeline/etl/play_events.py (extract_play_events)`, `pipeline/etl/boxscore_ingest.py (parse_boxscore, persist_sync — SIM-545)`, `MLB Stats API (feed/live, coaches, schedule)`, `psycopg2 (sync)`

**Used by:** `scripts/nightly_ingest.sh`, `scripts/reload_games.py`, `scripts/resumable_sweep.py`, `scripts/native_sweep_probe.py`

**Environment flags read here:** `ETL_HTTP_TRANSPORT (default 'urllib'; 'requests' opt-in)`, `ETL_DB_POOL_MIN / ETL_DB_POOL_MAX (connection-pool sizing, defaults 1/8)`, `ETL_CONSECUTIVE_FAILURE_LIMIT (default 5)`, `BASEBALL_DB_DSN`

> **Notes for anyone changing this file:** (1) load_game vs reload_game is the single biggest footgun in this file: after ANY change to _fetch_game_pitches/_build_row_dict/_validate_row that alters a column's VALUE, re-running load_game against already-ingested games is a silent no-op because of ON CONFLICT DO NOTHING — you must use reload_game / refresh_seasons(reload=True) / load_date_range(reload=True) to actually rewrite data. (2) ReloadShrinkError guards a reload from replacing a game's rows with strictly fewer rows (usually a transient empty feed); pass allow_shrink=True only for a genuine reduction. (3) The urllib-vs-requests transport choice is not stylistic — the module's own comment block documents a multi-thousand-game investigation into a stochastic CPython inline-cache-corruption crash (SIM-446) implicating the `requests`/charset_normalizer/brotli stack; do not swap the default back casually. (4) The physics-plausibility WARNING thresholds here (release_speed 50-110, launch_speed>125, IVB +/-25) must stay in lock-step with the Postgres trigger raw.flag_pitch_quality() (migration 0016) — whichever layer is stricter silently wins. (5) _VENUE_OVERRIDES is a hand-maintained dict for MLB's rare venue-less neutral-site games (Field of Dreams etc.); a new one must be looked up in the unfiltered /api/v1/venues catalogue and added there, or the loader raises MissingVenueError by design rather than guessing.

---

### `pipeline/live/live_ingestion_pipeline.py`

The live-game ingestion service: polls the MLB schedule for live games, opens one WebSocket per live game, rebuilds a game_state JSON on every signal, persists it (Postgres sim.lineup_state + raw.games, and Redis), fetches game/prop odds via the SIM-370 provider seam, fires a re-simulation callback at the end of every plate appearance, and broadcasts state to frontend WebSocket clients.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `LiveIngestionPipeline.__init__(dsn, redis_url, simulation_callback, odds_provider)` | Requires simulation_callback to be an async function (raises TypeError immediately otherwise — SIM-106); resolves its OddsProvider via pipeline.odds_provider.get_odds_provider() unless one is injected. | `api/main.py (lifespan, simulation_callback=_resim_signal)` | — |
| `LiveIngestionPipeline.start() / stop()` | Lifecycle: start() opens the DB pool/Redis/HTTP session and pre-hydrates _completed_games from today's already-Final raw.games rows (so a restart mid-afternoon doesn't trigger an upsert storm). | `api/main.py lifespan` | — |
| `LiveIngestionPipeline._sync_live_games()` | The 30s schedule poll: spins up/tears down an MLBGameWebSocket per Preview->Live->Final transition and upserts raw.games for every game on today's slate. | `_schedule_poller (internal loop)` | — |
| `LiveIngestionPipeline._refresh_game_state(game_pk)` | The core per-signal cycle: fetch feed -> GameStateBuilder.build() -> upsert sim.lineup_state -> cache to Redis -> fetch odds -> broadcast to frontend WS -> _should_resimulate() -> _signal_resimulation(). Guarded by a per-game asyncio.Lock that SKIPS (does not queue) an overlapping refresh. | `MLBGameWebSocket on_update callback`<br>`_sync_live_games (initial + final refresh)` | `GameStateBuilder.build`<br>`_fetch_feed`<br>`_upsert_lineup_state`<br>`_cache_to_redis`<br>`_fetch_odds`<br>`_should_resimulate`<br>`_signal_resimulation` |
| `LiveIngestionPipeline._should_resimulate(game_pk, feed)` | Fires exactly once per completed plate appearance (about.isComplete), de-duped via _last_resim_at_bat so a burst of WS messages for one PA cannot double-trigger a re-sim. | `_refresh_game_state` | — |
| `LiveIngestionPipeline.mark_closing_lines(game_pk, first_pitch_at) / mark_closing_prop_lines(game_pk, first_pitch_at)` | SIM-133/SIM-340: stamps the last pre-first-pitch line_type='current' row(s) in raw.game_odds/raw.prop_odds as 'closing' — the reference line the CLV engine reads. | — | — |
| `LiveIngestionPipeline._collect_prop_player_roles(game_state) / _prop_stats_for_role(role, prop_stats)` | SIM-421: player_id -> role (PROP_ROLE_PITCHER for the current pitcher, PROP_ROLE_BATTER for a lineup entry with a non-pitcher boxscore position, PROP_ROLE_BOTH — every market, the safe default — for an entry with no position, a pitcher code in the batting order, or a player seen in two roles) and the market subset each role is asked for. This is what keeps the batter 'hits' market apart from the pitcher 'hits_allowed' market. `_collect_prop_player_ids` (SIM-340) is now the keys of the roles map. | `_persist_prop_odds_cycle`<br>`_fetch_prop_odds` | `PITCHER_PROP_STATS`<br>`BATTER_PROP_STATS` |
| `LiveIngestionPipeline._fetch_prop_odds(game_pk, player_ids, *, line_type, books, prop_stats, roles=None) / capture_opening_prop_lines(..., roles=None)` | SIM-340 multi-book prop fetch; SIM-421 adds the optional `roles` map that splits the markets by role. `roles=None` (or a player missing from it) keeps the old every-market behaviour; `_persist_prop_odds_cycle` (the 60-second live cadence) passes the roles map. | `_persist_prop_odds_cycle`<br>`capture_opening_prop_lines (no production caller found in the repo — see the note below)` | `_prop_stats_for_role`<br>`OddsProvider.get_prop_odds` |
| `GameStateBuilder.build(feed)` | Transforms one feed/live payload into the game_state JSONB shape sim.lineup_state stores; caches per-game play history and last-parsed at-bat index (SIM-101) so a refresh only parses NEW plays. | `LiveIngestionPipeline._refresh_game_state` | — |
| `MockOddsAPI.get_odds / get_prop_odds` | Deterministic, game_pk-seeded fake odds/prop source; the default OddsProvider behind pipeline.odds_provider.get_odds_provider() unless ODDS_PROVIDER selects a real one. `_PROP_CONFIG` carries a line centre per market for all 15 (the eight SIM-421 centres are wiring placeholders a Betting Analyst pass can tune); a unit test pins its keys to PROP_STATS. | `pipeline/odds_provider.py (_make_mock_provider)` | — |

**Depends on:** `pipeline/odds_provider.py (OddsProvider, get_odds_provider, PROP_STATS, PITCHER_PROP_STATS, BATTER_PROP_STATS)`, `MLB Stats API + WebSocket (wss://ws.statsapi.mlb.com)`, `asyncpg`, `redis.asyncio`, `aiohttp`, `FastAPI/WebSocket`

**Used by:** `api/main.py (constructs and mounts the pipeline + its WS router)`

**Environment flags read here:** `BASEBALL_DB_DSN`, `REDIS_URL`, `ODDS_PROVIDER (indirectly, via get_odds_provider())`

> **Notes for anyone changing this file:** IMPORTANT GOTCHA: mark_closing_lines and mark_closing_prop_lines are fully implemented and tested, but a repo-wide search finds NO production caller — not from api/main.py, and not from inside this class's own _refresh_game_state/_sync_live_games. Despite CLAUDE.md calling CLV the platform's 'gold-standard metric', closing lines are never automatically marked in the running app today; something must call these methods manually or a wiring gap needs to be closed. Separately: the per-game asyncio.Lock in _refresh_game_state SKIPS (not queues) an overlapping refresh, so under a burst of WS messages some signals are silently dropped by design. simulation_callback must be `async def`; a sync function raises TypeError at construction time, not at first use. PROP_STATS is no longer defined here — it is imported from pipeline/odds_provider.py and re-exported (SIM-421); edit the vocabulary there, never here.

---

### `pipeline/live/bullpen_availability_ingest.py`

SIM-433 per-game bullpen availability ingestion: for every pitcher on each team's FULL active roster (not only arms that appeared), decides available/reason (IL, rest, recent_use, active) by combining live MLB-API roster/IL status with rest/workload derived from raw.pitches, producing raw.game_bullpen_availability rows the manager-usage similarity engine needs for its 'chose not to use' signal.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BullpenAvailabilityIngest.decide(pitcher_id, status, days_rest, pitches_last_3d, back_to_back)` | The precedence rule: IL/off-roster -> back-to-back rest -> heavy-3-day workload (>= REST_PITCHES_3D=45 pitches) -> active. | `build_rows` | — |
| `BullpenAvailabilityIngest.build_rows(game_pk, workload, roster_by_team, timeline, game_date)` | Builds one row per full-roster pitcher (deriving rest for arms that did NOT pitch from the appearance timeline via _rest_as_of) plus a fallback row for any arm that appeared but is missing from the roster fetch. | `ingest_game` | `decide`<br>`_rest_as_of (module function)` |
| `BullpenAvailabilityIngest.ingest_game(game_pk, workload, timeline) / ingest(workload_records)` | Per-game and whole-run entry points; per-game failures are logged and skipped rather than aborting the run. | `main() CLI` | `_resolve_game_meta`<br>`_fetch_team_pitchers`<br>`build_rows`<br>`_persist` |
| `BullpenAvailabilityIngest._fetch_team_pitchers(team_id, date_str)` | The MLB-API network seam: merges the active 26-man roster fetch with the fullSeason roster's IL entries so injured arms aren't lost. | `ingest_game` | — |
| `BullpenAvailabilityIngest._persist(rows)` | psycopg2 execute_values UPSERT into raw.game_bullpen_availability (ON CONFLICT game_pk/team_id/pitcher_id DO UPDATE); overridden in unit tests to avoid a live DB. | `ingest_game` | — |
| `_load_workload_records(duckdb_path, pg_dsn, seasons)` | Module-level: constructs a PlayerProfileComputor purely to run _compute_bullpen_workload and return its records as dicts, so the CLI is self-contained. | `main() CLI` | `pipeline.batch.player_profile_computor.PlayerProfileComputor._compute_bullpen_workload` |

**Depends on:** `pipeline/batch/player_profile_computor.py (_compute_bullpen_workload, workload source — no network)`, `MLB Stats API teams/{id}/roster (network)`, `psycopg2`

**Environment flags read here:** `BASEBALL_DB_DSN (CLI --dsn default)`

> **Notes for anyone changing this file:** AvailabilityRow.is_starter is a RESERVED field, always False today — no code path sets it True yet; a consumer must not treat it as a populated signal. The decision precedence matters: an IL pitcher who also happened to pitch back-to-back is still reported IL, not 'rest'. REST_PITCHES_3D=45 is explicitly documented as an untuned placeholder pending SIM-427/434 validation. Only invoked as a standalone CLI (`python -m pipeline.live.bullpen_availability_ingest`), not wired into any scheduler in this repo.

---

### `pipeline/batch/player_profile_computor.py`

The nightly Step-1.4 batch job: reads raw.pitches (plus raw.games/raw.managers) from Postgres via DuckDB's postgres extension and computes every derived per-player/park metric table (pitcher GMM arsenals, batter/fielder/baserunner/catcher/manager season metrics, park factors, the RE24 run-expectancy matrix) plus every simulation play-pool table (pitch/outcome/stolen-base/steal-opportunity/advancement-opportunity pools, IBB rates, at-bat situations) that the similarity engines and the full-pool sampler read from.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PlayerProfileComputor.run(seasons, full_rebuild)` | The single orchestration entry point; a FIXED step order (park factors -> pitcher -> batter -> baserunner -> steal metrics -> manager -> RE24 matrix -> catcher framing/blocking/throwing -> outfield -> infield/DP/bunt/scoop/errors -> season aggregation -> sim pools last) that later steps' comments say is load-bearing, not stylistic. | `module __main__ CLI`<br>`scripts/nightly_ingest.sh (step 2/3)`<br>`scripts/rebuild_pools.py and the various scripts/sim*_rebuild_pools.py one-off scripts` | `every _compute_*/_build_* method below, in order` |
| `PlayerProfileComputor._refuse_a_mixed_cutoff(table, seasons, asof_date)` / `_stamp_one_cutoff(table, asof_sql)` | SIM-551: one cutoff per stamped table (`STAMPED_TABLES`, the eight `derived.*` profile tables with an `asof_date`). Every builder calls the first at its head, BEFORE its delete and insert: a rebuild of `seasons` is refused when a row it leaves alone could not carry the new stamp (a row of the cutoff's own season at another date; a row stamped inside its own season before that season's last game, a truncated point-in-time build) — the message names the season to add. `run()` checks all eight tables up front. The second runs after the insert and stamps the WHOLE table (the old step stamped only the unstamped rows, which is how a partial rebuild left the batter table at two dates and the batter engine refused to build for five days). | `every _compute_*/_build_*/_aggregate_* builder of a stamped table`<br>`run()` | `_season_last_game_date` (pg.raw.pitches, for a same-year stamp) |
| `PlayerProfileComputor._compute_pitcher_profiles(seasons)` | Two-pass: SQL command/result metrics, then a per-(pitcher,season) sklearn GaussianMixture arsenal fit — the single most expensive step in run(). | — | `_fit_gmm_for_pitcher`<br>`_fit_gmm_batch`<br>`_flush_gmm_results` |
| `PlayerProfileComputor._compute_manager_profiles(seasons)` | SIM-408: usage/aggression/platoon manager-season tendency profile; the USAGE sub-score (starter pitch-count/quick-hook, opener/bulk usage, closer leverage, high-leverage bullpen reliance) is derived from raw.pitches pitcher-stints attributed to the fielding manager. | — | — |
| `PlayerProfileComputor._compute_bullpen_workload(seasons)` | SIM-433: per (game, team, pitcher) rest/pitches_last_3d/back_to_back straight from raw.pitches, no network. Deliberately NOT part of run()'s nightly chain — only pipeline/live/bullpen_availability_ingest.py calls it directly against the live roster. | `pipeline/live/bullpen_availability_ingest.py (_load_workload_records)` | — |
| `PlayerProfileComputor._compute_catcher_framing / _compute_catcher_blocking / _compute_catcher_throwing (seasons)` | Sigmoid probability models (called-strike, PB/WP, stolen-base prevention) whose temp tables feed _aggregate_catcher_season_metrics; must run in exactly this order because each consumes the prior step's temp table. | — | — |
| `PlayerProfileComputor._compute_infield_oaa / _compute_outfield_catch_probability / _compute_dp_metrics (seasons)` | Statcast-methodology OAA/DP defensive-value models (intercept-time-margin, catch-probability sigmoid) feeding _aggregate_fielder_season_metrics; depend on the RE24 matrix having already been built. | — | — |
| `PlayerProfileComputor._aggregate_fielder_season_metrics(seasons, asof)` | Combines the per-play temp tables into `derived.fielder_season_metrics` with a POSITIONAL insert (no column list: the SELECT's column count and order must equal the table's). SIM-550 writes the six arm columns NULL for `_fill_outfield_arm_block`. SIM-532 (2026-09-17, code and data landed): two more Savant joins, both season-shifted at a cutoff like arm strength (neither board has a date) — `pg.raw.savant_outs_above_average` per (player, season, POSITION) and `pg.raw.savant_outfield_jump` per (player, season) — and the six-column tail after `asof_date`, generated from `OAA_JUMP_COLUMN_ORDER` (`savant_oaa`, `savant_oaa_per_100` = the count × 100 / our `opportunities`, NULL at zero chances; `jump_reaction_ft`, `jump_burst_ft`, `jump_route_ft`, `jump_plays` on outfield rows only, `_SQL_IS_OF`). `FIELDER_TAIL_COLUMNS = ("asof_date", *OAA_JUMP_COLUMN_ORDER)` is the one definition; a unit test holds the migration, the canonical schema and the SELECT to it, and `scripts/sim532_fielder_recompute.py` checks the live table's tail. | `run()`<br>`scripts/sim532_fielder_recompute.py` | `_fill_outfield_arm_block` (after) |
| `PlayerProfileComputor._build_pitch_pool / _build_outcome_pool / _build_stolen_base_pool / _build_steal_opportunity_pool / _build_advancement_opportunity_pool / _build_ibb_rates / _build_at_bat_situations (seasons, incremental)` | Build the sim.* tables the SIM-422 engine-artifact bundle reads from; `incremental` (SIM-095) skips seasons whose upstream source is unchanged. _build_advancement_opportunity_pool must run AFTER _build_outcome_pool (reads its transition columns). | — | `pipeline/statcast_events.py helpers` |
| `SQL_OUTCOME_TYPE` + the code sets (`BALL_TYPES`, `CALLED_STRIKE_TYPES`, `WHIFF_TYPES`, `FOUL_TYPES`, `STRIKE_THREE_FOUL_TYPES`) | SIM-553 (2026-09-25): the pitch pool's class expression, rendered from one code set per class; `_build_pitch_pool` inserts it. A foul tip or a foul bunt with two strikes is `swinging_strike` (strike three); below two strikes it is a foul. There is no ELSE: an unknown MLB code fails the insert. `WHIFF_TYPES` / `NON_SWING_TYPES` feed the profile SQL, so a whiff rate and the pool describe the same events; `tests/unit/test_sim553_foul_tip_strike_three.py` and `test_sim501_profile_code_sets.py` run the expression as a truth table. `POOL_BUILDER_VERSION` = `sim553.1`. | `_build_pitch_pool` | `pipeline/statcast_events.py` (`IN_PLAY_TYPES`) |
| `build_run_expectancy_matrix(conn, seasons)` | Module-level function fitting the RE24 base-out logistic run-expectancy matrix; stored on self._re_matrix and consumed by the DP-run-value and OF-arm-runs steps, which must run after it. | — | — |
| `LeagueAverageProfiles.compute(seasons)` | Separate class computing derived.league_averages, the fallback profile similarity engines use for below-minimum-sample players; run after PlayerProfileComputor.run() in the CLI __main__. Since SIM-427 (2026-09-13) it also writes the `manager` entity: the game-weighted mean of the seventeen tendency columns over the season's qualifying managers (a column NULL on every row averages to NULL) — the manager engine's shrinkage target and the steal weight's league denominator (`steal_order_rate_per_1b_opp`). `scripts/sim427_manager_recompute.py` recomputes the manager profiles and this row on their own. | — | — |

**Depends on:** `pipeline/statcast_events.py (out-label/steal-label SQL helpers)`, `simulation/constants.py (DEFENSIVE_RUN_VALUES)`, `DuckDB's postgres extension (ATTACH ... READ_ONLY)`, `sklearn (GaussianMixture, LogisticRegression, StandardScaler)`, `raw.play_events (optional — probed for existence, degrades gracefully if the Alembic 0018 migration is absent)`

**Used by:** `scripts/nightly_ingest.sh (step 2/3)`, `scripts/rebuild_pools.py and scripts/sim*_rebuild_pools.py one-off recompute scripts`, `pipeline/live/bullpen_availability_ingest.py (workload query only, not the full run())`

**Environment flags read here:** `BASEBALL_DB_DSN (CLI default)`, `BASEBALL_DUCKDB_PATH (CLI default, /data/baseball_sim.duckdb)`

> **Notes for anyone changing this file:** (1) The step ORDER inside run() is load-bearing: baserunner profiles must precede infield/DP (sprint-speed dependency), the RE24 matrix must precede DP/OF-arm value, catcher framing->blocking->throwing share temp tables in that order, and sim pools must run LAST because they denormalize from derived.*. Reordering without re-reading the inline comments will silently empty or corrupt a downstream table. (2) The `_play_events_outs_cte`/`_play_events_disengagement_cte`/`_pickoff_outcomes_cte` helpers each probe for raw.play_events and fall back to an empty relation if migration 0018 hasn't run — a pre-migration DB silently loses ~0.5% innings-pitched accuracy rather than erroring. (3) A bare CLI run defaults to the CURRENT SEASON ONLY; a full historical recompute needs explicit `--seasons 2017 ... 2026 --full-rebuild` — CLAUDE.md calls this out as a recurring operational trap. (4) CLAUDE.md's 'DO NOT run the profile recompute' guidance is about running this against un-swept/stale upstream data, not a defect in this file itself — check BACKLOG.md / CLAUDE.md §2b for the current gating state before running a full rebuild.

---

### `pipeline/batch/engine_artifacts.py`

SIM-422 nightly BUILDER (and per-worker LOADER, EngineArtifacts) for the disk-resident 'engine artifact bundle' that lets a stateless ProcessPool worker reconstruct the full-pool similarity sampler without any live engine object crossing the fork: per-hand pitch/batted-ball/steal/advancement opportunity pools, the pitcher x pitcher similarity matrix, the SIM-523 actor score matrices, park geometry, the pitching-change opportunity pool, and catcher receiving profiles.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `build_pitch_pool_artifact / build_battedball_pool_artifact / build_steal_pool_artifact / build_advancement_pool_artifact (con, out_dir, seasons)` | Export sim.pitch_pool / sim.outcome_pool / the steal and advancement opportunity pools to per-hand .npy/.parquet files under <out_dir>. | `main() --what pool` | — |
| `build_pitcher_sim_matrix(duckdb_path, out_dir, seasons, limit)` | Runs the live PitcherSimilarityEngine and exports both the legacy dict-of-dicts and a dense (n_prof x n_prof) float32 matrix (pitcher_sim.npz); the dense form exists because the dict costs ~2GB/worker and cannot ride shared memory (the SIM-430 OOM root cause). | `main() --what pitcher_sim/all` | `similarity.engines.pitcher_similarity.PitcherSimilarityEngine` |
| `build_actor_sim_matrices(duckdb_path, out_dir, seasons, limit, strict, only=None)` | SIM-523 part A: for each actor role (batter/catcher/catcher_throwing/fielder_<POS>/runner_steal/runner_adv/pitcher_steal and, since SIM-427, `manager_usage` — the manager engine's USAGE sub-score, keyed `manager_id:season`), queries that role's live similarity engine for every profile pair, writes a dense score matrix per role plus a concentration report; a strict build RAISES if any matrix's p90 own-staff draw-weight ratio exceeds CONCENTRATION_MAX_RATIO (3.0). `only` (CLI `--matrix NAME ...`) builds one matrix and merges its size into the existing manifest, skipping the concentration report — the way around the SIM-445 segfault class on the full build. | `main() --what actors_sim/all` | `the six engines in _ACTOR_SIM_ENGINES (lazily loaded via _ENGINE_LOADER)`<br>`_matrix_from_queries`<br>`concentration_report` |
| `concentration_share(matrix, live_row, row_actor_col, row_pitcher, staff, power) / concentration_report(duckdb_path, sim_dir, seasons)` | Pure arithmetic plus a DuckDB-driven report: per catcher/fielder-season, the fraction of pool draw-weight that would land on that player's own team's rows, checked as a build-time gate. | `build_actor_sim_matrices` | — |
| `build_park_geometry(con, out_dir, seasons)` | SIM-523 part C: writes park_geometry.json — fence line per venue/sector, a carry (EV/LA -> distance) regression model, and its HR validation. SIM-478 (2026-09-21, version 2): the grid is eleven ten-degree sectors over ±55° (a ball down the line has its own sector); each venue's lines sit in SEASON GROUPS ("2023-2024", "2025-2026") that a change detector opens where a sector's early and late lines differ by ten feet with ten home runs on each side (`detect_moves`; every split is listed under `moves`); a thin sector reads the published-distance PRIOR (`pipeline/etl/mlb_venue_dimensions.py` fetches the Stats API's fence distances into `venue_dimensions.json`; `published_at` interpolates them at the sector's midpoint and `league_offsets` adds the league's effective-minus-published gap), blended toward the park's own balls at ten home runs (`_thin_rule`: "blend" / "prior"); a published park the pool never saw gets the prior alone. The CARRY OFFSET (2026-09-22, the plan's §11): `carry_offsets` fits the window's home runs on a quadratic in exit velocity and launch angle with spray terms and one dummy per park (100+ home runs), centred on the median park — each park's extra carry in its own air (Coors +21.4 ft, Kauffman +9.9, Yankee Stadium −6.5, Fenway −2.8) — written as `carry_offset_ft` and `carry_offset_fit`; the sampler reads a born ball in the live park's air with it. The pure pieces (`load_air_balls`, `sector_of`, `sector_line`, `build_geometry_document`) are what `scripts/sim478_fence_check.py` re-runs for the held-out and moved-wall reads. | `main() --what park/all`; `scripts/sim478_fence_check.py` | `pipeline.etl.mlb_venue_dimensions` |
| `build_pitching_change_pool(con, out_dir, seasons, *, game_context=True) / build_receiving_profiles(con, out_dir, seasons)` | SIM-523 parts D/E: the pitching-change opportunity pool and the catcher receiving (framing/blocking) ratio document; both are built but their consuming draw-weights ship OFF by default per CLAUDE.md. Since SIM-427 the change pool's `change.meta.parquet` also carries per row the fielding side's `manager_id` and the incoming arm's `in_days_rest` / `in_pitched_2d` / `in_pitches_3d` / `in_throws` (from `pipeline.bullpen_usage.fetch_game_context_sync`; -1 / 0 = unknown; the manifest reports `manager_coverage` and `rest_coverage`), plus the reliever ROLE sidecar `roles.parquet` (pitcher_id, season, n_entries, hi_lev_share, mean_entry_inning) the reliever draw's role weight reads (`EngineArtifacts.change_roles`), and — since 2026-09-13 — each row's `game_ymd` (the game date as YYYYMMDD, from the pitch pool's `game_date`; the manifest's `game_ymd` flag says whether the pitch pool had it), so a backtest's point-in-time cutoff (SIM-535, `FullPoolSampler.set_asof`) applies to the pitching-change draw too; a date column NULL on every row loads as ABSENT and a cutoff then refuses. | `main() --what manager/receiving/all` | `pipeline.bullpen_usage.fetch_game_context_sync` |
| `build_actor_embeddings(con, out_dir)` | Exports per-(actor, season) z-scored embeddings + global mean/std, kept for the (mostly retired) bell-curve-kernel / thin-profile fallback path. | `main() --what actors/all` | — |
| `EngineArtifacts.load(art_dir, shared_views=None) (classmethod)` | The per-worker loader; when shared_views (from extract_shared_arrays) is supplied, skips re-reading the big .npy arrays and takes zero-copy views instead, turning a ~150-300MB cold load into a ~5MB metadata-only read per worker (the SIM-402 cold-fan-out fix). | `simulation/production_factory.py`<br>`simulation/synthetic_bundle.py (test/no-DB analog)`<br>`api/main.py (lifespan pre-warm)` | — |
| `EngineArtifacts.extract_shared_arrays() / attach_shared_views(views)` | The SIM-403b parent-publishes / worker-attaches shared-memory contract; excludes only object-dtype columns and the legacy pitcher_sim dict (not shareable through multiprocessing.shared_memory). | `simulation/batch_runner.py (shared_arrays plumbing)` | — |
| `HandPool / BattedBallPool / StealPool / AdvancementPool / ChangePool (dataclasses)` | Typed in-memory pool shapes held by EngineArtifacts.pools/bb_pools/steal_pools/adv_pools/change_pool; several fields are Optional/None so an artifact bundle built before a given SIM ticket still loads and the consuming factor degrades to neutral. | — | — |

**Depends on:** `pipeline/batch/player_profile_computor.py's output tables (sim.*, derived.*)`, `similarity/engines/* classes (pitcher/batter/catcher/fielder/baserunner/baserunner_steal/pitcher_steal)`, `DuckDB (read-only)`

**Used by:** `simulation/production_factory.py`, `simulation/synthetic_bundle.py`, `scripts/nightly_ingest.sh (step 3/3, `--what all`)`, `api/main.py (lifespan pre-warm)`

**Environment flags read here:** `BASEBALL_PLAY_POOL_DIR (default /data/play_pool, artifact output root)`, `BASEBALL_DUCKDB_PATH (default /data/baseball_sim.duckdb, DuckDB source)`

> **Notes for anyone changing this file:** (1) There is no Makefile target for this build step (CLAUDE.md calls this out explicitly) — it must run via `python -m pipeline.batch.engine_artifacts --what all` AFTER player_profile_computor, exactly as scripts/nightly_ingest.sh does; skipping or misordering it leaves workers reading a stale bundle. (2) RECENCY_FLOOR_SEASONS=4 (last 3 complete seasons + current) is an owner-ruled constant (SIM-516) that determines the pool window every calibration/certification measures against — do not retune it as a perf knob without re-running the certifying lane. (3) Most pool-loading code probes information_schema.columns before reading a newer optional column (catcher_id, got_away, bat_home, zone, etc.) so an artifact built from a DB that hasn't run a given migration/rebuild still loads with that factor neutral — follow this 'probe, default-to-neutral' pattern for any new column. (4) The dense-matrix-over-dict pattern (pitcher_sim_matrix, actor_sim matrices) exists purely to survive multiprocessing.shared_memory's no-object-graph restriction after a real OOM incident (SIM-430) — do not reintroduce a dict-of-dicts for a new large per-pair score table without checking that history.

---

### `pipeline/batch/pool_chain.py`

SIM-553 (2026-09-25): the count chain and the label check. The count chain takes the pitch pool's class shares at each of the twelve counts, applies the count machine's rules (a ball at three balls is a walk; a called or swinging strike at two strikes is a strikeout; a foul below two strikes adds a strike and at two strikes repeats the count; in play and hit-by-pitch end the plate appearance) and returns the per-plate-appearance rates a faithful sampler of that pool produces. The label check compares that chain with real plate appearances from `raw.pitches`: it exists because two coding defects in the pool's class expression (the hit-by-pitch rows, SIM-509; the two-strike foul tips, SIM-553) were invisible to the pool-totals grade, which reads the same labels.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `solve_chain(rates)` | The absorbing count chain, verbatim to the solver the census used before 2026-09-06 (the deleted `scripts/sim429_chain_analysis.py`); returns walk / k / in_play / hbp / pitches per plate appearance. Raises on an all-foul two-strike count. | `chain_rates`; `scripts/pool_window_census.py` | — |
| `pool_count_matrix(con, pred, weighted=False, pa_only=False) / chain_rates(...)` | The 12 × 6 count-by-class matrix of `sim.pitch_pool WHERE pred` (COUNT or recency-weighted), and its chain. Raises on a class outside `OUTCOMES` and on an empty count. `pa_only=True` keeps only the at-bat groups that end in a batting event, judged by the pool's own `events` column with the same rule as `real_pa_rates` — the label check's population; the default (every row) gives the band centres, because the simulator draws every row. | `scripts/pool_window_census.py`; `scripts/sim553_rebuild_pitch_pool.py` (check d); `tests/acceptance/test_sim553_pool_label_check.py` | — |
| `attach_pg(con, dsn=None) / real_pa_rates(con, pred)` | Attaches Postgres read-only as `pg`; the real per-plate-appearance rates (K, BB, HBP, pitches, n_pa) over the at-bat groups of `raw.pitches` (data_quality_flag = FALSE) that end in a batting event. | the same | DuckDB's postgres extension |
| `pa_group_count(con, pred)` | The number of at-bat groups `pa_only` reads; the label check requires it to equal `real_pa_rates`' n_pa (708,439 on 2023–2026), so the two sides read the same plate appearances. | the same | — |
| `label_check(chain, real, tolerance=LABEL_CHECK_TOLERANCE) / format_*` | The per-channel relative gaps, PASS within 0.5%. Measured 2026-09-25 on the rebuilt pool: K +0.09%, BB −0.25%, HBP +0.08%, pitches +0.07%; the old coding read K −4.29%. | the same | — |

> **Notes for anyone changing this file:** Two non-label effects sit in the data and the population rule handles the first: at-bat groups cut short by a runner out have pitches in the pool but no plate appearance (the full-pool chain reads walks about +0.6 points high on them); pitch-clock automatic balls have no pitch row (walks −0.41% in 2023, fading to −0.16% in 2026). Do not widen the 0.5% tolerance to fix a red: read the class expression (`SQL_OUTCOME_TYPE`) first.

---

### `pipeline/odds_provider.py`

SIM-370 provider-abstraction seam: a typing.Protocol plus an env-selected factory that lets a real odds/prop feed swap in behind MockOddsAPI without touching ingestion, CLV, or persistence code. Since SIM-421 it is also the ONE place the raw.prop_odds.prop_stat vocabulary is written down (15 markets).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PROP_STATS / PITCHER_PROP_STATS / BATTER_PROP_STATS` | SIM-421: the 15-market vocabulary (the seven SIM-134 originals first: strikeouts, hits, home_runs, earned_runs, walks, total_bases, rbis; then singles, doubles, triples, runs, stolen_bases, hits_runs_rbis, outs_recorded, hits_allowed), split into the 5 pitcher markets and the 10 batter markets. Tuples, so a caller cannot mutate the order. The CHECK constraint on raw.prop_odds (Alembic 0022) lists the same 15 strings and a unit test keeps the two in step. | `pipeline/live/live_ingestion_pipeline.py`<br>`pipeline/bettingpros_odds_provider.py (an import-time guard against its market-id map)`<br>`pipeline/etl/opening_line_job.py`<br>`scripts/load_historical_odds.py` | — |
| `PROP_STAT_TO_MODEL_PROP` | SIM-421: odds prop_stat -> the model prop name simulation/prop_distributions.py emits (strikeouts->K, ..., hits->H, hits_allowed->H_ALLOWED, singles->1B, hits_runs_rbis->HRR). scripts/clv_backtest.py's PROP_VOCAB_MAP carries the same 15 pairs (scripts/ is not a package). | — | — |
| `GAME_MARKET_TYPES / LEGACY_GAME_MARKET_TYPES / GAME_MARKET_KIND / GAME_MARKET_SEGMENT / GAME_MARKET_SIDE / THREE_WAY_GAME_MARKET_TYPES / GAME_ODDS_FIELDS` | SIM-421 (owner ruling 2026-09-12): the raw.game_odds.market_type vocabulary — the three full-game markets (moneyline, runline, total) plus the twelve segment / team markets (f1_moneyline, f5_moneyline, f1_total, f5_total, f1_runline, f5_runline, team_total_home/away, f5_team_total_home/away, first_to_score, first_inning_run). KIND says which columns a market fills (moneyline / three_way / runline / total / yes_no), SEGMENT the slice it settles on (game / f1 / f5), SIDE the team a team total counts. The CHECK constraint on raw.game_odds (Alembic 0024) lists the same 15 values; a unit test keeps the constraint, the reference DDL and the BettingPros market-id map in step. | `pipeline/live/live_ingestion_pipeline.py`<br>`pipeline/bettingpros_odds_provider.py`<br>`scripts/load_historical_odds.py`<br>`scripts/clv_backtest.py`<br>`simulation/game_market_distributions.py` | — |
| `OddsProvider (Protocol)` | The structural interface (get_odds, get_prop_odds) every provider — mock, BettingPros, or a future real feed — must match; @runtime_checkable so isinstance() works as a duck-typed presence check. get_prop_odds must raise ValueError on a prop_stat outside PROP_STATS. | — | — |
| `get_odds_provider(name=None)` | Selects a provider by explicit name, else the ODDS_PROVIDER env var, else 'mock'. | `pipeline/live/live_ingestion_pipeline.py (LiveIngestionPipeline.__init__ default)`<br>`pipeline/etl/opening_line_job.py (_fetch_current_odds/_capture_prop_opening_lines)`<br>`scripts/load_historical_odds.py` | `registered factory (._make_mock_provider or ._make_bettingpros_provider)` |
| `register_odds_provider(name, factory)` | Registers a named zero-arg provider factory; the mock and BettingPros providers self-register at import time via this at module bottom, and tests use it to inject fakes. | — | — |
| `RealOddsAPIProvider` | A documented STUB template only — no ODDS_PROVIDER value resolves to it any more (both 'bettingpros' and 'real' map to BettingProsOddsProvider); every method raises RuntimeError. | — | — |

**Depends on:** `pipeline/live/live_ingestion_pipeline.py (MockOddsAPI, lazily imported to avoid a circular import and keep this module importable without the live pipeline's heavy deps)`, `pipeline/bettingpros_odds_provider.py (BettingProsOddsProvider, lazily imported)`

**Used by:** `pipeline/live/live_ingestion_pipeline.py`, `pipeline/etl/opening_line_job.py`, `scripts/load_historical_odds.py`

**Environment flags read here:** `ODDS_PROVIDER (selects 'mock' | 'bettingpros' | 'real')`, `ODDS_PROVIDER_API_KEY (read only by the unreachable RealOddsAPIProvider stub)`

> **Notes for anyone changing this file:** RealOddsAPIProvider is dead/unreachable via the public factory — it exists purely as an implementation template for a THIRD provider; setting ODDS_PROVIDER=real does not exercise unfinished code, it resolves to the live BettingProsOddsProvider. Any new provider MUST return the exact MockOddsAPI dict shapes documented in this module's docstring, since raw.game_odds/raw.prop_odds inserts and the CLV engine depend on those exact keys. A new prop market is added in FOUR places together: the tuples here, the CHECK constraint (a new Alembic migration in the 0022 style), MockOddsAPI._PROP_CONFIG and the BettingPros market-id map — the import-time guard and the vocabulary unit test fail if any one lags. `hits` is the BATTER market and `hits_allowed` the PITCHER market; they map to different model props (H vs H_ALLOWED) and must never be swapped.

---

### `pipeline/bettingpros_odds_provider.py`

SIM-405 real OddsProvider implementation backed by the BettingPros v3 HTTP API; bridges MLB game_pk/player_id identifiers to BettingPros events/offers (date+team-nickname matching, normalized player-name matching) and returns odds in the exact MockOddsAPI dict shape.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BettingProsOddsProvider.get_odds(game_pk, ...) / get_prop_odds(game_pk, player_id, prop_stat, ...)` | The two OddsProvider-protocol methods; both raise ValueError for an unknown market (prop_stat outside PROP_STATS, market_type outside GAME_MARKET_TYPES), mirroring the mock. SIM-421 (2026-09-12): get_odds serves every game market — the three full-game markets keep their legacy one-row shape; a segment / team market fills only its own fields through the per-kind parsers `_fill_moneyline` (three-way when the market has a `draw` selection), `_fill_runline`, `_fill_total`, `_fill_yes_no`, and `_team_offer_selections` picks a team total's offer by `team_id`. | `pipeline/odds_provider.py's _make_bettingpros_provider factory` | — |
| `BettingProsOddsProvider._resolve_event(game_pk) / _resolve_player_name(player_id)` | Identifier-bridge logic: schedule-date + team-nickname-suffix matching (double-headers disambiguated by earliest scheduled time), and normalized first+last player-name matching; both cached per instance. | `get_odds`<br>`get_prop_odds` | — |
| `BettingProsOddsProvider._pick_line(selection, line_type)` | Resolves 'opening' (the selection's opening_line), 'closing' (the max-`updated` line seen, since BettingPros has no explicit closing field — the SIM-435 canonical-closing-line convention), or 'current' (best/main book line) from one selection's book list. | `get_odds`<br>`get_prop_odds` | — |
| `BettingProsOddsProvider._offers(event_id, market_id) / _events_for_date(date_str)` | SIM-421: the two time-stamped per-instance caches. One `/offers` response carries EVERY player's offer for a market and serves every book and both line types, so one fetch per (event, market) per time-to-live replaces one fetch per (player, market, book, line type) — several hundred HTTP calls per game down to about fifteen. `_events_for_date` caches the day's `/events` slate the same way. A failed fetch raises to the caller and is never cached. | `_selections`<br>`_find_player_offer`<br>`_resolve_event` | `_is_fresh`<br>`_fetch_offers_all_pages` |
| `BettingProsOddsProvider._fetch_offers_all_pages(event_id, market_id)` | SIM-421: follows the API's `_pagination.total_pages` (10 offers per page; capped at `_MAX_OFFER_PAGES` = 20). Before this, page 1 alone was read, so a batter market's later pages were silently dropped and those players' quotes came back empty. | `_offers` | `_bp_get` |
| `_PROP_MARKET_IDS` | prop_stat -> BettingPros market id for all 15 markets (strikeouts 285, hits 287, home_runs 299, earned_runs 290, walks 408, total_bases 293, rbis 289; SIM-421: singles 295, doubles 291, triples 292, runs 288, stolen_bases 294, hits_runs_rbis 403, outs_recorded 405, hits_allowed 404). An import-time guard raises if its keys ever differ from PROP_STATS. | `get_prop_odds` | `pipeline.odds_provider.PROP_STATS` |
| `BettingProsOddsProvider._bp_get / _mlb_get` | The two stdlib-urllib network seams, both stubbed in unit tests against fixtures under tests/fixtures/bettingpros/ — no live call is ever made in tests. | `_resolve_event`<br>`_resolve_player_name`<br>`_fetch_offers_all_pages`<br>`_events_for_date` | — |

**Depends on:** `pipeline/odds_provider.py (PROP_STATS)`, `BettingPros v3 API (api.bettingpros.com)`, `MLB Stats API (schedule + people endpoints, for identifier bridging)`

**Used by:** `pipeline/odds_provider.py (registered under both 'bettingpros' and 'real')`

**Environment flags read here:** `ODDS_API_KEY (BettingPros API key)`, `ODDS_OFFERS_CACHE_TTL_S (SIM-421: the offers/events cache time-to-live in seconds; default 30 — half the live pipeline's 60-second prop cadence; 0 disables the caches; the constructor's offers_cache_ttl_s argument wins over the variable)`

> **Notes for anyone changing this file:** Double-headers are disambiguated by picking the EARLIEST scheduled BettingPros event when multiple team-name matches occur on the same date. `prefer_book_id` (constructor arg) does two things at once — it selects the preferred current-line book AND scopes the closing-line scan to that one book — changing it changes both behaviors together. The offers-cache time-to-live must stay BELOW the live pipeline's PROP_FETCH_CADENCE_S (60 s), or a live cycle persists a stale snapshot as 'current'; the offline historical loader is the only caller that should raise it (it sets 600 s when the variable is unset, because its games are over). The eight SIM-421 market ids were taken from the owner's scraper and have NOT been verified against the live API from inside this repo (no network in the build) — the first real odds run for the new markets is that check.

---

### `betting/__init__.py`

Package-level re-export surface for the betting layer; re-exports every public name from betting.clv_engine so callers can `from betting import prop_edge_report` etc.

**Depends on:** `betting/clv_engine.py`

**Used by:** `api/routes/games.py (`from betting import prop_edge_report as _prop_edge_report`)`

> **Notes for anyone changing this file:** betting.bet_signal and betting.line_movement are NOT re-exported here — api/routes/betting.py imports them directly from their submodules. betting/__init__.py re-exports an explicit SUBSET of betting.clv_engine: a new clv_engine function does NOT surface at package level until it joins that file's import list and `__all__`. The SIM-549 run-line functions, `run_line_edge_report` and `spread_cover_prob` are not re-exported; every caller imports them from `betting.clv_engine`.

---

### `betting/clv_engine.py`

SIM-339 pure, DB-free CLV/edge engine: American<->decimal<->implied-probability conversions, two-way/multi-way de-vig, edge/EV, and Closing Line Value, plus per-market EdgeReport builders consuming the simulator's own outputs (SIM-329 prop PMFs, SIM-330 win probability, SIM-327 raw score arrays).

> **A run line is a pair, or two separate bets (SIM-549, 2026-09-25).** raw.game_odds keeps each team's run line under its own spread (`home_spread` / `away_spread`), exactly as the book lists it. A row is a PAIR — one bet with two sides, whose prices de-vig against each other — only when the away spread is the negative of the home spread (the pick'em 0 / 0 included). Any other row is two separate bets: home −1.5 and away −1.5 both lose on a tie, home +1.5 and away +1.5 both win on a one-run game; 24% of the stored first-five closing rows, 80% of 2024's, 3% of the full-game and 10% of the first-inning. `devig_two_way` must never pair such prices: the accuracy comparison (`scripts/clv_backtest.py`, `score_run_line_market`) prices each bet from its own price over the margin of the same game's two-way markets — the segment's total, then the full-game total, then the moneyline, then a flat 1.05, never a three-way market (its margin prices the tie). Since the live-pages change of 2026-09-25 the rule lives HERE, in this file (`run_line_is_pair`, `reference_margin_from_prices`, `devig_one_sided`, `run_line_bet_cover_prob`, `one_sided_edge_report`), and all three readers use it: the accuracy comparison, the /edges and /signals route, and `betting/line_movement.py` (the CLV).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `devig_two_way(over_american, under_american) / devig_multiway(implied_probs)` | The proportional/multiplicative de-vig: normalizes raw implied probabilities (which sum to 1+overround) to sum to exactly 1.0. | `_build_edge_report`<br>`clv_from_odds`<br>`scripts/clv_backtest.py (the segment markets and a run-line pair, SIM-549)` | `implied_prob_from_american` |
| `edge(p_sim, p_fair) / expected_value(p_sim, offered_american)` | The two headline sign-convention functions: edge is measured against the de-vigged FAIR probability; EV is measured against the OFFERED (vigged) price. | `_build_edge_report` | — |
| `clv_from_odds(entry_side_american, entry_other_american, close_side_american, close_other_american) / clv_from_prob(entry_fair_prob, close_fair_prob) -> CLV` | Builds the entry-vs-close Closing Line Value; clv_prob > 0 means the entry beat the close. | `_build_edge_report (when a closing quote is present)`<br>`betting/line_movement.py (line_movement_from_quotes, entry=opening / close=closing quote)` | — |
| `moneyline_edge_report(win_prob, market, side) / prop_edge_report(dist, market, side) / total_over_under_edge_report(summary, market, side) / run_line_edge_report(summary_or_margin, market, side, line)` | The four market-specific EdgeReport builders, each a thin wrapper around _build_edge_report that supplies the sim-side probability from a different simulation output type (win probability, prop PMF, raw totals, raw score margin). | `api/routes/betting.py (moneyline/total/run_line)`<br>`api/routes/games.py (prop, via the betting package re-export)`<br>`scripts/clv_backtest.py (moneyline / total / prop; the run lines use run_line_bet_cover_prob and devig_one_sided since SIM-549)` | `_build_edge_report`<br>`spread_cover_prob (run_line only)` |
| `spread_cover_prob(summary_or_margin, line, side)` | Run-line cover-probability math from raw score margins, handling push exclusion at integer lines. `line` is always the HOME line; the AWAY side is priced at its negation. | `run_line_edge_report`<br>`run_line_bet_cover_prob` | — |
| `run_line_is_pair(home_spread, away_spread)` | SIM-549: True when the two spreads are one bet with two sides (away == −home, the pick'em 0 / 0 included); False for two separate bets or a missing spread. | `betting/line_movement.py`<br>`api/routes/betting.py (_build_edge_reports)`<br>`scripts/clv_backtest.py` | — |
| `reference_margin_from_prices(candidates) -> (margin, source)` | SIM-549: the game's two-way margin (the sum of two implied probabilities) from the first candidate market whose sum sits inside `REFERENCE_MARGIN_BAND` (1.00–1.15); the flat `DEFAULT_ONE_SIDED_MARGIN` (1.05) when none does. The caller orders the candidates: a segment total, the full-game total, the moneyline — never a three-way market. | `betting/line_movement.py (_reference_margin_reader)`<br>`api/routes/betting.py`<br>`scripts/clv_backtest.py (reference_margin)` | `implied_prob_from_american` |
| `devig_one_sided(american, margin)` | SIM-549: one separate bet's fair probability — its implied probability over the reference margin, capped at 1. | `betting/line_movement.py`<br>`one_sided_edge_report` callers<br>`scripts/clv_backtest.py` | `implied_prob_from_american` |
| `run_line_bet_cover_prob(summary_or_margin, side, own_line)` | SIM-549: the sim's cover probability for one side at ITS OWN spread (the away bet at −1.5 is the away team winning by 2+), so a two-bet row is never priced at the mirrored line. | `api/routes/betting.py`<br>`scripts/clv_backtest.py` | `spread_cover_prob` |
| `one_sided_edge_report(*, label, side, line, sim_prob, offered_american, fair_prob)` | SIM-549: the EdgeReport for one separate bet: the edge against the given fair probability, the EV against the offered price, no CLV. | `api/routes/betting.py (the two-bet run line)` | `edge`<br>`expected_value`<br>`prob_to_american` |

**Depends on:** `simulation/prop_distributions.py (PropDistribution)`, `simulation/results.py (GameSimSummary)`, `simulation/win_probability.py (WinProbability)`

**Used by:** `betting/__init__.py`, `betting/bet_signal.py`, `betting/line_movement.py`, `api/routes/betting.py`, `api/routes/games.py`, `scripts/clv_backtest.py`

> **Notes for anyone changing this file:** The edge-vs-fair / EV-vs-offered sign convention is the whole point of this file and is easy to invert accidentally — mixing them up produces a plausible-looking report that double- or never-counts the vig. `EdgeReport.positive_edge` is a STRICT `> 0` check, not `>= 0`. The simulation-type imports (PropDistribution/GameSimSummary/WinProbability) are for type clarity and convenience constructors only — the core arithmetic functions take plain floats and are independently testable with synthetic inputs.

---

### `betting/bet_signal.py`

SIM-369: sits on top of clv_engine's EdgeReports and turns a list of scored markets into a ranked list of fireable +EV bet recommendations with fractional-Kelly stake sizing; purely advisory, does not decide execution timing.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `bet_signals_from_edges(reports, config=DEFAULT_CONFIG) -> list[BetSignal]` | Gate (strictly positive edge >= min_edge AND ev > min_ev) -> size (fractional Kelly) -> rank (EV desc, then edge, then stake) -> stamp a 0-based rank. | `api/routes/betting.py` | `_passes_gate`<br>`stake_fraction` |
| `stake_fraction(edge_report, kelly_fraction, cap)` | clamp(kelly_fraction * max(0, full_Kelly), 0, cap); floors a -EV report at 0 stake since the module never backs the 'other side' from a report. | `bet_signals_from_edges` | `kelly_fraction_full` |
| `kelly_fraction_full(p_sim, offered_american)` | The raw full-Kelly fraction f* = (b*p - q)/b = EV/b; may be negative. | `stake_fraction` | — |
| `BetSignalConfig (frozen dataclass)` | min_edge (0.02 default), min_ev (0.0), kelly_fraction (0.25 = quarter Kelly), max_stake_fraction (0.05 = 5% cap); validates non-negativity in __post_init__. | — | — |

**Depends on:** `betting/clv_engine.py (EdgeReport, MarketSide, american_to_decimal)`

**Used by:** `api/routes/betting.py`

> **Notes for anyone changing this file:** A BetSignal is advisory only, per its own docstring — the caller decides WHEN to fire it relative to live line movement; the module deliberately encodes no timing rule. DEFAULT_MIN_EV=0.0 combined with the strict `>` check in _passes_gate means an exactly-breakeven report never fires.

---

### `betting/line_movement.py`

SIM-368: lifts clv_engine's single entry-vs-close CLV snapshot into a full opening->closing line-movement time series per (game_pk, market, side, book): running implied-probability series, per-step deltas, steam direction, and a sharp-books-vs-whole-market consensus flag.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `line_movement_from_quotes(rows, market_type, side, game_pk, book, sharp_consensus, reference_margin_at=None) -> LineMovement` | PURE builder: coerces raw.game_odds row mappings or LineQuote objects, sorts by fetched_at, computes the running implied-prob series + per-step deltas + opening/closing summary + steam direction, and builds the SIM-339 CLV from the opening/closing pair when both carry the opposite-side price. A run line (SIM-549) sets `run_line_shape` (pair / two_bets / mixed; a row with a missing spread does not count) and goes through `_run_line_clv`: no CLV when a spread is missing, or when the side's spread moved between open and close (a different bet; `clv_note` says so); the pair de-vig when both ends are pairs (`clv_basis` pair); otherwise BOTH ends priced the same way, each its own price over `reference_margin_at(quote)` (`clv_basis` two_bets), so a shape change alone never moves the CLV. The note names the flat 1.05 when it priced an end. | `fetch_line_movement`<br>`_net_direction` | `betting.clv_engine.clv_from_odds / clv_from_prob / devig_one_sided / run_line_is_pair`<br>`betting.clv_engine.implied_prob_from_american` |
| `fetch_line_movement(conn, game_pk, market_type, book=None) -> list[LineMovement] (async)` | The ONLY DB-touching function: reads every raw.game_odds row for (game_pk, market_type) ordered by fetched_at, groups by book, calls the pure builder per (side, book), and computes sharp_consensus (do the sharp books' net direction match the whole market's?). For a run line with any two-bet row it also reads the game's total and moneyline rows (`_SQL_FETCH_REFERENCE_ODDS`) and hands the builder `_reference_margin_reader`: the reference row of the quote's own line type nearest the quote in time (its own snapshot — the loader stamps a snapshot's total ~2 ms AFTER its run line), the same book first, then any book, then the flat 1.05. A reference market with no row of that line type falls back to its latest row at or before the quote. | `api/routes/betting.py` | `line_movement_from_quotes`<br>`_net_direction`<br>`_reference_margin_reader` |

**Depends on:** `betting/clv_engine.py (CLV, MarketSide, clv_from_odds, implied_prob_from_american)`, `an asyncpg-or-mock conn reading raw.game_odds (duck-typed like db/sim_store.py's readers)`

**Used by:** `api/routes/betting.py`

> **Notes for anyone changing this file:** The per-quote implied_prob is deliberately the RAW (vig-included) single-side probability — de-vig is a two-way operation and cannot apply to a lone side's time series — so implied_prob_delta's sign can DISAGREE with the embedded CLV.clv_prob's sign for the same series; treat CLV as the canonical value-bearing number and the implied-prob series as descriptive only. The ordering axis is strictly raw.game_odds.fetched_at (no other timestamp column exists on that table).

---

### `db/sim_store.py`

SIM-356: the single, mockable persistence layer for sim-run results and pitch-level replay data, split across Postgres (sim.sim_runs — the full GameSimSummary JSONB run history) and DuckDB (sim.play_stream, sim.state_snapshots, sim.game_cards — pitch-level and per-run derived data backing the replay/play-by-play/linescore endpoints).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `store_sim_run(conn, game_pk, summary, n_iterations, base_seed) -> run_id (async/Postgres)` | Persists one Monte-Carlo run's already-to_jsonable summary; returns the run_id every other store call keys on. | `api/routes/games.py (after a /simulate run)` | — |
| `load_latest_sim_run / load_sim_run / list_sim_runs (async/Postgres)` | Read back a specific or the most-recent run, or a run-history list, for a game_pk. | `api/routes/games.py` | — |
| `load_live_game_state(conn, game_pk) (async/Postgres)` | Reads the live sim.lineup_state row written by pipeline/live/live_ingestion_pipeline.py's _upsert_lineup_state. | `api/routes/games.py` | — |
| `store_play_stream(con, game_pk, run_id, play_entries) / load_play_stream(con, game_pk, run_id=None) (sync/DuckDB)` | Persist/read the per-pitch replay stream (sim.play_stream), normalizing each entry against the documented PLAY-ROW SCHEMA. | `api/routes/games.py` | — |
| `store_state_snapshots / load_state_at / load_state_snapshots (sync/DuckDB)` | SIM-357: per-pitch field/baserunner/count state snapshots backing GET .../state/{at_bat}/{pitch}. | `api/routes/games.py` | — |
| `store_game_card / load_game_card (sync/DuckDB)` | SIM-362/364: persists the per-run linescore + pitcher-decisions 'game card' at RECORD time, because it depends on PlayResult.next_state, which the persisted play-stream rows drop and which therefore CANNOT be re-derived later from load_play_stream alone. | `api/routes/games.py` | — |

**Depends on:** `an asyncpg-or-mock connection (Postgres functions)`, `a duckdb-or-mock connection (DuckDB functions)`

**Used by:** `api/routes/games.py (the /simulate, /plays, /state, /linescore, /decisions|/card endpoints)`

> **Notes for anyone changing this file:** This module does zero domain logic and zero connection/transaction management by design — every function is exactly one round trip against an already-open connection, which is what makes it unit-testable against a stub connection. store_game_card/load_game_card MUST be written at record time, not derived later, per the module's own architectural note. run_id is the join key across every store; a fresh run_id per run is the caller's ONLY idempotency mechanism — there is no upsert-by-natural-key on the DuckDB tables, so re-using a run_id raises a PK-violation.

---

### `db/migrations/`

Two independent, parallel migration systems for the platform's two databases: Alembic (db/migrations/versions/*.py, env.py reads BASEBALL_DB_DSN) manages the PostgreSQL raw.*/sim.* schema; a flat numbered-SQL-file series (db/migrations/duckdb/000N_*.sql) manages the DuckDB analytical schema and is applied by hand or by an operational script rather than a migration tool.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `db/migrations/env.py` | Alembic's environment config: injects BASEBALL_DB_DSN into the SQLAlchemy URL (auto-adding the +psycopg2 driver suffix) and sets up offline/online migration run modes. | — | — |

**Depends on:** `alembic, sqlalchemy (Postgres side only)`, `the DuckDB side has no library dependency — files are executed directly as SQL`

**Used by:** ``make migrate` (Alembic, Postgres)`, `pipeline/batch/player_profile_computor.py's _run_schema_ddl (applies the base db/schemas/02_duckdb_schema.sql on first connect)`, `later numbered DuckDB migrations are applied manually/by an operational script per ticket`

**Environment flags read here:** `BASEBALL_DB_DSN (read by env.py to configure Alembic's target Postgres DB)`

> **Notes for anyone changing this file:** The current DuckDB schema version is tracked in db/schemas/duckdb_schema_version.txt (30 as of 2026-09-17, matching the newest file db/migrations/duckdb/0030_sim532_fielder_oaa_jump.sql — the six fielder columns for Savant's per-position outs above average and the outfield jump; 0029 added the twelve lead-distance columns on the three runner-side profile tables; a unit test, tests/unit/test_docs_duckdb_version.py, holds every doc's citation of this number to the version file). CLAUDE.md explicitly warns that a past sprint bumped a DuckDB migration file but forgot to bump this version file (and its sanity test) — always verify version-file == latest-migration-number after any DuckDB schema change. The Alembic head on disk is 0027_sim532_savant_oaa_and_jump.py (2026-09-17: the two Savant fielding landing tables `raw.savant_outs_above_average` — one row per player, season and PULLED position, with the hand split stored and unread — and `raw.savant_outfield_jump` — one row per outfielder-season: reaction, burst, route, jump, feet covered, plays; SIM-532) after 0026_sim531_savant_running_game.py (2026-09-16: the two Savant running-game landing tables `raw.savant_basestealing` and `raw.savant_pitcher_running_game`, pulled at n=1 — the runner's lead and jump, the pitcher's lead and jump allowed; SIM-531) after 0025_sim427_game_bullpen.py (2026-09-13: `raw.game_bullpen`, one row per pitcher the MLB box lists for a game — `listed` = started / pitched / bullpen — the SIM-427 real pen's source, written by the historical loader and backfilled by `scripts/load_official_boxscores.py --bullpen-only`; 2026-09-12: 0022 widens the raw.prop_odds.prop_stat CHECK constraint to the 15-market vocabulary — its DOWNGRADE DELETES the rows of the eight new markets before it restores the seven-value constraint; 0023 creates raw.game_player_stats, the official per-player box score; 0024 widens the raw.game_odds.market_type CHECK constraint to the 15 game markets and adds draw_ml, the tie price of a three-way segment moneyline — its downgrade DELETES the twelve new markets' rows) — trust the files under db/migrations/versions/, not any prose, for the current head. All three (0022, 0023, 0024) were applied to the live database on 2026-09-12 0025 on 2026-09-13, and 0026 on 2026-09-16 (the lead-distance run book); the SIM-532 run book's step 2 applies 0027 — trust `alembic current`, not this prose, for the live state; the reference DDL db/schemas/01_postgres_schema.sql carries the same 15-value CHECK and the raw.game_player_stats table. Several pipeline/batch/player_profile_computor.py code paths (the _play_events_* CTEs) probe at runtime for whether a given migration has been applied and degrade gracefully instead of assuming a fixed schema version; follow that same pattern when adding a new column that must stay backward-compatible with not-yet-migrated/rebuilt databases.

---
