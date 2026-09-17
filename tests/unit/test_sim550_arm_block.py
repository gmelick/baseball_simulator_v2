"""
SIM-550 — the outfield arm block, rebuilt from our own advancement pool.

Plan: docs/audit/2026-09-16-sim550-outfield-arm-block-plan.md (§7 lists these).

The defect: the loader pulled Savant's baserunning board in its RUNNER view,
so the block described how the outfielder runs the bases. The fix: the
aggregator writes the six advancement columns NULL, and a new step after the
pools, ``_fill_outfield_arm_block``, fills them from
``sim.advancement_opportunity_pool`` — one row per real chance to take an
extra base, with the fielder who fielded the ball and his position.

What these tests hold:

* the fill, EXECUTED on an in-memory DuckDB with the canonical DDL: the
  counts and rates come out as hand-computed; a catcher's row and an
  infielder's row stay NULL; a chance with no fielder is ignored; the
  expectation is per season x decision x outs x position (every coarser
  cell, including one without the position or without the from / target
  bases, gives a different number), so a 0 prevention is average at every
  position; a cutoff drops later-dated rows and recomputes the expectation
  under it; ``of_arm_runs`` is written NULL; a database without the pool
  logs and leaves the block NULL;
* the reset: a row the pool does not support (the runner-view block on a
  position the player did not field, or on a designated hitter) is cleared
  by the fill alone, so the run book's fill-only path leaves no stale row;
* the ticket condition: a catcher who fielded a chance has NO block, and a
  designated hitter's stale block is cleared;
* the aggregator, executed then composed with the fill: the block is NULL
  after the aggregator, filled after the step, and the throw velocity still
  comes from the arm-strength board;
* the source shape: the dead extraction and its temp table are gone, the
  fill runs after the pool it reads, the shared prior is 50;
* the league writer: the outfield rows carry the three arm keys the model
  shrinks toward, and ``AVG`` skips NULL.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"
COMPUTOR = REPO / "pipeline" / "batch" / "player_profile_computor.py"

ARM_BLOCK = (
    "arm_opportunities",
    "arm_holds",
    "arm_hold_rate",
    "arm_assists",
    "arm_thrown_out_rate",
    "arm_advancement_prevention",
)
ARM_BLOCK_SQL = ", ".join(ARM_BLOCK)

LF, CF, RF = 7, 8, 9
CATCHER, SHORTSTOP = 2, 6


def _real_ddl(table: str) -> str:
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    start = text.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = text.index("\n);", start)
    return text[start : end + 3]


def _db(*, with_pool: bool = True) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB with the canonical fielder table and, unless told
    otherwise, the canonical advancement pool."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA derived")
    con.execute(_real_ddl("derived.fielder_season_metrics"))
    if with_pool:
        con.execute("CREATE SCHEMA sim")
        con.execute(_real_ddl("sim.advancement_opportunity_pool"))
    return con


def _computor(con: duckdb.DuckDBPyConnection):
    from pipeline.batch.player_profile_computor import PlayerProfileComputor

    c = PlayerProfileComputor.__new__(PlayerProfileComputor)
    c._conn = con
    return c


# (pitch_id, fielder_id, fielder_pos, season, scenario, from, to, outs, attempted, safe, date)
#
# Two left fielders (11 and 16), two center fielders (12 and 17) and one
# right fielder (18), so every expectation cell holds more than one
# fielder and the cell key can be told apart from a coarser one. Fielder
# 13 is a catcher who fielded one chance, fielder 14 a shortstop. Row 5 has
# no fielder. Row 6 is dated after the cutoff the cutoff test uses.
#
# The left-field cells (2024): first-to-third at 0 outs holds rows 1, 2, 6,
# 11 and 12 (two of five attempted, rate 0.4); second-to-home at 1 out holds
# rows 3 and 13 (rate 0.5).
#
# The center-field cells (2024) are two decisions of ONE scenario, the tag
# up (scenario 4): tag from first (1 -> 2) at 0 outs holds rows 4, 14, 15
# and 18 (rate 1/4); tag third to home (3 -> 4) at 0 outs holds rows 7, 16
# and 17 (rate 2/3); tag from first at 2 outs holds row 19 alone (rate 1).
# The right-field cell for the tag from first at 0 outs holds rows 20 and
# 21 (rate 1): the same decision as the center-field cell, at another
# position.
_POOL_ROWS = [
    (1, 11, LF, 2024, 1, 1, 3, 0, True, True, date(2024, 5, 1)),
    (2, 11, LF, 2024, 1, 1, 3, 0, False, False, date(2024, 5, 1)),
    (3, 11, LF, 2024, 2, 2, 4, 1, True, False, date(2024, 5, 2)),
    (4, 12, CF, 2024, 4, 1, 2, 0, False, False, date(2024, 5, 1)),
    (5, None, LF, 2024, 1, 1, 3, 0, True, True, date(2024, 5, 1)),
    (6, 11, LF, 2024, 1, 1, 3, 0, True, True, date(2024, 8, 1)),
    (7, 12, CF, 2024, 4, 3, 4, 0, True, True, date(2024, 5, 3)),
    (8, 12, CF, 2023, 4, 1, 2, 0, False, False, date(2023, 5, 1)),
    (9, 13, CATCHER, 2024, 5, 0, 2, 0, True, False, date(2024, 5, 1)),
    (10, 14, SHORTSTOP, 2024, 5, 0, 2, 0, False, False, date(2024, 5, 1)),
    (11, 16, LF, 2024, 1, 1, 3, 0, False, False, date(2024, 5, 1)),
    (12, 16, LF, 2024, 1, 1, 3, 0, False, False, date(2024, 5, 1)),
    (13, 16, LF, 2024, 2, 2, 4, 1, False, False, date(2024, 5, 2)),
    (14, 17, CF, 2024, 4, 1, 2, 0, False, False, date(2024, 5, 1)),
    (15, 17, CF, 2024, 4, 1, 2, 0, True, True, date(2024, 5, 1)),
    (16, 17, CF, 2024, 4, 3, 4, 0, True, True, date(2024, 5, 3)),
    (17, 17, CF, 2024, 4, 3, 4, 0, False, False, date(2024, 5, 3)),
    (18, 12, CF, 2024, 4, 1, 2, 0, False, False, date(2024, 5, 4)),
    (19, 17, CF, 2024, 4, 1, 2, 2, True, False, date(2024, 5, 4)),
    (20, 18, RF, 2024, 4, 1, 2, 0, True, True, date(2024, 5, 1)),
    (21, 18, RF, 2024, 4, 1, 2, 0, True, True, date(2024, 5, 1)),
]

# The fielder rows the aggregator would have written (the block NULL). The
# left fielder 11 carries a stale run value the fill must overwrite with
# NULL.
_FIELDER_ROWS = [
    (11, "LF", 2024, 1.5),
    (16, "LF", 2024, None),
    (12, "CF", 2024, None),
    (17, "CF", 2024, None),
    (18, "RF", 2024, None),
    (12, "CF", 2023, None),
    (13, "C", 2024, None),
    (14, "SS", 2024, None),
]

# The runner-view block the September join wrote: the player's OWN running
# figures (the live example is player 543807, CF, 2023). The fixture seeds
# it on rows the pool does not support, so the fill must clear them.
_RUNNER_VIEW_BLOCK = (140, 91, 0.65, 2, 0.041, 0.048, 1.89)
_STALE_ROWS = [
    # Center fielder 12 never fielded a left-field chance in 2024 or 2023.
    (12, "LF", 2024),
    (12, "LF", 2023),
    # The designated hitter, who fields no chance: on the runner view he
    # carried 60 to 160 chances of his own running.
    (15, "DH", 2024),
]


def _seed(con: duckdb.DuckDBPyConnection) -> None:
    con.executemany(
        "INSERT INTO sim.advancement_opportunity_pool "
        "(pitch_id, game_pk, at_bat_number, pitch_number, game_date, season, scenario, "
        "from_base, target_base, runner_id, fielder_id, fielder_pos, outs, attempted, safe) "
        "VALUES (?, 1, ?, 1, ?, ?, ?, ?, ?, 900, ?, ?, ?, ?, ?)",
        [
            (pid, pid, day, season, scen, frm, to, fielder, pos, outs, att, safe)
            for (pid, fielder, pos, season, scen, frm, to, outs, att, safe, day) in _POOL_ROWS
        ],
    )
    con.executemany(
        "INSERT INTO derived.fielder_season_metrics (player_id, position, season, of_arm_runs) "
        "VALUES (?, ?, ?, ?)",
        _FIELDER_ROWS,
    )
    con.executemany(
        "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
        f"{ARM_BLOCK_SQL}, of_arm_runs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(pid, pos, season, *_RUNNER_VIEW_BLOCK) for pid, pos, season in _STALE_ROWS],
    )


def _block(con: duckdb.DuckDBPyConnection, player_id: int, position: str, season: int) -> tuple:
    return con.execute(
        f"SELECT {ARM_BLOCK_SQL}, of_arm_runs FROM derived.fielder_season_metrics "
        "WHERE player_id = ? AND position = ? AND season = ?",
        [player_id, position, season],
    ).fetchone()


# ===========================================================================
# 1. The fill, executed
# ===========================================================================


class TestFillExecuted:
    def test_the_left_fielders_block_is_the_hand_computed_one(self) -> None:
        """Four chances (rows 1, 2, 3, 6), three attempts, one thrown out.
        The expectation: 0.4 in the (2024, first-to-third, 0 outs, LF) cell
        and 0.5 in the (2024, second-to-home, 1 out, LF) cell, so 1.7
        expected attempts."""
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        opps, holds, hold_rate, assists, to_rate, prevention, runs = _block(con, 11, "LF", 2024)
        assert opps == 4
        assert holds == 1
        assert hold_rate == pytest.approx(0.25)
        assert assists == 1
        assert to_rate == pytest.approx(1 / 3)
        assert prevention == pytest.approx((1.7 - 3) / 4)
        assert runs is None

    def test_the_other_left_fielders_block_shares_the_cells(self) -> None:
        """Fielder 16: three chances (rows 11, 12, 13), never challenged, so
        1.3 expected attempts against none."""
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        opps, holds, hold_rate, assists, to_rate, prevention, runs = _block(con, 16, "LF", 2024)
        assert (opps, holds, assists, runs) == (3, 3, 0, None)
        assert hold_rate == pytest.approx(1.0)
        assert to_rate is None
        assert prevention == pytest.approx(1.3 / 3)

    def test_an_unchallenged_arm_has_a_null_thrown_out_rate(self) -> None:
        """The center fielder in 2023: one chance, never challenged."""
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        opps, holds, hold_rate, assists, to_rate, prevention, _ = _block(con, 12, "CF", 2023)
        assert (opps, holds, assists) == (1, 1, 0)
        assert hold_rate == pytest.approx(1.0)
        assert to_rate is None
        assert prevention == pytest.approx(0.0)  # the cell's only row: expected 0, attempts 0

    def test_a_catchers_row_and_an_infielders_row_stay_null(self) -> None:
        """The ticket condition. The catcher fielded a chance (row 9) and the
        shortstop fielded one (row 10); neither is an outfield slot, so
        neither gets a block."""
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        assert _block(con, 13, "C", 2024) == (None,) * 7
        assert _block(con, 14, "SS", 2024) == (None,) * 7
        filled = con.execute(
            "SELECT player_id, position, season FROM derived.fielder_season_metrics "
            "WHERE arm_opportunities IS NOT NULL ORDER BY 1, 2, 3"
        ).fetchall()
        assert filled == [
            (11, "LF", 2024),
            (12, "CF", 2023),
            (12, "CF", 2024),
            (16, "LF", 2024),
            (17, "CF", 2024),
            (18, "RF", 2024),
        ]

    def test_a_designated_hitters_stale_block_is_cleared(self) -> None:
        """The designated hitter has a fielder row and fields no chance. On
        the runner view his row carried 60 to 160 chances of his OWN
        running (the seeded block); the fill leaves every arm column NULL."""
        con = _db()
        _seed(con)
        assert _block(con, 15, "DH", 2024) == pytest.approx(_RUNNER_VIEW_BLOCK, rel=1e-6)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        assert _block(con, 15, "DH", 2024) == (None,) * 7
        assert (
            con.execute(
                "SELECT COUNT(*) FROM derived.fielder_season_metrics "
                "WHERE player_id = 15 AND arm_opportunities IS NOT NULL"
            ).fetchone()[0]
            == 0
        )

    def test_a_row_the_pool_does_not_support_is_cleared_not_kept(self) -> None:
        """The live case (2026-09-17: 23 outfield rows): a block at a
        (player, position, season) where the pool holds NO chance fielded
        by him at that position. Center fielder 12 never fielded a
        left-field chance, and his LF row carries the runner-view block. The
        join UPDATE alone cannot touch that row; the reset must, or the
        run book's verify block reds after the fill has committed."""
        con = _db()
        _seed(con)
        assert _block(con, 12, "LF", 2024) == pytest.approx(_RUNNER_VIEW_BLOCK, rel=1e-6)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        assert _block(con, 12, "LF", 2024) == (None,) * 7
        assert _block(con, 12, "LF", 2023) == (None,) * 7
        # His center-field rows are filled as before: the reset takes
        # nothing the pool gives back.
        assert _block(con, 12, "CF", 2024)[0] == 3
        assert _block(con, 12, "CF", 2023)[0] == 1
        # Nothing in the requested seasons still carries the run value.
        assert (
            con.execute(
                "SELECT COUNT(*) FROM derived.fielder_season_metrics WHERE of_arm_runs IS NOT NULL"
            ).fetchone()[0]
            == 0
        )

    def test_a_chance_with_no_fielder_is_ignored(self) -> None:
        """Row 5 (no fielder, an attempt) is not a chance against anyone and
        does not move the cell's expectation: with it the first-to-third
        left-field cell would read 3/6, and the left fielder's prevention
        would move from -0.325 to -0.25."""
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        _, _, _, _, _, prevention, _ = _block(con, 11, "LF", 2024)
        assert prevention == pytest.approx(-0.325)
        assert prevention != pytest.approx((3 * 0.5 + 0.5 - 3) / 4)

    def test_the_expectation_cell_is_season_by_decision_by_outs_by_position(self) -> None:
        """Center fielder 12 in 2024: two tags from first at 0 outs (rate
        1/4 in his position's cell) and one tag third to home at 0 outs
        (rate 2/3), one attempt. The right cell gives (7/6 - 1) / 3 = 1/18.
        Each coarser key gives a different number, because the second
        center fielder, the right fielder, the 2-out row and the 2023 row
        each sit in a cell the coarser key would merge:

        * without the position (the right fielder's two attempts join the
          tag-from-first cell): (5/3 - 1) / 3 = 2/9;
        * without from_base / target_base (the two tag decisions of
          scenario 4 pooled at 3/7): (9/7 - 1) / 3 = 2/21;
        * without the outs (row 19 joins the tag-from-first cell): 7/45;
        * without the season (row 8 joins it): 1/45.
        """
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        _, _, _, _, _, prevention, _ = _block(con, 12, "CF", 2024)
        assert prevention == pytest.approx(1 / 18)
        for wrong in (2 / 9, 2 / 21, 7 / 45, 1 / 45):
            assert prevention != pytest.approx(wrong)

    def test_a_zero_prevention_is_average_at_every_position(self) -> None:
        """The chances-weighted prevention sums to zero WITHIN each position
        (the expectation cell carries the position), not only over the
        outfield as a whole. The right fielder alone in his cell reads 0;
        the two center fielders' chances-weighted preventions cancel."""
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        rows = con.execute(
            "SELECT position, SUM(arm_advancement_prevention * arm_opportunities) "
            "FROM derived.fielder_season_metrics "
            "WHERE season = 2024 AND arm_opportunities IS NOT NULL GROUP BY 1 ORDER BY 1"
        ).fetchall()
        assert [p for p, _ in rows] == ["CF", "LF", "RF"]
        for _, weighted in rows:
            assert weighted == pytest.approx(0.0, abs=1e-9)
        assert _block(con, 18, "RF", 2024)[5] == pytest.approx(0.0)
        assert _block(con, 17, "CF", 2024)[5] == pytest.approx(-1 / 30)

    def test_a_cutoff_drops_later_dated_rows_and_recomputes_the_expectation(self) -> None:
        """Under a 2024-06-01 cutoff row 6 (August) leaves: the left fielder
        has three chances, and the first-to-third left-field cell reads
        1/4, so his expected attempts are 1/4 + 1/4 + 1/2 and his
        prevention (1 - 2) / 3."""
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024], asof=date(2024, 6, 1))
        opps, holds, _, assists, _, prevention, _ = _block(con, 11, "LF", 2024)
        assert (opps, holds, assists) == (3, 1, 1)
        assert prevention == pytest.approx((1 - 2) / 3)

    def test_without_a_cutoff_every_row_counts(self) -> None:
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2023, 2024], asof=None)
        assert _block(con, 11, "LF", 2024)[0] == 4

    def test_a_season_outside_the_request_is_left_alone(self) -> None:
        """Neither the fill nor the reset touches a season not requested:
        the 2023 center-field row stays NULL, and the 2023 stale block on
        his left-field row stays as seeded."""
        con = _db()
        _seed(con)
        _computor(con)._fill_outfield_arm_block([2024])
        assert _block(con, 12, "CF", 2023) == (None,) * 7
        assert _block(con, 12, "LF", 2023) == pytest.approx(_RUNNER_VIEW_BLOCK, rel=1e-6)
        assert _block(con, 12, "LF", 2024) == (None,) * 7
        assert _block(con, 12, "CF", 2024)[0] == 3

    def test_the_run_value_is_written_null(self) -> None:
        con = _db()
        _seed(con)
        assert _block(con, 11, "LF", 2024)[-1] == pytest.approx(1.5)  # the stale figure
        _computor(con)._fill_outfield_arm_block([2023, 2024])
        assert _block(con, 11, "LF", 2024)[-1] is None

    def test_a_database_without_the_pool_logs_and_leaves_the_block_null(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        con = _db(with_pool=False)
        con.executemany(
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season) "
            "VALUES (?, ?, ?)",
            [(11, "LF", 2024)],
        )
        with caplog.at_level(logging.WARNING, logger="player_profile_computor"):
            _computor(con)._fill_outfield_arm_block([2024])
        assert any("advancement_opportunity_pool absent" in r.message for r in caplog.records)
        assert _block(con, 11, "LF", 2024) == (None,) * 7

    def test_the_fill_is_idempotent(self) -> None:
        con = _db()
        _seed(con)
        c = _computor(con)
        c._fill_outfield_arm_block([2023, 2024])
        first = _block(con, 11, "LF", 2024)
        c._fill_outfield_arm_block([2023, 2024])
        assert _block(con, 11, "LF", 2024) == first


# ===========================================================================
# 2. The aggregator, then the fill
# ===========================================================================


class TestAggregatorThenFill:
    def _db(self) -> duckdb.DuckDBPyConnection:
        con = _db()
        con.execute("ATTACH ':memory:' AS pg")
        con.execute("CREATE SCHEMA pg.raw")
        con.execute(
            "CREATE TABLE pg.raw.sprint_speed (player_id INTEGER, season SMALLINT, "
            "sprint_speed DOUBLE)"
        )
        con.execute(
            "CREATE TABLE pg.raw.savant_arm_strength (player_id INTEGER, season SMALLINT, "
            "arm_overall DOUBLE, arm_1b DOUBLE, arm_2b DOUBLE, arm_3b DOUBLE, arm_ss DOUBLE, "
            "arm_lf DOUBLE, arm_cf DOUBLE, arm_rf DOUBLE)"
        )
        con.execute(
            "INSERT INTO pg.raw.savant_arm_strength VALUES "
            "(11, 2024, 88.0, NULL, NULL, NULL, NULL, 91.5, NULL, NULL)"
        )
        # One outfield play for the left fielder and one infield play for
        # the shortstop drive the aggregator; the placeholders cover the rest.
        con.execute(
            "CREATE TABLE _tmp_of_plays (fielder_id INTEGER, position VARCHAR, season SMALLINT, "
            "catch_probability FLOAT, caught BOOLEAN, oaa_credit FLOAT, direction_cat VARCHAR, "
            "star_rating INTEGER)"
        )
        con.execute("INSERT INTO _tmp_of_plays VALUES (11, 'LF', 2024, 0.8, TRUE, 0.2, 'deep', 3)")
        con.execute(
            "CREATE TABLE _tmp_if_plays (fielder_id INTEGER, position VARCHAR, season SMALLINT, "
            "out_probability FLOAT, out_recorded BOOLEAN, oaa_credit FLOAT, "
            "direction_category VARCHAR)"
        )
        con.execute(
            "INSERT INTO _tmp_if_plays VALUES (14, 'SS', 2024, 0.7, TRUE, 0.3, 'glove_side')"
        )
        return con

    def test_the_block_is_null_after_the_aggregator_and_filled_after_the_step(self) -> None:
        con = self._db()
        c = _computor(con)
        c._aggregate_fielder_season_metrics([2024], asof=None)
        rows = con.execute(
            f"SELECT player_id, position, arm_strength, {ARM_BLOCK_SQL}, of_arm_runs "
            "FROM derived.fielder_season_metrics ORDER BY 1"
        ).fetchall()
        assert [r[:2] for r in rows] == [(11, "LF"), (14, "SS")]
        assert rows[0][2] == pytest.approx(91.5)  # the left-field velocity, from the board
        assert rows[0][3:] == (None,) * 7
        assert rows[1][3:] == (None,) * 7

        con.executemany(
            "INSERT INTO sim.advancement_opportunity_pool "
            "(pitch_id, game_pk, at_bat_number, pitch_number, game_date, season, scenario, "
            "from_base, target_base, runner_id, fielder_id, fielder_pos, outs, attempted, safe) "
            "VALUES (?, 1, ?, 1, ?, ?, ?, ?, ?, 900, ?, ?, ?, ?, ?)",
            [
                (pid, pid, day, season, scen, frm, to, fielder, pos, outs, att, safe)
                for (pid, fielder, pos, season, scen, frm, to, outs, att, safe, day) in _POOL_ROWS
                if season == 2024
            ],
        )
        c._fill_outfield_arm_block([2024])
        opps, holds, hold_rate, assists, to_rate, prevention, runs = _block(con, 11, "LF", 2024)
        assert (opps, holds, assists, runs) == (4, 1, 1, None)
        assert hold_rate == pytest.approx(0.25)
        velocity = con.execute(
            "SELECT arm_strength FROM derived.fielder_season_metrics WHERE player_id = 11"
        ).fetchone()[0]
        assert velocity == pytest.approx(91.5)  # the fill does not touch the velocity
        assert _block(con, 14, "SS", 2024) == (None,) * 7


# ===========================================================================
# 3. The source shape
# ===========================================================================


class TestSourceShape:
    def test_the_dead_extraction_and_its_temp_table_are_gone(self) -> None:
        src = COMPUTOR.read_text(encoding="utf-8")
        assert "_compute_outfield_arm_metrics" not in src
        assert "_tmp_of_arm" not in src
        assert "_SQL_IS_OF" not in src  # the guard only the deleted board join used

    def test_the_fielder_aggregator_reads_no_baserunning_board(self) -> None:
        src = COMPUTOR.read_text(encoding="utf-8")
        start = src.index("def _aggregate_fielder_season_metrics")
        body = src[start : src.index("def _assert_fielder_profiles_have_no_leakage", start)]
        assert "savant_baserunning" not in body
        assert "of_arm_runs_in_cutoff_season" not in body
        assert "LEFT JOIN pg.raw.savant_arm_strength sas" in body

    def test_the_fill_runs_after_the_pools_in_run(self) -> None:
        src = COMPUTOR.read_text(encoding="utf-8")
        start = src.index("    def run(")
        body = src[start : src.index("def _delete_seasons", start)]
        pool = body.index("self._build_advancement_opportunity_pool(")
        fill = body.index("self._fill_outfield_arm_block(seasons, asof=asof)")
        aggregate = body.index("self._aggregate_fielder_season_metrics(seasons, asof=asof)")
        assert aggregate < pool < fill

    def test_the_expectation_cell_key_carries_the_five_columns_and_the_position(self) -> None:
        """The cell is season x decision x outs x position, where the
        decision is the (scenario, from_base, target_base) triple: scenario
        4 spans the tag from first, second to third and third to home,
        whose attempt rates differ tenfold. The GROUP BY and the USING
        must carry the same six columns."""
        src = COMPUTOR.read_text(encoding="utf-8")
        start = src.index("def _fill_outfield_arm_block")
        body = src[start : src.index("def _ensure_catcher_temp_tables_exist", start)]
        assert "SELECT season, scenario, from_base, target_base, outs, fielder_pos," in body
        assert "GROUP BY 1, 2, 3, 4, 5, 6" in body
        assert "USING (season, scenario, from_base, target_base, outs, fielder_pos)" in body

    def test_the_fill_resets_the_requested_seasons_before_the_join(self) -> None:
        """The reset (every arm column NULL over the requested seasons) runs
        before the join UPDATE, so the fill alone leaves the table right."""
        src = COMPUTOR.read_text(encoding="utf-8")
        start = src.index("def _fill_outfield_arm_block")
        body = src[start : src.index("def _ensure_catcher_temp_tables_exist", start)]
        reset = body.index("arm_opportunities          = NULL")
        join = body.index("arm_opportunities          = a.chances")
        assert reset < join
        reset_stmt = body[reset : body.index("WHERE season IN ({season_list})", reset)]
        for col in (*ARM_BLOCK, "of_arm_runs"):
            assert f"{col:<26} = NULL" in reset_stmt, col

    def test_the_shared_prior_is_fifty_chances(self) -> None:
        from pipeline.batch.player_profile_computor import ARM_ALPHA_PRIOR_CHANCES

        assert ARM_ALPHA_PRIOR_CHANCES == 50

    def test_the_table_probe_answers_without_raising(self) -> None:
        from pipeline.batch.player_profile_computor import _table_exists

        con = _db(with_pool=False)
        assert _table_exists(con, "derived", "fielder_season_metrics") is True
        assert _table_exists(con, "sim", "advancement_opportunity_pool") is False


# ===========================================================================
# 4. The league writer
# ===========================================================================


class TestLeagueWriter:
    def test_the_outfield_rows_carry_the_three_arm_keys(self, tmp_path: Path) -> None:
        """Two left fielders above the sample floor: one never challenged
        (a NULL thrown-out rate, which AVG skips), one without a velocity.
        A third, below the floor, is excluded as every other key is."""
        from pipeline.batch.player_profile_computor import LeagueAverageProfiles

        path = str(tmp_path / "sim550.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        con.execute(
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
            "outs_above_average, error_rate, dp_run_value, below_minimum_sample, "
            "arm_strength, arm_hold_rate, arm_thrown_out_rate, arm_advancement_prevention) VALUES "
            "(1, 'LF', 2024, 2.0, 0.01, NULL, FALSE, 90.0, 0.80, NULL, 0.02), "
            "(2, 'LF', 2024, -1.0, 0.02, NULL, FALSE, NULL, 0.70, 0.10, -0.04), "
            "(3, 'LF', 2024, 0.0, 0.03, NULL, TRUE, 70.0, 0.50, 0.50, -0.50), "
            "(4, 'SS', 2024, 1.0, 0.01, 0.5, FALSE, 85.0, NULL, NULL, NULL)"
        )
        con.close()
        LeagueAverageProfiles(path).compute([2024])
        con = duckdb.connect(path, read_only=True)
        rows = {
            e: json.loads(pj) if isinstance(pj, str) else pj
            for e, pj in con.execute(
                "SELECT entity_type, profile_json FROM derived.league_averages WHERE season = 2024"
            ).fetchall()
        }
        con.close()
        lf = rows["fielder_LF"]
        assert lf["arm_hold_rate"] == pytest.approx(0.75)
        assert lf["arm_advancement_prevention"] == pytest.approx(-0.01)
        assert lf["arm_thrown_out_rate"] == pytest.approx(0.10)  # AVG skipped the NULL
        assert lf["arm_strength"] == pytest.approx(90.0)  # and the missing velocity
        # An infield row carries the keys with no value: the block is NULL
        # there and the infield groups do not read them.
        ss = rows["fielder_SS"]
        assert ss["arm_advancement_prevention"] is None
        assert ss["arm_thrown_out_rate"] is None
        assert ss["arm_strength"] == pytest.approx(85.0)
