"""
SIM-532 — Savant's outs above average and the outfield jump on the fielder
profile: the DuckDB migration, the canonical schema, the aggregator and the
league rows.

Plan: docs/audit/2026-09-17-sim532-fielder-hand-split-and-jump-plan.md (§4.2,
§5.2, §7).

The fielder INSERT is POSITIONAL (no column list), so the six new columns
go LAST, after ``asof_date``, in ``OAA_JUMP_COLUMN_ORDER``. The trap the plan
ranks first (§9): the SELECT tail and the table disagree and the values land
in the wrong columns. These tests hold the migration, the canonical DDL and
the SELECT to one tuple, ``FIELDER_TAIL_COLUMNS``.

What these tests hold:

* the aggregator, EXECUTED on an in-memory DuckDB with the canonical DDL and
  real rows: the outs above average is the figure AT the row's position (a
  player's CF and LF rows differ); the per-100 figure divides by our chances;
  an infield row has a Savant figure and NULL jump columns; a point-in-time
  build joins the prior season's board rows and divides by the prior season's
  chances (from the build, else from the surviving row, else NULL) and a
  live build joins the season's own; ``NULLIF`` guards a row with no chance;
* the table's last seven columns equal ``FIELDER_TAIL_COLUMNS``;
* migration 0030 is idempotent on the canonical schema and adds exactly
  ``OAA_JUMP_COLUMN_ORDER``; the version file reads 30 and equals the newest
  migration number;
* the league writer: every fielding position's row carries
  ``savant_oaa_per_100``; the three outfield rows also carry the three jump
  keys, and the infield rows do not; ``AVG`` skips NULL.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from pipeline.batch.player_profile_computor import (  # noqa: E402
    FIELDER_TAIL_COLUMNS,
    JUMP_ALPHA_PRIOR_PLAYS,
    OAA_JUMP_COLUMN_ORDER,
    LeagueAverageProfiles,
    PlayerProfileComputor,
)

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
MIGRATIONS = REPO / "db" / "migrations" / "duckdb"
MIGRATION_0030 = MIGRATIONS / "0030_sim532_fielder_oaa_jump.sql"
COMPUTOR = REPO / "pipeline" / "batch" / "player_profile_computor.py"

JUMP_COLUMNS = ("jump_reaction_ft", "jump_burst_ft", "jump_route_ft")
TAIL_SQL = ", ".join(OAA_JUMP_COLUMN_ORDER)

PLAYER = 11  # plays CF and LF
SHORTSTOP = 14
SECOND_BASEMAN = 15


def _real_ddl(table: str) -> str:
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    start = text.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = text.index("\n);", start)
    return text[start : end + 3]


def _placeholder_ddl(table: str) -> str:
    """The aggregator's own placeholder DDL for a ``_tmp_*`` table, copied
    from the source so the fixture's columns can never drift from it."""
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = src.index(")", start)
    return src[start : end + 1]


def _conn() -> duckdb.DuckDBPyConnection:
    """The SIM-542 fixture: the canonical fielder table, the four Savant
    boards the aggregator joins, and the six ``_tmp_*`` tables under the
    aggregator's own placeholder DDL."""
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.sprint_speed (player_id INTEGER, season SMALLINT, sprint_speed DOUBLE)"
    )
    c.execute(
        "CREATE TABLE pg.raw.savant_arm_strength (player_id INTEGER, season SMALLINT, "
        "arm_overall DOUBLE, arm_1b DOUBLE, arm_2b DOUBLE, arm_3b DOUBLE, arm_ss DOUBLE, "
        "arm_lf DOUBLE, arm_cf DOUBLE, arm_rf DOUBLE)"
    )
    c.execute(
        "CREATE TABLE pg.raw.savant_outs_above_average (player_id INTEGER, season INTEGER, "
        "position VARCHAR, primary_position VARCHAR, outs_above_average INTEGER, "
        "oaa_vs_rhh INTEGER, oaa_vs_lhh INTEGER)"
    )
    c.execute(
        "CREATE TABLE pg.raw.savant_outfield_jump (player_id INTEGER, season INTEGER, "
        "n_plays INTEGER, reaction_ft DOUBLE, burst_ft DOUBLE, route_ft DOUBLE)"
    )
    c.execute("CREATE SCHEMA derived")
    c.execute(_real_ddl("derived.fielder_season_metrics"))
    for table in (
        "_tmp_of_plays",
        "_tmp_if_plays",
        "_tmp_dp_plays",
        "_tmp_errors",
        "_tmp_bunt_defense",
        "_tmp_1b_scoop",
    ):
        c.execute(_placeholder_ddl(table))
    return c


# The plays: the two-position player has 40 center-field chances and 10
# left-field chances; the shortstop and the second baseman 20 each. Every
# chance is caught at probability 0.5 with credit 0.5, so the per-play detail
# does not matter here.
def _seed_plays(c: duckdb.DuckDBPyConnection, prior_cf_chances: int = 0) -> None:
    c.executemany(
        "INSERT INTO _tmp_of_plays VALUES (?, ?, 2024, 0.5, TRUE, 0.5, 'deep', 3)",
        [(PLAYER, "CF")] * 40 + [(PLAYER, "LF")] * 10,
    )
    # The prior season, when a test puts it in the build: the player's
    # center-field chances of 2023.
    if prior_cf_chances:
        c.executemany(
            "INSERT INTO _tmp_of_plays VALUES (?, ?, 2023, 0.5, TRUE, 0.5, 'deep', 3)",
            [(PLAYER, "CF")] * prior_cf_chances,
        )
    c.executemany(
        "INSERT INTO _tmp_if_plays VALUES (?, ?, 2024, 0.5, TRUE, 0.5, 'glove_side')",
        [(SHORTSTOP, "SS")] * 20 + [(SECOND_BASEMAN, "2B")] * 20,
    )


# The boards: the player's outs above average DIFFERS between center and left
# (the sharpest check of the positional join, plan §9), and the 2023 rows
# differ from the 2024 rows so a point-in-time build can be told apart.
_OAA_ROWS = [
    # (player_id, season, position, primary_position, oaa, vs_rhh, vs_lhh)
    (PLAYER, 2024, "CF", "CF", 8, 5, 3),
    (PLAYER, 2024, "LF", "CF", -2, -1, -1),
    (SHORTSTOP, 2024, "SS", "SS", 4, 3, 1),
    (PLAYER, 2023, "CF", "CF", 3, 2, 1),
    (PLAYER, 2023, "LF", "CF", 1, 1, 0),
    (SHORTSTOP, 2023, "SS", "SS", -5, -3, -2),
]
_JUMP_ROWS = [
    # (player_id, season, n_plays, reaction_ft, burst_ft, route_ft)
    (PLAYER, 2024, 40, 1.2, 0.4, -0.3),
    (PLAYER, 2023, 31, 0.6, 0.1, 0.2),
    # A shortstop with a jump row (he played some outfield): his infield row
    # must not read it.
    (SHORTSTOP, 2024, 5, 2.0, 2.0, 2.0),
]


def _seed_boards(c: duckdb.DuckDBPyConnection) -> None:
    c.executemany(
        "INSERT INTO pg.raw.savant_outs_above_average VALUES (?, ?, ?, ?, ?, ?, ?)", _OAA_ROWS
    )
    c.executemany("INSERT INTO pg.raw.savant_outfield_jump VALUES (?, ?, ?, ?, ?, ?)", _JUMP_ROWS)


def _computor(c: duckdb.DuckDBPyConnection) -> PlayerProfileComputor:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = c
    return comp


def _tail(c: duckdb.DuckDBPyConnection, player_id: int, position: str, season: int = 2024) -> tuple:
    return c.execute(
        f"SELECT opportunities, {TAIL_SQL} FROM derived.fielder_season_metrics "
        "WHERE player_id = ? AND position = ? AND season = ?",
        [player_id, position, season],
    ).fetchone()


def _built(
    asof: date | None = None,
    seasons: list[int] | None = None,
    prior_cf_chances: int = 0,
    stored_prior_chances: int | None = None,
) -> duckdb.DuckDBPyConnection:
    """Build the fielder rows. ``prior_cf_chances`` puts 2023 chances in the
    build; ``stored_prior_chances`` writes a 2023 row into the table by hand
    (a survivor of an earlier build, outside this run's seasons)."""
    c = _conn()
    _seed_plays(c, prior_cf_chances=prior_cf_chances)
    _seed_boards(c)
    if stored_prior_chances is not None:
        # The canonical DDL has many NOT NULL DEFAULT columns: name only
        # the columns the test needs. No stamp = a live build of a
        # completed season, which the SIM-551 guard accepts as whole.
        c.execute(
            "INSERT INTO derived.fielder_season_metrics "
            "(player_id, position, season, opportunities, outs_above_average, savant_oaa) "
            "VALUES (?, 'CF', 2023, ?, 1.0, 3)",
            [PLAYER, stored_prior_chances],
        )
    _computor(c)._aggregate_fielder_season_metrics(seasons or [2024], asof=asof)
    return c


# ===========================================================================
# 1. The aggregator, executed
# ===========================================================================


class TestAggregatorExecuted:
    def test_the_figure_is_the_one_at_the_rows_position(self) -> None:
        """The board is pulled once per position: the player's center-field
        row carries his center-field figure and his left-field row his
        left-field figure, and the two differ."""
        c = _built()
        cf = _tail(c, PLAYER, "CF")
        lf = _tail(c, PLAYER, "LF")
        assert cf[1] == 8
        assert lf[1] == -2
        assert cf[1] != lf[1]

    def test_the_per_100_figure_divides_by_our_chances(self) -> None:
        """The per-100 figure divides Savant's outs by our chances of the
        joined season: on a live build, the row's own."""
        c = _built()
        for position in ("CF", "LF"):
            opportunities, savant_oaa, per_100, *_ = _tail(c, PLAYER, position)
            assert per_100 == pytest.approx(savant_oaa * 100.0 / opportunities)
        assert _tail(c, PLAYER, "CF")[2] == pytest.approx(20.0)  # 8 * 100 / 40
        assert _tail(c, PLAYER, "LF")[2] == pytest.approx(-20.0)  # -2 * 100 / 10

    def test_an_outfield_row_carries_the_jump_and_its_plays(self) -> None:
        """One jump row serves both of the player's outfield positions."""
        c = _built()
        for position in ("CF", "LF"):
            row = _tail(c, PLAYER, position)
            assert row[3:] == (pytest.approx(1.2), pytest.approx(0.4), pytest.approx(-0.3), 40)

    def test_an_infield_row_has_a_figure_and_null_jump_columns(self) -> None:
        """The shortstop has a jump row on the board (he played some
        outfield), and his infield row still reads NULL for the jump."""
        c = _built()
        opportunities, savant_oaa, per_100, *jump = _tail(c, SHORTSTOP, "SS")
        assert (opportunities, savant_oaa) == (20, 4)
        assert per_100 == pytest.approx(20.0)
        assert jump == [None, None, None, None]

    def test_a_row_without_a_board_row_reads_null_everywhere_new(self) -> None:
        """The second baseman has no Savant row: NULL, never 0."""
        c = _built()
        row = _tail(c, SECOND_BASEMAN, "2B")
        assert row[0] == 20
        assert row[1:] == (None,) * len(OAA_JUMP_COLUMN_ORDER)

    def test_a_live_build_joins_the_seasons_own_board_rows(self) -> None:
        c = _built(asof=None)
        assert _tail(c, PLAYER, "CF")[1] == 8
        assert _tail(c, PLAYER, "CF")[3] == pytest.approx(1.2)
        assert _tail(c, SHORTSTOP, "SS")[1] == 4

    def test_a_point_in_time_build_joins_the_prior_seasons_board_rows(self) -> None:
        """A cutoff inside 2024 joins the 2023 rows of both boards (the
        season-shift substitution: the boards carry no date). The shifted
        numerator takes the shifted denominator: the per-100 figure divides
        the 2023 outs by the 2023 chances, here from the build (2023 is in
        the run), never by the 40 partial chances of 2024. The 2023 row
        divides by its own chances."""
        c = _built(asof=date(2024, 6, 1), seasons=[2023, 2024], prior_cf_chances=30)
        cf = _tail(c, PLAYER, "CF")
        assert cf[0] == 40
        assert cf[1] == 3
        assert cf[2] == pytest.approx(3 * 100.0 / 30)
        assert cf[2] != pytest.approx(3 * 100.0 / 40)
        assert cf[3:] == (pytest.approx(0.6), pytest.approx(0.1), pytest.approx(0.2), 31)
        assert _tail(c, PLAYER, "LF")[1] == 1
        assert _tail(c, SHORTSTOP, "SS")[1] == -5
        prior = _tail(c, PLAYER, "CF", season=2023)
        assert prior[0] == 30
        assert prior[1] == 3
        assert prior[2] == pytest.approx(3 * 100.0 / 30)

    def test_a_point_in_time_build_reads_the_prior_chances_from_the_surviving_row(
        self,
    ) -> None:
        """The prior season is NOT in the build, but its row already sits in
        the table (a survivor): the denominator is that row's chances."""
        c = _built(asof=date(2024, 6, 1), seasons=[2024], stored_prior_chances=30)
        cf = _tail(c, PLAYER, "CF")
        assert cf[0] == 40
        assert cf[1] == 3
        assert cf[2] == pytest.approx(3 * 100.0 / 30)
        # The survivor keeps its chances (only its stamp changes).
        assert _tail(c, PLAYER, "CF", season=2023)[0] == 30

    def test_a_point_in_time_build_without_prior_chances_reads_null_per_100(self) -> None:
        """Neither the build nor the table holds the prior season: the outs
        are known, the per-100 figure is NULL (never a mixed-season
        quotient), and the model reads the league mean for it."""
        c = _built(asof=date(2024, 6, 1), seasons=[2024])
        cf = _tail(c, PLAYER, "CF")
        assert cf[0] == 40
        assert cf[1] == 3
        assert cf[2] is None
        assert _tail(c, SHORTSTOP, "SS")[1] == -5
        assert _tail(c, SHORTSTOP, "SS")[2] is None

    def test_a_live_build_divides_by_the_rows_own_chances_only(self) -> None:
        """With asof None the denominator is the row's own chances even when
        a prior season is in the build."""
        c = _built(asof=None, seasons=[2023, 2024], prior_cf_chances=30)
        assert _tail(c, PLAYER, "CF")[2] == pytest.approx(8 * 100.0 / 40)
        assert _tail(c, PLAYER, "CF", season=2023)[2] == pytest.approx(3 * 100.0 / 30)

    def test_the_per_100_figure_is_null_at_zero_chances(self) -> None:
        """A row with zero opportunities cannot occur (a row exists because a
        chance did), so the guard is held in the SQL text: NULLIF turns the
        division into NULL rather than an error."""
        body = _aggregator_body()
        assert "NULLIF({savant_denominator}, 0)" in body
        assert "soaa.outs_above_average * 100.0 / NULLIF({savant_denominator}, 0)" in body
        # The live expression is the row's own chances; the cutoff expression
        # follows the numerator's season.
        assert 'else "c.opportunities"' in body
        assert "COALESCE(prev.opportunities, stored.opportunities)" in body

    def test_the_hand_split_is_stored_and_read_by_nothing(self) -> None:
        """Finding 1: the left-minus-right gap repeats at 0.04 to 0.14 for
        outfielders and, within the infield, at shortstop alone (0.36 to
        0.38). The raw table keeps the two columns; the profile has none."""
        assert "oaa_vs_lhh" not in _aggregator_body()
        assert "oaa_vs_rhh" not in _aggregator_body()
        assert not any("vs_lhh" in col or "vs_rhh" in col for col in OAA_JUMP_COLUMN_ORDER)


def _aggregator_body() -> str:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _aggregate_fielder_season_metrics")
    return src[start : src.index("def _assert_fielder_profiles_have_no_leakage", start)]


# ===========================================================================
# 2. The positional-insert contract
# ===========================================================================


class TestTailContract:
    def test_the_tuple_is_the_plans_order(self) -> None:
        assert OAA_JUMP_COLUMN_ORDER == (
            "savant_oaa",
            "savant_oaa_per_100",
            "jump_reaction_ft",
            "jump_burst_ft",
            "jump_route_ft",
            "jump_plays",
        )
        assert ("asof_date", *OAA_JUMP_COLUMN_ORDER) == FIELDER_TAIL_COLUMNS

    def test_the_canonical_tables_last_seven_columns_are_the_tail(self) -> None:
        """The DDL's trailing columns, by ordinal position, are exactly
        ``FIELDER_TAIL_COLUMNS`` — the schema and the SELECT can never
        desync."""
        c = _conn()
        cols = [
            r[0]
            for r in c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'derived' AND table_name = 'fielder_season_metrics' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        assert tuple(cols[-len(FIELDER_TAIL_COLUMNS) :]) == FIELDER_TAIL_COLUMNS

    def test_the_select_tail_names_the_columns_in_order(self) -> None:
        body = _aggregator_body()
        # ``\b`` so "AS savant_oaa" cannot match inside "AS savant_oaa_per_100".
        positions = [
            re.search(rf"AS {re.escape(col)}\b", body).start() for col in FIELDER_TAIL_COLUMNS
        ]
        assert positions == sorted(positions)
        assert body.index("ss.sprint_speed AS sprint_speed") < positions[0]
        assert positions[-1] < body.index("FROM combined_oaa c")

    def test_the_jump_columns_are_guarded_to_the_outfield(self) -> None:
        body = _aggregator_body()
        for col in (*JUMP_COLUMNS, "jump_plays"):
            at = re.search(rf"AS {re.escape(col)}\b", body).start()
            line = body[body.rindex("\n", 0, at) : at]
            assert "CASE WHEN {_SQL_IS_OF} THEN sj." in line, col

    def test_the_oaa_join_carries_the_position(self) -> None:
        body = _aggregator_body()
        join = body[body.index("LEFT JOIN pg.raw.savant_outs_above_average soaa") :]
        join = join[: join.index("LEFT JOIN pg.raw.savant_outfield_jump sj")]
        assert "soaa.position = c.position" in join
        assert "soaa.season = {savant_season}" in join

    def test_the_shared_prior_is_twenty_five_plays(self) -> None:
        assert JUMP_ALPHA_PRIOR_PLAYS == 25


# ===========================================================================
# 3. The migration and the version file
# ===========================================================================


def _migration_statements() -> str:
    return "\n".join(
        line
        for line in MIGRATION_0030.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    )


class TestMigration0030:
    def test_it_adds_exactly_the_six_columns_in_order(self) -> None:
        sql = MIGRATION_0030.read_text(encoding="utf-8")
        added = re.findall(r"^ALTER TABLE (\S+) ADD COLUMN IF NOT EXISTS (\S+)", sql, re.M)
        assert added == [("derived.fielder_season_metrics", col) for col in OAA_JUMP_COLUMN_ORDER]
        assert "DROP" not in sql.upper().replace("DROPPED", "")

    def test_it_is_idempotent_on_the_canonical_schema(self) -> None:
        """Applied twice to a database at the canonical schema (which already
        carries the six), the migration changes nothing and raises nothing."""
        c = duckdb.connect(":memory:")
        c.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        before = c.execute("DESCRIBE derived.fielder_season_metrics").fetchall()
        for _ in range(2):
            c.execute(_migration_statements())
        after = c.execute("DESCRIBE derived.fielder_season_metrics").fetchall()
        assert after == before

    def test_it_adds_the_tail_to_a_v29_table(self) -> None:
        """A table without the six (the live one before the recompute) gains
        them LAST, after asof_date, in OAA_JUMP_COLUMN_ORDER."""
        ddl = _real_ddl("derived.fielder_season_metrics")
        start = ddl.index("    -- SIM-532 (migration 0030)")
        end = ddl.index("\n    PRIMARY KEY")
        v29_ddl = ddl[:start] + ddl[end + 1 :]
        for col in OAA_JUMP_COLUMN_ORDER:
            assert col not in v29_ddl
        c = duckdb.connect(":memory:")
        c.execute("CREATE SCHEMA derived")
        c.execute(v29_ddl)
        c.execute(_migration_statements())
        cols = [r[0] for r in c.execute("DESCRIBE derived.fielder_season_metrics").fetchall()]
        assert tuple(cols[-len(FIELDER_TAIL_COLUMNS) :]) == FIELDER_TAIL_COLUMNS

    def test_the_version_file_reads_30(self) -> None:
        assert VERSION_FILE.read_text(encoding="utf-8").strip() == "30"

    def test_the_version_file_equals_the_newest_migration(self) -> None:
        newest = max(int(p.name[:4]) for p in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        assert newest == 30
        assert int(VERSION_FILE.read_text(encoding="utf-8").strip()) == newest


# ===========================================================================
# 4. The league writer
# ===========================================================================


class TestLeagueWriter:
    def _rows(self, tmp_path: Path) -> dict[str, dict]:
        path = str(tmp_path / "sim532.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        con.execute(
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
            "outs_above_average, error_rate, dp_run_value, below_minimum_sample, "
            f"{TAIL_SQL}) VALUES "
            # Two center fielders above the floor: one without a jump row
            # (AVG skips him on the jump keys), one with.
            "(1, 'CF', 2024, 2.0, 0.01, NULL, FALSE, 6, 3.0, NULL, NULL, NULL, NULL), "
            "(2, 'CF', 2024, -1.0, 0.02, NULL, FALSE, -2, -1.0, 1.2, 0.4, -0.3, 40), "
            # Below the floor: excluded, as every other key is.
            "(3, 'CF', 2024, 0.0, 0.03, NULL, TRUE, 20, 50.0, 9.0, 9.0, 9.0, 90), "
            # Two shortstops: one without a Savant figure.
            "(4, 'SS', 2024, 1.0, 0.01, 0.5, FALSE, 4, 1.5, NULL, NULL, NULL, NULL), "
            "(5, 'SS', 2024, 1.0, 0.01, 0.5, FALSE, NULL, NULL, NULL, NULL, NULL, NULL), "
            "(6, 'C', 2024, 0.0, 0.01, NULL, FALSE, NULL, NULL, NULL, NULL, NULL, NULL)"
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
        return rows

    def test_the_outfield_row_carries_the_four_keys(self, tmp_path: Path) -> None:
        cf = self._rows(tmp_path)["fielder_CF"]
        assert cf["savant_oaa_per_100"] == pytest.approx(1.0)  # (3.0 + -1.0) / 2
        # AVG skipped the center fielder without a jump row.
        assert cf["jump_reaction_ft"] == pytest.approx(1.2)
        assert cf["jump_burst_ft"] == pytest.approx(0.4)
        assert cf["jump_route_ft"] == pytest.approx(-0.3)

    def test_the_infield_row_carries_the_figure_and_no_jump_key(self, tmp_path: Path) -> None:
        ss = self._rows(tmp_path)["fielder_SS"]
        assert ss["savant_oaa_per_100"] == pytest.approx(1.5)  # AVG skipped the NULL
        for key in JUMP_COLUMNS:
            assert key not in ss

    def test_the_catcher_row_carries_none_of_the_new_keys(self, tmp_path: Path) -> None:
        catcher = self._rows(tmp_path)["fielder_C"]
        for key in ("savant_oaa_per_100", *JUMP_COLUMNS):
            assert key not in catcher

    def test_a_table_without_the_columns_still_gets_its_rows(self, tmp_path: Path) -> None:
        """A database that has not run migration 0030: the keys are skipped,
        the rows are written."""
        path = str(tmp_path / "v29.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        # The two indexes on the table block DROP COLUMN; the writer does
        # not need them.
        con.execute("DROP INDEX derived.idx_fsm_season")
        con.execute("DROP INDEX derived.idx_fsm_position")
        for col in OAA_JUMP_COLUMN_ORDER:
            con.execute(f"ALTER TABLE derived.fielder_season_metrics DROP COLUMN {col}")
        con.execute(
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
            "outs_above_average, error_rate, below_minimum_sample) "
            "VALUES (1, 'CF', 2024, 2.0, 0.01, FALSE)"
        )
        con.close()
        LeagueAverageProfiles(path).compute([2024])
        con = duckdb.connect(path, read_only=True)
        pj = con.execute(
            "SELECT profile_json FROM derived.league_averages WHERE entity_type = 'fielder_CF'"
        ).fetchone()[0]
        con.close()
        row = json.loads(pj) if isinstance(pj, str) else pj
        assert row["outs_above_average"] == pytest.approx(2.0)
        assert "savant_oaa_per_100" not in row
        assert "jump_reaction_ft" not in row
