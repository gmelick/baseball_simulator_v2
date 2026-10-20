# API Layer

The FastAPI application: the app factory and its lifespan, the routes, the response models and the auth.

*35 files documented — use the page outline (right sidebar) to jump to one.*

### `api/__init__.py`

Empty package marker for the api/ package. No code.

**Used by:** `every api.* import`

> **Notes for anyone changing this file:** File is literally empty (0 bytes) — just makes api/ a package.

---

### `api/main.py`

The FastAPI app: `create_app()` builds it (the middleware, every router, `/health`, `/ready`, `/` and the SPA mount) and `lifespan` opens and closes its resources. The lifespan's steps that tests patch or read stay here: the Postgres pool and the name resolver, the run registry, the Redis cache, the league reader, the replay store (`app.state.replay_duckdb`, its own file `REPLAY_DUCKDB_PATH`; the analytics file stays read-only), the park-factor prime and the background pre-warm. The other steps live in `api/startup.py`, the environment check in `api/env_check.py`, the park-factor header in `api/middleware.py`.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `lifespan(app)` | Async context manager. Startup, in order: `validate_environment()`, the pg pool and the name resolver, the run registry (`SimJobRegistry`, which marks a previous process's unfinished runs `failed('restart')`), the Redis cache, the league reader (unless `SLATE_SCHEDULE_ENABLED=0`), then `startup.build_engines`, `load_calibration`, `open_assistant_client`, `open_read_only_pool` (passed this module's `open_pg_pool`, so a test's patch reaches it), `start_live_feed`, the replay store, the park-factor prime, `startup.build_sim_runner`, the background pre-warm and `startup.start_bundle_watch`. Shutdown: `startup.close_resources`, then the park-factor source. | `FastAPI(app, lifespan=lifespan) inside create_app()` | `api.state.open_pg_pool / make_pg_name_resolver / open_redis_cache`<br>`api.startup`<br>`api.env_check.validate_environment`<br>`api.sim_jobs.SimJobRegistry`<br>`api.league_feed.LeagueFeed`<br>`db.sim_store.open_replay_store`<br>`simulation.sim_kwargs.prime_park_factor_source / close_park_factor_source` |
| `_background_prewarm(runner, pool_dir)` | Runs `BatchRunner.prewarm()` as a background task, so a slow or stalled warm-up never holds the boot. | `lifespan()` | `simulation.batch_runner.BatchRunner.prewarm` |
| `create_app()` | Builds the app: the CORS, rate-limit, latency and park-factor middleware; every router (auth, similarity, games, betting, data_health, ws/odds, metrics, sql_runner, analytics, players, schema, similarity_explorer, ai_assistant); the SPA mount; `/health`, `/ready`, `/`; the exception handlers. It attaches `app.state.sim_cache` (`make_cache()`). It opens no connection. | `module bottom: app = create_app()`<br>`the tests that build an app per case` | `api.auth`<br>`api.middleware.ParkFactorSourceMiddleware`<br>`api.errors.install_exception_handlers`<br>`every api.routes router`<br>`pipeline.live.live_ingestion_pipeline.ws_router / odds_router` |

**Depends on:** `api.auth`, `api.env_check`, `api.errors`, `api.middleware`, `api.startup`, `api.state`, `api.sim_jobs`, `api.league_feed`, `db.sim_store`, `simulation.sim_kwargs`, `simulation.batch_runner`, every `api.routes` router

**Used by:** `Dockerfile CMD (uvicorn api.main:app)`, the whole test suite through `create_app()`, `scripts/export_openapi.py` (`app.openapi()`)

**Environment flags read here:** `BASEBALL_DB_DSN`, `REDIS_URL`, `ENVIRONMENT`, `SLATE_SCHEDULE_ENABLED`, `REPLAY_PERSISTENCE_ENABLED`, `REPLAY_DUCKDB_PATH`, `BASEBALL_PLAY_POOL_DIR`, `AUTH_DISABLED` (the `/health` payload)

> **Notes for anyone changing this file:** `app.state` is the wiring between the lifespan and the routes: pg_pool, pg_pool_ro, sql_console_role, redis_client, similarity_cache, league_feed, sim_jobs, pitcher_engine, engines, calibration_report, calibration_map, anthropic_client, pipeline or live_bridge, replay_duckdb, park_factor_source, sim_runner, sim_cache, prewarm_task, bundle_version, bundle_watch_task. A route reads them off `request.app.state`; a renamed attribute breaks a route with no error at import. Tests patch `open_pg_pool`, `make_pg_name_resolver`, `open_redis_cache` and `_background_prewarm` on this module, and two source guards read this file: `tests/unit/test_replay_store.py` (the lifespan assigns `app.state.replay_duckdb` and never `app.state.sim_duckdb`) and `tests/unit/test_sim_kwargs.py` (the lifespan primes the park-factor source outside the `replay_enabled` branch). Keep those steps here. The SPA catch-all `/{full_path:path}` must stay after every API route, or it shadows them.

---

### `api/startup.py`

The lifespan's steps that no test patches, one function per step, in the order the lifespan calls them. They log as `api.main`, the lifespan's logger, so the boot reads as one log and the tests' `caplog` filters still match.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `build_engines(app)` | Builds the pitcher engine (`app.state.pitcher_engine`; a failure stops the boot) and all eleven engines (`app.state.engines`; a failing engine is skipped). `SIMILARITY_ENGINE_ENABLED=false` skips both and leaves `engines` empty. | `api.main.lifespan` | `api.state.build_pitcher_engine / build_all_engines` |
| `load_calibration(app)` | Loads the calibration report once, applies it to every engine that supports it, and sets `app.state.calibration_map` (the identity map without a report). | `api.main.lifespan` | `api.state.load_calibration_report / apply_calibration_to_engines`<br>`simulation.win_probability.CalibrationMap` |
| `open_assistant_client(app)` | Creates `app.state.anthropic_client` when `ANTHROPIC_API_KEY` is set; else None. | `api.main.lifespan` | `anthropic.AsyncAnthropic (lazy import)` |
| `open_read_only_pool(app, open_pg_pool)` | Opens the `BASEBALL_DB_RO_DSN` pool (`search_path = raw`), checks its role (`check_read_only_role`) and keeps it as `app.state.pg_pool_ro` only when the role is not a superuser and cannot write; else closes it and leaves None (the read routes answer 503). | `api.main.lifespan` | `api.routes.sql_safety.check_read_only_role`<br>`api.state.redact_dsn` |
| `start_live_feed(app, dsn, redis_url)` | With `LIVE_PIPELINE_ENABLED`, starts the live pipeline in this process; otherwise attaches `app.state.live_bridge`, which forwards the `live` service's Redis messages to the WebSocket clients. | `api.main.lifespan` | `pipeline.live.live_ingestion_pipeline.LiveIngestionPipeline / connection_manager`<br>`pipeline.live.broadcast.RedisBridge` |
| `build_sim_runner(app)` | Pins the bundle version (`api.bundle_watch.pin_bundle_version`), loads the bundle's large arrays for shared memory, and builds the one long-lived `BatchRunner` (`app.state.sim_runner`; `SIM_RUNNER_WORKERS` sets its worker count). A missing bundle never stops the boot. | `api.main.lifespan` | `simulation.batch_runner.BatchRunner / default_max_workers / make_cache`<br>`pipeline.batch.engine_artifacts.EngineArtifacts.load`<br>`api.bundle_watch` |
| `start_bundle_watch(app)` | With `SIM_RESTART_ON_NEW_BUNDLE` on and a pinned version, starts the task that restarts the app on a new bundle version. | `api.main.lifespan` | `api.bundle_watch.watch_for_new_bundle` |
| `close_resources(app)` | The shutdown: the bundle watch and its pin, the pre-warm task, the runner, the run jobs, the live bridge, the league reader, Redis, the read-only and main pools, the in-process pipeline and the replay store, each close guarded. | `api.main.lifespan` | — |

**Environment flags read here:** `SIMILARITY_ENGINE_ENABLED`, `ANTHROPIC_API_KEY`, `ASSISTANT_MODEL` (the log line), `BASEBALL_DB_RO_DSN`, `LIVE_PIPELINE_ENABLED`, `SIM_RUNNER_WORKERS`

> **Notes for anyone changing this file:** The test patches reach the lazy imports here because each step imports at call time (`api.bundle_watch.bundle_link`, `simulation.batch_runner.BatchRunner`, `api.routes.sql_safety.check_read_only_role`). Keep those imports inside the functions.

---

### `api/env_check.py`

The environment check the lifespan runs first.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `validate_environment()` | Raises when `BASEBALL_DB_DSN` or `REDIS_URL` is missing, then when `SECRET_KEY` or `AUTH_PASSWORD` is blank in any environment; the error names `scripts/gen_secrets.py`. With `AUTH_DISABLED=1` it logs one WARNING and skips the auth check. It logs as `api.main`. | `api.main.lifespan` | `api.auth._auth_disabled` |

---

### `api/middleware.py`

The app's own response middleware.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `ParkFactorSourceMiddleware.dispatch()` | Stamps `X-Park-Factor-Source` (`duckdb:<n>`, `unavailable` or `unknown`) on every response, so a caller sees whether the venue park factors were real. It reads the module's live verdict first (`park_factor_source_status()`), then `app.state.park_factor_source`. | `the Starlette middleware stack (every request)` | `simulation.sim_kwargs.park_factor_source_status` |

---

### `api/state.py`

Boot-time resource builders for app.state: the 11 similarity engines, the calibration report and its application, the Postgres-backed player-name resolver, the Redis-backed similarity cache, and the asyncpg pool opener. Each builder is a plain function so a unit test can call it directly without booting the app.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `build_pitcher_engine(duckdb_path=None)` | Constructs and build()s a PitcherSimilarityEngine from DuckDB, waiting out a DuckDB writer (`retry_on_duckdb_lock`); fatal on failure. | `api.startup.build_engines (via asyncio.to_thread)` | `similarity.engines.pitcher_similarity.PitcherSimilarityEngine` |
| `build_all_engines(duckdb_path=None, registry=None, loader=None)` | Builds all 11 engines named in ENGINE_REGISTRY, skipping (with a WARNING log) any engine that fails to construct or build() so one bad profile table never blocks boot. | `api.startup.build_engines` | `_default_engine_loader (importlib)`<br>`each similarity.engines.*.{Class}(duckdb_path=...).build()` |
| `load_calibration_report(path=None)` | Loads a persisted CalibrationReport JSON (CALIBRATION_REPORT_PATH env or arg); returns None on any missing/corrupt file (never raises). | `api.startup.load_calibration` | `similarity.similarity_calibration.CalibrationReport.from_json` |
| `apply_calibration_to_engines(engines, report)` | Calls apply_calibration(report) on every engine that supports it (the 8 RBF/GMM engines); returns the list of engine names it succeeded on. | `api.startup.load_calibration` | `engine.apply_calibration (duck-typed)` |
| `make_pg_name_resolver(pool)` | Builds an async closure resolving a set of player_ids to full names in one round trip against raw.players. | `api.main.lifespan (assigned to app.state.player_name_resolver)` | `asyncpg pool.fetch on raw.players` |
| `open_redis_cache(redis_url)` | Opens an aioredis client, pings it (fail-fast), and wraps it in RedisSimilarityCache. | `api.main.lifespan` | `redis.asyncio.from_url` |
| `open_pg_pool(dsn, min_size=2, max_size=10, server_settings=None)` | Opens an asyncpg connection pool. `server_settings` sets session parameters on every connection (the read-only pool sets `search_path` to `raw`). | `api.main.lifespan (the main pool) and api.startup.open_read_only_pool (when BASEBALL_DB_RO_DSN is set, the read-only pool; the read-only pool serves the SQL console, the assistant and the Data Lab read routes: schema, analytics, players and data freshness)` | `asyncpg.create_pool` |
| `redact_dsn(dsn)` | Returns the DSN as `scheme://user:***@host:port/db` for a log line: the password masked, any query string dropped, `<redacted>` when the DSN does not parse. | `api.startup.open_read_only_pool (the one INFO line that names the console pool)` | `urllib.parse.urlsplit` |

**Depends on:** `similarity.engines.* (11 engine classes, lazily imported)`, `similarity.similarity_calibration.CalibrationReport`, `simulation.win_probability.CalibrationMap / IDENTITY_CALIBRATION`, `asyncpg`, `redis.asyncio`

**Used by:** `api.main`, `api.startup`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH`, `CALIBRATION_REPORT_PATH`

> **Notes for anyone changing this file:** ENGINE_REGISTRY (name -> (module_path, class_name)) is the single source of truth for app.state.engines' keys; api/routes/similarity_explorer.py's SCORE_ADAPTERS dict must stay in sync with these names or a route 404s with 'unknown engine'. build_all_engines is resilient per-engine (partial dict on failure) while build_pitcher_engine is NOT (crashes boot) — the two engines can therefore end up out of sync (pitcher_engine attached but 'pitcher' missing from engines, or vice versa is not possible since pitcher always builds first, but a caller must not assume engines['pitcher'] exists just because app.state.pitcher_engine does elsewhere). CACHE_TTL_SECONDS / make_cache_key are the contract api/routes/similarity.py uses for its Redis key scheme.

---

### `api/auth.py`

Baseline security primitives: session-cookie + API-key authentication (the require_auth dependency and its WebSocket twin authenticate_websocket; SIM-443c: enforced in every environment, with `AUTH_DISABLED=1` the one opt-out), a stdlib in-memory rate limiter, a rolling p95 latency middleware, and the CORS-origin resolution logic. Dependency-light by design (no slowapi, no Redis) so tests and the dev environment work without extra services.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `require_auth(request)` | FastAPI dependency on every data router (SIM-443c). It passes with no credential only when `_auth_disabled()`; else it accepts a valid sim_session cookie, an `X-API-Key` header or `Authorization: Bearer <key>`; otherwise it raises 401 with a `WWW-Authenticate` header. `ENVIRONMENT` plays no part. | The router-level `dependencies=[Depends(require_auth)]` of `api/routes/games.py`, `betting.py`, `similarity.py`, `similarity_explorer.py`, `players.py`, `schema_introspect.py`, `data_health.py`, `sql_runner.py`, `ai_assistant.py`, `metrics.py`, `analytics.py`<br>`pipeline/live/live_ingestion_pipeline.py odds_router` | `_auth_disabled`<br>`_authenticate` |
| `authenticate_websocket(websocket)` | The WebSocket gate: the same check over the handshake's cookies and headers. On failure it closes the socket with code 1008 BEFORE accept and returns None. A router-level dependency gets nothing in a WebSocket scope, so the handler calls this itself. | `pipeline/live/live_ingestion_pipeline.py game_state_ws (first line)` | `_auth_disabled`<br>`_authenticate` |
| `_auth_disabled()` | The ONE opt-out: true when `AUTH_DISABLED=1`. To remove the opt-out, delete this helper and its call sites. | `require_auth`, `authenticate_websocket`, `api/routes/auth.py me()`, `api/env_check.py validate_environment()` and `/health` (`api/main.py`) | `_env_flag` |
| `Principal` / `_authenticate(cookies, headers)` | The shared core: a valid session cookie first, else an API key. It returns a `Principal(mode, subject, expires_at)` or None, and never raises. | `require_auth`, `authenticate_websocket`, `api/routes/auth.py me()` | `_verify_session_token`<br>`_presented_api_key`<br>`_key_matches` |
| `_presented_api_key(headers)` / `_key_matches(presented, keys)` | Reads `X-API-Key`, else `Authorization: Bearer <key>`. Compares the key against EVERY configured key with `hmac.compare_digest` (no early exit). | `_authenticate`<br>`RateLimitMiddleware._client_key (SIM-564)` | `_configured_api_keys` |
| `_mint_session_token(subject='user') / _verify_session_token(token)` | Stateless HMAC-SHA256-signed session token (mint) and its verification (signature + expiry check, never raises). | `api/routes/auth.py login() (mint)`<br>`require_auth() and api/routes/auth.py me() (verify)` | `_get_secret_key`<br>`hmac/hashlib/base64 stdlib` |
| `cookie_kwargs(max_age=None)` | Builds the response.set_cookie/delete_cookie kwargs (httponly, secure-outside-dev, samesite=strict). | `api/routes/auth.py login()/logout()` | `_is_development`<br>`_session_ttl_seconds` |
| `RateLimitMiddleware.dispatch(request, call_next)` | Sliding-window limiter per bucket: a configured API key, else the client address (SIM-564; behind a trusted proxy, the real client's address from `X-Real-IP`, SIM-569); 429 + Retry-After when the window's request count hits the configured limit. Disabled unless RATE_LIMIT_PER_MINUTE > 0. | `ASGI middleware stack, registered in api.main.create_app` | `_presented_api_key`<br>`_key_matches`<br>`_configured_api_keys`<br>`hashlib.sha256 (the bucket name)`<br>`_client_address` |
| `_client_address(request)` / `_trusted_proxy_addresses()` | The address a request comes from (SIM-569): the connection's own, or the value of `X-Real-IP` when the connection comes from a proxy named in `TRUSTED_PROXY_HOSTS`. nginx sets that header to the connection's address on every request, so a browser cannot forge it. The names resolve through `socket.getaddrinfo` at most once a minute; a name that does not resolve trusts nothing. | `RateLimitMiddleware._client_key` | `_resolve_proxy_host`<br>`socket.getaddrinfo` |
| `LatencyMiddleware.dispatch(request, call_next)` | Times every non-exempt request, keeps a 200-entry ring buffer, computes p95 and stores it on app.state.api_p95_seconds; also bumps the /metrics request counter. | `ASGI middleware stack, registered in api.main.create_app` | `api.routes.metrics.record_request` |
| `resolve_cors_origins()` | Resolves the CORS allowlist: CORS_ORIGINS env, else FRONTEND_URL, else (dev only) a fixed localhost list, else a single safe localhost fallback. No default is the wildcard; only an explicit `CORS_ORIGINS=*` gives it. | `api.main.create_app (CORSMiddleware allow_origins)` | `_is_development` |

**Depends on:** `fastapi`, `starlette.middleware.base.BaseHTTPMiddleware`

**Used by:** `api.main (middleware registration, /health)`, `api.env_check (validate_environment)`, `api.routes.auth`, every data router in `api.routes.*`, `pipeline.live.live_ingestion_pipeline (odds_router, game_state_ws)`

**Environment flags read here:** `ENVIRONMENT` (the cookie `secure` flag and the CORS defaults only), `AUTH_DISABLED`, `SECRET_KEY`, `AUTH_PASSWORD`, `SESSION_TTL_HOURS`, `API_KEYS`, `TRUSTED_PROXY_HOSTS` (SIM-569: the proxies whose `X-Real-IP` the limiter believes), `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_ENABLED`, `CORS_ORIGINS`, `FRONTEND_URL`

> **Notes for anyone changing this file:** The rate limiter is PROCESS-LOCAL (an in-memory dict) — correct only for a single worker; a multi-worker deployment would under-count. Since SIM-443c require_auth fails CLOSED: there is no development pass-through, no 'nothing configured' pass-through and no dev sentinel secret. _get_secret_key()/_get_auth_password() raise RuntimeError naming scripts/gen_secrets.py when the variable is blank. The only way to turn the check off is AUTH_DISABLED=1; the app logs it at boot and /health reports "auth_enforced": false. The cookie `secure` flag still follows ENVIRONMENT: on plain HTTP keep ENVIRONMENT=development, or the browser drops the cookie and the login loops. Behind nginx the connection's address is nginx's: `TRUSTED_PROXY_HOSTS` (compose sets `nginx`) names the proxies whose `X-Real-IP` gives the limiter the real client address (SIM-569). uvicorn's own `FORWARDED_ALLOW_IPS` stays at its default: it takes addresses, not names, and nginx's compose address is not fixed.

---

### `api/errors.py`

Installs a single catch-all FastAPI exception handler that turns any unhandled exception into a structured JSON envelope with a correlating request_id, instead of leaking a traceback to the client.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `install_exception_handlers(app)` | Registers an @app.exception_handler(Exception) that logs the full traceback server-side and returns {detail, error_type: 'internal_error', request_id} with a fresh uuid. | `api.main.create_app (last step before returning app)` | `logging.getLogger(...).exception` |

**Depends on:** `fastapi`

**Used by:** `api.main`

> **Notes for anyone changing this file:** HTTPException and RequestValidationError are untouched by this handler (FastAPI's own shapes still apply) — only a genuinely unhandled exception (bug) gets this envelope. Extracted from main.py specifically so it's unit-testable against a tiny app with no DB-dependent lifespan.

---

### `api/schemas/` (a package)

The API's response models: one Pydantic v2 model per simulation or betting output dataclass, each with a `from_dataclass()` classmethod that strips numpy, so the wire JSON holds plain Python types only. The package `__init__` re-exports every public model, so `from api.schemas import X` works for each; the modules group the models by source: `summary` (`ConfidenceIntervalModel`, `GameSimSummaryModel`, `GameSimSummaryLite`), `snapshots` (the player reference, the field, the play-by-play, the override delta), `game_card` (the linescore and the pitcher decisions), `props` (the boxscore card, `GENERIC_PEN_IDS`, `PropEdgeResponse`), `markets` (CLV, the edge report, the bet signal, the line movement) and `live` (`LiveGameStateResponse`). `_base._ApiModel` forbids extra fields.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `GameSimSummaryModel.from_dataclass(summary, include_raw_arrays=True)` | A GameSimSummary as JSON-safe fields, optionally without the three per-iteration score arrays. | `api/routes/games.py simulate_game_endpoint / simulate_with_override_endpoint` | `ConfidenceIntervalModel.from_dataclass` |
| `GameSimSummaryLite` | The array-free summary on a slate card, the status card and a run row. | `api/routes/games_models.py _sim_summary_lite_from_stored` (`model_validate`) | — |
| `LinescoreModel.from_jsonable / PitcherDecisionsModel.from_jsonable` | The model rebuilt from a stored `to_jsonable()` dict (the replay store's game card), the derived properties recomputed. | `api/routes/games.py get_game_linescore / get_game_decisions / get_game_card` | — |
| `BoxscoreCardRowModel / BoxscoreCardModel.from_prop_set` | One `{prop_name -> mean}` map per player, in BATTER_PROPS / PITCHER_PROPS order, with the name, side, slot and starter flag; a side's placeholder relievers fold into one "Bullpen (generic)" row (`GENERIC_PEN_IDS`). The card carries the run's `base_seed`. | `api/routes/games.py get_game_boxscore` | `simulation.prop_distributions.PropDistributionSet` |
| `EdgeReportModel / BetSignalModel / LineMovementModel / CLVModel .from_dataclass` | The betting dataclasses as wire-safe models; a MarketSide becomes its string value, and `price_book` adds the book's display name. | `api/routes/betting.py`, `betting_lines.py`<br>`api/routes/games.py get_player_prop_edge` | `pipeline.odds_provider.book_display_name` |

**Depends on:** `api.serialization.to_jsonable` (LineQuoteModel's `fetched_at`), `pipeline.odds_provider`, `pydantic`

**Used by:** `api.routes.games` and its sub-routers, `api.routes.betting` and its sub-routers

> **Notes for anyone changing this file:** Every model forbids extra fields (`_ApiModel`): a field added to a source dataclass does not reach the wire until its model gains it. A model's docstring and fields are the OpenAPI schema; regenerate the frontend snapshot after an edit (`scripts/export_openapi.py` in the app image, then `npm run gen:api`).

---

### `api/serialization.py`

The single recursive numpy-to-JSON conversion helper (to_jsonable) used everywhere a Phase-4 dataclass or a raw asyncpg/DuckDB value needs to become plain Python types before hitting json.dumps or a Pydantic model.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `to_jsonable(obj)` | Recursively converts numpy scalars/arrays, datetimes, enums, dataclass instances, mappings, and other iterables into JSON-native int/float/str/bool/None/list/dict; anything unrecognized falls back to str(obj). | `api/routes/games.py (_build_replay_artifacts, _store_history_run)`<br>`api/schemas/markets.py (LineQuoteModel.from_dataclass)`<br>`api/routes/sql_safety.py (run_read_only_sql cell conversion)` | `itself, recursively` |
| `_jsonable_key(key)` | Coerces a mapping key (numpy scalar / enum) to a JSON-object-key-safe Python value before to_jsonable stringifies it. | `to_jsonable (dict branch)` | — |

**Depends on:** `numpy`

**Used by:** `api.schemas`, `api.routes.games`, `api.routes.sql_safety`

> **Notes for anyone changing this file:** Deliberately lossless — never rounds, truncates, or drops elements (a consumer that wants a summary trims at the call site, not here). np.float64 subclasses Python float and np.str_ subclasses str, so the isinstance check at the top explicitly excludes np.generic first — removing that exclusion would silently let numpy scalars leak onto the wire again.

---

### `api/routes/__init__.py`

The routers package; its docstring names the aggregate routers (`games`, `betting`) and their sub-routers, `_common` and `sql_safety`. `api/main.py`'s `create_app()` holds the router list.

**Used by:** `every api.routes.* import`

---

### `api/routes/_common.py`

The small helpers and the one constant the route modules share, so no module keeps its own copy: the main-pool accessor, the read-only-pool accessor, the one 503 text for a missing read-only pool, the sim-cache accessor, and the row readers that work on an asyncpg Record or a plain dict.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_get_pool(request)` | Returns request.app.state.pg_pool, or raises 503 if the lifespan never attached one. The main pool logs in as `baseball_app`, which can write; only the routes that write or read `sim.*` use it. | `api/routes/games.py`<br>`api/routes/betting.py` | — |
| `_get_ro_pool(request)` | Returns request.app.state.pg_pool_ro (the `baseball_ro` pool the lifespan checked), or raises 503 with `RO_POOL_UNAVAILABLE_DETAIL`. It never falls back to the main pool, because the main pool's role can write. The read-only pool sets `search_path = raw` and can read `raw.*` and nothing else. | `api/routes/sql_runner.py`<br>`api/routes/analytics.py`<br>`api/routes/players.py`<br>`api/routes/schema_introspect.py`<br>`api/routes/data_health.py` | `RO_POOL_UNAVAILABLE_DETAIL` |
| `RO_POOL_UNAVAILABLE_DETAIL` | The one 503 text for a missing read-only pool. The constant lives here, not in sql_safety.py, so the route modules do not import `sqlglot`. | `_get_ro_pool`<br>`api/routes/sql_safety.py (re-export)`<br>`api/routes/ai_assistant.py (through sql_safety)` | — |
| `_get_sim_cache(request)` | Returns `app.state.sim_cache`, or None (the BatchRunner then picks its own cache). | `api/routes/games.py`, `games_slate.py`, `games_feed.py` | — |
| `_opt_str(row, key)` / `_opt_int(row, key)` / `_iso(v)` | A column as text, as an integer or as ISO-8601 text, or None. | `api/routes/games_slate.py`, `games_runs.py`, `data_health.py` | `_row_get` |
| `_row_get(row, key, default=None)` | Reads key from an asyncpg Record OR a plain dict uniformly, collapsing a present-but-None value to default. | `api/routes/games.py`<br>`api/routes/data_health.py`<br>`api/routes/players.py`<br>`api/routes/schema_introspect.py` | — |

**Depends on:** `fastapi`

**Used by:** `api.routes.games`, `api.routes.betting`, `api.routes.data_health`, `api.routes.players`, `api.routes.schema_introspect`, `api.routes.analytics`, `api.routes.sql_runner`, `api.routes.sql_safety`

> **Notes for anyone changing this file:** Do not put a per-file copy of any of these helpers back into a route module; import from here. A new route that only reads `raw.*` uses `_get_ro_pool`, never `_get_pool` (the Data Lab read routes on the read-only pool, SIM-566). `metrics.py` reads `app.state.pg_pool` directly and imports neither helper.

---

### `api/routes/auth.py`

Browser session-auth HTTP endpoints: POST /auth/login (password -> httpOnly cookie), GET /auth/me (probe auth state, always 200), POST /auth/logout (clear cookie). Registered with no router prefix so the paths are exactly /auth/*.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `login(body, response)` | Validates the posted password against AUTH_PASSWORD (constant-time compare) and, on success, mints and sets a signed session cookie. A blank AUTH_PASSWORD or SECRET_KEY answers 503 ("Login is not configured on this server"), not a 500. | `frontend login form (POST /auth/login)` | `api.auth._get_auth_password / _mint_session_token / cookie_kwargs` |
| `me(request)` | Never raises 401 — reports authenticated/mode/expires_in so the frontend can decide whether to show the login page on boot. `mode` is `cookie`, `api_key`, `disabled` or `none` (SIM-443c); `authenticated` is true exactly when require_auth would pass. | `frontend app-boot check (GET /auth/me)` | `api.auth._auth_disabled / _authenticate` |
| `logout(response)` | Idempotently clears the session cookie. | `frontend logout action (POST /auth/logout)` | `api.auth.cookie_kwargs` |

**Depends on:** `api.auth`

**Used by:** `api.main.create_app (registered first)`

> **Notes for anyone changing this file:** No server-side session store — the cookie IS the session (HMAC-signed, self-expiring). Programmatic clients bypass this router entirely and send an API key (X-API-Key or Authorization: Bearer) to require_auth directly. This router carries NO auth gate on purpose: the login page must reach it.

---

### `api/routes/metrics.py`

The /metrics Prometheus scrape endpoint (SIM-374). Uses the real prometheus_client library when importable, else a hand-rolled text-exposition writer emitting the identical series names, so the scrape config and Grafana dashboards bind either way.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `record_sim_latency(app_state, seconds)` | Public instrumentation seam: stores the last simulation wall-clock on app.state and updates the Prometheus gauge if available. | `api/routes/games.py simulate_game_endpoint (after a non-cached batch run)` | — |
| `record_request(app_state)` | Increments app.state.request_count on every non-exempt request. | `api.auth.LatencyMiddleware.dispatch` | — |
| `metrics(request)` | The GET /metrics handler: refreshes gauges from app.state and returns either the real Prometheus exposition or the hand-rolled fallback text. | `Prometheus scraper` | `_collect`<br>`_render_fallback (fallback path) or prometheus_client.generate_latest` |

**Depends on:** `api.auth.require_auth`, `prometheus_client (optional)`

**Used by:** `api.main.create_app`, `api.routes.games (record_sim_latency)`, `api.auth.LatencyMiddleware (record_request)`

**Environment flags read here:** `APP_VERSION`

> **Notes for anyone changing this file:** Guarded by require_auth on the router (SIM-443c). Prometheus sends one API_KEYS value as a Bearer token, read from deploy/monitoring/secrets/app_api_key. The module keeps a single hand-rolled fallback scrape counter at module scope (_FALLBACK_SCRAPE_COUNT) which resets on process restart — expected, not a bug. Only requests_total/sim_latency/api_p95/pipeline_freshness are 'live'; the rest of the app never calls these seams unless another route explicitly imports them (record_sim_latency is currently called from exactly one place).

---

### `api/routes/games.py`

The aggregate router of `/api/games` (the prefix, the `games` tag and the login) and every route that runs a simulation, with the seam they share: the factory ref, the game state, the sim kwargs, the batch runner and the replay store. It includes the three sub-routers (`games_slate`, `games_feed`, `games_runs`); each is a bare `APIRouter()`, so the OpenAPI contract does not depend on where a route lives.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_factory_ref(request)` | The StateMachine factory's dotted ref: `app.state.sim_factory_ref`, else `$SIM_MACHINE_FACTORY_REF`, else `PRODUCTION_FACTORY_REF` ('simulation.production_factory:production_machine_factory'). | every simulation route here<br>`api/routes/betting.py _summary_and_winprob` | `simulation.flags.read_str` |
| `_resolve_state_or_error(pool, game_pk, fallback_rows=None)` | Resolves a game's GameState from its lineup and marks its park factor unresolved. `LineupNotIngestedError` and `LineupIncompleteError` (a side with no pitcher, an empty batting order; the incomplete-lineup answer, SIM-559) are a 503 with `Retry-After: 900`; `LineupResolutionError` (an unknown game) is a 404. `fallback_rows` are lineup rows from the league feed, for a what-if on a game with no stored lineup. | every simulation route here<br>`api/routes/betting.py _summary_and_winprob`<br>`scripts/accuracy_comparison.py`, `scripts/validate_props.py` | `simulation.lineup_resolver.resolve_game_state`<br>`simulation.sim_kwargs.mark_park_factor_unresolved` |
| `_resolved_sim_kwargs(request, pool, state, game_pk)` | The one way a route builds sim kwargs: it looks the venue's park factor up first, then calls `simulation.sim_kwargs.build_sim_kwargs` (the bare builder raises `UnresolvedParkFactorError` on a marked state; the park-factor resolution, SIM-452). | every simulation route here<br>`api/routes/betting.py _summary_and_winprob` | `simulation.sim_kwargs.build_sim_kwargs` |
| `_build_runner(request)` / `_run_batch(runner, spec, ...)` | The shared `app.state.sim_runner`, else a one-worker runner on the app's cache (a test app without the lifespan); and the sync wrapper the routes run in a worker thread. | every simulation route here<br>`api/routes/betting.py _summary_and_winprob` | `simulation.batch_runner.BatchRunner` |
| `simulate_game_endpoint` | GET /{game_pk}/simulate — an N-game batch; records the latency metric, stores one game in the replay file and the summary in the sim-run history, and returns the GameSimSummary. | the API's clients; the tests | `_persist_replay_artifacts`<br>`api.routes.metrics.record_sim_latency` |
| `start_simulation_run` | POST /{game_pk}/simulate (202) — queues a durable run (`api.sim_jobs`), or returns the queued or running one with the same key; the run's representative game goes to the replay file. Before Alembic 0029, 503. | `frontend Simulation card` | `api.routes.games_runs._get_sim_jobs / _run_model`<br>`_lineup_source` |
| `start_what_if` | POST /{game_pk}/what-if (202) — the plate appearance's start state (`games_feed._start_state`), the changes applied (`simulation.whatif.apply_changes`; an illegal change is 422), and two runs at one seed: `whatif_base` and `whatif_change`. A game with no stored lineup builds its base state from the feed's starting lineup. Before Alembic 0030, 503. | `frontend WhatIfPanel` | `api.sim_jobs.SimJobRegistry`<br>`api.routes.games_feed._start_state / _league_game_payload / _feed_lineup_rows` |
| `simulate_sample_game` | POST /{game_pk}/sample-game — simulates ONE game at `base_seed` (random when omitted) and stores it; returns `{game_pk, run_id, base_seed}`. 503 with the store off or an unusable lineup, 404 for an unknown game, 500 when the store failed. | `frontend game page "Simulate a game"` | `_persist_replay_artifacts` |
| `simulate_with_override_endpoint` | POST /{game_pk}/simulate/with_override — a baseline batch and an override batch at one seed, and their `OverrideDelta`. | the API's clients (no frontend caller) | `_apply_override`<br>`simulation.snapshots.OverrideDelta.from_summaries` |
| `get_game_boxscore` | GET /{game_pk}/boxscore — each player's prop means over an N-game batch on the worker pool (`_build_prop_set`), with the name, side, slot and starter flag; `run_id=` or `latest_run=true` reads a stored run instead. With the replay store on, the batch's first game is stored for /linescore and /plays. | `frontend BoxscorePanel` | `_build_prop_set`<br>`_player_tags`<br>`_player_names`<br>`api.routes.games_runs._stored_prop_set` |
| `get_player_prop_edge` | GET /{game_pk}/props/{player_id}/{prop} — one player's PMF of one prop, the over / under at a line, and the edge report when both prices are given. With the boxscore's seed and N it reads the cached run. The valid props are `simulation.prop_distributions.ALL_PROPS` (fifteen). | `frontend BoxscorePanel` | `_build_prop_set`<br>`betting.prop_edge_report` |
| `get_game_plays / get_game_state_at_pitch / get_game_linescore / get_game_decisions / get_game_card` | The replay reads: the newest complete run (a run with a game card) unless /plays names `run_id`. /plays carries the run id, the seed and a `names` map (`raw.players`, and "Generic reliever N" for a placeholder arm); /card carries the run id and the seed, so the page reads the card and then that run's plays. 404 with nothing stored, 503 with the store off. | `frontend game page` | `db.sim_store`<br>`_replay_call`<br>`_state_at_pitch_model_from_snapshot` |
| `_persist_replay_artifacts(...)` | Records one game on the worker pool (`BatchRunner.record_game`; the API process never loads the bundle), builds its artifacts and stores them under the replay file's next run id; a /simulate batch also writes its summary to the Postgres history (`_store_history_run`). Never raises: None on any failure. | `simulate_game_endpoint`, `start_simulation_run`, `simulate_sample_game`, `get_game_boxscore` | `_build_replay_artifacts`<br>`_store_replay_run`<br>`_resolve_lineup_best_effort` |
| `_build_replay_artifacts(recorded, resolved=None)` | One recorded game as a `ReplayArtifacts`: the play-by-play, a state per thrown pitch (with the fielding side's nine fielders when a lineup is resolved), the linescore, the pitcher decisions and the result. The routes record on the worker pool (`BatchRunner.record_game`); no route module imports the in-process recorder. | `_persist_replay_artifacts` | `simulation.play_recorder.record_game`<br>`simulation.linescore.linescore_from_plays`<br>`simulation.pitcher_decisions.decisions_from_plays` |
| `_store_replay_run(cur, ...)` / `_replay_call(con, fn, **kw)` | One replay write under `_REPLAY_WRITE_LOCK` (the run id is the file's max + 1, and two prunes of one game conflict in DuckDB): the play stream, the states and the game card in one transaction, then the prune. `_replay_call` runs a store call on a cursor of its own. | `_persist_replay_artifacts` and the replay reads | `db.sim_store` |
| `_build_prop_set` / `_player_tags` / `_player_names` | The run's PropDistributionSet from `BatchRunner.run_prop_set` (a seeded set is cached); each player's side, slot and starter flag from the sim kwargs, the placeholder pen included; the players' names from `raw.players` (a failed lookup returns `{}`). | `get_game_boxscore`, `get_player_prop_edge` | `simulation.batch_runner.BatchRunner.run_prop_set`<br>`simulation.production_factory._default_bullpen_for_spec` |

**Depends on:** `api.auth.require_auth`, `api.routes._common`, `api.routes.games_slate / games_feed / games_runs / games_models`, `api.schemas`, `api.serialization.to_jsonable`, `betting`, `db.sim_store`, `simulation.batch_runner`, `simulation.linescore`, `simulation.lineup_resolver`, `simulation.pitcher_decisions`, `simulation.play_recorder`, `simulation.production_factory`, `simulation.prop_distributions`, `simulation.sim_kwargs`, `simulation.snapshots`

**Used by:** `api.main.create_app`, `api.routes.betting` (`_build_runner`, `_resolve_state_or_error`, `_resolved_sim_kwargs`, `_run_batch`, `resolve_factory_ref`)

**Environment flags read here:** `SIM_MACHINE_FACTORY_REF`

> **Notes for anyone changing this file:** Tests patch this module's `resolve_game_state`, `record_game`, `linescore_from_plays`, `decisions_from_plays`, `_build_replay_artifacts`, `_query_player_names`, `_build_prop_set`, `_resolve_state_or_error`, `PRODUCTION_FACTORY_REF` and `sim_store` (the replay reads), so the code that calls them stays here. `tests/unit/test_sim_kwargs.py` counts the sim-kwarg calls in this file (8: the seven simulation routes and `_resolved_sim_kwargs`); a route that runs a simulation lives here. `PRODUCTION_FACTORY_REF` stays a dotted string: it is part of the result and prop-set cache keys. A route description (`description=`) or a model docstring is part of the OpenAPI contract: change one and regenerate `frontend/openapi.json` and `schema.d.ts` (`scripts/export_openapi.py` in the app image, then `npm run gen:api`; `tests/unit/test_openapi_snapshot.py` fails on drift). Lines marked `# re-export: removed in the integration pass` keep the names other areas' callers still read here.

---

### `api/routes/games_slate.py`

The day slate and one game's status card. The league's schedule says which games exist and their state; the database adds the lineup flag and source, the venue city and the newest simulation run.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_games_on_date(date, request, use_cache)` | GET /{date} — the schedule's games (one card per game; a suspended game appears on its resume day too) merged with the stored data in two queries (`_slate_merge_rows`). The schedule is cached by date class (`_slate_ttl`: 20 s today, 10 min ahead, 24 h past). On a league error, the last good schedule (`slate:last:{date}`), else the stored listing (memoized for `POOL_QUERY_TTL_S`) mapped by `card_state_from_raw`; `source` says which. The records come from the schedule. | `frontend Daily Diamond slate` | `_slate_schedule_payload`<br>`_schedule_card`<br>`_slate_from_db`<br>`pipeline.mlb_schedule` |
| `get_game_status_card(game_pk, request)` | GET /{game_pk}/status — the game's identity, names, venue and season records (`_GAME_CARD_SQL`), its state, the final score, the newest stored summary (`_sim_summary_lite_from_stored`), and the schedule's fields for the page header (`_schedule_game_for`). 404 for an unknown game, 503 with no pool. | `frontend game page header` | `db.sim_store.load_latest_sim_run`<br>`_schedule_game_for` |

**Used by:** `api.routes.games` (included in the aggregate router)

> **Notes for anyone changing this file:** The tests patch `sim_store` on this module for /status. The two record-bearing queries share `_team_records_cte` and `_TEAM_RECORDS_JOIN_TAIL`; only the cutoff differs.

---

### `api/routes/games_feed.py`

The real game: the league's per-game feed (one cached copy, `_league_game_payload`) and the live service's stored state.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_game_feed(game_pk, request, use_cache)` | GET /{game_pk}/feed — the feed reduced to the slate card's detail (`parse_game_feed` to `GameFeedCardModel`). Cached 10 s live, 10 min before the game, 24 h final (`FEED_TTL_BY_STATE_S`); the last good copy on an error (`source = feed_cached`), else 503 with `Retry-After: 30`. | `frontend open slate card` | `_league_game_payload`<br>`pipeline.mlb_game_feed` |
| `get_real_plays(game_pk)` / `get_pa_state(game_pk, at_bat)` | GET /feed/plays — the real game's plate appearances and a names map. GET /feed/state — the game at the start of one plate appearance (no `at_bat`: first pitch): the field in `FeedLive`'s shape, each side's lineup, pitcher, defense, bench and pen. An unknown plate appearance is 404, no feed 503. | `frontend ActualPlayByPlay, WhatIfPanel` | `pipeline.mlb_game_feed.parse_plays / state_at_pa` |
| `get_live_game_state(game_pk, request)` | GET /{game_pk}/live — the live service's game state from `sim.lineup_state`; 404 with no live row, 503 with no pool. | `frontend game page (the fallback while the socket is closed)` | `db.sim_store.load_live_game_state` |
| `_league_game_payload` / `_start_state` / `_feed_lineup_rows` | The cached raw feed (also read by the what-if result and the betting card's grading); the start state of a plate appearance, or an HTTP error; both starting lineups from a feed as `raw.game_lineups` rows. | the routes here<br>`api.routes.games start_what_if`<br>`api.routes.games_runs get_what_if`<br>`api.routes.betting _actual_result` | `pipeline.mlb_game_feed`<br>`pipeline.etl.etl_historical_loader._build_starting_lineup_rows` |

**Used by:** `api.routes.games` (included in the aggregate router)

> **Notes for anyone changing this file:** The tests patch `sim_store` on this module for /live.

---

### `api/routes/games_runs.py`

The reads of a game's durable simulation runs (`sim.sim_runs`). The routes that start a run live in `games.py`.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_latest_simulation_run / get_simulation_run / cancel_simulation_run` | GET /simulate/runs/latest (404 with none), GET and DELETE /simulate/runs/{run_id} (409 once the run finished). `runs/latest` stays before `runs/{run_id}`: the router matches in order. | `frontend Simulation card` | `_get_sim_jobs`<br>`_run_model` |
| `get_what_if(game_pk, base_run_id, change_run_id)` | GET /what-if/{base}/{change} — both runs, the real final and the score at the start. | `frontend WhatIfPanel` | `api.routes.games_feed._league_game_payload`<br>`pipeline.mlb_game_feed.state_at_pa` |
| `_get_sim_jobs` / `_run_model` / `_stored_prop_set` | The run registry (503 with none), a row as `SimRunModel`, and a run's prop set (404 with a hint when none). | the routes here<br>`api.routes.games` | `api.sim_jobs`<br>`simulation.prop_distributions.PropDistributionSet` |

**Used by:** `api.routes.games` (included in the aggregate router)

---

### `api/routes/games_models.py`

The request and response models of `/api/games`, by the routes that use them, and `_sim_summary_lite_from_stored`: a stored summary as `GameSimSummaryLite`. The stored JSON lacks each interval's `half_width` (`to_jsonable` writes fields only, and `half_width` is a property), so the helper fills it from `(high - low) / 2`; it drops the per-iteration arrays and the inning grids, which the lite model forbids.

**Used by:** `api.routes.games`, `api.routes.games_slate`, `api.routes.games_feed`, `api.routes.games_runs`

> **Notes for anyone changing this file:** A model's docstring and fields are the OpenAPI schema; regenerate the frontend snapshot after an edit (see `games.py`).

---

### `api/routes/betting.py`

The aggregate router of `/api/betting` (the prefix, the `betting` tag and the login), /edges and /signals, the simulation they price from, and the grading on the real final. It includes `betting_lines` and `betting_card`; the prices and the edge math live in `betting_pricing`, the stored lines in `betting_stored_odds`. Every betting module logs as `api.routes.betting`.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_summary_and_winprob(request, game_pk, n_iterations, base_seed, use_cache)` | The game routes' seam: the game state, the sim kwargs, the batch (cached by spec, seed and N), and the WinProbability through the app's calibration map. | `get_game_edges`, `get_game_signals` | `api.routes.games` (five helpers)<br>`simulation.win_probability.win_probability` |
| `get_game_edges` | GET /games/{game_pk}/edges — the edge reports of the fifteen game markets by default (`?markets=` narrows; 422 on an unknown market). The response lists the priced markets in vocabulary order, `odds_source` per market, the graded book per stored market (`fair_book`, `fair_book_name`), each side's offered-price book (`price_book`), `market_names` and every run line's pricing. Once the game is final, each edge's `result` and the `final` score (`_actual_result`, `_grade`). | `frontend BettingCard` | `api.routes.betting_pricing`<br>`api.schemas.EdgeReportModel` |
| `get_game_signals` | GET /games/{game_pk}/signals — the same reports gated to +EV and ranked by EV (`bet_signals_from_edges`); the signals fire on all fifteen markets, the uncalibrated segment and team markets included. | `frontend BettingCard` | `betting.bet_signal` |
| `_actual_result` / `_grade` | The real final as a one-game `SegmentRuns` and its score (the stored grid, else the league feed's linescore); one side's "won" / "lost" / "push". | `get_game_edges` | `simulation.game_market_distributions.SegmentRuns / market_outcome`<br>`api.routes.games_feed._league_game_payload` |

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** `tests/unit/test_segment_edges.py` patches `_summary_and_winprob` on this module, so /edges and /signals stay here. The lines marked `# re-export: removed in the integration pass` keep the stored-odds names other areas' tests read here. `betting/` is baked into the app image while `api/` hot-reloads: rebuild the image before an `api/` edit that imports a new `betting/` name reaches the running app (CLAUDE.md §2a).

---

### `api/routes/betting_pricing.py`

The edge math: where a market's prices come from (injected, stored, mock), the run line as a pair or two separate bets, and the twelve segment and team markets.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_build_edge_reports(summary, win_prob, *, game_pk, markets, ...)` | The reports of every side of each requested market: the moneyline off the WinProbability, the total off the totals, the run line off the score margin (a pair de-vigs its two prices; two separate bets read the book's two-way margin: the total, then the moneyline, then a flat 1.05), and the segment and team markets off the summary's inning grid (`_price_segment_market`). A side with a sim probability of exactly 0 or 1 is skipped (`_safe_report`). A summary with no grid prices the full-game markets only and logs one INFO line. Returns an `_EdgeBuild`. | `_priced_edge_reports` | `betting.clv_engine`<br>`simulation.game_market_distributions` |
| `_parse_prices` / `_split_prices` | The `prices` document: a JSON object keyed by market type, each entry its kind's fields; any fault is a 422 naming the market (an owner ruling on the segment markets, SIM-546). A full-game market's entry becomes its named params. | `api.routes.betting` | `pipeline.odds_provider.GAME_MARKET_KIND / GAME_MARKET_TYPES` |
| `_priced_edge_reports` | Reads the stored lines when a requested market needs them, then builds the reports. | `api.routes.betting` | `api.routes.betting_stored_odds._read_stored_rows / _stored_read_markets` |
| `_mock_odds` / `_resolve_price` / `_at_offered_price` | The deterministic mock (`MockOddsAPI`, seeded on game_pk, imported lazily); an injected price else the mock's; a report re-read at the offered price (the EV moves, the edge stays). | `_build_edge_reports` | `pipeline.live.live_ingestion_pipeline.MockOddsAPI` |

---

### `api/routes/betting_stored_odds.py`

The stored price source: each sportsbook's latest usable one-book row (`bp:<id>`) of each market of a game (one book per odds row, SIM-555). A game in `Preview` reads its closing and current rows; any other game its closing rows only. The fair probability de-vigs the graded book's row (`GRADED_BOOK_PREFERENCE`); each side's offered price is the best stored price at that line. A current row not seen within `CURRENT_ROW_MAX_AGE_S` (20 min) of the game's newest sighting is a withdrawn price and drops out (`last_seen_at`, Alembic 0029). A failed read logs a WARNING and the markets fall to the mock.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_read_stored_rows` / `_fetch_stored_rows` | The game's status, then the latest usable row per (market, book) (`_SQL_STORED_GAME_ODDS_SEEN`, else `_SQL_STORED_GAME_ODDS` before 0029). | `api.routes.betting_pricing`, `api.routes.betting_card` | `pipeline.odds_provider.bettable_labels` |
| `_latest_pregame_rows` / `_stored_rows_by_market` / `_stored_market` | The rules again in Python (the allowed line types, one row per book, the withdrawn rows out); the usable rows by market in the graded order (every price present, none 0, `check_row` passed); the graded row and each side's best offered price. | `_read_stored_rows`, `api.routes.betting_pricing` | `pipeline.odds_row_guard.check_row` |
| `_stored_reference_margin` / `_reference_order` / `_stored_read_markets` | One book's two-way margin (a segment run line reads its segment's total first); the markets the read asks for. | `api.routes.betting_pricing` | `betting.clv_engine.reference_margin_from_prices` |

---

### `api/routes/betting_lines.py`

GET /games/{game_pk}/line-movement and /clv: the stored `raw.game_odds` history as the opening-to-closing series per side and book, and the series that carry an entry-versus-close CLV. One-book rows only; a run-line side whose spread moved has no CLV (the run lines scored by shape, SIM-549). With no pool, 503; there is no mock. 422 on a market type outside the three full-game markets.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_game_line_movement / get_game_clv` | The two reads. | the API's clients | `betting.line_movement.fetch_line_movement` |

---

### `api/routes/betting_card.py`

GET /games/{game_pk}/card-odds: the graded book's moneyline, run line and total for one slate card, from the stored rows only (never the mock); on a final game, `settled` says how each line settled. 404 when the game has no stored row for any of the three.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_card_odds` / `_card_odds_from_rows` / `_card_settlement` | The route; the graded row of each market as the card's lines; the winner side of each priced market on the final score. | `frontend CardDetail` | `api.routes.betting_stored_odds._read_stored_rows`<br>`pipeline.odds_provider.book_display_name` |

---

### `api/routes/similarity.py`

v1 Similarity Score Explorer: the pitcher-vs-pitcher engine's histogram/top-N distribution endpoint. Thin route handler over pure, separately-unit-tested helpers (binning, score-summary stats, diagnostic classification).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_pitcher_similarity_distribution(pitcher_id, season, bins, top_n, engine, name_resolver, cache)` | GET /api/similarity/pitcher/{pitcher_id}/{season} — queries the engine (with a 24h Redis cache), resolves names, and returns the score summary, histogram bins, top-N, and a diagnostic classification. | `frontend Similarity Explorer (pitcher view)` | `engine.query (PitcherSimilarityEngine)`<br>`compute_score_summary`<br>`build_bins`<br>`build_top_n`<br>`classify_diagnostic` |
| `get_pitcher_engine(request) / get_name_resolver(request) / get_similarity_cache(request)` | FastAPI dependencies pulling pitcher_engine / player_name_resolver / similarity_cache off app.state, with graceful degradation (placeholder names, no cache) and a 503 when the engine isn't attached. | `get_pitcher_similarity_distribution (Depends)`<br>`api/routes/similarity_explorer.py (_resolve_names reuses get_name_resolver)` | — |
| `compute_score_summary(scores) / classify_diagnostic(scores, n_bins) / build_bins / build_top_n / _BinSpec` | Pure, numpy-free statistics and binning helpers (min/percentiles/mean/std, COLLAPSED/NO_SPREAD/HEALTHY classification, histogram bucketing). | `get_pitcher_similarity_distribution`<br>`api/routes/similarity_explorer.py (reused directly for all other engines)` | — |

**Depends on:** `api.auth.require_auth (router-level dependency, SIM-443c)`, `api.state (CACHE_TTL_SECONDS, NameResolver, SimilarityCache, make_cache_key)`, `similarity.engines.pitcher_similarity`

**Used by:** `api.main.create_app`, `api.routes.similarity_explorer (imports _BinSpec, classify_diagnostic, compute_score_summary, get_name_resolver)`

> **Notes for anyone changing this file:** similarity_explorer.py deliberately reuses this module's pure helpers rather than re-implementing binning/diagnostics — keep them numpy-free (no import of numpy/scipy here) so both routers stay importable in a lint-only CI job. Both routers mount at the SAME prefix '/api/similarity' — FastAPI resolves by path pattern, not registration order, but a new route added to either file must not collide with '/pitcher/{pitcher_id}/{season}' vs '/{engine}/meta|query|pair'.

---

### `api/routes/similarity_explorer.py`

Generalizes the Similarity Explorer to all 8 score-producing engines (batter, fielder, catcher, baserunner, baserunner_steal, pitcher_steal, manager, plus pitcher) using each engine's real component sub-scores, and separately exposes the 3 distance engines' catalog entries plus the situation (nearest-game-state) query.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `SCORE_ADAPTERS / DISTANCE_ENGINES (module dicts)` | Per-engine metadata: method name, partition rule, id/sample field names, weighted sub-score list, and special-casing flags (position_keyed for fielder, supports_vs_hand for batter). | `every route handler in this file via _require_score_engine/_subscores_meta` | — |
| `engine_query(request, engine, entity_id, season, position, vs_hand, bins, top_n)` | GET /api/similarity/{engine}/query — ranked comps + histogram, calling the correct engine.query() signature per engine (fielder needs position, batter needs vs_hand). | `frontend Similarity Explorer (generalized engine view)` | `_require_score_engine`<br>`_call_query`<br>`_call_get_profile`<br>`compute_score_summary/classify_diagnostic/_build_bins (from api.routes.similarity)` |
| `engine_pair(request, engine, a_id, a_season, b_id, b_season, position, vs_hand)` | GET /api/similarity/{engine}/pair — a single subject-vs-comparison breakdown via engine.query_pair. | `frontend head-to-head comparison view` | `_call_pair`<br>`_member` |
| `situation_query(request, body)` | POST /api/similarity/situation/query — nearest historical game states from the KDTree situation engine. | `frontend Situation Finder` | `similarity.engines.situation_similarity.SituationVector`<br>`engine.query` |
| `list_engines(request)` | GET /api/similarity/engines — catalog of all 11 engines with build status + profile counts, for the UI's engine picker. | `frontend Similarity Explorer landing page` | — |

**Depends on:** `api.auth.require_auth (router-level dependency, SIM-443c)`, `api.routes.similarity (_BinSpec, classify_diagnostic, compute_score_summary, get_name_resolver)`, `similarity.engines.situation_similarity.SituationVector (lazy import)`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** SCORE_ADAPTERS' keys MUST exactly match api.state.ENGINE_REGISTRY's keys (both dicts are hand-maintained separately) — adding a 12th engine to state.py without adding an adapter here means the new engine is buildable but never exposed through this explorer. The composite score is explicitly NOT a literal weighted sum of the listed sub-scores (each engine applies its own confidence discount) — the module's own docstring calls this out as an honesty requirement, so a UI author must not reconstruct the score bar-by-bar from the weights shown.

---

### `api/routes/data_health.py`

SIM-417 data-freshness endpoint for the UI's 'data as of...' badge: aggregate ingest watermark and per-season game/pitch coverage from raw.etl_data_freshness / raw.games / raw.pitches.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_data_freshness(request)` | GET /api/data/freshness — runs one aggregate query plus a per-season GROUP BY on the read-only pool and returns last_ingest_at, latest_game_date, totals, and per-season SeasonCoverage rows. It answers 503 when the read-only pool is not open. | `frontend header/data-status badge` | `api.routes._common._get_ro_pool/_row_get` |

**Depends on:** `api.auth.require_auth (router-level dependency, SIM-443c)`, `api.routes._common`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** Gated by require_auth on the router since SIM-443c (it was open before; only /health, /ready, /, /auth/* and the docs stay open). The route reads the read-only pool since SIM-566 and answers 503 without it; it never falls back to the main pool. An empty store returns zeros, never an error.

---

### `api/routes/sql_safety.py`

The single shared read-only SQL validation + execution path used by BOTH the SQL Console and the AI Assistant, so the assistant is physically incapable of running anything the console couldn't. Defense-in-depth: statement validation before the DB, a read-only transaction, a statement timeout, and a hard SQL-enforced row cap.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `validate_read_only_sql(sql, max_len=MAX_SQL_LEN)` | Strips comments/string/dollar/quoted-identifier noise, rejects empty/oversized input, anything not starting with SELECT/WITH, any forbidden write/DDL/session keyword anywhere (even inside a CTE), multiple statements, and a blocklist of dangerous functions (including inside double-quoted identifiers). Last, it runs the relation allowlist. Returns the executable (semicolon-stripped) SQL string. | `run_read_only_sql (this file)`<br>`tests/unit/test_sql_safety.py` | `_strip_sql_noise`<br>`validate_relation_allowlist` |
| `validate_relation_allowlist(sql)` | SIM-443d. Parses the query with sqlglot (Postgres dialect) and rejects it unless every table is `raw.<name>` or a CTE in scope at that point (a non-recursive CTE sees only the CTEs before it). Also rejects: a parse error, more than one statement, a statement that is not a query, `SELECT ... INTO`, the `TABLE name` shorthand, a three-part name, and any function in a FROM clause (plain or LATERAL) that is not on `_TABLE_FUNCTIONS_ALLOWED`. Fails closed. | `validate_read_only_sql` | `sqlglot.parse`<br>`_ctes_in_scope`<br>`_strip_sql_noise` |
| `check_read_only_role(pool)` / `ReadOnlyRoleCheck` | SIM-443e. One query reads the pool's role, whether it is a superuser, whether it can use the `sim` schema, and whether it can INSERT into / SELECT from `raw.pitches`. `acceptable` is true when it is not a superuser, has no `sim` usage and no `raw` insert. | `api.startup.open_read_only_pool` | `pool.fetchrow` |
| `run_read_only_sql(pool, sql, max_rows=DEFAULT_MAX_ROWS, timeout_ms=DEFAULT_TIMEOUT_MS)` | Validates the query, wraps it in a row-capped subquery, executes it inside a read-only transaction with SET LOCAL statement_timeout, and returns a JSON-safe {columns, rows, row_count, truncated, elapsed_ms} payload. | `api/routes/sql_runner.py run_sql`<br>`api/routes/ai_assistant.py _run_conversation` | `validate_read_only_sql`<br>`_execute`<br>`api.serialization.to_jsonable` |
| `RO_POOL_UNAVAILABLE_DETAIL` | The 503 text for a missing read-only pool. sql_safety.py no longer defines it: it imports the constant from `api.routes._common` and keeps the name in `__all__`, so `from api.routes.sql_safety import RO_POOL_UNAVAILABLE_DETAIL` still works. | `api/routes/ai_assistant.py`<br>`tests/unit/test_sql_console_pool.py` | `api.routes._common.RO_POOL_UNAVAILABLE_DETAIL` |
| `_strip_sql_noise(sql, keep_identifiers=False)` | Blanks out comments and string literals (and, unless keep_identifiers, quoted identifiers) while preserving character offsets, so keyword/function scans never trip on literal text — but preserves quoted-identifier TEXT when keep_identifiers=True because a quoted identifier is executable code (Postgres folds unquoted names to lower case, so \"pg_read_file\" IS pg_read_file). | `validate_read_only_sql (called twice: once blanking identifiers for the keyword scan, once preserving them for the dangerous-function scan)` | — |

**Depends on:** `api.serialization.to_jsonable`, `sqlglot` (imported at module top: a missing package fails the boot)

**Used by:** `api.routes.sql_runner`, `api.routes.ai_assistant`, `api.startup.open_read_only_pool (check_read_only_role)`

**Environment flags read here:** `SQL_RUNNER_MAX_ROWS`, `SQL_RUNNER_TIMEOUT_MS`, `SQL_RUNNER_MAX_LEN`

> **Notes for anyone changing this file:** SECURITY-CRITICAL FILE (documented inline as SIM-442): the double-quoted-identifier handling was previously a real vulnerability — blanking a quoted identifier's contents let `SELECT \"pg_read_file\"('/etc/passwd')` slip past every entry in _DANGEROUS_TOKENS because the scanned copy no longer contained the function name, while the ORIGINAL (unblanked) string still executed. Any future edit to _strip_sql_noise or the two validate_read_only_sql scan passes must preserve the keep_identifiers=True pass for the dangerous-function scan or this bug reopens. raw.* is the ONLY readable schema, and two layers enforce it (SIM-443d/e): the relation allowlist rejects any other table before the database is touched, and the console's pool runs as the baseball_ro role, which can SELECT raw.* and nothing else. Postgres also holds a `sim` schema (sim.sim_runs); the allowlist rejects it like any other schema. An unqualified name is rejected even though the pool's search_path is raw, so the rule never depends on a session setting. A new set-returning function in FROM needs a line in `_TABLE_FUNCTIONS_ALLOWED`; never widen the rule to accept unknown functions. _HARD_MAX_ROWS/_HARD_MAX_TIMEOUT_MS are ceilings the env-var tunables can never exceed even if misconfigured.

---

### `api/routes/sql_runner.py`

POST /api/sql/run — the Data Lab SQL Console's HTTP endpoint. A thin wrapper that delegates all safety/execution to api.routes.sql_safety, and carries require_auth on its router (SIM-443c, as every data router does).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `run_sql(request, body)` | Runs body.sql through run_read_only_sql on the read-only pool and maps SqlValidationError / any DB exception to a 400 with the message verbatim in detail. It answers 503 when the read-only pool is not open; it never falls back to the main pool, whose role can write. | `frontend SQL Console` | `api.routes._common._get_ro_pool`<br>`api.routes.sql_safety.run_read_only_sql` |

**Depends on:** `api.auth.require_auth`, `api.routes._common`, `api.routes.sql_safety`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** Never re-implements validation — any safety change belongs in sql_safety.py, not here, to keep the console and the assistant on one path.

---

### `api/routes/players.py`

Player name typeahead + identity lookup for the Data Lab / Similarity UI, backed by raw.players (pg_trgm similarity ranking with an ILIKE fallback). Both routes read the read-only pool and answer 503 without it.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `search_players(request, q, limit)` | GET /api/players/search — ranks raw.players by pg_trgm `public.similarity()`; degrades to plain ILIKE ordering (logged once) if the extension/function is unavailable. | `frontend player search boxes (Data Lab, Similarity Explorer)` | `_fetch`<br>`api.routes._common._get_ro_pool/_row_get` |
| `get_player(request, player_id)` | GET /api/players/{player_id} — identity + the distinct seasons the player appears in raw.pitches as pitcher or batter. | `frontend player detail panel` | `_fetch`<br>`api.routes._common._get_ro_pool` |

**Depends on:** `api.auth.require_auth (router-level dependency, SIM-443c)`, `api.routes._common`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** Gated by require_auth on the router since SIM-443c (it was open before). The pg_trgm fallback path means a search that silently ISN'T similarity-ranked (alphabetical ILIKE instead) is possible in an environment missing the extension — worth checking server logs if search relevance looks off. The typeahead query calls `public.similarity(full_name, $1)` with the schema named. The read-only pool's session sets `search_path = raw`, and the pg_trgm extension lives in `public`, so an unqualified `similarity()` fails there and the route falls into the ILIKE fallback. Keep the schema name.

---

### `api/routes/schema_introspect.py`

GET /api/schema — the raw.* table/column tree consumed by the SQL Console's schema browser and the AI assistant's grounding. Process-lifetime cached on app.state since the schema is static between deploys.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_schema(request)` | Queries information_schema.columns for table_schema='raw' on the read-only pool, groups into TableInfo/ColumnInfo, caches the built SchemaResponse on request.app.state._schema_cache. It answers 503 when the read-only pool is not open. The `baseball_ro` role sees the same 33 tables and 586 columns as the owner. | `frontend SQL Console schema browser` | `api.routes._common._get_ro_pool/_row_get` |

**Depends on:** `api.auth.require_auth (router-level dependency, SIM-443c)`, `api.routes._common`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** The cache is never invalidated except by process restart — a Postgres migration that adds/removes a raw.* column will not show up until the app restarts. Only 'raw' is exposed on purpose; derived.*/sim.* are DuckDB-only and cannot be cross-joined.

---

### `api/routes/analytics.py`

Curated, parameterised (never ad-hoc) descriptive-analytics endpoints over raw.pitches for the Data Lab dashboard: KPIs, per-season coverage, pitch-mix, PA outcomes, count-state swing/whiff, velo histogram, zone whiff/called-strike, and pitcher/batter leaderboards.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| (every route) | Each of the nine routes reads the read-only pool and answers 503 when that pool is not open. Every query names only `raw.*` tables, plus `pg_class` for the KPIs row estimate. | — | `api.routes._common._get_ro_pool` |
| (the rate filters) | Seven module constants hold the pitch-result filters. `_SWING`, `_WHIFF` and `_CALLED_STRIKE` serve the one-table queries. `_SWING_P`, `_WHIFF_P`, `_CSW_P` and `_IN_PLAY_P` serve the two leaderboards. Each constant renders from a helper in `pipeline/statcast_events.py`. Every filter reads the `type` code (the Data Lab vocabulary fix, SIM-571). The whiff is Baseball Savant's (the public Statcast site): a foul tip counts as a whiff. A ball in play is `type IN ('X', 'D', 'E')`, so the hits count too. | `count_state`<br>`zone`<br>`pitcher_leaderboard`<br>`batter_leaderboard` | `pipeline.statcast_events.sql_swing/sql_savant_whiff/sql_called_strike/sql_csw/sql_in_play` |
| `GameType` / `REGULAR_SEASON` / `_filter_game_type(where, args, game_type, prefix)` | The game-type filter (the Data Lab game-type filter, SIM-572). A season of `raw.pitches` holds the regular season and the postseason. `raw.games.game_type` marks each game: 'R' is the regular season, and 'F', 'D', 'L' and 'W' are the four postseason rounds. Each of the seven routes that take `season` takes `game_type`, typed `'R' \| 'all'`. 'R', the default, adds the filter `game_pk IN (SELECT g.game_pk FROM raw.games g WHERE g.game_type = $n)`, with the type as the next query argument. 'all' adds no filter and reads every game of the season, as the routes did before. The filter keeps a pitch when its game has the type; a plain join would clash on `season`, which both tables hold. The leaderboards pass the prefix `p.`. In 2025 the postseason holds 47 games and 14,085 of the 723,055 clean pitches; the batter board reads Ohtani at 55 home runs by default and at 63 with 'all'. | `pitch_type_mix`<br>`pa_outcomes`<br>`count_state`<br>`velo_distribution`<br>`zone`<br>`pitcher_leaderboard`<br>`batter_leaderboard` | — |
| `get_kpis(request)` | GET /api/analytics/kpis — fast top-line coverage using pg_class.reltuples as a row-count estimate instead of COUNT(*) on a ~10^7-row table. It and `by_season` (GET /api/analytics/by-season, games and pitches per season) describe the whole store, so they take no `season` and no `game_type`. | `frontend Data Lab dashboard header` | `_fetch` |
| `pitch_type_mix / pa_outcomes / count_state / velo_distribution / zone` | Season-bounded, optionally player/hand-filtered GROUP BY queries returning usage/velocity/movement/outcome/whiff breakdowns; each defaults to excluding data_quality_flag=TRUE rows unless include_flagged is set. Each reads the regular season alone unless `game_type=all` (SIM-572). | `frontend dashboard cards` | `_fetch`<br>`_dicts`<br>`_quality_clause`<br>`_filter_game_type` |
| `pitcher_leaderboard / batter_leaderboard` | Rate-stat leaderboards (K%/BB%/CSW%/whiff% for pitchers; hard-hit%/EV/K%/BB%/HR for batters) with a minimum-PA HAVING floor, joined to raw.players for names. Each reads the regular season alone unless `game_type=all` (SIM-572). | — (no frontend caller) | `_fetch`<br>`_SWING_P/_WHIFF_P/_CSW_P/_IN_PLAY_P`<br>`_filter_game_type` |

**Depends on:** `api.auth.require_auth (router-level dependency)`, `api.routes._common`, `pipeline.statcast_events` (the pitch-result code sets and their SQL helpers)

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** Every query hard-requires a `season` query param specifically so an indexed predicate always bounds the ~10^7-row raw.pitches scan — do not add an endpoint here that omits it. _PITCHER_METRICS/_BATTER_METRICS are allow-lists for the `metric` sort column (an unrecognized metric silently falls back to a default rather than erroring or allowing SQL injection via the ORDER BY column name). The swing, whiff, CSW (called strikes plus whiffs), called-strike and ball-in-play rates filter on the `type` code. The code sets and the helpers live in `pipeline/statcast_events.py` (the Data Lab vocabulary fix, SIM-571). The pool build and the pitcher profile read the same sets. `description` holds the feed's display text ("Called Strike", "In play, out(s)"), never Statcast's snake_case names (lower case, with underscores: "called_strike"). No query reads it: before the fix, a snake_case match found no row, so the swing rate read 0. The Data Lab's whiff is Savant's: `SAVANT_WHIFF_TYPES` is the model's `WHIFF_TYPES` plus the foul tips (`FOUL_TIP_TYPES`, 'T' and 'O'). The model's whiff set excludes the foul tip. The profiles and the pool build count a foul tip as contact. Two unit tests pin that rule. Do not import the profile computor here: it imports duckdb, numpy, pandas and sklearn, and it calls `logging.basicConfig` at import. A new route that takes `season` must take `game_type` too and call `_filter_game_type` after its own WHERE arguments; `tests/unit/test_game_type_filter.py` fails until it does. The summary page's "Games" choice (`frontend/src/pages/SummaryPage.tsx`) sends the parameter.

---

### `api/routes/ai_assistant.py`

The optional (ANTHROPIC_API_KEY-gated) natural-language Data Lab assistant. Streams a Claude tool-use conversation over SSE where the only tool available is run_sql, executed through the exact same api.routes.sql_safety path as the manual SQL console.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `status_probe(request)` | GET /api/assistant/status — reports whether app.state.anthropic_client is set (configured:true/false) and the model name, so the UI can show a setup card instead of failing a call. | `frontend Data Lab assistant panel (on mount)` | `_client` |
| `ask(request, body)` | POST /api/assistant/ask — 503 JSON if unconfigured or when the read-only pool is not open (SIM-443e), else returns a StreamingResponse of SSE frames from _run_conversation. | `frontend assistant chat input` | `_client`<br>`_pool`<br>`_run_conversation` |
| `_pool(request)` | Returns app.state.pg_pool_ro or None. SIM-443e: no fallback to the main pool. | `ask`<br>`_run_conversation` | — |
| `_run_conversation(request, body)` | Drives up to MAX_STEPS turns of the Anthropic tool-use loop: sends the system prompt + schema catalog + RUN_SQL_TOOL, for each tool_use block runs the SQL through api.routes.sql_safety.run_read_only_sql (validation errors and DB errors are fed back to the model as a tool_result rather than raised), and yields typed SSE frames (text/query/result/error/done) as it goes. | `ask` | `client.messages.create (anthropic SDK)`<br>`api.routes.sql_safety.run_read_only_sql`<br>`_sse`<br>`_block_to_dict` |

**Depends on:** `api.auth.require_auth (router-level dependency since SIM-443c; covers /status too)`, `api.routes.sql_safety`, `pipeline.statcast_events` (the system prompt renders its swing, whiff, called-strike and ball-in-play filters from the shared code sets, the Data Lab vocabulary fix, SIM-571), `anthropic SDK (only via app.state.anthropic_client, never imported directly here)`

**Used by:** `api.main.create_app`

**Environment flags read here:** `ASSISTANT_MODEL`, `MAX_ASSISTANT_STEPS`

> **Notes for anyone changing this file:** Never imports the `anthropic` package at module load — the client instance lives only on app.state (built in api.startup.open_assistant_client when ANTHROPIC_API_KEY is set), so this module and the whole app boot/import fine without the package installed or the key set. The model is fed a TRUNCATED row view (_MODEL_ROW_CAP=60) while the UI gets up to _UI_ROW_CAP=200 rows — a 'truncated' flag in the SSE result event reflects the UI cap, but the tool_result content sent back to the model is separately re-truncated/flagged at 60. Any change to the read-only guarantee belongs in sql_safety.py — this file must never call the database directly.

---

### `api/league_feed.py`

The app's one reader of the league's public endpoints: the schedule of a date (the slate) and one game's live feed (the open card), over one lazy `aiohttp` session. The lifespan attaches it as `app.state.league_feed` unless `SLATE_SCHEDULE_ENABLED=0`; a test attaches a fake with the same two coroutines, or none (the slate then serves the stored listing).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `LeagueFeed.schedule_payload(start, end)` / `game_feed_payload(game_pk)` | The raw responses; they raise on any failure (the routes degrade). Timeout `SLATE_FEED_TIMEOUT_S` (5 s). | `api.routes.games_slate`, `api.routes.games_feed` | `pipeline.mlb_schedule.schedule_params` |

---

### `api/sim_jobs.py`

One simulation run per game (the run jobs of the live slate, SIM-519). A run is a `sim.sim_runs` row: queued, running, then done, failed or cancelled. One active run per key (the SHA-256 of the runner's cache key; a partial unique index holds it across processes); `SIM_JOB_MAX_CONCURRENT` (1) runs at a time; `BatchRunner.run_job` writes progress per chunk and stops on a cancel; a done row stores the summary, the prop set (`PropDistributionSet.to_json`), the inning grids and `replay_run_id`. `recover_orphans` marks a previous process's unfinished rows `failed('restart')` at boot.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `SimJobRegistry.submit / get / latest / cancel / prop_set_row / recover_orphans` | The registry `app.state.sim_jobs`. | `api.routes.games` (POST /simulate, POST /what-if, `/boxscore`, `/props`), `api.routes.games_runs`; the `api.main` lifespan | `simulation.batch_runner.BatchRunner.run_job` |

---
