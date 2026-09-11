"""
tests/unit/test_sim538_accuracy_comparison.py
===============================================
Unit tests for the SIM-538 sim-vs-closing-line accuracy comparison
(``scripts/clv_backtest.py``) — the platform's new headline market-accuracy
metric, which replaces Closing Line Value (CLV) as the gold standard.

What SIM-538 does, in plain words: for every game or player prop with a
closing betting price, compare the simulator's own probability and the
closing line's probability (bookmaker margin removed) against what actually
happened. Both are scored with a proper scoring rule — Brier score and log
loss — where a forecaster cannot improve its score by hedging, only by being
more accurate. The comparison is PAIRED (same game, same real outcome), and
always on a FIXED reference side (home / over), never the side the model
would have bet — so the read is not biased toward games the model disagreed
with the market on.

These tests run with NO DB and NO real sim: pure functions
(``_closing_prices``, ``_bootstrap_paired_diff_ci``, ``_accuracy_row_for``,
``aggregate_accuracy_comparison``, ``format_accuracy_comparison``) get
synthetic inputs directly; ``score_game_accuracy`` / ``score_prop_accuracy``
get synthetic ``WinProbability`` / ``GameSimSummary`` / ``PropDistributionSet``
objects built the same way ``tests/unit/test_betting_sim339.py`` and
``tests/unit/test_betting_sim367.py`` do, so no live sim run or DB is needed.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np
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

from simulation.prop_distributions import PropDistribution, PropDistributionSet  # noqa: E402
from simulation.results import ConfidenceInterval, GameSimSummary  # noqa: E402
from simulation.win_probability import TieHandling, WinProbability  # noqa: E402

AccuracyRecord = clv_backtest.AccuracyRecord
ClosingPrices = clv_backtest.ClosingPrices
_closing_prices = clv_backtest._closing_prices
score_game_accuracy = clv_backtest.score_game_accuracy
score_prop_accuracy = clv_backtest.score_prop_accuracy
_ACCURACY_SCORED_PROPS = clv_backtest._ACCURACY_SCORED_PROPS
_bootstrap_paired_diff_ci = clv_backtest._bootstrap_paired_diff_ci
_accuracy_row_for = clv_backtest._accuracy_row_for
aggregate_accuracy_comparison = clv_backtest.aggregate_accuracy_comparison
format_accuracy_comparison = clv_backtest.format_accuracy_comparison


# ---------------------------------------------------------------------------
# Synthetic-data helpers (mirror test_betting_sim339.py / test_betting_sim367.py)
# ---------------------------------------------------------------------------


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


def _summary_from_totals_and_margins(totals, margins) -> GameSimSummary:
    """A synthetic GameSimSummary carrying injected ``total_scores`` and a
    ``home_scores - away_scores`` margin (for the total and run-line accuracy
    scoring, both of which read raw per-iteration arrays off the summary)."""
    totals = np.asarray(totals, dtype=np.int64)
    margins = np.asarray(margins, dtype=np.int64)
    assert totals.size == margins.size
    # away = (total - margin) / 2, home = away + margin -- both must be
    # non-negative integers, so offset until they are.
    away = (totals - margins) // 2
    home = away + margins
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


def _dist_from_samples(player_id: int, prop: str, samples) -> PropDistribution:
    return PropDistribution.from_samples(player_id=player_id, prop=prop, samples=samples)


def _pset(*dists: PropDistribution) -> PropDistributionSet:
    by_player: dict[int, dict[str, PropDistribution]] = {}
    for d in dists:
        by_player.setdefault(d.player_id, {})[d.prop] = d
    return PropDistributionSet(n_iterations=1000, by_player=by_player)


#: A varied (non-constant) totals / margins sample so every sim probability
#: computed off it lands STRICTLY inside (0, 1) -- prob_to_american (used
#: inside every EdgeReport) raises on an exact 0.0 or 1.0, so a constant
#: sample would make score_game_accuracy silently swallow the record as
#: "degenerate" instead of exercising the scoring path under test.
_VARIED_TOTALS = [7, 8, 8, 9, 9, 9, 10, 10, 11, 12]
_VARIED_MARGINS = [0, 1, 1, 2, 2, 2, 3, 3, 4, 5]


def _odds_closing_only(
    market_type: str, side_col: str, other_col: str, line_col: str | None, *, side, other, line=None
) -> dict:
    """Build an odds dict with ONLY a closing row for one market_type."""
    row = {side_col: side, other_col: other}
    if line_col is not None:
        row[line_col] = line
    return {market_type: {"closing": row}}


# ---------------------------------------------------------------------------
# (a) _closing_prices -- reads the CLOSING row only, no opening row required
# ---------------------------------------------------------------------------


def test_closing_prices_reads_only_the_closing_row():
    """The returned line is the CLOSING row's own line, never the opening
    row's -- this is the SIM-538 fix for the legacy step's "cross-line" gap
    (_game_prices reads a market's line from the OPENING row only)."""
    odds = {
        "total": {
            "opening": {"over_ml": -110.0, "under_ml": -110.0, "total_line": 7.5},
            "closing": {"over_ml": -120.0, "under_ml": 100.0, "total_line": 8.5},
        }
    }
    cp = _closing_prices(odds, "total", "over_ml", "under_ml", "total_line")
    assert cp == ClosingPrices(side=-120.0, other=100.0, line=8.5)


def test_closing_prices_needs_no_opening_row_at_all():
    """Unlike the legacy _game_prices, a market with ONLY a closing row is
    still scoreable."""
    odds = _odds_closing_only("moneyline", "home_ml", "away_ml", None, side=-150.0, other=130.0)
    cp = _closing_prices(odds, "moneyline", "home_ml", "away_ml", None)
    assert cp == ClosingPrices(side=-150.0, other=130.0, line=None)


def test_closing_prices_none_when_no_closing_row():
    odds = {"total": {"opening": {"over_ml": -110.0, "under_ml": -110.0, "total_line": 7.5}}}
    assert _closing_prices(odds, "total", "over_ml", "under_ml", "total_line") is None


def test_closing_prices_none_when_market_type_absent():
    assert _closing_prices({}, "runline", "home_spread_ml", "away_spread_ml", "home_spread") is None


def test_closing_prices_none_when_a_price_is_null():
    odds = {"moneyline": {"closing": {"home_ml": -150.0, "away_ml": None}}}
    assert _closing_prices(odds, "moneyline", "home_ml", "away_ml", None) is None


# ---------------------------------------------------------------------------
# (b) AccuracyRecord round-trip (the picklable dict a parallel worker returns)
# ---------------------------------------------------------------------------


def test_accuracy_record_to_from_jsonable_roundtrip():
    rec = AccuracyRecord(
        game_pk=12345,
        market="K",
        market_type="prop",
        sim_prob=0.62,
        market_prob=0.55,
        outcome=1,
        player_id=42,
    )
    d = rec.to_jsonable()
    assert d == {
        "game_pk": 12345,
        "market": "K",
        "market_type": "prop",
        "sim_prob": 0.62,
        "market_prob": 0.55,
        "outcome": 1,
        "player_id": 42,
    }
    assert AccuracyRecord.from_jsonable(d) == rec


# ---------------------------------------------------------------------------
# (c) score_game_accuracy -- moneyline / total / runline on the FIXED side
# ---------------------------------------------------------------------------


def test_score_game_accuracy_moneyline_home_win():
    """Sim says home 65%; closing moneyline de-vigs to something else; the
    real game had the home team win -> outcome=1, sim_prob==0.65 exactly."""
    wp = _win_prob(0.65)
    summary = _summary_from_totals_and_margins(totals=[8] * 10, margins=[2] * 10)
    odds = _odds_closing_only("moneyline", "home_ml", "away_ml", None, side=-140.0, other=120.0)
    recs = score_game_accuracy(1, wp, summary, odds, home_score=5, away_score=3)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.market == "moneyline"
    assert rec.market_type == "moneyline"
    assert rec.sim_prob == pytest.approx(0.65)
    assert rec.outcome == 1
    assert 0.0 < rec.market_prob < 1.0


def test_score_game_accuracy_moneyline_home_loss():
    wp = _win_prob(0.65)
    summary = _summary_from_totals_and_margins(totals=[8] * 10, margins=[2] * 10)
    odds = _odds_closing_only("moneyline", "home_ml", "away_ml", None, side=-140.0, other=120.0)
    recs = score_game_accuracy(1, wp, summary, odds, home_score=2, away_score=6)
    assert len(recs) == 1
    assert recs[0].outcome == 0


def test_score_game_accuracy_moneyline_skipped_without_closing_price():
    wp = _win_prob(0.65)
    summary = _summary_from_totals_and_margins(totals=[8] * 10, margins=[2] * 10)
    recs = score_game_accuracy(1, wp, summary, {}, home_score=5, away_score=3)
    assert recs == []


def test_score_game_accuracy_total_over_and_under():
    wp = _win_prob(0.5)
    summary = _summary_from_totals_and_margins(totals=_VARIED_TOTALS, margins=_VARIED_MARGINS)
    odds = _odds_closing_only(
        "total", "over_ml", "under_ml", "total_line", side=-110.0, other=-110.0, line=8.5
    )
    over_recs = score_game_accuracy(1, wp, summary, odds, home_score=6, away_score=4)  # 10 > 8.5
    assert len(over_recs) == 1
    total_rec = [r for r in over_recs if r.market == "total"][0]
    assert total_rec.outcome == 1

    under_recs = score_game_accuracy(1, wp, summary, odds, home_score=4, away_score=3)  # 7 < 8.5
    total_rec2 = [r for r in under_recs if r.market == "total"][0]
    assert total_rec2.outcome == 0


def test_score_game_accuracy_total_push_is_excluded():
    """An INTEGER closing line the real total lands exactly on has no
    well-defined over/under label -- it contributes no record."""
    wp = _win_prob(0.5)
    summary = _summary_from_totals_and_margins(totals=[9] * 10, margins=[0] * 10)
    odds = _odds_closing_only(
        "total", "over_ml", "under_ml", "total_line", side=-110.0, other=-110.0, line=9.0
    )
    recs = score_game_accuracy(1, wp, summary, odds, home_score=5, away_score=4)  # 9 == 9.0 push
    assert [r for r in recs if r.market == "total"] == []


def test_score_game_accuracy_runline_cover_and_push():
    wp = _win_prob(0.5)
    summary = _summary_from_totals_and_margins(totals=_VARIED_TOTALS, margins=_VARIED_MARGINS)
    odds = _odds_closing_only(
        "runline",
        "home_spread_ml",
        "away_spread_ml",
        "home_spread",
        side=130.0,
        other=-150.0,
        line=-1.0,
    )
    # margin=3, threshold=-line=1.0 -> home covers (3 > 1.0).
    cover_recs = score_game_accuracy(1, wp, summary, odds, home_score=5, away_score=2)
    rl = [r for r in cover_recs if r.market == "runline"][0]
    assert rl.outcome == 1

    # margin=1 == threshold=1.0 -> push, excluded.
    push_recs = score_game_accuracy(1, wp, summary, odds, home_score=4, away_score=3)
    assert [r for r in push_recs if r.market == "runline"] == []


def test_score_game_accuracy_scores_all_three_markets_together():
    wp = _win_prob(0.55)
    summary = _summary_from_totals_and_margins(totals=_VARIED_TOTALS, margins=_VARIED_MARGINS)
    odds = {
        **_odds_closing_only("moneyline", "home_ml", "away_ml", None, side=-130.0, other=110.0),
        **_odds_closing_only(
            "total", "over_ml", "under_ml", "total_line", side=-105.0, other=-115.0, line=8.5
        ),
        **_odds_closing_only(
            "runline",
            "home_spread_ml",
            "away_spread_ml",
            "home_spread",
            side=140.0,
            other=-160.0,
            line=-1.5,
        ),
    }
    recs = score_game_accuracy(1, wp, summary, odds, home_score=5, away_score=3)
    assert {r.market for r in recs} == {"moneyline", "total", "runline"}


# ---------------------------------------------------------------------------
# (d) score_prop_accuracy -- H/HR/TB/K/BB on the OVER side, RBI/ER excluded
# ---------------------------------------------------------------------------


def _prop_odds_row(odds_stat: str, player_id: int, *, close_over, close_under, line) -> dict:
    return {
        (player_id, odds_stat): {
            "closing": {"over_ml": close_over, "under_ml": close_under, "line": line}
        }
    }


def test_score_prop_accuracy_batter_hits_over_and_under():
    dist = _dist_from_samples(7, "H", [0, 1, 1, 2, 2, 2, 3, 3, 3, 3])
    pset = _pset(dist)
    prop_odds = _prop_odds_row("hits", 7, close_over=110.0, close_under=-130.0, line=1.5)

    over_recs = score_prop_accuracy(1, pset, prop_odds, {7: {"H": 2}}, {})
    assert len(over_recs) == 1
    assert over_recs[0].market == "H"
    assert over_recs[0].market_type == "prop"
    assert over_recs[0].player_id == 7
    assert over_recs[0].outcome == 1  # actual 2 > line 1.5

    under_recs = score_prop_accuracy(1, pset, prop_odds, {7: {"H": 1}}, {})
    assert under_recs[0].outcome == 0  # actual 1 < line 1.5


def test_score_prop_accuracy_pitcher_strikeouts():
    dist = _dist_from_samples(9, "K", [3, 4, 5, 5, 6, 6, 6, 7, 8, 9])
    pset = _pset(dist)
    prop_odds = _prop_odds_row("strikeouts", 9, close_over=-115.0, close_under=-105.0, line=5.5)
    recs = score_prop_accuracy(1, pset, prop_odds, {}, {9: {"K": 6}})
    assert len(recs) == 1
    assert recs[0].market == "K"
    assert recs[0].outcome == 1  # 6 > 5.5


def test_score_prop_accuracy_excludes_rbi_and_er():
    """RBI and ER have no reliable per-PA-event ground truth and are not
    scored, even when the odds row and a PropDistribution both exist."""
    dist_rbi = _dist_from_samples(7, "RBI", [0, 1, 1, 2])
    dist_er = _dist_from_samples(9, "ER", [1, 2, 2, 3])
    pset = _pset(dist_rbi, dist_er)
    prop_odds = {
        **_prop_odds_row("rbis", 7, close_over=100.0, close_under=-120.0, line=0.5),
        **_prop_odds_row("earned_runs", 9, close_over=100.0, close_under=-120.0, line=1.5),
    }
    recs = score_prop_accuracy(1, pset, prop_odds, {7: {"RBI": 1}}, {9: {"ER": 2}})
    assert recs == []
    assert {"RBI", "ER"} & _ACCURACY_SCORED_PROPS == set()


def test_score_prop_accuracy_skips_when_no_real_outcome_recorded():
    """A player who has no PA-event-derived actual this game (didn't play) is
    skipped, not scored against a guessed ground truth."""
    dist = _dist_from_samples(7, "H", [0, 1, 1, 2, 2, 2, 3, 3, 3, 3])
    pset = _pset(dist)
    prop_odds = _prop_odds_row("hits", 7, close_over=110.0, close_under=-130.0, line=1.5)
    recs = score_prop_accuracy(1, pset, prop_odds, {}, {})  # no batter_actuals entry
    assert recs == []


def test_score_prop_accuracy_skips_push():
    dist = _dist_from_samples(7, "H", [0, 1, 1, 2, 2, 2, 3, 3, 3, 3])
    pset = _pset(dist)
    prop_odds = _prop_odds_row("hits", 7, close_over=110.0, close_under=-130.0, line=2.0)
    recs = score_prop_accuracy(1, pset, prop_odds, {7: {"H": 2}}, {})  # actual == line
    assert recs == []


def test_score_prop_accuracy_skips_when_no_model_distribution():
    """A player with an odds row but no model PropDistribution (never
    appeared in the sim's boxscores) is skipped."""
    prop_odds = _prop_odds_row("hits", 7, close_over=110.0, close_under=-130.0, line=1.5)
    recs = score_prop_accuracy(1, _pset(), prop_odds, {7: {"H": 2}}, {})
    assert recs == []


# ---------------------------------------------------------------------------
# (e) _bootstrap_paired_diff_ci -- a CLUSTER bootstrap by game, deterministic,
# degenerate on < 2 observations or < 2 distinct games
# ---------------------------------------------------------------------------


def test_bootstrap_ci_empty_is_nan_degenerate():
    lo, hi = _bootstrap_paired_diff_ci(
        np.array([], dtype=np.float64), np.array([], dtype=np.int64), n_bootstrap=100, seed=1
    )
    assert np.isnan(lo) and np.isnan(hi)


def test_bootstrap_ci_single_observation_is_degenerate_at_the_point():
    lo, hi = _bootstrap_paired_diff_ci(
        np.array([0.25]), np.array([1], dtype=np.int64), n_bootstrap=100, seed=1
    )
    assert lo == pytest.approx(0.25)
    assert hi == pytest.approx(0.25)


def test_bootstrap_ci_constant_diffs_collapses_to_the_constant():
    """Every possible resample of a CONSTANT array has the same mean, so the
    95% interval collapses exactly to that constant -- a strong, deterministic
    check that needs no statistical hand-waving. One distinct game per
    observation, so there is no clustering to obscure the point."""
    diffs = np.full(50, -0.10)
    game_pks = np.arange(50, dtype=np.int64)
    lo, hi = _bootstrap_paired_diff_ci(diffs, game_pks, n_bootstrap=500, seed=7)
    assert lo == pytest.approx(-0.10, abs=1e-9)
    assert hi == pytest.approx(-0.10, abs=1e-9)


def test_bootstrap_ci_is_reproducible_given_the_same_seed():
    rng = np.random.default_rng(3)
    diffs = rng.normal(loc=-0.05, scale=0.2, size=40)
    game_pks = np.arange(40, dtype=np.int64)
    r1 = _bootstrap_paired_diff_ci(diffs, game_pks, n_bootstrap=1000, seed=99)
    r2 = _bootstrap_paired_diff_ci(diffs, game_pks, n_bootstrap=1000, seed=99)
    assert r1 == r2


def test_bootstrap_ci_low_le_high():
    rng = np.random.default_rng(11)
    diffs = rng.normal(loc=0.0, scale=0.3, size=30)
    game_pks = np.arange(30, dtype=np.int64)
    lo, hi = _bootstrap_paired_diff_ci(diffs, game_pks, n_bootstrap=500, seed=42)
    assert lo <= hi


def test_bootstrap_ci_matches_a_flat_bootstrap_when_one_observation_per_game():
    """SIM-538 fix: the cluster bootstrap resamples GAMES, not raw rows (see
    the function's own docstring). When every game contributes exactly one
    observation -- true for a single-player market like moneyline -- that
    reduces EXACTLY to the plain per-observation bootstrap the code used to
    run. Prove it by reimplementing that plain bootstrap by hand, with the
    identical seed, and checking the two intervals match bit-for-bit."""
    rng = np.random.default_rng(5)
    diffs = rng.normal(loc=0.02, scale=0.15, size=25)
    game_pks = np.arange(25, dtype=np.int64)  # one distinct game per row
    lo, hi = _bootstrap_paired_diff_ci(diffs, game_pks, n_bootstrap=1000, seed=123)

    flat_rng = np.random.default_rng(123)
    idx = flat_rng.integers(0, 25, size=(1000, 25))
    means = diffs[idx].mean(axis=1)
    exp_lo, exp_hi = np.percentile(means, [2.5, 97.5])
    assert lo == pytest.approx(float(exp_lo))
    assert hi == pytest.approx(float(exp_hi))


def test_bootstrap_ci_is_degenerate_when_every_observation_shares_one_game():
    """The sharpest demonstration of the clustering fix: 20 observations that
    all come from the SAME game (e.g. 20 props from one start) carry exactly
    ONE independent trial's worth of information. There is nothing to
    resample BETWEEN games, so the honest answer is a degenerate point
    estimate at the mean -- not the artificially tight interval a
    per-observation bootstrap would have reported by treating 20 correlated
    rows as 20 independent games."""
    rng = np.random.default_rng(9)
    diffs = rng.normal(loc=-0.03, scale=0.2, size=20)
    game_pks = np.full(20, 777, dtype=np.int64)  # every row is the SAME game
    lo, hi = _bootstrap_paired_diff_ci(diffs, game_pks, n_bootstrap=1000, seed=1)
    expected = float(diffs.mean())
    assert lo == pytest.approx(expected)
    assert hi == pytest.approx(expected)


def test_bootstrap_ci_is_wider_when_the_same_data_clusters_into_fewer_games():
    """The SAME 20 raw diff values, correctly grouped into 4 real games (5
    observations each -- one start's worth of props, say), give a WIDER
    (more honest) interval than treating those same 20 values as if they
    came from 20 independent games. This is exactly the harm an adversarial
    review of SIM-538 found: pretending 5 correlated same-game rows are 5
    independent trials understates uncertainty.

    The 4 "games" here are built with a LARGE gap between their own means
    (-0.30 .. +0.30) and only tiny within-game noise, so the result the
    between-game spread carries almost all the real variance -- exactly the
    situation clustering matters for, and the one place a plain (wrong)
    per-observation bootstrap most understates it: resampling 20 individual
    rows nearly always draws a representative mix of all 4 games, averaging
    the spread away, while the correct game-level resample can by chance
    draw mostly-low or mostly-high games, which a bootstrap over only 4
    independent games must be able to show.
    """
    rng = np.random.default_rng(13)
    game_means = np.array([-0.30, -0.10, 0.10, 0.30])
    within_game_noise = rng.normal(scale=0.01, size=(4, 5))
    diffs = (game_means[:, None] + within_game_noise).ravel()  # shape (20,)

    # The TRUE grouping: 4 real games, 5 correlated observations each.
    real_games = np.repeat(np.arange(4, dtype=np.int64), 5)
    # The WRONG grouping this fix prevents: every row treated as its own
    # independent game, discarding the real correlation structure.
    fake_distinct_games = np.arange(20, dtype=np.int64)

    lo_real, hi_real = _bootstrap_paired_diff_ci(diffs, real_games, n_bootstrap=2000, seed=55)
    lo_fake, hi_fake = _bootstrap_paired_diff_ci(
        diffs, fake_distinct_games, n_bootstrap=2000, seed=55
    )

    assert (hi_real - lo_real) > (hi_fake - lo_fake)


# ---------------------------------------------------------------------------
# (f) _accuracy_row_for / aggregate_accuracy_comparison -- the roll-up
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


def test_accuracy_row_for_empty_is_safe_nan():
    row = _accuracy_row_for("overall", "—", [], n_bootstrap=100, seed=1)
    assert row.n == 0
    assert np.isnan(row.sim_brier)
    assert np.isnan(row.market_brier)


def test_accuracy_row_for_sim_more_accurate_gives_negative_diff():
    """Sim is spot-on (prob 0.9 when the event happened, every time); the
    market is much less sure (0.5) -- the sim's Brier/log-loss score must be
    LOWER (better), so the diff is negative and the CI does not cross zero."""
    records = [_rec("moneyline", "moneyline", 0.9, 0.5, 1, game_pk=i) for i in range(20)]
    row = _accuracy_row_for("moneyline", "loose", records, n_bootstrap=1000, seed=5)
    assert row.n == 20
    assert row.sim_brier < row.market_brier
    assert row.brier_diff_mean < 0.0
    assert row.brier_diff_ci_high < 0.0  # the whole interval is negative
    assert row.sim_log_loss < row.market_log_loss
    assert row.log_loss_diff_mean < 0.0


def test_accuracy_row_for_market_more_accurate_gives_positive_diff():
    records = [_rec("moneyline", "moneyline", 0.5, 0.9, 1, game_pk=i) for i in range(20)]
    row = _accuracy_row_for("moneyline", "loose", records, n_bootstrap=1000, seed=5)
    assert row.brier_diff_mean > 0.0
    assert row.brier_diff_ci_low > 0.0


def test_aggregate_accuracy_comparison_overall_and_by_market():
    records = [
        _rec("moneyline", "moneyline", 0.9, 0.5, 1, game_pk=1),
        _rec("moneyline", "moneyline", 0.1, 0.5, 0, game_pk=2),
        _rec("K", "prop", 0.7, 0.6, 1, game_pk=1),
        _rec("H", "prop", 0.4, 0.5, 0, game_pk=1),
    ]
    comparison = aggregate_accuracy_comparison(records, n_bootstrap=200, seed=538)
    assert comparison["overall"]["n"] == 4
    by_market = {r["group"]: r for r in comparison["by_market"]}
    assert set(by_market) == {"moneyline", "K", "H"}
    assert by_market["moneyline"]["n"] == 2
    assert by_market["K"]["n"] == 1
    assert by_market["H"]["n"] == 1
    # Sorted by trust tier (trustworthy first): H is trustworthy, K is
    # untrustworthy, moneyline is loose -- so H must come before moneyline
    # which must come before K.
    order = [r["group"] for r in comparison["by_market"]]
    assert order.index("H") < order.index("moneyline") < order.index("K")


def test_aggregate_accuracy_comparison_empty_is_safe():
    comparison = aggregate_accuracy_comparison([], n_bootstrap=100, seed=1)
    assert comparison["overall"]["n"] == 0
    assert comparison["by_market"] == []


def test_format_accuracy_comparison_renders_without_crashing():
    records = [
        _rec("moneyline", "moneyline", 0.9, 0.5, 1, game_pk=1),
        _rec("K", "prop", 0.7, 0.6, 1, game_pk=1),
    ]
    comparison = aggregate_accuracy_comparison(records, n_bootstrap=100, seed=538)
    text = format_accuracy_comparison(
        comparison,
        params={
            "seasons": [2024],
            "iterations": 100,
            "markets": "game+props",
            "base_seed": 538,
            "calibration_applied": True,
        },
    )
    assert "SIM-538" in text
    assert "moneyline" in text
    assert "K" in text
    assert "OVERALL" in text


# ---------------------------------------------------------------------------
# (g) _score_one_game -- an unresolved point-in-time cutoff skips the replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_one_game_skips_when_the_cutoff_cannot_be_resolved(monkeypatch):
    """SIM-538 fix: this script only ever replays PAST, completed games, so a
    None from resolve_asof_ymd here always means the lookup failed -- a live
    game (the other reason resolve_asof_ymd can return None) is not this
    script's case. Before this fix, a failed lookup let the replay proceed
    with NO cutoff at all: the sampler would then draw from the WHOLE pool,
    including this game's own real plays -- the exact leak SIM-535/538 exist
    to close. An adversarial review of SIM-538 confirmed this. Confirm the
    game is now skipped (status "unresolved", no records) and the replay
    (_collect_game_results) is NEVER reached."""
    import api.routes.games as games_mod
    import simulation.sim_kwargs as sim_kwargs_mod

    fake_state = object()  # opaque -- never touched if the skip works

    async def fake_resolve_state(pool, game_pk):
        return fake_state

    async def fake_park_factor(state, pool, duck, game_pk):
        return 1.05

    async def fake_asof(pool, game_pk):
        return None  # the lookup failed for this game

    async def fake_fetch_game_odds(pool, game_pk):
        return {"moneyline": {"opening": {"home_ml": -120}, "closing": {"home_ml": -130}}}

    def replay_must_not_run(*args, **kwargs):
        raise AssertionError(
            "the replay ran despite an unresolved point-in-time cutoff -- "
            "this is the exact leak SIM-535/538 exist to close"
        )

    monkeypatch.setattr(games_mod, "_resolve_state_or_error", fake_resolve_state)
    monkeypatch.setattr(sim_kwargs_mod, "resolve_park_factor_onto_state", fake_park_factor)
    monkeypatch.setattr(sim_kwargs_mod, "resolve_asof_ymd", fake_asof)
    monkeypatch.setattr(clv_backtest, "_fetch_game_odds", fake_fetch_game_odds)
    monkeypatch.setattr(clv_backtest, "_collect_game_results", replay_must_not_run)

    bets, accuracy, status, park_factor = await clv_backtest._score_one_game(
        pool=object(),
        game_pk=12345,
        duck=object(),
        do_game=True,
        do_props=False,
        score_accuracy=True,
        iterations=5,
        base_seed=1,
        min_edge=0.0,
    )
    assert status == "unresolved"
    assert bets == []
    assert accuracy == []
    assert park_factor == pytest.approx(1.05)
