"""
tests/unit/test_accuracy_hypothetical_return.py
================================================
Unit tests for the hypothetical dollar return, an OPT-IN companion report to
the accuracy comparison in ``scripts/accuracy_comparison.py``.

What the report does, in plain words: an accuracy score (Brier, log loss) is
the right instrument for the team but a hard one for a non-technical reader
to judge. For every accuracy observation where the simulator's probability
differs from the market's by at least a chosen amount, the report prices a
hypothetical 1-unit bet on the side the simulator favors, at the CLOSING
price. It reports two numbers: ``model_ev`` (what the model itself claims
that bet is worth, never touching the real outcome) and
``realized_return`` (what that bet pays, graded against the real outcome).
The "HYPOTHETICAL DOLLAR RETURN" section of the script's module docstring
says that the report is a DIAGNOSTIC, not a certified betting-edge
measurement. The owner's ruling gates any real betting-value claim on every
band the lanes grade being green; this report does not check that.

These tests run with NO DB and NO real sim: every function under test here
is PURE, built on the SAME synthetic-record pattern
``tests/unit/test_accuracy_comparison.py`` and
``tests/unit/test_accuracy_minimum_sample.py`` already use.
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.script_loader import load_script

accuracy_comparison = load_script("accuracy_comparison")

from accuracy.confidence import (  # noqa: E402
    MIN_DETECTABLE_EDGE,
    _bootstrap_paired_diff_ci,
)
from accuracy.hypothetical_return import (  # noqa: E402
    DEFAULT_RETURN_EDGE_THRESHOLD,
    ReturnRecord,
    _bootstrap_mean_ci,
    _favored_side_prob_and_price,
    _return_row_for,
    aggregate_hypothetical_return,
    format_hypothetical_return,
    score_hypothetical_return,
)
from accuracy.records import AccuracyRecord  # noqa: E402

parse_args = accuracy_comparison.parse_args


def _rec(
    sim_prob: float,
    market_prob: float,
    outcome: int,
    *,
    market: str = "moneyline",
    market_type: str = "moneyline",
    game_pk: int = 1,
    side_price: float | None = -110.0,
    other_price: float | None = -110.0,
    player_id: int | None = None,
) -> AccuracyRecord:
    return AccuracyRecord(
        game_pk=game_pk,
        market=market,
        market_type=market_type,
        sim_prob=sim_prob,
        market_prob=market_prob,
        outcome=outcome,
        player_id=player_id,
        market_side_price=side_price,
        market_other_price=other_price,
    )


# ---------------------------------------------------------------------------
# (a) _favored_side_prob_and_price -- which side, what probability, what price
# ---------------------------------------------------------------------------


def test_favored_side_is_reference_when_sim_prob_exceeds_market():
    rec = _rec(0.65, 0.50, 1, side_price=-120.0, other_price=110.0)
    prob, price, side = _favored_side_prob_and_price(rec)
    assert prob == pytest.approx(0.65)
    assert price == pytest.approx(-120.0)
    assert side == "reference"


def test_favored_side_is_fade_when_sim_prob_is_below_market():
    rec = _rec(0.30, 0.50, 0, side_price=-120.0, other_price=105.0)
    prob, price, side = _favored_side_prob_and_price(rec)
    assert prob == pytest.approx(0.70)  # 1 - sim_prob
    assert price == pytest.approx(105.0)  # the OTHER side's closing price
    assert side == "fade"


def test_favored_side_is_none_on_an_exact_tie():
    rec = _rec(0.50, 0.50, 1)
    assert _favored_side_prob_and_price(rec) is None


def test_favored_side_is_none_without_both_closing_prices():
    assert _favored_side_prob_and_price(_rec(0.65, 0.50, 1, side_price=None)) is None
    assert _favored_side_prob_and_price(_rec(0.65, 0.50, 1, other_price=None)) is None


# ---------------------------------------------------------------------------
# (b) score_hypothetical_return -- the filter + the priced, graded bet
# ---------------------------------------------------------------------------


def test_score_hypothetical_return_filters_by_disagreement_threshold():
    small_edge = _rec(0.51, 0.50, 1)  # disagreement 0.01
    big_edge = _rec(0.65, 0.50, 1)  # disagreement 0.15
    recs = score_hypothetical_return([small_edge, big_edge], edge_threshold=0.02)
    assert len(recs) == 1
    assert recs[0].disagreement == pytest.approx(0.15)


def test_score_hypothetical_return_boundary_is_inclusive():
    """A disagreement EXACTLY at the threshold qualifies (>=, not >)."""
    rec = _rec(0.52, 0.50, 1)  # disagreement exactly 0.02
    recs = score_hypothetical_return([rec], edge_threshold=0.02)
    assert len(recs) == 1


def test_score_hypothetical_return_skips_records_without_prices_even_with_a_big_edge():
    rec = _rec(0.90, 0.50, 1, side_price=None)
    assert score_hypothetical_return([rec], edge_threshold=0.02) == []


def test_score_hypothetical_return_reference_win():
    """sim favors reference (0.65 > 0.50); outcome=1 means reference happened
    -> the reference bet WINS. Exact EV and realized-return values."""
    rec = _rec(0.65, 0.50, 1, side_price=-120.0, other_price=110.0)
    [got] = score_hypothetical_return([rec], edge_threshold=0.0)
    assert got.favored_side == "reference"
    # decimal(-120) = 1 + 100/120 = 1.833333...; b = 0.833333...
    # EV = 0.65*b - 0.35
    expected_ev = 0.65 * (1.0 + 100.0 / 120.0 - 1.0) - 0.35
    assert got.model_ev == pytest.approx(expected_ev)
    assert got.realized_return == pytest.approx(1.0 + 100.0 / 120.0 - 1.0)  # won: decimal - 1


def test_score_hypothetical_return_reference_loss():
    """Same bet, but outcome=0 (reference did NOT happen) -> the reference
    bet LOSES the full unit stake."""
    rec = _rec(0.65, 0.50, 0, side_price=-120.0, other_price=110.0)
    [got] = score_hypothetical_return([rec], edge_threshold=0.0)
    assert got.favored_side == "reference"
    assert got.realized_return == pytest.approx(-1.0)


def test_score_hypothetical_return_fade_win():
    """sim favors the fade (0.30 < 0.50); outcome=0 means the reference did
    NOT happen -> the fade bet WINS, priced at the OTHER side's price."""
    rec = _rec(0.30, 0.50, 0, side_price=-120.0, other_price=105.0)
    [got] = score_hypothetical_return([rec], edge_threshold=0.0)
    assert got.favored_side == "fade"
    assert got.realized_return == pytest.approx(1.05)  # decimal(105) - 1 = 1.05


def test_score_hypothetical_return_fade_loss():
    """Same fade bet, but outcome=1 (reference DID happen) -> the fade bet
    LOSES."""
    rec = _rec(0.30, 0.50, 1, side_price=-120.0, other_price=105.0)
    [got] = score_hypothetical_return([rec], edge_threshold=0.0)
    assert got.favored_side == "fade"
    assert got.realized_return == pytest.approx(-1.0)


def test_score_hypothetical_return_carries_market_and_game_identity():
    rec = _rec(0.65, 0.50, 1, market="K", market_type="prop", game_pk=777, player_id=42)
    [got] = score_hypothetical_return([rec], edge_threshold=0.0)
    assert got.game_pk == 777
    assert got.market == "K"
    assert got.market_type == "prop"
    assert got.player_id == 42


def test_score_hypothetical_return_mixed_batch():
    """A realistic mixed batch: some qualify, some don't (small edge), some
    can't be priced (missing odds) -- confirm only the right ones survive."""
    records = [
        _rec(0.51, 0.50, 1),  # too small an edge
        _rec(0.90, 0.50, 1, side_price=None),  # big edge, no price
        _rec(0.70, 0.50, 1, game_pk=2),  # qualifies (reference win)
        _rec(0.20, 0.50, 0, game_pk=3),  # qualifies (fade win)
    ]
    recs = score_hypothetical_return(records, edge_threshold=0.02)
    assert {r.game_pk for r in recs} == {2, 3}


# ---------------------------------------------------------------------------
# (c) _bootstrap_mean_ci -- confirmed to be exactly _bootstrap_paired_diff_ci
# ---------------------------------------------------------------------------


def test_bootstrap_mean_ci_delegates_to_the_shared_bootstrap_exactly():
    rng = np.random.default_rng(3)
    values = rng.normal(loc=0.05, scale=0.3, size=30)
    game_pks = np.arange(30, dtype=np.int64)
    got = _bootstrap_mean_ci(values, game_pks, n_bootstrap=500, seed=7)
    expected = _bootstrap_paired_diff_ci(values, game_pks, n_bootstrap=500, seed=7)
    assert got == expected


# ---------------------------------------------------------------------------
# (d) _return_row_for / aggregate_hypothetical_return -- the roll-up
# ---------------------------------------------------------------------------


def _return_rec(
    game_pk: int, model_ev: float, realized_return: float, *, market: str = "moneyline"
):
    return ReturnRecord(
        game_pk=game_pk,
        market=market,
        market_type=market,
        disagreement=0.1,
        favored_side="reference",
        model_ev=model_ev,
        realized_return=realized_return,
    )


def test_return_row_for_empty_is_safe():
    row = _return_row_for("overall", "—", [], n_bootstrap=100, seed=1, alpha=0.05)
    assert row.n == 0
    assert row.n_games == 0
    assert np.isnan(row.mean_model_ev)
    assert row.total_model_ev == 0.0
    assert np.isnan(row.mean_realized_return)
    assert row.min_n_recommended == float("inf")
    assert row.underpowered is True


def test_return_row_for_totals_and_means():
    records = [
        _return_rec(1, model_ev=0.10, realized_return=0.83),
        _return_rec(2, model_ev=0.20, realized_return=-1.0),
        _return_rec(3, model_ev=0.05, realized_return=1.05),
    ]
    row = _return_row_for("overall", "—", records, n_bootstrap=100, seed=1, alpha=0.05)
    assert row.n == 3
    assert row.n_games == 3
    assert row.mean_model_ev == pytest.approx((0.10 + 0.20 + 0.05) / 3)
    assert row.total_model_ev == pytest.approx(0.10 + 0.20 + 0.05)
    assert row.mean_realized_return == pytest.approx((0.83 - 1.0 + 1.05) / 3)
    assert row.total_realized_return == pytest.approx(0.83 - 1.0 + 1.05)


def test_return_row_for_zero_spread_is_never_underpowered_with_enough_games():
    """Every game reads the identical realized return -- a perfectly
    resolved read needs no more games, PROVIDED there are enough distinct
    games to measure that zero spread from at all (see
    _minimum_observations_clustered's MIN_CLUSTERS_FOR_INFERENCE guard)."""
    records = [_return_rec(i, model_ev=0.05, realized_return=0.10) for i in range(15)]
    row = _return_row_for("overall", "—", records, n_bootstrap=100, seed=1, alpha=0.05)
    assert row.min_n_recommended == pytest.approx(0.0)
    assert row.underpowered is False


def test_return_row_for_too_few_games_is_infinitely_underpowered():
    """Every bet crammed into one game -- one independent trial -- must
    refuse a minimum rather than reporting a falsely-resolved read."""
    records = [_return_rec(1, model_ev=0.05, realized_return=0.10) for _ in range(20)]
    row = _return_row_for("overall", "—", records, n_bootstrap=100, seed=1, alpha=0.05)
    assert row.min_n_recommended == float("inf")
    assert row.underpowered is True


def test_aggregate_hypothetical_return_reports_the_disagreement_filter_counts():
    records = [
        _rec(0.51, 0.50, 1, game_pk=1),  # too small
        _rec(0.70, 0.50, 1, game_pk=2),  # qualifies
        _rec(0.20, 0.50, 0, game_pk=3, market="K", market_type="prop"),  # qualifies
    ]
    comparison = aggregate_hypothetical_return(
        records, edge_threshold=0.02, n_bootstrap=100, seed=1
    )
    assert comparison["n_accuracy_records"] == 3
    assert comparison["n_qualifying"] == 2
    assert comparison["edge_threshold"] == pytest.approx(0.02)


def test_aggregate_hypothetical_return_default_edge_threshold_matches_the_platform_convention():
    """DEFAULT_RETURN_EDGE_THRESHOLD uses the SAME platform edge floor as the
    minimum-sample estimate (MIN_DETECTABLE_EDGE). The test confirms that the
    two agree; the shared value is a deliberate choice."""
    assert pytest.approx(MIN_DETECTABLE_EDGE) == DEFAULT_RETURN_EDGE_THRESHOLD


def test_aggregate_hypothetical_return_bonferroni_corrects_by_market_rows_only():
    records = [
        _rec(0.70, 0.50, 1, game_pk=1, market="moneyline", market_type="moneyline"),
        _rec(0.20, 0.50, 0, game_pk=1, market="K", market_type="prop"),
        _rec(0.75, 0.50, 1, game_pk=1, market="H", market_type="prop"),
    ]
    comparison = aggregate_hypothetical_return(
        records, edge_threshold=0.0, n_bootstrap=100, seed=1, base_alpha=0.05
    )
    assert comparison["overall"]["alpha_used"] == pytest.approx(0.05)
    for r in comparison["by_market"]:
        assert r["alpha_used"] == pytest.approx(0.05 / 3)


def test_aggregate_hypothetical_return_empty_is_safe():
    comparison = aggregate_hypothetical_return([], edge_threshold=0.02, n_bootstrap=100, seed=1)
    assert comparison["n_accuracy_records"] == 0
    assert comparison["n_qualifying"] == 0
    assert comparison["overall"]["n"] == 0
    assert comparison["by_market"] == []


def test_format_hypothetical_return_renders_without_crashing():
    records = [
        _rec(0.70, 0.50, 1, game_pk=i, market="moneyline", market_type="moneyline")
        for i in range(3)
    ]
    comparison = aggregate_hypothetical_return(
        records, edge_threshold=0.02, n_bootstrap=100, seed=1
    )
    text = format_hypothetical_return(
        comparison,
        params={"seasons": [2024], "iterations": 100, "markets": "game", "base_seed": 0},
    )
    assert "SIM-540" in text
    assert "modelEV" in text
    assert "realzdROI" in text
    assert "moneyline" in text


def test_format_hypothetical_return_renders_an_underpowered_all_none_row_without_crashing():
    """The exact case most likely to trip up string formatting: min_n is
    inf, and there is exactly one qualifying, un-clustered-resolvable
    game."""
    records = [_rec(0.90, 0.50, 1, game_pk=1)]
    comparison = aggregate_hypothetical_return(
        records, edge_threshold=0.02, n_bootstrap=100, seed=1
    )
    text = format_hypothetical_return(
        comparison,
        params={"seasons": [2024], "iterations": 100, "markets": "game", "base_seed": 0},
    )
    assert "UNDERPWR" in text


# ---------------------------------------------------------------------------
# (e) CLI wiring -- the report's flags parse with the expected defaults
# ---------------------------------------------------------------------------


def test_parse_args_hypothetical_return_flags_default_off_and_to_the_platform_edge():
    args = parse_args(["--seasons", "2024"])
    assert args.report_hypothetical_return is False
    assert args.edge_threshold == pytest.approx(DEFAULT_RETURN_EDGE_THRESHOLD)


def test_parse_args_hypothetical_return_flags_are_settable():
    args = parse_args(
        ["--seasons", "2024", "--report-hypothetical-return", "--edge-threshold", "0.05"]
    )
    assert args.report_hypothetical_return is True
    assert args.edge_threshold == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# (f) JSON safety + the machine-readable "not certified" marker
# ---------------------------------------------------------------------------


def test_aggregate_hypothetical_return_is_never_certified():
    """A MACHINE-readable marker, not just console text: a script that reads
    the JSON report directly never sees format_hypothetical_return's printed
    disclaimer."""
    comparison = aggregate_hypothetical_return([], edge_threshold=0.02, n_bootstrap=100, seed=1)
    assert comparison["certified"] is False


def test_return_comparison_row_to_jsonable_is_json_safe_on_a_degenerate_row():
    """A bucket with zero qualifying bets (nan means/CI) or too few distinct
    games (inf min_n) must serialize to valid JSON: None, never the raw
    Python nan/inf that a plain asdict() gives. ReturnComparisonRow and
    AccuracyComparisonRow both pass through the shared _json_safe."""
    empty_row = _return_row_for("overall", "—", [], n_bootstrap=100, seed=1, alpha=0.05)
    d = empty_row.to_jsonable()
    assert d["mean_model_ev"] is None  # the raw value is nan
    assert d["mean_realized_return"] is None  # the raw value is nan
    assert d["min_n_recommended"] is None  # the raw value is inf
    # The whole dict must be JSON-serializable (no bare NaN/Infinity tokens).
    import json

    reserialized = json.loads(json.dumps(d))
    assert reserialized["mean_model_ev"] is None


def test_return_record_to_jsonable_round_trips_through_real_json():
    rec = _return_rec(1, model_ev=0.10, realized_return=0.83)
    import json

    reloaded = json.loads(json.dumps(rec.to_jsonable()))
    assert reloaded["model_ev"] == pytest.approx(0.10)
    assert reloaded["realized_return"] == pytest.approx(0.83)
