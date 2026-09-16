"""
test_sim434_manager_model.py
============================
SIM-434 -- what is left of the manager decision model after the SIM-427 flip
(2026-09-13) deleted its formulas: the **pitch-count fix** and the
**SIM_MANAGER gate** (the manager wiring in the production factory and the
``simulate_game`` passthrough).

WHAT THIS COVERS
----------------
  1. **The pitch-count fix** -- ``GameState.pitcher_pitch_count`` advances by
     exactly 1 per pitch in ``step_pitch`` (it was NEVER incremented before
     SIM-434), an IBB throws no pitch (no increment), and a pitching change
     resets it for the new arm.
  2. **The SIM_MANAGER gate** -- with the flag OFF the production factory wires
     NO manager (the game is the manager-less default == byte-identical); with
     it ON the gate marker + a per-team bullpen are wired and ``SIM_MANAGER_DRAW``
     sets the machine's draw switch.
  3. **The passthrough** -- ``simulate_game`` seeds the bullpen (an explicit
     ``bullpen=`` beats the machine-staged one); with the draw on and a change
     pool in the bundle the starter is pulled by the DRAW and the entering arm
     comes from the fielding side's pen; with no manager nothing fires.

WHAT LEFT AT THE FLIP (do not re-add)
------------------------------------
The pull formula (``_PULL_PITCH_FLOOR`` / ``_CEILING``), ``pitcher_fatigue``,
``tto_effectiveness``, ``platoon_factor``, ``score_reliever`` and the by-name
tendency reader: the pitching change is a draw from the change opportunity
pool (``tests/unit/test_sim523_manager_draw.py``) and the entering arm a draw
from the live pen (``tests/unit/test_sim427_relief_draw.py``). With the draw
switch off no pitcher is ever changed.

HOW THIS STAYS NO-DB (the existing SIM-316/323 pattern)
-------------------------------------------------------
Every machine is rng-driven with a fixed seed; the full games draw from the
in-memory synthetic bundle (SIM-486), whose change pool carries the pool's own
change rates per cell (the rates are set to 1.0 where a test needs a certain
pull).  The factory tests inject a mock sampler builder + a stub bullpen
builder via the production-factory seams.
"""

from __future__ import annotations

import numpy as np
import pytest

import simulation.production_factory as pf
from simulation.batch_runner import GameSpec
from simulation.game_state import Bases, GameState, Half, Team
from simulation.sim_loop import StateMachine, simulate_game
from simulation.synthetic_bundle import change_pool, league_artifacts, synthetic_sampler

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))

#: The gate marker: any non-None manager turns the hooks on (the profiles
#: themselves ride on the game state since SIM-427).
_GATE: dict[str, float] = {}


# ===========================================================================
# Helpers (mirror the SIM-323 test fixtures)
# ===========================================================================


def _state(
    *,
    inning: int = 9,
    half: Half = Half.TOP,
    home_score: int = 0,
    away_score: int = 0,
    first=None,
    second=None,
    third=None,
    outs: int = 0,
    batter_id: int = 101,
    pitch_count: int = 0,
) -> GameState:
    s = GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON)
    s.inning = inning
    s.half = half
    s.home_score = home_score
    s.away_score = away_score
    s.outs = outs
    s.bases = Bases(first=first, second=second, third=third)
    s.batter_id = batter_id
    s.pitcher_pitch_count = pitch_count
    return s


def _machine(manager=None, *, seed: int = 7, ibb_rates=None) -> StateMachine:
    return StateMachine(rng=np.random.default_rng(seed), manager=manager, ibb_rates=ibb_rates)


class _ScriptedDraw:
    """A duck-typed sampler whose change draw answers a fixed script (no
    ``relief_arm_draw`` -> the positional pick)."""

    def __init__(self, answers):
        self.answers = list(answers)

    def pitching_change_draw(self, pitcher_key, **kw):
        return self.answers.pop(0) if self.answers else None


def _certain_pull_sampler(seed: int = 0):
    """The league bundle with a change pool that ALWAYS changes a starter at
    90+ pitches on a half-inning boundary (rate 1.0 in that cell) and never
    otherwise -- a deterministic pull for the passthrough tests."""
    art = league_artifacts()
    art.change_pool = change_pool(
        {
            "starter_half_low": 0.0,
            "starter_half_mid": 0.0,
            "starter_half_high": 1.0,
            "starter_mid": 0.0,
            "reliever_half": 0.0,
            "reliever_mid": 0.0,
        },
        season=SEASON,
    )
    fp = synthetic_sampler(art, seed=seed)
    fp.change_min_cell = 1  # the hard cell decides alone (no widening into other buckets)
    return fp


# ===========================================================================
# 1. The pitch-count fix
# ===========================================================================


class TestPitchCountIncrement:
    def test_pitch_count_advances_one_per_pitch(self):
        # A count-machine-only machine (no sampler): drive explicit pitch outcomes
        # and assert the pitcher's pitch count climbs by exactly one each pitch.
        m = _machine(seed=1)
        s = GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON)
        assert s.pitcher_pitch_count == 0
        m.step_pitch(s, pitch_outcome="ball")
        assert s.pitcher_pitch_count == 1
        m.step_pitch(s, pitch_outcome="called_strike")
        assert s.pitcher_pitch_count == 2
        m.step_pitch(s, pitch_outcome="ball")
        assert s.pitcher_pitch_count == 3

    def test_the_count_keeps_climbing_across_plate_appearances(self):
        # Before the fix the count was stuck at 0.  Drive 80 pitches: the PA
        # rolls over but the SAME pitcher keeps accruing pitches (no manager ->
        # no change), so the count reads the whole outing.
        m = _machine(seed=1)
        s = GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON)
        for _ in range(80):
            m.step_pitch(s, pitch_outcome="ball")
        assert s.pitcher_pitch_count == 80

    def test_ibb_throws_no_pitch_so_no_increment(self):
        # An IBB short-circuits step_pitch BEFORE the pitch is counted.
        # SIM-515: the decision draws at the injected cell rate (1.0 = certain).
        m = _machine(_GATE, seed=1, ibb_rates={(2, 0, True, True): 1.0})
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55, batter_id=201)
        s.home_lineup = HOME_LINEUP
        s.home_lineup_slot = 0
        before = s.pitcher_pitch_count
        result = m.step_pitch(s)
        assert result.event == "intentional_walk"  # SIM-515/516: its own class
        assert s.pitcher_pitch_count == before  # no pitch thrown on an IBB

    def test_a_change_resets_the_new_arms_pitch_count(self):
        # The drawn row says "changed": the positional pick brings in the
        # closer (high leverage, late) and the new arm starts at 0 pitches.
        bullpen = {Team.HOME: [301, 302, 303]}
        s = _state(inning=9, half=Half.TOP, home_score=2, away_score=2, second=55, pitch_count=105)
        s.manager.bullpen_available = bullpen
        m = StateMachine(_ScriptedDraw([True]), rng=np.random.default_rng(4), manager=_GATE)
        m.manager_draw = True
        m._end_of_pa_hook(s)
        assert s.pitcher_id == 301
        assert s.pitcher_pitch_count == 0
        assert m.manager_decisions[-1]["pitch_count"] == 105  # the outgoing arm's count


# ===========================================================================
# 2. The SIM_MANAGER gate (production factory)
# ===========================================================================


def _spec() -> GameSpec:
    return GameSpec(
        sim_kwargs={
            "pitcher_id": PITCHER,
            "bat_hand": "R",
            "season": SEASON,
            "home_pitcher_id": 222,
            "away_pitcher_id": 333,
        }
    )


class TestSimManagerGate:
    def teardown_method(self):
        pf.reset_caches()
        pf.set_bullpen_builder(None)

    def test_flag_off_wires_no_manager(self, monkeypatch):
        monkeypatch.delenv("SIM_MANAGER", raising=False)
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: synthetic_sampler())
        machine = pf.production_machine_factory(7, _spec())
        assert machine.manager is None  # byte-identical: no manager hooks run
        assert getattr(machine, "bullpen", None) is None
        assert machine.manager_draw is False

    def test_flag_off_explicit_zero_wires_no_manager(self, monkeypatch):
        monkeypatch.setenv("SIM_MANAGER", "0")
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: synthetic_sampler())
        machine = pf.production_machine_factory(7, _spec())
        assert machine.manager is None

    def test_flag_on_wires_the_gate_and_a_bullpen(self, monkeypatch):
        monkeypatch.setenv("SIM_MANAGER", "1")
        monkeypatch.setenv("SIM_MANAGER_DRAW", "1")
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: synthetic_sampler())
        machine = pf.production_machine_factory(7, _spec())
        # The gate marker (a dict, possibly empty) -- not None -- turns the hooks on.
        assert machine.manager is not None
        assert machine.manager_draw is True
        bullpen = getattr(machine, "bullpen", None)
        assert bullpen is not None
        # A generic per-team pen keyed by the int Team value (0=away, 1=home).
        assert bullpen[0] and bullpen[1]

    def test_flag_on_with_the_draw_off_keeps_the_switch_off(self, monkeypatch):
        monkeypatch.setenv("SIM_MANAGER", "1")
        monkeypatch.setenv("SIM_MANAGER_DRAW", "0")
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: synthetic_sampler())
        machine = pf.production_machine_factory(7, _spec())
        assert machine.manager is not None and machine.manager_draw is False

    def test_flag_on_uses_the_injected_bullpen_builder(self, monkeypatch):
        monkeypatch.setenv("SIM_MANAGER", "1")
        pf.set_bullpen_builder(lambda spec: {0: [11, 12], 1: [21, 22]})
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: synthetic_sampler())
        machine = pf.production_machine_factory(7, _spec())
        assert machine.bullpen == {0: [11, 12], 1: [21, 22]}


# ===========================================================================
# 3. simulate_game passthrough -- flag-off no-op vs the draw's pull
# ===========================================================================


def _draw_machine(manager=_GATE, *, seed=5) -> StateMachine:
    m = StateMachine(_certain_pull_sampler(seed), rng=np.random.default_rng(seed), manager=manager)
    m.manager_draw = manager is not None
    return m


def _loaded_state(*, away_starter=333, home_starter=222, pitches=130) -> GameState:
    # A fresh top of the 1st with the pitcher on the mound ALREADY at 130
    # pitches, so the first plate appearance's boundary sits in the certain-
    # pull cell (a starter, a half-inning boundary, 90+ pitches).
    st = GameState(
        pitcher_id=away_starter,
        bat_hand="R",
        season=SEASON,
        away_lineup=AWAY_LINEUP,
        home_lineup=HOME_LINEUP,
        batter_id=AWAY_LINEUP[0],
    )
    st.home_pitcher_id = home_starter
    st.away_pitcher_id = away_starter
    st.pitcher_pitch_count = pitches
    return st


class TestSimulateGamePassthrough:
    def test_flag_off_path_is_a_no_op(self):
        # No manager passed + machine has no manager -> no change, manager stays None.
        sm = StateMachine(synthetic_sampler(seed=5), rng=np.random.default_rng(5))
        r = simulate_game(
            sm,
            seed=5,
            pitcher_id=PITCHER,
            away_lineup=AWAY_LINEUP,
            home_lineup=HOME_LINEUP,
        )
        assert sm.manager is None
        assert not any(d["kind"] == "pitching_change" for d in sm.manager_decisions)
        # The starter was never pulled -> the final pitcher is still the starter.
        assert r.final_state.pitcher_id == PITCHER

    def test_the_draw_pulls_the_starter_and_the_arm_comes_from_the_fielding_pen(self):
        # A machine with the gate + the draw + a wired bullpen: the drawn row
        # for the 130-pitch pitcher says "changed" on the first boundary and a
        # reliever from the HOME pen (the HOME side fields the top half) enters.
        sm = _draw_machine(seed=5)
        bullpen = {Team.AWAY: [801, 802, 803, 804], Team.HOME: [811, 812, 813, 814]}
        simulate_game(sm, initial_state=_loaded_state(), seed=5, bullpen=bullpen)
        changes = [d for d in sm.manager_decisions if d["kind"] == "pitching_change"]
        assert changes  # at least one change fired
        first = changes[0]
        assert first["out_pitcher_id"] == 333 and first["source"] == "draw"
        assert first["pitch_count"] == 130 and first["new_half"] is True
        assert first["in_pitcher_id"] in [811, 812, 813, 814]
        # The caller's bullpen dict was COPIED (pops never mutate it).
        assert bullpen[Team.HOME] == [811, 812, 813, 814]

    def test_machine_staged_bullpen_is_used_when_no_explicit_bullpen(self):
        # The production factory stages a bullpen on the machine (keyed by the int
        # Team value, as _default_bullpen_for_spec does); simulate_game seeds it
        # onto the state when no explicit bullpen= is passed.
        sm = _draw_machine(seed=5)
        sm.bullpen = {0: [901, 902, 903], 1: [911, 912, 913]}
        simulate_game(sm, initial_state=_loaded_state(), seed=5)
        changes = [d for d in sm.manager_decisions if d["kind"] == "pitching_change"]
        assert changes
        assert changes[0]["in_pitcher_id"] in [911, 912, 913]

    def test_explicit_bullpen_overrides_the_machine_staged_one(self):
        sm = _draw_machine(seed=5)
        sm.bullpen = {Team.HOME: [-1], Team.AWAY: [-2]}
        explicit = {Team.HOME: [711, 712, 713], Team.AWAY: [721, 722, 723]}
        simulate_game(sm, initial_state=_loaded_state(), seed=5, bullpen=explicit)
        changes = [d for d in sm.manager_decisions if d["kind"] == "pitching_change"]
        assert changes
        for d in changes:
            assert d["in_pitcher_id"] not in (-1, -2)

    def test_the_new_identity_fields_land_on_the_state(self):
        # SIM-427: the managers, the pen's recent usage and source, and the
        # per-side profiles ride through simulate_game onto the state.
        sm = StateMachine(synthetic_sampler(seed=3), rng=np.random.default_rng(3))
        r = simulate_game(
            sm,
            seed=3,
            pitcher_id=PITCHER,
            away_lineup=AWAY_LINEUP,
            home_lineup=HOME_LINEUP,
            home_manager_id=11,
            away_manager_id=22,
            pitcher_recent_usage={901: (1, 1, 30), "902": [5, 0, 0]},
            bullpen_source="box",
            manager_profiles={0: {"steal_order_rate_per_1b_opp": 0.05}, "1": {"x": 1.0}},
            manager_league_profile={"steal_order_rate_per_1b_opp": 0.04},
        )
        st = r.final_state
        assert (st.home_manager_id, st.away_manager_id) == (11, 22)
        assert st.pitcher_recent_usage == {901: (1, 1, 30), 902: (5, 0, 0)}
        assert st.bullpen_source == "box"
        assert st.away_manager_profile == {"steal_order_rate_per_1b_opp": 0.05}
        assert st.home_manager_profile == {"x": 1.0}
        assert st.manager_league_profile == {"steal_order_rate_per_1b_opp": 0.04}


# ===========================================================================
# Byte-identical guarantee: flag-off game == manager-less game shape
# ===========================================================================


class TestByteIdenticalWithFlagOff:
    def test_no_manager_game_is_reproducible_and_pulls_nothing(self):
        # Two runs at the same seed with no manager produce the identical final
        # score / innings / pitch total (the increment consumes no rng), and the
        # starter is never pulled.
        def _run():
            sm = StateMachine(synthetic_sampler(seed=9), rng=np.random.default_rng(9))
            return simulate_game(
                sm,
                seed=9,
                pitcher_id=PITCHER,
                away_lineup=AWAY_LINEUP,
                home_lineup=HOME_LINEUP,
            )

        a, b = _run(), _run()
        assert (a.home_score, a.away_score) == (b.home_score, b.away_score)
        assert a.innings_played == b.innings_played
        assert a.total_pitches == b.total_pitches
        assert a.final_state.pitcher_id == PITCHER


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
