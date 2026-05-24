# Sprint 2026-07-08 — Phase 4 Betting Chain + Bug Cleanup (executed 2026-05-23)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-23 · Disposition: ✅ all 7 tickets accepted after independent cross-validation*

Fourth Phase-4 sprint. With the loop + output contracts in place, this sprint stands up the
**betting chain** (prop PMFs → CLV → real odds ingestion), clears the **two remaining ⚠ audit
bugs** (park-factor SQL + ML calibration, plus the folded-in data-layer fixes), and fills the
**shared-memory perf mechanism**. The PM planned the sprint; role subagents implemented; the
cross-validation ran the full suite. Companion to `CHANGES.md` (per-agent detail) and
`BACKLOG.md` (banners).

## 1. Plan and execution model

The enabling property this sprint: **file ownership was disjoint.** Each large file had a
single owning ticket — SIM-340 → `pipeline/live/live_ingestion_pipeline.py`; SIM-336+345 →
`pipeline/batch/player_profile_computor.py`; SIM-346 → `similarity/engines/pitcher_similarity.py`
+ `similarity/similarity_calibration.py`; SIM-333 → `simulation/batch_runner.py` +
`simulation/play_pool_sampler.py`; SIM-329/339 were new modules. So five tickets ran fully in
parallel and only the betting chain had a true data dependency:

- **Wave A (5 parallel):** SIM-329 ∥ SIM-340 ∥ SIM-336+345 ∥ SIM-346 ∥ SIM-333.
- **Wave B:** SIM-339 (CLV) after SIM-329.
- **Wave C:** cross-validation (full suite, chunked).

Deferred to Sprint 5 (PM decision): SIM-323 (manager logic, L — exclusive `sim_loop.py`
editor, the hook stubs suffice) + SIM-349 (depends on 323); SIM-334/335 (perf columnarize +
CI gate); SIM-347/348 (stress + live-pipeline tests); P3 hygiene SIM-341–344.

## 2. Tickets and owners

| Ticket | Owner(s) | Deliverable |
|---|---|---|
| SIM-329 | Backend + ML + Betting | `simulation/prop_distributions.py` + `tests/unit/test_backend_sim329.py` |
| SIM-339 | Betting (lead) + ML | `betting/clv_engine.py` (+ `betting/__init__.py`) + `tests/unit/test_betting_sim339.py` |
| SIM-340 | Data (lead) + Betting | `pipeline/live/live_ingestion_pipeline.py` + Alembic `0013` + `tests/unit/test_data_engineer_sim340.py` |
| SIM-336+345 | Baseball Analyst + Data | `pipeline/batch/player_profile_computor.py` + DuckDB `0007` + schema v7 + `tests/unit/test_data_engineer_sim336.py` |
| SIM-346 | ML Engineer | `similarity/engines/pitcher_similarity.py` + `similarity/similarity_calibration.py` + `tests/unit/test_ml_engines_sim346.py` |
| SIM-333 | Performance Engineer | `simulation/batch_runner.py` + `simulation/play_pool_sampler.py` + `tests/unit/test_perf_eng_sim333.py` |

## 3. Per-ticket result

**SIM-329 — prop PMFs.** New `simulation/prop_distributions.py`: for each player and prop a full
integer-support PMF over the N per-game `BoxScore`s — pitcher K/BB/ER/outs, batter H/HR/RBI/TB
— with mean/median/std, a Wald `mean_ci()` (reusing the SIM-327 `ConfidenceInterval`), and
`p_over`/`p_under`/`p_push`/`p_at_least` at any integer or half-integer line (the core betting
query, preserving the half-integer-splits-cleanly / integer-line-has-push convention). TB is
computed as `h + 3·hr` — a documented **lower bound** isolated in one helper (2B/3B aren't
tracked in the boxscore yet; the single upgrade point is flagged). A DNP game contributes a 0
so the denominator stays the full N; absent player → `None`, never raises. Pure numpy, no
DB/RNG. 18 tests.

**SIM-339 — CLV engine.** New `betting/clv_engine.py`: implied probability from American odds
(±, with the inverse round-trip), two-way **proportional de-vig** + multi-way overround
normalization, edge = sim − no-vig market prob, EV per unit stake vs the **offered (vigged)**
price, and CLV on the no-vig probability scale (positive = beat the close; an odds-space delta
is also reported for display). Reports per market: `moneyline_edge_report` (SIM-330
`WinProbability`), `prop_edge_report` (SIM-329 PMFs), `total_over_under_edge_report` (SIM-327
raw score arrays). Odds injected via `OddsQuote`/`TwoWayMarket`; deterministic, no DB. 27 tests.

**SIM-340 — odds + prop ingestion.** Wired the previously-dead `_persist_prop_odds` into the
live refresh cycle (`_persist_prop_odds_cycle` after `_persist_odds`, cadence-gated at 60s so
it doesn't fire on every WS pitch signal); implemented `mark_closing_prop_lines` (mirrors
`mark_closing_lines` with `DISTINCT ON (player_id, prop_stat, book)`); multi-book config with
an `is_sharp_book` flag; opening-line capture (`capture_opening_prop_lines`, the SIM-138 hook);
dedup via `_prop_odds_hash` + `ON CONFLICT DO NOTHING`, requiring an `odds_hash` column →
Alembic `0013_sim340_prop_odds_dedup` (Postgres; revises 0012, single head). 17 tests; live-
pipeline regression (45) green.

**SIM-336 + SIM-345 — park factor + data layer.** Park factors: rewrote `_compute_park_factors`
into a `factors` CTE + a single grouped `UNPIVOT INCLUDE NULLS` so `factor_overall` is produced
before it's referenced and all 9 factor types always get a row; real `factor_vs_l`/`factor_vs_r`
splits on batter `stand` (switch hitters count to overall only); documented pool-neutralization
policy. Data layer: watermark `>=` + a `source_row_count` guard (catches same-date
late/doubleheader rows), a `_canonical_ref_season` consistent across all three pools,
`recency_weight` NOT NULL parity, and an enforced `stand` vs `bat_hand` pool contract. DuckDB
migration `0007` (idempotent ADD COLUMN + NULL-backfill-before-SET-NOT-NULL), schema v6→**v7**.
18 tests; computor regression (116) green.

**SIM-346 — ML calibration.** Replaced the no-arsenal ×1.0 no-op with true weight
redistribution (`_TOTAL_WEIGHT / WEIGHT_COMMAND` ≈ 2.857×) in both the vectorized and scalar
score paths, so a GMM-less pitcher's command-only composite lands on the full [0,1] scale.
Reconciled to ONE canonical linear `exp(-W₂/ARSENAL_SCALE)` with `ARSENAL_SCALE = 4.10` (the
locked value, replacing a drifted 4.25 literal) and removed the divergent squared-form path;
added `arsenal_scale_from_gamma(gamma, median)` mapping the calibrator's squared
`arsenal_gamma` to the linear scale. Wired `CalibrationReport.arsenal_gamma` into the engine
via `apply_calibration(report)` (the calibrator now actually populates `arsenal_gamma`), with
the 4.10 default if no report. Added a drift regression test (median arsenal similarity ≈ 0.50;
no-arsenal path comparable to full). 15 tests; pitcher-engine regression (73) green, no
expected-value corrections needed.

**SIM-333 — shared-memory attach.** Filled the SIM-332 seam: the parent copies the read-only
bulky buffers (situation KDTree ~88 MB, RBF/GMM matrices ~18 MB, FAISS-tile rowids + source
vectors ~64 MB) ONCE into named `multiprocessing.shared_memory` segments; `_worker_init`
attaches by name into a process-global and workers rebuild numpy views with no copy
(`PlayPoolSampler.attach_shared_tile`). Resident RAM stays ≈290 MB flat + W×~165 MB private →
≤2 GB to ~8–10 workers. The FAISS index header stays per-worker (not zero-copyable; documented).
Parent owns create/unlink; workers attach/close, never unlink; no-segments fallback preserves
the SIM-332 disk path. 13 tests (+1 slow 4-worker pool); SIM-332 (21) green; no `/dev/shm` leak.

## 4. Verification

The independent QA subagent again hit the shared session limit, so the orchestrator ran the
cross-validation directly (still independent of the implementers — auditing their files, not
self-certifying authored code). Every sprint file integrity-checked (compile + null-byte
clean); schema v7 + migration 0007 + Alembic 0013 verified; then the FULL unit+regression
suite run in chunks — Sprint-4 files; the regression-sensitive computor / pitcher /
live-pipeline / batch-runner / sampler areas; regression; data-engineering; ML;
engine/component; the loop incl. real-FAISS sim303/sim319; older backend; api/perf/smoke; the
slow-marked tests; and performance — every file covered, **zero failures**. **New baseline:
1380 passed / 1 skipped / 0 failed** unit+regression (1381 collected = 1375 not-slow + 6 slow;
the lone skip is the pre-existing engine-build-smoke skip), reconciling with 1271 + 109;
performance 3 passed / 2 skipped. No regressions; **DuckDB schema v7**.

### Environment note
Five large files were edited (the computor ~4,400 lines is the biggest truncation risk in the
repo). OneDrive truncation/null-byte injection hit them repeatedly on write; each was repaired
on the mount per the documented recipe with the authoritative Windows files verified intact.
The `/tmp` shim + pyc dir were recreated (the sandbox cycles between sessions).

## 5. Open follow-ups

1. **Sprint 5 (manager + remaining hardening):** SIM-323 (manager decision logic — pull /
   pinch-hit / bullpen-by-leverage / bunt; exclusive `sim_loop.py` editor) → SIM-349
   (situational decisions: IBB / sac / hit-and-run); SIM-334 (columnarize situation engine) +
   SIM-335 (CI perf/RAM gate); SIM-347 (stress 100×30) + SIM-348 (live-pipeline tests).
2. **Prop TB upgrade:** track 2B/3B in the boxscore so SIM-329 TB stops being a lower bound.
3. **SIM-315** — the OneDrive move + integrity guard remains the biggest standing infra risk.
4. **`backlog.xlsx`** needs regeneration from `BACKLOG.md`.

---

*End of sprint log.*
