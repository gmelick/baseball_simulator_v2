"""
test_lineup_resolver.py
=======================
Unit tests for ``simulation/lineup_resolver.py`` (SIM-353) — the runtime
lineup / substitution resolver that closes the SIM-338 gap (nothing read the
lineup stores to BUILD a ``GameState``).

These tests use the established tests/unit idiom: a fake asyncpg connection
(``_StubConn``, mirroring ``_StubPool`` in test_api_state.py) returning canned
dict rows (the same shape asyncpg ``Record`` exposes), plus direct in-memory
assembly so the pure GameState-building path is exercised with NO live DB.

What is asserted
----------------
  * A starting lineup maps to the right ordered ``home_lineup`` / ``away_lineup``
    and the slot pointers / leadoff ``batter_id`` in the built GameState.
  * A substitution (higher ``sequence`` row) correctly replaces the player at a
    slot; an ``as_of_at_bat`` rewind reverts to the pre-sub occupant.
  * A mid-game pitching change wins on ``pitcher_id``.
  * Edge cases: a missing game / empty lineup raises a clear
    ``LineupResolutionError``; an AL pitcher (no batting_order) is excluded from
    the batting order but still resolves as the pitcher.
  * The async orchestrator wires DB access -> pure assembly correctly and issues
    the expected queries.

Owned by Data Engineer (SIM-353).

Run:
    pytest tests/unit/test_lineup_resolver.py -v
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# Ensure repo root is importable when the file is run directly.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from simulation.game_state import GameState, Half, Team  # noqa: E402
from simulation.lineup_resolver import (  # noqa: E402
    LineupNotIngestedError,
    LineupResolutionError,
    ResolvedLineup,
    TeamLineup,
    build_game_state,
    fetch_player_hands,
    resolve_game_state,
    resolve_lineup,
    resolve_lineup_from_rows,
)

# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

GAME_PK = 745000
SEASON = 2024
HOME_TEAM = 147
AWAY_TEAM = 111

# Ascending player-id conventions: 1xx home batters, 2xx away batters,
# 9xx pitchers.  Keeps the assertions readable.
HOME_BATTERS = [101, 102, 103, 104, 105, 106, 107, 108, 109]
AWAY_BATTERS = [201, 202, 203, 204, 205, 206, 207, 208, 209]
HOME_PITCHER = 901
AWAY_PITCHER = 902


def _lineup_row(
    team_id: int,
    player_id: int,
    batting_order,
    position_code: str,
    *,
    is_starter: bool = True,
    sequence: int = 1,
    entered_inning=None,
    entered_at_bat=None,
    pinch_role=None,
) -> dict:
    """One ``raw.game_lineups`` row as a plain dict (asyncpg-Record-shaped)."""
    return {
        "team_id": team_id,
        "player_id": player_id,
        "batting_order": batting_order,
        "position_code": position_code,
        "is_starter": is_starter,
        "sequence": sequence,
        "entered_inning": entered_inning,
        "entered_at_bat": entered_at_bat,
        "pinch_role": pinch_role,
    }


def _starting_rows() -> list[dict]:
    """A complete two-team starting lineup (9 batters/side + a pitcher/side).

    The home pitcher bats in slot 9 (NL-style) so the batting order is full;
    the away pitcher is given batting slot 9 too for symmetry.
    """
    rows: list[dict] = []
    positions = ["8", "4", "9", "3", "7", "5", "6", "2", "1"]  # CF,2B,RF,1B,LF,3B,SS,C,P
    for slot, (pid, pos) in enumerate(zip(HOME_BATTERS, positions, strict=False), start=1):
        rows.append(_lineup_row(HOME_TEAM, pid, slot, pos))
    for slot, (pid, pos) in enumerate(zip(AWAY_BATTERS, positions, strict=False), start=1):
        rows.append(_lineup_row(AWAY_TEAM, pid, slot, pos))
    return rows


class _StubConn:
    """Fake asyncpg connection. Records SQL + args; returns scripted rows.

    Mirrors the ``_StubPool`` idiom in test_api_state.py. ``fetch`` returns rows
    chosen by which table the SQL touches (game_lineups / players); ``fetchrow``
    returns the single raw.games row.
    """

    def __init__(
        self,
        *,
        game_row: dict | None,
        lineup_rows: list[dict],
        hand_rows: list[dict] | None = None,
    ) -> None:
        self._game_row = game_row
        self._lineup_rows = lineup_rows
        self._hand_rows = hand_rows or []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self._game_row

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        if "raw.game_lineups" in sql:
            return list(self._lineup_rows)
        if "raw.players" in sql:
            return list(self._hand_rows)
        return []


def _game_row() -> dict:
    return {
        "game_pk": GAME_PK,
        "season": SEASON,
        "home_team_id": HOME_TEAM,
        "away_team_id": AWAY_TEAM,
    }


# ===========================================================================
# Pure assembly — resolve_lineup_from_rows
# ===========================================================================


class TestResolveLineupFromRows:
    def test_starting_lineup_orders_both_sides(self):
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=_starting_rows(),
        )
        assert resolved.home.batting_order_ids == HOME_BATTERS
        assert resolved.away.batting_order_ids == AWAY_BATTERS
        # The slot-9 pitcher (position '1') resolves as the pitcher.
        assert resolved.home.pitcher_id == HOME_BATTERS[8]
        assert resolved.away.pitcher_id == AWAY_BATTERS[8]

    def test_rows_for_other_teams_are_ignored(self):
        rows = _starting_rows()
        rows.append(_lineup_row(999, 555, 1, "8"))  # bogus third team
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
        )
        assert 555 not in resolved.home.batting_order_ids
        assert 555 not in resolved.away.batting_order_ids

    def test_substitution_replaces_slot_occupant(self):
        rows = _starting_rows()
        # A pinch hitter (sequence 2) takes over the home leadoff slot at AB 30.
        rows.append(
            _lineup_row(
                HOME_TEAM,
                150,
                1,
                "8",
                is_starter=False,
                sequence=2,
                entered_inning=7,
                entered_at_bat=30,
                pinch_role="PH",
            )
        )
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
        )
        # Latest occupant of slot 1 is the sub, not the starter.
        assert resolved.home.batting_order_ids[0] == 150
        assert resolved.home.batting_order_ids[1:] == HOME_BATTERS[1:]
        slot1 = resolved.home.slots[0]
        assert slot1.is_substitution is True
        assert slot1.pinch_role == "PH"

    def test_as_of_at_bat_rewinds_before_substitution(self):
        rows = _starting_rows()
        rows.append(
            _lineup_row(
                HOME_TEAM,
                150,
                1,
                "8",
                is_starter=False,
                sequence=2,
                entered_inning=7,
                entered_at_bat=30,
                pinch_role="PH",
            )
        )
        # As of AB 10 (before the sub at AB 30) the starter is still up.
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
            as_of_at_bat=10,
        )
        assert resolved.home.batting_order_ids[0] == HOME_BATTERS[0]
        assert resolved.home.slots[0].is_substitution is False

    def test_highest_sequence_wins_with_double_switch(self):
        rows = _starting_rows()
        # Slot 1: starter (seq1) -> PH (seq2 @ AB30) -> a later sub (seq3 @ AB55).
        rows.append(
            _lineup_row(
                HOME_TEAM,
                150,
                1,
                "8",
                is_starter=False,
                sequence=2,
                entered_at_bat=30,
                pinch_role="PH",
            )
        )
        rows.append(
            _lineup_row(
                HOME_TEAM,
                160,
                1,
                "8",
                is_starter=False,
                sequence=3,
                entered_at_bat=55,
                pinch_role="DEF",
            )
        )
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
        )
        assert resolved.home.batting_order_ids[0] == 160  # highest sequence

    def test_pitching_change_resolves_latest_pitcher(self):
        rows = _starting_rows()
        # A reliever (position '1', no batting_order) enters at AB 40, seq 2.
        rows.append(
            _lineup_row(
                HOME_TEAM,
                950,
                None,
                "1",
                is_starter=False,
                sequence=2,
                entered_inning=8,
                entered_at_bat=40,
            )
        )
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
        )
        # The relief pitcher wins on highest sequence; he is NOT in the batting
        # order (no batting_order).
        assert resolved.home.pitcher_id == 950
        assert 950 not in resolved.home.batting_order_ids

    def test_al_pitcher_without_batting_slot_excluded_from_order(self):
        # DH lineup: 9 position-player batters + a separate pitcher row with no
        # batting_order (the AL pitcher never bats).
        rows: list[dict] = []
        positions = ["8", "4", "9", "3", "7", "5", "6", "2", "10"]  # 10 = DH
        for slot, (pid, pos) in enumerate(zip(HOME_BATTERS, positions, strict=False), start=1):
            rows.append(_lineup_row(HOME_TEAM, pid, slot, pos))
        rows.append(_lineup_row(HOME_TEAM, HOME_PITCHER, None, "1"))
        # away side needs at least one batter so the resolve doesn't error.
        rows.append(_lineup_row(AWAY_TEAM, AWAY_BATTERS[0], 1, "8"))
        rows.append(_lineup_row(AWAY_TEAM, AWAY_PITCHER, None, "1"))

        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
        )
        assert resolved.home.batting_order_ids == HOME_BATTERS  # 9 batters, no pitcher
        assert resolved.home.pitcher_id == HOME_PITCHER
        assert HOME_PITCHER not in resolved.home.batting_order_ids

    def test_empty_lineup_raises(self):
        with pytest.raises(LineupResolutionError, match="no batting-order rows"):
            resolve_lineup_from_rows(
                game_pk=GAME_PK,
                season=SEASON,
                home_team_id=HOME_TEAM,
                away_team_id=AWAY_TEAM,
                lineup_rows=[],
            )


# ===========================================================================
# Pure GameState building — build_game_state
# ===========================================================================


class TestBuildGameState:
    def _resolved(self, **hand_overrides) -> ResolvedLineup:
        return resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=_starting_rows(),
            bat_hands=hand_overrides.get("bat_hands"),
            throw_hands=hand_overrides.get("throw_hands"),
        )

    def test_top_of_first_populates_lineups_and_pointers(self):
        state = build_game_state(self._resolved(), half=Half.TOP)
        assert isinstance(state, GameState)
        assert state.home_lineup == HOME_BATTERS
        assert state.away_lineup == AWAY_BATTERS
        assert state.home_lineup_slot == 0
        assert state.away_lineup_slot == 0
        assert state.season == SEASON
        # Top of the 1st: away bats, home pitches.
        assert state.offense == Team.AWAY
        assert state.batter_id == AWAY_BATTERS[0]  # away leadoff
        assert state.pitcher_id == HOME_BATTERS[8]  # home slot-9 pitcher

    def test_bottom_of_first_flips_offense_and_pitcher(self):
        state = build_game_state(self._resolved(), half=Half.BOTTOM)
        assert state.offense == Team.HOME
        assert state.batter_id == HOME_BATTERS[0]  # home leadoff
        assert state.pitcher_id == AWAY_BATTERS[8]  # away pitcher defends

    def test_hand_maps_drive_sampler_prefilter(self):
        bat_hands = {AWAY_BATTERS[0]: "L"}
        throw_hands = {HOME_BATTERS[8]: "R"}
        state = build_game_state(
            self._resolved(bat_hands=bat_hands, throw_hands=throw_hands),
            half=Half.TOP,
        )
        assert state.bat_hand == "L"
        assert state.throw_hand == "R"
        # sampler pre-filter tuple is (pitcher_id, bat_hand, season).
        assert state.sampler_prefilter() == (HOME_BATTERS[8], "L", SEASON)

    def test_unknown_bat_hand_defaults_to_R(self):
        state = build_game_state(self._resolved(), half=Half.TOP)
        assert state.bat_hand == "R"  # DEFAULT_BAT_HAND

    def test_seed_is_threaded_through(self):
        state = build_game_state(self._resolved(), half=Half.TOP, seed=42)
        assert state.seed == 42

    def test_substitution_shows_in_built_lineup(self):
        rows = _starting_rows()
        rows.append(
            _lineup_row(
                AWAY_TEAM,
                250,
                1,
                "8",
                is_starter=False,
                sequence=2,
                entered_at_bat=20,
                pinch_role="PH",
            )
        )
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
        )
        state = build_game_state(resolved, half=Half.TOP)
        # away leadoff slot now holds the sub.
        assert state.away_lineup[0] == 250
        assert state.batter_id == 250

    def test_missing_pitcher_raises(self):
        # A lineup with batters but no pitcher row on the defending side.
        rows = [_lineup_row(AWAY_TEAM, AWAY_BATTERS[0], 1, "8")]  # away only, no pitcher
        rows += [
            _lineup_row(HOME_TEAM, pid, slot, "8") for slot, pid in enumerate(HOME_BATTERS, start=1)
        ]  # home, no '1' pos
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
        )
        # Top of 1st -> home pitches; home has no pitcher -> error.
        with pytest.raises(LineupResolutionError, match="no resolvable pitcher"):
            build_game_state(resolved, half=Half.TOP)

    def test_empty_offense_raises(self):
        # Home has a full lineup + pitcher; away has only a pitcher (no batters).
        rows = _starting_rows_home_only()
        rows.append(_lineup_row(AWAY_TEAM, AWAY_PITCHER, None, "1"))
        resolved = resolve_lineup_from_rows(
            game_pk=GAME_PK,
            season=SEASON,
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
            lineup_rows=rows,
        )
        # Top of 1st -> away bats; away has no batting order -> error.
        with pytest.raises(LineupResolutionError, match="empty batting order"):
            build_game_state(resolved, half=Half.TOP)

    def test_game_state_passes_its_own_invariants(self):
        # The built GameState is a valid fresh state per GameState's own guards.
        state = build_game_state(self._resolved(), half=Half.TOP)
        state.assert_invariants()  # must not raise


def _starting_rows_home_only() -> list[dict]:
    positions = ["8", "4", "9", "3", "7", "5", "6", "2", "1"]
    return [
        _lineup_row(HOME_TEAM, pid, slot, pos)
        for slot, (pid, pos) in enumerate(zip(HOME_BATTERS, positions, strict=False), start=1)
    ]


# ===========================================================================
# DB-access layer + async orchestrator (fake asyncpg conn)
# ===========================================================================


class TestAsyncOrchestrator:
    @pytest.mark.asyncio
    async def test_resolve_lineup_wires_db_to_assembly(self):
        conn = _StubConn(
            game_row=_game_row(),
            lineup_rows=_starting_rows(),
            hand_rows=[
                {"player_id": AWAY_BATTERS[0], "bats": "L", "throws": "L"},
                {"player_id": HOME_BATTERS[8], "bats": "R", "throws": "R"},
            ],
        )
        resolved = await resolve_lineup(conn, GAME_PK)
        assert resolved.season == SEASON
        assert resolved.home.batting_order_ids == HOME_BATTERS
        assert resolved.away.batting_order_ids == AWAY_BATTERS
        assert resolved.bat_hands[AWAY_BATTERS[0]] == "L"
        assert resolved.throw_hands[HOME_BATTERS[8]] == "R"
        # One games lookup + at least the lineup + hands fetches happened.
        assert len(conn.fetchrow_calls) == 1
        assert any("raw.game_lineups" in sql for sql, _ in conn.fetch_calls)
        assert any("raw.players" in sql for sql, _ in conn.fetch_calls)

    @pytest.mark.asyncio
    async def test_resolve_game_state_end_to_end(self):
        conn = _StubConn(
            game_row=_game_row(),
            lineup_rows=_starting_rows(),
            hand_rows=[{"player_id": AWAY_BATTERS[0], "bats": "S", "throws": "R"}],
        )
        state = await resolve_game_state(conn, GAME_PK, half=Half.TOP, seed=7)
        assert isinstance(state, GameState)
        assert state.away_lineup == AWAY_BATTERS
        assert state.batter_id == AWAY_BATTERS[0]
        assert state.bat_hand == "S"  # switch hitter from the hand map
        assert state.seed == 7

    @pytest.mark.asyncio
    async def test_unknown_game_raises(self):
        conn = _StubConn(game_row=None, lineup_rows=[])
        with pytest.raises(LineupResolutionError, match="not found in raw.games"):
            await resolve_lineup(conn, GAME_PK)

    @pytest.mark.asyncio
    async def test_no_lineup_rows_raises_not_ingested(self):
        # SIM-409: empty lineup_rows → LineupNotIngestedError (subclass of
        # LineupResolutionError), not the plain base error.
        conn = _StubConn(game_row=_game_row(), lineup_rows=[])
        with pytest.raises(LineupNotIngestedError, match="not yet published"):
            await resolve_lineup(conn, GAME_PK)

    @pytest.mark.asyncio
    async def test_lineup_not_ingested_is_resolution_error(self):
        # LineupNotIngestedError must remain catchable as LineupResolutionError
        # so existing broad except clauses are not broken.
        conn = _StubConn(game_row=_game_row(), lineup_rows=[])
        with pytest.raises(LineupResolutionError):
            await resolve_lineup(conn, GAME_PK)

    @pytest.mark.asyncio
    async def test_fetch_player_hands_empty_input_no_query(self):
        conn = _StubConn(game_row=_game_row(), lineup_rows=[])
        bats, throws = await fetch_player_hands(conn, [])
        assert bats == {} and throws == {}
        # No round trip for empty input.
        assert conn.fetch_calls == []

    @pytest.mark.asyncio
    async def test_fetch_player_hands_dedupes_ids(self):
        conn = _StubConn(
            game_row=_game_row(),
            lineup_rows=[],
            hand_rows=[{"player_id": 101, "bats": "L", "throws": "R"}],
        )
        bats, throws = await fetch_player_hands(conn, [101, 101, 101])
        assert bats == {101: "L"}
        assert throws == {101: "R"}
        # Single round trip; the deduped/sorted id list is passed as $1.
        assert len(conn.fetch_calls) == 1
        assert conn.fetch_calls[0][1] == ([101],)


# ===========================================================================
# Direct ResolvedLineup / TeamLineup construction (DB-free building path)
# ===========================================================================


def test_build_from_directly_constructed_resolved_lineup():
    """A unit test can build a GameState from a hand-built ResolvedLineup with
    no rows / no DB at all — proves the GameState-building layer is decoupled."""
    home = TeamLineup(team_id=HOME_TEAM, slots=(), pitcher_id=HOME_PITCHER)
    away = TeamLineup(
        team_id=AWAY_TEAM,
        slots=(),
        pitcher_id=AWAY_PITCHER,
    )
    # Give away a batting order by re-creating slots via the rows path is simpler,
    # but to prove pure construction we build TeamLineup with explicit slots:
    from simulation.lineup_resolver import LineupSlot

    away_slots = tuple(
        LineupSlot(
            batting_order=i + 1, player_id=pid, position_code="8", is_starter=True, sequence=1
        )
        for i, pid in enumerate(AWAY_BATTERS)
    )
    away = TeamLineup(team_id=AWAY_TEAM, slots=away_slots, pitcher_id=AWAY_PITCHER)
    resolved = ResolvedLineup(game_pk=GAME_PK, season=SEASON, home=home, away=away)
    state = build_game_state(resolved, half=Half.TOP)
    assert state.away_lineup == AWAY_BATTERS
    assert state.pitcher_id == HOME_PITCHER  # home defends in the top
    assert state.batter_id == AWAY_BATTERS[0]
