# API Layer

The FastAPI application: routes, request/response schemas, auth, and the engine-build lifespan.

*24 files documented — use the page outline (right sidebar) to jump to one.*

### `api/__init__.py`

Empty package marker for the api/ package. No code.

**Used by:** `every api.* import`

> **Notes for anyone changing this file:** File is literally empty (0 bytes) — just makes api/ a package.

---

### `api/main.py`

The FastAPI application entry point: builds the app via create_app(), wires the startup/shutdown lifespan (DB pool, Redis, the 11 similarity engines, calibration, the live pipeline, the replay DuckDB store (SIM-561: its own file, `REPLAY_DUCKDB_PATH`, opened writable as `app.state.replay_duckdb`; the analytics file stays read-only), the park-factor source, and the persistent BatchRunner), registers every router and the two ops probes (/health, /ready), and mounts the built frontend SPA when present.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `validate_environment()` | Fails fast at boot if BASEBALL_DB_DSN/REDIS_URL are missing, and (outside ENVIRONMENT=development) if SECRET_KEY/AUTH_PASSWORD are missing. | `lifespan() (api/main.py)` | `os.environ reads only` |
| `lifespan(app)` | Async context manager: on startup opens the pg pool + Redis cache, builds the pitcher engine and all 11 engines (api.state.build_pitcher_engine/build_all_engines), loads + applies win-prob calibration, optionally wires the AI-assistant Anthropic client and a read-only pg pool, optionally starts the live ingestion pipeline and the replay-persistence DuckDB connection, opens the park-factor source, builds the persistent BatchRunner (with SIM-403b shared-memory engine artifacts) and schedules a background pre-warm; on shutdown tears all of that down in reverse order. | `FastAPI(app, lifespan=lifespan) inside create_app()` | `api.state.open_pg_pool`<br>`api.state.make_pg_name_resolver`<br>`api.state.open_redis_cache`<br>`api.state.build_pitcher_engine`<br>`api.state.build_all_engines`<br>`api.state.load_calibration_report`<br>`api.state.apply_calibration_to_engines`<br>`simulation.win_probability.CalibrationMap.from_report`<br>`simulation.sim_kwargs.prime_park_factor_source / close_park_factor_source`<br>`simulation.batch_runner.BatchRunner / default_max_workers / make_cache`<br>`pipeline.batch.engine_artifacts.EngineArtifacts.load`<br>`pipeline.live.live_ingestion_pipeline.LiveIngestionPipeline`<br>`anthropic.AsyncAnthropic (optional)`<br>`duckdb.connect (optional)` |
| `_background_prewarm(runner, pool_dir)` | Runs BatchRunner.prewarm() in a background asyncio task so a slow/OOM'd worker warm-up never blocks startup. | `lifespan()` | `simulation.batch_runner.BatchRunner.prewarm` |
| `ParkFactorSourceMiddleware.dispatch()` | Stamps an X-Park-Factor-Source response header (duckdb:<n> / unavailable / unknown) on every response so a caller can see whether venue park factors were real. | `Starlette middleware stack (every request)` | `app.state.park_factor_source.header_value()` |
| `create_app()` | Builds the FastAPI app: CORS, rate-limit, latency, and park-factor middleware, then registers every router (auth, similarity, games, betting, data_health, ws/odds, metrics, sql_runner, analytics, players, schema, similarity_explorer, ai_assistant), the SPA static mount, /health, /ready, /, and the SIM-416 exception handlers. | `module bottom: `app = create_app()``<br>`tests that build a fresh app per case` | `api.auth.resolve_cors_origins/RateLimitMiddleware/LatencyMiddleware`<br>`api.errors.install_exception_handlers`<br>`api.routes.auth/similarity/games/betting/data_health/metrics/sql_runner/analytics/players/schema_introspect/similarity_explorer/ai_assistant (router objects)`<br>`pipeline.live.live_ingestion_pipeline.ws_router/odds_router`<br>`simulation.batch_runner.make_cache` |

**Depends on:** `api.auth`, `api.errors`, `api.state`, `simulation.win_probability`, `simulation.sim_kwargs`, `simulation.batch_runner`, `pipeline.batch.engine_artifacts`, `pipeline.live.live_ingestion_pipeline`, `every api.routes.* router`

**Used by:** `Dockerfile CMD (uvicorn api.main:app)`, `the whole test suite via TestClient(create_app())`

**Environment flags read here:** `BASEBALL_DB_DSN`, `REDIS_URL`, `SECRET_KEY`, `AUTH_PASSWORD`, `ENVIRONMENT`, `SIMILARITY_ENGINE_ENABLED`, `ANTHROPIC_API_KEY`, `ASSISTANT_MODEL`, `BASEBALL_DB_RO_DSN`, `LIVE_PIPELINE_ENABLED`, `REPLAY_PERSISTENCE_ENABLED`, `REPLAY_DUCKDB_PATH`, `BASEBALL_DUCKDB_PATH`, `BASEBALL_PLAY_POOL_DIR`, `SIM_RUNNER_WORKERS`, `APP_VERSION`

> **Notes for anyone changing this file:** This is the single wiring point for app.state: pg_pool, pg_pool_ro, redis_client, similarity_cache, pitcher_engine, engines (dict), calibration_report, calibration_map, anthropic_client, pipeline, sim_duckdb, park_factor_source, sim_runner, sim_cache, prewarm_task. Every route file reads one or more of these off request.app.state — changing an attribute name here silently breaks a route elsewhere (no compile-time check). The engine build is FAIL-FAST (crashes boot) but build_all_engines is per-engine resilient (skips a bad engine). Routers must be registered in the exact order shown (auth first) only because of doc ergonomics, not because FastAPI requires it — but the SPA catch-all `/{full_path:path}` MUST stay last or it will shadow every API route registered after it. sim_runner is built once and reused across requests (SIM-360); a unit test that builds the app without running the lifespan must fall back to a per-request BatchRunner (see games.py `_build_runner`).

---

### `api/state.py`

Boot-time resource builders for app.state: the 11 similarity engines, the win-probability calibration report/map, the Postgres-backed player-name resolver, the Redis-backed similarity cache, and the asyncpg pool opener. Each builder is a plain function so a unit test can call it directly without booting the app.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `build_pitcher_engine(duckdb_path=None)` | Constructs and build()s a PitcherSimilarityEngine from DuckDB; fatal on failure. | `api.main.lifespan (via asyncio.to_thread)` | `similarity.engines.pitcher_similarity.PitcherSimilarityEngine` |
| `build_all_engines(duckdb_path=None, registry=None, loader=None)` | Builds all 11 engines named in ENGINE_REGISTRY, skipping (with a WARNING log) any engine that fails to construct or build() so one bad profile table never blocks boot. | `api.main.lifespan` | `_default_engine_loader (importlib)`<br>`each similarity.engines.*.{Class}(duckdb_path=...).build()` |
| `load_calibration_report(path=None)` | Loads a persisted CalibrationReport JSON (CALIBRATION_REPORT_PATH env or arg); returns None on any missing/corrupt file (never raises). | `api.main.lifespan`<br>`load_calibration_map (this file)` | `similarity.similarity_calibration.CalibrationReport.from_json` |
| `apply_calibration_to_engines(engines, report)` | Calls apply_calibration(report) on every engine that supports it (the 8 RBF/GMM engines); returns the list of engine names it succeeded on. | `api.main.lifespan` | `engine.apply_calibration (duck-typed)` |
| `load_calibration_map(path=None)` | Convenience wrapper returning a CalibrationMap (or IDENTITY_CALIBRATION) from a persisted report. | `tests/unit/test_ml_engines_sim361.py (test/legacy only — no production caller; api/main.py does the same thing inline so the loaded report can also feed apply_calibration_to_engines)` | `load_calibration_report`<br>`simulation.win_probability.CalibrationMap.from_report` |
| `make_pg_name_resolver(pool)` | Builds an async closure resolving a set of player_ids to full names in one round trip against raw.players. | `api.main.lifespan (assigned to app.state.player_name_resolver)` | `asyncpg pool.fetch on raw.players` |
| `open_redis_cache(redis_url)` | Opens an aioredis client, pings it (fail-fast), and wraps it in RedisSimilarityCache. | `api.main.lifespan` | `redis.asyncio.from_url` |
| `open_pg_pool(dsn, min_size=2, max_size=10)` | Opens an asyncpg connection pool. | `api.main.lifespan (main pool and, when BASEBALL_DB_RO_DSN is set, the read-only pool)` | `asyncpg.create_pool` |

**Depends on:** `similarity.engines.* (11 engine classes, lazily imported)`, `similarity.similarity_calibration.CalibrationReport`, `simulation.win_probability.CalibrationMap / IDENTITY_CALIBRATION`, `asyncpg`, `redis.asyncio`

**Used by:** `api.main`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH`, `CALIBRATION_REPORT_PATH`

> **Notes for anyone changing this file:** ENGINE_REGISTRY (name -> (module_path, class_name)) is the single source of truth for app.state.engines' keys; api/routes/similarity_explorer.py's SCORE_ADAPTERS dict must stay in sync with these names or a route 404s with 'unknown engine'. build_all_engines is resilient per-engine (partial dict on failure) while build_pitcher_engine is NOT (crashes boot) — the two engines can therefore end up out of sync (pitcher_engine attached but 'pitcher' missing from engines, or vice versa is not possible since pitcher always builds first, but a caller must not assume engines['pitcher'] exists just because app.state.pitcher_engine does elsewhere). CACHE_TTL_SECONDS / make_cache_key are the contract api/routes/similarity.py uses for its Redis key scheme.

---

### `api/auth.py`

Baseline security primitives: session-cookie + API-key authentication (require_auth dependency), a stdlib in-memory rate limiter, a rolling p95 latency middleware, and the CORS-origin resolution logic. Dependency-light by design (no slowapi, no Redis) so tests and the dev environment work without extra services.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `require_auth(request)` | FastAPI dependency gating expensive/sensitive routes: pass-through in development or when nothing is configured; else accepts a valid sim_session cookie OR a valid X-API-Key header; otherwise raises 401. | `api/routes/games.py (simulate, simulate/with_override)`<br>`api/routes/betting.py (edges, signals)`<br>`api/routes/metrics.py (/metrics)`<br>`api/routes/sql_runner.py (/api/sql/run)`<br>`api/routes/analytics.py (router-level dependency, all analytics endpoints)`<br>`api/routes/ai_assistant.py (/api/assistant/ask)` | `_is_development`<br>`_configured_api_keys`<br>`_verify_session_token` |
| `_mint_session_token(subject='user') / _verify_session_token(token)` | Stateless HMAC-SHA256-signed session token (mint) and its verification (signature + expiry check, never raises). | `api/routes/auth.py login() (mint)`<br>`require_auth() and api/routes/auth.py me() (verify)` | `_get_secret_key`<br>`hmac/hashlib/base64 stdlib` |
| `cookie_kwargs(max_age=None)` | Builds the response.set_cookie/delete_cookie kwargs (httponly, secure-outside-dev, samesite=strict). | `api/routes/auth.py login()/logout()` | `_is_development`<br>`_session_ttl_seconds` |
| `RateLimitMiddleware.dispatch(request, call_next)` | Per-key (API key, else client IP) sliding-window limiter; 429 + Retry-After when the window's request count hits the configured limit. Disabled unless RATE_LIMIT_PER_MINUTE > 0. | `ASGI middleware stack, registered in api.main.create_app` | `nothing external — pure in-memory deque bookkeeping` |
| `LatencyMiddleware.dispatch(request, call_next)` | Times every non-exempt request, keeps a 200-entry ring buffer, computes p95 and stores it on app.state.api_p95_seconds; also bumps the /metrics request counter. | `ASGI middleware stack, registered in api.main.create_app` | `api.routes.metrics.record_request` |
| `resolve_cors_origins()` | Resolves the CORS allowlist: CORS_ORIGINS env, else FRONTEND_URL, else (dev only) a fixed localhost list, else a single safe localhost fallback. | `api.main.create_app (CORSMiddleware allow_origins)` | `_is_development` |

**Depends on:** `fastapi`, `starlette.middleware.base.BaseHTTPMiddleware`

**Used by:** `api.main (middleware registration)`, `api.routes.auth`, `api.routes.games`, `api.routes.betting`, `api.routes.metrics`, `api.routes.sql_runner`, `api.routes.analytics`, `api.routes.ai_assistant`

**Environment flags read here:** `ENVIRONMENT`, `SECRET_KEY`, `AUTH_PASSWORD`, `SESSION_TTL_HOURS`, `API_KEYS`, `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_ENABLED`, `CORS_ORIGINS`, `FRONTEND_URL`

> **Notes for anyone changing this file:** The rate limiter is PROCESS-LOCAL (an in-memory dict) — correct only for a single worker; a multi-worker deployment would under-count. require_auth's 'nothing configured -> pass-through' branch means a non-dev deployment that forgets to set AUTH_PASSWORD/API_KEYS silently runs unauthenticated rather than failing closed — validate_environment() in api/main.py is what actually prevents that in a real deploy (it requires SECRET_KEY + AUTH_PASSWORD outside development). _get_secret_key()/_get_auth_password() fall back to hard-coded INSECURE dev sentinels only in development; changing _is_development()'s definition changes the security posture of the whole module.

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

### `api/schemas.py`

The Pydantic v2 response-model contract layer: one model per Phase-4 simulation/betting output dataclass (GameSimSummary, PropDistribution, Linescore, EdgeReport, BetSignal, LineMovement, etc.), each with a from_dataclass() classmethod that strips numpy so the wire JSON is guaranteed to be plain Python types.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `GameSimSummaryModel.from_dataclass(summary, include_raw_arrays=True)` | Converts a simulation.results.GameSimSummary into JSON-safe fields, optionally omitting the three raw per-iteration score arrays. | `api/routes/games.py simulate_game_endpoint/simulate_with_override_endpoint` | `ConfidenceIntervalModel.from_dataclass` |
| `GameSimSummaryLite.from_dataclass(summary)` | Array-free projection of GameSimSummaryModel for list/index endpoints (day-summary cards). | `api/routes/games.py _sim_summary_lite_from_stored (via model_validate) / get_game_status_card` | `GameSimSummaryModel.from_dataclass` |
| `LinescoreModel.from_jsonable(data) / PitcherDecisionsModel.from_jsonable(data)` | Rebuilds the response model directly from a persisted to_jsonable() dict (DuckDB game-card store) rather than from a live dataclass, recomputing derived properties. | `api/routes/games.py get_game_linescore/get_game_decisions/get_game_card` | `InningLineModel construction` |
| `BoxscoreCardRowModel / BoxscoreCardModel.from_prop_set` | SIM-366: one `{prop_name -> mean}` map per player, read off each PropDistribution.mean; the keys are whichever props the player owns in the prop set, in BATTER_PROPS / PITCHER_PROPS tuple order (fifteen possible since SIM-421 — 10 for a batter, 5 for a pitcher, both for a two-way player). The frontend BoxscorePanel renders a chip per key, so a new prop needs no frontend change. SIM-560: each row also carries `name` (raw.players), `side` ('away'/'home'), `lineup_slot` (1-9, None for a non-batter) and `starting_pitcher`; the card carries the run's `base_seed`. `from_prop_set(pset, base_seed=, names=, tags=)` takes the names and the `_player_tags` maps by player id; the panel groups the rows by team, batters by slot, pitchers starter first. | `api/routes/games.py get_game_boxscore` | `simulation.prop_distributions.PropDistributionSet` |
| `EdgeReportModel.from_dataclass / BetSignalModel.from_dataclass / LineMovementModel.from_dataclass / CLVModel.from_dataclass` | Convert the betting-engine dataclasses (EdgeReport, BetSignal, LineMovement, CLV) into wire-safe models, unwrapping MarketSide enums to their .value string. | `api/routes/betting.py (edges/signals/line-movement/clv endpoints)`<br>`api/routes/games.py get_player_prop_edge` | `nested *Model.from_dataclass converters` |

**Depends on:** `api.serialization.to_jsonable (LineQuoteModel's fetched_at conversion)`, `pydantic`

**Used by:** `api.routes.games`, `api.routes.betting`

> **Notes for anyone changing this file:** Every model sets extra='forbid' via the shared _ApiModel base — a route that tries to construct one with an unexpected key raises immediately (useful for catching drift, but means adding a field to a source dataclass does NOT automatically appear on the wire; the *Model must be updated too). Six models (PlayerStatLineModel, BoxScoreModel, CalibrationMapModel, WinProbabilityModel, PropDistributionModel, PropDistributionSetModel) are deliberately reserved/unbound to any route yet per the module's own audit note — do not delete them as 'dead code' without checking BoxscoreCardModel/PropEdgeResponse, which nest the per-line/per-prop building blocks conceptually.

---

### `api/serialization.py`

The single recursive numpy-to-JSON conversion helper (to_jsonable) used everywhere a Phase-4 dataclass or a raw asyncpg/DuckDB value needs to become plain Python types before hitting json.dumps or a Pydantic model.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `to_jsonable(obj)` | Recursively converts numpy scalars/arrays, datetimes, enums, dataclass instances, mappings, and other iterables into JSON-native int/float/str/bool/None/list/dict; anything unrecognized falls back to str(obj). | `api/routes/games.py (_record_and_build, _persist_replay_artifacts, StateAtPitch/Linescore/PitcherDecisions persistence)`<br>`api/schemas.py (LineQuoteModel.from_dataclass)`<br>`api/routes/sql_safety.py (run_read_only_sql cell conversion)` | `itself, recursively` |
| `_jsonable_key(key)` | Coerces a mapping key (numpy scalar / enum) to a JSON-object-key-safe Python value before to_jsonable stringifies it. | `to_jsonable (dict branch)` | — |

**Depends on:** `numpy`

**Used by:** `api.schemas`, `api.routes.games`, `api.routes.sql_safety`

> **Notes for anyone changing this file:** Deliberately lossless — never rounds, truncates, or drops elements (a consumer that wants a summary trims at the call site, not here). np.float64 subclasses Python float and np.str_ subclasses str, so the isinstance check at the top explicitly excludes np.generic first — removing that exclusion would silently let numpy scalars leak onto the wire again.

---

### `api/routes/__init__.py`

Package docstring only; no code. Documents that each module in api/routes owns exactly one router, registered lazily in api.main.create_app.

**Used by:** `every api.routes.* import`

> **Notes for anyone changing this file:** The docstring is stale (only mentions the similarity router) — harmless, but a new engineer should not treat it as an up-to-date router index; read api/main.py's create_app() for the real list.

---

### `api/routes/_common.py`

Two tiny helpers hoisted out of games.py/data_health.py to remove copy-paste drift: the app.state.pg_pool accessor (503 on missing) and a uniform asyncpg-Record-or-dict column reader.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_get_pool(request)` | Returns request.app.state.pg_pool, or raises 503 if the lifespan never attached one. | `api/routes/games.py`<br>`api/routes/betting.py`<br>`api/routes/data_health.py`<br>`api/routes/players.py`<br>`api/routes/schema_introspect.py`<br>`api/routes/analytics.py`<br>`api/routes/sql_runner.py (indirectly via _ro_pool)` | — |
| `_row_get(row, key, default=None)` | Reads key from an asyncpg Record OR a plain dict uniformly, collapsing a present-but-None value to default. | `api/routes/games.py`<br>`api/routes/data_health.py`<br>`api/routes/players.py`<br>`api/routes/schema_introspect.py` | — |

**Depends on:** `fastapi`

**Used by:** `api.routes.games`, `api.routes.betting`, `api.routes.data_health`, `api.routes.players`, `api.routes.schema_introspect`, `api.routes.sql_runner`, `api.routes.analytics`

> **Notes for anyone changing this file:** This is the canonical implementation chosen by the 2026-06-03 audit's 'api/ duplication' cleanup — do not re-introduce a per-file copy of either helper; every other route module should import from here.

---

### `api/routes/auth.py`

Browser session-auth HTTP endpoints: POST /auth/login (password -> httpOnly cookie), GET /auth/me (probe auth state, always 200), POST /auth/logout (clear cookie). Registered with no router prefix so the paths are exactly /auth/*.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `login(body, response)` | Validates the posted password against AUTH_PASSWORD (constant-time compare) and, on success, mints and sets a signed session cookie. | `frontend login form (POST /auth/login)` | `api.auth._get_auth_password / _mint_session_token / cookie_kwargs` |
| `me(request)` | Never raises 401 — reports authenticated/mode/expires_in so the frontend can decide whether to show the login page on boot. | `frontend app-boot check (GET /auth/me)` | `api.auth._is_development / _verify_session_token / _configured_api_keys` |
| `logout(response)` | Idempotently clears the session cookie. | `frontend logout action (POST /auth/logout)` | `api.auth.cookie_kwargs` |

**Depends on:** `api.auth`

**Used by:** `api.main.create_app (registered first)`

> **Notes for anyone changing this file:** No server-side session store — the cookie IS the session (HMAC-signed, self-expiring). Programmatic clients bypass this router entirely and use X-API-Key headers against require_auth directly.

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

> **Notes for anyone changing this file:** Guarded by require_auth (SIM-374's auth gate list). The module keeps a single hand-rolled fallback scrape counter at module scope (_FALLBACK_SCRAPE_COUNT) which resets on process restart — expected, not a bug. Only requests_total/sim_latency/api_p95/pipeline_freshness are 'live'; the rest of the app never calls these seams unless another route explicitly imports them (record_sim_latency is currently called from exactly one place).

---

### `api/routes/games.py`

The core Phase-5 game-simulation HTTP surface: list games on a date, run/replay Monte-Carlo simulations (with optional roster override), and serve the persisted play-by-play, per-pitch state, linescore, pitcher decisions, boxscore-card, and player-prop-edge views derived from those runs. The biggest and most-touched route file; also the module several other files (betting.py) import shared helpers from.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_factory_ref(request)` | The testability seam for the StateMachine factory: app.state.sim_factory_ref -> $SIM_MACHINE_FACTORY_REF env -> the module-level PRODUCTION_FACTORY_REF ('simulation.production_factory:production_machine_factory'). | `every /simulate-family handler in this file`<br>`api/routes/betting.py _summary_and_winprob` | — |
| `_resolve_state_or_error(pool, game_pk)` | Resolves a game's GameState via simulation.lineup_resolver, mapping LineupNotIngestedError->503 (Retry-After 900), LineupIncompleteError->503 (the same Retry-After; SIM-559: a known game whose lineup rows do not make a playable game — a side with no pitcher, an empty batting order) and LineupResolutionError->404 (the game is unknown); stamps the state with the UNRESOLVED_PARK_FACTOR sentinel. | `simulate_game_endpoint, simulate_with_override_endpoint, get_game_boxscore, get_player_prop_edge`<br>`api/routes/betting.py _summary_and_winprob` | `simulation.lineup_resolver.resolve_game_state`<br>`simulation.sim_kwargs.mark_park_factor_unresolved` |
| `_resolved_sim_kwargs(request, pool, state, game_pk)` | SIM-452: the ONE path every route uses to build sim kwargs — resolves the venue park factor onto state first, then delegates to simulation.sim_kwargs.build_sim_kwargs (raises UnresolvedParkFactorError if a caller skips this and calls the bare builder). | `simulate_game_endpoint, simulate_with_override_endpoint, get_game_boxscore, get_player_prop_edge`<br>`api/routes/betting.py _summary_and_winprob` | `simulation.sim_kwargs.build_sim_kwargs` |
| `_build_runner(request)` | Returns the shared app.state.sim_runner (persistent, warm ProcessPoolExecutor) if the lifespan attached one, else builds a transient single-worker BatchRunner (the fast synchronous unit-test path). | `every /simulate-family handler here`<br>`api/routes/betting.py _summary_and_winprob` | `simulation.batch_runner.BatchRunner` |
| `_run_batch(runner, spec, n_iterations, base_seed, use_cache)` | Thin sync wrapper around BatchRunner.run so the route can await asyncio.to_thread(...) it. | `simulate_game_endpoint, simulate_with_override_endpoint`<br>`api/routes/betting.py _summary_and_winprob` | `simulation.batch_runner.BatchRunner.run` |
| `simulate_game_endpoint(game_pk, request, n_iterations, base_seed, use_cache)` | GET /api/games/{game_pk}/simulate — resolves the lineup, runs an N-iteration Monte-Carlo batch, records the per-run latency metric, best-effort persists the replay artifacts, and returns the GameSimSummary as JSON. | `frontend game page; tests/unit/test_api_games.py` | `_resolve_state_or_error`<br>`resolve_factory_ref`<br>`_resolved_sim_kwargs`<br>`_build_runner`<br>`_run_batch`<br>`api.routes.metrics.record_sim_latency`<br>`_persist_replay_artifacts`<br>`api.schemas.GameSimSummaryModel.from_dataclass` |
| `simulate_with_override_endpoint(game_pk, override, request, ...)` | POST /api/games/{game_pk}/simulate/with_override — runs a baseline AND an override batch at the same seed and returns both summaries plus the OverrideDelta. | `frontend managerial-override UI` | `_resolve_state_or_error`<br>`_resolved_sim_kwargs`<br>`_apply_override`<br>`_run_batch (x2)`<br>`simulation.snapshots.OverrideDelta.from_summaries` |
| `_persist_replay_artifacts(request, game_pk, factory_ref, base_seed, sim_kwargs, batch=None) -> run_id or None` | Best-effort: records one game ON THE WORKER POOL (SIM-561, `BatchRunner.record_game`; the API process never loads the sim bundle), builds its artifacts and stores them in the replay file under the FILE'S OWN next run id. Only a /simulate batch (`batch` given) also writes a Postgres sim-run history row (`_store_history_run`, its own id); a sample game or the projections' first game writes none — a one-game summary is not a Monte-Carlo run. Returns the replay run id; never raises (None on any failure). | `simulate_game_endpoint`, `get_game_boxscore` (the run's first game, in parallel with the batch), `simulate_sample_game` | `BatchRunner.record_game`<br>`_build_replay_artifacts`<br>`_store_replay_run`<br>`_store_history_run`<br>`_resolve_lineup_best_effort` |
| `_store_replay_run(cur, game_pk, built, base_seed) -> run_id` | SIM-561: under `_REPLAY_WRITE_LOCK` (one replay write at a time in the process: the id is max + 1, and two prunes of one game conflict in DuckDB), draws `next_replay_run_id`, writes the play stream, the snapshots and the game card (with the seed) in ONE transaction, then prunes the game's older runs; a prune failure logs a warning and keeps the game. | `_persist_replay_artifacts (via _replay_call in a worker thread)` | `db.sim_store.next_replay_run_id/store_play_stream/store_state_snapshots/store_game_card/prune_replay_runs` |
| `_get_replay_store(request)` / `_replay_call(con, fn, **kw)` | SIM-561: the replay file's connection (`app.state.replay_duckdb`, None = the store is off), and a helper that runs one store call on a cursor of its own (a DuckDB connection is not safe across threads). `_get_sim_duckdb` stays the analytics handle for the sim kwargs. | every replay read and write here | — |
| `_record_and_build(factory_ref, base_seed, sim_kwargs, resolved=None)` / `_build_replay_artifacts(recorded, resolved=None)` | `_build_replay_artifacts` turns one `RecordedGame` (the game, its plays, the state before each — SIM-561) into a `ReplayArtifacts`: the PlayByPlay (each entry with its inning, half, outs, batter, pitcher and score), per-pitch StateAtPitch snapshots (optionally with the 9 fielders via a defense map), the Linescore, the PitcherDecisions and the game result. `_record_and_build` records in THIS process and builds; the routes record on the pool instead. | `_persist_replay_artifacts (offloaded via asyncio.to_thread)` | `simulation.play_recorder.record_game`<br>`simulation.snapshots.PlayByPlay.from_play_results`<br>`simulation.linescore.linescore_from_plays`<br>`simulation.pitcher_decisions.decisions_from_plays`<br>`simulation.lineup_resolver.build_defense_map_for_state` |
| `get_games_on_date(date, request, use_cache)` | GET /api/games/{date} — SIM-519: the league's schedule says which games exist and their state (`pipeline.mlb_schedule.card_state`); our data merges per game in two queries (`_SLATE_ENRICH_SOURCE_SQL`: the lineup flag and source; `_SLATE_RUNS_SQL`: the newest run). The schedule is cached by date class (`_slate_ttl`: 20 s today, 10 min ahead, 24 h past). On a league error: the last good schedule (`slate:last:{date}`), else the stored listing (memoized 300 s) mapped by `card_state_from_raw`; the envelope's `source` says which. Records are the schedule's (D1). | `frontend Daily Diamond slate` | `_slate_schedule_payload`<br>`_slate_merge_rows`<br>`_schedule_card`<br>`_slate_from_db` |
| `get_game_feed(game_pk, request, use_cache)` | GET /api/games/{game_pk}/feed — SIM-519 Part I: the league's live feed reduced to the slate card's detail (`pipeline.mlb_game_feed.parse_game_feed` to `GameFeedCardModel`). Cached 10 s live, 10 min preview, 24 h final; the last good copy on an error (`source = feed_cached`), else 503 with Retry-After 30. | `frontend open slate card` | `app.state.league_feed`<br>`pipeline.mlb_game_feed` |
| `start_simulation_run` / `get_latest_simulation_run` / `get_simulation_run` / `cancel_simulation_run` | SIM-519 Part E: POST /simulate (202; queues a run or returns the active one with the same key), GET /simulate/runs/latest and /runs/{run_id}, DELETE /runs/{run_id} (409 once finished). Before Alembic 0029 the reads answer 404 and the POST 503. `/boxscore` and `/props` read a stored run with `run_id=` or `latest_run=true`. | `frontend Simulation card` | `api.sim_jobs.SimJobRegistry`<br>`_persist_replay_artifacts` (the representative game) |
| `get_game_status_card(game_pk, request)` | GET /api/games/{game_pk}/status — SIM-519: also merges the game's schedule entry (`_schedule_game_for`: state, start, series, probables, live or final score) for the game page header. The SIM-384 aggregate: enriched identity + 3-state GameStatus + the most-recently persisted GameSimSummaryLite. SIM-546: `_sim_summary_lite_from_stored` drops the stored summary's `inning_grids` with the three score arrays (the lite model forbids unknown fields). It also fills each interval's missing `half_width` from `(high - low) / 2` (`_with_half_width`): `to_jsonable` writes no `half_width`, because it is a property of the interval dataclass, so before this fix the projection returned None for every stored summary and the card's `sim_summary` was always None. | `frontend 3-state game cards` | `_get_pool`<br>`db.sim_store.load_latest_sim_run`<br>`_sim_summary_lite_from_stored` |
| `get_live_game_state(game_pk, request)` | GET /api/games/{game_pk}/live — reads sim.lineup_state's JSONB game_state written by the live ingestion pipeline. | `frontend live in-progress game view` | `_get_pool`<br>`db.sim_store.load_live_game_state` |
| `get_game_plays / get_game_state_at_pitch / get_game_linescore / get_game_decisions / get_game_card` | Read the replay file's artifacts (play-by-play, per-pitch state, linescore, W/L/S decisions, or the combined card) for a game's NEWEST COMPLETE run (SIM-561: the newest run with a game card; before, /plays concatenated every stored run). /plays takes an optional `run_id` and returns the `run_id`, the `base_seed` and a `names` map; /card returns the run's `run_id` and `base_seed` too, so the page reads the card and then that run's plays for every batter and pitcher (`_replay_player_names`: raw.players, and 'Generic reliever N' for a placeholder arm). 404/503 when nothing is persisted / the store is off. | `frontend play-by-play scroll, field graphic, linescore view` | `_read_newest_plays/_read_newest_state`<br>`db.sim_store.latest_replay_run_id/load_play_stream/load_state_at/load_game_card`<br>`_state_at_pitch_model_from_snapshot`<br>`api.schemas.LinescoreModel.from_jsonable/PitcherDecisionsModel.from_jsonable` |
| `simulate_sample_game(game_pk, request, base_seed=None)` | SIM-561: POST /api/games/{game_pk}/sample-game — simulates ONE game at `base_seed` (random when omitted) and stores it, so the game page's linescore and play-by-play show it; returns `{game_pk, run_id, base_seed}`. 503 when the store is off or the lineup is not usable, 404 for an unknown game, 500 when the store failed. | `frontend game page "Simulate a game" button` | `_resolve_state_or_error`<br>`_resolved_sim_kwargs`<br>`_persist_replay_artifacts` |
| `get_game_boxscore(game_pk, request, n_iterations, base_seed)` | GET /api/games/{game_pk}/boxscore — runs an N-game batch on the app's worker pool (via _build_prop_set; SIM-560, was a serial replay in the API process) and returns each player's prop-mean card: fifteen props since SIM-421 (a batter's H/HR/RBI/TB/1B/2B/3B/R/SB/HRR means, a pitcher's K/BB/ER/OUTS/H_ALLOWED means), in the order of simulation.prop_distributions' BATTER_PROPS / PITCHER_PROPS tuples, plus each player's name, side, slot and starter flag. | `frontend boxscore view` | `_resolve_state_or_error`<br>`_resolved_sim_kwargs`<br>`_build_runner`<br>`_build_prop_set`<br>`_player_names`<br>`_player_tags`<br>`api.schemas.BoxscoreCardModel.from_prop_set` |
| `get_player_prop_edge(game_pk, player_id, prop, request, ...)` | GET /api/games/{game_pk}/props/{player_id}/{prop} — full PMF for one player's prop, optional over/under probabilities, optional CLV-engine edge report. SIM-560: with the card's seed and N it reads the card's cached run (a click returns in about 0.1 s). Valid props (`_VALID_PROP_NAMES` = simulation.prop_distributions.ALL_PROPS, 15 since SIM-421): K, BB, ER, OUTS, H_ALLOWED (pitcher); H, HR, RBI, TB, 1B, 2B, 3B, R, SB, HRR (batter; HRR = hits + runs + RBI as one per-game sum). | `frontend player-prop card` | `_build_prop_set`<br>`betting.prop_edge_report`<br>`api.schemas.EdgeReportModel.from_dataclass` |
| `_build_prop_set(runner, spec, n_iterations, base_seed)` | SIM-560: the run's PropDistributionSet from `BatchRunner.run_prop_set` — the N games on the worker pool, a seeded set cached 15 minutes, so the card and every prop the user then clicks read one run. Sync; the routes offload it with asyncio.to_thread. | `get_game_boxscore, get_player_prop_edge` | `simulation.batch_runner.BatchRunner.run_prop_set` |
| `_player_tags(sim_kwargs)` | SIM-560: tags every player the game's sim kwargs name — the two batting orders (side + 1-9 slot), the two starters (`starting_pitcher`), each side's pen (`bullpen`, 0 = away, 1 = home), and the factory's placeholder arms that a game with no pen in the box pitches (`_placeholder_pens`; `_placeholder_names` labels them 'Generic reliever 1'–'6') — as `_PlayerTag(side, lineup_slot, starting_pitcher)`. | `get_game_boxscore` | — |
| `_player_names(pool, player_ids)` / `_query_player_names` | SIM-560: each player's `full_name` from raw.players. Best-effort: a failed lookup logs a warning and returns `{}`, and the card's names stay empty. | `get_game_boxscore` | `api.routes._common._row_get` |

**Depends on:** `api.auth.require_auth`, `api.routes._common`, `api.schemas`, `api.serialization.to_jsonable`, `betting (MarketSide, OddsQuote, TwoWayMarket, prop_edge_report)`, `db.sim_store`, `simulation.batch_runner`, `simulation.linescore`, `simulation.lineup_resolver`, `simulation.pitcher_decisions`, `simulation.play_recorder`, `simulation.prop_distributions`, `simulation.sim_kwargs`, `simulation.snapshots`

**Used by:** `api.main.create_app`, `api.routes.betting (imports _build_runner, _resolve_state_or_error, _resolved_sim_kwargs, _run_batch, resolve_factory_ref directly)`

**Environment flags read here:** `SIM_MACHINE_FACTORY_REF`

> **Notes for anyone changing this file:** The valid-prop set is NOT a literal here — `_VALID_PROP_NAMES` is built from simulation.prop_distributions.ALL_PROPS, so a prop added there is accepted by /props automatically; only the two human-readable route descriptions (the /boxscore and /props `description=` strings) list the names and must be updated by hand (SIM-421 did). The frontend's generated OpenAPI mirror (frontend/openapi.json, frontend/src/api/schema.d.ts) copies those strings and needs a regenerate via scripts/export_openapi.py after such an edit — as of 2026-09-12 it still shows the old eight-prop text. betting.py imports FIVE private helpers straight out of this module — this is a real coupling, not an accident: changing any of _build_runner/_resolve_state_or_error/_resolved_sim_kwargs/_run_batch/resolve_factory_ref's signature breaks betting.py too. PRODUCTION_FACTORY_REF must stay a dotted-string ref (not a direct import) because tests monkeypatch the module attribute to swap in the no-DB rng factory. Every sim-kwargs call MUST go through _resolved_sim_kwargs (never simulation.sim_kwargs.build_sim_kwargs directly) or it raises UnresolvedParkFactorError — this was a real, fixed bug class (SIM-452) where 5 of 8 call sites silently ran park-blind. The DuckDB replay store (app.state.sim_duckdb) is a completely separate connection from the Postgres pool; a missing one degrades /plays,/state,/linescore,/decisions,/card to 503/404 without touching /simulate itself.

---

### `api/routes/betting.py`

The betting/CLV HTTP surface: per-market edge reports for the fifteen game markets (SIM-546, 2026-10-09: the three full-game markets — moneyline, total, run line — and the twelve segment and team markets), ranked +EV bet signals, and the opening->closing line-movement / CLV-snapshot time-series. Reuses the games.py sim seam directly rather than duplicating it. A *segment and team market* settles on the first inning, the first five innings, one team's runs, or the team that scores first (`pipeline.odds_provider.GAME_MARKET_TYPES` lists all fifteen).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `_summary_and_winprob(request, game_pk, n_iterations, base_seed, use_cache)` | Resolves the lineup, runs (or reuses via cache) the SAME sim machinery as /simulate, and derives a calibrated WinProbability from the summary. | `get_game_edges`<br>`get_game_signals` | `api.routes.games._resolve_state_or_error / _resolved_sim_kwargs / _build_runner / _run_batch / resolve_factory_ref`<br>`simulation.win_probability.win_probability` |
| `_build_edge_reports(summary, win_prob, *, game_pk, markets, ..., away_run_line=None, stored=None, injected=None) -> _EdgeBuild` | Builds EdgeReports for the requested markets, every side of each, from injected odds, the stored rows or the deterministic mock provider; skips (rather than 500s) a side whose sim probability is exactly 0/1. SIM-549: the away run line is the injected `away_run_line`, else the mirror of an injected home line, else the provider's own away spread. A pair de-vigs the two prices against each other; two separate bets price each side at its own spread over the game's two-way margin (the total, then the moneyline, then 1.05). SIM-546: the three full-game markets price as before and keep their labels (`moneyline`, `total`, `run_line`). The twelve segment and team markets price off the summary's inning grid (`simulation.game_market_distributions.segment_runs_from_summary`) through `_price_segment_market`; each of their reports carries its market type as its label (`f5_total`). A summary with no grid (a cache entry pickled before the change, or a summary built by hand in a test) logs one INFO line and prices the full-game markets only. It returns an `_EdgeBuild` (a NamedTuple): the reports, `odds_source`, `run_line_pricing` (the full-game run line), `price_book`, `fair_book` and `run_line_pricing_by_label` (every priced run line, by report label). Only the full-game moneyline goes through the fitted reliability curve; the twelve segment and team probabilities are the raw simulated frequencies. | `_priced_edge_reports` (for `get_game_edges` and `get_game_signals`) | `betting.clv_engine.moneyline_edge_report/total_over_under_edge_report/run_line_edge_report`<br>`_price_run_line`<br>`_price_segment_market`<br>`_mock_odds`<br>`_safe_report` |
| `_segment_quote / _price_segment_market / _price_run_line` | SIM-546: one segment or team market. `_segment_quote` picks its prices: the `prices` document's entry, else the graded stored row, else the mock. `_price_segment_market` prices every side by the market's kind. A total, a team total and the yes / no market ("a run in the first inning", stored as over / under) go through `samples_over_under_edge_report` over `SegmentRuns.market_samples`; the yes / no market is priced at line 0.5 whatever the row says. The first-inning and first-five moneylines are THREE-WAY markets (the tie is a third priced outcome): they go through `three_way_edge_report` and carry a `draw` side. The first team to score goes through `_build_edge_report` with `side_probabilities`. A segment run line goes through `_price_run_line` over `SegmentRuns.segment_margin`. `_price_run_line` is the one run-line pricer for the full game and both segments: a pair or two separate bets, as above. | `_build_edge_reports` | `simulation.game_market_distributions (SegmentRuns, side_probabilities)`<br>`betting.clv_engine.samples_over_under_edge_report / three_way_edge_report / _build_edge_report / run_line_edge_report / one_sided_edge_report` |
| `_parse_prices / _split_prices` | SIM-546 (owner decision 2, 2026-10-09): the `prices` query param on /edges and /signals. It is a JSON document keyed by market type, and it may name any of the fifteen markets. Each entry holds its kind's fields: `home_ml` and `away_ml` (a moneyline kind); those two and `draw_ml` (a three-way market); `over_ml`, `under_ml` and `line` (a total kind; `first_inning_run` takes an optional `line` and prices at 0.5); `home_ml`, `away_ml`, `home_line` and `away_line` (a run line). A 422 names the market and the fault: a document that does not parse or is not an object, an unknown market, an entry that is not an object, an unknown field, a missing field, a field that is not a finite number, an American price of 0, or a market also given by the named query params. The route parses the document before the simulation runs. A market in the document is priced from it alone (`odds_source` reads `injected`; no stored row is read for it). `_split_prices` turns a full-game market's entry into its named params, so it prices exactly as the named params do. | `get_game_edges`<br>`get_game_signals` | `pipeline.odds_provider.GAME_MARKET_KIND / GAME_MARKET_TYPES` |
| `_read_stored_rows / _stored_rows_by_market / _stored_market / _at_offered_price` | SIM-555 (2026-09-28): the STORED price source, between the injected query params and the mock. A market with no injected price reads the game's stored one-book rows (`bp:` only; the listed sportsbooks only, `bettable_labels()` — never the blend, a pick'em app, an exchange, a prediction market or an unlisted id), the latest row per (market, book): the closing rows, and the `current` rows only while the game is still in `Preview` (a pre-game line; an in-play or after-the-game line is never read). Each row must pass the load guard. The FAIR probability comes from ONE book's row — the first book on `GRADED_BOOK_PREFERENCE` with a row — so no report de-vigs two books against each other; each side's OFFERED price is the best sportsbook price at that book's line, and the EV is re-read at it. The response names the fair book per market and the book of each side's offered price; `odds_source` reads `injected` / `stored` / `mock`. A run line listed as two bets uses the fair book's own two-way margin. No pool, no stored row or a failed read → the mock, as before. SIM-546: the read asks for the markets `_stored_read_markets` names, in vocabulary order: the three full-game markets always (a two-bets run line reads the total and the moneyline for its margin), every requested market, and the segment total of a requested segment run line. It selects `draw_ml`. The side and line columns come from one table keyed by the market's kind (`_STORED_SIDE_COLUMNS`). A three-way row with no `draw_ml` is not usable, so the graded row is the first book with a complete row. A segment run line listed as two bets reads the fair book's own segment total first, then the full-game total, then the moneyline, then the flat 1.05 (`_reference_order`, the order the accuracy comparison uses). | `_build_edge_reports` | `pipeline.odds_provider (graded order, bettable_labels, is_bettable)`<br>`pipeline.odds_row_guard.check_row`<br>`betting.clv_engine.expected_value` |
| `get_game_edges(game_pk, request, ...)` | GET /api/betting/games/{game_pk}/edges — the per-market EdgeReportModel list. SIM-546: the default request prices all fifteen game markets; `?markets=` narrows (a 422 on an unknown market). The response's `markets` lists the markets priced, in vocabulary order (`markets=total,moneyline` returns `["moneyline", "total"]`). A report's `side` is `home`, `away`, `over`, `under` or `draw` (the tie of a first-inning or first-five moneyline). Two response maps are new: `market_names` (report label -> plain name, "First five total", from `pipeline.odds_provider.GAME_MARKET_NAMES`; the priced markets only) and `run_line_pricing_by_label` (report label -> `RunLinePricingModel` for every priced run line: `run_line`, `f1_runline`, `f5_runline`). `run_line_pricing` keeps its shape for the full-game run line. The `prices` param (`_parse_prices`) hands in prices for any market. | `frontend betting card` | `_summary_and_winprob`<br>`_parse_markets`<br>`_parse_prices`<br>`_priced_edge_reports`<br>`api.schemas.EdgeReportModel.from_dataclass` |
| `get_game_signals(game_pk, request, ...)` | GET /api/betting/games/{game_pk}/signals — the same edges, gated to +EV and ranked by EV via bet_signals_from_edges. SIM-546: the same fifteen markets, the same `prices` param and the same two maps (`market_names`, `run_line_pricing_by_label`) on `SignalsResponse`. The signals fire on every market, the `draw` side included (owner decision 4, 2026-10-09), although the twelve segment and team probabilities are not calibrated. | `frontend betting card` | `_summary_and_winprob`<br>`_parse_prices`<br>`_priced_edge_reports`<br>`betting.bet_signal.bet_signals_from_edges` |
| `get_game_line_movement / get_game_clv` | GET .../line-movement and .../clv — read raw.game_odds history and build the opening->closing time-series (and a CLV-only projection of it). SIM-555: one-book rows only (`bp:`), and each series carries `book_name`. | `frontend CLV chart` | `_fetch_movements`<br>`betting.line_movement.fetch_line_movement` |

**Depends on:** `api.auth.require_auth`, `api.routes._common`, `api.routes.games (5 private helpers)`, `api.schemas`, `betting.bet_signal`, `betting.clv_engine` (SIM-546: also the private `_build_edge_report`, for the first team to score), `betting.line_movement`, `pipeline.odds_provider (the market vocabulary and GAME_MARKET_NAMES)`, `simulation.batch_runner.GameSpec`, `simulation.game_market_distributions`, `simulation.win_probability`, `pipeline.live.live_ingestion_pipeline.MockOddsAPI (lazy import)`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** Odds are layered: an injected query param always wins over the deterministic MockOddsAPI fallback (seeded on game_pk, so it's reproducible with no live feed). Line-movement/CLV are pool-backed only — there is no mock fallback for a real time-series, so they 503 without app.state.pg_pool even though edges/signals would still work via the mock provider. _safe_report swallowing a ValueError (degenerate 0/1 sim probability) means a market can silently disappear from the response rather than erroring — a caller must check `markets` vs `len(edges)` to notice a skipped side. SIM-546: this file imports new `betting/` names (`samples_over_under_edge_report`, `three_way_edge_report`, `MarketSide.DRAW`). `betting/` is baked into the app image and `api/` hot-reloads, so rebuild the image (`docker compose build app`, then `docker compose up -d app`) before an `api/` edit that imports a new `betting/` name reaches the running app, or the reload fails to import (CLAUDE.md §2a). The line-movement and CLV routes still know the three full-game markets only (`betting/line_movement.py`); widening them to the twelve is a follow-on of the SIM-546 design (§11).

---

### `api/routes/similarity.py`

v1 Similarity Score Explorer: the pitcher-vs-pitcher engine's histogram/top-N distribution endpoint. Thin route handler over pure, separately-unit-tested helpers (binning, score-summary stats, diagnostic classification).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_pitcher_similarity_distribution(pitcher_id, season, bins, top_n, engine, name_resolver, cache)` | GET /api/similarity/pitcher/{pitcher_id}/{season} — queries the engine (with a 24h Redis cache), resolves names, and returns the score summary, histogram bins, top-N, and a diagnostic classification. | `frontend Similarity Explorer (pitcher view)` | `engine.query (PitcherSimilarityEngine)`<br>`compute_score_summary`<br>`build_bins`<br>`build_top_n`<br>`classify_diagnostic` |
| `get_pitcher_engine(request) / get_name_resolver(request) / get_similarity_cache(request)` | FastAPI dependencies pulling pitcher_engine / player_name_resolver / similarity_cache off app.state, with graceful degradation (placeholder names, no cache) and a 503 when the engine isn't attached. | `get_pitcher_similarity_distribution (Depends)`<br>`api/routes/similarity_explorer.py (_resolve_names reuses get_name_resolver)` | — |
| `compute_score_summary(scores) / classify_diagnostic(scores, n_bins) / build_bins / build_top_n / _BinSpec` | Pure, numpy-free statistics and binning helpers (min/percentiles/mean/std, COLLAPSED/NO_SPREAD/HEALTHY classification, histogram bucketing). | `get_pitcher_similarity_distribution`<br>`api/routes/similarity_explorer.py (reused directly for all other engines)` | — |

**Depends on:** `api.state (CACHE_TTL_SECONDS, NameResolver, SimilarityCache, make_cache_key)`, `similarity.engines.pitcher_similarity`

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

**Depends on:** `api.routes.similarity (_BinSpec, classify_diagnostic, compute_score_summary, get_name_resolver)`, `similarity.engines.situation_similarity.SituationVector (lazy import)`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** SCORE_ADAPTERS' keys MUST exactly match api.state.ENGINE_REGISTRY's keys (both dicts are hand-maintained separately) — adding a 12th engine to state.py without adding an adapter here means the new engine is buildable but never exposed through this explorer. The composite score is explicitly NOT a literal weighted sum of the listed sub-scores (each engine applies its own confidence discount) — the module's own docstring calls this out as an honesty requirement, so a UI author must not reconstruct the score bar-by-bar from the weights shown.

---

### `api/routes/data_health.py`

SIM-417 data-freshness endpoint for the UI's 'data as of...' badge: aggregate ingest watermark and per-season game/pitch coverage from raw.etl_data_freshness / raw.games / raw.pitches.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_data_freshness(request)` | GET /api/data/freshness — runs one aggregate query plus a per-season GROUP BY and returns last_ingest_at, latest_game_date, totals, and per-season SeasonCoverage rows. | `frontend header/data-status badge` | `api.routes._common._get_pool/_row_get` |

**Depends on:** `api.routes._common`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** No auth dependency — deliberately unauthenticated (pure aggregate counts, nothing sensitive), sitting alongside /health and /ready. An empty store returns zeros, never an error.

---

### `api/routes/sql_safety.py`

The single shared read-only SQL validation + execution path used by BOTH the SQL Console and the AI Assistant, so the assistant is physically incapable of running anything the console couldn't. Defense-in-depth: statement validation before the DB, a read-only transaction, a statement timeout, and a hard SQL-enforced row cap.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `validate_read_only_sql(sql, max_len=MAX_SQL_LEN)` | Strips comments/string/dollar/quoted-identifier noise, rejects empty/oversized input, anything not starting with SELECT/WITH, any forbidden write/DDL/session keyword anywhere (even inside a CTE), multiple statements, and a blocklist of dangerous functions (including inside double-quoted identifiers). Returns the executable (semicolon-stripped) SQL string. | `run_read_only_sql (this file)`<br>`tests/unit/test_sql_safety.py` | `_strip_sql_noise` |
| `run_read_only_sql(pool, sql, max_rows=DEFAULT_MAX_ROWS, timeout_ms=DEFAULT_TIMEOUT_MS)` | Validates the query, wraps it in a row-capped subquery, executes it inside a read-only transaction with SET LOCAL statement_timeout, and returns a JSON-safe {columns, rows, row_count, truncated, elapsed_ms} payload. | `api/routes/sql_runner.py run_sql`<br>`api/routes/ai_assistant.py _run_conversation` | `validate_read_only_sql`<br>`_execute`<br>`api.serialization.to_jsonable` |
| `_strip_sql_noise(sql, keep_identifiers=False)` | Blanks out comments and string literals (and, unless keep_identifiers, quoted identifiers) while preserving character offsets, so keyword/function scans never trip on literal text — but preserves quoted-identifier TEXT when keep_identifiers=True because a quoted identifier is executable code (Postgres folds unquoted names to lower case, so \"pg_read_file\" IS pg_read_file). | `validate_read_only_sql (called twice: once blanking identifiers for the keyword scan, once preserving them for the dangerous-function scan)` | — |

**Depends on:** `api.serialization.to_jsonable`

**Used by:** `api.routes.sql_runner`, `api.routes.ai_assistant`

**Environment flags read here:** `SQL_RUNNER_MAX_ROWS`, `SQL_RUNNER_TIMEOUT_MS`, `SQL_RUNNER_MAX_LEN`

> **Notes for anyone changing this file:** SECURITY-CRITICAL FILE (documented inline as SIM-442): the double-quoted-identifier handling was previously a real vulnerability — blanking a quoted identifier's contents let `SELECT \"pg_read_file\"('/etc/passwd')` slip past every entry in _DANGEROUS_TOKENS because the scanned copy no longer contained the function name, while the ORIGINAL (unblanked) string still executed. Any future edit to _strip_sql_noise or the two validate_read_only_sql scan passes must preserve the keep_identifiers=True pass for the dangerous-function scan or this bug reopens. raw.* (Postgres) is the ONLY reachable schema — derived.*/sim.* live in a separate DuckDB process and simply cannot be joined here, which is why the assistant's system prompt and the schema browser only expose raw.*. _HARD_MAX_ROWS/_HARD_MAX_TIMEOUT_MS are ceilings the env-var tunables can never exceed even if misconfigured.

---

### `api/routes/sql_runner.py`

POST /api/sql/run — the Data Lab SQL Console's HTTP endpoint. A thin wrapper that delegates all safety/execution to api.routes.sql_safety, so it is the single place that must be authenticated (require_auth) for this capability.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `run_sql(request, body)` | Runs body.sql through run_read_only_sql against the read-only pool (or the main pool) and maps SqlValidationError / any DB exception to a 400 with the message verbatim in detail. | `frontend SQL Console` | `_ro_pool`<br>`api.routes.sql_safety.run_read_only_sql` |
| `_ro_pool(request)` | Prefers app.state.pg_pool_ro (a GRANT-SELECT-only role) if attached, else falls back to the main pool (still executed inside a read-only transaction). | `run_sql` | `api.routes._common._get_pool` |

**Depends on:** `api.auth.require_auth`, `api.routes._common`, `api.routes.sql_safety`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** Never re-implements validation — any safety change belongs in sql_safety.py, not here, to keep the console and the assistant on one path.

---

### `api/routes/players.py`

Player name typeahead + identity lookup for the Data Lab / Similarity UI, backed by raw.players (pg_trgm similarity ranking with an ILIKE fallback).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `search_players(request, q, limit)` | GET /api/players/search — ranks raw.players by pg_trgm similarity(); degrades to plain ILIKE ordering (logged once) if the extension/function is unavailable. | `frontend player search boxes (Data Lab, Similarity Explorer)` | `_fetch`<br>`api.routes._common._get_pool/_row_get` |
| `get_player(request, player_id)` | GET /api/players/{player_id} — identity + the distinct seasons the player appears in raw.pitches as pitcher or batter. | `frontend player detail panel` | `_fetch` |

**Depends on:** `api.routes._common`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** No auth dependency (pure read, no sensitive data). The pg_trgm fallback path means a search that silently ISN'T similarity-ranked (alphabetical ILIKE instead) is possible in an environment missing the extension — worth checking server logs if search relevance looks off.

---

### `api/routes/schema_introspect.py`

GET /api/schema — the raw.* table/column tree consumed by the SQL Console's schema browser and the AI assistant's grounding. Process-lifetime cached on app.state since the schema is static between deploys.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_schema(request)` | Queries information_schema.columns for table_schema='raw', groups into TableInfo/ColumnInfo, caches the built SchemaResponse on request.app.state._schema_cache. | `frontend SQL Console schema browser` | `api.routes._common._get_pool/_row_get` |

**Depends on:** `api.routes._common`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** The cache is never invalidated except by process restart — a Postgres migration that adds/removes a raw.* column will not show up until the app restarts. Only 'raw' is exposed on purpose; derived.*/sim.* are DuckDB-only and cannot be cross-joined.

---

### `api/routes/analytics.py`

Curated, parameterised (never ad-hoc) descriptive-analytics endpoints over raw.pitches for the Data Lab dashboard: KPIs, per-season coverage, pitch-mix, PA outcomes, count-state swing/whiff, velo histogram, zone whiff/called-strike, and pitcher/batter leaderboards.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `get_kpis(request)` | GET /api/analytics/kpis — fast top-line coverage using pg_class.reltuples as a row-count estimate instead of COUNT(*) on a ~10^7-row table. | `frontend Data Lab dashboard header` | `_fetch` |
| `pitch_type_mix / pa_outcomes / count_state / velo_distribution / zone` | Season-bounded, optionally player/hand-filtered GROUP BY queries returning usage/velocity/movement/outcome/whiff breakdowns; each defaults to excluding data_quality_flag=TRUE rows unless include_flagged is set. | `frontend dashboard cards` | `_fetch`<br>`_dicts`<br>`_quality_clause` |
| `pitcher_leaderboard / batter_leaderboard` | Rate-stat leaderboards (K%/BB%/CSW%/whiff% for pitchers; hard-hit%/EV/K%/BB%/HR for batters) with a minimum-PA HAVING floor, joined to raw.players for names. | `frontend leaderboard views, deep-linking into the Similarity engines` | `_fetch` |

**Depends on:** `api.auth.require_auth (router-level dependency)`, `api.routes._common`

**Used by:** `api.main.create_app`

> **Notes for anyone changing this file:** Every query hard-requires a `season` query param specifically so an indexed predicate always bounds the ~10^7-row raw.pitches scan — do not add an endpoint here that omits it. _PITCHER_METRICS/_BATTER_METRICS are allow-lists for the `metric` sort column (an unrecognized metric silently falls back to a default rather than erroring or allowing SQL injection via the ORDER BY column name).

---

### `api/routes/ai_assistant.py`

The optional (ANTHROPIC_API_KEY-gated) natural-language Data Lab assistant. Streams a Claude tool-use conversation over SSE where the only tool available is run_sql, executed through the exact same api.routes.sql_safety path as the manual SQL console.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `status_probe(request)` | GET /api/assistant/status — reports whether app.state.anthropic_client is set (configured:true/false) and the model name, so the UI can show a setup card instead of failing a call. | `frontend Data Lab assistant panel (on mount)` | `_client` |
| `ask(request, body)` | POST /api/assistant/ask — 503 JSON if unconfigured, else returns a StreamingResponse of SSE frames from _run_conversation. | `frontend assistant chat input` | `_client`<br>`_run_conversation` |
| `_run_conversation(request, body)` | Drives up to MAX_STEPS turns of the Anthropic tool-use loop: sends the system prompt + schema catalog + RUN_SQL_TOOL, for each tool_use block runs the SQL through api.routes.sql_safety.run_read_only_sql (validation errors and DB errors are fed back to the model as a tool_result rather than raised), and yields typed SSE frames (text/query/result/error/done) as it goes. | `ask` | `client.messages.create (anthropic SDK)`<br>`api.routes.sql_safety.run_read_only_sql`<br>`_sse`<br>`_block_to_dict` |

**Depends on:** `api.auth.require_auth (on /ask only)`, `api.routes.sql_safety`, `anthropic SDK (only via app.state.anthropic_client, never imported directly here)`

**Used by:** `api.main.create_app`

**Environment flags read here:** `ASSISTANT_MODEL`, `MAX_ASSISTANT_STEPS`

> **Notes for anyone changing this file:** Never imports the `anthropic` package at module load — the client instance lives only on app.state (built in api.main.lifespan when ANTHROPIC_API_KEY is set), so this module and the whole app boot/import fine without the package installed or the key set. The model is fed a TRUNCATED row view (_MODEL_ROW_CAP=60) while the UI gets up to _UI_ROW_CAP=200 rows — a 'truncated' flag in the SSE result event reflects the UI cap, but the tool_result content sent back to the model is separately re-truncated/flagged at 60. Any change to the read-only guarantee belongs in sql_safety.py — this file must never call the database directly.

---

### `api/websocket/__init__.py`

Empty placeholder package docstring — the actual WebSocket router (ws_router) lives in pipeline.live.live_ingestion_pipeline, not in this package, despite the package's name.

**Used by:** `api.websocket.schemas (sibling module)`

> **Notes for anyone changing this file:** Misleading by name: a new engineer looking for the live WebSocket handler should look in pipeline/live/live_ingestion_pipeline.py (imported and registered in api.main.create_app as ws_router/odds_router), not here. This package currently holds only schemas.py.

---

### `api/websocket/schemas.py`

Typed Pydantic v2 models for every event exchanged on /ws/games/{game_pk}: server-broadcast GameStateUpdateEvent and ResimPendingEvent, and the Ping/Pong keep-alive pair. The canonical wire-format documentation for the live WebSocket channel.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `GameStateUpdateEvent / LiveGameState / LiveOdds` | The highest-frequency event: a full live game-state snapshot (inning/outs/score/runners/lineups) plus an odds snapshot, broadcast after every live-feed tick with meaningful state. | `pipeline.live.live_ingestion_pipeline._broadcast_to_clients (server-side construction)`<br>`frontend/src/api/ws.ts (client-side mirror type, per this file's own docstring)` | — |
| `ResimPendingEvent` | Signal-only event announcing a plate-appearance just ended and a re-sim was queued; the actual result arrives later via the REST /simulate endpoint. | `pipeline.live.live_ingestion_pipeline._signal_resimulation path` | — |
| `PingEvent / PongEvent` | Server keep-alive probe (sent after 30s of silence) and the server's reply to a client 'ping' text frame. | `pipeline.live.live_ingestion_pipeline WebSocket handler` | — |

**Depends on:** `pydantic`

**Used by:** `pipeline.live.live_ingestion_pipeline (the actual WS endpoint implementation)`

> **Notes for anyone changing this file:** This module is schemas only — it defines no route and no connection-handling code; the live WebSocket handler itself is registered from pipeline.live.live_ingestion_pipeline in api.main.create_app, not from this package. Every model uses extra='allow' so the live pipeline can add fields to the JSONB game_state blob without a synchronized schema bump on this side.

---

### `api/league_feed.py`

SIM-519: the app's one reader of the league's public endpoints: the schedule of a date (the slate) and one game's live feed (the open card), over one lazy `aiohttp` session. The lifespan attaches it as `app.state.league_feed` unless `SLATE_SCHEDULE_ENABLED=0`; a test attaches a fake with the same two coroutines, or none (the slate then serves the stored listing).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `LeagueFeed.schedule_payload(start, end)` / `game_feed_payload(game_pk)` | The raw responses; they raise on any failure (the routes degrade). Timeout `SLATE_FEED_TIMEOUT_S` (5 s). | `api.routes.games` | `pipeline.mlb_schedule.schedule_params` |

---

### `api/sim_jobs.py`

SIM-519 Part E: one simulation run per game. A run is a `sim.sim_runs` row: queued, running, then done, failed or cancelled. One active run per key (the SHA-256 of the runner's cache key; a partial unique index holds it across processes); `SIM_JOB_MAX_CONCURRENT` (1) runs at a time; `BatchRunner.run_job` writes progress per chunk and stops on a cancel; a done row stores the summary, the prop set (`PropDistributionSet.to_json`), the inning grids and `replay_run_id`. `recover_orphans` marks a previous process's unfinished rows `failed('restart')` at boot.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `SimJobRegistry.submit / get / latest / cancel / prop_set_row / recover_orphans` | The registry `app.state.sim_jobs`. | `api.routes.games` run routes, `/boxscore`, `/props`; the `api.main` lifespan | `simulation.batch_runner.BatchRunner.run_job` |

---
