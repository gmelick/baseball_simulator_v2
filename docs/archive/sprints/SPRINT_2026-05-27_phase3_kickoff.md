# Sprint 2026-05-27 — Phase 3 Kickoff (executed 2026-05-20)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-20 · Disposition: ✅ all tickets accepted after independent QA cross-validation*

This document is the transparency record for the Phase 3 kickoff sprint. It records
who (which agent) did what, how each ticket was verified, the cross-validation
result, and the follow-ups left open. It is the companion to the per-ticket detail
in `CHANGES.md` and the one-line rows in `BACKLOG.md`.

---

## 1. Goal and disposition

Per `docs/HANDOFF_PHASE3.md` §6–§7, Phase 3 feature work could not begin until the
missing SIM-300 architecture spec and the §7 P0 documentation/test gaps (lost to the
documented OneDrive mid-edit truncation) were reconciled. This sprint therefore ran
in two parts:

1. **§7 reconciliation** — rebuild SIM-300 and the lost P0 migration + test files.
2. **Phase 3 sprint tickets** — the six tickets on the "Current Sprint" sheet of
   `backlog.xlsx`: SIM-301, SIM-302, SIM-118, SIM-202, SIM-280, SIM-281.

All eleven work items landed, the full test suite is green, and an independent
QA/DevOps pass returned a **SHIPPABLE** verdict.

## 2. How the work was run (agent team)

Each ticket was implemented by the agent that owns its domain (per `agent_team.md`),
then cross-validated by an independent QA/DevOps pass that audited acceptance criteria
against the actual files rather than trusting implementer self-reports. Engine
score-discipline and the SIM-301↔SIM-302 file-format contract were explicitly checked.

| Ticket | Owning agent(s) | Deliverable |
|---|---|---|
| SIM-300 | Product Manager (from handoff) → reviewed by Backend + ML | `docs/architecture/2026-05-20-play-pool.md` |
| SIM-051 | Data Engineer | `db/migrations/duckdb/0003_sim051_pull_relative_spray_angle.sql` + `tests/unit/test_data_engineer_sim051.py` |
| SIM-162 | Data Engineer | `tests/unit/test_data_engineer_sim162.py` |
| SIM-149 | ML Engineer | `tests/unit/test_baserunner_steal_engine.py` |
| SIM-150 | ML Engineer | `tests/unit/test_ml_engines_sim150.py` |
| SIM-202 | Baseball Analyst | `simulation/constants.py` + `tests/unit/test_baseball_analyst_sim202.py` |
| SIM-118 | Performance Engineer | `tests/performance/bench_simulation.py` + `.github/workflows/perf-weekly.yml` |
| SIM-301 | Backend Developer | `pipeline/batch/play_pool_cache.py` + `tests/unit/test_backend_sim301.py` |
| SIM-302 | Backend Developer + ML Engineer | `simulation/play_pool_sampler.py` + `tests/unit/test_backend_sim302.py` |
| SIM-280 | Performance Engineer | `docs/perf/2026-05-27-ram-budget.md` |
| SIM-281 | Performance Engineer | `docs/architecture/2026-05-27-parallelism.md` |
| QA | QA/DevOps | independent cross-validation (this sprint's gate) |

## 3. §7 reconciliation — what was rebuilt

| §7 gap | Status | Note |
|---|---|---|
| `docs/architecture/2026-05-20-play-pool.md` (SIM-300) | ✅ Rebuilt | Full contract: pre-filter, FAISS materialization, recency lifecycle, sampler API, ≤2 GB budget. |
| `db/migrations/duckdb/0003_*.sql` (SIM-051) | ✅ Rebuilt | Idempotent `ADD COLUMN IF NOT EXISTS`; schema + version file were already correct (only the migration file was missing). |
| `tests/unit/test_data_engineer_sim051.py` | ✅ Rebuilt | 7 tests of the handedness spray flip. |
| `tests/unit/test_data_engineer_sim162.py` | ✅ Rebuilt | 5 tests; LeagueAverageProfiles non-empty inserts for all entity types. |
| `tests/unit/test_baserunner_steal_engine.py` (SIM-149) | ✅ Rebuilt | 9 invariant tests. |
| `tests/unit/test_ml_engines_sim150.py` (SIM-150) | ✅ Rebuilt | 3 calibration regressions (5 tests). |
| `docs/audit/2026-05-21-program-audit.md` | ⏳ Still open | Informational audit rebuild — out of this sprint's chosen scope. See §6. |
| `docs/audit/2026-05-21-prioritized-tickets.md` | ⏳ Still open | Same. |

## 4. Per-ticket result

**SIM-300 — Play-pool architecture spec.** Reconstructed from the handoff's §6 points.
Defines the five contracts SIM-301/302 build against: pre-filter keys (pitcher_id+bat_hand
for pitch, bat_hand for batted-ball, none for situation), tile disk layout +
`.meta`/`.rowids.npy` sidecars, the recency-boost (×2 last-2-seasons) lifecycle, the
four-method `PlayPoolSampler` API, and the ≤2 GB RAM envelope.

**SIM-051 — Spray-angle migration + tests.** The flip is inline SQL in
`PlayerProfileComputor._build_outcome_pool` (R→+spray, L→−spray, else NULL). Migration
0003 matches the 0002 style and is idempotent. Tests exercise the real CASE expression
through in-memory DuckDB for LHB/RHB pull, switch-hitters (resolved via per-PA
`bat_hand`, not roster `bats`), and NULL/`'S'` rows.

**SIM-162 — League-average regression.** Integration-style test on a real temp DuckDB:
builds the `derived.*` inputs, runs `LeagueAverageProfiles.compute()`, asserts non-empty
inserts for pitcher, batter, baserunner, catcher, and all 8 fielder positions.

**SIM-149 — Baserunner-steal invariants.** 9 tests: zero-distance-to-self, monotonic
ordering, [0,1] bounds, EB shrinkage, query_pair symmetry, below-minimum handling,
constant-column normalization, n-limit/sort, partition scoring.

**SIM-150 — Calibration regressions.** Drives the real `calibrate_sigma` path on
synthetic, well-separated data for catcher v2, FAISS pitch, and FAISS batted-ball;
asserts no COLLAPSED/NO_SPREAD sentinel and median similarity within ±0.05 of the 0.50
target; FAISS self-as-nearest-neighbour at ~0 distance.

**SIM-202 — Run-value constants.** `simulation/constants.py` defines `RUN_VALUES` (12
PA outcomes, 2024-anchored linear weights, cited to Tango *The Book* / FanGraphs) and a
separate `DEFENSIVE_RUN_VALUES`. The four defensive constants in
`player_profile_computor.py` now reference the module; numeric values preserved
byte-for-byte (0.75 / 0.90 / 0.25 / 0.125). Verified no full-suite regression.

**SIM-118 — Benchmark harness.** `bench_simulation.py` (pytest-benchmark): pitcher
`query()` (p50≤5 ms / p99≤20 ms), arsenal cache lookup (≤0.1 ms), GMM single-fit (under
the 10-min CI budget), plus skipped Phase 4/5 stubs. Thresholds are soft by default and
hard under `PERF_STRICT=1`. Weekly CI job at `.github/workflows/perf-weekly.yml` (Mon
04:00 UTC). One additive change: `pyproject.toml` `python_files` now also matches
`bench_*.py` (required for collection of the mandated filename).

**SIM-301 — Play-pool cache serializer.** `pipeline/batch/play_pool_cache.py` writes
`IndexFlatL2` tiles at `<season>/<pitcher_id>/<bat_hand>.faiss` + `.meta` JSON +
`.rowids.npy`, with the `pitcher_id=0` league-average fall-back for tiles under
`MIN_TILE_ROWS=50`, recency duplication, and atomic (tmp→fsync→replace) idempotent
stale-only rebuilds. CLI (`python -m pipeline.batch.play_pool_cache`) + `make
play-pool-cache` target, scheduled after the profile computor. Two documented
deviations from the spec, both sound: production tables have no `updated_at` (watermark
falls back to `MAX(game_date)`), and the handedness column is `stand` (builder accepts
`bat_hand` or `stand`).

**SIM-302 — PlayPoolSampler.** `simulation/play_pool_sampler.py` implements
`load_tile` / `sample_pitch` / `sample_batted_ball` / `reload_recent`, an LRU tile cache
(`max_resident_tiles`), fall-back resolution, and the only distance→weight conversion in
the system (`1/(d+ε)`, normalized; `return_distribution=True` yields an outcome
probability dict summing to 1.0). Tested with a true round-trip that runs the real
SIM-301 builder against a synthetic DuckDB and reads it back — proving the writer/reader
formats match.

**SIM-280 — RAM budget.** `docs/perf/2026-05-27-ram-budget.md`. Measured footprints
(numpy `.nbytes`, `faiss.serialize_index`, `tracemalloc`). Shared read-only payload
≈290 MB; `total(W) ≈ 290 MB + W×165 MB`. PASS at 1/4/8 workers (~0.45/0.93/1.58 GB);
RISK at 16 workers (~2.86 GB), bound by per-interpreter overhead, not data. Flags the
arsenal-cache (~0.58 GB/process — must be shared/lazy) and the situation engine's
1M-row list (~120 MB — convert to columnar to share).

**SIM-281 — Parallelism ADR.** `docs/architecture/2026-05-27-parallelism.md`. Keeps
`ProcessPoolExecutor` (adds a `max_workers=min(CPU-1, 10)` ceiling to stay under 2 GB),
specifies the `multiprocessing.shared_memory` zero-copy attach for read-only FAISS
tiles/indexes, the worker startup/tile-load sequence, and a PM sign-off section.

## 5. Verification

Independent QA/DevOps cross-validation (separate agent, audited files directly):

| Suite | Collected | Passed | Skipped | Failed | Exit |
|---|---|---|---|---|---|
| unit + regression | 834 | 833 | 1 | 0 | 0 |
| performance | 5 | 3 | 2 | 0 | 0 |

- Pre-sprint baseline was 771/1; this sprint adds 63 tests (+62 passing; the 1 skip is
  the pre-existing `test_engine_build_smoke` skip).
- Writer↔reader format match (SIM-301↔SIM-302) verified end-to-end; engine score
  discipline intact (distance→weight conversion lives only in the sampler).
- **No real defects found. Verdict: SHIPPABLE.**

### Test environment note (sandbox vs production)
The verification sandbox is Python 3.10; the project targets 3.11+. Two non-invasive
shims were used **only to run the suite here** and touch nothing in the repo:
a `sitecustomize` that backfills `datetime.UTC`, and `-p no:cacheprovider` (the
OneDrive-mounted `.pytest_cache` is unwritable). Production on 3.11/3.12 needs neither.

## 6. Open follow-ups (not blocking this sprint)

1. **Audit docs** `docs/audit/2026-05-21-program-audit.md` and
   `…-prioritized-tickets.md` remain missing (the two non-P0 §7 gaps). Rebuild at next
   PM pass if the original findings are still needed for traceability.
2. **Three empty scratch files** `tests/unit/test_zz_repro{,2,3}.py` (0 bytes) are
   OneDrive-locked against deletion from the sandbox; harmless (collect 0 tests).
   Delete manually on the host.
3. **`backlog.xlsx` regeneration.** The workbook was open/locked during this sprint, so
   it was not edited. `BACKLOG.md` is the authoritative surface; regenerate the xlsx
   from it to publish the closed-sprint state (move SIM-301/302/118/202/280/281 to
   Closed; refresh the Dashboard counts and the §7 banner).
4. **Next Phase 3 work:** SIM-303 (wire sampler into the sim-loop scaffold), SIM-220
   (backtesting framework — consumes the SIM-302 distribution API), SIM-201 (manager
   decision logic spec). Performance follow-ups from SIM-280/281: share the arsenal
   cache, columnarize the situation engine, revisit IVF/HNSW once tiles grow.

---

*End of sprint log.*
