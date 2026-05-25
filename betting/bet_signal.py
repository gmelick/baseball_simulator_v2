"""
bet_signal.py
=============
SIM-369 -- the **bet-signal / +EV recommendation module** (Phase 5, Sprint 5).
The layer that sits on top of the SIM-339 CLV / edge engine and turns a list of
scored markets (``EdgeReport`` objects, one per moneyline / total / prop /
run-line side) into a ranked list of FIREABLE +EV bet recommendations, each with a
fractional-Kelly stake.

WHAT THIS IS
------------
The CLV engine (:mod:`betting.clv_engine`) answers "what is the edge / EV of this
one side at this price?".  It does NOT decide whether to bet, how much to bet, or
which of many candidate bets to prefer.  This module does exactly that:

  * **GATE** -- keep only the reports that clear the action thresholds: a strictly
    POSITIVE edge (the sim beats the de-vigged market) AND an EV above ``min_ev``
    (default 0.0, i.e. strictly +EV at the offered price) AND an edge above
    ``min_edge`` (a noise floor; a 0.1% edge is not worth the variance / vig risk).
  * **SIZE** -- attach a recommended ``stake_fraction`` (fraction of bankroll) via
    FRACTIONAL KELLY (see below).  Quarter-Kelly by default, hard-capped, so a
    single mis-estimated probability cannot blow up the bankroll.
  * **RANK** -- return the survivors sorted by EV descending (ties broken by edge,
    then stake), so the caller sees the strongest +EV opportunities first.

THE KELLY STAKE (documented)
----------------------------
The Kelly criterion maximises the long-run growth rate of a bankroll.  For a
single binary bet at net odds ``b`` (decimal odds minus 1, the profit per unit
staked on a win) with win probability ``p`` and loss probability ``q = 1 - p``:

    full-Kelly fraction  f* = (b * p - q) / b

Note ``b * p - q`` is exactly the per-unit-stake EV (the same quantity SIM-339's
:func:`expected_value` returns), so ``f* = EV / b``: full Kelly stakes the EV
divided by the net odds.  We never bet full Kelly -- it is too aggressive given
model uncertainty and over-bets when ``p`` is over-estimated -- so we apply a
FRACTIONAL-KELLY multiplier (``kelly_fraction``, default 0.25 == quarter Kelly):

    stake_fraction = clamp( kelly_fraction * max(0, (b*p - q) / b),  0,  cap )

``max(0, .)`` makes a -EV bet stake zero (full Kelly would say "bet the other
side"; we only ever back the report's own side, so a non-positive Kelly fraction
means no bet).  The ``cap`` (``max_stake_fraction``, default 0.05 == 5% of
bankroll) bounds tail risk regardless of how large the raw Kelly number is.

GATING + TIMING GUIDANCE (advisory)
-----------------------------------
A :class:`BetSignal` is ADVISORY, not an auto-execution order.  It says "at the
price captured in the source ``EdgeReport`` this side is +EV; here is the
quarter-Kelly size".  The CALLER decides WHEN to fire relative to line movement.

Per the Betting Analyst's market-timing note: the +EV edge is computed against the
ENTRY / offered price snapshot, so a signal is only good while the market is at (or
better than) that price.  Beating the close (positive CLV -- see SIM-339) is the
validation metric; fire EARLY on a soft line you expect to firm toward your number,
and re-score (rebuild the ``EdgeReport``) if the market moves against you before you
transact -- a signal computed at a stale price may no longer be +EV.  This module
deliberately does NOT bake in a timing rule; it surfaces the *current* +EV set and
leaves execution timing to the caller / live pipeline.

DETERMINISM / PURITY
--------------------
Pure Python / numpy arithmetic over injected ``EdgeReport`` objects.  No DB, no
api, no RNG, no live odds fetch.  The same reports + config always yield the same
ranked signal list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from betting.clv_engine import EdgeReport, MarketSide, american_to_decimal

# Defaults (module-level so the API agent / callers can reference the same numbers).
DEFAULT_MIN_EDGE: float = 0.02            # 2% edge noise floor.
DEFAULT_MIN_EV: float = 0.0               # strictly +EV at the offered price.
DEFAULT_KELLY_FRACTION: float = 0.25      # quarter Kelly.
DEFAULT_MAX_STAKE_FRACTION: float = 0.05  # cap at 5% of bankroll.


# ===========================================================================
# Fractional-Kelly stake sizing
# ===========================================================================

def kelly_fraction_full(p_sim: float, offered_american: float) -> float:
    """FULL-Kelly fraction f* = (b*p - q) / b for win prob ``p_sim`` at ``offered_american``.

    ``b = american_to_decimal(offered_american) - 1`` is the net profit per unit on
    a win, ``p = p_sim``, ``q = 1 - p``.  Equivalently ``f* = EV / b``.  May be
    NEGATIVE (a -EV bet under full Kelly says "lay the other side"); callers that
    only back the report's own side should floor it at 0 (see :func:`stake_fraction`).
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
    """Recommended stake as a FRACTION OF BANKROLL for ``edge_report`` (fractional Kelly).

    Computes the full-Kelly fraction ``f* = (b*p - q) / b`` for the report's own
    side -- ``p = edge_report.sim_prob``, ``q = 1 - p``,
    ``b = american_to_decimal(edge_report.offered_american) - 1`` -- floors it at 0
    (we never back the other side from this report), scales by ``kelly_fraction``
    (quarter Kelly by default), and clamps the result to ``[0, cap]``:

        stake = clamp( kelly_fraction * max(0, (b*p - q) / b),  0,  cap )

    Returns 0.0 for a non-positive-Kelly (i.e. non-+EV) report.  ``kelly_fraction``
    and ``cap`` must be non-negative.  Pure arithmetic; no side effects.
    """
    if kelly_fraction < 0.0:
        raise ValueError("kelly_fraction must be >= 0")
    if cap < 0.0:
        raise ValueError("cap (max_stake_fraction) must be >= 0")
    f_full = kelly_fraction_full(edge_report.sim_prob, edge_report.offered_american)
    raw = kelly_fraction * max(0.0, f_full)
    return min(cap, max(0.0, raw))


# ===========================================================================
# Bet-signal config + record
# ===========================================================================

@dataclass(frozen=True, slots=True)
class BetSignalConfig:
    """Thresholds + sizing knobs for turning ``EdgeReport``s into bet signals (SIM-369).

    A report must clear BOTH gates to produce a signal: ``edge >= min_edge`` (and a
    strictly positive edge) AND ``ev > min_ev``.  Sizing uses fractional Kelly:
    ``kelly_fraction`` scales the full-Kelly stake and ``max_stake_fraction`` caps it.
    """

    #: Minimum edge (sim_prob - market_fair_prob) to act on; a noise floor.  The
    #: edge must ALSO be strictly positive (a 0.0 floor still requires edge > 0).
    min_edge: float = DEFAULT_MIN_EDGE
    #: Minimum EV per unit stake (strict ``>``) at the offered price.  Default 0.0
    #: == only strictly +EV bets.
    min_ev: float = DEFAULT_MIN_EV
    #: Fractional-Kelly multiplier (0.25 == quarter Kelly).
    kelly_fraction: float = DEFAULT_KELLY_FRACTION
    #: Hard cap on the recommended stake fraction (0.05 == 5% of bankroll).
    max_stake_fraction: float = DEFAULT_MAX_STAKE_FRACTION

    def __post_init__(self) -> None:
        if self.kelly_fraction < 0.0:
            raise ValueError("kelly_fraction must be >= 0")
        if self.max_stake_fraction < 0.0:
            raise ValueError("max_stake_fraction must be >= 0")


#: A sensible shared default (quarter Kelly, 2% edge floor, strictly +EV, 5% cap).
DEFAULT_CONFIG = BetSignalConfig()


@dataclass(frozen=True, slots=True)
class BetSignal:
    """A FIREABLE +EV bet recommendation derived from one :class:`EdgeReport` (SIM-369).

    Self-describing: it carries the source report's key identity fields (label / side
    / line / offered price) plus its ``edge`` and ``ev``, the recommended fractional
    Kelly ``stake_fraction`` (fraction of bankroll), a ``confidence`` score and a
    ``rank`` within the returned list (0 == strongest), and a back-reference to the
    full source ``report``.  ADVISORY -- the caller decides when to fire it relative
    to line movement (see the module docstring's timing guidance).
    """

    #: Source report's market label (e.g. "moneyline", "total", "prop:K", "run_line").
    label: str
    #: Which side of the two-way market this signal backs.
    side: MarketSide
    #: The market line (None for a moneyline; a float for total / prop / run line).
    line: "float | None"
    #: American price of the side we would transact at (the report's offered price).
    offered_american: float

    #: edge = sim_prob - market_fair_prob (strictly > 0 for any emitted signal).
    edge: float
    #: EV per unit stake at the offered price (> min_ev for any emitted signal).
    ev: float

    #: Recommended stake as a FRACTION OF BANKROLL (fractional Kelly, capped).
    stake_fraction: float

    #: Confidence in [0, 1]: the report's edge, which is also the rank key proxy.
    #: Larger == the sim is more bullish over the de-vigged market.  Advisory only.
    confidence: float
    #: 0-based rank within the returned list (0 == strongest by EV).
    rank: int

    #: Back-reference to the full source EdgeReport (carries sim_prob, market fair
    #: prob, sim_fair_american, CLV, etc.).  Compared by identity-free value.
    report: EdgeReport = field(repr=False, compare=True)


# ===========================================================================
# The gate + rank: EdgeReports -> BetSignals
# ===========================================================================

def _passes_gate(report: EdgeReport, config: BetSignalConfig) -> bool:
    """True iff ``report`` clears BOTH action gates (positive edge AND +EV).

    Requires a STRICTLY positive edge that also meets ``config.min_edge`` and an EV
    STRICTLY above ``config.min_ev``.  A report at exactly the threshold EV (e.g.
    ev == min_ev == 0.0) is excluded -- we only fire on strictly +EV opportunities.
    """
    if not report.positive_edge:
        return False
    if report.edge < config.min_edge:
        return False
    if report.ev <= config.min_ev:
        return False
    return True


def bet_signals_from_edges(
    reports: "Sequence[EdgeReport]",
    *,
    config: BetSignalConfig = DEFAULT_CONFIG,
) -> "list[BetSignal]":
    """Filter, size, and RANK ``reports`` into a list of fireable +EV :class:`BetSignal`s.

    Pipeline (SIM-369):

      1. **GATE** -- keep only reports that clear :func:`_passes_gate`: a strictly
         positive edge >= ``config.min_edge`` AND ev > ``config.min_ev`` (a report
         failing either gate yields NO signal and is dropped silently).
      2. **SIZE** -- attach a fractional-Kelly :func:`stake_fraction` using
         ``config.kelly_fraction`` and ``config.max_stake_fraction``.
      3. **RANK** -- sort the survivors by EV descending (ties broken by edge then
         stake, both descending) and stamp a 0-based ``rank`` (0 == strongest).

    Returns ``[]`` for empty input or when no report clears the gates.  ADVISORY:
    the signals say WHAT is +EV at the captured price and HOW MUCH (quarter Kelly);
    the caller decides WHEN to fire relative to line movement (see module docstring).
    """
    survivors = [r for r in reports if _passes_gate(r, config)]
    # Rank by EV desc, then edge desc, then stake desc for a fully deterministic order.
    sized: "list[tuple[EdgeReport, float]]" = [
        (r, stake_fraction(
            r,
            kelly_fraction=config.kelly_fraction,
            cap=config.max_stake_fraction,
        ))
        for r in survivors
    ]
    sized.sort(key=lambda rs: (rs[0].ev, rs[0].edge, rs[1]), reverse=True)

    signals: "list[BetSignal]" = []
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
