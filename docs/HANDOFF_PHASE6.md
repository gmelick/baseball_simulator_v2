# Phase 6 Handoff — Frontend Build

*Author: Product Manager (Agent 1) · Date: 2026-09-02 (executed 2026-05-25) · Phase 5 closure*

---

## TL;DR

**Phase 5 (Backend API & Simulation Runner) is COMPLETE and CI-green on Python 3.11.15** — unit+regression
**1814 pass / 1 skip / 0 fail @ 89% coverage**, 8 CI jobs + weekly perf/integration. The `api/` layer serves
the full surface and the simulation/output/betting layers are done.

**Phase 6 = the Frontend Build (6 weeks).** This is the user-facing application: a Day Summary slate page,
3-state game cards, a Game page with pitch-level play-by-play + per-player projections, a managerial override
UI, a boxscore, and a betting/CLV surface — all consuming the existing API. A full 9-agent audit filed **43
tickets (SIM-378→SIM-420)**; the prioritized list is `docs/audit/2026-09-02-phase6-prioritized-tickets.md`
and the audit narrative is `docs/audit/2026-09-02-phase5-close-program-audit.md`.

**The honest starting truth:** Phase 6 is effectively **greenfield**. `frontend/{components,graphics,pages}/`
are empty dirs (only `similarity_explorer.html` exists); there's no build tooling, design system, or API→UI
serving path; and the pre-existing Phase-6 tickets (SIM-127–131) depend on parent tickets that were never
filed. Do **not** start building components before the P0 kickoff gates land.

---

## 1. What Phase 5 leaves you (build on these, don't rebuild)

The API the frontend consumes (`api/routes/`, contracts in `api/schemas.py`, numpy-safe via
`api/serialization.py`):
- `GET /api/games/{date}` — slate listing *(today returns bare integer IDs — SIM-383 enriches it)*
- `GET /api/games/{game_pk}/simulate` — 100-iteration runner → `GameSimSummary`
- `GET /api/games/{game_pk}/plays` — pitch-level play-by-play
- `GET /api/games/{game_pk}/state/{at_bat}/{pitch}` — point-in-time state
- `POST /api/games/{game_pk}/simulate/with_override` — re-sim w/ a modified roster *(single-sub today — SIM-388 adds multi-sub)*
- `GET /api/games/{game_pk}/linescore` · `/decisions` · `/boxscore` · `/card`
- `/api/betting/*` — edges / signals / line-movement / clv
- `WS /ws/games/{game_pk}` — live channel *(untyped today — SIM-385 schematizes it)*
- `/api/odds/*`, `/api/similarity/*`, `/metrics`, `/health`, `/ready`

Backed by: a persistent `ProcessPool` runner (`simulation/batch_runner.py`), Redis caching, DuckDB v10 /
Alembic 0014 persistence (`db/sim_store.py`), an 11-engine build (`api/state.py`), auth/rate-limit/CORS
(`api/auth.py`), nginx + Prometheus/Grafana (`deploy/`). The sim loop (`simulation/sim_loop.py`) and the 11
similarity engines (`similarity/engines/`) are complete and regression-gated.

## 2. Phase 6 scope (from `agent_team.md` — UX Designer, Agent 7)

Design system (tokens/typography/spacing/cards); **Day Summary page** (date nav + game-count badge +
3-state cards); **3-state game cards** (in-progress / completed / not-started); **LinescoreGraphic**
(inning grid + R/H/E, partial + extra innings); **BaseballFieldGraphic** SVG (9 positions + batter +
runners); **Game page** (play-by-play scroll + pitch drill-down + per-player sim panels); **managerial
override UI** (staged multi-change + undo + amber-indicator system + side-by-side compare); **boxscore**
(per-player 100-iteration averages + prop distributions). **Open decision:** React vs. vanilla JS +
WebSocket — decide at kickoff (SIM-378), keyed to override-UI complexity.

## 3. The 43-ticket plan (SIM-378→420) — read the prioritized doc for full rows

- **P0 — kickoff gates (SIM-378–390, 13):** ADR, scaffold+CI, design system, serving path, backfill the
  phantom deps; and the API contracts the UI can't start without — enriched games list + records, aggregate
  card + status enum, typed WS, live read path, the ⚠ calibration-wiring fix, multi-sub override, auth
  enforcement, prop edges.
- **P1 — frontend build (SIM-391–401, 11):** Day Summary + 3-state cards, linescore + field graphic, Game
  page, boxscore + distributions, betting card, CLV chart, override v1 (single-sub) then v2 (staged queue),
  a11y/responsive/cross-browser, Playwright e2e, frontend deploy/CD.
- **P1 — prerequisites + live-env verification (SIM-402–410, 9):** real-DB SLA, real parallelism +
  shared-memory wiring, stress/leak suite, real odds provider, fitted CalibrationReport + apply-to-all-engines,
  prop/ablation validation, DuckDB profile provisioning, lineup ingestion, p95 middleware.
- **P2 — realism + hardening (SIM-411–420, 10):** park factor, home-field advantage, pitcher-hand platoon,
  W/L/S+ER+R reconciliation, pagination, exception envelope, data-freshness API, slow-test CI lane, DuckDB
  index hardening, OpenAPI typed client.

**Critical path:** SIM-378 → 379/380/381 (foundation) + 382/383/384/385/387/389 (contracts) → 386 (live) →
391/392 (cards + graphics) → 393/394 (game page + boxscore) → 395/396/397 (betting + override v1) → 398
(override v2). The data/ML/perf track (402–409) runs alongside and must be **live-env verified** before the
numbers it backs reach users.

## 4. ⚠ Defects/dead-wiring to fix early (found in the audit)

- **CLV computed off an uncalibrated probability** — `betting.py:329` calls `win_probability()` with no
  `calibration_map=` though one is loaded at boot (SIM-387).
- **Auth defined but unenforced** — `require_api_key` is on zero routes; dev CORS is `*`+credentials (SIM-389).
- **Runner serialized** — `SIM_RUNNER_WORKERS=1` + shared-memory tiles unwired (SIM-403).
- **Dead `park` field** — the run environment ignores venue; no home-field edge; pitcher hand unused in the
  batted-ball matchup (SIM-411/412/413).
- **p95 metric is an unwired placeholder** (SIM-410).

## 5. Live-environment verification debt (carried from Phase 5)

Code-complete and unit/mock-verified, never run against a live stack — a Phase-6 staging bring-up burns these
down: `/simulate` 2s/30s SLA over the real DB factory (SIM-402); the real odds provider (SIM-405); a fitted
`CalibrationReport` (SIM-406); the DuckDB-profile 11-engine build (SIM-408); the replay/card endpoints
(`REPLAY_PERSISTENCE_ENABLED` + a writable replay DuckDB); a full `docker compose up` of nginx+app+monitoring.

## 6. Risks

- **Override UI is the single largest UI risk** (SIM-398) — split into v1 (single-sub, ships early) and v2
  (staged queue) so a usable override exists before the hard part.
- **Mock-vs-real odds** — until SIM-405, every displayed edge/CLV is against deterministic mock lines; the UI
  must label `odds_source` so mock isn't shown as real.
- **Uncalibrated numbers reaching users** — gate prop/win-prob exposure on SIM-387 + SIM-406/407.
- **Interactive latency unknown** — the headline SLA was only measured on the no-DB path; verify (SIM-402/403)
  before putting real users on it.

## 7. How to start (recommended first step)

Run the React-vs-vanilla ADR (SIM-378) first — it gates the scaffold, design system, CI, and the override
complexity. Then stand up SIM-379/380/381 (scaffold + design system + serving) in parallel with the backend
contract gates SIM-382/383/384/385/387/389. Only after the P0 tier is green should component build (SIM-391+)
begin. Keep the established workflow: role subagents per ticket cluster + an independent QA cross-validation
pass per sprint, documented in `CHANGES.md` / `BACKLOG.md` / a `docs/SPRINT_*.md`, with the backlog.xlsx
regenerated and git-sync commands handed off at each sprint close.

**Next free ticket ID: SIM-421.**
