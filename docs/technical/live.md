# Live service

The `live` service follows the day's games. Its code lives in `pipeline/live/`. It polls
the MLB schedule and watches each live game's push socket. After each signal it rebuilds
the game state over REST. It writes the game rows, the posted lineups and the odds, and it
publishes every browser-bound message on Redis. The app's bridge forwards those messages
to the browsers.

The service reads the league through `ingest.mlb.client.LeagueFeed` and builds its
schedule query with `ingest.mlb.schedule.schedule_params` ([Data ingestion](ingest.md)).
The odds code lives in `betting/` ([Betting](betting.md)); this page covers only the live
service's odds hooks. The table layouts are on the [database page](database.md).

| Part | File |
|---|---|
| The entry point | [`pipeline/live/run_live.py`](../../pipeline/live/run_live.py) |
| The pipeline, its app and the browser socket route | [`pipeline/live/live_ingestion_pipeline.py`](../../pipeline/live/live_ingestion_pipeline.py) |
| The pipeline's parts: the settings, the odds hooks, the state builder, the sockets | [`pipeline/live/ingestion/`](../../pipeline/live/ingestion/) |
| The published-lineup writer | [`pipeline/live/lineup_writer.py`](../../pipeline/live/lineup_writer.py) |
| The Redis bridge and the heartbeat | [`pipeline/live/broadcast.py`](../../pipeline/live/broadcast.py) |

## How it fits together

1. The compose `live` service runs `python -m pipeline.live.run_live`. It waits up to ten
   minutes for Postgres, then serves the live app on port 8001 (`LIVE_PORT`).
2. Every 30 seconds `_sync_live_games` reads the schedule from yesterday to tomorrow, in
   Eastern time, the league's day. The read goes through `LeagueFeed`.
3. Each schedule entry acts by its own state. A Preview game gets its pre-game odds
   (`_persist_pregame_odds`) and its posted lineup (`_write_preview_lineup`).
4. A game that turns Live gets its closing rows promoted (`_mark_closing_rows`) and its
   first-pitch time stamped (`on_game_live`). Then a socket watcher starts
   (`MLBGameWebSocket`), and the first refresh runs at once.
5. A live game with no stored lineup gets one from its feed (`_write_live_lineup`).
6. A watched game that turns Final loses its watcher, gets one last refresh and joins
   `_completed_games`.
7. The poll upserts every game that is not yet completed into `raw.games`. Then it writes
   the heartbeat.
8. A socket message is only a change signal. `_refresh_game_state` re-reads the full feed
   over REST; when the read fails, it takes the Redis copy.
9. The refresh builds the game state, writes `sim.lineup_state`, caches the feed and the
   state in Redis and runs the odds cycles. Then it broadcasts the update and signals the
   end of a plate appearance.
10. The service publishes each browser-bound message on the Redis channel
    `live:game:{game_pk}` (`RedisBroadcaster`). The app's `RedisBridge` forwards it to the
    browsers that watch the game.

**The odds hooks.** The live service loads every watched game's current lines at most once
a minute. A Preview game's lines load at most every ten minutes, and every minute inside 15
minutes of the start. The fetch runs on one vendor thread. The check and the write run on
the event loop (`betting.load_odds_to_db.write_offers`). At first pitch the poll stamps each
book's last pre-pitch row as `closing`. The service stores no opening line: the nightly
odds pass loads it ([the historical odds loader](betting.md#bettinghistorical_loaderpy)).

**One publisher.** The app mounts this folder's browser socket route (`ws_router`)
whatever else runs. `LIVE_PIPELINE_ENABLED` (default off) starts the pipeline inside the
app instead of the bridge (`api/startup.py`, `start_live_feed`). Never set it while the
`live` service runs: two publishers would double-write every table.

**Docker.** The `live` service has a 1 GB memory cap, and its health check reads `/health`.
[docker-compose.yml](../../docker-compose.yml) mounts `./pipeline`, `./ingest`, `./betting`,
`./db`, `./api`, `./simulation` and `./operations` into it, so an edit there needs no
image rebuild.

### What the service writes

| Store | What |
|---|---|
| `raw.games` | Each game of the three-day window, with its status and the schedule's fields; `first_pitch_at` once a game turns Live. |
| `raw.game_lineups` | The published or projected starting rows of a game the final box has not reached. |
| `raw.players` | A player the database does not know yet, before his lineup row (the foreign key). |
| `raw.game_odds`, `raw.prop_odds` | Every book's current rows, and the closing stamp at first pitch. |
| `sim.lineup_state` | Each live game's state, kept for 24 hours. |
| Redis | `game_feed:{game_pk}` and `game_state:{game_pk}`; `live:heartbeat` and `live:watching`; `resim_cooldown:{game_pk}`; the channel `live:game:{game_pk}`. |

---

## The modules

### `pipeline/live/run_live.py`

[Source](../../pipeline/live/run_live.py) · 71 lines · Tests: none

The `live` service's entry point.

| Function / class | What it does | Called from |
|---|---|---|
| `wait_for_postgres` | Returns once Postgres accepts a connection. It raises after `DB_WAIT_S` (600 seconds). | `main` |
| `main` | Reads `BASEBALL_DB_DSN` and `REDIS_URL`, waits for Postgres, builds `create_app(publish_to_redis=True)` and serves it with uvicorn on `LIVE_PORT` (default 8001). | the compose `live` service |

The service passes no simulation callback, so the end of a plate appearance is logged, not
simulated.

**Watch out:** never set `LIVE_PIPELINE_ENABLED` in the app while this service runs. Two
publishers would double-write every table.

### `pipeline/live/live_ingestion_pipeline.py`

[Source](../../pipeline/live/live_ingestion_pipeline.py) · 1116 lines · Tests: [test_live_pipeline.py](../../tests/unit/test_live_pipeline.py), [test_live_pipeline_bugs.py](../../tests/unit/test_live_pipeline_bugs.py), [test_live_pipeline_setup.py](../../tests/unit/test_live_pipeline_setup.py), [test_live_resimulate_cooldown.py](../../tests/unit/test_live_resimulate_cooldown.py), [test_live_bridge.py](../../tests/unit/test_live_bridge.py), [test_live_connection_manager.py](../../tests/unit/test_live_connection_manager.py), [test_live_game_state_builder.py](../../tests/unit/test_live_game_state_builder.py), [test_published_lineup_writer.py](../../tests/unit/test_published_lineup_writer.py), [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_odds_game_markets.py](../../tests/unit/test_odds_game_markets.py), [test_odds_provider_ingestion.py](../../tests/unit/test_odds_provider_ingestion.py), [test_odds_provider_pin.py](../../tests/unit/test_odds_provider_pin.py), [test_odds_vocabulary.py](../../tests/unit/test_odds_vocabulary.py), [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_api_main_wiring.py](../../tests/unit/test_api_main_wiring.py), [test_route_auth_coverage.py](../../tests/unit/test_route_auth_coverage.py), [test_venue_id_fallback.py](../../tests/unit/ingest/test_venue_id_fallback.py), [test_live_game_upsert.py](../../tests/integration/test_live_game_upsert.py), [test_live_pipeline_upsert.py](../../tests/integration/test_live_pipeline_upsert.py), [test_api_end_to_end.py](../../tests/integration/test_api_end_to_end.py)

`LiveIngestionPipeline`: the day's games, their live state and their odds. It mixes in the
game-odds and prop-odds cycles from `pipeline/live/ingestion/`. The file also holds
`create_app` (the live service's app), the browser socket route `game_state_ws` on
`ws_router`, and the `connection_manager` singleton.

| Function / class | What it does | Called from |
|---|---|---|
| `start` / `stop` | `start` opens the Postgres pool, Redis and the `LeagueFeed` reader, loads today's Final games into `_completed_games`, and starts the poller. `stop` closes them all and the vendor thread. | `create_app`'s lifespan; `api/startup.py` (with `LIVE_PIPELINE_ENABLED`) |
| `_sync_live_games` | The 30-second schedule poll (steps 2 to 7 above). It first drops the odds provider's cached facts of a postponed, suspended or made-up game. | `_schedule_poller` |
| `_refresh_game_state` | One update of one game (steps 8 and 9 above). A lock skips a signal while a refresh of the same game runs. | `MLBGameWebSocket`, `_sync_live_games` |
| `_fetch_feed` | The game's feed through `LeagueFeed`. On an `aiohttp.ClientError` it returns the Redis copy (`game_feed:{game_pk}`). | `_refresh_game_state`, the resimulate route |
| `_persist_pregame_odds` | Before first pitch: the game row first (the foreign key), then the game-odds cycle and the prop cycle. The prop players are the probable pitchers and the posted lineups. Its own clock per game reads ten minutes, or one minute inside 15 minutes of the start. | `_sync_live_games` |
| `_mark_closing_rows` / `on_game_live` | The first time the poll sees a game Live: promote each book's last pre-pitch row to `closing`, then record `raw.games.first_pitch_at` (the first write wins). A failure is logged; the nightly closing pass repairs it. | `_sync_live_games` |
| `_should_resimulate` / `_signal_resimulation` | True once per completed plate appearance (`about.isComplete`). The signal calls the simulation callback, or logs it when none is set. | `_refresh_game_state`, the resimulate route |
| `_upsert_game_record` / `_upsert_schedule_fields` | The `raw.games` row from the schedule entry: the date is the official date, and a missing venue is NULL. Then the schedule's fields through `parse_game`: the start, the double-header fields, the detailed state and the probable pitchers. | `_sync_live_games`, `_persist_pregame_odds`, `_write_preview_lineup` |
| `_upsert_lineup_state` | The game state in `sim.lineup_state`, live for 24 hours. | `_refresh_game_state` |
| `_write_preview_lineup` / `_write_live_lineup` | Hand a game to the published-lineup writer, then broadcast `lineup_published`. The preview path writes the game row first. | `_sync_live_games` |
| `_cache_to_redis` | `game_feed:{game_pk}` and `game_state:{game_pk}`: 60 seconds while live, an hour once Final. | `_refresh_game_state` |
| `game_state_ws` | The browser socket `/ws/games/{game_pk}`. It checks the login first, joins the game's subscribers and holds a bridge subscription while open. It pings after 30 seconds of silence. | `ws_router`, mounted by `create_app` and `api/main.py` |
| `create_app` | The live app: the pipeline's lifespan, `ws_router`, `/health` (503 when the heartbeat is absent or older than 90 seconds), `/api/pipeline/status` and `POST /api/games/{game_pk}/resimulate` with a 10-second cooldown per game. | `run_live.main` |

**Flags:** `BASEBALL_DB_DSN` and `REDIS_URL` (both required). `ODDS_API_KEY` (read by the
BettingPros provider). `LIVE_PIPELINE_ENABLED` (read by the app, not by this service).

**Watch out:** the app mounts `ws_router` from this module whether or not the pipeline runs
in its process. The poll skips a rescheduled or resumed entry, but it reads the entry first
to drop the odds provider's stale facts.

### `pipeline/live/ingestion/`

[Source](../../pipeline/live/ingestion/) · 7 modules, 1237 lines · Tests: [test_live_pipeline.py](../../tests/unit/test_live_pipeline.py), [test_live_pipeline_bugs.py](../../tests/unit/test_live_pipeline_bugs.py), [test_live_game_state_builder.py](../../tests/unit/test_live_game_state_builder.py), [test_live_connection_manager.py](../../tests/unit/test_live_connection_manager.py), [test_odds_writers.py](../../tests/unit/test_odds_writers.py), [test_odds_closing_rows.py](../../tests/unit/test_odds_closing_rows.py), [test_odds_vocabulary.py](../../tests/unit/test_odds_vocabulary.py)

The live pipeline's parts. The three odds modules are hooks: they keep the cadence clocks
and the first-pitch trigger. `betting.load_odds_to_db` does the fetch, the check, the write
and the closing stamp ([Betting](betting.md#bettingload_odds_to_dbpy)).

| Module | Lines | What it holds |
|---|---|---|
| [`base.py`](../../pipeline/live/ingestion/base.py) | 100 | `PipelineBase`: the shared state; `_off_loop`, a vendor read on the pipeline's one worker thread, off the event loop; `_odds_provider`, the provider set in `__init__`, else `odds_source()`; `_refusal_tally`; `_last_seen_available`, whether `raw.game_odds.last_seen_at` exists (Alembic 0029). |
| [`constants.py`](../../pipeline/live/ingestion/constants.py) | 61 | The settings: the MLB socket address, the schedule hydrate (`probablePitcher,lineups`), the 30-second poll, the socket reconnect backoff (2 to 60 seconds), the 10-second read time-out, the Redis lifetimes (60 seconds live, an hour once Final), the 10-second resimulate cooldown, the 60-second odds cadence and the pre-game cadences (600 seconds; 60 seconds inside 900 seconds of the start). |
| [`game_odds.py`](../../pipeline/live/ingestion/game_odds.py) | 100 | `GameOddsMixin`: every book's row of all fifteen game markets, at most once a minute per game (`_persist_game_odds_cycle`: `game_offers` on the vendor thread, then `write_offers`). `_fetch_odds` reads the graded book's moneyline for the broadcast and never stores it. `mark_closing_lines` calls `promote_closing_game_rows`. `_forget_odds_game` drops the provider's cached facts of one game. |
| [`prop_odds.py`](../../pipeline/live/ingestion/prop_odds.py) | 206 | `PropOddsMixin`: each player's role from the game state or, before first pitch, from the schedule's probable pitchers and posted lineups. A pitcher gets the pitcher markets, a hitter the batter markets, an unknown or two-way player both. The prop cycle runs on the same one-minute clock (`prop_offers`, then `write_offers`). `mark_closing_prop_lines` calls `promote_closing_prop_rows`. |
| [`odds_cycle.py`](../../pipeline/live/ingestion/odds_cycle.py) | 105 | The pre-game cadence (`pregame_odds_cadence_s`); the scheduled start of an entry (`_scheduled_start`); the test for a postponed, suspended or made-up game whose cached facts may be old (`_odds_facts_may_be_stale`); the JSON form of a broadcast row (`_jsonable_odds` drops the guard-only keys; `_json_default` writes a date as ISO-8601 text). |
| [`game_state_builder.py`](../../pipeline/live/ingestion/game_state_builder.py) | 499 | `GameStateBuilder.build`: one feed to the game-state JSON (inning, count, runners, lineups, bullpen with rest, bench, play history, linescore). The pipeline keeps one builder per game, and the builder parses only the new plays. The bullpen's rest is one database query per team. |
| [`mlb_websocket.py`](../../pipeline/live/ingestion/mlb_websocket.py) | 166 | `MLBGameWebSocket`: one game's MLB push socket. Any message is a change signal; the socket reconnects with a doubling backoff. `ConnectionManager`: the browser sockets per game, with a broadcast over a copy of the set. |

**Watch out:** the closing rows depend on the poll's own clock. Some keys have no closing
row at first pitch, such as a book that first posts in play. Such a key can take an in-play
row on a later promotion. A prop cycle with no players does not stamp its clock, so the next signal tries
again at once.

### `pipeline/live/lineup_writer.py`

[Source](../../pipeline/live/lineup_writer.py) · 362 lines · Tests: [test_published_lineup_writer.py](../../tests/unit/test_published_lineup_writer.py)

`PublishedLineupWriter` makes an upcoming game simulable. The simulator resolves a game
from `raw.game_lineups`, and the game loader writes that table only from a final box.
Without this writer, every upcoming game answers 503 to a simulation request. The writer
reads the league through the pipeline's `LeagueFeed`.

| Function / class | What it does | Called from |
|---|---|---|
| `on_preview` | When both lineups are posted, reads the feed once and writes the starting rows with `source = 'published'`. The schedule's lineups stand in when the box has none. Each side's probable pitcher becomes its `P` row. It writes again only when the lineups or the probables change (`lineup_signature`). It never raises. | `_write_preview_lineup` |
| `on_live` | A started game with no lineup row gets its starting rows from the feed's box. It writes once, when both sides are complete: nine batting slots and a pitcher. | `_write_live_lineup` |
| `_projected_rows` | With `LIVE_PROJECTED_LINEUPS=1` and both probables known: each team's last final lineup, against a starter of the same hand when one of the last ten games had one. Today's probable is `P`, and the rows carry `source = 'projected'`. | `_on_preview` |
| `_write_rows` | Replaces the game's published and projected rows in one transaction. | `_on_preview`, `_on_live` |
| `_ensure_players` | Adds a player the database does not know, from the people endpoint (the foreign key). A player with no known hand raises: the writer never guesses a hand. | `_write_rows` |

**Flags:** `LIVE_PROJECTED_LINEUPS` (default 0; production 0).

**Watch out:** the final box is the authority. The writer never touches a game that has
box rows. The game loader deletes the published and projected rows before its own insert.

### `pipeline/live/broadcast.py`

[Source](../../pipeline/live/broadcast.py) · 160 lines · Tests: [test_live_bridge.py](../../tests/unit/test_live_bridge.py)

The Redis bridge between the live service and the browsers. The channel stores nothing. A
browser that connects late gets the next message; the game page reads `/live` for the
current state.

| Function / class | What it does | Called from |
|---|---|---|
| `RedisBroadcaster` | Publishes each message on `live:game:{game_pk}`. A failed publish logs a warning; the next poll sends a new message. | `create_app`'s lifespan |
| `RedisBridge` | The app side: one subscription per watched game, counted by reference. The last browser to leave ends it. | `api/startup.py` (`start_live_feed`), `game_state_ws` |
| `write_heartbeat` / `heartbeat_age_s` / `heartbeat_status` | `live:heartbeat` (the poll time) and `live:watching` (the live games), each with a 90-second lifetime; the heartbeat's age; `ok`, `stale` or `absent`. | `write_heartbeat`: `_write_heartbeat`. `heartbeat_age_s`: the live `/health`. `heartbeat_status`: `api/main.py` (`/ready`) |

`api/routes/metrics.py` reads `HEARTBEAT_KEY` for its freshness gauge.
