# Sprint 2026-06-17 — Phase 4 P0 Gates (executed 2026-05-22)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-22 · Disposition: ✅ all 8 tickets accepted after independent QA cross-validation*

First Phase-4 sprint. Goal: land the Tier-P0 gates that must precede any simulation-loop
code (the loop spec and the `GameState`/`PlayResult` contract), and fix the
"fix-as-touched" live bugs in the touched areas — so the loop build (SIM-316→320) can
start cleanly. Scope confirmed with Greg before kickoff: **full P0 set** (SIM-310, 311,
312, 313, 314) **+ the two quick bug fixes** (SIM-322, SIM-337) pulled forward, with
**SIM-315 documented and deferred** (no code this sprint). Companion to `CHANGES.md`
(per-agent detail) and `BACKLOG.md` (one-line rows + banners).

## 1. Tickets and owners

| Ticket | Owner(s) | Deliverable |
|---|---|---|
| SIM-310 | Backend Developer (lead) + Baseball Analyst | `docs/architecture/2026-06-17-phase4-sim-loop-spec.md` |
| SIM-311 | Backend Developer + Data Engineer | `simulation/game_state.py` + `tests/unit/test_backend_sim311.py` |
| SIM-312 | Baseball Analyst + Backend Developer | `simulation/constants.py` (extended) + `simulation/run_resolution.py` + `tests/unit/test_baseball_analyst_sim312.py` |
| SIM-313 | ML Engineer + Backend Developer | `simulation/play_pool_sampler.py` + `tests/unit/test_ml_engines_sim313.py` |
| SIM-322 | ML Engineer | `similarity/engines/pitcher_similarity.py` + `tests/unit/test_ml_engines_sim322.py` |
| SIM-337 | Data Engineer + Performance Engineer | `db/migrations/duckdb/0006_sim337_reconcile_pool_indexes.sql` + schema/version bump + `tests/unit/test_data_engineer_sim337.py` |
| SIM-314 | Product Manager (orchestrator) | `docs/audit/2026-06-17-sim314-id-collision-resolution.md` |
| SIM-315 | QA/DevOps (orchestrator) | `docs/architecture/2026-06-17-sim315-onedrive-remediation.md` (document-only) |

Execution model (per Greg's standing preference): role subagents implement, an independent
QA/DevOps pass audits acceptance criteria against the actual files and runs the suite.
Dependency order honored — SIM-310 (spec) landed before SIM-311 (contract). The bug fixes
and the two doc/governance items were independent and ran in parallel. Each ticket touched
distinct files, so no large-file serialization conflict arose (only SIM-322 edits a large
file, `pitcher_similarity.py`).

## 2. Per-ticket result

**SIM-310 — Canonical sim-loop spec.** Reconciled the two competing loop definitions
(README's steal-determination-first 8 steps vs the SIM-119 time-budget's 8 steps) into one
authoritative ordering: (1) game-state read, (2) pitch selection, (3) pitch sampling,
(4) outcome determination, (5) batted-ball sampling, (6) fielding resolution,
(7) baserunner advancement, (8) state update + loop control. Adopted the time-budget
ordering (it matches the SIM-317-builds-fingerprint / SIM-302-draws code seam); steal / IBB
/ pitch-out are repositioned to a **pre-pitch manager hook** (between steps 1 and 2) and
substitution to an **end-of-PA hook**, so nothing from the README is lost. Fingerprint
feature lists (10-dim pitch, 3-dim batted-ball) copied in exact engine order; the SIM-056
two-strike-foul absorbing rule referenced; run resolution deferred to SIM-312, the
GameState/PlayResult contract to SIM-311, cross-engine fusion to SIM-321.

**SIM-311 — GameState + PlayResult contract.** New `simulation/game_state.py`: mutable
`GameState` (pre-filter keys pitcher_id/bat_hand/season; balls/strikes/outs; a `Bases` value
type with runner ids; inning + `Half`; home/away score; per-team lineup + slot pointers;
batter/pitcher ids; a `ManagerContext` hook for SIM-323; seed) with ergonomic mutators and
lightweight guards; `PlayResult` generalizes the scaffold dict with run-value + SIM-312
provenance, step 5/6/7 deltas, raw sampler payloads, and a `next_state` handle. The base
state uses the same bit0=1B/bit1=2B/bit2=3B encoding as `run_resolution`/RE24. The SIM-303
scaffold (`sim_loop.py`) was left untouched and still imports. 17 tests.

**SIM-312 — RUN_VALUES↔events + run resolution.** Confirmed the bug: the pool serves
Statcast-raw `sim.outcome_pool.events` verbatim, and `sim_loop.py` did
`RUN_VALUES.get(event, 0.0)`, so `field_out`/`force_out`/etc. (not RUN_VALUES keys) silently
scored 0.0. Kept `RUN_VALUES` at exactly the canonical 12 keys (SIM-202 back-compat) and
added `STATCAST_EVENT_ALIASES` mapping the raw vocabulary onto them, with an import-time
assert that every alias targets a real key. New `simulation/run_resolution.py`:
`resolve_runs()` is RE24-primary (24 base-out states, ~2024 run env, mirrors
`derived.run_expectancy_matrix`) when base-out state + sampled `result_hits/outs/runs` are
present, with a context-free linear-weight fallback; no silent 0.0 for a known out (unknown
events are detectable / can raise in strict mode). Did not touch `sim_loop.py` (SIM-311's
file). 30 tests / 60 subtests; SIM-202's 12 RUN_VALUES tests stayed green.

**SIM-313 — recency_weight wired into the sampler.** `PlayPoolSampler` now multiplies each
neighbour's normalized FAISS-distance weight by its per-row `recency_weight` and
renormalizes (in `sample_pitch`, `sample_batted_ball`, and the `return_distribution` path),
restoring the SIM-111 §8 contract / SIM-076 recency boost that was a read-side no-op. Added
an injectable `recency_fetch` (mirrors `outcome_fetch`) with a lazy DuckDB default; missing /
None recency → 1.0, and a DB lacking the column is treated as recency-neutral — so uniform
recency exactly reproduces the prior pure-distance behavior. 9 new tests; the existing
SIM-302 sampler tests (14) and SIM-303 (4) stayed green.

**SIM-322 — GMM covariance double-standardization.** Root cause: the computor stores each
GMM component's `mean` in original units but its `covariance` already standardized; the
engine then standardized the covariance a second time (`D⁻¹·cov·D⁻¹`) while standardizing
the mean once — corrupting the Wasserstein-2 arsenal distances. Fixed engine-side (no
nightly recompute): `GMMModel.from_json` now de-standardizes the stored covariance back to
original units (`cov_orig = D_feat·cov_std·D_feat`) so the in-memory component is internally
consistent and the single downstream `standardize_gmm` applies one consistent
standardization to both mean and covariance. 9 new tests (incl. a guard proving the old
double-standardized path differs); existing pitcher-engine tests (56) + SIM-075 (8) stayed
green with no expected-value corrections.

**SIM-337 — sim-pool index reconciliation.** Confirmed migration 0005 (SIM-115) inverted
§6.2 of the play-pool query contract on `sim.pitch_pool` (kept outcome/count, dropped the
pitcher index) and that neither pool had a `stand` index though `stand` is half the
pre-filter. New migration `0006_sim337_reconcile_pool_indexes.sql` (all DROPs
`sim.`-qualified) restores `idx_pp_pitcher`/`idx_pp_pitcher_season`/`idx_pp_season`, adds
`idx_pp_pitcher_stand_season` and `idx_op_stand_season`, and drops `idx_pp_outcome` /
`idx_pp_count`. `db/schemas/02_duckdb_schema.sql` updated to the post-0006 set;
`duckdb_schema_version.txt` bumped 5 → 6. The `stand`/`bat_hand` contract cleanup remains
SIM-345 (untouched). 9 new tests (+ the SIM-115 test, left unmodified, stays green).

**SIM-314 — ID collision (governance).** SIM-200/201 are kept as the paired Step-3b catcher
*framing*/*blocking* placeholders (Held); the manager-decision-logic scope is the
audit-minted **SIM-323**. Decision recorded; the manager-logic references in `BACKLOG.md`
repointed to SIM-323.

**SIM-315 — OneDrive remediation (documented, deferred).** Remediation plan written:
recommended Option A (move the working tree off OneDrive — manual, host-side, restores git)
+ Option B (an in-repo `ast.parse` + null-byte file-integrity guard wired into CI and
pre-commit). Document-only this sprint per Greg; ticket stays **Open** with suggested
acceptance criteria attached.

## 3. Verification

Independent QA pass run in chunks (the full suite exceeds the sandbox's 45 s/call limit):
all 8 tickets audited against the actual files (not implementer self-reports) — all PASS.
**New baseline: unit 941 + regression 55 = 996 passed / 1 skipped / 0 failed (+60
subtests)** (was 927); performance 3 passed / 2 skipped. No regressions. A pre-existing
corrupted unrelated test file (`test_data_engineer_sim162.py`, stray trailing `)` — an
OneDrive-corruption casualty) was repaired (one char), restoring 5 tests → **1001 passed**.

### Environment notes
- `pytest-asyncio` is required by the baseline (`asyncio_mode=auto` in `pyproject.toml`) in
  addition to `pytest-benchmark` — without it, 19 async tests error and the perf suite can't
  find the `benchmark` fixture. Both are now part of the documented sandbox setup.
- OneDrive truncation/null-byte injection hit several files this sprint (notably
  `pitcher_similarity.py` and `test_data_engineer_sim162.py`); the authoritative files were
  verified correct via the file tools and the **mount** copies repaired per the documented
  recipe (`head -n <last-good>` + authoritative tail; `tr -d '\000'`). This is exactly the
  hazard SIM-315 addresses.

## 4. Open follow-ups

1. **Phase 4 loop build (next sprint):** SIM-316 (GameState state machine) → SIM-317 (real
   fingerprints) → SIM-318 (outcome + foul re-weight) / SIM-319 (fielding + baserunning +
   steals) → SIM-320 (`simulate_game()`); SIM-321 (cross-engine fusion) feeds 317/318.
   Build the validation spine alongside (SIM-220, SIM-324/325/326).
2. **Remaining audit bugs:** SIM-336 (park-factor SQL + neutralization) and SIM-346 (ML
   calibration wiring) are the two of the six live bugs not yet fixed.
3. **SIM-315 implementation:** schedule the OneDrive move + integrity guard (ties to
   SIM-343 CI Python unification).
4. **`backlog.xlsx`** needs regeneration from `BACKLOG.md` to publish the closed state.

---

*End of sprint log.*
