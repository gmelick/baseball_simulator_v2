"""
test_linescore_sim362.py
========================
Unit tests for SIM-362 -- the per-inning linescore + team R/H/E derivation in
simulation.linescore (Phase 5, Sprint 4).

These build small synthetic ``PlayResult`` streams from plainly-constructed
``GameState`` next-states (no loop run, no DB, no FAISS, no API) and assert:
  * the per-inning runs grid is correct across several innings (TOP -> away
    column, BOTTOM -> home column);
  * team hits are counted from the hit events (1B/2B/3B/HR), batting side;
  * errors are charged to the correct (fielding) side, not the batter;
  * extra innings extend the grid;
  * an in-progress bottom-of-the-last-inning is represented as not-yet-played
    (None / "x"), NOT 0;
  * an empty stream -> an empty linescore.
"""

from __future__ import annotations

import pytest

from simulation.game_state import GameState, Half, PlayResult
from simulation.linescore import (
    HIT_EVENTS,
    InningLine,
    Linescore,
    linescore_from_plays,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _state(inning: int, half: Half) -> GameState:
    """A minimal committed GameState tagged with an inning + half.

    Scores are left at their defaults (0/0); the linescore derives runs from each
    play's ``runs_scored``, NOT from the running score on ``next_state``, so the
    score fields are irrelevant to the grid here.
    """
    st = GameState(pitcher_id=900, bat_hand="R", season=2024)
    st.inning = inning
    st.half = half
    return st


def _play(
    inning: int,
    half: Half,
    *,
    runs_scored: int = 0,
    event: str | None = None,
    canonical_event: str | None = None,
    is_error: bool = False,
) -> PlayResult:
    """A PlayResult whose committed next_state sits in (inning, half)."""
    return PlayResult(
        pitch_outcome="in_play",
        is_contact=True,
        pa_terminal=True,
        event=event,
        canonical_event=canonical_event,
        runs_scored=runs_scored,
        is_error=is_error,
        next_state=_state(inning, half),
    )


# ===========================================================================
# Empty stream
# ===========================================================================


def test_empty_stream_yields_empty_linescore():
    ls = linescore_from_plays([])
    assert isinstance(ls, Linescore)
    assert ls.innings == []
    assert ls.n_innings == 0
    assert ls.away_runs == ls.home_runs == 0
    assert ls.away_hits == ls.home_hits == 0
    assert ls.away_errors == ls.home_errors == 0
    assert ls.away_by_inning == []
    assert ls.home_by_inning == []


# ===========================================================================
# Per-inning runs grid
# ===========================================================================


def test_runs_grid_maps_top_to_away_and_bottom_to_home():
    # Inning 1: away scores 1 (top), home scores 0 (bottom played, scoreless).
    # Inning 2: away 0 (top played), home 2 (bottom).
    # Inning 3: away 3 (top, two plays summing to 3), home 0.
    plays = [
        _play(1, Half.TOP, runs_scored=1, event="single"),
        _play(1, Half.BOTTOM, runs_scored=0, event="field_out"),
        _play(2, Half.TOP, runs_scored=0, event="strikeout"),
        _play(2, Half.BOTTOM, runs_scored=2, event="home_run"),
        _play(3, Half.TOP, runs_scored=1, event="single"),
        _play(3, Half.TOP, runs_scored=2, event="double"),
        _play(3, Half.BOTTOM, runs_scored=0, event="field_out"),
    ]
    ls = linescore_from_plays(plays)

    assert ls.n_innings == 3
    assert ls.away_by_inning == [1, 0, 3]
    assert ls.home_by_inning == [0, 2, 0]

    # Row-sum totals.
    assert ls.away_runs == 4
    assert ls.home_runs == 2

    # Spot-check the InningLine shape.
    assert ls.innings[0] == InningLine(inning=1, away=1, home=0)
    assert ls.innings[1].home == 2
    assert all(ln.away_played and ln.home_played for ln in ls.innings)


# ===========================================================================
# Hits
# ===========================================================================


def test_hits_counted_from_hit_events_per_batting_side():
    plays = [
        # Away (top) hits: single + double + triple + home_run = 4 hits.
        _play(1, Half.TOP, event="single"),
        _play(1, Half.TOP, event="double"),
        _play(1, Half.TOP, event="triple", runs_scored=1),
        _play(1, Half.TOP, event="home_run", runs_scored=1),
        _play(1, Half.TOP, event="field_out"),  # not a hit
        _play(1, Half.TOP, event="walk"),  # not a hit
        _play(1, Half.TOP, event="strikeout"),  # not a hit
        # Home (bottom) hits: 1 single.
        _play(1, Half.BOTTOM, event="single"),
        _play(1, Half.BOTTOM, event="field_out"),
    ]
    ls = linescore_from_plays(plays)
    assert ls.away_hits == 4
    assert ls.home_hits == 1


def test_hits_tolerate_canonical_event_naming():
    # canonical_event populated, raw event left None -> still counts as a hit.
    plays = [
        _play(1, Half.TOP, canonical_event="double"),
        _play(1, Half.TOP, canonical_event="home_run", runs_scored=1),
    ]
    ls = linescore_from_plays(plays)
    assert ls.away_hits == 2


def test_reach_on_error_is_not_a_hit_even_if_canonical_maps_to_single():
    # SIM-312 maps a reach-on-error to the run-value of a "single", but it must
    # NOT be counted as a base hit -- and it IS an error.
    plays = [
        _play(1, Half.TOP, event="field_error", canonical_event="single", is_error=True),
    ]
    ls = linescore_from_plays(plays)
    assert ls.away_hits == 0
    # The error is charged to the home (fielding) defense -- see error tests.
    assert ls.home_errors == 1


# ===========================================================================
# Errors -- charged to the FIELDING side
# ===========================================================================


def test_errors_charged_to_fielding_side():
    plays = [
        # Error in the TOP half (away batting) -> charged to HOME defense.
        _play(1, Half.TOP, event="field_error", is_error=True),
        # Error in the BOTTOM half (home batting) -> charged to AWAY defense.
        _play(1, Half.BOTTOM, event="field_error", is_error=True),
        _play(1, Half.BOTTOM, event="field_error", is_error=True),
    ]
    ls = linescore_from_plays(plays)
    assert ls.home_errors == 1  # the top-half error
    assert ls.away_errors == 2  # the two bottom-half errors


# ===========================================================================
# Extra innings
# ===========================================================================


def test_extra_innings_extend_the_grid():
    plays = []
    # 9 scoreless innings for both sides.
    for inning in range(1, 10):
        plays.append(_play(inning, Half.TOP, runs_scored=0, event="field_out"))
        plays.append(_play(inning, Half.BOTTOM, runs_scored=0, event="field_out"))
    # 10th: away scores 1 in the top, home walks it off with 2 in the bottom.
    plays.append(_play(10, Half.TOP, runs_scored=1, event="single"))
    plays.append(_play(10, Half.BOTTOM, runs_scored=2, event="home_run"))

    ls = linescore_from_plays(plays)
    assert ls.n_innings == 10
    assert ls.away_by_inning[-1] == 1
    assert ls.home_by_inning[-1] == 2
    assert ls.away_runs == 1
    assert ls.home_runs == 2


# ===========================================================================
# In-progress / unplayed bottom half
# ===========================================================================


def test_bottom_of_last_inning_unplayed_is_none_not_zero():
    # Home team leads after 8.5; never bats in the bottom of the 9th.
    plays = [
        _play(8, Half.TOP, runs_scored=0, event="field_out"),
        _play(8, Half.BOTTOM, runs_scored=1, event="home_run"),
        _play(9, Half.TOP, runs_scored=0, event="strikeout"),
        # No bottom-of-9 plays.
    ]
    ls = linescore_from_plays(plays)
    assert ls.n_innings == 9
    last = ls.innings[-1]
    assert last.inning == 9
    assert last.away == 0  # top of 9 was played, scoreless
    assert last.away_played is True
    assert last.home is None  # bottom of 9 NOT played -> None, not 0
    assert last.home_played is False
    assert ls.home_by_inning[-1] is None
    # Totals only sum the played cells.
    assert ls.home_runs == 1
    assert ls.away_runs == 0


def test_in_progress_top_only_inning_first():
    # A game frozen mid-top-of-1: only the away half exists.
    plays = [_play(1, Half.TOP, runs_scored=0, event="walk")]
    ls = linescore_from_plays(plays)
    assert ls.n_innings == 1
    assert ls.innings[0].away == 0
    assert ls.innings[0].home is None
    assert ls.innings[0].home_played is False


# ===========================================================================
# Robustness
# ===========================================================================


def test_plays_without_next_state_are_skipped():
    good = _play(1, Half.TOP, runs_scored=2, event="home_run")
    orphan = PlayResult(pitch_outcome="ball", next_state=None)
    ls = linescore_from_plays([orphan, good, orphan])
    assert ls.n_innings == 1
    assert ls.away_by_inning == [2]
    assert ls.away_hits == 1


def test_hit_events_constant_is_the_four_hit_types():
    assert frozenset({"single", "double", "triple", "home_run"}) == HIT_EVENTS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
