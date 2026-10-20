"""SIM-554 — the running game on the pitch: the loop's order and its rules.

The steal draw used to run before the pitch and read only the count. So a
third of its attempts landed on a foul, a ball in play or a hit by pitch,
where real runners never earn one. The design
(``docs/audit/2026-09-29-sim554-running-game-on-the-pitch-plan.md``, §5 and
§7) puts four draws on every pitch with a runner who could steal:

  1. the pickoff draw, BEFORE the pitch, from the count group of the steal
     opportunity pool (its pitch rows and its pickoff rows);
  2. the pitch, on the bases the pickoff left;
  3. its result;
  4. the steal draw, among the real pitches of the same count AND the same
     class as the pitch that came out. A class group answers from its own
     rows, however few. Nothing falls back to the count group.

A pickoff that makes the third out before the pitch throws no pitch: the
result reads ``NO_PITCH``, no pitch, event or plate appearance is credited
(the out counts for the pitcher on the mound), and the same batter leads off
his team's next inning. Two rules of baseball apply in both orders. With two outs, a
third strike the catcher holds is the third out before any throw, so a steal
staged on it is void (decision 3). On a dropped-third-strike reach every
runner moves up one base before the batter takes first (decision 4).

The flag (``steal_pitch_class``) off, a pool without classes, or a sampler
without the new methods keeps the single pre-pitch draw of SIM-474.

The harness is the dropped-third-strike suite's: a synthetic bundle whose
pitch model is one class, ``sm._got_away = True`` so the drawn row's got-away
fact counts, and a steal staged on ``_pending_steal``. A wrapper on a real
synthetic sampler's methods records the order of the draws.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    STEAL_PITCH_CLASS_CODE,
    STEAL_PITCH_CLASSES,
    EngineArtifacts,
    StealPool,
)
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import NO_PITCH, PITCH_OUTCOMES, Bases, GameState, Half
from simulation.sim_loop import (
    STEAL_CAUGHT,
    STEAL_SAFE,
    StateMachine,
    StealResolution,
    simulate_game,
)
from simulation.synthetic_bundle import (
    LEAGUE_INPLAY_MODEL,
    LEAGUE_PITCH_MODEL,
    STEALABLE_CLASSES,
    fixed_play_artifacts,
    steal_pools,
    synthetic_artifacts,
    synthetic_sampler,
)

SEASON = 2024
PITCHER = 477132
BATTER = 900
RUNNER = 101

#: A steal pool whose draw never stages a steal. The order tests need the
#: classes in the pool, not the attempts.
NO_ATTEMPTS = (0.0, 0.78)
#: Every steal on a stealable class goes, and every one is safe.
ALWAYS_SAFE = (1.0, 1.0)

#: The two orders. ``None`` = no steal pool, so the loop runs the single
#: pre-pitch draw; ``NO_ATTEMPTS`` = a pool with classes, so the loop runs the
#: running game on the pitch. The rules of decisions 3 and 4 hold in both.
BOTH_ORDERS = pytest.mark.parametrize(
    "steal", [None, NO_ATTEMPTS], ids=["single_pre_pitch_draw", "on_the_pitch"]
)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


def _state(**kw) -> GameState:
    defaults = {
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
    got_away: bool = False,
    steal: tuple[float, float] | None = NO_ATTEMPTS,
    art: EngineArtifacts | None = None,
    seed: int = 0,
    **steal_kw: Any,
) -> StateMachine:
    """A machine whose every drawn pitch is ``pitch_outcome``. ``got_away``
    marks every drawn row a real passed ball or wild pitch; ``steal`` and
    ``steal_kw`` build the synthetic steal pool (by class unless
    ``by_class=False``)."""
    if art is None:
        art = fixed_play_artifacts(
            "field_out",
            pitch_model={pitch_outcome: 1.0},
            got_away=got_away,
            steal=steal,
            steal_kw=steal_kw or None,
        )
    sm = StateMachine(synthetic_sampler(art, seed), rng=np.random.default_rng(seed))
    sm._got_away = True
    return sm


def _certain_pickoff(kind: str = "out", counts=((0, 0),)) -> dict[str, Any]:
    """Steal-pool keywords for a pickoff that happens on every draw in the
    named counts: one pickoff row a billion times heavier than the count
    group's pitch rows (which weigh 1 in all)."""
    return {
        "pickoff_rows": 1,
        "pickoff_weight": 1e9,
        "pickoff_kind": kind,
        "pickoff_counts": counts,
    }


def _record(
    sm: StateMachine,
    *names: str,
    note: Callable[[], Any] | None = None,
) -> list[tuple[str, tuple, dict, Any]]:
    """Wrap the sampler's ``names`` so each call is logged as (name, args,
    keywords, ``note()`` at the call) and then runs the real method."""
    fp = sm.full_pool_sampler
    calls: list[tuple[str, tuple, dict, Any]] = []
    for name in names:
        real = getattr(fp, name)

        def rec(*args, _name=name, _real=real, **kw):
            calls.append((_name, args, kw, note() if note is not None else None))
            return _real(*args, **kw)

        setattr(fp, name, rec)
    return calls


def _names(calls) -> list[str]:
    return [c[0] for c in calls]


def _steal(runner: int, from_base: int, to_base: int, *, safe: bool) -> StealResolution:
    return StealResolution(
        attempted=True, runner_id=runner, from_base=from_base, to_base=to_base, safe=safe
    )


def _pickoff(runner: int = RUNNER, *, error: bool = False) -> StealResolution:
    """A staged pickoff throw to first: an out, or (``error``) a throw that
    gets away and moves the runner to second."""
    return StealResolution(attempted=True, runner_id=runner, from_base=1, safe=error, pickoff=True)


def _cell_pool(
    classes: list[str | None],
    attempted: list[int],
    *,
    success: list[int] | None = None,
    pickoff_out: list[int] | None = None,
    is_pickoff_row: list[int] | None = None,
) -> StealPool:
    """A hand-built steal pool: every row in the (0 outs, 0-0) group. A
    class of None is code 0: a pickoff row, unless ``is_pickoff_row`` says
    the row is a pitch row with no class."""
    n = len(classes)
    att = np.asarray(attempted, dtype=np.int8)
    return StealPool(
        sit=np.zeros((n, 4), dtype=np.float32),
        runner_id=np.full(n, 11, dtype=np.int64),
        pitcher_id=np.full(n, 901, dtype=np.int64),
        catcher_id=np.full(n, 902, dtype=np.int64),
        season=np.full(n, SEASON, dtype=np.int64),
        attempted=att,
        success=np.asarray(success, dtype=np.int8) if success is not None else att.copy(),
        recency=np.ones(n, dtype=np.float32),
        pickoff_out=(np.asarray(pickoff_out, dtype=np.int8) if pickoff_out is not None else None),
        pitch_class=np.asarray(
            [0 if c is None else STEAL_PITCH_CLASS_CODE[c] for c in classes], dtype=np.int8
        ),
        is_pickoff_row=(
            np.asarray(is_pickoff_row, dtype=np.int8) if is_pickoff_row is not None else None
        ),
    )


def _cell_sampler(pool: StealPool, seed: int = 7) -> FullPoolSampler:
    return FullPoolSampler(
        EngineArtifacts({}, steal_pools={"2": pool}), np.random.default_rng(seed)
    )


def _steal_draw(fp: FullPoolSampler, **kw):
    return fp.steal_draw(
        2, "11:2024", "901:2024", "902:2024", outs=0, balls=0, strikes=0, score_diff=0, **kw
    )


def _pickoff_draw(fp: FullPoolSampler, **kw):
    kw.setdefault("outs", 0)
    kw.setdefault("balls", 0)
    kw.setdefault("strikes", 0)
    return fp.pickoff_draw(2, "11:2024", "901:2024", "902:2024", score_diff=0, **kw)


def _attempt_share(sm: StateMachine, n: int) -> float:
    """The share of ``n`` pitches on which a steal resolved. Each pitch is a
    fresh 0-0 count, nobody out, a runner on first."""
    went = 0
    for _ in range(n):
        state = _state()
        state.bases = Bases(first=RUNNER)
        went += int(sm.step_pitch(state).steal_attempted)
    return went / n


# ---------------------------------------------------------------------------
# The order and the two draws (design tests 1-8)
# ---------------------------------------------------------------------------


class TestTheOrderOfTheDraws:
    @pytest.mark.parametrize("pitch", ["ball", "called_strike", "foul", "in_play"])
    def test_the_pickoff_draw_comes_first_and_the_steal_draw_reads_the_class(self, pitch):
        sm = _machine(pitch)
        assert sm._steal_order_active()
        calls = _record(sm, "pickoff_draw", "draw", "steal_draw")
        state = _state()
        state.bases = Bases(first=RUNNER)
        result = sm.step_pitch(state)
        assert result.pitch_outcome == pitch
        assert _names(calls) == ["pickoff_draw", "draw", "steal_draw"]
        pickoff_kw, steal_kw = calls[0][2], calls[2][2]
        assert (pickoff_kw["outs"], pickoff_kw["balls"], pickoff_kw["strikes"]) == (0, 0, 0)
        assert calls[0][1][0] == 2  # target base: the runner on first goes for second
        # The class of the pitch that came out, at the count it was thrown in.
        assert steal_kw["pitch_class"] == pitch
        assert (steal_kw["balls"], steal_kw["strikes"]) == (0, 0)

    def test_no_stealable_runner_draws_only_the_pitch(self):
        sm = _machine("ball")
        calls = _record(sm, "pickoff_draw", "draw", "steal_draw")
        sm.step_pitch(_state())
        assert _names(calls) == ["draw"]

    def test_a_pickoff_out_comes_before_the_pitch_and_one_result_carries_both(self):
        sm = _machine("ball", **_certain_pickoff("out"))
        state = _state()
        state.bases = Bases(first=RUNNER)
        calls = _record(
            sm,
            "draw",
            "new_plate_appearance",
            note=lambda: (state.bases.first, int(state.outs)),
        )
        result = sm.step_pitch(state)
        # The pitch draw ran on the bases the pickoff left: nobody on, one out.
        draw = [c for c in calls if c[0] == "draw"]
        assert len(draw) == 1 and draw[0][3] == (None, 1)
        base_out = [c[1][1] for c in calls if c[0] == "new_plate_appearance"][-1]
        assert int(base_out[0]) == 1 and int(base_out[1]) == 0  # outs, runners
        # One result: the pickoff and the pitch.
        assert result.pickoff_out and not result.pickoff_error
        assert not result.steal_attempted and result.steal_outcome is None
        assert result.pitch_outcome == "ball" and not result.no_pitch
        assert result.pa_voided is None
        assert result.outs_recorded == 1 and state.outs == 1
        assert state.bases.first is None
        assert (state.balls, state.strikes) == (1, 0)
        assert state.pitcher_pitch_count == 1
        assert RUNNER not in result.baserunner_advances
        tally = sm.running_game_tally
        assert tally.pickoff_draws == 1 and tally.pickoff_outs == 1
        assert tally.pickoffs_by_count == {(0, 0): 1}
        assert tally.no_pitch_third_outs == 0

    def test_a_pickoff_outcome_means_no_steal_draw_on_that_pitch(self):
        # Runners on first and second: the lead runner (second, target 3) is
        # picked off before the pitch. The runner on first could still
        # steal, and on a ball every row goes, but the pitch that carried
        # the pickoff draws no steal.
        sm = _machine("ball", steal=ALWAYS_SAFE, **_certain_pickoff("out"))
        calls = _record(sm, "pickoff_draw", "steal_draw")
        state = _state()
        state.bases = Bases(first=RUNNER, second=202)
        result = sm.step_pitch(state)
        assert result.pickoff_out
        assert calls[0][0] == "pickoff_draw" and calls[0][1][0] == 3
        assert "steal_draw" not in _names(calls)
        assert not result.steal_attempted
        assert state.bases.first == RUNNER and state.bases.second is None
        assert state.outs == 1
        # The next pitch (1-0: no pickoff row there) draws the steal again.
        calls.clear()
        result = sm.step_pitch(state)
        assert _names(calls) == ["pickoff_draw", "steal_draw"]
        assert calls[1][2]["pitch_class"] == "ball"
        assert result.steal_attempted and result.steal_outcome == STEAL_SAFE
        assert state.bases.second == RUNNER and state.bases.first is None

    def test_a_pickoff_throw_that_gets_away_moves_the_runner_up_before_the_pitch(self):
        sm = _machine("ball", steal=ALWAYS_SAFE, **_certain_pickoff("error"))
        state = _state()
        state.bases = Bases(first=RUNNER)
        calls = _record(
            sm,
            "draw",
            "steal_draw",
            note=lambda: (state.bases.first, state.bases.second),
        )
        result = sm.step_pitch(state)
        assert result.pickoff_error and not result.pickoff_out
        # The runner stood on second when the pitch was drawn.
        assert [c[3] for c in calls if c[0] == "draw"] == [(None, RUNNER)]
        # No steal draw on the pitch, and no steal credit for the advance.
        assert "steal_draw" not in _names(calls)
        assert not result.steal_attempted and result.steal_outcome is None
        assert result.baserunner_advances == {RUNNER: 2}
        assert state.bases.second == RUNNER and state.outs == 0
        assert sm.boxscore is None  # no stolen base, no box credit of any kind
        assert result.pitch_outcome == "ball" and state.pitcher_pitch_count == 1
        tally = sm.running_game_tally
        assert tally.pickoff_errors == 1 and tally.pickoff_outs == 0


class TestAttemptsRideTheirClass:
    """Design test 5: a pool whose runners go on balls only."""

    #: Runners go on 10% of balls and on no other class. At 4,000 pitches the
    #: share's standard error is 0.47 points, so 2 points is 4.2 of them.
    RATE = 0.10
    N_BALLS = 4000

    def _machine(self, pitch: str) -> StateMachine:
        return _machine(
            pitch,
            steal=(self.RATE, 0.78),
            class_rates={"called_strike": 0.0, "swinging_strike": 0.0},
        )

    def test_balls_in_play_never_carry_a_steal(self):
        sm = self._machine("in_play")
        calls = _record(sm, "steal_draw")
        assert _attempt_share(sm, 500) == 0.0
        # The draw ran on every pitch, in the in-play group, and never went.
        assert len(calls) == 500
        assert {c[2]["pitch_class"] for c in calls} == {"in_play"}

    def test_balls_carry_steals_at_the_pools_rate(self):
        share = _attempt_share(self._machine("ball"), self.N_BALLS)
        assert share == pytest.approx(self.RATE, abs=0.02)

    @pytest.mark.parametrize("pitch", ["called_strike", "swinging_strike", "foul"])
    def test_the_other_classes_carry_none(self, pitch):
        assert _attempt_share(self._machine(pitch), 300) == 0.0


class TestAClassGroupAnswersFromItsOwnRows:
    """Design test 6. The (0 outs, 0-0) count group holds three ball rows,
    all of them a safe steal, and three called-strike rows, none of them an
    attempt. It holds no swinging-strike row. The count group's answer is a
    steal half the time; each class group's answer is its own."""

    def _pool(self) -> StealPool:
        return _cell_pool(
            ["ball"] * 3 + ["called_strike"] * 3,
            [1, 1, 1, 0, 0, 0],
        )

    def test_the_sampler_reads_each_class_group_alone(self):
        fp = _cell_sampler(self._pool())
        ball = [_steal_draw(fp, pitch_class="ball") for _ in range(200)]
        assert all(d == (True, True, False, False, False) for d in ball)
        called = [_steal_draw(fp, pitch_class="called_strike") for _ in range(200)]
        assert all(d == (False, False, False, False, False) for d in called)

    def test_an_empty_class_group_stages_no_steal(self):
        fp = _cell_sampler(self._pool())
        for pitch in ("swinging_strike", "foul", "in_play", "hit_by_pitch"):
            assert all(_steal_draw(fp, pitch_class=pitch) is None for _ in range(50))

    def test_the_count_group_mixes_the_classes(self):
        # The contrast that proves nothing falls back: the count group (the
        # draw with no class) goes about half the time.
        fp = _cell_sampler(self._pool())
        went = sum(_steal_draw(fp)[0] for _ in range(400))
        assert 120 < went < 280

    @pytest.mark.parametrize(
        ("pitch", "steals"),
        [("ball", True), ("called_strike", False), ("swinging_strike", False)],
    )
    def test_the_loop_stages_the_class_groups_answer(self, pitch, steals):
        art = fixed_play_artifacts("field_out", pitch_model={pitch: 1.0})
        art.steal_pools = {"2": self._pool()}
        sm = _machine(art=art)
        assert sm._steal_order_active()
        share = _attempt_share(sm, 60)
        assert share == (1.0 if steals else 0.0)


class TestPickoffRowsBelongToThePickoffDraw:
    """Design test 7. The (0 outs, 0-0) count group holds three ball rows
    (never an attempt) and three pickoff rows (a pickoff out each)."""

    def _pool(self) -> StealPool:
        return _cell_pool(
            ["ball"] * 3 + [None] * 3,
            [0] * 6,
            pickoff_out=[0, 0, 0, 1, 1, 1],
        )

    def test_the_pickoff_draw_reads_every_row_of_the_count_group(self):
        fp = _cell_sampler(self._pool())
        draws = [_pickoff_draw(fp) for _ in range(800)]
        outs = sum(d[0] for d in draws)
        # Half the group's weight is a pickoff out.
        assert outs / 800 == pytest.approx(0.5, abs=0.06)
        assert not any(d[2] for d in draws)

    def test_a_count_group_with_no_pickoff_outcome_skips_the_draw(self):
        fp = _cell_sampler(self._pool())
        state = fp.rng.bit_generator.state
        assert _pickoff_draw(fp, balls=1) is None
        assert fp.rng.bit_generator.state == state  # no random number used

    def test_the_steal_draw_never_draws_a_pickoff_row(self):
        fp = _cell_sampler(self._pool())
        quiet = (False, False, False, False, False)
        assert all(_steal_draw(fp, pitch_class="ball") == quiet for _ in range(300))

    def test_the_single_draw_leaves_the_pickoff_rows_out_on_a_classed_pool(self):
        # The flag off: the pre-pitch draw reads the count group of PITCH
        # rows, the rows it read before migration 0031.
        fp = _cell_sampler(self._pool())
        quiet = (False, False, False, False, False)
        assert all(_steal_draw(fp) == quiet for _ in range(300))
        fp.steal_pitch_class = False
        assert all(_steal_draw(fp, pitch_class="ball") == quiet for _ in range(300))

    def test_the_flag_off_ignores_the_class_keyword(self):
        """With the flag off, a caller that passes ``pitch_class`` still gets
        the count group. Here the two groups answer differently: runners never
        go on balls, always on called strikes."""
        pool = _cell_pool(["ball"] * 3 + ["called_strike"] * 3, [0, 0, 0, 1, 1, 1])
        fp = _cell_sampler(pool)
        assert not any(_steal_draw(fp, pitch_class="ball")[0] for _ in range(200))
        fp.steal_pitch_class = False
        went = sum(_steal_draw(fp, pitch_class="ball")[0] for _ in range(600))
        assert 0.4 < went / 600 < 0.6  # the whole count group, half of it attempts


class TestTheOldOrderTalliesItsPickoffs:
    """The census reads the tally for both arms, so the single pre-pitch draw
    must count the pickoffs it stages, at the count it drew them."""

    def test_a_pickoff_staged_before_the_pitch_is_counted(self):
        pool = dataclasses.replace(
            _cell_pool(["ball"] * 2, [0, 0], pickoff_out=[1, 1]), pitch_class=None
        )
        fp = _cell_sampler(pool)
        sm = StateMachine(fp, rng=np.random.default_rng(1))
        assert not sm._steal_order_active()
        state = _state()
        state.bases = Bases(first=RUNNER)
        result = sm.step_pitch(state, pitch_outcome="ball")
        assert result.pickoff_out and state.bases.first is None
        tally = sm.running_game_tally
        assert (tally.pickoff_outs, tally.pickoff_errors) == (1, 0)
        assert tally.pickoffs_by_count == {(0, 0): 1}
        assert tally.pickoff_draws == 0  # no pickoff draw ran: the old order


class TestAPartlyClassedPoolKeepsTheSingleDraw:
    """A season no builder rebuilt since migration 0031 leaves pitch rows with
    no class beside the pickoff rows (both code 0). The pickoff-row mark tells
    them apart. Such a pool keeps the single pre-pitch draw over EVERY pitch
    row, and says so; it never runs a steal draw blind to some seasons."""

    def _pool(self) -> StealPool:
        # A classed ball, an unclassed pitch row (a steal), a pickoff row.
        return _cell_pool(
            ["ball", None, None],
            [0, 1, 0],
            pickoff_out=[0, 0, 1],
            is_pickoff_row=[0, 0, 1],
        )

    def test_the_new_order_is_off_and_every_pitch_row_stays(self, caplog):
        fp = _cell_sampler(self._pool())
        with caplog.at_level(logging.WARNING, logger="simulation.full_pool_sampler"):
            assert fp.has_steal_classes() is False
        meta = fp._steal_meta("2")
        assert "class_cells" not in meta and "pickoff_cells" not in meta
        assert meta["cells"][(0, 0, 0)].tolist() == [0, 1]  # the pickoff row is out
        assert "1 of 2 pitch rows carry no pitch class" in caplog.text

    def test_the_single_draw_reaches_the_unclassed_steal_row(self):
        fp = _cell_sampler(self._pool())
        draws = [_steal_draw(fp) for _ in range(400)]
        assert 0.4 < sum(d[0] for d in draws) / 400 < 0.6
        assert not any(d[2] for d in draws)  # the pickoff row is never drawn

    def test_one_unclassed_target_keeps_both_targets_in_the_old_order(self):
        classed = _cell_pool(["ball", "foul"], [1, 0])
        fp = FullPoolSampler(
            EngineArtifacts({}, steal_pools={"2": classed, "3": self._pool()}),
            np.random.default_rng(3),
        )
        assert "class_cells" in fp._steal_meta("2")
        assert fp.has_steal_classes() is False
        sm = StateMachine(fp, rng=np.random.default_rng(3))
        assert sm._steal_order_active() is False


class _Sim474Sampler:
    """A duck-typed sampler with the SIM-474 surface only: no
    ``steal_pitch_class``, no ``has_steal_classes``, no ``pickoff_draw``.
    Every other read goes to a real sampler."""

    _HIDDEN = frozenset({"steal_pitch_class", "has_steal_classes", "pickoff_draw"})

    def __init__(self, inner: FullPoolSampler) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name in self._HIDDEN:
            raise AttributeError(name)
        return getattr(self._inner, name)


class TestTheSinglePrePitchDrawStays:
    """Design test 8: with the flag off, a pool without classes or a sampler
    without the new methods, the loop runs the SIM-474 order: one steal draw
    in the pre-pitch hook, with no class, and no pickoff draw."""

    def _one_pitch(self, sm: StateMachine) -> list[tuple[str, tuple, dict, Any]]:
        in_hook = [False]
        real_hook = sm._pre_pitch_hook

        def hook(state):
            in_hook[0] = True
            try:
                return real_hook(state)
            finally:
                in_hook[0] = False

        sm._pre_pitch_hook = hook
        names = ["draw", "steal_draw"]
        if hasattr(sm.full_pool_sampler, "pickoff_draw"):
            names.append("pickoff_draw")
        calls = _record(sm, *names, note=lambda: in_hook[0])
        state = _state()
        state.bases = Bases(first=RUNNER)
        sm.step_pitch(state)
        return calls

    def _assert_the_old_order(self, calls) -> None:
        assert _names(calls) == ["steal_draw", "draw"]
        steal = calls[0]
        assert steal[3] is True  # drawn inside the pre-pitch hook
        assert "pitch_class" not in steal[2]

    def test_the_flag_off(self):
        sm = _machine("called_strike", **_certain_pickoff("out"))
        sm.full_pool_sampler.steal_pitch_class = False
        assert not sm._steal_order_active()
        self._assert_the_old_order(self._one_pitch(sm))

    def test_a_pool_without_classes(self):
        sm = _machine("called_strike", by_class=False)
        assert sm.full_pool_sampler.has_steal_pool()
        assert not sm.full_pool_sampler.has_steal_classes()
        assert not sm._steal_order_active()
        self._assert_the_old_order(self._one_pitch(sm))

    def test_a_duck_typed_sampler(self):
        inner = _machine("called_strike", **_certain_pickoff("out")).full_pool_sampler
        sm = StateMachine(_Sim474Sampler(inner), rng=np.random.default_rng(0))
        assert not sm._steal_order_active()
        self._assert_the_old_order(self._one_pitch(sm))

    def test_a_magicmock_sampler(self):
        fp = MagicMock()
        fp.steal_draw.return_value = (True, True)
        fp.draw.return_value = "ball"
        sm = StateMachine(fp, rng=np.random.default_rng(0))
        assert not sm._steal_order_active()
        state = _state()
        state.bases = Bases(first=RUNNER)
        result = sm.step_pitch(state)
        fp.pickoff_draw.assert_not_called()
        assert "pitch_class" not in fp.steal_draw.call_args.kwargs
        order = [c[0] for c in fp.method_calls]
        assert order.index("steal_draw") < order.index("draw")
        assert result.steal_attempted and result.steal_outcome == STEAL_SAFE


class TestAnInjectedPitchIsCheckedFirst:
    def test_an_unknown_pitch_raises_before_any_draw(self):
        sm = _machine("ball", **_certain_pickoff("out"))
        calls = _record(sm, "pickoff_draw", "draw", "steal_draw")
        state = _state()
        state.bases = Bases(first=RUNNER)
        for bad in (NO_PITCH, "strike"):
            with pytest.raises(ValueError, match="is not one of"):
                sm.step_pitch(state, pitch_outcome=bad)
        assert calls == []
        assert state.pitcher_pitch_count == 0
        assert state.bases.first == RUNNER and state.outs == 0
        assert sm._pending_steal is None
        assert NO_PITCH not in PITCH_OUTCOMES

    def test_an_unknown_pitch_raises_before_any_draw_in_the_single_draw_order_too(self):
        sm = _machine("ball", steal=ALWAYS_SAFE, by_class=False)
        assert not sm._steal_order_active()
        calls = _record(sm, "draw", "steal_draw")
        state = _state()
        state.bases = Bases(first=RUNNER)
        with pytest.raises(ValueError, match="is not one of"):
            sm.step_pitch(state, pitch_outcome="strike")
        assert state.pitcher_pitch_count == 0
        assert sm._pending_steal is None
        assert calls == []


# ---------------------------------------------------------------------------
# The pickoff's third out (design tests 10-11)
# ---------------------------------------------------------------------------


class TestThePickoffThirdOutThrowsNoPitch:
    def _third_out(self, kind: str = "out", *, balls: int = 0, strikes: int = 0):
        sm = _machine("ball", **_certain_pickoff(kind, counts=((balls, strikes),)))
        calls = _record(sm, "pickoff_draw", "draw", "steal_draw")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 2
        state.balls = balls
        state.strikes = strikes
        result = sm.step_pitch(state)
        return sm, state, result, calls

    @pytest.mark.parametrize("kind", ["out", "advancing"])
    def test_no_pitch_is_thrown_and_only_the_out_is_credited(self, kind):
        sm, state, result, calls = self._third_out(kind)
        assert _names(calls) == ["pickoff_draw"]  # no pitch draw, no steal draw
        assert result.no_pitch and result.pitch_outcome == NO_PITCH
        assert result.pa_voided == "pickoff_third_out"
        assert result.event is None and not result.pa_terminal and not result.is_contact
        assert result.pickoff_out and result.outs_recorded == 1
        assert result.next_state is state
        assert state.pitcher_pitch_count == 0
        assert state.pitcher_bf.get(PITCHER, 0) == 0
        assert state.away_lineup_slot == 0
        # No at-bat, no strikeout, no walk: the batter's line is untouched.
        assert sm.boxscore.batters == {}
        # The out is the pitcher's (his outs feed innings pitched and the
        # pitcher-outs prop), and it is his only credit.
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 1
        assert (pit.k, pit.bb, pit.er, pit.r_allowed, pit.h_allowed) == (0, 0, 0, 0, 0)
        assert set(sm.boxscore.pitchers) == {PITCHER}
        # A picked-off caught stealing also charges the runner a CS.
        assert sm.boxscore.line(RUNNER).cs == (1 if kind == "advancing" else 0)

    def test_the_half_rolls_and_the_same_batter_leads_off_next_time(self):
        sm, state, _result, _calls = self._third_out()
        assert state.half == Half.BOTTOM and state.inning == 1
        assert state.outs == 0 and state.bases.occupancy == (False, False, False)
        assert state.batter_id == 800  # the home side's leadoff man is up
        sm.advance_half_inning(state)  # the bottom half ends
        assert state.half == Half.TOP and state.inning == 2
        assert state.batter_id == BATTER

    def test_the_tally_counts_it(self):
        sm, _state_, _result, _calls = self._third_out()
        tally = sm.running_game_tally
        assert tally.no_pitch_third_outs == 1
        assert tally.pickoff_draws == 1 and tally.pickoff_outs == 1
        assert tally.pickoffs_by_count == {(0, 0): 1}

    def test_in_a_full_count_the_batter_starts_over_at_0_0(self):
        sm, state, result, _calls = self._third_out(balls=3, strikes=2)
        assert result.pa_voided == "pickoff_third_out"
        assert sm.running_game_tally.pickoffs_by_count == {(3, 2): 1}
        assert (state.balls, state.strikes) == (0, 0)
        sm.advance_half_inning(state)  # the bottom half ends
        assert state.batter_id == BATTER
        calls = _record(sm, "draw")
        result = sm.step_pitch(state)
        assert calls[0][1] == (0, 0)  # his first pitch is drawn at 0-0
        assert result.pitch_outcome == "ball" and state.pitcher_pitch_count == 1
        assert (state.balls, state.strikes) == (1, 0)


# ---------------------------------------------------------------------------
# The third out first (decision 3, design tests 13-17)
# ---------------------------------------------------------------------------


@BOTH_ORDERS
class TestTheThirdOutComesFirst:
    def _held_k3(self, pitch: str, steal, *, outs: int, staged: StealResolution, bases: Bases):
        sm = _machine(pitch, got_away=False, steal=steal)
        state = _state()
        state.bases = bases
        state.outs = outs
        state.strikes = 2
        sm._pending_steal = staged
        result = sm.step_pitch(state)
        return sm, state, result

    def test_a_safe_steal_on_a_two_out_called_strike_three_is_void(self, steal):
        sm, state, result = self._held_k3(
            "called_strike",
            steal,
            outs=2,
            staged=_steal(RUNNER, 1, 2, safe=True),
            bases=Bases(first=RUNNER),
        )
        assert result.steal_voided == "third_out_first"
        assert not result.steal_attempted and result.steal_outcome is None
        assert RUNNER not in result.baserunner_advances
        assert result.canonical_event == "strikeout" and result.outs_recorded == 1
        assert state.half == Half.BOTTOM and state.outs == 0  # the half rolled
        assert sm.boxscore.line(RUNNER).sb == 0
        assert sm.boxscore.line(PITCHER).k == 1 and sm.boxscore.line(BATTER).so == 1
        assert state.away_score == 0
        assert sm.running_game_tally.voided == {"third_out_first": 1}

    def test_a_caught_stealing_on_a_two_out_swinging_strike_three_is_void(self, steal):
        sm, state, result = self._held_k3(
            "swinging_strike",
            steal,
            outs=2,
            staged=_steal(RUNNER, 1, 2, safe=False),
            bases=Bases(first=RUNNER),
        )
        assert result.steal_voided == "third_out_first"
        assert result.steal_outcome is None
        assert result.outs_recorded == 1  # the strikeout, once
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 1
        assert sm.boxscore.line(RUNNER).cs == 0
        assert state.half == Half.BOTTOM

    def test_with_one_out_the_strikeout_and_the_caught_stealing_both_stand(self, steal):
        sm, state, result = self._held_k3(
            "swinging_strike",
            steal,
            outs=1,
            staged=_steal(RUNNER, 1, 2, safe=False),
            bases=Bases(first=RUNNER),
        )
        assert result.steal_voided is None
        assert result.steal_attempted and result.steal_outcome == STEAL_CAUGHT
        assert result.outs_recorded == 2
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 2
        assert sm.boxscore.line(RUNNER).cs == 1
        assert state.half == Half.BOTTOM
        assert sm.running_game_tally.voided == {}

    def test_a_two_out_got_away_strike_three_keeps_the_steal_first(self, steal):
        # The SIM-484 order: the steal resolves on the pitch, then the
        # dropped-third-strike reach reads the bases at the pitch.
        sm = _machine("swinging_strike", got_away=True, steal=steal)
        state = _state()
        state.bases = Bases(second=202)
        state.outs = 2
        state.strikes = 2
        sm._pending_steal = _steal(202, 2, 3, safe=True)
        result = sm.step_pitch(state)
        assert result.steal_voided is None
        assert result.steal_attempted and result.steal_outcome == STEAL_SAFE
        assert result.canonical_event == "strikeout" and result.outs_recorded == 0
        assert (state.bases.first, state.bases.second, state.bases.third) == (BATTER, None, 202)
        assert state.outs == 2 and state.away_score == 0
        assert sm.boxscore.line(202).sb == 1
        assert sm.boxscore.line(PITCHER).k == 1 and sm.boxscore.line(BATTER).so == 1
        assert sm.running_game_tally.voided == {}

    def test_a_caught_stealing_at_third_on_ball_four_is_the_third_out(self, steal):
        sm = _machine("ball", steal=steal)
        state = _state()
        state.bases = Bases(second=202)
        state.outs = 2
        state.balls = 3
        sm._pending_steal = _steal(202, 2, 3, safe=False)
        result = sm.step_pitch(state)
        assert result.steal_voided is None
        assert result.event == "walk" and result.steal_outcome == STEAL_CAUGHT
        assert result.outs_recorded == 1
        # The walk stands: no at-bat for the batter, a walk for the pitcher.
        bat = sm.boxscore.line(BATTER)
        pit = sm.boxscore.line(PITCHER)
        assert bat.ab == 0 and bat.so == 0
        assert pit.bb == 1 and pit.outs_recorded == 1
        assert sm.boxscore.line(202).cs == 1
        # The caught stealing ended the half; the order moved past the batter.
        assert state.half == Half.BOTTOM
        assert state.away_lineup_slot == 1
        assert state.pitcher_bf[PITCHER] == 1

    @pytest.mark.parametrize("error", [False, True], ids=["pickoff_out", "pickoff_error"])
    def test_a_staged_pickoff_is_never_void(self, steal, error):
        # The pickoff is thrown before the pitch, so the strikeout cannot
        # come first.
        sm, _state_, result = self._held_k3(
            "called_strike",
            steal,
            outs=2,
            staged=_pickoff(error=error),
            bases=Bases(first=RUNNER),
        )
        assert result.steal_voided is None
        assert result.pickoff_error if error else result.pickoff_out
        if error:
            assert result.baserunner_advances == {RUNNER: 2}
        assert sm.running_game_tally.voided == {}


# ---------------------------------------------------------------------------
# The runners on the reach (decision 4, design tests 18-21)
# ---------------------------------------------------------------------------


@BOTH_ORDERS
class TestTheRunnersMoveOnTheReach:
    """A swinging third strike that gets away with first base open or two
    outs: every runner moves up one base while the ball is loose, then the
    batter takes first."""

    def _reach(self, steal, bases: Bases, *, outs: int, staged: StealResolution | None = None):
        sm = _machine("swinging_strike", got_away=True, steal=steal)
        state = _state()
        state.bases = bases
        state.outs = outs
        state.strikes = 2
        if staged is not None:
            sm._pending_steal = staged
        result = sm.step_pitch(state)
        return sm, state, result

    def _k_on_both_lines(self, sm: StateMachine) -> None:
        assert sm.boxscore.line(PITCHER).k == 1 and sm.boxscore.line(PITCHER).outs_recorded == 0
        bat = sm.boxscore.line(BATTER)
        assert bat.so == 1 and bat.ab == 1 and bat.k == 0

    def test_a_runner_on_second_takes_third(self, steal):
        sm, state, result = self._reach(steal, Bases(second=202), outs=1)
        assert result.canonical_event == "strikeout" and result.outs_recorded == 0
        assert (state.bases.first, state.bases.second, state.bases.third) == (BATTER, None, 202)
        assert result.baserunner_advances == {202: 3, BATTER: 1}
        assert state.outs == 1 and result.runs_scored == 0 and state.away_score == 0
        assert not result.steal_attempted and sm.boxscore.line(202).sb == 0
        self._k_on_both_lines(sm)
        tally = sm.running_game_tally
        assert tally.d3k_reaches == 1 and tally.d3k_runners_moved == 1

    def test_a_runner_on_third_scores_with_no_rbi(self, steal):
        sm, state, result = self._reach(steal, Bases(third=303), outs=1)
        assert result.runs_scored == 1 and state.away_score == 1
        assert result.steal_runs_scored == 1  # the no-RBI marker
        assert (state.bases.first, state.bases.second, state.bases.third) == (BATTER, None, None)
        assert state.outs == 1
        assert sm.boxscore.line(303).r == 1  # credited once
        assert sm.boxscore.line(BATTER).rbi == 0
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 1 and pit.er == 1  # earned
        self._k_on_both_lines(sm)
        assert sm.running_game_tally.d3k_runners_moved == 1

    def test_bases_loaded_two_outs_every_runner_moves_up_one(self, steal):
        # The SIM-484 test's bases and score: the run now scores on the
        # got-away advance, not on the reach's push.
        sm, state, result = self._reach(steal, Bases(first=RUNNER, second=202, third=303), outs=2)
        assert result.canonical_event == "strikeout"
        assert result.runs_scored == 1 and state.away_score == 1
        assert result.steal_runs_scored == 1
        assert state.outs == 2
        assert (state.bases.first, state.bases.second, state.bases.third) == (BATTER, RUNNER, 202)
        assert result.baserunner_advances == {303: 0, 202: 3, RUNNER: 2, BATTER: 1}
        assert sm.boxscore.line(303).r == 1
        assert sm.boxscore.line(BATTER).rbi == 0
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 1 and pit.er == 1
        self._k_on_both_lines(sm)
        tally = sm.running_game_tally
        assert tally.d3k_reaches == 1 and tally.d3k_runners_moved == 3

    def test_a_steal_on_the_pitch_skips_the_advance_and_the_push_remains(self, steal):
        # Runners on first and second, two outs: the runner on second steals
        # third on the pitch. The steal is the pitch's one mover, so the
        # got-away advance does not run; the batter's reach still forces the
        # runner on first to second.
        sm, state, result = self._reach(
            steal,
            Bases(first=RUNNER, second=202),
            outs=2,
            staged=_steal(202, 2, 3, safe=True),
        )
        assert result.steal_attempted and result.steal_outcome == STEAL_SAFE
        assert (state.bases.first, state.bases.second, state.bases.third) == (BATTER, RUNNER, 202)
        assert result.baserunner_advances == {202: 3, RUNNER: 2, BATTER: 1}
        assert sm.boxscore.line(202).sb == 1
        assert state.away_score == 0 and state.outs == 2
        self._k_on_both_lines(sm)
        tally = sm.running_game_tally
        assert tally.d3k_reaches == 1 and tally.d3k_runners_moved == 0

    def test_without_the_steal_the_advance_moves_both_runners(self, steal):
        sm, state, result = self._reach(steal, Bases(first=RUNNER, second=202), outs=2)
        assert not result.steal_attempted
        assert (state.bases.first, state.bases.second, state.bases.third) == (BATTER, RUNNER, 202)
        assert sm.boxscore.line(202).sb == 0
        tally = sm.running_game_tally
        assert tally.d3k_reaches == 1 and tally.d3k_runners_moved == 2

    def test_a_held_strike_three_is_no_reach(self, steal):
        sm = _machine("swinging_strike", got_away=False, steal=steal)
        state = _state()
        state.bases = Bases(second=202)
        state.strikes = 2
        result = sm.step_pitch(state)
        assert result.outs_recorded == 1 and state.bases.second == 202
        assert sm.running_game_tally.d3k_reaches == 0


# ---------------------------------------------------------------------------
# The synthetic pool (design test 25)
# ---------------------------------------------------------------------------


def _groups(pool: StealPool) -> Counter:
    sit = pool.sit.astype(int)
    return Counter(
        (o, b, s, int(c))
        for b, s, o, c in zip(sit[:, 0], sit[:, 1], sit[:, 2], pool.pitch_class, strict=True)
    )


def _class_rates(pool: StealPool) -> dict[str, float]:
    """The attempted share of each class's weight, over the whole pool."""
    out = {}
    for name in STEAL_PITCH_CLASSES:
        rows = pool.pitch_class == STEAL_PITCH_CLASS_CODE[name]
        w = pool.recency[rows]
        out[name] = float(w[pool.attempted[rows] == 1].sum() / w.sum())
    return out


class TestTheSyntheticPool:
    def test_three_rows_per_count_and_class(self):
        pools = steal_pools(0.2, 0.75)
        assert set(pools) == {"2", "3"}
        pool = pools["2"]
        assert pool.n == 36 * 6 * 3
        assert pool.pitch_class is not None and pool.pitch_class.dtype == np.int8
        groups = _groups(pool)
        assert len(groups) == 36 * 6 and set(groups.values()) == {3}
        assert {k[3] for k in groups} == set(range(1, 7))
        # Each count group's pitch rows weigh 1 in all.
        sit = pool.sit.astype(int)
        for o in range(3):
            for b in range(4):
                for s in range(3):
                    cell = (sit[:, 2] == o) & (sit[:, 0] == b) & (sit[:, 1] == s)
                    assert float(pool.recency[cell].sum()) == pytest.approx(1.0, rel=1e-5)
        assert not pool.pickoff_out.any() and not pool.pickoff_error.any()

    def test_the_attempt_rate_lands_on_the_stealable_classes_only(self):
        pool = steal_pools(0.2, 0.75)["2"]
        rates = _class_rates(pool)
        for name in STEAL_PITCH_CLASSES:
            want = 0.2 if name in STEALABLE_CLASSES else 0.0
            assert rates[name] == pytest.approx(want, abs=1e-6)
        att = pool.attempted == 1
        safe = pool.recency[att & (pool.success == 1)].sum() / pool.recency[att].sum()
        assert float(safe) == pytest.approx(0.75, abs=1e-6)

    def test_class_rates_override_per_class(self):
        pool = steal_pools(0.2, class_rates={"ball": 0.5, "called_strike": 0.0, "foul": 0.05})["3"]
        rates = _class_rates(pool)
        assert rates["ball"] == pytest.approx(0.5, abs=1e-6)
        assert rates["called_strike"] == pytest.approx(0.0, abs=1e-6)
        assert rates["swinging_strike"] == pytest.approx(0.2, abs=1e-6)
        assert rates["foul"] == pytest.approx(0.05, abs=1e-6)
        assert rates["in_play"] == 0.0 and rates["hit_by_pitch"] == 0.0

    def test_an_unknown_class_is_refused(self):
        with pytest.raises(ValueError, match="unknown pitch classes"):
            steal_pools(0.2, class_rates={"strike": 0.1})

    @pytest.mark.parametrize(
        ("kind", "labels"),
        [("out", (1, 0, 0)), ("advancing", (1, 1, 0)), ("error", (0, 0, 1))],
    )
    def test_pickoff_rows_land_in_the_named_counts_only(self, kind, labels):
        pool = steal_pools(
            0.2,
            pickoff_rows=2,
            pickoff_weight=0.03,
            pickoff_kind=kind,
            pickoff_counts=((0, 0), (3, 2)),
        )["2"]
        po = pool.pitch_class == 0
        assert int(po.sum()) == 2 * 3 * 2  # two rows x three outs x two counts
        sit = pool.sit[po].astype(int)
        assert {(b, s) for b, s in zip(sit[:, 0], sit[:, 1], strict=True)} == {(0, 0), (3, 2)}
        assert set(sit[:, 2]) == {0, 1, 2}
        assert np.all(pool.attempted[po] == 0) and np.all(pool.success[po] == 0)
        assert np.allclose(pool.recency[po], 0.03)
        got = (pool.pickoff_out[po], pool.pickoff_advancing[po], pool.pickoff_error[po])
        for col, want in zip(got, labels, strict=True):
            assert np.all(col == want)
        # The pitch rows carry no pickoff label.
        assert not pool.pickoff_out[~po].any() and not pool.pickoff_error[~po].any()

    def test_pickoff_rows_go_in_every_count_by_default(self):
        pool = steal_pools(0.2, pickoff_rows=1)["2"]
        assert int((pool.pitch_class == 0).sum()) == 36

    def test_the_pickoff_draw_answers_at_the_rows_share(self):
        # k rows of weight w beside pitch rows weighing 1: k*w / (1 + k*w).
        art = fixed_play_artifacts(
            "field_out",
            steal=NO_ATTEMPTS,
            steal_kw={"pickoff_rows": 1, "pickoff_weight": 1.0, "pickoff_counts": ((0, 0),)},
        )
        fp = synthetic_sampler(art, 3)
        outs = sum(_pickoff_draw(fp)[0] for _ in range(1500))
        assert outs / 1500 == pytest.approx(0.5, abs=0.05)
        assert _pickoff_draw(fp, balls=1) is None  # no pickoff row at 1-0

    def test_by_class_false_is_the_sim474_shape(self):
        pool = steal_pools(0.2, 0.75, by_class=False)["2"]
        assert pool.n == 36 * 3 and pool.pitch_class is None
        art = fixed_play_artifacts("field_out", steal=(0.2, 0.75), steal_kw={"by_class": False})
        fp = synthetic_sampler(art, 0)
        assert fp.has_steal_pool() and not fp.has_steal_classes()
        assert _pickoff_draw(fp) is None

    def test_pickoff_rows_need_classes(self):
        with pytest.raises(ValueError, match="pickoff rows need"):
            steal_pools(0.2, by_class=False, pickoff_rows=1)

    def test_a_classed_pool_turns_the_new_order_on(self):
        sm = _machine("ball")
        assert sm.full_pool_sampler.has_steal_classes()
        assert sm._steal_order_active()


# ---------------------------------------------------------------------------
# A whole game in the new order
# ---------------------------------------------------------------------------


class TestAWholeGame:
    """Full games on a classed bundle: busy runners, pickoffs at every count,
    hit by pitches in the pitch mix. ``simulate_game`` checks the state's
    invariants after every step."""

    def _games(self, seeds) -> tuple[list, list, list[StateMachine]]:
        results: list = []
        games: list = []
        machines: list[StateMachine] = []
        for seed in seeds:
            art = synthetic_artifacts(
                pitch_model={**LEAGUE_PITCH_MODEL, "hit_by_pitch": 0.01},
                inplay_model=LEAGUE_INPLAY_MODEL,
                advancement=True,
                steal=(0.25, 0.78),
                steal_kw={
                    "class_rates": {"foul": 0.0, "in_play": 0.0, "hit_by_pitch": 0.0},
                    "pickoff_rows": 1,
                    "pickoff_weight": 0.03,
                },
            )
            sm = StateMachine(synthetic_sampler(art, seed), rng=np.random.default_rng(seed))
            assert sm._steal_order_active()
            real = sm.step_pitch

            def step(state, _real=real, **kw):
                result = _real(state, **kw)
                results.append(result)
                return result

            sm.step_pitch = step
            games.append(
                simulate_game(
                    sm,
                    seed=seed,
                    away_lineup=list(range(101, 110)),
                    home_lineup=list(range(201, 210)),
                )
            )
            machines.append(sm)
        return results, games, machines

    def test_steals_ride_only_the_stealable_classes(self):
        results, games, machines = self._games(range(4))
        steals = [r for r in results if r.steal_attempted]
        assert steals, "the busy pool staged no steal in four games"
        assert {r.pitch_outcome for r in steals} <= set(STEALABLE_CLASSES)
        # The pitch count skips a pickoff that ended the half before a pitch.
        no_pitch = [r for r in results if r.no_pitch]
        assert no_pitch, "no pickoff ended a half before a pitch in four games"
        assert sum(g.total_pitches for g in games) == len(results) - len(no_pitch)
        assert all(r.pa_voided == "pickoff_third_out" and r.pickoff_out for r in no_pitch)
        assert sum(m.running_game_tally.no_pitch_third_outs for m in machines) == len(no_pitch)
        picked = sum(m.running_game_tally.pickoff_outs for m in machines)
        assert picked == sum(r.pickoff_out for r in results)
        assert picked > 0
