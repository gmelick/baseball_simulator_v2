> SUPERSEDED by SIM-432 (2026-06-01): calibration is now live; the identity-map / not-yet-run-live caveats below are historical.

# Sprint — SIM-406: Fit + persist a CalibrationReport over real data + apply to all engines

**Date:** 2026-05-30
**Owner:** ML Engineer (Agent 3) · **Support:** Backend Developer (Agent 5), QA/DevOps (Agent 9)
**Ticket:** SIM-406 (P1) — depends-on SIM-408 (11/11 real-data engine build, closed)

---

## 1. Problem

The Phase-5 close audit flagged calibration as live-env debt: *"today: nothing
fits it; `apply_calibration` only on the pitcher engine."* The `CalibrationReport`
machinery + lossless JSON persistence + the win-prob `CalibrationMap` seam all
existed (SIM-346/361), and `SimilarityCalibrator.calibrate_from_population` could
fit batter/pitcher/fielder/baserunner params from the real `derived.*` tables —
but **nothing ran the fit + wrote the artifact**, and **only the pitcher engine
consumed a report**. So every other engine scored similarity on hardcoded
module-literal RBF sigmas. With SIM-408 now building 11/11 engines on real
all-seasons data, the fit is unblocked.

## 2. What shipped

### Engine layer — `apply_calibration` on all 8 similarity-score engines
A uniform `apply_calibration(report)` was added to **batter, fielder, baserunner,
catcher, baserunner_steal, pitcher_steal, manager** (pitcher already had the
SIM-346 arsenal seam). Each rebuilds its `WeightedRBFSimilarity` sub-score
scorers from the report's fitted sigmas — and reliability weights where the
report carries them (batter/fielder/baserunner). A `0.0`/`None` field falls back
to the locked module default, so a partial report degrades gracefully. The swap
is query-time only (no rebuild of the loaded population), so it is safe before or
after `build()`. The 3 **distance** engines (situation / pitch_pitch /
batted_ball) have no RBF sigma and are intentionally without the seam.

### Calibrator — extended to the 4 SIM-408-era engines
`SimilarityCalibrator` gained `_calibrate_catcher_params`,
`_calibrate_baserunner_steal_params`, `_calibrate_pitcher_steal_params`, and
`_calibrate_manager_params`, each reading the engine's own feature columns from
the new `derived.*` metrics tables (catcher's framing/blocking expressions are
mirrored verbatim), z-scoring, and solving the 0.50-median sigma. Each is guarded
so a missing table leaves the engine's sigmas at `0.0` (→ module default), never
aborting the fit. New `CalibrationReport` fields:
`sigma_catcher_{framing,blocking,throwing,deterrence}`,
`sigma_baserunner_steal_{tendency,success}`, `sigma_pitcher_steal_outcome`,
`sigma_manager_{usage,aggression,platoon}` — all round-trip through the existing
lossless `to_json`/`from_json`.

### Boot wiring
- `api.state.apply_calibration_to_engines(engines, report)` — applies a loaded
  report to every engine exposing the seam, best-effort per engine (one bad
  engine never blocks boot), returning the applied names.
- `api.main` lifespan now loads the `CalibrationReport` **once** and both
  (a) applies it to `app.state.engines` and (b) derives the win-prob
  `CalibrationMap` from its reliability curve. `app.state.calibration_report` is
  exposed alongside `app.state.calibration_map`.

### Fit + persist tooling
- `scripts/fit_calibration.py` — builds the pitcher engine, **samples** same-hand
  arsenal W₂ distances (fast vs. the O(N²) full cache) to anchor `arsenal_gamma`,
  runs `calibrate_from_population`, and writes `CalibrationReport.to_json()` to
  `--output` (default `CALIBRATION_REPORT_PATH` → `/data/calibration.json`).
  Flags: `--seasons`, `--no-arsenal`, `--target-median`, `--validate`.
- `make calibrate` wraps it; `scripts/nightly_ingest.sh` re-fits over the full
  population each night (`--no-arsenal` for speed/memory); `docker-compose.yml`
  sets `CALIBRATION_REPORT_PATH` on the `app` service.

### Tests
`tests/unit/test_ml_engines_sim406.py` (29 tests): per-engine apply (override /
default fallback / reliability-weight override), the fielder DP+pivot weight
concatenation + the one-array-present partial-report path, the live-instance sigma
fallback chain, the 8-engine seam roster, `apply_calibration_to_engines`
(apply / skip-no-seam / resilient-to-failure / None-report), the new report-field
JSON round-trip, and the `_fit_sigma` degenerate→0.0 sentinel.

### Adversarial self-review (4-dimension workflow)
A 4-agent review (engines / calibrator / wiring / tests) with per-finding
adversarial verification found NO critical/high/medium defects in the production
path. Two LOW findings were folded in:
1. **Fielder DP/pivot partial-report consistency.** `apply_calibration` now builds
   the middle-IF DP scorer from INDEPENDENT per-array fallbacks, so a report with
   only `reliability_weights_if_dp` (no pivot) feeds the fitted DP weights to both
   the middle and corner scorers instead of reverting the whole concatenation to
   defaults. (Production always sets both arrays, so this only affected hand-built
   partial reports — but it now matches the graceful-degradation contract.)
2. **Degenerate-fit sentinel.** The four new sub-calibrators route through
   `SimilarityCalibrator._fit_sigma`, which returns `0.0` (the keep-default
   sentinel) when a feature matrix has no variance — e.g. the manager USAGE column
   gated NULL on SIM-427. Previously `calibrate_sigma`'s degenerate `1.0` fallback
   was persisted and applied, silently overriding the engine's tuned sigma (harmless
   only because `RBF_SIGMA_USAGE` happened to equal 1.0). The contract now holds
   regardless of the module default's value.

Other LOW notes left as-is (out of scope / pre-existing): `api.state.load_calibration_map`
is now app-orphaned but still unit-tested (SIM-361) and a valid standalone helper;
the Makefile `type-check` target's `--python-version 3.13` predates this ticket.

## 3. Verification

- `ruff check` — All checks passed; `ruff format --check` — clean; `mypy
  similarity/ pipeline/ api/` — Success, no issues.
- `pytest tests/unit/test_ml_engines_sim406.py` — 19 passed; the SIM-346 +
  SIM-361 calibration suites stay green.
- Live fit (`make calibrate` / nightly) writes `/data/calibration.json`; the
  serving lifespan logs `SIM-406: applied fitted calibration to N engines`.

## 4. Design decisions / boundaries

- **Sigmas, not reliability weights, for the 4 newer engines.** The sigma is the
  median-target knob; the reliability weights are stabilization-research priors
  that don't need population fitting. (batter/fielder/baserunner still get fitted
  reliability weights, as the report already carried them.)
- **Engine layer, not the full-pool sampler.** SIM-406 calibrates the per-engine
  similarity scorers (the per-tile path + the `/api/similarity/*` routes). The
  production `FullPoolSampler` (SIM-422→429) scores over engine *artifacts* with
  its own embedding-space sigmas (`batter_sigma`/`sit_sigma`); calibrating those
  is a different basis and belongs to the SIM-429 run-conversion track, not here.
- **EB priors are build-time.** `apply_calibration` swaps only query-time scorers;
  the report's EB priors are applied during profile shrinkage at `build()`, not
  re-applied post-build.

## 5. Out of scope → SIM-407

The win-probability **reliability-curve** fit (sim-vs-actual ECE/Brier, the
isotonic/Platt curve that makes the win-prob `CalibrationMap` non-identity) and
the **prop-PMF validation + ablation/walk-forward** are SIM-407. This ticket
leaves `reliability_curve` empty, so the win-prob map stays identity until
SIM-407 fits one — the engine-layer similarity calibration is the SIM-406
deliverable.
