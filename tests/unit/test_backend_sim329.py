"""
test_backend_sim329.py
======================
Unit tests for SIM-329 -- the **prop-distribution aggregator**
:mod:`simulation.prop_distributions` (Phase 4, Sprint 4).

These tests build synthetic per-game :class:`BoxScore`s (no loop run, no DB/FAISS)
and assert the prop-PMF contract:

  * the PMF sums to 1.0 and lists only values that occurred;
  * mean / median / std match the raw samples;
  * ``P(X >= line)`` / ``P(X <= line)`` / strict variants are correct at INTEGER
    and HALF-INTEGER lines, and the betting over/under/push convention holds
    (half-integer: over + under == 1; integer: over + under + push == 1, with a
    5.5 line splitting cleanly);
  * total bases is computed as ``h + 3*hr`` (the documented (h, hr)-only lower
    bound), per game;
  * an absent player yields ``None`` rather than crashing;
  * pitcher vs batter props are assigned by activity (a pure batter has no K PMF);
  * a DNP game contributes a 0 to the prop's PMF (denominator stays the full N);
  * :meth:`from_results` reads ``GameSimResult.boxscore``.
"""

from __future__ import annotations

import math
import statistics

import numpy as np
import pytest

from simulation.game_state import GameState
from simulation.results import BoxScore, GameSimResult, PlayerStatLine
from simulation.prop_distributions import (
    ALL_PROPS,
    BATTER_PROPS,
    PITCHER_PROPS,
    PropDistribution,
    PropDistributionSet,
    TB_IS_LOWER_BOUND,
    _total_bases,
)

PITCHER = 477132
BATTER = 545361


# ===========================================================================
# Helpers — synthetic per-game boxscores
# ===========================================================================


def _state() -> GameState:
    return GameState(pitcher_id=0, bat_hand="R", season=2024)


def _box(lines: "list[PlayerStatLine]") -> BoxScore:
    """A per-game BoxScore from explicit PlayerStatLines."""
    box = BoxScore()
    for ln in lines:
        box.lines[ln.player_id] = ln
    return box


def _pitcher_line(pid: int, *, k=0, bb=0, er=0, outs=0) -> PlayerStatLine:
    return PlayerStatLine(player_id=pid, k=k, bb=bb, er=er, outs_recorded=outs)


def _batter_line(pid: int, *, ab=0, h=0, hr=0, rbi=0) -> PlayerStatLine:
    return PlayerStatLine(player_id=pid, ab=ab, h=h, hr=hr, rbi=rbi)


def _result(box: "BoxScore | None") -> GameSimResult:
    return GameSimResult(
        home_score=1, away_score=0, innings_played=9, final_state=_state(),
        boxscore=box,
    )


# ===========================================================================
# PropDistribution.from_samples — PMF correctness
# ===========================================================================


def test_pmf_sums_to_one_and_compact_support():
    samples = [3, 3, 5, 7, 7, 7]
    d = PropDistribution.from_samples(PITCHER, "K", samples)
    pmf = d.pmf()
    assert math.isclose(sum(pmf.values()), 1.0, rel_tol=0, abs_tol=1e-12)
    # only values that actually occurred appear (compact support)
    assert set(pmf.keys()) == {3, 5, 7}
    assert math.isclose(pmf[7], 3 / 6)
    assert math.isclose(pmf[3], 2 / 6)
    assert math.isclose(pmf[5], 1 / 6)
    # zero-safe lookup for an absent value
    assert d.prob(4) == 0.0
    assert math.isclose(d.prob(7), 0.5)


def test_mean_median_std_match_samples():
    samples = [1, 2, 2, 4, 6]
    d = PropDistribution.from_samples(BATTER, "H", samples)
    assert math.isclose(d.mean, float(np.mean(samples)))
    assert math.isclose(d.median, statistics.median(samples))
    assert math.isclose(d.std, float(np.std(samples, ddof=1)))


def test_single_sample_std_is_zero_and_ci_degenerate():
    d = PropDistribution.from_samples(PITCHER, "K", [5])
    assert d.n == 1
    assert d.std == 0.0
    assert math.isclose(d.mean, 5.0)
    ci = d.mean_ci()
    assert ci.low == ci.high == ci.point == 5.0


def test_empty_samples_raises():
    with pytest.raises(ValueError):
        PropDistribution.from_samples(PITCHER, "K", [])


# ===========================================================================
# Over/under — integer & half-integer lines, betting convention
# ===========================================================================


def test_p_at_least_and_at_most_integer_line():
    # K samples: 4,5,6,6,8 -> support {4,5,6,8} probs {.2,.2,.4,.2}
    d = PropDistribution.from_samples(PITCHER, "K", [4, 5, 6, 6, 8])
    assert math.isclose(d.p_at_least(6), 0.4 + 0.2)   # P(X>=6) = 6,6,8 = 3/5
    assert math.isclose(d.p_at_most(6), 0.2 + 0.2 + 0.4)  # P(X<=6) = 4,5,6,6 = 4/5
    assert math.isclose(d.p_greater(6), 0.2)          # P(X>6) = 8 = 1/5
    assert math.isclose(d.p_less(6), 0.2 + 0.2)       # P(X<6) = 4,5 = 2/5
    assert math.isclose(d.p_push(6), 0.4)             # P(X==6) = 2/5
    # at_least + (less) cover everything; greater + at_most cover everything
    assert math.isclose(d.p_at_least(6) + d.p_less(6), 1.0)
    assert math.isclose(d.p_greater(6) + d.p_at_most(6), 1.0)


def test_half_integer_line_splits_cleanly_no_push():
    # The standard 5.5 prop line: over == P(X>=6), under == P(X<=5), sum to 1.
    d = PropDistribution.from_samples(PITCHER, "K", [4, 5, 6, 6, 8])
    over = d.p_over(5.5)
    under = d.p_under(5.5)
    assert math.isclose(over, 3 / 5)    # 6,6,8
    assert math.isclose(under, 2 / 5)   # 4,5
    assert math.isclose(over + under, 1.0)
    assert d.p_push(5.5) == 0.0
    # over at 5.5 equals the inclusive P(X>=6)
    assert math.isclose(over, d.p_at_least(6))
    assert math.isclose(under, d.p_at_most(5))


def test_integer_line_over_under_push_sum_to_one():
    d = PropDistribution.from_samples(PITCHER, "K", [4, 5, 6, 6, 8])
    over = d.p_over(6)     # strict P(X>6)
    under = d.p_under(6)   # strict P(X<6)
    push = d.p_push(6)     # P(X==6)
    assert math.isclose(over, 1 / 5)
    assert math.isclose(under, 2 / 5)
    assert math.isclose(push, 2 / 5)
    assert math.isclose(over + under + push, 1.0)


def test_line_outside_support():
    d = PropDistribution.from_samples(BATTER, "H", [0, 0, 1, 2])
    assert d.p_at_least(0) == 1.0
    assert math.isclose(d.p_at_least(3), 0.0)
    assert d.p_at_most(5) == 1.0
    assert math.isclose(d.p_over(-0.5), 1.0)  # everyone is over a negative line


# ===========================================================================
# Total bases — the documented (h, hr)-only lower bound
# ===========================================================================


def test_total_bases_formula():
    # 3 hits, 1 of them a HR -> 2 singles (2 TB) + 1 HR (4 TB) = 6
    ln = _batter_line(BATTER, ab=4, h=3, hr=1, rbi=2)
    assert _total_bases(ln) == 3 + 3 * 1  # h + 3*hr = 6
    # all singles
    assert _total_bases(_batter_line(BATTER, ab=4, h=2, hr=0)) == 2
    # two HR among 2 hits -> 8
    assert _total_bases(_batter_line(BATTER, ab=4, h=2, hr=2)) == 8
    assert TB_IS_LOWER_BOUND is True


def test_tb_pmf_per_game():
    # game1: 1 single (TB 1); game2: 1 HR among 1 hit (TB 4); game3: 0 (TB 0)
    boxes = [
        _box([_batter_line(BATTER, ab=4, h=1, hr=0, rbi=0)]),
        _box([_batter_line(BATTER, ab=4, h=1, hr=1, rbi=1)]),
        _box([_batter_line(BATTER, ab=3, h=0, hr=0, rbi=0)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    tb = s.get(BATTER, "TB")
    assert tb is not None
    assert tb.pmf() == pytest.approx({0: 1 / 3, 1: 1 / 3, 4: 1 / 3})
    assert math.isclose(tb.mean, (0 + 1 + 4) / 3)


# ===========================================================================
# PropDistributionSet — aggregation by player, prop assignment, DNP zero-fill
# ===========================================================================


def test_pitcher_gets_pitcher_props_only():
    boxes = [
        _box([_pitcher_line(PITCHER, k=6, bb=2, er=1, outs=18)]),
        _box([_pitcher_line(PITCHER, k=8, bb=1, er=0, outs=21)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    props = s.get(PITCHER)
    assert props is not None
    assert set(props.keys()) == set(PITCHER_PROPS)
    # no batter props on a pure pitcher
    assert s.get(PITCHER, "H") is None
    k = s.get(PITCHER, "K")
    assert k.support.tolist() == [6, 8]
    assert math.isclose(k.mean, 7.0)
    # OUTS prop is the raw outs (IP in thirds); 18 outs == 6.0 IP
    outs = s.get(PITCHER, "OUTS")
    assert math.isclose(outs.p_at_least(18), 1.0)  # both games >= 6 IP
    assert math.isclose(outs.p_at_least(21), 0.5)  # one game >= 7 IP


def test_batter_gets_batter_props_only():
    boxes = [
        _box([_batter_line(BATTER, ab=4, h=2, hr=1, rbi=3)]),
        _box([_batter_line(BATTER, ab=3, h=0, hr=0, rbi=0)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    props = s.get(BATTER)
    assert set(props.keys()) == set(BATTER_PROPS)
    assert s.get(BATTER, "K") is None
    h = s.get(BATTER, "H")
    assert h.pmf() == pytest.approx({0: 0.5, 2: 0.5})


def test_dnp_game_contributes_zero():
    # Pitcher appears in game 1 only; game 2 he is absent -> a 0 K for game 2.
    boxes = [
        _box([_pitcher_line(PITCHER, k=10, outs=21)]),
        _box([_batter_line(BATTER, ab=4, h=1)]),  # different player; pitcher DNP
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    assert s.n_iterations == 2
    k = s.get(PITCHER, "K")
    # denominator is the full N=2: one game of 10 K, one DNP game of 0 K
    assert k.pmf() == pytest.approx({0: 0.5, 10: 0.5})
    assert math.isclose(k.mean, 5.0)


def test_absent_player_returns_none():
    boxes = [_box([_pitcher_line(PITCHER, k=5, outs=15)])]
    s = PropDistributionSet.from_boxscores(boxes)
    assert s.get(99999999) is None
    assert s.get(99999999, "K") is None
    # an unknown prop on a known player is also None (no crash)
    assert s.get(PITCHER, "NOPE") is None
    assert 99999999 not in s
    assert PITCHER in s


def test_empty_input_yields_empty_set():
    s = PropDistributionSet.from_boxscores([])
    assert s.n_iterations == 0
    assert s.player_ids() == []
    assert s.get(PITCHER) is None


# ===========================================================================
# from_results — reads GameSimResult.boxscore (incl. a None boxscore)
# ===========================================================================


def test_from_results_reads_boxscore():
    boxes = [
        _box([_pitcher_line(PITCHER, k=7, bb=1, er=2, outs=18)]),
        _box([_pitcher_line(PITCHER, k=5, bb=3, er=4, outs=15)]),
    ]
    results = [_result(b) for b in boxes]
    s = PropDistributionSet.from_results(results)
    assert s.n_iterations == 2
    k = s.get(PITCHER, "K")
    assert k.pmf() == pytest.approx({5: 0.5, 7: 0.5})


def test_from_results_none_boxscore_counts_as_zero_game():
    results = [
        _result(_box([_pitcher_line(PITCHER, k=8, outs=18)])),
        _result(None),  # SIM-320 run with no accumulated boxscore
    ]
    s = PropDistributionSet.from_results(results)
    assert s.n_iterations == 2
    k = s.get(PITCHER, "K")
    # the None-boxscore game is a 0 for the pitcher; denominator stays 2
    assert k.pmf() == pytest.approx({0: 0.5, 8: 0.5})


# ===========================================================================
# A two-player run sanity check (the realistic shape)
# ===========================================================================


def test_two_player_run():
    boxes = []
    for kk, hh in [(6, 2), (7, 1), (5, 3), (9, 0)]:
        boxes.append(_box([
            _pitcher_line(PITCHER, k=kk, bb=2, er=1, outs=18),
            _batter_line(BATTER, ab=4, h=hh, hr=(1 if hh else 0), rbi=hh),
        ]))
    s = PropDistributionSet.from_boxscores(boxes)
    assert set(s.player_ids()) == {PITCHER, BATTER}
    # pitcher K PMF over 4 games
    k = s.get(PITCHER, "K")
    assert math.isclose(sum(k.pmf().values()), 1.0)
    assert math.isclose(k.p_over(6.5), 2 / 4)  # 7 and 9 are over 6.5
    # batter H PMF
    h = s.get(BATTER, "H")
    assert h.pmf() == pytest.approx({0: 0.25, 1: 0.25, 2: 0.25, 3: 0.25})
    assert math.isclose(h.p_at_least(1), 0.75)
