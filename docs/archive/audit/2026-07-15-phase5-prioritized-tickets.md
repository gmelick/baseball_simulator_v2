# Phase 5 — Prioritized Ticket List (from the Phase-4-close program audit)

*Author: Product Manager (Agent 1) · 2026-07-15 (executed 2026-05-24) · Companion to `2026-07-15-phase4-close-program-audit.md`*

28 tickets (**SIM-350 … SIM-377**) consolidated + deduped from the 9-agent (3-cluster) audit,
plus the **SIM-315 carryover** (OneDrive — still Open). Tiers gate Phase 5 sequencing.
⚠ = a bug/defect that exists today.

Legend — Type: Feature / Bug / Gap / Spec / Perf / Test / Infra / Chore. Size: S (<1d) · M (3–5d) · L (1–2wk).

**Phase 5 = the Backend API & Simulation Runner.** The simulation/output/perf/betting layers
exist (Phase 4); Phase 5 is application-layer wiring (FastAPI / Redis / WebSocket / persistence)
— PLUS a handful of loop-output gaps the frontend/betting actually need that Phase 4 didn't
produce (R/H/E, fielders, W/L/S, richer boxscore).

---

## Tier P0 — Gates (land before / very early in endpoint coding)

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-350 | API response-serialization contract — Pydantic models + array-safe JSON for `GameSimSummary`/`WinProbability`/snapshots/PMFs/`EdgeReport` (dataclasses hold numpy arrays today; no `to_dict`) | Spec | M | Backend + UX | — |
| SIM-351 | Auth + rate-limit + CORS baseline (none exists anywhere; CORS is `["*"]`) | Feature | M | Backend + QA | — |
| SIM-352 | Production DB-backed `machine_factory` for `BatchRunner` (real sims over `PlayPoolSampler` + SIM-333 shared tiles) — without it `/simulate` can't run a real game; the 2s/30s SLA is unverified | Feature | L | Backend + Perf + ML | — |
| SIM-353 | Runtime lineup/substitution resolver: Postgres `raw.game_lineups`/`sim.lineup_state` → `GameState` (the SIM-338 gap; biggest data blocker) | Feature | L | Data + Backend | — |
| SIM-354 | API skeleton — mount the existing `ws_router`/`odds_router`/live pipeline into `api/main.py` + lifespan + the `simulation_callback` hook (all built, currently commented out) | Gap | S | Backend + Data | — |

## Tier P1 — Core endpoints + persistence

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-355 | `GET /api/games/{date}` + `GET /{game_pk}/simulate` (100-iteration runner wrap → `GameSimSummary`) | Feature | L | Backend | SIM-350, SIM-352, SIM-353 |
| SIM-356 | Sim-result + pitch-snapshot persistence (Alembic 0014 / DuckDB v8) backing `/state` + `/plays` + sim history (only an ephemeral Redis/JSONB blob today) | Feature | M | Data | SIM-350 |
| SIM-357 | `GET /{game_pk}/plays` + `GET /{game_pk}/state/{at_bat}/{pitch}` (from persisted SIM-331 snapshots) | Feature | M | Backend | SIM-356 |
| SIM-358 | `POST /{game_pk}/simulate/with_override` (roster-mod re-sim → `OverrideDelta`, baseline-delta-comparable) | Feature | M | Backend | SIM-355 |
| SIM-359 | Redis TTL wiring for the API (sim 60s / pool 5-min / odds 5-min; constants exist) | Feature | S | Backend | SIM-355 |
| SIM-360 | Persistent ProcessPool + shared-memory lifecycle for the long-lived API (today: a fresh pool + shared-mem publish/unlink per request) | Perf | M | Perf | SIM-352 |
| SIM-361 | `CalibrationReport` JSON persistence + API-startup load + `CalibrationMap` wiring; build all 11 engines at startup (only the pitcher engine today) | Feature | M | ML | — |

## Tier P2 — Loop-output gaps the frontend/betting need (loop additions feeding the API)

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-362 | Per-inning linescore + team R/H/E tracking in the loop (only cumulative score exists; `LinescoreGraphic` unservable) | Gap | L | Backend + BA | — |
| SIM-363 | Per-position fielder tracking → populate `FieldSnapshot`'s 9 defensive slots (empty today) | Gap | M | Backend + ML | SIM-353 |
| SIM-364 | Winning / losing / save pitcher attribution (required by the Completed game card; absent today) | Gap | M | BA + Backend | SIM-362 |
| SIM-365 | Extend `PlayerStatLine` (2B/3B/R/SB; pitcher hits/runs-allowed) + fix the SIM-329 prop-TB lower bound (`h+3·hr`) | Improvement | M | BA + Backend | — |
| SIM-366 | Per-player 100-iteration boxscore-average API shape (expose `PropDistributionSet` means as a boxscore-card payload) | Feature | S | Backend + UX | SIM-350 |

## Tier P2 — Betting API surface

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-367 | Run-line / spread `EdgeReport` from raw score-margin arrays (only ML/total/prop exist) | Gap | S | Betting + ML | — |
| SIM-368 | CLV / line-movement time-series surface (opening→closing; today CLV is a single entry-vs-close snapshot) | Gap | M | Betting + Data | SIM-370 |
| SIM-369 | Bet-signal / +EV recommendation endpoint contract (aggregate `EdgeReport`s into a fireable signal w/ threshold + timing + stake) | Feature | M | Betting | SIM-367 |
| SIM-370 | Real odds/prop provider swap behind `MockOddsAPI` (multi-book, sharp flag, cadence already wired by SIM-340) | Feature | M | Data + Betting | — |

## Tier P2/P3 — Testing / deploy / infra

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-371 | API integration + WebSocket + historical-replay E2E suite (FastAPI `TestClient`; `api/websocket/` is an empty placeholder) | Test | L | QA + BA | SIM-355 |
| SIM-372 | End-to-end `/simulate` latency perf gate (the 2s/30s SLA; current bench skips the request path) | Perf+Test | M | Perf | SIM-352 |
| SIM-373 | nginx reverse proxy w/ WebSocket upgrade + dev/staging/prod env configs | Infra | M | QA | SIM-354 |
| SIM-374 | Prometheus + Grafana monitoring (sim latency, API p95, pipeline-freshness alerts) | Infra | M | QA | SIM-373 |

## Tier P3 — Hygiene / bugs

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-375 | ⚠ Fix `docker-compose.yml` to mount `./simulation` (it mounts the empty `./simulator`) + remove the dead `simulator/` package | Bug | S | Data + QA | — |
| SIM-376 | ⚠ Add `api/` to the coverage gate (`pyproject.toml` source + `ci.yml --cov=api`) — only the Makefile measures it today | Bug | S | QA | — |
| SIM-377 | ⚠ Fix/remove `GameSpec._hit_rate` — the factory reads it but `simulate_game` has no `**kwargs`, so setting it raises `TypeError` | Bug | S | Backend | — |
| **SIM-315** | ⚠ **(carryover, still Open)** OneDrive remediation Option B: `scripts/check_file_integrity.py` (`ast.parse` + null-byte scan) + pre-commit + CI job; physical move is Greg-only | Infra | M | QA | — |

---

## Suggested first sprint (Phase 5 P0 gates)

**Sprint 1:** SIM-350 (serialization), SIM-353 (lineup resolver), SIM-352 (real `machine_factory`),
SIM-354 (mount the API skeleton), SIM-351 (auth baseline) + the quick ⚠ bugs SIM-375/376/377.
This unblocks real endpoints and verifies the SLA. **Critical path:**
SIM-350 → SIM-352/SIM-353 → SIM-355 (`/simulate`) → SIM-356 → SIM-357/SIM-358; SIM-354 + SIM-351
land first.

**Note on the loop-output gaps (SIM-362/363/364/365):** these are NOT pure API wiring — they
require the Phase-4 loop to *produce* data it currently doesn't (R/H/E, fielders, W/L/S). Sequence
them ahead of the UI-facing endpoints that serve them; they can run in parallel with the API
plumbing since they touch the loop, not `api/`.

**Carryover:** action **SIM-315** early — the OneDrive truncation tax hit nearly every large-file
edit across Phase 4 and will keep doing so. The dead `simulator/` package (SIM-375) and
`backlog.xlsx` regeneration are cheap cleanups.
