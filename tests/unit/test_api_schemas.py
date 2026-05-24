"""
test_api_schemas.py
===================
Unit tests for SIM-350 -- the **API response-serialization contract**
(:mod:`api.schemas` + :mod:`api.serialization`), Phase 5, Sprint 1.

WHAT THESE ASSERT
-----------------
The Phase-4 output dataclasses hold numpy arrays / numpy scalars, which plain
``json.dumps`` cannot serialise.  SIM-350 adds a Pydantic v2 response model +
``from_dataclass`` converter for each.  For EVERY target dataclass these tests:

  (a) build the SOURCE dataclass with representative numpy-bearing data -- using
      the dataclass's OWN factory methods where they exist (``GameSimSummary.
      from_results``, ``win_probability(...)``, ``PlayByPlay.from_play_results``,
      ``OverrideDelta.from_summaries``, ``PropDistributionSet.from_boxscores``,
      the ``betting.clv_engine`` report builders), otherwise a minimal instance;
  (b) convert via the model's ``from_dataclass`` and assert
      ``model.model_dump_json()`` succeeds;
  (c) ``json.loads`` the JSON and assert it round-trips to PLAIN Python types
      with NO numpy anywhere in the tree (a deep ``_assert_no_numpy`` walk);
  (d) assert key numeric values match the source dataclass.

No DB, no FAISS, no live API, no RNG -- pure construction + conversion.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from simulation.game_state import Bases, GameState, Half, PlayResult
from simulation.results import ConfidenceInterval, GameSimResult, GameSimSummary
from simulation.snapshots import (
    FieldSnapshot,
    OverrideDelta,
    PlayByPlay,
    PlayerRef,
    StateAtPitch,
)
from simulation.prop_distributions import PropDistribution, PropDistributionSet
from simulation.win_probability import TieHandling, win_probability
from simulation.sim_loop import BoxScore, PlayerStatLine

from betting.clv_engine import (
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    clv_from_odds,
    moneyline_edge_report,
    prop_edge_report,
)

from api.serialization import to_jsonable
from api.schemas import (
    BoxScoreModel,
    CLVModel,
    CalibrationMapModel,
    ConfidenceIntervalModel,
    EdgeReportModel,
    FieldSnapshotModel,
    GameSimSummaryLite,
    GameSimSummaryModel,
    MetricDeltaModel,
    OverrideDeltaModel,
    PlayByPlayEntryModel,
    PlayByPlayModel,
    PlayerRefModel,
    PlayerStatLineModel,
    PropDistributionModel,
    PropDistributionSetModel,
    StateAtPitchModel,
    WinProbabilityModel,
)
from simulation.win_probability import IDENTITY_CALIBRATION


# ===========================================================================
# Helpers
# ===========================================================================


def _assert_no_numpy(obj) -> None:
    """Recursively assert ``obj`` contains ONLY plain JSON-native Python types.

    The whole point of SIM-350: after ``json.loads`` nothing in the tree may be
    a numpy scalar/array (or any non-JSON type).  ``json.loads`` itself only ever
    yields dict/list/str/int/float/bool/None, so a positive find here would mean
    the JSON text smuggled a numpy repr -- but we walk defensively anyway.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        assert not isinstance(obj, (np.generic, np.ndarray)), f"numpy leaked: {obj!r}"
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert isinstance(k, str), f"non-str JSON key: {k!r}"
            _assert_no_numpy(v)
        return
    if isinstance(obj, list):
        for v in obj:
            _assert_no_numpy(v)
        return
    raise AssertionError(f"non-JSON-native type survived: {type(obj)} -> {obj!r}")


def _roundtrip(model):
    """Dump a model to JSON, reload to plain python, assert no numpy, return it."""
    text = model.model_dump_json()
    assert isinstance(text, str) and text
    loaded = json.loads(text)
    _assert_no_numpy(loaded)
    return loaded


def _state() -> GameState:
    """A minimal valid GameState for GameSimResult.final_state."""
    return GameState(pitcher_id=0, bat_hand="R", season=2024)


def _result(home: int, away: int, boxscore: "BoxScore | None" = None) -> GameSimResult:
    return GameSimResult(
        home_score=home,
        away_score=away,
        innings_played=9,
        final_state=_state(),
        boxscore=boxscore,
    )


def _summary() -> GameSimSummary:
    """A real GameSimSummary aggregated from N synthetic results (numpy arrays)."""
    results = _result_list()
    return GameSimSummary.from_results(results, confidence_level=0.95)


def _result_list() -> "list[GameSimResult]":
    return [_result(h, a) for (h, a) in [(5, 1), (4, 2), (3, 3), (1, 4), (6, 2), (2, 7)]]


def _boxscores() -> "list[BoxScore]":
    """N per-game boxscores so PropDistributionSet builds real numpy PMFs."""
    boxes: "list[BoxScore]" = []
    # Pitcher id 900 (K/BB/ER/OUTS), batter id 101 (AB/H/HR/RBI).
    for k, h, hr in [(6, 2, 1), (8, 1, 0), (5, 3, 1), (7, 0, 0)]:
        box = BoxScore()
        p = box.line(900)
        p.outs_recorded, p.k, p.bb, p.er = 18, k, 2, 2
        b = box.line(101)
        b.ab, b.h, b.hr, b.rbi = 4, h, hr, hr + 1
        boxes.append(box)
    return boxes


# ===========================================================================
# serialization.to_jsonable (the building block)
# ===========================================================================


def test_to_jsonable_strips_numpy_scalars_and_arrays():
    out = to_jsonable(
        {
            "i": np.int64(7),
            "f": np.float64(1.5),
            "b": np.bool_(True),
            "arr": np.array([1, 2, 3], dtype=np.int64),
            "farr": np.array([0.25, 0.75], dtype=np.float64),
            "nested": [np.int32(2), {"x": np.float32(0.5)}],
        }
    )
    # The whole structure must now json.dumps with NO default= hook.
    json.dumps(out)
    assert out["i"] == 7 and isinstance(out["i"], int)
    assert out["f"] == 1.5 and isinstance(out["f"], float)
    assert out["b"] is True
    assert out["arr"] == [1, 2, 3]
    assert out["farr"] == [0.25, 0.75]
    _assert_no_numpy(out)


# ===========================================================================
# results.py -- ConfidenceInterval, GameSimSummary
# ===========================================================================


def test_confidence_interval_model_roundtrips():
    ci = ConfidenceInterval(point=0.5, low=0.4, high=0.6, level=0.95, method="normal")
    model = ConfidenceIntervalModel.from_dataclass(ci)
    loaded = _roundtrip(model)
    assert loaded["point"] == pytest.approx(0.5)
    assert loaded["low"] == pytest.approx(0.4)
    assert loaded["high"] == pytest.approx(0.6)
    assert loaded["half_width"] == pytest.approx(0.1)


def test_game_sim_summary_model_roundtrips_with_raw_arrays():
    s = _summary()
    model = GameSimSummaryModel.from_dataclass(s)
    loaded = _roundtrip(model)
    # Raw per-iteration arrays present and are plain lists of ints, full length.
    assert loaded["home_scores"] == s.home_scores.tolist()
    assert loaded["away_scores"] == s.away_scores.tolist()
    assert loaded["total_scores"] == s.total_scores.tolist()
    assert len(loaded["home_scores"]) == s.n_iterations
    # Key numeric values match the source dataclass.
    assert loaded["n_iterations"] == s.n_iterations
    assert loaded["home_win_pct"] == pytest.approx(s.home_win_pct)
    assert loaded["home_score_mean"] == pytest.approx(s.home_score_mean)
    assert loaded["home_win_ci"]["point"] == pytest.approx(s.home_win_ci.point)
    # simulated_at survives as an ISO string.
    assert isinstance(loaded["simulated_at"], str)


def test_game_sim_summary_model_can_omit_large_raw_arrays():
    s = _summary()
    model = GameSimSummaryModel.from_dataclass(s, include_raw_arrays=False)
    loaded = _roundtrip(model)
    # Opt-in trimming: arrays omitted (None), aggregates still present + correct.
    assert loaded["home_scores"] is None
    assert loaded["away_scores"] is None
    assert loaded["total_scores"] is None
    assert loaded["home_score_mean"] == pytest.approx(s.home_score_mean)


def test_game_sim_summary_lite_has_no_raw_arrays():
    s = _summary()
    model = GameSimSummaryLite.from_dataclass(s)
    loaded = _roundtrip(model)
    assert "home_scores" not in loaded
    assert "total_scores" not in loaded
    assert loaded["n_iterations"] == s.n_iterations
    assert loaded["total_score_mean"] == pytest.approx(s.total_score_mean)


# ===========================================================================
# sim_loop.py boxscore -- PlayerStatLine, BoxScore
# ===========================================================================


def test_player_stat_line_model_roundtrips():
    ln = PlayerStatLine(player_id=900, outs_recorded=19, k=7, bb=2, er=3)
    model = PlayerStatLineModel.from_dataclass(ln)
    loaded = _roundtrip(model)
    assert loaded["player_id"] == 900
    assert loaded["k"] == 7
    assert loaded["ip_outs"] == 19
    assert loaded["ip"] == pytest.approx(ln.ip)  # 19 outs -> 6.1
    assert loaded["ip_thirds"] == pytest.approx(19 / 3.0)


def test_boxscore_model_roundtrips_with_str_keys():
    box = _boxscores()[0]
    model = BoxScoreModel.from_dataclass(box)
    loaded = _roundtrip(model)
    # JSON object keys are strings (the player ids stringified).
    assert set(loaded["lines"].keys()) == {"900", "101"}
    assert loaded["lines"]["900"]["k"] == box.lines[900].k
    assert "900" in loaded["pitchers"]
    assert "101" in loaded["batters"]


# ===========================================================================
# win_probability.py -- CalibrationMap, WinProbability
# ===========================================================================


def test_calibration_map_model_roundtrips():
    model = CalibrationMapModel.from_dataclass(IDENTITY_CALIBRATION)
    loaded = _roundtrip(model)
    assert loaded["name"] == "identity"


def test_win_probability_model_roundtrips():
    wp = win_probability(_summary(), tie_handling=TieHandling.SPLIT)
    model = WinProbabilityModel.from_dataclass(wp)
    loaded = _roundtrip(model)
    assert loaded["home_win_prob"] == pytest.approx(wp.home_win_prob)
    assert loaded["away_win_prob"] == pytest.approx(wp.away_win_prob)
    assert loaded["home_win_prob"] + loaded["away_win_prob"] == pytest.approx(1.0)
    # Enum serialised to its string value.
    assert loaded["tie_handling"] == "split"
    assert loaded["home_win_ci"]["point"] == pytest.approx(wp.home_win_ci.point)


# ===========================================================================
# snapshots.py -- PlayerRef, FieldSnapshot, PlayByPlay(Entry), StateAtPitch,
#                 MetricDelta, OverrideDelta
# ===========================================================================


def _live_state() -> GameState:
    st = GameState(pitcher_id=900, bat_hand="L", season=2024)
    st.batter_id = 101
    st.bases = Bases(first=201, second=None, third=203)
    st.balls, st.strikes, st.outs = 2, 1, 1
    st.inning, st.half = 7, Half.BOTTOM
    st.home_score, st.away_score = 3, 5
    return st


def test_player_ref_model_roundtrips():
    ref = PlayerRef.of(101, {101: "Batter A"})
    model = PlayerRefModel.from_dataclass(ref)
    loaded = _roundtrip(model)
    assert loaded["player_id"] == 101
    assert loaded["label"] == "Batter A"


def test_field_snapshot_model_roundtrips():
    snap = FieldSnapshot.from_game_state(
        _live_state(), labels={101: "Batter A", 201: "Runner 1B"}
    )
    model = FieldSnapshotModel.from_dataclass(snap)
    loaded = _roundtrip(model)
    assert loaded["balls"] == 2 and loaded["strikes"] == 1 and loaded["outs"] == 1
    assert loaded["inning"] == 7 and loaded["half"] == "bottom"
    assert loaded["batter"]["player_id"] == 101
    assert loaded["baserunners"]["1B"]["player_id"] == 201
    assert loaded["baserunners"]["2B"] is None  # empty bag -> JSON null
    assert set(loaded["occupied_bases"]) == {"1B", "3B"}
    assert loaded["runners_on"] == 2


def _pitch(outcome, *, terminal=False, event=None, contact=False, ev=None) -> PlayResult:
    return PlayResult(
        pitch_outcome=outcome,
        is_contact=contact,
        pa_terminal=terminal,
        event=event,
        exit_velo=ev,
    )


def test_play_by_play_model_roundtrips():
    results = [
        _pitch("ball"),
        _pitch("called_strike"),
        _pitch("in_play", terminal=True, event="single", contact=True, ev=98.5),
        _pitch("swinging_strike"),
        _pitch("swinging_strike", terminal=True, event="strikeout"),
    ]
    pbp = PlayByPlay.from_play_results(results)
    model = PlayByPlayModel.from_dataclass(pbp)
    loaded = _roundtrip(model)
    assert loaded["n_pitches"] == 5
    assert loaded["n_plate_appearances"] == 2
    # Terminal pitch carries the resolved event; exit_velo is a plain float.
    assert loaded["entries"][2]["event"] == "single"
    assert loaded["entries"][2]["is_pa_end"] is True
    assert loaded["entries"][2]["exit_velo"] == pytest.approx(98.5)


def test_play_by_play_entry_model_roundtrips_standalone():
    entry = PlayByPlay.from_play_results(
        [_pitch("in_play", terminal=True, event="home_run", contact=True)]
    ).entries[0]
    model = PlayByPlayEntryModel.from_dataclass(entry)
    loaded = _roundtrip(model)
    assert loaded["event"] == "home_run"
    assert loaded["pitch"] == 1 and loaded["at_bat"] == 0


def test_state_at_pitch_model_roundtrips():
    sap = StateAtPitch.from_game_state(_live_state(), at_bat=3, pitch=2, sequence=11)
    model = StateAtPitchModel.from_dataclass(sap)
    loaded = _roundtrip(model)
    assert loaded["at_bat"] == 3 and loaded["pitch"] == 2 and loaded["sequence"] == 11
    assert loaded["field"]["inning"] == 7


def test_override_delta_model_roundtrips():
    baseline = _summary()
    # An "override" summary skewed toward more home wins.
    override = GameSimSummary.from_results(
        [_result(h, a) for (h, a) in [(7, 1), (8, 2), (6, 0), (9, 1), (5, 4), (7, 3)]]
    )
    od = OverrideDelta.from_summaries(baseline, override, description="swap SP")
    model = OverrideDeltaModel.from_dataclass(od)
    loaded = _roundtrip(model)
    assert loaded["description"] == "swap SP"
    md = loaded["metrics"]["home_win_pct"]
    assert md["delta"] == pytest.approx(od.metrics["home_win_pct"].delta)


def test_metric_delta_model_roundtrips_standalone():
    od = OverrideDelta.from_summaries(_summary(), _summary())
    md = od.metrics["home_score_mean"]
    model = MetricDeltaModel.from_dataclass(md)
    loaded = _roundtrip(model)
    assert loaded["metric"] == "home_score_mean"
    assert loaded["delta"] == pytest.approx(0.0)  # same summary -> zero delta


# ===========================================================================
# prop_distributions.py -- PropDistribution, PropDistributionSet
# ===========================================================================


def test_prop_distribution_model_roundtrips():
    dist = PropDistribution.from_samples(900, "K", [6, 8, 5, 7, 6, 8])
    model = PropDistributionModel.from_dataclass(dist)
    loaded = _roundtrip(model)
    # support / probabilities became plain int / float lists.
    assert loaded["support"] == dist.support.tolist()
    assert loaded["probabilities"] == pytest.approx(dist.probabilities.tolist())
    assert sum(loaded["probabilities"]) == pytest.approx(1.0)
    # pmf keys are stringified ints.
    assert all(isinstance(k, str) for k in loaded["pmf"].keys())
    assert loaded["pmf"]["6"] == pytest.approx(dist.prob(6))
    assert loaded["mean"] == pytest.approx(dist.mean)


def test_prop_distribution_set_model_roundtrips():
    dist_set = PropDistributionSet.from_boxscores(_boxscores())
    model = PropDistributionSetModel.from_dataclass(dist_set)
    loaded = _roundtrip(model)
    assert loaded["n_iterations"] == dist_set.n_iterations
    # player-id keys stringified; pitcher 900 has a K PMF.
    assert "900" in loaded["by_player"]
    k_dist = loaded["by_player"]["900"]["K"]
    assert sum(k_dist["probabilities"]) == pytest.approx(1.0)
    assert k_dist["player_id"] == 900


# ===========================================================================
# clv_engine.py -- CLV, EdgeReport
# ===========================================================================


def test_clv_model_roundtrips():
    clv = clv_from_odds(
        entry_side_american=+150,
        entry_other_american=-170,
        close_side_american=+120,
        close_other_american=-140,
    )
    model = CLVModel.from_dataclass(clv)
    loaded = _roundtrip(model)
    assert loaded["clv_prob"] == pytest.approx(clv.clv_prob)
    assert loaded["beat_close"] == clv.beat_close


def test_edge_report_model_roundtrips_moneyline_with_clv():
    wp = win_probability(_summary())
    market = TwoWayMarket(
        side=MarketSide.HOME,
        entry=OddsQuote(side=-110, other=-110),
        close=OddsQuote(side=-130, other=+110),
    )
    report = moneyline_edge_report(wp, market, side=MarketSide.HOME)
    model = EdgeReportModel.from_dataclass(report)
    loaded = _roundtrip(model)
    assert loaded["label"] == "moneyline"
    assert loaded["side"] == "home"  # MarketSide enum -> its value
    assert loaded["sim_prob"] == pytest.approx(report.sim_prob)
    assert loaded["edge"] == pytest.approx(report.edge)
    assert loaded["positive_edge"] == report.positive_edge
    # closing quote supplied -> nested CLV present and JSON-safe.
    assert loaded["clv"] is not None
    assert loaded["clv"]["clv_prob"] == pytest.approx(report.clv.clv_prob)


def test_edge_report_model_roundtrips_prop_without_clv():
    dist = PropDistribution.from_samples(900, "K", [6, 8, 5, 7, 6, 8])
    market = TwoWayMarket(
        side=MarketSide.OVER,
        entry=OddsQuote(side=-115, other=-105, line=6.5),
    )
    report = prop_edge_report(dist, market, side=MarketSide.OVER)
    model = EdgeReportModel.from_dataclass(report)
    loaded = _roundtrip(model)
    assert loaded["label"] == "prop:K"
    assert loaded["side"] == "over"
    assert loaded["line"] == pytest.approx(6.5)
    assert loaded["clv"] is None  # no closing quote supplied
