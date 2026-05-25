"""
clv_engine.py
=============
SIM-339 -- the **CLV / edge engine** (Phase 4, Sprint 4).  The gold-standard
betting layer: it turns simulation outputs plus market odds into actionable edge.

WHAT THIS IS
------------
The translation layer between the simulator and the betting market.  A simulation
run gives us *probabilities*: SIM-330 a calibrated home/away win probability, SIM-329
a full prop PMF (``p_over`` / ``p_under`` / ``p_at_least`` at any line), SIM-327 raw
per-iteration score arrays (totals / spreads).  The market gives us *prices*:
American odds (e.g. ``-110``, ``+150``) carrying a bookmaker margin (the "vig").
This module makes the two comparable and produces, per market and per prop:

  * **implied probability** from American odds (and its inverse, prob -> fair odds);
  * **de-vig / no-vig fair odds** -- the margin removed from a two-way OR multi-way
    market so the prices sum to a true 1.0 probability;
  * **edge** = our simulation probability minus the market's no-vig (fair) probability;
  * **expected value (EV)** per unit stake from the sim probability and the offered
    (vigged) odds we would actually bet into;
  * **Closing Line Value (CLV)** -- the value of the line we ENTERED at versus the
    CLOSING line, in both probability and odds terms, per market / prop.

DE-VIG METHODS (documented)
---------------------------
A book quotes both sides of a market with implied probabilities that sum to MORE
than 1.0; the excess is the overround / vig.  We remove it two ways:

  * **two-way (multiplicative / proportional, the standard):** given raw implied
    probabilities ``(q_a, q_b)`` with ``q_a + q_b = 1 + m`` (m = overround), the
    no-vig probabilities are ``p_a = q_a / (q_a + q_b)``, ``p_b = q_b / (q_a + q_b)``.
    They sum to exactly 1.0 and EACH is reduced (the margin is shared in proportion
    to each side's raw implied probability).  This is the proportional / "normalize"
    method -- the workhorse used across moneyline, spread (run line), totals and
    each prop's over/under pair.
  * **multi-way (normalize the overround across N outcomes):** the same proportional
    normalization generalised to N >= 2 raw implied probabilities ``q_i``:
    ``p_i = q_i / sum_j(q_j)``.  Used when a market has more than two outcomes (e.g.
    a 3-way market, or a futures field); for a two-way market it reduces to the
    two-way formula above.

EDGE / EV SIGN CONVENTION
-------------------------
  * ``edge = p_sim - p_fair`` (no-vig).  POSITIVE edge means the simulator thinks
    the outcome is MORE likely than the de-vigged market does -- a candidate +EV bet.
  * ``EV per unit stake = p_sim * b - (1 - p_sim)`` where ``b`` is the decimal
    payout odds minus 1 (the net profit per unit on a win) for the OFFERED price.
    POSITIVE EV means a profitable bet at that price under the sim probability.
    EV is computed against the *offered* (vigged) odds because that is the price
    you actually transact at; edge is computed against the *fair* (no-vig)
    probability because that is the market's honest opinion stripped of margin.

CLV SIGN CONVENTION (the validation metric)
-------------------------------------------
Closing Line Value compares the line you ENTERED at to the CLOSING line (the final
market consensus before first pitch -- SIM-133 ``mark_closing_lines`` for game odds,
SIM-340 ``mark_closing_prop_lines`` for props designate that closing row).  We
express CLV on the SAME no-vig probability scale so it is directly comparable
across markets and prices:

    clv_prob = p_close_fair - p_entry_fair

POSITIVE CLV means you BEAT THE CLOSE: the de-vigged probability of your side rose
from entry to close (equivalently your entry odds were longer / cheaper than the
closing odds for the same true probability), so you got a better price than the
final market consensus.  NEGATIVE CLV means the line moved against you.  We also
report the odds-space delta (entry decimal odds vs the implied fair decimal odds at
the close) for display, but the PROBABILITY delta is the canonical sign-bearing
metric -- it is symmetric, additive across a portfolio, and immune to the +/-
American-odds discontinuity at even money.

DETERMINISM
-----------
Pure numpy / Python arithmetic.  No DB, no FAISS, no RNG.  Odds are injected by the
caller (the live provider / mock lines feed them in elsewhere).  The same inputs
always yield the same reports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import numpy as np

# Consumed simulation outputs (SIM-329 / SIM-330 / SIM-327).  Imported for type
# clarity and the convenience constructors; the core math takes plain probabilities
# so the engine is testable with synthetic inputs and never needs a live run.
from simulation.prop_distributions import PropDistribution
from simulation.results import GameSimSummary
from simulation.win_probability import WinProbability

# ===========================================================================
# American <-> decimal <-> implied-probability conversions
# ===========================================================================


def american_to_decimal(american: float) -> float:
    """Convert American odds to decimal (European) odds (total return per 1 staked).

    ``+150`` -> ``2.50`` (stake 1, win 1.5 profit, return 2.5).
    ``-110`` -> ``1.9090...`` (stake 1.1 to win 1).
    American odds of 0 are undefined (no such price); raise.
    """
    a = float(american)
    if a == 0:
        raise ValueError("American odds of 0 are undefined")
    if a > 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / (-a)


def decimal_to_american(decimal_odds: float) -> float:
    """Convert decimal odds (>1.0) to American odds.

    ``2.50`` -> ``+150``; ``1.9090...`` -> ``-110``.  At exactly even money
    (decimal 2.0) the convention is ``+100``.  ``decimal_odds`` must be > 1.
    """
    d = float(decimal_odds)
    if d <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    if d >= 2.0:
        return (d - 1.0) * 100.0
    return -100.0 / (d - 1.0)


def implied_prob_from_american(american: float) -> float:
    """Raw implied probability from American odds (margin NOT removed).

    For a favourite (negative): ``p = -a / (-a + 100)``; e.g. ``-110 -> 0.5238``.
    For an underdog (positive): ``p = 100 / (a + 100)``; e.g. ``+150 -> 0.40``.
    This is the book's quoted probability for ONE side and INCLUDES its share of
    the vig; sum the two sides of a market and you get ``1 + overround``.  De-vig
    (see :func:`devig_two_way` / :func:`devig_multiway`) to recover a true prob.
    """
    a = float(american)
    if a == 0:
        raise ValueError("American odds of 0 are undefined")
    if a > 0:
        return 100.0 / (a + 100.0)
    return -a / (-a + 100.0)


#: Alias matching the live-pipeline / DB vocabulary ("implied").
american_to_implied_prob = implied_prob_from_american


def prob_to_american(prob: float) -> float:
    """Inverse of :func:`implied_prob_from_american`: a probability -> fair American odds.

    "Fair" means the zero-vig price for that probability (a single side priced so
    its implied probability EQUALS ``prob``).  ``0.5238... -> -110``, ``0.40 -> +150``.
    ``prob`` must be strictly in (0, 1).  At exactly 0.5 the convention is ``+100``.
    """
    p = float(prob)
    if not (0.0 < p < 1.0):
        raise ValueError("probability must be strictly between 0 and 1")
    if p <= 0.5:
        return (1.0 - p) / p * 100.0
    return -p / (1.0 - p) * 100.0


#: Alias: "fair American odds from a probability".
fair_american_from_prob = prob_to_american


# ===========================================================================
# De-vig / no-vig fair odds
# ===========================================================================


def devig_multiway(
    implied_probs: Sequence[float] | np.ndarray,
) -> list[float]:
    """Remove the bookmaker margin from an N-way market (N >= 2) by normalisation.

    Given the raw implied probabilities ``q_i`` of every outcome (each from
    :func:`implied_prob_from_american`), which sum to ``1 + overround``, the no-vig
    (fair) probabilities are ``p_i = q_i / sum_j q_j``.  The returned list sums to
    exactly 1.0 and preserves the relative ordering of the inputs.  This is the
    proportional / multiplicative de-vig generalised to any number of outcomes.

    Raises if there are fewer than two outcomes, any implied prob is non-positive,
    or they sum to <= 0 (a degenerate market).
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
    """De-vig a TWO-way market (the proportional / multiplicative method).

    Takes the two sides' American odds (e.g. an over price and an under price, or a
    home ML and an away ML), converts each to a raw implied probability, then
    normalises so the pair sums to exactly 1.0:

        p_a = q_a / (q_a + q_b),   p_b = q_b / (q_a + q_b)

    Returns ``(p_a, p_b)`` -- the no-vig fair probabilities for (over/first side,
    under/second side).  BOTH are reduced relative to their raw implied
    probabilities (the shared overround is stripped in proportion to each side).
    """
    q_a = implied_prob_from_american(over_american)
    q_b = implied_prob_from_american(under_american)
    p_a, p_b = devig_multiway([q_a, q_b])
    return p_a, p_b


# ===========================================================================
# Edge and expected value
# ===========================================================================


def edge(p_sim: float, p_fair: float) -> float:
    """Edge = simulation probability - market FAIR (no-vig) probability.

    POSITIVE => the simulator is more bullish than the de-vigged market (a candidate
    +EV side).  ``p_fair`` should be a NO-VIG probability (from a de-vig call) so
    the comparison is against the market's honest opinion, not its vigged price.
    """
    return float(p_sim) - float(p_fair)


def expected_value(p_sim: float, offered_american: float) -> float:
    """EV per unit stake of betting ``offered_american`` when the true win prob is ``p_sim``.

    ``EV = p_sim * b - (1 - p_sim) * 1`` where ``b = decimal_odds - 1`` is the net
    profit per unit on a win.  Equivalently ``EV = p_sim * decimal_odds - 1``.
    POSITIVE EV is a profitable bet at that price under the sim probability.  EV uses
    the OFFERED (vigged) odds -- the price you actually transact at -- whereas
    :func:`edge` uses the fair no-vig probability.
    """
    p = float(p_sim)
    b = american_to_decimal(offered_american) - 1.0
    return p * b - (1.0 - p)


# ===========================================================================
# Closing Line Value
# ===========================================================================


@dataclass(frozen=True, slots=True)
class CLV:
    """Closing Line Value for one side of one market / prop (SIM-339).

    Positive :attr:`clv_prob` == you BEAT THE CLOSE (the de-vigged probability of
    your side rose from entry to close, i.e. you locked a better price than the
    final market consensus).  See the module docstring for the full sign convention.
    """

    #: No-vig fair probability of the bet side implied by the ENTRY line.
    entry_fair_prob: float
    #: No-vig fair probability of the bet side implied by the CLOSING line.
    close_fair_prob: float

    #: clv_prob = close_fair_prob - entry_fair_prob.  Positive == beat the close.
    clv_prob: float

    #: Fair (no-vig) decimal odds implied by the entry line for this side.
    entry_fair_decimal: float
    #: Fair (no-vig) decimal odds implied by the closing line for this side.
    close_fair_decimal: float
    #: Odds-space view: entry fair decimal - close fair decimal.  Positive == your
    #: entry secured longer (more generous) fair odds than the close.  Reported for
    #: display; clv_prob is the canonical sign-bearing metric.
    clv_decimal: float

    @property
    def beat_close(self) -> bool:
        """True iff this entry beat the closing line (clv_prob > 0)."""
        return self.clv_prob > 0.0


def clv_from_prob(entry_fair_prob: float, close_fair_prob: float) -> CLV:
    """Build a :class:`CLV` directly from two NO-VIG probabilities (entry vs close).

    Use this when you have already de-vigged both the entry and the closing market
    yourself (e.g. each two-way pair via :func:`devig_two_way`).  ``clv_prob`` is
    ``close_fair_prob - entry_fair_prob`` -- positive means the bet side's fair
    probability rose to the close, i.e. you beat the close.
    """
    p_entry = float(entry_fair_prob)
    p_close = float(close_fair_prob)
    if not (0.0 < p_entry < 1.0) or not (0.0 < p_close < 1.0):
        raise ValueError("fair probabilities must be strictly in (0, 1)")
    entry_dec = 1.0 / p_entry
    close_dec = 1.0 / p_close
    return CLV(
        entry_fair_prob=p_entry,
        close_fair_prob=p_close,
        clv_prob=p_close - p_entry,
        entry_fair_decimal=entry_dec,
        close_fair_decimal=close_dec,
        clv_decimal=entry_dec - close_dec,
    )


def clv_from_odds(
    *,
    entry_side_american: float,
    entry_other_american: float,
    close_side_american: float,
    close_other_american: float,
) -> CLV:
    """CLV from raw TWO-way American odds at entry and at the close.

    Pass, for the side you bet, the American price you ENTERED at plus the OTHER
    side's price at entry (so the entry market can be de-vigged), and the same pair
    at the CLOSE.  Each two-way market is de-vigged (:func:`devig_two_way`) to a
    fair probability for your side, then the two fair probabilities are compared
    (:func:`clv_from_prob`).  Positive ``clv_prob`` == beat the close.
    """
    p_entry, _ = devig_two_way(entry_side_american, entry_other_american)
    p_close, _ = devig_two_way(close_side_american, close_other_american)
    return clv_from_prob(p_entry, p_close)


# ===========================================================================
# Per-market / per-prop edge reports (consume SIM-329 / SIM-330 / SIM-327)
# ===========================================================================


class MarketSide(Enum):
    """Which side of a two-way market a report is about."""

    HOME = "home"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"


@dataclass(frozen=True, slots=True)
class OddsQuote:
    """An injected two-way American-odds quote (one snapshot of a market).

    ``side`` and ``other`` are the two American prices of the market.  For a
    moneyline that is (home_ml, away_ml); for a total or a prop over/under that is
    (over_ml, under_ml).  ``line`` is the market line (None for a moneyline, a
    float for a total / prop / spread).  Odds are INJECTED -- this engine never
    fetches them.
    """

    side: float
    other: float
    line: float | None = None


@dataclass(frozen=True, slots=True)
class TwoWayMarket:
    """An injected two-way market with an ENTRY quote and an optional CLOSING quote.

    Bundles the prices needed to price a bet end to end: the entry quote (what you
    can bet now) and, when known, the closing quote (the SIM-133 / SIM-340
    closing-line reference) for CLV.  ``side`` names which leg ``quote.side`` is.
    """

    side: MarketSide
    entry: OddsQuote
    close: OddsQuote | None = None


@dataclass(frozen=True, slots=True)
class EdgeReport:
    """The full edge / EV / CLV report for one side of one market or prop (SIM-339).

    Self-describing: it records the sim probability, the de-vigged market fair
    probability, the resulting edge and EV (vs the entry/offered price), and -- when
    a closing quote was supplied -- the :class:`CLV` of the entry versus the close.
    """

    label: str
    side: MarketSide
    line: float | None

    #: Our simulation probability of the bet side (SIM-329 / SIM-330 / SIM-327).
    sim_prob: float
    #: No-vig fair probability of the bet side from the de-vigged ENTRY market.
    market_fair_prob: float

    #: edge = sim_prob - market_fair_prob.  Positive == sim more bullish than market.
    edge: float
    #: EV per unit stake at the OFFERED (entry) American price under sim_prob.
    ev: float

    #: American price of the bet side we would transact at (the entry side price).
    offered_american: float
    #: Fair American odds for sim_prob (the price our model would post, zero-vig).
    sim_fair_american: float

    #: CLV of the entry vs the close, if a closing quote was supplied (else None).
    clv: CLV | None = None

    @property
    def positive_edge(self) -> bool:
        """True iff the simulator beats the de-vigged market (edge > 0)."""
        return self.edge > 0.0


def _build_edge_report(
    *,
    label: str,
    side: MarketSide,
    line: float | None,
    sim_prob: float,
    market: TwoWayMarket,
) -> EdgeReport:
    """Shared assembly: de-vig the entry market, compute edge/EV, and CLV if a close
    is supplied.  ``market.entry.side`` is the bet side's price; ``market.entry.other``
    the opposite side (needed to de-vig).  All market arithmetic is two-way."""
    p = float(sim_prob)
    # De-vig the ENTRY market to a fair probability for the bet side.
    entry_fair, _ = devig_two_way(market.entry.side, market.entry.other)
    ev = expected_value(p, market.entry.side)
    clv: CLV | None = None
    if market.close is not None:
        clv = clv_from_odds(
            entry_side_american=market.entry.side,
            entry_other_american=market.entry.other,
            close_side_american=market.close.side,
            close_other_american=market.close.other,
        )
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
        clv=clv,
    )


def moneyline_edge_report(
    win_prob: WinProbability,
    market: TwoWayMarket,
    *,
    side: MarketSide = MarketSide.HOME,
) -> EdgeReport:
    """Edge / EV / CLV for a MONEYLINE side, consuming a SIM-330 :class:`WinProbability`.

    The sim probability is ``win_prob.home_win_prob`` for :data:`MarketSide.HOME` and
    ``win_prob.away_win_prob`` for :data:`MarketSide.AWAY` (these sum to 1.0 -- ties
    have already been folded in by SIM-330).  ``market.entry.side`` must be the
    chosen side's American moneyline; ``market.entry.other`` the opposite side's.
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
    """Edge / EV / CLV for a PLAYER PROP side, consuming a SIM-329 :class:`PropDistribution`.

    The sim probability is ``dist.p_over(line)`` for :data:`MarketSide.OVER` and
    ``dist.p_under(line)`` for :data:`MarketSide.UNDER`, using the betting over/under
    convention SIM-329 documents (half-integer line: over + under == 1; integer
    line: a push mass is excluded from both).  The line comes from
    ``market.entry.line`` (the book's prop line).  ``market.entry.side`` is the
    chosen side's price (over_ml for OVER, under_ml for UNDER); ``.other`` the
    opposite price.
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
    """Edge / EV / CLV for a game TOTAL (over/under), consuming SIM-327 raw arrays.

    The sim probability is computed from the RAW per-iteration ``total_scores``
    array on the :class:`GameSimSummary` (SIM-327 preserves it precisely so the
    betting layer can build distributions).  We use the sportsbook over/under
    convention: at a HALF-integer line there is no push (over == P(total > line),
    under == P(total < line), they sum to 1); at an INTEGER line a total ON the line
    pushes, so over is strict ``P(total > line)``, under strict ``P(total < line)``,
    and the push mass is excluded from both -- mirroring SIM-329's prop convention.

    ``market.entry.line`` is the total line; ``market.entry.side`` the chosen side's
    price (over_ml for OVER, under_ml for UNDER); ``.other`` the opposite price.
    """
    line = market.entry.line
    if line is None:
        raise ValueError("a total market must carry a line (market.entry.line)")
    totals = np.asarray(summary.total_scores, dtype=np.float64)
    n = totals.size
    if n == 0:
        raise ValueError("GameSimSummary has no total_scores to price a total")
    if side is MarketSide.OVER:
        sim_p = float(np.count_nonzero(totals > float(line))) / n
    elif side is MarketSide.UNDER:
        sim_p = float(np.count_nonzero(totals < float(line))) / n
    else:
        raise ValueError("total side must be OVER or UNDER")
    return _build_edge_report(
        label="total",
        side=side,
        line=float(line),
        sim_prob=sim_p,
        market=market,
    )


# ---------------------------------------------------------------------------
# Run line / spread (SIM-367) -- cover probability from raw score margins
# ---------------------------------------------------------------------------


def _as_margin_array(
    summary_or_margin: GameSimSummary | Sequence[float] | np.ndarray,
) -> np.ndarray:
    """Coerce the input to a 1-D float64 SCORE-MARGIN array (``home - away``).

    Accepts either a SIM-327 :class:`GameSimSummary` -- in which case the margin is
    computed as ``home_scores - away_scores`` per iteration -- or a raw margin array
    (any sequence / ndarray of ``home - away`` values).  The returned array is the
    per-iteration home-minus-away run differential the spread is priced against.
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
    """Sim cover probability of a SPREAD (run line) side at ``line`` from raw margins.

    The score margin is ``margin = home - away`` per iteration (from a SIM-327
    :class:`GameSimSummary` or a raw margin array).  ``line`` is the spread of the
    **HOME** side under the standard convention: a home favourite is laying runs at a
    NEGATIVE line (e.g. ``-1.5``), a home underdog is getting runs at a POSITIVE line.
    The away line is the NEGATION of the home line (home ``-1.5`` <=> away ``+1.5``).

    Cover definitions (``L`` == the home line):

      * **HOME** covers iff the margin plus the line is positive::

            (home - away) + L > 0   <=>   margin > -L

        e.g. at ``L = -1.5`` the home favourite covers iff ``margin > 1.5`` (win by
        2+).  At ``L = +1.5`` the home underdog covers iff ``margin > -1.5`` (lose by
        at most 1, or win).

      * **AWAY** covers iff the away margin plus the away line is positive.  With the
        away line ``= -L``::

            (away - home) + (-L) > 0   <=>   -margin - L > 0   <=>   margin < -L

        i.e. AWAY covers iff ``margin < -L`` -- the exact complement of HOME's strict
        ``margin > -L``, with the boundary ``margin == -L`` belonging to NEITHER (the
        push, see below).

    Push handling.  The cover boundary is ``margin == -L``.  Baseball margins are
    integers, so:

      * a HALF-integer line (e.g. ``-1.5`` / ``+1.5``) can NEVER push -- ``-L`` is not
        an integer, no margin lands on it, and ``P(home covers) + P(away covers) == 1``;
      * an INTEGER line (e.g. ``-1`` / ``+1``, or ``0`` a "draw no bet" pick'em) CAN
        push -- iterations with ``margin == -L`` are pushes that count toward NEITHER
        side, so ``P(home covers) + P(away covers) + P(push) == 1`` and the push mass
        is excluded from both probabilities (mirrors the SIM-329 / total push rule).

    Both HOME and AWAY use STRICT inequalities, which is exactly what splits the push
    out at an integer line and is harmless at a half-integer line (nothing sits on the
    boundary).  Returns a probability in ``[0, 1]``.
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
    """Edge / EV / CLV for a RUN LINE / spread side, consuming SIM-327 raw margins.

    The sim probability is the spread COVER probability (:func:`spread_cover_prob`)
    computed from the per-iteration score margin ``home - away`` -- taken from a
    SIM-327 :class:`GameSimSummary` (``home_scores - away_scores``) or a raw margin
    array.  Baseball's standard run line is ``+/-1.5``; this is the default (home
    favourite ``-1.5`` / home underdog or away ``+1.5``).

    Line / side conventions (see :func:`spread_cover_prob` for the cover math):

      * ``line`` is the HOME line under the home convention (negative == home laying
        runs, positive == home getting runs).  The away line is its NEGATION.  When
        ``market.entry.line`` is set it OVERRIDES the ``line`` argument (the injected
        market line is authoritative, mirroring :func:`total_over_under_edge_report`);
        otherwise the ``line`` keyword (default ``-1.5``) is used.  The SAME home line
        is used for both sides -- AWAY is priced as the complement at ``-line`` -- so
        callers pass the home line regardless of which side they are reporting.
      * ``side`` selects HOME or AWAY; ``market.entry.side`` must be that side's
        American price and ``market.entry.other`` the opposite side's (needed to
        de-vig), exactly as for the moneyline / total reports.
      * Pushes (only possible at an integer line) are excluded from both sides; at the
        ``+/-1.5`` default no push is possible and HOME / AWAY cover probabilities are
        complementary.

    Returns an :class:`EdgeReport` labelled ``"run_line"`` carrying the cover
    ``sim_prob``, the de-vigged ``market_fair_prob``, ``edge``, ``ev`` (vs the offered
    entry price), and the :class:`CLV` when a closing quote is supplied.
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
    # CLV
    "CLV",
    "clv_from_prob",
    "clv_from_odds",
    # reports
    "MarketSide",
    "OddsQuote",
    "TwoWayMarket",
    "EdgeReport",
    "moneyline_edge_report",
    "prop_edge_report",
    "total_over_under_edge_report",
    "spread_cover_prob",
    "run_line_edge_report",
]
