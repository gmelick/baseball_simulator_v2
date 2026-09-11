"""
tests/unit/test_sim539_minimum_sample_size.py
================================================
Unit tests for SIM-539 — the stated MINIMUM SAMPLE SIZE for both reports in
``scripts/clv_backtest.py``: the new sim-vs-closing-line accuracy comparison
(SIM-538) and the legacy CLV scoreboard (SIM-429).

What SIM-539 adds, in plain words: a small sample can look like real skill
by pure chance. This ticket states, per market row, the smallest number of
observations that market needs before its result should be trusted at all —
using a standard power-analysis formula, the platform's own existing
"is this edge big enough to act on" floor (``betting.bet_signal.
DEFAULT_MIN_EDGE``), the SAME detection-margin CONSTANT ``tests/acceptance/
bands.py`` already uses, and a Bonferroni correction for scanning several
market rows at once. Both reports ALSO get an honest, GAME-CLUSTERED spread:
several observations from one game (several props from one start; three
game markets off one score) are correlated, not independent trials, and a
cluster-robust standard error is mathematically degenerate (always exactly
zero) with a single distinct game. An adversarial review of the first
version of this ticket found that a bare "at least 2 games" guard was not
enough — a small sample can still read as PERFECTLY resolved (zero
uncertainty) purely by chance — so this file also proves the raised
``MIN_CLUSTERS_FOR_INFERENCE`` floor and documents, rather than hides, the
limitation that remains even above it.

These tests run with NO DB and NO real sim: every function under test here
is PURE. See ``tests/unit/test_sim538_accuracy_comparison.py`` and
``tests/unit/test_clv_backtest.py`` for the surrounding scoring/aggregation
tests this ticket builds on.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys

import numpy as np
import pytest
from scipy.stats import norm

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

BetRecord = clv_backtest.BetRecord
AccuracyRecord = clv_backtest.AccuracyRecord
_bonferroni_alpha = clv_backtest._bonferroni_alpha
_z_two_sided = clv_backtest._z_two_sided
_minimum_paired_observations = clv_backtest._minimum_paired_observations
_minimum_observations_clustered = clv_backtest._minimum_observations_clustered
_clustered_se = clv_backtest._clustered_se
_json_safe = clv_backtest._json_safe
_row_for = clv_backtest._row_for
aggregate_scoreboard = clv_backtest.aggregate_scoreboard
format_scoreboard = clv_backtest.format_scoreboard
_accuracy_row_for = clv_backtest._accuracy_row_for
aggregate_accuracy_comparison = clv_backtest.aggregate_accuracy_comparison
format_accuracy_comparison = clv_backtest.format_accuracy_comparison
MIN_DETECTABLE_EDGE = clv_backtest.MIN_DETECTABLE_EDGE
DETECTION_MARGIN = clv_backtest.DETECTION_MARGIN
DEFAULT_ALPHA = clv_backtest.DEFAULT_ALPHA
MIN_CLUSTERS_FOR_INFERENCE = clv_backtest.MIN_CLUSTERS_FOR_INFERENCE


# ---------------------------------------------------------------------------
# (a) _bonferroni_alpha -- the multiple-comparisons correction
# ---------------------------------------------------------------------------


def test_bonferroni_alpha_no_correction_for_one_hypothesis():
    assert _bonferroni_alpha(0.05, 1) == pytest.approx(0.05)


def test_bonferroni_alpha_divides_by_the_hypothesis_count():
    assert _bonferroni_alpha(0.05, 5) == pytest.approx(0.01)
    assert _bonferroni_alpha(0.05, 10) == pytest.approx(0.005)


def test_bonferroni_alpha_treats_zero_or_negative_as_one():
    """A market count that is somehow zero or negative must not divide by
    zero or WIDEN alpha (which would make the test LESS conservative)."""
    assert _bonferroni_alpha(0.05, 0) == pytest.approx(0.05)
    assert _bonferroni_alpha(0.05, -3) == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# (b) _z_two_sided -- cross-checked against scipy directly
# ---------------------------------------------------------------------------


def test_z_two_sided_matches_the_well_known_95_percent_value():
    """The textbook value everyone recognizes: z_(alpha/2) at alpha=0.05 is
    1.959963984540054 (tests/acceptance/bands.py's own _Z_95 in the
    wave1-remediation branch this ticket picks up from)."""
    assert _z_two_sided(0.05) == pytest.approx(1.959963984540054, abs=1e-9)


def test_z_two_sided_cross_checked_against_scipy_directly():
    for alpha in (0.10, 0.05, 0.01, 0.005, 0.001):
        expected = float(norm.ppf(1.0 - alpha / 2.0))
        assert _z_two_sided(alpha) == pytest.approx(expected)


def test_z_two_sided_is_monotonically_decreasing_in_alpha():
    """A STRICTER significance level (smaller alpha) demands a LARGER z --
    Bonferroni-correcting across more markets must never make detection
    easier. alpha descends down the list, so z must ASCEND."""
    zs = [_z_two_sided(a) for a in (0.20, 0.10, 0.05, 0.01, 0.001)]
    assert zs == sorted(zs)


# ---------------------------------------------------------------------------
# (c) _minimum_paired_observations -- the shared power-analysis formula
# ---------------------------------------------------------------------------


def test_minimum_paired_observations_matches_the_textbook_formula():
    """n = (z_alpha/2 * sd / floor) ** 2 -- checked against a hand
    computation, not just re-derived from the same code."""
    sd, floor, alpha = 1.25, 0.08, 0.05
    z = 1.959963984540054
    expected = (z * sd / floor) ** 2
    assert _minimum_paired_observations(sd, floor=floor, alpha=alpha) == pytest.approx(expected)


def test_minimum_paired_observations_zero_floor_is_infinite():
    """Nothing can ever resolve an edge of zero (or less)."""
    assert _minimum_paired_observations(1.0, floor=0.0, alpha=0.05) == float("inf")
    assert _minimum_paired_observations(1.0, floor=-0.01, alpha=0.05) == float("inf")


def test_minimum_paired_observations_zero_sd_needs_no_more_data():
    """A perfectly resolved (zero-spread) population needs nothing more."""
    assert _minimum_paired_observations(0.0, floor=0.02, alpha=0.05) == 0.0


def test_minimum_paired_observations_scales_with_sd_squared():
    base = _minimum_paired_observations(1.0, floor=0.1, alpha=0.05)
    doubled = _minimum_paired_observations(2.0, floor=0.1, alpha=0.05)
    assert doubled == pytest.approx(base * 4.0)


def test_minimum_paired_observations_scales_inversely_with_floor_squared():
    base = _minimum_paired_observations(1.0, floor=0.1, alpha=0.05)
    halved_floor = _minimum_paired_observations(1.0, floor=0.05, alpha=0.05)
    assert halved_floor == pytest.approx(base * 4.0)


def test_minimum_paired_observations_stricter_alpha_needs_more_data():
    looser = _minimum_paired_observations(1.0, floor=0.1, alpha=0.05)
    stricter = _minimum_paired_observations(1.0, floor=0.1, alpha=0.005)
    assert stricter > looser


# ---------------------------------------------------------------------------
# (d) _clustered_se -- the cluster-robust standard error (generalized to
# arbitrary real values: 0/1 beat indicators, or Brier/log-loss differences)
# ---------------------------------------------------------------------------


def test_clustered_se_empty_is_zero():
    assert _clustered_se({}) == 0.0


def test_clustered_se_reduces_exactly_to_the_naive_se_for_singleton_clusters():
    """One observation per game (no clustering at all): the CR0 sandwich SE
    must reduce EXACTLY to the ordinary i.i.d. proportion SE,
    sqrt(p(1-p)/n)."""
    values_by_game = {1: [1], 2: [0], 3: [1], 4: [1]}
    n = 4
    phat = 3 / 4
    naive_se = math.sqrt(phat * (1 - phat) / n)
    assert _clustered_se(values_by_game) == pytest.approx(naive_se)


def test_clustered_se_reduces_to_the_naive_population_se_for_continuous_values():
    """The same singleton-cluster identity holds for CONTINUOUS values (the
    accuracy comparison's Brier/log-loss differences), not just 0/1s."""
    values = [0.10, -0.05, 0.30, -0.20, 0.00]
    values_by_game = {i: [v] for i, v in enumerate(values)}
    n = len(values)
    xbar = sum(values) / n
    naive_se = math.sqrt(sum((v - xbar) ** 2 for v in values) / n) / math.sqrt(n)
    assert _clustered_se(values_by_game) == pytest.approx(naive_se)


def test_clustered_se_inflates_for_perfectly_correlated_within_game_bets():
    """Two games, each contributing two IDENTICAL (perfectly correlated)
    outcomes: the clustered SE must be LARGER than the naive SE a reader
    would get by (wrongly) treating all four rows as independent."""
    values_by_game = {1: [1, 1], 2: [0, 0]}
    n = 4
    phat = 0.5
    naive_se = math.sqrt(phat * (1 - phat) / n)
    clustered = _clustered_se(values_by_game)
    assert clustered > naive_se
    # Exact hand computation: sum_g((y-phat))^2 per cluster = 1.0 + 1.0 = 2.0;
    # SE = sqrt(2.0) / 4.
    assert clustered == pytest.approx(math.sqrt(2.0) / 4)


def test_clustered_se_is_degenerate_zero_with_a_single_cluster():
    """A SINGLE game, however many observations it contributes, always
    reduces the CR0 formula to exactly zero -- its own deviations from the
    overall mean (which IS its own mean, with only one cluster) sum to
    zero by construction. This is the exact degeneracy
    _minimum_observations_clustered / _row_for / _accuracy_row_for must all
    guard against rather than trust at face value."""
    values_by_game = {777: [1, 0, 1, 1, 0, 0, 1, 0, 1, 1]}
    assert _clustered_se(values_by_game) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# (e) _minimum_observations_clustered -- the guarded, caller-facing minimum
# ---------------------------------------------------------------------------


def test_minimum_observations_clustered_none_below_two_total_observations():
    assert _minimum_observations_clustered({}, floor=0.02, alpha=0.05) is None
    assert _minimum_observations_clustered({1: [1]}, floor=0.02, alpha=0.05) is None


def test_minimum_observations_clustered_none_with_a_single_cluster():
    """However many observations, ONE game is not enough to even estimate a
    game-level spread -- this must return None (undetermined), never a
    falsely-resolved 0.0 from the degenerate _clustered_se value."""
    values_by_game = {1: [1, 0, 1, 1, 0, 1, 0, 0, 1, 1]}  # 10 obs, 1 game
    assert _minimum_observations_clustered(values_by_game, floor=0.02, alpha=0.05) is None


def test_minimum_observations_clustered_none_below_the_cluster_floor():
    """Fewer than MIN_CLUSTERS_FOR_INFERENCE distinct games -- even well
    above 1 -- must still read as undetermined, not just barely-resolved.
    An adversarial review of SIM-539 confirmed a bare '>= 2 games' guard
    was not enough: a small sample can read as perfectly resolved (zero
    uncertainty) purely by chance. This test sits one game short of the
    floor."""
    assert MIN_CLUSTERS_FOR_INFERENCE > 2, "the test below assumes a floor above 2"
    values_by_game = {i: [1 if i % 2 == 0 else 0] for i in range(MIN_CLUSTERS_FOR_INFERENCE - 1)}
    assert _minimum_observations_clustered(values_by_game, floor=0.02, alpha=0.05) is None


def test_minimum_observations_clustered_resolves_at_exactly_the_cluster_floor():
    """Exactly MIN_CLUSTERS_FOR_INFERENCE distinct games is the smallest
    input the function is designed to resolve (not return None) -- a
    boundary test so a future change to the guard's comparison operator
    (e.g. accidentally tightening `<` to `<=`) is caught."""
    n = MIN_CLUSTERS_FOR_INFERENCE
    values_by_game = {i: [1 if i % 2 == 0 else 0] for i in range(n)}
    got = _minimum_observations_clustered(values_by_game, floor=0.02, alpha=0.05)
    assert got is not None
    assert got >= 0.0


def test_minimum_observations_clustered_reduces_to_the_paired_formula_when_unclustered():
    """One observation per game: the implied per-observation sd backed out
    of clustered_se (clustered_se * sqrt(n)) must feed the SAME shared
    formula an unclustered call would use directly. An EVEN n with an exact
    half/half split keeps phat at exactly 0.5, so the comparison is exact,
    not approximate."""
    n = 24
    p = 0.5
    values_by_game = {i: [1 if i % 2 == 0 else 0] for i in range(n)}
    floor = 0.02
    alpha = 0.05
    expected = _minimum_paired_observations(math.sqrt(p * (1 - p)), floor=floor, alpha=alpha)
    got = _minimum_observations_clustered(values_by_game, floor=floor, alpha=alpha)
    assert got == pytest.approx(expected)


def test_minimum_observations_clustered_more_clustering_needs_more_observations():
    """The SAME overall rate (0.5), but grouped into exactly
    MIN_CLUSTERS_FOR_INFERENCE heavily-correlated games instead of many more
    independent ones, must ask for a LARGER minimum -- both sides clear the
    cluster-count floor, so this isolates the clustering effect itself, not
    the floor guard."""
    n_clustered_games = MIN_CLUSTERS_FOR_INFERENCE
    clustered = {
        i: ([1] * 5 if i % 2 == 0 else [0] * 5) for i in range(n_clustered_games)
    }  # n_clustered_games games, 5 identical obs each
    unclustered = {i: [1 if i % 2 == 0 else 0] for i in range(50)}  # 50 games, 1 obs each
    floor, alpha = 0.02, 0.05
    clustered_min_n = _minimum_observations_clustered(clustered, floor=floor, alpha=alpha)
    unclustered_min_n = _minimum_observations_clustered(unclustered, floor=floor, alpha=alpha)
    assert clustered_min_n is not None and unclustered_min_n is not None
    assert clustered_min_n > unclustered_min_n


def test_minimum_observations_clustered_zero_se_needs_no_more_data():
    """Every game reads exactly the same value -- a perfectly resolved
    (zero-spread) population, with enough distinct games to clear the
    cluster floor and prove it."""
    values_by_game = {i: [0.5] for i in range(MIN_CLUSTERS_FOR_INFERENCE)}
    assert _minimum_observations_clustered(values_by_game, floor=0.02, alpha=0.05) == pytest.approx(
        0.0
    )


# ---------------------------------------------------------------------------
# (f) Wiring into the legacy CLV scoreboard (_row_for / aggregate_scoreboard)
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


def test_row_for_empty_bucket_is_safe():
    row = _row_for("overall", "—", [], alpha=0.05)
    assert math.isnan(row.clustered_se)
    assert row.n_games_placed == 0
    assert row.beat_close_ci_low == pytest.approx(0.0)
    assert row.beat_close_ci_high == pytest.approx(1.0)  # widest honest interval, not [0,0]
    assert row.min_bets_recommended is None
    assert row.underpowered is True
    assert row.alpha_used == pytest.approx(0.05)


def test_row_for_single_game_is_the_widest_honest_interval():
    """Every placed bet comes from the SAME game -- one independent trial.
    The row must report the widest possible ([0, 1]) interval and refuse a
    minimum, not a falsely narrow CI built on a degenerate zero SE."""
    bets = [
        _bet("K", "prop", placed=True, beat=(i % 2 == 0), clv=0.03, edge=0.02, game_pk=1)
        for i in range(10)
    ]
    row = _row_for("K", "untrustworthy", bets, alpha=0.05)
    assert math.isnan(row.clustered_se)
    assert row.n_games_placed == 1
    assert row.beat_close_ci_low == pytest.approx(0.0)
    assert row.beat_close_ci_high == pytest.approx(1.0)
    assert row.min_bets_recommended is None
    assert row.underpowered is True


def test_row_for_below_the_cluster_floor_is_still_the_widest_interval():
    """One game short of MIN_CLUSTERS_FOR_INFERENCE: still undetermined,
    not just barely resolved."""
    n_games = MIN_CLUSTERS_FOR_INFERENCE - 1
    bets = [
        _bet("K", "prop", placed=True, beat=(i % 2 == 0), clv=0.03, edge=0.02, game_pk=i)
        for i in range(n_games)
    ]
    row = _row_for("K", "untrustworthy", bets, alpha=0.05)
    assert math.isnan(row.clustered_se)
    assert row.min_bets_recommended is None
    assert row.underpowered is True


def test_row_for_ci_is_clamped_to_zero_one():
    """A near-certain rate, with enough distinct games to clear the cluster
    floor, must not push the interval outside [0, 1]."""
    bets = [
        _bet("H", "prop", placed=True, beat=True, clv=0.5, edge=0.1, game_pk=i)
        for i in range(MIN_CLUSTERS_FOR_INFERENCE)
    ]
    row = _row_for("H", "trustworthy", bets, alpha=0.05)
    assert row.beat_close_rate == pytest.approx(1.0)
    assert 0.0 <= row.beat_close_ci_low <= row.beat_close_ci_high <= 1.0
    assert row.beat_close_ci_high == pytest.approx(1.0)


def test_row_for_ci_widens_as_alpha_is_bonferroni_corrected():
    """A stricter (Bonferroni-corrected) alpha must widen the interval, not
    narrow it -- correcting for more markets is more conservative."""
    bets = [
        _bet(
            "moneyline", "moneyline", placed=True, beat=(i % 2 == 0), clv=0.05, edge=0.02, game_pk=i
        )
        for i in range(20)
    ]
    loose = _row_for("moneyline", "loose", bets, alpha=0.05)
    strict = _row_for("moneyline", "loose", bets, alpha=0.005)
    loose_width = loose.beat_close_ci_high - loose.beat_close_ci_low
    strict_width = strict.beat_close_ci_high - strict.beat_close_ci_low
    assert strict_width > loose_width


def test_row_for_clustered_bets_need_more_than_the_naive_count():
    """The SAME overall beat-close rate (0.5), but crammed into exactly
    MIN_CLUSTERS_FOR_INFERENCE heavily-correlated games instead of many more
    separate ones, must report a WORSE (larger) minimum-bets figure --
    clustering must never make a market look MORE resolved than it is. Both
    sides clear the cluster-count floor, isolating the clustering effect."""
    n_clustered_games = MIN_CLUSTERS_FOR_INFERENCE
    clustered_bets = []
    for i in range(n_clustered_games):
        beat_value = i % 2 == 0
        clustered_bets.extend(
            _bet("K", "prop", placed=True, beat=beat_value, clv=0.03, edge=0.02, game_pk=i)
            for _ in range(5)
        )
    unclustered_bets = [
        _bet("K", "prop", placed=True, beat=(i % 2 == 0), clv=0.03, edge=0.02, game_pk=i)
        for i in range(50)
    ]
    clustered_row = _row_for("K", "untrustworthy", clustered_bets, alpha=0.05)
    unclustered_row = _row_for("K", "untrustworthy", unclustered_bets, alpha=0.05)
    assert clustered_row.clustered_se > unclustered_row.clustered_se
    assert clustered_row.min_bets_recommended is not None
    assert unclustered_row.min_bets_recommended is not None
    assert clustered_row.min_bets_recommended > unclustered_row.min_bets_recommended


def test_aggregate_scoreboard_bonferroni_corrects_by_market_rows_only():
    """`overall` uses the UNCORRECTED base alpha (it is one top-line figure,
    not one of several rows a reader scans); `by_market` rows are corrected
    by however many distinct markets this run actually produced."""
    bets = [
        _bet("moneyline", "moneyline", placed=True, beat=True, clv=0.1, edge=0.02, game_pk=1),
        _bet("total", "total", placed=True, beat=False, clv=-0.1, edge=0.02, game_pk=1),
        _bet("H", "prop", placed=True, beat=True, clv=0.05, edge=0.02, game_pk=1),
        _bet("K", "prop", placed=True, beat=False, clv=-0.05, edge=0.02, game_pk=1),
    ]
    sb = aggregate_scoreboard(bets, base_alpha=0.05)
    assert sb["overall"]["alpha_used"] == pytest.approx(0.05)
    for r in sb["by_market"]:
        assert r["alpha_used"] == pytest.approx(0.05 / 4)


def test_aggregate_scoreboard_bonferroni_denominator_is_markets_not_records():
    """Distinguishes the CORRECT denominator (distinct market rows) from a
    plausible bug (total record/bet count): 2 markets, each carrying MANY
    bets, must still correct by 2, not by the total bet count. An
    adversarial review of SIM-539 confirmed the earlier version of this
    test used a 1-bet-per-market fixture that could not tell the two
    denominators apart."""
    bets = []
    for market in ("moneyline", "H"):
        bets.extend(
            _bet(
                market,
                "moneyline" if market == "moneyline" else "prop",
                placed=True,
                beat=True,
                clv=0.05,
                edge=0.02,
                game_pk=i,
            )
            for i in range(6)
        )
    assert len(bets) == 12  # 2 markets x 6 bets each -- a wrong denominator would be 12
    sb = aggregate_scoreboard(bets, base_alpha=0.05)
    for r in sb["by_market"]:
        assert r["alpha_used"] == pytest.approx(0.05 / 2)


def test_format_scoreboard_renders_an_underpowered_row():
    """Too few distinct games: minBets must render 'n/a' and pwr 'UNDERPWR',
    not crash and not a misleading finite number."""
    bets = [
        _bet(
            "moneyline", "moneyline", placed=True, beat=(i % 2 == 0), clv=0.1, edge=0.02, game_pk=i
        )
        for i in range(3)
    ]
    sb = aggregate_scoreboard(bets)
    text = format_scoreboard(
        sb,
        params={
            "seasons": [2024],
            "iterations": 100,
            "markets": "game",
            "min_edge": 0.0,
            "base_seed": 0,
        },
    )
    assert "95% range" in text
    assert "minBets" in text
    assert "SIM-539" in text
    assert "n/a" in text
    assert "UNDERPWR" in text


def test_format_scoreboard_renders_a_resolved_row():
    """Enough distinct games with an exactly-constant rate: minBets must
    render a real (finite) number and pwr 'ok'."""
    bets = [
        _bet("moneyline", "moneyline", placed=True, beat=True, clv=0.1, edge=0.02, game_pk=i)
        for i in range(MIN_CLUSTERS_FOR_INFERENCE)
    ]
    sb = aggregate_scoreboard(bets)
    text = format_scoreboard(
        sb,
        params={
            "seasons": [2024],
            "iterations": 100,
            "markets": "game",
            "min_edge": 0.0,
            "base_seed": 0,
        },
    )
    assert "ok" in text
    # minBets rendered as a plain integer-looking token, not "n/a", for this row.
    by_market_lines = text.splitlines()[-3:]
    assert any("n/a" not in line and "moneyline" in line for line in by_market_lines)


def test_format_scoreboard_renders_the_zero_bets_row_without_crashing():
    """The scoreboard's own analog of the zero-games crash found in
    format_accuracy_comparison: confirms beat_close_rate / mean_clv_prob /
    mean_model_edge are genuinely always real floats (never nan) even at
    zero bets, so this table never needed the same None-guard -- proven,
    not just assumed."""
    sb = aggregate_scoreboard([])
    text = format_scoreboard(
        sb,
        params={
            "seasons": [2024],
            "iterations": 100,
            "markets": "game",
            "min_edge": 0.0,
            "base_seed": 0,
        },
    )
    assert "n/a" in text
    assert "UNDERPWR" in text


def test_json_safe_replaces_nan_and_infinite_with_none():
    d = {
        "a": float("nan"),
        "b": float("inf"),
        "c": float("-inf"),
        "d": 1.5,
        "e": None,
        "f": "text",
        "g": 0,
    }
    safe = _json_safe(d)
    assert safe["a"] is None
    assert safe["b"] is None
    assert safe["c"] is None
    assert safe["d"] == 1.5
    assert safe["e"] is None
    assert safe["f"] == "text"
    assert safe["g"] == 0


def test_row_for_to_jsonable_is_valid_json_even_when_underpowered():
    """The exact case an adversarial review flagged: a degenerate
    (too-few-games) row's clustered_se is nan, which json.dumps would
    otherwise emit as the illegal bare token NaN."""
    import json

    bets = [
        _bet("K", "prop", placed=True, beat=True, clv=0.03, edge=0.02, game_pk=1) for _ in range(5)
    ]
    row = _row_for("K", "untrustworthy", bets, alpha=0.05)
    d = row.to_jsonable()
    assert d["clustered_se"] is None
    encoded = json.dumps(d)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded
    json.loads(encoded)  # round-trips through a strict parser


# ---------------------------------------------------------------------------
# (g) Wiring into the accuracy comparison (_accuracy_row_for / aggregate)
# ---------------------------------------------------------------------------


def _rec(
    market: str,
    market_type: str,
    sim_prob: float,
    market_prob: float,
    outcome: int,
    game_pk: int = 1,
):
    return AccuracyRecord(
        game_pk=game_pk,
        market=market,
        market_type=market_type,
        sim_prob=sim_prob,
        market_prob=market_prob,
        outcome=outcome,
    )


def test_accuracy_row_for_empty_is_infinitely_underpowered():
    row = _accuracy_row_for("overall", "—", [], n_bootstrap=100, seed=1, alpha=0.05)
    assert row.n_games == 0
    assert row.brier_min_n == float("inf")
    assert row.log_loss_min_n == float("inf")
    assert row.min_n_recommended == float("inf")
    assert row.underpowered is True


def test_accuracy_row_for_single_game_is_infinitely_underpowered():
    """Every record comes from the SAME game -- one independent trial, not
    enough to even estimate a game-clustered spread, regardless of how many
    props/markets that one game contributed. Checks brier_min_n and
    log_loss_min_n INDIVIDUALLY, not just their max, so a regression
    isolated to one of the two would still be caught."""
    records = [_rec("K", "prop", 0.6 + 0.01 * i, 0.5, i % 2, game_pk=1) for i in range(10)]
    row = _accuracy_row_for("K", "untrustworthy", records, n_bootstrap=100, seed=1, alpha=0.05)
    assert row.n_games == 1
    assert row.brier_min_n == float("inf")
    assert row.log_loss_min_n == float("inf")
    assert row.min_n_recommended == float("inf")
    assert row.underpowered is True


def test_accuracy_row_for_below_the_cluster_floor_is_still_underpowered():
    n_games = MIN_CLUSTERS_FOR_INFERENCE - 1
    records = [_rec("moneyline", "moneyline", 0.6, 0.5, 1, game_pk=i) for i in range(n_games)]
    row = _accuracy_row_for("moneyline", "loose", records, n_bootstrap=100, seed=1, alpha=0.05)
    assert row.min_n_recommended == float("inf")
    assert row.underpowered is True


def test_accuracy_row_for_zero_spread_is_never_underpowered():
    """Every record has the IDENTICAL sim/market probability and outcome, so
    the paired difference has zero spread -- a perfectly resolved read needs
    no more data at all, given enough distinct games to clear the cluster
    floor."""
    records = [
        _rec("moneyline", "moneyline", 0.9, 0.5, 1, game_pk=i)
        for i in range(MIN_CLUSTERS_FOR_INFERENCE)
    ]
    row = _accuracy_row_for("moneyline", "loose", records, n_bootstrap=100, seed=1, alpha=0.05)
    assert row.min_n_recommended == pytest.approx(0.0)
    assert row.underpowered is False


def test_accuracy_row_for_min_n_matches_the_shared_clustered_formula():
    """The row's own brier_min_n must equal calling the shared, game-grouped
    formula directly with THIS row's own observed diffs -- a wiring/
    consistency check, not a re-derivation. One distinct game per record,
    so the clustered computation reduces to the plain per-observation one."""
    rng = np.random.default_rng(11)
    sim_p = np.clip(0.5 + rng.normal(scale=0.05, size=40), 0.05, 0.95)
    market_p = np.clip(0.5 + rng.normal(scale=0.05, size=40), 0.05, 0.95)
    y = (rng.random(40) < 0.5).astype(np.int64)
    records = [
        _rec("moneyline", "moneyline", float(sp), float(mp), int(yy), game_pk=i)
        for i, (sp, mp, yy) in enumerate(zip(sim_p, market_p, y, strict=True))
    ]
    row = _accuracy_row_for("moneyline", "loose", records, n_bootstrap=100, seed=1, alpha=0.05)

    brier_diffs = (sim_p - y) ** 2 - (market_p - y) ** 2
    brier_by_game = {i: [float(d)] for i, d in enumerate(brier_diffs)}
    floor = MIN_DETECTABLE_EDGE / DETECTION_MARGIN
    expected_min_n = _minimum_observations_clustered(brier_by_game, floor=floor, alpha=0.05)
    assert row.brier_min_n == pytest.approx(expected_min_n)
    assert row.min_n_recommended >= row.brier_min_n
    assert row.min_n_recommended >= row.log_loss_min_n


def test_accuracy_row_for_clustered_records_need_more_than_the_naive_count():
    """The SAME overall pattern, split into exactly
    MIN_CLUSTERS_FOR_INFERENCE heavily-correlated games instead of many more
    independent ones, must report a WORSE (larger) minimum -- several props
    from one start are not several independent trials. Both sides clear the
    cluster-count floor, isolating the clustering effect from the guard."""
    n_clustered_games = MIN_CLUSTERS_FOR_INFERENCE
    clustered_records = []
    for i in range(n_clustered_games):
        sim_p = 0.7 if i % 2 == 0 else 0.3
        clustered_records.extend(_rec("H", "prop", sim_p, 0.5, 1, game_pk=i) for _ in range(5))
    unclustered_records = [
        _rec("H", "prop", 0.7 if i % 2 == 0 else 0.3, 0.5, 1, game_pk=i) for i in range(50)
    ]
    clustered_row = _accuracy_row_for(
        "H", "trustworthy", clustered_records, n_bootstrap=100, seed=1, alpha=0.05
    )
    unclustered_row = _accuracy_row_for(
        "H", "trustworthy", unclustered_records, n_bootstrap=100, seed=1, alpha=0.05
    )
    assert clustered_row.brier_min_n > unclustered_row.brier_min_n


def test_aggregate_accuracy_comparison_bonferroni_corrects_by_market_rows_only():
    records = [
        _rec("moneyline", "moneyline", 0.6, 0.5, 1, game_pk=1),
        _rec("K", "prop", 0.6, 0.5, 1, game_pk=1),
        _rec("H", "prop", 0.6, 0.5, 0, game_pk=1),
    ]
    comparison = aggregate_accuracy_comparison(records, n_bootstrap=100, seed=1, base_alpha=0.05)
    assert comparison["overall"]["alpha_used"] == pytest.approx(0.05)
    for r in comparison["by_market"]:
        assert r["alpha_used"] == pytest.approx(0.05 / 3)


def test_aggregate_accuracy_comparison_bonferroni_denominator_is_markets_not_records():
    """Same distinction as the scoreboard's version: 2 markets, each with
    MANY records, must correct by 2, not by the total record count."""
    records = []
    for market in ("moneyline", "H"):
        market_type = "moneyline" if market == "moneyline" else "prop"
        records.extend(_rec(market, market_type, 0.6, 0.5, 1, game_pk=i) for i in range(6))
    assert len(records) == 12
    comparison = aggregate_accuracy_comparison(records, n_bootstrap=100, seed=1, base_alpha=0.05)
    for r in comparison["by_market"]:
        assert r["alpha_used"] == pytest.approx(0.05 / 2)


def test_format_accuracy_comparison_renders_an_underpowered_row():
    records = [_rec("moneyline", "moneyline", 0.6, 0.5, i % 2, game_pk=1) for i in range(5)]
    comparison = aggregate_accuracy_comparison(records, n_bootstrap=100, seed=1)
    text = format_accuracy_comparison(
        comparison,
        params={
            "seasons": [2024],
            "iterations": 100,
            "markets": "game",
            "base_seed": 0,
            "calibration_applied": True,
        },
    )
    assert "minN" in text
    assert "SIM-539" in text
    assert "inf" in text
    assert "UNDERPWR" in text


def test_format_accuracy_comparison_renders_a_resolved_row():
    records = [
        _rec("moneyline", "moneyline", 0.6, 0.5, 1, game_pk=i)
        for i in range(MIN_CLUSTERS_FOR_INFERENCE)
    ]
    comparison = aggregate_accuracy_comparison(records, n_bootstrap=100, seed=1)
    text = format_accuracy_comparison(
        comparison,
        params={
            "seasons": [2024],
            "iterations": 100,
            "markets": "game",
            "base_seed": 0,
            "calibration_applied": True,
        },
    )
    assert "ok" in text


def test_format_accuracy_comparison_renders_the_zero_games_row_without_crashing():
    """A run over ZERO completed games ("no completed games found" — a real,
    common outcome, e.g. an empty --max-games slate or an --seasons range
    with no Final games yet) makes every one of sim_brier / brier_diff_mean
    / the CI bounds nan for the 'overall' row, and _json_safe turns each
    into None before this ever reaches the formatter. This exact path
    crashed in an integration test (test_clv_backtest_runs_when_park_
    factors_are_available / test_clv_backtest_honours_the_explicit_opt_out
    in test_sim449_sim_kwargs.py) before _fmt_or_na existed — this is the
    faster, unit-level regression lock for the same scenario."""
    comparison = aggregate_accuracy_comparison([], n_bootstrap=100, seed=1)
    text = format_accuracy_comparison(
        comparison,
        params={
            "seasons": [2024],
            "iterations": 100,
            "markets": "game",
            "base_seed": 0,
            "calibration_applied": True,
        },
    )
    assert "n/a" in text
    assert "inf" in text


def test_accuracy_row_for_to_jsonable_is_valid_json_even_when_underpowered():
    import json

    records = [_rec("K", "prop", 0.6, 0.5, 1, game_pk=1) for _ in range(5)]
    row = _accuracy_row_for("K", "untrustworthy", records, n_bootstrap=100, seed=1, alpha=0.05)
    d = row.to_jsonable()
    assert d["brier_min_n"] is None
    assert d["log_loss_min_n"] is None
    assert d["min_n_recommended"] is None
    encoded = json.dumps(d)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded
    json.loads(encoded)


# ---------------------------------------------------------------------------
# (h) DETECTION_MARGIN parity with tests/acceptance/bands.py
# ---------------------------------------------------------------------------


def test_detection_margin_matches_bands_py():
    """clv_backtest.py re-declares DETECTION_MARGIN rather than importing it
    (tests/ and scripts/ are separate deployables), so nothing else keeps
    the two in sync. An adversarial review of SIM-539 confirmed no test
    enforced this; this one does, so a future change to either constant
    without the other fails loudly here rather than silently diverging."""
    from tests.acceptance import bands

    assert DETECTION_MARGIN == bands.DETECTION_MARGIN
