"""
prop_distributions.py
=====================
The **prop-distribution aggregator**: one probability distribution per player
and prop over the N games of a Monte-Carlo run.

WHAT THIS IS
------------
Every simulated game produces a :class:`~simulation.results.BoxScore`: one
:class:`PlayerStatLine` per player, keyed by ``player_id`` (AB/H/HR/RBI on
offense, outs/K/BB/ER on defense).  A run keeps every game's box score, not a
pre-binned histogram, so this module can build full distributions.  For **each
player and each prop** it builds a full integer-support **probability mass
function** (PMF): value -> probability, summing to 1, not just a mean.  The
betting layer (``betting/clv_engine.py``) prices an over/under against a book
line from these PMFs; a mean alone cannot answer ``P(K >= 6.5)``.

THE PROPS
---------
Every prop reads directly off the :class:`PlayerStatLine` fields.  Each tuple
lists its four core props first, then the other prop markets the platform
prices (the fifteen prop markets, SIM-421).  Keep that order: the frontend
renders a player's prop chips in tuple order.

  * **Pitcher** (charged on defense) -- :data:`PITCHER_PROPS`:
      - ``K``         -- strikeouts (``line.k``)
      - ``BB``        -- walks (``line.bb``)
      - ``ER``        -- earned runs (``line.er``)
      - ``OUTS``      -- outs recorded == IP in thirds (``line.outs_recorded``).
        The prop is in *outs* (an exact integer), not the ``x.1/x.2`` IP
        string, because a PMF needs an integer support: ``2.1`` IP is not a
        number you can sum, ``7`` outs is.  A consumer that wants
        "P(IP >= 6.0)" asks ``p_at_least(18)`` (6 innings == 18 outs).
      - ``H_ALLOWED`` -- hits allowed (``line.h_allowed``).  This is the
        PITCHER's market.  It is a separate field from the batter's ``H``.

  * **Batter** (credited on offense) -- :data:`BATTER_PROPS`:
      - ``H``   -- hits (``line.h``)
      - ``HR``  -- home runs (``line.hr``)
      - ``RBI`` -- runs batted in (``line.rbi``)
      - ``TB``  -- total bases (see the note below).
      - ``1B``  -- singles.  The box score tracks doubles, triples and home
        runs as subsets of ``h``, so singles are the remainder
        ``h - b2 - b3 - hr``.
      - ``2B``  -- doubles (``line.b2``)
      - ``3B``  -- triples (``line.b3``)
      - ``R``   -- runs scored (``line.r``)
      - ``SB``  -- stolen bases (``line.sb``)
      - ``HRR`` -- hits + runs + RBI (``line.h + line.r + line.rbi``).
        The market prices this combined line as ONE number.  The module sums
        it PER ITERATION (one game's ``h + r + rbi``) and builds the PMF from
        those per-game sums.  Do NOT build it from the three marginal PMFs.
        A convolution of the H, R and RBI PMFs treats them as independent.
        They are not: a home run is a hit, a run and an RBI at once.  The
        convolution throws that within-game correlation away and gives a
        wrong PMF.

TOTAL BASES (TB)
----------------
True total bases is ``1B + 2*2B + 3*3B + 4*HR``.  The box score tracks doubles
(``b2``) and triples (``b3``) as subsets of ``h``, alongside ``hr``.  Singles
are therefore the remainder (``h - b2 - b3 - hr``), so :func:`_total_bases`
computes TB exactly::

    TB = (h - b2 - b3 - hr)*1 + b2*2 + b3*3 + hr*4 = h + b2 + 2*b3 + 3*hr

:func:`_total_bases` is the one place that maps a hit type to its bases.

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from simulation.results import (
    BoxScore,
    ConfidenceInterval,
    GameSimResult,
    PlayerStatLine,
)

#: Two-sided z-multipliers for the common confidence levels (mirrors the table in
#: ``simulation/results.py``, so a prop CI and a run-summary CI agree on z).
_Z_95 = 1.959963984540054
_Z_BY_LEVEL: dict[float, float] = {
    0.80: 1.2815515594457,
    0.90: 1.6448536269514722,
    0.95: _Z_95,
    0.99: 2.5758293035489004,
}

#: Pitcher prop names (charged on defense).  The four core props stay first:
#: the frontend renders chips in tuple order.
PITCHER_PROPS: tuple[str, ...] = ("K", "BB", "ER", "OUTS", "H_ALLOWED")
#: Batter prop names (credited on offense).  The four core props stay first.
BATTER_PROPS: tuple[str, ...] = ("H", "HR", "RBI", "TB", "1B", "2B", "3B", "R", "SB", "HRR")
#: Every prop name this aggregator can build.
ALL_PROPS: tuple[str, ...] = PITCHER_PROPS + BATTER_PROPS


def _total_bases(line: PlayerStatLine) -> int:
    """True total bases from the (h, b2, b3, hr) fields.

    True TB = 1B + 2*2B + 3*3B + 4*HR.  Doubles (``b2``) and triples (``b3``)
    are subsets of ``h``, so singles are the remainder (``h - b2 - b3 - hr``)::

        TB = (h - b2 - b3 - hr)*1 + b2*2 + b3*3 + hr*4
           = h + b2 + 2*b3 + 3*hr

    This is the one place that maps a hit type to its bases.
    """
    h = int(line.h)
    b2 = int(line.b2)
    b3 = int(line.b3)
    hr = int(line.hr)
    return h + b2 + 2 * b3 + 3 * hr


def _singles(line: PlayerStatLine) -> int:
    """Singles from the (h, b2, b3, hr) fields.

    The boxscore tracks doubles (``b2``), triples (``b3``) and home runs
    (``hr``) as subsets of ``h``, so singles are the remainder::

        1B = h - b2 - b3 - hr
    """
    return int(line.h) - int(line.b2) - int(line.b3) - int(line.hr)


def _hits_runs_rbi(line: PlayerStatLine) -> int:
    """Hits + runs + RBI for ONE game.

    The market prices this combined line as one number.  This function sums
    the three fields for one game, and the PMF is built from those per-game
    sums.  A convolution of the three marginal PMFs drops the within-game
    correlation between a hit, a run and an RBI, so that PMF is wrong.
    """
    return int(line.h) + int(line.r) + int(line.rbi)


#: How to read each prop's integer value off a single-game :class:`PlayerStatLine`.
#: Keeping the extractors in one table makes :meth:`PropDistributionSet.from_boxscores`
#: a single uniform loop and keeps "which field is which prop" in exactly one place.
_PROP_EXTRACTORS = {
    "K": lambda ln: int(ln.k),
    "BB": lambda ln: int(ln.bb),
    "ER": lambda ln: int(ln.er),
    "OUTS": lambda ln: int(ln.outs_recorded),
    "H_ALLOWED": lambda ln: int(ln.h_allowed),  # the pitcher's hits allowed
    "H": lambda ln: int(ln.h),
    "HR": lambda ln: int(ln.hr),
    "RBI": lambda ln: int(ln.rbi),
    "TB": _total_bases,
    # The market's other batter lines.
    "1B": _singles,
    "2B": lambda ln: int(ln.b2),
    "3B": lambda ln: int(ln.b3),
    "R": lambda ln: int(ln.r),
    "SB": lambda ln: int(ln.sb),
    "HRR": _hits_runs_rbi,
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
    def pmf(self) -> dict[int, float]:
        """The PMF as a plain ``{value: probability}`` dict (probabilities sum to 1).

        Only values that actually occurred appear (a compact support); a value not
        in the dict has probability 0 -- use :meth:`prob` for a zero-safe lookup.
        """
        return {int(v): float(p) for v, p in zip(self.support, self.probabilities, strict=False)}

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
    def mean_ci(self, *, confidence_level: float = 0.95) -> ConfidenceInterval:
        """The normal-approximation (Wald) confidence interval on the prop's sample mean.

        half-width = z * (s / sqrt(n)), s = std(ddof=1).  With n < 2 the spread
        cannot be estimated, so the result is a zero-width interval at the single
        observation, the same answer ``simulation.results._mean_ci`` gives.
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
        margin = z * (self.std / (self.n**0.5))
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
        samples: Iterable[int],
    ) -> PropDistribution:
        """Build a PMF from the per-iteration integer ``samples`` for one prop.

        ``samples`` is one integer per simulated game (e.g. that game's K count).
        Must be non-empty.  The PMF support is the SORTED DISTINCT values that
        occurred and the probabilities are their relative frequencies.
        """
        arr = np.asarray(list(samples), dtype=np.int64)
        if arr.size == 0:
            raise ValueError(
                f"PropDistribution.from_samples({player_id!r}, {prop!r}) requires >= 1 sample"
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
    """All prop PMFs for all players over one Monte-Carlo run.

    The API's prop routes and the betting layer read it, indexed by
    ``player_id`` then prop name.  Build it with :meth:`from_boxscores` (a list of
    per-game :class:`BoxScore`) or :meth:`from_results` (a list of
    :class:`GameSimResult`, each carrying ``.boxscore``).

    A player who never appears in any box score is ABSENT from
    :attr:`by_player`; :meth:`get` returns ``None`` for them rather than raising.
    """

    #: Number of iterations (games) aggregated.
    n_iterations: int
    #: ``player_id -> {prop_name -> PropDistribution}``.  A player who pitched
    #: gets ALL pitcher props (even an all-zero ER PMF), and a player who batted
    #: gets all batter props, so the key set is predictable for a consumer that
    #: iterates a known prop list.
    by_player: dict[int, dict[str, PropDistribution]] = field(default_factory=dict)

    # ------------------------------------------------------------- lookups
    def get(
        self, player_id: int, prop: str | None = None
    ) -> PropDistribution | dict[str, PropDistribution] | None:
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

    def player_ids(self) -> list[int]:
        """All player ids with at least one prop PMF, ascending."""
        return sorted(self.by_player.keys())

    def __contains__(self, player_id: object) -> bool:
        try:
            return int(player_id) in self.by_player  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------ storage
    def to_json(self) -> dict[str, Any]:
        """A JSON-ready dict of the whole set (a stored run row's ``prop_set``)."""
        return {
            "n_iterations": int(self.n_iterations),
            "by_player": {
                str(pid): {
                    prop: {
                        "n": int(d.n),
                        "support": [int(v) for v in d.support],
                        "probabilities": [float(v) for v in d.probabilities],
                        "mean": float(d.mean),
                        "median": float(d.median),
                        "std": float(d.std),
                    }
                    for prop, d in props.items()
                }
                for pid, props in self.by_player.items()
            },
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PropDistributionSet:
        """The inverse of :meth:`to_json`."""
        by_player: dict[int, dict[str, PropDistribution]] = {}
        for pid_s, props in (data.get("by_player") or {}).items():
            pid = int(pid_s)
            by_player[pid] = {
                prop: PropDistribution(
                    player_id=pid,
                    prop=str(prop),
                    n=int(d["n"]),
                    support=np.asarray(d["support"], dtype=np.int64),
                    probabilities=np.asarray(d["probabilities"], dtype=np.float64),
                    mean=float(d["mean"]),
                    median=float(d["median"]),
                    std=float(d["std"]),
                )
                for prop, d in props.items()
            }
        return cls(n_iterations=int(data.get("n_iterations", 0)), by_player=by_player)

    # ------------------------------------------------------------ builders
    @classmethod
    def from_boxscores(cls, boxscores: Iterable[BoxScore]) -> PropDistributionSet:
        """Build the full prop-PMF set from the N per-game :class:`BoxScore`s.

        Every player's line is read for each game.  A player who did not appear
        in a given game contributes a ZERO for that game to every prop he owns (a
        DNP is 0 K / 0 H, etc.), so the PMF denominator is always the full N.  A
        consumer reading ``P(K >= 6)`` gets a probability over the whole run, not
        over "games the pitcher happened to be listed in".

        Whether a player gets pitcher props, batter props, or both depends on
        whether he EVER recorded pitching or batting activity across the run,
        so a pure batter never gets a meaningless all-zero K PMF.  Batting
        activity is an AB, a hit, an RBI, a run, a steal, a double or a triple,
        so a pinch runner who only scores or steals owns the batter props.
        Pitching activity is an out, a batter faced, a K, a BB, an ER or a hit
        allowed, so a reliever who faces one batter and records no out owns the
        pitcher props.  This net is wider than :attr:`BoxScore.batters` (an AB,
        a hit or an RBI) and :attr:`BoxScore.pitchers` (an out, a batter faced,
        a K, a BB or an ER, but not a hit allowed).
        """
        games = list(boxscores)
        n = len(games)
        if n == 0:
            return cls(n_iterations=0, by_player={})

        # Pass 1: discover, per player, whether they ever pitched / batted, and
        # collect every per-game line keyed by (game_index, player_id).
        pitched: set[int] = set()
        batted: set[int] = set()
        all_ids: set[int] = set()
        per_game_lines: list[dict[int, PlayerStatLine]] = []
        for box in games:
            lines = dict(box.lines)
            per_game_lines.append(lines)
            for pid, ln in lines.items():
                all_ids.add(int(pid))
                # A hit allowed is pitching activity too, and so is a batter
                # faced, which lists a reliever with no out.
                if (
                    ln.outs_recorded
                    or getattr(ln, "bf", 0)
                    or ln.k
                    or ln.bb
                    or ln.er
                    or ln.h_allowed
                ):
                    pitched.add(int(pid))
                # A run, a steal, a double or a triple is batting activity too,
                # so a pinch-runner-only game owns batter props.
                if ln.ab or ln.h or ln.rbi or ln.r or ln.sb or ln.b2 or ln.b3:
                    batted.add(int(pid))

        # Pass 2: for each player, build the per-prop sample vector (length N,
        # zero-filled for games the player did not appear) and PMF it.
        by_player: dict[int, dict[str, PropDistribution]] = {}
        for pid in sorted(all_ids):
            props_for_player: list[str] = []
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

            dists: dict[str, PropDistribution] = {}
            for prop in props_for_player:
                extract = _PROP_EXTRACTORS[prop]
                samples = [extract(ln) if ln is not None else 0 for ln in game_lines]
                dists[prop] = PropDistribution.from_samples(pid, prop, samples)
            by_player[pid] = dists

        return cls(n_iterations=n, by_player=by_player)

    @classmethod
    def from_results(cls, results: Iterable[GameSimResult]) -> PropDistributionSet:
        """Build the prop-PMF set from N :class:`GameSimResult`s (each ``.boxscore``).

        A convenience wrapper over :meth:`from_boxscores` that pulls
        ``r.boxscore`` from each result.  A result with ``boxscore is None`` (a
        result built without one) contributes an EMPTY box score.  It still counts
        toward N (every player gets a 0 for that game), so the denominator stays
        the full iteration count.
        """
        boxscores = [r.boxscore if r.boxscore is not None else BoxScore() for r in results]
        return cls.from_boxscores(boxscores)


__all__ = [
    "PropDistribution",
    "PropDistributionSet",
    "PITCHER_PROPS",
    "BATTER_PROPS",
    "ALL_PROPS",
]
