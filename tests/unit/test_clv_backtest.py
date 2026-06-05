"""
tests/unit/test_clv_backtest.py
===============================
Unit tests for the SIM-429 CLV backtest scoreboard (``scripts/clv_backtest.py``).

These run with NO DB and NO real sim: they exercise the PURE per-bet CLV logic
(:func:`evaluate_two_way_market`) and the PURE aggregation
(:func:`aggregate_scoreboard`) on a small MOCKED slate — synthetic opening /
closing two-way prices + a synthetic model price — plus the prop vocab map.

The DB readers + the sim replay are never imported here (they live behind lazy
``asyncpg`` / ``api.routes.games`` imports inside the async ``run`` path), so this
module imports cleanly with only numpy + the betting engine available.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# Import the script module by path (scripts/ is not a package). It MUST be
# registered in sys.modules before exec_module so that @dataclass(slots=True) can
# resolve the module's namespace (dataclasses looks the module up by name).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MODULE_PATH = os.path.join(_REPO_ROOT, "scripts", "clv_backtest.py")
_spec = importlib.util.spec_from_file_location("clv_backtest", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
clv_backtest = importlib.util.module_from_spec(_spec)
sys.modules["clv_backtest"] = clv_backtest
_spec.loader.exec_module(clv_backtest)

from betting.clv_engine import MarketSide  # noqa: E402
from simulation.prop_distributions import ALL_PROPS, PropDistribution  # noqa: E402

BetRecord = clv_backtest.BetRecord
TwoWayPrices = clv_backtest.TwoWayPrices
evaluate_two_way_market = clv_backtest.evaluate_two_way_market
aggregate_scoreboard = clv_backtest.aggregate_scoreboard
PROP_VOCAB_MAP = clv_backtest.PROP_VOCAB_MAP


# ---------------------------------------------------------------------------
# Helpers: a synthetic model PropDistribution + an over/under edge-report closure
# ---------------------------------------------------------------------------


def _dist_centered_high() -> PropDistribution:
    """A model PMF strongly favouring the OVER of a 5.5 line, but INTERIOR.

    p_over(5.5) == P(X > 5.5) must be strictly in (0, 1): the betting engine's
    fair-odds conversion raises on a degenerate 0/1 model probability. So we keep
    a few samples below the line (4/5) so the over prob is high (~0.8) but not 1.0.
    """
    samples = [4] * 2 + [5] * 4 + [6] * 8 + [7] * 8 + [8] * 8
    dist = PropDistribution.from_samples(player_id=42, prop="K", samples=samples)
    # Sanity: high but interior so the OVER carries positive edge vs a +120 entry.
    assert 0.6 < dist.p_over(5.5) < 1.0
    return dist


def _over_under_report(dist: PropDistribution):
    """A ``report_for_side(side, entry_market)`` closure over a PropDistribution."""
    from betting.clv_engine import prop_edge_report

    def _builder(side, mkt):
        return prop_edge_report(dist, mkt, side=side)

    return _builder


# ---------------------------------------------------------------------------
# (a) per-bet CLV: a known opening-beats-close pair -> clv_prob>0 / beat_close
# ---------------------------------------------------------------------------


def test_opening_beats_close_yields_positive_clv():
    """Model loves the OVER; entry price (+120) is longer than the close (-140),
    so the de-vigged probability of the OVER rose to the close -> beat_close True,
    clv_prob > 0."""
    dist = _dist_centered_high()
    # Opening OVER at +120 (cheap), closing OVER at -140 (expensive) -> the market
    # moved TOWARD the over -> entering at +120 beat the close.
    prices = TwoWayPrices(
        open_side=120.0,  # opening over
        open_other=-140.0,  # opening under
        close_side=-140.0,  # closing over (steamed toward over)
        close_other=120.0,  # closing under
        line=5.5,
    )
    rec = evaluate_two_way_market(
        game_pk=12345,
        market="K",
        market_type="prop",
        side_a=MarketSide.OVER,
        side_b=MarketSide.UNDER,
        report_for_side=_over_under_report(dist),
        prices=prices,
        min_edge=0.0,
        player_id=42,
    )
    assert rec.placed is True
    assert rec.side == "over"  # the model's positive-edge side
    assert rec.model_edge is not None and rec.model_edge > 0.0
    assert rec.clv_prob is not None and rec.clv_prob > 0.0
    assert rec.beat_close is True


def test_reverse_close_beats_opening_yields_negative_clv():
    """The reverse: the market moved AWAY from the over (opening over expensive,
    closing over cheap), so the placed over bet did NOT beat the close."""
    dist = _dist_centered_high()
    prices = TwoWayPrices(
        open_side=-140.0,  # opening over (expensive)
        open_other=120.0,  # opening under
        close_side=120.0,  # closing over (steamed AWAY -> cheaper)
        close_other=-140.0,
        line=5.5,
    )
    rec = evaluate_two_way_market(
        game_pk=12345,
        market="K",
        market_type="prop",
        side_a=MarketSide.OVER,
        side_b=MarketSide.UNDER,
        report_for_side=_over_under_report(dist),
        prices=prices,
        min_edge=0.0,
        player_id=42,
    )
    assert rec.placed is True
    assert rec.side == "over"
    assert rec.clv_prob is not None and rec.clv_prob < 0.0
    assert rec.beat_close is False


def test_no_positive_edge_yields_no_bet():
    """When neither side clears the min-edge floor the model places NO bet."""
    dist = _dist_centered_high()
    prices = TwoWayPrices(
        open_side=120.0,
        open_other=-140.0,
        close_side=-140.0,
        close_other=120.0,
        line=5.5,
    )
    # An absurd min-edge no real edge can clear -> a 'no-bet' record.
    rec = evaluate_two_way_market(
        game_pk=12345,
        market="K",
        market_type="prop",
        side_a=MarketSide.OVER,
        side_b=MarketSide.UNDER,
        report_for_side=_over_under_report(dist),
        prices=prices,
        min_edge=0.99,
    )
    assert rec.placed is False
    assert rec.side is None
    assert rec.clv_prob is None
    assert rec.beat_close is None


# ---------------------------------------------------------------------------
# (b) aggregation: beat_close_rate + mean_clv_prob over a few mock bets
# ---------------------------------------------------------------------------


def _bet(
    market: str,
    market_type: str,
    *,
    placed: bool,
    beat: bool | None,
    clv: float | None,
    edge: float | None,
    game_pk: int,
) -> BetRecord:
    return BetRecord(
        game_pk=game_pk,
        market=market,
        market_type=market_type,
        placed=placed,
        side="over" if placed else None,
        line=5.5,
        model_edge=edge,
        model_ev=edge,
        clv_prob=clv,
        beat_close=beat,
    )


def test_aggregation_beat_close_rate_and_means():
    """beat_close_rate = placed-bets-that-beat / placed; means over placed only;
    a no-bet row counts toward n_markets_priced but not n_bets_placed."""
    bets = [
        # H market: 2 placed, 1 beat -> beat_close_rate 0.5; mean clv (0.1 + -0.3)/2 = -0.1
        _bet("H", "prop", placed=True, beat=True, clv=0.10, edge=0.05, game_pk=1),
        _bet("H", "prop", placed=True, beat=False, clv=-0.30, edge=0.02, game_pk=2),
        # moneyline: 1 placed (beat), 1 no-bet
        _bet("moneyline", "moneyline", placed=True, beat=True, clv=0.20, edge=0.08, game_pk=1),
        _bet("moneyline", "moneyline", placed=False, beat=None, clv=None, edge=None, game_pk=2),
    ]
    sb = aggregate_scoreboard(bets)

    overall = sb["overall"]
    # 4 markets priced, 3 placed, 2 of 3 beat the close.
    assert overall["n_markets_priced"] == 4
    assert overall["n_bets_placed"] == 3
    assert overall["n_games"] == 2
    assert overall["beat_close_rate"] == pytest.approx(2 / 3)
    # mean clv over placed bets: (0.10 - 0.30 + 0.20) / 3
    assert overall["mean_clv_prob"] == pytest.approx((0.10 - 0.30 + 0.20) / 3)
    assert overall["mean_model_edge"] == pytest.approx((0.05 + 0.02 + 0.08) / 3)

    by_market = {r["group"]: r for r in sb["by_market"]}
    assert by_market["H"]["n_bets_placed"] == 2
    assert by_market["H"]["beat_close_rate"] == pytest.approx(0.5)
    assert by_market["H"]["mean_clv_prob"] == pytest.approx((0.10 - 0.30) / 2)
    assert by_market["H"]["trust"] == "trustworthy"

    # moneyline: 1 placed (beat), 1 priced-but-no-bet.
    assert by_market["moneyline"]["n_markets_priced"] == 2
    assert by_market["moneyline"]["n_bets_placed"] == 1
    assert by_market["moneyline"]["beat_close_rate"] == pytest.approx(1.0)
    assert by_market["moneyline"]["trust"] == "loose"


def test_aggregation_empty_slate_is_safe():
    """An empty slate aggregates to all-zero rows (no division by zero)."""
    sb = aggregate_scoreboard([])
    assert sb["overall"]["n_markets_priced"] == 0
    assert sb["overall"]["n_bets_placed"] == 0
    assert sb["overall"]["beat_close_rate"] == 0.0
    assert sb["overall"]["mean_clv_prob"] == 0.0
    assert sb["by_market"] == []


def test_by_market_sorted_by_trust_tier():
    """The by-market rows are ordered best trust tier first (trustworthy ->
    untrustworthy), so the readable table groups correctly."""
    bets = [
        _bet("K", "prop", placed=True, beat=True, clv=0.1, edge=0.01, game_pk=1),  # untrustworthy
        _bet("HR", "prop", placed=True, beat=True, clv=0.1, edge=0.01, game_pk=1),  # trustworthy
        _bet("total", "total", placed=True, beat=True, clv=0.1, edge=0.01, game_pk=1),  # caution
        _bet(
            "moneyline", "moneyline", placed=True, beat=True, clv=0.1, edge=0.01, game_pk=1
        ),  # loose
    ]
    sb = aggregate_scoreboard(bets)
    trusts = [r["trust"] for r in sb["by_market"]]
    # The first must be the trustworthy one, the last the untrustworthy one.
    assert trusts[0] == "trustworthy"
    assert trusts[-1] == "untrustworthy"
    # And it is monotonic by tier rank.
    rank = {"trustworthy": 0, "loose": 1, "caution": 2, "untrustworthy": 3}
    assert trusts == sorted(trusts, key=lambda t: rank[t])


# ---------------------------------------------------------------------------
# (c) the prop vocab map covers all 7 stats and maps to real model props
# ---------------------------------------------------------------------------


def test_prop_vocab_map_covers_all_seven_markets():
    """The 7 SIM-134 odds prop_stats each map to a model PropDistribution stat."""
    expected_odds_stats = {
        "strikeouts",
        "walks",
        "earned_runs",
        "hits",
        "home_runs",
        "total_bases",
        "rbis",
    }
    assert set(PROP_VOCAB_MAP) == expected_odds_stats
    assert len(PROP_VOCAB_MAP) == 7
    # Every mapped target is a real model prop name.
    for model_stat in PROP_VOCAB_MAP.values():
        assert model_stat in ALL_PROPS
    # The expected one-to-one mapping.
    assert PROP_VOCAB_MAP == {
        "strikeouts": "K",
        "walks": "BB",
        "earned_runs": "ER",
        "hits": "H",
        "home_runs": "HR",
        "total_bases": "TB",
        "rbis": "RBI",
    }


def test_trust_label_for_every_mapped_prop_and_game_market():
    """Every model prop + every game market_type has a (non-'unknown') trust tier."""
    for model_stat in PROP_VOCAB_MAP.values():
        assert clv_backtest.trust_label(model_stat) != "unknown"
    for game_market in ("moneyline", "total", "runline"):
        assert clv_backtest.trust_label(game_market) != "unknown"
    # An unrecognized market degrades gracefully.
    assert clv_backtest.trust_label("nonsense") == "unknown"
