"""
The pricing half of :mod:`betting.bet_signal`: the implied probabilities and the edges.

The edge is the simulator's probability minus the market's no-vig
probability. The EV (expected value) is the profit per unit staked at the
offered price. To de-vig is to remove the book's margin from a market's prices.

The tests build SYNTHETIC simulator outputs by hand (a prop PMF, that is a
probability mass function over a stat's values; a ``WinProbability``; a
``GameSimSummary``) and pass American odds in directly: no live provider, no
database, no random draw. They check:

  * the implied probability of known American odds (-110 -> 0.5238,
    +150 -> 0.40, +100 -> 0.50) and its inverse, the fair American odds;
  * the round trip between American and decimal odds;
  * a two-way de-vig sums to exactly 1.0 and LOWERS both sides; a multi-way
    de-vig scales N outcomes to a sum of 1.0;
  * the signs: a simulator probability ABOVE the no-vig probability gives a
    positive edge; the EV sign reads the offered price, vig included;
  * the report builders read the prop PMFs, the win probability and the raw
    total-score arrays.
"""

from __future__ import annotations

import numpy as np
import pytest

from betting.bet_signal import (
    MarketSide,
    OddsQuote,
    american_to_decimal,
    devig,
    edge_report,
    expected_value,
    implied_prob_from_american,
    moneyline_edge_report,
    prob_to_american,
    prop_edge_report,
    samples_over_under_edge_report,
)
from simulation.prop_distributions import PropDistribution
from simulation.results import ConfidenceInterval, GameSimSummary
from simulation.win_probability import TieHandling, WinProbability

# ===========================================================================
# Implied probability / fair-odds conversions
# ===========================================================================


def test_implied_prob_known_american_odds():
    # -110 -> 0.5238...  (the standard price with the vig)
    assert implied_prob_from_american(-110) == pytest.approx(0.5238095238, abs=1e-9)
    # +150 -> 0.40       (underdog)
    assert implied_prob_from_american(+150) == pytest.approx(0.40, abs=1e-12)
    # +100 -> 0.50       (even money)
    assert implied_prob_from_american(+100) == pytest.approx(0.50, abs=1e-12)
    # -200 -> 0.6667     (heavy favourite)
    assert implied_prob_from_american(-200) == pytest.approx(2.0 / 3.0, abs=1e-9)


def test_american_zero_is_undefined():
    with pytest.raises(ValueError):
        implied_prob_from_american(0)
    with pytest.raises(ValueError):
        american_to_decimal(0)


def test_prob_to_american_is_inverse():
    for a in (-110, +150, +100, -200, +250, -350):
        p = implied_prob_from_american(a)
        back = prob_to_american(p)
        assert back == pytest.approx(a, abs=1e-6), a
    # known anchors
    assert prob_to_american(0.40) == pytest.approx(150.0, abs=1e-9)
    assert prob_to_american(0.50) == pytest.approx(100.0, abs=1e-9)
    assert prob_to_american(2.0 / 3.0) == pytest.approx(-200.0, abs=1e-6)


def test_prob_to_american_rejects_out_of_range():
    for p in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            prob_to_american(p)


def test_american_to_decimal():
    assert american_to_decimal(+150) == pytest.approx(2.50, abs=1e-12)
    assert american_to_decimal(-110) == pytest.approx(1.0 + 100.0 / 110.0, abs=1e-12)
    assert american_to_decimal(+100) == pytest.approx(2.0, abs=1e-12)


# ===========================================================================
# De-vig
# ===========================================================================


def test_two_way_devig_sums_to_one_and_reduces_both_sides():
    # A vigged -110 / -110 total: raw implied 0.5238 each, sum 1.0476 overround.
    q_over = implied_prob_from_american(-110)
    q_under = implied_prob_from_american(-110)
    assert q_over + q_under > 1.0  # there IS a vig

    p_over, p_under = devig([-110, -110])
    # Sums to exactly 1.0
    assert p_over + p_under == pytest.approx(1.0, abs=1e-12)
    # Symmetric market -> 0.5 each
    assert p_over == pytest.approx(0.5, abs=1e-12)
    assert p_under == pytest.approx(0.5, abs=1e-12)
    # Each fair prob is LOWER than its raw implied prob
    assert p_over < q_over
    assert p_under < q_under


def test_two_way_devig_asymmetric_reduces_both():
    # A lopsided market: -200 favourite / +170 dog.
    q_fav = implied_prob_from_american(-200)
    q_dog = implied_prob_from_american(+170)
    p_fav, p_dog = devig([-200, +170])
    assert p_fav + p_dog == pytest.approx(1.0, abs=1e-12)
    assert p_fav < q_fav  # both sides reduced
    assert p_dog < q_dog
    assert p_fav > p_dog  # ordering preserved (favourite still favoured)


def test_three_way_devig_normalises_every_outcome():
    # A home / away / tie market with an overround.
    prices = [+150, +160, +250]
    raw = [implied_prob_from_american(a) for a in prices]
    assert sum(raw) > 1.0
    fair = devig(prices)
    assert sum(fair) == pytest.approx(1.0, abs=1e-12)
    # Each side is scaled by the same factor, so the order is kept.
    for f, q in zip(fair, raw, strict=True):
        assert f == pytest.approx(q / sum(raw), abs=1e-12)
    assert fair[0] > fair[1] > fair[2]


def test_devig_rejects_bad_input():
    with pytest.raises(ValueError):
        devig([-110])  # fewer than 2 prices
    with pytest.raises(ValueError):
        devig([-110, 0])  # a price of 0


# ===========================================================================
# Edge & EV signs
# ===========================================================================


def _edge(p_sim: float, p_fair: float) -> float:
    return edge_report(
        label="t",
        side=MarketSide.HOME,
        line=None,
        sim_prob=p_sim,
        fair_prob=p_fair,
        offered_american=-110,
    ).edge


def test_edge_sign_against_fair_prob():
    # Fair market prob 0.50; sim more bullish -> positive edge.
    assert _edge(0.55, 0.50) == pytest.approx(0.05, abs=1e-12)
    # sim less bullish -> negative edge.
    assert _edge(0.45, 0.50) == pytest.approx(-0.05, abs=1e-12)
    # equal -> zero edge.
    assert _edge(0.50, 0.50) == pytest.approx(0.0, abs=1e-12)


def test_ev_sign_at_offered_price():
    # Even-money (+100): EV = 2*p - 1.  p>0.5 -> +EV, p<0.5 -> -EV.
    assert expected_value(0.55, +100) == pytest.approx(0.10, abs=1e-12)
    assert expected_value(0.45, +100) == pytest.approx(-0.10, abs=1e-12)
    assert expected_value(0.50, +100) == pytest.approx(0.0, abs=1e-12)
    # Break-even at the offered price equals that price's implied prob: a sim prob
    # exactly equal to the (vigged) implied prob gives EV == 0.
    p_be = implied_prob_from_american(-110)
    assert expected_value(p_be, -110) == pytest.approx(0.0, abs=1e-12)
    # Above break-even -> +EV, below -> -EV.
    assert expected_value(p_be + 0.02, -110) > 0.0
    assert expected_value(p_be - 0.02, -110) < 0.0


def test_edge_and_ev_agree_in_sign_vs_fair():
    # A sim prob above the FAIR (no-vig) prob gives edge > 0. The EV at the OFFERED
    # price can still be negative, because the offered price carries the vig. The
    # edge (against the fair price) and the EV (against the offered price) differ
    # on purpose.
    p_over, _ = devig([-110, -110])  # fair 0.50
    sim = 0.52
    assert sim - p_over > 0.0  # beats the no-vig market
    # at -110 the break-even is 0.5238; sim 0.52 < that -> EV negative despite +edge
    assert expected_value(sim, -110) < 0.0


# ===========================================================================
# Report builders (they read the prop PMFs, the win probability, the totals)
# ===========================================================================


def _win_prob(p_home: float) -> WinProbability:
    """A synthetic WinProbability with a given home win probability."""
    return WinProbability(
        home_win_prob=p_home,
        away_win_prob=1.0 - p_home,
        n_iterations=1000,
        n_decisive=1000,
        home_win_ci=ConfidenceInterval(point=p_home, low=p_home, high=p_home),
        tie_pct=0.0,
        alpha=0.5,
        tie_handling=TieHandling.SPLIT,
        calibration_map="identity",
        confidence_level=0.95,
    )


def _prop_dist(prop: str, support, probs) -> PropDistribution:
    """A synthetic PropDistribution from an explicit PMF."""
    support = np.asarray(support, dtype=np.int64)
    probs = np.asarray(probs, dtype=np.float64)
    mean = float((support * probs).sum())
    return PropDistribution(
        player_id=1,
        prop=prop,
        n=1000,
        support=support,
        probabilities=probs,
        mean=mean,
        median=float(support[len(support) // 2]),
        std=1.0,
    )


def _summary_from_totals(totals) -> GameSimSummary:
    """A synthetic GameSimSummary that carries the given ``total_scores``.

    The totals pricing reads only ``total_scores``. The helper splits each total
    into a home and an away score and builds the dataclass directly from the raw
    arrays, with no ``from_results`` call."""
    totals = np.asarray(totals, dtype=np.int64)
    home = totals // 2
    away = totals - home
    ci = ConfidenceInterval(point=0.0, low=0.0, high=0.0)
    return GameSimSummary(
        n_iterations=int(totals.size),
        home_win_pct=0.5,
        away_win_pct=0.5,
        tie_pct=0.0,
        home_score_mean=float(home.mean()),
        away_score_mean=float(away.mean()),
        total_score_mean=float(totals.mean()),
        home_score_median=0.0,
        away_score_median=0.0,
        total_score_median=0.0,
        home_scores=home,
        away_scores=away,
        total_scores=totals,
        home_win_ci=ci,
        away_win_ci=ci,
        home_score_ci=ci,
        away_score_ci=ci,
        total_score_ci=ci,
    )


def test_moneyline_edge_report_consumes_win_prob():
    wp = _win_prob(0.60)  # sim says home 60%
    # Market: home -130 (fair ~0.54 after de-vig), away +110.
    market = OddsQuote(side=-130, other=+110)
    rep = moneyline_edge_report(wp, market, side=MarketSide.HOME)
    assert rep.sim_prob == pytest.approx(0.60, abs=1e-12)
    fair, _ = devig([-130, +110])
    assert rep.market_fair_prob == pytest.approx(fair, abs=1e-12)
    # Sim 0.60 > fair ~0.54 -> positive edge.
    assert rep.positive_edge is True
    assert rep.edge == pytest.approx(0.60 - fair, abs=1e-12)
    # EV at the offered -130 under p=0.60.
    assert rep.ev == pytest.approx(expected_value(0.60, -130), abs=1e-12)
    # sim_fair_american is the fair price for 0.60.
    assert rep.sim_fair_american == pytest.approx(prob_to_american(0.60), abs=1e-9)


def test_moneyline_away_side_uses_away_prob():
    wp = _win_prob(0.60)  # away is 0.40
    market = OddsQuote(side=+150, other=-170)
    rep = moneyline_edge_report(wp, market, side=MarketSide.AWAY)
    assert rep.sim_prob == pytest.approx(0.40, abs=1e-12)
    # away fair = devig(+150, -170)[0] ~0.39; sim 0.40 is close to it -> small edge
    fair, _ = devig([+150, -170])
    assert rep.edge == pytest.approx(0.40 - fair, abs=1e-12)


def test_prop_edge_report_consumes_pmf_over():
    # Strikeouts PMF centred so P(K >= 6) is high.  Support 4..8.
    dist = _prop_dist("K", [4, 5, 6, 7, 8], [0.05, 0.15, 0.30, 0.30, 0.20])
    # Line 5.5 -> over == P(K >= 6) == 0.30+0.30+0.20 = 0.80.
    market = OddsQuote(side=-110, other=-110, line=5.5)
    rep = prop_edge_report(dist, market, side=MarketSide.OVER)
    assert rep.sim_prob == pytest.approx(0.80, abs=1e-12)
    assert rep.line == pytest.approx(5.5, abs=1e-12)
    # Fair market 0.50 (symmetric -110/-110); sim 0.80 -> big positive edge & EV.
    assert rep.market_fair_prob == pytest.approx(0.50, abs=1e-12)
    assert rep.positive_edge is True
    assert rep.ev > 0.0
    assert rep.label == "prop:K"


def test_prop_edge_report_under_and_push_convention():
    dist = _prop_dist("K", [4, 5, 6, 7, 8], [0.05, 0.15, 0.30, 0.30, 0.20])
    # Integer line 6: under == P(K < 6) == 0.05+0.15 = 0.20 (push mass at 6 excluded).
    market = OddsQuote(side=-110, other=-110, line=6)
    rep = prop_edge_report(dist, market, side=MarketSide.UNDER)
    assert rep.sim_prob == pytest.approx(0.20, abs=1e-12)
    # Over at the same integer line excludes the push too: P(K>6)=0.50.
    over_rep = prop_edge_report(
        dist,
        OddsQuote(-110, -110, line=6),
        side=MarketSide.OVER,
    )
    assert over_rep.sim_prob == pytest.approx(0.50, abs=1e-12)
    # over + under + push == 1 at the integer line.
    assert rep.sim_prob + over_rep.sim_prob + dist.p_push(6) == pytest.approx(1.0, abs=1e-12)


def test_prop_edge_report_requires_line():
    dist = _prop_dist("H", [0, 1, 2], [0.4, 0.4, 0.2])
    with pytest.raises(ValueError):
        prop_edge_report(
            dist,
            OddsQuote(-110, -110, line=None),
            side=MarketSide.OVER,
        )


def test_total_edge_report_consumes_raw_arrays():
    # Totals: ten games, five at 7 and five at 10.
    summary = _summary_from_totals([7, 7, 7, 7, 7, 10, 10, 10, 10, 10])
    # Half-integer line 8.5 -> over == P(total > 8.5) == 5/10 = 0.50.
    market = OddsQuote(side=-105, other=-115, line=8.5)
    rep = samples_over_under_edge_report(
        summary.total_scores, market, side=MarketSide.OVER, label="total"
    )
    assert rep.sim_prob == pytest.approx(0.50, abs=1e-12)
    under_rep = samples_over_under_edge_report(
        summary.total_scores, OddsQuote(-115, -105, line=8.5), side=MarketSide.UNDER, label="total"
    )
    assert under_rep.sim_prob == pytest.approx(0.50, abs=1e-12)
    # half-integer: over + under == 1 (no push).
    assert rep.sim_prob + under_rep.sim_prob == pytest.approx(1.0, abs=1e-12)


def test_total_edge_report_integer_line_excludes_push():
    summary = _summary_from_totals([7, 7, 8, 8, 8, 9, 9])  # three 8s push at line 8
    market = OddsQuote(side=-110, other=-110, line=8)
    over = samples_over_under_edge_report(
        summary.total_scores, market, side=MarketSide.OVER, label="total"
    )
    under = samples_over_under_edge_report(
        summary.total_scores, OddsQuote(-110, -110, line=8), side=MarketSide.UNDER, label="total"
    )
    # over = P(>8)=2/7, under = P(<8)=2/7, push = 3/7; over+under+push == 1.
    assert over.sim_prob == pytest.approx(2.0 / 7.0, abs=1e-12)
    assert under.sim_prob == pytest.approx(2.0 / 7.0, abs=1e-12)
    assert over.sim_prob + under.sim_prob + 3.0 / 7.0 == pytest.approx(1.0, abs=1e-12)


def test_total_end_to_end():
    summary = _summary_from_totals([9] * 6 + [7] * 4)  # P(over 8.5) = 0.60
    market = OddsQuote(side=+105, other=-125, line=8.5)
    rep = samples_over_under_edge_report(
        summary.total_scores, market, side=MarketSide.OVER, label="total"
    )
    assert rep.sim_prob == pytest.approx(0.60, abs=1e-12)
    fair, _ = devig([+105, -125])
    assert rep.edge == pytest.approx(0.60 - fair, abs=1e-12)
    assert rep.ev == pytest.approx(expected_value(0.60, +105), abs=1e-12)
