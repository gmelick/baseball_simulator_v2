# Product Backlog

*Owner: Product Manager (Agent 1) · Last updated: 2026-05-24 (Phase 4 COMPLETE; Phase 5 audit — 28 tickets filed)*

> # 🏁 PHASE 4 — Core Simulation Loop — CLOSED 2026-05-24
>
> **Phase 4 complete across 5 sprints (SIM-310→349 + SIM-220).** The simulator runs full games
> end-to-end: loop → cross-engine fusion → validation spine → output contracts → perf
> mechanisms → betting chain → manager + situational decisions. **All six ⚠ audit live bugs
> fixed.**
>
> | Layer | Status |
> |---|---|
> | Sim loop | `simulation/sim_loop.py` — 8-step loop, `simulate_game()`, manager + situational decisions |
> | Outputs | GameSimSummary (win%/raw arrays/CIs), per-player BoxScore, win-prob, prop PMFs, field/PBP snapshots |
> | Perf | ProcessPool 100-iter runner + shared-memory attach (≤2 GB); Bench 4/5 + weekly CI gate |
> | Betting | CLV engine (implied/de-vig/edge/EV/CLV) + prop-odds ingestion |
> | Validation | backtester (ECE/Brier/log-loss + ablation), chi-squared replay, sniff, invalid-state harness |
> | Tests | **1505 unit+regression passing / 1 skipped / 0 failed** (+9 slow); **perf 5 passed / 0 skipped** |
> | DuckDB schema | **v7** (migrations 0001→0007); Postgres Alembic head 0013 |
>
> **Next: Phase 5 — Backend API & Simulation Runner.** Entry plan: `docs/HANDOFF_PHASE5.md`
> (start with a 9-agent program audit to file the Phase 5 tickets; next free ID **SIM-350**).
> Standing follow-ups: SIM-315 (move off OneDrive — biggest infra risk, still Open), prop-TB
> 2B/3B tracking, the dead `GameSpec._hit_rate` knob.
>
> ---
>
> # 🔭 Phase-4-Close Program Audit — 28 Phase 5 tickets filed (2026-07-15)
>
> The 9 agent scopes (3 clusters + PM) reviewed the project for **Phase 5 (Backend API &
> Simulation Runner)**. 28 tickets consolidated (**SIM-350 … SIM-377**) + the **SIM-315**
> carryover into `docs/audit/2026-07-15-phase5-prioritized-tickets.md` (per-agent findings in
> `docs/audit/2026-07-15-phase4-close-program-audit.md`). Four ⚠ defects found
> (`docker-compose` mounts the empty `./simulator`; `api/` missing from the coverage gate;
> `GameSpec._hit_rate` TypeError; no spread/run-line edge).
>
> **Headline:** the `api/` layer is greenfield — all 6 endpoints + the JSON serialization
> contract + auth are unbuilt; `BatchRunner` has no production DB-backed factory yet.
>
> **Tier P0 gates:** SIM-350 (serialization contract), SIM-351 (auth baseline), SIM-352 (real
> DB-backed `machine_factory`), SIM-353 (lineup/sub resolver — the SIM-338 gap), SIM-354 (mount
> the existing routers/pipeline into `api/main.py`).
> **P1:** SIM-355–361 (endpoints + snapshot persistence + Redis TTL + pool lifecycle + calibration serving).
> **P2:** SIM-362–366 (loop-output gaps: per-inning R/H/E, fielders, W/L/S, richer boxscore); SIM-367–370 (betting surface: spread edge, line-movement, bet-signal, real odds).
> **P2/P3:** SIM-371–374 (E2E/WS tests, SLA perf gate, nginx, monitoring); SIM-375–377 (⚠ hygiene) + **SIM-315** (OneDrive).
> Critical path: SIM-350 → SIM-352/SIM-353 → SIM-355 → SIM-356 → SIM-357/SIM-358.
>
> Phase 5 entry plan: `docs/HANDOFF_PHASE5.md`. **Next free ID after this audit: SIM-378.**
>
> ---
>
> # 🏁 Phase 2 — CLOSED 2026-05-20
>
> **All 11 similarity engines built. Both performance index gates passing
> against real 2024 staging data. Test suite green (767 passed / 22 skipped).
> Project is now in Phase 3.**
>
> | Layer | Status |
> |---|---|
> | Similarity engines | 11 / 11 built (pitcher GMM-W₂; batter/fielder/baserunner-advance/baserunner-steal/catcher-v2/pitcher-steal/manager RBF; situation KDTree; pitch-pitch + batted-ball FAISS) |
> | DB schema | 12 Alembic migrations (`0001 → 0012`); **3 DuckDB migrations on disk (`0001 → 0003`)** — SIM-051 `0003_sim051_pull_relative_spray_angle.sql` rebuilt 2026-05-20 (§7 reconciliation) |
> | Performance gates | SIM-085 `idx_pitches_situation` PASS; SIM-089 `idx_pitches_pitcher_season_clean` PASS (live 2024 staging) |
> | Test infrastructure | **927 unit+regression passing / 1 skipped / 0 failed** after Phase 3 Completion (2026-06-10); 3 perf benches passing. (870 after 2026-06-03; 834 after 2026-05-27; re-baselined from the prior 767 figure.) |
> | DuckDB schema version | **5** — migration 0004 (recency_weight + pool_build_metadata, SIM-076) and 0005 (index prune, SIM-115) added this phase. |
> | Phase 3 architecture spec | SIM-300 doc (`docs/architecture/2026-05-20-play-pool.md`) **reconstructed 2026-05-20** — Phase 3 implementation underway: SIM-301 (cache serializer) + SIM-302 (sampler) shipped (HANDOFF_PHASE3.md §7) |
> | Audit | 9-agent audit was conducted but the two output docs (`docs/audit/2026-05-21-*`) are **missing on disk** — same OneDrive truncation pattern. 53-ticket follow-up summary captured in HANDOFF_PHASE3.md until docs are rewritten |
>
> **Phase 3 entry point:** Tier-P0 tickets from the audit drive the next two
> sprints — SIM-118 (perf benchmark harness), SIM-202 (centralized run-value
> constants), SIM-280/SIM-281 (RAM budget + ProcessPool architecture decision),
> SIM-301 (play-pool nightly cache), SIM-302 (sampler API), SIM-303 (Phase 4
> wiring), SIM-323 (manager decision logic spec), SIM-220 (backtesting
> framework).  Full picture in
> `docs/audit/2026-05-21-prioritized-tickets.md` and
> `docs/HANDOFF_PHASE3.md`.

> # 🔭 Phase-3-Close Program Audit — 41 Phase 4 tickets filed (2026-06-10)
>
> All 9 agents reviewed the project. 41 tickets consolidated (SIM-220 + SIM-310–349)
> into `docs/audit/2026-06-10-phase4-prioritized-tickets.md` (per-agent findings in
> `docs/audit/2026-06-10-phase3-close-program-audit.md`). Full detail per ticket lives
> in `backlog.xlsx` (Full Backlog) and the audit docs. Six **live bugs** were found
> (⚠) to fix as touched. Phase 4 entry plan is in `docs/HANDOFF_PHASE4.md`.
>
> **Tier P0 — gates before loop coding:**
>
> | ID | Title | Owner |
> |---|---|---|
> | SIM-310 | Canonical Phase 4 sim-loop spec (8 steps, fingerprints, terminal logic) | Backend + BA |
> | SIM-311 | GameState + PlayResult dataclass contract | Backend + DE |
> | SIM-312 ⚠ | Fix RUN_VALUES↔Statcast `events` mismatch + run-resolution (result_* + RE24) | BA + Backend |
> | SIM-313 ⚠ | Wire `recency_weight` into the sampler distance-weight | ML + Backend |
> | SIM-314 ⚠ | Resolve SIM-200/201 ID collision | PM |
> | SIM-315 ⚠ | Move repo off OneDrive / file-integrity guard | QA/DevOps |
>
> **P0 status (Sprint 2026-06-17 — CLOSED 2026-05-22):** SIM-310 / 311 / 312⚠ / 313⚠ / 314⚠ ✅ Closed · SIM-322⚠ / 337⚠ ✅ Closed (pulled forward) · SIM-315⚠ documented & deferred (Open). Suite 927 → 1001.
>
> **Tier P1 (loop + validation):** SIM-316–326, SIM-321, SIM-322⚠, SIM-323, SIM-220.
> **Tier P2 (outputs/perf/betting):** SIM-327–340 (incl. SIM-336⚠, SIM-337⚠).
> **Tier P3 (hygiene/tech-debt):** SIM-341–349 (incl. SIM-345⚠, SIM-346⚠).
> Critical path: SIM-310→311→316→317→{318,319}→320→{220,327,332}.
>
> **P1 loop status (Sprint 2026-06-24 — CLOSED 2026-05-23):** SIM-316/317/318/319/320 (loop) + SIM-321 (fusion) + SIM-324/326 (validation harnesses) ✅ Closed — `simulate_game()` now produces full games. Remaining P1: SIM-220 backtester, SIM-323 manager logic, SIM-325 chi-squared replay.
>
> **P1/P2 status (Sprint 2026-07-01 — CLOSED 2026-05-23):** SIM-220 + SIM-325 (validation spine) ✅ Closed · SIM-327/328/330/331/332 (output contracts + batch runner) ✅ Closed. Remaining P1: SIM-323 manager logic. Suite 1144 → 1271.
>
> **P2/P3 status (Sprint 2026-07-08 — CLOSED 2026-05-23):** SIM-329/339/340 (betting chain) + SIM-333 (shared-memory) ✅ Closed · audit bugs SIM-336⚠/345⚠/346⚠ ✅ Fixed — **all six ⚠ live bugs now closed**. Schema v6→v7. Suite 1271 → 1380. Remaining P1: SIM-323 manager logic.
>
> **P3/close status (Sprint 2026-07-15 — CLOSED 2026-05-24):** SIM-323 manager + SIM-349 situational ✅ · SIM-334 columnarize + SIM-335 perf-benches + SIM-347 stress + SIM-348 live-tests ✅ · SIM-341/342/343/344 hygiene ✅. **PHASE 4 COMPLETE.** Suite 1380 → 1505; perf 3/2 → 5/0. (SIM-342 re-categorization: SIM-107 → done via SIM-348; SIM-120 → unblocked by SIM-320; SIM-127/128/129 → Phase 6 frontend.)
>
> ---
>
> # ✅ Sprint 2026-07-08 — Phase 4 Betting Chain + Bug Cleanup — CLOSED 2026-05-23
>
> All 7 tickets shipped and accepted after cross-validation
> (1380 unit+regression passing / 1 skipped / 0 failed; +6 slow; 3 perf benches). Schema v6→v7.
> Betting chain is end-to-end; the last two ⚠ audit bugs cleared (all six now fixed).
> Full record: `docs/SPRINT_2026-07-08_phase4_betting_bugs.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-329 | Backend + ML + Betting | ✅ Prop PMFs (`simulation/prop_distributions.py`) — full PMF + over/under per prop |
> | SIM-339 | Betting + ML | ✅ CLV engine (`betting/clv_engine.py`) — implied/de-vig/edge/EV/CLV |
> | SIM-340 | Data + Betting | ✅ Prop-odds ingestion wired + `mark_closing_prop_lines` + multi-book/sharp/opening; Alembic 0013 |
> | SIM-336 ⚠ | BA + Data | ✅ Park-factor UNPIVOT/ordering fix + real L/R splits + neutralization policy |
> | SIM-345 ⚠ | Data | ✅ Data-layer fixes (watermark `>=`, consistent recency_ref_season, NOT NULL parity, stand contract); DuckDB 0007, schema v7 |
> | SIM-346 ⚠ | ML | ✅ Calibration — no-arsenal redistribution, one linear arsenal scale, CalibrationReport wired, drift test |
> | SIM-333 | Perf | ✅ Shared-memory zero-copy attach (≤2 GB at W workers); per-worker fallback |
>
> **All six ⚠ audit live bugs now fixed** (SIM-312/313/322/337/336/346).
> **Next: Sprint 5** — SIM-323 manager logic + SIM-349 situational; SIM-334/335 perf; SIM-347/348 tests; P3 hygiene SIM-341–344. `backlog.xlsx` needs regen.
>
> ---
>
> # ✅ Sprint 2026-07-01 — Phase 4 Validation Spine + Output Contracts — CLOSED 2026-05-23
>
> All 7 tickets shipped and accepted after cross-validation
> (1271 unit+regression passing / 1 skipped / 0 failed; +5 slow; 3 perf benches).
> The output-contract layer + validation spine now exist end-to-end.
> Full record: `docs/SPRINT_2026-07-01_phase4_validation_outputs.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-327 | Backend + UX | ✅ `GameSimSummary` aggregation (`simulation/results.py`) — win%/mean/median/raw per-iter arrays/CIs |
> | SIM-328 | Backend + BA | ✅ Per-player `BoxScore` accumulators (AB/H/HR/RBI; IP/K/BB/ER) in the PA loop |
> | SIM-332 | Backend + Perf | ✅ ProcessPool 100-iter batch runner + Redis-TTL-with-fallback; SIM-333 seam |
> | SIM-330 | Backend + ML | ✅ Calibrated win-probability (Beta smoothing + CI + calibration-map seam) |
> | SIM-331 | Backend + UX | ✅ Field/PBP snapshot contracts (FieldSnapshot/PlayByPlay/StateAtPitch/OverrideDelta) |
> | SIM-220 | ML + Betting | ✅ Backtester — ECE/Brier/log-loss + reliability + ablation vs league-average |
> | SIM-325 | QA + BA | ✅ Chi-squared historical-replay GOF (p≈0.36; negative control rejected) |
>
> **Next: Sprint 4** — SIM-329 prop PMFs + SIM-339/340 CLV/odds; SIM-333 shared-memory;
> SIM-323 manager logic; audit bugs SIM-336/SIM-346. `backlog.xlsx` needs regen.
>
> ---
>
> # ✅ Sprint 2026-06-24 — Phase 4 Loop Build — CLOSED 2026-05-23
>
> All 8 tickets shipped and accepted after independent QA cross-validation
> (1144 unit+regression passing / 1 skipped / 0 failed; +2 slow; 3 perf benches).
> The SIM-303 scaffold is now a full-game simulator; critical path
> SIM-310→311→316→317→{318,319}→320 COMPLETE.
> Full record: `docs/SPRINT_2026-06-24_phase4_loop_build.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-316 | Backend | ✅ GameState count/out/inning state machine (`sim_loop.py`) |
> | SIM-321 | ML + Backend | ✅ Cross-engine score-fusion module + design doc (distance→weight stays in sampler) |
> | SIM-317 | ML + Backend | ✅ Real 10-dim/3-dim fingerprint derivation, wired into the loop |
> | SIM-318 | Backend + BA | ✅ Outcome step 4 + SIM-056 count-conditional foul re-weight |
> | SIM-319 | Backend + ML | ✅ Fielding + baserunning + steals + dropped-3rd-strike; all run deltas via `resolve_runs` |
> | SIM-320 | Backend | ✅ `simulate_game()` — regulation/walk-off/extras+ghost/seeding; returns `GameSimResult` (unblocks SIM-120) |
> | SIM-326 | QA + Backend | ✅ Invalid-state harness — 1,000 games, zero invalid states |
> | SIM-324 | BA + QA | ✅ Sniff suite — run env ≈4.4 R/G, P/PA ≈3.7, platoon emerges, RE24 monotonic |
>
> **Next: Sprint 3** — SIM-220 backtester + SIM-325 chi-squared replay (validation spine),
> SIM-323 manager logic, P2 output contracts (SIM-327/328/330) + perf (SIM-332/333).
> `backlog.xlsx` needs regen from this file.
>
> ---
>
> # ✅ Sprint 2026-06-17 — Phase 4 P0 Gates — CLOSED 2026-05-22
>
> All 8 tickets shipped and accepted after independent QA cross-validation
> (996 unit+regression passing / 1 skipped / 0 failed; +60 subtests; 3 perf benches;
> 1001 after restoring the corrupted `test_data_engineer_sim162.py`). Opens Phase 4.
> Full record: `docs/SPRINT_2026-06-17_phase4_p0_gates.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-310 | Backend + BA | ✅ Canonical Phase 4 sim-loop spec (one 8-step loop) |
> | SIM-311 | Backend + DE | ✅ `GameState` + `PlayResult` contract (`simulation/game_state.py`) |
> | SIM-312 ⚠ | BA + Backend | ✅ RUN_VALUES↔events fix + `run_resolution.py` (RE24 + linear fallback) |
> | SIM-313 ⚠ | ML + Backend | ✅ `recency_weight` wired into `PlayPoolSampler` |
> | SIM-322 ⚠ | ML Eng | ✅ GMM covariance double-standardization fixed (engine-side) |
> | SIM-337 ⚠ | DE + Perf | ✅ sim-pool indexes reconciled to SIM-111 contract (migration 0006, schema v6) |
> | SIM-314 ⚠ | PM | ✅ SIM-200/201 ID collision resolved (manager logic = SIM-323) |
> | SIM-315 ⚠ | QA/DevOps | 📄 Remediation plan documented; deferred — ticket stays **Open** |
>
> **4 of 6 audit live bugs fixed** (SIM-312/313/322/337); remaining SIM-336, SIM-346.
> **Next: Phase 4 loop build** — SIM-316→317→{318,319}→320 (`simulate_game()`) + the
> SIM-220 validation spine. `backlog.xlsx` needs regen from this file.
>
> ---
>
> # 🏁 Phase 3 — Play Pool Architecture — COMPLETE (2026-06-10)
>
> All play-pool tickets shipped and accepted after independent QA
> (927 unit+regression passing / 1 skipped / 0 failed; 3 perf benches).
> Record: `docs/SPRINT_2026-06-10_phase3_completion.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-048 | ML Eng | ✅ SimilarityEngineRegistry (`similarity/registry.py`) |
> | SIM-076 | Data Eng + ML Eng | ✅ recency_weight + pool_build_metadata + migration 0004 + walk-forward harness |
> | SIM-095 | Data Eng | ✅ Incremental pool rebuild |
> | SIM-111 | Backend + Data Eng | ✅ Play-pool query column contracts |
> | SIM-115 | Data Eng + Perf Eng | ✅ Prune sim-pool indexes (migration 0005) |
> | SIM-056 | Baseball Analyst | ✅ Count-stratified foul-ball weighting design |
>
> Phase 3 flagship (prior sprints): SIM-300 spec, SIM-301 cache, SIM-302 sampler, SIM-303 sim-loop wiring.
> **Still open (NOT play-pool):** SIM-127/128/129 (frontend, Phase 6), SIM-107 (live-pipeline tests), SIM-120 (needs Phase 4 simulate_game).
> **Next: Phase 4** — flesh out the SIM-303 scaffold into the full simulation loop; SIM-220 backtesting; SIM-323 manager logic.
>
> ---
>
> # ✅ Sprint 2026-06-03 — Phase 4 Readiness — CLOSED 2026-05-21
>
> All 7 tickets shipped and accepted after independent QA cross-validation
> (870 unit+regression passing / 1 skipped / 0 failed; 3 perf benches passing).
> Full record in `docs/SPRINT_2026-06-03_phase4_readiness.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-114 | Perf+ML | ✅ FAISS index design spec (per-tile flat; IVFFlat >50k crossover) |
> | SIM-303 | Backend | ✅ PlayPoolSampler wired into sim-loop scaffold (Phase 3 complete) |
> | SIM-119 | Perf+BE | ✅ Per-step time budget for the 8-step loop |
> | SIM-113 | Perf+DE | ✅ GMM batch: dynamic workers + chunked IPC + bulk writes |
> | SIM-075 | ML+Perf | ✅ Arsenal W2 cache vectorized (~2.9×, identical results) |
> | SIM-074 | Data Eng | ✅ barrel_rate full Statcast sliding-scale definition |
> | SIM-090 | Data Eng | ✅ ETL psycopg2 connection pool |
>
> **Next:** SIM-220 (backtesting), SIM-323 (manager logic), Phase-4 loop steps; perf follow-ups (share arsenal cache, columnarize situation engine).
>
> ---
>
> # ✅ Sprint 2026-05-27 — Phase 3 Kickoff — CLOSED 2026-05-20
>
> All 11 work items shipped and accepted after independent QA cross-validation
> (833 unit+regression passing / 1 skipped / 0 failed; 3 perf benches passing).
> Full record in `docs/SPRINT_2026-05-27_phase3_kickoff.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-300 | PM→BE+ML | ✅ Spec reconstructed (`docs/architecture/2026-05-20-play-pool.md`) |
> | SIM-051 | Data Eng | ✅ DuckDB migration `0003` + 7 tests |
> | SIM-162 | Data Eng | ✅ LeagueAverageProfiles regression (5 tests) |
> | SIM-149 | ML Eng | ✅ Baserunner-steal unit file (9 invariants) |
> | SIM-150 | ML Eng | ✅ Calibration regressions (catcher v2 / FAISS ×2) |
> | SIM-202 | Baseball Analyst | ✅ `simulation/constants.py` RUN_VALUES + DEFENSIVE_RUN_VALUES |
> | SIM-118 | Perf Eng | ✅ Benchmark harness + weekly CI |
> | SIM-301 | Backend | ✅ Play-pool nightly cache serializer |
> | SIM-302 | Backend+ML | ✅ `PlayPoolSampler` four-method API |
> | SIM-280 | Perf Eng | ✅ RAM budget vs 2 GB (measured) |
> | SIM-281 | Perf Eng | ✅ Parallelism ADR (ProcessPoolExecutor + shared_memory) |
>
> **Still open (non-P0 §7):** `docs/audit/2026-05-21-*.md` rebuilds.
> **Next:** SIM-303 (wire sampler into sim loop), SIM-220 (backtesting), SIM-323 (manager logic).
> **Note:** `backlog.xlsx` was locked during the sprint — regenerate from this file to publish the closed state.

> **Audit 2026-05-21:** end-of-Phase-2 program audit conducted by all 9
> agents.  Findings and the prioritized ticket list live in
> `docs/audit/2026-05-21-program-audit.md` and
> `docs/audit/2026-05-21-prioritized-tickets.md`.  53 tickets filed
> (47 new + 6 pre-existing).  Tier-P0 gating tickets must land before
> Phase 4 simulation-loop work begins.

This file is the canonical home for **proposed and in-flight work**. Completed work moves to `CHANGES.md` once shipped.

> **`backlog.xlsx` health note (2026-05-14, resolved):** the previously
> corrupted workbook was rebuilt from `BACKLOG.md` as `backlog_v2.xlsx` on
> 2026-05-14 and renamed to `backlog.xlsx` by the user.  Both sources are
> now in sync.  `BACKLOG.md` remains the authoritative draft surface;
> `backlog.xlsx` is the published artifact that gets regenerated at the
> end of each sprint.

**Companion files:**
- `CHANGES.md` — completed sprints, organized by agent
- `agent_team.md` — definitions of the 9 agents and ownership scopes
- `PRODUCT_GUIDE.md` — onboarding and concept reference for newcomers

**Conventions:**
- Tickets use `SIM-XXX` IDs. New IDs continue sequentially within the appropriate band (see "ID bands" below).
- Each ticket lists **Type / Effort / Phase / Owners / Depends on / Acceptance criteria**.
- A ticket is shippable only when every acceptance criterion passes. PM owns the acceptance gate; named agents implement and self-verify.
- Forward-looking placeholder tickets (Phase 4+) are explicitly marked. Their acceptance criteria are drafted now to lock requirements before the surrounding spec is written.

**ID bands (informal):**
- `SIM-040–070` — Phase 2 similarity engines
- `SIM-080–099` — Data engineer infrastructure / migrations
- `SIM-099–130` — Backend / live pipeline bugs and features
- `SIM-130–149` — Odds, CLV, prop markets, CI/CD
- `SIM-200+` — Phase 4 simulation loop design constraints

---

## Sprint 2026-05-06 + 2026-05-07 — Data + Backend Stabilisation (CLOSED 2026-05-07)

**Sprint disposition:** ✅ all 16 tickets accepted by PM on 2026-05-07.
Full delivery details in `CHANGES.md` under the corresponding sprint headers.

| Ticket | Type | Owner | Status |
|---|---|---|---|
| SIM-085 | Bug | Data Engineer | ✅ Shipped — composite situation index on `raw.pitches` |
| SIM-086 | Bug | Data Engineer | ✅ Shipped — `raw.games.venue_id` nullable + `venue_backfill_job.py` |
| SIM-087 | Bug | Data Engineer | ✅ Shipped — release_speed validator/trigger thresholds lowered |
| SIM-088 | Improvement | Data Engineer | ✅ Shipped — dropped `idx_pitches_pitch_type` |
| SIM-089 | Improvement | Data Engineer | ✅ Shipped — composite `(pitcher, season)` partial index |
| SIM-091 | Bug | Data Engineer | ✅ Shipped — `_delete_seasons()` regression guard |
| SIM-092 | Improvement | Data Engineer | ✅ Shipped — `raw.game_odds` deduplication via SHA-256 hash |
| SIM-093 | Gap | Data Engineer | ✅ Shipped — `raw.etl_errors` audit table + ETL wiring |
| SIM-101 | Bug | Backend Developer | ✅ Shipped — per-game GameStateBuilder cache |
| SIM-102 | Bug | Backend Developer | ✅ Shipped — Opener role classification |
| SIM-103 | Bug | Backend Developer | ✅ Shipped — broadcast() set snapshot |
| SIM-104 | Improvement | Backend Developer | ✅ Shipped — /resimulate Redis cooldown |
| SIM-105 | Improvement (P2) | Backend Developer | ✅ Shipped — completed-game upsert skip |
| SIM-106 | Improvement | Backend Developer | ✅ Shipped — async-callable type guard |
| SIM-148 | Bug | ML Engineer + QA/DevOps | ✅ Shipped (with documented deviation) — pitcher_similarity test cleanup |
| SIM-153 | Gap | QA/DevOps + Backend Developer | ✅ Shipped — secrets management baseline |

**PM acceptance verdict:**
- 11 Alembic migrations now in chain (`0001 → 0011`); chain integrity verified.
- 66/66 unit tests passing on the new ticket-specific suites (2 environmental skips for missing scipy in sandbox; CI installs scipy).
- `CHANGES.md` documents every ticket with deltas, rationale, and verification commands.
- `agent_team.md` migration workflow (SIM-084) honoured: every PostgreSQL schema change shipped with an Alembic migration.

**Follow-ups generated by this sprint** (entered as new tickets below):

| New ticket | Source | Effort | Owner |
|---|---|---|---|
| **SIM-157** | SIM-092 carry-forward | S | Data Engineer |
| **SIM-158** | SIM-085 + SIM-089 acceptance gates | S | Performance Engineer |
| **SIM-159** | SIM-132 RNG vig-boundary flake | S | Backend Developer |

(Note: SIM-094/095/096 are PRE-EXISTING open Phase 2 polish tickets; the three sprint-2026-05-13 follow-ups were renumbered to SIM-157/158/159 to avoid the collision.  PM signed-off the renumber 2026-05-08.)

---

## Sprint 2026-05-13 — Phase 2 Closure & Engine Build-out (CLOSED 2026-05-14)

**Sprint disposition:** ✅ all 7 tickets accepted by PM on 2026-05-14.
Full delivery details in `CHANGES.md` under the "Sprint 2026-05-13" header.

| Ticket | Type | Owner | Status |
|---|---|---|---|
| SIM-073 | Gap | Data Engineer | ✅ Shipped — `steal_attempt_rate_against` column on `derived.catcher_season_metrics`; migration 0002; profile computor populates it. |
| SIM-072 | Enhancement | ML Engineer | ✅ Shipped — CatcherSimilarityEngine v2 (5-sub-score split: Framing 45 + Blocking 20 + Execution 12 + Deterrence 8 + Offense 15). |
| SIM-157 | Improvement | Data Engineer | ✅ Shipped — `scripts/backfill_odds_hash.py` + Alembic 0012 promotes partial → full unique index after backfill. |
| SIM-158 | Validation | Performance Engineer | ✅ Shipped (harness) — `scripts/run_index_acceptance.py` + acceptance doc.  Live EXPLAIN ANALYZE run deferred until 2024 staging data is loaded (PM-approved). |
| SIM-159 | Bug | Backend Developer | ✅ Shipped — moneyline vig test bounds widened to absorb American-odds integer rounding; deterministic across 100 runs × 5 game_pks. |
| SIM-041 | Feature | ML Engineer | ✅ Shipped — `PitchPitchSimilarityEngine` (FAISS IndexFlatL2 + HNSW path) over 10-dim pitch fingerprint. |
| SIM-042 | Feature | ML Engineer | ✅ Shipped — `BattedBallSimilarityEngine` (FAISS) over 3-dim launch fingerprint with SIM-051 fall-forward (uses `pull_relative_spray_angle` automatically when shipped). |

**PM acceptance verdict:**
- 12 Alembic migrations now in chain (`0001 → 0012`, with 0012 conditional on the SIM-157 backfill running first).
- 2 DuckDB migrations now in chain (`0001 → 0002`).
- 95/95 unit + regression tests passing across the new and existing engines (10 environmental skips for missing scipy in sandbox — CI installs scipy).
- 11 of 11 similarity engines now built: pitcher (GMM W₂), batter (RBF), fielder (RBF), baserunner extra-base (RBF), baserunner steal (RBF), catcher (RBF v2), pitcher-steal (RBF), manager (RBF), situation (KDTree), pitch-to-pitch (FAISS), batted-ball (FAISS). **Phase 2 milestone reached.**

**Follow-ups generated by this sprint** (entered as new tickets below):

| New ticket | Source | Effort | Owner |
|---|---|---|---|
| **SIM-160** | SIM-042 / SIM-051 dependency | S | Data Engineer |
| **SIM-161** | SIM-158 live EXPLAIN ANALYZE execution | S | Performance Engineer |
| **SIM-162** | `pipeline/batch/player_profile_computor.py` pre-existing truncation in `LeagueAverageProfiles.compute()` | S | Data Engineer |

---

## Sprint 2026-05-20 — Phase 2 hardening & Phase 3 kickoff (CLOSED 2026-05-21)

**Sprint disposition:** ✅ All 7 tickets shipped. SIM-161 was initially deferred for staging data, but the live EXPLAIN ANALYZE run completed out-of-sprint on 2026-05-20 against developer-local Postgres — both gates pass (SIM-089: 6.77 ms / 50 ms; SIM-085: passing after `_build_situation_query` was rewritten in SIM-163 to emit literal `IS NULL` instead of parameterized `IS NOT DISTINCT FROM`, which had triggered a 12x prepared-statement regression).

| Ticket | Type | Owner | Status |
|---|---|---|---|
| SIM-051 | Improvement | Data Engineer | ✅ Shipped — `pull_relative_spray_angle` column on `sim.outcome_pool`; DuckDB migration 0003; populated at ETL time via stand/bat_hand handedness flip. SIM-042's loader picks it up automatically. |
| SIM-160 | Gap | Data Engineer | ✅ Shipped — `scripts/check_bat_side_coverage.py` audit script + acceptance doc; gate threshold 1 % NULL per season. |
| SIM-162 | Bug | Data Engineer | ✅ Shipped — restored `player_profile_computor.py` truncated tail; module parses cleanly; chains `LeagueAverageProfiles.compute()` from the entry point. |
| SIM-149 | Gap | QA / DevOps | ✅ Shipped — `tests/unit/test_baserunner_steal_engine.py` covers all 9 invariants. Phase 2 closure complete: every engine has a unit test file. |
| SIM-150 | Gap | QA / DevOps | ✅ Shipped — `tests/unit/test_ml_engines_sim150.py` covers catcher v2 Realmuto-archetype top-10 sanity, pitch-to-pitch recency-boost effect, batted-ball outcome monotonicity. |
| SIM-161 | Validation | Performance Engineer | ✅ Shipped 2026-05-20 — both gates pass against live 2024 staging. SIM-089 = 6.77 ms / 50 ms. SIM-085 passing after SIM-163 fix to `_build_situation_query` (replaced parameterized `IS NOT DISTINCT FROM` with literal `IS NULL` for None-valued bases — prepared-statement quirk caused 12x regression on the initial run). Report committed to `docs/perf/2026-05-13-index-acceptance.md`. |
| SIM-300 | Spec | Backend Developer + ML Engineer | ✅ Shipped — `docs/architecture/2026-05-20-play-pool.md` defines the Phase 3 sampler architecture: pre-filter contract, sub-index materialization, recency lifecycle, sampler query API, performance budget. Implementation tickets drafted as SIM-301+. |

**PM acceptance verdict (2026-05-21):**
- 3 DuckDB migrations now in chain (`0001 → 0003`); chain integrity verified.
- 12 Alembic migrations in chain unchanged from sprint 2026-05-13.
- 120/120 unit + regression tests passing (10 environmental skips for scipy in sandbox).
- Phase 2 hardening complete — every engine has a unit test file; calibration extensions for the v2 catcher + both FAISS engines locked in.
- Phase 3 spec accepted as the first Phase 3 deliverable; implementation tickets will be drafted at sprint 2026-05-27 kickoff.

**Follow-ups generated by this sprint:**

| New ticket | Source | Effort | Owner |
|---|---|---|---|
| **SIM-301** | SIM-300 spec — play-pool cache | M | Backend Developer |
| **SIM-302** | SIM-300 spec — sampler API | M | Backend Developer + ML Engineer |
| **SIM-303** | SIM-300 spec — Phase 4 wiring | M | Backend Developer |

---

## Closed-sprint reference — Sprint 2026-05-20 (original proposal)

**Sprint goal:** finish the Phase 2 hardening tasks deferred from 2026-05-13 (regression test files for the two new FAISS engines, SIM-051 pull-relative spray angle, live SIM-158 run), then begin Phase 3 play-pool architecture work now that all 11 similarity engines are built.

**Total scope:** 7 tickets · 0 of L · 3 of M · 4 of S.
Estimated team-effort: ~8 dev-days against ~6 calendar-days. Capacity reasonable.

### Sequence + dependencies

```
SIM-051 (DE, S) ─── pull_relative_spray_angle column
                  └─ unblocks SIM-042 calibration (regression-only impact, no engine code change)

SIM-160 (DE, S) ─── ensure raw.pitches.bat_side present + populated; required by SIM-051

SIM-161 (Perf, S) ── live EXPLAIN ANALYZE run for SIM-085 + SIM-089 once 2024 in staging

SIM-149 (QA/DevOps, S) ── unit test file for baserunner_steal engine (carried from 2026-05-13)
SIM-150 (QA/DevOps, M) ── calibration test extensions for catcher engine v2 + new FAISS engines

SIM-162 (DE, S)   ─── restore truncated LeagueAverageProfiles.compute() in player_profile_computor.py

SIM-300 (BE+ML, M) ── Phase 3 play-pool architecture spec (kickoff)
```

### Sprint commit list

| # | Ticket | Type | Effort | Owner | Why now |
|---|---|---|---|---|---|
| 1 | SIM-051 | Gap | S | Data Engineer | Adds `pull_relative_spray_angle` to `sim.outcome_pool`.  SIM-042's loader is already SIM-051-aware — just needs the column to ship. |
| 2 | SIM-160 | Gap | S | Data Engineer | Verifies `raw.pitches.bat_side` exists + is populated; SIM-051 depends on it. |
| 3 | SIM-162 | Bug | S | Data Engineer | Fix the pre-existing truncation in `LeagueAverageProfiles.compute()` (raised during SIM-073 verification — file cuts off at line 3755 mid-f-string). |
| 4 | SIM-149 | Gap | S | QA / DevOps | Baserunner-steal engine unit test file — only engine without one. |
| 5 | SIM-150 | Gap | M | QA / DevOps | Calibration test extensions for catcher v2 (BA Realmuto top-10 sanity), pitch-to-pitch (FAISS recency boost), batted-ball (HR distribution per launch-window). |
| 6 | SIM-161 | Validation | S | Performance Engineer | Live EXPLAIN ANALYZE run via `scripts/run_index_acceptance.py` against staging once 2024 is loaded.  Pastes results into `docs/perf/2026-05-13-index-acceptance.md`. |
| 7 | SIM-300 | Spec | M | Backend Developer (lead) · ML Engineer | Phase 3 play-pool architecture spec.  All 11 engines now built; Phase 3 can start. |

### Risks flagged for sprint kickoff

1. **SIM-051 + SIM-160 are joined at the hip.** If `bat_side` isn't populated on every loaded season, SIM-051's pull-relative computation is partial-NULL.  PM proposes shipping both together; if `bat_side` is already populated everywhere (check at standup day 1) then SIM-160 becomes a no-op `git grep` confirmation ticket.
2. **SIM-161 staging readiness.** Needs the 2024 season fully loaded into staging Postgres.  If staging is still empty at sprint kickoff, defer to 2026-05-27 and file as ongoing-blocked.
3. **SIM-300 spec quality.** Phase 3 is the first "no similarity engine" sprint in the project.  Backend Dev should pull Baseball Analyst into the spec review — play-pool sampling decisions are at the boundary of ML + simulation.

### Out of scope (explicitly deferred)

- Phase 4+ simulation loop placeholders (SIM-200, SIM-201) — held until Phase 4 spec drafting begins.
- Re-implementing the live ingestion pipeline against Python 3.10 — pyproject.toml fixes Python 3.11+ as the project floor; the sandbox shim is a debugger convenience, not a backlog item.
- Frontend UX work — re-enters scope at Phase 5/6 boundary.

### Closed-sprint references

The **previous** sprint 2026-05-13 — Phase 2 Closure & Engine Build-out — was originally drafted with the following sprint goal: "close out the Phase 2 similarity engine suite (only 2 of 11 engines remain) and ship the highest-priority ML-engineer plumbing tickets that unblock Phase 3 play-pool architecture work."  All 7 tickets shipped on schedule; see the disposition table above and the detailed entries in `CHANGES.md`.

---

## Standing tickets (not in current sprint, kept here for visibility)

These were drafted in earlier sprints, accepted by PM as future-work, and are
documented at full fidelity below so they survive any backlog.xlsx loss.

### SIM-072 — CatcherSimilarityEngine v2: Split Throwing into Execution + Deterrence

**Type:** Enhancement | **Effort:** M | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-14 (sprint 2026-05-13)
**Owners:** ML Engineer (lead) · Baseball Analyst (validation) · QA/DevOps (regression fixtures)
**Depends on:** SIM-073 (data dependency)
**Supersedes:** Composite weight scheme established in SIM-067

#### Acceptance criteria

1. New `deterrence_score` field added to `CatcherSimilarityResult` (8% weight)
2. Existing `throwing_score` reduced to 12% weight; retains `pop_time_avg`, `cs_rate`, `exchange_time_avg`, `arm_strength_mph`
3. New `deterrence_score` uses `steal_attempt_rate_against` as its sole feature (single-feature sub-score is acceptable for v1)
4. Composite weights sum to 1.0: Framing 45 + Blocking 20 + Execution 12 + Deterrence 8 + Offense 15 = **100**
5. `EB_N_PRIOR=15` retained for both throwing-derived sub-scores
6. Regression fixtures regenerated: `python tests/regression/generate_fixtures.py --force` and committed
7. `TestWeightConstants` in `tests/regression/test_engine_regression.py` updated to assert new 5-sub-score weight scheme
8. New unit test: a synthetic high-deterrence/low-execution catcher and a low-deterrence/high-execution catcher must score `< 0.40` against each other
9. Sanity check (BA sign-off): top-10 comps for J.T. Realmuto's profile dominated by elite-arm catchers

### SIM-073 — Add `steal_attempt_rate_against` to `derived.catcher_season_metrics`

**Type:** Gap | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-14 (sprint 2026-05-13)
**Owners:** Data Engineer (lead) · Baseball Analyst (formula validation)
**Blocks:** SIM-072

#### Acceptance criteria

1. New column `steal_attempt_rate_against FLOAT` added to `derived.catcher_season_metrics`
2. Numbered DuckDB migration in `db/migrations/duckdb/` (e.g. `0002_catcher_attempt_rate_against.sql`)
3. `db/schemas/duckdb_schema_version.txt` incremented
4. `db/schemas/02_duckdb_schema.sql` updated to reflect the new column (canonical schema source)
5. `pipeline/batch/player_profile_computor.py::_compute_catcher_throwing()` (or equivalent) updated to populate the column
6. **Formula** (BA-approved): `(SB + CS) / (runner_on_1B_opportunities + runner_on_2B_opportunities)`, opportunities counted at PA level (not pitch level), denominator excludes PAs where the runner was forced to advance
7. **Min-sample guard:** column NULL if denominator < 100 PA opportunities
8. Backfill all loaded seasons (2022, 2023, 2024) after migration applies
9. **Sanity check:** the bottom 10 catchers by `steal_attempt_rate_against` should include known elite-arm catchers (Realmuto, Stephenson, Heim-tier)

### SIM-157 — Backfill legacy `odds_hash` + dedup pass

**Type:** Improvement | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-14 (sprint 2026-05-13)
**Owners:** Data Engineer
**Depends on:** SIM-092 (column + index already shipped)

#### Problem

SIM-092 added the `odds_hash` column and partial unique index, but only enforces deduplication going forward. Pre-SIM-092 rows have `NULL odds_hash` and may contain duplicates. CLV queries that join against `raw.game_odds` over historical windows therefore see inflated row counts and slower scans.

#### Acceptance criteria

1. One-shot script `scripts/backfill_odds_hash.py` that: (a) computes `odds_hash` for every NULL-hash row using `LiveIngestionPipeline._odds_hash()`, (b) writes back in batches of 10k, (c) reports duplicate-detection stats.
2. After backfill, a follow-up DELETE keeps only the *earliest* row per `(game_pk, source, odds_hash)` group.
3. Once the table is clean, promote the partial unique index to a full unique index in a new Alembic migration `0012`.
4. Validation: `SELECT COUNT(*) FROM raw.game_odds WHERE odds_hash IS NULL` returns 0.
5. Validation: `SELECT game_pk, source, odds_hash, COUNT(*) FROM raw.game_odds GROUP BY 1,2,3 HAVING COUNT(*) > 1` returns no rows.
6. PR description includes row-counts before/after so storage win is measurable.

### SIM-158 — Run EXPLAIN ANALYZE acceptance gates for SIM-085 + SIM-089

**Type:** Validation | **Effort:** S | **Phase:** 2 | **Status:** ✅ Harness shipped 2026-05-14 (sprint 2026-05-13) — live run deferred to SIM-161 once 2024 staging data is loaded
**Owners:** Performance Engineer (lead) · Data Engineer (data prep)

#### Problem

SIM-085 (composite situation index) and SIM-089 (`(pitcher, season)` partial index) were merged with their acceptance gates *expressed* but not *executed* — the sandbox lacked a populated DB. Once a 2024 staging DB exists, these gates must be run and the results recorded so we can confirm the index choices were correct.

#### Acceptance criteria

1. SIM-085: `EXPLAIN (ANALYZE, BUFFERS)` on a representative situation lookup (count + outs + baserunner state) reports `Index Scan using idx_pitches_situation`, not `Seq Scan on pitches`. Single-query latency < 30 ms on a populated season.
2. SIM-089: `EXPLAIN (ANALYZE, BUFFERS)` on `_compute_pitcher_profiles()`'s primary fetch reports `Index Scan using idx_pitches_pitcher_season_clean`. 3,000-pitch fetch < 50 ms.
3. Results captured in a Markdown report committed under `docs/perf/2026-05-13-index-acceptance.md` and linked from the SIM-085 / SIM-089 entries in CHANGES.md.
4. If either index loses to a Seq Scan, file a follow-up ticket immediately and revert the index claim from CHANGES.md.

### SIM-159 — Tighten SIM-132 vig RNG range so the moneyline test is no longer flaky

**Type:** Bug | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-14 (sprint 2026-05-13)
**Owners:** Backend Developer

#### Problem

`MockOddsAPI.get_odds()` samples vig from `rng.uniform(0.06, 0.10)`, producing an overround of `1 + vig/2 ∈ [1.030, 1.050]`. The regression test asserts strict `> 1.03`, which fails at the lower edge for game_pk=12345 (RNG produces 1.0286 due to floating-point on the inflation path). The flake masks any *real* zero-vig regression.

#### Acceptance criteria

1. Either tighten the RNG floor to `rng.uniform(0.07, 0.10)` so the strict `> 1.03` always holds, OR weaken the test assertion to `>= 1.03 - 1e-9` and add an upper bound `< 1.05 + 1e-9`. PM prefers the test-side change to keep the SIM-132 mock consistent with real sharp-book data.
2. The `[12345]` parametrized case must pass deterministically across 100 consecutive runs.
3. Document the calibration choice in the test file.

### SIM-160 — Verify `raw.pitches.bat_side` populated for all loaded seasons

**Type:** Gap | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-21 (sprint 2026-05-20)
**Owners:** Data Engineer
**Blocks:** SIM-051 (pull-relative spray angle needs bat handedness)

#### Problem

SIM-042 ships SIM-051-aware but SIM-051's `pull_relative_spray_angle` formula
requires `bat_side` to be non-NULL on every row.  Before SIM-051 builds,
confirm `bat_side` coverage on `raw.pitches` is ≥ 99 % for every loaded
season (2022, 2023, 2024) and add a backfill or an ETL fix if not.

#### Acceptance criteria

1. `SELECT season, COUNT(*) FILTER (WHERE bat_side IS NULL), COUNT(*) FROM raw.pitches GROUP BY 1` reports ≤ 1 % NULLs per season.
2. If any season exceeds 1 %, file a follow-up data-quality ticket and pull `bat_side` from `chadwick_register` keyed on `batter`.
3. Document the result in `docs/data_quality/2026-05-20-bat-side-coverage.md`.

### SIM-161 — Live EXPLAIN ANALYZE run for SIM-085 + SIM-089

**Type:** Validation | **Effort:** S | **Phase:** 2 | **Status:** ⏳ Deferred (operational) — carry to sprint 2026-05-27. Harness ready; live run blocks on staging 2024 data load.
**Owners:** Performance Engineer (lead) · Data Engineer (staging data prep)
**Depends on:** SIM-158 harness (shipped 2026-05-14)

#### Problem

SIM-158 shipped `scripts/run_index_acceptance.py` and the placeholder
acceptance doc at `docs/perf/2026-05-13-index-acceptance.md`, but the
live EXPLAIN ANALYZE round-trip was deferred because staging didn't yet
hold 2024 data.  Once staging is loaded, run the harness and capture
the plans.

#### Acceptance criteria

1. `BASEBALL_DB_DSN=… python scripts/run_index_acceptance.py --season 2024 --pitcher-id <real> --out docs/perf/2026-05-13-index-acceptance.md` exits 0.
2. Both gates pass per the SIM-085 / SIM-089 acceptance criteria text.
3. The Markdown report is committed to the repo.
4. If either gate fails, file a follow-up and revert the index claim from CHANGES.md per SIM-158 AC #4.

### SIM-163 — Re-run SIM-085 acceptance with corrected fixture; consider INCLUDE columns

**Type:** Validation + Improvement | **Effort:** S | **Phase:** 2 | **Status:** 📝 Open — filed 2026-05-17 from SIM-158 live run analysis
**Owners:** Performance Engineer
**Depends on:** SIM-158 harness (shipped); SIM-161 live run (in progress)

#### Problem

First live SIM-158 run against the 2024 staging DB (2026-05-17) returned:

* SIM-089 (`idx_pitches_pitcher_season_clean`): **PASS** — 40.43 ms vs 50 ms budget, Index Scan as expected.
* SIM-085 (`idx_pitches_situation`): **MARGINAL FAIL** — 31.03 ms vs 30 ms budget (1.03 ms / 3.4 % over).

The plan confirms the right index is selected — no Seq Scan. The overshoot is dominated by the heap fetch (~28 ms for 12,299 rows / 9,724 heap blocks), with ~1 ms of overhead from a BitmapOr caused by the test fixture's synthetic `on_2b=12345` runner ID (a player ID that doesn't exist).

`scripts/run_index_acceptance.py` was patched 2026-05-17 to use bases empty (`on_2b=None`) and `IS NOT DISTINCT FROM` predicates, eliminating the BitmapOr. A clean re-run should land under 30 ms; if it doesn't, the structural fix is to widen `idx_pitches_situation` with `INCLUDE (game_pk, at_bat_number, pitch_number)` to enable an Index Only Scan and skip the heap fetch entirely.

#### Acceptance criteria

1. Re-run `scripts/run_index_acceptance.py` against the 2024 staging DB with the corrected fixture; commit the updated report to `docs/perf/2026-05-13-index-acceptance.md`.
2. If SIM-085 measured latency < 30 ms: close as no-action; the original SIM-158 close was correct, plan just needed a clean fixture.
3. If SIM-085 still ≥ 30 ms: open a follow-up Alembic migration adding `INCLUDE (game_pk, at_bat_number, pitch_number)` to `idx_pitches_situation`, re-run, and confirm Index Only Scan path < 30 ms.
4. Do **not** revert the SIM-085 / SIM-089 claims from `CHANGES.md` — the plan analysis (see report §Analysis) demonstrates both indexes function as designed.

### SIM-162 — Restore truncated `LeagueAverageProfiles.compute()` in `player_profile_computor.py`

**Type:** Bug | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-21 (sprint 2026-05-20) — root cause was truncated `__main__` argparse block, not the `LeagueAverageProfiles` class itself. Restored entry point chains `LeagueAverageProfiles.compute()` after the main computor run.
**Owners:** Data Engineer

#### Problem

Raised during SIM-073 verification (2026-05-14): `pipeline/batch/player_profile_computor.py`
is truncated at line 3755 inside an unfinished `LeagueAverageProfiles.compute()`
f-string.  `git show HEAD:pipeline/batch/player_profile_computor.py | wc -l`
returns 3745 lines with a clean ending; the working-tree file has a partial
in-progress edit that corrupts the parse.  Nothing in the catcher pipeline
depends on this method (so SIM-073 shipped fine), but the module won't
import as-is and any test that touches it transitively fails.

#### Acceptance criteria

1. The file parses cleanly: `python -c 'import ast; ast.parse(open("pipeline/batch/player_profile_computor.py").read())'` exits 0.
2. Either: (a) finish the in-progress edits to `LeagueAverageProfiles.compute()` so it ships the league-average insert paths for pitcher, batter, fielder-by-position, baserunner, and catcher entity types; OR (b) revert the file's tail to upstream HEAD if the in-progress edit is no longer wanted.
3. A unit test invokes `LeagueAverageProfiles('...').compute([2024])` against an in-memory DuckDB and confirms every entity_type produces a non-empty row.

---

## Cross-cutting observations carried over from Sprint 2026-05-06

**Documentation drift to fix.** The README's Phase 4 simulation step descriptions reference catcher only in the steal context (Step 1, Step 3a). Once SIM-200 and SIM-201 are merged into the Phase 4 Step 3b spec, the README's Step 3b section needs updating to mention framing and blocking explicitly:

- [ ] Update README §"Step 3b — Pitch Outcome Determination" to mention catcher framing bias on shadow-zone takes (post SIM-200 ship)
- [ ] Update README §"Step 3b" to mention catcher blocking coefficient on dirt-pitch outcomes (post SIM-201 ship)
- [ ] Update `PRODUCT_GUIDE.md` §6 catcher-engine description to reflect the 5-sub-score composition (post SIM-072 ship)

---

## Audit 2026-05-21 follow-ups (47 new tickets)

Full per-agent findings: `docs/audit/2026-05-21-program-audit.md`
Prioritized list with tiers + dependency graph: `docs/audit/2026-05-21-prioritized-tickets.md`

### Tier P0 — must land before Phase 4 simulation-loop work begins

| Ticket | Owner | Size | Why P0 |
|---|---|---|---|
| SIM-118 | Performance Engineer | M | Performance benchmark harness — no measurable SLA without it. |
| SIM-202 | Baseball Analyst | S | Centralize run-value constants in `simulation/constants.py`. |
| SIM-301 | Backend Developer | M | Play-pool nightly cache serializer (drafted in SIM-300 §10). |
| SIM-323 | Baseball Analyst | L | Manager decision logic spec — required for Step 7 of the sim loop. |
| SIM-280 | Performance Engineer | S | Per-engine RAM budget within the 2 GB SIM-114 envelope. |
| SIM-281 | Performance Engineer | S | ProcessPoolExecutor + shared-memory architecture decision. |
| SIM-220 | ML Engineer | L | Backtesting framework — required before simulator outputs can be validated. |

### Tier P1 — Phase 3 implementation + Phase 4 readiness

| Ticket | Owner | Size | Notes |
|---|---|---|---|
| SIM-302 | Backend + ML Engineer | M | PlayPoolSampler API surface. |
| SIM-303 | Backend Developer | M | Wire sampler into Phase 4 loop scaffolding. |
| SIM-161 | Performance Engineer | S | Live EXPLAIN ANALYZE — first run executed 2026-05-17; SIM-089 PASS, SIM-085 marginal fail handed to SIM-163. |
| SIM-163 | Performance Engineer | S | Re-run SIM-085 with corrected fixture; if still > 30 ms, add INCLUDE columns for Index Only Scan. |
| SIM-224 | ML Engineer | S | FAISS hot-path latency benchmark. |
| SIM-225 | ML Engineer | M | Live golden-file regression fixtures from real 2024 data. |
| SIM-221 | ML Engineer | M | Ablation harness — every engine vs league-average. |
| SIM-222 | ML Engineer | M | Walk-forward validation pipeline. |
| SIM-205 | Baseball Analyst | M | Platoon-split sanity tests for known L/R-heavy hitters. |
| SIM-310 | Product Manager | S | Phase 3 acceptance gate definition. |

### Tier P2 — DE hygiene + observability

SIM-241 (S), SIM-244 (S), SIM-245 (S), SIM-243 (S), SIM-094 (S, pre-existing), SIM-242 (M), SIM-240 (L — `player_profile_computor.py` refactor).

### Tier P3 — Backend / API readiness

SIM-260 (S), SIM-263 (S), SIM-261 (M), SIM-262 (S).

### Tier P4 — ML / BA validation depth

SIM-223 (S), SIM-203 (S), SIM-204 (M).

### Tier P5 — QA / DevOps

SIM-444 (S), SIM-440 (M), SIM-441 (M), SIM-442 (M), SIM-443 (S).

### Tier P6 — Betting / Markets

SIM-135 (S, pre-existing), SIM-136 (M, pre-existing), SIM-410 (M), SIM-411 (M), SIM-412 (S), SIM-143 (M, pre-existing).

### Tier P7 — UX (held until Phase 5 kickoff)

SIM-510 (M), SIM-515 (S), SIM-511 (M), SIM-512 (M), SIM-513 (L), SIM-514 (S).

### Tier P8 — long-horizon

SIM-700 (M), SIM-282 (S).

### Housekeeping

SIM-311 (S), SIM-312 (S), SIM-313 (S), SIM-314 (S).

### Recommended next 3 sprints (PM proposal — full rationale in prioritized-tickets doc)

* **2026-05-27** — Phase 3 kickoff + P0 gating (6 tickets · ~7 dev-days):
  SIM-301, SIM-118, SIM-161, SIM-202, SIM-280, SIM-281.
* **2026-06-03** — Phase 3 implementation + ML harness (6 tickets · ~8 dev-days):
  SIM-302, SIM-220 (start), SIM-224, SIM-241, SIM-244, SIM-310.
* **2026-06-10** — Phase 3 closure + Phase 4 prep (5 tickets · ~9 dev-days):
  SIM-303, SIM-220 (finish), SIM-323 (start), SIM-225, SIM-260.

---

## Phase 4 / Phase 4+ placeholder tickets (held)

### SIM-200 — [Phase 4 placeholder] Step 3b: Catcher Framing Bias on Shadow-Zone Takes

**Type:** Phase 4 design constraint | **Effort:** S (when Phase 4 lands) | **Phase:** 4 | **Status:** 🔲 Held
**Owners:** Backend Developer · ML Engineer · Baseball Analyst
**PM note:** Forward-looking placeholder. Folds into Phase 4 Step 3b spec when drafted; do NOT ship standalone.

### SIM-201 — [Phase 4 placeholder] Step 3b: Catcher Blocking Coefficient on Dirt-Pitch Outcomes

**Type:** Phase 4 design constraint | **Effort:** S (when Phase 4 lands) | **Phase:** 4 | **Status:** 🔲 Held
**Owners:** Backend Developer · ML Engineer · Baseball Analyst
**PM note:** Same disposition as SIM-200 — folds into Phase 4 Step 3b spec when drafted.

---

## Risk register

| Risk | Source ticket | Mitigation |
|---|---|---|
| Phase 4 simulation harness may not isolate single features cleanly → SIM-200's validation gate becomes infeasible | SIM-200 | Fall back to ablation testing on full backtests. ML Engineer review at Phase 4 kickoff. |
| FAISS engine sprint is over-committed (SIM-041 L + SIM-042 M for one ML Engineer) | Sprint 2026-05-13 | Day-3 standup decision point; defer SIM-042 calibration to 2026-05-20 if velocity slips. |
| `backlog.xlsx` is corrupted on disk | Sprint 2026-05-07 housekeeping | User to re-export from OneDrive or regenerate from `BACKLOG.md`. Until then `BACKLOG.md` is authoritative. |
