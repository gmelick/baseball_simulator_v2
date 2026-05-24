"""
results.py
==========
SIM-327 -- the **multi-iteration aggregation contract** (Phase 4, Sprint 3).

WHAT THIS IS
------------
:func:`simulation.sim_loop.simulate_game` returns ONE per-game
:class:`GameSimResult` (SIM-320).  A Monte-Carlo run plays the *same* matchup N
times; the downstream consumers (the UI win-probability / score-distribution
panels, the betting / CLV edge engine, the perf batch runner) do not read N
individual games -- they read **one aggregate**.  This module defines that
aggregate, :class:`GameSimSummary`, and its pure-Python/numpy constructor
:meth:`GameSimSummary.from_results`.

DESIGN DECISIONS (the SIM-327 acceptance criteria, see the audit ticket row)
---------------------------------------------------------------------------
  * **One import home.**  Consumers import BOTH the per-game result and the
    aggregate from here -- :class:`GameSimResult` is re-exported from
    :mod:`simulation.sim_loop` so a downstream ticket never has to know the
    aggregate and the per-game type live in different files.  The loop file is
    NOT modified by this ticket; the aggregation is additive and decoupled.
  * **RAW per-iteration arrays, not a histogram.**  We keep the full
    per-iteration ``home_score`` / ``away_score`` / total arrays (length N, in
    input order).  Props / over-under / any percentile the betting layer wants
    can be computed from the raw signal; a pre-binned histogram would throw away
    exactly the resolution SIM-329 (prop PMFs) needs.  This is the SIM-112
    "preserve the raw distribution" requirement.
  * **Confidence intervals: normal-approximation (Wald), documented.**  We use
    the closed-form normal approximation rather than a bootstrap so the contract
    is deterministic, dependency-light (no resampling rng to seed) and O(1):
      - win%:   Wald interval  p +/- z * sqrt(p(1-p)/N)  (clamped to [0, 1]).
      - mean score: Wald interval  xbar +/- z * (s / sqrt(N))  using the sample
        standard deviation (ddof=1).
    Both narrow as O(1/sqrt(N)).  ``z`` defaults to 1.96 (95%); the confidence
    level is recorded on the summary so a consumer knows what it was given.  A
    bootstrap variant can be added later WITHOUT changing this contract.
  * **simulated_at** is a timezone-aware UTC timestamp stamped at construction.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from numpy.typing import NDArray

# Re-export the per-game result so consumers have ONE import home (the loop file
# is the source of truth for the per-game type; we never redefine it).
# SIM-328: also re-export the per-game boxscore types (PlayerStatLine / BoxScore)
# the loop attaches to ``GameSimResult.boxscore`` so a downstream prop-aggregation
# consumer (SIM-329) imports them from the same one aggregation home.
from simulation.sim_loop import BoxScore, GameSimResult, PlayerStatLine

#: Default two-sided z-multiplier for a 95% normal-approximation interval.
_Z_95 = 1.959963984540054
#: Map a handful of common confidence levels to their two-sided z-multiplier so
#: callers can pass a level (0.95) instead of a raw z.
_Z_BY_LEVEL: "dict[float, float]" = {
    0.80: 1.2815515594457,
    0.90: 1.6448536269514722,
    0.95: _Z_95,
    0.99: 2.5758293035489004,
}


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    """A two-sided confidence interval around a point estimate.

    ``low <= point <= high`` always holds (the point estimate is the interval
    centre for the symmetric normal approximation, modulo clamping at 0/1 for a
    proportion).  ``level`` is the nominal confidence (e.g. 0.95) and ``method``
    names how it was computed so a downstream consumer can report it honestly.
    """

    point: float
    low: float
    high: float
    level: float = 0.95
    method: str = "normal"

    @property
    def half_width(self) -> float:
        """Half the interval width -- the +/- margin around the point estimate."""
        return (self.high - self.low) / 2.0


@dataclass(slots=True)
class GameSimSummary:
    """Aggregate of N per-game :class:`GameSimResult`s for one matchup (SIM-327).

    This is the contract every multi-iteration consumer reads: the UI score /
    win-probability panels, the betting / CLV edge engine, and the perf batch
    runner.  Build it with :meth:`from_results`; do not populate it by hand.
    """

    #: Number of iterations aggregated (== len of every raw array below).
    n_iterations: int

    # --- win rates (sum to 1.0 including ties) ---------------------------------
    #: P(home win), P(away win), P(tie).  These three sum to 1.0 exactly (the tie
    #: bucket exists because the loop can return a tie at the max-innings guard).
    home_win_pct: float
    away_win_pct: float
    tie_pct: float

    # --- central tendency ------------------------------------------------------
    home_score_mean: float
    away_score_mean: float
    total_score_mean: float
    home_score_median: float
    away_score_median: float
    total_score_median: float

    # --- RAW per-iteration signal (length N, INPUT ORDER) ----------------------
    #: Full per-iteration arrays; NOT a histogram.  Order matches the input list
    #: so a consumer can correlate iteration i across arrays / with a seed list.
    home_scores: NDArray[np.int64]
    away_scores: NDArray[np.int64]
    total_scores: NDArray[np.int64]

    # --- confidence intervals (SIM-112) ----------------------------------------
    home_win_ci: ConfidenceInterval
    away_win_ci: ConfidenceInterval
    home_score_ci: ConfidenceInterval
    away_score_ci: ConfidenceInterval
    total_score_ci: ConfidenceInterval

    #: UTC, timezone-aware; stamped when the summary is constructed.
    simulated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    #: The nominal confidence level used for every CI on this summary.
    confidence_level: float = 0.95
    #: The CI method (matches each ``ConfidenceInterval.method``).
    ci_method: str = "normal"

    @classmethod
    def from_results(
        cls,
        results: "list[GameSimResult]",
        *,
        confidence_level: float = 0.95,
        simulated_at: "datetime | None" = None,
    ) -> "GameSimSummary":
        """Aggregate a list of per-game results into one :class:`GameSimSummary`.

        Pure Python + numpy; no DB, no FAISS, no rng.  ``results`` must be
        non-empty (you cannot summarise zero iterations).  ``confidence_level``
        selects the z-multiplier (0.80 / 0.90 / 0.95 / 0.99 are tabulated; any
        other level falls back to the 95% z with the level still recorded for
        honest reporting).  ``simulated_at`` can be injected for deterministic
        tests; it defaults to ``datetime.now(timezone.utc)``.
        """
        if not results:
            raise ValueError("GameSimSummary.from_results requires >= 1 result")

        n = len(results)
        home = np.fromiter((int(r.home_score) for r in results), dtype=np.int64, count=n)
        away = np.fromiter((int(r.away_score) for r in results), dtype=np.int64, count=n)
        total = home + away

        # Win buckets -- partition every iteration into exactly one of home /
        # away / tie so the three rates sum to 1.0 by construction.
        home_wins = int(np.count_nonzero(home > away))
        away_wins = int(np.count_nonzero(away > home))
        ties = n - home_wins - away_wins  # everything not a strict win is a tie.

        home_win_pct = home_wins / n
        away_win_pct = away_wins / n
        tie_pct = ties / n

        z = _Z_BY_LEVEL.get(round(float(confidence_level), 4), _Z_95)

        home_win_ci = _proportion_ci(home_win_pct, n, z, confidence_level)
        away_win_ci = _proportion_ci(away_win_pct, n, z, confidence_level)
        home_score_ci = _mean_ci(home, z, confidence_level)
        away_score_ci = _mean_ci(away, z, confidence_level)
        total_score_ci = _mean_ci(total, z, confidence_level)

        return cls(
            n_iterations=n,
            home_win_pct=home_win_pct,
            away_win_pct=away_win_pct,
            tie_pct=tie_pct,
            home_score_mean=float(home.mean()),
            away_score_mean=float(away.mean()),
            total_score_mean=float(total.mean()),
            home_score_median=float(statistics.median(home.tolist())),
            away_score_median=float(statistics.median(away.tolist())),
            total_score_median=float(statistics.median(total.tolist())),
            home_scores=home,
            away_scores=away,
            total_scores=total,
            home_win_ci=home_win_ci,
            away_win_ci=away_win_ci,
            home_score_ci=home_score_ci,
            away_score_ci=away_score_ci,
            total_score_ci=total_score_ci,
            simulated_at=simulated_at or datetime.now(timezone.utc),
            confidence_level=float(confidence_level),
            ci_method="normal",
        )


def _proportion_ci(
    p: float, n: int, z: float, level: float
) -> ConfidenceInterval:
    """Wald (normal-approximation) CI on a proportion, clamped to [0, 1].

    half-width = z * sqrt(p(1-p)/n).  Degenerate at p in {0, 1} (zero variance)
    -- the interval collapses to the point, which is the honest Wald answer; a
    consumer that needs a non-degenerate bound there can switch to Wilson later.
    """
    se = (p * (1.0 - p) / n) ** 0.5
    margin = z * se
    return ConfidenceInterval(
        point=p,
        low=max(0.0, p - margin),
        high=min(1.0, p + margin),
        level=float(level),
        method="normal",
    )


def _mean_ci(values: NDArray, z: float, level: float) -> ConfidenceInterval:
    """Wald CI on a sample mean: xbar +/- z * (s / sqrt(n)), s = std(ddof=1).

    With n == 1 the sample std is undefined; we report a zero-width interval at
    the single observation (no spread is estimable from one draw)."""
    n = int(values.size)
    mean = float(values.mean())
    if n < 2:
        return ConfidenceInterval(
            point=mean, low=mean, high=mean, level=float(level), method="normal"
        )
    std = float(values.std(ddof=1))
    margin = z * (std / (n ** 0.5))
    return ConfidenceInterval(
        point=mean,
        low=mean - margin,
        high=mean + margin,
        level=float(level),
        method="normal",
    )


__all__ = [
    "GameSimResult",      # re-exported from sim_loop -> one import home
    "PlayerStatLine",     # SIM-328 per-game boxscore line (re-exported)
    "BoxScore",           # SIM-328 per-game boxscore accumulator (re-exported)
    "GameSimSummary",
    "ConfidenceInterval",
]
