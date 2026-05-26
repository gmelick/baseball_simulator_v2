# Phase 6 — Prioritized Ticket List (from the Phase-5-close program audit)

*Author: Product Manager (Agent 1) · 2026-09-02 (executed 2026-05-25) · Companion to `2026-09-02-phase5-close-program-audit.md`*

43 tickets (**SIM-378 … SIM-420**) consolidated + deduped from the full 9-agent audit and an
independent QA cross-validation pass. **Phase 6 = the Frontend Build.** The backend/API/sim/betting
layers are complete (Phases 1–5); Phase 6 is the user-facing application — PLUS the backend contracts
the UI cannot start against today, and the live-env / realism / hardening debt that should not reach
users uncaught.

Legend — Type: Feature / Bug / Gap / Spec / Perf / Test / Infra / Improvement / Security / Chore.
Size: S (<1d) · M (3–5d) · L (1–2wk). ⚠ = a defect/dead-wiring that exists today.

> **Reality check (QA-confirmed).** The pre-existing Phase-6 tickets **SIM-127–131** all cite parent
> tickets **SIM-108/109/112/122/123/124/125/126 that do not exist anywhere in the backlog** — the design
> chain is broken (SIM-382 backfills it). The `frontend/` "prototypes" are **empty directories** plus one
> standalone `similarity_explorer.html`; there is no build tooling, design system, or API→UI serving path.
> Phase 6 is effectively **greenfield frontend on a strong, complete backend.**

---

## Tier P0 — Kickoff gates (frontend foundation + the API contracts the UI can't start without)

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-378 | React-vs-vanilla-JS architecture decision, recorded as an ADR (the agent_team.md:238 "key deferred decision"; gates all build tooling; override UI is the complexity driver) | Spec | S | UX + Backend + QA | — |
| SIM-379 | Frontend scaffold + build tooling + a frontend CI job (no package.json/bundler/lint/test runner exists; component dirs empty; CI has no JS lane) | Infra | M | UX + QA | SIM-378 |
| SIM-380 | Design-system foundation: tokens, typography, spacing, Card/Panel/Badge primitives (only ad-hoc CSS vars in similarity_explorer.html today) | Feature | M | UX | SIM-379 |
| SIM-381 | API→frontend serving path — StaticFiles/nginx SPA fallback (`api/main.py:15` "frontend serving" TODO; nginx has no static location) | Gap | S | Backend + UX | SIM-378 |
| SIM-382 | Backfill the 8 phantom Phase-6 parent tickets + re-map SIM-127–131 deps (SIM-108/109/112/122–126 don't exist; QA-confirmed) | Gap | M | PM + UX + Backend | — |
| SIM-383 | Enrich `GET /api/games/{date}` with team/venue names + records (`GameCard` returns bare int IDs; raw.teams/raw.venues unjoined; **no standings table exists**) | Feature | M | Backend + Data | — |
| SIM-384 | Single game-card aggregate endpoint + game-status enum scheduled/live/final (3-state cards need identity+status+sim+odds in one call; `raw.games.status` unmapped) | Feature | M | Backend | SIM-383 |
| SIM-385 | Typed + documented WebSocket event schema (WS broadcasts are untyped raw dicts; no Pydantic models, no AsyncAPI doc — frontend has no live contract) | Feature | M | Backend | — |
| SIM-386 | Live in-progress game-state read path on the main API (live `sim.lineup_state` is only served by the separate pipeline app on :8001; in-progress cards unreachable) | Feature | L | Data + Backend | SIM-384 |
| SIM-387 | ⚠ Fix dead calibration wiring at the betting edge/CLV call site (`betting.py:329` win_probability() w/o `calibration_map=` → CLV uses the IDENTITY map though one is loaded at boot) | Bug | S | Backend + ML | — |
| SIM-388 | Multi-substitution override (array body) — unblocks SIM-128 (`RosterOverride` accepts a single sub + full lineups only) | Feature | M | Backend | — |
| SIM-389 | Enforce auth on data/expensive routes + browser session model + fix dev CORS `*`+credentials (`require_api_key` is defined but wired to ZERO routes) | Security | M | Backend + QA | — |
| SIM-390 | Player-prop edge/signal API endpoints (`prop_edge_report`/`p_over` exist + tested but NO route calls them; props only surface as boxscore means) | Feature | L | Backend + Betting | SIM-387 |

## Tier P1 — Frontend build (the components, on the now-buildable contracts)

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-391 | Build Day Summary page — date nav + game-count badge + 3-state game cards | Feature | L | UX + Backend | SIM-380, SIM-383, SIM-384 |
| SIM-392 | Build LinescoreGraphic (inning grid + R/H/E, partial/extra/tie "x") + BaseballFieldGraphic SVG (9 positions + batter + runners, label-collision rules) | Feature | M | UX + Backend | SIM-380, SIM-386 |
| SIM-393 | Build Game page — play-by-play scroll (virtualized) + pitch drill-down + per-player sim panels + live WS updates | Feature | L | UX + Backend | SIM-391, SIM-385 |
| SIM-394 | Build per-player boxscore — 100-iteration averages + distribution views w/ prop-line marker (subsumes SIM-129) | Feature | M | UX + Betting | SIM-380, SIM-390 |
| SIM-395 | Build betting card surface — ML/spread/total layout + winning-side highlight + +EV signal badge (stake% + offered price + mock-vs-real `odds_source` indicator) | Feature | L | UX + Betting | SIM-380, SIM-390 |
| SIM-396 | Build CLV / line-movement time-series chart (`/line-movement` series + close marker + sharp/steam badges) | Feature | M | UX + Betting | SIM-395 |
| SIM-397 | Managerial override UI — v1 single-sub (ships early on the existing `/simulate/with_override` + amber indicator + before/after compare) | Feature | M | UX + Backend | SIM-393 |
| SIM-398 | Managerial override UI — v2 staged queue + undo + multi-change + 4-rule amber-indicator system (the SIM-128 build) | Feature | L | UX + Backend | SIM-397, SIM-388 |
| SIM-399 | Frontend a11y + responsive/mobile + cross-browser acceptance gate (Chrome/FF/Safari desktop+mobile; keyboard/ARIA) | Gap | M | UX + QA | SIM-391 |
| SIM-400 | Cross-browser E2E harness (Playwright chromium/firefox/webkit + mobile; live WS smoke flow) | Test | L | QA | SIM-379 |
| SIM-401 | Frontend deploy — static build artifacts + nginx serving + CD push to ghcr | Infra | M | QA + Backend | SIM-381 |

## Tier P1 — Backend / perf / data prerequisites + live-env verification (de-risk before users)

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-402 | Real-DB `/simulate` 2s/30s SLA verification on dedicated hardware (today asserted only over the no-DB rng factory on shared CI — documented as a LOWER BOUND) | Perf | M | Perf + QA | — |
| SIM-403 | Enable real parallelism + wire shared-memory tiles into the live runner (`SIM_RUNNER_WORKERS=1` serializes; lifespan `BatchRunner` built w/o `shared_arrays=` → ~290MB re-loaded per worker) | Perf | M | Backend + Perf | SIM-402 |
| SIM-404 | Stress / concurrency / leak suite — 100 sims × 30 concurrent games (none exists; long-lived-server leak/race risk) | Test | L | QA + Perf | SIM-403 |
| SIM-405 | Real odds-provider implementation behind the SIM-370 seam (`RealOddsAPIProvider` is a stub; all UI odds/edges/CLV are mock — real CLV is blocked on this) | Feature | L | Data + Betting | — |
| SIM-406 | Fit + persist a `CalibrationReport` over real data (SIM-220 debt) + apply to all engines (today: nothing fits it; `apply_calibration` only on the pitcher engine) | Feature | L | ML | SIM-408 |
| SIM-407 | Validate prop PMFs + run ablation/walk-forward over real outcomes before frontend exposure (PMFs have no calibration seam/backtest; ablation is synthetic-only) | Validation | M | ML + Betting | SIM-406 |
| SIM-408 | DuckDB profile/pool build + provisioning for the 11-engine startup (engines need populated `derived.*`/`sim.*_pool`; partial builds serve degraded similarity silently) | Infra | M | Data | — |
| SIM-409 | Lineup ingestion guarantee for scheduled games (SIM-338 lineage — `resolve_lineup` raises when `raw.game_lineups` empty → blank field graphic / sim 500s) | Bug | M | Data + Backend | SIM-386 |
| SIM-410 | Wire the API p95 timing middleware (the `metrics.py` gauge is a PLACEHOLDER never populated → Grafana panel reads 0) | Improvement | S | Backend + QA | — |

## Tier P2 — Realism + hardening

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-411 | Park factor into the run environment (`GameState.park` is a DEAD field — never read; Coors vs Petco give identical distributions, a user-visible hole) | Improvement | L | BA + ML + Data | — |
| SIM-412 | Home-field run advantage in the score distribution (only structural last-AB/walk-off rules today; aggregate home_win_pct won't hit the empirical ~.535–.540) | Improvement | M | BA | SIM-411 |
| SIM-413 | Pitcher throwing-hand → batter platoon split in the batted-ball matchup (engine has vs-LHP/RHP machinery but the loop fingerprint keys on `bat_hand` only) | Improvement | M | BA + ML | — |
| SIM-414 | W/L/S + ER + per-runner R cross-surface reconciliation (sub-5-IP "winners", under-counted unearned runs, walk-forced R missing → boxscore disagrees with linescore once shown together) | Bug | M | BA + Backend | SIM-384 |
| SIM-415 | Pagination / payload-trim for heavy endpoints (`/plays` unpaged; `GameSimSummaryModel` ships raw N-length score arrays by default → multi-MB browser payloads) | Improvement | M | Backend | — |
| SIM-416 | App-level exception handler + structured `{error, code}` envelope (no `add_exception_handler` anywhere; raw 500s leak) | Improvement | S | Backend | — |
| SIM-417 | Data-freshness/health API surface for the UI (freshness/error tables exist but no endpoint → UI can't warn "profiles stale" / NULL venue) | Feature | S | Data | — |
| SIM-418 | Split slow tests into a dedicated CI lane (~15–16 `@pytest.mark.slow` run in the default unit lane at `--timeout=30`; coverage-timeout flake risk) | Chore | S | QA | — |
| SIM-419 | Harden DuckDB profile-rebuild index recreate (best-effort WARN today → silent index loss after the ART-on-DELETE workaround) | Reliability | S | Data | — |
| SIM-420 | OpenAPI typed-client generation for the frontend (WS events + new aggregates aren't in `/openapi.json`; FE would hand-write types) | Improvement | S | Backend + QA | SIM-383, SIM-385 |

---

## Suggested Phase 6 sprint sequence (6 weeks)

**Sprint 1 — Kickoff gates (P0 foundation + contracts).** SIM-378 (ADR) → SIM-379/380/381 (scaffold + design system + serving) in parallel with the backend-contract gates SIM-382 (backfill), SIM-383/384 (games enrich + aggregate), SIM-387 (⚠ calibration wiring), SIM-389 (auth), SIM-385 (WS schema). These unblock *everything* — nothing buildable should start before SIM-378.

**Sprint 2 — Cards + linescore/field + live read.** SIM-386 (live state) → SIM-391 (Day Summary + 3-state cards), SIM-392 (linescore + field graphic). Backend SIM-388 (multi-sub) + SIM-390 (prop edges) land here to unblock later UI.

**Sprint 3 — Game page + boxscore.** SIM-393 (game page + WS), SIM-394 (boxscore + distributions). SIM-420 (typed client) supports both.

**Sprint 4 — Betting surface + override v1.** SIM-395 (betting card), SIM-396 (CLV chart), SIM-397 (override v1). Betting prerequisites SIM-405 (real odds) + SIM-406/407 (calibration/validation) run in parallel on the data/ML track.

**Sprint 5 — Override v2 + perf/verification.** SIM-398 (staged override), SIM-402/403/404 (real-DB SLA + concurrency + stress), SIM-408/409 (DuckDB profiles + lineup ingestion).

**Sprint 6 — Hardening + deploy + close.** SIM-399/400/401 (a11y + Playwright + frontend CD), SIM-410–419 (p95, realism, reconciliation, pagination, error envelope, freshness, slow-test lane, DuckDB hardening), staging bring-up burns down the live-env debt.

## Critical path
SIM-378 → SIM-379/380/381 (foundation) + SIM-382/383/384/385/387/389 (contracts) → SIM-386 → SIM-391/392 → SIM-393/394 → SIM-395/396/397 → SIM-398. The data/ML/perf prerequisite track (SIM-402–409) runs alongside and must be **live-env verified** before the betting + sim numbers it backs are shown to users.

## Notes
- **Override UI is the project's largest single UI risk** — split into v1 (single-sub, ships early) and v2 (staged queue, the SIM-128 build) so a usable override exists before the hard part lands.
- **Live-environment verification debt** (carried from Phase 5): SIM-402 (SLA), SIM-405 (real odds), SIM-406 (fitted calibration), SIM-408 (DuckDB profiles) + a full `docker compose up` of nginx+app+monitoring. These are *verification*, not greenfield — the code is complete and unit/mock-tested.
- **Realism (SIM-411/412/413)** is the highest-value modeling work: the frontend will visualize score/win distributions that are currently park-blind and home-field-flat.
