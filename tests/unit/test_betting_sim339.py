"""
test_betting_sim339.py
======================
Unit tests for SIM-339 -- the **CLV / edge engine** :mod:`betting.clv_engine`
(Phase 4, Sprint 4).

These tests build SYNTHETIC sim outputs (a hand-made prop PMF, a hand-made
``WinProbability``, a hand-made ``GameSimSummary``) and INJECT American odds (no
live provider, no DB, no RNG), then assert the CLV framework's contract:

  * implied-probability math on known American odds (-110 -> 0.5238, +150 -> 0.40,
    +100 -> 0.50) and its inverse (prob -> fair American odds);
  * American <-> decimal round-trips;
  * two-way de-vig sums to exactly 1.0 and REDUCES both sides; multi-way de-vig
    normalises N outcomes to 1.0;
  * edge / EV signs: a sim prob ABOVE the no-vig prob => positive edge AND positive
    EV at the offered price; below => negative;
  * CLV is positive when the entry line beats the close, negative otherwise, zero
    when entry == close;
  * the report builders consume SIM-329 prop PMFs, SIM-330 win-prob, and SIM-327
    raw total arrays.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from simulation.prop_distributions import PropDistribution
from simulation.results import ConfidenceInterval, GameSimSummary
from simulation.win_probability import TieHandling, WinProbability

from betting.clv_engine import (
    CLV,
    EdgeReport,
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    american_to_decimal,
    american_to_implied_prob,
    clv_from_odds,
    clv_from_prob,
    decimal_to_american,
    devig_multiway,
    devig_two_way,
    edge,
    expected_value,
    fair_american_from_prob,
    implied_prob_from_american,
    moneyline_edge_report,
    prob_to_american,
    prop_edge_report,
    total_over_under_edge_report,
)


# ===========================================================================
# Implied probability / fair-odds conversions
# ===========================================================================

def test_implied_prob_known_american_odds():
    # -110 -> 0.5238...  (favourite juice)
    assert implied_prob_from_american(-110) == pytest.approx(0.5238095238, abs=1e-9)
    # +150 -> 0.40       (underdog)
    assert implied_prob_from_american(+150) == pytest.approx(0.40, abs=1e-12)
    # +100 -> 0.50       (even money)
    assert implied_prob_from_american(+100) == pytest.approx(0.50, abs=1e-12)
    # -200 -> 0.6667     (heavy favourite)
    assert implied_prob_from_american(-200) == pytest.approx(2.0 / 3.0, abs=1e-9)
    # alias agrees
    assert american_to_implied_prob(-110) == implied_prob_from_american(-110)


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
    assert fair_american_from_prob(2.0 / 3.0) == pytest.approx(-200.0, abs=1e-6)


def test_prob_to_american_rejects_out_of_range():
    for p in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            prob_to_american(p)


def test_american_decimal_roundtrip():
    assert american_to_decimal(+150) == pytest.approx(2.50, abs=1e-12)
    assert american_to_decimal(-110) == pytest.approx(1.0 + 100.0 / 110.0, abs=1e-12)
    assert american_to_decimal(+100) == pytest.approx(2.0, abs=1e-12)
    for a in (-110, +150, +100, -200, +250):
        d = american_to_decimal(a)
        assert decimal_to_american(d) == pytest.approx(a, abs=1e-6), a
    with pytest.raises(ValueError):
        decimal_to_american(1.0)


# ===========================================================================
# De-vig
# ===========================================================================

def test_two_way_devig_sums_to_one_and_reduces_both_sides():
    # A vigged -110 / -110 total: raw implied 0.5238 each, sum 1.0476 overround.
    q_over = implied_prob_from_american(-110)
    q_under = implied_prob_from_american(-110)
    assert q_over + q_under > 1.0  # there IS a vig

    p_over, p_under = devig_two_way(-110, -110)
    # Sums to exactly 1.0
    assert p_over + p_under == pytest.approx(1.0, abs=1e-12)
    # Symmetric market -> 0.5 each
    assert p_over == pytest.approx(0.5, abs=1e-12)
    assert p_under == pytest.approx(0.5, abs=1e-12)
    # Each fair prob is REDUCED relative to its raw implied prob
    assert p_over < q_over
    assert p_under < q_under


def test_two_way_devig_asymmetric_reduces_both():
    # A lopsided market: -200 favourite / +170 dog.
    q_fav = implied_prob_from_american(-200)
    q_dog = implied_prob_from_american(+170)
    p_fav, p_dog = devig_two_way(-200, +170)
    assert p_fav + p_dog == pytest.approx(1.0, abs=1e-12)
    assert p_fav < q_fav  # both sides reduced
    assert p_dog < q_dog
    assert p_fav > p_dog  # ordering preserved (favourite still favoured)


def test_multiway_devig_normalises_n_outcomes():
    # Three raw implied probs summing to 1.08 (8% overround).
    raw = [0.40, 0.38, 0.30]  # sums to 1.08
    fair = devig_multiway(raw)
    assert sum(fair) == pytest.approx(1.0, abs=1e-12)
    # Each reduced proportionally; ordering preserved.
    for f, r in zip(fair, raw):
        assert f < r
    assert fair[0] > fair[1] > fair[2]
    # Two-way multiway reduces to devig_two_way.
    q_a = implied_prob_from_american(-130)
    q_b = implied_prob_from_american(+110)
    mw = devig_multiway([q_a, q_b])
    tw = devig_two_way(-130, +110)
    assert mw[0] == pytest.approx(tw[0], abs=1e-12)
    assert mw[1] == pytest.approx(tw[1], abs=1e-12)


def test_devig_rejects_bad_input():
    with pytest.raises(ValueError):
        devig_multiway([0.5])  # < 2 outcomes
    with pytest.raises(ValueError):
        devig_multiway([0.5, 0.0])  # non-positive


# ===========================================================================
# Edge & EV signs
# ===========================================================================

def test_edge_sign_against_fair_prob():
    # Fair market prob 0.50; sim more bullish -> positive edge.
    assert edge(0.55, 0.50) == pytest.approx(0.05, abs=1e-12)
    # sim less bullish -> negative edge.
    assert edge(0.45, 0.50) == pytest.approx(-0.05, abs=1e-12)
    # equal -> zero edge.
    assert edge(0.50, 0.50) == pytest.approx(0.0, abs=1e-12)


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
    # If sim prob beats the FAIR (no-vig) prob, edge > 0.  EV at the OFFERED price
    # can still be negative because the offered price carries vig -- this documents
    # that edge (vs fair) and EV (vs offered) are distinct and intentionally so.
    p_over, _ = devig_two_way(-110, -110)  # fair 0.50
    sim = 0.52
    assert edge(sim, p_over) > 0.0          # beats the no-vig market
    # at -110 the break-even is 0.5238; sim 0.52 < that -> EV negative despite +edge
    assert expected_value(sim, -110) < 0.0


# ===========================================================================
# CLV
# ===========================================================================

def test_clv_positive_when_entry_beats_close():
    # Bet a side whose fair prob RISES from entry (0.50) to close (0.55):
    # you locked a longer price than the close -> positive CLV.
    clv = clv_from_prob(0.50, 0.55)
    assert clv.clv_prob == pytest.approx(0.05, abs=1e-12)
    assert clv.beat_close is True
    # Odds-space view: entry fair decimal (1/0.50=2.0) > close (1/0.55=1.818) -> +.
    assert clv.clv_decimal > 0.0


def test_clv_negative_when_line_moves_against():
    clv = clv_from_prob(0.55, 0.50)  # fair prob FELL from entry to close
    assert clv.clv_prob == pytest.approx(-0.05, abs=1e-12)
    assert clv.beat_close is False
    assert clv.clv_decimal < 0.0


def test_clv_zero_when_no_movement():
    clv = clv_from_prob(0.52, 0.52)
    assert clv.clv_prob == pytest.approx(0.0, abs=1e-12)
    assert clv.beat_close is False


def test_clv_from_two_way_odds_beats_close():
    # Entry: our side +120 (dog), other -140.  Close: our side -120 (now fav),
    # other +100.  Our side's fair prob rose -> we beat the close.
    clv = clv_from_odds(
        entry_side_american=+120,
        entry_other_american=-140,
        close_side_american=-120,
        close_other_american=+100,
    )
    assert clv.clv_prob > 0.0
    assert clv.beat_close is True


def test_clv_from_two_way_odds_worse_than_close():
    # Reverse: entry our side -120, close our side +120 -> line moved against us.
    clv = clv_from_odds(
        entry_side_american=-120,
        entry_other_american=+100,
        close_side_american=+120,
        close_other_american=-140,
    )
    assert clv.clv_prob < 0.0
    assert clv.beat_close is False


def test_clv_from_prob_rejects_degenerate():
    with pytest.raises(ValueError):
        clv_from_prob(0.0, 0.5)
    with pytest.raises(ValueError):
        clv_from_prob(0.5, 1.0)


# ===========================================================================
# Report builders (consume SIM-329 / SIM-330 / SIM-327)
# ===========================================================================

def _win_prob(p_home: float) -> WinProbability:
    """A synthetic SIM-330 WinProbability with an injected home win prob."""
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
    """A synthetic SIM-329 PropDistribution from an explicit PMF."""
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
    """A synthetic SIM-327 GameSimSummary carrying an injected total_scores array.

    We only need ``total_scores`` for the totals pricing, so split each total into a
    home/away pair and let from_results-style construction be bypassed by building
    the dataclass directly with the raw arrays."""
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
    # Market: home -130 (fair ~0.55 after de-vig), away +110.
    market = TwoWayMarket(
        side=MarketSide.HOME,
        entry=OddsQuote(side=-130, other=+110),
    )
    rep = moneyline_edge_report(wp, market, side=MarketSide.HOME)
    assert rep.sim_prob == pytest.approx(0.60, abs=1e-12)
    fair, _ = devig_two_way(-130, +110)
    assert rep.market_fair_prob == pytest.approx(fair, abs=1e-12)
    # Sim 0.60 > fair ~0.55 -> positive edge.
    assert rep.positive_edge is True
    assert rep.edge == pytest.approx(0.60 - fair, abs=1e-12)
    # EV at the offered -130 under p=0.60.
    assert rep.ev == pytest.approx(expected_value(0.60, -130), abs=1e-12)
    # sim_fair_american is the fair price for 0.60.
    assert rep.sim_fair_american == pytest.approx(prob_to_american(0.60), abs=1e-9)


def test_moneyline_away_side_uses_away_prob():
    wp = _win_prob(0.60)  # away is 0.40
    market = TwoWayMarket(
        side=MarketSide.AWAY,
        entry=OddsQuote(side=+150, other=-170),  # away dog priced +150 (fair 0.40)
    )
    rep = moneyline_edge_report(wp, market, side=MarketSide.AWAY)
    assert rep.sim_prob == pytest.approx(0.40, abs=1e-12)
    # away fair ~ devig(+150, -170)[0]; sim 0.40 close to it -> small edge
    fair, _ = devig_two_way(+150, -170)
    assert rep.edge == pytest.approx(0.40 - fair, abs=1e-12)


def test_moneyline_with_clv():
    wp = _win_prob(0.55)
    market = TwoWayMarket(
        side=MarketSide.HOME,
        entry=OddsQuote(side=+110, other=-130),   # entry: home dog +110
        close=OddsQuote(side=-120, other=+100),   # close: home now favourite
    )
    rep = moneyline_edge_report(wp, market, side=MarketSide.HOME)
    assert rep.clv is not None
    # Home's fair prob rose entry->close -> beat the close.
    assert rep.clv.beat_close is True
    assert rep.clv.clv_prob > 0.0


def test_prop_edge_report_consumes_pmf_over():
    # Strikeouts PMF centred so P(K >= 6) is high.  Support 4..8.
    dist = _prop_dist("K", [4, 5, 6, 7, 8], [0.05, 0.15, 0.30, 0.30, 0.20])
    # Line 5.5 -> over == P(K >= 6) == 0.30+0.30+0.20 = 0.80.
    market = TwoWayMarket(
        side=MarketSide.OVER,
        entry=OddsQuote(side=-110, other=-110, line=5.5),
    )
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
    market = TwoWayMarket(
        side=MarketSide.UNDER,
        entry=OddsQuote(side=-110, other=-110, line=6),
    )
    rep = prop_edge_report(dist, market, side=MarketSide.UNDER)
    assert rep.sim_prob == pytest.approx(0.20, abs=1e-12)
    # Over at the same integer line excludes the push too: P(K>6)=0.50.
    over_rep = prop_edge_report(
        dist,
        TwoWayMarket(side=MarketSide.OVER, entry=OddsQuote(-110, -110, line=6)),
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
            TwoWayMarket(side=MarketSide.OVER, entry=OddsQuote(-110, -110, line=None)),
            side=MarketSide.OVER,
        )


def test_total_edge_report_consumes_raw_arrays():
    # Totals: ten games, five at 7 and five at 10.
    summary = _summary_from_totals([7, 7, 7, 7, 7, 10, 10, 10, 10, 10])
    # Half-integer line 8.5 -> over == P(total > 8.5) == 5/10 = 0.50.
    market = TwoWayMarket(
        side=MarketSide.OVER,
        entry=OddsQuote(side=-105, other=-115, line=8.5),
    )
    rep = total_over_under_edge_report(summary, market, side=MarketSide.OVER)
    assert rep.sim_prob == pytest.approx(0.50, abs=1e-12)
    under_rep = total_over_under_edge_report(
        summary,
        TwoWayMarket(side=MarketSide.UNDER, entry=OddsQuote(-115, -105, line=8.5)),
        side=MarketSide.UNDER,
    )
    assert under_rep.sim_prob == pytest.approx(0.50, abs=1e-12)
    # half-integer: over + under == 1 (no push).
    assert rep.sim_prob + under_rep.sim_prob == pytest.approx(1.0, abs=1e-12)


def test_total_edge_report_integer_line_excludes_push():
    summary = _summary_from_totals([7, 7, 8, 8, 8, 9, 9])  # three 8s push at line 8
    market = TwoWayMarket(
        side=MarketSide.OVER,
        entry=OddsQuote(side=-110, other=-110, line=8),
    )
    over = total_over_under_edge_report(summary, market, side=MarketSide.OVER)
    under = total_over_under_edge_report(
        summary,
        TwoWayMarket(side=MarketSide.UNDER, entry=OddsQuote(-110, -110, line=8)),
        side=MarketSide.UNDER,
    )
    # over = P(>8)=2/7, under = P(<8)=2/7, push = 3/7; over+under+push == 1.
    assert over.sim_prob == pytest.approx(2.0 / 7.0, abs=1e-12)
    assert under.sim_prob == pytest.approx(2.0 / 7.0, abs=1e-12)
    assert over.sim_prob + under.sim_prob + 3.0 / 7.0 == pytest.approx(1.0, abs=1e-12)


def test_total_with_clv_end_to_end():
    summary = _summary_from_totals([9] * 6 + [7] * 4)  # P(over 8.5) = 0.60
    market = TwoWayMarket(
        side=MarketSide.OVER,
        entry=OddsQuote(side=+105, other=-125, line=8.5),   # entry over +105
        close=OddsQuote(side=-115, other=-105, line=8.5),   # close over -115
    )
    rep = total_over_under_edge_report(summary, market, side=MarketSide.OVER)
    assert rep.sim_prob == pytest.approx(0.60, abs=1e-12)
    # Over's fair prob rose entry->close -> beat the close.
    assert rep.clv is not None
    assert rep.clv.beat_close is True
    assert rep.clv.clv_prob > 0.0
