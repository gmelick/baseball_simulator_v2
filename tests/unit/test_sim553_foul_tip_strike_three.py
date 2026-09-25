"""SIM-553: a two-strike foul tip or foul bunt is strike three in the pitch pool.

WHY THIS FILE EXISTS
====================
The pool build maps the MLB feed's one-letter pitch code to the class the
simulator's count machine reads. It mapped a foul tip ('T') and a foul bunt
('L') to ``foul`` at every count. With two strikes both are strike three by
rule. The count machine keeps a two-strike foul alive, so every real strikeout
of that kind became one more pitch, and the loop turned 63% of them into a
walk, a hit-by-pitch or a ball in play.

The measured loss (2026-09-23, the pool window 2023-2026): 11,168 rows are a
two-strike 'T', 'L' or 'O'; 11,165 of them end the plate appearance as a
strikeout. The pool's count chain read 0.2163 strikeouts per plate appearance
against 0.2255 in real plate appearances: the simulator made 4.4% too few
strikeouts. The corrected coding reads 0.2262. Two rarer codes ('O', a foul
tip on a bunt, and 'Q', a swing and a miss at a pitchout) fell through the old
``ELSE 'ball'`` and played as balls.

The pool-totals grade could not see it, because its strikeout centre came from
the same labels. The old tests read the pool build's source with a regular
expression, which cannot see a conditional branch or a branch-order bug. So
every test here EXECUTES ``SQL_OUTCOME_TYPE`` in DuckDB. The design:
``docs/audit/2026-09-23-foul-tip-strike-three-pool-coding-plan.md``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import duckdb
import pytest

from pipeline.batch.player_profile_computor import (
    BALL_TYPES,
    CALLED_STRIKE_TYPES,
    FOUL_TYPES,
    POOL_BUILDER_VERSION,
    SQL_OUTCOME_TYPE,
    STRIKE_THREE_FOUL_TYPES,
    WHIFF_TYPES,
    PlayerProfileComputor,
)
from pipeline.statcast_events import IN_PLAY_TYPES
from simulation.game_state import PITCH_OUTCOMES
from simulation.sim_loop import EVENT_IN_PROGRESS, EVENT_STRIKEOUT, advance_count

_BALL = "ball"
_CALLED = "called_strike"
_SWING = "swinging_strike"
_FOUL = "foul"
_IN_PLAY = "in_play"
_HBP = "hit_by_pitch"

#: The design's §2.1 "Class after" column: the class of every code the feed
#: uses, at strikes 0, 1 and 2 (the count BEFORE the pitch). These are all the
#: codes ``raw.pitches`` holds in ten seasons (re-read 2026-09-25: 17 codes, no
#: NULL code, no NULL strikes).
_CLASS_AFTER: dict[str, tuple[str, str, str]] = {
    "B": (_BALL, _BALL, _BALL),
    "*B": (_BALL, _BALL, _BALL),
    "P": (_BALL, _BALL, _BALL),
    "C": (_CALLED, _CALLED, _CALLED),
    "S": (_SWING, _SWING, _SWING),
    "W": (_SWING, _SWING, _SWING),
    "M": (_SWING, _SWING, _SWING),
    "Q": (_SWING, _SWING, _SWING),
    "F": (_FOUL, _FOUL, _FOUL),
    "T": (_FOUL, _FOUL, _SWING),
    "L": (_FOUL, _FOUL, _SWING),
    "O": (_FOUL, _FOUL, _SWING),
    "R": (_FOUL, _FOUL, _FOUL),
    "X": (_IN_PLAY, _IN_PLAY, _IN_PLAY),
    "D": (_IN_PLAY, _IN_PLAY, _IN_PLAY),
    "E": (_IN_PLAY, _IN_PLAY, _IN_PLAY),
    "H": (_HBP, _HBP, _HBP),
}

_TRUTH_TABLE = [
    pytest.param(code, strikes, classes[strikes], id=f"{code}-{strikes}")
    for code, classes in _CLASS_AFTER.items()
    for strikes in (0, 1, 2)
]


def _classify(rows: list[tuple[str | None, int, str | None]]) -> list[str | None]:
    """Run ``SQL_OUTCOME_TYPE`` over ``(type, strikes, events)`` rows, in order."""
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE p (i INTEGER, type VARCHAR, strikes SMALLINT, events VARCHAR)")
        con.executemany(
            "INSERT INTO p VALUES (?, ?, ?, ?)",
            [(i, code, strikes, events) for i, (code, strikes, events) in enumerate(rows)],
        )
        got = con.execute(f"SELECT {SQL_OUTCOME_TYPE} FROM p ORDER BY i").fetchall()
    finally:
        con.close()
    return [r[0] for r in got]


def _class_of(code: str, strikes: int, events: str | None = None) -> str | None:
    return _classify([(code, strikes, events)])[0]


class TestTheTruthTable:
    @pytest.mark.parametrize(("code", "strikes", "expected"), _TRUTH_TABLE)
    def test_every_feed_code_at_every_strike_count(self, code, strikes, expected):
        """The feed's codes are char(2), so a one-letter code arrives
        space-padded ('T '). The bare form must read the same."""
        padded = code.ljust(2)
        assert _classify([(padded, strikes, None), (code, strikes, None)]) == [
            expected,
            expected,
        ]

    def test_the_table_covers_every_code_the_sets_name(self):
        """A code added to a set without a row here fails this test."""
        named = (
            set(BALL_TYPES)
            | set(CALLED_STRIKE_TYPES)
            | set(WHIFF_TYPES)
            | set(FOUL_TYPES)
            | set(IN_PLAY_TYPES)
            | {"H"}
        )
        assert set(_CLASS_AFTER) == named

    def test_every_class_is_in_the_loops_vocabulary(self):
        """The count machine raises on a class outside PITCH_OUTCOMES."""
        classes = {c for triple in _CLASS_AFTER.values() for c in triple}
        assert classes <= set(PITCH_OUTCOMES)


class TestTheBranchOrder:
    """A CASE takes the first branch that matches. The two-strike branch placed
    after the foul branch is valid SQL that never fires, and the loss stays."""

    def test_a_two_strike_foul_tip_is_strike_three(self):
        assert _class_of("T ", 2) == _SWING

    def test_a_one_strike_foul_tip_is_a_foul(self):
        assert _class_of("T ", 1) == _FOUL

    def test_a_two_strike_plain_foul_stays_a_foul(self):
        assert _class_of("F ", 2) == _FOUL

    def test_a_two_strike_foul_pitchout_stays_a_foul(self):
        assert _class_of("R ", 2) == _FOUL


class TestTheHitByPitchBranch:
    def test_a_ball_coded_row_with_the_hbp_event_is_hit_by_pitch(self):
        """SIM-509: an HBP pitch can carry a ball code. The events branch
        comes first, or the pitch becomes ball four."""
        assert _class_of("B ", 0, "hit_by_pitch") == _HBP
        assert _class_of("*B", 2, "hit_by_pitch") == _HBP

    def test_an_h_row_with_no_event_is_hit_by_pitch(self):
        """The 2022 row (game_pk 662280, at bat 69, pitch 5) has no event label;
        only its 'H' code names it. The old expression made it a ball."""
        assert _class_of("H ", 0, None) == _HBP


class TestAnUnknownCodeFailsLoudly:
    """Decision 3: no ELSE. The old ``ELSE 'ball'`` is how 'O', 'Q' and the
    hit-by-pitch rows hid."""

    @pytest.mark.parametrize("code", ["Z ", "V", "", None])
    def test_an_unknown_code_yields_null(self, code):
        assert _class_of(code, 0) is None

    def test_the_expression_has_no_else(self):
        assert "ELSE" not in SQL_OUTCOME_TYPE.upper()

    def test_a_not_null_column_refuses_the_row(self):
        con = duckdb.connect(":memory:")
        try:
            con.execute("CREATE TABLE p (type VARCHAR, strikes SMALLINT, events VARCHAR)")
            con.execute("INSERT INTO p VALUES ('Z ', 0, NULL)")
            con.execute("CREATE TEMP TABLE pool (outcome_type VARCHAR NOT NULL)")
            with pytest.raises(duckdb.ConstraintException):
                con.execute(f"INSERT INTO pool SELECT {SQL_OUTCOME_TYPE} FROM p")
        finally:
            con.close()


class TestThePoolBuildUsesTheConstant:
    def test_the_select_renders_the_constant_and_spells_no_code_inline(self):
        src = inspect.getsource(PlayerProfileComputor._build_pitch_pool)
        assert "{SQL_OUTCOME_TYPE}" in src
        assert "WHEN TRIM(type)" not in src

    def test_the_builder_version_moved(self):
        # sim523g.1 -> sim553.1 (SIM-553: a two-strike foul tip / foul bunt is strike three)
        assert POOL_BUILDER_VERSION == "sim553.1"


class TestTheCountMachineEndsThePlateAppearance:
    @pytest.mark.parametrize("code", STRIKE_THREE_FOUL_TYPES)
    @pytest.mark.parametrize("balls", [0, 1, 2, 3])
    def test_a_two_strike_foul_tip_or_foul_bunt_is_a_strikeout(self, code, balls):
        adv = advance_count(balls, 2, _class_of(code.ljust(2), 2))
        assert adv.terminal
        assert adv.event == EVENT_STRIKEOUT
        assert not adv.is_contact

    @pytest.mark.parametrize("balls", [0, 1, 2, 3])
    def test_a_two_strike_plain_foul_keeps_the_plate_appearance_alive(self, balls):
        adv = advance_count(balls, 2, _class_of("F ", 2))
        assert not adv.terminal
        assert adv.event == EVENT_IN_PROGRESS
        assert (adv.balls, adv.strikes) == (balls, 2)


class TestTheCodeSets:
    def test_the_strike_three_fouls_are_fouls(self):
        assert set(STRIKE_THREE_FOUL_TYPES) <= set(FOUL_TYPES)

    def test_contact_is_not_a_whiff(self):
        assert not set(STRIKE_THREE_FOUL_TYPES) & set(WHIFF_TYPES)

    def test_no_code_sits_in_two_unconditional_sets(self):
        """Each code names one class at each count. 'H' has its own branch."""
        sets = (BALL_TYPES, CALLED_STRIKE_TYPES, WHIFF_TYPES, FOUL_TYPES, IN_PLAY_TYPES)
        for code in set().union(*sets) | {"H"}:
            members = sum(code in s for s in sets)
            assert members == (0 if code == "H" else 1), code


# ---------------------------------------------------------------------------
# End to end: the real _build_pitch_pool over a stub pg.raw.pitches, into the
# real sim.pitch_pool DDL (outcome_type VARCHAR(20) NOT NULL).
# ---------------------------------------------------------------------------

_SCHEMA_SQL = (
    Path(__file__).resolve().parents[2] / "db" / "schemas" / "02_duckdb_schema.sql"
).read_text(encoding="utf-8")

#: Every raw.pitches column the pitch-pool SELECT reads.
_RAW_PITCHES_DDL = (
    "CREATE TABLE pg.raw.pitches ("
    "game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, game_date DATE, "
    "season SMALLINT, pitcher INTEGER, p_throws VARCHAR, batter INTEGER, stand VARCHAR, "
    "release_speed FLOAT, break_vertical_induced FLOAT, break_horizontal FLOAT, "
    "release_spin_rate FLOAT, spin_axis FLOAT, release_pos_x FLOAT, release_pos_z FLOAT, "
    "release_extension FLOAT, plate_x FLOAT, plate_z FLOAT, zone SMALLINT, "
    "balls SMALLINT, strikes SMALLINT, outs SMALLINT, on_1b INTEGER, on_2b INTEGER, "
    "on_3b INTEGER, inning SMALLINT, bat_score SMALLINT, fld_score SMALLINT, "
    "inning_topbot VARCHAR, type VARCHAR, events VARCHAR, des VARCHAR, fielder_2 INTEGER, "
    "passed_ball_wild_pitch BOOLEAN, data_quality_flag BOOLEAN)"
)


def _real_ddl(table: str) -> str:
    start = _SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = _SCHEMA_SQL.index("\n);", start)
    return _SCHEMA_SQL[start : end + 3]


def _build(pitches: list[tuple[int, int, int, str, str | None]]) -> dict[int, str]:
    """Build the pool from ``(pitch_number, balls, strikes, type, events)`` rows
    of one 2025 plate appearance; return ``{pitch_number: outcome_type}``."""
    con = duckdb.connect(":memory:")
    try:
        con.execute("ATTACH ':memory:' AS pg")
        con.execute("CREATE SCHEMA pg.raw")
        con.execute(_RAW_PITCHES_DDL)
        con.execute("CREATE SCHEMA sim")
        con.execute(_real_ddl("sim.pitch_pool"))
        con.execute(_real_ddl("sim.pool_build_metadata"))
        con.executemany(
            "INSERT INTO pg.raw.pitches (game_pk, at_bat_number, pitch_number, game_date, "
            "season, pitcher, p_throws, batter, stand, balls, strikes, outs, inning, "
            "bat_score, fld_score, inning_topbot, type, events, data_quality_flag) "
            "VALUES (1, 1, ?, DATE '2025-05-01', 2025, 10, 'R', 20, 'L', ?, ?, 0, 1, "
            "0, 0, 'Top', ?, ?, FALSE)",
            pitches,
        )
        comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
        comp._conn = con
        comp._build_pitch_pool([2025])
        got = con.execute("SELECT pitch_number, outcome_type FROM sim.pitch_pool").fetchall()
    finally:
        con.close()
    return dict(got)


class TestThePoolBuildEndToEnd:
    def test_a_foul_tip_is_a_foul_at_one_strike_and_strike_three_at_two(self):
        got = _build(
            [
                (1, 0, 0, "C ", None),
                (2, 0, 1, "T ", None),
                (3, 0, 2, "T ", "strikeout"),
            ]
        )
        assert got == {1: _CALLED, 2: _FOUL, 3: _SWING}

    def test_an_unknown_code_stops_the_build(self):
        with pytest.raises(duckdb.ConstraintException):
            _build([(1, 0, 0, "Z ", None)])
