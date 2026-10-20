"""Bet signals: the gate, the stake and the rank over a list of edge reports.

:mod:`betting.clv_engine` prices one side of one market at one price. Its
``EdgeReport`` holds the edge and the expected value (EV) per unit staked at
the offered price. The edge is the simulator's probability minus the no-vig
probability: the market's probability with the vig, the book's margin,
removed. The engine does not decide whether to bet, how much, or which bet
comes first. This module does that, in three steps:

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

The Kelly stake
---------------
The Kelly criterion is the stake that maximises the long-run growth of a
bankroll. Take one bet at net odds ``b`` (the decimal odds minus 1: the profit
per unit staked on a win), a win probability ``p`` and ``q = 1 - p``:

    full-Kelly fraction  f* = (b * p - q) / b

``b * p - q`` is the EV per unit staked
(:func:`~betting.clv_engine.expected_value`), so ``f* = EV / b``. Full Kelly
over-bets when ``p`` is too high. So the stake is a fraction of it
(``kelly_fraction``, default 0.25, "quarter Kelly"):

    stake_fraction = clamp( kelly_fraction * max(0, (b*p - q) / b),  0,  cap )

``max(0, .)`` gives a negative-EV side a zero stake: a signal backs only its
report's own side. The ``cap`` (``max_stake_fraction``, default 5% of the
bankroll) bounds the stake however large the raw Kelly number is.

Timing
------
A :class:`BetSignal` is advice, not an order. Its edge and EV hold at the price
that the source ``EdgeReport`` captured, and at a better price. When the price
moves, the caller rebuilds the report. This module has no timing rule.

The module is pure arithmetic over the reports the caller passes in: no
database, no random numbers, no odds fetch. The same reports and config always
give the same list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from betting.clv_engine import EdgeReport, MarketSide, american_to_decimal

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

    #: The full source EdgeReport (sim_prob, market_fair_prob, sim_fair_american,
    #: the CLV). The repr leaves it out; equality compares it by value.
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
