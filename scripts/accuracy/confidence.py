"""How far a row of a report can be trusted: the cluster bootstrap interval and
the stated minimum sample size. The accuracy table and the hypothetical-return
table both use these.

THE MINIMUM SAMPLE SIZE
-----------------------
A small sample can look like real skill by chance, so every row states the
smallest number of observations it needs before its result means anything:

1. The smallest edge worth caring about is the platform's own floor for acting
   on an edge, ``betting.bet_signal.DEFAULT_MIN_EDGE`` (0.02,
   :data:`MIN_DETECTABLE_EDGE`). A Brier or log-loss difference is not on a
   probability scale, so the floor is an approximation there: near a coin-flip
   market, a probability shift of ``e`` changes one observation's Brier score by
   about ``e``.
2. The floor shrinks by :data:`DETECTION_MARGIN` (1.6), the constant of
   ``tests/acceptance/bands.py``, so a real edge of the full size clears the
   minimum with room to spare.
3. The significance level is 0.05 two-sided, Bonferroni-corrected by the number
   of market rows in the run (:func:`_bonferroni_alpha`). ``--markets`` can
   narrow that count. The "overall" row uses the uncorrected level: it is one
   top-line figure, not one of many rows a reader scans.
4. The minimum is the observation count at which the row's own measured spread
   clears the shrunk floor at that level (:func:`_minimum_paired_observations`).
   The spread is clustered by game (:func:`_clustered_se`,
   :func:`_minimum_observations_clustered`): the observations of one game share
   its park, bullpen and score, so a market whose observations sit in a few games
   needs more observations. A row backed by fewer than
   :data:`MIN_CLUSTERS_FOR_INFERENCE` games reads as undetermined.

WHAT THE MINIMUM DOES NOT CLAIM
-------------------------------
* That 0.02 is the right edge for a Brier score. It is a stand-in until real
  backtests give a measured magnitude, the way the box-score floors of
  ``tests/acceptance/bands.py`` are measured.
* That the coin-flip approximation holds for every market. Away from p = 0.5 a
  shift of ``e`` changes the Brier score by about ``4 p (1 - p) e`` (about half
  of ``e`` at p = 0.15). The same floor applies to every market, so it buys less
  power on a market far from a coin flip, such as a home-run prop.
* That clearing :data:`MIN_CLUSTERS_FOR_INFERENCE` certifies a row. A small
  sample whose observed spread is zero by chance reads as fully resolved: the
  formula trusts the observed spread, not an upper bound on it.
* That the correction covers repeated runs. It corrects for the rows of one run;
  checking the "overall" row over many runs is repeated testing it does not
  address.
* That the minimum counts games. ``brier_min_n``, ``log_loss_min_n`` and
  ``min_n_recommended`` count paired observations; a row's ``n_games`` is its
  game count. Read the two together.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.stats import norm

from betting.bet_signal import DEFAULT_MIN_EDGE

#: The resamples of the bootstrap interval. 2000 is the usual floor for a
#: percentile bootstrap; the command line can raise it for a tighter interval.
DEFAULT_BOOTSTRAP_SAMPLES = 2000

#: The smallest edge worth caring about: the platform's own floor for acting on
#: an edge. It is an approximation for a Brier or log-loss difference (see the
#: module docstring).
MIN_DETECTABLE_EDGE: float = DEFAULT_MIN_EDGE

#: The detection margin of ``tests/acceptance/bands.py`` ("HOW EACH FLOOR IS
#: CHOSEN"). Shrinking the floor by this factor gives a real edge of exactly
#: MIN_DETECTABLE_EDGE a better chance of clearing the minimum.
#:
#: Only the constant is shared, not its power. The 99.2% power of bands.py holds
#: at its fixed Z = 4.0 ("WHY Z = 4.0"). Here z varies per row
#: (``_z_two_sided(alpha)``, alpha uncorrected or Bonferroni-corrected), so the
#: power is about 88% on an uncorrected row (alpha 0.05) and rises toward 99.2% as
#: the correction grows. Do not quote 99.2% for this procedure.
DETECTION_MARGIN: float = 1.6

#: The two-sided significance level before the Bonferroni correction.
DEFAULT_ALPHA: float = 0.05

#: The fewest distinct games a bucket needs before its clustered standard error,
#: interval and minimum sample size are trusted. Below it the read is
#: undetermined.
#:
#: Two clusters is the least the CR0 sandwich formula computes a nonzero spread
#: from (one cluster always gives exactly 0.0). The cluster-robust literature
#: (Cameron and Miller's practitioner's guide) finds that CR0 understates the
#: uncertainty with few clusters and wants dozens before trusting it: a zero spread
#: estimated from 2 or 3 games is weak evidence that the true spread is zero. 10 is
#: a round floor at the low end of that guidance, chosen so a read stays usable at
#: the slate sizes this platform runs (a day is a few dozen games). It is not a
#: claim that 10 games make the estimate reliable; look for dozens before treating
#: a row as certified.
MIN_CLUSTERS_FOR_INFERENCE: int = 10


def _json_safe(d: dict[str, Any]) -> dict[str, Any]:
    """A flat dict with every non-finite float (``nan``, ``inf``, ``-inf``)
    replaced by ``None`` (JSON ``null``).

    ``nan`` and ``inf`` are legal Python floats but not legal JSON (RFC 8259).
    ``json.dumps`` writes the bare tokens ``NaN`` and ``Infinity`` without
    raising, and a strict parser (a browser's ``JSON.parse``) then rejects the
    report. The low-sample rows carry such values (a degenerate clustered spread,
    an unresolved minimum), so every row's ``to_jsonable`` passes through here.
    """
    return {
        k: (None if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))) else v)
        for k, v in d.items()
    }


def _bonferroni_alpha(base_alpha: float, n_hypotheses: int) -> float:
    """The Bonferroni-corrected two-sided significance level.

    Scanning ``n_hypotheses`` rows for the one that looks good raises the chance
    that some row reads significant by luck. Dividing the level by the row count
    is the standard conservative fix; ``tests/acceptance/bands.py`` corrects its
    own channels the same way ("WHY Z = 4.0"). ``n_hypotheses < 1`` counts as 1
    (no correction).
    """
    n = max(1, int(n_hypotheses))
    return float(base_alpha) / n


def _z_two_sided(alpha: float) -> float:
    """The two-sided critical z-value of significance level ``alpha``."""
    return float(norm.ppf(1.0 - float(alpha) / 2.0))


def _minimum_paired_observations(sd: float, *, floor: float, alpha: float) -> float:
    """The fewest paired observations that detect a mean difference of at least
    ``floor`` at two-sided level ``alpha``, given the sample's own standard
    deviation ``sd``.

    ``n = (z * sd / floor) ** 2``, the shape of the "required observations" of
    ``tests/acceptance/bands.py``. The formula knows no margin: a caller that
    wants one passes a shrunk ``floor`` (``target_edge / DETECTION_MARGIN``).

    Returns ``inf`` when ``floor`` is not positive (no sample resolves an edge of
    zero or less) and ``0.0`` when ``sd`` is zero (a perfectly resolved
    population needs no more observations).
    """
    if float(floor) <= 0.0:
        return float("inf")
    if sd <= 0.0:
        return 0.0
    z = _z_two_sided(alpha)
    return float((z * float(sd) / float(floor)) ** 2)


def _clustered_se(values_by_game: dict[int, list[float]]) -> float:
    """The cluster-robust (by game) standard error of the mean of per-observation
    values, such as Brier or log-loss differences.

    The observations of one game share its simulated scores and player
    distributions, so they are correlated, and the plain i.i.d. error understates
    the uncertainty of a pooled mean. This is the CR0 sandwich error::

        SE = sqrt( sum_g ( sum_{i in g} (x_i - xbar) )^2 )  /  N

    With one observation per game it equals the i.i.d. error; with many
    observations in one game it widens. Returns 0.0 for an empty input.

    With fewer than 2 distinct games it is always exactly 0.0: one game's
    deviations from the overall mean sum to zero, which reads as "no uncertainty"
    for the least resolved case there is. This plain formula does not guard that
    case; :func:`_minimum_observations_clustered` is the guarded version.
    """
    all_x = [float(x) for xs in values_by_game.values() for x in xs]
    n = len(all_x)
    if n == 0:
        return 0.0
    xbar = sum(all_x) / n
    total = 0.0
    for xs in values_by_game.values():
        g = sum((float(x) - xbar) for x in xs)
        total += g * g
    return float(total**0.5 / n)


def _minimum_observations_clustered(
    values_by_game: dict[int, list[float]], *, floor: float, alpha: float
) -> float | None:
    """The fewest total observations that trust a mean differing from its
    reference by ``floor`` at level ``alpha``, given the market's own clustering by
    game.

    It backs out the implied per-observation standard deviation
    (``_clustered_se(values_by_game) * sqrt(n)``) and feeds it to
    :func:`_minimum_paired_observations`. A market whose observations sit in a few
    games (several props of one start, several game markets off one score) has a
    larger implied spread, so it needs more observations than a naive formula asks.

    Returns ``None`` with fewer than 2 observations, or fewer than
    :data:`MIN_CLUSTERS_FOR_INFERENCE` distinct games: too little data to estimate
    the spread.
    """
    n = sum(len(xs) for xs in values_by_game.values())
    if n < 2 or len(values_by_game) < MIN_CLUSTERS_FOR_INFERENCE:
        return None
    implied_sd = _clustered_se(values_by_game) * float(n) ** 0.5
    return _minimum_paired_observations(implied_sd, floor=floor, alpha=alpha)


def _bootstrap_paired_diff_ci(
    diffs: np.ndarray, game_pks: np.ndarray, *, n_bootstrap: int, seed: int
) -> tuple[float, float]:
    """The 95% percentile cluster-bootstrap interval of the mean of ``diffs``.

    ``diffs`` are per-observation paired differences: the simulator's score minus
    the market's, so a negative value means the simulator was more accurate on
    that observation. The function works on any per-observation array.

    The bootstrap resamples GAMES, not observations. One game can give several
    observations (up to three full-game markets off one final score; several
    players of one prop bucket), and they share the game's park, bullpen and
    score. Resampling them one at a time treats a busy game as several
    independent games and gives an interval narrower than the data supports. So
    each of the ``n_bootstrap`` draws picks games with replacement and pools every
    observation of the picked games, through each game's (sum, count) computed
    once. With one observation per game this is the ordinary bootstrap.

    The draw is seeded, so a rerun reproduces the interval. Fewer than 2
    observations, or fewer than 2 distinct games, give the degenerate
    ``(mean, mean)``: nothing to resample.
    """
    n = diffs.size
    if n < 2:
        m = float(diffs.mean()) if n else float("nan")
        return m, m
    unique_games, inverse = np.unique(game_pks, return_inverse=True)
    g = unique_games.size
    if g < 2:
        m = float(diffs.mean())
        return m, m
    # One (sum, count) per distinct game, computed once: a draw's mean over its
    # resampled games is sum(picked sums) / sum(picked counts).
    sums = np.zeros(g, dtype=np.float64)
    counts = np.zeros(g, dtype=np.float64)
    np.add.at(sums, inverse, diffs)
    np.add.at(counts, inverse, 1.0)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, g, size=(int(n_bootstrap), g))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)
