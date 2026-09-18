"""
SIM-532 — the recompute script for Savant's per-position outs above average and the
outfield jump in the fielder profile.

Plan: docs/audit/2026-09-17-sim532-fielder-hand-split-and-jump-plan.md (§5.5 the script).

What these tests hold, on an in-memory DuckDB with the canonical schema:

* each verify check passes on a fielder table that matches the raw rows and names
  the defect when the table drifts: a tail out of order, a thin season, a jump on
  an infield row, a per-100 figure off the count (and a thin row whose per-100
  figure rounds in the 32-bit FLOAT column passes), a Savant figure that does
  not match the raw row at the position, a jump on a player without a jump row,
  a league row without its keys, a mixed cutoff;
* the fielder chain runs the computor's methods in the nightly order — the
  run-expectancy matrix before the double plays, the arm fill after the
  aggregator — and closes the computor;
* the migration applier drops the comment lines, splits on ';' and is idempotent;
* ``main`` refuses a season list that omits the season in progress unless
  ``--allow-partial`` is passed, and ``--skip-profiles`` runs no computor method.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"
_SCRIPTS = REPO / "scripts"


def _load(name: str):
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


recompute = _load("sim532_fielder_recompute")

TODAY = date(2026, 9, 17)
STAMP = date(2026, 9, 17)

#: The six columns, typed as migration 0030 declares them. The canonical DDL
#: gains them in the same ticket (track B); the fixture adds them IF NOT EXISTS
#: so it builds on either copy of the schema.
_SIX_COLUMNS = (
    ("savant_oaa", "INTEGER"),
    ("savant_oaa_per_100", "FLOAT"),
    ("jump_reaction_ft", "FLOAT"),
    ("jump_burst_ft", "FLOAT"),
    ("jump_route_ft", "FLOAT"),
    ("jump_plays", "INTEGER"),
)

# ---------------------------------------------------------------------------
# The fixture: 2024 — a center fielder who also plays right (his Savant figure
# differs by position; the most jump plays), a left fielder, a center fielder
# with no jump row, four infielders, a catcher, and two thin infielders whose
# per-100 figure (100/3 and 200/6 = 33.333...) does not fit a 32-bit FLOAT
# exactly — the per-100 check must compare at the column's own precision.
# ---------------------------------------------------------------------------

#: (player_id, position, opportunities, savant_oaa, reaction, burst, route, plays)
_ROWS = (
    (101, "CF", 200, 6, 1.2, 0.5, -0.3, 60),
    (101, "RF", 100, 2, 1.2, 0.5, -0.3, 60),
    (102, "LF", 150, 3, 0.4, 0.1, 0.2, 30),
    (103, "CF", 80, -1, None, None, None, None),
    (400, "SS", 300, 4, None, None, None, None),
    (500, "1B", 120, 1, None, None, None, None),
    (600, "2B", 100, 0, None, None, None, None),
    (700, "3B", 100, -2, None, None, None, None),
    (800, "SS", 3, 1, None, None, None, None),
    (801, "2B", 6, 2, None, None, None, None),
    (300, "C", 0, None, None, None, None, None),
)
#: The two thin rows: their per-100 figure rounds when the FLOAT column stores it.
_THIN_ROWS = (800, 801)


def _raw() -> recompute.RawFieldingRows:
    return recompute.RawFieldingRows(
        jump={
            101: (1.2, 0.5, -0.3, 60),
            102: (0.4, 0.1, 0.2, 30),
            999: (0.9, 0.9, 0.9, 10),  # a jump row without a fielder row: not the most plays
        },
        oaa={
            (101, "CF"): 6,
            (101, "RF"): 2,
            (102, "LF"): 3,
            (103, "CF"): -1,
            (400, "SS"): 4,
            (500, "1B"): 1,
            (600, "2B"): 0,
            (700, "3B"): -2,
            (800, "SS"): 1,
            (801, "2B"): 2,
        },
    )


def _schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    for col, typ in _SIX_COLUMNS:
        con.execute(
            f"ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS {col} {typ}"
        )


def _insert_rows(con: duckdb.DuckDBPyConnection, season: int = 2024, stamp: date = STAMP) -> None:
    con.executemany(
        "INSERT INTO derived.fielder_season_metrics (player_id, position, season, opportunities, "
        "savant_oaa, savant_oaa_per_100, jump_reaction_ft, jump_burst_ft, jump_route_ft, "
        "jump_plays, below_minimum_sample, asof_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                pid,
                pos,
                season,
                opp,
                oaa,
                (oaa * 100.0 / opp) if oaa is not None and opp else None,
                reaction,
                burst,
                route,
                plays,
                False,
                stamp,
            )
            for pid, pos, opp, oaa, reaction, burst, route, plays in _ROWS
        ],
    )


def _seed_league_rows(
    con: duckdb.DuckDBPyConnection, season: int = 2024, overrides: dict | None = None
) -> None:
    overrides = overrides or {}
    con.execute(recompute.LeagueAverageProfiles.LEAGUE_AVG_DDL)
    for pos in recompute.FIELDING_POSITIONS:
        profile = {"outs_above_average": 0.5, "error_rate": 0.01, "savant_oaa_per_100": 1.5}
        if pos in recompute.OUTFIELD:
            profile.update({"jump_reaction_ft": 0.6, "jump_burst_ft": 0.2, "jump_route_ft": 0.1})
        profile.update(overrides.get(pos, {}))
        for k in overrides.get(("drop", pos), ()):
            profile.pop(k, None)
        con.execute(
            "INSERT OR REPLACE INTO derived.league_averages VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            [f"fielder_{pos}", season, json.dumps(profile)],
        )


def _fixture() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    _schema(con)
    _insert_rows(con)
    _seed_league_rows(con)
    return con


@pytest.fixture
def small_floors(monkeypatch):
    """The coverage floors are sized for a real season; the fixture has eight
    fielder-position rows and two outfielders with a jump."""
    monkeypatch.setattr(recompute, "OAA_FLOOR_FULL", 2)
    monkeypatch.setattr(recompute, "OAA_FLOOR_2020", 1)
    monkeypatch.setattr(recompute, "OAA_FLOOR_CURRENT", 1)
    monkeypatch.setattr(recompute, "JUMP_FLOOR_FULL", 2)
    monkeypatch.setattr(recompute, "JUMP_FLOOR_2020", 1)
    monkeypatch.setattr(recompute, "JUMP_FLOOR_CURRENT", 1)


# ---------------------------------------------------------------------------
# The floors and the migration applier
# ---------------------------------------------------------------------------


class TestFloorsAndMigration:
    def test_the_floors_by_season(self) -> None:
        assert recompute.oaa_floor(2024, TODAY) == recompute.OAA_FLOOR_FULL
        assert recompute.oaa_floor(2020, TODAY) == recompute.OAA_FLOOR_2020
        assert recompute.oaa_floor(2026, TODAY) == recompute.OAA_FLOOR_CURRENT
        assert recompute.jump_floor(2024, TODAY) == recompute.JUMP_FLOOR_FULL
        assert recompute.jump_floor(2020, TODAY) == recompute.JUMP_FLOOR_2020
        assert recompute.jump_floor(2026, TODAY) == recompute.JUMP_FLOOR_CURRENT

    def test_the_applier_drops_comment_lines_and_splits_statements(self) -> None:
        text = (
            "-- 0030 — a comment; with a semicolon\n"
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS a INTEGER;  -- trailing\n"
            "-- a comment between two statements\n"
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS b FLOAT;\n"
        )
        statements = recompute.migration_statements(text)
        assert len(statements) == 2
        assert statements[0].startswith("ALTER TABLE t ADD COLUMN IF NOT EXISTS a")
        assert statements[1].startswith("ALTER TABLE t ADD COLUMN IF NOT EXISTS b")

    def test_the_applier_is_idempotent_on_a_file_database(self, tmp_path) -> None:
        path = str(tmp_path / "sim532.duckdb")
        con = duckdb.connect(path)
        con.execute("CREATE SCHEMA derived; CREATE TABLE derived.t (x INTEGER)")
        con.close()
        migration = tmp_path / "0030_test.sql"
        migration.write_text(
            "-- the test migration\n"
            "ALTER TABLE derived.t ADD COLUMN IF NOT EXISTS savant_oaa INTEGER;\n"
            "ALTER TABLE derived.t ADD COLUMN IF NOT EXISTS jump_plays INTEGER;\n",
            encoding="utf-8",
        )
        assert recompute.apply_migration(path, migration) == 2
        assert recompute.apply_migration(path, migration) == 2
        con = duckdb.connect(path, read_only=True)
        cols = [r[0] for r in con.execute("DESCRIBE derived.t").fetchall()]
        con.close()
        assert cols == ["x", "savant_oaa", "jump_plays"]

    def test_the_real_migration_file_applies_on_the_canonical_schema(self, tmp_path) -> None:
        """The 0030 file applies on the canonical schema and leaves the tail in
        the contract's order. ``apply_migration`` reads the file itself: a
        missing file fails here, loudly."""
        path = str(tmp_path / "sim532_real.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        con.close()
        recompute.apply_migration(path)
        con = duckdb.connect(path, read_only=True)
        try:
            assert recompute.check_tail_order(con) == []
        finally:
            con.close()


# ---------------------------------------------------------------------------
# The verify checks
# ---------------------------------------------------------------------------


class TestVerifyChecks:
    def test_a_table_that_matches_the_raw_rows_passes_every_check(self, small_floors) -> None:
        con = _fixture()
        assert recompute.check_tail_order(con) == []
        assert recompute.check_coverage(con, [2024], TODAY) == []
        assert recompute.check_jump_outfield_only(con) == []
        assert recompute.check_per_100(con) == []
        assert recompute.check_named_players(con, _raw(), 2024) == []
        assert recompute.check_league_rows(con, [2024]) == []
        assert recompute.check_one_asof(con) == []

    def test_the_tail_order_is_the_contract(self) -> None:
        con = _fixture()
        assert recompute.check_tail_order(con) == []
        con.execute("ALTER TABLE derived.fielder_season_metrics ADD COLUMN extra INTEGER")
        problems = recompute.check_tail_order(con)
        assert len(problems) == 1 and "positional INSERT" in problems[0]
        assert "extra" in problems[0]

    def test_the_coverage_check_names_a_missing_season_and_a_thin_one(
        self, small_floors, monkeypatch
    ) -> None:
        con = _fixture()
        problems = recompute.check_coverage(con, [2023, 2024], TODAY)
        assert problems == ["season 2023: no fielder-position rows at all"]
        monkeypatch.setattr(recompute, "OAA_FLOOR_FULL", 100)
        monkeypatch.setattr(recompute, "JUMP_FLOOR_FULL", 100)
        problems = recompute.check_coverage(con, [2024], TODAY)
        assert len(problems) == 2
        assert "10 of 10 fielder-position rows carry a Savant figure (floor 100)" in problems[0]
        assert "3 of 4 outfield rows carry a jump (floor 100)" in problems[1]

    def test_a_season_outside_the_run_is_logged_not_gated(self, monkeypatch) -> None:
        con = _fixture()
        monkeypatch.setattr(recompute, "OAA_FLOOR_FULL", 100)
        assert recompute.check_coverage(con, [2025], TODAY) == [
            "season 2025: no fielder-position rows at all"
        ]

    def test_a_jump_on_an_infield_row_is_a_problem(self) -> None:
        con = _fixture()
        con.execute(
            "UPDATE derived.fielder_season_metrics SET jump_plays = 12 WHERE player_id = 400"
        )
        problems = recompute.check_jump_outfield_only(con)
        assert len(problems) == 1 and problems[0].startswith("1 infield, catcher or pitcher rows")

    def test_a_per_100_off_the_count_is_a_problem(self) -> None:
        con = _fixture()
        con.execute(
            "UPDATE derived.fielder_season_metrics SET savant_oaa_per_100 = 9.9 "
            "WHERE player_id = 102"
        )
        problems = recompute.check_per_100(con)
        assert len(problems) == 1 and "not savant_oaa * 100 / opportunities" in problems[0]

    def test_a_thin_row_passes_the_per_100_check_at_float_precision(self) -> None:
        """The column is a 32-bit FLOAT: 100/3 and 200/6 round when stored, so
        the stored value sits about 1e-6 from the DOUBLE recomputation. The
        check compares at the column's precision and passes a correct row."""
        con = _fixture()
        stored = con.execute(
            "SELECT player_id, savant_oaa_per_100, savant_oaa * 100.0 / opportunities "
            "FROM derived.fielder_season_metrics WHERE player_id IN (800, 801) ORDER BY 1"
        ).fetchall()
        assert [r[0] for r in stored] == list(_THIN_ROWS)
        for _pid, kept, exact in stored:
            assert kept != exact, "the FLOAT column must round the figure for this test to bite"
            assert abs(kept - exact) < 1e-5
        assert recompute.check_per_100(con) == []
        # A real drift on a thin row still reads as a problem.
        con.execute(
            "UPDATE derived.fielder_season_metrics SET savant_oaa_per_100 = 33.4 "
            "WHERE player_id = 800"
        )
        problems = recompute.check_per_100(con)
        assert len(problems) == 1 and problems[0].startswith("1 rows carry a savant_oaa_per_100")

    def test_a_per_100_without_a_count_is_a_problem(self) -> None:
        con = _fixture()
        con.execute(
            "UPDATE derived.fielder_season_metrics SET savant_oaa_per_100 = 0.0 "
            "WHERE player_id = 300"
        )
        problems = recompute.check_per_100(con)
        assert len(problems) == 1 and "with no savant_oaa" in problems[0]

    def test_the_named_check_reads_the_figure_at_the_position(self) -> None:
        con = _fixture()
        # The right-field row reads the center-field figure: the join is not per position.
        con.execute(
            "UPDATE derived.fielder_season_metrics SET savant_oaa = 6 "
            "WHERE player_id = 101 AND position = 'RF'"
        )
        problems = recompute.check_named_players(con, _raw(), 2024)
        assert len(problems) == 1
        assert "RF 101 savant_oaa = 6, the raw row at RF says 2" in problems[0]

    def test_the_named_check_reads_the_jump_value_for_value(self) -> None:
        con = _fixture()
        con.execute(
            "UPDATE derived.fielder_season_metrics SET jump_burst_ft = 0.75 "
            "WHERE player_id = 101 AND position = 'CF'"
        )
        problems = recompute.check_named_players(con, _raw(), 2024)
        assert problems == ["season 2024: CF 101 jump_burst_ft = 0.75, the raw jump row says 0.5"]

    def test_the_named_check_catches_a_jump_on_a_player_without_a_jump_row(self) -> None:
        con = _fixture()
        con.execute(
            "UPDATE derived.fielder_season_metrics SET jump_reaction_ft = 0.1, jump_plays = 5 "
            "WHERE player_id = 103"
        )
        problems = recompute.check_named_players(con, _raw(), 2024)
        assert len(problems) == 1 and "CF 103 has no raw jump row and carries a jump" in problems[0]

    def test_the_named_check_reports_the_most_plays_player_without_a_row(self) -> None:
        con = _fixture()
        con.execute("DELETE FROM derived.fielder_season_metrics WHERE player_id = 101")
        problems = recompute.check_named_players(con, _raw(), 2024)
        assert problems == [
            "season 2024: player 101 has the most jump plays (60) and no outfield fielder row"
        ]

    def test_the_named_check_skips_a_candidate_it_cannot_find(self) -> None:
        """One outfielder (102, LF) with a jump row and a matching fielder row:
        the most-jump-plays check reads him value for value and passes. The
        two-position candidate and the no-jump-row candidate do not exist (101
        and 103 are deleted), so those two checks log a line and skip. No
        problem."""
        con = _fixture()
        raw = recompute.RawFieldingRows(jump={102: (0.4, 0.1, 0.2, 30)}, oaa={(102, "LF"): 3})
        con.execute("DELETE FROM derived.fielder_season_metrics WHERE player_id IN (101, 103)")
        assert recompute.check_named_players(con, raw, 2024) == []

    def test_the_league_rows_need_their_keys(self) -> None:
        con = _fixture()
        _seed_league_rows(con, overrides={("drop", "CF"): ("jump_route_ft",)})
        problems = recompute.check_league_rows(con, [2024])
        assert problems == [
            "'fielder_CF' league row 2024 lacks a finite value for ['jump_route_ft'] — "
            "the shrinkage has no target"
        ]
        _seed_league_rows(con, overrides={"SS": {"savant_oaa_per_100": None}})
        problems = recompute.check_league_rows(con, [2024])
        assert len(problems) == 1 and "'fielder_SS'" in problems[0]

    def test_an_infield_row_does_not_need_the_jump_keys(self) -> None:
        con = _fixture()
        _seed_league_rows(con, overrides={("drop", "1B"): ("jump_reaction_ft",)})
        assert recompute.check_league_rows(con, [2024]) == []

    def test_a_missing_league_row_for_a_season_with_data(self) -> None:
        con = _fixture()
        con.execute("DELETE FROM derived.league_averages WHERE entity_type = 'fielder_LF'")
        problems = recompute.check_league_rows(con, [2024])
        assert problems == ["no 'fielder_LF' league-average row for season 2024"]

    def test_a_mixed_cutoff_is_a_problem(self) -> None:
        con = _fixture()
        assert recompute.check_one_asof(con) == []
        con.execute(
            "UPDATE derived.fielder_season_metrics SET asof_date = DATE '2026-09-16' "
            "WHERE player_id = 400"
        )
        problems = recompute.check_one_asof(con)
        assert len(problems) == 1 and "2 distinct asof_date" in problems[0]

    def test_verify_runs_every_check_and_raises_on_a_problem(self, tmp_path, small_floors) -> None:
        path = str(tmp_path / "sim532_verify.duckdb")
        con = duckdb.connect(path)
        _schema(con)
        _insert_rows(con)
        _seed_league_rows(con)
        con.close()
        recompute.verify(path, [2024], raw=_raw())
        con = duckdb.connect(path)
        con.execute(
            "UPDATE derived.fielder_season_metrics SET jump_plays = 3 WHERE player_id = 500"
        )
        con.close()
        with pytest.raises(SystemExit, match="1 verification problem"):
            recompute.verify(path, [2024], raw=_raw())

    def test_verify_skips_the_named_checks_without_a_dsn(
        self, tmp_path, small_floors, monkeypatch
    ) -> None:
        path = str(tmp_path / "sim532_nodsn.duckdb")
        con = duckdb.connect(path)
        _schema(con)
        _insert_rows(con)
        _seed_league_rows(con)
        # A drift only the named checks would catch.
        con.execute(
            "UPDATE derived.fielder_season_metrics SET savant_oaa = 6, savant_oaa_per_100 = 6.0 "
            "WHERE player_id = 101 AND position = 'RF'"
        )
        con.close()
        reader = MagicMock(side_effect=AssertionError("no DSN: the raw read must not run"))
        monkeypatch.setattr(recompute, "read_raw_fielding_rows", reader)
        recompute.verify(path, [2024], dsn="")
        reader.assert_not_called()

    def test_verify_stops_at_a_wrong_tail(self, tmp_path) -> None:
        """The other checks read the six columns; a table without them reports
        the tail alone instead of a stack of binder errors."""
        path = str(tmp_path / "sim532_tail.duckdb")
        con = duckdb.connect(path)
        _schema(con)
        con.execute("ALTER TABLE derived.fielder_season_metrics ADD COLUMN extra INTEGER")
        con.close()
        with pytest.raises(SystemExit, match="1 verification problem"):
            recompute.verify(path, [2024], raw=_raw())


# ---------------------------------------------------------------------------
# The fielder chain
# ---------------------------------------------------------------------------


class TestFielderChain:
    def test_the_chain_runs_the_computor_methods_in_the_nightly_order(self, monkeypatch) -> None:
        computor = MagicMock()
        re_builder = MagicMock(return_value={(0, 0): 0.5})
        computor.attach_mock(re_builder, "re_builder")
        monkeypatch.setattr(recompute, "build_run_expectancy_matrix", re_builder)

        recompute.run_fielder_chain("postgresql://x", "/tmp/y.duckdb", [2024], computor=computor)

        names = [c[0] for c in computor.mock_calls]
        assert names == [
            "_connect",
            "re_builder",
            "_compute_outfield_catch_probability",
            "_compute_infield_oaa",
            "_compute_dp_metrics",
            "_compute_bunt_defense",
            "_compute_first_base_scooping",
            "_compute_error_decomposition",
            "_aggregate_fielder_season_metrics",
            "_fill_outfield_arm_block",
            "_close",
        ]
        re_builder.assert_called_once_with(computor._conn, [2024], asof=None)
        assert computor._re_matrix == {(0, 0): 0.5}
        for name in names[2:-1]:
            getattr(computor, name).assert_called_once_with([2024], asof=None)

    def test_the_chain_closes_the_computor_when_a_step_raises(self, monkeypatch) -> None:
        computor = MagicMock()
        monkeypatch.setattr(recompute, "build_run_expectancy_matrix", MagicMock(return_value={}))
        computor._compute_dp_metrics.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            recompute.run_fielder_chain("dsn", "path", [2024], computor=computor)
        computor._close.assert_called_once()
        computor._aggregate_fielder_season_metrics.assert_not_called()


# ---------------------------------------------------------------------------
# main: the season-list refusal, --allow-partial, --skip-profiles
# ---------------------------------------------------------------------------


def _stub_main_steps(monkeypatch, tmp_path) -> dict[str, MagicMock]:
    """Every step past the argument checks, stubbed: the lock probe, the
    migration, the chain, the league writer, the verify. Every stub is attached
    to ``stubs["parent"]`` so a test can read the order of the calls."""
    parent = MagicMock()
    stubs = {
        "connect": MagicMock(return_value=MagicMock()),
        "migration": MagicMock(return_value=6),
        "chain": MagicMock(),
        "league": MagicMock(),
        "verify": MagicMock(),
        "computor": MagicMock(side_effect=AssertionError("no computor with --skip-profiles")),
    }
    for name, stub in stubs.items():
        parent.attach_mock(stub, name)
    monkeypatch.setattr(recompute, "_connect_writable", stubs["connect"])
    monkeypatch.setattr(recompute, "apply_migration", stubs["migration"])
    monkeypatch.setattr(recompute, "run_fielder_chain", stubs["chain"])
    monkeypatch.setattr(recompute, "LeagueAverageProfiles", stubs["league"])
    monkeypatch.setattr(recompute, "verify", stubs["verify"])
    monkeypatch.setattr(recompute, "PlayerProfileComputor", stubs["computor"])
    stubs["parent"] = parent
    return stubs


def _assert_main_order(stubs: dict[str, MagicMock], *, with_chain: bool) -> None:
    """The migration runs before the chain, the chain before the league rows,
    the league rows before the verify. The league stub is a class: its
    constructor call is the first ``league`` entry, its ``compute`` follows as
    ``league().compute``; ``index`` on the first occurrence handles it."""
    names = [c[0] for c in stubs["parent"].mock_calls]
    if with_chain:
        assert (
            names.index("migration")
            < names.index("chain")
            < names.index("league")
            < names.index("verify")
        )
    else:
        assert names.index("migration") < names.index("league") < names.index("verify")
        assert "chain" not in names


class TestMain:
    def test_missing_current_season(self) -> None:
        assert recompute.missing_current_season([2023, 2024], TODAY) == 2026
        assert recompute.missing_current_season([2024, 2026], TODAY) is None

    def test_main_refuses_a_season_list_without_the_season_in_progress(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        stubs = _stub_main_steps(monkeypatch, tmp_path)
        last_year = date.today().year - 1
        rc = recompute.main(
            ["--seasons", str(last_year - 1), str(last_year), "--dsn", "postgresql://x"]
        )
        assert rc == 2
        assert "omits the season in progress" in caplog.text
        assert "--allow-partial" in caplog.text
        stubs["migration"].assert_not_called()
        stubs["chain"].assert_not_called()

    def test_allow_partial_runs_the_chain_on_the_list_as_given(self, monkeypatch, tmp_path) -> None:
        stubs = _stub_main_steps(monkeypatch, tmp_path)
        last_year = date.today().year - 1
        rc = recompute.main(
            [
                "--seasons",
                str(last_year),
                str(last_year - 1),
                "--dsn",
                "postgresql://x",
                "--duckdb-path",
                "/tmp/z.duckdb",
                "--allow-partial",
            ]
        )
        assert rc == 0
        stubs["migration"].assert_called_once_with("/tmp/z.duckdb")
        stubs["chain"].assert_called_once_with(
            "postgresql://x", "/tmp/z.duckdb", [last_year - 1, last_year]
        )
        stubs["league"].return_value.compute.assert_called_once_with([last_year - 1, last_year])
        stubs["verify"].assert_called_once()
        _assert_main_order(stubs, with_chain=True)

    def test_skip_profiles_runs_no_computor_method(self, monkeypatch, tmp_path) -> None:
        stubs = _stub_main_steps(monkeypatch, tmp_path)
        rc = recompute.main(["--skip-profiles", "--duckdb-path", "/tmp/z.duckdb"])
        assert rc == 0
        stubs["chain"].assert_not_called()
        stubs["computor"].assert_not_called()
        stubs["migration"].assert_called_once()
        stubs["league"].return_value.compute.assert_called_once_with(
            list(recompute.DEFAULT_SEASONS)
        )
        stubs["verify"].assert_called_once()
        _assert_main_order(stubs, with_chain=False)

    def test_main_needs_a_dsn_for_the_chain(self, monkeypatch, tmp_path, caplog) -> None:
        stubs = _stub_main_steps(monkeypatch, tmp_path)
        monkeypatch.setattr(recompute, "DEFAULT_DSN", "")
        rc = recompute.main(["--dsn", ""])
        assert rc == 2
        assert "no Postgres DSN" in caplog.text
        stubs["migration"].assert_not_called()

    def test_a_locked_database_gives_the_run_book_message(self, tmp_path, monkeypatch) -> None:
        path = str(tmp_path / "sim532_locked.duckdb")
        real_connect = duckdb.connect

        def locked_connect(*args, **kwargs):
            if args and args[0] == path and not kwargs.get("read_only", False):
                raise duckdb.IOException(
                    f"IO Error: Could not set lock on file {path}: File is already open"
                )
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(duckdb, "connect", locked_connect)
        with pytest.raises(SystemExit, match="docker compose stop app") as info:
            recompute.main(["--skip-profiles", "--duckdb-path", path])
        assert "sim532_locked.duckdb" in str(info.value)
