"""The betting layer: compare the simulator's probabilities with the market's prices.

The module has two stages.

1. **Pricing.** Each side of a market gets an :class:`EdgeReport`. The report
   holds the simulator's probability, the market's no-vig probability, the
   edge (the first minus the second) and the expected value (EV) at the
   offered price.
2. **Signals.** :func:`bet_signals_from_edges` keeps the reports that pass the
   gate, gives each a stake, and ranks them by EV.

Terms:

* **American odds.** ``-110`` means stake 110 to win 100. ``+150`` means stake
  100 to win 150.
* **Decimal odds.** The total return per unit staked. ``+150`` is 2.50.
* **The vig.** The book's margin. It makes a market's implied probabilities add
  up to more than 1.
* **The de-vig.** Scale the implied probabilities so they add up to exactly 1.
  Each side gives up the margin in proportion to its own probability.

A signal says the simulator prices a side above the market. It is not proof
that the bet makes money. The module is pure arithmetic: no database, no
random numbers and no odds fetch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from simulation.prop_distributions import PropDistribution
from simulation.results import GameSimSummary
from simulation.win_probability import WinProbability

# ===========================================================================
# 1. The conversions
# ===========================================================================


def american_to_decimal(american: float) -> float:
    """Convert American odds to decimal odds: ``+150`` -> 2.50, ``-110`` -> 1.909.

    A price of 0 raises ``ValueError``.
    """
    a = float(american)
    if a == 0:
        raise ValueError("American odds of 0 are undefined")
    if a > 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / (-a)


def implied_prob_from_american(american: float) -> float:
    """The implied probability of one price, with the vig still in it.

    ``-110`` -> 0.5238 and ``+150`` -> 0.40. A price of 0 raises ``ValueError``.
    """
    a = float(american)
    if a == 0:
        raise ValueError("American odds of 0 are undefined")
    if a > 0:
        return 100.0 / (a + 100.0)
    return -a / (-a + 100.0)


def prob_to_american(prob: float) -> float:
    """The fair American price of a probability: the price with no vig.

    ``0.40`` -> ``+150`` and ``0.5238`` -> ``-110``; 0.5 gives ``+100``. A
    probability of 0 or 1 or beyond raises ``ValueError``.
    """
    p = float(prob)
    if not (0.0 < p < 1.0):
        raise ValueError("probability must be strictly between 0 and 1")
    if p <= 0.5:
        return (1.0 - p) / p * 100.0
    return -p / (1.0 - p) * 100.0


# ===========================================================================
# 2. The de-vig
# ===========================================================================


def devig(american_prices: Sequence[float]) -> list[float]:
    """The no-vig probabilities of a market's sides, in the order given.

    Each price becomes its implied probability ``q_i``. Each result is
    ``q_i / sum(q)``, so the results add up to 1. Pass two prices for a two-way
    market and three for a market with a tie. Fewer than two prices, or a price
    of 0, raises ``ValueError``.
    """
    if len(american_prices) < 2:
        raise ValueError("a market needs at least 2 prices to de-vig")
    q = np.array([implied_prob_from_american(a) for a in american_prices], dtype=np.float64)
    total = float(q.sum())
    return [float(x / total) for x in q]


def devig_one_sided(american: float, margin: float) -> float:
    """The no-vig probability of a bet with no opposite side.

    The price's implied probability is divided by ``margin``, the book's margin
    on another two-way market of the same game (see
    :func:`reference_margin_from_prices`). The result is capped at 1. A margin
    below 1, or a price of 0, raises ``ValueError``.
    """
    if float(margin) < 1.0:
        raise ValueError(f"a two-way margin is at least 1.0, not {margin}")
    return min(implied_prob_from_american(float(american)) / float(margin), 1.0)


# ===========================================================================
# 3. The expected value
# ===========================================================================


def expected_value(p_sim: float, offered_american: float) -> float:
    """The EV per unit staked at the offered price, if ``p_sim`` is the truth.

    ``EV = p * b - (1 - p)``, where ``b`` is the profit per unit on a win. The
    EV reads the offered price, vig included, because a bet takes that price.
    """
    p = float(p_sim)
    b = american_to_decimal(offered_american) - 1.0
    return p * b - (1.0 - p)


# ===========================================================================
# 4. The records
# ===========================================================================


class MarketSide(Enum):
    """The side of a market. ``DRAW`` is the tie of a three-way market."""

    HOME = "home"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"
    DRAW = "draw"


@dataclass(frozen=True, slots=True)
class OddsQuote:
    """The prices of a two-way market, from the caller's point of view.

    ``side`` is the price of the side being priced, ``other`` the opposite
    side's price, and ``line`` the market line (None on a moneyline).
    """

    side: float
    other: float
    line: float | None = None


@dataclass(frozen=True, slots=True)
class EdgeReport:
    """The edge and the EV of one side of one market."""

    label: str
    side: MarketSide
    line: float | None
    #: The simulator's probability of the side.
    sim_prob: float
    #: The market's no-vig probability of the side.
    market_fair_prob: float
    #: sim_prob - market_fair_prob. Positive: the simulator rates the side higher.
    edge: float
    #: The EV per unit staked at the offered price.
    ev: float
    #: The price a bet takes.
    offered_american: float
    #: The simulator's own fair price (no vig), shown next to the offered price.
    sim_fair_american: float

    @property
    def positive_edge(self) -> bool:
        """True when the edge is above zero."""
        return self.edge > 0.0


def edge_report(
    *,
    label: str,
    side: MarketSide,
    line: float | None,
    sim_prob: float,
    fair_prob: float,
    offered_american: float,
) -> EdgeReport:
    """Build a report from the simulator's probability and the market's no-vig one.

    Every builder below ends here. A ``sim_prob`` of exactly 0 or 1 raises
    ``ValueError``, because it has no fair price.
    """
    p = float(sim_prob)
    fair = float(fair_prob)
    offered = float(offered_american)
    return EdgeReport(
        label=label,
        side=side,
        line=None if line is None else float(line),
        sim_prob=p,
        market_fair_prob=fair,
        edge=p - fair,
        ev=expected_value(p, offered),
        offered_american=offered,
        sim_fair_american=prob_to_american(p),
    )


def _two_way(
    *, label: str, side: MarketSide, line: float | None, sim_prob: float, quote: OddsQuote
) -> EdgeReport:
    """Build a report for a two-way market: de-vig the quote, then call :func:`edge_report`."""
    fair, _ = devig([quote.side, quote.other])
    return edge_report(
        label=label,
        side=side,
        line=line,
        sim_prob=sim_prob,
        fair_prob=fair,
        offered_american=quote.side,
    )


# ===========================================================================
# 5. The report builders: one per kind of simulator output
# ===========================================================================


def moneyline_edge_report(
    win_prob: WinProbability,
    quote: OddsQuote,
    *,
    side: MarketSide = MarketSide.HOME,
) -> EdgeReport:
    """The report of a moneyline side, from the simulator's win probability.

    ``side`` must be HOME or AWAY.
    """
    if side is MarketSide.HOME:
        sim_p = win_prob.home_win_prob
    elif side is MarketSide.AWAY:
        sim_p = win_prob.away_win_prob
    else:
        raise ValueError("moneyline side must be HOME or AWAY")
    return _two_way(label="moneyline", side=side, line=None, sim_prob=sim_p, quote=quote)


def prop_edge_report(
    dist: PropDistribution,
    quote: OddsQuote,
    *,
    side: MarketSide = MarketSide.OVER,
) -> EdgeReport:
    """The report of a player prop side, from the player's prop distribution.

    The line is ``quote.line``. On a whole-number line, a value on the line is
    a push and counts for neither side. ``side`` must be OVER or UNDER.
    """
    line = quote.line
    if line is None:
        raise ValueError("a prop quote must carry a line")
    if side is MarketSide.OVER:
        sim_p = dist.p_over(line)
    elif side is MarketSide.UNDER:
        sim_p = dist.p_under(line)
    else:
        raise ValueError("prop side must be OVER or UNDER")
    return _two_way(label=f"prop:{dist.prop}", side=side, line=line, sim_prob=sim_p, quote=quote)


def samples_over_under_edge_report(
    samples: Sequence[float] | np.ndarray,
    quote: OddsQuote,
    *,
    side: MarketSide = MarketSide.OVER,
    label: str,
) -> EdgeReport:
    """The report of an over / under side, from one sample per simulated game.

    The samples are what the market settles on: the game total
    (``summary.total_scores``, label ``"total"``), the first-five runs, one
    team's runs. The over is ``P(sample > line)`` and the under
    ``P(sample < line)``, so a sample on the line is a push. ``side`` must be
    OVER or UNDER.
    """
    line = quote.line
    if line is None:
        raise ValueError("an over / under quote must carry a line")
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
    return _two_way(label=label, side=side, line=line, sim_prob=sim_p, quote=quote)


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
    """The report of one side of a market with a tie (the first-inning and first-five moneylines).

    The de-vig spreads the margin over all three prices. ``side`` must be HOME,
    AWAY or DRAW.
    """
    order = (MarketSide.HOME, MarketSide.AWAY, MarketSide.DRAW)
    if side not in order:
        raise ValueError("a three-way side must be HOME, AWAY or DRAW")
    i = order.index(side)
    prices = (float(home_ml), float(away_ml), float(draw_ml))
    return edge_report(
        label=label,
        side=side,
        line=None,
        sim_prob=(p_home, p_away, p_draw)[i],
        fair_prob=devig(prices)[i],
        offered_american=prices[i],
    )


# ===========================================================================
# 6. The run line
# ===========================================================================
#
# The book lists a run line in one of two ways:
#
# * A PAIR: one bet with two sides (home -1.5 / away +1.5). Exactly one side
#   wins, so the two prices de-vig against each other (run_line_edge_report).
# * TWO SEPARATE BETS: the spreads share a sign or differ (home -1.5 / away
#   -1.5). Both can lose, or both win, so de-vigging one against the other is
#   wrong. Each bet is priced from its own price over the margin of another
#   market of the same game (devig_one_sided).
#
# The /edges route and the accuracy comparison share these functions.

#: The margin used when no reference market qualifies: the league mean of the
#: closing two-way margin (SIM-549).
DEFAULT_ONE_SIDED_MARGIN = 1.05

#: A reference market's margin must fall in this band to be used.
REFERENCE_MARGIN_BAND: tuple[float, float] = (1.0, 1.15)


def _as_margin_array(
    summary_or_margin: GameSimSummary | Sequence[float] | np.ndarray,
) -> np.ndarray:
    """The per-game margin ``home - away`` as a float array."""
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
    """The simulator's cover probability of a run-line side. ``line`` is the HOME line.

    With ``margin = home - away`` and home line ``L``:

    * HOME covers when ``margin > -L`` (at ``L = -1.5``, a home win by 2 or more).
    * AWAY covers when ``margin < -L``.
    * ``margin == -L`` covers neither: the push, possible only on a
      whole-number line.

    ``side`` must be HOME or AWAY.
    """
    margin = _as_margin_array(summary_or_margin)
    n = margin.size
    if n == 0:
        raise ValueError("no score margins to price a run line")
    threshold = -float(line)
    if side is MarketSide.HOME:
        return float(np.count_nonzero(margin > threshold)) / n
    if side is MarketSide.AWAY:
        return float(np.count_nonzero(margin < threshold)) / n
    raise ValueError("run line side must be HOME or AWAY")


def run_line_bet_cover_prob(
    summary_or_margin: GameSimSummary | Sequence[float] | np.ndarray,
    side: MarketSide,
    own_line: float,
) -> float:
    """The cover probability of one run-line bet at its OWN spread.

    :func:`spread_cover_prob` takes the home line for both sides. An away bet
    at its own spread ``A`` is the AWAY side at home line ``-A``. Keep the two
    functions apart: mixing up the two line conventions is an easy mistake.
    """
    if side is MarketSide.HOME:
        return spread_cover_prob(summary_or_margin, float(own_line), MarketSide.HOME)
    if side is MarketSide.AWAY:
        return spread_cover_prob(summary_or_margin, -float(own_line), MarketSide.AWAY)
    raise ValueError("run line side must be HOME or AWAY")


def run_line_edge_report(
    summary_or_margin: GameSimSummary | Sequence[float] | np.ndarray,
    quote: OddsQuote,
    *,
    side: MarketSide = MarketSide.HOME,
    line: float = -1.5,
) -> EdgeReport:
    """The report of one side of a run-line PAIR.

    ``line`` is the HOME line for both sides; ``quote.line`` overrides it when
    set. ``side`` must be HOME or AWAY.
    """
    home_line = quote.line if quote.line is not None else float(line)
    sim_p = spread_cover_prob(summary_or_margin, home_line, side)
    return _two_way(label="run_line", side=side, line=home_line, sim_prob=sim_p, quote=quote)


def run_line_is_pair(home_spread: float | None, away_spread: float | None) -> bool:
    """True when the two spreads are the two sides of one bet (away = -home)."""
    if home_spread is None or away_spread is None:
        return False
    return float(away_spread) == -float(home_spread)


def reference_margin_from_prices(
    candidates: Sequence[tuple[str, float | None, float | None]],
) -> tuple[float, str]:
    """The margin and the name of the first usable reference market.

    Each candidate is ``(name, price_a, price_b)``. The first one whose two
    implied probabilities add up to a margin inside
    :data:`REFERENCE_MARGIN_BAND` wins. A candidate with a missing or invalid
    price is skipped. With none usable, the result is
    ``(DEFAULT_ONE_SIDED_MARGIN, "flat")``. The caller orders the candidates
    and never passes a three-way market.
    """
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


# ===========================================================================
# 7. The signals: the gate, the stake and the rank
# ===========================================================================

#: The smallest edge that passes the gate. The accuracy scripts read it too.
DEFAULT_MIN_EDGE: float = 0.02


@dataclass(frozen=True, slots=True)
class BetSignalConfig:
    """The gate and the stake settings.

    A report passes when its edge is above zero and at least ``min_edge``, and
    its EV is above ``min_ev``. The stake is ``kelly_fraction`` of the full
    Kelly stake, capped at ``max_stake_fraction`` of the bankroll.
    """

    min_edge: float = DEFAULT_MIN_EDGE
    min_ev: float = 0.0
    #: 0.25 is "quarter Kelly".
    kelly_fraction: float = 0.25
    #: 0.05 is 5% of the bankroll.
    max_stake_fraction: float = 0.05

    def __post_init__(self) -> None:
        if self.kelly_fraction < 0.0:
            raise ValueError("kelly_fraction must be >= 0")
        if self.max_stake_fraction < 0.0:
            raise ValueError("max_stake_fraction must be >= 0")


DEFAULT_CONFIG = BetSignalConfig()


def stake_fraction(report: EdgeReport, config: BetSignalConfig = DEFAULT_CONFIG) -> float:
    """The recommended stake for a report, as a fraction of the bankroll.

    The Kelly criterion gives the stake that grows a bankroll fastest:
    ``f* = EV / b``, where ``b`` is the profit per unit on a win. Full Kelly
    bets too much when the probability is overestimated, so the stake is a fraction
    of it::

        stake = min(cap, kelly_fraction * max(0, f*))

    A report with an EV of zero or less stakes 0.
    """
    b = american_to_decimal(report.offered_american) - 1.0
    full_kelly = expected_value(report.sim_prob, report.offered_american) / b
    return min(config.max_stake_fraction, config.kelly_fraction * max(0.0, full_kelly))


@dataclass(frozen=True, slots=True)
class BetSignal:
    """One side that passed the gate, with its stake and its rank.

    The first six fields copy the source report. ``rank`` 0 is the highest EV.
    A signal is advice: the caller decides when to bet.
    """

    label: str
    side: MarketSide
    line: float | None
    offered_american: float
    edge: float
    ev: float
    stake_fraction: float
    rank: int
    #: The source report. The repr leaves it out.
    report: EdgeReport = field(repr=False, compare=True)


def _passes_gate(report: EdgeReport, config: BetSignalConfig) -> bool:
    """True when the edge is above zero and at least ``min_edge``, and the EV is above ``min_ev``."""
    # ``not ev <= min_ev`` (not ``ev > min_ev``) keeps a NaN EV passing, as before.
    return (
        report.positive_edge and report.edge >= config.min_edge and not report.ev <= config.min_ev
    )


def bet_signals_from_edges(
    reports: Sequence[EdgeReport],
    *,
    config: BetSignalConfig = DEFAULT_CONFIG,
) -> list[BetSignal]:
    """Gate, stake and rank the reports.

    The signals are sorted by EV, then edge, then stake, each highest first.
    No report passing gives ``[]``.
    """
    sized = [(r, stake_fraction(r, config)) for r in reports if _passes_gate(r, config)]
    sized.sort(key=lambda rs: (rs[0].ev, rs[0].edge, rs[1]), reverse=True)
    return [
        BetSignal(
            label=r.label,
            side=r.side,
            line=r.line,
            offered_american=r.offered_american,
            edge=r.edge,
            ev=r.ev,
            stake_fraction=stake,
            rank=rank,
            report=r,
        )
        for rank, (r, stake) in enumerate(sized)
    ]


__all__ = [
    # 1. conversions
    "american_to_decimal",
    "implied_prob_from_american",
    "prob_to_american",
    # 2. de-vig
    "devig",
    "devig_one_sided",
    # 3. expected value
    "expected_value",
    # 4. records
    "MarketSide",
    "OddsQuote",
    "EdgeReport",
    "edge_report",
    # 5. report builders
    "moneyline_edge_report",
    "prop_edge_report",
    "samples_over_under_edge_report",
    "three_way_edge_report",
    # 6. run line
    "DEFAULT_ONE_SIDED_MARGIN",
    "REFERENCE_MARGIN_BAND",
    "spread_cover_prob",
    "run_line_bet_cover_prob",
    "run_line_edge_report",
    "run_line_is_pair",
    "reference_margin_from_prices",
    # 7. signals
    "DEFAULT_MIN_EDGE",
    "BetSignalConfig",
    "DEFAULT_CONFIG",
    "stake_fraction",
    "BetSignal",
    "bet_signals_from_edges",
]
