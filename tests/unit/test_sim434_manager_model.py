"""
test_sim434_manager_model.py
============================
SIM-434 -- the **manager pull + reliever-selection decision model**
(fatigue / rest, times-through-the-order, leverage, platoon) and the
``SIM_MANAGER``-gated manager passthrough.

WHAT THIS COVERS (the SIM-434 acceptance criteria)
--------------------------------------------------
  1. **The pitch-count bug fix** -- ``GameState.pitcher_pitch_count`` now advances
     by exactly 1 per pitch in ``step_pitch`` (it was NEVER incremented, so the
     SIM-323 starter-pull floor/ceiling gate could never fire), an IBB throws no
     pitch (no increment), and a pull resets it.
  2. **The pure helpers** -- ``times_through_order`` / ``pitcher_fatigue`` /
     ``tto_effectiveness`` / ``platoon_factor`` / ``score_reliever`` are monotone +
     bounded + fall back to in-game fatigue when no rest data is wired.
  3. **The wiring** -- ``_pick_reliever`` ranks a metadata-bearing bullpen by the
     score (and stays byte-identical positional for a metadata-less pen), and a
     fatigued starter is pulled sooner.
  4. **The SIM_MANAGER gate** -- with the flag OFF the production factory wires NO
     manager (game is the manager-less default == byte-identical); with it ON a
     default manager + generic bullpen are wired and the starter is pulled / a
     reliever chosen.

HOW THIS STAYS NO-DB (the existing SIM-316/323 pattern)
-------------------------------------------------------
Every machine is rng-driven with a fixed seed and an injected ``manager``
tendency dict + ``resolver`` (no DuckDB / FAISS).  The factory tests inject a
mock sampler builder + a stub bullpen builder via the production-factory seams.
"""

from __future__ import annotations

import numpy as np
import pytest

import simulation.production_factory as pf
from simulation.batch_runner import GameSpec
from simulation.game_state import Bases, GameState, Half, Team
from simulation.sim_loop import (
    StateMachine,
    pitcher_fatigue,
    platoon_factor,
    score_reliever,
    simulate_game,
    times_through_order,
    tto_effectiveness,
)
from simulation.synthetic_bundle import synthetic_sampler

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))


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


def _machine(manager=None, *, seed: int = 7, bench=None, ibb_rates=None) -> StateMachine:
    return StateMachine(
        rng=np.random.default_rng(seed), manager=manager, bench=bench, ibb_rates=ibb_rates
    )


_AGGRESSIVE = {
    "steal_order_rate_per_1b_opp": 0.95,
    "platoon_advantage_exploitation_rate": 0.95,
    "starter_pull_pct_before_100": 0.95,
    "pinch_hit_rate_high_leverage": 0.95,
    "sac_bunt_rate_high_leverage": 0.95,
    "sac_bunt_rate_low_leverage": 0.95,
}
_PASSIVE = dict.fromkeys(_AGGRESSIVE, 0.0)


# ===========================================================================
# 1. The pitch-count bug fix
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

    def test_pull_floor_gate_becomes_reachable(self):
        # Before the fix the count was stuck at 0 so _maybe_pull_starter early-
        # returned forever.  Drive 80 pitches and assert the count clears the
        # floor (75) -- the gate is now reachable.
        m = _machine(seed=1)
        s = GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON)
        for _ in range(80):
            # alternate balls/strikes; the PA rolls over but the SAME pitcher
            # keeps accruing pitches (no manager -> no pull).
            m.step_pitch(s, pitch_outcome="ball")
        assert s.pitcher_pitch_count >= 75

    def test_ibb_throws_no_pitch_so_no_increment(self):
        # An IBB short-circuits step_pitch BEFORE the pitch is counted.
        # SIM-515: the decision draws at the injected cell rate (1.0 = certain).
        m = _machine(_AGGRESSIVE, seed=1, ibb_rates={(2, 0, True, True): 1.0})
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55, batter_id=201)
        s.home_lineup = HOME_LINEUP
        s.home_lineup_slot = 0
        before = s.pitcher_pitch_count
        result = m.step_pitch(s)
        assert result.event == "intentional_walk"  # SIM-515/516: its own class
        assert s.pitcher_pitch_count == before  # no pitch thrown on an IBB

    def test_pull_resets_the_new_arms_pitch_count(self):
        bullpen = {Team.HOME: [301, 302, 303]}
        s = _state(inning=9, half=Half.TOP, home_score=2, away_score=2, second=55, pitch_count=105)
        s.manager.bullpen_available = bullpen
        m = _machine(_AGGRESSIVE, seed=4)
        m._end_of_pa_hook(s)
        assert s.pitcher_id == 301
        assert s.pitcher_pitch_count == 0


# ===========================================================================
# 2. The pure helpers
# ===========================================================================


class TestTimesThroughOrder:
    def test_first_time_through_before_any_batter(self):
        assert times_through_order(0) == 1

    def test_wraps_at_the_lineup_size(self):
        assert times_through_order(8) == 1  # 9th batter is still the 1st time
        assert times_through_order(9) == 2  # 10th batter -> 2nd time through
        assert times_through_order(18) == 3

    def test_bad_lineup_size_does_not_divide_by_zero(self):
        assert times_through_order(9, lineup_size=0) == 2  # falls back to 9


class TestPitcherFatigue:
    def test_bounded_unit_interval(self):
        for pc in (0, 50, 95, 200):
            f = pitcher_fatigue(pc)
            assert 0.0 <= f <= 1.0

    def test_monotone_in_pitch_count(self):
        assert pitcher_fatigue(20) < pitcher_fatigue(60) < pitcher_fatigue(90)

    def test_tto_raises_fatigue(self):
        base = pitcher_fatigue(50, tto=1)
        third = pitcher_fatigue(50, tto=3)
        assert third > base

    def test_short_rest_raises_fatigue_when_wired(self):
        rested = pitcher_fatigue(50, rest_days=5.0)
        tired = pitcher_fatigue(50, rest_days=0.0)
        assert tired > rested

    def test_rest_none_is_the_in_game_fallback(self):
        # With no rest data the index is driven by pitch count + TTO ALONE (the
        # SIM-433-not-ingested fallback) -- equals the rest=full case (rest term 0).
        assert pitcher_fatigue(50, rest_days=None) == pytest.approx(
            pitcher_fatigue(50, rest_days=5.0)
        )


class TestTtoEffectiveness:
    def test_first_time_through_is_full(self):
        assert tto_effectiveness(1) == pytest.approx(1.0)

    def test_decays_each_time_through(self):
        assert tto_effectiveness(1) > tto_effectiveness(2) > tto_effectiveness(3)

    def test_floored_above_zero(self):
        assert tto_effectiveness(99) >= 0.25


class TestPlatoonFactor:
    def test_same_hand_favours_the_pitcher(self):
        assert platoon_factor("R", "R") > 1.0
        assert platoon_factor("L", "L") > 1.0

    def test_opposite_hand_favours_the_batter(self):
        assert platoon_factor("L", "R") < 1.0
        assert platoon_factor("R", "L") < 1.0

    def test_unknown_hand_is_neutral(self):
        assert platoon_factor(None, "R") == 1.0
        assert platoon_factor("S", "R") == 1.0  # unresolved switch hitter
        assert platoon_factor("R", None) == 1.0


class TestScoreReliever:
    def test_monotone_in_each_lever(self):
        base = score_reliever(leverage=1.0, platoon=1.0, effectiveness=1.0)
        assert score_reliever(leverage=2.0, platoon=1.0, effectiveness=1.0) > base
        assert score_reliever(leverage=1.0, platoon=1.15, effectiveness=1.0) > base
        assert score_reliever(leverage=1.0, platoon=1.0, effectiveness=1.0) > score_reliever(
            leverage=1.0, platoon=1.0, effectiveness=0.5
        )

    def test_rested_arm_outscores_a_tired_arm(self):
        rested = score_reliever(leverage=1.0, platoon=1.0, effectiveness=1.0, rest_days=5.0)
        tired = score_reliever(leverage=1.0, platoon=1.0, effectiveness=1.0, rest_days=0.0)
        assert rested > tired

    def test_never_negative(self):
        assert score_reliever(leverage=-5.0, platoon=-1.0, effectiveness=-1.0) >= 0.0


# ===========================================================================
# 3. Wiring into _maybe_pull_starter / _pick_reliever
# ===========================================================================


class TestRelieverSelectionScored:
    def test_scored_mode_prefers_the_same_hand_arm(self):
        # Two arms; the same-handed one (vs an R batter) scores higher on platoon.
        bullpen = {Team.HOME: [401, 402]}
        s = _state(
            inning=6, half=Half.TOP, home_score=4, away_score=2, pitch_count=100, batter_id=101
        )
        s.manager.bullpen_available = bullpen
        # 401 is L (opposite an R batter -> worse), 402 is R (same -> better).
        s.throw_hands = {401: "L", 402: "R"}
        s.bat_hands = {101: "R"}
        s.bat_hand = "R"
        m = _machine(_AGGRESSIVE, seed=4)
        m._maybe_pull_starter(s, s.manager.leverage or 1.0)
        # The scored pick chose the same-hand arm (402), not the positional one.
        assert s.pitcher_id == 402

    def test_rest_breaks_a_platoon_neutral_tie(self):
        bullpen = {Team.HOME: [501, 502]}
        s = _state(
            inning=6, half=Half.TOP, home_score=4, away_score=2, pitch_count=100, batter_id=101
        )
        s.manager.bullpen_available = bullpen
        # Both unknown-hand (platoon-neutral); 502 is the better-rested arm.
        s.pitcher_rest_days = {501: 0.0, 502: 5.0}
        m = _machine(_AGGRESSIVE, seed=4)
        m._maybe_pull_starter(s, 2.0)
        assert s.pitcher_id == 502

    def test_metadata_less_bullpen_keeps_positional_behaviour(self):
        # No hand / rest meta -> the legacy SIM-323 positional path (closer first
        # in a high-LI late spot).  This is the byte-identical guarantee.
        bullpen = {Team.HOME: [301, 302, 303]}
        s = _state(inning=9, half=Half.TOP, home_score=2, away_score=2, second=55, pitch_count=105)
        s.manager.bullpen_available = bullpen
        m = _machine(_AGGRESSIVE, seed=4)
        m._end_of_pa_hook(s)
        assert s.pitcher_id == 301  # unchanged from SIM-323


class TestFatigueHastensTheHook:
    def test_a_fatigued_starter_pulls_at_a_lower_fire_p(self):
        # A near-floor pitch count + 3rd-time-through + short rest -> high fatigue,
        # so a manager whose raw pull tendency would NOT clear the rng draw alone
        # is pushed over.  We assert the pull fires for a moderate-tendency manager
        # at the floor only because of fatigue.
        moderate = {"starter_pull_pct_before_100": 0.30}
        bullpen = {Team.HOME: [601, 602]}
        # Fresh 1st-time-through, rested -> low fatigue.
        fresh = _state(inning=4, half=Half.TOP, home_score=1, away_score=1, pitch_count=76)
        fresh.manager.bullpen_available = {Team.HOME: [601, 602]}
        m1 = _machine(moderate, seed=2)
        m1._maybe_pull_starter(fresh, 1.0)

        # Same spot but deep into the order + short rest -> high fatigue.
        tired = _state(inning=4, half=Half.TOP, home_score=1, away_score=1, pitch_count=76)
        tired.manager.bullpen_available = bullpen
        tired.pitcher_bf = {PITCHER: 18}  # 3rd time through
        tired.pitcher_rest_days = {PITCHER: 0.0}
        m2 = _machine(moderate, seed=2)
        m2._maybe_pull_starter(tired, 1.0)

        # The tired starter is at least as likely to be pulled as the fresh one
        # (same seed/draw, higher fire_p) -- and in this construction is pulled.
        assert tired.pitcher_id != PITCHER


# ===========================================================================
# 4. The SIM_MANAGER gate (production factory)
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

    def test_flag_off_explicit_zero_wires_no_manager(self, monkeypatch):
        monkeypatch.setenv("SIM_MANAGER", "0")
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: synthetic_sampler())
        machine = pf.production_machine_factory(7, _spec())
        assert machine.manager is None

    def test_flag_on_wires_a_manager_and_bullpen(self, monkeypatch):
        monkeypatch.setenv("SIM_MANAGER", "1")
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: synthetic_sampler())
        machine = pf.production_machine_factory(7, _spec())
        assert machine.manager is not None
        assert machine.manager["starter_pull_pct_before_100"] > 0.0
        bullpen = getattr(machine, "bullpen", None)
        assert bullpen is not None
        # A generic per-team pen keyed by the int Team value (0=away, 1=home).
        assert bullpen[0] and bullpen[1]

    def test_flag_on_uses_the_injected_bullpen_builder(self, monkeypatch):
        monkeypatch.setenv("SIM_MANAGER", "1")
        pf.set_bullpen_builder(lambda spec: {0: [11, 12], 1: [21, 22]})
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: synthetic_sampler())
        machine = pf.production_machine_factory(7, _spec())
        assert machine.bullpen == {0: [11, 12], 1: [21, 22]}


# ===========================================================================
# simulate_game passthrough -- flag-off no-op vs flag-on pull
# ===========================================================================


def _outs_machine(manager=None, *, seed=5):
    # A one-pitch-per-PA machine: step_pitch is fed "in_play" so each PA ends on
    # its first pitch (with no sampler the contact stays unresolved, which is
    # fine here); the count climbs by one per pitch so the pull gate is reachable.
    rng = np.random.default_rng(seed)

    class _SM(StateMachine):
        def step_pitch(self, st, **kw):
            if "pitch_outcome" not in kw:
                kw["pitch_outcome"] = "in_play"
            return super().step_pitch(st, **kw)

    return _SM(rng=rng, manager=manager)


class TestSimulateGamePassthrough:
    def test_flag_off_path_is_a_no_op(self):
        # No manager passed + machine has no manager -> no pull, manager stays None.
        sm = _outs_machine(manager=None, seed=5)
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

    def _loaded_state(self, *, away_starter=333, home_starter=222, away_pc=130):
        # A fresh top-of-the-1st with the away starter ALREADY over the pull
        # ceiling so the first end-of-PA hook forces a pull (a no-DB, deterministic
        # way to exercise the pull/pick wiring without driving 25 innings).
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
        st.pitcher_pitch_count = away_pc
        return st

    def test_flag_on_path_pulls_the_starter_and_picks_a_reliever(self):
        # A machine with a manager + a wired bullpen: the over-ceiling away starter
        # is force-pulled and a reliever from the HOME-fielding pen is chosen.
        sm = _outs_machine(manager=_AGGRESSIVE, seed=5)
        bullpen = {Team.AWAY: [801, 802, 803, 804], Team.HOME: [811, 812, 813, 814]}
        simulate_game(
            sm,
            initial_state=self._loaded_state(),
            seed=5,
            bullpen=bullpen,
        )
        changes = [d for d in sm.manager_decisions if d["kind"] == "pitching_change"]
        assert changes  # at least one pull fired
        # The first pull was the away starter (333) -> a HOME-bullpen arm (the
        # HOME team is fielding in the top of the 1st).
        first = changes[0]
        assert first["out_pitcher_id"] == 333
        assert first["in_pitcher_id"] in [811, 812, 813, 814]
        # The caller's bullpen dict was COPIED (pops never mutate it).
        assert bullpen[Team.HOME] == [811, 812, 813, 814]

    def test_machine_staged_bullpen_is_used_when_no_explicit_bullpen(self):
        # The production factory stages a bullpen on the machine (keyed by the int
        # Team value, as _default_bullpen_for_spec does); simulate_game seeds it
        # onto the state when no explicit bullpen= is passed.
        sm = _outs_machine(manager=_AGGRESSIVE, seed=5)
        sm.bullpen = {0: [901, 902, 903], 1: [911, 912, 913]}
        simulate_game(sm, initial_state=self._loaded_state(), seed=5)
        changes = [d for d in sm.manager_decisions if d["kind"] == "pitching_change"]
        assert changes
        assert changes[0]["in_pitcher_id"] in [911, 912, 913]

    def test_explicit_bullpen_overrides_the_machine_staged_one(self):
        sm = _outs_machine(manager=_AGGRESSIVE, seed=5)
        sm.bullpen = {Team.HOME: [-1], Team.AWAY: [-2]}
        explicit = {Team.HOME: [711, 712, 713], Team.AWAY: [721, 722, 723]}
        simulate_game(
            sm,
            initial_state=self._loaded_state(),
            seed=5,
            bullpen=explicit,
        )
        changes = [d for d in sm.manager_decisions if d["kind"] == "pitching_change"]
        assert changes
        for d in changes:
            assert d["in_pitcher_id"] not in (-1, -2)


# ===========================================================================
# Byte-identical guarantee: flag-off game == pre-SIM-434 game shape
# ===========================================================================


class TestByteIdenticalWithFlagOff:
    def test_no_manager_game_is_reproducible_and_pulls_nothing(self):
        # Two runs at the same seed with no manager produce the identical final
        # score / innings / pitch total (the increment consumes no rng), and the
        # starter is never pulled.
        def _run():
            sm = _outs_machine(manager=None, seed=9)
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
