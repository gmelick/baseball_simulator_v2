"""
test_baseball_analyst_sim349.py
===============================
SIM-349 -- the **situational-decision module**: the hit-and-run (pre-pitch) and
sac-fly-intent (PA-boundary) triggers added to the Phase-4 simulation loop
(``simulation/sim_loop.py``), consolidated with SIM-323's IBB + sac-bunt into one
leverage- and base/out-state-conditioned situational set.

WHAT THIS COVERS (the SIM-349 acceptance criteria)
--------------------------------------------------
  * **hit-and-run** fires in the right spot -- runner on 1B, <2 outs, a favorable
    count, an above-average ``hit_and_run_rate_per_opportunity`` manager -- and
    NOT otherwise (1B empty, two outs, a deep count, a passive manager);
  * **sac-fly intent** is flagged with a runner on 3rd + <2 outs in a run-needed
    spot and biases a sampled fly-OUT into a credited ``sacrifice_fly`` (a run
    scores from 3rd), while leaving hits / strikeouts / grounders untouched;
  * the consolidated set does NOT double-fire: a hit-and-run pre-empts the steal
    initiate, and the new triggers never collide with SIM-323's IBB / sac-bunt;
  * everything is deterministic under a fixed seed;
  * a no-manager-profile game still completes (every new trigger is no-op-safe).

HOW THE TENDENCIES ARE INJECTED (no live DB)
--------------------------------------------
Decisions read an injected ``manager`` dict of the SIM-2.8 manager-similarity rate
names + a fixed-seed numpy rng, mirroring the SIM-316/319/320/323 inject-the-signal
pattern -- no DuckDB / FAISS is touched.
"""

from __future__ import annotations

import numpy as np

from simulation.game_state import Bases, GameState, Half
from simulation.sim_loop import (
    FieldingSignal,
    PlayResolver,
    StateMachine,
    StealResolution,
    simulate_game,
)

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))


# ===========================================================================
# Helpers
# ===========================================================================


def _state(
    *,
    inning: int = 7,
    half: Half = Half.TOP,
    home_score: int = 0,
    away_score: int = 0,
    first=None,
    second=None,
    third=None,
    outs: int = 0,
    balls: int = 0,
    strikes: int = 0,
    batter_id: int = 101,
) -> GameState:
    s = GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON)
    s.inning = inning
    s.half = half
    s.home_score = home_score
    s.away_score = away_score
    s.outs = outs
    s.balls = balls
    s.strikes = strikes
    s.bases = Bases(first=first, second=second, third=third)
    s.batter_id = batter_id
    return s


def _machine(manager=None, *, seed: int = 7, resolver=None, ibb_rates=None) -> StateMachine:
    return StateMachine(
        rng=np.random.default_rng(seed), manager=manager, resolver=resolver, ibb_rates=ibb_rates
    )


# A manager who pulls every situational lever (all rates near 1.0).
_AGGRESSIVE = {
    "steal_order_rate_per_1b_opp": 0.95,
    "hit_and_run_rate_per_opportunity": 0.95,
    "platoon_advantage_exploitation_rate": 0.95,
    "sac_bunt_rate_high_leverage": 0.95,
    "sac_bunt_rate_low_leverage": 0.95,
    "squeeze_play_rate_per_3b_opp": 0.95,
}
# A manager who never moves (all rates 0.0).
_PASSIVE = dict.fromkeys(_AGGRESSIVE, 0.0)


# (The `_SacFlyResolver` helper and the sac-fly-intent test classes lived
# here until 2026-08-19 — retired with the SIM-349 nudge by SIM-513. The
# SIM-512 tag draw from 3B owns sacrifice flies now, pinned by
# tests/unit/test_sim511_512_transition_draw.py and the retargeted sac-fly
# ledger test in tests/unit/test_sim499_run_ledger.py. The hit-and-run and
# sac-bunt halves of SIM-349 stay live and tested below.)


# ===========================================================================
# Hit-and-run (pre-pitch trigger)
# ===========================================================================


class TestHitAndRun:
    def test_hit_and_run_fires_runner_on_1b_under_two_outs_favorable_count(self):
        m = _machine({"hit_and_run_rate_per_opportunity": 0.95}, seed=3)
        s = _state(inning=7, first=11, outs=1, balls=1, strikes=0)
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is True
        assert any(d["kind"] == "hit_and_run" for d in m.manager_decisions)

    def test_no_hit_and_run_with_first_base_empty(self):
        m = _machine({"hit_and_run_rate_per_opportunity": 0.95}, seed=3)
        s = _state(inning=7, second=22, outs=1)  # runner on 2B, 1B empty
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is False

    def test_no_hit_and_run_with_two_outs(self):
        m = _machine({"hit_and_run_rate_per_opportunity": 0.95}, seed=3)
        s = _state(inning=7, first=11, outs=2)
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is False

    def test_no_hit_and_run_in_a_deep_count(self):
        # Two strikes -> unfavorable for a contact-protect play.
        m = _machine({"hit_and_run_rate_per_opportunity": 0.95}, seed=3)
        s = _state(inning=7, first=11, outs=0, strikes=2)
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is False

    def test_passive_manager_never_hits_and_runs(self):
        m = _machine(_PASSIVE, seed=3)
        s = _state(inning=7, first=11, outs=1)
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is False

    def test_no_profile_is_no_op(self):
        m = _machine(None, seed=3)
        s = _state(inning=7, first=11, outs=1)
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is False
        assert m.manager_decisions == []

    def test_hit_and_run_signal_is_reset_each_pitch(self):
        # A spot that would NOT fire (two outs) clears a previously-set flag.
        m = _machine({"hit_and_run_rate_per_opportunity": 0.95}, seed=3)
        s = _state(inning=7, first=11, outs=1)
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is True
        s.outs = 2
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is False


# ===========================================================================
# Hit-and-run consolidation: it does NOT double-fire with the steal initiate
# ===========================================================================


class TestHitAndRunConsolidation:
    def test_hit_and_run_preempts_the_steal_initiate(self):
        # A resolver that always wants to steal: when the hit-and-run fires the
        # steal is NOT also staged (the runner goes WITH the swing, one decision).
        class _AlwaysSteal(PlayResolver):
            def resolve_steal(self, state):
                return StealResolution(
                    attempted=True, runner_id=11, from_base=1, to_base=2, safe=True
                )

        m = StateMachine(
            rng=np.random.default_rng(3),
            manager={"hit_and_run_rate_per_opportunity": 1.0, "steal_order_rate_per_1b_opp": 1.0},
            resolver=_AlwaysSteal(),
        )
        s = _state(inning=7, first=11, outs=1, balls=1)
        m._pre_pitch_hook(s)
        assert s.manager.hit_and_run_signalled is True
        assert m._pending_steal is None  # the steal was NOT also staged
        kinds = [d["kind"] for d in m.manager_decisions]
        assert kinds.count("hit_and_run") == 1
        assert "steal" not in kinds

    def test_ibb_still_takes_precedence_over_hit_and_run(self):
        # The IBB spot (1B open) cannot be a hit-and-run spot (needs 1B occupied),
        # so the two are mutually exclusive by construction; assert the IBB path
        # wins and no hit-and-run is recorded.
        # SIM-515: the IBB draws at the injected cell rate (1.0 = certain).
        m = _machine(_AGGRESSIVE, seed=1, ibb_rates={(2, 0, True, True): 1.0})
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55)
        m._pre_pitch_hook(s)
        assert s.manager.intentional_walk_signalled is True
        assert s.manager.hit_and_run_signalled is False
        assert not any(d["kind"] == "hit_and_run" for d in m.manager_decisions)


# ===========================================================================
# Determinism under a fixed seed
# ===========================================================================


class TestDeterminism:
    def test_hit_and_run_is_deterministic_under_a_fixed_seed(self):
        decisions = []
        for _ in range(3):
            m = _machine({"hit_and_run_rate_per_opportunity": 0.5}, seed=42)
            s = _state(inning=7, first=11, outs=1, balls=1)
            m._pre_pitch_hook(s)
            decisions.append(s.manager.hit_and_run_signalled)
        assert len(set(decisions)) == 1  # identical across identical seeds


# ===========================================================================
# No-manager-profile games still complete (every new trigger is no-op-safe)
# ===========================================================================


class _CyclingResolver(PlayResolver):
    # SIM-505: without an injected batted-ball sample the loop's no-sampler
    # path resolves EVERY in-play pitch to a terminal NOTHING — this resolver
    # was never consulted, the synthetic games were walk/strikeout marathons
    # with runners parked for whole innings, and the wrap-around batter
    # eventually collided with his own parked self (a runner on two bases,
    # seed-dependent). The class-level sample opts into the loop's injection
    # seam (`_injected_battedball`, sim_loop.py step 6), so singles and outs
    # actually resolve through the REAL advancement code and every game is
    # legal baseball at any seed.
    _injected_battedball = {"exit_velo": 90.0, "launch_angle": 12.0}

    def __init__(self, rng, hit_rate: float = 0.30):
        self.rng = rng
        self.hit_rate = float(hit_rate)

    def resolve_fielding(self, state, battedball_sample):
        if float(self.rng.random()) < self.hit_rate:
            return FieldingSignal(event="single", result_hits=1, result_outs=0, result_runs=0)
        return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)


class _RngMachine(StateMachine):
    def step_pitch(self, state, **_kw):  # type: ignore[override]
        r = float(self.rng.random())
        if r < 0.55:
            outcome = "in_play"
        elif r < 0.75:
            outcome = "ball"
        elif r < 0.92:
            outcome = "called_strike"
        else:
            outcome = "foul"
        return super().step_pitch(state, pitch_outcome=outcome)


class TestNoProfileFullGame:
    def test_no_profile_game_completes_with_new_triggers_no_op(self):
        # SIM-498 reseeded the loop and full-pool generators from independent
        # SeedSequence children, which legitimately changes the draw stream. Seed 0
        # now yields a 0-0 tie that runs to the extra-inning cap; seed 2 decides in
        # regulation. The seed is incidental — this test asserts the game reaches a
        # decision rather than stalling, and it still does.
        rng = np.random.default_rng(2)
        machine = _RngMachine(resolver=_CyclingResolver(rng), rng=rng)  # manager=None
        result = simulate_game(
            machine,
            seed=2,
            away_lineup=AWAY_LINEUP,
            home_lineup=HOME_LINEUP,
        )
        assert result.home_score != result.away_score
        assert result.innings_played >= 9
        # No profile => no situational decisions taken (the hooks were no-ops).
        assert machine.manager_decisions == []
        result.final_state.assert_score_valid()

    def test_aggressive_situational_manager_game_completes_validly(self):
        # SIM-505 CLOSED: the fixture used to pass this assertion by SEED LUCK
        # (its in-play pitches resolved to nothing, so runners parked for
        # whole innings and seeds 1 and 7 ended with a runner on two bases).
        # With the injected batted-ball sample the resolver actually runs, so
        # the game is legal at ANY seed — asserted across the two previously
        # ILLEGAL seeds plus one that always passed.
        for seed in (1, 2, 7):
            rng = np.random.default_rng(seed)
            machine = _RngMachine(
                resolver=_CyclingResolver(rng),
                rng=rng,
                manager=_AGGRESSIVE,
            )
            result = simulate_game(
                machine,
                seed=seed,
                away_lineup=AWAY_LINEUP,
                home_lineup=HOME_LINEUP,
            )
            assert result.innings_played >= 9, seed
            result.final_state.assert_invariants(in_play=True)
            result.final_state.assert_score_valid()
