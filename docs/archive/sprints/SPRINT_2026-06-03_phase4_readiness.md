# Sprint 2026-06-03 — Phase 4 Readiness (executed 2026-05-21)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-21 · Disposition: ✅ all 7 tickets accepted after independent QA cross-validation*

Second Phase-3/4 sprint. Goal: land the Phase-4-gating performance specs and the
remaining unblocked perf/quality work, and complete Phase 3 by wiring the
`PlayPoolSampler` into a simulation-loop scaffold. Companion to `CHANGES.md` (detail)
and `BACKLOG.md` (one-line rows).

## 1. Tickets and owners

| Ticket | Owner(s) | Deliverable |
|---|---|---|
| SIM-114 | Performance Engineer + ML Engineer | `docs/architecture/2026-06-03-faiss-index-design.md` |
| SIM-303 | Backend Developer | `simulation/sim_loop.py` + `tests/unit/test_backend_sim303.py` |
| SIM-119 | Performance Engineer + Backend Developer | `docs/perf/2026-06-03-sim-loop-time-budget.md` |
| SIM-113 | Performance Engineer + Data Engineer | `pipeline/batch/player_profile_computor.py` (GMM batch) + `tests/unit/test_perf_eng_sim113.py` |
| SIM-075 | ML Engineer + Performance Engineer | `similarity/engines/pitcher_similarity.py` (arsenal cache) + `tests/unit/test_ml_engines_sim075.py` |
| SIM-074 | Data Engineer | `pipeline/batch/player_profile_computor.py` (barrel_rate) + `tests/unit/test_data_engineer_sim074.py` |
| SIM-090 | Data Engineer | `pipeline/etl/etl_historical_loader.py` (connection pool) + `tests/unit/test_data_engineer_sim090.py` |

Execution model (per Greg's standing preference): role subagents implement, an
independent QA/DevOps pass audits acceptance criteria against the actual files and
runs the suite. SIM-074 and SIM-113 both edit the 4,200-line profile computor and were
therefore serialized (SIM-074 → SIM-113) rather than run in parallel.

## 2. Per-ticket result

**SIM-114 — FAISS index design spec.** Benchmarked `IndexIVFFlat` vs `IndexFlatL2`
on synthetic 10-dim vectors at 3k / 50k / 250k / 1M (faiss 1.13.2, single thread).
Headline: at 1M vectors, IVFFlat (`nlist=512, nprobe=32`) = recall 1.000 @ 188 µs/query
vs flat 966 µs (~5.1×); `nprobe=16` = 0.996 recall @ 99 µs. Decision: **per-tile
`IndexFlatL2` stays correct** (pre-filtered tiles are 200–5,000 vectors where flat is
1.9–8.8 µs and exact); IVFFlat only above a **50,000-vector/index crossover** (large
fall-back or any non-tiled monolithic index). Confirms SIM-300's per-tile flat choice
and SIM-281's previously-unmeasured ~50k assertion with real numbers. Shared-memory and
≤2 GB carried from SIM-281/SIM-280.

**SIM-303 — sampler wired into sim loop (Phase 3 complete).** `simulation/sim_loop.py`
adds `PlateAppearanceSimulator(sampler)` with `simulate_pitch(state)`: builds a query
fingerprint (stubbed, Phase-4 TODO), calls `sampler.sample_pitch`, and on contact
(`in_play`) calls `sampler.sample_batted_ball`, returning a `PlayResult` dict with a
placeholder run value from `RUN_VALUES`. Clearly marked Phase-4 TODOs for manager
decisions, fielding/baserunning resolution, and the full 8-step loop. 4 tests build real
tiles via the SIM-301 builder and exercise contact/non-contact paths with a fixed RNG.

**SIM-119 — per-step time budget.** Eight loop steps budgeted to ~1.23 ms/pitch
(measured roll-up ~0.62 ms on this box); per game (~300 pitches) ~0.37 s budgeted vs the
2 s SLA (≈81% headroom); 100-game batch under the SIM-281 7-worker model ~5.6 s vs 30 s.
Anchored to the SIM-118 bench (pitcher `query()` ≈0.5–0.76 ms) and a timed `sample_pitch`
(~42 µs warm). Riskiest: step-2 `query()` (~81% of the budget) and any per-pitch DuckDB
outcome fetch (PK point-lookup in prod; mitigations tie to SIM-075/SIM-113).

**SIM-113 — GMM batch performance.** Three changes in the profile computor's GMM pass:
(1) dynamic pool size `_resolve_gmm_workers()` replaces the hardcoded `n_workers = 8`
(it oversubscribed small CI boxes and underused large hosts); (2) chunked submission via
`_fit_gmm_batch()` — one task per worker instead of one per pitcher, carrying compact
float32 arrays rather than DataFrames (collapses per-pitcher submission/result IPC);
(3) `_flush_gmm_results()` writes all results in three set-based statements instead of
~5,600 per-pitcher UPDATE/INSERTs. 10 tests; per-pitcher crash→fallback semantics
preserved at chunk granularity.

**SIM-075 — arsenal W2 cache vectorized.** `ArsenalCache` now keeps a dense symmetric
NumPy distance matrix + an id→row index alongside the dict; all-vs-one lookups in
`HandednessPartition.score_all` are a single row slice instead of an O(N) dict loop.
Numerically identical (NaN→inf mapping preserves the finite-mask behavior); ~2.9×
faster warm. 8 tests assert equivalence, self-distance 0, symmetry, NaN handling.

**SIM-074 — barrel_rate (full Statcast).** Replaced the placeholder
`type IN ('D','E','X')` count with the real sliding scale: barrel ⇔ `EV ≥ 98` and
`LA ∈ [max(8, 26−(EV−98)), min(50, 30+2·(EV−98))]`, for overall / vs-L / vs-R. 6 tests
verify the band edges (98@26–30, 100@24–34, 116@8–50; <98 never; NULL excluded).

**SIM-090 — ETL connection pool.** `HistoricalDataLoader._get_conn()` now draws from a
lazily-created `psycopg2.pool.ThreadedConnectionPool` (`getconn`/`putconn`, env-sized
`ETL_DB_POOL_MIN/MAX`) and `close()` calls `closeall()`; wired into the CLI `finally`.
Replaces dozens of per-game connect/teardown cycles. 8 tests with a mocked pool.

## 3. Verification

Independent QA pass run in chunks (the full suite exceeds the sandbox's 45 s/call limit):
engines+ML, regression (55), data-engineering (66), backend/API/live (102), computor +
SIM-113 (98), and the new sprint files — all green; performance suite 3 passed / 2
skipped. **New baseline: 870 unit+regression passing** (834 + 36 new), 1 pre-existing
skip, 0 failures.

### Environment note — OneDrive truncation (important)
Editing the three large source files (`player_profile_computor.py` ~4,275 lines,
`pitcher_similarity.py` ~1,910, `etl_historical_loader.py` ~2,077) repeatedly truncated
the **sandbox mount copies** mid-sync (the documented OneDrive hazard, HANDOFF §3). The
**authoritative files were verified complete and correct via the file tools**; the mount
copies were rebuilt (clean tails re-appended; spurious null bytes stripped from two test
files) so the suite could run. Tests run with `PYTHONPATH=/tmp/sbshim` (datetime.UTC
shim), `PYTHONPYCACHEPREFIX=/tmp/pyc3` (avoids stale locked `__pycache__`), and
`-p no:cacheprovider`. Production on Python 3.11/3.12 needs none of these.

## 4. Open follow-ups

1. **Next Phase 4 work:** SIM-220 (backtesting framework — consumes the SIM-302
   distribution API), SIM-201 (manager decision logic), and the Phase-4 simulation-loop
   steps that flesh out the SIM-303 scaffold (manager/fielding/baserunning).
2. **Performance follow-ups** surfaced by SIM-280/281/114: share the arsenal cache
   read-only across workers, columnarize the situation engine, and only revisit IVFFlat
   if any index crosses 50k vectors.
3. **Audit docs** `docs/audit/2026-05-21-*.md` rebuilds remain outstanding (from §7).

---

*End of sprint log.*
