"""
test_sim421_prop_wrappers.py
============================
Unit tests for the seven prop-bet types the market offers but the platform
did not price (SIM-421) in :mod:`simulation.prop_distributions`.

The market prices these props and the platform now builds a PMF (a
probability mass function: value -> probability, summing to 1) for each:

  * batter: singles (``1B``), doubles (``2B``), triples (``3B``), runs
    (``R``), stolen bases (``SB``) and hits + runs + RBI (``HRR``);
  * pitcher: hits allowed (``H_ALLOWED``).

The tests build synthetic per-game :class:`BoxScore`s (no loop run, no DB)
and assert:

  * each new extractor reads the right :class:`PlayerStatLine` field;
  * ``1B`` is the remainder ``h - b2 - b3 - hr`` over mixed hit types;
  * ``HRR`` is the PER-GAME sum, so its PMF keeps the within-game
    correlation that a sum of the three marginal PMFs throws away;
  * the keyspace: a batter owns all ten batter props, a pitcher all five
    pitcher props, a two-way player both; the first four of each tuple keep
    their order (the frontend renders chips in tuple order);
  * ``p_over`` / ``p_under`` / ``p_push`` work on the new props;
  * a pinch-runner-only appearance (a run or a steal, no AB) gets batter
    props, and a pitcher who only allowed a hit gets pitcher props;
  * a DNP game still contributes a zero to a new prop.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from simulation.prop_distributions import (
    _PROP_EXTRACTORS,
    ALL_PROPS,
    BATTER_PROPS,
    PITCHER_PROPS,
    PropDistributionSet,
)
from simulation.results import BoxScore, PlayerStatLine

PITCHER = 477132
BATTER = 545361
TWO_WAY = 660271
RUNNER = 700001


# ===========================================================================
# Helpers -- synthetic per-game boxscores
# ===========================================================================


def _box(lines: list[PlayerStatLine]) -> BoxScore:
    """A per-game BoxScore from explicit PlayerStatLines."""
    box = BoxScore()
    for ln in lines:
        box.lines[ln.player_id] = ln
    return box


def _batter_line(
    pid: int,
    *,
    ab: int = 0,
    h: int = 0,
    b2: int = 0,
    b3: int = 0,
    hr: int = 0,
    rbi: int = 0,
    r: int = 0,
    sb: int = 0,
) -> PlayerStatLine:
    return PlayerStatLine(player_id=pid, ab=ab, h=h, b2=b2, b3=b3, hr=hr, rbi=rbi, r=r, sb=sb)


def _pitcher_line(
    pid: int,
    *,
    k: int = 0,
    bb: int = 0,
    er: int = 0,
    outs: int = 0,
    h_allowed: int = 0,
) -> PlayerStatLine:
    return PlayerStatLine(player_id=pid, k=k, bb=bb, er=er, outs_recorded=outs, h_allowed=h_allowed)


# ===========================================================================
# The prop vocabulary
# ===========================================================================


def test_prop_tuples_match_the_contract():
    # The first four of each tuple keep their order: the frontend renders
    # chips in tuple order, so the existing chips stay where they are.
    assert PITCHER_PROPS == ("K", "BB", "ER", "OUTS", "H_ALLOWED")
    assert BATTER_PROPS == ("H", "HR", "RBI", "TB", "1B", "2B", "3B", "R", "SB", "HRR")
    assert ALL_PROPS == PITCHER_PROPS + BATTER_PROPS
    assert len(ALL_PROPS) == 15
    assert len(set(ALL_PROPS)) == 15


def test_every_prop_has_exactly_one_extractor():
    assert set(_PROP_EXTRACTORS) == set(ALL_PROPS)


# ===========================================================================
# The new extractors, one field each
# ===========================================================================


def test_singles_is_the_remainder_over_mixed_hit_types():
    # 5 hits: 1 double, 1 triple, 1 HR -> 2 singles.
    ln = _batter_line(BATTER, ab=5, h=5, b2=1, b3=1, hr=1)
    assert _PROP_EXTRACTORS["1B"](ln) == 2
    # All singles.
    assert _PROP_EXTRACTORS["1B"](_batter_line(BATTER, ab=4, h=3)) == 3
    # No singles: every hit is a double or a HR.
    assert _PROP_EXTRACTORS["1B"](_batter_line(BATTER, ab=4, h=2, b2=1, hr=1)) == 0
    # No hits at all.
    assert _PROP_EXTRACTORS["1B"](_batter_line(BATTER, ab=4)) == 0


def test_doubles_triples_runs_steals_read_their_fields():
    ln = _batter_line(BATTER, ab=5, h=3, b2=2, b3=1, r=2, sb=3)
    assert _PROP_EXTRACTORS["2B"](ln) == 2
    assert _PROP_EXTRACTORS["3B"](ln) == 1
    assert _PROP_EXTRACTORS["R"](ln) == 2
    assert _PROP_EXTRACTORS["SB"](ln) == 3


def test_hrr_is_runs_plus_hits_plus_rbi_on_one_line():
    ln = _batter_line(BATTER, ab=4, h=2, hr=1, rbi=3, r=1)
    assert _PROP_EXTRACTORS["HRR"](ln) == 1 + 2 + 3
    # An empty line is 0.
    assert _PROP_EXTRACTORS["HRR"](_batter_line(BATTER)) == 0


def test_hits_allowed_reads_the_pitcher_field():
    ln = _pitcher_line(PITCHER, k=5, outs=18, h_allowed=7)
    assert _PROP_EXTRACTORS["H_ALLOWED"](ln) == 7
    # The batter-side H field is NOT the pitcher's hits allowed.
    assert _PROP_EXTRACTORS["H"](ln) == 0


def test_extractors_return_plain_ints():
    ln = _batter_line(BATTER, ab=4, h=2, b2=1, r=1, sb=1, rbi=1)
    for prop in ("1B", "2B", "3B", "R", "SB", "HRR"):
        assert type(_PROP_EXTRACTORS[prop](ln)) is int
    assert type(_PROP_EXTRACTORS["H_ALLOWED"](_pitcher_line(PITCHER, h_allowed=2))) is int


# ===========================================================================
# HRR is the per-game sum, not a sum of marginal PMFs
# ===========================================================================


def test_hrr_pmf_is_the_per_game_sum():
    # Three games: (r, h, rbi) = (1, 2, 1) -> 4; (0, 0, 0) -> 0; (2, 1, 3) -> 6.
    boxes = [
        _box([_batter_line(BATTER, ab=4, h=2, r=1, rbi=1)]),
        _box([_batter_line(BATTER, ab=4)]),
        _box([_batter_line(BATTER, ab=4, h=1, r=2, rbi=3)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    hrr = s.get(BATTER, "HRR")
    assert hrr is not None
    assert hrr.pmf() == pytest.approx({0: 1 / 3, 4: 1 / 3, 6: 1 / 3})
    assert math.isclose(hrr.mean, (4 + 0 + 6) / 3)
    # The betting query on the combined line.
    assert math.isclose(hrr.p_over(3.5), 2 / 3)
    assert math.isclose(hrr.p_under(3.5), 1 / 3)


def test_hrr_differs_from_a_convolution_of_the_marginals():
    # The same three games as above.  Build the PMF a naive consumer would get
    # by convolving the three marginal PMFs (R, H, RBI) as if they were
    # independent, and show it is NOT the per-game HRR PMF.
    boxes = [
        _box([_batter_line(BATTER, ab=4, h=2, r=1, rbi=1)]),
        _box([_batter_line(BATTER, ab=4)]),
        _box([_batter_line(BATTER, ab=4, h=1, r=2, rbi=3)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    hrr = s.get(BATTER, "HRR")
    assert hrr is not None

    def _dense(prop: str) -> np.ndarray:
        d = s.get(BATTER, prop)
        assert d is not None
        out = np.zeros(int(d.support.max()) + 1)
        out[d.support] = d.probabilities
        return out

    convolved = np.convolve(np.convolve(_dense("R"), _dense("H")), _dense("RBI"))
    # Independence puts mass on values no real game produced (e.g. 1 = a run
    # with no hit and no RBI) and shrinks P(0) from 1/3 to (1/3)^3.
    assert convolved[1] > 0.0
    assert hrr.prob(1) == 0.0
    assert math.isclose(convolved[0], (1 / 3) ** 3)
    assert math.isclose(hrr.prob(0), 1 / 3)
    # And the over on a real line disagrees.
    assert not math.isclose(float(convolved[4:].sum()), hrr.p_over(3.5))


# ===========================================================================
# The keyspace: which props a player owns
# ===========================================================================


def test_batter_owns_all_ten_batter_props_in_tuple_order():
    boxes = [
        _box([_batter_line(BATTER, ab=4, h=2, b2=1, r=1, rbi=1, sb=1)]),
        _box([_batter_line(BATTER, ab=3)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    props = s.get(BATTER)
    assert props is not None
    assert list(props.keys()) == list(BATTER_PROPS)
    assert len(props) == 10
    # No pitcher props on a pure batter.
    for prop in PITCHER_PROPS:
        assert s.get(BATTER, prop) is None


def test_pitcher_owns_all_five_pitcher_props_in_tuple_order():
    boxes = [
        _box([_pitcher_line(PITCHER, k=6, bb=2, er=1, outs=18, h_allowed=5)]),
        _box([_pitcher_line(PITCHER, k=8, bb=1, er=0, outs=21, h_allowed=3)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    props = s.get(PITCHER)
    assert props is not None
    assert list(props.keys()) == list(PITCHER_PROPS)
    assert len(props) == 5
    # No batter props on a pure pitcher.
    for prop in BATTER_PROPS:
        assert s.get(PITCHER, prop) is None


def test_two_way_player_owns_both_sets():
    line = PlayerStatLine(
        player_id=TWO_WAY, ab=4, h=1, hr=1, rbi=2, r=1, k=9, outs_recorded=18, h_allowed=4
    )
    s = PropDistributionSet.from_boxscores([_box([line])])
    props = s.get(TWO_WAY)
    assert props is not None
    assert list(props.keys()) == list(PITCHER_PROPS) + list(BATTER_PROPS)
    assert len(props) == 15
    assert s.get(TWO_WAY, "H_ALLOWED").prob(4) == 1.0
    assert s.get(TWO_WAY, "H").prob(1) == 1.0
    assert s.get(TWO_WAY, "HRR").prob(1 + 1 + 2) == 1.0


# ===========================================================================
# Over / under / push on the new props
# ===========================================================================


def test_over_under_on_steals_and_runs_half_integer_line():
    # SB per game: 0, 1, 1, 2 ; R per game: 0, 0, 1, 3
    boxes = [
        _box([_batter_line(BATTER, ab=4, sb=0, r=0)]),
        _box([_batter_line(BATTER, ab=4, sb=1, r=0)]),
        _box([_batter_line(BATTER, ab=4, sb=1, r=1)]),
        _box([_batter_line(BATTER, ab=4, sb=2, r=3)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    sb = s.get(BATTER, "SB")
    assert math.isclose(sb.p_over(0.5), 3 / 4)
    assert math.isclose(sb.p_under(0.5), 1 / 4)
    assert math.isclose(sb.p_over(0.5) + sb.p_under(0.5), 1.0)
    assert sb.p_push(0.5) == 0.0
    r = s.get(BATTER, "R")
    assert math.isclose(r.p_over(0.5), 2 / 4)
    assert math.isclose(r.p_over(1.5), 1 / 4)


def test_over_under_push_on_doubles_integer_line():
    # 2B per game: 0, 1, 1, 2 -> at the integer line 1: over 1/4, under 1/4, push 2/4.
    boxes = [
        _box([_batter_line(BATTER, ab=4, h=0)]),
        _box([_batter_line(BATTER, ab=4, h=1, b2=1)]),
        _box([_batter_line(BATTER, ab=4, h=2, b2=1)]),
        _box([_batter_line(BATTER, ab=4, h=2, b2=2)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    d = s.get(BATTER, "2B")
    assert math.isclose(d.p_over(1), 1 / 4)
    assert math.isclose(d.p_under(1), 1 / 4)
    assert math.isclose(d.p_push(1), 2 / 4)
    assert math.isclose(d.p_over(1) + d.p_under(1) + d.p_push(1), 1.0)
    # The singles PMF on the same games: 0, 0, 1, 0.
    singles = s.get(BATTER, "1B")
    assert singles.pmf() == pytest.approx({0: 3 / 4, 1: 1 / 4})


def test_over_under_on_hits_allowed():
    # H allowed per game: 3, 5, 6, 8
    boxes = [
        _box([_pitcher_line(PITCHER, outs=18, h_allowed=3)]),
        _box([_pitcher_line(PITCHER, outs=18, h_allowed=5)]),
        _box([_pitcher_line(PITCHER, outs=18, h_allowed=6)]),
        _box([_pitcher_line(PITCHER, outs=18, h_allowed=8)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    ha = s.get(PITCHER, "H_ALLOWED")
    assert ha.pmf() == pytest.approx({3: 0.25, 5: 0.25, 6: 0.25, 8: 0.25})
    assert math.isclose(ha.p_over(5.5), 2 / 4)
    assert math.isclose(ha.p_under(5.5), 2 / 4)
    assert math.isclose(ha.mean, 5.5)


# ===========================================================================
# Activity detection: the pinch runner and the hit-only pitcher
# ===========================================================================


def test_pinch_runner_with_only_a_run_gets_batter_props():
    # A pinch runner scores without a plate appearance: ab=0, h=0, rbi=0, r=1.
    boxes = [
        _box([_batter_line(RUNNER, r=1)]),
        _box([_batter_line(RUNNER)]),
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    props = s.get(RUNNER)
    assert props is not None
    assert set(props.keys()) == set(BATTER_PROPS)
    assert s.get(RUNNER, "R").pmf() == pytest.approx({0: 0.5, 1: 0.5})
    assert s.get(RUNNER, "HRR").pmf() == pytest.approx({0: 0.5, 1: 0.5})
    assert s.get(RUNNER, "K") is None


def test_pinch_runner_with_only_a_steal_gets_batter_props():
    boxes = [_box([_batter_line(RUNNER, sb=1)])]
    s = PropDistributionSet.from_boxscores(boxes)
    assert s.get(RUNNER) is not None
    assert s.get(RUNNER, "SB").prob(1) == 1.0
    assert s.get(RUNNER, "H").prob(0) == 1.0


def test_pitcher_who_only_allowed_a_hit_gets_pitcher_props():
    # A reliever enters, allows a single, and leaves: no out, K, BB or ER.
    boxes = [_box([_pitcher_line(PITCHER, h_allowed=1)])]
    s = PropDistributionSet.from_boxscores(boxes)
    props = s.get(PITCHER)
    assert props is not None
    assert set(props.keys()) == set(PITCHER_PROPS)
    assert s.get(PITCHER, "H_ALLOWED").prob(1) == 1.0
    assert s.get(PITCHER, "OUTS").prob(0) == 1.0


def test_line_with_no_activity_is_still_skipped():
    # Present in the boxscore but every field is 0: no prop is meaningful.
    boxes = [_box([PlayerStatLine(player_id=RUNNER)])]
    s = PropDistributionSet.from_boxscores(boxes)
    assert s.get(RUNNER) is None
    assert RUNNER not in s


# ===========================================================================
# DNP zero-fill on a new prop
# ===========================================================================


def test_dnp_game_contributes_zero_to_a_new_prop():
    boxes = [
        _box([_batter_line(BATTER, ab=4, h=1, sb=2)]),
        _box([_pitcher_line(PITCHER, k=3, outs=9)]),  # the batter is absent
    ]
    s = PropDistributionSet.from_boxscores(boxes)
    assert s.n_iterations == 2
    sb = s.get(BATTER, "SB")
    assert sb.pmf() == pytest.approx({0: 0.5, 2: 0.5})
    assert math.isclose(sb.mean, 1.0)
    # The pitcher's hits allowed is 0 in both games (one DNP, one clean inning).
    ha = s.get(PITCHER, "H_ALLOWED")
    assert ha.pmf() == pytest.approx({0: 1.0})
