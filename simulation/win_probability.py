"""
win_probability.py
==================
The **calibrated game win probability**.

WHAT THIS IS
------------
A Monte-Carlo run plays one matchup N times.  :class:`simulation.results.GameSimSummary`
exposes the RAW win frequencies (``home_win_pct`` / ``away_win_pct`` /
``tie_pct``) and a confidence interval (CI) on each.  A raw frequency is the
*base estimate* of the true win probability, but it is a poor probability for
small N: 0/N collapses to a hard 0.0 and N/N to a hard 1.0.  Both are
over-confident, and both break any consumer that takes ``log(p)`` (the
log-loss, the odds, the betting layer).  This module turns a
:class:`GameSimSummary` (or a list of :class:`GameSimResult`) into a *calibrated*
home/away win probability through a deterministic transform with no random draw.

THE CALIBRATION METHOD
----------------------
  1. **Tie handling (first, so the base rates are a two-class signal).**
     A simulated tie happens only at the loop's max-innings guard; the betting
     layer cannot price it as a "no winner" outcome.  The caller picks one of
     two policies:
       * ``TieHandling.SPLIT`` (default): a tie counts as half a win for each
         side (the standard "push split" convention).  The smoothing below then
         works on the effective home-win count ``home_wins + 0.5 * ties`` out
         of N.
       * ``TieHandling.DROP``: ties leave the denominator; the probability is
         conditioned on a decisive game (``home_wins`` out of
         ``home_wins + away_wins``).  ``n_decisive`` records the reduced N.
     Either way the result keeps ``tie_pct``, so a consumer can see how many
     ties were folded in.

  2. **Beta / Laplace smoothing (so 0/N is never a hard 0.0).**
     The smoothed point estimate is the posterior mean of a Beta(alpha, alpha)
     prior updated with the effective home-win count ``k`` out of ``n``:

         p_home = (k + alpha) / (n + 2 * alpha)

     ``alpha`` is the pseudo-count added to *each* side (the prior strength).
     ``alpha = 0.5`` is the Jeffreys prior (the default, the standard
     uninformative choice for a binomial proportion); ``alpha = 1.0`` is
     Laplace / add-one.  The prior pulls the estimate toward 0.5 by an amount
     that vanishes as N grows, so a large-N estimate is close to the raw
     frequency.  A 0/N run stays at a small, nonzero ``alpha / (n + 2a)``
     rather than a hard 0.0.  This is the same kind of shrinkage toward a prior
     as the Empirical-Bayes ``N_PRIOR`` shrinkage of the similarity engines
     (``similarity/similarity_calibration.py``).

  3. **Calibration-map hook (identity by default).**
     The smoothed probability passes through a :class:`CalibrationMap`, a
     monotone p -> p map.  The default is the identity: the simulator is
     treated as well-calibrated until a reliability curve says otherwise.  The
     app builds the map from the reliability curve in its calibration report
     (:meth:`CalibrationMap.from_report`), and
     :func:`simulation.prop_validation.fit_reliability_curve` fits that curve.
     This module only applies the fitted map; it does not fit one.

  4. **Confidence interval (the results module's shape).**
     The result carries a :class:`~simulation.results.ConfidenceInterval` on the
     home win probability: the Wald (normal-approximation) interval on the
     reported probability (the smoothed proportion after the calibration map),
     at the *effective* N (``n_eff``):
         half-width = z * sqrt(p(1-p) / n_eff),  clamped to [0, 1].
     With the identity map the probability is never exactly 0 or 1, so the
     interval is non-degenerate even for a lopsided run.  It narrows as
     O(1/sqrt(N_eff)).

DETERMINISM
-----------
Pure arithmetic on the summary's counts; no RNG, no DB, no FAISS.  The same input
summary (and the same ``alpha`` / tie policy / calibration map / confidence
level) always yields the same :class:`WinProbability`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass

from simulation.results import (
    ConfidenceInterval,
    GameSimResult,
    GameSimSummary,
)

#: Two-sided z-multipliers for the common confidence levels (mirrors the table in
#: ``simulation/results.py``, so a win-prob CI and a run-summary CI agree on z).
_Z_95 = 1.959963984540054
_Z_BY_LEVEL: dict[float, float] = {
    0.80: 1.2815515594457,
    0.90: 1.6448536269514722,
    0.95: _Z_95,
    0.99: 2.5758293035489004,
}

#: Jeffreys prior pseudo-count (per side).  The default smoothing strength.
JEFFREYS_ALPHA = 0.5
#: Laplace / add-one prior pseudo-count (per side).
LAPLACE_ALPHA = 1.0


class TieHandling(enum.Enum):
    """How a simulated tie (the loop's max-innings guard) is folded into the
    two-class home/away win probability.

    ``SPLIT`` -- a tie is half a win for each side (the betting "push split"
    convention); ties stay in the denominator.
    ``DROP``  -- ties are removed from the denominator; the probability is
    conditioned on a decisive game.
    """

    SPLIT = "split"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class CalibrationMap:
    """A monotone p -> p calibration map (the seam for a fitted reliability curve).

    The default instance is the identity map: the raw smoothed simulator
    probability is taken at face value.  A caller with a fitted curve builds
    ``CalibrationMap(fn=fitted_curve, name=...)``, or :meth:`from_report` builds
    one from a ``CalibrationReport``, and passes it to :func:`win_probability`.

    ``fn`` must map [0, 1] -> [0, 1] and SHOULD be monotone non-decreasing so the
    relative ordering of probabilities is preserved; the result is clamped to
    [0, 1] defensively regardless.
    """

    fn: Callable[[float], float] | None = None
    name: str = "identity"

    def apply(self, p: float) -> float:
        """Apply the map to ``p`` and clamp the result to [0, 1]."""
        q = p if self.fn is None else float(self.fn(p))
        return min(1.0, max(0.0, q))

    @classmethod
    def from_report(cls, report: object) -> CalibrationMap:
        """Build a :class:`CalibrationMap` from a ``CalibrationReport``.

        The report carries a fitted reliability curve as
        ``report.reliability_curve``: a list of ``(predicted_p, observed_p)``
        anchor points, each in [0, 1]
        (:func:`simulation.prop_validation.write_reliability_curve_to_calibration_report`
        writes it). This method turns those points into a monotone
        **piecewise-linear** p -> p map:

          * The anchors are sorted by ``predicted_p`` and a running max is taken
            over the ``observed_p`` values so the curve is monotone
            non-decreasing (the relative ordering of probabilities is preserved
            even if the raw fit dips slightly -- the same guarantee
            :meth:`apply` documents).
          * ``fn`` linearly interpolates between anchors; queries below the first
            / above the last anchor clamp to that anchor's observed value.
            ``apply`` then clamps the result to [0, 1] defensively.

        When the report has **no** fitted curve (``reliability_curve`` is missing,
        ``None``, empty, or has fewer than two usable points), the simulator is
        treated as well-calibrated and the module's :data:`IDENTITY_CALIBRATION`
        singleton is returned unchanged -- so an absent curve costs nothing.
        """
        curve = getattr(report, "reliability_curve", None)
        if not curve:
            return IDENTITY_CALIBRATION

        # Parse + sanitize the anchor points: keep finite (x, y) pairs only.
        pts: list[tuple[float, float]] = []
        for item in curve:
            try:
                x, y = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if x != x or y != y:  # NaN guard
                continue
            pts.append((x, y))

        if len(pts) < 2:
            # Not enough to interpolate -> identity (well-calibrated default).
            return IDENTITY_CALIBRATION

        pts.sort(key=lambda t: t[0])
        xs = [p[0] for p in pts]
        # Enforce monotone non-decreasing observed values via a running max.
        ys: list[float] = []
        running = pts[0][1]
        for _, y in pts:
            running = max(running, y)
            ys.append(running)

        def _interp(p: float) -> float:
            # Clamp outside the anchor range to the end anchors.
            if p <= xs[0]:
                return ys[0]
            if p >= xs[-1]:
                return ys[-1]
            # Find the bracketing segment (small curves -> linear scan is fine).
            for i in range(1, len(xs)):
                if p <= xs[i]:
                    x0, x1 = xs[i - 1], xs[i]
                    y0, y1 = ys[i - 1], ys[i]
                    span = x1 - x0
                    if span <= 0:  # coincident xs -> step to upper anchor
                        return y1
                    t = (p - x0) / span
                    return y0 + t * (y1 - y0)
            return ys[-1]  # pragma: no cover - covered by the >= xs[-1] guard

        name = "reliability-curve"
        seasons = getattr(report, "seasons_used", None)
        if seasons:
            name = f"reliability-curve({','.join(str(s) for s in seasons)})"
        return cls(fn=_interp, name=name)


#: The default (identity) calibration map -- module-level singleton so callers can
#: reference "the identity map" by name and so the default is allocation-free.
IDENTITY_CALIBRATION = CalibrationMap()


@dataclass(frozen=True, slots=True)
class WinProbability:
    """A calibrated home/away win probability for one simulated matchup.

    ``home_win_prob`` and ``away_win_prob`` sum to 1.0 exactly (the chosen
    :class:`TieHandling` has already folded the ties in).  The result is
    self-describing: it records the N it was based on, the effective N after tie
    handling, the smoothing/tie/calibration choices, and a CI on the home win
    probability (a :class:`ConfidenceInterval`).
    """

    home_win_prob: float
    away_win_prob: float

    #: Raw iteration count the summary aggregated (== summary.n_iterations).
    n_iterations: int
    #: Effective denominator after tie handling (== n for SPLIT, == decisive
    #: games for DROP).  The CI is computed against this.
    n_decisive: int

    #: CI on the HOME win probability (Wald, on the reported ``home_win_prob``).
    home_win_ci: ConfidenceInterval

    #: The fraction of iterations that were ties (preserved for transparency).
    tie_pct: float

    # --- the calibration choices, recorded for honest reporting ----------------
    alpha: float
    tie_handling: TieHandling
    calibration_map: str
    confidence_level: float
    method: str = "beta-smoothed+wald"


def win_probability(
    summary: GameSimSummary | list[GameSimResult],
    *,
    alpha: float = JEFFREYS_ALPHA,
    tie_handling: TieHandling = TieHandling.SPLIT,
    calibration_map: CalibrationMap = IDENTITY_CALIBRATION,
    confidence_level: float = 0.95,
) -> WinProbability:
    """Turn a sim run into a calibrated home/away win probability.

    Parameters
    ----------
    summary
        A :class:`~simulation.results.GameSimSummary` (preferred) OR a non-empty
        list of per-game :class:`~simulation.results.GameSimResult`s, which is
        aggregated via :meth:`GameSimSummary.from_results` first.
    alpha
        Beta-prior pseudo-count added to EACH side (the smoothing strength).
        ``0.5`` = Jeffreys (default), ``1.0`` = Laplace/add-one, ``0.0`` = no
        smoothing (raw frequency -- can return a hard 0/1, not recommended).
        Must be >= 0.
    tie_handling
        :class:`TieHandling.SPLIT` (default, tie = half a win each) or
        :class:`TieHandling.DROP` (condition on a decisive game).
    calibration_map
        A :class:`CalibrationMap`; identity by default.  The seam for a fitted
        reliability curve (see :meth:`CalibrationMap.from_report`).
    confidence_level
        Nominal level for the CI on the home win probability (0.80 / 0.90 / 0.95
        / 0.99 tabulated; any other level falls back to the 95% z, with the level
        still recorded honestly).

    Returns
    -------
    A :class:`WinProbability` whose ``home_win_prob`` + ``away_win_prob`` == 1.0.

    Deterministic: pure arithmetic on the summary's counts; no RNG.
    """
    if alpha < 0:
        raise ValueError("alpha (Beta-prior pseudo-count) must be >= 0")

    # Accept a raw list of results too: aggregate it to a summary first, so the
    # math below has exactly one path.
    if not isinstance(summary, GameSimSummary):
        summary = GameSimSummary.from_results(list(summary), confidence_level=confidence_level)

    n = int(summary.n_iterations)
    if n <= 0:
        raise ValueError("win_probability requires a summary over >= 1 iteration")

    # Recover the win counts from the summary's rates. The rates sum to 1.0, so
    # each product equals its count up to float rounding; the counts stay floats.
    home_wins = summary.home_win_pct * n
    away_wins = summary.away_win_pct * n
    ties = summary.tie_pct * n

    # --- Step 1: tie handling -> an effective (k home-wins out of n) two-class
    #     signal.
    if tie_handling is TieHandling.SPLIT:
        # A tie is half a win for each side; ties stay in the denominator.
        k = home_wins + 0.5 * ties
        n_eff = float(n)
        n_decisive = n
    elif tie_handling is TieHandling.DROP:
        # Condition on a decisive game; ties leave the denominator.
        k = home_wins
        n_eff = home_wins + away_wins
        n_decisive = int(round(n_eff))
        if n_eff <= 0:
            # Every iteration was a tie -- no decisive signal at all.  Fall back
            # to the smoothing prior's centre (0.5) with the prior as the only
            # "evidence" so the result is still well-defined and non-degenerate.
            k = 0.0
            n_eff = 0.0
    else:  # pragma: no cover - exhaustive enum
        raise ValueError(f"unknown tie_handling: {tie_handling!r}")

    # --- Step 2: Beta/Laplace smoothing -- posterior mean of Beta(alpha, alpha).
    #     p = (k + alpha) / (n_eff + 2*alpha).  With n_eff == 0 (all ties, DROP)
    #     and alpha == 0 this would be 0/0; guard to the prior centre 0.5.
    denom = n_eff + 2.0 * alpha
    p_home_smoothed = 0.5 if denom <= 0 else (k + alpha) / denom

    # --- Step 3: calibration map (identity by default) -------------------------
    p_home = calibration_map.apply(p_home_smoothed)
    p_away = 1.0 - p_home

    # --- Step 4: Wald CI on the reported probability at the effective N --------
    z = _Z_BY_LEVEL.get(round(float(confidence_level), 4), _Z_95)
    home_win_ci = _smoothed_proportion_ci(
        p_home, n_eff if n_eff > 0 else float(n), z, confidence_level
    )

    return WinProbability(
        home_win_prob=p_home,
        away_win_prob=p_away,
        n_iterations=n,
        n_decisive=n_decisive,
        home_win_ci=home_win_ci,
        tie_pct=float(summary.tie_pct),
        alpha=float(alpha),
        tie_handling=tie_handling,
        calibration_map=calibration_map.name,
        confidence_level=float(confidence_level),
        method="beta-smoothed+wald",
    )


def _smoothed_proportion_ci(p: float, n_eff: float, z: float, level: float) -> ConfidenceInterval:
    """Wald (normal-approximation) CI on the home-win proportion ``p``.

    half-width = z * sqrt(p(1-p) / n_eff), clamped to [0, 1].  The Beta prior
    keeps the smoothed ``p`` off 0 and 1, so with the identity map this interval
    is non-degenerate even for a lopsided run.  It narrows as O(1/sqrt(n_eff)).
    It mirrors ``simulation.results._proportion_ci``, so the win-prob CI and the
    run summary's CIs share one method ("normal").
    """
    n_eff = max(n_eff, 1.0)
    se = (p * (1.0 - p) / n_eff) ** 0.5
    margin = z * se
    return ConfidenceInterval(
        point=p,
        low=max(0.0, p - margin),
        high=min(1.0, p + margin),
        level=float(level),
        method="normal",
    )


__all__ = [
    "WinProbability",
    "win_probability",
    "CalibrationMap",
    "IDENTITY_CALIBRATION",
    "TieHandling",
    "JEFFREYS_ALPHA",
    "LAPLACE_ALPHA",
]
