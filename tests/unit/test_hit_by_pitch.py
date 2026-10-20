"""Tests that a hit by pitch (HBP) is its own pitch outcome, never a walk.

An HBP pitch carries a ball-class Gameday type code. A builder that read the
code alone would label the row ``ball``, and every simulated HBP would become
ball four and a WALK: in 2025, 1,928 HBP, 0.397 a team-game, about 82% of the
walk band's +0.481 surplus (the hit-by-pitch fix, SIM-509). So the pool
builder labels the row ``hit_by_pitch``, the count machine ends the plate
appearance on it at any count, and the walk resolver applies the same force
rules under its own canonical event, which the walk probes (canonical
``walk`` / ``intentional_walk``) never count. The tests run the count machine
and the state machine in memory, and the builder's SQL expression on an
in-memory DuckDB.
"""

from __future__ import annotations

import inspect

import duckdb
import numpy as np

from pipeline.batch import player_profile_computor as ppc
from simulation.game_state import PITCH_OUTCOMES, Bases, GameState, PlayResult
from simulation.loop.values import EVENT_HIT_BY_PITCH, advance_count
from simulation.sim_loop import StateMachine


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
    @staticmethod
    def _classify(rows):
        """Run the pool build's class expression over ``(type, events)`` rows."""
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                "CREATE TABLE p (i INTEGER, type VARCHAR, strikes SMALLINT, events VARCHAR)"
            )
            con.executemany(
                "INSERT INTO p VALUES (?, ?, 0, ?)",
                [(i, code, events) for i, (code, events) in enumerate(rows)],
            )
            got = con.execute(f"SELECT {ppc.SQL_OUTCOME_TYPE} FROM p ORDER BY i").fetchall()
        finally:
            con.close()
        return [r[0] for r in got]

    def test_the_events_branch_wins_over_the_ball_branch(self):
        """The pool builder must classify events='hit_by_pitch' BEFORE the
        Gameday type-code branches — an HBP pitch carries a ball-class code,
        so a later branch would swallow it back into 'ball'.

        The expression is the module constant ``SQL_OUTCOME_TYPE``, so this
        test runs it: the same ball code reads ``hit_by_pitch`` with the HBP
        event and ``ball`` without it."""
        got = self._classify(
            [
                ("B ", "hit_by_pitch"),
                ("*B", "hit_by_pitch"),
                ("B ", None),
                ("*B", None),
            ]
        )
        assert got == ["hit_by_pitch", "hit_by_pitch", "ball", "ball"]

    def test_the_pool_build_renders_the_constant(self):
        src = inspect.getsource(ppc.PlayerProfileComputor._build_pitch_pool)
        assert "{SQL_OUTCOME_TYPE}" in src

    def test_the_builder_version_is_current(self):
        """The watermark guard skips unchanged builders: a formula change
        that forgets the version bump never lands. Bump this assertion with
        every pool-formula change. The current stamp, sim554, marks the steal
        opportunity pool's pitch class and pickoff rows (migration 0031)."""
        assert "sim554" in ppc.POOL_BUILDER_VERSION
