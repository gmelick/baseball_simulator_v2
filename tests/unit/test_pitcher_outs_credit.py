"""SIM-557 — every out on a pitcher's line (the plan's tests 1 to 18).

The plan is ``docs/audit/2026-10-04-sim557-pitcher-outs-credit-plan.md``.

Every out the defense records passes once through the run ledger's recorder,
``StateMachine._record_outs``. Since SIM-557 that recorder is the one writer of
a pitcher's outs on his box line. Before the change, two other writers credited
the outs: the end of a plate appearance (``_accumulate_pa``) and the no-pitch
pickoff return. A caught stealing or a pickoff out on a pitch that did not end
the plate appearance reached neither, so the box lost about 0.32 outs a game.

The tests here cover two groups:

* the paths (tests 1 to 14): each way a step of the loop records an out puts
  that out on the line of the pitcher on the mound, exactly once;
* the list (tests 15 to 18): a pitcher who faced a batter or recorded an out is
  in ``BoxScore.pitchers`` and owns the pitcher props. The batters-faced count
  (``PlayerStatLine.bf``) is new.

The harness is the running-game suite's: a synthetic bundle whose pitch model
is one class, an injected pitch, and a steal or a pickoff staged on the
machine or drawn from a synthetic steal pool.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts, StealPool
from simulation import sim_loop
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import NO_PITCH, Bases, GameState, Half, PlayResult, Team
from simulation.prop_distributions import PITCHER_PROPS, PropDistributionSet
from simulation.sim_loop import STEAL_CAUGHT, StateMachine, StealResolution
from simulation.synthetic_bundle import fixed_play_artifacts, synthetic_sampler

SEASON = 2024
PITCHER = 477132  # the home side's pitcher: he works the top half
AWAY_PITCHER = 555001  # the away side's pitcher: he works the bottom half
RELIEVER = 600123
BATTER = 900
RUNNER = 101

#: A steal pool whose draw never stages a steal: the loop runs the running
#: game on the pitch, and only a staged steal or a pickoff row moves a runner.
NO_ATTEMPTS = (0.0, 0.78)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


def _state(**kw: Any) -> GameState:
    defaults: dict[str, Any] = {
        "pitcher_id": PITCHER,
        "bat_hand": "R",
        "season": SEASON,
        "batter_id": BATTER,
        "away_lineup": [900 + i for i in range(9)],
        "home_lineup": [800 + i for i in range(9)],
    }
    defaults.update(kw)
    return GameState(**defaults)


def _machine(
    pitch_outcome: str = "ball",
    *,
    event: str = "field_out",
    got_away: bool = False,
    steal: tuple[float, float] | None = NO_ATTEMPTS,
    **steal_kw: Any,
) -> StateMachine:
    """A machine whose every drawn pitch is ``pitch_outcome`` and whose every
    ball in play is ``event``. ``steal`` and ``steal_kw`` build the synthetic
    steal pool, by pitch class."""
    art = fixed_play_artifacts(
        event,
        pitch_model={pitch_outcome: 1.0},
        got_away=got_away,
        steal=steal,
        steal_kw=steal_kw or None,
    )
    sm = StateMachine(synthetic_sampler(art, 0), rng=np.random.default_rng(0))
    sm._got_away = True
    return sm


def _certain_pickoff(kind: str = "out", counts=((0, 0),)) -> dict[str, Any]:
    """Steal-pool keywords for a pickoff on every draw in the named counts:
    one pickoff row a billion times heavier than the count group's pitch rows."""
    return {
        "pickoff_rows": 1,
        "pickoff_weight": 1e9,
        "pickoff_kind": kind,
        "pickoff_counts": counts,
    }


def _steal(runner: int, from_base: int, to_base: int, *, safe: bool) -> StealResolution:
    return StealResolution(
        attempted=True, runner_id=runner, from_base=from_base, to_base=to_base, safe=safe
    )


def _outs(sm: StateMachine, pid: int) -> int:
    """The outs on ``pid``'s box line; 0 when he has no line."""
    if sm.boxscore is None or pid not in sm.boxscore.lines:
        return 0
    return int(sm.boxscore.lines[pid].outs_recorded)


def _drive_strikeout(sm: StateMachine, state: GameState) -> None:
    """Three called strikes, injected."""
    for _ in range(3):
        sm.step_pitch(state, pitch_outcome="called_strike")


def _unclassed_pickoff_pool() -> StealPool:
    """A steal pool with no pitch class (the single pre-pitch draw of the old
    order): two rows in the (0 outs, 0-0) group, both a pickoff out."""
    n = 2
    return StealPool(
        sit=np.zeros((n, 4), dtype=np.float32),
        runner_id=np.full(n, 11, dtype=np.int64),
        pitcher_id=np.full(n, 901, dtype=np.int64),
        catcher_id=np.full(n, 902, dtype=np.int64),
        season=np.full(n, SEASON, dtype=np.int64),
        attempted=np.zeros(n, dtype=np.int8),
        success=np.zeros(n, dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
        pickoff_out=np.ones(n, dtype=np.int8),
        pitch_class=None,
        is_pickoff_row=None,
    )


# ---------------------------------------------------------------------------
# The paths (plan tests 1 to 14)
# ---------------------------------------------------------------------------


class TestEveryOutLandsOnThePitchersLine:
    def test_a_strikeout_credits_one_out(self):
        sm = _machine("ball")
        state = _state()
        _drive_strikeout(sm, state)
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 1
        assert pit.k == 1 and pit.bf == 1
        assert state.outs == 1

    def test_a_double_play_on_a_ball_in_play_credits_two_outs(self):
        sm = _machine("in_play", event="grounded_into_double_play")
        state = _state()
        state.bases = Bases(first=RUNNER)
        result = sm.step_pitch(state)
        assert result.outs_recorded == 2 and state.outs == 2
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 2
        assert pit.bf == 1

    def test_a_caught_stealing_on_a_ball_credits_one_out_and_the_count_goes_on(self):
        sm = _machine("ball")
        state = _state()
        state.bases = Bases(first=RUNNER)
        sm.stage_steal(runner_id=RUNNER, from_base=1, safe=False)
        result = sm.step_pitch(state, pitch_outcome="ball")
        assert result.steal_outcome == STEAL_CAUGHT and not result.pa_terminal
        assert result.outs_recorded == 1 and state.outs == 1
        assert (state.balls, state.strikes) == (1, 0)
        pit = sm.boxscore.line(PITCHER)
        # The out is his although no plate appearance ended (was 0 before).
        assert pit.outs_recorded == 1
        assert pit.bf == 0
        assert sm.boxscore.line(RUNNER).cs == 1
        # The same batter strikes out: two outs on the line, one batter faced.
        _drive_strikeout(sm, state)
        assert state.outs == 2
        assert pit.outs_recorded == 2
        assert pit.k == 1 and pit.bf == 1

    def test_a_caught_stealing_on_a_ball_for_the_third_out_credits_the_pitcher_on_the_mound(
        self,
    ):
        sm = _machine("ball")
        state = _state(home_pitcher_id=PITCHER, away_pitcher_id=AWAY_PITCHER)
        state.bases = Bases(first=RUNNER)
        state.outs = 2
        sm.stage_steal(runner_id=RUNNER, from_base=1, safe=False)
        result = sm.step_pitch(state, pitch_outcome="ball")
        assert result.steal_outcome == STEAL_CAUGHT and result.outs_recorded == 1
        # The half rolled and the away side's pitcher is now on the mound.
        assert state.half == Half.BOTTOM and state.outs == 0
        assert state.pitcher_id == AWAY_PITCHER
        assert _outs(sm, PITCHER) == 1
        assert _outs(sm, AWAY_PITCHER) == 0
        assert sm.boxscore.line(PITCHER).bf == 0

    def test_a_pickoff_out_before_a_pitch_that_does_not_end_the_plate_appearance_credits_one(
        self,
    ):
        sm = _machine("ball", **_certain_pickoff("out"))
        state = _state()
        state.bases = Bases(first=RUNNER)
        result = sm.step_pitch(state)
        assert result.pickoff_out and result.pitch_outcome == "ball"
        assert not result.pa_terminal
        assert result.outs_recorded == 1 and state.outs == 1
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 1
        assert pit.bf == 0

    def test_a_pickoff_out_before_strike_three_credits_two_outs_not_three(self):
        sm = _machine("called_strike", **_certain_pickoff("out", counts=((0, 2),)))
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.strikes = 2
        result = sm.step_pitch(state)
        assert result.pickoff_out and result.canonical_event == "strikeout"
        assert result.outs_recorded == 2 and state.outs == 2
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 2
        assert pit.k == 1 and pit.bf == 1

    def test_a_caught_stealing_on_strike_three_with_nobody_out_credits_each_out_once(self):
        sm = _machine("swinging_strike", got_away=False)
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.strikes = 2
        sm.stage_steal(runner_id=RUNNER, from_base=1, safe=False)
        result = sm.step_pitch(state)
        assert result.steal_outcome == STEAL_CAUGHT
        assert result.canonical_event == "strikeout"
        assert result.outs_recorded == 2 and state.outs == 2
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 2
        assert pit.k == 1 and pit.bf == 1
        assert sm.boxscore.line(RUNNER).cs == 1

    def test_the_pickoff_third_out_before_the_pitch_credits_one_out_through_the_recorder(
        self,
    ):
        sm = _machine("ball", **_certain_pickoff("out"))
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 2
        result = sm.step_pitch(state)
        assert result.no_pitch and result.pitch_outcome == NO_PITCH
        assert result.pa_voided == "pickoff_third_out"
        assert result.outs_recorded == 1
        pit = sm.boxscore.line(PITCHER)
        # Once: the recorder credits it, and the no-pitch return no longer does.
        assert pit.outs_recorded == 1
        assert pit.bf == 0

    def test_the_single_pre_pitch_draw_credits_a_pickoff_out_on_a_ball(self):
        fp = FullPoolSampler(
            EngineArtifacts({}, steal_pools={"2": _unclassed_pickoff_pool()}),
            np.random.default_rng(7),
        )
        sm = StateMachine(fp, rng=np.random.default_rng(1))
        assert not sm._steal_order_active()  # the old order: one pre-pitch draw
        state = _state()
        state.bases = Bases(first=RUNNER)
        result = sm.step_pitch(state, pitch_outcome="ball")
        assert result.pickoff_out and state.bases.first is None
        assert not result.pa_terminal
        assert result.outs_recorded == 1 and state.outs == 1
        assert (state.balls, state.strikes) == (1, 0)
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 1
        assert pit.bf == 0

    def test_a_caught_stealing_for_the_third_out_on_ball_four_credits_the_walk_and_one_out(
        self,
    ):
        sm = _machine("ball")
        state = _state()
        state.bases = Bases(second=202)
        state.outs = 2
        state.balls = 3
        sm._pending_steal = _steal(202, 2, 3, safe=False)
        result = sm.step_pitch(state)
        assert result.event == "walk" and result.steal_outcome == STEAL_CAUGHT
        assert result.outs_recorded == 1
        assert state.half == Half.BOTTOM
        pit = sm.boxscore.line(PITCHER)
        assert pit.bb == 1 and pit.bf == 1
        assert pit.outs_recorded == 1

    def test_after_a_pitching_change_the_out_goes_to_the_reliever(self):
        sm = _machine("called_strike", **_certain_pickoff("out"))
        state = _state(home_pitcher_id=PITCHER, away_pitcher_id=AWAY_PITCHER)
        # The starter strikes out the first batter with the bases empty.
        _drive_strikeout(sm, state)
        assert _outs(sm, PITCHER) == 1
        # The next plate appearance opens with a pitching change: the
        # machine's own seam brings in the reliever at the start of the plate
        # appearance, before the pickoff draw.
        sm._pick_reliever = lambda _state, _li, source="draw": RELIEVER
        real_hook = sm._start_of_pa_hook

        def hook(st):
            if st.pitcher_id == PITCHER:
                sm._change_pitcher(st, 0.0, forced=False, source="draw")
            return real_hook(st)

        sm._start_of_pa_hook = hook
        state.bases = Bases(first=RUNNER)
        result = sm.step_pitch(state)
        assert state.pitcher_id == RELIEVER and state.home_pitcher_id == RELIEVER
        assert result.pickoff_out and result.outs_recorded == 1
        assert state.outs == 2
        assert _outs(sm, RELIEVER) == 1
        # The starter keeps his one out and gains none.
        assert _outs(sm, PITCHER) == 1
        assert sm.boxscore.line(PITCHER).bf == 1
        assert sm.boxscore.line(RELIEVER).bf == 0

    def test_with_no_pitcher_on_the_state_the_recorder_makes_no_line(self):
        sm = _machine("ball")
        state = _state(pitcher_id=None)
        sm._record_outs(state, 1)
        assert state.outs == 1
        assert sm.boxscore is None

    def test_accumulate_pa_called_alone_adds_no_out_and_one_batter_faced(self):
        sm = _machine("ball")
        state = _state()
        state.outs = 1
        result = PlayResult(pitch_outcome="called_strike", pa_terminal=True, event="strikeout")
        result.outs_recorded = 1
        sm._accumulate_pa(state, result)
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 0
        assert pit.bf == 1
        assert pit.k == 1

    def test_the_recorder_is_the_one_writer_of_a_pitchers_outs(self):
        source = Path(inspect.getfile(sim_loop)).read_text(encoding="utf-8")
        # Text check: split at the recorder. Its body holds one box-line write.
        head, sep, tail = source.partition("def _record_outs")
        assert sep, "sim_loop.py has no _record_outs"
        body = tail.split("\n    def ", 1)[0]
        assert body.count(".outs_recorded +=") == 1
        assert "_box_line(" in body
        # Structure check: every write to an ``outs_recorded`` attribute in the
        # module. The play result's counter (``result.outs_recorded``) is the
        # play stream's, written beside the recorder's call in the run ledger.
        # Every other write is a box line's, and there must be one, in
        # ``_record_outs``.
        tree = ast.parse(source)
        box_writes: list[str] = []
        result_writes: list[str] = []
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.AugAssign):
                    targets = [node.target]
                elif isinstance(node, ast.Assign):
                    targets = list(node.targets)
                else:
                    continue
                for target in targets:
                    if not (isinstance(target, ast.Attribute) and target.attr == "outs_recorded"):
                        continue
                    if isinstance(target.value, ast.Name) and target.value.id == "result":
                        result_writes.append(func.name)
                    else:
                        box_writes.append(func.name)
        assert box_writes == ["_record_outs"]
        assert set(result_writes) <= {"_commit_run_delta"}
        # The no-pitch return of ``step_pitch`` no longer writes a box line.
        assert (
            ".outs_recorded +=" not in head.split("def step_pitch", 1)[1].split("\n    def ", 1)[0]
        )


# ---------------------------------------------------------------------------
# The list (plan tests 15 to 18)
# ---------------------------------------------------------------------------


class TestThePitcherList:
    def _reliever_with_a_pickoff_only(self) -> StateMachine:
        sm = _machine("ball", **_certain_pickoff("out"))
        state = _state(pitcher_id=RELIEVER)
        state.bases = Bases(first=RUNNER)
        result = sm.step_pitch(state)
        assert result.pickoff_out and not result.pa_terminal
        return sm

    def test_a_reliever_whose_only_out_is_a_pickoff_is_in_the_pitcher_list(self):
        sm = self._reliever_with_a_pickoff_only()
        assert set(sm.boxscore.lines) == {RELIEVER}
        line = sm.boxscore.line(RELIEVER)
        assert line.outs_recorded == 1
        assert (line.bf, line.k, line.bb, line.er) == (0, 0, 0, 0)
        assert set(sm.boxscore.pitchers) == {RELIEVER}

    def test_a_reliever_whose_only_out_is_a_pickoff_owns_the_pitcher_props(self):
        sm = self._reliever_with_a_pickoff_only()
        props = PropDistributionSet.from_boxscores([sm.boxscore])
        mine = props.get(RELIEVER)
        assert mine is not None
        assert set(PITCHER_PROPS) <= set(mine)
        outs = props.get(RELIEVER, "OUTS")
        assert outs is not None and outs.n == 1
        assert outs.prob(1) == 1.0

    def test_a_reliever_who_allows_a_single_to_his_only_batter_is_listed(self):
        sm = _machine("in_play", event="single")
        state = _state(pitcher_id=RELIEVER)
        result = sm.step_pitch(state)
        assert result.canonical_event == "single" and result.outs_recorded == 0
        line = sm.boxscore.line(RELIEVER)
        assert line.bf == 1 and line.h_allowed == 1
        assert line.outs_recorded == 0
        assert (line.k, line.bb, line.er) == (0, 0, 0)
        # Listed by the batter he faced alone.
        assert RELIEVER in sm.boxscore.pitchers
        props = PropDistributionSet.from_boxscores([sm.boxscore])
        assert props.get(RELIEVER, "OUTS") is not None
        assert props.get(RELIEVER, "OUTS").prob(0) == 1.0

    def test_a_line_with_only_a_batter_faced_owns_the_pitcher_props(self):
        # A reliever whose only batter is hit by a pitch (or reaches on an
        # error) has a batter faced and nothing else: no out, strikeout,
        # walk, earned run or hit. The batter faced alone must list him and
        # give him the pitcher props, or the box and the props disagree.
        box = sim_loop.BoxScore()
        line = box.line(RELIEVER)
        line.bf = 1
        assert (line.outs_recorded, line.k, line.bb, line.er, line.h_allowed) == (0, 0, 0, 0, 0)
        assert RELIEVER in box.pitchers
        props = PropDistributionSet.from_boxscores([box])
        mine = props.get(RELIEVER)
        assert mine is not None
        assert set(PITCHER_PROPS) <= set(mine)
        outs = props.get(RELIEVER, "OUTS")
        assert outs is not None and outs.prob(0) == 1.0

    def test_an_intentional_walk_is_a_batter_faced(self):
        sm = _machine("ball")
        state = _state()
        result = sm._issue_intentional_walk(state)
        assert result.event == "intentional_walk" and result.pa_terminal
        line = sm.boxscore.line(PITCHER)
        assert line.bf == 1 and line.bb == 1
        assert line.outs_recorded == 0
        assert state.pitcher_bf[PITCHER] == 1

    def test_a_no_pitch_result_is_not_a_batter_faced(self):
        sm = _machine("ball", **_certain_pickoff("out"))
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 2
        result = sm.step_pitch(state)
        assert result.no_pitch
        line = sm.boxscore.line(PITCHER)
        assert line.bf == 0
        assert line.outs_recorded == 1
        assert state.pitcher_bf.get(PITCHER, 0) == 0
        assert result.fielding_team == Team.HOME
