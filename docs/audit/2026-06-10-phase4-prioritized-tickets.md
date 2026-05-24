# Phase 4 — Prioritized Ticket List (from the Phase-3-close program audit)

*Author: Product Manager (Agent 1) · 2026-06-10 · Companion to `2026-06-10-phase3-close-program-audit.md`*

41 tickets consolidated from the 9-agent audit (deduped from ~60 raw proposals). IDs:
**SIM-220** (reused — the long-planned backtester) + **SIM-310 … SIM-349**. Tiers gate
Phase 4 sequencing. ⚠ = a bug that exists today (not just a Phase 4 feature).

Legend — Type: Feature / Bug / Gap / Spec / Design / Improvement / Test / Validation / CI / Chore / Infra.
Size: S (<1d) · M (3-5d) · L (1-2wk).

---

## Tier P0 — Gates (must land before / very early in Phase 4 loop coding)

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-310 | Author canonical Phase 4 simulation-loop spec — the authoritative 8 steps, fingerprint derivation, terminal/PA logic (reconciles README vs SIM-119) | Spec | M | Backend (lead) + Baseball Analyst | — |
| SIM-311 | Define `GameState` + `PlayResult` dataclass contract (mutable count/out/base/score/inning/lineup/manager context) | Spec | M | Backend + Data Eng | SIM-310 |
| SIM-312 | ⚠ Fix RUN_VALUES↔Statcast `events` vocabulary mismatch + run-resolution policy (sampled `result_*` deltas + RE24 primary; linear weights fallback) | Bug | M | Baseball Analyst + Backend | — |
| SIM-313 | ⚠ Wire `recency_weight` into PlayPoolSampler distance-weight (contract SIM-111 §8 says multiply; sampler currently ignores it) | Bug | S | ML Eng + Backend | — |
| SIM-314 | ⚠ Resolve SIM-200/SIM-201 ID collision; split manager-logic vs catcher framing/blocking into distinct IDs | Gap | S | Product Manager | — |
| SIM-315 | ⚠ Move repo off OneDrive (or add a pre-commit file-integrity / byte-count + `ast.parse` guard) — truncation is corrupting the working tree | Infra | M | QA/DevOps | — |

## Tier P1 — Core simulation loop + validation spine

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-316 | GameState manager + count/out/base/inning state machine + invalid-state guards | Feature | M | Backend | SIM-311 |
| SIM-317 | Real query-fingerprint derivation from game state via engines/registry (SIM-041 10-dim, SIM-042 3-dim) | Feature | M | Backend + ML Eng | SIM-311 |
| SIM-318 | Outcome determination step + SIM-056 count-conditional foul re-weight + two-strike-foul absorbing rule | Feature | M | Backend + Baseball Analyst | SIM-316, SIM-317 |
| SIM-319 | Fielding + baserunning resolution (RBF engines, `result_*` deltas) + steal decision/outcome from stolen_base_pool | Feature | L | Backend + ML Eng | SIM-316, SIM-317 |
| SIM-320 | Half-inning + game loop control + `simulate_game()` entry point (unblocks SIM-120) | Feature | M | Backend | SIM-318, SIM-319 |
| SIM-321 | Cross-engine score-fusion spec + module (combine 11 engines incl. catcher/fielder/manager into one per-pitch draw) | Design+Feature | L | ML Eng + Backend | SIM-310 |
| SIM-322 | ⚠ Fix GMM covariance double-standardization (mean original-units vs covariance standardized-twice) | Bug | S | ML Eng | — |
| SIM-323 | Manager decision-logic spec + module (pull/pinch-hit/bullpen-by-leverage/bunt from manager-similarity tendencies) — the real SIM-201 manager scope | Gap | L | Baseball Analyst + Backend | SIM-312 |
| SIM-220 | Backtesting framework — walk-forward PA-outcome eval with ECE / Brier / log-loss + reliability curves + ablation vs league-average baseline | Feature | L | ML Eng + Betting Analyst | SIM-320 |
| SIM-324 | Phase 4 baseball sniff-test suite (run env ~4.4 R/G, platoon splits emerge, P/PA 3.8-3.9, RE24 monotonic) | Validation | M | Baseball Analyst + QA | SIM-320 |
| SIM-325 | E2E integration test: replay historical games through the loop, chi-squared vs actual run distributions (p>0.05) | Test | L | QA/DevOps + Baseball Analyst | SIM-320 |
| SIM-326 | Invalid-state detection harness — 1,000 complete games, zero negative scores / >3 outs / impossible advances | Test | M | QA/DevOps + Backend | SIM-320 |

## Tier P2 — Output contracts, performance mechanisms, betting

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-327 | `GameSimResult` aggregation contract (win%, avg + **raw per-iteration** score arrays, `simulated_at`, confidence intervals — SIM-112) | Spec | M | Backend + UX | SIM-320 |
| SIM-328 | Per-player sim-average accumulator (AB/H/HR/RBI; IP/K/BB/ER) keyed by player_id, built into the PA loop | Feature | M | Backend + Baseball Analyst | SIM-327 |
| SIM-329 | Prop-distribution aggregator over N iterations — full PMF per prop (K/hits/HR/RBI/TB/ER/BB), not means | Feature | L | Backend + ML + Betting | SIM-327 |
| SIM-330 | Calibrated game win-probability output from sim runs | Feature | M | Backend + ML Eng | SIM-320 |
| SIM-331 | Field/baserunner state + pitch-level play-by-play snapshot contracts (BaseballFieldGraphic, `/plays`, `/state/{ab}/{pitch}`, override-delta) | Spec | M | Backend + UX | SIM-327 |
| SIM-332 | ProcessPoolExecutor 100-iteration batch runner (`min(CPU-1,10)`) + Redis sim(60s)/pool(5-min) TTL caching | Feature | M | Backend + Perf Eng | SIM-320 |
| SIM-333 | Shared-memory / mmap zero-copy attach for FAISS tiles + situation KDTree + RBF matrices (keeps ≤2 GB at W workers) | Feature | L | Perf Eng | SIM-332 |
| SIM-334 | Columnarize situation engine `_index_meta` + enforce read-only/lazy arsenal cache per worker | Improvement | M | Perf Eng + ML Eng | — |
| SIM-335 | Fill perf Bench 4/5 per-step micro-benches (assert SIM-119 budget under PERF_STRICT) + wire weekly CI perf/RAM gate | Validation | M | Perf Eng | SIM-320 |
| SIM-336 | ⚠ Park-factor application + pool-neutralization policy; fix `factor_overall` UNPIVOT bug + NULL L/R splits | Bug+Design | M | Baseball Analyst + Data Eng | — |
| SIM-337 | ⚠ Reconcile SIM-115 indexes with the SIM-111 query contract; add `stand`-bearing composites | Bug | S | Data Eng + Perf Eng | — |
| SIM-338 | Phase 4 lineup/substitution read path (DuckDB↔Postgres `game_lineups`/`lineup_state`) | Design | M | Data Eng + Backend | SIM-311 |
| SIM-339 | CLV engine — implied prob, de-vig/no-vig fair odds, edge (sim vs market), EV, CLV per market/prop | Feature | L | Betting Analyst + ML Eng | SIM-329, SIM-330 |
| SIM-340 | Real odds provider + prop ingestion (multi-book, sharp flag, cadence; wire `_persist_prop_odds`); opening-line capture (SIM-138) + `mark_closing_prop_lines` | Feature | M | Data Eng + Betting Analyst | — |

## Tier P3 — Hygiene / tech-debt

| ID | Title | Type | Size | Owner | Depends-on |
|---|---|---|---|---|---|
| SIM-341 | Reconcile README + PRODUCT_GUIDE to current state (engines built, registry shipped, `simulator/`→`simulation/`, loop ordering) | Gap | S | Product Manager | SIM-310 |
| SIM-342 | Re-categorize the open "Phase 3 Gate" rows (SIM-107/120/127/128/129) to their true phases/blockers | Improvement | S | Product Manager | — |
| SIM-343 | Add `simulation/` to coverage `--cov` scope + extend the 80% gate; unify CI Python to 3.11 across all jobs | CI | S | QA/DevOps | — |
| SIM-344 | Remove stray files (`*.clean`, `*.tmp`, `*_output.txt`) + extend `.gitignore` | Chore | S | QA/DevOps + Data Eng | — |
| SIM-345 | ⚠ Data-layer fixes: incremental watermark `>=` + row-count guard; consistent cross-pool `recency_ref_season`; `recency_weight` NOT NULL parity; `stand` vs `bat_hand` pool contract | Bug/Tech-debt | M | Data Eng | — |
| SIM-346 | ⚠ ML calibration fixes: pitcher no-arsenal ×1.0 no-op; arsenal_gamma(squared) vs ARSENAL_SCALE(linear); wire `CalibrationReport` into engine constants + drift regression test | Bug | M | ML Eng | — |
| SIM-347 | Stress test — 100 sims × 30 concurrent games, assert no races/leaks | Test | M | QA/DevOps + Perf Eng | SIM-332 |
| SIM-348 | SIM-107 live_ingestion_pipeline tests (remove coverage omit) + finish the SIM-152 shared conftest | Test | L | QA/DevOps + Data Eng | — |
| SIM-349 | Situational-decision module — IBB / sac bunt / sac fly / hit-and-run triggers | Design | M | Baseball Analyst | SIM-323 |

---

## Suggested first three sprints

- **Sprint 1 (P0 gates):** SIM-310, SIM-311, SIM-312, SIM-313, SIM-314, SIM-315 + start SIM-322/SIM-337 (quick bug fixes). Unblocks the loop.
- **Sprint 2 (loop build):** SIM-316 → SIM-320 (the loop), SIM-317, SIM-321; begin SIM-220 + SIM-324/SIM-325/SIM-326.
- **Sprint 3 (validate + outputs + perf):** finish SIM-220 + sniff/chi-squared/invalid-state; SIM-327/328/330 output contracts; SIM-332/333 perf mechanisms. Betting (SIM-329/339/340) and remaining P3 hygiene slot opportunistically.

**Critical path:** SIM-310 → SIM-311 → SIM-316 → SIM-317 → SIM-318/SIM-319 → SIM-320 →
{SIM-220, SIM-327, SIM-332}. The six ⚠ bugs (SIM-312/313/322/336/337/346) should be fixed as
they're touched, not deferred.
