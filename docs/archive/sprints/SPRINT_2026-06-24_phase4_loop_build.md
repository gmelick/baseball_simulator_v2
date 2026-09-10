# Sprint 2026-06-24 — Phase 4 Loop Build (executed 2026-05-23)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-23 · Disposition: ✅ all 8 tickets accepted after independent QA cross-validation*

Second Phase-4 sprint. Goal: turn the SIM-303 single-pitch scaffold into a **full-game
simulator** — the GameState state machine, real fingerprint derivation, cross-engine
fusion, outcome/foul/fielding/baserunning/steal resolution, and the `simulate_game()`
entry point — plus the first two validation harnesses (invalid-state + baseball sniff).
The PM planned the sprint (see §1); execution honored Greg's standing model: role
subagents implement, an independent QA/DevOps pass cross-validates against the actual
files. Companion to `CHANGES.md` (per-agent detail) and `BACKLOG.md` (banners).

## 1. Plan and execution model

The defining constraint: **SIM-316, SIM-318, SIM-319, SIM-320 all mutate
`simulation/sim_loop.py`**, so they were strictly serialized — never two loop-file editors
at once. The separable new modules (SIM-321 fusion, SIM-317 fingerprints) and the
test-only harnesses (SIM-326, SIM-324) ran where parallel-safe. Execution waves:

- **Wave A (parallel):** SIM-316 (owns `sim_loop.py`) ∥ SIM-321 (new `score_fusion.py`).
- **Wave B:** SIM-317 (new `fingerprints.py`; consumes SIM-321; wired into the loop solo).
- **Wave C (serialized on `sim_loop.py`):** SIM-318 → SIM-319.
- **Wave D:** SIM-320 (`simulate_game()`).
- **Wave E (parallel, after SIM-320):** SIM-326 ∥ SIM-324 (separate test files).

Deferred to Sprint 3 (PM decision): SIM-220 (backtester, L) and SIM-325 (chi-squared
replay, L) — heavy validation that needs a stable loop + outputs first; SIM-323 (manager
logic, L) — gated on its own design, loop runs via the `ManagerContext` hook stubs;
SIM-315 (OneDrive infra) — still Open, not loop-blocking.

## 2. Tickets and owners

| Ticket | Owner(s) | Deliverable |
|---|---|---|
| SIM-316 | Backend Developer | `simulation/sim_loop.py` (state machine) + `tests/unit/test_backend_sim316.py` |
| SIM-321 | ML Engineer (lead) + Backend | `simulation/score_fusion.py` + `docs/architecture/2026-06-24-cross-engine-fusion.md` + `tests/unit/test_ml_engines_sim321.py` |
| SIM-317 | ML Engineer (lead) + Backend | `simulation/fingerprints.py` + loop wiring + `tests/unit/test_ml_engines_sim317.py` |
| SIM-318 | Backend + Baseball Analyst | `simulation/sim_loop.py` (outcome+foul) + `tests/unit/test_backend_sim318.py` |
| SIM-319 | Backend + ML Engineer | `simulation/sim_loop.py` (fielding/baserunning/steals) + `tests/unit/test_backend_sim319.py` |
| SIM-320 | Backend Developer | `simulation/sim_loop.py` (`simulate_game()`) + `tests/unit/test_backend_sim320.py` |
| SIM-326 | QA/DevOps + Backend | `tests/unit/test_qa_sim326.py` |
| SIM-324 | Baseball Analyst + QA/DevOps | `tests/unit/test_baseball_analyst_sim324.py` |

## 3. Per-ticket result

**SIM-316 — state machine.** Restructured `sim_loop.py` (255 → 709 lines) into a
GameState-driven `StateMachine`. Pure `advance_count` classifier: ball4→walk, K3→strikeout,
in_play→contact, and the SIM-056 two-strike-foul absorbing rule (a foul at two strikes does
not advance the count or end the PA). `advance_half_inning` clears bases, resets count/outs,
flips `Half`, advances the inning on BOTTOM→TOP, and carries each side's lineup pointer.
Invalid-state guards via the SIM-311 `assert_*` helpers. Left `# TODO(SIM-318/319/320/323)`
hooks; SIM-303 scaffold surface kept verbatim. 23 tests.

**SIM-321 — cross-engine fusion.** New `simulation/score_fusion.py` + design doc. Three
engines drive the per-pitch draw (pitcher 0.50 / batter 0.30 / situation 0.20); resolution
engines (catcher/fielder/manager/baserunner) reused via named profiles. Heterogeneous
outputs made comparable by respecting `score_type`: similarities pass through [0,1];
distances map to a bounded monotone affinity `exp(-d/scale)` — explicitly NOT the sampler's
`1/(d+EPS)` (the module never normalizes across candidates, never imports the sampler), so
the distance→weight boundary is preserved. Weighted geometric mean combination. 33 tests
(incl. a static-source guard on the boundary).

**SIM-317 — fingerprints.** New `simulation/fingerprints.py` derives the 10-dim pitch and
3-dim batted-ball query vectors in the engines' exact feature order (imported from the
engines, single source of truth), z-score + sqrt-weight normalized to engine space; the
pre-filter keys `(pitcher_id, bat_hand, season)` stay args, never vector dims; a per-PA
matchup cache serves repeated pitches. Wired into the loop's pitch-selection step via an
optional injected `FingerprintDeriver` (legacy stub when absent), so the count-machine-only /
no-DB test path is untouched. 16 tests; SIM-316/303 stayed green.

**SIM-318 — outcome + foul re-weight.** Step-4 outcome determination on the committed pitch
outcome, plus the SIM-056 count-conditional foul re-weight applied in the loop before the
count advances (factors {0 strikes: 1.00, 1: 1.05, 2: 1.55}, cross-checked against the foul
design CSV), re-normalized — the sampler stays count-blind. Two-strike-foul absorbing rule
holds end-to-end. 20 tests.

**SIM-319 — fielding / baserunning / steals.** Steps 6/7 via an injected `PlayResolver`
(fielder/baserunner signals). Every run and base-out delta routes through a single
`_commit_run_delta` → `resolve_runs` call site (RE24-primary + linear fallback, with
provenance recorded) — no inline run arithmetic anywhere. Steal decision in the pre-pitch
hook, resolution in step 7 against `sim.stolen_base_pool` (safe/caught, caught-stealing can
be the third out); dropped-third-strike gated on (1B open OR two outs) + swinging K3.
`sim_loop.py` → ~1,546 lines. 17 tests.

**SIM-320 — `simulate_game()`.** A thin game-level driver that calls the existing
`step_pitch` machinery (does not reimplement steps 1-7): tracks the (inning, half) pointer,
evaluates game-over after each completed half and walk-off after each pitch. Regulation 9;
walk-off ends mid-inning when the home team leads in the bottom of the 9th+; extra innings
place a ghost runner on 2B each half; the per-game seed is threaded through both the loop
rng and the sampler rng so a fixed seed reproduces a game exactly. Returns `GameSimResult`
(final score, innings, flags, seed, pitch count, winner). A pitch ceiling guards against a
pathological never-an-out machine. `sim_loop.py` → ~1,805 lines. 13 tests. (One obsolete
SIM-316 test — asserting the old `NotImplementedError` stub — was updated to assert the
driver's `ValueError` on the no-sampler path.)

**SIM-326 — invalid-state harness.** `test_qa_sim326.py` runs 1,000 games (default,
`SIM326_GAMES`-overridable) via `simulate_game()` with varied seeds, validating the
committed `GameState` after every transition (no negative scores, outs ≤3, consistent bases,
valid counts, lineup pointers in range) plus per-game terminal validity, with negative tests
proving the checker flags known-bad states. A slow-marked 5,000-game exhaustive run. Zero
invalid states; ~1.3s for 1,000 games.

**SIM-324 — baseball sniff suite.** `test_baseball_analyst_sim324.py` drives the loop with a
calibrated league-average per-pitch + PA-event model (runs *emerge* from the loop's
baserunning, not injected) and asserts noise-robust bands: run env 3.8–5.0 R/team/G
(observed ≈ 4.38), P/PA 3.7–4.0 (≈ 3.74), platoon split direction emerges under a wired L/R
skew, and RE24 read from `run_resolution.RE24_MATRIX` is monotonic (decreasing in outs,
non-decreasing on adding a runner). 11 tests.

## 4. Verification

Independent QA pass (chunked; the full suite exceeds the 45 s/call limit). All 8 tickets
audited against the actual files — all PASS, with the two locked boundaries grep-verified
(single `resolve_runs` call site; fusion never crosses the distance→weight line). **New
baseline: 1144 passed / 1 skipped / 0 failed** unit+regression (+2 slow-marked passed); was
996. Performance 3 passed / 2 skipped. No mount repairs needed at QA time; no regressions.
DuckDB schema unchanged at v6.

### Environment note
`simulation/sim_loop.py` grew 255 → ~1,805 lines; OneDrive truncation/null-byte injection
hit it on nearly every edit and was repaired on the mount per the documented recipe (the
authoritative Windows file stayed the intact source of truth). This is exactly the SIM-315
hazard — its remediation grows more valuable as the loop file grows.

## 5. Open follow-ups

1. **Sprint 3 (validation spine + outputs):** SIM-220 (backtester — ECE/Brier/log-loss +
   ablation), SIM-325 (chi-squared historical replay), then the P2 output contracts
   (SIM-327 `GameSimResult` aggregation, SIM-328 per-player accumulators, SIM-330 win-prob)
   and perf mechanisms (SIM-332 ProcessPool runner, SIM-333 shared-memory attach).
2. **SIM-323 manager logic** (L) — the real manager-decision module, currently a hook stub.
3. **Remaining audit bugs:** SIM-336 (park-factor SQL) and SIM-346 (ML calibration).
4. **SIM-315** — schedule the OneDrive move + integrity guard; the loop file is now the
   single biggest truncation risk in the tree.
5. **`backlog.xlsx`** needs regeneration from `BACKLOG.md`.

---

*End of sprint log.*
