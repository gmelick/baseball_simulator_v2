"""The betting layer: the market's prices against the simulator, and the bets.

The simulator gives probabilities: the win probability
(:class:`~simulation.win_probability.WinProbability`), a player's prop
distribution (:class:`~simulation.prop_distributions.PropDistribution`, with
``p_over`` / ``p_under`` at any line) and the raw per-game scores
(:class:`~simulation.results.GameSimSummary`, for the totals and the run
lines). The market gives prices in American odds: ``-110`` means stake 110 to
win 100, ``+150`` means stake 100 to win 150. Decimal odds are the total return
per unit staked (``+150`` is 2.50). A book's prices carry the vig, its margin,
so the implied probabilities of a market's sides add up to more than 1.

The module works in two stages.

**Pricing.** Per market and per prop it gives:

* the **implied probability** of an American price, and the fair price of a
  probability;
* the **no-vig probability**: the implied probabilities with the vig removed,
  so that a market's sides add up to exactly 1 (the "de-vig");
* the **edge**: the simulator's probability minus the no-vig probability;
* the **expected value (EV)** per unit staked, from the simulator's probability
  at the offered price.

One :class:`EdgeReport` holds these numbers for one side of one market.

**Signals.** :func:`bet_signals_from_edges` turns a list of reports into bet
signals, in three steps:

* **Gate.** A report passes when its edge is above zero and at least
  ``min_edge``, and its EV is above ``min_ev``. The ``min_edge`` floor (default
  2%) drops an edge too small to matter next to the noise and the vig.
* **Stake.** Each passing report gets a ``stake_fraction``, a fraction of the
  bankroll, by fractional Kelly (below).
* **Rank.** The signals come back sorted by EV, highest first. A tie goes to
  the larger edge, then the larger stake.

Every number comes from the simulator's probability. A signal says that the
simulator prices a side above the market. It is not evidence that the bet
makes money.

How the vig comes off
---------------------
A two-way market's raw implied probabilities ``(q_a, q_b)`` add up to
``1 + m``, where ``m`` is the overround (the vig). The no-vig probabilities are
``p_a = q_a / (q_a + q_b)`` and ``p_b = q_b / (q_a + q_b)``. They add up to
exactly 1, and each side gives up the margin in proportion to its own implied
probability (the proportional method). Every two-way market uses it: the
moneyline, the run line, the total and each prop's over / under. A market of N
outcomes uses the same rule, ``p_i = q_i / sum_j(q_j)``: the three-way
first-inning and first-five moneylines, where the tie is the third outcome. A
run line listed as two separate bets has no opposite side; each price is
divided by the book's margin on another two-way market of the same game (see
the run-line section below).

The sign of the edge and the EV
-------------------------------
* ``edge = p_sim - p_fair``, against the no-vig probability: the market's
  opinion with the margin removed. A positive edge means the simulator rates
  the outcome more likely than the market does.
* ``EV = p_sim * b - (1 - p_sim)`` per unit staked, where ``b`` is the decimal
  odds of the OFFERED price minus 1 (the profit per unit on a win). The EV reads
  the offered price, because that is the price a bet takes. A positive EV means
  that the bet profits if ``p_sim`` is the true probability.

The Kelly stake
---------------
The Kelly criterion is the stake that maximises the long-run growth of a
bankroll. Take one bet at net odds ``b``, a win probability ``p`` and
``q = 1 - p``:

    full-Kelly fraction  f* = (b * p - q) / b

``b * p - q`` is the EV per unit staked (:func:`expected_value`), so
``f* = EV / b``. Full Kelly over-bets when ``p`` is too high. So the stake is a
fraction of it (``kelly_fraction``, default 0.25, "quarter Kelly"):

    stake_fraction = clamp( kelly_fraction * max(0, (b*p - q) / b),  0,  cap )

``max(0, .)`` gives a negative-EV side a zero stake: a signal backs only its
report's own side. The ``cap`` (``max_stake_fraction``, default 5% of the
bankroll) bounds the stake however large the raw Kelly number is.

Timing
------
A :class:`BetSignal` is advice, not an order. Its edge and EV hold at the price
that the source ``EdgeReport`` captured, and at a better price. When the price
moves, the caller rebuilds the report. This module has no timing rule.

The module is pure arithmetic: no database, no random numbers, no odds fetch.
The caller supplies the odds. The same inputs always give the same reports and
the same signals.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

# The simulation outputs that the report builders read. The core arithmetic
# takes plain probabilities, so a test feeds it synthetic numbers.
from simulation.prop_distributions import PropDistribution
from simulation.results import GameSimSummary
from simulation.win_probability import WinProbability

# ===========================================================================
# American <-> decimal <-> implied-probability conversions
# ===========================================================================


def american_to_decimal(american: float) -> float:
    """Convert American odds to decimal odds (the total return per unit staked).

    ``+150`` -> ``2.50`` (stake 1, profit 1.5, return 2.5).
    ``-110`` -> ``1.9090...`` (stake 1.1 to win 1).
    An American price of 0 does not exist and raises ``ValueError``.
    """
    a = float(american)
    if a == 0:
        raise ValueError("American odds of 0 are undefined")
    if a > 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / (-a)


def decimal_to_american(decimal_odds: float) -> float:
    """Convert decimal odds (>1.0) to American odds.

    ``2.50`` -> ``+150``; ``1.9090...`` -> ``-110``. At exactly even money
    (decimal 2.0) the convention is ``+100``. A decimal price of 1.0 or less
    raises ``ValueError``.
    """
    d = float(decimal_odds)
    if d <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    if d >= 2.0:
        return (d - 1.0) * 100.0
    return -100.0 / (d - 1.0)


def implied_prob_from_american(american: float) -> float:
    """The raw implied probability of an American price (the vig NOT removed).

    For a favourite (negative): ``p = -a / (-a + 100)``; e.g. ``-110 -> 0.5238``.
    For an underdog (positive): ``p = 100 / (a + 100)``; e.g. ``+150 -> 0.40``.
    This is the book's probability for ONE side, with its share of the vig: the
    two sides of a market add up to ``1 + overround``. :func:`devig_two_way` and
    :func:`devig_multiway` give the no-vig probability. A price of 0 raises
    ``ValueError``.
    """
    a = float(american)
    if a == 0:
        raise ValueError("American odds of 0 are undefined")
    if a > 0:
        return 100.0 / (a + 100.0)
    return -a / (-a + 100.0)


#: An alias of :func:`implied_prob_from_american`.
american_to_implied_prob = implied_prob_from_american


def prob_to_american(prob: float) -> float:
    """The inverse of :func:`implied_prob_from_american`: a probability -> its fair American price.

    The fair price has no vig: its implied probability equals ``prob``.
    ``0.5238... -> -110``, ``0.40 -> +150``. At exactly 0.5 the convention is
    ``+100``. A ``prob`` outside the open interval (0, 1) raises ``ValueError``.
    """
    p = float(prob)
    if not (0.0 < p < 1.0):
        raise ValueError("probability must be strictly between 0 and 1")
    if p <= 0.5:
        return (1.0 - p) / p * 100.0
    return -p / (1.0 - p) * 100.0


#: An alias of :func:`prob_to_american`.
fair_american_from_prob = prob_to_american


# ===========================================================================
# The de-vig: the no-vig probabilities of a market
# ===========================================================================


def devig_multiway(
    implied_probs: Sequence[float] | np.ndarray,
) -> list[float]:
    """Remove the vig from a market of N >= 2 outcomes (the proportional method).

    The raw implied probabilities ``q_i`` of the outcomes (each from
    :func:`implied_prob_from_american`) add up to ``1 + overround``. The no-vig
    probabilities are ``p_i = q_i / sum_j q_j``. The returned list adds up to 1
    and keeps the order of the inputs.

    Fewer than two outcomes, or an implied probability of 0 or less, raises
    ``ValueError``.
    """
    q = np.asarray(list(implied_probs), dtype=np.float64)
    if q.size < 2:
        raise ValueError("a market needs >= 2 outcomes to de-vig")
    if np.any(q <= 0.0):
        raise ValueError("every implied probability must be > 0")
    total = float(q.sum())
    if total <= 0.0:
        raise ValueError("implied probabilities must sum to > 0")
    return [float(x / total) for x in q]


def devig_two_way(
    over_american: float,
    under_american: float,
) -> tuple[float, float]:
    """De-vig a TWO-way market (the proportional method).

    The two sides' American prices (an over and an under, or a home and an away
    moneyline) become raw implied probabilities, then the pair is scaled to add
    up to exactly 1:

        p_a = q_a / (q_a + q_b),   p_b = q_b / (q_a + q_b)

    Returns ``(p_a, p_b)``, the no-vig probabilities of the first and the second
    argument. Each side gives up the overround in proportion to its own implied
    probability.
    """
    q_a = implied_prob_from_american(over_american)
    q_b = implied_prob_from_american(under_american)
    p_a, p_b = devig_multiway([q_a, q_b])
    return p_a, p_b


# ===========================================================================
# The edge and the expected value
# ===========================================================================


def edge(p_sim: float, p_fair: float) -> float:
    """The edge: the simulator's probability minus the market's no-vig probability.

    A positive edge means the simulator rates the side more likely than the
    market does. ``p_fair`` is a no-vig probability (from a de-vig call), so the
    comparison leaves out the book's margin.
    """
    return float(p_sim) - float(p_fair)


def expected_value(p_sim: float, offered_american: float) -> float:
    """The EV per unit staked on ``offered_american`` when the win probability is ``p_sim``.

    ``EV = p_sim * b - (1 - p_sim)``, where ``b = decimal_odds - 1`` is the profit
    per unit on a win. Equivalently ``EV = p_sim * decimal_odds - 1``. A positive
    EV means the bet profits at that price if ``p_sim`` is the true probability.
    The EV reads the OFFERED price, the vig included, because that is the price a
    bet takes; :func:`edge` reads the no-vig probability.
    """
    p = float(p_sim)
    b = american_to_decimal(offered_american) - 1.0
    return p * b - (1.0 - p)


# ===========================================================================
# The edge reports: one per side of a market or a prop
# ===========================================================================


class MarketSide(Enum):
    """Which side of a market a report is about.

    ``DRAW`` is the tie of a three-way market. A first-inning or first-five
    moneyline can end tied, so the book prices the tie as a third outcome."""

    HOME = "home"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"
    DRAW = "draw"


@dataclass(frozen=True, slots=True)
class OddsQuote:
    """One snapshot of a two-way market's American prices, supplied by the caller.

    ``side`` is the price of the side the report is about; ``other`` is the
    opposite side's price, which the de-vig needs. For the away moneyline,
    ``side`` is the away price and ``other`` the home price. ``line`` is the
    market line (None for a moneyline; a float for a total, a prop or a run
    line). The engine never fetches odds.
    """

    side: float
    other: float
    line: float | None = None


@dataclass(frozen=True, slots=True)
class TwoWayMarket:
    """A two-way market: the ENTRY quote, the price a bet takes now.

    ``side`` names the side that ``entry.side`` prices. The report builders
    take the side as their own argument and do not read this field.
    """

    side: MarketSide
    entry: OddsQuote


@dataclass(frozen=True, slots=True)
class EdgeReport:
    """The edge and the EV of one side of one market or prop.

    It holds the simulator's probability, the market's no-vig probability, and
    the edge and the EV at the entry price.
    """

    label: str
    side: MarketSide
    line: float | None

    #: The simulator's probability of the bet side.
    sim_prob: float
    #: The no-vig probability of the bet side at the entry.
    market_fair_prob: float

    #: edge = sim_prob - market_fair_prob. Positive = the simulator rates the side
    #: above the market.
    edge: float
    #: The EV per unit staked at the offered (entry) American price, under sim_prob.
    ev: float

    #: The American price of the bet side at the entry (the offered price).
    offered_american: float
    #: The fair American price of sim_prob: the price with no vig.
    sim_fair_american: float

    @property
    def positive_edge(self) -> bool:
        """True when the edge is above zero."""
        return self.edge > 0.0


def _build_edge_report(
    *,
    label: str,
    side: MarketSide,
    line: float | None,
    sim_prob: float,
    market: TwoWayMarket,
) -> EdgeReport:
    """Build a two-way report: de-vig the entry market, then the edge and the EV.
    ``market.entry.side`` is the bet side's price and ``market.entry.other`` the
    opposite side's."""
    p = float(sim_prob)
    # The entry market's no-vig probability of the bet side.
    entry_fair, _ = devig_two_way(market.entry.side, market.entry.other)
    ev = expected_value(p, market.entry.side)
    return EdgeReport(
        label=label,
        side=side,
        line=line,
        sim_prob=p,
        market_fair_prob=entry_fair,
        edge=edge(p, entry_fair),
        ev=ev,
        offered_american=float(market.entry.side),
        sim_fair_american=prob_to_american(p),
    )


def moneyline_edge_report(
    win_prob: WinProbability,
    market: TwoWayMarket,
    *,
    side: MarketSide = MarketSide.HOME,
) -> EdgeReport:
    """The edge and EV of a MONEYLINE side, from a :class:`WinProbability`.

    The simulator's probability is ``win_prob.home_win_prob`` for
    :data:`MarketSide.HOME` and ``win_prob.away_win_prob`` for
    :data:`MarketSide.AWAY`. The two add up to 1: the win probability has
    already split or dropped the simulated ties. ``market.entry.side`` is the
    chosen side's American moneyline and ``market.entry.other`` the opposite
    side's. Any other side raises ``ValueError``.
    """
    if side is MarketSide.HOME:
        sim_p = win_prob.home_win_prob
    elif side is MarketSide.AWAY:
        sim_p = win_prob.away_win_prob
    else:
        raise ValueError("moneyline side must be HOME or AWAY")
    return _build_edge_report(
        label="moneyline",
        side=side,
        line=None,
        sim_prob=sim_p,
        market=market,
    )


def prop_edge_report(
    dist: PropDistribution,
    market: TwoWayMarket,
    *,
    side: MarketSide = MarketSide.OVER,
) -> EdgeReport:
    """The edge and EV of a PLAYER PROP side, from a :class:`PropDistribution`.

    The simulator's probability is ``dist.p_over(line)`` for :data:`MarketSide.OVER`
    and ``dist.p_under(line)`` for :data:`MarketSide.UNDER`, by the sportsbook
    convention: on a half-integer line the over and the under add up to 1; on a
    whole-number line a value on the line is a push and counts for neither. The
    line is ``market.entry.line``, the book's prop line. ``market.entry.side`` is
    the chosen side's price (over_ml for OVER, under_ml for UNDER) and ``.other``
    the opposite price. A missing line or another side raises ``ValueError``.
    """
    line = market.entry.line
    if line is None:
        raise ValueError("a prop market must carry a line (market.entry.line)")
    if side is MarketSide.OVER:
        sim_p = dist.p_over(line)
    elif side is MarketSide.UNDER:
        sim_p = dist.p_under(line)
    else:
        raise ValueError("prop side must be OVER or UNDER")
    return _build_edge_report(
        label=f"prop:{dist.prop}",
        side=side,
        line=float(line),
        sim_prob=sim_p,
        market=market,
    )


def total_over_under_edge_report(
    summary: GameSimSummary,
    market: TwoWayMarket,
    *,
    side: MarketSide = MarketSide.OVER,
) -> EdgeReport:
    """The edge and EV of a game TOTAL side (over / under), from the raw scores.

    The simulator's probability comes from the per-game ``total_scores`` of the
    :class:`GameSimSummary`, by the sportsbook convention. On a half-integer line
    there is no push: the over is ``P(total > line)``, the under
    ``P(total < line)``, and they add up to 1. On a whole-number line a total on
    the line is a push and counts for neither side, as on a prop.

    ``market.entry.line`` is the total line; ``market.entry.side`` is the chosen
    side's price (over_ml for OVER, under_ml for UNDER) and ``.other`` the
    opposite price. The arithmetic is :func:`samples_over_under_edge_report`; this
    function passes it ``summary.total_scores`` and the label ``"total"``.
    """
    if market.entry.line is not None and np.asarray(summary.total_scores).size == 0:
        raise ValueError("GameSimSummary has no total_scores to price a total")
    return samples_over_under_edge_report(summary.total_scores, market, side=side, label="total")


def samples_over_under_edge_report(
    samples: Sequence[float] | np.ndarray,
    market: TwoWayMarket,
    *,
    side: MarketSide = MarketSide.OVER,
    label: str,
) -> EdgeReport:
    """The edge and EV of one side of any over / under market, from raw samples.

    ``samples`` holds one number per simulated game: the number the market
    settles on (the full-game runs, the first-five runs, one team's runs, the
    first-inning runs). The over is the strict ``P(sample > line)`` and the under
    the strict ``P(sample < line)``. A sample on a whole-number line is a push and
    counts for neither side. ``label`` names the market on the report
    (``"f5_total"``).

    ``market.entry.line`` is the line; ``market.entry.side`` is the chosen side's
    price and ``.other`` the opposite price. A missing line, no samples or a side
    other than OVER / UNDER raises ``ValueError``.
    """
    line = market.entry.line
    if line is None:
        raise ValueError("a total market must carry a line (market.entry.line)")
    values = np.asarray(samples, dtype=np.float64)
    n = values.size
    if n == 0:
        raise ValueError("no samples to price an over / under market")
    if side is MarketSide.OVER:
        sim_p = float(np.count_nonzero(values > float(line))) / n
    elif side is MarketSide.UNDER:
        sim_p = float(np.count_nonzero(values < float(line))) / n
    else:
        raise ValueError("total side must be OVER or UNDER")
    return _build_edge_report(
        label=label,
        side=side,
        line=float(line),
        sim_prob=sim_p,
        market=market,
    )


def three_way_edge_report(
    p_home: float,
    p_away: float,
    p_draw: float,
    *,
    label: str,
    side: MarketSide,
    home_ml: float,
    away_ml: float,
    draw_ml: float,
) -> EdgeReport:
    """The edge and EV of one side of a THREE-way market (home, away or the tie).

    The first-inning and first-five moneylines can end tied, so the book prices
    three outcomes. The no-vig probability of the chosen side comes from
    :func:`devig_multiway` over the three implied probabilities, so the book's
    margin is shared across all three. ``p_home`` / ``p_away`` / ``p_draw`` are
    the simulator's probabilities of the three outcomes. The EV reads the side's
    own price. Like every report, a simulator probability of exactly 0 or 1
    raises ``ValueError`` (it has no fair price).
    """
    order = (MarketSide.HOME, MarketSide.AWAY, MarketSide.DRAW)
    if side not in order:
        raise ValueError("a three-way side must be HOME, AWAY or DRAW")
    index = order.index(side)
    prices = (float(home_ml), float(away_ml), float(draw_ml))
    fair = devig_multiway([implied_prob_from_american(a) for a in prices])[index]
    p = float((p_home, p_away, p_draw)[index])
    own = prices[index]
    return EdgeReport(
        label=label,
        side=side,
        line=None,
        sim_prob=p,
        market_fair_prob=fair,
        edge=edge(p, fair),
        ev=expected_value(p, own),
        offered_american=own,
        sim_fair_american=prob_to_american(p),
    )


# ---------------------------------------------------------------------------
# The run line (spread): the cover probability from the raw score margins
# ---------------------------------------------------------------------------


def _as_margin_array(
    summary_or_margin: GameSimSummary | Sequence[float] | np.ndarray,
) -> np.ndarray:
    """The per-game score margin (``home - away``) as a 1-D float64 array.

    A :class:`GameSimSummary` gives ``home_scores - away_scores``. Any other
    input is already a sequence of ``home - away`` margins.
    """
    if isinstance(summary_or_margin, GameSimSummary):
        home = np.asarray(summary_or_margin.home_scores, dtype=np.float64)
        away = np.asarray(summary_or_margin.away_scores, dtype=np.float64)
        return home - away
    return np.asarray(list(summary_or_margin), dtype=np.float64)


def spread_cover_prob(
    summary_or_margin: GameSimSummary | Sequence[float] | np.ndarray,
    line: float,
    side: MarketSide = MarketSide.HOME,
) -> float:
    """The simulator's cover probability of a RUN LINE (spread) side at ``line``.

    ``margin = home - away`` per simulated game (from a :class:`GameSimSummary`
    or a raw margin array). ``line`` is the HOME side's spread: a home favourite
    gives runs at a NEGATIVE line (e.g. ``-1.5``), a home underdog gets runs at a
    POSITIVE line. The away line is the negative of the home line (home ``-1.5``
    = away ``+1.5``).

    The cover rules (``L`` = the home line):

      * **HOME** covers when the margin plus the line is positive::

            (home - away) + L > 0   <=>   margin > -L

        At ``L = -1.5`` the home favourite covers when ``margin > 1.5`` (a win by
        2 or more). At ``L = +1.5`` the home underdog covers when
        ``margin > -1.5`` (a win, or a loss by 1).

      * **AWAY** covers when the away margin plus the away line (``-L``) is
        positive::

            (away - home) + (-L) > 0   <=>   -margin - L > 0   <=>   margin < -L

        So AWAY covers when ``margin < -L``. A margin of exactly ``-L`` belongs to
        neither side: it is the push.

    The push. Baseball margins are whole numbers, so:

      * a HALF-integer line (``-1.5`` / ``+1.5``) never pushes: no margin lands on
        ``-L``, and ``P(home covers) + P(away covers) == 1``;
      * a WHOLE-number line (``-1`` / ``+1``, or the pick'em ``0``) can push: a
        game with ``margin == -L`` counts for neither side, so
        ``P(home covers) + P(away covers) + P(push) == 1``. The totals and the
        props use the same push rule.

    Both sides use strict inequalities, which is what leaves the push out on a
    whole-number line. Returns a probability in ``[0, 1]``. No margins, or a side
    other than HOME / AWAY, raises ``ValueError``.
    """
    margin = _as_margin_array(summary_or_margin)
    n = margin.size
    if n == 0:
        raise ValueError("no score margins to price a run line / spread")
    threshold = -float(line)  # the cover boundary: margin vs -L
    if side is MarketSide.HOME:
        return float(np.count_nonzero(margin > threshold)) / n
    if side is MarketSide.AWAY:
        return float(np.count_nonzero(margin < threshold)) / n
    raise ValueError("run line / spread side must be HOME or AWAY")


def run_line_edge_report(
    summary_or_margin: GameSimSummary | Sequence[float] | np.ndarray,
    market: TwoWayMarket,
    *,
    side: MarketSide = MarketSide.HOME,
    line: float = -1.5,
) -> EdgeReport:
    """The edge and EV of a RUN LINE (spread) side that is one half of a pair.

    The simulator's probability is the cover probability
    (:func:`spread_cover_prob`) over the per-game margin ``home - away``, from a
    :class:`GameSimSummary` or a raw margin array. Baseball's standard run line
    is ``+/-1.5``, the default.

      * ``line`` is the HOME line (negative = home gives runs). The away line is
        its negative. A set ``market.entry.line`` overrides the ``line``
        argument. The SAME home line serves both sides: AWAY is priced at
        ``-line``. So the caller passes the home line, in ``line`` or in
        ``market.entry.line``, whichever side it reports.
      * ``side`` picks HOME or AWAY. ``market.entry.side`` is that side's American
        price and ``market.entry.other`` the opposite side's, as on the moneyline.
      * A push (only on a whole-number line) counts for neither side. At ``+/-1.5``
        the HOME and AWAY cover probabilities add up to 1.

    Returns an :class:`EdgeReport` labelled ``"run_line"``. A run line listed as
    two separate bets goes through :func:`one_sided_edge_report` instead.
    """
    eff_line = market.entry.line if market.entry.line is not None else float(line)
    sim_p = spread_cover_prob(summary_or_margin, eff_line, side)
    return _build_edge_report(
        label="run_line",
        side=side,
        line=float(eff_line),
        sim_prob=sim_p,
        market=market,
    )


# ===========================================================================
# A run line: one bet with two sides (a pair), or two separate bets
# ===========================================================================
#
# raw.game_odds keeps each team's run line at its own spread, as the book lists
# it. A PAIR is one bet with two sides (home −0.5 / away +0.5): exactly one side
# wins, so the two prices de-vig against each other. TWO SEPARATE BETS share a
# sign or carry two different lines. Home −1.5 / away −1.5 both lose when the
# margin is one run or less. Home +1.5 / away +1.5 both win then. De-vigging
# their prices against each other mis-prices each side, up or down. So each
# separate bet is priced from its OWN price over the book's margin on another
# two-way market of the same game. Here the margin is a two-way market's two
# implied probabilities added up: about 1.05, never below 1. The accuracy
# comparison (scripts/accuracy_comparison.py) and the /edges route share
# these functions.

#: The margin of last resort for a one-sided price: the league mean of the
#: closing two-way margin (the full-game total 1.048, the first-five total 1.059;
#: measured for the run-line pricing, SIM-549).
DEFAULT_ONE_SIDED_MARGIN = 1.05

#: A reference market's margin must fall in this band, or the next market is
#: tried. A stored "two-way" row outside the band is not a pair.
REFERENCE_MARGIN_BAND: tuple[float, float] = (1.0, 1.15)


def run_line_is_pair(home_spread: float | None, away_spread: float | None) -> bool:
    """True when the two spreads are the two sides of ONE bet: the away spread
    is the negative of the home spread (the pick'em 0 / 0 included). A missing
    spread is never a pair."""
    if home_spread is None or away_spread is None:
        return False
    return float(away_spread) == -float(home_spread)


def reference_margin_from_prices(
    candidates: Sequence[tuple[str, float | None, float | None]],
) -> tuple[float, str]:
    """``(margin, source)``: the first candidate ``(name, price_a, price_b)`` whose
    two implied probabilities add up to a margin inside
    :data:`REFERENCE_MARGIN_BAND`, else ``(DEFAULT_ONE_SIDED_MARGIN, "flat")``. A
    candidate with a missing or invalid price is skipped. The caller orders the
    candidates (the same segment's total first) and never passes a three-way
    market: its margin also prices the tie."""
    for name, a, b in candidates:
        if a is None or b is None:
            continue
        try:
            margin = implied_prob_from_american(float(a)) + implied_prob_from_american(float(b))
        except ValueError:
            continue
        if REFERENCE_MARGIN_BAND[0] <= margin <= REFERENCE_MARGIN_BAND[1]:
            return margin, name
    return DEFAULT_ONE_SIDED_MARGIN, "flat"


def devig_one_sided(american: float, margin: float) -> float:
    """The fair probability of a bet listed on its own: its implied probability
    over the reference margin, capped at 1. Raises ``ValueError`` on a margin
    below 1 (not a two-way margin) or an American price of 0."""
    if float(margin) < 1.0:
        raise ValueError(f"a two-way margin is at least 1.0, not {margin}")
    return min(implied_prob_from_american(float(american)) / float(margin), 1.0)


def run_line_bet_cover_prob(
    summary_or_margin: GameSimSummary | Sequence[float] | np.ndarray,
    side: MarketSide,
    own_line: float,
) -> float:
    """The simulator's cover probability of ONE run-line bet at its OWN spread.

    :func:`spread_cover_prob` takes the HOME line for both sides. An away bet at
    its own spread ``A`` covers when ``home − away < A``: the AWAY side at the
    mirrored home line ``−A``."""
    if side is MarketSide.HOME:
        return spread_cover_prob(summary_or_margin, float(own_line), MarketSide.HOME)
    if side is MarketSide.AWAY:
        return spread_cover_prob(summary_or_margin, -float(own_line), MarketSide.AWAY)
    raise ValueError("run line / spread side must be HOME or AWAY")


def one_sided_edge_report(
    *,
    label: str,
    side: MarketSide,
    line: float | None,
    sim_prob: float,
    offered_american: float,
    fair_prob: float,
) -> EdgeReport:
    """An :class:`EdgeReport` for a bet with no opposite side to de-vig against
    (a run line listed as two separate bets): ``fair_prob`` comes from
    :func:`devig_one_sided`. Like every report, a simulator probability of exactly
    0 or 1 raises ``ValueError`` (it has no fair price)."""
    p = float(sim_prob)
    return EdgeReport(
        label=label,
        side=side,
        line=None if line is None else float(line),
        sim_prob=p,
        market_fair_prob=float(fair_prob),
        edge=edge(p, float(fair_prob)),
        ev=expected_value(p, float(offered_american)),
        offered_american=float(offered_american),
        sim_fair_american=prob_to_american(p),
    )


# ===========================================================================
# The bet signals: the gate, the stake and the rank over a list of edge reports
# ===========================================================================

# The defaults, at module level so the API and the scripts read the same numbers.
DEFAULT_MIN_EDGE: float = 0.02  # the 2% edge floor
DEFAULT_MIN_EV: float = 0.0  # the EV must be above zero at the offered price
DEFAULT_KELLY_FRACTION: float = 0.25  # quarter Kelly
DEFAULT_MAX_STAKE_FRACTION: float = 0.05  # at most 5% of the bankroll


# ===========================================================================
# The fractional-Kelly stake
# ===========================================================================


def kelly_fraction_full(p_sim: float, offered_american: float) -> float:
    """The full-Kelly fraction ``f* = (b*p - q) / b`` of ``p_sim`` at ``offered_american``.

    ``b`` is the decimal odds of ``offered_american`` minus 1 (the profit per
    unit on a win), ``p = p_sim`` and ``q = 1 - p``. Equivalently ``f* = EV / b``.
    The result is negative for a negative-EV bet. :func:`stake_fraction` floors
    it at 0, because a signal backs only its report's own side.
    """
    p = float(p_sim)
    b = american_to_decimal(offered_american) - 1.0
    q = 1.0 - p
    return (b * p - q) / b


def stake_fraction(
    edge_report: EdgeReport,
    *,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    cap: float = DEFAULT_MAX_STAKE_FRACTION,
) -> float:
    """The recommended stake for ``edge_report``, as a fraction of the bankroll.

    The full-Kelly fraction of the report's own side (``p = edge_report.sim_prob``
    at ``edge_report.offered_american``) is floored at 0, scaled by
    ``kelly_fraction`` (quarter Kelly by default) and clamped to ``[0, cap]``:

        stake = clamp( kelly_fraction * max(0, (b*p - q) / b),  0,  cap )

    A report with an EV of zero or less stakes 0.0. A negative ``kelly_fraction``
    or ``cap`` raises ``ValueError``.
    """
    if kelly_fraction < 0.0:
        raise ValueError("kelly_fraction must be >= 0")
    if cap < 0.0:
        raise ValueError("cap (max_stake_fraction) must be >= 0")
    f_full = kelly_fraction_full(edge_report.sim_prob, edge_report.offered_american)
    raw = kelly_fraction * max(0.0, f_full)
    return min(cap, max(0.0, raw))


# ===========================================================================
# The config and the signal record
# ===========================================================================


@dataclass(frozen=True, slots=True)
class BetSignalConfig:
    """The gate thresholds and the stake settings of :func:`bet_signals_from_edges`.

    A report makes a signal when its edge is above zero and at least
    ``min_edge``, and its EV is above ``min_ev``. ``kelly_fraction`` scales the
    full-Kelly stake and ``max_stake_fraction`` caps it. A negative
    ``kelly_fraction`` or ``max_stake_fraction`` raises ``ValueError``.
    """

    #: The smallest edge (sim_prob - market_fair_prob) that passes: a noise floor.
    #: The edge must also be above zero, so a floor of 0.0 still needs edge > 0.
    min_edge: float = DEFAULT_MIN_EDGE
    #: The EV per unit staked at the offered price must be above this (strict
    #: ``>``). The default 0.0 passes only a positive EV.
    min_ev: float = DEFAULT_MIN_EV
    #: The fractional-Kelly multiplier (0.25 = quarter Kelly).
    kelly_fraction: float = DEFAULT_KELLY_FRACTION
    #: The cap on the recommended stake fraction (0.05 = 5% of the bankroll).
    max_stake_fraction: float = DEFAULT_MAX_STAKE_FRACTION

    def __post_init__(self) -> None:
        if self.kelly_fraction < 0.0:
            raise ValueError("kelly_fraction must be >= 0")
        if self.max_stake_fraction < 0.0:
            raise ValueError("max_stake_fraction must be >= 0")


#: The shared default config: quarter Kelly, a 2% edge floor, an EV above zero,
#: a 5% cap.
DEFAULT_CONFIG = BetSignalConfig()


@dataclass(frozen=True, slots=True)
class BetSignal:
    """One bet signal: a side that passes the gate, with its stake and its rank.

    It copies the source report's label, side, line, offered price, edge and EV.
    It adds the fractional-Kelly ``stake_fraction``, a ``confidence`` and a
    0-based ``rank`` (0 = the highest EV), and it keeps the full source
    ``report``. A signal is advice: the caller decides when to bet (see the
    module docstring).
    """

    #: The source report's market label (e.g. "moneyline", "total", "prop:K", "run_line").
    label: str
    #: The side of the market this signal backs.
    side: MarketSide
    #: The market line (None for a moneyline; a float for a total, a prop or a run line).
    line: float | None
    #: The American price of the backed side (the report's offered price).
    offered_american: float

    #: edge = sim_prob - market_fair_prob (above 0 on every signal).
    edge: float
    #: The EV per unit staked at the offered price (above min_ev on every signal).
    ev: float

    #: The recommended stake as a fraction of the bankroll (fractional Kelly, capped).
    stake_fraction: float

    #: A copy of the report's edge, in (0, 1] on every signal. The rank orders by
    #: EV first, so a larger confidence does not always mean a better rank.
    confidence: float
    #: The 0-based rank within the returned list (0 = the highest EV).
    rank: int

    #: The full source EdgeReport (sim_prob, market_fair_prob, sim_fair_american).
    #: The repr leaves it out; equality compares it by value.
    report: EdgeReport = field(repr=False, compare=True)


# ===========================================================================
# The gate, the stake and the rank: EdgeReports -> BetSignals
# ===========================================================================


def _passes_gate(report: EdgeReport, config: BetSignalConfig) -> bool:
    """True when ``report`` passes both gates: the edge and the EV.

    The edge must be above zero and at least ``config.min_edge``. The EV must be
    above ``config.min_ev``, so a report at exactly ``min_ev`` fails.
    """
    if not report.positive_edge:
        return False
    if report.edge < config.min_edge:
        return False
    return not report.ev <= config.min_ev


def bet_signals_from_edges(
    reports: Sequence[EdgeReport],
    *,
    config: BetSignalConfig = DEFAULT_CONFIG,
) -> list[BetSignal]:
    """Gate, stake and rank ``reports`` into a list of :class:`BetSignal`.

    1. **Gate.** Keep the reports that pass :func:`_passes_gate`. A report that
       fails makes no signal and no log line.
    2. **Stake.** Give each one a :func:`stake_fraction` from
       ``config.kelly_fraction`` and ``config.max_stake_fraction``.
    3. **Rank.** Sort by EV, then edge, then stake, each highest first, and
       number the ranks from 0.

    Returns ``[]`` when there are no reports or none passes. The caller decides
    when to bet (see the module docstring).
    """
    survivors = [r for r in reports if _passes_gate(r, config)]
    # Sort by EV, then edge, then stake, each descending, so the order is fixed.
    sized: list[tuple[EdgeReport, float]] = [
        (
            r,
            stake_fraction(
                r,
                kelly_fraction=config.kelly_fraction,
                cap=config.max_stake_fraction,
            ),
        )
        for r in survivors
    ]
    sized.sort(key=lambda rs: (rs[0].ev, rs[0].edge, rs[1]), reverse=True)

    signals: list[BetSignal] = []
    for rank, (report, stake) in enumerate(sized):
        signals.append(
            BetSignal(
                label=report.label,
                side=report.side,
                line=report.line,
                offered_american=report.offered_american,
                edge=report.edge,
                ev=report.ev,
                stake_fraction=stake,
                confidence=report.edge,
                rank=rank,
                report=report,
            )
        )
    return signals


__all__ = [
    # conversions
    "american_to_decimal",
    "decimal_to_american",
    "implied_prob_from_american",
    "american_to_implied_prob",
    "prob_to_american",
    "fair_american_from_prob",
    # de-vig
    "devig_two_way",
    "devig_multiway",
    # edge / EV
    "edge",
    "expected_value",
    # reports
    "MarketSide",
    "OddsQuote",
    "TwoWayMarket",
    "EdgeReport",
    "moneyline_edge_report",
    "prop_edge_report",
    "total_over_under_edge_report",
    # the segment and team markets, and the run line
    "samples_over_under_edge_report",
    "three_way_edge_report",
    "spread_cover_prob",
    "run_line_edge_report",
    # a run line as a pair or as two separate bets
    "DEFAULT_ONE_SIDED_MARGIN",
    "REFERENCE_MARGIN_BAND",
    "run_line_is_pair",
    "reference_margin_from_prices",
    "devig_one_sided",
    "run_line_bet_cover_prob",
    "one_sided_edge_report",
    # the bet signals
    "DEFAULT_MIN_EDGE",
    "DEFAULT_MIN_EV",
    "DEFAULT_KELLY_FRACTION",
    "DEFAULT_MAX_STAKE_FRACTION",
    "DEFAULT_CONFIG",
    "BetSignalConfig",
    "BetSignal",
    "kelly_fraction_full",
    "stake_fraction",
    "bet_signals_from_edges",
]
