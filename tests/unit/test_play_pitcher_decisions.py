"""SIM-557 — the play names its own pitcher, and the win module reads it.

The plan is ``docs/audit/2026-10-04-sim557-pitcher-outs-credit-plan.md``
(decision 2, sections 5.4 and 7, tests 22 to 28).

Every play result now names the pitcher on the mound for that play
(``PlayResult.pitcher_id``) and his side (``PlayResult.fielding_team``). The
win module (``decisions_from_plays``) used to read both from the play's next
state. On the play that ends a half-inning, the next state already shows the
NEXT half: the other fielding side and the other side's pitcher. So the last
out of every half went to the wrong pitcher. The starter's out count for the
five-inning rule was wrong in most games, and a reliever whose first play
ended a half never became the pitcher of record for it.

The tests here:

* drive ``step_pitch`` on a synthetic bundle and read the two new fields,
  the no-pitch pickoff third out and the intentional walk included;
* build play streams by hand, shaped like the REAL stream (the play that ends
  a half carries the next half's state and the other side's pitcher), and
  read the win, the loss and the save from them;
* strip the two fields from the same streams and pin today's miscount, so the
  fallback for a hand-built stream stays what it was;
* record whole synthetic games with scripted pitching changes and check the
  module's out count against the box line of every winning-side pitcher.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest

from simulation.batch_runner import GameSpec, rng_driven_machine_factory
from simulation.game_state import NO_PITCH, Bases, GameState, Half, PlayResult, Team
from simulation.pitcher_decisions import (
    STARTER_WIN_MIN_OUTS,
    _side_pitcher_outs,
    decisions_from_plays,
)
from simulation.play_recorder import RecordingMachine
from simulation.sim_loop import (
    EVENT_INTENTIONAL_WALK,
    StateMachine,
    simulate_game,
)
from simulation.synthetic_bundle import (
    LEAGUE_INPLAY_MODEL,
    LEAGUE_PITCH_MODEL,
    fixed_play_artifacts,
    synthetic_artifacts,
    synthetic_sampler,
)

SEASON = 2024
HOME_SP = 100
HOME_RP = 101
HOME_CL = 102
AWAY_SP = 200
AWAY_RP = 201
BATTER = 900
RUNNER = 101_101
AWAY_LINEUP = list(range(301, 310))
HOME_LINEUP = list(range(401, 410))

#: The steal-pool keywords for a pickoff on every draw at 0-0: one pickoff
#: row a billion times heavier than the count group's pitch rows.
CERTAIN_PICKOFF: dict[str, Any] = {
    "pickoff_rows": 1,
    "pickoff_weight": 1e9,
    "pickoff_kind": "out",
    "pickoff_counts": ((0, 0),),
}


# ---------------------------------------------------------------------------
# The step_pitch harness (the running-game suite's)
# ---------------------------------------------------------------------------


def _state(**kw: Any) -> GameState:
    """A top-of-the-first state: the home pitcher on the mound, both
    starters named, so the half roll swaps to the away starter."""
    defaults: dict[str, Any] = {
        "pitcher_id": HOME_SP,
        "bat_hand": "R",
        "season": SEASON,
        "batter_id": BATTER,
        "away_lineup": [BATTER + i for i in range(9)],
        "home_lineup": [800 + i for i in range(9)],
    }
    defaults.update(kw)
    state = GameState(**defaults)
    state.home_pitcher_id = HOME_SP
    state.away_pitcher_id = AWAY_SP
    return state


def _machine(
    pitch_outcome: str = "ball",
    *,
    steal: tuple[float, float] | None = (0.0, 0.78),
    steal_kw: dict[str, Any] | None = None,
    seed: int = 0,
) -> StateMachine:
    """A machine whose every drawn pitch is ``pitch_outcome`` and whose every
    ball in play is a field out."""
    art = fixed_play_artifacts(
        "field_out",
        pitch_model={pitch_outcome: 1.0},
        steal=steal,
        steal_kw=steal_kw,
    )
    sm = StateMachine(synthetic_sampler(art, seed), rng=np.random.default_rng(seed))
    sm._got_away = True
    return sm


def _bring_in(state: GameState, arm: int) -> None:
    """A pitching change, as ``_change_pitcher`` makes it: the new arm is
    the pitcher on the mound and his side's current pitcher."""
    state.pitcher_id = arm
    state.pitcher_pitch_count = 0
    if state.half == Half.TOP:
        state.home_pitcher_id = arm
    else:
        state.away_pitcher_id = arm


# ---------------------------------------------------------------------------
# Test 22 and 23 — every result names its pitcher and his side
# ---------------------------------------------------------------------------


class TestEveryResultNamesItsPitcher:
    def test_a_pitch_names_the_pitcher_and_the_fielding_side(self):
        sm = _machine("ball")
        state = _state()
        result = sm.step_pitch(state)
        assert result.pitcher_id == HOME_SP
        assert result.fielding_team == Team.HOME

    def test_a_pitch_in_the_bottom_half_names_the_away_pitcher(self):
        sm = _machine("ball")
        state = _state(half=Half.BOTTOM, pitcher_id=AWAY_SP, batter_id=800)
        result = sm.step_pitch(state)
        assert result.pitcher_id == AWAY_SP
        assert result.fielding_team == Team.AWAY

    def test_the_no_pitch_pickoff_third_out_names_the_pitcher_who_made_the_throw(self):
        sm = _machine("ball", steal_kw=dict(CERTAIN_PICKOFF))
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 2
        result = sm.step_pitch(state)
        assert result.no_pitch and result.pitch_outcome == NO_PITCH
        assert result.pa_voided == "pickoff_third_out"
        # The half rolled: the next state shows the away starter fielding.
        assert result.next_state.half == Half.BOTTOM
        assert result.next_state.pitcher_id == AWAY_SP
        # The play names the home starter, who made the out.
        assert result.pitcher_id == HOME_SP
        assert result.fielding_team == Team.HOME

    def test_the_intentional_walk_names_the_pitcher_who_issues_it(self):
        sm = _machine("ball")
        sm._should_issue_ibb = lambda state, li: True
        state = _state()
        state.bases = Bases(second=RUNNER)
        result = sm.step_pitch(state)
        assert result.event == EVENT_INTENTIONAL_WALK
        assert result.pitcher_id == HOME_SP
        assert result.fielding_team == Team.HOME

    def test_a_reliever_who_enters_on_this_pitch_is_the_one_named(self):
        sm = _machine("ball")
        real_hook = sm._start_of_pa_hook

        def hook(state: GameState) -> None:
            real_hook(state)
            _bring_in(state, HOME_RP)

        sm._start_of_pa_hook = hook
        state = _state()
        result = sm.step_pitch(state)
        assert result.pitcher_id == HOME_RP
        assert result.fielding_team == Team.HOME

    def test_every_result_of_a_whole_game_names_the_pitcher_of_its_half(self):
        """Busy runners and pickoffs at every count, both starters named. The
        side that fields a half is known before the step; every result,
        the no-pitch ones included, names that side and its starter."""
        seen_no_pitch = 0
        for seed in range(8):
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
            seen: list[tuple[Half, PlayResult]] = []
            real = sm.step_pitch

            def step(state, _real=real, _seen=seen, **kw):
                half = state.half
                result = _real(state, **kw)
                _seen.append((half, result))
                return result

            sm.step_pitch = step
            simulate_game(
                sm,
                seed=seed,
                pitcher_id=HOME_SP,
                home_pitcher_id=HOME_SP,
                away_pitcher_id=AWAY_SP,
                away_lineup=AWAY_LINEUP,
                home_lineup=HOME_LINEUP,
            )
            assert seen
            for half, result in seen:
                side = Team.HOME if half == Half.TOP else Team.AWAY
                assert result.fielding_team == side
                assert result.pitcher_id == (HOME_SP if side == Team.HOME else AWAY_SP)
                seen_no_pitch += int(result.no_pitch)
        assert seen_no_pitch > 0, "no pickoff ended a half before a pitch in eight games"


class TestThePlayThatEndsAHalf:
    def test_a_strikeout_for_the_third_out_names_the_pitcher_who_recorded_it(self):
        sm = _machine("called_strike")
        state = _state()
        state.outs = 2
        state.strikes = 2
        result = sm.step_pitch(state)
        assert result.outs_recorded == 1
        assert result.next_state.half == Half.BOTTOM
        assert result.next_state.pitcher_id == AWAY_SP
        assert result.pitcher_id == HOME_SP
        assert result.pitcher_id != result.next_state.pitcher_id
        assert result.fielding_team == Team.HOME

    def test_a_field_out_for_the_third_out_in_the_bottom_names_the_away_pitcher(self):
        sm = _machine("in_play")
        state = _state(half=Half.BOTTOM, pitcher_id=AWAY_SP, batter_id=800)
        state.outs = 2
        result = sm.step_pitch(state)
        assert result.outs_recorded == 1
        assert result.next_state.half == Half.TOP
        assert result.next_state.pitcher_id == HOME_SP
        assert result.pitcher_id == AWAY_SP
        assert result.fielding_team == Team.AWAY


# ---------------------------------------------------------------------------
# The real-shaped stream builder (tests 24 to 27)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class P:
    """One play of a hand-built game: who pitched it, the outs it made and
    the runs that scored on it."""

    pitcher: int
    outs: int = 0
    runs: int = 0


def _gs(*, inning: int, half: Half, home: int, away: int, pitcher_id: int) -> GameState:
    return GameState(
        pitcher_id=pitcher_id,
        bat_hand="R",
        season=SEASON,
        inning=inning,
        half=half,
        home_score=home,
        away_score=away,
    )


def _real_stream(
    halves: Sequence[Sequence[P]],
    *,
    own: bool = True,
) -> list[PlayResult]:
    """A play stream shaped like the simulator's.

    ``halves`` lists the half-innings in order: top 1, bottom 1, top 2, and so
    on. A play's next state is the state after the play. When the play makes
    the third out, the half rolls, as in the loop: the next state is the next
    half, and its pitcher is the fielding side's current pitcher (the last
    pitcher that side used, or its starter). A reliever who starts a half
    enters on that half's first pitch, so the play before him still names the
    old pitcher in its next state.

    ``own`` sets the play's own pitcher and fielding side; ``own=False``
    leaves both at None, the shape of a stream built before SIM-557.
    """
    current = {Team.HOME: halves[0][0].pitcher, Team.AWAY: halves[1][0].pitcher}
    home = away = 0
    stream: list[PlayResult] = []
    for i, plays in enumerate(halves):
        inning = i // 2 + 1
        half = Half.TOP if i % 2 == 0 else Half.BOTTOM
        side = Team.HOME if half == Half.TOP else Team.AWAY
        outs = 0
        for j, p in enumerate(plays):
            current[side] = p.pitcher
            outs += p.outs
            if half == Half.TOP:
                away += p.runs
            else:
                home += p.runs
            ends_half = outs >= 3
            assert outs <= 3, f"half {i} records more than three outs"
            if ends_half:
                assert j == len(plays) - 1, f"half {i} goes on after its third out"
                nxt_half = Half.BOTTOM if half == Half.TOP else Half.TOP
                nxt_inning = inning if half == Half.TOP else inning + 1
                other = Team.AWAY if side == Team.HOME else Team.HOME
                nxt = _gs(
                    inning=nxt_inning,
                    half=nxt_half,
                    home=home,
                    away=away,
                    pitcher_id=current[other],
                )
            else:
                nxt = _gs(inning=inning, half=half, home=home, away=away, pitcher_id=p.pitcher)
            stream.append(
                PlayResult(
                    pitch_outcome="in_play",
                    outs_recorded=p.outs,
                    next_state=nxt,
                    pitcher_id=p.pitcher if own else None,
                    fielding_team=side if own else None,
                )
            )
    return stream


def _strip(stream: Sequence[PlayResult]) -> list[PlayResult]:
    """The same stream with the play's own pitcher and side removed."""
    return [dataclasses.replace(r, pitcher_id=None, fielding_team=None) for r in stream]


def _three(pitcher: int) -> list[P]:
    return [P(pitcher, 1), P(pitcher, 1), P(pitcher, 1)]


def _double_play_starter_game() -> list[list[P]]:
    """Test 24. The home starter pitches five innings, and each of his
    innings ends on a double play: a single, an out, a double play. That is
    15 outs. A reliever pitches the 6th to the 9th. The home side scores in
    the 1st and leads for good, 1-0; the bottom of the 9th is not played."""
    halves: list[list[P]] = []
    for inning in range(1, 10):
        if inning <= 5:
            halves.append([P(HOME_SP, 0), P(HOME_SP, 1), P(HOME_SP, 2)])
        else:
            halves.append(_three(HOME_RP))
        if inning == 1:
            halves.append([P(AWAY_SP, 0, runs=1), *_three(AWAY_SP)])
        elif inning < 9:
            halves.append(_three(AWAY_SP))
    return halves


def _away_starter_four_and_two_thirds() -> list[list[P]]:
    """Test 25. The away starter gets 14 outs: four full innings and two
    outs of the 5th. A reliever gets the 5th's third out and every out after
    it (13 in all). The away side scores in the 2nd and wins 1-0."""
    halves: list[list[P]] = []
    for inning in range(1, 10):
        if inning == 2:
            halves.append([P(HOME_SP, 0, runs=1), *_three(HOME_SP)])
        else:
            halves.append(_three(HOME_SP))
        if inning <= 4:
            halves.append(_three(AWAY_SP))
        elif inning == 5:
            halves.append([P(AWAY_SP, 1), P(AWAY_SP, 1), P(AWAY_RP, 1)])
        else:
            halves.append(_three(AWAY_RP))
    return halves


def _one_pitch_reliever_game() -> list[list[P]]:
    """Test 26. A scoreless game through the top of the 7th. The home
    starter gets two outs in the 7th; a reliever comes in and his first play
    ends the half. The home side scores in the bottom of the 7th and wins
    1-0. A closer pitches the 8th and the 9th."""
    halves: list[list[P]] = []
    for inning in range(1, 10):
        if inning <= 6:
            halves.append(_three(HOME_SP))
        elif inning == 7:
            halves.append([P(HOME_SP, 1), P(HOME_SP, 1), P(HOME_RP, 1)])
        else:
            halves.append(_three(HOME_CL))
        if inning == 7:
            halves.append([P(AWAY_SP, 0, runs=1), *_three(AWAY_SP)])
        elif inning < 9:
            halves.append(_three(AWAY_SP))
    return halves


def _own_outs(stream: Sequence[PlayResult]) -> dict[int, int]:
    """Each pitcher's outs, read from the play's own pitcher (the truth)."""
    outs: dict[int, int] = {}
    for r in stream:
        assert r.pitcher_id is not None
        outs[r.pitcher_id] = outs.get(r.pitcher_id, 0) + r.outs_recorded
    return outs


class TestTheBuilderIsShapedLikeTheRealStream:
    def test_the_play_that_ends_a_half_carries_the_next_half_and_the_other_pitcher(self):
        stream = _real_stream(_double_play_starter_game())
        last_of_top_1 = stream[2]
        assert last_of_top_1.pitcher_id == HOME_SP
        assert last_of_top_1.next_state.half == Half.BOTTOM
        assert last_of_top_1.next_state.pitcher_id == AWAY_SP

    def test_a_reliever_who_starts_a_half_is_not_in_the_state_that_rolls_into_it(self):
        stream = _real_stream(_double_play_starter_game())
        # The last play of the bottom of the 5th rolls into the top of the
        # 6th; the home side's pitcher there is still the starter.
        rolls_into_6th = [
            r
            for r in stream
            if r.next_state.inning == 6
            and r.next_state.half == Half.TOP
            and r.fielding_team == Team.AWAY
        ]
        assert len(rolls_into_6th) == 1
        assert rolls_into_6th[0].next_state.pitcher_id == HOME_SP


# ---------------------------------------------------------------------------
# Tests 24 to 26 — the play's own pitcher decides
# ---------------------------------------------------------------------------


class TestThePlaysOwnPitcherDecides:
    def test_a_starter_whose_innings_end_on_double_plays_reaches_15_outs_and_keeps_the_win(
        self,
    ):
        stream = _real_stream(_double_play_starter_game())
        assert _own_outs(stream)[HOME_SP] == STARTER_WIN_MIN_OUTS
        d = decisions_from_plays(stream)
        assert (d.home_score, d.away_score) == (1, 0)
        assert d.winning_pitcher_id == HOME_SP
        assert d.losing_pitcher_id == AWAY_SP
        # The reliever finished a one-run game he did not win: the save.
        assert d.save_pitcher_id == HOME_RP

    def test_an_away_starter_pulled_after_four_and_two_thirds_has_14_outs_and_the_reliever_wins(
        self,
    ):
        stream = _real_stream(_away_starter_four_and_two_thirds())
        outs = _own_outs(stream)
        assert outs[AWAY_SP] == 14
        assert outs[AWAY_RP] == 13
        d = decisions_from_plays(stream)
        assert (d.home_score, d.away_score) == (0, 1)
        assert d.winning_pitcher_id == AWAY_RP
        assert d.losing_pitcher_id == HOME_SP
        # The winner finished the game: no save.
        assert d.save_pitcher_id is None

    def test_a_reliever_whose_first_play_ends_the_half_wins_when_his_side_then_leads_for_good(
        self,
    ):
        stream = _real_stream(_one_pitch_reliever_game())
        outs = _own_outs(stream)
        assert outs[HOME_RP] == 1
        d = decisions_from_plays(stream)
        assert (d.home_score, d.away_score) == (1, 0)
        assert d.winning_pitcher_id == HOME_RP
        assert d.losing_pitcher_id == AWAY_SP
        assert d.save_pitcher_id == HOME_CL


# ---------------------------------------------------------------------------
# Test 27 — a stream without the two fields reads as today
# ---------------------------------------------------------------------------


class TestAStreamWithoutTheTwoFieldsReadsAsToday:
    """The same three games with the play's own pitcher removed. The module
    falls back to the next state and makes today's mistakes, on purpose:
    these assertions pin the fallback, not the truth."""

    def test_the_double_play_starter_is_short_of_15_and_loses_the_win(self):
        # Today's count gives the starter 10 outs: one out a half from his
        # own innings (the double play's two go to the away side) and the
        # last out of each bottom half he rolls into.
        d = decisions_from_plays(_strip(_real_stream(_double_play_starter_game())))
        assert d.winning_pitcher_id == HOME_RP
        assert d.losing_pitcher_id == AWAY_SP
        assert d.save_pitcher_id is None

    def test_the_away_starter_of_four_and_two_thirds_is_counted_to_15_and_keeps_the_win(self):
        d = decisions_from_plays(_strip(_real_stream(_away_starter_four_and_two_thirds())))
        assert d.winning_pitcher_id == AWAY_SP
        assert d.losing_pitcher_id == HOME_SP
        assert d.save_pitcher_id == AWAY_RP

    def test_the_one_play_reliever_is_never_the_pitcher_of_record(self):
        d = decisions_from_plays(_strip(_real_stream(_one_pitch_reliever_game())))
        assert d.winning_pitcher_id == HOME_SP
        assert d.losing_pitcher_id == AWAY_SP
        assert d.save_pitcher_id == HOME_CL

    def test_a_stream_built_in_the_old_shape_keeps_its_meaning(self):
        # The win module's own suites build each play with a next state in
        # the play's own half. Without the two fields that shape reads the
        # truth, as it always did.
        stream = [
            PlayResult(
                pitch_outcome="in_play",
                outs_recorded=0,
                next_state=_gs(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=HOME_SP),
            ),
            PlayResult(
                pitch_outcome="in_play",
                outs_recorded=0,
                next_state=_gs(inning=1, half=Half.BOTTOM, home=2, away=0, pitcher_id=AWAY_SP),
            ),
        ]
        d = decisions_from_plays(stream)
        assert d.winning_pitcher_id == HOME_SP
        assert d.losing_pitcher_id == AWAY_SP


class TestALeadTakenBeforeTheWinnersPitcherFields:
    """Found while building these tests; outside the plan's decision 2.

    The module learns a side's pitcher of record from a play that side
    fields. The away side bats first. When it takes the lead for good in the
    top of the 1st, its starter has fielded no play yet. The module used to
    have no pitcher of record for it at the decisive play and gave no win.
    One of the 24 recorded games below (the busy bundle, seed 2) ended this
    way, under the old reading too. The module now puts the side's first
    pitcher (its starter) on record until the side fields a play. It does so
    only for a stream whose plays name their fielding side: a stream without
    the two fields keeps the old answer (no winning pitcher)."""

    @staticmethod
    def _away_leads_from_the_first() -> list[list[P]]:
        halves: list[list[P]] = []
        for inning in range(1, 10):
            if inning == 1:
                halves.append([P(HOME_SP, 0, runs=1), *_three(HOME_SP)])
            else:
                halves.append(_three(HOME_SP))
            halves.append(_three(AWAY_SP))
        return halves

    def test_an_away_lead_taken_for_good_in_the_top_of_the_first_wins_for_the_away_starter(
        self,
    ):
        d = decisions_from_plays(_real_stream(self._away_leads_from_the_first()))
        assert (d.home_score, d.away_score) == (0, 1)
        assert d.winning_pitcher_id == AWAY_SP

    def test_a_stream_without_the_two_fields_keeps_the_old_answer_of_no_winner(self):
        # The starter fallback applies only to a stream whose plays name
        # their fielding side. Without the two fields the stream reads as it
        # did before SIM-557: the away side has no pitcher of record at the
        # decisive play, so no pitcher gets the win.
        d = decisions_from_plays(_strip(_real_stream(self._away_leads_from_the_first())))
        assert (d.home_score, d.away_score) == (0, 1)
        assert d.winning_pitcher_id is None
        assert d.losing_pitcher_id == HOME_SP


# ---------------------------------------------------------------------------
# Test 28 — recorded synthetic games: the module's count is the box line
# ---------------------------------------------------------------------------


def _module_count(stream: Sequence[PlayResult], winner: Team) -> dict[int, int]:
    """The win module's own out count for the winning side.

    The module's starter rule reads these counts (``_side_pitcher_outs``),
    so the tests below check the module, not a copy of its rule."""
    outs, _order = _side_pitcher_outs(stream, winner)
    return outs


def _expected_winner(stream: Sequence[PlayResult], winner: Team, box_outs: dict[int, int]):
    """The winning pitcher by the module's rules, with the five-inning
    starter rule applied to the BOX line's outs (the truth)."""
    poR: dict[Team, int | None] = {Team.HOME: None, Team.AWAY: None}
    order: list[int] = []
    snaps: list[tuple[int, int, int | None]] = []
    for r in stream:
        st = r.next_state
        poR[r.fielding_team] = r.pitcher_id
        if r.fielding_team == winner and r.pitcher_id not in order:
            order.append(r.pitcher_id)
        w = st.home_score if winner == Team.HOME else st.away_score
        lo = st.away_score if winner == Team.HOME else st.home_score
        snaps.append((w, lo, poR[winner]))
    decisive = next(i for i in range(len(snaps)) if all(s[0] > s[1] for s in snaps[i:]))
    starter = order[0] if order else None
    # A side that has fielded no play by the decisive play has its first
    # pitcher (its starter) on record.
    candidate = snaps[decisive][2] if snaps[decisive][2] is not None else starter
    if (
        candidate is not None
        and candidate == starter
        and box_outs.get(starter, 0) < STARTER_WIN_MIN_OUTS
    ):
        relievers = [p for p in order if p != starter]
        if relievers:
            best = max(relievers, key=lambda p: (box_outs.get(p, 0), -order.index(p)))
            if box_outs.get(best, 0) > 0:
                return best
    return candidate


#: Scripted pull points: (home starter's outs, away starter's outs) at which
#: the starter leaves at the start of the next plate appearance. Each value
#: sits near the five-inning line, where a miscount changes the win.
PULLS = [(14, 15), (15, 14), (13, 16), (16, 13), (15, 15), (14, 14), (12, 17), (17, 12)]


def _scripted_changes(sm: Any, pulls: tuple[int, int]) -> None:
    """Pull each starter at the start of the first plate appearance after
    he reaches his scripted out count. The change runs inside the
    start-of-plate-appearance hook, where the manager's change runs."""
    inner = sm._inner if isinstance(sm, RecordingMachine) else sm
    real_hook = inner._start_of_pa_hook
    relief = {Team.HOME: HOME_RP, Team.AWAY: AWAY_RP}
    starter = {Team.HOME: HOME_SP, Team.AWAY: AWAY_SP}
    limit = {Team.HOME: pulls[0], Team.AWAY: pulls[1]}

    def hook(state: GameState) -> None:
        real_hook(state)
        side = state.defense
        if state.pitcher_id != starter[side]:
            return
        line = inner.boxscore.lines.get(starter[side]) if inner.boxscore else None
        if line is not None and line.outs_recorded >= limit[side]:
            _bring_in(state, relief[side])

    inner._start_of_pa_hook = hook


def _busy_machine(seed: int) -> StateMachine:
    art = synthetic_artifacts(
        pitch_model={**LEAGUE_PITCH_MODEL, "hit_by_pitch": 0.01},
        inplay_model=LEAGUE_INPLAY_MODEL,
        advancement=True,
        steal=(0.25, 0.6),
        steal_kw={
            "class_rates": {"foul": 0.0, "in_play": 0.0, "hit_by_pitch": 0.0},
            "pickoff_rows": 1,
            "pickoff_weight": 0.05,
        },
    )
    return StateMachine(synthetic_sampler(art, seed), rng=np.random.default_rng(seed))


def _record(machine: StateMachine, seed: int, pulls: tuple[int, int]):
    rec = RecordingMachine(machine)
    _scripted_changes(rec, pulls)
    game = simulate_game(
        rec,
        seed=seed,
        pitcher_id=HOME_SP,
        home_pitcher_id=HOME_SP,
        away_pitcher_id=AWAY_SP,
        away_lineup=AWAY_LINEUP,
        home_lineup=HOME_LINEUP,
    )
    return game, rec.recorded_plays


def _games():
    """Twenty-four recorded games: the no-DB factory's league bundle, and a
    bundle with busy runners (caught stealings and pickoff outs that do not
    end a plate appearance)."""
    out = []
    for seed in range(12):
        pulls = PULLS[seed % len(PULLS)]
        factory_machine = rng_driven_machine_factory(seed, GameSpec(sim_kwargs={}))
        out.append(("factory", seed, *_record(factory_machine, seed, pulls)))
        out.append(("busy", seed, *_record(_busy_machine(seed), seed, pulls)))
    return out


@pytest.fixture(scope="module")
def recorded_games():
    return _games()


class TestRecordedGamesCountLikeTheBox:
    def test_every_recorded_play_names_its_pitcher_and_side(self, recorded_games):
        for _kind, _seed, _game, plays in recorded_games:
            assert all(r.pitcher_id is not None for r in plays)
            assert all(r.fielding_team is not None for r in plays)

    def test_the_module_count_equals_the_box_line_for_every_winning_side_pitcher(
        self, recorded_games
    ):
        decided = 0
        for kind, seed, game, plays in recorded_games:
            winner = game.winner
            if winner is None:
                continue
            decided += 1
            counted = _module_count(plays, winner)
            side_pitchers = {HOME_SP, HOME_RP} if winner == Team.HOME else {AWAY_SP, AWAY_RP}
            for pid in side_pitchers:
                line = game.boxscore.lines.get(pid)
                box = line.outs_recorded if line is not None else 0
                assert counted.get(pid, 0) == box, (kind, seed, pid, counted, box)
        assert decided >= 20

    def test_the_old_reading_misses_the_box_in_some_games(self, recorded_games):
        """Without the play's own pitcher the count differs from the box in
        some games. This proves the recorded games exercise the defect."""
        differs = 0
        for _kind, _seed, game, plays in recorded_games:
            winner = game.winner
            if winner is None:
                continue
            old = _module_count(_strip(plays), winner)
            for pid, line in game.boxscore.lines.items():
                if pid in (HOME_SP, HOME_RP, AWAY_SP, AWAY_RP):
                    side = Team.HOME if pid in (HOME_SP, HOME_RP) else Team.AWAY
                    if side == winner and old.get(pid, 0) != line.outs_recorded:
                        differs += 1
        assert differs > 0

    def test_the_win_goes_where_the_box_line_says(self, recorded_games):
        for kind, seed, game, plays in recorded_games:
            winner = game.winner
            if winner is None:
                continue
            box_outs = {pid: ln.outs_recorded for pid, ln in game.boxscore.lines.items()}
            d = decisions_from_plays(plays)
            assert d.winning_pitcher_id == _expected_winner(plays, winner, box_outs), (
                kind,
                seed,
            )

    def test_some_recorded_games_change_pitchers_on_both_sides(self, recorded_games):
        for side_rp in (HOME_RP, AWAY_RP):
            assert any(
                game.boxscore.lines.get(side_rp) is not None
                and game.boxscore.lines[side_rp].outs_recorded > 0
                for _k, _s, game, _p in recorded_games
            )
