# Frontend

The frontend is the browser app, "Daily Diamond". It is React 18, Vite 5 and TypeScript 5, and it
lives in [`frontend/`](../../frontend/). It shows the day's games, a game page with the real game
and our simulations, the Data Lab (a read-only explorer of the pitch table) and the Similarity
Explorer. It reads everything from the FastAPI app over REST, plus one WebSocket per live game. It
holds no model logic: every number it shows comes from the API.

## Run, build and test

You need Node 20 or later (`engines` in [`package.json`](../../frontend/package.json); CI uses
Node 20). Run every command in `frontend/`.

| Command | What it does |
|---|---|
| `npm ci` | Installs the exact packages in `package-lock.json`. |
| `npm run dev` | Starts the Vite dev server on `http://localhost:5173`, with hot reload. |
| `npm run build` | Runs the type checker (`tsc --noEmit`), then writes the static bundle and its source maps to `frontend/dist/`. |
| `npm run preview` | Serves the built bundle. Playwright runs it on port 4173. |
| `npm run type-check` | Runs the TypeScript type checker alone. |
| `npm run lint` | Runs ESLint over `src/`. One warning fails it. |
| `npm run gen:api` | Regenerates `src/api/schema.d.ts` from `openapi.json` (openapi-typescript). |
| `npm run e2e:install` | Installs the Playwright browsers and their system packages. |
| `npm run e2e` | Builds the app, serves it on :4173 and runs the Playwright specs in Chromium, Firefox and WebKit. |

**The dev proxy.** The dev server forwards the API calls to the backend. The browser then sees one
origin, so no CORS (cross-origin permission) setup is needed. [`vite.config.ts`](../../frontend/vite.config.ts)
sends `/api`, `/auth`, `/health`, `/ready`, `/docs` and `/openapi.json` to `API_TARGET`. The
default is `http://localhost:8000`, where the stack publishes the app on the loopback address. The
proxy also upgrades `/ws` to a WebSocket on the same host. To point the dev server at another app,
for example a worktree's app on :8010, set `API_TARGET=http://localhost:8010`. Because `/auth`
goes through the proxy, the browser stores the session cookie for the dev origin.

**Log in.** Every page needs a login. The password is the stack's `AUTH_PASSWORD` in `.env`. A
server that runs with `AUTH_DISABLED=1` lets every browser in.

**Production.** The compose service `nginx` builds [`frontend/Dockerfile`](../../frontend/Dockerfile)
with the repo root as the build context. Stage one runs `npm ci` and `npm run build` on Node 20.
Stage two copies `dist/` into nginx 1.27 at `/var/www/baseball-sim`, with
[`deploy/nginx/nginx.conf`](../../deploy/nginx/nginx.conf). nginx serves the app on port 80. It
sends every unknown path to `index.html`, so the browser router handles it. It proxies `/api`,
`/auth`, `/ws` and the API docs to the app. The bundle is baked into the image, so a frontend
change reaches the stack only when the `nginx` image is rebuilt. [`deploy/README.md`](../../deploy/README.md)
describes the proxy.

**Tests.** The frontend has no unit-test runner. Its tests are four Playwright specs (21 tests) in
[`frontend/e2e/`](../../frontend/e2e/). Each spec mocks the whole backend with `page.route`, so no
API or database runs. CI runs two jobs in [`ci.yml`](../../.github/workflows/ci.yml):

- `frontend`: regenerates the API types and fails on any difference from the committed
  `schema.d.ts`, then runs the type check, the lint and the build;
- `frontend-e2e`: runs the Playwright specs in the three browsers, with two retries.

The Data Lab and the Similarity Explorer have no Playwright spec.

**Release.** [`frontend-release.yml`](../../.github/workflows/frontend-release.yml) builds the same
image and pushes it to the GitHub container registry as `<repo>-frontend`. It runs on a push to
`main` or on a published release. The repository's branch is `master`, so today only a published
release runs it.

## How it fits together

1. `index.html` loads [`src/main.tsx`](../../frontend/src/main.tsx). `main.tsx` imports the bundled
   fonts and `global.css` (which imports the design tokens), then renders `App`.
2. [`App.tsx`](../../frontend/src/App.tsx) wraps the tree in `AuthProvider`. The provider calls
   `GET /auth/me` once. While that call runs, `AppShell` shows a loading splash. With no session it
   shows `LoginPage`. With a session it shows the header (Games, Data Lab, Similarity, Log out) and
   the routes.
3. React Router picks the page from the URL (see [Routes](#routes)).
4. A page loads its data through a client in `src/api/`. Every client calls the helpers in
   `http.ts`. The helpers send the session cookie, throw `GamesApiError` on a non-2xx answer, and
   fire the "unauthorized" event on a 401.
5. `AuthProvider` listens for the unauthorized event and marks the session logged out. An expired
   session therefore shows the login page, not a wall of error cards.
6. The game page opens the live WebSocket only for a game in progress (`useGameSocket`). While the
   game is live, the page also re-reads the REST state every 15 seconds.
7. Each component styles itself with a CSS module beside it. Every colour, size and font comes
   from a `--sim-*` or `--dd-*` token in `tokens.css`.

## Routes

| Path | Page | What it shows | API calls |
|---|---|---|---|
| `/` (today), `/date/:date` | `DaySummaryPage` | The day slate: one card per game, live first, then scheduled, final and postponed. A click opens a card in place. | `GET /api/games/{date}`; an open card adds `GET /api/games/{pk}/feed` and `GET /api/betting/games/{pk}/card-odds` |
| `/game/:gamePk` | `GamePage` | The header, the real game (live or final), the what-if panel, the Simulation card, one stored simulated game, and the collapsible Projections and Betting panels. | `/status`, `/live`, `/feed`, `/feed/plays`, `/feed/state`, `/card`, `/plays`, `POST /sample-game`, `/simulate` and `/simulate/runs/*`, `/boxscore`, `/props/{player}/{prop}`, `/what-if`, and `/api/betting/games/{pk}/edges` + `/signals` |
| `/lab` (goes to `/lab/summary`) | `SummaryPage` inside `DataLabLayout` | The descriptive dashboard over `raw.pitches`: a KPI strip, filters and six chart cards. | `GET /api/analytics/*`, `GET /api/data/freshness` |
| `/lab/console` | `ConsolePage` | The read-only SQL console. | `GET /api/schema`, `POST /api/sql/run` |
| `/lab/assistant` | `AssistantPage` | The AI assistant chat that writes and runs SQL. | `GET /api/assistant/status`, `POST /api/assistant/ask` (a streamed answer) |
| `/similarity` | `EngineIndexPage` | The engine chooser. | `GET /api/similarity/engines` |
| `/similarity/situation` | `SituationFinderPage` | The nearest historical game states to a state you build. | `POST /api/similarity/situation/query` |
| `/similarity/:engine` | `EngineExplorerPage` | One engine's ranked comps, score distribution and pair breakdown. | `GET /api/similarity/{engine}/query`, `GET /api/similarity/{engine}/pair` |
| `/player/:playerId` | `PlayerDetailPage` | One player's identity, seasons and a card per relevant engine. | `GET /api/players/{id}` |
| any other path | — | Goes to `/`. | — |

The name search box (`PlayerSearchInput`) on the Data Lab and the Similarity Explorer pages calls
`GET /api/players/search`.

## The API clients and the generated types

The frontend has two layers of API types.

- **The hand-written clients** in `src/api/` hold one function per route and one interface per
  response model. Each interface mirrors its Pydantic model by hand.
- **The generated types.** [`frontend/openapi.json`](../../frontend/openapi.json) is a copy of the
  API's contract (its OpenAPI schema). `npm run gen:api` turns it into
  [`src/api/schema.d.ts`](../../frontend/src/api/schema.d.ts).
  [`src/api/typed.ts`](../../frontend/src/api/typed.ts) gives the main shapes short names. No
  client imports `typed.ts` yet, so the hand-written interfaces can drift from the API; the
  generated types cannot.

A change to the API's contract (a route, a parameter, a field, a description) follows the API
snapshot rule (SIM-573), in the same commit:

1. Run [`scripts/export_openapi.py`](../../scripts/export_openapi.py) in the app image with the
   checkout mounted. It writes `frontend/openapi.json`. Never run it on the host: the host's
   Pydantic 2.10 drops `additionalProperties: true`, and the generator then types those objects as
   empty.
2. Run `npm run gen:api` in `frontend/`.
3. Update the hand-written interface that mirrors the changed model.
4. Commit `openapi.json` and `schema.d.ts` with the API change.

Two checks hold the rule. [`tests/unit/test_openapi_snapshot.py`](../../tests/unit/test_openapi_snapshot.py)
fails when `openapi.json` differs from the app's schema. The CI `frontend` job fails when
`schema.d.ts` differs from a fresh generation.

## Login

1. `LoginPage` sends the password to `AuthContext.login`.
2. `login` posts it to `POST /auth/login`. The server answers with an httpOnly cookie,
   `sim_session` ([`api/routes/auth.py`](../../api/routes/auth.py)). The cookie is SameSite=strict,
   Secure outside development, and lives `SESSION_TTL_HOURS` (default 8 hours).
3. `login` calls `GET /auth/me` again to read the auth mode (`cookie`, `api_key`, `disabled` or
   `none`).
4. A reload keeps the session: the provider's first `GET /auth/me` finds the cookie.
5. `Log out` posts to `POST /auth/logout`, which expires the cookie.

`checkAuth` never fires the unauthorized event: a non-OK answer reads as "not logged in". A 401
from `/auth/login` is a wrong password, and the login page shows it inline. The WebSocket
handshake carries the same cookie. The server closes a handshake with no valid cookie or key with
code 1008.

## The live WebSocket

[`useGameSocket`](../../frontend/src/hooks/useGameSocket.ts) opens `ws://` (or `wss://` on HTTPS)
`<host>/ws/games/{gamePk}`. The game page passes `enabled = true` only for a live game.

On the server, `game_state_ws` in
[`pipeline/live/live_ingestion_pipeline.py`](../../pipeline/live/live_ingestion_pipeline.py) holds
the socket. The live service publishes each message on the Redis channel `live:game:{pk}`. The
app subscribes while at least one browser watches that game and forwards each message
([`pipeline/live/broadcast.py`](../../pipeline/live/broadcast.py)). The channel stores nothing: a
browser that connects late gets the next message, and the page reads `/live` for the current state.

| Message | What the hook does |
|---|---|
| `game_state_update` | Stores the state. It renames `runner_on_first` / `_second` / `_third` to the REST names `on_1b` / `on_2b` / `on_3b`, and copies `resim_triggered` to `resimPending`. |
| `resim_pending` | Sets `resimPending` (a new simulation is queued). |
| `ping` | Answers `{"type": "pong"}`. The server sends a ping after 30 seconds with no message. |
| anything else | Records the time only. |

On an unexpected close the hook reconnects after 3 seconds. The game page takes the score from the
socket first, then from `GET /live`, then from the card.

## Design tokens and styles

- [`styles/tokens.css`](../../frontend/src/styles/tokens.css) defines every value as a CSS custom
  property on `:root`. The names follow `--sim-{category}-{scale}`: the gray, primary (forest
  green), success (grass), danger (stitch red), warning (scoreboard gold) and info scales; the
  semantic colours (`--sim-color-*`); the type families and sizes; the spacing scale on a 4 px
  grid; the radii, shadows, transitions, z-index layers, and the header height and page widths. The `--dd-*` tokens
  hold the Daily Diamond design's paper, field and scoreboard colours and its display face. The
  page has no dark mode.
- [`styles/global.css`](../../frontend/src/styles/global.css) holds the reset, the app shell (the
  header and its navigation) and a few utility classes.
- The fonts are bundled through `@fontsource` (Barlow, Barlow Condensed, JetBrains Mono; the Latin
  subset and only the weights the design uses), so the page calls no font host.
- The 30 team logos are committed SVGs in `public/logos/{team id}.svg`.
  [`scripts/fetch-logos.mjs`](../../frontend/scripts/fetch-logos.mjs) downloaded them once, by hand.
  [`src/teams.ts`](../../frontend/src/teams.ts) adds each club's colour and logo path.
- The charts are hand-written SVG ([`components/charts/`](../../frontend/src/components/charts/index.tsx)).
  The app uses no chart library.

## Entry, shell and shared state

### `frontend/src/main.tsx`

[Source](../../frontend/src/main.tsx) · 23 lines · Tests: none

The entry point. It imports the bundled font weights and `global.css`, then renders `App` inside
`React.StrictMode`.

### `frontend/src/App.tsx`

[Source](../../frontend/src/App.tsx) · 126 lines · Tests: [smoke.spec.ts](../../frontend/e2e/smoke.spec.ts)

The root component and the route table.

| Function / class | What it does | Called from |
|---|---|---|
| `App` | Wraps the tree in `AuthProvider`. | `main.tsx` |
| `AppShell` | Shows the splash, the login page or the routed app with the header, by the auth state. | `App` |

### `frontend/src/contexts/AuthContext.tsx`

[Source](../../frontend/src/contexts/AuthContext.tsx) · 126 lines · Tests: [smoke.spec.ts](../../frontend/e2e/smoke.spec.ts)

The auth state (`loading`, `authenticated`, `mode`) and the two actions, for the whole tree.

| Function / class | What it does | Called from |
|---|---|---|
| `AuthProvider` | Probes `GET /auth/me` on mount, listens for the unauthorized event, and holds `login` and `logout`. | `App` |
| `useAuth` | Returns the context. It throws outside `AuthProvider`. | `AppShell`, `LoginPage` |

### `frontend/src/hooks/useAsync.ts`

[Source](../../frontend/src/hooks/useAsync.ts) · 40 lines · Tests: none

| Function / class | What it does | Called from |
|---|---|---|
| `useAsync` | Runs `fn(signal)` when `deps` change and returns `loading`, `ok` or `error`. It aborts and drops a stale request. | The Data Lab and Similarity Explorer pages |

### `frontend/src/hooks/useGameSocket.ts`

[Source](../../frontend/src/hooks/useGameSocket.ts) · 181 lines · Tests: none

| Function / class | What it does | Called from |
|---|---|---|
| `useGameSocket` | Opens the game's WebSocket, maps its messages to `WsLiveState`, answers pings and reconnects after 3 s (see [The live WebSocket](#the-live-websocket)). | `GamePage` |

### `frontend/src/teams.ts`

[Source](../../frontend/src/teams.ts) · 59 lines · Tests: [slate.spec.ts](../../frontend/e2e/slate.spec.ts)

The 30 clubs by MLB team id, with an abbreviation and a primary colour.

| Function / class | What it does | Called from |
|---|---|---|
| `teamColor` | The club's colour, or the ink colour for an unknown id. | `TeamBlock`, the slate card |
| `teamLogo` | `/logos/{id}.svg`, or null for an unknown id. | `TeamBlock` |

`src/vite-env.d.ts` (6 lines) loads Vite's client types, so the type checker accepts
`*.module.css` imports and `import.meta.env`.

## The API clients (`frontend/src/api/`)

### `frontend/src/api/http.ts`

[Source](../../frontend/src/api/http.ts) · 75 lines · Tests: none

The JSON request helpers that every client except `auth.ts` and `assistant.ts` uses.

| Function / class | What it does | Called from |
|---|---|---|
| `GamesApiError` | The error for any non-2xx answer: the HTTP status, the body's `detail`, and the Retry-After seconds when asked for. | Every page that shows an error |
| `getJson` / `postJson` / `deleteJson` | Send one request with `credentials: 'include'` and read the JSON body. A 401 also calls `notifyUnauthorized`. `postJson` with `retryAfter: true` keeps the Retry-After header (the game routes' 503 for an unpublished lineup). | `games.ts`, `betting.ts`, `lab.ts`, `similarity.ts`, `players.ts` |

### `frontend/src/api/games.ts`

[Source](../../frontend/src/api/games.ts) · 693 lines · Tests: [slate.spec.ts](../../frontend/e2e/slate.spec.ts), [gamepage.spec.ts](../../frontend/e2e/gamepage.spec.ts), [simulation.spec.ts](../../frontend/e2e/simulation.spec.ts)

The client of the game routes (`/api/games`). Its interfaces mirror the models in
`api/routes/games_models.py` and `api/schemas/` by hand.

| Function / class | What it does | Called from |
|---|---|---|
| `cardStatus` | The card state of a row: the server's `game_status`, else a map of the stored status to `scheduled` / `live` / `final` / `postponed`. | The slate, the card text |
| `fetchGamesOnDate` | `GET /api/games/{date}`: the slate, with `source` (`schedule`, `schedule_cached` or `db`) and any feed error. | `DaySummaryPage` |
| `fetchGameFeed`, `fetchRealPlays`, `fetchPaState` | The real game from the league feed: the card detail, the plate appearances, and the game as it stood at one plate appearance. | `CardDetail`, `GamePage`, `WhatIfPanel` |
| `fetchGameCard`, `fetchLiveState` | One game's identity, state and score; the in-progress state (404 when not live). | `GamePage` |
| `fetchReplayCard`, `fetchPlays`, `postSampleGame` | The stored simulated game: its linescore, run id and seed; its pitch stream; a new stored game. | `GamePage` |
| `startSimRun`, `fetchLatestRun`, `fetchSimRun`, `cancelSimRun` | The durable simulation run: queue it, read the newest, poll one, cancel one. | `SimulationCard` |
| `fetchBoxscore`, `fetchPropEdge` | Each player's prop means over a run (or an unstored batch); one prop's full PMF, plus the over / under and the edge report when a line and odds are supplied. | `BoxscorePanel` |
| `startWhatIf`, `fetchWhatIf` | Queue the two what-if runs, then read both with the real final. | `WhatIfPanel` |

### `frontend/src/api/betting.ts`

[Source](../../frontend/src/api/betting.ts) · 139 lines · Tests: [smoke.spec.ts](../../frontend/e2e/smoke.spec.ts), [gamepage.spec.ts](../../frontend/e2e/gamepage.spec.ts), [slate.spec.ts](../../frontend/e2e/slate.spec.ts)

| Function / class | What it does | Called from |
|---|---|---|
| `fetchEdges`, `fetchSignals` | `GET /api/betting/games/{pk}/edges` and `/signals` (200 games by default). Both run a simulation on the server. | `BettingCard` |
| `fetchCardOdds` | `GET /api/betting/games/{pk}/card-odds`: the stored lines and their settlement (404 when the game has no line). | `CardDetail` |

### The smaller clients

| Module | Lines | What it holds |
|---|---|---|
| [`auth.ts`](../../frontend/src/api/auth.ts) | 50 | `login`, `checkAuth`, `logout` over `/auth/*`. It calls `fetch` directly and never fires the unauthorized event. |
| [`unauthorized.ts`](../../frontend/src/api/unauthorized.ts) | 22 | `UNAUTHORIZED_EVENT` (`sim:unauthorized`) and `notifyUnauthorized`. |
| [`lab.ts`](../../frontend/src/api/lab.ts) | 171 | The Data Lab: the analytics routes (with the game-type filter `R` or `all`, SIM-572), the data freshness, the schema tree, the SQL runner and the assistant status. |
| [`assistant.ts`](../../frontend/src/api/assistant.ts) | 96 | `askAssistant`: posts the question and history to `/api/assistant/ask` and reads the streamed answer as server-sent events (`data:` frames), one event per frame. |
| [`similarity.ts`](../../frontend/src/api/similarity.ts) | 153 | The engine catalog, an engine's query and pair reads, and the situation query. |
| [`players.ts`](../../frontend/src/api/players.ts) | 25 | `searchPlayers` (the name type-ahead) and `fetchPlayer`. |
| [`typed.ts`](../../frontend/src/api/typed.ts) | 37 | Short names for the generated response shapes. No module imports it yet. |
| [`schema.d.ts`](../../frontend/src/api/schema.d.ts) | 5,379 | Generated by `npm run gen:api`. Never edit it by hand; ESLint skips it. |

## The day slate (`frontend/src/pages/DaySummaryPage.tsx`, `components/slate/`)

### `frontend/src/pages/DaySummaryPage.tsx`

[Source](../../frontend/src/pages/DaySummaryPage.tsx) · 196 lines · Tests: [slate.spec.ts](../../frontend/e2e/slate.spec.ts), [smoke.spec.ts](../../frontend/e2e/smoke.spec.ts)

The slate page. The date lives in the URL; a bad date reads as today. The page sorts the cards
with `sortSlate`. It re-reads the slate every 30 seconds while a game is live or the date is
today, and only while the tab is visible. On a wide screen a live card starts open; on a phone
(640 px or narrower) every card starts closed. When `source` is not `schedule`, a banner says the
league feed is down and the list is a stored copy.

### `frontend/src/components/games/GameCard.tsx`

[Source](../../frontend/src/components/games/GameCard.tsx) · 105 lines · Tests: [slate.spec.ts](../../frontend/e2e/slate.spec.ts)

The slate card. Its header shows the state tag, both clubs, the score (or "vs") and our sim line. A
click opens the card in place and mounts `CardDetail`.

### `frontend/src/components/slate/CardDetail.tsx`

[Source](../../frontend/src/components/slate/CardDetail.tsx) · 514 lines · Tests: [slate.spec.ts](../../frontend/e2e/slate.spec.ts), [gamepage.spec.ts](../../frontend/e2e/gamepage.spec.ts)

The open card. It reads the real game (`/feed`) and the book's lines (`/card-odds`) and shows the
sections of the card's state. A section with no data hides itself. Every open card links to the
game page.

| Function / class | What it does | Called from |
|---|---|---|
| `CardDetail` | Scheduled: the lineups or the probable starters, and the pregame odds. Live: the linescore, the field, the count, the pitcher, the last play and the box score. Final: the linescore, the box score and how the pregame lines settled. Postponed: the reason and the make-up date. | `GameCard` |
| `Linescore`, `OnTheField`, `BoxScore` | The three real-game sections. The game page reuses them. | `CardDetail`, `GamePage` |
| `Section` | The titled wrapper of a card section. | `CardDetail` |

### The slate helpers

| Module | Lines | What it holds |
|---|---|---|
| [`slate/slate.ts`](../../frontend/src/components/slate/slate.ts) | 25 | `SLATE_POLL_MS` (30 s) and `sortSlate`: live, scheduled, final, postponed, each group by first pitch. |
| [`slate/cardText.ts`](../../frontend/src/components/slate/cardText.ts) | 54 | `statusTagText` (for example "Top 7th · 1 out", "Final/10", "PPD · Rain"), `centerSub`, and `simLine` ("Sim · NYY 54% · 2h ago", or "Sim (pre-game)" once the game starts). |
| [`slate/format.ts`](../../frontend/src/components/slate/format.ts) | 108 | Ordinals, short names, American odds, run lines, local start times, ages, and the local-date arithmetic of the date navigator. |
| [`slate/DatePicker.tsx`](../../frontend/src/components/slate/DatePicker.tsx) | 126 | ‹ and › step a day; the date button opens a month calendar; "Today" shows on another day. |
| [`slate/TeamBlock.tsx`](../../frontend/src/components/slate/TeamBlock.tsx) | 43 | One club: the logo, the colour chip, the name and the record. A losing club dims. |
| [`slate/CardField.tsx`](../../frontend/src/components/slate/CardField.tsx) | 91 | The "On the field" SVG: each fielder at his position, the runners on red bases, the batter at the plate. |

## The game page (`frontend/src/pages/GamePage.tsx`, `components/games/`)

### `frontend/src/pages/GamePage.tsx`

[Source](../../frontend/src/pages/GamePage.tsx) · 444 lines · Tests: [gamepage.spec.ts](../../frontend/e2e/gamepage.spec.ts), [simulation.spec.ts](../../frontend/e2e/simulation.spec.ts), [smoke.spec.ts](../../frontend/e2e/smoke.spec.ts)

The game page, top to bottom: the header (the team blocks, the score, the state, the probable
starters); for a live game, the socket's state and a re-simulating badge; for a live or final game,
the real game (the league feed's linescore, field and box score, and its play-by-play); the
what-if panel when open; the Simulation card; one stored simulated game (its linescore and
play-by-play); and the Projections and Betting panels, which start collapsed.

| Function / class | What it does | Called from |
|---|---|---|
| `useOptionalResource` | Loads one resource and reads a 404 as "no data yet" (null), not an error. | `GamePage` |
| `simulateOneGame` | Posts `/sample-game`, then reloads the stored game's card. The card's run id then loads that run's plays, so the linescore and the play-by-play always show the same game. | The "Simulate a game" button |
| the live poll | While the game is live, re-reads `/live`, `/feed` and `/feed/plays` every 15 seconds. | `GamePage` |

### `frontend/src/components/games/SimulationCard.tsx`

[Source](../../frontend/src/components/games/SimulationCard.tsx) · 208 lines · Tests: [simulation.spec.ts](../../frontend/e2e/simulation.spec.ts)

One durable simulation run per game. The user picks 100, 300 or 1,000 games (the time estimate
assumes 0.3 s a game) and presses Run. On mount the card reads the newest run, so a reload resumes
the progress bar. It polls a queued or running run every 2 seconds. Cancel stops a run between
chunks of games. A 503 for an unpublished lineup shows the server's words and counts down the
Retry-After seconds. `onDone` hands each finished run to the page, which gives it to the
projections.

### `frontend/src/components/games/BoxscorePanel.tsx`

[Source](../../frontend/src/components/games/BoxscorePanel.tsx) · 345 lines · Tests: [simulation.spec.ts](../../frontend/e2e/simulation.spec.ts)

The Projections panel. With a stored run it reads that run's prop means; each click on a prop chip
reads the same run's distribution. With no run, "Quick look" runs 100 games that are not stored as
a run. The quick look picks one seed and sends it with every request, so each click reads the same
cached batch; the API stores the batch's first game, and `onLoaded` tells the page to reload the
linescore and the play-by-play. The players are grouped by team: the batters in batting order,
then the pitchers, the starter first. A line input redraws the over / under marker.

### `frontend/src/components/games/BettingCard.tsx`

[Source](../../frontend/src/components/games/BettingCard.tsx) · 298 lines · Tests: [smoke.spec.ts](../../frontend/e2e/smoke.spec.ts), [gamepage.spec.ts](../../frontend/e2e/gamepage.spec.ts)

The Betting panel. "Load betting" reads `/edges`, then `/signals` (both run a simulation on the
server). The card renders every market the response carries, in the response's `markets` order,
under four sub-headings: Full game, First five innings, First inning, Team totals. Each market
shows its edge reports with the favoured side marked, the +EV signal badges (stake and price), the
odds source, and for a stored line the book of the fair price and the best book per side. After the
game it grades each side on the real final.

| Function / class | What it does | Called from |
|---|---|---|
| `LABEL_OF` | Maps the market type `runline` to its edge label `run_line`, the one place the two names differ. | `BettingCard` |
| `SEGMENT_OF` | Puts each market under its sub-heading. A market it does not know goes in a last group with no heading. | `BettingCard` |
| `FALLBACK_NAMES` | Market titles for a response without `market_names`. | `referenceName` |

### `frontend/src/components/games/WhatIfPanel.tsx`

[Source](../../frontend/src/components/games/WhatIfPanel.tsx) · 527 lines · Tests: [gamepage.spec.ts](../../frontend/e2e/gamepage.spec.ts)

The what-if from a play of the real game (SIM-562). The panel reads the game as it stood at the
start of the chosen plate appearance (`/feed/state`) and draws it on the slate's field. The user
stages a pinch hitter, a reliever, a pinch runner or a defensive change; before first pitch, either
side's lineup and starter. "Simulate 100 from here" queues two runs with the same seeds, as it
stood and with the changes. The panel polls both every 2 seconds and then compares them beside the
real final.

### The other game-page components

| Module | Lines | What it holds |
|---|---|---|
| [`games/ActualPlayByPlay.tsx`](../../frontend/src/components/games/ActualPlayByPlay.tsx) | 133 | The real game's plate appearances by half-inning (newest first while live), each with its pitches folded under it and a "What if from here" button. |
| [`games/PlayByPlayList.tsx`](../../frontend/src/components/games/PlayByPlayList.tsx) | 208 | The stored simulated game's pitch stream, grouped into plate appearances and half-innings; a click expands a plate appearance's pitches. |
| [`games/PropDistributionChart.tsx`](../../frontend/src/components/games/PropDistributionChart.tsx) | 114 | One prop's PMF (the probability of each count) as SVG bars, with the line and the mean marked; bars over the line are tinted. |
| [`graphics/LinescoreGraphic.tsx`](../../frontend/src/components/graphics/LinescoreGraphic.tsx) | 98 | The simulated game's linescore: runs per inning and R / H / E. A null cell is a half never played, shown as "x". |
| [`graphics/index.ts`](../../frontend/src/components/graphics/index.ts) | 3 | Exports `LinescoreGraphic`. |

## The design-system primitives (`frontend/src/components/ui/`)

| Module | Lines | What it holds |
|---|---|---|
| [`ui/Badge.tsx`](../../frontend/src/components/ui/Badge.tsx) | 61 | The compact status tag: LIVE (pulsing), FINAL, SCHEDULED, +EV, a result, a caution, a count. |
| [`ui/Card.tsx`](../../frontend/src/components/ui/Card.tsx) | 71 | The surface container with an optional title and header slot. |
| [`ui/Panel.tsx`](../../frontend/src/components/ui/Panel.tsx) | 106 | A lighter content section. A `collapsible` panel opens and closes on its heading; `storageKey` remembers the choice in `localStorage` (the game page uses `gamepage.projections` and `gamepage.betting`). |
| [`ui/Drawer.tsx`](../../frontend/src/components/ui/Drawer.tsx) | 63 | The slide-over panel from the right (a bottom sheet on a phone). It closes on Esc or a backdrop click and locks the page scroll. |
| [`ui/index.ts`](../../frontend/src/components/ui/index.ts) | 18 | Exports the four primitives. |

## The Data Lab (`/lab`)

The Data Lab reads `raw.pitches` and the other `raw.*` tables. The server runs its SQL on the
read-only database pool and accepts only reads of `raw.*`.

| Module | Lines | What it holds |
|---|---|---|
| [`pages/DataLabLayout.tsx`](../../frontend/src/pages/DataLabLayout.tsx) | 39 | The tab bar (Summary · SQL Console · AI Assistant) over the nested route. |
| [`pages/SummaryPage.tsx`](../../frontend/src/pages/SummaryPage.tsx) | 291 | The KPI strip, the data freshness badge, the filters (season, pitcher, batter side, games) and six cards: pitches by season, pitch-type mix, plate-appearance outcomes, whiff rate by count, release-velocity distribution, whiff rate by zone. Each card loads on its own and links to its SQL in the console. The "Games" filter reads the regular season (the default) or every game (SIM-572); the KPI strip and "Pitches by season" ignore it. |
| [`pages/ConsolePage.tsx`](../../frontend/src/pages/ConsolePage.tsx) | 271 | The SQL editor (Ctrl/Cmd+Enter runs, Esc cancels, Tab inserts two spaces), the starter queries, the schema browser, the result grid with CSV export, and the last 25 queries in `localStorage` (`datalab.sqlHistory`). It asks for at most 1,000 rows. |
| [`pages/AssistantPage.tsx`](../../frontend/src/pages/AssistantPage.tsx) | 205 | The streamed assistant chat. Each turn shows its prose, the SQL it ran (Copy, Open in Console) and its result grid. With no assistant configured it shows the not-configured state. |
| [`lab/ResultGrid.tsx`](../../frontend/src/components/lab/ResultGrid.tsx) | 114 | The sortable result table. A player-id cell links to `/player/:id`. |
| [`lab/StatTile.tsx`](../../frontend/src/components/lab/StatTile.tsx) | 39 | `StatTile`, `KpiRow` and `DataFreshnessBadge`. |
| [`lab/states.tsx`](../../frontend/src/components/lab/states.tsx) | 82 | The shared loading, empty, error, engine-warming and not-configured states. |
| [`lab/starterQueries.ts`](../../frontend/src/components/lab/starterQueries.ts) | 153 | The nine starter queries. Each bounds its scan, filters `data_quality_flag = FALSE`, keeps a sample floor and a `LIMIT`. |
| [`lab/csv.ts`](../../frontend/src/components/lab/csv.ts) | 20 | `downloadCsv`. |
| [`charts/index.tsx`](../../frontend/src/components/charts/index.tsx) | 321 | `ScoreBar`, `BarList`, `ColumnChart`, `HeatGrid`, `RadarChart`: SVG charts on the tokens, each with `role="img"` and a label. The Similarity Explorer uses them too. |

## The Similarity Explorer (`/similarity`, `/player/:playerId`)

The explorer shows what each similarity engine scores. The design is in
[`docs/similarity_visualization_spec.md`](../similarity_visualization_spec.md).

| Module | Lines | What it holds |
|---|---|---|
| [`pages/EngineIndexPage.tsx`](../../frontend/src/pages/EngineIndexPage.tsx) | 86 | The catalog in two groups: the player comparisons (score engines, with a built badge) and the pattern finders (distance engines). `situation` goes to the Situation Finder; another distance engine goes to the Data Lab. |
| [`pages/EngineExplorerPage.tsx`](../../frontend/src/pages/EngineExplorerPage.tsx) | 302 | One engine: search a subject, see the top 50 comps and the 20-bin score distribution, open a comp's pair breakdown in a drawer. The subject (`subject=<id>-<season>`), the position (`pos`, fielder engine only, default `2B`), the hand split (`vs`, batter engine only) and the comp (`compare`) live in the URL. |
| [`pages/SituationFinderPage.tsx`](../../frontend/src/pages/SituationFinderPage.tsx) | 155 | The situation engine: build a game state, see the nearest historical states with their DISTANCE (lower is closer), not a 0-to-1 score. |
| [`pages/PlayerDetailPage.tsx`](../../frontend/src/pages/PlayerDetailPage.tsx) | 91 | One player: the identity, the seasons, and a link per relevant engine seeded with the latest season. |
| [`similarity/CompTable.tsx`](../../frontend/src/components/similarity/CompTable.tsx) | 82 | The ranked comps: rank, player, season, composite bar, a bar per sub-score, sample size. |
| [`similarity/ComponentScoreBars.tsx`](../../frontend/src/components/similarity/ComponentScoreBars.tsx) | 77 | One bar per sub-score with its weight marked. A footnote says the composite carries a sample-confidence discount, so it is not the weighted sum of the bars. |
| [`similarity/PlayerSearchInput.tsx`](../../frontend/src/components/similarity/PlayerSearchInput.tsx) | 145 | The debounced name search (a combobox with arrow, Enter and Escape keys) over `/api/players/search`. |

## The login page

| Module | Lines | What it holds |
|---|---|---|
| [`auth/LoginPage.tsx`](../../frontend/src/components/auth/LoginPage.tsx) | 91 | The password form. Enter submits; Escape clears the error; a failed login shows inline. |

## Stylesheets

Each component's CSS module sits beside it and uses only the tokens.

| Folder | Modules |
|---|---|
| `src/styles/` | [`tokens.css`](../../frontend/src/styles/tokens.css) (208 lines), [`global.css`](../../frontend/src/styles/global.css) (284 lines) |
| `src/pages/` | `DaySummaryPage.module.css`, `GamePage.module.css`, `dataLab.module.css`, `console.module.css`, `assistant.module.css`, `similarity.module.css` (the last four are shared by the Data Lab and explorer pages) |
| `src/components/games/` | `ActualPlayByPlay`, `BettingCard`, `BoxscorePanel`, `GameCard`, `PlayByPlayList`, `PropDistributionChart`, `SimulationCard`, `WhatIfPanel` (`.module.css` each) |
| `src/components/slate/` | `CardDetail`, `CardField`, `DatePicker`, `TeamBlock` (`.module.css` each) |
| `src/components/ui/` | `Badge`, `Card`, `Drawer`, `Panel` (`.module.css` each) |
| `src/components/lab/` | `ResultGrid.module.css`, `StatTile.module.css`, `states.module.css` |
| `src/components/similarity/` | `CompTable`, `ComponentScoreBars`, `PlayerSearchInput` (`.module.css` each) |
| `src/components/` other | `auth/LoginPage.module.css`, `charts/charts.module.css`, `graphics/LinescoreGraphic.module.css` |

## The Playwright specs (`frontend/e2e/`)

| Spec | Lines | Tests | What it checks |
|---|---|---|---|
| [`smoke.spec.ts`](../../frontend/e2e/smoke.spec.ts) | 327 | 6 | The login gate, the slate and its count, an open card's link to the game page, date navigation, the betting card's fifteen markets with a tie side, and the signals read only after the edges answer. |
| [`slate.spec.ts`](../../frontend/e2e/slate.spec.ts) | 339 | 8 | The sort and the state tags, a live card's default open state with its field, a final card's linescore, box tabs and settled lines, a scheduled card's lineups and odds, a postponed card, the calendar, the degraded banner, the phone layout. |
| [`gamepage.spec.ts`](../../frontend/e2e/gamepage.spec.ts) | 313 | 4 | The real game above the simulation, the collapsed and remembered panels, the what-if from a play, the betting card's grading on the real final. |
| [`simulation.spec.ts`](../../frontend/e2e/simulation.spec.ts) | 152 | 3 | Run, progress and projections from the finished run; resume and cancel; the unpublished-lineup countdown. |

## Configuration and build files

| File | Lines | What it holds |
|---|---|---|
| [`package.json`](../../frontend/package.json) | 40 | The scripts, Node 20+, React 18, React Router 6, the three `@fontsource` families; the dev tools (Vite 5, TypeScript 5, ESLint 8, Playwright, openapi-typescript 7). |
| `package-lock.json` | 3,855 | The locked dependency tree. `npm ci` installs from it. |
| [`vite.config.ts`](../../frontend/vite.config.ts) | 57 | The React plugin, the `@` alias to `src/`, the build options and the dev proxy (`API_TARGET`). |
| [`tsconfig.json`](../../frontend/tsconfig.json) | 31 | Strict TypeScript for `src/`, bundler module resolution, the `@/*` path alias. |
| [`tsconfig.node.json`](../../frontend/tsconfig.node.json) | 11 | The settings for `vite.config.ts`. |
| [`.eslintrc.cjs`](../../frontend/.eslintrc.cjs) | 26 | ESLint with the TypeScript and React-hooks rules; unused names starting with `_` pass; it skips `schema.d.ts`. |
| [`playwright.config.ts`](../../frontend/playwright.config.ts) | 38 | The three browsers, the build-and-preview web server on :4173, retries and the GitHub reporter on CI. |
| [`index.html`](../../frontend/index.html) | 14 | The page shell: the title "Daily Diamond", the root element, the module script. |
| [`Dockerfile`](../../frontend/Dockerfile) | 40 | The two-stage image: the Node 20 build, then nginx 1.27 with the shared `nginx.conf`. |
| [`Dockerfile.dockerignore`](../../frontend/Dockerfile.dockerignore) | 23 | The ignore list for that build. It overrides the root `.dockerignore`, which excludes `frontend/` from the backend image. |
| [`openapi.json`](../../frontend/openapi.json) | 10,394 | The API contract snapshot (see [The API clients and the generated types](#the-api-clients-and-the-generated-types)). |
| [`scripts/fetch-logos.mjs`](../../frontend/scripts/fetch-logos.mjs) | 23 | Downloads the 30 logos into `public/logos/`. Run once by hand; the logos are committed. |
| `public/logos/*.svg` | — | The 30 committed team logos. |

**Watch out:**

- `index.html` links the favicon `/baseball.svg`, but `public/` holds only the logos, so the
  browser's favicon request gets a 404.
- The folders `frontend/components/`, `frontend/pages/` and `frontend/graphics/` exist on this
  machine but are empty, and git does not track them. They are leftovers; the code lives under
  `frontend/src/`.
- The hand-written interfaces in `src/api/*.ts` do not follow an API change by themselves. Update
  them in the same commit as the snapshot.
