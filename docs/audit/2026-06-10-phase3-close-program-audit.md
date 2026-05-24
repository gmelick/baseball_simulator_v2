# Program Audit — Phase 3 Close (looking ahead to Phase 4)

*Conducted 2026-05-21 (sprint date 2026-06-10) by all 9 agents · Author: Product Manager (Agent 1)*

This is the end-of-Phase-3 program audit. Each of the 9 agents reviewed the whole project
from their scope for gaps, bugs, and improvements relevant to Phase 4 (the core simulation
loop). Findings are recorded below; the consolidated, de-duplicated, prioritized ticket
list is in `docs/audit/2026-06-10-phase4-prioritized-tickets.md`. (This pair of docs also
finally fills the long-missing `docs/audit/2026-05-21-*` referenced since Phase 2.)

**Headline:** Phase 3 (Play Pool Architecture) is complete and the perf/architecture
*design* (SIM-114/119/280/281) is rigorous, but the two SLA-load-bearing *mechanisms* (the
parallel runner and shared-memory attach) and the entire simulation loop are still
greenfield. The audit also surfaced **six real bugs that exist today** (not just Phase 4
features) — see the ⚠ items.

---

## Agent 1 — Product Manager
- ⚠ **SIM-200/201 ID collision**: SIM-201 is both "Manager decision logic spec (L, P0)" and a "Step 3b catcher-blocking placeholder (S, Held)" — two tickets, one ID; corrupts the dependency graph.
- No canonical Phase 4 loop spec: `README.md` defines an 8-step loop *starting with steal determination*; `docs/perf/2026-06-03-sim-loop-time-budget.md` reconciles a *different* 8 steps. No single source of truth.
- `README.md`/`PRODUCT_GUIDE.md` badly stale: engines 5-11 marked "Planned", registry "planned", and a code sample imports `simulator.core.simulate_game` which doesn't exist (`simulator/` is empty; real code is in `simulation/`).
- SIM-220 backtester is a P0 gate but unbuilt (only the SIM-076 recency harness exists).
- "Phase 3 Gate" rows SIM-107/120/127/128/129 are mis-categorized (frontend / Phase-4-blocked), not play-pool work.
- Risk: OneDrive truncation hit every recent sprint; Phase 4 adds several large new modules at highest risk.

## Agent 2 — Baseball Analyst
- ⚠ **RUN_VALUES ↔ `events` vocabulary mismatch**: `simulation/constants.py` keys (`intentional_walk`, `ground_into_double_play`, `sacrifice_fly`…) don't match the pool's Statcast-raw `events` (`intent_walk`, `field_out`, `force_out`, `sac_fly`, `fielders_choice`…). `sim_loop.py` does `RUN_VALUES.get(event, 0.0)` → the most common outs silently score **0.0 runs**; run environment will be badly inflated.
- ⚠ **Park-factor builder bug**: `_compute_park_factors` writes `factor_vs_l/_vs_r` as NULL (computed then discarded) and references `factor_overall` before the UNPIVOT produces it (looks broken). Park effects also not wired into any sim path; double-counting risk if applied on top of an already-park-influenced pool without neutralization.
- Run resolution should use sampled `result_hits/outs/runs` deltas + the RE24 matrix (both exist), not context-free linear weights.
- Manager decision *logic* (pull/pinch-hit/bullpen-by-leverage/bunt) is entirely unspecified (only `manager_similarity.py` tendencies exist).
- SIM-056 foul design is sound but its host (a count/PA state machine) doesn't exist; IBB/sac/hit-and-run/shift decisions unmodeled.

## Agent 3 — ML / Modeling Engineer
- ⚠ **GMM covariance double-standardization**: the computor stores component `mean` in original units but `covariance` already standardized; the engine then standardizes the covariance again (`D_inv @ cov @ D_inv`) while the mean is standardized once → mean/cov on inconsistent scales feeding the Wasserstein-2 term.
- ⚠ **Calibration computed but never applied**: `SimilarityCalibrator` produces sigmas/gamma/EB-priors but engines use hardcoded literals; no loader wires `CalibrationReport`; medians can silently drift off the 0.50 target (no live test).
- ⚠ **Pitcher no-arsenal fallback is a ×1.0 no-op** renormalization → GMM-less pitchers get non-comparable command-only composites. `arsenal_gamma` (squared form) vs engine `ARSENAL_SCALE` (linear) are inconsistent — one is dead code.
- SIM-220 gold-standard metrics (ECE/Brier/log-loss/reliability) and the ablation-vs-league-average baseline are entirely unbuilt; the SIM-076 harness only does scalar MAE/RMSE and does **not** generalize to them.
- No cross-engine score-fusion: the 11 engines never combine into one per-pitch sampling decision; catcher/fielder/baserunner/manager engines aren't wired into the loop at all.

## Agent 4 — Data Engineer
- ⚠ **SIM-115 pruning contradicts the SIM-111 query contract**: shipped migration 0005 keeps `idx_pp_outcome`/`idx_pp_count` and drops `idx_pp_pitcher`, but the contract §6.2 (the stated input) says keep the pitcher/season/handedness indexes and drop outcome/count, since the pitch path filters on `pitcher_id`+`stand`. Needs reconciliation. No `stand` index on either pool (it's half the pre-filter).
- Incremental watermark uses strict `>` → same-`game_date` late/doubleheader rows are silently skipped; should be `>=` + a row-count guard.
- `recency_ref_season = max(seasons)` computed per-pool independently → inconsistent weights if pools built with different season lists. `recency_weight` is `NOT NULL` in schema but only `DEFAULT 1.0` (no NOT NULL) in migration 0004 → nullability drifts by build path.
- `stand` (pool column) vs `bat_hand` (spray correction + FAISS tile key) diverge for switch hitters — unresolved contract.
- Phase 4 lineup access gap: `raw.game_lineups` / `sim.lineup_state` are Postgres-only; the DuckDB-reading sim loop has no defined path to resolve lineups/subs at runtime.
- Stray `pipeline/live/live_ingestion_pipeline.py.clean` duplicate module.

## Agent 5 — Backend Developer
- `simulation/sim_loop.py` resolves one pitch (+ one batted ball); no count/out/base/inning state machine, no PA/half-inning/game loop, no terminal logic (ball4→walk, strike3→K), no invalid-state guards. `PitchState` is a throwaway with no runners/lineup/score/manager context — the real `GameState` is missing.
- Fingerprints are STUB hashes; Phase 4 must derive the real 10-dim (SIM-041) / 3-dim (SIM-042) vectors from game state. The 11 engines + registry are ready to wire but nothing calls them.
- Package split: `simulation/` (real) vs `simulator/` (empty `steps/` scaffold) — needs consolidation.
- No simulation REST endpoints, no ProcessPool 100-iteration runner, no Redis sim/pool TTL caching, no `with_override`, no snapshot storage (Phase 5). `ws_router`/`odds_router`/`connection_manager` already exist in the live pipeline; `api/websocket/` is an empty placeholder.

## Agent 6 — Performance Engineer
- ⚠ **The two SLA-load-bearing mechanisms are 100% unbuilt**: zero `ProcessPoolExecutor`/`multiprocessing`/`shared_memory` anywhere (SIM-281 is spec-only), and the sampler uses plain `faiss.read_index` + `np.load` (full copy, no `mmap_mode='r'`) — so each worker gets a private ~290 MB copy, the exact ">2 GB at W workers" failure SIM-280 warns of.
- Situation engine still holds the 1M-row `list[NearestSituation]` (~120 MB) — not columnarized, un-shareable. Arsenal cache has no read-only per-worker attach path (~0.58 GB/process risk).
- Production PK outcome-fetch latency unmeasured (the 300 µs anchor was an unindexed synthetic scan); no in-proc payload cache. Perf Bench 4/5 are skip-stubs; no CI perf/RAM regression gate.
- The IVF 50k crossover guard is unimplemented but acceptable (no tile approaches it).

## Agent 7 — UX Designer
- `PlayResult` is single-pitch raw sampler payload — no PA/game aggregation, no win%, no per-player attribution. The UI's boxscore/linescore/field graphic/distribution views all need a `GameSimResult` aggregation contract that Phase 4 must build in (not bolt on).
- Score output must include **raw per-iteration arrays** (not a pre-binned histogram) to preserve prop/over-under signal; per-player sim averages (AB/H/HR/RBI; IP/K/BB/ER) have no shape anywhere; `simulated_at`/confidence-interval fields (SIM-112) unspecified; field/baserunner state + pitch-level play-by-play persistence undefined; override re-sim must be baseline-delta-comparable.

## Agent 8 — Betting / Markets Analyst
- The gold-standard CLV framework is **specified, not built**: schema columns exist but zero application code for implied-prob/de-vig/edge/EV/CLV (`grep` finds none). `mark_closing_lines()` exists for game odds; `mark_closing_prop_lines()` and the SIM-138 nightly opening-line job are referenced but unimplemented; `_persist_prop_odds()` exists but is never called.
- Odds are MockOddsAPI-only (vig 6-10%, wider than real 3-5% sharp books). No real provider/multi-book/prop cadence.
- No prop-distribution aggregation (full PMF for K/hits/HR/RBI/TB) and no calibrated win-probability output — nothing betting-actionable is emitted yet. No Brier/ECE/log-loss (owned by the unbuilt SIM-220).

## Agent 9 — QA / DevOps
- No Phase 4 loop tests exist: no E2E historical-replay + chi-squared run-distribution test, no invalid-state harness (1,000 games), no stress test (100×30 concurrent). `simulation/` is **not** in the coverage `--cov` scope → the whole loop would ship with no coverage enforcement.
- SIM-107 confirmed open (`live_ingestion_pipeline.py` 1,828 LOC `omit`-ed, 0% coverage); SIM-152 shared conftest only partially done (2 trivial fixtures).
- ⚠ **Infra hazard**: OneDrive truncation is actively occurring (`.git/config` returns "Invalid argument" → git unusable from the tree; unreadable `.pyc`). Recommend moving the repo off OneDrive or a pre-commit integrity check.
- Python drift: CI lint/test on 3.13, weekly/Docker on 3.11, sandbox 3.10 — divergent-artifact risk. Stray untracked files (`*.tmp`, `*.clean`, `*_output.txt`) not git-ignored. Perf Bench 4/5 skip-stubs; weekly jobs won't auto-pick up new suites.

---

## Cross-cutting themes (drove the consolidation)
1. **Phase 4 needs a spec + a `GameState` contract before any loop code** (PM, BE, BA, UX all hit this).
2. **Six live bugs** to fix early: RUN_VALUES↔events, recency_weight-not-applied, GMM double-standardization, park-factor SQL, SIM-115/contract index conflict, pitcher no-arsenal no-op.
3. **The validation spine (SIM-220 + ablation + sniff tests + chi-squared + invalid-state)** must be built alongside the loop or its output is unverifiable.
4. **Perf is design-complete but mechanism-empty** — the parallel runner + shared-memory attach are the P0 perf gates.
5. **Output contracts** (GameSimResult, per-player aggregates, prop PMFs, win-prob, field/PBP) must be designed into Phase 4 so UI (Phase 6) and betting/CLV have a stable target.
6. **Hygiene/infra**: OneDrive, README currency, scratch files, Python version unification, coverage scope.

*See the prioritized ticket list for the SIM-IDs, tiers, owners, sizes, and dependencies.*
