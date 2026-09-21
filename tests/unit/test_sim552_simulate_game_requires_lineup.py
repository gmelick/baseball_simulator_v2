"""
test_sim552_simulate_game_requires_lineup.py
============================================
``simulate_game`` refuses a game with no batting order BEFORE the first pitch
(SIM-552).

Why this guard exists.  The loop rotates the batter through each side's
lineup.  It seats a batter who reaches base by his id.  With no lineup the
batter id is None.  The in-play resolver then substitutes ``-1``, and the
first hit trips the SIM-500 base guard deep inside the resolver.  That
message does not name the cause ("Bases.1B has a negative runner id: -1").
The weekly performance lane failed on exactly that: its batch bench ran the
no-DB factory with no lineup.  Since the per-tile fallback was deleted
(SIM-486), that factory builds the production machine.  The driver now names
the requirement up front.

Coverage:
  * no lineup on either side -> ValueError that names the batting order;
  * one side only (away or home) -> the same ValueError;
  * an ``initial_state`` with no lineup -> the same rule on that path;
  * the no-DB batch factory with no lineup -> the clear error, never the
    SIM-500 negative-runner message;
  * both lineups -> the game plays (the control).
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.batch_runner import GameSpec, _run_one, derive_seed
from simulation.game_state import GameState
from simulation.sim_loop import GameSimResult, StateMachine, simulate_game
from simulation.synthetic_bundle import league_artifacts, synthetic_sampler

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))  # 9 batters
HOME_LINEUP = list(range(201, 210))  # 9 batters

_NO_DB_FACTORY = "simulation.batch_runner:rng_driven_machine_factory"


def _machine(seed: int = 0) -> StateMachine:
    """The production machine over the in-memory league bundle (SIM-486)."""
    return StateMachine(
        synthetic_sampler(league_artifacts(), seed), rng=np.random.default_rng(seed)
    )


class _CountingMachine(StateMachine):
    """Counts ``step_pitch`` calls so a test can prove no pitch was thrown."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pitches = 0

    def step_pitch(self, st, **kw):
        self.pitches += 1
        return super().step_pitch(st, **kw)


class TestRefusesAGameWithNoBattingOrder:
    def test_no_lineup_on_either_side_raises_before_the_first_pitch(self):
        sm = _CountingMachine(
            synthetic_sampler(league_artifacts(), 0), rng=np.random.default_rng(0)
        )
        with pytest.raises(ValueError, match="batting order on each side") as info:
            simulate_game(sm, seed=0, pitcher_id=PITCHER, season=SEASON)
        # The message names both sides' counts, so the caller sees what is missing.
        assert "0 away and 0 home" in str(info.value)
        # The refusal came before the loop threw a pitch.
        assert sm.pitches == 0

    def test_away_lineup_alone_raises(self):
        with pytest.raises(ValueError, match="batting order on each side") as info:
            simulate_game(_machine(1), seed=1, away_lineup=AWAY_LINEUP)
        assert "9 away and 0 home" in str(info.value)

    def test_home_lineup_alone_raises(self):
        with pytest.raises(ValueError, match="batting order on each side") as info:
            simulate_game(_machine(2), seed=2, home_lineup=HOME_LINEUP)
        assert "0 away and 9 home" in str(info.value)

    def test_an_initial_state_with_no_lineup_gets_the_same_rule(self):
        state = GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON)
        with pytest.raises(ValueError, match="batting order on each side"):
            simulate_game(_machine(3), initial_state=state, seed=3)

    def test_the_no_db_batch_factory_with_no_lineup_fails_loudly(self):
        # The shape the weekly perf bench used to run: the no-DB factory and a
        # spec with no lineup.  The error must name the batting order, never
        # the SIM-500 negative-runner message the lane used to fail on.
        spec = GameSpec(machine_factory=_NO_DB_FACTORY, sim_kwargs={"max_innings": 9})
        with pytest.raises(ValueError) as info:
            _run_one(spec, derive_seed(335, 0))
        msg = str(info.value)
        assert "batting order on each side" in msg
        assert "negative runner id" not in msg


class TestAGameWithBothLineupsPlays:
    def test_both_lineups_play_a_complete_game(self):
        r = simulate_game(
            _machine(4),
            seed=4,
            pitcher_id=PITCHER,
            season=SEASON,
            away_lineup=AWAY_LINEUP,
            home_lineup=HOME_LINEUP,
        )
        assert isinstance(r, GameSimResult)
        assert r.total_pitches > 0
        assert r.innings_played >= 9

    def test_the_no_db_batch_factory_with_both_lineups_plays(self):
        # The spec the perf bench and tests/unit/test_backend_sim332.py run.
        spec = GameSpec(
            machine_factory=_NO_DB_FACTORY,
            sim_kwargs={
                "away_lineup": AWAY_LINEUP,
                "home_lineup": HOME_LINEUP,
                "season": SEASON,
                "pitcher_id": PITCHER,
                "bat_hand": "R",
            },
        )
        r = _run_one(spec, derive_seed(335, 0))
        assert r.total_pitches > 0
        assert r.innings_played >= 9
