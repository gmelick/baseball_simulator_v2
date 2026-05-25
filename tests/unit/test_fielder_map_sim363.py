"""
test_fielder_map_sim363.py
==========================
Unit tests for the SIM-363 per-position fielder map: the builder in
``simulation/lineup_resolver.py`` that turns a resolved lineup into the
``{defensive-slot name: player_id}`` map ``FieldSnapshot.from_game_state``
consumes (Phase 5, Sprint 4).

Background
----------
``simulation/snapshots.py`` ``FieldSnapshot.from_game_state(..., defense_positions=...)``
already accepts an optional ``{position_name: player_id}`` map and leaves the 9
defensive slots empty when it is omitted ("the loop does not yet track
per-position fielders").  SIM-363 supplies the builder that produces that map for
the fielding side, keyed by the canonical DEFENSE_POSITIONS slot names derived
from each player's MLB ``position_code`` (1=P .. 9=RF).

What is asserted
----------------
  * A full 9-position fielding lineup maps to the correct {position: player_id}.
  * The position-code -> slot-name mapping is the canonical 1..9 -> P..RF.
  * The map feeds FieldSnapshot.from_game_state(defense_positions=...) and
    populates ALL 9 slots (round-trip, imported read-only).
  * The pitcher slot ('P') is populated (from TeamLineup.pitcher_id).
  * A substitution at a position resolves to the *current* occupant.
  * A DH / pinch role (non-fielding code) is skipped.
  * The correct fielding side is chosen for TOP vs BOTTOM (HOME fields in the
    top, AWAY fields in the bottom).

Owned by Backend Developer (SIM-363).

Run:
    pytest tests/unit/test_fielder_map_sim363.py -v
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
    DEFENSE_POSITION_NAMES,
    POSITION_CODE_TO_NAME,
    LineupResolutionError,
    build_defense_map,
    build_defense_map_for_state,
    build_team_defense_map,
    fielding_side_for_half,
    resolve_lineup_from_rows,
)

# Read-only: assert the map round-trips through the real contract.
from simulation.snapshots import (  # noqa: E402
    DEFENSE_POSITIONS,
    FieldSnapshot,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers (mirror tests/unit/test_lineup_resolver.py idioms)
# ---------------------------------------------------------------------------

GAME_PK = 745363
SEASON = 2024
HOME_TEAM = 147
AWAY_TEAM = 111

# 1xx home batters, 2xx away batters.  Index aligns to a position layout below.
HOME_BATTERS = [101, 102, 103, 104, 105, 106, 107, 108, 109]
AWAY_BATTERS = [201, 202, 203, 204, 205, 206, 207, 208, 209]

# Batting-order layout -> MLB position code per slot.  A full nine-in-the-field
# lineup (NL style): the slot-9 hitter is the pitcher (code '1').
#   slot: 1   2   3   4   5   6   7   8   9
#   pos : CF  2B  RF  1B  LF  3B  SS  C   P
POSITIONS = ["8", "4", "9", "3", "7", "5", "6", "2", "1"]

# The expected {slot name: player_id} for the HOME side given the layout above.
# Pitcher ('P') is HOME_BATTERS[8] (the slot-9 occupant, code '1').
EXPECTED_HOME_MAP = {
    POSITION_CODE_TO_NAME[code]: pid for pid, code in zip(HOME_BATTERS, POSITIONS, strict=False)
}
EXPECTED_AWAY_MAP = {
    POSITION_CODE_TO_NAME[code]: pid for pid, code in zip(AWAY_BATTERS, POSITIONS, strict=False)
}


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
    """A complete two-team NL-style starting lineup (9 in the field per side)."""
    rows: list[dict] = []
    for slot, (pid, pos) in enumerate(zip(HOME_BATTERS, POSITIONS, strict=False), start=1):
        rows.append(_lineup_row(HOME_TEAM, pid, slot, pos))
    for slot, (pid, pos) in enumerate(zip(AWAY_BATTERS, POSITIONS, strict=False), start=1):
        rows.append(_lineup_row(AWAY_TEAM, pid, slot, pos))
    return rows


def _resolved(rows: list[dict] | None = None, *, as_of_at_bat=None):
    return resolve_lineup_from_rows(
        game_pk=GAME_PK,
        season=SEASON,
        home_team_id=HOME_TEAM,
        away_team_id=AWAY_TEAM,
        lineup_rows=rows if rows is not None else _starting_rows(),
        as_of_at_bat=as_of_at_bat,
    )


# ===========================================================================
# The position-code -> slot-name mapping is the canonical 1..9 -> P..RF.
# ===========================================================================


class TestPositionCodeMapping:
    def test_code_map_is_canonical_1_through_9(self):
        assert POSITION_CODE_TO_NAME == {
            "1": "P",
            "2": "C",
            "3": "1B",
            "4": "2B",
            "5": "3B",
            "6": "SS",
            "7": "LF",
            "8": "CF",
            "9": "RF",
        }

    def test_slot_names_match_snapshots_defense_positions(self):
        # The resolver's slot-name order must equal the contract's, so the map
        # keys line up 1:1 with what FieldSnapshot renders.
        assert DEFENSE_POSITION_NAMES == DEFENSE_POSITIONS


# ===========================================================================
# build_team_defense_map / build_defense_map — full lineup
# ===========================================================================


class TestBuildDefenseMapFullLineup:
    def test_full_nine_maps_every_position(self):
        resolved = _resolved()
        dmap = build_team_defense_map(resolved.home)
        # All nine canonical slots present and correct.
        assert dmap == EXPECTED_HOME_MAP
        assert set(dmap) == set(DEFENSE_POSITIONS)

    def test_pitcher_slot_is_populated(self):
        resolved = _resolved()
        dmap = build_team_defense_map(resolved.home)
        assert "P" in dmap
        # Pitcher comes from TeamLineup.pitcher_id (slot-9 code '1' occupant).
        assert dmap["P"] == resolved.home.pitcher_id
        assert dmap["P"] == HOME_BATTERS[8]

    def test_build_defense_map_by_side_string(self):
        resolved = _resolved()
        assert build_defense_map(resolved, fielding_side="home") == EXPECTED_HOME_MAP
        assert build_defense_map(resolved, fielding_side="away") == EXPECTED_AWAY_MAP

    def test_build_defense_map_accepts_team_enum(self):
        resolved = _resolved()
        assert build_defense_map(resolved, fielding_side=Team.HOME) == EXPECTED_HOME_MAP
        assert build_defense_map(resolved, fielding_side=Team.AWAY) == EXPECTED_AWAY_MAP

    def test_unknown_side_raises(self):
        resolved = _resolved()
        with pytest.raises(LineupResolutionError, match="not a recognizable side"):
            build_defense_map(resolved, fielding_side="dugout")


# ===========================================================================
# Round-trip: the map feeds FieldSnapshot and populates all 9 slots.
# ===========================================================================


class TestFieldSnapshotRoundTrip:
    def _state_top_of_first(self) -> GameState:
        # A minimal live state: top of the 1st, home fielding.  The defense map
        # is supplied separately (the loop does not track fielders).
        return GameState(pitcher_id=HOME_BATTERS[8], bat_hand="R", season=SEASON, half=Half.TOP)

    def test_map_populates_all_nine_field_snapshot_slots(self):
        resolved = _resolved()
        state = self._state_top_of_first()
        dmap = build_defense_map_for_state(resolved, state)  # home fields in top
        snap = FieldSnapshot.from_game_state(state, defense_positions=dmap)
        # Every defensive slot is filled (no None) and carries the right id.
        for pos in DEFENSE_POSITIONS:
            ref = snap.positions[pos]
            assert ref is not None, f"slot {pos} left empty"
            assert ref.player_id == EXPECTED_HOME_MAP[pos]

    def test_without_map_slots_stay_empty(self):
        # Control: the documented "omit -> 9 empty slots" behaviour still holds,
        # so the map is what fills them.
        state = self._state_top_of_first()
        snap = FieldSnapshot.from_game_state(state)
        assert all(snap.positions[pos] is None for pos in DEFENSE_POSITIONS)

    def test_pitcher_slot_round_trips_to_snapshot(self):
        resolved = _resolved()
        state = self._state_top_of_first()
        dmap = build_defense_map_for_state(resolved, state)
        snap = FieldSnapshot.from_game_state(state, defense_positions=dmap)
        assert snap.positions["P"] is not None
        assert snap.positions["P"].player_id == HOME_BATTERS[8]


# ===========================================================================
# Substitutions resolve to the current occupant.
# ===========================================================================


class TestSubstitutions:
    def test_substitution_at_a_position_uses_current_occupant(self):
        rows = _starting_rows()
        # A defensive sub takes over the home CF slot (slot 1, code '8') at AB 40.
        rows.append(
            _lineup_row(
                HOME_TEAM,
                150,
                1,
                "8",
                is_starter=False,
                sequence=2,
                entered_inning=7,
                entered_at_bat=40,
                pinch_role="DEF",
            )
        )
        resolved = _resolved(rows)
        dmap = build_team_defense_map(resolved.home)
        # CF now belongs to the sub (highest sequence), not the starter.
        assert dmap["CF"] == 150
        assert dmap["CF"] != HOME_BATTERS[0]

    def test_position_change_sub_does_not_leak_old_position(self):
        # A sub that ENTERS at a different code than the slot's starter: the slot
        # occupant's *current* position_code wins, and only one slot maps to it.
        rows = _starting_rows()
        # Slot 8 starter is the catcher (code '2'); a sub enters at slot 8 but is
        # now playing LF (code '7'), seq 2.  (Contrived double-switch shape.)
        rows.append(
            _lineup_row(
                HOME_TEAM,
                170,
                8,
                "7",
                is_starter=False,
                sequence=2,
                entered_at_bat=50,
                pinch_role="DEF",
            )
        )
        resolved = _resolved(rows)
        dmap = build_team_defense_map(resolved.home)
        # Slot 8's current occupant (170) plays LF now.
        assert dmap["LF"] == 170
        # The original LF starter (slot 5, code '7', id 105) is overwritten by the
        # higher-sequence occupant of the LF position.
        assert dmap["LF"] != HOME_BATTERS[4]

    def test_mid_game_pitching_change_owns_P_slot(self):
        rows = _starting_rows()
        # A reliever (code '1', no batting_order) enters seq 2 -> wins pitcher.
        rows.append(
            _lineup_row(
                HOME_TEAM,
                950,
                None,
                "1",
                is_starter=False,
                sequence=2,
                entered_inning=8,
                entered_at_bat=60,
            )
        )
        resolved = _resolved(rows)
        dmap = build_team_defense_map(resolved.home)
        assert dmap["P"] == 950
        assert resolved.home.pitcher_id == 950


# ===========================================================================
# Non-fielding occupants (DH / pinch roles) are skipped.
# ===========================================================================


class TestNonFieldingSkipped:
    def test_dh_is_not_a_fielding_slot(self):
        # AL-style: slot 9 is the DH (code '10'); a separate pitcher row (code
        # '1', no batting_order) holds the glove.
        rows: list[dict] = []
        positions = ["8", "4", "9", "3", "7", "5", "6", "2", "10"]  # 10 = DH
        for slot, (pid, pos) in enumerate(zip(HOME_BATTERS, positions, strict=False), start=1):
            rows.append(_lineup_row(HOME_TEAM, pid, slot, pos))
        rows.append(_lineup_row(HOME_TEAM, 901, None, "1"))  # AL pitcher
        # Away side needs a batter + pitcher so the resolve doesn't error.
        rows.append(_lineup_row(AWAY_TEAM, AWAY_BATTERS[0], 1, "8"))
        rows.append(_lineup_row(AWAY_TEAM, 902, None, "1"))

        resolved = _resolved(rows)
        dmap = build_team_defense_map(resolved.home)
        # The DH (slot-9 hitter) holds no glove -> not in the map.
        assert HOME_BATTERS[8] not in dmap.values()
        # The eight position players + the pitcher fill nine slots.
        assert dmap["P"] == 901
        assert set(dmap) == set(DEFENSE_POSITIONS)
        # Exactly the eight fielders (codes 8,4,9,3,7,5,6,2) + pitcher.
        assert dmap["CF"] == HOME_BATTERS[0]
        assert dmap["C"] == HOME_BATTERS[7]

    def test_pinch_role_without_field_code_is_skipped(self):
        # A pinch hitter whose position_code is the non-numeric role 'PH' is in
        # the batting order but holds no glove -> contributes no field slot.
        rows = _starting_rows()
        # Replace slot 1 occupant with a PH carrying position_code 'PH'.
        rows.append(
            _lineup_row(
                HOME_TEAM,
                180,
                1,
                "PH",
                is_starter=False,
                sequence=2,
                entered_at_bat=70,
                pinch_role="PH",
            )
        )
        resolved = _resolved(rows)
        dmap = build_team_defense_map(resolved.home)
        # The PH (180) is the current slot-1 batter but is not in the field.
        assert 180 not in dmap.values()
        # CF (the slot-1 starter's position) is now vacated (no fielder mapped).
        assert "CF" not in dmap


# ===========================================================================
# Correct fielding side for TOP vs BOTTOM.
# ===========================================================================


class TestFieldingSideSelection:
    def test_fielding_side_for_half_convention(self):
        # HOME fields in the top, AWAY fields in the bottom (== GameState.defense).
        assert fielding_side_for_half(Half.TOP) == Team.HOME
        assert fielding_side_for_half(Half.BOTTOM) == Team.AWAY

    def test_top_of_inning_uses_home_defense(self):
        resolved = _resolved()
        state = GameState(pitcher_id=HOME_BATTERS[8], bat_hand="R", season=SEASON, half=Half.TOP)
        dmap = build_defense_map_for_state(resolved, state)
        assert dmap == EXPECTED_HOME_MAP
        # Sanity: this is the team GameState.defense names too.
        assert state.defense == Team.HOME

    def test_bottom_of_inning_uses_away_defense(self):
        resolved = _resolved()
        state = GameState(pitcher_id=AWAY_BATTERS[8], bat_hand="R", season=SEASON, half=Half.BOTTOM)
        dmap = build_defense_map_for_state(resolved, state)
        assert dmap == EXPECTED_AWAY_MAP
        assert state.defense == Team.AWAY

    def test_for_state_matches_explicit_side_choice(self):
        resolved = _resolved()
        top = GameState(pitcher_id=HOME_BATTERS[8], bat_hand="R", season=SEASON, half=Half.TOP)
        bot = GameState(pitcher_id=AWAY_BATTERS[8], bat_hand="R", season=SEASON, half=Half.BOTTOM)
        assert build_defense_map_for_state(resolved, top) == build_defense_map(
            resolved, fielding_side="home"
        )
        assert build_defense_map_for_state(resolved, bot) == build_defense_map(
            resolved, fielding_side="away"
        )
