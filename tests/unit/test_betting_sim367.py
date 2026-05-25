"""
test_betting_sim367.py
======================
Unit tests for SIM-367 -- the **run line / spread EdgeReport** added to
:mod:`betting.clv_engine` (Phase 5, Sprint 5).

These tests build a SYNTHETIC per-iteration score-margin signal (a raw
``home - away`` array, and a SIM-327 :class:`GameSimSummary` carrying matching
``home_scores`` / ``away_scores``) and INJECT American odds at a run line (no live
provider, no DB, no RNG), then assert the spread pricing contract:

  * the sim COVER probability matches a hand computation -- HOME at ``-1.5`` covers
    iff ``margin > 1.5`` (win by 2+); AWAY (the ``+1.5`` complement) covers iff
    ``margin < 1.5``;
  * at the half-integer ``+/-1.5`` line HOME and AWAY cover probs are COMPLEMENTARY
    (no push); at an INTEGER line (``-1`` / ``+1``) a margin exactly on the boundary
    is a PUSH excluded from both sides (home + away + push == 1);
  * the input accepts BOTH a raw margin array and a :class:`GameSimSummary`;
  * the :class:`EdgeReport` carries the right ``label`` / ``side`` / ``line`` /
    ``sim_prob`` / ``market_fair_prob`` / ``edge`` / ``ev``;
  * CLV is computed when a closing quote is supplied;
  * ``market.entry.line`` overrides the ``line`` keyword default.
"""

from __future__ import annotations

import numpy as np
import pytest

from betting.clv_engine import (
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    devig_two_way,
    edge,
    expected_value,
    prob_to_american,
    run_line_edge_report,
    spread_cover_prob,
)
from simulation.results import ConfidenceInterval, GameSimSummary

# ---------------------------------------------------------------------------
# Synthetic-data helpers
# ---------------------------------------------------------------------------


def _summary_from_margins(margins) -> GameSimSummary:
    """A synthetic SIM-327 GameSimSummary whose home/away scores realise ``margins``.

    We only need the per-iteration ``home_scores - away_scores`` margin for run-line
    pricing, so we synthesise an away baseline of 0 and a home score equal to the
    desired margin (margin = home - away).  Negative margins are realised by lifting
    both scores by a constant offset so the stored arrays stay non-negative integers
    while the DIFFERENCE is exactly the requested margin."""
    margins = np.asarray(list(margins), dtype=np.int64)
    offset = int(max(0, -int(margins.min()))) if margins.size else 0
    away = np.full(margins.size, offset, dtype=np.int64)
    home = margins + offset
    total = home + away
    ci = ConfidenceInterval(point=0.0, low=0.0, high=0.0)
    return GameSimSummary(
        n_iterations=int(margins.size),
        home_win_pct=0.5,
        away_win_pct=0.5,
        tie_pct=0.0,
        home_score_mean=float(home.mean()),
        away_score_mean=float(away.mean()),
        total_score_mean=float(total.mean()),
        home_score_median=0.0,
        away_score_median=0.0,
        total_score_median=0.0,
        home_scores=home,
        away_scores=away,
        total_scores=total,
        home_win_ci=ci,
        away_win_ci=ci,
        home_score_ci=ci,
        away_score_ci=ci,
        total_score_ci=ci,
    )


# A reusable 10-iteration margin sample.  home - away per game:
#   -2, -1, 0, 1, 1, 2, 2, 3, 3, 4
# So: margin > 1.5 (home -1.5 covers) is {2,2,3,3,4} = 5/10 = 0.50.
#     margin < 1.5 (away +1.5 covers) is {-2,-1,0,1,1} = 5/10 = 0.50.
_MARGINS = [-2, -1, 0, 1, 1, 2, 2, 3, 3, 4]


# ===========================================================================
# spread_cover_prob -- cover math + push handling
# ===========================================================================


def test_home_cover_prob_half_integer_matches_hand_count():
    # HOME -1.5 covers iff margin > 1.5: {2,2,3,3,4} -> 5/10.
    p = spread_cover_prob(_MARGINS, -1.5, MarketSide.HOME)
    assert p == pytest.approx(0.50, abs=1e-12)
    # Explicit hand count using the documented rule margin > -L = margin > 1.5.
    hand = np.count_nonzero(np.asarray(_MARGINS) > 1.5) / len(_MARGINS)
    assert p == pytest.approx(hand, abs=1e-12)


def test_away_cover_prob_is_complement_at_half_integer():
    # AWAY (+1.5) covers iff margin < 1.5: {-2,-1,0,1,1} -> 5/10.
    p_home = spread_cover_prob(_MARGINS, -1.5, MarketSide.HOME)
    p_away = spread_cover_prob(_MARGINS, -1.5, MarketSide.AWAY)
    assert p_away == pytest.approx(0.50, abs=1e-12)
    # No push possible at +/-1.5 -> the two sides are complementary.
    assert p_home + p_away == pytest.approx(1.0, abs=1e-12)


def test_home_favourite_minus_one_point_five_typical():
    # A home blowout sample: win by 2+ in 7 of 10 -> -1.5 home covers 0.70.
    margins = [2, 2, 3, 4, 5, 2, 6, 0, 1, -1]  # margin>1.5 -> {2,2,3,4,5,2,6} = 7
    p = spread_cover_prob(margins, -1.5, MarketSide.HOME)
    assert p == pytest.approx(0.70, abs=1e-12)
    # Underdog away +1.5 is the complement.
    assert spread_cover_prob(margins, -1.5, MarketSide.AWAY) == pytest.approx(0.30, abs=1e-12)


def test_integer_line_excludes_push_both_sides():
    # Integer home line -1.0: boundary is margin == -L == 1 (home wins by exactly 1).
    # In _MARGINS, margin == 1 occurs TWICE -> those are PUSHES, in neither side.
    #   home -1 covers: margin > 1 -> {2,2,3,3,4} = 5/10 = 0.50
    #   away +1 covers: margin < 1 -> {-2,-1,0} = 3/10 = 0.30
    #   push: margin == 1 -> {1,1} = 2/10 = 0.20
    p_home = spread_cover_prob(_MARGINS, -1.0, MarketSide.HOME)
    p_away = spread_cover_prob(_MARGINS, -1.0, MarketSide.AWAY)
    push = np.count_nonzero(np.asarray(_MARGINS) == 1) / len(_MARGINS)
    assert p_home == pytest.approx(0.50, abs=1e-12)
    assert p_away == pytest.approx(0.30, abs=1e-12)
    assert push == pytest.approx(0.20, abs=1e-12)
    # The defining identity: cover + cover + push == 1 at the integer line.
    assert p_home + p_away + push == pytest.approx(1.0, abs=1e-12)


def test_pickem_zero_line_push_on_ties():
    # Line 0 (pick'em / draw-no-bet): boundary margin == 0 is a push (a tie game).
    margins = [-2, -1, 0, 0, 1, 2]  # two ties push
    p_home = spread_cover_prob(margins, 0.0, MarketSide.HOME)  # margin > 0 -> {1,2} = 2/6
    p_away = spread_cover_prob(margins, 0.0, MarketSide.AWAY)  # margin < 0 -> {-2,-1} = 2/6
    assert p_home == pytest.approx(2.0 / 6.0, abs=1e-12)
    assert p_away == pytest.approx(2.0 / 6.0, abs=1e-12)
    assert p_home + p_away + 2.0 / 6.0 == pytest.approx(1.0, abs=1e-12)


def test_summary_and_raw_margin_array_agree():
    # The GameSimSummary path (home_scores - away_scores) equals the raw-array path.
    summary = _summary_from_margins(_MARGINS)
    # Sanity: the summary really realises the requested margins.
    realised = (summary.home_scores - summary.away_scores).tolist()
    assert realised == _MARGINS
    for side in (MarketSide.HOME, MarketSide.AWAY):
        assert spread_cover_prob(summary, -1.5, side) == pytest.approx(
            spread_cover_prob(_MARGINS, -1.5, side), abs=1e-12
        )


def test_cover_prob_rejects_bad_side_and_empty():
    with pytest.raises(ValueError):
        spread_cover_prob(_MARGINS, -1.5, MarketSide.OVER)
    with pytest.raises(ValueError):
        spread_cover_prob([], -1.5, MarketSide.HOME)


# ===========================================================================
# run_line_edge_report -- full EdgeReport assembly
# ===========================================================================


def test_run_line_report_home_fields_and_edge():
    summary = _summary_from_margins(_MARGINS)  # home -1.5 cover = 0.50
    # Market: home -1.5 at +110 (dog price on the spread), away +1.5 at -130.
    market = TwoWayMarket(
        side=MarketSide.HOME,
        entry=OddsQuote(side=+110, other=-130, line=-1.5),
    )
    rep = run_line_edge_report(summary, market, side=MarketSide.HOME)
    # Core fields.
    assert rep.label == "run_line"
    assert rep.side is MarketSide.HOME
    assert rep.line == pytest.approx(-1.5, abs=1e-12)
    assert rep.sim_prob == pytest.approx(0.50, abs=1e-12)
    # De-vigged fair prob of the home side from the entry market.
    fair, _ = devig_two_way(+110, -130)
    assert rep.market_fair_prob == pytest.approx(fair, abs=1e-12)
    assert rep.edge == pytest.approx(edge(0.50, fair), abs=1e-12)
    # EV at the offered +110 under p = 0.50.
    assert rep.ev == pytest.approx(expected_value(0.50, +110), abs=1e-12)
    assert rep.offered_american == pytest.approx(+110, abs=1e-12)
    assert rep.sim_fair_american == pytest.approx(prob_to_american(0.50), abs=1e-9)
    # Sim 0.50 vs fair ~0.467 (devig of +110/-130) -> positive edge.
    assert rep.positive_edge is True


def test_run_line_report_away_side_uses_complement():
    summary = _summary_from_margins(_MARGINS)  # away +1.5 cover = 0.50
    market = TwoWayMarket(
        side=MarketSide.AWAY,
        entry=OddsQuote(side=-130, other=+110, line=-1.5),  # away side priced -130
    )
    rep = run_line_edge_report(summary, market, side=MarketSide.AWAY)
    assert rep.side is MarketSide.AWAY
    # AWAY is priced at the SAME home line (-1.5); cover = complement = 0.50.
    assert rep.line == pytest.approx(-1.5, abs=1e-12)
    assert rep.sim_prob == pytest.approx(0.50, abs=1e-12)
    fair, _ = devig_two_way(-130, +110)
    assert rep.market_fair_prob == pytest.approx(fair, abs=1e-12)


def test_run_line_report_default_line_when_market_line_missing():
    # No market.entry.line -> the line keyword default (-1.5) is used.
    summary = _summary_from_margins(_MARGINS)
    market = TwoWayMarket(
        side=MarketSide.HOME,
        entry=OddsQuote(side=+100, other=-120, line=None),
    )
    rep = run_line_edge_report(summary, market, side=MarketSide.HOME)
    assert rep.line == pytest.approx(-1.5, abs=1e-12)
    assert rep.sim_prob == pytest.approx(0.50, abs=1e-12)  # home -1.5 cover


def test_run_line_report_market_line_overrides_keyword():
    # market.entry.line (an integer -1.0) overrides the line keyword default.
    summary = _summary_from_margins(_MARGINS)
    market = TwoWayMarket(
        side=MarketSide.HOME,
        entry=OddsQuote(side=-110, other=-110, line=-1.0),
    )
    rep = run_line_edge_report(summary, market, side=MarketSide.HOME, line=-1.5)
    # The market's -1.0 line is authoritative -> cover = P(margin > 1) = 0.50,
    # and the push at margin==1 is excluded (distinct from the -1.5 result here only
    # in that the away side would differ; sim_prob equals 0.50 either way for HOME).
    assert rep.line == pytest.approx(-1.0, abs=1e-12)
    assert rep.sim_prob == pytest.approx(0.50, abs=1e-12)


def test_run_line_report_with_clv_end_to_end():
    summary = _summary_from_margins(_MARGINS)  # home -1.5 cover = 0.50
    market = TwoWayMarket(
        side=MarketSide.HOME,
        entry=OddsQuote(side=+120, other=-140, line=-1.5),  # entry: home dog +120
        close=OddsQuote(side=-120, other=+100, line=-1.5),  # close: home now favoured
    )
    rep = run_line_edge_report(summary, market, side=MarketSide.HOME)
    assert rep.clv is not None
    # Home's fair prob rose entry->close -> beat the close.
    assert rep.clv.beat_close is True
    assert rep.clv.clv_prob > 0.0


def test_run_line_report_rejects_over_under_side():
    summary = _summary_from_margins(_MARGINS)
    market = TwoWayMarket(
        side=MarketSide.OVER,
        entry=OddsQuote(side=-110, other=-110, line=-1.5),
    )
    with pytest.raises(ValueError):
        run_line_edge_report(summary, market, side=MarketSide.OVER)
