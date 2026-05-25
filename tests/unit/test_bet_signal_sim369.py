"""
test_bet_signal_sim369.py
=========================
Unit tests for SIM-369 -- the **bet-signal / +EV recommendation module**
:mod:`betting.bet_signal` (Phase 5, Sprint 5).

These tests build SYNTHETIC :class:`~betting.clv_engine.EdgeReport` objects with the
REAL constructor from the CLV engine (no live provider, no DB, no RNG, no actual
simulation run) and assert the bet-signal contract:

  * a clearly +EV report (sim_prob 0.60 at +100 -> positive edge, positive EV)
    yields a :class:`BetSignal` whose ``stake_fraction`` matches the fractional-Kelly
    HAND CALCULATION and is positive and capped;
  * the stake cap is respected when raw quarter-Kelly would exceed it;
  * a -EV / sub-threshold report yields NO signal (gated out);
  * signals are returned sorted by EV descending (with a stable tie-break);
  * empty input and all-negative input both return the empty list;
  * :func:`stake_fraction` is pure and matches ``clamp(kf * max(0, (b*p - q)/b), 0, cap)``.
"""

from __future__ import annotations

import pytest

from betting.clv_engine import (
    EdgeReport,
    MarketSide,
    american_to_decimal,
    edge as edge_fn,
    expected_value,
    prob_to_american,
)
from betting.bet_signal import (
    DEFAULT_KELLY_FRACTION,
    DEFAULT_MAX_STAKE_FRACTION,
    BetSignal,
    BetSignalConfig,
    bet_signals_from_edges,
    kelly_fraction_full,
    stake_fraction,
)


# ===========================================================================
# Synthetic EdgeReport factory (real constructor, hand-set probabilities)
# ===========================================================================

def _edge_report(
    *,
    label: str,
    side: MarketSide,
    sim_prob: float,
    market_fair_prob: float,
    offered_american: float,
    line: "float | None" = None,
) -> EdgeReport:
    """Build a real EdgeReport with consistent edge/EV derived from the inputs.

    edge and ev are computed the SAME way the CLV engine computes them so the
    synthetic report is internally consistent (edge = sim - fair; ev at the offered
    price), but we never need a live simulation to produce one.
    """
    return EdgeReport(
        label=label,
        side=side,
        line=line,
        sim_prob=sim_prob,
        market_fair_prob=market_fair_prob,
        edge=edge_fn(sim_prob, market_fair_prob),
        ev=expected_value(sim_prob, offered_american),
        offered_american=offered_american,
        sim_fair_american=prob_to_american(sim_prob),
        clv=None,
    )


# ===========================================================================
# stake_fraction / Kelly hand-checks
# ===========================================================================

def test_full_kelly_hand_calc_even_money():
    # +100 -> decimal 2.0 -> b = 1.0.  p = 0.60, q = 0.40.
    # full Kelly = (b*p - q)/b = (0.6 - 0.4)/1 = 0.20.
    assert kelly_fraction_full(0.60, +100) == pytest.approx(0.20, abs=1e-12)
    # f* == EV / b cross-check.
    b = american_to_decimal(+100) - 1.0
    assert kelly_fraction_full(0.60, +100) == pytest.approx(
        expected_value(0.60, +100) / b, abs=1e-12
    )


def test_stake_fraction_quarter_kelly_hand_calc_below_cap():
    # +100, p=0.60 -> full Kelly 0.20.  Quarter Kelly = 0.25*0.20 = 0.05.
    # With a GENEROUS cap (0.10) the quarter-Kelly 0.05 is returned uncapped.
    rep = _edge_report(
        label="moneyline", side=MarketSide.HOME,
        sim_prob=0.60, market_fair_prob=0.50, offered_american=+100,
    )
    s = stake_fraction(rep, kelly_fraction=0.25, cap=0.10)
    assert s == pytest.approx(0.05, abs=1e-12)


def test_stake_fraction_respects_cap():
    # Half Kelly at p=0.60/+100: 0.5*0.20 = 0.10, but cap 0.05 -> clamped to 0.05.
    rep = _edge_report(
        label="moneyline", side=MarketSide.HOME,
        sim_prob=0.60, market_fair_prob=0.50, offered_american=+100,
    )
    s = stake_fraction(rep, kelly_fraction=0.50, cap=0.05)
    assert s == pytest.approx(0.05, abs=1e-12)


def test_stake_fraction_zero_for_non_positive_kelly():
    # p below break-even at the price -> negative full Kelly -> floored to 0.
    # +100 break-even p = 0.50; p = 0.45 is -EV.
    rep = _edge_report(
        label="moneyline", side=MarketSide.AWAY,
        sim_prob=0.45, market_fair_prob=0.50, offered_american=+100,
    )
    assert kelly_fraction_full(0.45, +100) < 0.0
    assert stake_fraction(rep) == 0.0


def test_stake_fraction_matches_closed_form():
    # Generic price: -120 -> decimal 1.8333.., b = 0.83333.., p = 0.58.
    p = 0.58
    american = -120
    rep = _edge_report(
        label="total", side=MarketSide.OVER, line=8.5,
        sim_prob=p, market_fair_prob=0.50, offered_american=american,
    )
    b = american_to_decimal(american) - 1.0
    q = 1.0 - p
    expected = max(0.0, min(0.05, 0.25 * max(0.0, (b * p - q) / b)))
    assert stake_fraction(rep, kelly_fraction=0.25, cap=0.05) == pytest.approx(
        expected, abs=1e-12
    )


def test_stake_fraction_rejects_negative_knobs():
    rep = _edge_report(
        label="moneyline", side=MarketSide.HOME,
        sim_prob=0.60, market_fair_prob=0.50, offered_american=+100,
    )
    with pytest.raises(ValueError):
        stake_fraction(rep, kelly_fraction=-0.1)
    with pytest.raises(ValueError):
        stake_fraction(rep, cap=-0.1)


# ===========================================================================
# bet_signals_from_edges -- gating
# ===========================================================================

def test_positive_ev_report_yields_signal_with_capped_kelly_stake():
    # Clearly +EV: sim 0.60 vs fair 0.50 at +100.  edge = 0.10 > 0, ev > 0.
    rep = _edge_report(
        label="moneyline", side=MarketSide.HOME,
        sim_prob=0.60, market_fair_prob=0.50, offered_american=+100,
    )
    assert rep.edge == pytest.approx(0.10, abs=1e-12)
    assert rep.ev > 0.0

    signals = bet_signals_from_edges([rep])
    assert len(signals) == 1
    sig = signals[0]
    assert isinstance(sig, BetSignal)
    assert sig.label == "moneyline"
    assert sig.side is MarketSide.HOME
    assert sig.offered_american == +100
    assert sig.edge == pytest.approx(0.10, abs=1e-12)
    assert sig.ev == pytest.approx(rep.ev, abs=1e-12)
    assert sig.rank == 0
    assert sig.report is rep
    # Default quarter Kelly = 0.05, default cap = 0.05 -> stake == 0.05 (at the cap),
    # which equals the hand calc 0.25 * 0.20 = 0.05.
    assert sig.stake_fraction == pytest.approx(0.05, abs=1e-12)
    assert 0.0 < sig.stake_fraction <= DEFAULT_MAX_STAKE_FRACTION


def test_negative_ev_report_yields_no_signal():
    # -EV: sim 0.45 (< break-even 0.50) at +100; edge also negative vs fair 0.50.
    rep = _edge_report(
        label="moneyline", side=MarketSide.AWAY,
        sim_prob=0.45, market_fair_prob=0.50, offered_american=+100,
    )
    assert rep.positive_edge is False
    assert rep.ev < 0.0
    assert bet_signals_from_edges([rep]) == []


def test_sub_threshold_edge_yields_no_signal():
    # Tiny POSITIVE edge (0.5%) below the 2% min_edge floor -> gated out even though
    # it may be marginally +EV.
    rep = _edge_report(
        label="moneyline", side=MarketSide.HOME,
        sim_prob=0.505, market_fair_prob=0.50, offered_american=+100,
    )
    assert rep.positive_edge is True
    assert rep.edge < BetSignalConfig().min_edge
    assert bet_signals_from_edges([rep]) == []


def test_zero_ev_at_threshold_is_excluded():
    # EV exactly at min_ev (0.0) is excluded -- gate is strict ev > min_ev.
    # +100 with p = 0.50 -> ev == 0 exactly; pair with a fair prob low enough that
    # edge clears min_edge so EV is the binding gate.
    rep = _edge_report(
        label="moneyline", side=MarketSide.HOME,
        sim_prob=0.50, market_fair_prob=0.40, offered_american=+100,
    )
    assert rep.edge == pytest.approx(0.10, abs=1e-12)  # clears min_edge
    assert rep.ev == pytest.approx(0.0, abs=1e-12)      # at the EV floor
    assert bet_signals_from_edges([rep]) == []


def test_custom_config_thresholds():
    # A 3% edge report passes the default 2% floor but fails a stricter 5% floor.
    rep = _edge_report(
        label="total", side=MarketSide.OVER, line=8.5,
        sim_prob=0.53, market_fair_prob=0.50, offered_american=+100,
    )
    assert len(bet_signals_from_edges([rep])) == 1  # default min_edge 0.02
    strict = BetSignalConfig(min_edge=0.05)
    assert bet_signals_from_edges([rep], config=strict) == []


# ===========================================================================
# bet_signals_from_edges -- ranking
# ===========================================================================

def test_signals_sorted_by_ev_descending():
    # Three +EV reports with DIFFERENT EVs (drive EV via the offered price + prob).
    # Bigger underdog price at a high sim prob -> larger EV.
    low = _edge_report(
        label="ml_low", side=MarketSide.HOME,
        sim_prob=0.55, market_fair_prob=0.50, offered_american=+100,
    )
    mid = _edge_report(
        label="ml_mid", side=MarketSide.HOME,
        sim_prob=0.60, market_fair_prob=0.50, offered_american=+120,
    )
    high = _edge_report(
        label="ml_high", side=MarketSide.HOME,
        sim_prob=0.65, market_fair_prob=0.50, offered_american=+150,
    )
    # sanity: EVs strictly increasing low < mid < high
    assert low.ev < mid.ev < high.ev

    signals = bet_signals_from_edges([low, mid, high])
    assert [s.label for s in signals] == ["ml_high", "ml_mid", "ml_low"]
    # EVs are monotonically non-increasing and ranks are 0,1,2.
    assert [s.rank for s in signals] == [0, 1, 2]
    assert signals[0].ev >= signals[1].ev >= signals[2].ev


def test_mixed_input_filters_then_ranks():
    pos_big = _edge_report(
        label="ml_big", side=MarketSide.HOME,
        sim_prob=0.65, market_fair_prob=0.50, offered_american=+150,
    )
    neg = _edge_report(
        label="ml_neg", side=MarketSide.AWAY,
        sim_prob=0.40, market_fair_prob=0.50, offered_american=+100,
    )
    pos_small = _edge_report(
        label="ml_small", side=MarketSide.HOME,
        sim_prob=0.55, market_fair_prob=0.50, offered_american=+100,
    )
    tiny_edge = _edge_report(  # +EV-ish but edge below the 2% floor
        label="ml_tiny", side=MarketSide.HOME,
        sim_prob=0.505, market_fair_prob=0.50, offered_american=+100,
    )
    signals = bet_signals_from_edges([pos_small, neg, pos_big, tiny_edge])
    # Only the two clearing both gates survive, ranked by EV desc.
    assert [s.label for s in signals] == ["ml_big", "ml_small"]


def test_empty_input_returns_empty_list():
    assert bet_signals_from_edges([]) == []


def test_all_negative_input_returns_empty_list():
    reports = [
        _edge_report(
            label=f"neg{i}", side=MarketSide.AWAY,
            sim_prob=0.40, market_fair_prob=0.55, offered_american=-110,
        )
        for i in range(4)
    ]
    assert all(not r.positive_edge for r in reports)
    assert bet_signals_from_edges(reports) == []


# ===========================================================================
# BetSignalConfig validation
# ===========================================================================

def test_config_rejects_negative_knobs():
    with pytest.raises(ValueError):
        BetSignalConfig(kelly_fraction=-0.1)
    with pytest.raises(ValueError):
        BetSignalConfig(max_stake_fraction=-0.1)


def test_config_defaults():
    cfg = BetSignalConfig()
    assert cfg.kelly_fraction == DEFAULT_KELLY_FRACTION
    assert cfg.max_stake_fraction == DEFAULT_MAX_STAKE_FRACTION
    assert cfg.min_edge == 0.02
    assert cfg.min_ev == 0.0
