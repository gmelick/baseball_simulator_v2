# API Layer

The API is a FastAPI app in `api/`. It serves the game slate, the real game from the league
feed, the simulations, the betting prices, the Data Lab and the similarity explorer. The
frontend calls it over REST and one WebSocket; Prometheus scrapes its `/metrics`. Every
route except the login, the health probes and the API docs needs a login.

## How it fits together

### The boot

`uvicorn api.main:app` imports [main.py](../../api/main.py), whose `create_app()` builds the
app without opening any connection. When the server starts, `lifespan()` runs these steps
in order:

1. `validate_environment()` ([env_check.py](../../api/env_check.py)) stops the boot when
   `BASEBALL_DB_DSN`, `REDIS_URL`, `SECRET_KEY` or `AUTH_PASSWORD` is missing
   (`AUTH_DISABLED=1` skips the last two).
2. `open_pg_pool()` ([state.py](../../api/state.py)) opens the main Postgres pool. The name
   resolver and the run registry (`SimJobRegistry`, [sim_jobs.py](../../api/sim_jobs.py))
   attach to it. The registry marks every run a dead process left queued or running as
   `failed('restart')`.
3. `open_redis_cache()` opens Redis and pings it. A failure stops the boot.
4. `LeagueFeed()` ([league_feed.py](../../api/league_feed.py)) attaches unless
   `SLATE_SCHEDULE_ENABLED=0`.
5. `startup.build_engines()` ([startup.py](../../api/startup.py)) builds the pitcher engine
   (a failure stops the boot) and all eleven similarity engines (a failure skips that
   engine).
6. `startup.load_calibration()` applies `/data/calibration.json` to the engines and sets the
   win-probability map. A missing file leaves the identity map.
7. `startup.open_assistant_client()`, `startup.open_read_only_pool()` and
   `startup.start_live_feed()` attach the optional resources.
8. The replay store opens when `REPLAY_PERSISTENCE_ENABLED` is on (`/data/replay.duckdb`).
9. `prime_park_factor_source()` (in `simulation/sim_kwargs.py`) reads the venue run factors
   and the manager profiles into memory and closes the analytics file. The lifespan logs the
   manager counts.
10. `startup.build_sim_runner()` pins the engine-artifact bundle version, loads the bundle's
    large arrays for shared memory and builds the one `BatchRunner`.
11. `_background_prewarm()` warms the worker pool as a background task. The boot never waits
    for it.
12. `startup.start_bundle_watch()` starts the task that restarts the app when the nightly
    rebuild publishes a new bundle version.

Shutdown runs `startup.close_resources()` (the reverse order), then closes the park-factor
source.

### A simulation request

1. The browser calls `POST /api/games/{game_pk}/simulate`. The router-level `require_auth`
   ([auth.py](../../api/auth.py)) checks the cookie or the key.
2. `start_simulation_run()` ([games.py](../../api/routes/games.py)) calls
   `_resolve_state_or_error()`, which resolves the lineup into a `GameState` and marks its
   park factor unresolved.
3. `_resolved_sim_kwargs()` looks the venue's park factor and both managers' profiles up and
   builds the simulator's keyword arguments.
4. `resolve_factory_ref()` names the machine factory. The `GameSpec` carries the factory and
   the kwargs.
5. `SimJobRegistry.submit()` writes a queued `sim.sim_runs` row, or returns the active run
   with the same key.
6. The job runs `BatchRunner.run_job()` in a worker thread. The runner plays the games in
   chunks on the worker pool and reports progress after each chunk.
7. `_persist_replay_artifacts()` records the run's representative game on the worker pool
   and stores it in the replay file.
8. The job writes the summary, the prop set and the inning grids on the row. The browser
   polls `GET /simulate/runs/{run_id}` and then reads `/boxscore`, `/props`, `/card` and
   `/plays`.

### The other request paths

- **The slate and the real game.** `GET /api/games/{date}` reads the league schedule through
  `LeagueFeed` and adds our stored data ([games_slate.py](../../api/routes/games_slate.py)).
  `/feed`, `/feed/plays` and `/feed/state` read one cached copy of the league's per-game feed
  ([games_feed.py](../../api/routes/games_feed.py)).
- **The prices.** `/api/betting/games/{game_pk}/edges` and `/signals` run (or read from the
  cache) a simulation, then price every side of the fifteen game markets
  ([betting.py](../../api/routes/betting.py), [betting_pricing.py](../../api/routes/betting_pricing.py)).
- **The Data Lab.** The SQL console, the assistant, the analytics, the player search and the
  schema tree run only on the read-only pool ([sql_safety.py](../../api/routes/sql_safety.py)).
- **The live socket.** `/ws/games/{game_pk}` forwards the `live` service's messages, which
  arrive on Redis through `RedisBridge`.

## The routes

The OpenAPI snapshot is [frontend/openapi.json](../../frontend/openapi.json). "Login" means a
valid session cookie, `X-API-Key` header or `Authorization: Bearer` key. "Read-only pool"
means the route answers 503 when the lifespan has no read-only pool.

### The game routes (`/api/games`, [games.py](../../api/routes/games.py) and its sub-routers)

| Method | Path | Returns | Access |
|---|---|---|---|
| GET | `/api/games/{date}` | The day's games (`GamesOnDateResponse`): the schedule, the lineup flag, the newest run's summary; `source` = `schedule`, `schedule_cached` or `db` | Login |
| GET | `/api/games/{game_pk}/status` | One game's identity, state, records, final score and newest summary | Login |
| GET | `/api/games/{game_pk}/feed` | The real game for a slate card: linescore, box scores, lineups, the live field | Login |
| GET | `/api/games/{game_pk}/feed/plays` | The real game's plate appearances and their pitches | Login |
| GET | `/api/games/{game_pk}/feed/state?at_bat=` | The real game at the start of a plate appearance (the what-if's base) | Login |
| GET | `/api/games/{game_pk}/live` | The live service's stored game state (`sim.lineup_state`) | Login |
| GET | `/api/games/{game_pk}/simulate` | An N-game Monte-Carlo summary, run now (or from the cache) | Login |
| POST | `/api/games/{game_pk}/simulate` | Queues a durable run (202, `SimRunModel`) | Login |
| GET | `/api/games/{game_pk}/simulate/runs/latest` | The game's newest pregame run, any state | Login |
| GET | `/api/games/{game_pk}/simulate/runs/{run_id}` | One run's state, progress and summary | Login |
| DELETE | `/api/games/{game_pk}/simulate/runs/{run_id}` | Cancels a run (409 once it finished) | Login |
| POST | `/api/games/{game_pk}/what-if` | Queues two runs from a plate appearance: as played and with the changes (202) | Login |
| GET | `/api/games/{game_pk}/what-if/{base_run_id}/{change_run_id}` | Both what-if runs, the real final and the score at the start | Login |
| POST | `/api/games/{game_pk}/sample-game` | Simulates and stores one game for the game page | Login |
| POST | `/api/games/{game_pk}/simulate/with_override` | A baseline batch and a roster-override batch at one seed, and their difference | Login |
| GET | `/api/games/{game_pk}/boxscore` | Every player's prop means over a run | Login |
| GET | `/api/games/{game_pk}/props/{player_id}/{prop}` | One player's prop distribution, the over / under at a line, and an edge report | Login |
| GET | `/api/games/{game_pk}/plays` | The stored game's pitch-by-pitch play stream (paged with `limit` / `offset`) | Login |
| GET | `/api/games/{game_pk}/state/{at_bat}/{pitch}` | The stored game's field at one pitch | Login |
| GET | `/api/games/{game_pk}/linescore` | The stored game's linescore | Login |
| GET | `/api/games/{game_pk}/decisions` | The stored game's winning, losing and saving pitchers | Login |
| GET | `/api/games/{game_pk}/card` | The linescore and the decisions together, with the run id and seed | Login |

### The betting routes (`/api/betting`, [betting.py](../../api/routes/betting.py) and its sub-routers)

| Method | Path | Returns | Access |
|---|---|---|---|
| GET | `/api/betting/games/{game_pk}/edges` | The edge report of every side of each requested game market; `result` once the game is final | Login |
| GET | `/api/betting/games/{game_pk}/signals` | The +EV bets (positive expected value), ranked by EV and sized by fractional Kelly | Login |
| GET | `/api/betting/games/{game_pk}/line-movement` | The opening-to-closing series per side and book | Login |
| GET | `/api/betting/games/{game_pk}/clv` | The series that carry a closing-line value | Login |
| GET | `/api/betting/games/{game_pk}/card-odds` | The graded book's stored moneyline, run line and total for a slate card | Login |
| GET | `/api/odds/{game_pk}` | Mock odds for one game (`MockOddsAPI`) | Login |
| GET | `/api/odds/today/all` | Mock odds for every game on today's league schedule | Login |

### The Data Lab and the similarity explorer

| Method | Path | Returns | Access |
|---|---|---|---|
| POST | `/api/sql/run` | The rows of one validated read-only query on `raw.*` | Login; read-only pool |
| GET | `/api/schema` | The `raw.*` tables and columns | Login; read-only pool |
| GET | `/api/players/search?q=` | The name typeahead over `raw.players` | Login; read-only pool |
| GET | `/api/players/{player_id}` | A player's identity and the seasons on file | Login; read-only pool |
| GET | `/api/analytics/kpis`, `/by-season` | The store's coverage counts | Login; read-only pool |
| GET | `/api/analytics/pitch-type-mix`, `/pa-outcomes`, `/count-state`, `/velo-distribution`, `/zone`, `/leaderboard/pitchers`, `/leaderboard/batters` | Season-bounded summaries of `raw.pitches` | Login; read-only pool |
| GET | `/api/data/freshness` | The ingest watermark and the coverage per season | Login; read-only pool |
| GET | `/api/assistant/status` | Whether the assistant has an Anthropic client, and its model | Login |
| POST | `/api/assistant/ask` | A server-sent event stream of the assistant's text, queries and results | Login; read-only pool |
| GET | `/api/similarity/pitcher/{pitcher_id}/{season}` | The pitcher engine's score distribution, binned, with the top matches | Login |
| GET | `/api/similarity/engines` | The eleven engines and their build status | Login |
| GET | `/api/similarity/{engine}/meta` | One score engine's sub-scores and weights | Login |
| GET | `/api/similarity/{engine}/query` | One subject's ranked matches and score histogram | Login |
| GET | `/api/similarity/{engine}/pair` | One subject-versus-match breakdown | Login |
| POST | `/api/similarity/situation/query` | The nearest historical game states | Login |

### The session, the probes and the socket

| Method | Path | Returns | Access |
|---|---|---|---|
| POST | `/auth/login` | Sets the `sim_session` cookie for the right password | Open |
| GET | `/auth/me` | The caller's login state; never 401 | Open |
| POST | `/auth/logout` | Clears the cookie | Open |
| GET | `/health` | Liveness: `status`, `auth_enforced` and the read-only pool's state | Open |
| GET | `/ready` | Readiness: Postgres, Redis, the live service's heartbeat and the pitcher engine | Open |
| GET | `/` | The service name | Open |
| GET | `/docs`, `/redoc`, `/openapi.json` | FastAPI's own API docs | Open |
| GET | `/metrics` | The Prometheus series (not in the OpenAPI schema) | Login |
| WS | `/ws/games/{game_pk}` | The live game's messages; a ping every 30 seconds | Handshake check |

## Logging in

[auth.py](../../api/auth.py) holds the one credential check. `require_auth` and
`authenticate_websocket` both call `_authenticate()`, which tries the session cookie first,
then an API key.

- **The session cookie.** `POST /auth/login` compares the password with `AUTH_PASSWORD` and
  sets an httpOnly `sim_session` cookie, SameSite strict. The cookie holds a token signed
  with HMAC-SHA256 (a keyed hash) under `SECRET_KEY`, so the server keeps no session store.
  The token expires after `SESSION_TTL_HOURS` (default 8). The cookie is `Secure` outside
  `ENVIRONMENT=development`, so plain HTTP needs the development setting.
- **The API key.** A key from `API_KEYS` (comma-separated) arrives in `X-API-Key`, or as
  `Authorization: Bearer <key>`. Prometheus sends its key the second way. The comparison
  checks every configured key in constant time.
- **The opt-out.** `AUTH_DISABLED=1` turns every check off. The boot logs a warning, and
  `/health` reports `auth_enforced: false`. The test suite runs this way.
- **The WebSocket.** FastAPI gives a WebSocket handler no `Request`, so `require_auth` cannot
  guard the socket. The handler calls `authenticate_websocket()` first; it closes a refused
  handshake with code 1008 before it accepts it.

Every data router carries `require_auth` at the router level, so a new route on one of them
is gated with no line of its own. `auth_router` and `ws_router` are open on purpose.

The rate limiter (`RateLimitMiddleware`) is off unless `RATE_LIMIT_PER_MINUTE` is above 0. It
counts a request against a configured API key, else against the client address. Behind a
proxy that `TRUSTED_PROXY_HOSTS` names (compose sets `nginx`), the client address comes from
`X-Real-IP`. The limiter keeps its counts in process memory, so it suits one server process.

## The data handles

| Handle | Opened by | Role and scope | Used by |
|---|---|---|---|
| `app.state.pg_pool` | `open_pg_pool(BASEBALL_DB_DSN)` | `baseball_app`: reads and writes `raw` and `sim`, no DDL (schema changes) | The game and betting routes, the run registry, the name resolver, `/metrics` |
| `app.state.pg_pool_ro` | `startup.open_read_only_pool(BASEBALL_DB_RO_DSN)` | `baseball_ro`: reads `raw` only; `search_path = raw`; refused when the role is a superuser, can use `sim` or can insert into `raw.pitches` | The SQL console, the assistant, the analytics, the player search, the schema tree, the data freshness |
| The analytics DuckDB file | The engine builds and the park-factor prime | Opened read-only, read, and closed. The app never holds the file, so the nightly rebuild can write it | `api.state.build_*`, `simulation.sim_kwargs.prime_park_factor_source` |
| `app.state.replay_duckdb` | `db.sim_store.open_replay_store(REPLAY_DUCKDB_PATH)` | A DuckDB file of its own (`/data/replay.duckdb`), held writable by the app alone | The replay writes and reads in `games.py` |
| `app.state.redis_client` | `open_redis_cache(REDIS_URL)` | The similarity payload cache, the live bridge, the live heartbeat | `similarity.py`, `startup.start_live_feed`, `/ready` |
| `app.state.sim_cache` | `create_app()` through `simulation.batch_runner.make_cache()` | Redis when a server answers, else an in-process cache; typed JSON only | `BatchRunner` (summaries for 60 s), the slate and feed caches |

## The app.state map

The lifespan and `create_app()` attach every shared resource to `app.state`; a route reads it
off `request.app.state`. A renamed attribute breaks its routes with no import error.

| Attribute | Set by | Read by |
|---|---|---|
| `pg_pool`, `player_name_resolver` | `lifespan` | Most game and betting routes; `similarity.get_name_resolver` |
| `sim_jobs` | `lifespan` | `games_runs._get_sim_jobs`, `startup.start_bundle_watch` |
| `redis_client`, `similarity_cache` | `lifespan` | `/ready`, `similarity.get_similarity_cache` |
| `league_feed` | `lifespan` | `games_slate._slate_schedule_payload`, `games_feed._league_game_payload` |
| `pitcher_engine`, `engines` | `startup.build_engines` | `similarity.get_pitcher_engine`, `similarity_explorer._engines` |
| `calibration_report`, `calibration_map` | `startup.load_calibration` | `betting._summary_and_winprob` |
| `anthropic_client` | `startup.open_assistant_client` | `ai_assistant._client` |
| `pg_pool_ro`, `sql_console_role` | `startup.open_read_only_pool` | `_common._get_ro_pool`, `ai_assistant._pool`, `/health` |
| `pipeline` or `live_bridge` | `startup.start_live_feed` | The WebSocket handler (`live_bridge.retain` / `release`) |
| `replay_duckdb` | `lifespan` | `games._get_replay_store` |
| `park_factor_source` | `lifespan`, through `prime_park_factor_source`, which also loads the manager profiles; the lifespan logs the manager-season and league-row counts, or a warning when it finds none | `ParkFactorSourceMiddleware` |
| `sim_runner`, `bundle_version`, `bundle_in_use_marker` | `startup.build_sim_runner` | `games._build_runner`, `startup.close_resources` |
| `prewarm_task`, `bundle_watch_task` | `lifespan`, `startup.start_bundle_watch` | `startup.close_resources` |
| `sim_cache` | `create_app` | `_common._get_sim_cache` |
| `api_p95_seconds`, `request_count`, `sim_latency_last_seconds` | `LatencyMiddleware`, `metrics.record_request`, `metrics.record_sim_latency` | `/metrics` |
| `finals_newest_date`, `live_heartbeat_ts`, `backup_ages` and their check times | `metrics._schedule_finals_refresh`, `metrics._backup_ages` | `/metrics` |

A test sets two more: `sim_factory_ref` (the machine factory) and `sim_duckdb` (a fake
analytics connection for the park-factor and manager-profile lookups). Production sets neither,
so a production route reads both lookups from the boot snapshot.

## The app and its boot

### `api/main.py`

[Source](../../api/main.py) · 487 lines · Tests: [test_api_main_wiring.py](../../tests/unit/test_api_main_wiring.py), [test_route_auth_coverage.py](../../tests/unit/test_route_auth_coverage.py), [test_replay_store.py](../../tests/unit/test_replay_store.py), [test_sim_kwargs.py](../../tests/unit/test_sim_kwargs.py), [test_openapi_snapshot.py](../../tests/unit/test_openapi_snapshot.py)

The app factory and the lifespan. `app = create_app()` at the bottom is what uvicorn serves.

| Function / class | What it does | Called from |
|---|---|---|
| `create_app()` | Adds the CORS, rate-limit, latency and park-factor middleware; includes every router (auth, similarity, games, betting, data freshness, the WebSocket and odds routers, metrics, the Data Lab, the similarity explorer, the assistant); adds `/health`, `/ready` and `/`; mounts the built frontend when `frontend/dist` exists; installs the 500 handler. Opens no connection. | Module bottom; `scripts/export_openapi.py`; the tests |
| `lifespan(app)` | The boot steps and the shutdown above. | FastAPI, when the server starts |
| `_background_prewarm(runner, pool_dir)` | Runs `BatchRunner.prewarm` in a thread and logs the outcome. With a pinned bundle version it passes no pool folder, so the workers find the bundle through the pin. | `lifespan` |
| `health()` / `ready()` / `root()` | The probes. `/ready` answers 503 when Postgres or Redis fails; the live heartbeat and the pitcher engine are informational. | The compose health check (`/health`); nginx proxies both probes |

**Flags:** `SLATE_SCHEDULE_ENABLED` (default on), `REPLAY_PERSISTENCE_ENABLED` (default off;
production 1), `REPLAY_DUCKDB_PATH`, `BASEBALL_PLAY_POOL_DIR`.

**Watch out:** Tests patch `open_pg_pool`, `open_redis_cache` and `_background_prewarm` on
this module, so those calls stay here. `test_replay_store.py` reads this file's AST: the
lifespan sets `app.state.replay_duckdb` and never `app.state.sim_duckdb`.
`test_sim_kwargs.py` reads it too: the park-factor prime stays outside the
`if replay_enabled:` branch. The SPA catch-all `/{full_path:path}` must stay after every API
route; Starlette matches routes in registration order.

### `api/startup.py`

[Source](../../api/startup.py) · 457 lines · Tests: [test_api_main_wiring.py](../../tests/unit/test_api_main_wiring.py), [test_worker_prewarm.py](../../tests/unit/test_worker_prewarm.py), [test_sql_console_pool.py](../../tests/unit/test_sql_console_pool.py), [test_bundle_worker_and_boot.py](../../tests/unit/test_bundle_worker_and_boot.py)

The lifespan's steps that no test patches, one function per step. They log under `api.main`,
so the boot reads as one log.

| Function / class | What it does | Called from |
|---|---|---|
| `build_engines(app)` | Builds the pitcher engine and all eleven engines in a thread. `SIMILARITY_ENGINE_ENABLED=false` skips both; the similarity routes then answer 503. | `lifespan` |
| `load_calibration(app)` | Loads the calibration report, applies it to every engine with `apply_calibration`, and builds `CalibrationMap.from_report`. | `lifespan` |
| `open_assistant_client(app)` | Creates `anthropic.AsyncAnthropic()` when `ANTHROPIC_API_KEY` is set. | `lifespan` |
| `open_read_only_pool(app, open_pg_pool)` | Opens the read-only pool with `search_path = raw`, runs `check_read_only_role`, and keeps the pool only when the role is acceptable. Logs a warning when the role cannot read `raw.pitches`. | `lifespan` |
| `start_live_feed(app, dsn, redis_url)` | With `LIVE_PIPELINE_ENABLED` (default off) it runs the live pipeline in this process. Otherwise it attaches `RedisBridge`, which forwards the `live` service's Redis messages to the browsers. | `lifespan` |
| `build_sim_runner(app)` | Pins the bundle version (`pin_bundle_version`) before the pool starts, loads `EngineArtifacts` and publishes its large arrays to shared memory, then builds the `BatchRunner`. A missing bundle never stops the boot; each worker then fails at its first game. | `lifespan` |
| `start_bundle_watch(app)` | Starts `watch_for_new_bundle` when `SIM_RESTART_ON_NEW_BUNDLE` is on and a version is pinned. | `lifespan` |
| `close_resources(app)` | Stops the watch and undoes the pin, cancels the pre-warm wait, closes the runner, the jobs, the bridge, the league reader, Redis, both pools, the in-process pipeline and the replay store. Each close is guarded. | `lifespan` |

**Flags:** `SIM_RUNNER_WORKERS` (default unset = min(CPU count − 1, 10); `1` runs in this
process), `SIM_RESTART_ON_NEW_BUNDLE` (default off; production 1), `SIMILARITY_ENGINE_ENABLED`,
`LIVE_PIPELINE_ENABLED`, `ANTHROPIC_API_KEY`, `BASEBALL_DB_RO_DSN`.

**Watch out:** Each step imports its heavy modules inside the function, so a test's patch of
the source module reaches it. Never set `LIVE_PIPELINE_ENABLED` on the compose stack: the
`live` service is the one publisher.

### `api/env_check.py`

[Source](../../api/env_check.py) · 68 lines · Tests: [test_route_auth_coverage.py](../../tests/unit/test_route_auth_coverage.py)

| Function / class | What it does | Called from |
|---|---|---|
| `validate_environment()` | Raises `RuntimeError` when `BASEBALL_DB_DSN` or `REDIS_URL` is missing, then when `SECRET_KEY` or `AUTH_PASSWORD` is blank. A blank value counts as missing. With `AUTH_DISABLED=1` it logs one warning and skips the auth check. | `lifespan` (first step) |

### `api/state.py`

[Source](../../api/state.py) · 471 lines · Tests: [test_api_state.py](../../tests/unit/test_api_state.py), [test_bundle_worker_and_boot.py](../../tests/unit/test_bundle_worker_and_boot.py), [test_calibration_fit_and_apply.py](../../tests/unit/test_calibration_fit_and_apply.py), [test_similarity_endpoint.py](../../tests/unit/test_similarity_endpoint.py)

The builders of the resources the lifespan attaches. Each is a plain function, so a test runs
it without the app.

| Function / class | What it does | Called from |
|---|---|---|
| `ENGINE_REGISTRY` | The eleven engines: name → (module, class). The names key `app.state.engines`. | `build_all_engines` |
| `build_pitcher_engine(duckdb_path)` | Builds `PitcherSimilarityEngine` from `BASEBALL_DUCKDB_PATH`. A failure is fatal. | `startup.build_engines` |
| `build_all_engines(duckdb_path, registry, loader)` | Builds every registry engine; an engine that fails is logged and left out. | `startup.build_engines` |
| `retry_on_duckdb_lock(fn, what)` | Retries a build every 15 seconds while a DuckDB writer holds the file, up to `SIM_BOOT_LOCK_WAIT_S` (default 1,800). Any other error rises at once. | The two engine builders |
| `load_calibration_report(path)` | Reads `CALIBRATION_REPORT_PATH` into `similarity.calibration.report.CalibrationReport`; None when the file is missing or bad. | `startup.load_calibration` |
| `apply_calibration_to_engines(engines, report)` | Calls `apply_calibration(report)` on each engine that has it; returns the names. The three distance engines have none. | `startup.load_calibration` |
| `make_pg_name_resolver(pool)` | An async closure: player ids → names from `raw.players`, in one query. | `lifespan` |
| `RedisSimilarityCache`, `make_cache_key`, `CACHE_TTL_SECONDS` | The similarity payload cache: JSON under `simviz:pitcher:...`, 24 hours. A value that does not decode reads as a miss. | `similarity.py` |
| `open_redis_cache(redis_url)` | Opens the Redis client and pings it. | `lifespan` |
| `open_pg_pool(dsn, ...)` | An asyncpg pool (2 to 10 connections); `server_settings` sets session parameters. | `lifespan`, `startup.open_read_only_pool` |
| `redact_dsn(dsn)` | A DSN with its password masked, for a log line. | `startup.open_read_only_pool` |

**Flags:** `SIM_BOOT_LOCK_WAIT_S` (default 1,800 seconds), `BASEBALL_DUCKDB_PATH`,
`CALIBRATION_REPORT_PATH` (production `/data/calibration.json`).

**Watch out:** `similarity_explorer.SCORE_ADAPTERS` and `DISTANCE_ENGINES` repeat the registry
names by hand; an engine added here and not there is built but never shown.

### `api/bundle_watch.py`

[Source](../../api/bundle_watch.py) · 153 lines · Tests: [test_bundle_versions.py](../../tests/unit/test_bundle_versions.py), [test_bundle_worker_and_boot.py](../../tests/unit/test_bundle_worker_and_boot.py), [test_api_main_wiring.py](../../tests/unit/test_api_main_wiring.py)

The app picks up a nightly rebuild by restarting itself. The bundle path
`/data/play_pool/engine_artifacts` is a link to a version folder
(`pipeline/batch/bundle_versions.py`).

| Function / class | What it does | Called from |
|---|---|---|
| `pin_bundle_version(link)` | Resolves the link, sets `BASEBALL_ENGINE_ARTIFACT_DIR` for the workers and marks the version in use, so pruning keeps it. | `startup.build_sim_runner` |
| `watch_for_new_bundle(link, loaded, active_jobs, ...)` | Looks at the link every 60 seconds. On a new version it waits for the run jobs (at most 600 seconds), then for any DuckDB writer, then calls `restart_container()`. | `startup.start_bundle_watch` |
| `restart_container()` | Sends SIGTERM to PID 1; Docker's `restart: unless-stopped` starts the app again. | `watch_for_new_bundle` |
| `duckdb_writer_free(path)` | True when a read-only open of the analytics file succeeds. | `watch_for_new_bundle` |

**Watch out:** A restart, not an in-place swap, is an owner ruling (the nightly rebuild that
reaches users, SIM-563). The pin must happen before the worker pool starts: the forkserver's
workers inherit the environment.

### `api/league_feed.py`

[Source](../../api/league_feed.py) · 65 lines · Tests: [test_slate_endpoint.py](../../tests/unit/test_slate_endpoint.py), [test_game_feed.py](../../tests/unit/test_game_feed.py), [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py)

The app's reader of the league's public endpoints, over one lazy `aiohttp` session.

| Function / class | What it does | Called from |
|---|---|---|
| `LeagueFeed.schedule_payload(start, end)` | The raw schedule response for a date range. Raises on any failure. | `games_slate._slate_schedule_payload` |
| `LeagueFeed.game_feed_payload(game_pk)` | One game's live feed with `hydrate=alignment` (the fielders at every plate appearance). | `games_feed._league_game_payload` |

**Flags:** `SLATE_FEED_TIMEOUT_S` (default 5 seconds).

### `api/sim_jobs.py`

[Source](../../api/sim_jobs.py) · 365 lines · Tests: [test_sim_jobs.py](../../tests/unit/test_sim_jobs.py), [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py)

The run registry (the run jobs of the live slate, SIM-519). A run is a `sim.sim_runs` row
whose state goes queued → running → done, failed or cancelled.

| Function / class | What it does | Called from |
|---|---|---|
| `SimJobRegistry.submit(...)` | Keys the run by the SHA-256 of `kind` plus the runner's cache key (spec, seed, N). An active run with the key comes back with `existing = True`; a partial unique index holds the rule across processes. Otherwise it inserts a queued row and starts a task. `kind` is `pregame`, `whatif_base` or `whatif_change`. | `games.start_simulation_run`, `games.start_what_if` |
| `SimJobRegistry._run` / `_play` | Waits on a semaphore of `SIM_JOB_MAX_CONCURRENT` slots, marks the row running, runs `BatchRunner.run_job` in a thread with a progress callback and a cancel check, records the representative game, and writes the summary, the prop set and the inning grids. | `submit` |
| `SimJobRegistry.get` / `latest` / `prop_set_row` | Row reads. `latest` and the newest prop set read `kind = 'pregame'` only. | `games_runs`, `games` |
| `SimJobRegistry.cancel` | A queued run stops at once; a running run stops after its current chunk and keeps what it played. | `games_runs.cancel_simulation_run` |
| `SimJobRegistry.recover_orphans` | Marks every queued or running row `failed('restart')`. | `lifespan` |
| `SimJobRegistry.active_count` | The jobs in flight in this process; the bundle restart waits for 0. | `startup.start_bundle_watch` |

**Flags:** `SIM_JOB_MAX_CONCURRENT` (default 1).

### `api/auth.py`

[Source](../../api/auth.py) · 655 lines · Tests: [test_api_auth.py](../../tests/unit/test_api_auth.py), [test_api_auth_session.py](../../tests/unit/test_api_auth_session.py), [test_route_auth_coverage.py](../../tests/unit/test_route_auth_coverage.py), [test_trusted_proxy.py](../../tests/unit/test_trusted_proxy.py)

The security primitives: the login check, the rate limit, the latency window and the CORS
list. The section [Logging in](#logging-in) explains the model.

| Function / class | What it does | Called from |
|---|---|---|
| `require_auth(request)` | The router-level dependency: returns `cookie:<sub>` or `api_key:key`, else raises 401 with `WWW-Authenticate`. | Every data router |
| `authenticate_websocket(websocket)` | The same check on a handshake; closes with 1008 on failure. | `game_state_ws` in `pipeline/live/live_ingestion_pipeline.py` |
| `_authenticate(cookies, headers)` / `Principal` | The shared core: cookie first, then key. Never raises. | `require_auth`, `authenticate_websocket`, `routes/auth.me` |
| `_auth_disabled()` | The one opt-out, `AUTH_DISABLED`. | `require_auth`, `authenticate_websocket`, `env_check`, `/health`, `routes/auth.me` |
| `_mint_session_token` / `_verify_session_token` | The signed token: `<payload>.<signature>`, with `sub`, `iat` and `exp`. | `routes/auth.login`, `_authenticate` |
| `cookie_kwargs(max_age)` | The cookie settings: httpOnly, SameSite strict, `Secure` outside development. | `routes/auth.login` / `logout` |
| `RateLimitMiddleware` | The sliding-window limiter; 429 with `Retry-After`. `/health`, `/ready` and `/` are exempt. | `create_app` |
| `_client_address(request)` / `_trusted_proxy_addresses()` | The client address, from `X-Real-IP` only when the connection comes from a trusted proxy. The proxy names resolve at most once a minute. | `RateLimitMiddleware._client_key` |
| `LatencyMiddleware` | Keeps the p95 latency of the last 200 requests on `app.state.api_p95_seconds` and counts each request. `/metrics` is exempt. | `create_app` |
| `resolve_cors_origins()` | `CORS_ORIGINS`, else `FRONTEND_URL`, else three localhost origins in development, else one. Never the wildcard by default. | `create_app` |

**Flags:** `AUTH_DISABLED`, `SECRET_KEY`, `AUTH_PASSWORD`, `SESSION_TTL_HOURS` (default 8),
`API_KEYS`, `RATE_LIMIT_PER_MINUTE` (default 0 = off), `RATE_LIMIT_ENABLED`,
`TRUSTED_PROXY_HOSTS` (production `nginx`), `CORS_ORIGINS`, `FRONTEND_URL`, `ENVIRONMENT`.

**Watch out:** There is no default secret or password: `_get_secret_key` and
`_get_auth_password` raise when the variable is blank. `tests/unit/test_route_auth_coverage.py`
walks `app.routes` and fails on any data route without the gate.

### `api/middleware.py`

[Source](../../api/middleware.py) · 49 lines · Tests: [test_sim_kwargs.py](../../tests/unit/test_sim_kwargs.py), [test_park_factor_snapshot.py](../../tests/unit/test_park_factor_snapshot.py)

| Function / class | What it does | Called from |
|---|---|---|
| `ParkFactorSourceMiddleware` | Stamps `X-Park-Factor-Source` on every response: `duckdb:<n>` (real venue factors, `n` rows), `unavailable` (every simulation ran at a neutral 1.0) or `unknown` (no lifespan ran). It reads the live verdict of `simulation.sim_kwargs` first, so the header follows a later re-read. | `create_app` |

### `api/errors.py`

[Source](../../api/errors.py) · 59 lines · Tests: [test_api_error_envelope.py](../../tests/unit/test_api_error_envelope.py)

| Function / class | What it does | Called from |
|---|---|---|
| `install_exception_handlers(app)` | Turns an unhandled exception into `{detail, error_type: "internal_error", request_id}` with status 500 and logs the traceback under that id. `HTTPException` and validation errors keep FastAPI's own shapes. | `create_app` |

### `api/serialization.py`

[Source](../../api/serialization.py) · 140 lines · Tests: [test_api_schemas.py](../../tests/unit/test_api_schemas.py), [test_api_game_card.py](../../tests/unit/test_api_game_card.py), [test_cache_codec.py](../../tests/unit/test_cache_codec.py), [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py)

| Function / class | What it does | Called from |
|---|---|---|
| `to_jsonable(obj)` | Walks any object graph and returns JSON-native types: numpy scalars and arrays, datetimes, enums, dataclass instances, mappings and sequences. Lossless; an unknown type falls back to `str`. | `api.schemas`, `games._build_replay_artifacts`, `sim_jobs._json`, `sql_safety.run_read_only_sql` |

**Watch out:** `np.float64` subclasses `float`, so the fast path excludes `np.generic` first.
Removing that check lets numpy scalars reach the wire.

## The response models

### `api/schemas/` (a package)

[Source](../../api/schemas/__init__.py) · 1,285 lines in 8 files · Tests: [test_api_schemas.py](../../tests/unit/test_api_schemas.py), [test_api_game_card.py](../../tests/unit/test_api_game_card.py), [test_inning_grid.py](../../tests/unit/test_inning_grid.py), [test_odds_live_pages.py](../../tests/unit/test_odds_live_pages.py)

One Pydantic model per simulation or betting dataclass, with JSON-native fields. Each has a
`from_dataclass` (or `from_jsonable`, `from_prop_set`) builder, so no numpy value reaches the
wire. `__init__.py` re-exports every public model: `from api.schemas import X`.

| Module | Models | Source dataclasses |
|---|---|---|
| [`_base.py`](../../api/schemas/_base.py) (20 lines) | `_ApiModel`: the base; forbids extra fields | — |
| [`summary.py`](../../api/schemas/summary.py) (171) | `ConfidenceIntervalModel`, `GameSimSummaryModel` (the per-iteration score arrays by default; `include_raw_arrays=False` drops them), `GameSimSummaryLite` (no arrays) | `simulation.results` |
| [`snapshots.py`](../../api/schemas/snapshots.py) (267) | `PlayerRefModel`, `FieldSnapshotModel`, `PlayByPlayEntryModel`, `PlayByPlayModel`, `StateAtPitchModel`, `MetricDeltaModel`, `OverrideDeltaModel` | `simulation.snapshots` |
| [`game_card.py`](../../api/schemas/game_card.py) (181) | `InningLineModel`, `LinescoreModel`, `PitcherDecisionsModel` (`from_jsonable` rebuilds a stored card) | `simulation.linescore`, `simulation.pitcher_decisions` |
| [`props.py`](../../api/schemas/props.py) (180) | `BoxscoreCardRowModel`, `BoxscoreCardModel` (`from_prop_set`; a side's placeholder relievers fold into one row, `GENERIC_PEN_IDS`), `PropEdgeResponse` | `simulation.prop_distributions` |
| [`markets.py`](../../api/schemas/markets.py) (313) | `CLVModel`, `EdgeReportModel` (with `price_book` and `result`), `BetSignalModel`, `LineQuoteModel`, `LineMovementModel` | `betting.clv_engine`, `betting.bet_signal`, `betting.line_movement` |
| [`live.py`](../../api/schemas/live.py) (57) | `LiveGameStateResponse` | The live service's `game_state` JSON |

**Watch out:** A model's docstring and fields are the OpenAPI schema. A change to either, or
to a route's `description=`, needs `scripts/export_openapi.py` run in the app image and
`npm run gen:api` in the same commit; `tests/unit/test_openapi_snapshot.py` fails on drift.

## The game routes

### `api/routes/games.py`

[Source](../../api/routes/games.py) · 1,677 lines · Tests: [test_api_games.py](../../tests/unit/test_api_games.py), [test_api_game_card.py](../../tests/unit/test_api_game_card.py), [test_api_games_plays.py](../../tests/unit/test_api_games_plays.py), [test_projections_on_pool.py](../../tests/unit/test_projections_on_pool.py), [test_api_end_to_end.py](../../tests/integration/test_api_end_to_end.py)

The aggregate `/api/games` router (prefix, tag, login), every route that runs a simulation,
the replay reads, and the seam those routes share. It includes `games_slate`, `games_feed` and
`games_runs`; each is a bare `APIRouter()`, so the OpenAPI contract does not depend on a
route's module.

| Function / class | What it does | Called from |
|---|---|---|
| `resolve_factory_ref(request)` | The machine factory's dotted name: `app.state.sim_factory_ref`, else `SIM_MACHINE_FACTORY_REF`, else `PRODUCTION_FACTORY_REF` (`simulation.production_factory:production_machine_factory`). | Every simulation route; `betting._summary_and_winprob` |
| `_resolve_state_or_error(pool, game_pk, fallback_rows)` | Resolves the lineup into a `GameState` and marks its park factor unresolved. An unpublished or incomplete lineup is 503 with `Retry-After: 900`; an unknown game is 404. | Every simulation route; `betting._summary_and_winprob`; `scripts/accuracy/` |
| `_resolved_sim_kwargs(request, pool, state, game_pk)` | Looks the venue's park factor and both managers' profiles up, then builds the sim kwargs (`simulation.sim_kwargs.build_sim_kwargs`). With no `app.state.sim_duckdb` (production) both lookups read the boot snapshot. The one way a route gets kwargs. | Every simulation route; `betting._summary_and_winprob` |
| `_build_runner(request)` / `_run_batch(...)` | The shared `app.state.sim_runner` (else a one-process runner for a test app), and the sync batch call a route runs in a thread. | Every simulation route; `betting._summary_and_winprob` |
| `simulate_game_endpoint` | `GET /simulate`: a batch now, the latency gauge, one stored game and a history row. | Clients, scripts |
| `start_simulation_run` | `POST /simulate`: queues a run job (503 before the run-job columns exist). | The game page |
| `start_what_if` | `POST /what-if`: the real game's state at the plate appearance, the changes applied (`simulation.whatif.apply_changes`; an illegal change is 422), and two runs at one random seed. A game with no stored lineup builds its base state from the feed. | The what-if panel |
| `simulate_sample_game` | `POST /sample-game`: one game at a seed, stored; returns the run id and seed. 503 with the replay store off; 500 when the store fails. | The game page |
| `simulate_with_override_endpoint` | `POST /simulate/with_override`: two batches at one seed and their `OverrideDelta`. | Clients only |
| `get_game_boxscore` | `GET /boxscore`: the prop means of every player from `BatchRunner.run_prop_set` on the worker pool, with name, side, batting slot and starter flag. `run_id` or `latest_run=true` reads a stored run instead. The batch's first game is stored at the same time. | The projections panel |
| `get_player_prop_edge` | `GET /props/{player_id}/{prop}`: one PMF (probability of each count), the over / under at `line`, and an edge report when both prices are given. A seeded run is cached, so it reads the `/boxscore` run. | The projections panel |
| `get_game_plays` / `get_game_state_at_pitch` / `get_game_linescore` / `get_game_decisions` / `get_game_card` | The replay reads: the newest complete run (a run with a game card) unless `/plays` names `run_id`. 404 when nothing is stored; 503 with the store off. | The game page |
| `_persist_replay_artifacts(...)` | Records one game with `BatchRunner.record_game` on the worker pool, builds its artifacts and stores them under the replay file's next run id. A `/simulate` batch also writes a Postgres history row. Never raises. | The four storing routes |
| `_build_replay_artifacts(recorded, resolved)` | One `RecordedGame` → the play-by-play, one state per thrown pitch (with the fielders when a lineup resolves), the linescore and the decisions. A pickoff that ends a half with no pitch makes no play-by-play entry. | `_persist_replay_artifacts` |
| `_store_replay_run(cur, ...)` | Writes the play stream, the states and the card in one transaction under `_REPLAY_WRITE_LOCK`, then prunes the game to its newest five runs. | `_persist_replay_artifacts` |
| `_player_tags` / `_player_names` / `_placeholder_names` | Each player's side, slot and starter flag from the sim kwargs; names from `raw.players`; "Generic reliever N" for the placeholder pen of a game with no box pen. | `/boxscore`, `/props`, `/plays` |

**Flags:** `SIM_MACHINE_FACTORY_REF` (default unset = production).

**Watch out:** Tests patch `resolve_game_state`, `_resolve_state_or_error`, `_build_prop_set`,
`_query_player_names`, `_build_replay_artifacts`, `PRODUCTION_FACTORY_REF` and `sim_store` on
this module, so those calls stay here. `test_sim_kwargs.py` counts the resolving sim-kwarg
calls in this file (8) and in `betting.py` (1); a new park-blind call site fails it.
`PRODUCTION_FACTORY_REF` stays a string: it is part of the cache keys.

### `api/routes/games_slate.py`

[Source](../../api/routes/games_slate.py) · 650 lines · Tests: [test_slate_endpoint.py](../../tests/unit/test_slate_endpoint.py), [test_api_games.py](../../tests/unit/test_api_games.py)

The day slate and one game's status card. The league schedule says which games exist; the
database adds the lineup flag and its source, the venue city and the newest run.

| Function / class | What it does | Called from |
|---|---|---|
| `get_games_on_date(date, request, use_cache)` | `GET /{date}`: the schedule's games (one card per game), merged with two stored reads (`_slate_merge_rows`). | The Daily Diamond slate |
| `_slate_schedule_payload(request, day, use_cache)` | The cache (`slate:v2:{date}`: 20 s today, 10 min ahead, 24 h past), then the feed, then the last good copy (`slate:last:{date}`). With all three missing, the stored listing from `raw.games` serves (`source = "db"`). | `get_games_on_date`, `_schedule_game_for` |
| `_schedule_card(g, enrich, run)` | One schedule game and our stored data as a `GameCard`. | `get_games_on_date` |
| `get_game_status_card(game_pk, request)` | `GET /{game_pk}/status`: identity, records, state, final score, the newest summary and the schedule's header fields. 404 for an unknown game. | The game page header |

**Watch out:** Each stored read falls back to its older form before the migration that added
its columns. A suspended game appears on its resume day too; the route keeps one card.

### `api/routes/games_feed.py`

[Source](../../api/routes/games_feed.py) · 397 lines · Tests: [test_game_feed.py](../../tests/unit/test_game_feed.py), [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py), [test_api_games.py](../../tests/unit/test_api_games.py)

The real game: one cached copy of the league's per-game feed, and the live service's stored
state.

| Function / class | What it does | Called from |
|---|---|---|
| `_league_game_payload(request, game_pk, use_cache)` | The raw feed, cached by the game's state (10 s live, 10 min scheduled, 24 h final). On a league error the last good copy serves (`feed_cached`); with none, the payload is None. | The feed routes; `games.start_what_if`; `games_runs.get_what_if`; `betting._actual_result` |
| `get_game_feed` | `GET /feed`: `pipeline.mlb_game_feed.parse_game_feed` → `GameFeedCardModel`. 503 with `Retry-After: 30` when no copy exists. | The open slate card |
| `get_real_plays` | `GET /feed/plays`: `parse_plays` → `RealPlaysModel`. | The real play-by-play panel |
| `get_pa_state` / `_start_state` / `_pa_state_model` | `GET /feed/state`: `pipeline.mlb_game_feed.state_at_pa` at a plate appearance (none = first pitch), as lineups, pitchers, defense, bench and pen. An unknown plate appearance is 404. | The what-if panel |
| `_feed_lineup_rows(payload)` | Both starting lineups as `raw.game_lineups` rows, built by `pipeline.etl.historical.game_values._build_starting_lineup_rows`. | `games.start_what_if` |
| `get_live_game_state` | `GET /live`: `db.sim_store.load_live_game_state` from `sim.lineup_state`. 404 with no live row. | The game page |

### `api/routes/games_runs.py`

[Source](../../api/routes/games_runs.py) · 172 lines · Tests: [test_sim_jobs.py](../../tests/unit/test_sim_jobs.py), [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py)

The reads of a game's runs. The routes that start a run live in `games.py`.

| Function / class | What it does | Called from |
|---|---|---|
| `get_latest_simulation_run` / `get_simulation_run` / `cancel_simulation_run` | `GET /simulate/runs/latest` (404 with none), `GET` and `DELETE /simulate/runs/{run_id}`. | The game page |
| `get_what_if` | `GET /what-if/{base}/{change}`: both runs, the real final when the game is final, and the score at the start. | The what-if panel |
| `_get_sim_jobs` / `_run_model` / `_stored_prop_set` | The registry (503 with none), a row as `SimRunModel`, a run's `PropDistributionSet` (404 with a hint). | The routes here; `games` |

**Watch out:** `runs/latest` stays above `runs/{run_id}`; the router matches in order.

### `api/routes/games_models.py`

[Source](../../api/routes/games_models.py) · 592 lines · Tests: [test_api_games.py](../../tests/unit/test_api_games.py), [test_game_feed.py](../../tests/unit/test_game_feed.py), [test_slate_endpoint.py](../../tests/unit/test_slate_endpoint.py), [test_inning_grid.py](../../tests/unit/test_inning_grid.py)

The request and response models of `/api/games`: the slate (`GameCard`,
`GamesOnDateResponse`, `GameCardAggregateResponse`), the feed (`GameFeedCardModel`,
`RealPlaysModel`, `PaStateModel` and their parts), the simulations (`SimulateResponse`,
`SampleGameResponse`, `RosterOverride`, `WithOverrideResponse`, `GameCardResponse`) and the
runs (`SimRunRequest`, `SimRunModel`, `WhatIfRequest`, `WhatIfStarted`, `WhatIfResult`).

| Function / class | What it does | Called from |
|---|---|---|
| `_sim_summary_lite_from_stored(summary)` | A stored summary → `GameSimSummaryLite`: drops the per-iteration arrays and inning grids, and fills each interval's `half_width` from its bounds. None on any failure. | `games_slate`, `games_runs` |

## The betting routes

### `api/routes/betting.py`

[Source](../../api/routes/betting.py) · 610 lines · Tests: [test_api_betting.py](../../tests/unit/test_api_betting.py), [test_segment_edges.py](../../tests/unit/test_segment_edges.py), [test_game_page_grading.py](../../tests/unit/test_game_page_grading.py), [test_live_run_lines.py](../../tests/unit/test_live_run_lines.py)

The aggregate `/api/betting` router, `/edges`, `/signals`, the simulation they price from and
the grading on the real final. It includes `betting_lines` and `betting_card`.

| Function / class | What it does | Called from |
|---|---|---|
| `_summary_and_winprob(request, ...)` | The game routes' seam: state, kwargs, batch (cached by spec, seed and N), and the win probability through `app.state.calibration_map`. | `get_game_edges`, `get_game_signals` |
| `get_game_edges` | `GET /edges`: the reports of the requested markets (all fifteen by default), with `odds_source`, the graded book, each side's offered-price book, the market names and each run line's pricing. Once final, each edge's `result` and the `final` score. | The Betting card |
| `get_game_signals` | `GET /signals`: the same reports gated to +EV by `betting.bet_signal.bet_signals_from_edges`, ranked by EV. The gate's knobs are query parameters. | The Betting card |
| `_actual_result` / `_grade` | The real final from `raw.games.inning_scores`, else the feed's linescore; one side's "won", "lost" or "push". | `get_game_edges` |

**Watch out:** `test_segment_edges.py` patches `_summary_and_winprob` here. `betting/` is baked
into the app image while `api/` hot-reloads: rebuild the image before an `api/` edit imports a
new `betting/` name.

### `api/routes/betting_pricing.py`

[Source](../../api/routes/betting_pricing.py) · 891 lines · Tests: [test_segment_edges.py](../../tests/unit/test_segment_edges.py), [test_odds_live_pages.py](../../tests/unit/test_odds_live_pages.py)

The edge math. A market's prices come from, in order: the injected query parameters or the
`prices` document, the stored lines, then the deterministic mock (`MockOddsAPI`, seeded on
the game).

| Function / class | What it does | Called from |
|---|---|---|
| `_build_edge_reports(summary, win_prob, ...)` | Prices every side of each market: the moneyline from the win probability, the total from the totals, the run line from the score margin, and the twelve segment and team markets from the inning grid. A run line is a pair when the away spread is the negative of the home spread; otherwise each side is a separate bet priced over the book's two-way margin (the run lines scored by shape, SIM-549). A side with a simulated probability of 0 or 1 is skipped. | `_priced_edge_reports` |
| `_priced_edge_reports(request, ...)` | Reads the stored rows when an uninjected market needs them, then builds the reports. | `betting.get_game_edges` / `get_game_signals` |
| `_parse_prices` / `_split_prices` | The `prices` JSON document: any fault is a 422 naming the market. A full-game market in it becomes its named parameters. | `betting` |
| `_parse_markets(markets)` | The comma-separated `markets` parameter; an unknown market is a 422. | `betting` |
| `_mock_odds` / `_at_offered_price` | The mock's prices (imported lazily from `pipeline.live.ingestion.mock_odds`); a report re-read at the best offered price (the EV moves, the edge stays). | `_build_edge_reports` |

**Watch out:** None of the twelve segment and team markets is calibrated. The signals still
fire on them (an owner ruling on the segment markets, SIM-546).

### `api/routes/betting_stored_odds.py`

[Source](../../api/routes/betting_stored_odds.py) · 467 lines · Tests: [test_odds_last_seen.py](../../tests/unit/test_odds_last_seen.py), [test_odds_live_pages.py](../../tests/unit/test_odds_live_pages.py), [test_segment_edges.py](../../tests/unit/test_segment_edges.py)

The stored price source: each sportsbook's latest usable one-book row (`bp:<id>`) in
`raw.game_odds` (one book per odds row, SIM-555).

| Function / class | What it does | Called from |
|---|---|---|
| `_read_stored_rows(request, game_pk, markets)` | Reads the game's status, then the latest row per market and book. A `Preview` game reads `closing` and `current` rows; any other game reads `closing` rows only. A failed read logs and returns nothing; the markets then use the mock. | `betting_pricing`, `betting_card` |
| `_latest_pregame_rows` / `_drop_withdrawn` | The same rules in Python: a closing row beats a current row of the same book; a current row not seen within 20 minutes (`CURRENT_ROW_MAX_AGE_S`) of the game's newest sighting is withdrawn. | `_read_stored_rows` |
| `_stored_market(stored, market)` | The graded book's row (the first book on `GRADED_BOOK_PREFERENCE`) gives the fair probability; each side's offered price is the best stored price at that line. | `betting_pricing` |
| `_stored_reference_margin` | One book's two-way margin for a run line listed as two separate bets; a segment run line reads its own segment's total first. | `betting_pricing` |

### `api/routes/betting_lines.py`

[Source](../../api/routes/betting_lines.py) · 172 lines · Tests: [test_api_betting.py](../../tests/unit/test_api_betting.py), [test_line_movement.py](../../tests/unit/test_line_movement.py)

| Function / class | What it does | Called from |
|---|---|---|
| `get_game_line_movement` | `GET /line-movement`: `betting.line_movement.fetch_line_movement` over the stored history, one series per side and book. `market_type` is `moneyline`, `runline` or `total` (else 422). 503 with no pool; there is no mock. | Clients |
| `get_game_clv` | `GET /clv`: the series that carry a closing-line value (CLV: the opening price against the closing price). A run-line side whose spread moved has none. | Clients |

### `api/routes/betting_card.py`

[Source](../../api/routes/betting_card.py) · 177 lines · Tests: [test_slate_card_odds.py](../../tests/unit/test_slate_card_odds.py)

| Function / class | What it does | Called from |
|---|---|---|
| `get_card_odds` | `GET /card-odds`: the graded book's moneyline, run line and total from the stored rows only, never the mock. 404 when none exists. On a final game, `settled` says how each line settled. | The slate card |
| `_card_odds_from_rows` / `_card_settlement` | The graded row of each market as the card's lines; the winning side of each line on the final score. | `get_card_odds` |

## The Data Lab and the similarity explorer

### `api/routes/sql_safety.py`

[Source](../../api/routes/sql_safety.py) · 699 lines · Tests: [test_sql_safety.py](../../tests/unit/test_sql_safety.py), [test_sql_console_pool.py](../../tests/unit/test_sql_console_pool.py)

The one read-only SQL path. The console and the assistant both run their SQL through it, so
the assistant can do nothing the console cannot.

| Function / class | What it does | Called from |
|---|---|---|
| `validate_read_only_sql(sql)` | Accepts one `SELECT` or `WITH … SELECT`. Rejects writes and DDL anywhere (a CTE included), stacked statements, dangerous functions (inside quoted identifiers too) and long text, then runs the relation allowlist. | `run_read_only_sql` |
| `validate_relation_allowlist(sql)` | Parses the query with sqlglot (a SQL parser). Every table must be `raw.<table>` or a CTE in scope. An unqualified name, a system catalog, `SELECT … INTO` and an unlisted table function are rejected. Fails closed. | `validate_read_only_sql` |
| `run_read_only_sql(pool, sql, max_rows, timeout_ms)` | Validates, wraps the query in a row-capped subquery, and runs it in a read-only transaction with `SET LOCAL statement_timeout`. Returns columns, rows, `truncated` and the time. | `sql_runner.run_sql`, `ai_assistant._run_conversation` |
| `check_read_only_role(pool)` / `ReadOnlyRoleCheck` | One query: the role, superuser, `sim` usage, insert and select on `raw.pitches`. `acceptable` = not a superuser, no `sim` usage, no insert. | `startup.open_read_only_pool` |

**Flags:** `SQL_RUNNER_MAX_ROWS` (1,000; ceiling 5,000), `SQL_RUNNER_TIMEOUT_MS` (5,000;
ceiling 30,000), `SQL_RUNNER_MAX_LEN` (20,000).

**Watch out:** A security-critical file. The dangerous-function scan must keep quoted
identifiers (`keep_identifiers=True`): `"pg_read_file"(...)` runs as `pg_read_file`. sqlglot
imports at the top on purpose: a missing package fails the boot instead of turning the
allowlist off.

### `api/routes/sql_runner.py`

[Source](../../api/routes/sql_runner.py) · 104 lines · Tests: [test_read_only_routes.py](../../tests/unit/test_read_only_routes.py), [test_sql_console_pool.py](../../tests/unit/test_sql_console_pool.py)

| Function / class | What it does | Called from |
|---|---|---|
| `run_sql(request, body)` | `POST /api/sql/run` on the read-only pool. A rejected query and a database error are both 400 with the message as `detail`. | The SQL console |

### `api/routes/ai_assistant.py`

[Source](../../api/routes/ai_assistant.py) · 367 lines · Tests: [test_sql_console_pool.py](../../tests/unit/test_sql_console_pool.py), [test_pitch_result_vocabulary.py](../../tests/unit/test_pitch_result_vocabulary.py)

The optional Data Lab assistant. It writes read-only SQL, runs it through `sql_safety`, and
narrates the result over server-sent events.

| Function / class | What it does | Called from |
|---|---|---|
| `status_probe` | `GET /status`: `configured` and the model name. | The assistant panel |
| `ask` | `POST /ask`: 503 without the client or the read-only pool; else a `text/event-stream` of `text`, `query`, `result`, `error` and `done` frames. | The assistant panel |
| `_run_conversation(request, body)` | Up to `MAX_ASSISTANT_STEPS` tool-use turns with one tool, `run_sql`. A rejected or failed query goes back to the model as a tool result. The model sees at most 60 rows; the user sees up to 200. | `ask` |

**Flags:** `ASSISTANT_MODEL` (default `claude-sonnet-5`), `MAX_ASSISTANT_STEPS` (default 4).

**Watch out:** The module never imports `anthropic`; the client lives on `app.state`, so the app
boots without the package or the key.

### `api/routes/analytics.py`

[Source](../../api/routes/analytics.py) · 469 lines · Tests: [test_read_only_routes.py](../../tests/unit/test_read_only_routes.py), [test_game_type_filter.py](../../tests/unit/test_game_type_filter.py), [test_pitch_result_vocabulary.py](../../tests/unit/test_pitch_result_vocabulary.py)

Nine parameterised summaries of `raw.pitches` on the read-only pool. Each query is bounded by
`season` (an indexed filter on a table of about ten million rows) and leaves out the flagged
rows unless `include_flagged` is set.

| Function / class | What it does | Called from |
|---|---|---|
| `get_kpis` / `by_season` | The store's coverage; `kpis` reads `pg_class` for a row estimate. Neither takes `season`. | The Data Lab summary |
| `pitch_type_mix` / `pa_outcomes` / `count_state` / `velo_distribution` / `zone` | Season summaries with optional player and hand filters. | The Data Lab summary |
| `pitcher_leaderboard` / `batter_leaderboard` | Rate leaderboards with a minimum-PA floor; `metric` is checked against an allowlist. | Clients |
| `_filter_game_type(where, args, game_type, prefix)` | The Data Lab game-type filter (SIM-572): `R` (the default) keeps the regular season, `all` keeps the postseason too. Every route that takes `season` takes it. | The seven season routes |

**Watch out:** The swing, whiff, called-strike, CSW (called strikes plus whiffs) and in-play
filters read the pitch-result `type` code through `pipeline/statcast_events.py` (the Data Lab
vocabulary, SIM-571), never the `description` display text. The whiff here is Baseball
Savant's: a foul tip counts. A new season route without `_filter_game_type` fails
`test_game_type_filter.py`.

### `api/routes/players.py`

[Source](../../api/routes/players.py) · 155 lines · Tests: [test_read_only_routes.py](../../tests/unit/test_read_only_routes.py)

| Function / class | What it does | Called from |
|---|---|---|
| `search_players(request, q, limit)` | `GET /search`: an `ILIKE` match ranked by `public.similarity()` (the pg_trgm trigram score); without the extension it falls back to alphabetical order. | The player pickers |
| `get_player(request, player_id)` | `GET /{player_id}`: identity and the seasons the player appears in `raw.pitches`. | The similarity explorer |

**Watch out:** Keep the `public.` prefix: the read-only pool's `search_path` is `raw`, so a bare
`similarity()` fails and the search silently loses its ranking.

### `api/routes/schema_introspect.py`

[Source](../../api/routes/schema_introspect.py) · 95 lines · Tests: [test_read_only_routes.py](../../tests/unit/test_read_only_routes.py)

| Function / class | What it does | Called from |
|---|---|---|
| `get_schema(request)` | `GET /api/schema`: the `raw` tables and columns from `information_schema.columns`, cached on `app.state` for the life of the process. | The SQL console, the assistant |

**Watch out:** A migration that changes a `raw` column shows here only after an app restart.

### `api/routes/data_health.py`

[Source](../../api/routes/data_health.py) · 118 lines · Tests: [test_api_data_health.py](../../tests/unit/test_api_data_health.py), [test_read_only_routes.py](../../tests/unit/test_read_only_routes.py)

| Function / class | What it does | Called from |
|---|---|---|
| `get_data_freshness(request)` | `GET /api/data/freshness`: the newest ingest from `raw.etl_data_freshness`, the totals and the games and pitches per season. An empty store returns zeros. | The data badge |

### `api/routes/similarity.py`

[Source](../../api/routes/similarity.py) · 490 lines · Tests: [test_similarity_endpoint.py](../../tests/unit/test_similarity_endpoint.py)

The pitcher similarity explorer. The route is thin; the binning and the diagnostic are pure
helpers that `similarity_explorer.py` reuses.

| Function / class | What it does | Called from |
|---|---|---|
| `get_pitcher_similarity_distribution` | `GET /pitcher/{pitcher_id}/{season}`: the engine's scores against every same-hand pitcher-season, binned, with the top matches and a diagnostic. Cached 24 hours in Redis. 404 when the pitcher is not in the engine. | The similarity explorer |
| `get_pitcher_engine` / `get_name_resolver` / `get_similarity_cache` | Dependencies that read `app.state`. No engine is 503 ("engine warming"); no resolver gives placeholder names. | The route; `similarity_explorer` |
| `compute_score_summary` / `classify_diagnostic` / `build_bins` / `build_top_n` | The summary statistics, the HEALTHY / COLLAPSED / NO_SPREAD verdict (median target 0.50) and the histogram. | The route; `similarity_explorer` |

**Watch out:** Keep this module free of numpy, so both routers import in a lint-only job.
`docs/similarity_visualization_spec.md` is the route's design.

### `api/routes/similarity_explorer.py`

[Source](../../api/routes/similarity_explorer.py) · 726 lines · Tests: [test_similarity_explorer_adapters.py](../../tests/unit/test_similarity_explorer_adapters.py)

The explorer across all eleven engines. Eight score engines return a 0-to-1 composite with
sub-scores; three distance engines (situation, pitch-to-pitch, batted ball) return a distance
and are not keyed by a player.

| Function / class | What it does | Called from |
|---|---|---|
| `SCORE_ADAPTERS` / `DISTANCE_ENGINES` | Each engine's method, partition rule, id and sample fields, minimum sample and sub-score weights. | Every route here |
| `list_engines` / `engine_meta` | `GET /engines` and `GET /{engine}/meta`. | The explorer |
| `engine_query` / `engine_pair` | `GET /{engine}/query` (ranked matches and a histogram) and `GET /{engine}/pair`. The fielder engine needs `position` (else 400); the batter engine takes `vs_hand`. A distance engine is 404. | The explorer |
| `situation_query` | `POST /situation/query`: the nearest game states from the KD-tree situation engine; 503 when it is not built. | The situation finder |

**Watch out:** The composite is not the weighted sum of the sub-scores: each engine applies a
confidence discount. The adapter weights are copies of the engines' weights.

### `api/routes/metrics.py`

[Source](../../api/routes/metrics.py) · 633 lines · Tests: [test_metrics_endpoint.py](../../tests/unit/test_metrics_endpoint.py), [test_backup_age_gauge.py](../../tests/unit/test_backup_age_gauge.py)

`GET /metrics` in the Prometheus text format. It uses `prometheus_client` when it imports, else
a hand-written writer with the same series names.

| Function / class | What it does | Called from |
|---|---|---|
| `metrics(request)` | Reads `app.state` only, so a cold app never fails the scrape. Series: `baseball_sim_app_info`, `_metrics_scrapes_total`, `_requests_total`, `_latency_seconds` (the newest `GET /simulate` batch), `_api_p95_seconds`, `_pipeline_freshness_seconds`, `_finals_age_hours`, `_backup_age_hours{store}`, `_offsite_backup_age_hours{store}`. | Prometheus |
| `record_sim_latency` / `record_request` | The two write seams. | `games.simulate_game_endpoint`; `LatencyMiddleware` |
| `_schedule_finals_refresh(app_state)` | Starts background refreshes of the newest final game (every 10 minutes), the live heartbeat (every 15 seconds) and the off-site backup ages (every 10 minutes). The off-site read runs on its own daemon thread, because a OneDrive file can stall it. | `metrics` |
| `_backup_ages(app_state)` | The local backup ages, read from disk at most once in 10 minutes. | `metrics` |

**Flags:** `APP_VERSION`, `BACKUPS_ROOT` (production `/backups`), `OFFSITE_ROOT` (production
`/offsite`).

### `api/routes/auth.py`

[Source](../../api/routes/auth.py) · 181 lines · Tests: [test_api_auth_session.py](../../tests/unit/test_api_auth_session.py), [test_route_auth_coverage.py](../../tests/unit/test_route_auth_coverage.py)

| Function / class | What it does | Called from |
|---|---|---|
| `login(body, response)` | Compares the password in constant time and sets the cookie. 401 on a wrong password; 503 when `AUTH_PASSWORD` or `SECRET_KEY` is blank. | The login page |
| `me(request)` | `authenticated`, `mode` (`cookie`, `api_key`, `disabled` or `none`) and `expires_in`. Always 200. | The frontend at boot |
| `logout(response)` | Clears the cookie. | The logout button |

**Watch out:** The router has no gate on purpose: the login page must reach it.

### `api/routes/_common.py`

[Source](../../api/routes/_common.py) · 112 lines · Tests: [test_read_only_routes.py](../../tests/unit/test_read_only_routes.py), [test_api_data_health.py](../../tests/unit/test_api_data_health.py)

| Function / class | What it does | Called from |
|---|---|---|
| `_get_pool(request)` | The main pool, or 503. | The game and betting routes |
| `_get_ro_pool(request)` / `RO_POOL_UNAVAILABLE_DETAIL` | The read-only pool, or 503 with the one shared message. No fallback to the main pool. | The Data Lab routes |
| `_get_sim_cache(request)` | `app.state.sim_cache`, or None. | The game routes |
| `_row_get` / `_opt_str` / `_opt_int` / `_iso` | One column from an asyncpg record or a dict, with a default for a missing or NULL value. | Most route modules |

**Watch out:** A new route that reads only `raw.*` uses `_get_ro_pool`, never `_get_pool`.

### Package markers

| File | Lines | What it holds |
|---|---|---|
| [`api/__init__.py`](../../api/__init__.py) | 0 | Empty; makes `api` a package. |
| [`api/routes/__init__.py`](../../api/routes/__init__.py) | 12 | A docstring naming the aggregate routers and their sub-routers. |

## Routes defined outside `api/`

`create_app()` includes two routers from the live pipeline package:

- `ws_router` (`/ws/games/{game_pk}`, in
  [live_ingestion_pipeline.py](../../pipeline/live/live_ingestion_pipeline.py)): the handler
  checks the handshake, registers the socket with `connection_manager`, asks the bridge to
  subscribe to the game's Redis channel, and answers `ping` with `pong`. With no message for
  30 seconds it sends a `ping`.
- `odds_router` (`/api/odds/*`, in
  [odds_routes.py](../../pipeline/live/ingestion/odds_routes.py)): both routes return the
  mock provider's odds, never stored or live lines. The Betting card reads `/api/betting`
  instead.
