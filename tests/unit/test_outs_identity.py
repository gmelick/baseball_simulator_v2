"""SIM-557 — every out on a pitcher's line: the identity over whole games.

The box used to lose about 0.3 outs a game. A caught stealing or a pickoff
on a pitch that did not end the plate appearance recorded an out on the
state, but no pitcher's line received it, because the credit sat at the end
of the plate appearance. The fix moves the one credit to the out recorder
(``StateMachine._record_outs``), so every out the defense records lands on
the line of the pitcher on the mound
(``docs/audit/2026-10-04-sim557-pitcher-outs-credit-plan.md``, §5 and §7).

This file holds the plan's three identity tests (§7, tests 19 to 21). Each
runs 25 seeded synthetic games on a bundle with busy runners, pickoff rows at
every count and the pitching-change draw on, in both steal orders:

* the running game on the pitch (the pickoff before the pitch, the steal
  after its result), and
* the single pre-pitch draw, on a pool of the shape exported before the
  pitch class existed (its pickoff rows read as pitch rows).

19. In every game, the outs on the pitchers' box lines equal the outs the
    play stream recorded (``GameSimResult.outs_played``).
20. The home pitchers record three outs an inning. The away pitchers record
    three outs an inning, or one half-inning fewer when the home side does
    not bat last, or that plus the outs of the walk-off half.
21. ``outs_played`` equals the sum of the recorded plays' ``outs_recorded``.

A guard test checks that the games hold caught-stealing and pickoff outs and
pitching changes. Without them the identity proves nothing about the defect.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import StealPool
from simulation.game_state import PlayResult, Team
from simulation.play_recorder import RecordingMachine
from simulation.sim_loop import GameSimResult, StateMachine, simulate_game
from simulation.synthetic_bundle import (
    LEAGUE_INPLAY_MODEL,
    LEAGUE_PITCH_MODEL,
    synthetic_artifacts,
    synthetic_sampler,
)

SEASON = 2024
GAMES = 25

#: Each side's starter first, then its pen. The ids do not overlap the
#: lineups, so a box line with outs belongs to exactly one side.
HOME_PITCHERS = (100, 101, 102, 103, 104, 105)
AWAY_PITCHERS = (200, 201, 202, 203, 204, 205)
AWAY_LINEUP = list(range(1, 10))
HOME_LINEUP = list(range(11, 20))

#: A steal pool with busy runners (a quarter of the stealable pitches carry
#: an attempt, 78% safe) and one pickoff row in every count group.
STEAL = (0.25, 0.78)
STEAL_KW = {"pickoff_rows": 1, "pickoff_weight": 0.03}


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


@dataclass
class _Game:
    """One recorded game: the result and its ordered play stream."""

    result: GameSimResult
    plays: list[PlayResult]
    changes: int = 0


def _side_of(pid: int) -> Team:
    """The side a pitcher belongs to, from the game's construction."""
    if pid in HOME_PITCHERS:
        return Team.HOME
    if pid in AWAY_PITCHERS:
        return Team.AWAY
    raise AssertionError(f"player {pid} is not one of the game's pitchers")


def _without_classes(pool: StealPool) -> StealPool:
    """The pool with no pitch class and no pickoff mark.

    The single pre-pitch draw leaves a marked pickoff row out of its count
    group. Without the mark, the pickoff rows read as pitch rows whose
    pickoff label is set: the shape of a pool exported before the pitch
    class existed. The single draw then retires a runner on a pickoff row.
    """
    return dataclasses.replace(pool, pitch_class=None, is_pickoff_row=None)


def _play_game(seed: int, *, on_the_pitch: bool) -> _Game:
    """Play one synthetic game through a RecordingMachine.

    ``on_the_pitch`` picks the steal order. True runs the running game on
    the pitch. False runs the single pre-pitch draw on a pool of the shape
    exported before the pitch class existed. :func:`_without_classes` builds
    that pool from the same rows.
    """
    art = synthetic_artifacts(
        pitch_model=LEAGUE_PITCH_MODEL,
        inplay_model=LEAGUE_INPLAY_MODEL,
        advancement=True,
        steal=STEAL,
        steal_kw=dict(STEAL_KW),
        season=SEASON,
        manager=True,
    )
    if not on_the_pitch:
        for target, pool in list(art.steal_pools.items()):
            art.steal_pools[target] = _without_classes(pool)
    fp = synthetic_sampler(art, seed)
    sm = StateMachine(fp, rng=np.random.default_rng(seed), manager={"any": 0.0})
    # The pitching change as a draw from the synthetic change pool, so the
    # credit has to follow a reliever as well as a starter.
    sm.manager_draw = True
    assert sm._steal_order_active() is on_the_pitch
    recorder = RecordingMachine(sm)
    result = simulate_game(
        recorder,
        seed=seed,
        season=SEASON,
        away_lineup=list(AWAY_LINEUP),
        home_lineup=list(HOME_LINEUP),
        # The home starter works the top of the first (with no pitcher_id the
        # opening half would go to a pitcher with id 0).
        pitcher_id=HOME_PITCHERS[0],
        home_pitcher_id=HOME_PITCHERS[0],
        away_pitcher_id=AWAY_PITCHERS[0],
        bullpen={Team.HOME: list(HOME_PITCHERS[1:]), Team.AWAY: list(AWAY_PITCHERS[1:])},
    )
    changes = sum(1 for d in sm.manager_decisions if d.get("kind") == "pitching_change")
    return _Game(result=result, plays=list(recorder.recorded_plays), changes=changes)


@pytest.fixture(
    scope="module",
    params=[True, False],
    ids=["on_the_pitch", "single_pre_pitch_draw"],
)
def games(request: Any) -> list[_Game]:
    return [_play_game(seed, on_the_pitch=request.param) for seed in range(GAMES)]


def _box_outs(game: _Game) -> dict[int, int]:
    """Each box line's outs, for the lines that hold any."""
    box = game.result.boxscore
    assert box is not None, "the game produced no box score"
    return {pid: ln.outs_recorded for pid, ln in box.lines.items() if ln.outs_recorded}


def _caught_stealing_outs(plays: list[PlayResult]) -> int:
    return sum(1 for r in plays if r.steal_attempted and r.steal_outcome == "caught")


def _pickoff_outs(plays: list[PlayResult]) -> int:
    return sum(1 for r in plays if r.pickoff_out)


# ---------------------------------------------------------------------------
# The guard: the games hold the outs the defect lost
# ---------------------------------------------------------------------------


class TestTheGamesHoldTheRunningGame:
    def test_the_games_hold_caught_stealing_outs_pickoff_outs_and_pitching_changes(self, games):
        cs = sum(_caught_stealing_outs(g.plays) for g in games)
        po = sum(_pickoff_outs(g.plays) for g in games)
        changes = sum(g.changes for g in games)
        assert cs > 0, "no caught stealing in 25 games: the identity proves nothing"
        assert po > 0, "no pickoff out in 25 games: the identity proves nothing"
        assert changes > 0, "no pitching change in 25 games: no reliever's line is tested"

    def test_some_caught_stealing_or_pickoff_outs_land_on_a_pitch_that_does_not_end_the_plate_appearance(
        self, games
    ):
        # These are the outs the old credit at the end of the plate
        # appearance missed. A play that ends no plate appearance has no
        # event; a caught stealing or a pickoff out on it must still reach a
        # pitcher's line.
        mid_pa = sum(
            1
            for g in games
            for r in g.plays
            if (r.pickoff_out or (r.steal_attempted and r.steal_outcome == "caught"))
            and r.event is None
        )
        assert mid_pa > 0


# ---------------------------------------------------------------------------
# The identity (plan §7, tests 19 to 21)
# ---------------------------------------------------------------------------


class TestTheIdentity:
    def test_the_outs_on_the_pitchers_lines_equal_the_outs_played_in_every_game(self, games):
        mismatches = []
        for seed, g in enumerate(games):
            credited = sum(_box_outs(g).values())
            if credited != g.result.outs_played:
                mismatches.append((seed, credited, g.result.outs_played))
        assert not mismatches, f"(seed, credited, played): {mismatches}"

    def test_only_the_games_pitchers_hold_outs_on_the_box(self, games):
        # No batter's line and no stray pitcher (such as an id of 0) holds an out.
        for g in games:
            for pid in _box_outs(g):
                _side_of(pid)

    def test_the_home_pitchers_record_three_outs_an_inning_and_the_away_pitchers_fit_how_the_game_ended(
        self, games
    ):
        checked = 0
        for seed, g in enumerate(games):
            res = g.result
            if res.home_score == res.away_score:
                continue  # a tie stops at the inning cap; no shape applies.
            checked += 1
            n = res.innings_played
            outs = _box_outs(g)
            home = sum(o for pid, o in outs.items() if _side_of(pid) == Team.HOME)
            away = sum(o for pid, o in outs.items() if _side_of(pid) == Team.AWAY)
            # The home pitchers work every top half, and every top half ends.
            assert home == 3 * n, f"seed {seed}: home pitchers {home} outs in {n} innings"
            if res.walk_off:
                # The winning run ends the last bottom half mid-inning.
                expected = {3 * (n - 1) + int(res.final_state.outs)}
            elif res.home_score > res.away_score:
                # The home side led after the top of the last inning and did
                # not bat (3(n-1)); or a run scored on the play that made the
                # last bottom half's third out (3n).
                expected = {3 * (n - 1), 3 * n}
            else:
                # The away side won: the home side batted a full last half.
                expected = {3 * n}
            assert away in expected, (
                f"seed {seed}: away pitchers {away} outs, expected one of {sorted(expected)} "
                f"({n} innings, walk-off {res.walk_off}, final outs {res.final_state.outs})"
            )
        assert checked > 0

    def test_each_sides_box_outs_equal_the_outs_of_the_plays_it_fielded(self, games):
        # The play names its own side (SIM-557, decision 2). Read the sides
        # from the stream: the box and the stream agree side by side.
        for seed, g in enumerate(games):
            assert all(r.fielding_team is not None for r in g.plays), f"seed {seed}"
            by_stream = {Team.HOME: 0, Team.AWAY: 0}
            for r in g.plays:
                by_stream[r.fielding_team] += int(r.outs_recorded)
            by_box = {Team.HOME: 0, Team.AWAY: 0}
            for pid, o in _box_outs(g).items():
                by_box[_side_of(pid)] += o
            assert by_box == by_stream, f"seed {seed}: box {by_box} vs stream {by_stream}"

    def test_each_pitchers_box_outs_equal_the_outs_of_the_plays_he_was_on_the_mound_for(
        self, games
    ):
        for seed, g in enumerate(games):
            by_stream: dict[int, int] = {}
            for r in g.plays:
                if r.outs_recorded:
                    assert r.pitcher_id is not None, f"seed {seed}: an out with no pitcher"
                    pid = int(r.pitcher_id)
                    by_stream[pid] = by_stream.get(pid, 0) + int(r.outs_recorded)
            assert _box_outs(g) == by_stream, f"seed {seed}"

    def test_the_outs_played_equal_the_sum_of_the_recorded_plays_outs(self, games):
        for seed, g in enumerate(games):
            stream = sum(int(r.outs_recorded) for r in g.plays)
            assert g.result.outs_played == stream, f"seed {seed}"
            # A finished game plays at least every half but the last bottom.
            assert stream >= 3 * (2 * g.result.innings_played - 1), f"seed {seed}"


class TestTheBattersFacedFollowTheLoopsCount:
    """The box line's batters faced (``bf``) follows the loop's own count of
    completed plate appearances (``GameState.pitcher_bf``), in both steal
    orders.

    A known limit, accepted and documented at the credit in ``_accumulate_pa``:
    in the single pre-pitch draw, a pickoff or a caught stealing can make the
    third out before the pitch's result. The loop still ends that plate
    appearance, so the pitcher gets a batter faced (and the batter an at-bat).
    The batter's line and the loop's count carried that flaw before the batters
    faced existed. Its fix changes the batter's line too, so it belongs to its
    own ticket. This test pins that the two counts agree, so the batters faced
    never drift from the count the manager reads."""

    def test_each_pitchers_box_batters_faced_equal_the_loops_count(self, games):
        for seed, g in enumerate(games):
            box = g.result.boxscore
            assert box is not None
            loop_bf = {int(pid): int(n) for pid, n in g.result.final_state.pitcher_bf.items() if n}
            box_bf = {int(pid): ln.bf for pid, ln in box.lines.items() if ln.bf}
            assert box_bf == loop_bf, f"seed {seed}"
            assert sum(box_bf.values()) > 0, f"seed {seed}"
