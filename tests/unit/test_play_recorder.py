"""
test_play_recorder.py
=====================
Unit tests for simulation/play_recorder.py — the non-invasive capture of the
ordered PlayResult stream of ONE game (SIM-355 deliverable for the SIM-357
agent).

Covers:
  * record_game_plays returns a non-empty ORDERED list[PlayResult];
  * the captured list feeds PlayByPlay.from_play_results (SIM-331) and yields
    terminal-flagged entries (so PAs are inferable);
  * determinism — same seed/factory/sim_kwargs => identical ordered plays;
  * the RecordingMachine wrapper delegates attribute access to the inner machine
    (so simulate_game's rng re-seed / boxscore harvest reach the real machine);
  * the RecordingStateMachine subclass records when built directly.

All paths use the no-DB rng factory (the module default) so the whole game runs
with no live sampler / DuckDB / Postgres.

Owned by Backend Developer (SIM-355).
"""

from __future__ import annotations

from simulation.game_state import PlayResult
from simulation.play_recorder import (
    DEFAULT_FACTORY_REF,
    RecordingMachine,
    RecordingStateMachine,
    record_game_plays,
)
from simulation.snapshots import PlayByPlay

SIM_KWARGS = {
    "season": 2024,
    "pitcher_id": 600001,
    "bat_hand": "R",
    "away_lineup": [101, 102, 103, 104, 105, 106, 107, 108, 109],
    "home_lineup": [201, 202, 203, 204, 205, 206, 207, 208, 209],
    "max_innings": 12,
}


# ---------------------------------------------------------------------------
# record_game_plays
# ---------------------------------------------------------------------------


def test_record_game_plays_returns_ordered_play_results():
    result, plays = record_game_plays(seed=42, sim_kwargs=dict(SIM_KWARGS))
    # Non-empty ordered list of PlayResult.
    assert isinstance(plays, list)
    assert len(plays) > 0
    assert all(isinstance(p, PlayResult) for p in plays)
    # The recorded count matches the game's pitch tally (the wrapper saw every
    # step_pitch the loop drove).
    assert len(plays) == result.total_pitches


def test_recorded_plays_feed_playbyplay_with_terminals():
    _result, plays = record_game_plays(seed=7, sim_kwargs=dict(SIM_KWARGS))
    pbp = PlayByPlay.from_play_results(plays)
    assert pbp.n_pitches == len(plays)
    # At least one PA resolved (a terminal pitch), so PAs are inferable.
    assert pbp.n_plate_appearances > 0
    assert any(e.is_pa_end for e in pbp.entries)
    # Entry sequence is the global pitch index in order.
    assert [e.sequence for e in pbp.entries] == list(range(len(plays)))


def test_record_game_plays_is_deterministic_for_fixed_seed():
    _r1, plays1 = record_game_plays(seed=99, sim_kwargs=dict(SIM_KWARGS))
    _r2, plays2 = record_game_plays(seed=99, sim_kwargs=dict(SIM_KWARGS))
    sig1 = [(p.pitch_outcome, p.event, p.pa_terminal) for p in plays1]
    sig2 = [(p.pitch_outcome, p.event, p.pa_terminal) for p in plays2]
    assert sig1 == sig2


def test_record_game_plays_differs_across_seeds():
    _r1, plays1 = record_game_plays(seed=1, sim_kwargs=dict(SIM_KWARGS))
    _r2, plays2 = record_game_plays(seed=2, sim_kwargs=dict(SIM_KWARGS))
    sig1 = [(p.pitch_outcome, p.event) for p in plays1]
    sig2 = [(p.pitch_outcome, p.event) for p in plays2]
    assert sig1 != sig2


def test_default_factory_ref_is_the_no_db_rng_factory():
    assert DEFAULT_FACTORY_REF == "simulation.batch_runner:rng_driven_machine_factory"


def test_underscore_kwargs_are_factory_only():
    """A ``_``-prefixed key (e.g. _hit_rate) is read by the factory but must NOT
    be splatted into simulate_game (which has a fixed signature) — i.e. the call
    must not raise a TypeError."""
    kwargs = dict(SIM_KWARGS)
    kwargs["_hit_rate"] = 0.9
    result, plays = record_game_plays(seed=5, sim_kwargs=kwargs)
    assert len(plays) > 0
    assert isinstance(result.total_pitches, int)


# ---------------------------------------------------------------------------
# RecordingMachine wrapper
# ---------------------------------------------------------------------------


class _DummyResult:
    def __init__(self, tag):
        self.tag = tag


class _DummyMachine:
    """A stand-in machine to prove the wrapper's delegation + recording."""

    def __init__(self):
        self.rng = "original-rng"
        self.boxscore = "the-boxscore"
        self._calls = 0

    def step_pitch(self, state, **kwargs):
        self._calls += 1
        return _DummyResult(self._calls)


def test_recording_machine_records_and_returns_verbatim():
    inner = _DummyMachine()
    rec = RecordingMachine(inner)
    r1 = rec.step_pitch("state")
    r2 = rec.step_pitch("state")
    # The wrapper returns exactly what the inner machine returned.
    assert r1.tag == 1 and r2.tag == 2
    # ...and recorded them in order.
    assert rec.recorded_plays == [r1, r2]
    assert inner._calls == 2


def test_recording_machine_delegates_attribute_reads():
    inner = _DummyMachine()
    rec = RecordingMachine(inner)
    # Reads fall through to the inner machine (boxscore harvest / rng access).
    assert rec.rng == "original-rng"
    assert rec.boxscore == "the-boxscore"


def test_recording_machine_delegates_attribute_writes():
    """simulate_game does ``state_machine.rng = ...`` — that write must land on
    the INNER machine, not the wrapper, so the loop's re-seed takes effect."""
    inner = _DummyMachine()
    rec = RecordingMachine(inner)
    rec.rng = "reseeded"
    assert inner.rng == "reseeded"
    # The wrapper-owned attrs are NOT forwarded.
    rec.recorded_plays.append("x")
    assert "x" in rec.recorded_plays
    assert not hasattr(inner, "recorded_plays")


# ---------------------------------------------------------------------------
# RecordingStateMachine subclass
# ---------------------------------------------------------------------------


def test_recording_state_machine_subclass_records():
    """Built directly (no factory), the subclass records each count-machine pitch."""
    from simulation.game_state import GameState

    machine = RecordingStateMachine()  # no sampler => count-machine-only mode
    state = GameState(pitcher_id=1, bat_hand="R", season=2024)
    # Drive a few deterministic pitches by supplying the outcome directly.
    machine.step_pitch(state, pitch_outcome="ball")
    machine.step_pitch(state, pitch_outcome="called_strike")
    assert len(machine.recorded_plays) == 2
    assert all(isinstance(p, PlayResult) for p in machine.recorded_plays)
    assert machine.recorded_plays[0].pitch_outcome == "ball"
    assert machine.recorded_plays[1].pitch_outcome == "called_strike"
