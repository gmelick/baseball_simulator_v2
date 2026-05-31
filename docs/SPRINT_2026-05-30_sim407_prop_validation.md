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
reliability is wrong. So SIM-407 adds the **binary counterpart** in
`simulation/prop_validation.py`, reusing SIM-220's log-loss epsilon so the two
layers agree numerically. The multi-class ablation/walk-forward stays where it
already lives (SIM-220).

## 3. What shipped

### `simulation/prop_validation.py` (pure, deterministic, no DB/FAISS/RNG)
- **Binary metrics:** `binary_reliability_curve` (bins the POSITIVE-class prob),
  `binary_ece`, `binary_brier`, `binary_log_loss` (eps-clipped → finite on
  confident-wrong).
- **`fit_reliability_curve`** → `[[predicted_p, observed_p], …]`, the exact shape
  `CalibrationReport.reliability_curve` consumes; endpoint-anchored to span [0,1];
  empty/too-sparse → `[]` (win-prob map stays identity, the documented default).
- **`validate_prop_over_under`** — `(PropDistribution, actual)` pairs + a line →
  `PropCalibration` (ECE/Brier/log-loss + mean-predicted vs observed over-rate),
  using the same sportsbook push convention `PropDistribution.p_over` encodes.
- **`pit_values` / `pmf_coverage`** — deterministic mid-P PIT goodness-of-fit.
- **`real_props_from_pa_events`** — the prop ground truth: aggregates the per-PA
  `events` label from `raw.pitches` into real per-player totals (batter H/HR/TB,
  pitcher K/BB). Same pitch table the engines are built from — NO extra data
  source. RBI/ER/OUTS are deliberately NOT derived (the event label carries no
  runs-driven-in / earned-run / per-event-out info; deriving them would corrupt
  the calibration). Intentional walks are excluded from BB (the sim models no IBB
  decision). `pair_props_for_validation` pairs the actuals with the sim PMFs.
- **`build_validation_report`** aggregator + **`PropValidationReport`** (JSON
  round-trippable) + **`write_reliability_curve_to_calibration_report`** (writes
  the fitted curve into the on-disk `CalibrationReport` without touching sigmas).

### `scripts/validate_props.py` (offline orchestration)
Fetches Final games from `raw.games`, resolves each via the SIM-353 path, and
**replays N iterations per game through the SIM-356 `record_game_plays` seam** —
the same factory the API/batch runner use — collecting one `GameSimResult`
(carrying `.boxscore`) per iteration. `BatchRunner` retains only the aggregate
summary, so per-iteration boxscores (which the prop PMFs need) come from this
seam, exactly as `/api/.../boxscore` (`_build_prop_set`) does. It pairs the sim
home-win-prob against the real score AND the sim prop PMFs against the real
per-player totals from `raw.pitches.events`, builds + writes the report, and with
`--write-calibration` writes the fitted curve into `CALIBRATION_REPORT_PATH`.
`make validate-props` wraps it. The asyncpg pool is created inside the `try` and
closed in `finally`; per-game failures are skipped, not fatal. `--no-props` runs
the win-prob fit alone (a faster smoke run).

### The SIM-406 ↔ 407 loop
`make calibrate` (SIM-406) writes the engine sigmas to `calibration.json` with an
empty reliability curve. `make validate-props --write-calibration` (SIM-407) fits
the curve and writes it INTO the same file. On the next boot,
`CalibrationMap.from_report` turns it into a monotone correction and the win-prob
layer applies it.

## 4. Props derived from `raw.pitches` (the over-cautious gate, removed)

An earlier draft gated prop-pairing behind "needs a completed-game box-score
source not assumed present." That was wrong: `raw.pitches.events` already stores
the per-PA outcome label (single/double/.../strikeout/walk/...) with `batter` and
`pitcher` ids on the terminal pitch of each PA — the SAME table the engines are
built from. So the real per-player prop totals are derivable with no new source,
and props are validated by default (`--no-props` is now just a faster win-prob-only
mode). Only the props EXACTLY recoverable from the label are scored (H/HR/TB,
K/BB); RBI/ER/OUTS are out of scope until a richer box source is wired.

## 5. Verification

- `pytest tests/unit/test_ml_engines_sim407.py` — 35 passed. SIM-406 (29) + SIM-220
  + SIM-330 win-prob suites stay green (351 across the combined run).
- ruff + ruff-format + mypy clean on the new module/script (mypy: no issues across
  the api/ + simulation/ scope).
- End-to-end handoff test: a curve fitted on an over-confident forecaster, written
  onto a `CalibrationReport`, yields a non-identity `CalibrationMap` that pulls 0.9
  down — proving the loop closes. Real-prop derivation unit-verified (hits/HR/TB,
  K/BB, IBB excluded, case/blank tolerance) end-to-end with the over/under scorer.

## 6. Review-caught bugs (fixed pre-push; never shipped)

A 3-dimension adversarial review of the first draft caught a **CRITICAL**: the
script constructed `BatchRunner(machine_factory=None, …)`, a kwarg `BatchRunner`
doesn't accept (the factory rides on `GameSpec` / `record_game_plays`) — it would
`TypeError` before any game + leak the pool. The tightening pass then found a
second instance of the same class of error: a non-existent `_run_batch_results`
seam. Both are replaced with the proven `record_game_plays` per-iteration
collector, and locked with contract-guard unit tests (the BatchRunner signature +
the `record_game_plays` keyword seam + the `_collect_game_results` signature).

## 7. Out of scope / next

- Multi-class outcome ablation/walk-forward already exists (SIM-220) — SIM-407 adds
  the binary win-prob/prop layer it lacked, not a replacement.
- RBI/ER/OUTS prop validation (needs a richer box source than the event label).
- **Live follow-up:** run `make validate-props --write-calibration` over a
  multi-season window on the live stack to actually fit + persist the win-prob
  curve (the code path is in place; it has not yet been run against live data).
- Remaining open P1: SIM-430 (full-pool `/simulate` throughput).
