# Sprint — SIM-407: Validate prop PMFs + fit the win-probability reliability curve

**Date:** 2026-05-30
**Owner:** ML Engineer (Agent 3) · **Support:** Betting/Markets Analyst (Agent 8), QA/DevOps (Agent 9)
**Ticket:** SIM-407 (P1) — depends-on SIM-406 (closed same day)

---

## 1. Problem

The simulator emits two kinds of probability that reach users/bets: a home/away
**win probability** (SIM-330) and per-player **prop PMFs** (SIM-329, e.g.
`P(K ≥ 6.5)`). Both were *uncalibrated model probabilities* — nothing had ever
checked them against what actually happened. The Phase-5 audit: *"PMFs have no
calibration seam/backtest; ablation is synthetic-only."* And SIM-406 fitted the
similarity dials but deliberately left `CalibrationReport.reliability_curve` EMPTY,
so the win-prob `CalibrationMap` was still the identity. SIM-407 is the validation
+ the win-prob curve fit.

## 2. Why a new binary layer (not just SIM-220)

SIM-220 (`similarity/backtesting/backtester.py`) already has a calibration spine
— ECE / Brier / log-loss + reliability curve + `walk_forward_ablation` — but it is
**multi-class** (a distribution over discrete PA outcomes), and its "confidence" is
the *argmax* class probability. A win probability and an over/under are **binary**
events (home won? / went over?). Scoring a binary forecast with argmax-confidence
reliability is wrong (it can't see a 0.3 forecast as "the positive class 30% of the
time"). So SIM-407 adds the **binary counterpart** in `simulation/prop_validation.py`,
reusing SIM-220's log-loss epsilon so the two layers agree numerically. The
multi-class ablation/walk-forward is left where it already lives (SIM-220).

## 3. What shipped

### `simulation/prop_validation.py` (pure, deterministic, no DB/FAISS/RNG)
- **Binary metrics:** `binary_reliability_curve` (bins the POSITIVE-class prob, not
  argmax confidence), `binary_ece` (population-weighted mean bin gap), `binary_brier`,
  `binary_log_loss` (eps-clipped → finite on confident-wrong).
- **`fit_reliability_curve`** → `[[predicted_p, observed_p], …]`, the exact shape
  `CalibrationReport.reliability_curve` consumes. Endpoint-anchored to span [0,1].
  Empty / too-sparse input → `[]` (the win-prob map stays identity, the documented
  "well-calibrated until a curve says otherwise" default).
- **`validate_prop_over_under`** — `(PropDistribution, actual)` pairs + a line →
  `PropCalibration` (ECE/Brier/log-loss + mean-predicted vs observed over-rate). Uses
  the SAME over/under convention `PropDistribution.p_over` encodes (sportsbook push:
  a value ON an integer line is NOT an over).
- **`pit_values` / `pmf_coverage`** — deterministic mid-P PIT goodness-of-fit
  (calibrated ⇒ PIT mean ≈ 0.5, central interval covers at nominal).
- **`build_validation_report`** aggregator + **`PropValidationReport`** (JSON
  round-trippable) + **`write_reliability_curve_to_calibration_report`** (writes the
  fitted curve into the on-disk `CalibrationReport` without touching its sigmas).

### `scripts/validate_props.py` (offline orchestration)
Fetches Final games from `raw.games`, resolves each via the SIM-353 path, runs the
real `BatchRunner` (the SAME seam the API serves), pairs the sim home-win-prob
against the actual result, builds + writes the report, and with `--write-calibration`
writes the fitted curve into `CALIBRATION_REPORT_PATH`. `make validate-props` wraps it.
Pool + runner are closed in `finally`; per-game failures are skipped, not fatal.

### The SIM-406 ↔ 407 loop
`make calibrate` (SIM-406) writes the engine sigmas to `calibration.json` with an
empty reliability curve. `make validate-props --write-calibration` (SIM-407) fits
the curve and writes it INTO the same file. On the next boot, `CalibrationMap.from_report`
turns it into a monotone correction and the win-prob layer applies it.

## 4. Scope boundary (win-prob vs prop pairing)

The **win-prob fit** needs only the real final score (always in `raw.games`) — it
is the shippable deliverable and runs by default. The **prop-pair population** needs
the real per-player box-score line for each completed game; that requires a
completed-game box-score source the sandbox doesn't assume is present, so the prop
scaffolding (metrics, aggregation, report fields, tests on synthetic PMFs) is
complete and unit-verified, but the script's prop-pairing step is gated behind a box
source (`--no-props` runs win-prob alone). When a box source is wired, the same
`validate_prop_over_under` + `build_validation_report` path populates the prop rows
with no code change.

## 5. Verification

- `pytest tests/unit/test_ml_engines_sim407.py` — 28 passed. SIM-406 (29) + SIM-220
  calibration suites stay green.
- ruff + ruff-format + mypy clean on the new module/script (mypy: no issues across
  the api/ + simulation/ scope).
- End-to-end handoff test: a curve fitted on an over-confident forecaster, written
  onto a `CalibrationReport`, yields a non-identity `CalibrationMap` that pulls 0.9
  down — proving the loop closes.

## 6. Out of scope / next

- Multi-class outcome ablation/walk-forward already exists (SIM-220) — SIM-407 adds
  the binary win-prob/prop layer it lacked, not a replacement.
- Populating the prop-pair rows over real box scores (needs the completed-game box
  source) and running the full multi-season `validate-props --write-calibration` on
  the live stack are the live follow-ups; the code path is in place.
- Remaining P1: SIM-430 (full-pool `/simulate` throughput).
