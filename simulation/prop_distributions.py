"""
prop_distributions.py
=====================
SIM-329 -- the **prop-distribution aggregator** over N Monte-Carlo iterations
(Phase 4, Sprint 4).

WHAT THIS IS
------------
SIM-328 made every simulated game produce a per-game :class:`~simulation.results.BoxScore`
(per-player AB/H/HR/RBI on offense, outs/K/BB/ER on defense, keyed by
``player_id``).  SIM-327 keeps the RAW per-iteration substrate rather than a
pre-binned histogram precisely so the betting layer can build full distributions.
THIS module is that layer's head: over the N per-game boxscores of a Monte-Carlo
run it builds, for **each player and each prop**, a full integer-support
**probability mass function** (PMF) -- value -> probability, summing to 1 -- not
just a mean.  The CLV / edge engine (SIM-339) consumes these PMFs to price an
over/under against a book line; a mean alone cannot answer ``P(K >= 6.5)``.

THE PROPS
---------
Derived directly from the SIM-328 :class:`PlayerStatLine` fields:

  * **Pitcher** (charged on defense):
      - ``K``    -- strikeouts (``line.k``)
      - ``BB``   -- walks (``line.bb``)
      - ``ER``   -- earned runs (``line.er``)
      - ``OUTS`` -- outs recorded == IP in thirds (``line.outs_recorded``).  We
        expose the prop in *outs* (an exact integer) rather than the ``x.1/x.2``
        IP string, because a PMF needs an integer support and ``2.1`` IP is not a
        real number you can sum -- ``7`` outs is.  A consumer that wants "P(IP >=
        6.0)" asks ``p_at_least(18)`` (6 innings == 18 outs).

  * **Batter** (credited on offense):
      - ``H``    -- hits (``line.h``)
      - ``HR``   -- home runs (``line.hr``)
      - ``RBI``  -- runs batted in (``line.rbi``)
      - ``TB``   -- total bases (see the limitation below).

TOTAL BASES (TB) -- NOW EXACT (SIM-365)
---------------------------------------
True total bases is ``1B + 2*2B + 3*3B + 4*HR``.  SIM-365 extended the boxscore
:class:`PlayerStatLine` to track doubles (``b2``) and triples (``b3``) as
subsets of ``h``, alongside ``hr``.  Singles are therefore the remainder
(``h - b2 - b3 - hr``), so :func:`_total_bases` now computes TB EXACTLY::

    TB = (h - b2 - b3 - hr)*1 + b2*2 + b3*3 + hr*4 = h + b2 + 2*b3 + 3*hr

This is no longer a lower bound: :data:`TB_IS_LOWER_BOUND` is now ``False``.
:func:`_total_bases` remains the single place that maps hit-types to bases; the
PMF/API above it are unchanged.  (Prior to SIM-365 the boxscore tracked only
``(h, hr)`` and TB was a documented lower bound, treating every non-HR hit as a
single: ``h + 3*hr``.)

THE OVER/UNDER API (the core betting query)
-------------------------------------------
Each :class:`PropDistribution` exposes the PMF plus the over/under primitives a
book line needs, for ANY (possibly half-integer) line:

  * ``p_at_least(line)`` -- ``P(X >= line)``  (over, inclusive lower)
  * ``p_at_most(line)``  -- ``P(X <= line)``  (under, inclusive upper)
  * ``p_greater(line)``  -- ``P(X > line)``   (strict over)
  * ``p_less(line)``     -- ``P(X < line)``   (strict under)
  * ``p_over(line)`` / ``p_under(line)`` -- the BETTING convention: a half-integer
    line (e.g. 5.5) has no push, so over == ``P(X >= 6)`` and under ==
    ``P(X <= 5)`` and they sum to 1.  An INTEGER line (e.g. 6) can push, so over
    is the strict ``P(X > 6)``, under is the strict ``P(X < 6)``, and the
    leftover ``p_push(6) == P(X == 6)`` is reported separately (over + under +
    push == 1).  This mirrors the standard sportsbook treatment of whole-number
    totals.

DETERMINISM
-----------
Pure numpy / Python over the raw per-iteration substrate.  No DB, no FAISS, no
RNG.  The same list of boxscores always yields the same PMFs.  Aggregation is by
``player_id``; a player absent from every boxscore yields ``None`` (the explicit
"no signal" answer) rather than an exception.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable, Mapping

import numpy as np
from numpy.typing import NDArray

from simulation.results import (
    BoxScore,
    ConfidenceInterval,
    GameSimResult,
    PlayerStatLine,
)

#: Two-sided z-multipliers for the common confidence levels (mirrors the table in
#: ``simulation/results.py`` so a prop CI and a SIM-327 CI agree on z).
_Z_95 = 1.959963984540054
_Z_BY_LEVEL: "dict[float, float]" = {
    0.80: 1.2815515594457,
    0.90: 1.6448536269514722,
    0.95: _Z_95,
    0.99: 2.5758293035489004,
}

#: Pitcher prop names (charged on defense).
PITCHER_PROPS: "tuple[str, ...]" = ("K", "BB", "ER", "OUTS")
#: Batter prop names (credited on offense).
BATTER_PROPS: "tuple[str, ...]" = ("H", "HR", "RBI", "TB")
#: Every prop name this aggregator can build.
ALL_PROPS: "tuple[str, ...]" = PITCHER_PROPS + BATTER_PROPS

#: TB is now EXACT (SIM-365): the boxscore tracks ``b2``/``b3`` alongside
#: ``h``/``hr``, so :func:`_total_bases` computes true total bases.
TB_IS_LOWER_BOUND = False


def _total_bases(line: PlayerStatLine) -> int:
    """True total bases from the (h, b2, b3, hr) fields (EXACT, SIM-365).

    True TB = 1B + 2*2B + 3*3B + 4*HR.  With doubles (``b2``) and triples
    (``b3``) now tracked as subsets of ``h`` (SIM-365), singles are the
    remainder (``h - b2 - b3 - hr``), so::

        TB = (h - b2 - b3 - hr)*1 + b2*2 + b3*3 + hr*4
           = h + b2 + 2*b3 + 3*hr

    This is the single place that reads the bases-per-hit; nothing above this
    function changes.
    """
    h = int(line.h)
    b2 = int(line.b2)
    b3 = int(line.b3)
    hr = int(line.hr)
    return h + b2 + 2 * b3 + 3 * hr


#: How to read each prop's integer value off a single-game :class:`PlayerStatLine`.
#: Keeping the extractors in one table makes :meth:`PropDistributionSet.from_boxscores`
#: a single uniform loop and keeps "which field is which prop" in exactly one place.
_PROP_EXTRACTORS = {
    "K": lambda ln: int(ln.k),
    "BB": lambda ln: int(ln.bb),
    "ER": lambda ln: int(ln.er),
    "OUTS": lambda ln: int(ln.outs_recorded),
    "H": lambda ln: int(ln.h),
    "HR": lambda ln: int(ln.hr),
    "RBI": lambda ln: int(ln.rbi),
    "TB": _total_bases,
}


@dataclass(frozen=True, slots=True)
class PropDistribution:
    """The full integer-support PMF of one prop for one player over N iterations.

    Build it via :meth:`from_samples` (or, for a whole run, via
    :class:`PropDistributionSet`).  The PMF is stored as two aligned arrays:
    :attr:`support` (the sorted distinct integer values that occurred at least
    once -- a COMPACT support, not a dense 0..max range) and :attr:`probabilities`
    (each value's relative frequency, summing to 1.0).  The over/under helpers
    work for ANY line, integer or half-integer.
    """

    player_id: int
    prop: str
    #: Number of iterations (== number of per-game samples this PMF summarises).
    n: int
    #: Sorted distinct integer values that occurred (the compact PMF support).
    support: NDArray[np.int64]
    #: Probability of each value in :attr:`support` (aligned; sums to 1.0).
    probabilities: NDArray[np.float64]
    #: Sample mean / median / std (ddof=1; 0.0 for n < 2) of the raw values.
    mean: float
    median: float
    std: float

    # ------------------------------------------------------------------ PMF
    def pmf(self) -> "dict[int, float]":
        """The PMF as a plain ``{value: probability}`` dict (probabilities sum to 1).

        Only values that actually occurred appear (a compact support); a value not
        in the dict has probability 0 -- use :meth:`prob` for a zero-safe lookup.
        """
        return {int(v): float(p) for v, p in zip(self.support, self.probabilities)}

    def prob(self, value: int) -> float:
        """``P(X == value)`` -- 0.0 for a value that never occurred (zero-safe)."""
        idx = np.searchsorted(self.support, int(value))
        if idx < self.support.size and int(self.support[idx]) == int(value):
            return float(self.probabilities[idx])
        return 0.0

    # ----------------------------------------------------------- over/under
    def p_at_least(self, line: float) -> float:
        """``P(X >= line)`` -- the inclusive over.  ``line`` may be half-integer."""
        mask = self.support >= float(line)
        return float(self.probabilities[mask].sum())

    def p_at_most(self, line: float) -> float:
        """``P(X <= line)`` -- the inclusive under.  ``line`` may be half-integer."""
        mask = self.support <= float(line)
        return float(self.probabilities[mask].sum())

    def p_greater(self, line: float) -> float:
        """``P(X > line)`` -- the strict over.  ``line`` may be half-integer."""
        mask = self.support > float(line)
        return float(self.probabilities[mask].sum())

    def p_less(self, line: float) -> float:
        """``P(X < line)`` -- the strict under.  ``line`` may be half-integer."""
        mask = self.support < float(line)
        return float(self.probabilities[mask].sum())

    def p_push(self, line: float) -> float:
        """``P(X == line)`` -- the push probability.

        Nonzero only for an INTEGER line that a value can land exactly on; a
        half-integer line can never push, so this is 0.0 there.
        """
        if float(line) != round(float(line)):
            return 0.0
        return self.prob(int(round(float(line))))

    def p_over(self, line: float) -> float:
        """Betting OVER at ``line`` (the sportsbook convention).

        Half-integer line -> no push, over == ``P(X >= ceil(line))`` == strict
        over.  Integer line -> a value ON the line pushes, so over is the STRICT
        ``P(X > line)`` and the push mass is reported by :meth:`p_push`.  Thus
        ``p_over + p_under + p_push == 1`` for an integer line, and
        ``p_over + p_under == 1`` for a half-integer line.
        """
        return self.p_greater(line)

    def p_under(self, line: float) -> float:
        """Betting UNDER at ``line`` (the sportsbook convention; see :meth:`p_over`)."""
        return self.p_less(line)

    # ----------------------------------------------------------------- CI
    def mean_ci(
        self, *, confidence_level: float = 0.95
    ) -> ConfidenceInterval:
        """Wald CI on the prop's sample mean (the SIM-327 :class:`ConfidenceInterval`).

        half-width = z * (s / sqrt(n)), s = std(ddof=1).  With n < 2 the spread is
        unestimable, so a zero-width interval at the single observation is
        returned -- the same honest degenerate answer ``simulation.results._mean_ci``
        gives.
        """
        z = _Z_BY_LEVEL.get(round(float(confidence_level), 4), _Z_95)
        if self.n < 2:
            return ConfidenceInterval(
                point=self.mean,
                low=self.mean,
                high=self.mean,
                level=float(confidence_level),
                method="normal",
            )
        margin = z * (self.std / (self.n ** 0.5))
        return ConfidenceInterval(
            point=self.mean,
            low=self.mean - margin,
            high=self.mean + margin,
            level=float(confidence_level),
            method="normal",
        )

    # ------------------------------------------------------------- builder
    @classmethod
    def from_samples(
        cls,
        player_id: int,
        prop: str,
        samples: "Iterable[int]",
    ) -> "PropDistribution":
        """Build a PMF from the per-iteration integer ``samples`` for one prop.

        ``samples`` is one integer per simulated game (e.g. that game's K count).
        Must be non-empty.  The PMF support is the SORTED DISTINCT values that
        occurred and the probabilities are their relative frequencies.
        """
        arr = np.asarray(list(samples), dtype=np.int64)
        if arr.size == 0:
            raise ValueError(
                f"PropDistribution.from_samples({player_id!r}, {prop!r}) "
                "requires >= 1 sample"
            )
        values, counts = np.unique(arr, return_counts=True)
        probs = counts.astype(np.float64) / float(arr.size)
        n = int(arr.size)
        return cls(
            player_id=int(player_id),
            prop=str(prop),
            n=n,
            support=values.astype(np.int64),
            probabilities=probs,
            mean=float(arr.mean()),
            median=float(statistics.median(arr.tolist())),
            std=float(arr.std(ddof=1)) if n >= 2 else 0.0,
        )


@dataclass(slots=True)
class PropDistributionSet:
    """All prop PMFs for all players over one Monte-Carlo run (SIM-329).

    The collection a downstream consumer (SIM-339 CLV) reads: indexed by
    ``player_id`` then prop name.  Build it with :meth:`from_boxscores` (a list of
    per-game :class:`BoxScore`) or :meth:`from_results` (a list of
    :class:`GameSimResult`, each carrying ``.boxscore``).

    A player who never appears in any boxscore is simply ABSENT from
    :attr:`by_player`; :meth:`get` returns ``None`` for them rather than raising.
    """

    #: Number of iterations (games) aggregated.
    n_iterations: int
    #: ``player_id -> {prop_name -> PropDistribution}``.  Built lazily-uniformly:
    #: a player gets exactly the props (pitcher and/or batter) for which they had
    #: any per-game line -- but every player who pitched gets ALL pitcher props
    #: (even an all-zero ER PMF), and every batter gets all batter props, so the
    #: keyspace is predictable for a consumer iterating a known prop list.
    by_player: "dict[int, dict[str, PropDistribution]]" = field(default_factory=dict)

    # ------------------------------------------------------------- lookups
    def get(
        self, player_id: int, prop: "str | None" = None
    ) -> "PropDistribution | dict[str, PropDistribution] | None":
        """Look up a player's props (or one prop), ``None`` if absent.

        ``get(pid)`` -> the ``{prop: PropDistribution}`` map (or ``None``);
        ``get(pid, "K")`` -> that single :class:`PropDistribution` (or ``None``).
        Never raises on an unknown id/prop -- the explicit "no signal" contract.
        """
        player = self.by_player.get(int(player_id))
        if player is None:
            return None
        if prop is None:
            return player
        return player.get(str(prop))

    def player_ids(self) -> "list[int]":
        """All player ids with at least one prop PMF, ascending."""
        return sorted(self.by_player.keys())

    def __contains__(self, player_id: object) -> bool:
        try:
            return int(player_id) in self.by_player  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------ builders
    @classmethod
    def from_boxscores(
        cls, boxscores: "Iterable[BoxScore]"
    ) -> "PropDistributionSet":
        """Build the full prop-PMF set from the N per-game :class:`BoxScore`s.

        For each game we read every player's line; a player who did not appear in
        a given game contributes a ZERO for that game to every prop they own (a
        DNP is 0 K / 0 H, etc.), so the PMF denominator is always the full N -- a
        consumer reading ``P(K >= 6)`` gets a probability over the whole run, not
        over "games the pitcher happened to be listed in".

        Whether a player gets pitcher props, batter props, or both is decided by
        whether they EVER recorded pitching / batting activity across the run
        (mirroring :attr:`BoxScore.pitchers` / :attr:`BoxScore.batters`), so a
        pure batter never gets a (meaningless) all-zero K PMF.
        """
        games = list(boxscores)
        n = len(games)
        if n == 0:
            return cls(n_iterations=0, by_player={})

        # Pass 1: discover, per player, whether they ever pitched / batted, and
        # collect every per-game line keyed by (game_index, player_id).
        pitched: "set[int]" = set()
        batted: "set[int]" = set()
        all_ids: "set[int]" = set()
        per_game_lines: "list[dict[int, PlayerStatLine]]" = []
        for box in games:
            lines = dict(box.lines)
            per_game_lines.append(lines)
            for pid, ln in lines.items():
                all_ids.add(int(pid))
                if ln.outs_recorded or ln.k or ln.bb or ln.er:
                    pitched.add(int(pid))
                if ln.ab or ln.h or ln.rbi:
                    batted.add(int(pid))

        # Pass 2: for each player, build the per-prop sample vector (length N,
        # zero-filled for games the player did not appear) and PMF it.
        by_player: "dict[int, dict[str, PropDistribution]]" = {}
        for pid in sorted(all_ids):
            props_for_player: "list[str]" = []
            if pid in pitched:
                props_for_player.extend(PITCHER_PROPS)
            if pid in batted:
                props_for_player.extend(BATTER_PROPS)
            if not props_for_player:
                # Appeared in a line but with no activity at all -- nothing to
                # build a meaningful prop on; skip (treated as absent).
                continue

            # Gather this player's per-game line (or None for a DNP) once.
            game_lines = [g.get(pid) for g in per_game_lines]

            dists: "dict[str, PropDistribution]" = {}
            for prop in props_for_player:
                extract = _PROP_EXTRACTORS[prop]
                samples = [
                    extract(ln) if ln is not None else 0 for ln in game_lines
                ]
                dists[prop] = PropDistribution.from_samples(pid, prop, samples)
            by_player[pid] = dists

        return cls(n_iterations=n, by_player=by_player)

    @classmethod
    def from_results(
        cls, results: "Iterable[GameSimResult]"
    ) -> "PropDistributionSet":
        """Build the prop-PMF set from N :class:`GameSimResult`s (each ``.boxscore``).

        Convenience wrapper over :meth:`from_boxscores`: pulls ``r.boxscore`` from
        each result.  A result with ``boxscore is None`` (a SIM-320/327 run that
        did not accumulate one) contributes an EMPTY boxscore -- it still counts
        toward N (every player gets a 0 for that game) so the denominator stays the
        full iteration count.
        """
        boxscores = [
            r.boxscore if r.boxscore is not None else BoxScore()
            for r in results
        ]
        return cls.from_boxscores(boxscores)


__all__ = [
    "PropDistribution",
    "PropDistributionSet",
    "PITCHER_PROPS",
    "BATTER_PROPS",
    "ALL_PROPS",
    "TB_IS_LOWER_BOUND",
]
