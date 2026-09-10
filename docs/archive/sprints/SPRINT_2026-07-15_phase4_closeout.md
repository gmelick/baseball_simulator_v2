# Sprint 2026-07-15 — Phase 4 Close-Out (Manager Logic + Hardening) (executed 2026-05-24)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-24 · Disposition: ✅ all 9 tickets accepted after cross-validation — **Phase 4 COMPLETE***

Fifth and final Phase-4 sprint. Lands the manager decision logic + situational decisions (the
last behavioral gap in the loop), the remaining perf mechanisms, the stress + live-pipeline
test hardening, and the P3 hygiene — closing Phase 4. The PM planned the sprint; role
subagents implemented; the orchestrator ran the cross-validation. Companion to `CHANGES.md`
(per-agent detail), `BACKLOG.md` (banners), and `docs/HANDOFF_PHASE5.md` (Phase 5 entry).

## 1. Plan and execution model

File ownership was almost disjoint; the only true serialization point was `simulation/sim_loop.py`
(the manager tickets). Planned waves: A (SIM-334 ∥ SIM-348) → B (SIM-323 → SIM-349, serial on
the loop) → C (SIM-335 ∥ SIM-347) → C2 (SIM-343+344) → orchestrator (SIM-341/342) → QA.

**Reality — session-limit pressure.** The shared session limit was hit repeatedly this sprint:
both Wave-A agents and several QA agents limited out mid-run. SIM-334's first attempt left the
situation engine half-edited (an unclosed-bracket SyntaxError). Recovery: completed SIM-334 via
a fresh agent, then ran the remaining tickets **one at a time** (lower peak load), and the
orchestrator ran the cross-validation directly. All work landed and was verified.

## 2. Tickets and owners

| Ticket | Owner(s) | Deliverable |
|---|---|---|
| SIM-334 | Performance + ML | `similarity/engines/situation_similarity.py` (columnar store) + `tests/unit/test_perf_eng_sim334.py` |
| SIM-348 | QA/DevOps + Data | `tests/unit/test_live_pipeline_sim348.py` + `pyproject.toml` (omit removal) |
| SIM-323 | Baseball Analyst + Backend | `simulation/sim_loop.py` (manager hooks) + `tests/unit/test_baseball_analyst_sim323.py` |
| SIM-349 | Baseball Analyst + Backend | `simulation/sim_loop.py` + `simulation/game_state.py` (2 ManagerContext fields) + `tests/unit/test_baseball_analyst_sim349.py` |
| SIM-335 | Performance Engineer | `tests/performance/bench_simulation.py` + `.github/workflows/perf-weekly.yml` |
| SIM-347 | QA/DevOps + Performance | `tests/unit/test_qa_sim347.py` |
| SIM-343+344 | QA/DevOps | `pyproject.toml` + `.github/workflows/ci.yml` + `.gitignore` |
| SIM-341+342 | Product Manager (orchestrator) | `README.md` + `BACKLOG.md` (re-categorization) |

## 3. Per-ticket result

**SIM-334 — columnarize situation engine.** Replaced `_index_meta: list[NearestSituation]`
(~120 MB of slots objects, un-shareable) with a `ColumnarSituationMeta` of parallel read-only
numpy arrays (one per field; `play_id` as fixed-width `<U`, ints/floats typed), frozen
`writeable=False` for safe sharing. `build()` populates columns; `query()`/`query_batch()`
reconstruct identical `NearestSituation` objects. 20 new tests + the existing 37 situation unit
tests green. **(See §4 — QA caught a regression here.)**

**SIM-348 — live-pipeline tests.** 51 real tests over `GameStateBuilder`, `ConnectionManager`
broadcast safety, the WS→REST refetch + re-sim trigger, odds/prop persistence + closing-line
marking, cooldown/429, and the `__init__` guards — reusing the async+mock idiom; removed the
`live_ingestion_pipeline.py` coverage omit. The SIM-152 shared conftest was already complete
(the "2 fixtures" symptom was a mount-truncation artifact). 51 + 45 existing live tests green.

**SIM-323 — manager decision logic.** Filled the loop's `_pre_pitch_hook` (IBB on first-base-
open/RISP/high-LI gated by tendency; pitch-out; steal green-light) and `_end_of_pa_hook`
(starter pull + bullpen-by-leverage with the closer in high-LI late spots; pinch-hit; sac-bunt)
from `manager_similarity` tendencies gated by a documented leverage index, written through
`ManagerContext`. No-DB-safe (manager=None → all hooks no-op). 24 new tests + 71 loop regression
green; 0 `TODO(SIM-323)` markers remain.

**SIM-349 — situational decisions.** Added hit-and-run (runner on 1B, <2 outs, favorable
count/leverage, gated by H&R tendency) and sac-fly intent (runner on 3rd, <2 outs, run-needed;
biases a fly-out to a scoring `sacrifice_fly` via the SIM-312 resolver — a bias, never a forced
outcome) on the SIM-323 hooks, with two new `ManagerContext` fields. Mutually exclusive with
SIM-323's IBB/sac-bunt (no double-fire). 23 new tests + 125 broader loop sweep green.

**SIM-335 — perf benches + CI gate.** Implemented Bench 4 (one `step_pitch` over a representative
pitch cycle, no-DB injected resolver) and Bench 5 (the SIM-332 batch runner), asserting the
SIM-119 ~1.23 ms/pitch budget HARD only under `PERF_STRICT`+`PERF_STRICT_SANDBOX` (so the noisy
sandbox never false-fails); wired `perf-weekly.yml` (`PERF_STRICT=1`, `--benchmark-max-time`, a
1.5 GB peak-RSS gate under the SIM-280 2 GB cap). Perf suite **3→5 passed / 0 skipped**.

**SIM-347 — stress test.** Drives the real `ProcessPoolExecutor` runner to 30 concurrent games
× repeats (≥100 sims), no-DB injected; asserts every future resolves (no worker exception/race),
pooled-vs-in-process score equality (rng isolation), reproducibility under base seed, valid
results, and `/dev/shm` returns to baseline after `close()` (no leak). 4 always-on + 3 slow
(full 100×30 ≈ 2.9 s, clean). Latent finding: `GameSpec._hit_rate` is a dead knob (filed).

**SIM-343 + SIM-344 — coverage/CI/hygiene.** Added `simulation`+`betting` to the coverage
source (gate 80 unchanged); unified CI Python to 3.11 across all workflows (`ci.yml` 3.13→3.11;
others already 3.11; docker has no Python). Extended `.gitignore` for scratch outputs; the
stray files (`smoke_output.txt`/`test_output.txt`) are untracked and now ignored (the OneDrive
mount blocks the physical `rm`, so `.gitignore` is the durable fix).

**SIM-341 + SIM-342 — docs reconcile.** README: marked engines 5-11 + the registry shipped,
replaced the stale `from simulator.core import simulate_game` sample with the real
`simulation.sim_loop` / `batch_runner` / `win_probability` / `betting.clv_engine` API, and
corrected the repo tree (`simulator/`→`simulation/` + `betting/`). PRODUCT_GUIDE had no stale
`simulator.core`/Planned markers. Re-categorized the stale Phase-3-Gate rows (SIM-107 →
addressed by SIM-348; SIM-120 → unblocked by SIM-320; SIM-127/128/129 → Phase 6).

## 4. Verification

Orchestrator-run cross-validation (the QA subagent hit the session limit). Every sprint file
integrity-checked (compile + null-byte); the FULL unit+regression suite run in chunks covering
every file. **New baseline: 1505 passed / 1 skipped / 0 failed** unit+regression (1506 collected
incl. 9 slow; lone skip = pre-existing engine-build-smoke), up from 1380 (+125 new tests);
**performance 5 passed / 0 skipped**. DuckDB schema v7.

**A real regression was caught and fixed in QA.** SIM-334's columnarization broke the
situation-engine golden-file + `batch_equals_individual` regression tests because the implementing
agent ran only the unit tests, not `tests/regression/`. Root cause: those regression tests inject
`_index_meta` as a plain `list[NearestSituation]`, but the new `query()`/`query_batch()` only
handled the columnar `.row()` (→ `AttributeError: 'list' object has no attribute 'row'`). Fixed
with a `_row_from_meta` helper that reconstructs a `NearestSituation` from EITHER the columnar
store or a list — identical results. All situation tests green afterward. (This validates the
independent-cross-validation discipline: per-ticket self-reports said green, but the from-scratch
suite run surfaced the gap.)

### Environment note
The OneDrive truncation hazard was acute this sprint: `sim_loop.py` (→2,678 lines),
`situation_similarity.py`, `pyproject.toml`, `game_state.py`, and `.gitignore` all truncated /
null-byte-corrupted on the mount and were repaired per the documented recipe (authoritative
Windows files verified intact). The `/tmp` shim + pyc dir were recreated after the sandbox
cycled. This is precisely the risk SIM-315 addresses — and it now bites on nearly every large-file
edit.

## 5. Phase 4 status & follow-ups

**PHASE 4 (Core Simulation Loop) is COMPLETE** — spec → GameState contract → loop (state
machine, fingerprints, outcome/foul, fielding/baserunning/steals, `simulate_game`) → fusion →
validation spine (backtester, chi-squared replay, sniff, invalid-state) → output contracts
(GameSimSummary, per-player boxscore, win-prob, prop PMFs, snapshots) → perf (ProcessPool +
shared-memory) → betting chain (PMFs → CLV → odds) → manager + situational decisions. All six ⚠
audit live bugs fixed. Suite 1505; perf 5/5; schema v7.

1. **Phase 5 — Backend API & Simulation Runner** (next phase): REST endpoints, the WebSocket
   live channel, the 100-iteration `/simulate` endpoint over the SIM-332 runner, the
   `with_override` endpoint over the SIM-331 OverrideDelta, snapshot/replay endpoints, Redis
   wiring. See `docs/HANDOFF_PHASE5.md`.
2. **SIM-315** — move the repo off OneDrive / add the integrity guard. The single biggest
   standing infra risk; it cost real time on nearly every large-file edit this phase.
3. **Small follow-ups:** prop-TB 2B/3B tracking (SIM-329 TB is a lower bound); the dead
   `GameSpec._hit_rate` knob (SIM-347 finding); `backlog.xlsx` regeneration from `BACKLOG.md`.

---

*End of sprint log. End of Phase 4.*
