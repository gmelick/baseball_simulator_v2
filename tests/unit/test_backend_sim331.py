"""
test_backend_sim331.py
======================
Unit tests for SIM-331 -- the field/baserunner state + pitch-level play-by-play
snapshot contracts in simulation.snapshots (Phase 4, Sprint 3).

These build the four contracts from plainly-constructed GameState / PlayResult /
summary objects (no loop run, no DB, no FAISS, no API) and assert:
  * FieldSnapshot captures the 9 defensive positions, batter, baserunners
    (ids/labels), and count/outs/inning/half/score chrome;
  * a /plays PlayByPlay is pitch-level, ordered, groups pitches into PAs, and
    carries the resolved event on the terminal pitch;
  * StateAtPitch reflects the right point in time;
  * OverrideDelta captures the baseline-vs-override diff.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from simulation.game_state import Bases, GameState, Half, PlayResult
from simulation.snapshots import (
    DEFENSE_POSITIONS,
    OVERRIDE_METRIC_FIELDS,
    FieldSnapshot,
    MetricDelta,
    OverrideDelta,
    PlayByPlay,
    PlayByPlayEntry,
    PlayerRef,
    StateAtPitch,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _state_with_runners() -> GameState:
    """A GameState mid-game: runners on 1B+3B, 2-1 count, 1 out, bottom 7th."""
    st = GameState(pitcher_id=900, bat_hand="L", season=2024)
    st.batter_id = 101
    st.bases = Bases(first=201, second=None, third=203)
    st.balls = 2
    st.strikes = 1
    st.outs = 1
    st.inning = 7
    st.half = Half.BOTTOM
    st.home_score = 3
    st.away_score = 5
    return st


def _pitch(outcome, *, terminal=False, event=None, contact=False,
           runs_scored=0, outs=0, ev=None) -> PlayResult:
    return PlayResult(
        pitch_outcome=outcome,
        is_contact=contact,
        pa_terminal=terminal,
        event=event,
        runs_scored=runs_scored,
        outs_recorded=outs,
        exit_velo=ev,
    )


# ===========================================================================
# FieldSnapshot
# ===========================================================================


def test_field_snapshot_captures_positions_and_chrome():
    st = _state_with_runners()
    labels = {101: "Batter Joe", 201: "Runner One", 203: "Runner Three",
              555: "SS Sam"}
    snap = FieldSnapshot.from_game_state(
        st, labels=labels, defense_positions={"SS": 555},
    )

    # All 9 canonical defensive positions present as slots.
    assert set(snap.positions) == set(DEFENSE_POSITIONS)
    assert len(snap.positions) == 9
    # The supplied SS is resolved (id + label); unassigned slots are None.
    assert snap.positions["SS"] == PlayerRef(player_id=555, label="SS Sam")
    assert snap.positions["P"] is None

    # Batter captured.
    assert snap.batter == PlayerRef(player_id=101, label="Batter Joe")

    # Baserunners: 1B + 3B occupied, 2B empty.
    assert snap.baserunners["1B"] == PlayerRef(player_id=201, label="Runner One")
    assert snap.baserunners["2B"] is None
    assert snap.baserunners["3B"] == PlayerRef(player_id=203, label="Runner Three")
    assert snap.occupied_bases == ("1B", "3B")
    assert snap.runners_on == 2
    # Bitmask: 1B (bit0) + 3B (bit2) == 0b101 == 5.
    assert snap.runners_state == 0b101

    # Chrome.
    assert (snap.balls, snap.strikes, snap.outs) == (2, 1, 1)
    assert snap.inning == 7
    assert snap.half == "bottom"
    assert snap.home_score == 3
    assert snap.away_score == 5


def test_field_snapshot_label_fallback_when_no_map():
    st = GameState(pitcher_id=1, bat_hand="R", season=2024)
    st.batter_id = 42
    snap = FieldSnapshot.from_game_state(st)
    # No labels map -> "#<id>" fallback; empty bases stay None.
    assert snap.batter == PlayerRef(player_id=42, label="#42")
    assert all(snap.baserunners[b] is None for b in ("1B", "2B", "3B"))
    assert snap.runners_on == 0


# ===========================================================================
# PlayByPlay (/plays)
# ===========================================================================


def _sample_pa_stream():
    """Two PAs: a 3-pitch strikeout, then a 1-pitch home run."""
    return [
        _pitch("ball"),
        _pitch("called_strike"),
        _pitch("swinging_strike", terminal=True, event="strikeout", outs=1),
        _pitch("in_play", terminal=True, event="home_run", contact=True,
               runs_scored=1, ev=104.2),
    ]


def test_plays_are_pitch_level_and_ordered():
    pbp = PlayByPlay.from_play_results(_sample_pa_stream())

    # One entry per pitch (pitch-level granularity).
    assert pbp.n_pitches == 4
    # sequence is 0..N-1 strictly increasing (emission order preserved).
    assert [e.sequence for e in pbp.entries] == [0, 1, 2, 3]

    # Two PAs detected from the terminal flag.
    assert pbp.n_plate_appearances == 2

    # The within-PA pitch counter resets at the new PA.
    first_pa = pbp.pitches_for_at_bat(0)
    assert [e.pitch for e in first_pa] == [1, 2, 3]
    assert [e.at_bat for e in first_pa] == [0, 0, 0]
    second_pa = pbp.pitches_for_at_bat(1)
    assert [e.pitch for e in second_pa] == [1]
    assert second_pa[0].at_bat == 1


def test_plays_terminal_event_on_last_pitch_only():
    pbp = PlayByPlay.from_play_results(_sample_pa_stream())

    # Non-terminal pitches carry the pitch outcome but no resolved event.
    assert pbp.entries[0].pitch_outcome == "ball"
    assert pbp.entries[0].is_pa_end is False
    assert pbp.entries[0].event is None

    # The PA's resolved event lives on the terminal pitch.
    k_pitch = pbp.entries[2]
    assert k_pitch.is_pa_end is True
    assert k_pitch.event == "strikeout"
    assert k_pitch.outs_recorded == 1

    # The home-run pitch carries event + run + batted-ball detail.
    hr = pbp.entries[3]
    assert hr.is_pa_end is True
    assert hr.is_contact is True
    assert hr.event == "home_run"
    assert hr.runs_scored == 1
    assert hr.exit_velo == 104.2

    # Grouped view: one inner list per PA.
    groups = pbp.plate_appearances
    assert len(groups) == 2
    assert [len(g) for g in groups] == [3, 1]


def test_empty_play_stream_yields_empty_pbp():
    pbp = PlayByPlay.from_play_results([])
    assert pbp.n_pitches == 0
    assert pbp.n_plate_appearances == 0
    assert pbp.plate_appearances == []


# ===========================================================================
# StateAtPitch (/state/{at_bat}/{pitch})
# ===========================================================================


def test_state_at_pitch_reflects_point_in_time():
    st = _state_with_runners()
    sap = StateAtPitch.from_game_state(
        st, at_bat=14, pitch=3, sequence=57,
        labels={101: "Batter Joe"},
    )
    # Indices tag the point in time.
    assert sap.at_bat == 14
    assert sap.pitch == 3
    assert sap.sequence == 57
    # The embedded field snapshot reflects the same state.
    assert isinstance(sap.field, FieldSnapshot)
    assert sap.field.inning == 7
    assert sap.field.half == "bottom"
    assert sap.field.balls == 2 and sap.field.strikes == 1
    assert sap.field.batter == PlayerRef(player_id=101, label="Batter Joe")
    assert sap.field.occupied_bases == ("1B", "3B")


def test_state_at_pitch_sequence_optional():
    st = GameState(pitcher_id=1, bat_hand="R", season=2024)
    sap = StateAtPitch.from_game_state(st, at_bat=0, pitch=0)
    assert sap.sequence is None
    assert sap.field.balls == 0 and sap.field.outs == 0


# ===========================================================================
# OverrideDelta
# ===========================================================================


@dataclass
class _FakeSummary:
    """Minimal stand-in exposing the metric attributes OverrideDelta reads."""
    home_win_pct: float
    away_win_pct: float
    home_score_mean: float
    away_score_mean: float
    total_score_mean: float


def test_override_delta_captures_diff():
    baseline = _FakeSummary(
        home_win_pct=0.52, away_win_pct=0.48,
        home_score_mean=4.5, away_score_mean=4.2, total_score_mean=8.7,
    )
    override = _FakeSummary(
        home_win_pct=0.61, away_win_pct=0.39,
        home_score_mean=5.1, away_score_mean=3.9, total_score_mean=9.0,
    )

    od = OverrideDelta.from_summaries(
        baseline, override, description="sub in closer for 8th",
    )

    # Each tracked metric carries baseline / override / delta.
    assert od.metrics["home_win_pct"].baseline == 0.52
    assert od.metrics["home_win_pct"].override == 0.61
    assert od.delta("home_win_pct") == pytest.approx(0.09)
    assert od.home_win_pct_delta == pytest.approx(0.09)

    # A drop is a negative delta.
    assert od.delta("away_win_pct") == pytest.approx(-0.09)
    assert od.delta("home_score_mean") == pytest.approx(0.6)
    assert od.delta("total_score_mean") == pytest.approx(0.3)

    assert od.description == "sub in closer for 8th"
    # Default tracked field set.
    assert set(od.metrics) == set(OVERRIDE_METRIC_FIELDS)


def test_override_delta_custom_metric_subset():
    baseline = _FakeSummary(0.5, 0.5, 4.0, 4.0, 8.0)
    override = _FakeSummary(0.5, 0.5, 4.0, 4.0, 8.0)
    od = OverrideDelta.from_summaries(
        baseline, override, metrics=["home_win_pct"],
    )
    assert set(od.metrics) == {"home_win_pct"}
    assert od.delta("home_win_pct") == 0.0


def test_override_delta_works_with_real_gamesimsummary():
    """Cross-consistency: build deltas from two real GameSimSummary objects."""
    from simulation.results import GameSimResult, GameSimSummary

    def _summary(scores):
        results = [
            GameSimResult(
                home_score=h, away_score=a, innings_played=9,
                final_state=GameState(pitcher_id=0, bat_hand="R", season=2024),
            )
            for (h, a) in scores
        ]
        return GameSimSummary.from_results(results)

    baseline = _summary([(3, 5), (2, 4), (1, 6)])   # home loses all 3
    override = _summary([(7, 2), (8, 1), (6, 3)])   # home wins all 3
    od = OverrideDelta.from_summaries(baseline, override)

    # Override flipped the home win rate from 0.0 to 1.0.
    assert od.delta("home_win_pct") == pytest.approx(1.0)
    assert od.delta("home_score_mean") > 0
