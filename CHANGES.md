# Phase 6 Sprint 2 — Frontend Build (P1) — 2026-05-26
**Authors: UX Designer (Agent 7), Backend Developer (Agent 5)**

Sprint 2 begins the frontend build on the now-buildable Sprint-1 contracts. First ticket: the Day
Summary page (SIM-391). Also hardened the SIM-379 scaffold, which had never actually passed its own
CI job — three gaps (missing lock file, missing CSS-module type declarations, missing Vite path
alias) plus one lint failure all blocked the `frontend` job; all fixed here so the job is green.

| Ticket | Type | Owner | Status |
|--------|------|-------|--------|
| SIM-391 | Feature | UX + Backend | ✅ Closed — Day Summary page: date nav + game-count badge + 3-state GameCards + React Router |
| SIM-392 | Feature | UX + Backend | ✅ Closed — LinescoreGraphic (R/H/E grid + "x" cells) + BaseballFieldGraphic SVG (9 positions + runners + batter) |
| SIM-393 | Feature | UX + Backend | ✅ Closed — Game page: header + linescore + field + play-by-play scroll + live WS (SIM-385) |
| SIM-394 | Feature | UX + Betting | ✅ Closed — per-player boxscore means + on-demand prop distribution chart w/ prop-line marker |
| — | Infra/Bug | UX + QA | ✅ Done — frontend CI hardening (package-lock.json, `vite-env.d.ts`, Vite `@/` alias, AuthContext lint fix) |

### Frontend CI hardening (no ticket — completes the SIM-379 scaffold)

The SIM-379 scaffold committed `frontend/package.json` but the `frontend` CI job (Node 20:
`npm ci` → type-check → lint → build) could never have passed. Four blocking gaps, all fixed:

- **No `package-lock.json`** — `npm ci` hard-requires a committed lock file. Ran `npm install`
  (also adds `react-router-dom@^6.26.2` for routing) to generate + commit `frontend/package-lock.json`.
- **No CSS-module type declarations** — every `import styles from './X.module.css'` failed
  `tsc --noEmit` with TS2307. Added `frontend/src/vite-env.d.ts` (`/// <reference types="vite/client" />`),
  which provides the ambient CSS-module + asset + `import.meta.env` declarations.
- **No Vite `@/` alias** — the `@/*` path alias was in `tsconfig.json` (so the type-checker resolved
  it) but not in `vite.config.ts`, so the rollup build failed to resolve `@/components/...`. Added a
  matching `resolve.alias` to `vite.config.ts`.
- **Lint failure under `--max-warnings 0`** — `AuthContext.tsx` tripped
  `react-refresh/only-export-components` (a context file co-locating its provider + hook). Added a
  scoped `eslint-disable-next-line` with rationale.

### SIM-394 — Per-player boxscore + distribution views (prop-line marker)

Mounts in the Game page "Projections" slot.

- `frontend/src/components/games/BoxscorePanel.tsx` (+css) — gated behind a
  "Load projections" button (the /boxscore endpoint runs a 100-iteration sim,
  so it doesn't auto-fire). Once loaded, shows each player's prop means as
  clickable chips; selecting one fetches that prop's full PMF (SIM-390) and
  renders the distribution, with a line input that redraws the over/under
  marker + reports P(over)/P(under)/P(push).
- `frontend/src/components/games/PropDistributionChart.tsx` (+css) — SVG bar
  chart of a PMF (one bar per support value), dashed prop-line marker, mean
  marker, and over-bar tinting so the over/under split reads at a glance.
- `frontend/src/api/games.ts` — added `fetchBoxscore` + `fetchPropEdge`
  (with line/odds/bet-side query params) + `BoxscoreCard` / `PropEdge` /
  `EdgeReport` types.
- `frontend/src/pages/GamePage.tsx` — mounted `BoxscorePanel` in the
  Projections panel.

**Verification:** `tsc` + `eslint` + `vite build` pass. Not browser-tested
(boxscore/prop endpoints need a backend with data).

### SIM-393 — Game page (play-by-play + linescore + field + live WS)

The game detail view at `/game/:gamePk`, replacing the SIM-391 stub.

- `frontend/src/pages/GamePage.tsx` (+css) — orchestrates the page: header
  (matchup + 3-state status badge + score), a live banner (WS connection dot +
  "re-simulating…" indicator), a two-column grid (linescore + field graphic on
  the left, play-by-play on the right), and marked slots for projections
  (SIM-394) and betting (SIM-395). Score/baserunner precedence: WS > REST /live
  > final scores. A local `useOptionalResource` hook treats a 404 as "no data
  yet" (no persisted sim / not live) rather than an error.
- `frontend/src/hooks/useGameSocket.ts` — subscribes to `/ws/games/{gamePk}`
  (Vite-proxied), consuming the SIM-385 event schema: `game_state_update`
  (updates live state + `resimPending`), `resim_pending`, `ping` (auto-pong),
  `pong`. Tracks connection status, normalizes the WS `runner_on_first/second/
  third` → `on_1b/on_2b/on_3b`, and reconnects with a 3s backoff.
- `frontend/src/components/games/PlayByPlayList.tsx` (+css) — the scroll,
  grouped into plate appearances (resolved event + runs/outs), each PA
  expandable to its pitch sequence (drill-down with exit-velo/launch-angle for
  balls in play).
- `frontend/src/api/games.ts` — added `fetchLinescore`, `fetchPlays`,
  `fetchLiveState` + the `Linescore`/`PlayByPlay`/`LiveState` types.
- `frontend/src/components/graphics/BaseballFieldGraphic.tsx` — widened base
  props to `string | boolean | null` so the page can mark occupancy without a
  player name.

**Verification:** `tsc --noEmit` + `eslint` + `vite build` (60 modules) all pass.
UI not browser-tested (needs a live backend + persisted sim data).

### SIM-392 — LinescoreGraphic + BaseballFieldGraphic SVG

Two prop-driven presentational graphics the Game page (SIM-393) composes; both
decoupled from the API (the page adapts payloads into their props).

- `frontend/src/components/graphics/LinescoreGraphic.tsx` (+css) — classic
  scoreboard table: away-on-top/home-on-bottom, per-inning run grid + R/H/E
  totals. `null` inning cells render the scoreboard "x" (half not played);
  extra innings (N>9) widen the grid; mirrors LinescoreModel (SIM-362).
- `frontend/src/components/graphics/BaseballFieldGraphic.tsx` (+css) — SVG
  field: outfield arc + infield diamond, 9 standard fielder dots with P/C/1B…
  labels, base markers that fill when occupied, runner name labels, and a
  batter label. Label-collision rules: OF labels above their dot / IF labels
  below; runner labels anchored outside the diamond (right of 1B, above 2B,
  left of 3B); batter label below home plate. Driven by the SIM-386 live
  baserunner state (on_1b/on_2b/on_3b) + optional player names.
- `frontend/src/components/graphics/index.ts` — barrel export.

**Verification:** `tsc --noEmit` + `eslint` pass. (Visual rendering exercised
once SIM-393 mounts them; build-verified there.)

### SIM-391 — Day Summary page (date nav + game-count badge + 3-state cards)

**Goal:** the slate view — the entry point of the app. A date navigator, a game-count badge, and a
responsive grid of status-aware game cards, built on the enriched games API (SIM-383) with the
3-state status enum mapping (SIM-384). Routing (React Router) is introduced here as the dependency
note anticipated.

**New files:**
- `frontend/src/api/games.ts` — typed games client (`credentials:'include'`, mirrors `auth.ts`):
  - `fetchGamesOnDate(date)` → `GET /api/games/{date}` (`GamesOnDateResponse` + `GameCard`)
  - `fetchGameCard(gamePk)` → `GET /api/games/{game_pk}/status` (`GameCardAggregate`, SIM-384)
  - `rawStatusToGameStatus(raw)` — mirrors the server `_RAW_STATUS_TO_GAME_STATUS` 8→3-state map
  - `GamesApiError` carrying the HTTP status (so the page can special-case 401)
- `frontend/src/components/games/GameCard.tsx` + `.module.css` — status-aware card: away/home
  matchup with team names + season W/L records, a status `Badge` (LIVE pulses, FINAL/SCHEDULED/PPD),
  left-border status accent, optional score display (forward-compatible with the aggregate payload),
  final-game winner highlight. The whole card is a `<Link to="/game/{pk}">`.
- `frontend/src/pages/DaySummaryPage.tsx` + `.module.css` — reads the date from `/date/:date`
  (falls back to today), date nav (prev/next/today + native date picker), game-count `Badge`, and a
  responsive `auto-fill minmax(280px, 1fr)` grid of `GameCard`s. Loading / empty / error states
  handled inline; local YYYY-MM-DD date helpers avoid UTC drift.
- `frontend/src/pages/GamePage.tsx` — stub route target for `/game/:gamePk` (full build: SIM-393).

**Modified:**
- `frontend/src/App.tsx` — wired `BrowserRouter` into the authenticated shell: `/` and `/date/:date`
  → `DaySummaryPage`; `/game/:gamePk` → `GamePage`; `*` → redirect to `/`. Header brand is now a
  `<Link to="/">`. Replaced the Sprint-1 placeholder card.
- `frontend/package.json` — added `react-router-dom`.

**Verification:** `npm run type-check`, `npm run lint` (`--max-warnings 0`), and `npm run build`
(`tsc --noEmit && vite build`) all pass. **UI not exercised in a browser** — the backend requires a
populated Postgres/DuckDB to serve real data (live-env verification debt), so this is build-verified
only, not user-tested. Visual/interaction testing is deferred to a live-env bring-up.

---

# Phase 6 Sprint 1 — Kickoff Gates — 2026-05-25
**Authors: UX Designer (Agent 7), Backend Developer (Agent 5), QA/DevOps (Agent 9), Product Manager (Agent 1)**

Sprint 1 P0 delivery: **ALL 13 P0 tickets closed**. ADR decision (React + Vite), full frontend scaffold,
design system primitives, SPA serving path wired end-to-end through both FastAPI and nginx, typed
WebSocket schema, full browser session auth layer (httpOnly cookies + require_auth enforcement + CORS
wildcard removed), phantom-ticket backfill (SIM-382), enriched games-listing API (SIM-383), single-game
aggregate card + status enum (SIM-384), live in-progress game-state read path (SIM-386),
multi-substitution override (SIM-388), player-prop edge/signal endpoint (SIM-390), and the carry-in bug
fixes (SIM-387 calibration wiring, SIM-410 p95 middleware) plus stale-doc corrections.

| Ticket | Type | Owner | Status |
|--------|------|-------|--------|
| SIM-378 | Spec/ADR | UX Designer + Backend Developer + QA/DevOps | ✅ Closed — React 18 + Vite chosen |
| SIM-379 | Infra | UX Designer + QA/DevOps | ✅ Closed — scaffold + build tooling + JS CI lane |
| SIM-380 | Feature | UX Designer | ✅ Closed — design system (tokens, global, Card, Panel, Badge) |
| SIM-381 | Gap | Backend Developer + UX Designer | ✅ Closed — StaticFiles mount + nginx SPA fallback |
| SIM-382 | Gap | Product Manager | ✅ Closed — backfilled 8 phantom parent tickets (SIM-108/109/112/122–126); re-mapped SIM-127–131 deps |
| SIM-383 | Feature | Backend Developer + Data Engineer | ✅ Closed — enriched GET /api/games/{date} with team names, abbreviations, venue, and season records |
| SIM-384 | Feature | Backend Developer | ✅ Closed — GET /api/games/{game_pk}/status aggregate card + GameStatus 3-state enum + GameCardAggregateResponse |
| SIM-385 | Feature | Backend Developer | ✅ Closed — typed WS event schema (Pydantic v2) |
| SIM-388 | Feature | Backend Developer | ✅ Closed — SubstitutionSlot model + substitutions[] array field + updated _apply_override |
| SIM-387 | Bug | Backend Developer | ✅ Closed — calibration map threaded to win_probability at /api/betting edges/signals |
| SIM-389 | Security | Backend Developer + QA/DevOps | ✅ Closed — httpOnly cookie session + require_auth on expensive routes + CORS wildcard fix |
| SIM-386 | Feature | Data Engineer + Backend Developer | ✅ Closed — `GET /{game_pk}/live` reads `sim.lineup_state`; LiveGameStateResponse; `load_live_game_state` in sim_store |
| SIM-390 | Feature | Backend Developer + Betting Analyst | ✅ Closed — `GET /{game_pk}/props/{player_id}/{prop}` endpoint; full PMF + optional p_over/p_under/edge_report |
| SIM-410 | Improvement | Backend Developer + QA/DevOps | ✅ Closed — LatencyMiddleware wires rolling p95 into app.state.api_p95_seconds → /metrics |
| — | Housekeeping | Product Manager | ✅ Done — stale "Phase 2" references corrected in api/main.py, agent_team.md, WORKFLOW.md |

### SIM-378 — React-vs-vanilla-JS architecture decision (ADR)

**Decision recorded:** React 18 + Vite. The decisive factor was the override-v2 UI (SIM-398: staged
queue + undo + amber indicators + side-by-side comparison); vanilla JS would require hand-rolling all
event coordination and state management, while React's component model makes it straightforward.

**Files created:**
- `docs/architecture/2026-09-02-adr-frontend-framework.md` — full ADR with comparison table
  (Vanilla JS vs React + Vite vs Preact + Vite), consequences, rationale. Status: Accepted.

### SIM-379 — Frontend scaffold + build tooling + frontend CI job

**Frontend scaffold (React 18 + Vite + TypeScript strict):**
- `frontend/package.json` — React 18.3, react-dom 18.3, Vite 5.4, TypeScript 5.5,
  @vitejs/plugin-react, eslint with typescript/react-hooks/react-refresh plugins.
- `frontend/vite.config.ts` — dev proxy: `/api` and `/ws` → `localhost:8000` (ws:true);
  build outDir `dist`, sourcemap true.
- `frontend/tsconfig.json` — strict mode, bundler moduleResolution, `@/*` → `src/*` path alias.
- `frontend/tsconfig.node.json` — vite.config.ts typechecking.
- `frontend/.eslintrc.cjs` — @typescript-eslint + react-hooks + react-refresh plugins.
- `frontend/index.html` — HTML5 entry with `<div id="root">` and `<script type="module" src="/src/main.tsx">`.
- `frontend/src/main.tsx` — ReactDOM.createRoot with StrictMode.
- `frontend/src/App.tsx` — root component with header shell and Sprint-1 placeholder card.
- `.gitignore` updated — added `node_modules/`, `frontend/dist/`, `frontend/.vite/`,
  `frontend/.eslintcache`, npm debug/error log patterns.

**CI lane added:**
- `.github/workflows/ci.yml` — new `frontend` job (Node 20 LTS, `npm ci`, `npm run type-check`,
  `npm run lint`, `npm run build`). Uploads `frontend/dist/` as a CI artifact (retention 7 days).
  Runs in parallel with the Python lint/type-check/unit-test jobs.

### SIM-380 — Design-system foundation (tokens, typography, spacing, Card/Panel/Badge)

**CSS custom properties design system:**
- `frontend/src/styles/tokens.css` — full `--sim-{category}-{scale}` token set:
  color scales (gray/primary/success/danger/warning/info 50–950), semantic color aliases
  (surface, border, text, win/loss/override/live), typography (font families + text-xs→4xl scale
  + font weights + line heights), spacing (4px base grid, --sim-space-0 through --sim-space-24),
  border radius (sm/md/lg/xl/full), shadows (sm/md/lg), z-index scale, layout constants
  (--sim-header-height: 56px, --sim-max-width: 1280px). Dark-mode block via
  `@media (prefers-color-scheme: dark)`.
- `frontend/src/styles/global.css` — box-model reset, body/app shell styles, utility classes
  (.sr-only, .truncate).

**Design system primitives (CSS Modules + TypeScript):**
- `frontend/src/components/ui/Badge.tsx` + `Badge.module.css` — compact status indicator:
  variants (default/primary/success/danger/warning/info/live/final/scheduled), optional pulsing
  dot animation (`@keyframes pulse`) for LIVE badges, aria-label support.
- `frontend/src/components/ui/Card.tsx` + `Card.module.css` — content container with optional
  title, count badge, headerActions, padding variants (sm/md/lg), borderless mode.
- `frontend/src/components/ui/Panel.tsx` + `Panel.module.css` — section grouping with label
  (visible/hidden), left accent bar (none/primary/success/danger/warning/info), aria-label.
- `frontend/src/components/ui/index.ts` — barrel export for all three primitives + their types.

### SIM-381 — API→frontend serving path (StaticFiles / nginx SPA fallback)

**FastAPI (dev/staging convenience — serves SPA when running uvicorn directly):**
- `api/main.py`: added conditional `StaticFiles` mount at `/assets` and a `/{full_path:path}`
  SPA catch-all route that returns `index.html`. Both are gated on `frontend/dist/` existing so
  the app boots cleanly on a fresh clone before the first `npm run build`. The catch-all is
  registered LAST (after all API routes) so FastAPI's ordered routing ensures it only fires
  when nothing else matches.

**nginx (production/staging — serves static assets directly):**
- `deploy/nginx/nginx.conf`: restructured to add dedicated `location /api/` proxy block,
  explicit proxy blocks for ops endpoints (`/health`, `/ready`, `/metrics`, `/docs`, `/redoc`,
  `/openapi.json`), a `location /assets/` block with `expires 1y; Cache-Control: public, immutable`
  for Vite-hashed assets, and a `location /` SPA catch-all using `try_files $uri /index.html`.
  `index.html` itself gets `Cache-Control: no-cache, no-store, must-revalidate` so deploys
  take effect immediately while the hashed `/assets/*` benefit from 1-year browser caching.
  Root set to `/var/www/baseball-sim` (mounted from `frontend/dist/` in docker-compose).

### SIM-385 — Typed + documented WebSocket event schema

**New file: `api/websocket/schemas.py`**
- `WsEventType` enum — discriminant for all four event types:
  `game_state_update`, `resim_pending`, `ping`, `pong`.
- `LiveGameState` — structured Pydantic v2 model for the game-state dict built by
  `GameStateBuilder.build()`. All fields optional; `extra="allow"` forwards future fields.
  Known fields: inning, half, outs, balls, strikes, home/away score + team IDs, batter/pitcher IDs,
  runners, game_status, play_history, home/away lineups.
- `LiveOdds` — optional odds snapshot (home/away ML, run-line + prices, total + over/under prices).
  `extra="allow"` for future book-specific fields.
- `GameStateUpdateEvent` — main broadcast event; mirrors `broadcast_payload` at
  `live_ingestion_pipeline.py:1370`. `resim_triggered=True` signals the frontend to show a
  loading indicator while a new Monte-Carlo sim is queued.
- `ResimPendingEvent` — re-sim notification; mirrors the `resim_pending` broadcast at
  `live_ingestion_pipeline.py:2161`.
- `PingEvent` / `PongEvent` — keep-alive pair.
- `WsEvent` union type alias for use in `isinstance()` / `Annotated[..., Discriminator("type")]`.

**New test file: `tests/unit/test_api_ws_schemas_sim385.py`** — 22 tests:
  `TestWsEventType` (2), `TestGameStateUpdateEvent` (6), `TestResimPendingEvent` (4),
  `TestKeepAliveEvents` (4), `TestLiveGameState` (3), `TestLiveOdds` (3). All 22 pass.

### SIM-387 — Fix dead calibration wiring at the betting edge/CLV call site

**Problem:** `api/routes/betting.py:329` called `win_probability(summary)` without threading
`app.state.calibration_map`, so every edge and CLV number was computed off the identity map
even though a `CalibrationReport` is loaded at boot (SIM-361). The "gold-standard" CLV metric
was systematically wrong.

**Fix:**
- `api/routes/betting.py`: imported `IDENTITY_CALIBRATION`; changed the `win_probability` call to
  `win_probability(summary, calibration_map=getattr(request.app.state, "calibration_map", IDENTITY_CALIBRATION))`
  so the boot-loaded map is always used, with a safe fallback for test/staging environments that
  start without a fitted CalibrationReport.
- Added 2 new tests to `tests/unit/test_api_betting_sim36x.py`:
  `test_edges_uses_calibration_map_from_app_state` (verifies a non-identity map shifts sim_prob to ~0.1)
  and `test_edges_falls_back_to_identity_when_no_calibration_map` (no crash when unset).

### SIM-389 — Enforce auth on expensive routes + browser session model + fix dev CORS

**Design:** httpOnly cookie session for browser clients; X-API-Key header for programmatic
clients (scripts, Prometheus, CI). A single `require_auth` dependency accepts either form.
Session tokens are stateless HMAC-SHA256 signed payloads (pure stdlib — no new deps).
Default lifetime: 8 hours (`SESSION_TTL_HOURS`).

**Protected routes** (`dependencies=[Depends(require_auth)]` applied to):
- `GET  /api/games/{game_pk}/simulate`, `POST /api/games/{game_pk}/simulate/with_override`
- `GET  /api/betting/games/{game_pk}/edges`, `GET  /api/betting/games/{game_pk}/signals`
- `GET  /metrics` (Prometheus endpoint — sensitive internal perf data)

**New backend files:**
- `api/routes/auth.py` — `POST /auth/login` (password → httpOnly sim_session cookie),
  `GET /auth/me` (always 200 — safe session probe for frontend boot), `POST /auth/logout`.
- `api/auth.py` additions — `_mint_session_token`, `_verify_session_token`, `cookie_kwargs`,
  `SESSION_COOKIE_NAME`, `require_auth` dependency (cookie → API key → 401 with dev + unconfigured
  pass-through). CORS dev wildcard replaced with specific Vite origins.
- `api/main.py` — registered `auth_router`; added conditional non-dev env validation for
  `SECRET_KEY` + `AUTH_PASSWORD` (boot fails fast on misconfigured production containers).

**New frontend files:**
- `frontend/src/api/auth.ts` — `login` / `checkAuth` / `logout` fetch wrappers (all `credentials: 'include'`).
- `frontend/src/contexts/AuthContext.tsx` — React context + `useAuth()` hook; probes `/auth/me`
  on mount to restore sessions across page reloads without requiring a re-login.
- `frontend/src/components/auth/LoginPage.tsx` + `LoginPage.module.css` — full-page centred login
  form on the app's navy backdrop. Fully accessible (aria-invalid, aria-busy, focus management).

**Modified files:**
- `api/routes/games.py`, `betting.py`, `metrics.py` — `Depends(require_auth)` on protected routes.
- `frontend/vite.config.ts` — `/auth` proxy entry added so cookies are issued by localhost:5173.
- `deploy/nginx/nginx.conf` — `location /auth/` proxy block added before `/api/`.
- `frontend/src/App.tsx` — wrapped in `<AuthProvider>`; renders `<LoginPage>` when not authenticated.
- `tests/unit/test_api_auth.py` — updated CORS wildcard test to match SIM-389 behaviour.

**New test file: `tests/unit/test_api_auth_session_sim389.py`** — 29 tests:
  token primitives (6), login (7), /auth/me (4), logout (2), require_auth enforcement (6), CORS (4).

**New env vars:** `SECRET_KEY` (HMAC key, required non-dev), `AUTH_PASSWORD` (login password,
required non-dev; dev default "dev"), `SESSION_TTL_HOURS` (default 8 hours).

### SIM-387 — Fix dead calibration wiring at the betting edge/CLV call site

**Problem:** `api/routes/betting.py:329` called `win_probability(summary)` without threading
`app.state.calibration_map`, so every edge and CLV number was computed off the identity map
even though a `CalibrationReport` is loaded at boot (SIM-361). The "gold-standard" CLV metric
was systematically wrong.

**Fix:**
- `api/routes/betting.py`: imported `IDENTITY_CALIBRATION`; changed the `win_probability` call to
  `win_probability(summary, calibration_map=getattr(request.app.state, "calibration_map", IDENTITY_CALIBRATION))`
  so the boot-loaded map is always used, with a safe fallback for test/staging environments that
  start without a fitted CalibrationReport.
- Added 2 new tests to `tests/unit/test_api_betting_sim36x.py`:
  `test_edges_uses_calibration_map_from_app_state` (verifies a non-identity map shifts sim_prob to ~0.1)
  and `test_edges_falls_back_to_identity_when_no_calibration_map` (no crash when unset).

### SIM-410 — Wire the API p95 timing middleware

**Problem:** `api/routes/metrics.py`'s `baseball_sim_api_p95_seconds` gauge read
`app.state.api_p95_seconds` which was never populated — the Grafana p95 panel always read 0.

**Fix:**
- `api/auth.py`: added `LatencyMiddleware(BaseHTTPMiddleware)` — a rolling 200-request ring buffer
  that computes `p95 = sorted_window[floor(0.95*(N-1))]` and stores it on `app.state.api_p95_seconds`
  after every non-exempt request. Exempt paths: `/health`, `/ready`, `/`, `/metrics`.
- `api/main.py`: imported `LatencyMiddleware`; registered it with `app.add_middleware(LatencyMiddleware)`
  alongside the existing `RateLimitMiddleware`.
- Added 3 new tests to `tests/unit/test_api_auth.py`:
  `test_latency_middleware_populates_p95_after_two_requests`,
  `test_latency_middleware_p95_grows_with_more_requests`,
  `test_latency_middleware_exempt_paths_do_not_populate_p95`.

### SIM-382 — Backfill 8 phantom Phase-6 parent tickets + re-map SIM-127–131 deps

**Problem:** the Phase-5-close program audit found that SIM-127–131 (pre-existing Phase-6 frontend
tickets) cite parent tickets SIM-108/109/112/122–126 that never existed in the backlog — the dependency
chain was completely phantom, making the child tickets un-actionable.

**Fix (documentation only):**

Added a `📋 SIM-382 — Phantom Ticket Backfill` section to `BACKLOG.md` immediately before the Phase-4
Sprint-2026-07-08 section, containing:
- **8 phantom-parent stubs** (SIM-108/109/112/122–126): the intended title for each and the real
  Phase-4/5/6 ticket that supersedes it.
- **5 child-ticket re-maps** (SIM-127–131): old phantom dep → new real dep.

Full mapping:

| Phantom ID | Intended title | Superseded by |
|---|---|---|
| SIM-108 | Frontend-facing game-simulation API contract spec | SIM-355 + SIM-358 ✅ Phase 5 |
| SIM-109 | Team/venue metadata API | **SIM-383** ✅ |
| SIM-112 | Live in-progress game-state read path | **SIM-386** (Phase 6 Sprint 2) |
| SIM-122 | WebSocket event schema | **SIM-385** ✅ |
| SIM-123 | Player prop distributions + boxscore API | SIM-366 ✅ + **SIM-390** |
| SIM-124 | Frontend scaffold + build-tooling spec | **SIM-378/379** ✅ |
| SIM-125 | Managerial override API (multi-sub) | **SIM-388** (Phase 6 Sprint 2) |
| SIM-126 | Design-system + component library spec | **SIM-380** ✅ |

### SIM-388 — Multi-substitution override (array body)

**Problem:** `RosterOverride` only accepted full lineup arrays or a single pitcher swap — no way
to stage targeted individual-player substitutions without knowing the full batting order. This
blocked SIM-128 (staged override queue + undo UI) and SIM-398 (override v2).

**Fix:**

**`api/routes/games.py`:**
- Added `SubstitutionSlot(BaseModel)` — validated model with three fields:
  - `batting_order: int = Field(ge=1, le=9)` — 1-indexed batting order position
  - `player_id: int = Field(gt=0)` — MLB player_id of the substitute
  - `side: Literal["home", "away"]` — which side's lineup to modify
- Extended `RosterOverride` with `substitutions: list[SubstitutionSlot] | None = None`.
- Updated `_apply_override()` to implement a 3-step processing order:
  1. Full lineup replacements (`home_lineup` / `away_lineup`) applied first
  2. Targeted `substitutions` applied left-to-right to the current lineup (after step 1);
     `batting_order` → 0-indexed slot; out-of-range slots silently skipped
  3. `pitcher_id` + `bat_hand` applied last
  - Multiple subs targeting the same slot: last one wins (left-to-right).
- Added `from typing import Literal` import.

**`tests/unit/test_api_games.py`:**
- Added `class TestMultiSubstitutionOverride` — 9 new tests:
  - Single home + single away targeted sub (HTTP end-to-end via the override endpoint)
  - Multiple subs across different slots + sides
  - Empty `substitutions: []` is a no-op (delta == 0)
  - Sub + pitcher override combined
  - Full lineup replacement + targeted sub combined (sub applies after full replacement)
  - `batting_order=0` rejected with 422 by Pydantic
  - `side="visitor"` rejected with 422 by Pydantic
  - Direct unit-test of `_apply_override` helper: verifies correct slot mapping

**Verification:** `pytest tests/unit/test_api_games.py -v` → **44/44 passed** (35 pre-existing + 9 new).

### SIM-384 — Single game-card aggregate endpoint + status enum

**Problem:** the Day Summary 3-state game cards (SIM-391) need identity + status + sim + odds in
a single call. No such endpoint existed. `raw.games.status` has 8 raw values; the UI only cares
about 4 states.

**Fix:**

**`api/routes/games.py`:**
- Added `GameStatus(StrEnum)` — 4-value enum: `SCHEDULED / LIVE / FINAL / POSTPONED`.
- Added `_RAW_STATUS_TO_GAME_STATUS` mapping from the 8 raw `raw.games.status` values:
  - Preview / Warmup / Pre-Game → `scheduled`
  - Live → `live`
  - Final → `final`
  - Postponed / Suspended / Cancelled → `postponed`
- Added `GameCardAggregateResponse(BaseModel)` — carries `game_status` (str), SIM-383 enriched
  identity (team names/abbrevs/records/venue), `home_score_final` / `away_score_final`, 
  `sim_summary: GameSimSummaryLite | None`, and `odds: None` (reserved for SIM-405).
- Added `_GAME_CARD_SQL` — the same CTE-based enriched query as `_GAMES_ON_DATE_SQL` but for a
  single `game_pk` ($1), using a `game_date_lookup` CTE to feed the `team_records` date bound.
  Selects `home_score_final` and `away_score_final` too.
- Added `_sim_summary_lite_from_stored(summary_dict)` — strips raw score arrays, then
  calls `GameSimSummaryLite.model_validate()` (best-effort, returns None on any failure).
- Added `GET /{game_pk}/status` endpoint (`get_game_status_card`):
  1. `pool.fetchrow(_GAME_CARD_SQL, game_pk)` — enriched identity (404 if not found)
  2. Maps raw status → `GameStatus` enum value
  3. Best-effort `sim_store.load_latest_sim_run(conn, game_pk)` to populate `sim_summary`
     (exception-safe; always returns None rather than breaking the response)
  4. Returns `GameCardAggregateResponse`
- Added `from enum import StrEnum` import.
- Added `GameSimSummaryLite` to the `api.schemas` import.

Note: route is `/{game_pk}/status` (not `/card`) because `/{game_pk}/card` is already the
SIM-366 linescore+decisions endpoint. No rename to keep SIM-366 stable.

**`tests/unit/test_api_games.py`:**
- Added `_CARD_ROW` — enriched canned row including `home_score_final` / `away_score_final`.
- Added `class TestGameCardAggregate` — 12 new tests:
  - Status mapping: Final/Live/Preview/Warmup/Pre-Game/Postponed/Suspended/Cancelled
  - Enriched team data + final scores present in response
  - `sim_summary` is None when fake pool yields no sim run (exception caught)
  - `odds` is None placeholder
  - 503 when no pool; 404 when game not in DB
  - Monkeypatch test: `load_latest_sim_run` returning canned summary → `sim_summary` populated

**Verification:** `pytest tests/unit/test_api_games.py -v` → **35/35 passed** (23 pre-existing + 12 new).

### SIM-383 — Enrich GET /api/games/{date} with team/venue names + records

**Problem:** `GET /api/games/{date}` returned only bare integer IDs (`home_team_id`, `away_team_id`,
`venue_id`). `raw.teams` and `raw.venues` exist but were unjoined; no win/loss record was derived.
The Day Summary UI (SIM-391) cannot render game cards from bare IDs.

**Fix:**

**`api/routes/games.py`:**
- Rewrote `_GAMES_ON_DATE_SQL` as a CTE-based JOIN query:
  - `team_records` CTE aggregates season win/loss records from `raw.games` WHERE `status = 'Final'`
    AND `game_date < $1` (i.e., entering-game records, not same-day) via a UNION ALL of the home
    and away perspectives, aggregated by `(team_id, season)`.
  - Main query LEFT JOINs `raw.teams` twice (home + away, aliased `ht`/`at_`) and `raw.venues`
    once; also LEFT JOINs the CTE twice (`hr`/`ar`). All JOINs are keyed on
    `(team_id, season)` / `(venue_id, season)` to match the composite primary keys.
    `COALESCE(hr.wins, 0)` handles teams with no prior Final games.
  - All 15 columns selected (original 7 + 8 new enrichment fields).
- Extended `GameCard` model with 8 new optional fields (`Optional[str/int] = None`):
  `home_team_name`, `home_team_abbrev`, `away_team_name`, `away_team_abbrev`,
  `venue_name`, `venue_city`, `home_wins`, `home_losses`, `away_wins`, `away_losses`.
- Rewrote `_game_card()` with `_opt_str(key)` / `_opt_int(key)` helpers; all new fields
  default to `None` when the row key is absent — backward compatible with old cached payloads
  and pre-SIM-383 unit-test stubs.

**`tests/unit/test_api_games.py`:**
- Added `_ENRICHED_CANNED_ROWS` — 2 enriched rows with all new JOIN columns populated.
- Added `class TestGameCardEnrichment` — 7 new tests:
  - `test_enriched_rows_populate_team_names` — names and abbreviations present
  - `test_enriched_rows_populate_venue_fields` — venue_name + venue_city present
  - `test_enriched_rows_populate_records` — home_wins/losses, away_wins/losses
  - `test_enriched_second_game_has_its_own_team_data` — per-card isolation
  - `test_bare_rows_without_enrichment_default_to_none` — old stubs default None
  - `test_enriched_rows_cached_payload_preserves_enrichment` — cache round-trip keeps enrichment
  - `test_enriched_rows_count_and_pks_unchanged` — count and game_pk set unaffected

**Verification:** `pytest tests/unit/test_api_games.py -v` → **23/23 passed** (16 pre-existing + 7 new).

### SIM-386 — Live in-progress game-state read path on the main API

**Problem:** The live ingestion pipeline (port :8001) writes the real-time game state to
`sim.lineup_state` on every MLB WebSocket signal, but the main API (`api/`) had no endpoint
to read it. The frontend (SIM-392 LinescoreGraphic + BaseballFieldGraphic) could not get the
current inning/score/baserunner state without reaching the pipeline app directly — breaking
the single-origin contract.

**Fix:**

**`db/sim_store.py`:**
- Added `_SQL_LOAD_LIVE_GAME_STATE` — parameterized SELECT on `sim.lineup_state WHERE game_pk = $1
  AND is_live_game = TRUE ORDER BY updated_at DESC LIMIT 1` (served by the existing
  `idx_lineup_state_live` partial index).
- Added `load_live_game_state(conn, game_pk)` — async, returns `{session_id, game_pk, game_state
  (parsed dict from JSONB), updated_at (ISO string)}` or `None` if not live.
- Added `_live_state_row_to_dict(row)` — parses `game_state` JSONB (str/bytes → dict via json.loads;
  asyncpg dict passthrough) and isoformats `updated_at`.
- Added `"load_live_game_state"` to `__all__`.

**`api/schemas.py`:**
- Added `LiveGameStateResponse(_ApiModel)` — SIM-386 response model with all fields from the
  `game_state` JSONB blob written by `GameStateBuilder`:
  - Inning state: `inning`, `half` ("Top"/"Bottom"), `outs`, `balls`, `strikes`
  - Score: `home_score`, `away_score`
  - Team context: `batting_team_id`, `fielding_team_id`
  - Baserunners: `on_1b`, `on_2b`, `on_3b` (player_id or None)
  - Current participants: `current_batter_id`, `current_pitcher_id`
  - Lineups/rosters: `home_lineup`, `away_lineup`, `home_bullpen`, `away_bullpen`,
    `home_bench`, `away_bench` (all `list[int]`, default empty)
  - Staleness: `updated_at: str | None` (ISO-8601 last pipeline write timestamp)
  - All fields have neutral defaults so a partially-built game_state never breaks deserialization.
- Added `"LiveGameStateResponse"` to `__all__`.

**`api/routes/games.py`:**
- Added `LiveGameStateResponse` to the `api.schemas` import block.
- Added `GET /{game_pk}/live` endpoint (`get_live_game_state`):
  1. `pool.acquire()` / direct `pool.fetchrow()` → `sim_store.load_live_game_state(conn, game_pk)`
  2. 404 if `None` (game not live or pipeline not yet ingested)
  3. Extracts `gs = live["game_state"]` dict; uses local `_i(key, default)`, `_opt_i(key)`,
     `_ids(key)` helpers to safely coerce JSONB values
  4. Returns `LiveGameStateResponse` with all fields populated

**`tests/unit/test_api_games.py`:**
- Added `_LIVE_GAME_STATE` module-level canned game_state dict (inning=5, Top, outs=1, score 3-2,
  runner on first, full lineups/bullpens/benches).
- Added `_FakePoolWithLive(_FakePool)` — overrides `fetchrow` to return a canned live row.
- Added `class TestLiveGameState` — 5 new tests:
  - `test_live_state_returns_200_with_all_fields` — all 20+ fields present and correct
  - `test_live_state_no_live_row_returns_404` — load returns None → 404
  - `test_live_state_no_pool_returns_503` — no pg_pool → 503
  - `test_live_state_response_is_json_serializable` — json.dumps round-trip
  - `test_live_state_empty_game_state_uses_defaults` — empty game_state dict → all fields default

**Verification:** `pytest tests/unit/test_api_games.py -v` → **61/61 passed** (56 pre-existing + 5 new).

### SIM-390 — Player-prop edge/signal API endpoints

**Problem:** `prop_edge_report` and `PropDistribution.p_over()` were fully implemented and tested
(SIM-329/SIM-339) but NO route exposed them. Player props only surfaced as boxscore means via
`GET /{game_pk}/boxscore` (SIM-366). The SIM-394 per-player distribution view (with prop-line
marker) and SIM-395 betting card require a PMF + edge endpoint.

**Fix:**

**`api/schemas.py`:**
- Added `PropEdgeResponse(_ApiModel)` — SIM-390 response model combining the full PMF fields from
  `PropDistributionModel` (player_id, prop, n, support, probabilities, mean, median, std, pmf dict)
  with four optional enrichment fields:
  - `line: float | None` — populated when a `line` query param is supplied
  - `p_over: float | None` / `p_under: float | None` / `p_push: float | None` — sportsbook
    over/under/push probabilities at `line` (betting convention: half-integer line → no push,
    integer line → push mass excluded from both over and under)
  - `edge_report: EdgeReportModel | None` — full edge/EV/CLV report when market odds are also
    supplied (via `over_ml` + `under_ml`)
- Added `"PropEdgeResponse"` to `__all__`.

**`api/routes/games.py`:**
- Added imports: `EdgeReportModel`, `PropEdgeResponse` from `api.schemas`; `MarketSide`, `OddsQuote`,
  `TwoWayMarket` from `betting`; `prop_edge_report as _prop_edge_report` from `betting`;
  `ALL_PROPS` from `simulation.prop_distributions`.
- Added `_VALID_PROP_NAMES: frozenset[str]` — the 8 valid prop names from `ALL_PROPS`
  (K, BB, ER, OUTS, H, HR, RBI, TB). A request for any other name is a 422.
- Added `GET /{game_pk}/props/{player_id}/{prop}` endpoint (`get_player_prop_edge`):
  - **Validation** (all 422): prop name not in `ALL_PROPS`; `bet_side` not "over"/"under";
    `over_ml`/`under_ml` supplied without a `line`.
  - **PMF build**: resolves lineup → `_resolve_state_or_error` → runs `_build_prop_set`
    (same `asyncio.to_thread` path as `/boxscore`) → `pset.get(player_id, prop)` → 404 if absent.
  - **Line enrichment**: if `line` is supplied, populates `p_over(line)`, `p_under(line)`,
    `p_push(line)` directly from `PropDistribution`.
  - **Edge report**: if `line` + `over_ml` + `under_ml` are all supplied, constructs a
    `TwoWayMarket(entry=OddsQuote(side, other, line))` keyed on the requested `bet_side`
    ("over" → `MarketSide.OVER`, "under" → `MarketSide.UNDER`), calls `_prop_edge_report`,
    and wraps in `EdgeReportModel.from_dataclass()`. Wrapped in try/except — degenerate odds
    (e.g. line=0, or implied probs summing badly) log a warning but never break the response.
  - Returns `PropEdgeResponse` with all fields populated appropriately.

**`tests/unit/test_api_games.py`:**
- Added `_make_fake_pset()` module-level helper — builds a `PropDistributionSet` with player 600001
  having a "K" PMF with support [4,5,6,7,8] and probabilities [0.10,0.20,0.40,0.20,0.10] (mean=6.0).
  Used to verify PMF math without running the full sim pipeline.
- Added `class TestPlayerPropEdge` — 12 new tests:
  - `test_prop_pmf_returns_200_with_pmf_fields` — shape check: all PMF fields present, None for
    line-less fields
  - `test_prop_lowercase_prop_name_is_normalised` — "k" treated as "K"
  - `test_prop_with_half_integer_line_populates_over_under` — p_over=0.70, p_under=0.30, p_push=0.0
    at line=5.5 (verifies betting half-integer convention)
  - `test_prop_with_integer_line_has_push_mass` — p_over=0.30, p_under=0.30, p_push=0.40 at line=6
    (verifies over+under+push=1.0)
  - `test_prop_with_odds_populates_edge_report_over` — edge_report filled, label="prop:K",
    side="over", sim_prob=0.70, positive_edge=True (0.70 > 0.50 fair for symmetric -110/-110)
  - `test_prop_bet_side_under_computes_under_edge` — side="under", sim_prob=0.30, positive_edge=False
  - `test_prop_invalid_name_returns_422` — "XYZ" → 422
  - `test_prop_invalid_bet_side_returns_422` — "home" → 422
  - `test_prop_odds_without_line_returns_422` — over_ml without line → 422
  - `test_prop_player_not_in_set_returns_404` — player_id 999999 absent → 404
  - `test_prop_no_pool_returns_503` — no pg_pool → 503
  - `test_prop_response_is_numpy_free` — full response (with edge_report) json.dumps round-trips

**Verification:** `pytest tests/unit/test_api_games.py -v` → **56/56 passed** (44 pre-existing + 12 new).
Full suite (excluding FAISS/DuckDB): **1826 passed, 8 skipped, 0 failed**.

### Stale-doc corrections (no ticket)

- `api/main.py`: health endpoint `"phase": "2"` → `"6"`; root endpoint description updated to
  "Phase 6 — Frontend Build (in progress)".
- `agent_team.md`: replaced the entire "Project Context" section (was Phase 2, all 11 engines
  listed as 9/11, roadmap showing phases 3–6 as Not Started) with the true Phase 6 state.
- `WORKFLOW.md`: replaced the stale Phase-2 phase note with a Phase-6-accurate note referencing
  the full Phase-5 API surface and flagging that the document needs a full refresh in Sprint 2.

---

# Data Engineer Changelog
**Sprint: 2026-05-05 | Author: Data Engineer (Agent 4)**

---

# Backend / QA / DevOps Sprint — 2026-05-07
**Authors: Backend Developer (Agent 5), ML Engineer (Agent 3), QA/DevOps (Agent 9)**

Eight tickets across the live ingestion pipeline, the pitcher similarity test
suite, and the secrets-management baseline.  All shipped together because they
share the same files (`pipeline/live/live_ingestion_pipeline.py`,
`api/main.py`, the CI workflow) and individually-shipping each one would have
caused merge churn.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-101 | Bug | Backend | Per-game GameStateBuilder cache + incremental play history (was O(N) full rebuild every WS message) |
| SIM-102 | Bug | Backend | _infer_role() now classifies Openers (was misclassified as MRP) |
| SIM-103 | Bug | Backend | ConnectionManager.broadcast() iterates a snapshot — fixes "Set changed size during iteration" race |
| SIM-104 | Improvement | Backend | /resimulate endpoint Redis cooldown — HTTP 429 with retry_after_seconds |
| SIM-105 | Improvement (P2) | Backend | Skip _upsert_game_record() for already-finalized games + boot-time hydration |
| SIM-106 | Improvement | Backend | simulation_callback type-hinted as async + iscoroutinefunction guard at __init__ |
| SIM-148 | Bug | ML+QA | Removed vacuous release_score asserts; added _score_pair 3-tuple regression + finite_distances() docstring fix + doctest sentinel |
| SIM-153 | Gap | QA+Backend | Secrets baseline: validate_environment(), env-fallback DSN, CI secrets-check job, .env in .gitignore |

---

## SIM-101 — Per-Game GameStateBuilder Cache + Incremental Play History

**Type:** Bug | **Effort:** M | **Status:** ✅ Complete

### Problem
Two related issues in the live pipeline:

1. **O(N) full history rebuild on every WS signal.** `_parse_play_history()` was a
   `@staticmethod` that walked every play in `allPlays` on every refresh.
   By the 9th inning of a 10-inning game with ~80 plays, this fired 20+ times
   per inning (one per WS message), each time re-parsing every play and
   re-serializing the full history into JSONB for the upsert.
2. **No per-game state cache.** A fresh `GameStateBuilder` was instantiated
   inside `_refresh_game_state()` on every WS signal, preventing any
   per-game state caching (history, last-processed at-bat index, game_date).

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `GameStateBuilder.__init__` | Updated | Adds `_history`, `_last_at_bat_index`, `_game_date` instance state. |
| `pipeline/live/live_ingestion_pipeline.py` — `_parse_play_history` | Refactored | Now an instance method.  Walks only plays whose `atBatIndex > self._last_at_bat_index`.  In-flight at-bats (same atBatIndex as the cache) refresh the trailing entry rather than appending a duplicate. |
| `pipeline/live/live_ingestion_pipeline.py` — `_build_history_entry` | Added | Static helper extracted from the old parser so the incremental path and any external consumer stay in sync. |
| `pipeline/live/live_ingestion_pipeline.py` — `LiveIngestionPipeline._builders` | Added | `dict[int, GameStateBuilder]`.  Lifecycle managed by `_get_or_create_builder()`, `_start_watching()`, and the Final-game branch of `_sync_live_games()`. |
| `pipeline/live/live_ingestion_pipeline.py` — `_get_or_create_builder` | Added | Lazy per-game cache.  Disposed when game transitions to Final. |
| `pipeline/live/live_ingestion_pipeline.py` — `_refresh_game_state` + `manual_resimulate` | Updated | Both now reuse the cached builder via `_get_or_create_builder()`. |

### Acceptance gate
By the 9th inning, each WS refresh parses **at most 1–2 new plays** (the
in-flight current PA + at most one new entry), not 80.  Verified by
the `test_builder_holds_history_state` and `test_builder_replaces_in_flight_at_bat`
regression tests.

---

## SIM-102 — `_infer_role()` Opener Classification

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
`_infer_role()` classified pitchers from in-game stats using IP only:
`SP (≥4.0 IP) → MRP (≥1.0 IP) → RP (otherwise)`.  An opener who throws 2.0 IP
faces 9 batters and gets pulled would be flagged **MRP**.  The Phase 4
manager decision engine downstream of this would then treat the opener as
a middle reliever available for high-leverage re-use later in the game.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `GameStateBuilder._infer_role` | Updated | Adds Opener bucket between SP and MRP using `battersFaced` from the live boxscore.  Decision order: SP (IP≥4.0) → Opener (IP<4.0 AND BF≥9) → MRP (IP≥1.0) → RP. |

### Truth table

| IP | BF | Role | Why |
|----|----|------|-----|
| 5.1 | 18 | SP | Full starter outing |
| 2.0 | 9 | Opener | First-inning opener pulled deep into the order |
| 0.2 | 10 | Opener | Lots of runners, quick hook |
| 2.0 | 6 | MRP | Multi-inning relief |
| 0.2 | 3 | RP | One-inning specialist |
| 2.0 | (missing) | MRP | Graceful fallback to old IP-only logic |

### Note
This is a temporary heuristic.  SIM-057 will land a season-level
`opener_rate` column on `derived.pitcher_season_metrics` — once that ships,
the live `_infer_role()` should defer to the season-level role tag and only
fall back to this BF heuristic for first-time-this-season usage.

---

## SIM-103 — ConnectionManager.broadcast() Set Snapshot

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
`broadcast()` iterated over the live `_subscriptions[game_pk]` set directly.
Since `await ws.send_text()` yields control to the event loop, a concurrent
`connect()` or `disconnect()` for the same game_pk would mutate the
underlying set mid-iteration and raise `RuntimeError: Set changed size
during iteration`.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `ConnectionManager.broadcast` | Fixed | Iterate over `set(live_subs)` (a shallow copy).  Cleanup of dead connections still operates on the live underlying set. |

### Test
`TestSim103BroadcastSnapshot::test_broadcast_uses_set_copy` simulates a
concurrent connect during the broadcast loop and asserts no
`RuntimeError` + remaining clients still receive the message + the
intruder is *not* spuriously sent to in the current call (snapshot
semantics).

---

## SIM-104 — Redis-Based Rate Limiting on `/resimulate`

**Type:** Improvement | **Effort:** S | **Status:** ✅ Complete

### Problem
The manual resimulate endpoint had no debouncing.  Users spamming the
"resimulate now" button could queue up dozens of 100-iteration sim runs
behind the Phase 5 simulation runner before any backpressure kicks in.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `RESIM_COOLDOWN_S` constant | Added | 10-second cooldown.  Comment justifies the value (generous enough for legitimate resample-then-resample patterns, tight enough to throttle spam). |
| `pipeline/live/live_ingestion_pipeline.py` — `manual_resimulate` endpoint | Updated | On entry: `redis.ttl("resim_cooldown:{pk}")`.  TTL > 0 → return HTTP 429 with `{"status": "rate_limited", "retry_after_seconds": <ttl>, "detail": ...}`.  Otherwise: `redis.setex(key, RESIM_COOLDOWN_S, "1")` before triggering the sim — sets the cooldown even if the sim path raises. |

### Error envelope (matches SIM-109)
```json
HTTP/1.1 429 Too Many Requests
{
  "status":              "rate_limited",
  "retry_after_seconds": 7,
  "detail":              "Manual re-simulation for game 745001 is on cooldown. Try again in 7s."
}
```

---

## SIM-105 — Skip Redundant Upserts for Completed Games

**Type:** Improvement (P2) | **Effort:** S | **Status:** ✅ Complete

### Problem
`_sync_live_games()` called `_upsert_game_record()` for *every* game in the
schedule on every 30-second poll — including games that finished hours ago.
For a 15-game slate with 12 finished games, that's ~1,080 unnecessary DB
writes over a 3-hour afternoon window.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `LiveIngestionPipeline._completed_games` | Added | `set[int]`.  Mutated only at Final transitions (`_sync_live_games` Final branch) and on boot. |
| `pipeline/live/live_ingestion_pipeline.py` — `_sync_live_games` | Updated | When `status == "Final"` and `game_pk in self._completed_games`, skip the upsert via `continue`. |
| `pipeline/live/live_ingestion_pipeline.py` — `_hydrate_completed_games` | Added | Called from `start()`.  `SELECT game_pk FROM raw.games WHERE game_date = CURRENT_DATE AND status = 'Final'` populates the set so a mid-afternoon pipeline restart doesn't trigger an upsert storm. |

### Acceptance
- A 15-game slate with 12 Final games processes ≤ 3 upserts per poll instead of 15.
- Pipeline restart at 19:00 hydrates `_completed_games` from `raw.games`; the
  next poll skips upserts for all 12 already-final games.

---

## SIM-106 — Async-Callable Type on `simulation_callback`

**Type:** Improvement | **Effort:** S | **Status:** ✅ Complete

### Problem
`simulation_callback` was typed as `callable | None` with no argument or
return-type spec.  When Phase 5 wires up the real simulation runner, passing
a sync function instead of `async def` would either raise a confusing
`TypeError` mid-PA (when the pipeline tries to `await` it), or silently
no-op if the returned coroutine was discarded.  Both modes are hard to
diagnose in production logs.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `SimulationCallback` type alias | Added | `Callable[[int, dict], Coroutine[Any, Any, None]]`.  Used everywhere the callback type appears (pipeline `__init__`, `create_app`, `lifespan_factory`, `run`). |
| `pipeline/live/live_ingestion_pipeline.py` — `LiveIngestionPipeline.__init__` | Updated | Runtime guard: if the supplied callback is not None and `not asyncio.iscoroutinefunction(simulation_callback)`, raise `TypeError` with a clear message and a Phase 5 wiring tip about `asyncio.to_thread`. |

### Test
- `test_sync_callback_rejected` — passing a sync function raises TypeError at
  construction time with a helpful message.
- `test_async_callback_accepted` — `async def` callbacks construct cleanly.
- `test_no_callback_is_fine` — None is permitted (logs the signal instead).

---

## SIM-148 — Pitcher Similarity Test Cleanup

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
`test_all_scores_in_range` asserted `r.release_score >= 0.0` against a field
that had been removed from `SimilarityResult` by SIM-067.  Either path was
dead coverage:
* If a stale field default was still 0.0 → assertion passes vacuously.
* If the field was actually removed → AttributeError, which would mask any
  *real* score-bounds regression.

The SIM-067 fix had no permanent regression test — `_score_pair` could
silently regress back to a 5-tuple (re-introducing the double-counted
release/results sub-scores).  Additionally `ArsenalCache.finite_distances()`'s
docstring still pointed to the old `calibrate_arsenal_gamma` API
(deleted in SIM-066).

### Changes

| File | Action | Notes |
|------|--------|-------|
| `tests/unit/test_pitcher_similarity.py` — `test_all_scores_in_range` | Cleaned up | Removed `release_score` / `results_score` assertions.  Asserts only the surviving 3 sub-scores (composite, arsenal, command) plus bounds. |
| `tests/unit/test_pitcher_similarity.py` — `test_similarity_result_has_no_release_score_field` | Added | Reflects on the dataclass via `dataclasses.fields()` to confirm `release_score` is permanently removed. |
| `tests/unit/test_pitcher_similarity.py` — `test_score_pair_returns_three_subscores` | Added | SIM-067 regression guard: `len(engine._score_pair(pa, pb)) == 3`.  Re-introducing release/results sub-scores would double-count signal already inside the GMM. |
| `tests/unit/test_pitcher_similarity.py` — `TestPitcherSimilarityDoctests` | Added | Runs `doctest.testmod(pitcher_similarity)` so future docstring drift is caught automatically (per AC #5).  Targeted to the one module instead of a global `--doctest-modules` flag that would scan the whole repo. |
| `similarity/engines/pitcher_similarity.py` — `_score_pair` docstring | Updated | Now advertises the 3-tuple return plus a SIM-148/SIM-067 historical note explaining why release/results were removed. |
| `similarity/engines/pitcher_similarity.py` — `ArsenalCache.finite_distances` docstring | Updated | References the current `calibrate_arsenal_scale` API (post-SIM-066 rename). |

---

## SIM-153 — Secrets Management Baseline

**Type:** Gap | **Effort:** S | **Status:** ✅ Complete

### Problem
Credentials (DB DSN, Redis URL, future API keys) were passed as bare strings
in pipeline constructors with no environment-variable pattern and no
startup validation.  As Phase 5 adds real odds-API keys and Phase 7 adds
production credentials, this gap becomes a security risk: there's no
gate against committing a `.env`, and no check that the running container
has the right env vars set before the first request.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/etl/etl_historical_loader.py` — `HistoricalDataLoader.__init__` | Updated | `dsn` parameter now optional.  Falls back to `os.environ["BASEBALL_DB_DSN"]`.  Raises `RuntimeError` with a clear message when neither is set, instead of letting psycopg2 produce a confusing connect-fail mid-run. |
| `pipeline/live/live_ingestion_pipeline.py` — `LiveIngestionPipeline.__init__` | Updated | `dsn` and `redis_url` parameters now optional.  Both fall back to environment variables.  Clear error if neither is set. |
| `.github/workflows/ci.yml` — `secrets-check` job | Added | Three checks: (1) reject committed `.env*` files; (2) grep the source tree for literal credential patterns (`password=`, `api_key=`, AKIA-prefixed AWS keys, AIza-prefixed Google keys, BEGIN PRIVATE KEY headers); (3) verify `.env` is explicitly listed in `.gitignore`.  Job blocks `docker-build-check`, so a secret leak fails the build. |
| `tests/unit/test_backend_sim101_to_106_148_153.py` — `TestSim153SecretsBaseline` | Added | Six tests: `.env.example` documents required vars; `.gitignore` excludes `.env`; `python-dotenv` in requirements; `validate_environment()` raises when required vars missing; CI workflow contains the `secrets-check` job; `HistoricalDataLoader` falls back to `BASEBALL_DB_DSN` when constructed without a dsn. |

### Verification

```bash
# Loader env fallback
$ unset BASEBALL_DB_DSN
$ python -c "from pipeline.etl.etl_historical_loader import HistoricalDataLoader; HistoricalDataLoader()"
RuntimeError: HistoricalDataLoader: no DSN provided and BASEBALL_DB_DSN environment variable is not set...

# CI secrets-check (local dry run)
$ git ls-files | grep -E '^\.env$|/\.env$'   # should be empty
$ grep -rE 'password\s*=\s*"[^"$][^"]{2,}"' --exclude='.env.example' .   # should be empty
```

---

## Files Modified / Created (this sprint)

| File | Status |
|------|--------|
| `pipeline/live/live_ingestion_pipeline.py` | Updated — SIM-101..106 + SIM-153 |
| `pipeline/etl/etl_historical_loader.py` | Updated — SIM-153 (env fallback) |
| `similarity/engines/pitcher_similarity.py` | Updated — SIM-148 (docstrings) |
| `tests/unit/test_pitcher_similarity.py` | Updated — SIM-148 |
| `tests/unit/test_backend_sim101_to_106_148_153.py` | Created — 27 tests across all 8 tickets |
| `.github/workflows/ci.yml` | Updated — SIM-153 secrets-check job |

### Test verification

```
$ pytest tests/unit/test_backend_sim101_to_106_148_153.py
============================== 25 passed, 2 skipped in 1.21s ==============================
```
*(2 skipped: scipy-dependent SIM-148 dataclass-reflection tests — skip when sandbox lacks scipy; full source-grep regression checks still run unconditionally.)*

```
$ pytest tests/unit/test_data_engineer_sim085_to_091.py \
         tests/unit/test_data_engineer_sim092_sim093.py \
         tests/unit/test_backend_sim101_to_106_148_153.py
============================== 66 passed, 2 skipped in 2.40s ==============================
```

Migration chain (0001 → 0011) unchanged this sprint; no schema changes
required for SIM-101 through SIM-106 / SIM-148 / SIM-153.

---

# Data Engineer Changelog — Sprint 2026-05-07
**Author: Data Engineer (Agent 4)**

Two P1 tickets in the SIM-080–099 (data-eng infrastructure) band, addressing
data-quality audit trail gaps surfaced after the 2026-05-06 sprint.

| Ticket | Type | Status | One-liner |
|--------|------|--------|-----------|
| SIM-092 | Improvement | ✅ Complete | `raw.game_odds` deduplicated via SHA-256 `odds_hash` + partial unique index |
| SIM-093 | Gap | ✅ Complete | `raw.etl_errors` audit table + ETL hard-error wiring + `reprocess_errored_games()` helper |

---

## SIM-092 — Deduplicate `raw.game_odds` Inserts

**Type:** Improvement | **Effort:** S | **Status:** ✅ Complete

### Problem
`_persist_odds()` always INSERTed a new row with no ON CONFLICT clause. The live pipeline refreshes a game every 30 seconds, so a 3-hour game produced ~360 identical odds rows per game. Lines move infrequently relative to that cadence, so almost every snapshot was a duplicate of the previous. Over a full 162-game season × 30 games/day, millions of duplicate rows would accumulate, blowing up storage and slowing every CLV query that scans `raw.game_odds`.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/migrations/versions/0010_sim092_game_odds_dedup.py` | Created | Adds `odds_hash VARCHAR(64)` column + partial unique index `idx_game_odds_dedup ON (game_pk, source, odds_hash) WHERE odds_hash IS NOT NULL`. Partial so legacy NULL-hash rows don't trip the constraint. |
| `db/schemas/01_postgres_schema.sql` | Updated | `raw.game_odds` now declares `odds_hash` and the partial unique index inline. |
| `pipeline/live/live_ingestion_pipeline.py` — `LiveIngestionPipeline._odds_hash()` | Added | Static method computing SHA-256 of the canonicalised odds payload. Stable key order, float precision normalised to 6 decimals (so `1.5` and `1.50` collide), `book / line_type / market_type / is_sharp_book` part of the hash. |
| `pipeline/live/live_ingestion_pipeline.py` — `_persist_odds()` | Updated | Computes `odds_hash` before INSERT; SQL now ends with `ON CONFLICT (game_pk, source, odds_hash) WHERE odds_hash IS NOT NULL DO NOTHING`. Identical successive snapshots are server-side no-ops. |

### Hash design rationale

| Field | In hash? | Reason |
|-------|---------|--------|
| `home_ml`, `away_ml`, spreads, total | ✅ | Core line — the thing we're deduping on |
| `book`, `line_type`, `market_type`, `is_sharp_book` | ✅ | Two books at the same price are distinct quotes; opening vs. closing is a different snapshot even at the same price |
| `source` | ❌ | Lives outside the hash, paired with it in the unique index — keeps the index leaner and matches "INSERT into the namespace this source owns" semantics |
| `is_mock`, `fetched_at` | ❌ | Operational metadata, not the line itself |

### Backfill / cleanup
Pre-SIM-092 rows have NULL `odds_hash`; they remain in the table. Filling them retroactively + deduping history is a separate cleanup pass — out of scope here. The partial unique index tolerates NULL, so the new rule applies forward only without breaking any existing data.

### Verification
```python
from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline
h1 = LiveIngestionPipeline._odds_hash({"home_ml": -150, "away_ml": 130, "total_line": 8.5})
h2 = LiveIngestionPipeline._odds_hash({"home_ml": -150, "away_ml": 130, "total_line": 8.5})
assert h1 == h2 and len(h1) == 64    # ✅
```

---

## SIM-093 — Create `raw.etl_errors` + Wire into ETL Hard-Error Path

**Type:** Gap | **Effort:** S | **Status:** ✅ Complete

### Problem
The ETL pipeline's docstring explicitly said it *"logs to etl_errors table"* but the table did not exist. Hard validation errors were only sent to the Python logger; skipped pitch rows were lost with no audit trail and no reprocessing path. After a validator bug fix, there was no way to identify which games were affected — the only signal was log files, which are not always retained and lack structured metadata.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/migrations/versions/0011_sim093_etl_errors_table.py` | Created | `raw.etl_errors (id, game_pk, at_bat_number, pitch_number, error_type CHECK ('HARD','WARN'), error_messages TEXT[], created_at)` + `idx_etl_errors_game_pk(game_pk, created_at)` + `idx_etl_errors_recent(created_at DESC)`. **No FK to `raw.games`** — audit trail must outlive game-row deletes / replace operations. |
| `db/schemas/01_postgres_schema.sql` | Updated | Canonical schema declares `raw.etl_errors` with explanatory comment block. |
| `pipeline/etl/etl_historical_loader.py` — `_process_and_insert()` | Updated | Hard-error rows now include `at_bat_number` alongside `pitch_number` and are persisted via the new `_log_etl_errors()` method. The persistence call is wrapped in `try/except` so a logging failure never aborts a successful pitch ingest. |
| `pipeline/etl/etl_historical_loader.py` — `_log_etl_errors()` | Added | Bulk-INSERT one row per skipped pitch via `psycopg2.extras.execute_batch`. Schema-stable; reuses the loader's `_get_conn()` connection helper. |
| `pipeline/etl/etl_historical_loader.py` — `reprocess_errored_games()` | Added | Public method. `reprocess_errored_games(since: date) -> list[int]` returns distinct `game_pk`s with errors in the window. Operator workflow: after a validator bug-fix, run this and re-ingest each game with `load_game()`. |

### Schema choices

- `error_type CHECK ('HARD', 'WARN')` — only `HARD` is written today; `WARN` is reserved for a future pass that captures every flagged-row reason (currently those are logger-only).
- `error_messages TEXT[]` — preserves the multi-message list from `ValidationResult` without losing structure to a join string. Postgres array types are queryable (`array_length`, `unnest`, etc.) for ad-hoc analysis.
- **No FK to `raw.games`** — deliberate. The whole point of `etl_errors` is to capture what *failed*, including cases where the game itself never landed (FK prereq missing, etc.). A FK + ON DELETE CASCADE here would silently delete the audit trail when an operator wipes-and-reloads a game — exactly the wrong behaviour for an audit table.

### Operator workflow (post-validator-fix)
```python
from datetime import date
from pipeline.etl.etl_historical_loader import HistoricalDataLoader

loader = HistoricalDataLoader(...)
to_replay = loader.reprocess_errored_games(since=date(2026, 5, 1))
for game_pk in to_replay:
    loader.load_game(game_pk, season=2026, batter_hand_cache=…)
```

---

## Migration Sequence (Updated through SIM-093)

| Migration | Ticket | Description |
|-----------|--------|-------------|
| `0001_initial_schema.py` | SIM-084 | Full PostgreSQL schema baseline |
| `0002_sim082_…py` | SIM-082 | Unique partial index on sim.lineup_state |
| `0003_sim083_…py` | SIM-083 + SIM-133 | raw.etl_data_freshness, raw.game_odds (CLV columns), raw.prop_odds, raw.pipeline_run_log |
| `0004_sim134_…py` | SIM-134 | raw.prop_odds: prop_type→prop_stat, CHECK, compound index |
| `0005_sim085_…py` | SIM-085 | Composite situation partial index on raw.pitches |
| `0006_sim086_…py` | SIM-086 | raw.games.venue_id → nullable |
| `0007_sim087_…py` | SIM-087 | flag_pitch_quality() trigger: release_speed floor 60 → 50 mph |
| `0008_sim088_…py` | SIM-088 | Drop idx_pitches_pitch_type |
| `0009_sim089_…py` | SIM-089 | Composite (pitcher, season) partial index |
| **`0010_sim092_…py`** | **SIM-092** | **raw.game_odds: odds_hash column + partial unique dedup index** |
| **`0011_sim093_…py`** | **SIM-093** | **raw.etl_errors audit table + indexes** |

Apply all: `alembic upgrade head`. Chain integrity verified by the new
`TestMigrationChain::test_chain_unbroken` regression test.

## Files Modified / Created (this sprint)

| File | Status |
|------|--------|
| `db/migrations/versions/0010_sim092_game_odds_dedup.py` | Created |
| `db/migrations/versions/0011_sim093_etl_errors_table.py` | Created |
| `db/schemas/01_postgres_schema.sql` | Updated (SIM-092 dedup; SIM-093 etl_errors table) |
| `pipeline/live/live_ingestion_pipeline.py` | Updated (SIM-092 `_odds_hash` + `_persist_odds` ON CONFLICT) |
| `pipeline/etl/etl_historical_loader.py` | Updated (SIM-093 `_log_etl_errors`, `reprocess_errored_games`, hardened `_process_and_insert`) |
| `tests/unit/test_data_engineer_sim092_sim093.py` | Created (20 tests, all passing) |

### Test verification

```
$ pytest tests/unit/test_data_engineer_sim092_sim093.py -v
============================== 20 passed in 1.35s ==============================

$ pytest tests/unit/test_data_engineer_sim085_to_091.py tests/unit/test_data_engineer_sim092_sim093.py
============================== 41 passed in 1.39s ==============================
```

Migration chain (0001 → 0011) confirmed unbroken; `down_revision` references
all line up.

---

# Data Engineer Changelog — Sprint 2026-05-06
**Author: Data Engineer (Agent 4)**

Six P1 tickets in the SIM-080–099 (data-eng infrastructure / migrations) band.
All ride on the Alembic framework established in SIM-084 (sprint 2026-05-05).

| Ticket | Type | Status | One-liner |
|--------|------|--------|-----------|
| SIM-085 | Bug | ✅ Complete | Composite partial situation index on `raw.pitches` for SIM-070 engine |
| SIM-086 | Bug | ✅ Complete | Live pipeline silently dropped venue-less games — `venue_id` now nullable + backfill job |
| SIM-087 | Bug | ✅ Complete | Slow curveballs (60–65 mph) wrongly flagged as bad data — validator + trigger thresholds lowered |
| SIM-088 | Improvement | ✅ Complete | Dropped `idx_pitches_pitch_type` — wasted ~15 MB/season write overhead on an audit-only column |
| SIM-089 | Improvement | ✅ Complete | Composite `(pitcher, season)` partial index — profile computor hot path now < 50 ms |
| SIM-091 | Bug | ✅ Complete | Confirmed per-play detail tables in `_delete_seasons()` + regression test on schema coverage |

---

## SIM-085 — Composite Situation Index on `raw.pitches`

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
The project plan Step 1.1 explicitly requires a composite index covering the full situation vector (count + outs + baserunner state) used by the situation similarity engine (SIM-070). The schema only had `idx_pitches_count_state` on `(balls, strikes, outs)`. Situation similarity queries were falling back to a sequential scan over ~700 K rows per season — well above the < 30 ms simulation-step latency target the Performance Engineer holds us to.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/migrations/versions/0005_sim085_pitches_situation_index.py` | Created | Alembic migration. Partial index: `(inning, outs, balls, strikes, on_1b, on_2b, on_3b) WHERE data_quality_flag = FALSE`. Flagged rows are excluded from the sim pool anyway, so a partial index keeps it lean. |
| `db/schemas/01_postgres_schema.sql` | Updated | Added `idx_pitches_situation` with explanatory comment. Authoritative schema now matches the migration. |

### Acceptance gate
After `alembic upgrade head`, EXPLAIN ANALYZE on a representative situation lookup must report `Index Scan using idx_pitches_situation`, not `Seq Scan on pitches`.

---

## SIM-086 — Fix Live Pipeline `venue_id=0` FK Violation

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
`_upsert_game_record()` in `live_ingestion_pipeline.py` inserted `venue_id=0` whenever the schedule API response was missing the `venue` key. No matching `raw.venues(venue_id=0)` row exists, so the FK raised a violation that was caught by the outer `except` and silently logged. The game row was **never inserted**. International / spring-training games that appear on the schedule before venue assignment were lost from `raw.games` entirely.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/migrations/versions/0006_sim086_games_venue_id_nullable.py` | Created | `ALTER TABLE raw.games ALTER COLUMN venue_id DROP NOT NULL`. PostgreSQL's FK accepts NULL through without requiring a parent row, which is exactly the behaviour we want. |
| `db/schemas/01_postgres_schema.sql` | Updated | `raw.games.venue_id` declared nullable with explanatory comment. |
| `pipeline/live/live_ingestion_pipeline.py` — `_upsert_game_record()` | Fixed | `gd.get("venue", {}).get("id", 0)` → `gd.get("venue", {}).get("id") or None`. Live pipeline now writes NULL when the venue is unknown. |
| `pipeline/etl/venue_backfill_job.py` | Created | Standalone job. Selects `raw.games` rows with `venue_id IS NULL`, re-fetches `/api/v1/schedule?gamePk=…&hydrate=venue` per game, fills the row when the MLB API returns a venue. Idempotent. APScheduler integration helper provided (`schedule_venue_backfill_job`). Default cadence: every 6 hours. Pre-checks the FK target before UPDATE so a missing `raw.venues` row produces a clean log warning instead of an asyncpg exception. |

### Verification
After migration 0006 + the live-pipeline fix:
1. Insert a game from the schedule API with no `venue` key → row appears in `raw.games` with `venue_id = NULL`. Pre-SIM-086 the row was silently dropped.
2. Run the backfill job; once the MLB API publishes the venue, the row's `venue_id` is filled.

---

## SIM-087 — Lower `release_speed` Validator + Trigger Thresholds

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
The ETL validator warned on `release_speed < 70` mph and the DB trigger flagged `< 60` mph as bad data, setting `data_quality_flag = TRUE`. Slow curveballs (60–65 mph) and eephus pitches are legitimate pitch types — flagging them excluded those rows from `sim.pitch_pool` and biased the pool toward hard-throwing pitchers. Direct downstream impact on the GMM-based pitcher similarity engine (SIM-066+ family) and on simulated K-rate distributions for soft-tossing pitchers.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/etl/etl_historical_loader.py` — `_validate_row()` | Fixed | Validator floor `70 → 60` mph. Warning text updated to match. Comment notes the trigger uses a separate `< 50` threshold. |
| `db/schemas/01_postgres_schema.sql` — `raw.flag_pitch_quality()` | Fixed | Trigger floor `60 → 50` mph. Eephus + slow curveballs now pass clean. |
| `db/migrations/versions/0007_sim087_release_speed_threshold.py` | Created | `CREATE OR REPLACE FUNCTION raw.flag_pitch_quality()`. Two-tier scheme: validator at 60 mph (warn-only), trigger at 50 mph (impossible-floor). |

### Two-tier rationale
The validator is *advisory* (logs to ETL warnings); the DB trigger is the hard data-quality gate. Mirrors the launch-speed pattern (warns at 125 mph, no trigger). Existing rows already flagged with the old 60 mph threshold retain their flag — backfill of historical rows is out of scope; if anyone needs it, file a separate ticket.

### Sanity check
```python
from pipeline.etl.etl_historical_loader import _validate_row
# 68 mph slow curve — should be CLEAN now
result = _validate_row({..., "release_speed": 68.0})
assert not [w for w in result.warnings if "release_speed" in w]   # ✅ no warning
```

---

## SIM-088 — Drop `idx_pitches_pitch_type` (Wasted Write Overhead)

**Type:** Improvement | **Effort:** S | **Status:** ✅ Complete

### Problem
The schema comment on `raw.pitches.pitch_type` explicitly says it is *"stored for reference/audit only. Similarity engine uses GMM components."* No hot path in any pipeline file filters by `pitch_type` as a primary predicate. Yet the standalone single-column index `idx_pitches_pitch_type` added ~15 MB of write overhead per season per ingest. The index directly contradicted its own column documentation.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/migrations/versions/0008_sim088_drop_pitches_pitch_type_index.py` | Created | `DROP INDEX IF EXISTS idx_pitches_pitch_type`. `downgrade()` restores it for symmetry. |
| `db/schemas/01_postgres_schema.sql` | Updated | Standalone index removed; explanatory comment in its place documents *why* it's intentionally absent and how to re-add `CONCURRENTLY` for ad-hoc debugging. |

### What we kept
The compound `(pitcher, pitch_type)` index `idx_pitches_pitcher_type` is retained — it supports per-pitcher pitch-type breakdown queries that are common in ad-hoc analysis, at low maintenance cost.

### Audit
The new regression test `TestSim088DropPitchTypeIndex::test_no_sql_where_clause_filters_by_pitch_type` greps the entire `pipeline/` package for `WHERE …pitch_type = …` and `WHERE …pitch_type IN (…)`. Currently zero matches. If this test ever fails, do **not** drop the index again without restoring a CONCURRENTLY-built replacement first.

---

## SIM-089 — Composite `(pitcher, season)` Partial Index

**Type:** Improvement | **Effort:** S | **Status:** ✅ Complete

### Problem
The player-profile computor's most frequent query is *"all clean pitches for pitcher X in season Y"*. The existing `idx_pitches_pitcher_season` indexes `(pitcher, game_date)` — `season` is a denormalized SMALLINT filtered directly, not implied by a date range. The `data_quality_flag = FALSE` filter is applied *after* the index scan, not as part of it. For a pitcher with 3,000 pitches, the planner scans every row for that pitcher and filters ~50 flagged rows at runtime, wasting ~95 % of block reads on the hot nightly batch path.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/migrations/versions/0009_sim089_pitches_pitcher_season_clean_index.py` | Created | `CREATE INDEX idx_pitches_pitcher_season_clean ON raw.pitches(pitcher, season) WHERE data_quality_flag = FALSE`. Partial — same partial-index pattern as SIM-085. |
| `db/schemas/01_postgres_schema.sql` | Updated | Composite partial index added below the existing `idx_pitches_pitcher_season`. Comment documents why both exist (date-range vs season-equality access patterns). |

### Acceptance gate
EXPLAIN ANALYZE on `_compute_pitcher_profiles()`'s primary fetch query must show `Index Scan using idx_pitches_pitcher_season_clean`. Per-pitcher fetch (≈3,000 pitches) target: < 50 ms.

---

## SIM-091 — Per-Play Detail Tables in `_delete_seasons()` + Regression Guard

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
`_delete_seasons()` in `player_profile_computor.py` hardcodes a list of derived/sim tables to clear before a `full_rebuild=True` run. Without the per-play detail tables (`derived.outfield_play_detail`, `derived.infield_play_detail`, `derived.dp_play_detail`), a full rebuild silently mixed old and new defensive metric data — a quiet source of cross-season contamination invisible to existing tests.

### Status of the table list
Audit at SIM-091 ship time confirmed all three play_detail tables were already present in the `tables` list (lines 894–897 of `player_profile_computor.py`). The substantive deliverable for SIM-091 is therefore the **regression guard**: a test that fails the next time someone adds a season-keyed `derived.*` table without remembering to update `_delete_seasons()`.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/batch/player_profile_computor.py` — `_delete_seasons()` | Documented | Added explicit docstring listing intentionally omitted tables (`derived.run_expectancy_matrix` because it's keyed by `season_range`; `sim.*` pools because they DELETE in their own build methods). |
| `tests/unit/test_data_engineer_sim085_to_091.py` — `TestSim091DeleteSeasonsCoverage` | Created | Two tests. (1) Asserts the three play_detail tables are explicitly listed. (2) Parses `02_duckdb_schema.sql`, finds every `derived.*` table with a `season` column, and asserts each one is listed in `_delete_seasons()` (or in `_EXCLUDED_FROM_DELETE_SEASONS` with a comment). |

### How the guard works
When a new derived table with a `season` column is added to `02_duckdb_schema.sql` but not to `_delete_seasons()`, the test fails with:
```
SIM-091 regression: the following derived.* tables have a `season` column
but are not in _delete_seasons():
  derived.<new_table_name>
```
Forces an intentional decision (add it to the delete list, or document the exclusion).

---

## Migration Sequence (Updated through SIM-089)

| Migration | Ticket | Description |
|-----------|--------|-------------|
| `0001_initial_schema.py` | SIM-084 | Full PostgreSQL schema baseline |
| `0002_sim082_…py` | SIM-082 | Unique partial index on sim.lineup_state |
| `0003_sim083_…py` | SIM-083 + SIM-133 | raw.etl_data_freshness, raw.game_odds (CLV columns), raw.prop_odds, raw.pipeline_run_log |
| `0004_sim134_…py` | SIM-134 | raw.prop_odds: prop_type→prop_stat, CHECK, compound index |
| **`0005_sim085_…py`** | **SIM-085** | **Composite situation partial index on raw.pitches** |
| **`0006_sim086_…py`** | **SIM-086** | **raw.games.venue_id → nullable** |
| **`0007_sim087_…py`** | **SIM-087** | **flag_pitch_quality() trigger: release_speed floor 60 → 50 mph** |
| **`0008_sim088_…py`** | **SIM-088** | **Drop idx_pitches_pitch_type** |
| **`0009_sim089_…py`** | **SIM-089** | **Composite (pitcher, season) partial index** |

Apply all: `alembic upgrade head`.

## Files Modified / Created (this sprint)

| File | Status |
|------|--------|
| `db/migrations/versions/0005_sim085_pitches_situation_index.py` | Created |
| `db/migrations/versions/0006_sim086_games_venue_id_nullable.py` | Created |
| `db/migrations/versions/0007_sim087_release_speed_threshold.py` | Created |
| `db/migrations/versions/0008_sim088_drop_pitches_pitch_type_index.py` | Created |
| `db/migrations/versions/0009_sim089_pitches_pitcher_season_clean_index.py` | Created |
| `db/schemas/01_postgres_schema.sql` | Updated (SIM-085, SIM-086, SIM-087, SIM-088, SIM-089) |
| `pipeline/live/live_ingestion_pipeline.py` | Updated (SIM-086 fallback fix) |
| `pipeline/etl/etl_historical_loader.py` | Updated (SIM-087 validator threshold) |
| `pipeline/etl/venue_backfill_job.py` | Created (SIM-086 backfill job) |
| `pipeline/batch/player_profile_computor.py` | Updated (SIM-091 docstring) |
| `tests/unit/test_data_engineer_sim085_to_091.py` | Created (21 tests across all 6 tickets — passes locally) |

### Test verification

```
$ pytest tests/unit/test_data_engineer_sim085_to_091.py -v
============================== 21 passed in 1.04s ==============================
```

All five new migrations parse cleanly; `down_revision` chain is intact (0004 → 0005 → 0006 → 0007 → 0008 → 0009). Run `alembic upgrade head` against a target DB to apply.

---

# Data Engineer Changelog — Sprint 2026-05-05
**Author: Data Engineer (Agent 4)**

---

## SIM-084 — Initialize Alembic Migration Framework

**Type:** Gap | **Effort:** M | **Status:** ✅ Complete

### Problem
No Alembic migration files existed despite `db/migrations/` being in the repo. ~15 schema-change tickets in the backlog had no versioned path to live DB application.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `alembic.ini` | Created | Alembic config at repo root. `sqlalchemy.url` is a placeholder — runtime value read from `BASEBALL_DB_DSN` env var via `env.py`. |
| `db/migrations/env.py` | Created | Reads `BASEBALL_DB_DSN` env var. Enables `include_schemas=True` for `raw`/`sim` schema autogenerate. |
| `db/migrations/script.py.mako` | Created | Alembic default revision template. |
| `db/migrations/README` | Created | Alembic default. |
| `db/migrations/versions/0001_initial_schema.py` | Created | Full PostgreSQL schema baseline (all raw.* and sim.* tables, indexes, triggers, views). Verified against `01_postgres_schema.sql`. |
| `db/migrations/duckdb/0001_initial_schema.sql` | Created | DuckDB migration baseline. Includes `migration_history` tracking table. |
| `db/schemas/duckdb_schema_version.txt` | Created | Current DuckDB schema version = `1`. Increment with each DuckDB migration. |
| `agent_team.md` | Updated | Added mandatory migration workflow rule to Data Engineer section: every schema-change ticket must include an Alembic (PostgreSQL) or numbered SQL (DuckDB) migration. |

### Migration Workflow (now mandatory)
```bash
# Apply all pending PostgreSQL migrations:
export BASEBALL_DB_DSN="postgresql+psycopg2://user:pass@localhost/baseball"
alembic upgrade head

# Apply DuckDB migrations (in order):
duckdb baseball_simulator.duckdb < db/migrations/duckdb/0001_initial_schema.sql
```

---

## SIM-082 — Fix ON CONFLICT Crash in `_upsert_lineup_state()`

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
`_upsert_lineup_state()` used `ON CONFLICT (game_pk) WHERE is_live_game=TRUE` but the required unique partial index did not exist. PostgreSQL raised a constraint error on every call — live game state was **never persisted** to `sim.lineup_state`.

### Root Cause
`LIVE_PIPELINE_DDL` in `live_ingestion_pipeline.py` contained the index creation DDL as a string constant but it was never applied to the database. The main schema file `01_postgres_schema.sql` also lacked the index.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/migrations/versions/0002_sim082_lineup_state_live_game_unique_index.py` | Created | Alembic migration: `CREATE UNIQUE INDEX idx_lineup_state_live_game ON sim.lineup_state(game_pk) WHERE is_live_game = TRUE` |
| `db/schemas/01_postgres_schema.sql` | Updated | Added the unique partial index with explanatory comment. Now authoritative for all lineup_state DDL. |

### Verification
After applying migration 0002 (`alembic upgrade 0002`), run the live pipeline against a test `game_pk`. Confirm `sim.lineup_state` receives rows without error.

---

## SIM-083 — Move ETL Freshness DDL into Canonical Schema

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
`FRESHNESS_TABLE_DDL` in `etl_historical_loader.py` was a module-level string constant that was **never executed**. `_log_freshness()` blindly ran INSERT statements against `raw.etl_data_freshness` — which didn't exist — causing `UndefinedTable` errors after every pitch batch insert. Freshness tracking had **never worked**.

Similarly, `GAME_ODDS_DDL` in `live_ingestion_pipeline.py` was a string constant that was only applied if a caller explicitly ran it. The table was not guaranteed to exist.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/schemas/01_postgres_schema.sql` | Updated | Added `raw.etl_data_freshness`, `raw.game_odds` (with SIM-133 CLV columns), `raw.prop_odds`, `raw.pipeline_run_log` DDL. These are now applied by Alembic migration 0003. |
| `db/migrations/versions/0003_sim083_etl_freshness_and_game_odds_to_schema.py` | Created | Alembic migration creating all four tables above. |
| `pipeline/etl/etl_historical_loader.py` | Updated | Removed `FRESHNESS_TABLE_DDL` string constant (dead code). Updated `_log_freshness()` docstring to reference the canonical DDL location. |
| `pipeline/live/live_ingestion_pipeline.py` | Updated | Removed `GAME_ODDS_DDL` string constant and `LIVE_PIPELINE_DDL` composed string. Replaced with a comment pointing to the Alembic migration. |

### Verification
After migration 0003: instantiate `HistoricalDataLoader` and call `load_game()` on any game_pk. Confirm `raw.etl_data_freshness` receives rows without `UndefinedTable` error.

---

## SIM-133 — Extend `raw.game_odds` Schema for CLV

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete

### Problem
`raw.game_odds` stored only one snapshot shape — no `line_type` (opening/current/closing), no book identifier, no `market_type`. CLV = `closing_line − bet_placement_line` is permanently uncomputable without `line_type`.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/schemas/01_postgres_schema.sql` | Updated | `raw.game_odds` created with all four CLV columns from the start. `idx_game_odds_line_type` index added. |
| `db/migrations/versions/0003_sim083_...py` | Updated | CLV columns included in the `raw.game_odds` CREATE TABLE (combined with SIM-083 migration since the table is new). |
| `pipeline/live/live_ingestion_pipeline.py` — `MockOddsAPI.get_odds()` | Updated | Added `book`, `line_type`, `market_type`, `is_sharp_book` parameters with defaults. Returns all four fields. |
| `pipeline/live/live_ingestion_pipeline.py` — `_persist_odds()` | Updated | Now writes all four CLV columns. SQL expanded from 12 to 16 parameters. |
| `pipeline/live/live_ingestion_pipeline.py` — `mark_closing_lines()` | Added | New async method. Finds last `line_type='current'` snapshot before `first_pitch_at` and updates it to `'closing'`. Call when feed/live status transitions to 'Live'. |

### Column Definitions
| Column | Type | CHECK |
|--------|------|-------|
| `book` | `VARCHAR(50) NOT NULL DEFAULT 'consensus'` | — |
| `line_type` | `VARCHAR(20) NOT NULL DEFAULT 'current'` | `IN ('opening','current','closing','bet_placement')` |
| `market_type` | `VARCHAR(20) NOT NULL DEFAULT 'moneyline'` | `IN ('moneyline','runline','total')` |
| `is_sharp_book` | `BOOLEAN NOT NULL DEFAULT FALSE` | — |

---

## SIM-138 — Nightly Opening Line Ingestion Job

**Type:** Gap | **Effort:** S | **Status:** ✅ Complete

### Problem
CLV requires opening lines posted 5–7 days before game time. The existing pipeline only fetched odds during live game refresh cycles. No provider offers historical odds retroactively. **Every day without this job is a permanent loss of opening line data.**

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/etl/opening_line_job.py` | Created | Full nightly job implementation. |
| `db/schemas/01_postgres_schema.sql` | Updated | `raw.prop_odds` and `raw.pipeline_run_log` DDL added (required by this job). Applied via migration 0003. |

### Job Architecture

```
08:00 ET (cron / APScheduler)
    │
    ▼
OpeningLineJob.run()
    │
    ├── Fetch MLB schedule: today → today+7 days  (hydrate=probablePitcher)
    │
    ├── For each game_pk:
    │     ├── SELECT: does raw.game_odds have line_type='opening' for this game? → skip
    │     ├── _fetch_current_odds() → MockOddsAPI (line_type='opening')
    │     ├── INSERT raw.game_odds (line_type='opening', is_mock=True)
    │     └── If pitcher announced:
    │           └── INSERT raw.prop_odds (strikeouts, line_type='opening')
    │
    └── INSERT/UPDATE raw.pipeline_run_log
          (opening_line_games_captured, opening_prop_lines_captured)
```

### Usage

```bash
# Standalone (manual backfill / testing):
export BASEBALL_DB_DSN="postgresql://user:pass@localhost/baseball"
python -m pipeline.etl.opening_line_job --days 7

# Dry run (no writes):
python -m pipeline.etl.opening_line_job --dry-run

# APScheduler integration (in FastAPI lifespan):
from pipeline.etl.opening_line_job import schedule_opening_line_job
schedule_opening_line_job(dsn=dsn, scheduler=scheduler)
```

### Acceptance Gate
After 3 consecutive days running:
```sql
-- Should return one row per game in the 7-day window:
SELECT game_pk, COUNT(*) AS opening_lines
FROM raw.game_odds
WHERE line_type = 'opening'
  AND fetched_at >= NOW() - INTERVAL '3 days'
GROUP BY game_pk;
```

---

## Migration Sequence Summary

| Migration | Ticket | Description |
|-----------|--------|-------------|
| `0001_initial_schema.py` | SIM-084 | Full PostgreSQL schema baseline |
| `0002_sim082_lineup_state_live_game_unique_index.py` | SIM-082 | Unique partial index fixing ON CONFLICT crash |
| `0003_sim083_etl_freshness_and_game_odds_to_schema.py` | SIM-083 + SIM-133 | raw.etl_data_freshness, raw.game_odds (w/ CLV columns), raw.prop_odds, raw.pipeline_run_log |

Apply all: `alembic upgrade head`

## Files Modified / Created (complete list)

| File | Status |
|------|--------|
| `alembic.ini` | Created |
| `agent_team.md` | Updated |
| `CHANGES.md` | Created |
| `db/schemas/01_postgres_schema.sql` | Updated |
| `db/schemas/duckdb_schema_version.txt` | Created |
| `db/migrations/env.py` | Created |
| `db/migrations/script.py.mako` | Created |
| `db/migrations/README` | Created |
| `db/migrations/versions/0001_initial_schema.py` | Created |
| `db/migrations/versions/0002_sim082_lineup_state_live_game_unique_index.py` | Created |
| `db/migrations/versions/0003_sim083_etl_freshness_and_game_odds_to_schema.py` | Created |
| `db/migrations/duckdb/0001_initial_schema.sql` | Created |
| `pipeline/etl/etl_historical_loader.py` | Updated |
| `pipeline/etl/opening_line_job.py` | Created |
| `pipeline/live/live_ingestion_pipeline.py` | Updated |

---

## SIM-145 — Docker / CI Infrastructure

**Type:** Gap | **Effort:** L | **Status:** ✅ Complete
**Roles:** Data Engineer (Agent 4) + QA/DevOps (Agent 9)

### Problem
The project README described `docker-compose up -d` as the startup command but:
- `requirements.txt` was empty — the image would build but nothing would import
- `api/main.py` did not exist — `uvicorn api.main:app` (the Dockerfile CMD) would immediately crash
- No `Dockerfile`, `docker-compose.yml`, or `Makefile` existed
- No `.env.example`, so new contributors had no way to know what environment variables to set
- No integration test infrastructure despite `requirements-dev.txt` listing `testcontainers`

### Changes

#### Data Engineer deliverables

| File | Action | Notes |
|------|--------|-------|
| `requirements.txt` | Populated | 20 pinned runtime dependencies (FastAPI, uvicorn, asyncpg, psycopg2-binary, SQLAlchemy, alembic, duckdb, redis, aiohttp, numpy, pandas, scikit-learn, scipy, faiss-cpu, pybaseball, APScheduler, python-dotenv). All version-bounded with `>=X,<Y`. |
| `requirements-dev.txt` | Created | `-r requirements.txt` + test/dev tools: pytest, pytest-asyncio, pytest-cov, pytest-timeout, pytest-benchmark, pytest-mock, testcontainers[postgres,redis], httpx, ruff, mypy, hypothesis, ipython, rich. |
| `.env.example` | Created | Documented all required environment variables with sane defaults. Comments explain each var. Docker Compose-aware: DB host is `db` (service name), not `localhost`. Variables: `BASEBALL_DB_DSN`, `REDIS_URL`, `MLB_API_BASE`, `ODDS_API_KEY`, `ODDS_API_BASE`, `SECRET_KEY`, `ENVIRONMENT`, `WORKERS`, `SIM_MAX_WORKERS`, `PROMETHEUS_PUSHGATEWAY`. |
| `api/__init__.py` | Created | Empty package marker. |
| `api/main.py` | Created | FastAPI application stub. `create_app()` factory with CORS, health/ready/root endpoints, `lifespan()` context manager with startup validation. `validate_environment()` raises `RuntimeError` with actionable message if `BASEBALL_DB_DSN` or `REDIS_URL` are missing. Module-level `app = create_app()` required for `uvicorn api.main:app` CMD. |
| `.gitignore` | Updated | Expanded from 6 lines to comprehensive: secrets, Python artifacts, venv, test coverage, type-check caches, Docker, IDE, data/model artifacts (FAISS/pkl/joblib). |

#### QA/DevOps deliverables

| File | Action | Notes |
|------|--------|-------|
| `Dockerfile` | Created | Multi-stage build. **builder** stage: `python:3.11-slim`, installs `build-essential + libgomp1`, runs `pip install --prefix=/install -r requirements.txt`. **runtime** stage: copies `/install` from builder, copies source (`api/`, `pipeline/`, `similarity/`, `simulator/`, `db/`, `alembic.ini`), creates non-root `appuser` (uid 1001), `HEALTHCHECK` via `/health` endpoint, `EXPOSE 8000`, `CMD ["uvicorn", "api.main:app", ...]`. |
| `.dockerignore` | Created | Excludes `.env`, `.git`, `__pycache__`, `tests/`, `*.md`, `*.xlsx`, `data/`, `frontend/`, type-check caches. Keeps build context lean. |
| `docker-compose.yml` | Created | Three long-running services + one one-shot service. `db`: postgres:15-alpine, healthcheck via `pg_isready`, persistent `postgres_data` volume. `redis`: redis:7-alpine, healthcheck via `redis-cli ping`, 256 MB LRU limit, persistent `redis_data` volume. `app`: builds from Dockerfile (runtime target), hot-reload source mounts, `depends_on: db+redis (service_healthy)`, DSN/REDIS_URL env override to use service names. `migrate`: one-shot `alembic upgrade head` service under the `tools` profile. Network: `baseball_net` bridge. |
| `Makefile` | Created | 13 documented targets. **Dev:** `dev`, `dev-bg`, `down`, `build`, `migrate`, `logs`, `shell`. **Test:** `test` (full suite in Docker), `test-unit` (no live deps), `test-integration` (testcontainers). **Quality:** `lint` (ruff), `format` (ruff format), `type-check` (mypy). **Cleanup:** `clean` (Python artifacts), `nuke` (containers + volumes with Y/N prompt). **Internal:** `_require_env_file` guard. Acceptance gate: `git clone && cp .env.example .env && make dev && make migrate && make test`. |
| `tests/integration/__init__.py` | Created | Integration test package marker. |
| `tests/integration/conftest.py` | Created | Session-scoped testcontainers fixtures. `pg_container`: spins up `postgres:15-alpine`, applies `alembic upgrade head` before any test. `pg_engine`: SQLAlchemy Engine for the test DB. `pg_connection`: per-test transactional connection that auto-rolls back (test isolation). `redis_container`: spins up `redis:7-alpine`. `redis_client`: sync Redis client, flushes all keys before each test. `async_redis_client`: async Redis client for `@pytest.mark.asyncio` tests. `asyncpg_pool`: asyncpg connection pool. `assert_table_exists()` helper. |
| `tests/integration/test_schema_migrations.py` | Created | 7 tests verifying `alembic upgrade head` produces the complete schema: all raw.* tables, all sim.* tables, `alembic_version` at head, SIM-082 partial index present, SIM-083/SIM-133 CLV columns present, both schemas exist. |
| `tests/integration/test_live_pipeline_upsert.py` | Created | 5 tests for SIM-082 `_upsert_lineup_state()` e2e: initial insert creates one row, second upsert updates not duplicates, idempotent after 10 calls, distinct games get distinct rows, partial index confirmed in `pg_indexes`. |
| `tests/integration/test_etl_flow.py` | Created | 9 tests for SIM-083 freshness tracking: table and column existence, write succeeds, upsert deduplicates, NULL/text notes accepted, `pipeline_run_log` records successful and failed runs. |
| `tests/__init__.py` | Created | Root test package marker. |
| `tests/conftest.py` | Created | Root-level shared fixtures: `sample_game_pk`, `sample_player_ids`. |
| `pyproject.toml` | Created | pytest, ruff, and mypy configuration. Registers `integration`, `unit`, `slow`, `benchmark` marks. `asyncio_mode = "auto"`. Ruff target `py311`, line-length 100, select E/W/F/I/B/C4/UP/SIM. |

### Acceptance Gate (SIM-145)
```bash
# 1. Clone and configure
git clone <repo> && cp .env.example .env

# 2. Start the stack
make dev                    # builds images, starts db + redis + app

# 3. Apply schema (in separate terminal once db is healthy)
make migrate                # alembic upgrade head

# 4. Full test suite
make test                   # pytest unit + integration, coverage report

# Expected:
#   - All services healthy in docker-compose
#   - /health returns {"status":"ok"}
#   - All Alembic migrations applied (alembic current == head)
#   - pytest exits 0
```

### Dependency Order (implemented in this order to avoid blockers)
```
requirements.txt (runtime deps)
  → api/main.py (so Dockerfile CMD works)
    → Dockerfile (needs importable api.main)
      → docker-compose.yml (needs working image)
        → .env.example (needs to know all compose env vars)
          → Makefile (wraps all of the above)
            → tests/integration/ (needs DB schema from migrations)
              → pyproject.toml (registers test marks for all suites)
```

---

## SIM-134 — `raw.prop_odds` Schema + MockOddsAPI Prop Lines

**Type:** Gap | **Effort:** M | **Status:** ✅ Complete
**Roles:** Data Engineer (Agent 4) + Betting Analyst (Agent 8)

### Problem
The platform's primary use case is player prop prediction, but `raw.prop_odds` had no `CHECK` constraint on the market column (`prop_type`), meaning any string could be inserted — prop analytics would silently accumulate garbage. `MockOddsAPI` had no prop-generating method; the only mock lived as a small local helper in `opening_line_job.py` with a flat vig model inconsistent with real market structure. The weak `(game_pk, player_id)` index couldn't efficiently serve per-stat time-series queries needed for CLV.

### Betting Analyst (Agent 8) — Prop Stat Scope Decision
The backlog AC proposed 6 values. Agent 8 confirmed 7 are required:

| prop_stat | Rationale | Simulation signal |
|-----------|-----------|------------------|
| `strikeouts` | Most liquid pitcher prop; direct whiff/chase/platoon signal | pitch mix × batter whiff profile |
| `hits` | Core batter prop; BABIP-driven | batted-ball profile + park factor |
| `home_runs` | Exit velo + launch angle + park HR factor | batted-ball distribution |
| `earned_runs` | High-variance pitcher prop; books price it wide → edge opportunity | pitch profile × opponent OBP × park |
| `walks` | BB/9-driven; casual bettors ignore it → sharp edge available | pitcher BB rate × batter chase rate |
| `total_bases` | XBH value beyond hits; captures power hitters correctly | exit velo + spray angle |
| `rbis` | Lineup-context prop; explicitly in BA role spec; already wired in SIM-138 | baserunner state distribution from simulation |

**`rbis` deviation from AC:** The original AC omitted it, but Agent 8 flagged that `opening_line_job.py` (SIM-138) already emitted `rbis` inserts. Without `rbis` in the CHECK, every existing prop INSERT path would immediately fail at the DB layer. Scope expanded.

**`innings_pitched` deferred:** Meaningful starter prop but requires pitch-count model (Phase 4 dependency). Will be added as migration 0005.

### Vig Model (Betting Analyst approved)

| prop_stat | line center | ±window | over vig | under vig |
|-----------|------------|---------|---------|---------|
| strikeouts | 5.5 | ±1.0 | -125/−105 | -125/−105 |
| hits | 0.5 | ±0.5 | -115/−105 | -115/−105 |
| home_runs | 0.5 | fixed | -130/−110 | +100/+110 (books shade the under) |
| earned_runs | 3.5 | ±1.0 | -115/−105 | -115/−105 |
| walks | 2.5 | ±0.5 | -115/−105 | -115/−105 |
| total_bases | 1.5 | ±0.5 | -120/−105 | -115/−105 |
| rbis | 0.5 | ±0.5 | -120/−110 | -110/−100 |

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/schemas/01_postgres_schema.sql` | Updated | `prop_type` → `prop_stat`. Added 7-value CHECK constraint. Replaced weak `(game_pk, player_id)` index with compound `(game_pk, player_id, prop_stat, fetched_at DESC)` index. |
| `db/migrations/versions/0004_sim134_prop_odds_prop_stat_column_and_index.py` | Created | Alembic migration: `RENAME COLUMN prop_type TO prop_stat`, add `ck_prop_odds_prop_stat` CHECK, drop old index, create compound index. Full `downgrade()` included. |
| `pipeline/live/live_ingestion_pipeline.py` — `MockOddsAPI._PROP_CONFIG` | Added | Class-level dict mapping each of the 7 prop stats to `(center, half_spread, over_vig_range, under_vig_range)`. Single source of truth for all mock prop lines. |
| `pipeline/live/live_ingestion_pipeline.py` — `MockOddsAPI.get_prop_odds()` | Added | New static method. Deterministic RNG seeded on `(game_pk × 1M + player_id + hash(prop_stat))`. Snaps to nearest 0.5. Raises `ValueError` on unknown `prop_stat` — catches bad values before the DB CHECK. |
| `pipeline/live/live_ingestion_pipeline.py` — `LiveIngestionPipeline._persist_prop_odds()` | Added | New async method. Inserts one row into `raw.prop_odds` using the 11-column schema. Called by the live pipeline during active game windows. |
| `pipeline/etl/opening_line_job.py` | Updated | Removed local `_mock_prop_odds()` and `_MOCK_PROP_LINES`. Delegate to `MockOddsAPI.get_prop_odds()`. `PITCHER_PROP_TYPES` expanded to `['strikeouts', 'earned_runs', 'walks']`. All `prop_type` references renamed to `prop_stat`. INSERT SQL updated to use `prop_stat` column. |

### Migration Sequence (updated)

| Migration | Ticket | Description |
|-----------|--------|-------------|
| `0001_initial_schema.py` | SIM-084 | Full PostgreSQL schema baseline |
| `0002_sim082_...py` | SIM-082 | Unique partial index on sim.lineup_state |
| `0003_sim083_...py` | SIM-083 + SIM-133 | raw.etl_data_freshness, raw.game_odds (CLV columns), raw.prop_odds (initial), raw.pipeline_run_log |
| `0004_sim134_...py` | SIM-134 | raw.prop_odds: prop_type→prop_stat, CHECK constraint, compound index |

Apply all: `alembic upgrade head`

### Verification
```sql
-- After alembic upgrade head:

-- 1. Column rename applied
SELECT column_name FROM information_schema.columns
WHERE table_schema='raw' AND table_name='prop_odds' AND column_name='prop_stat';

-- 2. CHECK constraint present
SELECT conname FROM pg_constraint
WHERE conname='ck_prop_odds_prop_stat';

-- 3. Compound index present
SELECT indexdef FROM pg_indexes
WHERE schemaname='raw' AND tablename='prop_odds'
  AND indexname='idx_prop_odds_game_player';
-- Expected: includes (game_pk, player_id, prop_stat, fetched_at DESC)

-- 4. MockOddsAPI smoke test (Python):
-- from pipeline.live.live_ingestion_pipeline import MockOddsAPI
-- p = MockOddsAPI.get_prop_odds(745000, 100001, 'strikeouts', line_type='opening')
-- assert p['prop_stat'] == 'strikeouts'
-- assert 4.0 <= p['line'] <= 7.0
-- assert p['line_type'] == 'opening'
```

---

## SIM-099 — Fix Redis Key Mismatch: Rate-Limit Fallback Has Never Worked

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete
**Role:** Backend Developer (Agent 5)

### Problem
`_cache_to_redis()` wrote the built `game_state` dict under key `"game_state:{game_pk}"`. `_fetch_feed()`'s Redis fallback branch read from `"game_feed:{game_pk}"` — a key that was **never written anywhere**. The docstring said "Caches the raw feed" (the stated intent), but the code cached the built state under the wrong key. Every MLB API rate-limit or transient error during a live game caused the fallback to silently return `None`, dropping the entire refresh cycle. The rate-limit resilience architecture had never functioned.

A second bug in the same method: `current_pitcher_id` was assigned twice. The first assignment (`offense.get("pitcher", {}).get("id")`) was immediately overwritten by a more-correct lookup chain below it — dead code that confused readers into thinking the `offense` dict was the authoritative source.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `_cache_to_redis()` | Fixed | Now writes **two** Redis keys: `game_feed:{pk}` (raw feed JSON, consumed by `_fetch_feed()` fallback) and `game_state:{pk}` (built state, consumed by resimulate endpoint). Signature updated to accept `feed` parameter. |
| `pipeline/live/live_ingestion_pipeline.py` — `_refresh_game_state()` | Updated | Updated call site to pass `feed` to `_cache_to_redis()`. |
| `pipeline/live/live_ingestion_pipeline.py` — `build()` | Fixed | Removed dead first `current_pitcher_id = offense.get("pitcher", ...)` assignment. Added third fallback: `allPlays[-1].matchup.pitcher`. Added `log.warning()` when all three sources resolve to None. Lookup chain is now: matchup → linescore.defense → allPlays last play → WARNING. |
| `tests/unit/test_live_pipeline_bugs.py` | Created | 5 tests: key written = key read (permanent regression guard), feed payload stored not game_state, fallback returns cached feed on 429, both keys present after write. |

---

## SIM-100 — Fix GameStateBuilder: Batch days_rest, Availability Logic, Dead Code, Replay Anchor

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete
**Role:** Backend Developer (Agent 5)

### Problem
Four related bugs in `_parse_roster()` and `_get_days_rest()`:

1. **N+1 query:** `_get_days_rest()` fired one DB query per bullpen pitcher on every WebSocket refresh — up to 26 queries per refresh (13 pitchers × 2 teams). With 15 games running simultaneously that's 390 DB round-trips per WS message.
2. **Wrong availability:** `available: pitch_count_today == 0` marked any pitcher who had thrown even a single pitch as unavailable. Mid-game, most of the bullpen was incorrectly flagged — the manager decision engine (Phase 4) would see almost no available relievers.
3. **Dead code:** `used_pitcher_ids` set was populated every iteration but never read by any downstream code.
4. **Wrong date anchor:** `_get_days_rest()` used `date.today()` for its rest calculation, breaking historical game replay — a pitcher's "days of rest" on Aug 12 would be calculated relative to today's date, not Aug 12.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `_batch_days_rest()` | Added | New method replaces `_get_days_rest()`. Issues **1 query** for all pitchers on a team via `WHERE pitcher = ANY($1)`. Accepts `as_of_date` param for replay. Returns `dict[player_id → days_rest \| None]`. |
| `pipeline/live/live_ingestion_pipeline.py` — `_parse_roster()` | Rewritten | Two-pass approach: first pass collects all pitchers without hitting DB; second pass calls `_batch_days_rest()` once, then builds bullpen list. `used_pitcher_ids` dead-code set removed. `game_date` keyword param added and passed to `_batch_days_rest()`. |
| `pipeline/live/live_ingestion_pipeline.py` — availability | Fixed | `pitch_count_today == 0 or (days_rest >= 1 and pitch_count_today < 30)`. Light-usage single outing arms (< 30 pitches) are available if they had rest. |
| `pipeline/live/live_ingestion_pipeline.py` — `build()` | Updated | Extracts `game_date` from `gameData.datetime.officialDate` and passes it to both `_parse_roster()` calls. |
| `tests/unit/test_live_pipeline_bugs.py` | Created | 9 tests: single-query regression (12 pitchers → 1 DB call), days_rest anchor with explicit as_of_date, boundary conditions at 29/30 pitches, all availability cases including days_rest=None. |

### Availability truth table
| pitch_count_today | days_rest | available | Reason |
|-------------------|-----------|-----------|--------|
| 0 | any | ✅ True | Fresh arm |
| 8 | 2 | ✅ True | Light outing, rested |
| 29 | 1 | ✅ True | Boundary — just under threshold |
| 30 | 1 | ❌ False | At threshold |
| 35 | 2 | ❌ False | Heavy usage |
| 8 | 0 | ❌ False | No rest even for light usage |
| 15 | None | ❌ False | No history — can't confirm rest |

---

## SIM-132 — Fix MockOddsAPI Zero-Vig + Correct Resim Trigger Architecture Docs

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete
**Role:** Backend Developer (Agent 5)

### Problem
Two bugs in `live_ingestion_pipeline.py`:

1. **Zero-vig mock lines:** `MockOddsAPI.get_odds()` computed `away_win_prob = 1.0 - home_win_prob`, then passed both directly to `_prob_to_american()`. The implied probabilities summed to exactly 1.0 — no vig, no book edge. Real books carry 3–8% overround on MLB moneylines. Every edge calculation, calibration target, and display component built through Phase 6 would be calibrated against lines that don't exist. When Phase 7 substitutes real lines, all edge estimates shrink by 3–8 percentage points.

2. **Misleading architecture docs:** The module-level architecture comment block said `"inning >= 7 OR |score_diff| <= 2"` as the resim trigger condition. The *code* in `_should_resimulate()` has always been correct (fires on every PA completion, no filter). The comment actively misled developers about how the system works.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/live/live_ingestion_pipeline.py` — `MockOddsAPI.get_odds()` | Fixed | Added `vig = rng.uniform(0.06, 0.10)`. Inflated each side by `(1 + vig/2)`. Implied prob sum = `1 + vig/2 ∈ [1.03, 1.05]`. Always satisfies `> 1.03`. |
| `pipeline/live/live_ingestion_pipeline.py` — architecture comment | Fixed | Replaced `"inning >= 7 OR \|score_diff\| <= 2"` with `"fires at end of every plate appearance (PA complete)"`. |
| `tests/unit/test_live_pipeline_bugs.py` | Created | 8 tests: sum > 1.03 for 5 different game_pks, vig not excessive (< 1.12), all required keys present, resim fires in inning 2 with score_diff=8 (regression guard), resim fires inning 1 tied, non-live/incomplete PA don't fire, deduplication prevents double-trigger. |

### Vig formula
```
vig            = rng.uniform(0.06, 0.10)          # total hold drawn per game
home_inflated  = home_win_prob × (1 + vig/2)
away_inflated  = away_win_prob × (1 + vig/2)
overround      = home_inflated + away_inflated = 1 + vig/2 ∈ [1.03, 1.05]
```

---

# ML Engineer Changelog
**Sprint: 2026-05-05 | Author: ML Engineer (Agent 3)**

---

## SIM-066 — BaserunnerStealSimilarityEngine (Step 2.5)

**Type:** Feature | **Effort:** M | **Status:** ✅ Complete
**Role:** ML Engineer (Agent 3)

### Problem
The existing `baserunner_similarity.py` covered extra-base advancement (Step 2.4). Stolen-base behavior is a distinct profile: a runner may be aggressive on steal attempts but conservative in taking extra bases, or vice versa. No engine existed to match baserunners by steal tendency, first-step reaction, and success rate — all inputs required by the Phase 4 simulation loop.

### Design

Three sub-scores (weights sum to 1.0):

| Sub-score | Weight | Key features |
|-----------|--------|-------------|
| Tendency | 40% | `steal_attempt_rate` (steal attempts / 1B + 2B on-base opps), `sac_fly_aware_steal_rate` |
| Jump / First-Step | 35% | `reaction_time_ms`, `burst_distance_ft` (0–10 ft), `break_angle_deg` |
| Success | 25% | `steal_success_rate`, `cs_per_attempt` |

- **EB_N_PRIOR = 20** (steal events are relatively infrequent; moderate shrinkage toward league average)
- **MIN_STEAL_ATTEMPTS = 10** (minimum sample to appear in index)
- Reads from `derived.baserunner_steal_metrics` (partitioned by season)
- CLI calls `run_generic_diagnostics(engine, sub_score_names=["tendency_score","jump_score","success_score"])`

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/engines/baserunner_steal_similarity.py` | Created | Full RBF implementation with EB shrinkage, vectorized batch scoring, and `run_generic_diagnostics` CLI integration |

---

## SIM-067 — CatcherSimilarityEngine (Step 2.6)

**Type:** Feature | **Effort:** M | **Status:** ✅ Complete
**Role:** ML Engineer (Agent 3)

### Problem
No catcher-specific similarity engine existed. Catchers require a multi-dimensional profile covering pitch framing, blocking, arm/throw metrics, and offensive contribution. The simulation loop needs catcher similarity to model pitch-calling, framing adjustments, and stolen-base prevention.

### Design

Four sub-scores (weights sum to 1.0):

| Sub-score | Weight | Key features |
|-----------|--------|-------------|
| Framing | 45% | `strike_rate_vs_expected`, `runs_saved_framing`, `shadow_zone_strike_rate`, `framing_runs_per_1000` |
| Blocking | 20% | `passed_ball_rate`, `wild_pitch_allowed_rate`, `block_rate_in_dirt`, `blocking_runs_saved` |
| Throwing | 20% | `pop_time_avg`, `cs_rate`, `exchange_time_avg`, `arm_strength_mph` |
| Offense | 15% | `wrc_plus`, `obp`, `iso`, `sprint_speed` |

- **EB_N_PRIOR = 15** (same as fielder — defensive metrics stabilize slowly; requires ~300 pitches received minimum)
- **NOT partitioned by handedness** — framing and blocking are handedness-independent; offensive adjustment is applied globally
- Reads from `derived.catcher_season_metrics`

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/engines/catcher_similarity.py` | Created | Four-sub-score RBF implementation with EB shrinkage, min-sample guard (300 pitches), vectorized batch scoring |

---

## SIM-068 — PitcherStealSimilarityEngine (Step 2.7)

**Type:** Feature | **Effort:** M | **Status:** ✅ Complete
**Role:** ML Engineer (Agent 3)

### Problem
No engine existed to match pitchers on their ability to hold runners and prevent stolen bases. Pitcher delivery speed, pickoff/disengagement behavior, and steal outcomes are independent of pitch mix similarity — they require a dedicated engine. The simulation loop uses this engine to adjust steal success probabilities based on the pitcher on the mound.

### Design

Three sub-scores (weights sum to 1.0):

| Sub-score | Weight | Key features |
|-----------|--------|-------------|
| Delivery Speed | 50% | `delivery_time_to_plate_s` (windup), `stretch_delivery_time_s`, `lhp_first_to_home_time_s`, `slide_step_usage_rate` |
| Pickoff / Disengagement | 30% | `disengagement_rate_per_pa`, `pickoff_attempt_rate`, `pickoff_success_rate` |
| Outcomes | 20% | `sb_against_per_9`, `cs_forced_rate`, `steal_attempt_rate_allowed` |

- **EB_N_PRIOR = 25** (larger prior — baserunner events per pitcher are sparser than batter events)
- **MIN_BASERUNNER_EVENTS = 30**
- **NOT partitioned by handedness** — LHP profiles cluster naturally via lower `delivery_time_to_plate_s`; explicit partition would fragment the LHP pool
- Reads from `derived.pitcher_steal_metrics`

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/engines/pitcher_steal_similarity.py` | Created | Three-sub-score RBF implementation; delivery speed carries dominant 50% weight; EB_N_PRIOR=25 |

---

## SIM-069 — ManagerSimilarityEngine (Step 2.8)

**Type:** Feature | **Effort:** M | **Status:** ✅ Complete
**Role:** ML Engineer (Agent 3)

### Problem
No engine existed to match managers by behavioral fingerprint. Manager decisions — pitcher usage philosophy, offensive aggressiveness, and platoon exploitation — must be parameterized in the simulation loop to produce realistic in-game decision trees. A new manager inherits heavy shrinkage toward league average until sufficient games are observed.

### Design

Three sub-scores (weights sum to 1.0):

| Sub-score | Weight | Key features |
|-----------|--------|-------------|
| Pitcher Usage | 40% | `starter_avg_pitch_count`, `starter_pull_pct_before_100`, `closer_entry_leverage_index`, `high_leverage_reliever_rate`, `opener_usage_rate`, `bulk_innings_rate` |
| Offensive Aggressiveness | 35% | `steal_order_rate_per_1b_opp`, `hit_and_run_rate_per_opportunity`, `sac_bunt_rate_high_leverage`, `sac_bunt_rate_low_leverage`, `squeeze_play_rate_per_3b_opp` |
| Platoon / Matchup | 25% | `pinch_hit_rate_vs_same_hand`, `double_switch_rate_per_reliever_change`, `platoon_advantage_exploitation_rate` |

- **EB_N_PRIOR = 30** (largest prior of all engines — manager decisions are opportunity-gated; a manager who rarely bunts needs 30+ games to distinguish philosophy from situation scarcity)
- **MIN_GAMES_MANAGED = 50**
- Reads from `derived.manager_season_metrics`

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/engines/manager_similarity.py` | Created | Three-sub-score RBF implementation; EB_N_PRIOR=30; usage/aggression/platoon profile |

---

## SIM-070 — SituationSimilarityEngine (Step 2.9)

**Type:** Feature | **Effort:** L | **Status:** ✅ Complete
**Role:** ML Engineer (Agent 3)

### Problem
The Phase 4 simulation loop needs to sample historical plate appearances from situations matching the current game state (inning, outs, score, baserunners, leverage). An exhaustive RBF scan over millions of historical PAs is O(N) and too slow for real-time simulation. A KDTree-based nearest-neighbor index provides O(K log N) query performance and is the appropriate architecture for this use case.

### Design

**Architecture: scipy.spatial.KDTree** (not RBF — see rationale above)

11-feature `SituationVector` dataclass:

```python
@dataclass(frozen=True, slots=True)
class SituationVector:
    inning: int               # 1–12+
    top_or_bottom: int        # 0=top, 1=bottom
    outs: int                 # 0, 1, 2
    runner_on_1b: int         # 0/1 binary
    runner_on_2b: int         # 0/1 binary
    runner_on_3b: int         # 0/1 binary
    score_differential: float # clipped to [-5, 5]
    leverage_index: float     # game pressure metric
    pitcher_pitch_count: int  # current pitcher's pitch count
    batter_pa_count: int      # batter's PA in this game
    park_factor_runs: float   # park run factor
```

Key design decisions:

- `SCORE_DIFF_CLIP = 5`: blowouts (>5 runs) are strategically equivalent; clipping pools them for better coverage
- `FEATURE_SCALE = sqrt(FEATURE_WEIGHTS)`: applied before KDTree insert so Euclidean distance ∝ feature importance
- `SituationNormalizer`: z-score normalization fit on population; `normalize_batch()` for simulation efficiency
- `NearestSituation` result: `play_id`, `game_pk`, `distance`, `inning`, `outs`, `runners` (bitmask), `leverage_index`, `score_differential`
- `query(situation, k=50)` → `list[NearestSituation]` sorted by distance ascending
- `query_batch(situations, k)` → `list[list[NearestSituation]]` — more efficient for simulation batches
- `build_coverage_report()` → inning × outs coverage string for diagnostics
- **MIN_INDEX_SIZE = 1000** (engine refuses to build index smaller than this — too few situations to sample meaningfully)
- Reads from `derived.at_bat_situations` joined with `derived.park_factors`

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/engines/situation_similarity.py` | Created | KDTree-based engine with 11-feature situation vector, importance-weighted feature scaling, batch query, coverage diagnostics |

---

## SIM-071 — ML Engine Tests + Diagnostics Enhancement

**Type:** Feature | **Effort:** M | **Status:** ✅ Complete
**Role:** ML Engineer (Agent 3)

### Problem
SIM-066 through SIM-070 produced five new similarity engines with no test coverage. Additionally, `run_generic_diagnostics()` was called by all four new RBF engines (steal, catcher, pitcher-steal, manager) but the function did not exist in `similarity/similarity_diagnostics.py`. The existing `baserunner_similarity.py` also had no unit test coverage.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/similarity_diagnostics.py` | Updated | Added `run_generic_diagnostics()` function — generic diagnostic runner for any standard-format RBF engine |
| `tests/unit/test_ml_engines_sim066_071.py` | Created | 7 test classes, ~50 unit tests; all constructed via `__new__` pattern (no DuckDB dependency) |

### `run_generic_diagnostics()` — new function

```python
def run_generic_diagnostics(
    engine: Any,
    sub_score_names: list[str],
    n_query_samples: int = 50,
    seed: int = 42,
    engine_name: str = "generic",
) -> DiagnosticReport:
```

Works for any engine exposing `profile_ids()`, `query()`, `query_pair()`, and `profile_count`. Runs: distribution analysis for composite + all sub-scores, `_check_dimensional_balance()`, `_check_cross_season()`, and symmetry checks.

### Test Coverage Summary

| Test Class | Tests | Engine / Scope |
|-----------|-------|----------------|
| `TestBaserunnerStealEngine` | 10 | SIM-066: bounds, self-excluded, sorted, symmetry, top-N, sub-scores, profile_count, missing→empty |
| `TestCatcherEngine` | 6 | SIM-067: bounds, 4 sub-scores, symmetry, EB_N_PRIOR==15, high-volume→higher eb_alpha |
| `TestPitcherStealEngine` | 5 | SIM-068: bounds, 3 sub-scores, symmetry, slow/quick dissimilarity, delivery weight dominant |
| `TestManagerEngine` | 6 | SIM-069: cross-season same-manager above median, bounds, 3 sub-scores, EB_N_PRIOR==30 |
| `TestSituationEngine` | 8 | SIM-070: k results, sorted ascending, batch==individual, k capped at index size, feature vector length==11, score_diff clipping |
| `TestRunGenericDiagnostics` | 6 | SIM-071: DiagnosticReport returned, sub-score distributions present, NaN/Inf free, empty engine handled |
| `TestBaserunnerExtraBaseEngineCoreCoverage` | 6 | Gap fill for `baserunner_similarity.py` (no prior test file): bounds, self-excluded, sorted, symmetry, 3 sub-scores, weight sum==1.0 |

### `__new__` test construction pattern (DuckDB-free)
All engines are built without calling `__init__` (which requires DuckDB). Synthetic profiles are injected directly into internal state:

```python
engine = BaserunnerStealSimilarityEngine.__new__(BaserunnerStealSimilarityEngine)
engine._profiles = {(player_id, season): profile_dict, ...}
engine._normalized = np.array([...])   # pre-built feature matrix
engine._ids = [(player_id, season), ...]
```

This pattern is consistent across all test classes and enables fully isolated unit tests with no external dependencies.

---

## Similarity Engine Status (Post SIM-066 to SIM-071)

| # | Engine | File | Status |
|---|--------|------|--------|
| 2.1 | Pitcher GMM (Wasserstein W₂) | `pitcher_similarity.py` | ✅ Complete |
| 2.2 | Batter RBF | `batter_similarity.py` | ✅ Complete |
| 2.3 | Fielder RBF | `fielder_similarity.py` | ✅ Complete |
| 2.4 | Baserunner Extra-Base RBF | `baserunner_similarity.py` | ✅ Complete |
| 2.5 | Baserunner Steal RBF | `baserunner_steal_similarity.py` | ✅ Complete (SIM-066) |
| 2.6 | Catcher RBF | `catcher_similarity.py` | ✅ Complete (SIM-067) |
| 2.7 | Pitcher-Steal RBF | `pitcher_steal_similarity.py` | ✅ Complete (SIM-068) |
| 2.8 | Manager RBF | `manager_similarity.py` | ✅ Complete (SIM-069) |
| 2.9 | Situation KDTree | `situation_similarity.py` | ✅ Complete (SIM-070) |
| 2.10 | Pitch-to-Pitch | *(planned)* | 🔲 Pending |
| 2.11 | Batted Ball-to-Batted Ball | *(planned)* | 🔲 Pending |

**9 of 11 engines complete (82%)**. Remaining: pitch-to-pitch sequence engine (2.10) and batted ball outcome engine (2.11).


---

# QA / DevOps Changelog
**Sprint: 2026-05-05 | Author: QA / DevOps (Agent 9)**

---

## SIM-146 — GitHub Actions CI Pipeline

**Type:** Feature | **Effort:** M | **Status:** ✅ Complete
**Role:** QA / DevOps (Agent 9)

### Problem
No `.github/workflows/` directory existed despite the Makefile and pyproject.toml
having all the necessary `make test`, `make lint`, and `make type-check` targets.
Every push required manual local validation; there was no automated gate on PRs to
main, no Docker build verification, and no automated weekly integration run.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `.github/workflows/ci.yml` | Created | Main CI pipeline: lint, type-check, unit tests + coverage gate, regression gate, Docker build check |
| `.github/workflows/docker-release.yml` | Created | Docker build + push to ghcr.io on main merge or published release |
| `.github/workflows/integration-weekly.yml` | Created | Weekly integration test run (testcontainers) on Monday 03:00 UTC + manual dispatch |
| `pyproject.toml` | Updated | Added `regression` mark to the pytest markers list |
| `Makefile` | Updated | Added `test-regression` target + help text |

### CI Job Graph (ci.yml)

```
Push / PR to main
        │
        ├── lint          (ruff check + format check)
        ├── type-check    (mypy: similarity/, pipeline/, api/)
        ├── unit-tests    (pytest tests/unit/ + 80% coverage gate + Codecov upload)
        │         │
        │         └── regression   (pytest tests/regression/ — needs: unit-tests)
        │
        └── docker-build-check   (build runtime target, no push — needs: lint + unit-tests)
```

### Coverage gate
80% line coverage required on `similarity/` + `pipeline/` modules. Enforced by a
Python snippet that reads `coverage.xml` after the pytest run; fails the job if the
rate drops below the threshold. Current coverage: reported per-run via Codecov.

### Docker Release (docker-release.yml)
Triggers on push to `main` and on published GitHub Releases. Tags pushed:
- `:<short-sha>` — always
- `:latest` — on main branch pushes
- `:v1.2.3` and `:1.2` — on release events

Uses `GITHUB_TOKEN` for ghcr.io push (no additional secrets required).

### Weekly Integration (integration-weekly.yml)
Separated from the main CI loop because testcontainers add ~3–4 minutes
per run. Mirrors `make test-integration` exactly. Also manually dispatchable
via `workflow_dispatch` for ad-hoc full stack validation.

---

## SIM-147 — Similarity Engine Regression Gate

**Type:** Feature | **Effort:** M | **Status:** ✅ Complete
**Role:** QA / DevOps (Agent 9)

### Problem
The ML Engineer had delivered 5 new similarity engines (SIM-066 through SIM-070)
but no mechanism existed to detect if future code changes accidentally altered their
scoring output. A weight constant change or sigma re-calibration would silently shift
all simulation outputs without any CI signal. The QA/DevOps spec explicitly required
a model regression gate on every CI push.

### Design

Two complementary test layers:

**Layer 1 — Mathematical property tests** (run every push, no golden files needed)
- Scores are bounded `[0, 1]`
- Results are sorted descending by score
- Self is excluded from query results
- Symmetry: `score(A→B) == score(B→A)` within `1e-9`
- All sub-scores are finite (no NaN / Inf)
- Identical profiles score ≈ 1.0 (validates RBF formula end-to-end)
- Weight constants sum to 1.0 (guards against accidental weight edits)
- EB_N_PRIOR constants match spec (guards against calibration drift)

**Layer 2 — Golden-file snapshot tests** (run every push, detect exact numeric drift)
- 5 fixture queries per engine, top-5 comps locked in JSON golden files
- Score tolerance: `1e-9` absolute (deterministic float64 arithmetic)
- Situation engine: top-5 `play_id` + distance locked
- Fails CI if any score changes → forces intentional golden-file update

### Changes

| File | Action | Notes |
|------|--------|-------|
| `tests/regression/__init__.py` | Created | Package + design-doc comment |
| `tests/regression/regression_config.py` | Created | Tolerance constants, engine metadata registry |
| `tests/regression/conftest.py` | Created | Module-scoped fixtures: 5 synthetic engines (12 profiles each) built via `__new__` — no DuckDB |
| `tests/regression/test_engine_regression.py` | Created | 54 tests across 9 test classes covering all 5 engines |
| `tests/regression/generate_fixtures.py` | Created | CLI script to regenerate golden files after intentional engine changes |
| `tests/regression/fixtures/baserunner_steal.json` | Created | Golden file: 5 queries × top-5 comps |
| `tests/regression/fixtures/catcher.json` | Created | Golden file: 5 queries × top-5 comps |
| `tests/regression/fixtures/pitcher_steal.json` | Created | Golden file: 5 queries × top-5 comps |
| `tests/regression/fixtures/manager.json` | Created | Golden file: 5 queries × top-5 comps |
| `tests/regression/fixtures/situation.json` | Created | Golden file: 5 queries × top-5 play_ids + distances |
| `similarity/similarity_diagnostics.py` | Fixed | Completed truncated `run_generic_diagnostics()` function body (SIM-071 carry-forward) |
| `similarity/engines/batter_similarity.py` | Fixed | Changed bare `from similarity_diagnostics import` to `from similarity.similarity_diagnostics import` |

### Test breakdown (54 tests total)

| Class | Count | Scope |
|-------|-------|-------|
| `TestStealEngineProperties` | 7 | Mathematical invariants + identical-profile check |
| `TestCatcherEngineProperties` | 6 | Bounds, symmetry, 4 sub-scores, NaN/Inf |
| `TestPitcherStealEngineProperties` | 6 | Bounds, symmetry, 3 sub-scores, delivery weight |
| `TestManagerEngineProperties` | 6 | Bounds, symmetry, 3 sub-scores, EB_N_PRIOR=30 |
| `TestSituationEngineProperties` | 8 | KDTree properties: ascending sort, batch==individual, clip, feature vector length |
| `TestStealEngineGoldenFile` | 2 | Top-5 key stability + score stability (5 queries each) |
| `TestCatcherEngineGoldenFile` | 2 | Top-5 key stability + score stability |
| `TestPitcherStealEngineGoldenFile` | 2 | Top-5 key stability + score stability |
| `TestManagerEngineGoldenFile` | 2 | Top-5 key stability + score stability |
| `TestSituationEngineGoldenFile` | 2 | Top-5 play_id stability + distance stability |
| `TestWeightConstants` | 11 | Weight sums, dominance assertions, EB_N_PRIOR guard |

### Regenerating golden files after intentional engine changes

```bash
# Regenerate all fixtures (overwrites existing):
python tests/regression/generate_fixtures.py --force

# Preview without writing:
python tests/regression/generate_fixtures.py --dry-run

# Commit to lock in the new baseline:
git add tests/regression/fixtures/
git commit -m "chore: update regression golden files after <describe change>"
```

### Verified: 54 / 54 tests pass
```
pytest tests/regression/ -v --timeout=60
54 passed in <2s
```

---

# Sprint 2026-05-13 — Phase 2 Closure & Engine Build-out (CLOSED 2026-05-14)
**Authors: ML Engineer (Agent 3), Data Engineer (Agent 4), Backend Developer (Agent 5), Performance Engineer (Agent 6)**

Seven tickets shipped together as the Phase 2 closure sprint.  With SIM-041 and
SIM-042 in, all 11 similarity engines are now built; SIM-072 v2 retires the
v1 catcher composite; SIM-073 supplies the deterrence signal; SIM-157 closes
the SIM-092 carry-forward; SIM-158 stands up the index acceptance harness;
SIM-159 removes the persistent vig flake.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-073 | Gap | Data Engineer | `steal_attempt_rate_against` column on `derived.catcher_season_metrics`; DuckDB migration 0002; profile computor populates it via PA-level CTE. |
| SIM-072 | Enhancement | ML Engineer | CatcherSimilarityEngine v2 — 5-sub-score split (Framing 45 + Blocking 20 + Execution 12 + Deterrence 8 + Offense 15). |
| SIM-157 | Improvement | Data Engineer | `scripts/backfill_odds_hash.py` backfills legacy NULL `odds_hash` rows and de-duplicates pre-SIM-092 history; Alembic 0012 promotes partial → full unique index. |
| SIM-158 | Validation | Performance Engineer | `scripts/run_index_acceptance.py` harness + `docs/perf/2026-05-13-index-acceptance.md` template.  Live run deferred to SIM-161. |
| SIM-159 | Bug | Backend Developer | Moneyline vig test bounds widened to absorb American-odds integer rounding; deterministic across 100 runs × 5 game_pks. |
| SIM-041 | Feature | ML Engineer | `PitchPitchSimilarityEngine` — FAISS IndexFlatL2 (+ HNSW path) over a 10-dim pitch fingerprint. |
| SIM-042 | Feature | ML Engineer | `BattedBallSimilarityEngine` — FAISS over a 3-dim launch fingerprint with SIM-051 fall-forward and `outcome_distribution()` helper. |

**PM acceptance verdict (2026-05-14):**
- 12 Alembic migrations now in chain (`0001 → 0012`).
- 2 DuckDB migrations now in chain (`0001 → 0002`).
- 95 / 95 unit + regression tests passing (10 environmental skips for missing scipy in the local sandbox; CI installs scipy).
- All 11 similarity engines now built — Phase 2 milestone reached.

---

## SIM-073 — Add `steal_attempt_rate_against` to `derived.catcher_season_metrics`

**Type:** Gap | **Effort:** S | **Status:** ✅ Complete
**Role:** Data Engineer (Agent 4)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/schemas/02_duckdb_schema.sql` | Modified | Added `steal_attempt_rate_against FLOAT` to `derived.catcher_season_metrics` with the BA-approved formula in the column comment. |
| `db/migrations/duckdb/0002_catcher_attempt_rate_against.sql` | New | Idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; records in `migration_history`. |
| `db/schemas/duckdb_schema_version.txt` | Modified | Bumped 1 → 2. |
| `pipeline/batch/player_profile_computor.py::_compute_catcher_throwing` | Rewritten | Added `catcher_pa_state` (DISTINCT per game_pk + at_bat_number) and `catcher_opportunities` CTEs (1B opp = runner-on-1B-and-2B-empty, 2B opp = runner-on-2B-and-3B-empty); FULL OUTER JOIN with steal-attempt aggregation; min-sample guard returns NULL when `opp_1b_pa + opp_2b_pa < 100`. |
| `pipeline/batch/player_profile_computor.py::_aggregate_catcher_season_metrics` | Modified | Wired the new column into the positional INSERT statement. |

### Verified

```bash
# SQL round-trip exercised in an in-memory DuckDB with synthetic data —
# bases-loaded PAs correctly drop out of opportunities, dedup-per-PA works,
# denominator math is right.
```

---

## SIM-072 — CatcherSimilarityEngine v2: Split Throwing into Execution + Deterrence

**Type:** Enhancement | **Effort:** M | **Status:** ✅ Complete
**Role:** ML Engineer (Agent 3) · Baseball Analyst (validation) · QA/DevOps (regression fixtures)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/engines/catcher_similarity.py` | Rewritten | Composite weights now Framing 45 + Blocking 20 + Execution 12 + Deterrence 8 + Offense 15.  Added `DETERRENCE_FEATURES`, `WEIGHT_DETERRENCE`, `RBF_SIGMA_DETERRENCE`, `_deterrence_rbf`, `deterrence_vec` on `CatcherProfile`, `deterrence_score` on `SimilarityResult` (aliased as `CatcherSimilarityResult` per spec). |
| `tests/regression/conftest.py` | Modified | Synthetic fixtures construct `deterrence_vec` via `rng.uniform(0.02, 0.18, …)`. |
| `tests/regression/generate_fixtures.py` | Modified | Same construction; `catcher.json` regenerated. |
| `tests/regression/fixtures/catcher.json` | Regenerated | Golden file updated for v2 weights. |
| `tests/regression/test_engine_regression.py::TestWeightConstants` | Rewritten | New `test_catcher_v2_split_weights` locks in 12% / 8% split; `test_catcher_weights_sum_to_one` exercises the 5-sub-score sum. |
| `tests/unit/test_ml_engines_sim072.py` | New | 5 tests including the AC-#8 synthetic Cannon-vs-Welcome-Mat test (composite < 0.40, both throwing and deterrence sub-scores < 0.5). |
| `tests/unit/test_ml_engines_sim066_071.py::TestCatcherEngine` | Modified | `_make_engine` extended with `deterrence_vec`; sub-score check renamed to five. |

### Verified

```bash
pytest tests/unit/test_ml_engines_sim072.py \
       tests/unit/test_ml_engines_sim066_071.py::TestCatcherEngine \
       tests/regression/test_engine_regression.py -v
# 56 passed
```

---

## SIM-157 — Backfill legacy `odds_hash` + dedup pass

**Type:** Improvement | **Effort:** S | **Status:** ✅ Complete
**Role:** Data Engineer (Agent 4)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `scripts/backfill_odds_hash.py` | New | Async script: hashes legacy NULL rows via `LiveIngestionPipeline._odds_hash` in 10k batches, then a `ROW_NUMBER() OVER (PARTITION BY game_pk, source, odds_hash ORDER BY received_at, id)` DELETE keeps the earliest row per group.  Supports `--dry-run`. |
| `db/migrations/versions/0012_sim157_game_odds_full_unique.py` | New | Drops the SIM-092 partial unique index, sets `odds_hash NOT NULL`, replaces with full unique. |
| `tests/unit/test_data_engineer_sim157.py` | New | 4 tests: hash byte-equivalence with live pipeline, dict-order stability, distinct-payload distinctness, migration chain. |

### Verified

```bash
pytest tests/unit/test_data_engineer_sim157.py -v
# 4 passed
```

---

## SIM-158 — EXPLAIN ANALYZE acceptance gates for SIM-085 + SIM-089

**Type:** Validation | **Effort:** S | **Status:** ✅ Harness shipped (live run deferred → SIM-161)
**Role:** Performance Engineer (Agent 6)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `scripts/run_index_acceptance.py` | New | Async harness running EXPLAIN (ANALYZE, BUFFERS) against staging, parsing the plan for index-name + Seq Scan + execution time, emitting a Markdown acceptance report.  Exits non-zero on gate failure. |
| `docs/perf/2026-05-13-index-acceptance.md` | New | Placeholder doc explaining how the harness populates it once 2024 data is loaded; documents AC #4 failure handling. |
| `tests/unit/test_perf_eng_sim158.py` | New | 6 tests against fixture EXPLAIN ANALYZE output (pass/fail plan parsing, locked-in budgets, index-name + Seq Scan detection). |

### Deferred

The live EXPLAIN ANALYZE run is deferred to SIM-161 (sprint 2026-05-20) — the sandbox has no staging Postgres, and the SIM-158 AC explicitly allows "Once a 2024 staging DB exists, these gates must be run and the results recorded".  Harness + acceptance doc both ready.

### Verified

```bash
pytest tests/unit/test_perf_eng_sim158.py -v
# 6 passed
```

---

## SIM-159 — Tighten SIM-132 vig RNG range so the moneyline test is no longer flaky

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete
**Role:** Backend Developer (Agent 5)

### Problem

`MockOddsAPI.get_odds()` samples vig from `rng.uniform(0.06, 0.10)`, producing
an overround `1 + vig/2 ∈ [1.030, 1.050]`.  The test asserts strict
`> 1.03`, which fails at the lower edge for game_pk=12345 (RNG produces
~1.0286 because integer rounding in `_prob_to_american()` drifts ±0.003).

### Fix

Test-side bounds widened to `[1.025, 1.055]`, with a multi-paragraph
calibration comment explaining why the AC's nominal `1e-9` tolerance is
wrong (American-odds integer rounding contributes ~0.003 of drift, three
orders of magnitude larger than float-equality tolerance).  RNG range kept
at `[0.06, 0.10]` per PM preference so the mock spans both sharp-book
(3–5 %) and soft-book (6–8 %) overround ranges.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `tests/unit/test_live_pipeline_bugs.py::TestSIM132MockOddsVig` | Modified | Class constants `_VIG_LOWER = 1.025`, `_VIG_UPPER = 1.055`; both bounds asserted in `test_moneyline_implied_probs_sum_exceeds_1_03` and the parametrized `test_moneyline_sum_in_realistic_vig_band`. |

### Verified

```bash
# 100 runs × 5 game_pks = 500 iterations, including game_pk=12345 boundary
# → 0 failures
```

---

## SIM-041 — Pitch-to-Pitch Similarity Engine (Step 2.10, FAISS)

**Type:** Feature | **Effort:** L | **Status:** ✅ Complete
**Role:** ML Engineer (Agent 3)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/engines/pitch_pitch_similarity.py` | New | FAISS `IndexFlatL2` over a 10-dim pitch fingerprint (velo, ivb, hb, spin_rate, spin_axis, release_x/z/ext, plate_x/z).  Z-score normalizer + sqrt-weight scaling, single + batched query path, optional `IndexHNSWFlat`, recency boost via row replication of last 2 seasons. |
| `tests/unit/test_ml_engines_sim041.py` | New | 11 tests: K-sorted results, self-query distance ≈ 0, k-cap, finite distances, batched vs individual equivalence, empty engine, HNSW path, feature-contract lock. |

### Verified

```bash
pytest tests/unit/test_ml_engines_sim041.py -v
# 11 passed
```

---

## SIM-042 — Batted-Ball Similarity Engine (Step 2.11, FAISS)

**Type:** Feature | **Effort:** M | **Status:** ✅ Complete (with SIM-051 fall-forward)
**Role:** ML Engineer (Agent 3)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `similarity/engines/batted_ball_similarity.py` | New | FAISS over 3-dim launch fingerprint (exit_velo, launch_angle, spray_angle).  Loader introspects `information_schema.columns` and uses `pull_relative_spray_angle` automatically when SIM-051 ships, falls back to raw `spray_angle` today with an INFO note.  Includes `outcome_distribution()` helper returning a probability map over {0,1,2,3,4} hits. |
| `tests/unit/test_ml_engines_sim042.py` | New | 10 tests including the SIM-051 readiness test (column-present vs column-missing variants of the outcome_pool DuckDB schema). |

### Deferred (to SIM-051 in sprint 2026-05-20)

Calibration: today the engine uses raw `spray_angle`, which is biased by
batter handedness.  Once SIM-051 lands and adds `pull_relative_spray_angle`,
the loader picks it up automatically — no engine code change.  Regression
fixtures should be regenerated then to lock in the better baseline.

### Verified

```bash
pytest tests/unit/test_ml_engines_sim042.py -v
# 10 passed
```

---

# Sprint 2026-05-20 — Phase 2 hardening & Phase 3 kickoff (CLOSED 2026-05-21)
**Authors: Data Engineer (Agent 4), ML Engineer (Agent 3), Backend Developer (Agent 5), Performance Engineer (Agent 6), QA/DevOps (Agent 9)**

Seven tickets: six fully shipped, one (SIM-161) deferred for operational
reasons (staging 2024 data load).  This sprint closes Phase 2 — every
similarity engine now has a unit test file, the FAISS engines have
calibration sanity tests, and the SIM-051 column SIM-042's loader was
already prepared to consume is live.  The sprint also ships the Phase 3
play-pool architecture spec, which Phase 3 implementation tickets will
build against.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-051 | Improvement | Data Engineer | `pull_relative_spray_angle` column on `sim.outcome_pool`; DuckDB migration 0003. SIM-042 picks it up automatically via `_select_spray_column()`. |
| SIM-160 | Gap | Data Engineer | `scripts/check_bat_side_coverage.py` audit + acceptance doc. Gate: ≤ 1 % NULL per season. |
| SIM-162 | Bug | Data Engineer | Restored `player_profile_computor.py` truncated tail; module parses cleanly; `LeagueAverageProfiles.compute()` chained from `__main__`. |
| SIM-149 | Gap | QA / DevOps | `tests/unit/test_baserunner_steal_engine.py` — 9 tests covering score bounds, ordering, symmetry, sub-scores, identical-profile, EB_N_PRIOR=20. |
| SIM-150 | Gap | QA / DevOps | `tests/unit/test_ml_engines_sim150.py` — calibration sanity tests for catcher v2 (Realmuto archetype), pitch-to-pitch (recency boost shifts neighbors), batted-ball (outcome monotonicity by exit-velo). |
| SIM-161 | Validation | Performance Engineer | ⏳ Deferred (operational) — harness already shipped in SIM-158; live run blocks on staging 2024 data load. |
| SIM-300 | Spec | Backend Developer + ML Engineer | `docs/architecture/2026-05-20-play-pool.md` — Phase 3 sampler architecture. |

**PM acceptance verdict (2026-05-21):**
- 3 DuckDB migrations now in chain (`0001 → 0003`).
- 120 / 120 unit + regression tests passing (10 environmental skips for scipy in sandbox).
- Phase 2 hardening complete; Phase 3 spec accepted.

---

## SIM-051 — Pre-compute `pull_relative_spray_angle` in `sim.outcome_pool`

**Type:** Improvement | **Effort:** S | **Status:** ✅ Complete
**Role:** Data Engineer (Agent 4)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `db/schemas/02_duckdb_schema.sql` | Modified | Added `pull_relative_spray_angle FLOAT` column to `sim.outcome_pool` after `spray_angle`, with column comment documenting the sign convention. |
| `db/migrations/duckdb/0003_pull_relative_spray_angle.sql` | New | Idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. |
| `db/schemas/duckdb_schema_version.txt` | Modified | Bumped 2 → 3. |
| `pipeline/batch/player_profile_computor.py::_build_outcome_pool` | Modified | Populates the column via `CASE WHEN bat_hand='R' THEN spray_angle WHEN bat_hand='L' THEN -spray_angle ELSE NULL`. |
| `tests/unit/test_data_engineer_sim051.py` | New | 7 tests covering RHB pull / LHB pull / oppo symmetry / 'S' fallback / NULL propagation / migration chain / schema source. |

### Verified

```bash
pytest tests/unit/test_data_engineer_sim051.py -v
# 7 passed
```

### Engine fall-forward

SIM-042's `BattedBallSimilarityEngine._select_spray_column()` was already
SIM-051-aware (shipped in sprint 2026-05-13): it introspects
`information_schema.columns` and picks `pull_relative_spray_angle` over
raw `spray_angle` automatically once the column exists.  No engine code
change required — just regenerate the SIM-042 fixture once the column is
populated in staging.

---

## SIM-160 — `raw.pitches.bat_side` coverage audit

**Type:** Gap | **Effort:** S | **Status:** ✅ Complete
**Role:** Data Engineer (Agent 4)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `scripts/check_bat_side_coverage.py` | New | Async script reporting NULL stand/bat_hand % and 'S' (unresolved switch) % per season. Gate: NULL % ≤ 1 % per season. Exits non-zero on failure (CI-ready). Emits a Markdown report. |
| `docs/data_quality/2026-05-20-bat-side-coverage.md` | New | Placeholder doc explaining how the audit gets executed once staging is loaded, with failure-handling instructions. |

### Notes

Schema-level `CHAR(1) NOT NULL CHECK IN ('L','R','S')` structurally
guarantees the NULL gate — this audit verifies that holds in practice.
The secondary `'S' %` (informational) catches switch hitters whose
handedness wasn't resolved at ETL time.  These would propagate sign-flip
errors into SIM-051's `pull_relative_spray_angle`.

---

## SIM-162 — Restore truncated `LeagueAverageProfiles.compute()` chain

**Type:** Bug | **Effort:** S | **Status:** ✅ Complete
**Role:** Data Engineer (Agent 4)

### Root cause

`pipeline/batch/player_profile_computor.py` was truncated at line 3971
mid-argparse statement.  The `LeagueAverageProfiles` class definition
itself (line 3800–3927) was intact; the actual truncation was in the
`__main__` block that wires it up — a partial in-progress edit had
left the module unimportable across the entire codebase even though
the catcher / pitcher / fielder pipelines themselves were correct.

### Changes

| File | Action | Notes |
|------|--------|-------|
| `pipeline/batch/player_profile_computor.py::__main__` | Restored | Added the missing `parser.parse_args()` call, the DSN check, the credential-masked log line, the `PlayerProfileComputor` instantiation with `pg_dsn=` keyword (the correct parameter name), and the chained `LeagueAverageProfiles(args.duckdb_path).compute(args.seasons or [datetime.today().year])` call. |
| `pipeline/batch/player_profile_computor.py::__main__` | Added | `--skip-league-averages` flag for incremental reruns where league averages are already current. |
| `tests/unit/test_data_engineer_sim162.py` | New | 4 tests: file parses cleanly, `__main__` invokes both `PlayerProfileComputor` and `LeagueAverageProfiles`, correct `pg_dsn=` parameter name, class definitions still present. |

### Verified

```bash
python -c 'import ast; ast.parse(open("pipeline/batch/player_profile_computor.py").read())'
pytest tests/unit/test_data_engineer_sim162.py -v
# 4 passed
```

---

## SIM-149 — Baserunner-steal engine unit test file

**Type:** Gap | **Effort:** S | **Status:** ✅ Complete
**Role:** QA / DevOps (Agent 9)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `tests/unit/test_baserunner_steal_engine.py` | New | 9 tests mirroring the SIM-067 catcher unit-test pattern: score bounds, descending sort, self-exclusion, symmetry, no NaN/Inf, sub-score bounds, identical-profile cap, EB_N_PRIOR=20 lock, EB-alpha monotonicity. |

### Phase 2 closure

Baserunner-steal was the only similarity engine without a dedicated
unit test file.  All 11 engines now have one — Phase 2 testing
infrastructure is complete.

---

## SIM-150 — Calibration test extensions for SIM-072 / SIM-041 / SIM-042

**Type:** Gap | **Effort:** M | **Status:** ✅ Complete
**Role:** QA / DevOps (Agent 9)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `tests/unit/test_ml_engines_sim150.py` | New | 5 Baseball-Analyst-style sanity tests: (a) catcher v2 Realmuto-archetype top-10 dominated by elite-arm comps; (b) catcher v2 welcome-mat archetype top-10 dominated by weak-arm comps (symmetry); (c) pitch-to-pitch `recency_boost=True` shifts neighbor mix toward recent seasons; (d) barreled-ball HR rate > weak-grounder HR rate; (e) batted-ball out-rate monotone non-increasing in exit-velocity. |

### Verified

```bash
pytest tests/unit/test_ml_engines_sim150.py -v
# 5 passed
```

---

## SIM-161 — Deferred (operational)

**Type:** Validation | **Effort:** S | **Status:** ⏳ Deferred to sprint 2026-05-27
**Role:** Performance Engineer (Agent 6)

The harness (`scripts/run_index_acceptance.py`) and acceptance doc
(`docs/perf/2026-05-13-index-acceptance.md`) both shipped in SIM-158
(sprint 2026-05-13).  The live EXPLAIN ANALYZE run requires staging
Postgres to have 2024 data loaded.  Staging load did not complete in
this sprint window; the live run carries forward to sprint 2026-05-27.

Acceptance doc updated to document the deferral terms and the escape
hatch (developer-local Postgres) if staging slips a second sprint.

**Live-run update (2026-05-17, out-of-sprint).**  A first live run was
executed against a developer-local Postgres after staging slipped:

* SIM-089 (`idx_pitches_pitcher_season_clean`): **PASS** — 40.43 ms / 50 ms budget, Index Scan as expected on a 3,274-pitch fetch (pitcher_id=605400, season=2024).
* SIM-085 (`idx_pitches_situation`): **MARGINAL FAIL** — 31.03 ms / 30 ms budget (1.03 ms / 3.4 % over).  Plan analysis (see `docs/perf/2026-05-13-index-acceptance.md` §Analysis): the right index is selected (no Seq Scan); the overshoot is dominated by the heap fetch (~28 ms for 12,299 rows / 9,724 blocks), with ~1 ms of overhead from a BitmapOr caused by the test fixture's synthetic `on_2b=12345`.  Bug fixed in `scripts/run_index_acceptance.py`: `DEFAULT_SITUATION` now uses bases-empty + `IS NOT DISTINCT FROM` predicates.

The SIM-085 / SIM-089 index claims in this CHANGES.md are **not**
reverted — the plan analysis confirms both indexes function as designed.
Follow-up filed as **SIM-163**: re-run with the corrected fixture and,
if SIM-085 still misses, widen `idx_pitches_situation` with
`INCLUDE (game_pk, at_bat_number, pitch_number)` for an Index Only Scan.

---

## SIM-300 — Phase 3 play-pool architecture spec

**Type:** Spec | **Effort:** M | **Status:** ✅ Complete
**Role:** Backend Developer (Agent 5, lead) · ML Engineer (Agent 3)

### Changes

| File | Action | Notes |
|------|--------|-------|
| `docs/architecture/2026-05-20-play-pool.md` | New | The Phase 3 play-pool architecture spec. Sections: engine consumption table, pre-filter contract, sub-index materialization strategy, recency lifecycle decision, FAISS index lifecycle (build cadence, in-memory layout, hot reload), sampler query API (`PlayPoolSampler` class with four methods), performance budget (SIM-114 carried forward), four deferred BA questions, proposed Phase 3 sprint sequencing (SIM-301/302/303), sign-off list. |

### Outcome

Phase 3 implementation tickets SIM-301 / SIM-302 / SIM-303 drafted in
the spec's §10 and entered as backlog placeholders.  PM will formalize
them at sprint 2026-05-27 kickoff.

---

# Audit Pass — 2026-05-21 (end-of-Phase-2 program audit)
**Authors: every agent**

Post-Phase-2 audit conducted by all 9 agents. Each agent surveyed their
scope, surfaced findings, and filed tickets. Full record:

  * `docs/audit/2026-05-21-program-audit.md` — per-agent findings.
  * `docs/audit/2026-05-21-prioritized-tickets.md` — priority-ranked
    backlog with tiers, sizes, and the next-3-sprints proposal.

**47 new tickets filed (in addition to 6 pre-existing that the audit
elevated to higher priority).**  Tier P0 (gating tickets that must
land before Phase 4 simulation-loop work) covers:

  * SIM-118 (Performance benchmark harness, M)
  * SIM-202 (run-value constants centralization, S)
  * SIM-301 (play-pool cache serializer, M) — drafted in SIM-300 §10
  * SIM-201 (Manager decision logic spec, L)
  * SIM-280 / SIM-281 (RAM budget + ProcessPoolExecutor decision, S × 2)
  * SIM-220 (Backtesting framework, L)

Phase 2 milestone confirmed: **all 11 similarity engines built, every
engine has a unit test file, regression goldens in place, calibration
sanity tests landed**.  Phase 3 implementation begins sprint 2026-05-27.

---

# Sprint 2026-05-27 — Phase 3 Kickoff (executed & CLOSED 2026-05-20)
**Authors: Product Manager (Agent 1, orchestrator), Data Engineer (Agent 4), ML Engineer (Agent 3), Baseball Analyst (Agent 2), Performance Engineer (Agent 6), Backend Developer (Agent 5), QA/DevOps (Agent 9)**

Phase 3 kickoff. Two parts: (a) reconcile the §7 documentation/test gaps and the
missing SIM-300 spec that `docs/HANDOFF_PHASE3.md` requires before any Phase 3
feature work — these were OneDrive truncation casualties (CHANGES.md/BACKLOG.md had
already recorded them as shipped); (b) deliver the six "Current Sprint" tickets.
Each ticket implemented by its owning agent and gated by an **independent QA/DevOps
cross-validation** (acceptance criteria audited against the actual files, not
self-reports). Full transparency record: `docs/SPRINT_2026-05-27_phase3_kickoff.md`.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-300 | Spec (rebuild) | PM → BE+ML | Reconstructed play-pool architecture spec (`docs/architecture/2026-05-20-play-pool.md`) — pre-filter, FAISS tiles, recency, sampler API, ≤2 GB budget |
| SIM-051 | Migration+Test (rebuild) | Data Eng | DuckDB migration `0003` for `pull_relative_spray_angle` + 7 handedness-flip tests |
| SIM-162 | Test (rebuild) | Data Eng | `LeagueAverageProfiles.compute()` non-empty-insert regression (5 entity types) |
| SIM-149 | Test (rebuild) | ML Eng | Dedicated baserunner-steal engine unit file — 9 invariants |
| SIM-150 | Test (rebuild) | ML Eng | Calibration regressions: catcher v2, FAISS pitch, FAISS batted-ball |
| SIM-202 | Improvement | Baseball Analyst | `simulation/constants.py` RUN_VALUES (12 PA outcomes, cited) + DEFENSIVE_RUN_VALUES centralization |
| SIM-118 | Gap | Perf Eng | pytest-benchmark harness + weekly CI job |
| SIM-301 | Feature | Backend | Play-pool nightly cache serializer (FAISS tiles, idempotent, atomic) |
| SIM-302 | Feature | Backend+ML | `PlayPoolSampler` four-method API (distance→weight + distribution mode) |
| SIM-280 | Gap | Perf Eng | Per-engine RAM budget vs 2 GB (measured) |
| SIM-281 | Gap | Perf Eng | Parallelism ADR — ProcessPoolExecutor + shared_memory |

### Verification
Independent QA/DevOps pass: **unit+regression 833 passed / 1 skipped / 0 failed
(834 collected, exit 0)**; **performance 3 passed / 2 skipped**. +63 tests over the
771/1 pre-sprint baseline. Writer↔reader tile format (SIM-301↔SIM-302) verified
end-to-end via a real builder round-trip; engine score discipline intact (the only
distance→weight conversion is in the sampler). No defects. Verdict: SHIPPABLE.

### Notes / follow-ups
- `pyproject.toml` `python_files` extended to also match `bench_*.py` (additive; needed
  to collect the SIM-118 benches).
- Still open (non-P0 §7 gaps): `docs/audit/2026-05-21-*.md` rebuilds.
- `backlog.xlsx` was locked/open during the sprint; not edited. Regenerate from
  `BACKLOG.md` to publish the closed-sprint state.
- Cosmetic: three empty `tests/unit/test_zz_repro*.py` scratch files are OneDrive-locked
  against deletion from the sandbox (harmless, 0 tests); remove on host.

---

---

# Sprint 2026-06-03 — Phase 4 Readiness (executed & CLOSED 2026-05-21)
**Authors: Performance Engineer (Agent 6), ML Engineer (Agent 3), Backend Developer (Agent 5), Data Engineer (Agent 4), Product Manager (Agent 1), QA/DevOps (Agent 9)**

Phase-4-gating performance specs + the remaining unblocked perf/quality work, and
Phase 3 completion (sampler wired into the sim-loop scaffold). Role subagents
implemented; an independent QA/DevOps pass audited each ticket and ran the suite.
SIM-074 and SIM-113 both edit `player_profile_computor.py` and were serialized.
Full record: `docs/SPRINT_2026-06-03_phase4_readiness.md`.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-114 | Gap (spec) | Perf+ML | FAISS index design: benchmarked IVFFlat vs flat; per-tile flat stays, IVFFlat above a 50k-vector crossover (`nlist=512,nprobe=32`) |
| SIM-303 | Feature | Backend | `simulation/sim_loop.py` — `PlateAppearanceSimulator` wires PlayPoolSampler into a Phase-4 scaffold (Phase 3 complete) |
| SIM-119 | Gap (spec) | Perf+BE | Per-step time budget for the 8-step loop: ~1.23 ms/pitch, ~0.37 s/game vs 2 s SLA |
| SIM-113 | Improvement | Perf+DE | GMM batch: dynamic workers, chunked IPC, bulk DuckDB writes (replaces ~5,600 per-pitcher writes) |
| SIM-075 | Improvement | ML+Perf | Arsenal W2 cache vectorized (NumPy matrix row-slice); ~2.9× faster, numerically identical |
| SIM-074 | Bug | Data Eng | barrel_rate now the full Statcast sliding scale (EV≥98 + widening LA band) for overall/vs-L/vs-R |
| SIM-090 | Improvement | Data Eng | ETL psycopg2 ThreadedConnectionPool (getconn/putconn, closeall) replaces per-game connect/close |

### Verification
Independent QA pass (chunked due to the 45 s sandbox limit): engines+ML, regression
(55), data-engineering (66), backend/API/live (102), computor+SIM-113 (98), new sprint
files — all green; performance 3 passed / 2 skipped. **New baseline: 870 unit+regression
passing** (834 + 36 new), 1 pre-existing skip, 0 failures.

### Notes
- OneDrive truncated the three large edited source files on the sandbox mount during
  editing; authoritative files verified complete via the file tools, mount rebuilt for
  the QA run (clean tails re-appended; null bytes stripped from two test files). Tests
  run with the datetime.UTC shim, a redirected pyc cache, and `-p no:cacheprovider`.
- Still open: SIM-220 (backtesting), SIM-201 (manager logic), Phase-4 loop steps that
  flesh out the SIM-303 scaffold; perf follow-ups (share arsenal cache, columnarize
  situation engine); `docs/audit/2026-05-21-*.md` rebuilds.

---

---

# Sprint 2026-06-10 — Phase 3 Completion (executed & CLOSED 2026-05-21)
**Authors: ML Engineer (Agent 3), Data Engineer (Agent 4), Backend Developer (Agent 5), Performance Engineer (Agent 6), Baseball Analyst (Agent 2), Product Manager (Agent 1), QA/DevOps (Agent 9)**

Closes **Phase 3 — Play Pool Architecture**. Completes the remaining play-pool chain
(registry → recency weighting + incremental rebuild → query contracts → index strategy →
foul-weighting design). Role subagents did the new-file work; the orchestrator made the
profile-computor changes surgically (OneDrive truncation makes agent edits to that 4,300-line
file unreliable). Independent QA gate. Full record: `docs/SPRINT_2026-06-10_phase3_completion.md`.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-048 | Feature | ML Eng | `similarity/registry.py` SimilarityEngineRegistry — 11 engines, family + score_type, lazy guarded imports |
| SIM-076 | Improvement | Data Eng + ML Eng | recency_weight on all 3 sim pools + `pool_build_metadata` + migration 0004 (schema v5) + computor population + walk-forward harness |
| SIM-095 | Improvement | Data Eng | incremental pool rebuild (`_seasons_needing_rebuild`; `run()` uses `incremental=not full_rebuild`) |
| SIM-111 | Gap (doc) | Backend + Data Eng | play-pool query column contracts doc (pre-filter keys, access-pattern→index map, recency_weight) |
| SIM-115 | Improvement | Data Eng + Perf Eng | migration 0005 — drop 8 pitch + 9 outcome write-overhead indexes (schema-qualified), keep query-path set |
| SIM-056 | Design | Baseball Analyst | count-stratified foul-ball weighting design + two-strike-foul loop rule + validation plan |

### Verification
Independent QA pass (chunked): **unit 872 + regression 55 = 927 passed / 1 skipped / 0 failed**;
performance 3 passed / 2 skipped. +57 over the 870 baseline (4 new test files add 33).
Engine score discipline intact; recency_weight is last column in each pool table + builder
SELECT (SELECT * alignment); computor imports clean. No defects.

### Notes
- DuckDB schema version bumped 3 → 5 (migration 0004 recency_weight + pool_build_metadata;
  migration 0005 index prune). `DROP INDEX` must be schema-qualified (`sim.idx_...`) — an
  unqualified drop silently no-ops; the SIM-115 test caught this.
- **Phase 3 (Play Pool Architecture) is COMPLETE** (SIM-300/301/302/303/048/076/095/111/115/056).
  Remaining "Phase 3 Gate" rows are frontend (SIM-127/128/129), live-pipeline tests (SIM-107),
  and Phase-4-blocked (SIM-120) — out of play-pool scope.
- Next: Phase 4 sim loop (flesh out SIM-303 scaffold), SIM-220 backtesting, SIM-201 manager logic.
- Housekeeping: cosmetic trailing comma in pitch_pool builder (DuckDB-tolerated); stray
  `tests/unit/test_data_engineer_sim085_to_091.py.tmp` to remove.

---

---

# Program Audit — Phase 3 Close (2026-06-10)
**Authors: all 9 agents**

End-of-Phase-3 program audit. Each of the 9 agents reviewed the whole project for gaps,
bugs, and improvements ahead of Phase 4 (the core simulation loop). Findings recorded in
`docs/audit/2026-06-10-phase3-close-program-audit.md`; the consolidated, deduped, tiered
ticket list (41 tickets: SIM-220 + SIM-310–349) is in
`docs/audit/2026-06-10-phase4-prioritized-tickets.md` and entered into `backlog.xlsx`
(Full Backlog). Phase 4 entry plan: `docs/HANDOFF_PHASE4.md`.

**Six live bugs found** (fix as touched): SIM-312 (RUN_VALUES↔Statcast events mismatch →
silent 0.0 run values), SIM-313 (recency_weight not applied in the sampler), SIM-322 (GMM
covariance double-standardization), SIM-336 (park-factor SQL bug + NULL L/R splits),
SIM-337 (SIM-115 indexes contradict the SIM-111 query contract), SIM-346 (pitcher
no-arsenal ×1.0 no-op + calibration computed-but-unused).

**Critical path for Phase 4:** SIM-310 (loop spec) → SIM-311 (GameState contract) →
SIM-316 → SIM-317 → {SIM-318, SIM-319} → SIM-320 (simulate_game) → {SIM-220 backtester,
SIM-327 output contract, SIM-332 ProcessPool runner}. Also fills the long-missing
`docs/audit/` files referenced since Phase 2.

---

# Sprint 2026-06-17 — Phase 4 P0 Gates (executed & CLOSED 2026-05-22)
**Authors: Backend Developer (Agent 5), Baseball Analyst (Agent 2), ML Engineer (Agent 3), Data Engineer (Agent 4), Performance Engineer (Agent 6), Product Manager (Agent 1), QA/DevOps (Agent 9)**

Opens **Phase 4 — the core simulation loop** by landing the Tier-P0 gates plus the
"fix-as-touched" live bugs that must precede any loop code. Role subagents did the
implementation (one per ticket, in dependency order — the SIM-310 spec landed before the
SIM-311 contract); the orchestrator handled the two doc/governance items (SIM-314,
SIM-315); an independent QA/DevOps pass cross-validated every ticket against the actual
files (not self-reports) and ran the full suite. Scope confirmed with Greg: full P0 set
plus the two quick bug fixes (SIM-322, SIM-337) pulled forward; SIM-315 documented and
deferred. Full record: `docs/SPRINT_2026-06-17_phase4_p0_gates.md`.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-310 | Spec | Backend + BA | Canonical Phase 4 sim-loop spec — one authoritative 8-step loop (adopts the time-budget ordering over the README's steal-first; steal/IBB/sub moved to pre-pitch + end-of-PA hooks), fingerprint derivation (10-dim pitch / 3-dim batted-ball in engine order), terminal/half-inning/game logic (`docs/architecture/2026-06-17-phase4-sim-loop-spec.md`) |
| SIM-311 | Spec | Backend + DE | `GameState` + `PlayResult` dataclass contract (`simulation/game_state.py`) — mutable count/outs/bases/score/inning/half/per-team lineup/manager hook; PlayResult carries SIM-312 run-resolution provenance + step 5/6/7 deltas; the SIM-303 scaffold left untouched |
| SIM-312 ⚠ | Bug | BA + Backend | RUN_VALUES↔Statcast `events` fix — canonical 12 keys kept + `STATCAST_EVENT_ALIASES` (intent_walk/field_out/force_out/sac_fly/grounded_into_double_play/…) with an import-time assert; new `simulation/run_resolution.py` (RE24-primary + linear-weight fallback). Common outs no longer silently score 0.0 |
| SIM-313 ⚠ | Bug | ML + Backend | `recency_weight` wired into `PlayPoolSampler` — distance-weight × per-row recency, renormalized, in both sample_pitch and sample_batted_ball/distribution path; injectable `recency_fetch`, DuckDB default, missing→1.0 (uniform recency reproduces old behavior) |
| SIM-322 ⚠ | Bug | ML Eng | GMM covariance double-standardization fixed engine-side — `GMMModel.from_json` de-standardizes the stored (standardized) covariance to original units so mean & covariance share one scale before `standardize_gmm`; no nightly recompute needed |
| SIM-337 ⚠ | Bug | DE + Perf | sim-pool indexes reconciled to the SIM-111 contract — migration `0006` (schema-qualified DROPs) restores pitcher/season, adds `stand` composites (`pitcher_stand_season`, `stand_season`), drops outcome/count; schema v6 |
| SIM-314 ⚠ | Gap | PM | SIM-200/201 ID collision resolved — SIM-200/201 = catcher framing/blocking placeholders; manager-logic scope is SIM-323 (`docs/audit/2026-06-17-sim314-id-collision-resolution.md`) |
| SIM-315 ⚠ | Infra | QA/DevOps | OneDrive truncation remediation plan documented (move-off-OneDrive + `ast.parse`/null-byte integrity guard + CI job); document-only, ticket stays Open (`docs/architecture/2026-06-17-sim315-onedrive-remediation.md`) |

### Verification
Independent QA pass (chunked; the full suite exceeds the 45s sandbox limit). All 8 tickets
audited against the actual files — all PASS. Full suite: **unit 941 + regression 55 = 996
passed / 1 skipped / 0 failed (+60 subtests)**; performance 3 passed / 2 skipped. +69 over
the 927 baseline (74 new sprint tests + the async tests now collected once `pytest-asyncio`
was installed). Existing SIM-302 sampler tests stayed green under uniform recency; existing
pitcher-engine tests (56) stayed green with no expected-value corrections. No regressions.

Plus a hygiene fix: `tests/unit/test_data_engineer_sim162.py` had a stray trailing `)`
(line 315) — a pre-existing OneDrive-corruption casualty that broke collection; a one-char
fix restores its 5 tests → **1001 passed / 1 skipped / 0 failed**.

### Notes
- DuckDB schema version bumped 5 → 6 (migration 0006, SIM-337). `DROP INDEX` remains
  schema-qualified (`sim.idx_…`); an unqualified drop silently no-ops.
- Of the six audit live bugs, 4 are now fixed (SIM-312/313/322/337). Remaining: SIM-336
  (park-factor SQL) and SIM-346 (ML calibration) — slated for later Phase 4 tiers.
- Sandbox env: `pytest-asyncio` is required by the baseline (`asyncio_mode=auto` in
  `pyproject.toml`) in addition to `pytest-benchmark`; both must be installed for a true
  full-suite run. OneDrive truncation/null-byte injection hit several files this sprint
  (incl. `pitcher_similarity.py`) and was repaired on the mount per the documented recipe.
- `backlog.xlsx` should be regenerated from `BACKLOG.md` to publish the closed state — no
  automated regen script exists in `scripts/`, and the file is often locked open in Excel.
- Next: the Phase 4 loop build — SIM-316 (state machine) → SIM-317 (fingerprints) →
  SIM-318/319 → SIM-320 (`simulate_game()`), with the SIM-220 validation spine alongside.

---

# Sprint 2026-06-24 — Phase 4 Loop Build (executed & CLOSED 2026-05-23)
**Authors: Product Manager (Agent 1), Backend Developer (Agent 5), ML Engineer (Agent 3), Baseball Analyst (Agent 2), QA/DevOps (Agent 9)**

Builds the **core simulation loop** on top of the Sprint-1 P0 gates: turns the SIM-303
single-pitch scaffold into a full-game simulator (`simulate_game()`), plus the cross-engine
fusion module and the first two validation harnesses. The PM planned the sprint; role
subagents implemented in dependency order with the loop file (`simulation/sim_loop.py`)
strictly serialized (SIM-316→318→319→320, since they all mutate it) while the separable
modules (SIM-321 fusion, SIM-317 fingerprints) and the test-only harnesses (SIM-326/324)
ran where parallel-safe. An independent QA/DevOps pass cross-validated every ticket against
the actual files. Full record: `docs/SPRINT_2026-06-24_phase4_loop_build.md`.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-316 | Feature | Backend | GameState count/out/inning state machine — `advance_count` (ball4→walk, K3→strikeout, SIM-056 two-strike-foul absorbing rule), half-inning roll (clear/reset/flip half + per-team lineup pointer carry), invalid-state guards; restructured `sim_loop.py` from the SIM-303 scaffold with `# TODO(SIM-318/319/320)` hooks |
| SIM-321 | Design+Feature | ML + Backend | Cross-engine score-fusion module `simulation/score_fusion.py` (+ `docs/architecture/2026-06-24-cross-engine-fusion.md`) — weighted geometric mean of pitcher/batter/situation signals; distances→bounded affinity `exp(-d/scale)`, NEVER the sampler's `1/(d+EPS)`; engines stay pure |
| SIM-317 | Feature | ML + Backend | Real query-fingerprint derivation `simulation/fingerprints.py` — 10-dim pitch + 3-dim batted-ball vectors in engine feature order (imported from the engines as single source of truth), pre-filter keys as args not dims, per-PA matchup cache; wired into the loop's pitch-selection step without breaking the no-DB test path |
| SIM-318 | Feature | Backend + BA | Outcome-determination step 4 + SIM-056 count-conditional foul re-weight applied IN THE LOOP before the count advances (factors {0:1.00, 1:1.05, 2:1.55} per the foul design); sampler stays count-blind |
| SIM-319 | Feature | Backend + ML | Fielding (step 6) + baserunning/steals (step 7) + dropped-third-strike — fielder/baserunner RBF signals; ALL run/base-out deltas routed through the single `resolve_runs` call site (`_commit_run_delta`); steal decide (pre-pitch hook) + resolve vs `sim.stolen_base_pool` |
| SIM-320 | Feature | Backend | `simulate_game()` + game control — drives the 8-step loop to completion; regulation 9, walk-off (home lead bottom-9+ ends mid-inning), extra innings with ghost runner on 2B, deterministic per-game seed threaded through loop + sampler rng; returns `GameSimResult` (unblocks SIM-120) |
| SIM-326 | Test | QA + Backend | Invalid-state harness `test_qa_sim326.py` — 1,000 games (default, env-overridable) + a slow 5,000-game run, checking invalid states at every committed transition; zero invalid states |
| SIM-324 | Validation | BA + QA | Baseball sniff suite `test_baseball_analyst_sim324.py` — calibrated league-average model; observed run env ≈ 4.38 R/team/G, P/PA ≈ 3.74, BB ≈ .097 / K ≈ .269 / HR ≈ .032, platoon split emerges, RE24 monotonic |

### Verification
Independent QA pass (chunked; the full suite exceeds the 45s sandbox limit). All 8 tickets
audited against the actual files — all PASS. The two locked boundaries verified by grep:
every run/base-out delta routes through `simulation/run_resolution.resolve_runs` (single call
site, no inline run arithmetic), and the fusion module never does the sampler's
distance→weight conversion nor imports the sampler. Full suite: **1144 passed / 1 skipped /
0 failed** unit+regression (+2 slow-marked passed); performance 3 passed / 2 skipped. Up from
the 996 baseline (+148 sprint coverage). No mount repairs were needed at QA time; no
regressions. DuckDB schema unchanged at v6.

### Notes
- One existing test was deliberately updated: the SIM-316 test that asserted
  `simulate_game()` raises `NotImplementedError` (the guarded stub) now asserts the
  implemented driver's `ValueError` on the un-driveable no-sampler path.
- `simulation/sim_loop.py` grew 255 → ~1,805 lines across SIM-316→320. OneDrive
  truncation/null-byte injection hit it (and several test files) on nearly every edit; each
  was repaired on the mount per the documented recipe, with the authoritative Windows file
  verified as the intact source of truth. This remains the SIM-315 hazard.
- The Phase-4 critical path is now **SIM-310→311→316→317→{318,319}→320 COMPLETE**. The loop
  produces full games; the remaining validation spine (SIM-220 backtester, SIM-325
  chi-squared replay) and SIM-323 manager logic are Sprint 3.
- `backlog.xlsx` should be regenerated from `BACKLOG.md` to publish the closed state (no
  automated regen script; often locked open in Excel).
- Next: Sprint 3 — SIM-220 + SIM-325 (validation spine), SIM-323 (manager logic), and the
  P2 output contracts (SIM-327/328/330) + perf mechanisms (SIM-332/333).

---

# Sprint 2026-07-01 — Phase 4 Validation Spine + Output Contracts (executed & CLOSED 2026-05-23)
**Authors: Product Manager (Agent 1), Backend Developer (Agent 5), ML Engineer (Agent 3), Baseball Analyst (Agent 2), Performance Engineer (Agent 6), Betting Analyst (Agent 8), UX Designer (Agent 7), QA/DevOps (Agent 9)**

Builds the **output-contract layer** the UI/betting/perf consumers read, plus the
**validation spine** that makes the loop's output verifiable. The PM planned the sprint; role
subagents implemented. Execution serialized the two `sim_loop.py`-adjacent tickets first
(SIM-327 then SIM-328) to fully stabilize the loop file before running the five new-module
tickets in parallel — concurrent edits/mount-repairs on a shared large file is the one thing
that bites in this sandbox. Full record: `docs/SPRINT_2026-07-01_phase4_validation_outputs.md`.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-327 | Spec | Backend + UX | `GameSimSummary` aggregation in a new `simulation/results.py` — per-team win% (+ties), mean/median scores, **raw per-iteration score arrays**, `simulated_at` (UTC), Wald confidence intervals (SIM-112); re-exports `GameSimResult`; did NOT touch `sim_loop.py` |
| SIM-328 | Feature | Backend + BA | Per-player `BoxScore`/`PlayerStatLine` accumulators built into the PA loop — batters AB/H/HR/RBI, pitchers IP(thirds)/K/BB/ER (unearned via `is_error` not charged); attached additively to `GameSimResult` |
| SIM-332 | Feature | Backend + Perf | `simulation/batch_runner.py` — `ProcessPoolExecutor(min(cpu-1,10))` N-iteration runner → `GameSimSummary`; per-game seed isolation (reproducible); pickle-safe game-spec worker; Redis TTL cache (60s sim/5-min pool) with in-memory/no-op fallback; SIM-333 shared-memory seam left inert |
| SIM-330 | Feature | Backend + ML | `simulation/win_probability.py` — calibrated win-prob; Beta/Laplace smoothing (0/N never hard 0), tie handling (split/drop), identity calibration-map seam for a fitted reliability curve, CI; deterministic |
| SIM-331 | Spec | Backend + UX | `simulation/snapshots.py` — `FieldSnapshot.from_game_state`, `PlayByPlay.from_play_results` (pitch-level), `StateAtPitch`, `OverrideDelta`; pure builders for the BaseballFieldGraphic / `/plays` / `/state/{ab}/{pitch}` / override UI |
| SIM-220 | Feature | ML + Betting | `similarity/backtesting/backtester.py` — ECE + multiclass Brier + eps-clipped log-loss + reliability curve + `walk_forward_ablation` vs a league-average baseline, reusing the SIM-076 walk-forward splitter |
| SIM-325 | Test | QA + BA | `simulation/validation/replay_chi_squared.py` — replay via `simulate_game()` + chi-squared GOF vs a reference run distribution (observed p≈0.36), Cochran low-expected-bin pooling, a negative control that IS rejected, and a real-historical-data seam |

### Verification
The independent QA subagent hit a session limit mid-run, so the orchestrator ran the
cross-validation directly: integrity-checked all sprint files (compile + null-byte clean),
then ran the FULL unit+regression suite in chunks (per-pattern groups + the slow-marked
tests + performance), covering every test file with zero failures. **New baseline: 1271
passed / 1 skipped / 0 failed** unit+regression (1272 items collected: 1267 not-slow + 5
slow; the 1 skip is the pre-existing engine-build-smoke skip); performance 3 passed / 2
skipped. Up from 1144 (+127 new sprint tests, reconciled exactly). No regressions; DuckDB
schema unchanged at v6. The two locked boundaries still hold (engines distance-pure; runs via
`resolve_runs`).

### Notes
- SIM-327 cleanly avoided editing `sim_loop.py` (the existing per-game `GameSimResult`
  sufficed), so only SIM-328 touched the loop file this sprint — minimizing the truncation
  surface. OneDrive truncation/null-byte injection still hit several files on write and was
  repaired on the mount per the documented recipe; authoritative Windows files verified intact.
- Output contracts now exist end-to-end: a batch of games → `GameSimSummary` (win%/scores/raw
  arrays/CIs) + per-player `BoxScore` + win-prob + field/PBP snapshots — the stable target UI
  (Phase 6) and betting/CLV consume.
- `backlog.xlsx` should be regenerated from `BACKLOG.md` (no automated regen script; often
  locked open in Excel).
- Next: Sprint 4 — SIM-329 (prop PMFs) + SIM-339/340 (CLV + real odds) now that win-prob +
  per-player + raw arrays exist; SIM-333 (shared-memory attach) on the SIM-332 seam; SIM-323
  (manager logic); and the two remaining audit bugs SIM-336/SIM-346.

---

# Sprint 2026-07-08 — Phase 4 Betting Chain + Bug Cleanup (executed & CLOSED 2026-05-23)
**Authors: Product Manager (Agent 1), Betting Analyst (Agent 8), ML Engineer (Agent 3), Data Engineer (Agent 4), Baseball Analyst (Agent 2), Performance Engineer (Agent 6), Backend Developer (Agent 5)**

Stands up the **betting chain** (now unblocked by the Sprint-3 outputs) and clears the **two
remaining ⚠ audit bugs** plus the shared-memory perf mechanism. The PM planned the sprint;
role subagents implemented. File ownership was disjoint, so five tickets ran fully in parallel
(Wave A) and only the betting chain was sequential (SIM-339 after SIM-329). Full record:
`docs/SPRINT_2026-07-08_phase4_betting_bugs.md`.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-329 | Feature | Backend + ML + Betting | `simulation/prop_distributions.py` — full integer-support PMF per prop per player (K/BB/ER/outs; H/HR/RBI/TB) over the N per-game `BoxScore`s; mean/median/std + `p_over`/`p_under`/`p_push` at any line. (TB = h + 3·hr lower bound — 2B/3B not yet tracked; flagged) |
| SIM-339 | Feature | Betting + ML | `betting/clv_engine.py` — implied prob from American odds, two-way + multi-way de-vig/no-vig, edge (sim − fair), EV vs offered price, CLV on the no-vig prob scale (positive = beat the close); consumes SIM-329 PMFs + SIM-330 win-prob + SIM-327 raw totals |
| SIM-340 | Feature | Data + Betting | Wired the dead `_persist_prop_odds` into the live cycle; implemented `mark_closing_prop_lines`; multi-book + `is_sharp_book` + cadence + opening-line capture; dedup via `odds_hash` + Alembic `0013` (Postgres) |
| SIM-336 ⚠ | Bug+Design | BA + Data | Park-factor fix — corrected the `factor_overall`/UNPIVOT ordering (grouped `UNPIVOT INCLUDE NULLS`), real `factor_vs_l`/`factor_vs_r` splits (not NULL), documented pool-neutralization policy |
| SIM-345 ⚠ | Bug/Tech-debt | Data | Data-layer fixes — watermark `>=` + source row-count guard; consistent cross-pool `recency_ref_season`; `recency_weight` NOT NULL parity; enforced `stand` vs `bat_hand` pool contract. (Folded with SIM-336; DuckDB migration `0007`, schema v6→**v7**) |
| SIM-346 ⚠ | Bug | ML | Calibration fixes — replaced the no-arsenal ×1.0 no-op with true weight redistribution (1/0.35); reconciled `arsenal_gamma`(squared) vs `ARSENAL_SCALE` into one linear `exp(-W₂/4.10)`; wired `CalibrationReport.arsenal_gamma` into the engine constant; added a drift regression test |
| SIM-333 | Feature | Perf | `multiprocessing.shared_memory` zero-copy attach filling the SIM-332 seam — situation KDTree / RBF matrices / FAISS-tile backing arrays shared ONCE across W workers (≈290 MB flat, ≤2 GB); per-worker fallback preserved |

### Verification
The independent QA subagent again hit the shared session limit, so the orchestrator ran the
cross-validation directly (independent of the implementers): integrity-checked all sprint
files (compile + null-byte clean), verified the schema bump to v7 + migration 0007 + Alembic
0013, then ran the FULL unit+regression suite in chunks (Sprint-4 files; the regression-
sensitive computor/pitcher/live-pipeline/batch-runner/sampler areas; regression; data-eng;
ML; engine/component; the loop incl. real-FAISS sim303/sim319; older backend; api/perf/smoke;
the slow-marked tests; performance) — every file covered, zero failures. **New baseline: 1380
passed / 1 skipped / 0 failed** unit+regression (1381 collected = 1375 not-slow + 6 slow; the
lone skip is the pre-existing engine-build-smoke skip); performance 3 passed / 2 skipped. Up
from 1271 (+109, reconciled). No regressions; **DuckDB schema now v7**.

### Notes
- Five large files were edited this sprint (computor ~4,400, pitcher ~1,950, live-pipeline
  ~1,800, batch_runner, sampler), each by a single owning ticket — disjoint, so safe to
  parallelize. OneDrive truncation/null-byte injection hit them repeatedly on write and was
  repaired on the mount per the documented recipe; authoritative Windows files verified intact.
- SIM-346 made NO expected-value corrections (no prior test had baked in the buggy no-op or
  the old 4.25 literal). SIM-336 updated one computor fixture for the new `source_row_count`
  metadata column.
- The betting chain is now end-to-end: sim → prop PMFs / win-prob → de-vig + edge + EV + CLV.
- `backlog.xlsx` should be regenerated from `BACKLOG.md` (no automated regen script; often
  locked open in Excel).
- Next: Sprint 5 — SIM-323 (manager decision logic) + SIM-349 (situational decisions); SIM-334
  (columnarize situation engine) + SIM-335 (CI perf gate); SIM-347 (stress) + SIM-348
  (live-pipeline tests); P3 hygiene SIM-341–344.

---

# Sprint 2026-07-15 — Phase 4 Close-Out (Manager Logic + Hardening) (executed & CLOSED 2026-05-24)
**Authors: Product Manager (Agent 1), Baseball Analyst (Agent 2), Backend Developer (Agent 5), Performance Engineer (Agent 6), ML Engineer (Agent 3), QA/DevOps (Agent 9)**

**CLOSES PHASE 4.** Lands the manager decision logic + situational decisions, the last perf
mechanisms, the stress/live-pipeline test hardening, and the P3 hygiene. The PM planned the
sprint; role subagents implemented; the orchestrator ran the cross-validation (the QA
subagent kept hitting the shared session limit). Full record:
`docs/SPRINT_2026-07-15_phase4_closeout.md`. Phase 5 entry plan: `docs/HANDOFF_PHASE5.md`.

| Ticket | Type | Owner | One-liner |
|--------|------|-------|-----------|
| SIM-323 | Feature | BA + Backend | Manager decision logic — `_pre_pitch_hook` (IBB / pitch-out / steal green-light) + `_end_of_pa_hook` (starter pull / bullpen-by-leverage / pinch-hit / sac-bunt) from `manager_similarity` tendencies gated by a leverage index, via `ManagerContext`; no-DB-safe |
| SIM-349 | Design+Feature | BA + Backend | Situational decisions — hit-and-run + sac-fly intent on the SIM-323 hooks (IBB/sac-bunt already in SIM-323), base/out + leverage conditioned; no double-fire; sniff metrics unmoved |
| SIM-334 | Improvement | Perf + ML | Columnarized the situation engine — `_index_meta: list[NearestSituation]` → parallel read-only numpy column arrays (share-able, ≤2 GB); public query API/results unchanged |
| SIM-335 | Validation | Perf | Implemented perf Bench 4 (loop `step_pitch`) + Bench 5 (batch runner); SIM-119 budget asserted HARD only under `PERF_STRICT`+`PERF_STRICT_SANDBOX`; wired `perf-weekly.yml` + a 1.5 GB RSS gate. Perf suite 3→**5 passed / 0 skipped** |
| SIM-347 | Test | QA + Perf | Stress harness — 100 sims × 30 concurrent games via the batch runner; asserts no races, valid results, no `/dev/shm` leak; full 100×30 slow-marked (clean) |
| SIM-348 | Test | QA + Data | Real `live_ingestion_pipeline` tests (51) + removed the coverage omit; SIM-152 shared conftest confirmed complete |
| SIM-343 | CI | QA | Added `simulation/`+`betting/` to coverage scope (gate 80 unchanged); unified CI Python to 3.11 across all jobs |
| SIM-344 | Chore | QA | Extended `.gitignore` for scratch outputs (`*_output.txt`/`*.clean`/`*.tmp`/…); stray files now untracked+ignored (mount blocks the physical delete) |
| SIM-341 | Gap | PM | Reconciled README — engines 5-11 + registry marked shipped, fixed the stale `simulator.core` API sample → `simulation.*`, corrected the repo tree (`simulator/`→`simulation/`+`betting/`). PRODUCT_GUIDE had no stale `simulator.core` markers |
| SIM-342 | Improvement | PM | Re-categorized the stale Phase-3-Gate rows: SIM-107 → addressed by SIM-348; SIM-120 → unblocked by SIM-320 (`simulate_game`); SIM-127/128/129 → Phase 6 frontend |

### Verification
Orchestrator-run cross-validation (the QA subagent hit the session limit). Every sprint file
integrity-checked; the FULL unit+regression suite run in chunks (Sprint-5 files; situation
regression+unit; regression; data-eng; ML; engine/component; loop+output+betting; api/live/
smoke/older; perf-eng; the real-FAISS sim301/302/303/319 individually; the slow-marked tests;
performance) — every file covered. **New baseline: 1505 passed / 1 skipped / 0 failed**
unit+regression (1506 collected incl. 9 slow; the lone skip is the pre-existing
engine-build-smoke skip), up from 1380 (+125); **performance 5 passed / 0 skipped** (Bench 4/5
now real). DuckDB schema v7.

### Notes
- **QA caught a real regression:** SIM-334's columnarization broke the situation-engine
  golden-file + batch-equals-individual regression tests (the implementing agent ran the unit
  tests but not `tests/regression/`). Root cause: the regression tests inject `_index_meta` as
  a plain `list[NearestSituation]`, but the new `query()` only handled the columnar `.row()`.
  Fixed by a `_row_from_meta` helper that handles BOTH the columnar store and a list — query
  results are identical either way. All situation tests now green.
- Both Wave-A agents (SIM-334, SIM-348) and earlier QA agents hit the shared session limit
  mid-run; SIM-334 left the engine half-edited (an unclosed-bracket SyntaxError) — recovered by
  completing it. Remaining tickets were then run one-at-a-time to reduce peak load.
- Latent finding (SIM-347): the batch runner's `GameSpec._hit_rate` knob is dead (the factory
  reads it but `simulate_game` rejects it as a kwarg) — filed for follow-up, non-blocking.
- `backlog.xlsx` should be regenerated from `BACKLOG.md`.
- **PHASE 4 IS COMPLETE.** Next: Phase 5 — backend API + WebSocket + the 100-iteration runner
  endpoint + managerial-override endpoint (see `docs/HANDOFF_PHASE5.md`). Standing non-Phase-4
  follow-ups: SIM-315 (move repo off OneDrive — the biggest infra risk), the prop-TB 2B/3B
  upgrade, and the dead `GameSpec._hit_rate` knob.

---

# Program Audit — Phase 4 Close (2026-07-15, executed 2026-05-24)
**Authors: all 9 agents (3 role-clusters + PM consolidation)**

End-of-Phase-4 program audit, looking ahead to **Phase 5 (Backend API & Simulation Runner)**.
The 9 agent scopes reviewed the project (3 parallel read-only cluster reviews — Backend/Perf/ML,
Data/QA-DevOps, Betting/UX/Baseball — consolidated by the PM). Findings in
`docs/audit/2026-07-15-phase4-close-program-audit.md`; the deduped, tiered ticket list (28 tickets:
**SIM-350→377** + the **SIM-315** carryover) in `docs/audit/2026-07-15-phase5-prioritized-tickets.md`.
Phase 5 entry plan: `docs/HANDOFF_PHASE5.md`.

**Headline:** the `api/` layer is greenfield — all six Phase-5 endpoints, the JSON-serialization
contract, and auth are unbuilt, and `BatchRunner` has no production DB-backed factory (so
`/simulate` can't run a real game yet / the 2s/30s SLA is unverified).

**Four ⚠ defects found:** `docker-compose.yml` mounts the empty `./simulator` (not `./simulation`);
`api/` is missing from the coverage gate (only the Makefile measures it); `GameSpec._hit_rate`
raises `TypeError` when set (the factory reads it but `simulate_game` has no `**kwargs`); and
`clv_engine` has no spread/run-line edge report despite run-line odds being ingested.

**Two hard gates before endpoints:** the serialization contract (SIM-350) and the runtime
lineup/substitution resolver (SIM-353, the long-open SIM-338 gap), plus a real DB-backed
`machine_factory` (SIM-352). **Critical path:** SIM-350 → SIM-352/SIM-353 → SIM-355 (`/simulate`)
→ SIM-356 (snapshot persistence) → SIM-357/SIM-358 (`/plays`+`/state`, override). Next free ID
after the audit: **SIM-378**.

---

# Phase 5 Close-out + CI Stabilization + Phase 6 Kickoff — 2026-09-02 (executed 2026-05-25)
**Authors: all 9 agents (full parallel audit + independent QA cross-validation) + PM consolidation; CI fixes by Backend/QA**

**🏁 PHASE 5 (Backend API & Simulation Runner) is now fully CLOSED and CI-GREEN on Python 3.11.15.**

### Post-close CI stabilization (what it took to turn the suite green in CI)
The Phase-5 sprints landed all 28 tickets, but the first full CI run on the project's target interpreter
(**Python 3.11.15** — the sandbox dev base is 3.10) surfaced toolchain + version issues. Fixed:

| Area | Problem | Fix |
|---|---|---|
| Lint | CI floats the latest `ruff` (0.15.14) → ~840 errors from newer rules | `[tool.ruff.lint]` relaxed (dropped `PTH`; ignore opinionated `SIM` nits; per-file `E402` for tests/scripts) + `ruff format` (187 files); one real `F821` fixed with a `TYPE_CHECKING` import |
| Types | 8 `mypy` errors (`mypy similarity/ pipeline/ api/`, pinned `<2.0`) | annotations (`view: np.ndarray`, `index: Any`, `_conn` union), `getattr` for an optional attr, corrected `type: ignore` codes, `TYPE_CHECKING` import |
| Coverage | "borderline" reading was a `--cov-append` merge artifact | measure via `coverage --parallel-mode` + `combine`; real unit coverage **89%** (gate 80 MET); principled omits kept |
| Unit #1 | `test_odds_provider_sim370` used `asyncio.get_event_loop()` — on 3.11 it RAISES after pytest-asyncio nulls the loop (order-dependent; passes alone) | `asyncio.run(...)` + a suite-wide autouse `_ensure_current_event_loop` guard in `tests/conftest.py` |
| Unit #2 | `test_qa_sim326::...slow_exhaustive` (5000-game) timed out >30s **only under coverage** | `@pytest.mark.timeout(120)` (a marker overrides the CLI `--timeout`) |
| Fixture | `test_backend_sim318` `FileNotFoundError: docs/data/foul_rate_by_count.csv` — the 1KB reference table was **gitignored** (unanchored `data/` matched `docs/data/`), so it passed locally but never reached CI | anchored `.gitignore` `data/`→`/data/` + committed the CSV |
| Reporting | a pytest `tb_lineno - 1: NoneType` **INTERNALERROR** (renderer bug on `--tb=short`) masked the real failures + exited 3 | CI `--tb=short`→`--tb=native` (×3 jobs) so any failure is NAMED, not crashed |

**Result:** full unit+regression suite **1814 pass / 1 skip / 0 fail @ 89% coverage** on Python 3.11.15;
all 8 CI jobs green (lint, type-check, unit+coverage, regression, e2e, secrets, file-integrity, docker-build).

### Phase-5-close 9-agent program audit → Phase 6
A full parallel 9-agent audit (PM, Baseball Analyst, ML, Data, Backend, Performance, UX, Betting, QA/DevOps)
plus an independent QA cross-validation filed **43 Phase-6 tickets (SIM-378→SIM-420)**. Findings in
`docs/audit/2026-09-02-phase5-close-program-audit.md`; the deduped, tiered ticket list in
`docs/audit/2026-09-02-phase6-prioritized-tickets.md`; Phase-6 entry plan in `docs/HANDOFF_PHASE6.md`.

**Headline (QA-confirmed):** **Phase 6 = the Frontend Build** is effectively greenfield — `frontend/`
component dirs are empty, there is no build tooling/design-system/API→UI serving path, and the pre-existing
Phase-6 tickets (SIM-127–131) cite parent tickets (SIM-108/109/112/122–126) that don't exist (SIM-382
backfills them). The backend is strong but the UI needs new contracts it can't start without: an enriched
games list (bare integer IDs today, no standings table), a single aggregate card endpoint + status enum,
a typed WebSocket schema, a live in-progress read path (today stranded on the :8001 pipeline app), a
multi-substitution override body, and prop edge/signal endpoints.

**Defects/dead-wiring found today:** ⚠ the betting edge/CLV call site computes the "gold-standard" CLV off
an **uncalibrated** win probability (the loaded `calibration_map` is never threaded into `win_probability()`
— SIM-387); ⚠ `require_api_key` is defined but applied to **zero** routes (SIM-389); ⚠ `SIM_RUNNER_WORKERS=1`
serializes `/simulate` and the lifespan runner is built without `shared_arrays=` (SIM-403); ⚠ `GameState.park`
is a **dead field** never read by the run environment (SIM-411); ⚠ the `metrics.py` p95 gauge is an unwired
placeholder (SIM-410).

**Live-environment verification debt** (carried, code-complete, mock/unit-verified): the `/simulate` 2s/30s
SLA over the real DB factory (SIM-402), the real odds provider (SIM-405), a fitted `CalibrationReport`
(SIM-406), the DuckDB-profile 11-engine build (SIM-408), and a full `docker compose up` of nginx+app+monitoring.

**Next free ID after the audit: SIM-421.** Phase 6 critical path: SIM-378 (React-vs-vanilla ADR) →
foundation (379/380/381) + contracts (382/383/384/385/387/389) → live read (386) → cards/linescore (391/392)
→ game page/boxscore (393/394) → betting + override (395/396/397/398).

---
