"""
simulation/prop_validation.py
=============================
SIM-407 — validate the simulator's PROBABILITY outputs against real outcomes and
FIT the win-probability reliability curve that SIM-406 left empty.

WHY THIS EXISTS
---------------
SIM-329 (:mod:`simulation.prop_distributions`) turns a Monte-Carlo run into
per-player prop PMFs (``P(K >= 6.5)`` etc.); SIM-330
(:mod:`simulation.win_probability`) turns it into a home/away win probability.
Both are *uncalibrated model probabilities* until something checks them against
what actually happened. The Phase-5 audit flagged exactly this: "PMFs have no
calibration seam/backtest; ablation is synthetic-only", and SIM-406 closed the
similarity-engine calibration but deliberately left
``CalibrationReport.reliability_curve`` EMPTY (so the win-prob ``CalibrationMap``
stayed the identity) — fitting it is THIS ticket.

The SIM-220 backtester (:mod:`similarity.backtesting.backtester`) already has the
MULTI-CLASS metric spine (ECE / Brier / log-loss over a discrete-outcome
distribution) and the walk-forward ablation harness. But a win probability and an
over/under are BINARY events (home-won? / went-over?), which the multi-class
confidence reliability does not score correctly (its "confidence" is the argmax
prob, not the probability of the positive class). This module is the BINARY
counterpart — the piece the prop/win-prob validation needs — and it reuses the
SIM-220 log-loss epsilon convention so the two layers agree.

WHAT IT PROVIDES
----------------
1. **Binary calibration metrics** (``binary_reliability_curve`` / ``binary_ece`` /
   ``binary_brier`` / ``binary_log_loss``): is a forecast of "p" right p of the
   time?

2. **The win-prob reliability fit** (``fit_reliability_curve``): bins predicted
   home-win probabilities, measures the observed home-win rate per bin, and emits
   the ``[[predicted_p, observed_p], ...]`` anchor list that
   ``CalibrationReport.reliability_curve`` consumes — closing the SIM-406 → 407
   handoff so ``CalibrationMap.from_report`` becomes a real (monotone) map.

3. **Prop-PMF over/under validation** (``validate_prop_over_under``): given
   ``(PropDistribution, actual_value)`` pairs + a book line, scores the PMF's
   ``p_over(line)`` against the realized over/under — the prop-PMF backtest the
   audit said was missing.

4. **PMF goodness-of-fit** (``pit_values`` / ``pmf_coverage``): the (mid-)PIT of a
   discrete PMF — if the PMFs are calibrated the PIT is ~Uniform(0,1), so its mean
   sits near 0.5 and central intervals cover at their nominal rate.

5. **A persistable report** (:class:`PropValidationReport`) aggregating the above
   + the fitted win-prob curve, JSON round-trippable like ``CalibrationReport``.

DETERMINISM
-----------
Pure NumPy / stdlib over arrays — no DB, no FAISS, no RNG (the PIT uses the
deterministic mid-P convention, not a random draw). The same inputs always yield
the same metrics, so the unit tests pin exact numbers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

# Reuse the SIM-220 log-loss clip so the binary + multi-class layers agree.
from similarity.backtesting.backtester import DEFAULT_N_BINS, OUTCOME_PROB_EPS

if TYPE_CHECKING:
    from simulation.prop_distributions import PropDistribution

__all__ = [
    "binary_reliability_curve",
    "binary_ece",
    "binary_brier",
    "binary_log_loss",
    "fit_reliability_curve",
    "validate_prop_over_under",
    "pit_values",
    "pmf_coverage",
    "build_validation_report",
    "reliability_curve_for_calibration_report",
    "write_reliability_curve_to_calibration_report",
    "PropCalibration",
    "PropValidationReport",
]


# ---------------------------------------------------------------------------
# Input coercion
# ---------------------------------------------------------------------------


def _as_prob_outcome(
    pred_probs: Sequence[float] | NDArray[np.float64],
    outcomes: Sequence[int] | NDArray[np.int64],
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """Validate + coerce a (predicted positive-class prob, 0/1 outcome) pair.

    Probabilities are clipped to [0, 1] defensively; outcomes must be 0/1. Raises
    on a length mismatch (a silent zip-truncation would corrupt every metric).
    """
    p = np.clip(np.asarray(pred_probs, dtype=np.float64), 0.0, 1.0)
    y = np.asarray(outcomes, dtype=np.int64)
    if p.shape != y.shape:
        raise ValueError(f"pred_probs {p.shape} and outcomes {y.shape} must align")
    if y.size and not np.all((y == 0) | (y == 1)):
        raise ValueError("outcomes must be 0/1 (binary event labels)")
    return p, y


def _bin_edges(n_bins: int) -> NDArray[np.float64]:
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    return np.linspace(0.0, 1.0, n_bins + 1)


# ---------------------------------------------------------------------------
# Binary calibration metrics (win prob / over-under are binary events)
# ---------------------------------------------------------------------------


def binary_reliability_curve(
    pred_probs: Sequence[float] | NDArray[np.float64],
    outcomes: Sequence[int] | NDArray[np.int64],
    n_bins: int = DEFAULT_N_BINS,
) -> list[dict[str, float]]:
    """Binary reliability-diagram points: predicted prob vs observed positive rate.

    Bins the predicted probability of the positive class into ``n_bins`` equal-width
    bins; for each populated bin returns ``mean_pred`` (mean forecast),
    ``observed`` (empirical positive rate), ``count``, and ``gap =
    |mean_pred - observed|``. A well-calibrated forecaster lies on the diagonal
    (gap≈0 everywhere). Empty bins are omitted. Unlike the SIM-220 multi-class
    ``reliability_curve`` (which bins the argmax *confidence*), this bins the
    probability of the POSITIVE class — the correct quantity for a binary forecast.
    """
    p, y = _as_prob_outcome(pred_probs, outcomes)
    edges = _bin_edges(n_bins)
    points: list[dict[str, float]] = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        # Last bin closed on the right so p == 1.0 lands in it.
        mask = (p >= lo) & (p <= hi) if i == n_bins - 1 else (p >= lo) & (p < hi)
        count = int(mask.sum())
        if count == 0:
            continue
        mean_pred = float(p[mask].mean())
        observed = float(y[mask].mean())
        points.append(
            {
                "mean_pred": mean_pred,
                "observed": observed,
                "count": count,
                "gap": float(abs(mean_pred - observed)),
            }
        )
    return points


def binary_ece(
    pred_probs: Sequence[float] | NDArray[np.float64],
    outcomes: Sequence[int] | NDArray[np.int64],
    n_bins: int = DEFAULT_N_BINS,
) -> float:
    """Binary Expected Calibration Error — population-weighted mean bin gap.

    0.0 == perfectly calibrated (predicted prob == observed rate in every bin).
    ``nan`` for an empty input.
    """
    p, y = _as_prob_outcome(pred_probs, outcomes)
    n = len(p)
    if n == 0:
        return float("nan")
    curve = binary_reliability_curve(p, y, n_bins=n_bins)
    return float(sum((pt["count"] / n) * pt["gap"] for pt in curve))


def binary_brier(
    pred_probs: Sequence[float] | NDArray[np.float64],
    outcomes: Sequence[int] | NDArray[np.int64],
) -> float:
    """Binary Brier score — mean squared error of the forecast vs the 0/1 outcome.

    ``mean((p - y)^2)``; range [0, 1], lower is better. ``nan`` for empty input.
    """
    p, y = _as_prob_outcome(pred_probs, outcomes)
    if len(p) == 0:
        return float("nan")
    return float(np.mean((p - y) ** 2))


def binary_log_loss(
    pred_probs: Sequence[float] | NDArray[np.float64],
    outcomes: Sequence[int] | NDArray[np.int64],
    eps: float = OUTCOME_PROB_EPS,
) -> float:
    """Binary cross-entropy with eps clipping (a confident-wrong forecast yields a
    large-but-finite penalty, never ``inf``). Lower is better; ``nan`` if empty."""
    p, y = _as_prob_outcome(pred_probs, outcomes)
    if len(p) == 0:
        return float("nan")
    pc = np.clip(p, eps, 1.0 - eps)
    return float(-np.mean(y * np.log(pc) + (1 - y) * np.log(1.0 - pc)))


# ---------------------------------------------------------------------------
# The win-prob reliability FIT (closes the SIM-406 → 407 handoff)
# ---------------------------------------------------------------------------


def fit_reliability_curve(
    pred_probs: Sequence[float] | NDArray[np.float64],
    outcomes: Sequence[int] | NDArray[np.int64],
    n_bins: int = DEFAULT_N_BINS,
    *,
    min_bin_count: int = 1,
    anchor_endpoints: bool = True,
) -> list[list[float]]:
    """Fit the ``[[predicted_p, observed_p], ...]`` reliability curve that
    ``CalibrationReport.reliability_curve`` consumes (and
    ``CalibrationMap.from_report`` turns into a monotone p→p win-prob map).

    For each populated bin we emit ``[mean_pred, observed_rate]`` — i.e. "when the
    simulator said ~``mean_pred``, the event actually happened ``observed_rate`` of
    the time". ``CalibrationMap.from_report`` sorts these by ``mean_pred``, enforces
    monotonicity via a running max, and linearly interpolates, so a forecaster that
    is systematically over-confident gets pulled back toward the truth.

    Parameters
    ----------
    pred_probs, outcomes:
        Predicted positive-class probabilities and the realized 0/1 outcomes
        (e.g. per-game home win prob and home-won indicator).
    n_bins:
        Number of equal-width probability bins.
    min_bin_count:
        Bins with fewer than this many samples are dropped (a 1-game bin is noise);
        default 1 keeps every populated bin.
    anchor_endpoints:
        When True (default), prepend ``[0.0, 0.0]`` and append ``[1.0, 1.0]`` if the
        fitted points don't already span those ends, so the interpolated map covers
        the full [0, 1] domain instead of clamping to the first/last populated bin.
        The anchors are only added when they don't contradict the fitted data
        (i.e. the lowest observed ≥ 0 and highest ≤ 1, always true), and never
        duplicate an existing endpoint.

    Returns
    -------
    A list of ``[predicted_p, observed_p]`` pairs, sorted by predicted_p. Returns
    ``[]`` for an empty input or when no bin meets ``min_bin_count`` (the caller
    then leaves ``reliability_curve`` empty → the win-prob map stays identity, the
    documented "well-calibrated until a curve says otherwise" default).
    """
    p, y = _as_prob_outcome(pred_probs, outcomes)
    if len(p) == 0:
        return []
    curve = binary_reliability_curve(p, y, n_bins=n_bins)
    pts: list[list[float]] = [
        [pt["mean_pred"], pt["observed"]] for pt in curve if pt["count"] >= min_bin_count
    ]
    if not pts:
        return []
    pts.sort(key=lambda xy: xy[0])
    if anchor_endpoints:
        if pts[0][0] > 0.0:
            pts.insert(0, [0.0, 0.0])
        if pts[-1][0] < 1.0:
            pts.append([1.0, 1.0])
    return pts


# ---------------------------------------------------------------------------
# Prop-PMF over/under validation (the prop backtest the audit wanted)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PropCalibration:
    """Calibration metrics for ONE prop's over/under forecast vs real outcomes."""

    prop: str
    line: float
    n: int
    ece: float
    brier: float
    log_loss: float
    #: Mean predicted P(over) and the realized over rate — a one-glance bias check
    #: (mean_pred >> observed ⇒ the sim systematically over-forecasts the over).
    mean_pred_over: float
    observed_over_rate: float
    reliability_curve: list[dict[str, float]] = field(default_factory=list)


def validate_prop_over_under(
    pairs: Sequence[tuple[PropDistribution, int]],
    line: float,
    *,
    n_bins: int = DEFAULT_N_BINS,
) -> PropCalibration:
    """Score a prop's PMF over/under forecast against the realized values.

    ``pairs`` is a sequence of ``(PropDistribution, actual_value)`` — one per
    player-game (e.g. each starter's K PMF for a game + the K's they actually got).
    For each pair we take the PMF's betting ``p_over(line)`` and the realized over
    indicator (the sportsbook convention: a half-integer line has no push; an
    integer line treats a value ON the line as NOT over). We then score the
    forecast with the binary calibration metrics. Empty ``pairs`` → an ``n=0``
    result with ``nan`` metrics (the explicit "no signal" answer, not an error).
    """
    preds: list[float] = []
    outs: list[int] = []
    for dist, actual in pairs:
        preds.append(float(dist.p_over(line)))
        # Realized OVER under the sportsbook convention p_over encodes:
        #   half-integer line  → over == actual > line  (no push)
        #   integer line       → over == actual > line  (a value == line pushes,
        #                         so it is NOT a win for the over)
        outs.append(1 if float(actual) > float(line) else 0)
    p = np.asarray(preds, dtype=np.float64)
    y = np.asarray(outs, dtype=np.int64)
    prop_name = pairs[0][0].prop if pairs else ""
    if len(p) == 0:
        return PropCalibration(
            prop=prop_name,
            line=float(line),
            n=0,
            ece=float("nan"),
            brier=float("nan"),
            log_loss=float("nan"),
            mean_pred_over=float("nan"),
            observed_over_rate=float("nan"),
            reliability_curve=[],
        )
    return PropCalibration(
        prop=prop_name,
        line=float(line),
        n=int(len(p)),
        ece=binary_ece(p, y, n_bins=n_bins),
        brier=binary_brier(p, y),
        log_loss=binary_log_loss(p, y),
        mean_pred_over=float(p.mean()),
        observed_over_rate=float(y.mean()),
        reliability_curve=binary_reliability_curve(p, y, n_bins=n_bins),
    )


# ---------------------------------------------------------------------------
# PMF goodness-of-fit — the (mid-)PIT
# ---------------------------------------------------------------------------


def pit_values(pairs: Sequence[tuple[PropDistribution, int]]) -> NDArray[np.float64]:
    """Deterministic mid-P Probability Integral Transform of discrete PMFs.

    For a calibrated discrete forecaster the mid-PIT
    ``F(actual - 1) + 0.5 * P(X == actual)`` is ~Uniform(0, 1) across samples
    (mean ≈ 0.5). A mean well below 0.5 means actuals land in the LOW tail more
    than the PMF predicts (the sim over-forecasts the prop); above 0.5 is the
    reverse. The mid-P convention (rather than a random draw) keeps this
    deterministic so tests pin exact values.
    """
    out = np.empty(len(pairs), dtype=np.float64)
    for i, (dist, actual) in enumerate(pairs):
        a = int(actual)
        below = float(dist.p_less(a))  # P(X < actual)
        at = float(dist.prob(a))  # P(X == actual)
        out[i] = below + 0.5 * at
    return out


def pmf_coverage(
    pairs: Sequence[tuple[PropDistribution, int]],
    *,
    central: float = 0.80,
) -> dict[str, float]:
    """Empirical coverage of the PMF's central interval at a nominal level.

    Builds the mid-PIT (:func:`pit_values`) and reports the fraction landing inside
    the central ``central`` mass (i.e. PIT in ``[(1-central)/2, 1-(1-central)/2]``)
    plus the PIT mean. A well-calibrated PMF set has ``coverage ≈ central`` and
    ``pit_mean ≈ 0.5``. Empty input → ``nan`` coverage.
    """
    pit = pit_values(pairs)
    if pit.size == 0:
        return {
            "nominal": float(central),
            "coverage": float("nan"),
            "pit_mean": float("nan"),
            "n": 0,
        }
    tail = (1.0 - central) / 2.0
    lo, hi = tail, 1.0 - tail
    inside = float(np.mean((pit >= lo) & (pit <= hi)))
    return {
        "nominal": float(central),
        "coverage": inside,
        "pit_mean": float(pit.mean()),
        "n": int(pit.size),
    }


# ---------------------------------------------------------------------------
# The persistable validation report
# ---------------------------------------------------------------------------


@dataclass
class PropValidationReport:
    """Aggregated SIM-407 validation output: win-prob calibration + the fitted
    reliability curve + per-prop over/under calibration + PMF coverage.

    JSON round-trippable (``to_json`` / ``from_json``) like ``CalibrationReport``.
    The ``winprob_reliability_curve`` is the artifact the SIM-406 ``CalibrationReport``
    consumes — see :func:`reliability_curve_for_calibration_report`.
    """

    #: Win-probability calibration over the validated games.
    winprob_n: int = 0
    winprob_ece: float = 0.0
    winprob_brier: float = 0.0
    winprob_log_loss: float = 0.0
    #: The fitted [[predicted_p, observed_p], ...] curve for the win-prob map.
    winprob_reliability_curve: list[list[float]] = field(default_factory=list)
    #: Per-prop over/under calibration (one entry per (prop, line) validated).
    prop_calibrations: list[dict[str, Any]] = field(default_factory=list)
    #: PMF coverage summaries keyed by prop.
    pmf_coverage: dict[str, dict[str, float]] = field(default_factory=dict)
    # Metadata
    n_games: int = 0
    seasons_used: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PropValidationReport:
        valid = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in valid})

    @classmethod
    def from_json(cls, s: str) -> PropValidationReport:
        return cls.from_dict(json.loads(s))


def reliability_curve_for_calibration_report(
    report: PropValidationReport,
) -> list[list[float]]:
    """The win-prob reliability curve in the exact shape
    ``CalibrationReport.reliability_curve`` expects (a list of ``[pred, obs]``).

    A thin accessor so the SIM-407 fit can be written onto the SIM-406
    ``CalibrationReport`` without the caller reaching into the dataclass:

        cal_report.reliability_curve = reliability_curve_for_calibration_report(val)

    After that, ``CalibrationMap.from_report(cal_report)`` is a non-identity map and
    the win-prob layer applies the empirical correction.
    """
    return list(report.winprob_reliability_curve)


# ---------------------------------------------------------------------------
# Aggregator — build the full report from collected (prediction, actual) data
# ---------------------------------------------------------------------------


def build_validation_report(
    winprob_pairs: Sequence[tuple[float, int]],
    prop_pairs_by_line: dict[tuple[str, float], Sequence[tuple[PropDistribution, int]]],
    *,
    n_bins: int = DEFAULT_N_BINS,
    coverage_central: float = 0.80,
    n_games: int = 0,
    seasons_used: Sequence[int] | None = None,
) -> PropValidationReport:
    """Assemble a :class:`PropValidationReport` from collected predictions + actuals.

    This is the pure, deterministic core the orchestration script
    (``scripts/validate_props.py``) calls once it has run the sims and gathered the
    realized outcomes — kept here (not in the script) so it is unit-testable with
    synthetic inputs and never needs a DB.

    Parameters
    ----------
    winprob_pairs:
        ``(predicted_home_win_prob, actual_home_won 0/1)`` — one per completed game.
        Drives the win-prob calibration metrics AND the fitted reliability curve.
    prop_pairs_by_line:
        ``{(prop_name, line): [(PropDistribution, actual_value), ...]}`` — the
        per-(prop, line) over/under samples. Each entry yields one
        :class:`PropCalibration` row.
    n_bins:
        Bin count for every reliability/ECE computation.
    coverage_central:
        Nominal central level for the per-prop PMF coverage check.
    n_games, seasons_used:
        Recorded as report metadata.

    Returns
    -------
    A populated :class:`PropValidationReport` (win-prob metrics + fitted curve +
    per-(prop,line) calibrations + per-prop PMF coverage). All metrics degrade to
    ``nan`` / empty on empty inputs rather than raising.
    """
    wp = list(winprob_pairs)
    wp_pred = [float(p) for p, _ in wp]
    wp_out = [int(o) for _, o in wp]

    prop_cals: list[dict[str, Any]] = []
    coverage: dict[str, dict[str, float]] = {}
    for (prop_name, line), pairs in prop_pairs_by_line.items():
        cal = validate_prop_over_under(list(pairs), float(line), n_bins=n_bins)
        prop_cals.append(asdict(cal))
        # PMF coverage is line-independent (a property of the PMF vs actual), so
        # compute it once per prop from whichever (prop,line) entry we see first.
        if prop_name not in coverage:
            coverage[prop_name] = pmf_coverage(list(pairs), central=coverage_central)

    return PropValidationReport(
        winprob_n=len(wp),
        winprob_ece=binary_ece(wp_pred, wp_out, n_bins=n_bins),
        winprob_brier=binary_brier(wp_pred, wp_out),
        winprob_log_loss=binary_log_loss(wp_pred, wp_out),
        winprob_reliability_curve=fit_reliability_curve(wp_pred, wp_out, n_bins=n_bins),
        prop_calibrations=prop_cals,
        pmf_coverage=coverage,
        n_games=int(n_games),
        seasons_used=list(seasons_used or []),
    )


def write_reliability_curve_to_calibration_report(
    report: PropValidationReport,
    calibration_path: str,
) -> bool:
    """Write the fitted win-prob reliability curve INTO an existing SIM-406
    ``CalibrationReport`` JSON file, closing the SIM-406 → 407 loop on disk.

    Loads the ``CalibrationReport`` at ``calibration_path`` (the file the API boots
    from — see ``CALIBRATION_REPORT_PATH``), sets its ``reliability_curve`` to this
    validation's fitted curve, and rewrites it. On the next boot,
    ``CalibrationMap.from_report`` turns that curve into a monotone win-prob map, so
    the displayed/priced win probabilities carry the empirical correction.

    Returns ``True`` on success, ``False`` if the curve is empty (nothing to write —
    the win-prob map stays identity, the documented default) or the file can't be
    loaded. Never raises: a failure is reported as ``False`` so the validation run
    still completes and writes its own report.
    """
    import os

    curve = reliability_curve_for_calibration_report(report)
    if not curve:
        return False
    if not os.path.exists(calibration_path):
        return False
    try:
        from similarity.similarity_calibration import CalibrationReport

        with open(calibration_path, encoding="utf-8") as fh:
            calib = CalibrationReport.from_json(fh.read())
        calib.reliability_curve = curve
        with open(calibration_path, "w", encoding="utf-8") as fh:
            fh.write(calib.to_json())
        return True
    except Exception:
        return False
