# Sprint 2026-07-01 — Phase 4 Validation Spine + Output Contracts (executed 2026-05-23)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-23 · Disposition: ✅ all 7 tickets accepted after independent cross-validation*

Third Phase-4 sprint. With the loop producing full games (Sprint 2), this sprint builds the
**output-contract layer** every downstream consumer (UI Phase 6, betting/CLV, the perf
runner) reads, plus the **validation spine** (backtester + chi-squared replay) that makes the
loop's output verifiable. The PM planned the sprint; role subagents implemented; the
cross-validation ran the full suite. Companion to `CHANGES.md` (per-agent detail) and
`BACKLOG.md` (banners).

## 1. Plan and execution model

The defining constraint remains `simulation/sim_loop.py` (~2,000 lines): only ONE agent may
edit it at a time, and importers must not run while it is mid-edit. SIM-327 turned out NOT to
need a loop edit (the existing per-game `GameSimResult` sufficed), so only SIM-328 touched the
loop file. Execution waves:

- **Wave A:** SIM-327 alone (defines `GameSimSummary` in a new `simulation/results.py`).
- **Wave B:** SIM-328 alone (per-player accumulators inside the PA loop — the only `sim_loop.py` edit).
- **Wave C (parallel, loop now stable):** SIM-332 (batch runner) ∥ SIM-330 (win-prob) ∥ SIM-331 (snapshots).
- **Wave D (parallel):** SIM-220 (backtester) ∥ SIM-325 (chi-squared replay).
- **Wave E:** cross-validation pass (full suite, chunked).

Deferred to Sprint 4 (PM decision): SIM-329 (prop PMFs, L — depends on SIM-327 + SIM-332),
SIM-333 (shared-memory, L — depends on SIM-332), SIM-323 (manager logic, L — own design +
loop contention; the `ManagerContext` hook stubs suffice for now), SIM-339/340 (betting CLV +
real odds — depend on props), and the two remaining audit bugs SIM-336/SIM-346.

## 2. Tickets and owners

| Ticket | Owner(s) | Deliverable |
|---|---|---|
| SIM-327 | Backend + UX | `simulation/results.py` + `tests/unit/test_backend_sim327.py` |
| SIM-328 | Backend + Baseball Analyst | `simulation/sim_loop.py` + `simulation/results.py` (re-exports) + `tests/unit/test_backend_sim328.py` |
| SIM-332 | Backend + Performance Engineer | `simulation/batch_runner.py` + `tests/unit/test_backend_sim332.py` |
| SIM-330 | Backend + ML Engineer | `simulation/win_probability.py` + `tests/unit/test_backend_sim330.py` |
| SIM-331 | Backend + UX | `simulation/snapshots.py` + `tests/unit/test_backend_sim331.py` |
| SIM-220 | ML Engineer + Betting Analyst | `similarity/backtesting/backtester.py` (+ `__init__` re-exports) + `tests/unit/test_ml_engines_sim220.py` |
| SIM-325 | QA/DevOps + Baseball Analyst | `simulation/validation/replay_chi_squared.py` + `tests/unit/test_qa_sim325.py` |

## 3. Per-ticket result

**SIM-327 — aggregation contract.** New `simulation/results.py` with `GameSimSummary.from_results(results, *, confidence_level=0.95)`: per-team win% + tie% (sum to 1), mean/median home/away/total scores, the **raw per-iteration score arrays** (numpy, input order — not a histogram, preserving prop/over-under signal per SIM-112), a UTC `simulated_at`, and `ConfidenceInterval`s (Wald normal-approx, documented) on win% and mean scores. Re-exports the per-game `GameSimResult`. Did not touch `sim_loop.py`. 16 tests.

**SIM-328 — per-player accumulators.** `PlayerStatLine` + `BoxScore` accumulated inside the PA loop's terminal-PA boundary (`_accumulate_pa` in `_end_of_pa`, before the batting-order advance). Batter (resolved from the offense lineup slot, correct on a fresh half) gets AB (excl. BB/IBB/HBP/SF/SH), H, HR, RBI (= runs driven in, not on error-runs); pitcher gets IP (accumulated in outs/thirds), K, BB, ER (earned = scored runs except on an `is_error` play — a documented per-play simplification, no inning reconstruction). Exposed as an additive optional `boxscore` on `GameSimResult`; re-exported from `results.py`. 15 tests; SIM-320/326/327 stayed green.

**SIM-332 — parallel batch runner.** `simulation/batch_runner.py`: `ProcessPoolExecutor(max_workers=max(1, min(cpu-1, 10)))` runs N iterations of `simulate_game()` and reassembles in seed order into a `GameSimSummary`. Per-game seed = base + i (fixed base ⇒ reproducible batch, each game distinct). Pickle-safe: workers receive a frozen `GameSpec` (a dotted factory ref + scalar kwargs) and build their own machine in-process — no live objects cross the boundary; `max_workers=1` is a synchronous fallback. Redis TTL cache (sim 60s / pool 5-min) behind a `SimCache` interface with `RedisCache` / `InMemoryCache` / `NullCache`, selecting Redis only after a live ping — so tests need no server. The SIM-333 shared-memory attach is left as an inert `initializer`/`shared_segments` seam. 23 tests (21 + 2 slow, incl. a real 100-game cross-process run).

**SIM-330 — calibrated win-probability.** `simulation/win_probability.py`: raw Monte-Carlo win frequency → Beta(α,α) posterior-mean smoothing (default Jeffreys α=0.5, so 0/N is never a hard 0/1 and shrinkage vanishes as N grows) → an identity `CalibrationMap` seam (a fitted isotonic/Platt curve from SIM-220 plugs in later) → a Wald CI reusing the SIM-327 shape. Tie handling is split (push) or drop, configurable. Pure/deterministic. 33 tests.

**SIM-331 — snapshot contracts.** `simulation/snapshots.py`: `FieldSnapshot.from_game_state` (9 positions + batter + baserunners + count/outs/score chrome for the BaseballFieldGraphic), `PlayByPlay.from_play_results` (pitch-level entries with at-bat/pitch/sequence indexing for `/plays` + drill-down), `StateAtPitch` (the `/state/{ab}/{pitch}` lookup), and `OverrideDelta` (baseline-vs-override comparison for the override UI). Pure builders from `GameState`/`PlayResult`; additive. 10 tests.

**SIM-220 — backtesting framework.** `similarity/backtesting/backtester.py`: ECE (binned), multi-class Brier, eps-clipped log-loss, and a reliability curve, plus `walk_forward_ablation` that scores the full model vs a league-average baseline (and arbitrary per-engine swaps) on each held-out window, reusing the SIM-076 `walk_forward_folds` splitter. Prediction function injectable → testable on synthetic data, no FAISS/DuckDB. New module (SIM-076 harness untouched). 13 tests; SIM-076 regression 9 green.

**SIM-325 — chi-squared replay.** `simulation/validation/replay_chi_squared.py`: replays games through `simulate_game()`, bins per-team-game run totals (0..9,10+), pools low-expected bins (Cochran ≥5), and runs `scipy.stats.chisquare` vs a reference distribution, asserting p>0.05. Validated on the SIM-324 calibrated league-average model (self-consistency p≈0.36; large-sample p≈0.84); a wrong/shifted reference is firmly rejected (p≈0) — proving power. A `HistoricalGame` + `replay_and_test(..., state_machine_factory=...)` seam plugs in real box-score data unchanged. 17 tests (16 + 1 slow).

## 4. Verification

The independent QA subagent hit a session limit partway in, so the orchestrator ran the
cross-validation directly: every sprint file integrity-checked (compile + null-byte clean on
the mount), then the FULL unit+regression suite run in chunks (Sprint-3 files; the loop
tests incl. real-FAISS sim303/sim319; regression; data-engineering; ML/engines;
API/live/perf/smoke; engine/component; older backend + sim301/302/202; the slow-marked tests;
and the performance suite) — every file covered, zero failures. **New baseline: 1271 passed /
1 skipped / 0 failed** unit+regression (1272 collected = 1267 not-slow + 5 slow; the lone skip
is the pre-existing engine-build-smoke skip), reconciling exactly with 1144 + 127 new tests;
performance 3 passed / 2 skipped. No regressions; schema v6.

### Environment note
OneDrive truncation/null-byte injection again hit several files on write (incl. `sim_loop.py`,
`results.py`, the new `__init__` files); repaired on the mount per the documented recipe with
the authoritative Windows files verified intact. The `/tmp` shim + pyc dir had to be recreated
(the sandbox cycled) — `pytest-asyncio` + the `datetime.UTC` shim remain required for a true
full-suite run.

## 5. Open follow-ups

1. **Sprint 4 (betting + remaining perf):** SIM-329 (prop PMFs over the per-player/raw-array
   substrate) → SIM-339 (CLV engine) + SIM-340 (real odds/prop ingestion); SIM-333
   (shared-memory zero-copy attach on the SIM-332 seam).
2. **SIM-323 manager logic** (L) — the real manager-decision module (currently hook stubs).
3. **Remaining audit bugs:** SIM-336 (park-factor SQL) and SIM-346 (ML calibration wiring).
4. **SIM-315** — the OneDrive move + integrity guard; `sim_loop.py` (~2,000 lines) is the
   biggest truncation risk and SIM-328's edits proved it again.
5. **`backlog.xlsx`** needs regeneration from `BACKLOG.md`.

---

*End of sprint log.*
