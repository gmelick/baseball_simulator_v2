# Sprint 2026-05-26 — Phase 6 Sprint 2: P1 Frontend Build (executed 2026-05-26)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-26 · Disposition: ✅ all 11 P1 frontend-build tickets accepted (build + lint + type-check green; chromium E2E green)*

Second Phase-6 sprint. Builds the entire frontend on the Sprint-1 contracts: the Day Summary slate,
the Game page (linescore + field + play-by-play + live WS), per-player projections with prop
distributions, the betting card + CLV chart, the managerial override UI (v1 + v2), an a11y/responsive
pass, a Playwright cross-browser E2E harness, and the deployable frontend Docker image + CD. Also
hardened the Sprint-1 scaffold (SIM-379), which had never actually passed its own CI job.

## 1. Tickets and owners

| Ticket | Type | Owner(s) | Deliverable |
|---|---|---|---|
| SIM-391 | Feature | UX + Backend | Day Summary page — date nav + game-count badge + 3-state GameCards + React Router; `api/games.ts` client |
| SIM-392 | Feature | UX + Backend | `LinescoreGraphic` (R/H/E grid + "x" cells) + `BaseballFieldGraphic` SVG (9 positions + runners + batter) |
| SIM-393 | Feature | UX + Backend | Game page — header + linescore + field + play-by-play scroll + live WS (`useGameSocket`, SIM-385) |
| SIM-394 | Feature | UX + Betting | Per-player boxscore means + on-demand prop distribution chart with prop-line marker |
| SIM-395 | Feature | UX + Betting | Betting card — ML/total/run-line edges + favored-side highlight + +EV signal badges + mock/live `odds_source` |
| SIM-396 | Feature | UX + Betting | CLV / line-movement chart — implied-prob series + close marker + sharp/steam/beat-close badges + market tabs |
| SIM-397 | Feature | UX + Backend | Managerial override v1 (single-sub form → `/simulate/with_override` → baseline-vs-override delta) |
| SIM-398 | Feature | UX + Backend | Managerial override v2 (staged queue + undo + multi-change via `substitutions[]`; supersedes v1 on the page) |
| SIM-399 | Gap | UX + QA | a11y + responsive pass — skip link, global `:focus-visible`, `prefers-reduced-motion`, responsive header, dead-CSS cleanup |
| SIM-400 | Test | QA | Playwright cross-browser E2E harness + 4 backend-mocked smoke specs (chromium-verified) + `frontend-e2e` CI job |
| SIM-401 | Infra | QA + Backend | Frontend Docker image (Vite build → nginx SPA + proxy) + CD to ghcr + compose wiring (image build-verified) |

## 2. Frontend scaffold CI hardening (completes SIM-379)

The SIM-379 scaffold committed `package.json` but the `frontend` CI job (`npm ci` → type-check → lint
→ build) could never have passed. Four blocking gaps, all fixed at the start of this sprint:

- **No `package-lock.json`** — `npm ci` hard-requires it. Generated via `npm install` (also adds
  `react-router-dom`, then `@playwright/test` for SIM-400).
- **No CSS-module type declarations** — added `frontend/src/vite-env.d.ts`
  (`/// <reference types="vite/client" />`).
- **No Vite `@/` alias** — the alias was tsconfig-only; added a matching `resolve.alias` to
  `vite.config.ts` (rollup was failing to resolve `@/…`).
- **Lint failure** under `--max-warnings 0` — `AuthContext.tsx` tripped
  `react-refresh/only-export-components`; added a scoped `eslint-disable`.

## 3. Architecture notes

- **Routing:** React Router introduced in SIM-391; routes `/`, `/date/:date` → Day Summary,
  `/game/:gamePk` → Game page, `*` → redirect. Mounted inside the authenticated shell.
- **API clients:** `src/api/games.ts` (slate, aggregate, linescore, plays, live, boxscore, prop edge,
  override) + `src/api/betting.ts` (edges, signals, line-movement). All use `credentials:'include'`
  and a shared `GamesApiError` carrying the HTTP status; a 404 is treated as "no data yet" by the
  Game page rather than an error.
- **Live updates:** `useGameSocket` subscribes to `/ws/games/{pk}`, consumes the SIM-385 event schema
  (`game_state_update` / `resim_pending` / `ping`→`pong`), normalizes runner fields, reconnects with
  backoff.
- **Expensive endpoints gated:** `/boxscore`, `/edges`, `/signals`, and `/simulate/with_override` run
  sims, so the boxscore / betting / override panels are behind explicit "load" / "run" actions, not
  auto-fetched on mount. `/linescore`, `/plays`, `/live`, `/line-movement` are cheap reads and load
  automatically.

## 4. Verification

- `npm run type-check` (tsc strict), `npm run lint` (eslint `--max-warnings 0`), and `npm run build`
  (tsc + vite) — **all green**.
- **Playwright E2E:** harness lists 12 tests (4 specs × chromium/firefox/webkit); the **chromium
  project was run locally and passed 4/4** — the first genuine browser verification of the frontend
  (login gating, slate render, card→game navigation, date nav). Firefox/WebKit run in the new
  `frontend-e2e` CI job.
- **Deploy:** the frontend Docker image **builds** (`docker build -f frontend/Dockerfile .`) and bakes
  the SPA into `/var/www/baseball-sim` with the matching nginx root.
- **Not browser-tested against a live backend:** every data-bound view still needs a populated
  Postgres/DuckDB to render real numbers (the standing live-env verification debt). The E2E suite
  proves rendering/routing/state with mocked responses; full-stack validation is a live-env run.

## 5. CI changes

- `frontend` job — now green (scaffold hardening above).
- `frontend-e2e` job (new) — installs Playwright browsers, runs the suite across all 3 engines,
  uploads the report.
- `frontend-release.yml` (new) — CD: builds + pushes `ghcr.io/<repo>-frontend` on main / release.

## 6. Bookkeeping

- `CHANGES.md` — Sprint 2 section + per-ticket detail.
- `BACKLOG.md` — banner flipped to "Sprint 2 COMPLETE"; SIM-391→401 marked Closed.
- `backlog.xlsx` — SIM-391→401 set to `Closed` in `Full Backlog` + `Phase 6 Build` (25 closed / 18 open).
- **Next free ticket ID: SIM-421.**

## 7. Next

The remaining open Phase-6 work is the **P1 data/ML/perf prerequisite + live-env verification track
(SIM-402→409)** and the **P2 realism + hardening tier (SIM-411→420)**. The frontend is now feature-
complete and CI-green; its data-bound views light up once the backend is brought up on a populated DB
(SIM-402/405/406/408) — the same live-env bring-up that clears the verification debt.
