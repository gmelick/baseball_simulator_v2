# Phase 5 Close — 9-Agent Program Audit (Phase 6 readiness)

*2026-09-02 (executed 2026-05-25) · Full 9-agent audit + independent QA cross-validation · Companion: `2026-09-02-phase6-prioritized-tickets.md`*

## Method

All 9 roles (PM, Baseball Analyst, ML, Data, Backend, Performance, UX, Betting, QA/DevOps) ran an
independent READ-ONLY audit of the codebase against the Phase-6 = **Frontend Build** theme. A separate
QA reviewer then cross-validated the load-bearing claims against the code (file:line), deduped overlaps,
and surfaced misses. Output: 43 tickets, **SIM-378 … SIM-420**.

## State at Phase-5 close

Phase 5 (Backend API & Simulation Runner) is **complete and CI-green on Python 3.11.15** — unit+regression
**1814 pass / 1 skip / 0 fail @ 89% coverage**, 8 CI jobs + weekly perf/integration. The `api/` layer
serves the full surface (games/simulate/with_override/plays/state/linescore/decisions/boxscore/card,
`/api/betting`, `/ws`, `/api/odds`, `/api/similarity`, `/metrics`, `/health`, `/ready`) behind
auth/rate-limit/CORS, a persistent ProcessPool runner, Redis caching, DuckDB v10 / Alembic 0014
persistence, an 11-engine build, nginx, and Prometheus/Grafana. Phases 1–4 (data, 11 similarity engines,
play pool, core sim loop) are done. **The backend is genuinely strong; the per-game API contracts are
excellent.**

## The eight cross-validated findings that shape Phase 6

1. **Frontend is greenfield.** `frontend/{components,graphics,pages}/` are empty dirs; only
   `similarity_explorer.html` exists. No build tooling, design system, or API→UI serving path
   (`api/main.py:15` TODO). *(PM, UX — CONFIRMED)*
2. **The existing Phase-6 tickets are un-actionable.** SIM-127–131 cite parents SIM-108/109/112/122–126
   that **do not exist** in any backlog sheet. *(PM, UX — CONFIRMED)* → SIM-382.
3. **The games list is unrenderable.** `GET /api/games/{date}` returns bare integer IDs; `raw.teams`/
   `raw.venues` exist but aren't joined, and **no standings/records table exists at all**.
   *(Backend, Data — CONFIRMED)* → SIM-383/384.
4. **The live in-progress feed is stranded** on the separate pipeline app (:8001); the main API has no
   live read path, and WS events are untyped raw dicts. *(Data, Backend — CONFIRMED)* → SIM-385/386.
5. **⚠ The "gold-standard" CLV is computed off an uncalibrated probability.** `betting.py:329` calls
   `win_probability()` with no `calibration_map=`, so edges/CLV use the IDENTITY map though a map is
   loaded at boot. *(ML, Betting — CONFIRMED, betting-only)* → SIM-387. And the whole **player-prop edge
   path is unwired** in the API. → SIM-390.
6. **The runner is single-worker.** `SIM_RUNNER_WORKERS=1` serializes `/simulate`, and the lifespan
   `BatchRunner` is built without `shared_arrays=`, so shared-memory tiles are unwired and the 2s/30s
   SLA was only ever measured over the no-DB rng path on shared CI. *(Perf — CONFIRMED)* → SIM-402/403/404.
7. **Auth is defined but unenforced.** `require_api_key` is applied to zero routes; the dev CORS path is
   `*`+credentials. *(Backend, QA — CONFIRMED)* → SIM-389.
8. **The run environment is context-blind.** `GameState.park` is a dead field (never read); there is no
   home-field advantage and the pitcher's throwing hand never selects the batter platoon split — so the
   score/win distributions the UI will visualize are park-blind and flat. *(Baseball Analyst — CONFIRMED)*
   → SIM-411/412/413.

## QA cross-validation additions (missed by the role agents)

- No `simulated_at` timestamp or win-prob/confidence-interval field is surfaced on the games API, yet
  SIM-127's staleness/CI UX depends on them — a second layer of phantom contract beyond the ticket IDs.
- The dev CORS `["*"]` + `allow_credentials=True` combination is a latent security footgun if
  `ENVIRONMENT` is misset (folded into SIM-389).
- `metrics.py` p95 gauge is a placeholder never populated (no timing middleware) → SIM-410.

## Live-environment verification debt (carried from Phase 5)

Code-complete and unit/mock-verified, but never run against a live stack: the `/simulate` real-DB SLA
(SIM-402), the real odds provider (SIM-405), a fitted `CalibrationReport` (SIM-406), the DuckDB-profile
11-engine build (SIM-408), the replay/card endpoints (`REPLAY_PERSISTENCE_ENABLED` + writable replay
DuckDB), and a full `docker compose up` of nginx+app+monitoring. A Phase-6 staging bring-up burns these down.

## Verdict

Phase 6 is **not kickoff-ready as filed** despite a "Phase 6 Gate" sheet existing: the frontend is empty,
the architecture decision is unrecorded, the spec chain has phantom dependencies, and several backend
contracts the UI consumes (enriched games list, aggregate card, typed WS, multi-sub override, prop edges,
calibrated CLV) don't exist yet. The work is real, well-scoped, and sits on a complete backend — the
immediate job is the P0 kickoff gates (SIM-378–390) before any component is built.
