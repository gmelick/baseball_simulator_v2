"""SIM-509 — hit-by-pitch is its own pitch outcome, never a walk.

The pool builder's outcome classification collapsed an HBP pitch (a ball-class
Gameday type code) into ``ball``, so every simulated HBP became ball four and a
WALK. Measured 2025: 1,928 HBP = 0.397/team-game — ~82% of the BB band's
+0.481 surplus. The fix labels the pool row ``hit_by_pitch``, the count
machine terminates on it at any count, and the walk resolver applies the same
force mechanics under its own canonical event, which the BB probes (canonical
``walk``/``intentional_walk``) never count.
"""

from __future__ import annotations

import inspect

import numpy as np

from pipeline.batch import player_profile_computor as ppc
from simulation.game_state import PITCH_OUTCOMES, Bases, GameState, PlayResult
from simulation.sim_loop import (
    EVENT_HIT_BY_PITCH,
    StateMachine,
    advance_count,
)


class TestTheCountMachine:
    def test_hbp_is_terminal_at_any_count(self):
        for balls, strikes in ((0, 0), (3, 0), (0, 2), (3, 2)):
            adv = advance_count(balls, strikes, "hit_by_pitch")
            assert adv.terminal and not adv.is_contact
            assert adv.event == EVENT_HIT_BY_PITCH

    def test_hbp_is_in_the_closed_vocabulary(self):
        assert "hit_by_pitch" in PITCH_OUTCOMES


class TestTheResolution:
    def _machine_and_state(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = GameState(
            pitcher_id=901,
            bat_hand="R",
            season=2025,
            batter_id=555,
            away_lineup=[550 + i for i in range(9)],
            home_lineup=[650 + i for i in range(9)],
        )
        return sm, state

    def test_hbp_awards_first_with_walk_forces(self):
        sm, state = self._machine_and_state()
        state.bases = Bases(first=11, second=22)
        result = PlayResult(pitch_outcome="hit_by_pitch")
        sm._resolve_walk(state, result, event=EVENT_HIT_BY_PITCH)
        assert result.event == "hit_by_pitch" and result.pa_terminal
        assert state.bases.first == 555  # the batter
        assert state.bases.second == 11 and state.bases.third == 22  # forced
        assert result.baserunner_advances[555] == 1

    def test_hbp_is_not_an_ab_and_never_a_bb(self):
        """The accumulator books the PA: no AB, no batter hit, and the
        pitcher's walk column stays untouched (HBP is excluded from BB
        everywhere, including WHIP's numerator)."""
        sm, state = self._machine_and_state()
        result = PlayResult(pitch_outcome="hit_by_pitch", pa_terminal=True)
        result.event = "hit_by_pitch"
        result.canonical_event = "hit_by_pitch"
        sm._accumulate_pa(state, result)
        batter = sm._current_batter_id(state)
        assert sm.boxscore.line(int(batter)).ab == 0
        assert sm.boxscore.line(int(batter)).h == 0
        assert sm.boxscore.line(901).bb == 0

    def test_a_walk_still_credits_the_pitcher_bb(self):
        sm, state = self._machine_and_state()
        result = PlayResult(pitch_outcome="ball", pa_terminal=True)
        result.event = "walk"
        result.canonical_event = "walk"
        sm._accumulate_pa(state, result)
        assert sm.boxscore.line(901).bb == 1


class TestTheBuilderMapping:
    def test_the_events_branch_precedes_the_type_codes(self):
        """The pool builder must classify events='hit_by_pitch' BEFORE the
        Gameday type-code branches — an HBP pitch carries a ball-class code,
        so a later branch would swallow it back into 'ball'."""
        src = inspect.getsource(ppc.PlayerProfileComputor._build_pitch_pool)
        hbp = src.index("WHEN events = 'hit_by_pitch' THEN 'hit_by_pitch'")
        ball = src.index("WHEN TRIM(type) IN ('B', '*B') THEN 'ball'")
        assert hbp < ball

    def test_the_builder_version_is_current(self):
        """The watermark guard skips unchanged builders — a formula change
        that forgets the version bump never lands (the SIM-501 lesson).
        SIM-510 superseded the sim509 stamp when the transition columns
        landed; SIM-491 superseded sim510 when bat_home landed; bump this
        assertion with every pool-formula change."""
        assert "sim491" in ppc.POOL_BUILDER_VERSION
