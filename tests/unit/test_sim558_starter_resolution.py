"""
test_sim558_starter_resolution.py
=================================
Each side's starting pitcher comes from the official box, and a game with a
missing starter is refused (SIM-558).

Why this exists.  Game 823372 (2026-06-10) resolved with no away starter.  The
away starter was a two-way player: he pitched and batted as the designated
hitter.  A player holds one row in ``raw.game_lineups``, and that row carried
his batting position ('DH'), so his side had no pitcher row.  The resolver
checked only the side that pitches first, and the loop then kept the home
starter on the mound for both halves.  A count over the 22,742 Final games of
2017-2026 found the lineup's pitcher row missing in 80 team-games and wrong in
317 more.

Coverage:
  * the pure assembly reads the box starter in each of the three real cases
    (a two-way starter, a position player who finished on the mound, an
    announced starter who was scratched);
  * a recorded pitching change still wins over the box starter;
  * a side the box does not name keeps the lineup's pitcher row;
  * ``build_game_state`` refuses a game when EITHER side has no pitcher;
  * ``resolve_lineup`` reads ``raw.game_player_stats``, fetches the hand of a
    starter the lineup does not list, and survives a failed box read;
  * ``simulate_game`` refuses a game that names one starter and not the other,
    before the first pitch;
  * with both starters named, each half's first pitch comes from the fielding
    side's pitcher (the control).
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from simulation.game_state import GameState, Half
from simulation.lineup_resolver import (
    LineupResolutionError,
    build_game_state,
    fetch_box_starters,
    resolve_game_state,
    resolve_lineup,
    resolve_lineup_from_rows,
)
from simulation.sim_loop import GameSimResult, StateMachine, simulate_game
from simulation.synthetic_bundle import league_artifacts, synthetic_sampler

GAME_PK = 823372
SEASON = 2026
HOME_TEAM = 134
AWAY_TEAM = 119

HOME_BATTERS = [101, 102, 103, 104, 105, 106, 107, 108, 109]
AWAY_BATTERS = [201, 202, 203, 204, 205, 206, 207, 208, 209]
HOME_STARTER = 901
AWAY_STARTER = 902
#: The away two-way starter: he leads off as the designated hitter.
TWO_WAY = AWAY_BATTERS[0]

#: Position names as ``raw.game_lineups`` stores them, for batting slots 1..9.
_FIELD = ["CF", "2B", "RF", "1B", "LF", "3B", "SS", "C", "DH"]


def _row(team_id, player_id, batting_order, position_code, *, sequence=1, entered_at_bat=None):
    """One ``raw.game_lineups`` row as a plain dict (asyncpg-Record-shaped)."""
    return {
        "team_id": team_id,
        "player_id": player_id,
        "batting_order": batting_order,
        "position_code": position_code,
        "is_starter": sequence == 1,
        "sequence": sequence,
        "entered_inning": None,
        "entered_at_bat": entered_at_bat,
        "pinch_role": None,
    }


def _batting_rows(team_id, batters, positions=None):
    positions = positions or _FIELD
    return [
        _row(team_id, pid, slot, pos)
        for slot, (pid, pos) in enumerate(zip(batters, positions, strict=True), start=1)
    ]


def _clean_rows():
    """Both sides: nine batters and a pitcher row with no batting slot."""
    return (
        _batting_rows(HOME_TEAM, HOME_BATTERS)
        + [_row(HOME_TEAM, HOME_STARTER, None, "P")]
        + _batting_rows(AWAY_TEAM, AWAY_BATTERS)
        + [_row(AWAY_TEAM, AWAY_STARTER, None, "P")]
    )


def _two_way_rows():
    """The shape of game 823372: the away starter bats first as the DH, and
    his side has no pitcher row."""
    away_positions = ["DH", "CF", "1B", "SS", "3B", "RF", "LF", "C", "2B"]
    return (
        _batting_rows(HOME_TEAM, HOME_BATTERS)
        + [_row(HOME_TEAM, HOME_STARTER, None, "P")]
        + _batting_rows(AWAY_TEAM, AWAY_BATTERS, away_positions)
    )


def _resolve(rows, box_starters=None, **kw):
    return resolve_lineup_from_rows(
        game_pk=GAME_PK,
        season=SEASON,
        home_team_id=HOME_TEAM,
        away_team_id=AWAY_TEAM,
        lineup_rows=rows,
        box_starters=box_starters,
        **kw,
    )


# ===========================================================================
# Pure assembly — the box names the starter
# ===========================================================================


class TestTheBoxNamesTheStarter:
    def test_a_two_way_starter_resolves_from_the_box(self):
        # Without the box his side has no pitcher: the defect.
        assert _resolve(_two_way_rows()).away.pitcher_id is None
        resolved = _resolve(_two_way_rows(), {HOME_TEAM: HOME_STARTER, AWAY_TEAM: TWO_WAY})
        assert resolved.away.pitcher_id == TWO_WAY
        assert resolved.home.pitcher_id == HOME_STARTER
        # He still leads off.
        assert resolved.away.batting_order_ids[0] == TWO_WAY

    def test_a_two_way_starter_reaches_the_game_state(self):
        resolved = _resolve(_two_way_rows(), {HOME_TEAM: HOME_STARTER, AWAY_TEAM: TWO_WAY})
        state = build_game_state(resolved)
        assert state.home_pitcher_id == HOME_STARTER
        assert state.away_pitcher_id == TWO_WAY
        assert state.pitcher_id == HOME_STARTER  # home pitches the top of the first
        assert state.away_defense["P"] == TWO_WAY

    def test_a_position_player_who_finished_on_the_mound_is_not_the_starter(self):
        # The loader stores a player's LAST position.  The home third baseman
        # pitched the ninth, so his batting row carries the pitcher code and
        # comes before the real starter's row.
        positions = list(_FIELD)
        positions[5] = "P"
        rows = (
            _batting_rows(HOME_TEAM, HOME_BATTERS, positions)
            + [_row(HOME_TEAM, HOME_STARTER, None, "P")]
            + _batting_rows(AWAY_TEAM, AWAY_BATTERS)
            + [_row(AWAY_TEAM, AWAY_STARTER, None, "P")]
        )
        # The lineup alone picks the position player: the defect.
        assert _resolve(rows).home.pitcher_id == HOME_BATTERS[5]
        resolved = _resolve(rows, {HOME_TEAM: HOME_STARTER, AWAY_TEAM: AWAY_STARTER})
        assert resolved.home.pitcher_id == HOME_STARTER
        assert build_game_state(resolved).home_defense["P"] == HOME_STARTER

    def test_a_scratched_announced_starter_gives_way_to_the_box(self):
        # The lineup names the announced starter; the box credits the start to
        # a pitcher with no lineup row.
        replacement = 955
        resolved = _resolve(_clean_rows(), {HOME_TEAM: replacement, AWAY_TEAM: AWAY_STARTER})
        assert resolved.home.pitcher_id == replacement
        assert resolved.away.pitcher_id == AWAY_STARTER

    def test_a_recorded_pitching_change_wins_over_the_box_starter(self):
        # The box names the starter, not the current pitcher.
        reliever = 977
        rows = _clean_rows() + [_row(HOME_TEAM, reliever, None, "P", sequence=2, entered_at_bat=40)]
        box = {HOME_TEAM: HOME_STARTER, AWAY_TEAM: AWAY_STARTER}
        assert _resolve(rows, box).home.pitcher_id == reliever
        # Rewound to before the change, the starter is back.
        assert _resolve(rows, box, as_of_at_bat=10).home.pitcher_id == HOME_STARTER

    def test_a_side_the_box_does_not_name_keeps_its_lineup_row(self):
        resolved = _resolve(_clean_rows(), {HOME_TEAM: 955})
        assert resolved.home.pitcher_id == 955
        assert resolved.away.pitcher_id == AWAY_STARTER

    def test_no_box_keeps_the_lineup_rows(self):
        # A game with no box yet: the lineup's pitcher rows stand.
        for box in (None, {}):
            resolved = _resolve(_clean_rows(), box)
            assert resolved.home.pitcher_id == HOME_STARTER
            assert resolved.away.pitcher_id == AWAY_STARTER

    def test_a_box_row_for_another_team_is_ignored(self):
        resolved = _resolve(_clean_rows(), {999: 955})
        assert resolved.home.pitcher_id == HOME_STARTER
        assert resolved.away.pitcher_id == AWAY_STARTER


# ===========================================================================
# build_game_state — both sides need a pitcher
# ===========================================================================


class TestBuildGameStateNeedsBothPitchers:
    def test_a_missing_away_pitcher_is_refused_in_the_top_half(self):
        # The old check read only the side that pitches first (home in the top)
        # and let this game through with ``away_pitcher_id = None``.
        resolved = _resolve(_two_way_rows())
        with pytest.raises(LineupResolutionError, match="no resolvable pitcher") as info:
            build_game_state(resolved, half=Half.TOP)
        assert f"the away team ({AWAY_TEAM})" in str(info.value)
        assert str(GAME_PK) in str(info.value)

    def test_a_missing_home_pitcher_is_refused_in_the_bottom_half(self):
        rows = (
            _batting_rows(HOME_TEAM, HOME_BATTERS)
            + _batting_rows(AWAY_TEAM, AWAY_BATTERS)
            + [_row(AWAY_TEAM, AWAY_STARTER, None, "P")]
        )
        with pytest.raises(LineupResolutionError, match="no resolvable pitcher") as info:
            build_game_state(_resolve(rows), half=Half.BOTTOM)
        assert f"the home team ({HOME_TEAM})" in str(info.value)

    def test_both_pitchers_build_a_state(self):
        state = build_game_state(_resolve(_clean_rows()))
        assert (state.home_pitcher_id, state.away_pitcher_id) == (HOME_STARTER, AWAY_STARTER)


# ===========================================================================
# DB access — the box read
# ===========================================================================


class _Conn:
    """Fake asyncpg connection: rows chosen by the table the SQL reads."""

    def __init__(self, *, lineup_rows, box_rows=None, hand_rows=None, box_error=None):
        self._lineup_rows = lineup_rows
        self._box_rows = box_rows or []
        self._hand_rows = hand_rows or []
        self._box_error = box_error
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        return {
            "game_pk": GAME_PK,
            "season": SEASON,
            "home_team_id": HOME_TEAM,
            "away_team_id": AWAY_TEAM,
        }

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        if "raw.game_player_stats" in sql:
            if self._box_error is not None:
                raise self._box_error
            return list(self._box_rows)
        if "raw.game_lineups" in sql:
            return list(self._lineup_rows)
        if "raw.players" in sql:
            return list(self._hand_rows)
        return []


def _box(home, away):
    return [
        {"team_id": HOME_TEAM, "player_id": home},
        {"team_id": AWAY_TEAM, "player_id": away},
    ]


class TestResolveLineupReadsTheBox:
    @pytest.mark.asyncio
    async def test_the_two_way_starter_resolves_end_to_end(self):
        conn = _Conn(lineup_rows=_two_way_rows(), box_rows=_box(HOME_STARTER, TWO_WAY))
        state = await resolve_game_state(conn, GAME_PK, seed=0)
        assert isinstance(state, GameState)
        assert state.home_pitcher_id == HOME_STARTER
        assert state.away_pitcher_id == TWO_WAY
        # The box read is filtered to this game's started rows.
        box_calls = [(s, a) for s, a in conn.fetch_calls if "raw.game_player_stats" in s]
        assert len(box_calls) == 1
        assert "p_started" in box_calls[0][0]
        assert box_calls[0][1] == (GAME_PK,)

    @pytest.mark.asyncio
    async def test_a_starter_the_lineup_does_not_list_gets_his_throwing_hand(self):
        replacement = 955
        conn = _Conn(
            lineup_rows=_clean_rows(),
            box_rows=_box(replacement, AWAY_STARTER),
            hand_rows=[{"player_id": replacement, "bats": "L", "throws": "L"}],
        )
        resolved = await resolve_lineup(conn, GAME_PK)
        assert resolved.home.pitcher_id == replacement
        assert resolved.throw_hands[replacement] == "L"
        hand_ids = next(a[0] for s, a in conn.fetch_calls if "raw.players" in s)
        assert replacement in hand_ids

    @pytest.mark.asyncio
    async def test_a_failed_box_read_keeps_the_lineup_rows_and_warns(self, caplog):
        conn = _Conn(lineup_rows=_clean_rows(), box_error=RuntimeError("no such table"))
        with caplog.at_level(logging.WARNING, logger="simulation.lineup_resolver"):
            resolved = await resolve_lineup(conn, GAME_PK)
        assert resolved.home.pitcher_id == HOME_STARTER
        assert resolved.away.pitcher_id == AWAY_STARTER
        assert any("box starters" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_failed_box_read_still_refuses_a_missing_starter(self):
        conn = _Conn(lineup_rows=_two_way_rows(), box_error=RuntimeError("no such table"))
        with pytest.raises(LineupResolutionError, match="no resolvable pitcher"):
            await resolve_game_state(conn, GAME_PK)

    @pytest.mark.asyncio
    async def test_two_started_rows_for_one_side_are_left_out(self, caplog):
        rows = _box(HOME_STARTER, AWAY_STARTER) + [{"team_id": HOME_TEAM, "player_id": 955}]
        conn = _Conn(lineup_rows=_clean_rows(), box_rows=rows)
        with caplog.at_level(logging.WARNING, logger="simulation.lineup_resolver"):
            starters = await fetch_box_starters(conn, GAME_PK)
        assert starters == {AWAY_TEAM: AWAY_STARTER}
        assert any("credits 2 starters" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_game_with_no_box_returns_an_empty_map(self):
        conn = _Conn(lineup_rows=_clean_rows())
        assert await fetch_box_starters(conn, GAME_PK) == {}


# ===========================================================================
# simulate_game — a game that names one starter names both
# ===========================================================================


def _machine(seed: int = 0) -> StateMachine:
    """The production machine over the in-memory league bundle (SIM-486)."""
    return StateMachine(
        synthetic_sampler(league_artifacts(), seed), rng=np.random.default_rng(seed)
    )


class _RecordingMachine(StateMachine):
    """Records the pitcher on the mound at each half's first pitch."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pitches = 0
        self.first_pitcher: dict[tuple[int, str], int | None] = {}

    def step_pitch(self, st, **kw):
        self.pitches += 1
        self.first_pitcher.setdefault((int(st.inning), st.half.name), st.pitcher_id)
        return super().step_pitch(st, **kw)


def _recording_machine(seed: int = 0) -> _RecordingMachine:
    return _RecordingMachine(
        synthetic_sampler(league_artifacts(), seed), rng=np.random.default_rng(seed)
    )


class TestSimulateGameRefusesAOneSidedPitcherMap:
    def test_a_missing_away_starter_raises_before_the_first_pitch(self):
        # The kwargs game 823372 produced: the home starter on both keys the
        # loop opens with, and no away starter.
        sm = _recording_machine(0)
        with pytest.raises(ValueError, match="starting pitcher on each side") as info:
            simulate_game(
                sm,
                seed=0,
                season=SEASON,
                pitcher_id=HOME_STARTER,
                home_pitcher_id=HOME_STARTER,
                away_pitcher_id=None,
                away_lineup=AWAY_BATTERS,
                home_lineup=HOME_BATTERS,
            )
        assert "the away side has none" in str(info.value)
        assert sm.pitches == 0

    def test_a_missing_home_starter_raises(self):
        with pytest.raises(ValueError, match="starting pitcher on each side") as info:
            simulate_game(
                _machine(1),
                seed=1,
                season=SEASON,
                pitcher_id=AWAY_STARTER,
                away_pitcher_id=AWAY_STARTER,
                away_lineup=AWAY_BATTERS,
                home_lineup=HOME_BATTERS,
            )
        assert "the home side has none" in str(info.value)

    def test_an_initial_state_with_one_starter_gets_the_same_rule(self):
        state = GameState(
            pitcher_id=HOME_STARTER,
            bat_hand="R",
            season=SEASON,
            away_lineup=list(AWAY_BATTERS),
            home_lineup=list(HOME_BATTERS),
        )
        state.batter_id = AWAY_BATTERS[0]
        state.home_pitcher_id = HOME_STARTER
        with pytest.raises(ValueError, match="starting pitcher on each side"):
            simulate_game(_machine(2), initial_state=state, seed=2)

    def test_a_game_with_no_pitcher_map_still_plays(self):
        # The fixed-matchup seam of the no-DB tests: one pitcher id, no map.
        r = simulate_game(
            _machine(3),
            seed=3,
            season=SEASON,
            pitcher_id=HOME_STARTER,
            away_lineup=AWAY_BATTERS,
            home_lineup=HOME_BATTERS,
        )
        assert isinstance(r, GameSimResult)
        assert r.innings_played >= 9


class TestEachHalfOpensWithTheFieldingSidesPitcher:
    def test_both_starters_named_each_side_pitches_its_own_half(self):
        # No manager is attached, so no pitching change is drawn: each starter
        # pitches every one of his side's halves.
        sm = _recording_machine(4)
        r = simulate_game(
            sm,
            seed=4,
            season=SEASON,
            pitcher_id=HOME_STARTER,
            home_pitcher_id=HOME_STARTER,
            away_pitcher_id=AWAY_STARTER,
            away_lineup=AWAY_BATTERS,
            home_lineup=HOME_BATTERS,
        )
        assert r.innings_played >= 9
        tops = {p for (_, half), p in sm.first_pitcher.items() if half == "TOP"}
        bottoms = {p for (_, half), p in sm.first_pitcher.items() if half == "BOTTOM"}
        assert tops == {HOME_STARTER}  # home fields the top half
        assert bottoms == {AWAY_STARTER}  # away fields the bottom half
