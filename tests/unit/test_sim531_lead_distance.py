"""
SIM-531 — lead distance in the stolen-base and baserunning models.

Plan: docs/audit/2026-09-16-sim531-lead-distance-build-plan.md (§7 lists these).

What the ticket adds, and what these tests hold:

* the loader registry: two new Savant boards (the runner's Basestealing Run
  Value, the Pitcher Running Game), both at ``n=1`` (every player with one
  chance) and in ``RUNNING_BOARDS``; the count columns coerce to whole numbers;
* the migrations: DuckDB 0029 adds twelve columns (the baserunner table's four
  stay LAST, in the builder's order — a positional INSERT), the canonical DDL
  mirrors them, the version file reads 29; Alembic 0026 creates and drops the
  two raw tables;
* the profile builders, EXECUTED on an in-memory DuckDB with a fake ``pg``
  catalog: the steal driver covers every runner with a chance (a runner who
  never went has attempt rate 0.0 and a NULL success rate), the lead columns
  land, the four positional-tail columns land in the right slots, and under a
  cutoff every Savant join season-shifts (the SIM-537 rule) while a live build
  does not; the league-average writer emits the two rows the engines always
  read;
* the steal-runner model: the weights sum to one at 0.45 / 0.45 / 0.10; a pair
  missing the lead on either side scores over tendency and success alone (no
  phantom match); ``score_all`` and ``query_pair`` agree; the confidence basis
  is first-base opportunities; a thin runner's lead shrinks toward the league
  mean; NULL loads as NaN and stays neutral; the calibration sigma applies;
* the pitcher-hold model: the mirror set at 0.35 / 0.65;
* the advancement model: the masked kernel drops a missing feature from the
  distance AND its normalisation; a fully-measured pair is unchanged; a
  half-measured pair is not inflated; a stale six-weight calibration is refused;
* the calibrator: the two sigmas round-trip, the fits use measured rows, and the
  sentinel stays 0.0 with none.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import numpy as np
import pytest

duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[2]
MIGRATION_0029 = REPO / "db" / "migrations" / "duckdb" / "0029_sim531_lead_distance.sql"
ALEMBIC_0026 = REPO / "db" / "migrations" / "versions" / "0026_sim531_savant_running_game.py"
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"
PG_SCHEMA_SQL = REPO / "db" / "schemas" / "01_postgres_schema.sql"
COMPUTOR = REPO / "pipeline" / "batch" / "player_profile_computor.py"

STEAL_COLUMNS = (
    "lead_primary_ft",
    "lead_secondary_ft",
    "lead_jump_ft",
    "savant_steal_opps",
    "sample_second_base_opps",
)
HOLD_COLUMNS = (
    "lead_allowed_primary_ft",
    "lead_allowed_secondary_ft",
    "lead_allowed_jump_ft",
    "savant_hold_opps",
)
XB_COLUMNS = (
    "xb_opportunities",
    "xb_attempt_rate",
    "xb_expected_attempt_rate",
    "xb_attempt_rate_above_expected",
)


# ===========================================================================
# 1. The loader registry
# ===========================================================================


class TestLoaderRegistry:
    def test_both_boards_resolve_and_sit_in_running_boards(self) -> None:
        from pipeline.etl.savant_boards import BOARDS, RUNNING_BOARDS
        from pipeline.etl.savant_loader import resolve_boards

        assert RUNNING_BOARDS == ("basestealing", "pitcher_running_game", "baserunning")
        assert [b.name for b in resolve_boards(["running"])] == list(RUNNING_BOARDS)
        for name in ("basestealing", "pitcher_running_game"):
            board = BOARDS[name]
            assert board.season_style == "snake"
            assert board.season_column == "start_year"
            assert board.player_column == "player_id"
            assert board.extra == {"n": "1"}, "n=1 is the smallest honoured minimum"

    def test_the_boards_send_n_one_and_the_snake_season_pair(self) -> None:
        from pipeline.etl.savant_boards import BOARDS
        from pipeline.etl.savant_loader import build_url

        url = build_url(BOARDS["basestealing"], 2024)
        assert url.startswith("https://baseballsavant.mlb.com/leaderboard/basestealing-run-value?")
        assert "n=1" in url and "season_start=2024" in url and "season_end=2024" in url
        assert "csv=true" in url
        url = build_url(BOARDS["pitcher_running_game"], 2024)
        assert url.startswith("https://baseballsavant.mlb.com/leaderboard/pitcher-running-game?")
        assert "n=1" in url

    def test_the_boards_target_the_0026_tables_with_the_csv_columns(self) -> None:
        from pipeline.etl.savant_boards import BOARDS

        runner = BOARDS["basestealing"]
        assert runner.table == "raw.savant_basestealing"
        srcs = {src for src, _ in runner.columns}
        assert {"n_init", "rate_sbx", "r_primary_lead", "r_sec_minus_prim_lead"} <= srcs
        pitcher = BOARDS["pitcher_running_game"]
        assert pitcher.table == "raw.savant_pitcher_running_game"
        srcs = {src for src, _ in pitcher.columns}
        assert {
            "runs_prevented_on_running_attr",
            "n_pitcher_cs_aa",
            "r_sec_minus_prim_lead",
        } <= srcs
        # The unglossed outcome decomposition is not stored.
        assert not {"n_fb", "n_plus", "n_minus", "net_act_plus"} & srcs

    def test_a_row_whose_start_year_differs_raises(self) -> None:
        from pipeline.etl.savant_boards import BOARDS
        from pipeline.etl.savant_loader import SeasonParamError, check_row_seasons

        with pytest.raises(SeasonParamError):
            check_row_seasons(BOARDS["basestealing"], [{"start_year": "2025"}], 2024)

    def test_counts_coerce_to_int_and_leads_to_float(self) -> None:
        from pipeline.etl.savant_boards import BOARDS
        from pipeline.etl.savant_loader import coerce_row, parse_rows

        header = (
            "player_id,player_name,start_year,n_init,n_sb,n_cs,n_pk,n_bk,rate_sbx,"
            "r_primary_lead,r_sec_minus_prim_lead"
        )
        line = '691026,"Witt Jr., Bobby",2024,300,31,4,1,0,0.1167,"12.752",'
        body = header + chr(10) + line + chr(10)
        rows = parse_rows(body)
        assert len(rows) == 1
        out = coerce_row(BOARDS["basestealing"], rows[0], 2024, None)
        assert out is not None
        assert out["player_id"] == 691026 and out["season"] == 2024
        for k in ("n_init", "n_sb", "n_cs", "n_pk", "n_bk"):
            assert isinstance(out[k], int), k
        assert out["n_init"] == 300
        assert out["r_primary_lead"] == pytest.approx(12.752)
        assert out["r_sec_minus_prim_lead"] is None  # a blank lead is None, never 0.0
        assert out["rate_sbx"] == pytest.approx(0.1167)


# ===========================================================================
# 2. The migrations and the schema mirrors
# ===========================================================================


class TestMigrations:
    def test_0029_adds_the_twelve_columns_in_order(self) -> None:
        text = MIGRATION_0029.read_text(encoding="utf-8")
        found = re.findall(r"ALTER TABLE derived\.(\w+) ADD COLUMN IF NOT EXISTS (\w+)", text)
        by_table: dict[str, list[str]] = {}
        for table, col in found:
            by_table.setdefault(table, []).append(col)
        assert by_table["baserunner_steal_metrics"] == list(STEAL_COLUMNS)
        assert by_table["pitcher_steal_metrics"] == list(HOLD_COLUMNS)
        assert by_table["baserunner_season_metrics"] == list(XB_COLUMNS)

    def test_the_version_file_reads_29(self) -> None:
        assert VERSION_FILE.read_text(encoding="utf-8").strip() == "29"

    def test_the_canonical_duckdb_schema_mirrors_the_columns(self) -> None:
        ddl = SCHEMA_SQL.read_text(encoding="utf-8")
        for col in STEAL_COLUMNS + HOLD_COLUMNS + XB_COLUMNS:
            assert re.search(rf"^\s+{col}\s", ddl, re.M), col

    def test_the_baserunner_tail_is_the_builders_order_in_the_canonical_ddl(self) -> None:
        """The baserunner INSERT is positional: the DDL's trailing columns must
        be exactly ``BASERUNNER_TAIL_COLUMNS`` — the schema and the SELECT can
        never desync."""
        from pipeline.batch.player_profile_computor import BASERUNNER_TAIL_COLUMNS

        con = duckdb.connect(":memory:")
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        cols = [r[0] for r in con.execute("DESCRIBE derived.baserunner_season_metrics").fetchall()]
        assert tuple(cols[-len(BASERUNNER_TAIL_COLUMNS) :]) == BASERUNNER_TAIL_COLUMNS
        assert ("asof_date", *XB_COLUMNS) == BASERUNNER_TAIL_COLUMNS

    def test_0029_applies_idempotently_on_the_canonical_schema(self) -> None:
        con = duckdb.connect(":memory:")
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        body = "\n".join(
            line
            for line in MIGRATION_0029.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        statements = [s for s in body.split(";") if s.strip()]
        assert len(statements) == 13
        for _ in range(2):  # twice: ADD COLUMN IF NOT EXISTS
            for stmt in statements:
                con.execute(stmt)

    def test_alembic_0026_creates_and_drops_the_two_raw_tables(self) -> None:
        text = ALEMBIC_0026.read_text(encoding="utf-8")
        assert 'revision = "0026"' in text
        assert 'down_revision = "0025"' in text
        assert "CREATE TABLE IF NOT EXISTS raw.savant_basestealing" in text
        assert "CREATE TABLE IF NOT EXISTS raw.savant_pitcher_running_game" in text
        assert "REFERENCES raw.players(player_id)" in text
        assert "PRIMARY KEY (player_id, season)" in text
        assert "def downgrade()" in text and "DROP TABLE IF EXISTS raw.{table}" in text
        for col in (
            "n_init",
            "rate_sbx",
            "r_primary_lead",
            "r_secondary_lead",
            "r_sec_minus_prim_lead",
            "r_primary_lead_sbx",
        ):
            assert col in text, col
        assert "runs_prevented_on_running_attr" in text and "n_pitcher_cs_aa" in text

    def test_the_postgres_reference_ddl_mirrors_the_two_tables(self) -> None:
        ddl = PG_SCHEMA_SQL.read_text(encoding="utf-8")
        assert "CREATE TABLE raw.savant_basestealing" in ddl
        assert "CREATE TABLE raw.savant_pitcher_running_game" in ddl
        assert "idx_savant_basestealing_season" in ddl
        assert "idx_savant_pitcher_running_game_season" in ddl


# ===========================================================================
# 3. The profile builders, executed on an in-memory DuckDB
# ===========================================================================

_PITCH_COLS = 30


def _fake_catalog() -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB with the canonical derived schema and a fake ``pg``
    catalog holding the six raw tables the three builders read."""
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    con.execute("ATTACH ':memory:' AS pg")
    con.execute("CREATE SCHEMA pg.raw")
    con.execute(
        """
        CREATE TABLE pg.raw.pitches (
            game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, season INTEGER,
            game_date DATE, data_quality_flag BOOLEAN, pitcher INTEGER, batter INTEGER,
            p_throws VARCHAR, events VARCHAR, type VARCHAR, bb_type VARCHAR,
            on_1b INTEGER, on_2b INTEGER, on_3b INTEGER,
            post_on_1b INTEGER, post_on_2b INTEGER, post_on_3b INTEGER,
            runner_1b_scored BOOLEAN, runner_2b_scored BOOLEAN, runner_3b_scored BOOLEAN,
            runner_1b_out_advancing BOOLEAN, runner_2b_out_advancing BOOLEAN,
            runner_3b_out_advancing BOOLEAN,
            sb_attempt_2b BOOLEAN, sb_attempt_3b BOOLEAN, sb_attempt_home BOOLEAN,
            sb_success_2b BOOLEAN, sb_success_3b BOOLEAN, sb_success_home BOOLEAN
        )
        """
    )
    con.execute(
        "CREATE TABLE pg.raw.savant_basestealing (player_id INTEGER, season INTEGER, "
        "n_init INTEGER, r_primary_lead FLOAT, r_secondary_lead FLOAT, "
        "r_sec_minus_prim_lead FLOAT)"
    )
    con.execute(
        "CREATE TABLE pg.raw.savant_pitcher_running_game (player_id INTEGER, season INTEGER, "
        "n_init INTEGER, r_primary_lead FLOAT, r_secondary_lead FLOAT, "
        "r_sec_minus_prim_lead FLOAT)"
    )
    con.execute(
        "CREATE TABLE pg.raw.savant_baserunning (player_id INTEGER, season INTEGER, "
        "n_opp_xb INTEGER, n_att_xb INTEGER, rate_att_xb FLOAT, "
        "est_rate_att_generic_runner FLOAT)"
    )
    con.execute(
        "CREATE TABLE pg.raw.sprint_speed (player_id INTEGER, season INTEGER, sprint_speed FLOAT)"
    )
    return con


def _pitch(
    ab: int,
    *,
    season: int = 2024,
    day: date = date(2024, 5, 1),
    pitcher: int = 500,
    on_1b: int | None = None,
    on_2b: int | None = None,
    attempt: bool = False,
    safe: bool = False,
    events: str | None = None,
    type_code: str = "S",
    post_on_3b: int | None = None,
) -> tuple:
    return (
        1,
        ab,
        1,
        season,
        day,
        False,
        pitcher,
        900,
        "R",
        events,
        type_code,
        "line_drive" if events == "single" else None,
        on_1b,
        on_2b,
        None,
        None,
        None,
        post_on_3b,
        False,
        False,
        False,
        False,
        False,
        False,
        attempt,
        False,
        False,
        attempt and safe,
        False,
        False,
    )


def _seed(con: duckdb.DuckDBPyConnection, *, savant_season: int = 2024) -> None:
    """Runner 11: 20 attempts (15 safe) in 40 chances against pitcher 500, plus
    four extra-base chances on singles (two taken). Runner 12: 30 chances,
    never goes, against pitcher 501. Runner 13: six chances on second only,
    never goes. Savant rows for runner 11 and pitcher 500 in ``savant_season``."""
    rows = []
    for i in range(40):
        rows.append(_pitch(i + 1, on_1b=11, attempt=i < 20, safe=(i < 20 and i % 4 != 0)))
    for i in range(30):
        rows.append(_pitch(100 + i, pitcher=501, on_1b=12, type_code="B"))
    # Runner 13: six chances that all began on SECOND (the automatic runner),
    # never went — a row with no first-base chance at all.
    for i in range(6):
        rows.append(_pitch(300 + i, pitcher=501, on_2b=13, type_code="B"))
    for i in range(4):
        rows.append(
            _pitch(
                200 + i, on_1b=11, events="single", type_code="X", post_on_3b=11 if i < 2 else None
            )
        )
    con.executemany(
        "INSERT INTO pg.raw.pitches VALUES (" + ", ".join(["?"] * _PITCH_COLS) + ")", rows
    )
    con.execute(
        "INSERT INTO pg.raw.savant_basestealing VALUES (11, ?, 300, 12.752, 16.041, 3.289)",
        [savant_season],
    )
    con.execute(
        "INSERT INTO pg.raw.savant_pitcher_running_game VALUES (500, ?, 700, 11.629, 15.869, 4.240)",
        [savant_season],
    )
    con.execute(
        "INSERT INTO pg.raw.savant_baserunning VALUES (11, ?, 152, 60, 0.3947, 0.346)",
        [savant_season],
    )
    con.execute("INSERT INTO pg.raw.sprint_speed VALUES (11, ?, 28.5)", [savant_season])


def _computor(con: duckdb.DuckDBPyConnection):
    from pipeline.batch.player_profile_computor import PlayerProfileComputor

    c = PlayerProfileComputor(pg_dsn="", duckdb_path=":memory:")
    c._conn = con
    return c


class TestBuildersExecuted:
    def test_the_steal_driver_covers_every_runner_with_a_chance(self) -> None:
        con = _fake_catalog()
        _seed(con)
        _computor(con)._build_baserunner_steal_metrics([2024], asof=None)
        rows = {
            r[0]: r
            for r in con.execute(
                "SELECT player_id, sample_steal_attempts, sample_first_base_opps, "
                "steal_attempt_rate, steal_success_rate, below_minimum_sample, "
                "lead_primary_ft, lead_secondary_ft, lead_jump_ft, savant_steal_opps, "
                "sample_second_base_opps, steal_attempt_rate_2b "
                "FROM derived.baserunner_steal_metrics"
            ).fetchall()
        }
        assert set(rows) == {11, 12, 13}, "every runner with a chance has a row"
        r11, r12, r13 = rows[11], rows[12], rows[13]
        assert (r11[10], r12[10]) == (0, 0)
        # The second-base-only runner: no first-base chance, six on second, a
        # measured 0.0 rate from second — and a chance count the model can use.
        assert r13[1] == 0 and r13[2] == 0 and r13[10] == 6
        assert r13[3] is None and r13[11] == 0.0
        assert r11[1] == 20 and r11[2] == 44
        assert r11[3] == pytest.approx(20 / 44)
        assert r11[4] == pytest.approx(15 / 20)
        assert r11[5] is False
        assert (r11[6], r11[7], r11[8], r11[9]) == pytest.approx(
            (12.752, 16.041, 3.289, 300), rel=1e-5
        )
        assert r12[1] == 0 and r12[2] == 30
        assert r12[3] == 0.0, "a measured rate over his chances"
        assert r12[4] is None, "unmeasured, never 0.0"
        assert r12[5] is True
        assert r12[6] is None and r12[9] is None, "no Savant row: NULL, never 0.0 ft"

    def test_the_pitcher_builder_lands_the_lead_allowed(self) -> None:
        con = _fake_catalog()
        _seed(con)
        _computor(con)._build_pitcher_steal_metrics([2024], asof=None)
        rows = {
            r[0]: r
            for r in con.execute(
                "SELECT pitcher_id, lead_allowed_primary_ft, lead_allowed_secondary_ft, "
                "lead_allowed_jump_ft, savant_hold_opps FROM derived.pitcher_steal_metrics"
            ).fetchall()
        }
        assert rows[500][1:] == pytest.approx((11.629, 15.869, 4.240, 700), rel=1e-5)
        assert rows[501][1:] == (None, None, None, None)

    def test_the_positional_insert_lands_the_four_tail_columns(self) -> None:
        con = _fake_catalog()
        _seed(con)
        _computor(con)._compute_baserunner_profiles([2024], asof=None)
        row = con.execute(
            "SELECT sprint_speed, extra_base_attempt_rate, asof_date, "
            + ", ".join(XB_COLUMNS)
            + " FROM derived.baserunner_season_metrics WHERE player_id = 11"
        ).fetchone()
        assert row[0] == pytest.approx(28.5)
        assert row[1] == pytest.approx(0.5)  # two of four extra-base chances taken
        assert row[2] == date.today()
        assert row[3] == 152
        assert row[4] == pytest.approx(0.3947, rel=1e-5)
        assert row[5] == pytest.approx(0.346, rel=1e-5)
        assert row[6] == pytest.approx(0.3947 - 0.346, rel=1e-4)

    def test_under_a_cutoff_every_savant_join_season_shifts(self) -> None:
        """The season containing the cutoff joins the PRIOR season's Savant row
        (the SIM-537 rule); with the row filed under the cutoff season it is
        not read, and with it filed under the prior season it is."""
        for savant_season, expect_lead in ((2024, False), (2023, True)):
            con = _fake_catalog()
            _seed(con, savant_season=savant_season)
            c = _computor(con)
            asof = date(2024, 6, 1)
            c._build_baserunner_steal_metrics([2024], asof=asof)
            c._build_pitcher_steal_metrics([2024], asof=asof)
            c._compute_baserunner_profiles([2024], asof=asof)
            lead = con.execute(
                "SELECT lead_primary_ft FROM derived.baserunner_steal_metrics WHERE player_id = 11"
            ).fetchone()[0]
            hold = con.execute(
                "SELECT lead_allowed_jump_ft FROM derived.pitcher_steal_metrics "
                "WHERE pitcher_id = 500"
            ).fetchone()[0]
            xb = con.execute(
                "SELECT xb_attempt_rate_above_expected FROM derived.baserunner_season_metrics "
                "WHERE player_id = 11"
            ).fetchone()[0]
            assert (lead is not None) is expect_lead, (savant_season, lead)
            assert (hold is not None) is expect_lead, (savant_season, hold)
            assert (xb is not None) is expect_lead, (savant_season, xb)

    def test_the_league_average_writer_emits_the_two_steal_rows(self, tmp_path: Path) -> None:
        from pipeline.batch.player_profile_computor import LeagueAverageProfiles

        path = str(tmp_path / "sim531.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        con.execute(
            "INSERT INTO derived.baserunner_steal_metrics (player_id, season, "
            "sample_steal_attempts, sample_first_base_opps, steal_attempt_rate, "
            "steal_attempt_rate_2b, steal_success_rate, steal_success_rate_2b, "
            "below_minimum_sample, lead_primary_ft, lead_jump_ft) VALUES "
            "(1, 2024, 20, 100, 0.2, 0.05, 0.8, 0.5, FALSE, 12.0, 3.0), "
            "(2, 2024, 12, 80, 0.15, 0.02, 0.7, NULL, FALSE, 11.0, 4.0), "
            "(3, 2024, 0, 30, 0.0, 0.0, NULL, NULL, TRUE, NULL, NULL)"
        )
        con.execute(
            "INSERT INTO derived.pitcher_steal_metrics (pitcher_id, season, throws, "
            "sample_baserunner_events, sample_steal_attempts_against, sb_against_per_9, "
            "cs_rate_forced, steal_attempt_rate_allowed, below_minimum_sample, "
            "lead_allowed_primary_ft, lead_allowed_jump_ft) VALUES "
            "(500, 2024, 'R', 200, 20, 0.6, 0.3, 0.1, FALSE, 11.6, 4.2), "
            "(501, 2024, 'L', 150, 10, 0.4, 0.5, 0.07, FALSE, 11.2, NULL)"
        )
        con.execute(
            "INSERT INTO derived.baserunner_season_metrics (player_id, season, sprint_speed, "
            "extra_base_attempt_rate, extra_base_success_rate, sb_success_rate, "
            "below_minimum_sample, xb_attempt_rate_above_expected) VALUES "
            "(1, 2024, 28.0, 0.4, 0.9, 0.8, FALSE, 0.05), "
            "(2, 2024, 27.0, 0.3, 0.8, 0.7, FALSE, NULL)"
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
        steal = rows["baserunner_steal"]
        assert steal["lead_primary_ft"] == pytest.approx(11.5)  # the thin runner is excluded
        assert steal["lead_jump_ft"] == pytest.approx(3.5)
        assert steal["steal_attempt_rate"] == pytest.approx(0.175)
        hold = rows["pitcher_steal"]
        assert hold["lead_allowed_primary_ft"] == pytest.approx(11.4)
        assert hold["lead_allowed_jump_ft"] == pytest.approx(4.2)  # AVG skips the NULL
        # The advancement row gains the new key (AVG skips the runner with no row).
        assert rows["baserunner"]["xb_attempt_rate_above_expected"] == pytest.approx(0.05)

    def test_the_league_average_writer_tolerates_a_pre_0029_database(self, tmp_path: Path) -> None:
        """A database without the 0029 columns (or without the two steal tables
        at all) still gets its league averages — the writer probes, never
        assumes (the SIM-162 fixture builds only a few narrow tables)."""
        from pipeline.batch.player_profile_computor import LeagueAverageProfiles

        path = str(tmp_path / "old.duckdb")
        con = duckdb.connect(path)
        con.execute("CREATE SCHEMA derived")
        for table, cols in (
            (
                "pitcher_season_metrics",
                "bb_rate FLOAT, k_rate FLOAT, csw_rate FLOAT, zone_rate FLOAT, "
                "chase_rate FLOAT, era FLOAT, fip FLOAT, ground_ball_rate FLOAT, fly_ball_rate FLOAT",
            ),
            (
                "batter_season_metrics",
                "first_pitch_take_rate FLOAT, o_swing_rate FLOAT, "
                "z_swing_rate FLOAT, whiff_rate FLOAT, contact_rate FLOAT, walk_rate FLOAT, "
                "k_rate FLOAT, avg_exit_velo FLOAT, hard_hit_rate FLOAT",
            ),
            (
                "fielder_season_metrics",
                "position VARCHAR, outs_above_average FLOAT, error_rate FLOAT, "
                "arm_hold_rate FLOAT, dp_run_value FLOAT",
            ),
            (
                "baserunner_season_metrics",
                "sprint_speed FLOAT, extra_base_attempt_rate FLOAT, "
                "extra_base_success_rate FLOAT, sb_success_rate FLOAT",
            ),
            ("catcher_season_metrics", "framing_runs FLOAT, blocking_runs FLOAT, cs_rate FLOAT"),
        ):
            con.execute(
                f"CREATE TABLE derived.{table} (player_id INTEGER, season INTEGER, {cols}, "
                "below_minimum_sample BOOLEAN, sample_games INTEGER)"
            )
        con.execute(
            "CREATE TABLE derived.manager_season_metrics (manager_id INTEGER, season INTEGER, "
            "sample_games INTEGER, below_minimum_sample BOOLEAN, "
            + ", ".join(
                f"{c} FLOAT"
                for c in (
                    "starter_avg_pitch_count",
                    "starter_pull_pct_before_100",
                    "closer_entry_leverage_index",
                    "high_leverage_reliever_rate",
                    "opener_usage_rate",
                    "bulk_innings_rate",
                    "available_reliever_usage_rate",
                    "steal_order_rate_per_1b_opp",
                    "hit_and_run_rate_per_opportunity",
                    "sac_bunt_rate_high_leverage",
                    "sac_bunt_rate_low_leverage",
                    "squeeze_play_rate_per_3b_opp",
                    "pinch_hit_rate_vs_same_hand",
                    "pinch_hit_rate_high_leverage",
                    "defensive_sub_rate_late_innings",
                    "double_switch_rate_per_reliever_change",
                    "platoon_advantage_exploitation_rate",
                )
            )
            + ")"
        )
        con.execute(
            "INSERT INTO derived.baserunner_season_metrics VALUES (1, 2024, 28.0, 0.4, 0.9, 0.8, "
            "FALSE, 10)"
        )
        con.close()
        LeagueAverageProfiles(path).compute([2024])
        con = duckdb.connect(path, read_only=True)
        rows = {
            e: json.loads(pj) if isinstance(pj, str) else pj
            for e, pj in con.execute(
                "SELECT entity_type, profile_json FROM derived.league_averages"
            ).fetchall()
        }
        con.close()
        assert "xb_attempt_rate_above_expected" not in rows["baserunner"]
        assert "baserunner_steal" not in rows and "pitcher_steal" not in rows


class TestBuilderSource:
    """The SIM-537 style: the cutoff rule read off the source, so the check
    needs no database."""

    @staticmethod
    def _body(name: str, next_marker: str) -> str:
        src = COMPUTOR.read_text(encoding="utf-8")
        start = src.index(f"def {name}")
        return src[start : src.index(next_marker, start)]

    def test_the_steal_builder_shifts_the_savant_season_under_a_cutoff(self) -> None:
        body = self._body(
            "_build_baserunner_steal_metrics",
            "def _assert_baserunner_steal_profiles_have_no_leakage",
        )
        assert "CASE WHEN r.season = {asof_date.year} THEN r.season - 1 ELSE r.season END" in body
        assert 'else "r.season"' in body
        assert "sv.season = {savant_season}" in body
        assert "FROM pg.raw.savant_basestealing" in body
        assert re.search(
            r"asof_date,\s*\n\s*lead_primary_ft, lead_secondary_ft, lead_jump_ft, savant_steal_opps",
            body,
        )
        assert "SELECT player_id, season FROM opp_1b" in body  # the widened driver

    def test_the_pitcher_builder_shifts_the_savant_season_under_a_cutoff(self) -> None:
        body = self._body(
            "_build_pitcher_steal_metrics", "def _assert_pitcher_steal_profiles_have_no_leakage"
        )
        assert "CASE WHEN p.season = {asof_date.year} THEN p.season - 1 ELSE p.season END" in body
        assert 'else "p.season"' in body
        assert "LEFT JOIN pg.raw.savant_pitcher_running_game sv" in body
        assert "sv.season = {savant_season}" in body
        for col in HOLD_COLUMNS:
            assert col in body, col

    def test_the_baserunner_builder_joins_the_baserunning_board_on_the_sprint_speed_shift(
        self,
    ) -> None:
        body = self._body(
            "_compute_baserunner_profiles", "def _assert_baserunner_profiles_have_no_leakage"
        )
        assert "LEFT JOIN pg.raw.savant_baserunning sbr" in body
        assert body.count("{sprint_speed_season}") == 2  # sprint speed AND the board
        # The four tail columns, in XB_COLUMN_ORDER, after asof_date.
        idx = [body.index(f"AS {c}") for c in ("asof_date", *XB_COLUMNS)]
        assert idx == sorted(idx)


# ===========================================================================
# 4. The steal-runner model
# ===========================================================================


def _steal_engine(profiles):
    from similarity.engines.baserunner_steal_similarity import (
        RBF_SIGMA_SUCCESS,
        RBF_SIGMA_TENDENCY,
        SUCCESS_FEATURES,
        TENDENCY_FEATURES,
        BaserunnerStealSimilarityEngine,
        EmpiricalBayesShrinkage,
        FeatureNormalizer,
        StealPartition,
        WeightedRBFSimilarity,
    )

    engine = BaserunnerStealSimilarityEngine.__new__(BaserunnerStealSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {(p.player_id, p.season): p for p in profiles}
    engine._league_avg = {"tendency": {}, "success": {}, "lead": {}}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._partition = StealPartition()
    engine._tend_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_TENDENCY, np.array([w for _, w in TENDENCY_FEATURES])
    )
    engine._succ_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_SUCCESS, np.array([w for _, w in SUCCESS_FEATURES])
    )
    # No ``_lead_rbf`` on purpose: the no-DB fixtures built before SIM-531 do not
    # set one, and ``query`` must fall back to the module bandwidth.
    engine._normalizer.fit(profiles)
    engine._partition.build(profiles, engine._normalizer)
    return engine


def _steal_profile(pid, tend, succ, lead=None, *, alpha=1.0):
    from similarity.engines.baserunner_steal_similarity import BaserunnerStealProfile

    kw = {}
    if lead is not None:
        kw = {"lead_vec": np.array(lead, dtype=np.float64), "has_lead": True}
    return BaserunnerStealProfile(
        player_id=pid,
        season=2024,
        sample_steal_attempts=20,
        sample_first_base_opps=200,
        tendency_vec=np.array(tend, dtype=np.float64),
        success_vec=np.array(succ, dtype=np.float64),
        eb_alpha=alpha,
        **kw,
    )


class TestStealRunnerModel:
    def test_weights_sum_to_one_at_the_decided_split(self) -> None:
        from similarity.engines.baserunner_steal_similarity import (
            LEAD_FEATURES,
            WEIGHT_LEAD,
            WEIGHT_SUCCESS,
            WEIGHT_TENDENCY,
        )

        assert (WEIGHT_TENDENCY, WEIGHT_LEAD, WEIGHT_SUCCESS) == (0.45, 0.45, 0.10)
        assert pytest.approx(1.0) == WEIGHT_TENDENCY + WEIGHT_LEAD + WEIGHT_SUCCESS
        assert [f for f, _ in LEAD_FEATURES] == ["lead_primary_ft", "lead_jump_ft"]

    def test_a_profile_without_a_lead_has_nan_not_zero(self) -> None:
        p = _steal_profile(1, [0.1, 0.02], [0.8, 0.5])
        assert p.has_lead is False
        assert np.isnan(p.lead_vec).all()

    def test_no_lead_pairs_score_over_tendency_and_success_only(self) -> None:
        """Two runners identical on tendency and success and without a lead score
        1.0 — the renormalized two-way blend — never a phantom lead match, and
        never a penalty for the missing group."""
        engine = _steal_engine(
            [
                _steal_profile(1, [0.10, 0.02], [0.80, 0.50]),
                _steal_profile(2, [0.10, 0.02], [0.80, 0.50]),
                _steal_profile(3, [0.30, 0.10], [0.60, 0.40]),
            ]
        )
        r = {x.player_id: x for x in engine.query(1, 2024)}
        assert r[2].score == pytest.approx(1.0)
        assert r[2].lead_score is None
        assert r[3].score < 1.0 and r[3].lead_score is None

    def test_measured_pairs_use_all_three_groups(self) -> None:
        """Identical tendency and success but very different leads: the lead
        group pulls the score down by up to its 0.45 weight."""
        engine = _steal_engine(
            [
                _steal_profile(1, [0.10, 0.02], [0.80, 0.50], lead=[12.0, 3.0]),
                _steal_profile(2, [0.10, 0.02], [0.80, 0.50], lead=[12.0, 3.0]),
                _steal_profile(3, [0.10, 0.02], [0.80, 0.50], lead=[9.0, 1.0]),
                _steal_profile(4, [0.10, 0.02], [0.80, 0.50]),  # no lead
            ]
        )
        r = {x.player_id: x for x in engine.query(1, 2024)}
        assert r[2].score == pytest.approx(1.0) and r[2].lead_score == pytest.approx(1.0)
        assert r[3].score < r[2].score and 0.0 < r[3].lead_score < 1.0
        assert r[3].score > 0.55  # tendency + success still carry 0.55
        # The no-lead runner is scored over the groups both have: identical there.
        assert r[4].score == pytest.approx(1.0) and r[4].lead_score is None

    def test_score_all_equals_query_pair(self) -> None:
        rng = np.random.default_rng(531)
        profiles = []
        for pid in range(1, 13):
            lead = [rng.uniform(9, 14), rng.uniform(1, 5)] if pid % 3 else None
            profiles.append(
                _steal_profile(
                    pid,
                    rng.uniform(0.0, 0.3, 2),
                    rng.uniform(0.5, 0.95, 2),
                    lead,
                    alpha=rng.uniform(0.3, 1.0),
                )
            )
        engine = _steal_engine(profiles)
        for pid in (1, 2, 3):
            by_id = {x.player_id: x for x in engine.query(pid, 2024)}
            for other in by_id:
                pair = engine.query_pair((pid, 2024), (other, 2024))
                assert pair.score == pytest.approx(by_id[other].score, abs=1e-12)
                if pair.lead_score is None:
                    assert by_id[other].lead_score is None
                else:
                    assert pair.lead_score == pytest.approx(by_id[other].lead_score, abs=1e-12)
                # symmetric
                back = engine.query_pair((other, 2024), (pid, 2024))
                assert back.score == pytest.approx(pair.score, abs=1e-12)

    def test_the_confidence_basis_is_the_runners_chances(self) -> None:
        from similarity.engines.baserunner_steal_similarity import (
            EB_N_PRIOR_OPPS,
            BaserunnerStealSimilarityEngine,
            EmpiricalBayesShrinkage,
        )

        assert EB_N_PRIOR_OPPS == 50
        assert EmpiricalBayesShrinkage(EB_N_PRIOR_OPPS).alpha(300) == pytest.approx(300 / 350)
        # 0 attempts over 300 opportunities: a confidence of 0.857, not 0.0.
        assert EmpiricalBayesShrinkage(EB_N_PRIOR_OPPS).alpha(300) == pytest.approx(0.857, abs=1e-3)
        chances = BaserunnerStealSimilarityEngine.chances
        assert chances(300) == 300
        assert chances(0, 6, 0) == 6, "chances on second alone count"
        assert chances(0, 0, 3) == 3, "an attempt is a chance (a steal of third after a triple)"
        assert chances(40, 10, 20) == 50
        assert chances(None, None, None) == 0

    def test_no_row_the_driver_admits_reads_confidence_zero(self) -> None:
        """The review of 2026-09-16: a first-base-only basis gave a runner whose
        chances were all on second (or whose only attempts were of third) a
        confidence of 0 — a zero score against every row, an all-zero matrix
        row, and a steal draw with no weight at all."""
        from similarity.engines.baserunner_steal_similarity import BaserunnerStealProfile

        second_only = BaserunnerStealProfile(
            player_id=1,
            season=2024,
            sample_steal_attempts=0,
            sample_first_base_opps=0,
            sample_second_base_opps=6,
            tendency_vec=np.array([np.nan, 0.0]),
            success_vec=np.array([np.nan, np.nan]),
            eb_alpha=6 / 56,
        )
        attempts_only = BaserunnerStealProfile(
            player_id=2,
            season=2024,
            sample_steal_attempts=3,
            sample_first_base_opps=0,
            tendency_vec=np.array([np.nan, np.nan]),
            success_vec=np.array([2 / 3, np.nan]),
            eb_alpha=3 / 53,
        )
        measured = _steal_profile(3, [0.1, 0.02], [0.8, 0.5], lead=[12.0, 3.0])
        engine = _steal_engine([second_only, attempts_only, measured])
        for pid in (1, 2):
            scores = [r.score for r in engine.query(pid, 2024)]
            assert scores and all(x > 0.0 for x in scores), (pid, scores)

    def test_load_profiles_reads_null_as_nan_and_confidence_from_opps(self) -> None:
        from similarity.engines.baserunner_steal_similarity import BaserunnerStealSimilarityEngine

        class _Res:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class _Conn:
            def execute(self, sql, *a):
                if "information_schema" in sql:
                    return _Res(
                        [
                            ("asof_date",),
                            ("lead_primary_ft",),
                            ("lead_jump_ft",),
                            ("sample_second_base_opps",),
                        ]
                    )
                assert "bss.lead_primary_ft, bss.lead_jump_ft" in sql
                assert "bss.sample_second_base_opps" in sql
                return _Res(
                    [
                        # pid, season, attempts, opps, sar, sar2, suc, suc2, below, asof,
                        # lead, jump, opps_2b
                        (1, 2024, 0, 300, 0.0, None, None, None, True, None, 12.5, 3.1, 0),
                        (2, 2024, 20, 100, 0.2, 0.05, 0.8, None, False, None, None, None, 40),
                        (3, 2024, 0, 0, None, 0.0, None, None, True, None, None, None, 6),
                        (4, 2024, 3, 0, None, None, 2 / 3, None, True, None, None, None, 0),
                    ]
                )

        engine = BaserunnerStealSimilarityEngine(duckdb_path=":memory:")
        engine._load_profiles(_Conn(), [2024], include_below_minimum=True)
        p1, p2 = engine._profiles[(1, 2024)], engine._profiles[(2, 2024)]
        p3, p4 = engine._profiles[(3, 2024)], engine._profiles[(4, 2024)]
        assert p1.has_lead and p1.lead_vec.tolist() == pytest.approx([12.5, 3.1])
        assert np.isnan(p1.success_vec).all(), "never went: unmeasured, not 0.0"
        assert np.isnan(p1.tendency_vec[1]), "never on second: unmeasured"
        assert p1.eb_alpha == pytest.approx(300 / 350)
        assert not p2.has_lead and np.isnan(p2.lead_vec).all()
        assert p2.sample_second_base_opps == 40
        assert p2.eb_alpha == pytest.approx(140 / 190), "chances = first + second"
        assert p3.eb_alpha == pytest.approx(6 / 56), "second-base chances alone count"
        assert p4.eb_alpha == pytest.approx(3 / 53), "an attempt is a chance"

    def test_a_thin_runners_lead_shrinks_toward_the_league_mean(self) -> None:
        from similarity.engines.baserunner_steal_similarity import BaserunnerStealSimilarityEngine

        engine = BaserunnerStealSimilarityEngine(duckdb_path=":memory:")
        thin = _steal_profile(1, [0.1, 0.02], [0.8, 0.5], lead=[14.0, 5.0])
        thin.sample_first_base_opps = 5
        thin.sample_steal_attempts = 0
        fat = _steal_profile(2, [0.1, 0.02], [0.8, 0.5], lead=[14.0, 5.0])
        fat.sample_first_base_opps = 300
        unmeasured = _steal_profile(3, [0.1, 0.02], [0.8, 0.5])
        engine._profiles = {(1, 2024): thin, (2, 2024): fat, (3, 2024): unmeasured}
        engine._league_avg["lead"][2024] = np.array([11.6, 3.4])
        engine._apply_shrinkage()
        # 5 / 55 = 9% his own: shrunk ~91% toward the league mean.
        assert thin.lead_vec[0] == pytest.approx(11.6 + (14.0 - 11.6) * 5 / 55)
        assert fat.lead_vec[0] == pytest.approx(11.6 + (14.0 - 11.6) * 300 / 350)
        assert thin.has_lead and fat.has_lead
        # An UNMEASURED lead is never shrunk: it stays NaN (see the next test).
        assert not unmeasured.has_lead and np.isnan(unmeasured.lead_vec).all()

    def test_unmeasured_leads_do_not_move_the_normalizers_statistics(self) -> None:
        """The review of 2026-09-16: filling an unmeasured lead with the league
        mean put every such runner at the exact mean inside the normalizer,
        deflated the lead's spread (-24 to -27%) and sharpened the kernel
        past the fitted bandwidth. The NaN must survive shrinkage."""
        from similarity.engines.baserunner_steal_similarity import (
            BaserunnerStealSimilarityEngine,
            FeatureNormalizer,
        )

        rng = np.random.default_rng(9)
        measured = [
            _steal_profile(
                pid,
                [0.1, 0.02],
                [0.8, 0.5],
                lead=[rng.normal(11.6, 0.9), rng.normal(3.4, 0.6)],
            )
            for pid in range(1, 301)
        ]
        unmeasured = [_steal_profile(pid, [0.1, 0.02], [0.8, 0.5]) for pid in range(1001, 1201)]
        engine = BaserunnerStealSimilarityEngine(duckdb_path=":memory:")
        engine._profiles = {(p.player_id, p.season): p for p in measured + unmeasured}
        engine._league_avg["lead"][2024] = np.array([11.6, 3.4])
        engine._apply_shrinkage()
        assert all(np.isnan(p.lead_vec).all() for p in unmeasured)
        norm_measured = FeatureNormalizer()
        norm_measured.fit(measured)
        norm_all = FeatureNormalizer()
        norm_all.fit(measured + unmeasured)
        assert norm_all.lead_std == pytest.approx(norm_measured.lead_std)
        assert norm_all.lead_mean == pytest.approx(norm_measured.lead_mean)
        # The old behaviour, for contrast: 200 runners parked at the exact mean
        # would have deflated the spread by roughly a quarter.
        parked = list(measured) + [
            _steal_profile(pid, [0.1, 0.02], [0.8, 0.5], lead=[11.6, 3.4])
            for pid in range(2001, 2201)
        ]
        norm_parked = FeatureNormalizer()
        norm_parked.fit(parked)
        assert (norm_parked.lead_std < 0.85 * norm_measured.lead_std).all()

    def test_a_missing_league_key_leaves_the_raw_lead_alone(self) -> None:
        from similarity.engines.baserunner_steal_similarity import BaserunnerStealSimilarityEngine

        engine = BaserunnerStealSimilarityEngine(duckdb_path=":memory:")
        p = _steal_profile(1, [0.1, 0.02], [0.8, 0.5], lead=[14.0, 5.0])
        p.sample_first_base_opps = 5
        engine._profiles = {(1, 2024): p}
        engine._league_avg["lead"][2024] = np.array([np.nan, np.nan])
        engine._apply_shrinkage()
        assert p.lead_vec.tolist() == pytest.approx([14.0, 5.0])

    def test_the_lead_sigma_applies_and_the_sentinel_keeps_the_default(self) -> None:
        from similarity.engines.baserunner_steal_similarity import (
            RBF_SIGMA_LEAD,
            BaserunnerStealSimilarityEngine,
        )
        from similarity.similarity_calibration import CalibrationReport

        engine = BaserunnerStealSimilarityEngine(duckdb_path=":memory:")
        engine.apply_calibration(CalibrationReport())
        assert engine._lead_rbf.sigma == RBF_SIGMA_LEAD
        engine.apply_calibration(CalibrationReport(sigma_baserunner_steal_lead=0.77))
        assert engine._lead_rbf.sigma == pytest.approx(0.77)


# ===========================================================================
# 5. The pitcher-hold model
# ===========================================================================


def _hold_profile(pid, out, hold=None, *, alpha=1.0):
    from similarity.engines.pitcher_steal_similarity import PitcherStealProfile

    kw = {}
    if hold is not None:
        kw = {"hold_vec": np.array(hold, dtype=np.float64), "has_hold": True}
    return PitcherStealProfile(
        pitcher_id=pid,
        season=2024,
        throws="R",
        sample_baserunner_events=200,
        sample_steal_attempts_against=20,
        outcome_vec=np.array(out, dtype=np.float64),
        eb_alpha=alpha,
        **kw,
    )


def _hold_engine(profiles):
    from similarity.engines.pitcher_steal_similarity import (
        OUTCOME_FEATURES,
        RBF_SIGMA_OUTCOME,
        EmpiricalBayesShrinkage,
        FeatureNormalizer,
        PitcherStealPartition,
        PitcherStealSimilarityEngine,
        WeightedRBFSimilarity,
    )

    engine = PitcherStealSimilarityEngine.__new__(PitcherStealSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {(p.pitcher_id, p.season): p for p in profiles}
    engine._league_avg = {"outcome": {}, "hold": {}}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._partition = PitcherStealPartition()
    engine._out_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_OUTCOME, np.array([w for _, w in OUTCOME_FEATURES])
    )
    # No ``_hold_rbf`` on purpose (see _steal_engine).
    engine._normalizer.fit(profiles)
    engine._partition.build(profiles, engine._normalizer)
    return engine


class TestPitcherHoldModel:
    def test_weights_sum_to_one_at_the_decided_split(self) -> None:
        from similarity.engines.pitcher_steal_similarity import (
            HOLD_FEATURES,
            WEIGHT_HOLD,
            WEIGHT_OUTCOME,
        )

        assert (WEIGHT_OUTCOME, WEIGHT_HOLD) == (0.35, 0.65)
        assert [f for f, _ in HOLD_FEATURES] == ["lead_allowed_primary_ft", "lead_allowed_jump_ft"]

    def test_no_hold_pairs_score_on_the_outcome_alone(self) -> None:
        engine = _hold_engine(
            [
                _hold_profile(1, [0.5, 0.3, 0.1]),
                _hold_profile(2, [0.5, 0.3, 0.1]),
                _hold_profile(3, [0.9, 0.1, 0.2]),
            ]
        )
        r = {x.pitcher_id: x for x in engine.query(1, 2024)}
        assert r[2].score == pytest.approx(1.0) and r[2].hold_score is None
        assert r[3].score < 1.0 and r[3].hold_score is None

    def test_measured_pairs_use_both_groups(self) -> None:
        engine = _hold_engine(
            [
                _hold_profile(1, [0.5, 0.3, 0.1], hold=[11.6, 4.2]),
                _hold_profile(2, [0.5, 0.3, 0.1], hold=[11.6, 4.2]),
                _hold_profile(3, [0.5, 0.3, 0.1], hold=[13.0, 6.0]),
                _hold_profile(4, [0.5, 0.3, 0.1]),
            ]
        )
        r = {x.pitcher_id: x for x in engine.query(1, 2024)}
        assert r[2].score == pytest.approx(1.0) and r[2].hold_score == pytest.approx(1.0)
        assert r[3].score < 1.0 and r[3].score > 0.35 and 0.0 < r[3].hold_score < 1.0
        assert r[4].score == pytest.approx(1.0) and r[4].hold_score is None

    def test_score_all_equals_query_pair(self) -> None:
        rng = np.random.default_rng(532)
        profiles = [
            _hold_profile(
                pid,
                rng.uniform(0, 1, 3),
                [rng.uniform(10, 13), rng.uniform(2, 6)] if pid % 2 else None,
                alpha=rng.uniform(0.3, 1.0),
            )
            for pid in range(1, 11)
        ]
        engine = _hold_engine(profiles)
        by_id = {x.pitcher_id: x for x in engine.query(1, 2024)}
        for other, res in by_id.items():
            pair = engine.query_pair((1, 2024), (other, 2024))
            assert pair.score == pytest.approx(res.score, abs=1e-12)
            assert (pair.hold_score is None) == (res.hold_score is None)
            assert engine.query_pair((other, 2024), (1, 2024)).score == pytest.approx(
                pair.score, abs=1e-12
            )

    def test_load_profiles_reads_null_as_nan(self) -> None:
        from similarity.engines.pitcher_steal_similarity import PitcherStealSimilarityEngine

        class _Res:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class _Conn:
            def execute(self, sql, *a):
                if "information_schema" in sql:
                    return _Res(
                        [("asof_date",), ("lead_allowed_primary_ft",), ("lead_allowed_jump_ft",)]
                    )
                return _Res(
                    [
                        (500, 2024, "R", 200, 20, 0.6, None, 0.1, False, None, 11.6, 4.2),
                        (501, 2024, "L", 20, 2, 0.4, 0.5, 0.07, False, None, None, None),
                    ]
                )

        engine = PitcherStealSimilarityEngine(duckdb_path=":memory:")
        engine._load_profiles(_Conn(), [2024])
        p500, p501 = engine._profiles[(500, 2024)], engine._profiles[(501, 2024)]
        assert p500.has_hold and p500.hold_vec.tolist() == pytest.approx([11.6, 4.2])
        assert np.isnan(p500.outcome_vec[1]), "a NULL caught-stealing rate is unmeasured"
        assert not p501.has_hold and np.isnan(p501.hold_vec).all()
        assert p501.eb_alpha == pytest.approx(20 / 45)  # events, prior 25: 56% league mean

    def test_a_thin_pitchers_hold_shrinks_on_baserunner_events(self) -> None:
        from similarity.engines.pitcher_steal_similarity import PitcherStealSimilarityEngine

        engine = PitcherStealSimilarityEngine(duckdb_path=":memory:")
        p = _hold_profile(500, [0.5, 0.3, 0.1], hold=[13.0, 6.0])
        p.sample_baserunner_events = 20
        unmeasured = _hold_profile(501, [0.5, 0.3, 0.1])
        engine._profiles = {(500, 2024): p, (501, 2024): unmeasured}
        engine._league_avg["hold"][2024] = np.array([11.6, 4.2])
        engine._league_avg["outcome"][2024] = np.array([np.nan, np.nan, np.nan])
        engine._apply_shrinkage()
        assert p.hold_vec[1] == pytest.approx(4.2 + (6.0 - 4.2) * 20 / 45)
        assert p.outcome_vec.tolist() == pytest.approx([0.5, 0.3, 0.1])  # no league key: raw
        # An unmeasured hold is never shrunk: it stays NaN, out of the normalizer.
        assert not unmeasured.has_hold and np.isnan(unmeasured.hold_vec).all()

    def test_the_hold_sigma_applies_and_the_sentinel_keeps_the_default(self) -> None:
        from similarity.engines.pitcher_steal_similarity import (
            RBF_SIGMA_HOLD,
            PitcherStealSimilarityEngine,
        )
        from similarity.similarity_calibration import CalibrationReport

        engine = PitcherStealSimilarityEngine(duckdb_path=":memory:")
        engine.apply_calibration(CalibrationReport())
        assert engine._hold_rbf.sigma == RBF_SIGMA_HOLD
        engine.apply_calibration(CalibrationReport(sigma_pitcher_steal_hold=0.66))
        assert engine._hold_rbf.sigma == pytest.approx(0.66)


# ===========================================================================
# 6. The advancement model: the masked kernel
# ===========================================================================


class TestAdvancementMaskedKernel:
    def test_the_aggression_feature_joins_at_its_repeat(self) -> None:
        from similarity.engines.baserunner_similarity import AGGRESSION_FEATURES

        assert AGGRESSION_FEATURES[-1] == ("xb_attempt_rate_above_expected", 0.760)
        assert len(AGGRESSION_FEATURES) == 7

    def test_a_fully_measured_pair_is_unchanged(self) -> None:
        from similarity.engines.baserunner_similarity import WeightedRBFSimilarity

        w = np.array([0.2, 0.3, 0.5])
        k = WeightedRBFSimilarity(sigma=1.0, reliability_weights=w)
        x, y = np.array([0.0, 1.0, 2.0]), np.array([0.5, 0.0, 2.5])
        diff = x - y
        expected = float(np.exp(-k.gamma * np.dot(k.weights * diff, diff)))
        assert k.score(x, y) == pytest.approx(expected)
        assert k.score_batch(x, y[None, :])[0] == pytest.approx(expected)

    def test_a_missing_feature_drops_out_of_distance_and_normalisation(self) -> None:
        from similarity.engines.baserunner_similarity import WeightedRBFSimilarity

        w = np.array([0.2, 0.3, 0.5])
        k = WeightedRBFSimilarity(sigma=1.0, reliability_weights=w)
        x = np.array([0.0, 1.0, np.nan])
        y = np.array([0.5, 0.0, 2.5])
        # The weighted average over the two present features only.
        d2 = (0.2 * 0.25 + 0.3 * 1.0) / (0.2 + 0.3)
        expected = float(np.exp(-k.gamma * d2))
        assert k.score(x, y) == pytest.approx(expected)
        assert k.score_batch(x, np.array([y, y]))[1] == pytest.approx(expected)
        # The old kernel read the missing feature as an exact match and inflated
        # the score; the masked kernel does not.
        inflated = float(np.exp(-k.gamma * (0.2 * 0.25 + 0.3 * 1.0)))
        assert k.score(x, y) < inflated

    def test_normalisation_keeps_a_nan(self) -> None:
        from similarity.engines.baserunner_similarity import (
            AGGRESSION_FEATURES,
            BaserunnerProfile,
            FeatureNormalizer,
        )

        n_agg = len(AGGRESSION_FEATURES)
        profiles = []
        for pid in range(1, 6):
            agg = np.full(n_agg, 0.2 + pid * 0.05)
            if pid % 2:
                agg[-1] = np.nan
            profiles.append(
                BaserunnerProfile(
                    player_id=pid,
                    season=2024,
                    sample_advancement_opps=40,
                    speed_vec=np.array([27.0 + pid * 0.2]),
                    aggression_vec=agg,
                    success_vec=np.full(5, 0.9),
                )
            )
        norm = FeatureNormalizer()
        norm.fit(profiles)
        z = norm.normalize_aggression(profiles[0].aggression_vec)
        assert np.isnan(z[-1]) and np.isfinite(z[:-1]).all()
        assert np.isfinite(norm.aggression_mean).all() and np.isfinite(norm.aggression_std).all()

    def test_a_stale_six_weight_calibration_is_refused(self) -> None:
        from similarity.engines.baserunner_similarity import (
            AGGRESSION_FEATURES,
            BaserunnerSimilarityEngine,
        )
        from similarity.similarity_calibration import CalibrationReport

        eng = BaserunnerSimilarityEngine(duckdb_path=":memory:")
        stale = CalibrationReport(
            sigma_baserunner_aggression=0.9,
            reliability_weights_baserunner_aggression=np.ones(6) / 6,
        )
        eng.apply_calibration(stale)
        expected = np.array([w for _, w in AGGRESSION_FEATURES])
        assert eng._agg_rbf.weights == pytest.approx(expected / expected.sum())
        assert eng._agg_rbf.sigma == pytest.approx(0.9)  # the sigma still applies
        fresh = CalibrationReport(reliability_weights_baserunner_aggression=np.ones(7) / 7)
        eng.apply_calibration(fresh)
        assert eng._agg_rbf.weights == pytest.approx(np.ones(7) / 7)

    def test_load_profiles_reads_the_feature_as_nan_when_null(self) -> None:
        from similarity.engines.baserunner_similarity import BaserunnerSimilarityEngine

        class _Res:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class _Conn:
            def execute(self, sql, *a):
                if "information_schema" in sql:
                    return _Res([("asof_date",), ("xb_attempt_rate_above_expected",)])
                assert "brm.xb_attempt_rate_above_expected" in sql
                base = (
                    2024,
                    40,
                    28.0,
                    0.4,
                    0.3,
                    0.5,
                    0.2,
                    0.3,
                    0.6,
                    0.9,
                    0.8,
                    0.9,
                    0.7,
                    0.8,
                    10,
                    8,
                    6,
                    4,
                    False,
                    None,
                )
                return _Res([(1, *base, 0.05), (2, *base, None)])

        engine = BaserunnerSimilarityEngine(duckdb_path=":memory:")
        engine._load_profiles(_Conn(), [2024])
        assert engine._profiles[(1, 2024)].aggression_vec[-1] == pytest.approx(0.05)
        assert np.isnan(engine._profiles[(2, 2024)].aggression_vec[-1])
        assert len(engine._profiles[(2, 2024)].aggression_vec) == 7

    def test_an_unmeasured_feature_survives_shrinkage_whatever_the_league_row(self) -> None:
        """The review of 2026-09-16: the shrinkage used to fill the unmeasured
        Savant feature with the league mean whenever the league row carried the
        key — every in-window season after the recompute — so the masked kernel
        never ran in production. The NaN must survive; a MEASURED value shrinks
        like every other aggression feature."""
        from similarity.engines.baserunner_similarity import (
            AGGRESSION_FEATURES,
            BaserunnerProfile,
            BaserunnerSimilarityEngine,
        )

        n_agg = len(AGGRESSION_FEATURES)
        eng = BaserunnerSimilarityEngine(duckdb_path=":memory:")
        agg = np.full(n_agg, 0.3)
        agg[-1] = np.nan
        p = BaserunnerProfile(
            player_id=1,
            season=2024,
            sample_advancement_opps=5,
            speed_vec=np.array([27.0]),
            aggression_vec=agg.copy(),
            success_vec=np.full(5, 0.9),
        )
        measured = BaserunnerProfile(
            player_id=2,
            season=2024,
            sample_advancement_opps=5,
            speed_vec=np.array([27.0]),
            aggression_vec=np.full(n_agg, 0.3),
            success_vec=np.full(5, 0.9),
        )
        eng._profiles = {(1, 2024): p, (2, 2024): measured}
        league = np.full(n_agg, 0.25)
        league[-1] = np.nan  # a league row written before the Savant load
        eng._league_avg["aggression"][2024] = league
        eng._apply_shrinkage()
        assert np.isnan(p.aggression_vec[-1]), "no league key: the NaN survives (masked)"
        assert p.aggression_vec[0] == pytest.approx(0.25 + (0.3 - 0.25) * 5 / 20)
        p.aggression_vec = agg.copy()
        measured.aggression_vec = np.full(n_agg, 0.3)
        league[-1] = 0.02  # the row after the recompute carries the key
        eng._apply_shrinkage()
        assert np.isnan(p.aggression_vec[-1]), "the NaN still survives: the kernel drops it"
        assert measured.aggression_vec[-1] == pytest.approx(0.02 + (0.3 - 0.02) * 5 / 20)
        # And the pair is scored over the six shared features only.
        eng._normalizer.fit([p, measured])
        eng._partition.build([p, measured], eng._normalizer)
        r = eng.query_pair((1, 2024), (2, 2024))
        assert r is not None and 0.0 < r.aggression_score <= 1.0


# ===========================================================================
# 7. The calibrator
# ===========================================================================


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, present: list[str], rows: list[tuple]) -> None:
        self._present = present
        self._rows = rows
        self.queries: list[str] = []

    def execute(self, sql: str, *args):
        self.queries.append(sql)
        if "information_schema" in sql:
            return _FakeResult([(c,) for c in self._present])
        return _FakeResult(self._rows)


class TestCalibrator:
    def test_the_two_sigmas_round_trip_through_json(self) -> None:
        from similarity.similarity_calibration import CalibrationReport

        r = CalibrationReport(sigma_baserunner_steal_lead=0.81, sigma_pitcher_steal_hold=0.67)
        back = CalibrationReport.from_json(r.to_json())
        assert back.sigma_baserunner_steal_lead == pytest.approx(0.81)
        assert back.sigma_pitcher_steal_hold == pytest.approx(0.67)
        assert "BR-steal lead" in r.summary() and "P-steal hold" in r.summary()

    def test_the_steal_fit_uses_measured_rows_only(self) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(5)
        rows = []
        for i in range(120):
            lead = (
                (float(rng.normal(11.6, 0.9)), float(rng.normal(3.4, 0.6)))
                if i % 3
                else (None, None)
            )
            rows.append(
                (
                    rng.uniform(0, 0.3),
                    rng.uniform(0, 0.1),
                    rng.uniform(0.5, 1),
                    rng.uniform(0.4, 1),
                    *lead,
                )
            )
        conn = _FakeConn(["lead_primary_ft", "lead_jump_ft"], rows)
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_baserunner_steal_params(conn, [2024], 0.5, CalibrationReport())
        assert report.sigma_baserunner_steal_tendency > 0.0
        assert report.sigma_baserunner_steal_lead > 0.0
        assert any("lead_primary_ft, lead_jump_ft" in q for q in conn.queries)
        # The sigma is the one fitted on the MEASURED rows alone …
        measured = np.array([[r[4], r[5]] for r in rows if r[4] is not None])
        expected = cal._fit_sigma(cal._zscore_matrix(measured), 0.5)
        assert report.sigma_baserunner_steal_lead == pytest.approx(expected)
        # … and adding more unmeasured rows does not move it (a zero-filled fit would).
        more = rows + [(0.1, 0.05, 0.7, 0.6, None, None)] * 60
        again = cal._calibrate_baserunner_steal_params(
            _FakeConn(["lead_primary_ft", "lead_jump_ft"], more), [2024], 0.5, CalibrationReport()
        )
        assert again.sigma_baserunner_steal_lead == pytest.approx(expected)

    def test_the_steal_fit_keeps_the_sentinel_without_a_lead(self) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(6)
        rows = [
            (
                rng.uniform(0, 0.3),
                rng.uniform(0, 0.1),
                rng.uniform(0.5, 1),
                rng.uniform(0.4, 1),
                None,
                None,
            )
            for _ in range(60)
        ]
        conn = _FakeConn([], rows)  # a pre-0029 database
        report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_baserunner_steal_params(
            conn, [2024], 0.5, CalibrationReport()
        )
        assert report.sigma_baserunner_steal_tendency > 0.0
        assert report.sigma_baserunner_steal_lead == 0.0
        assert any("NULL AS lead_primary_ft" in q for q in conn.queries)

    def test_the_hold_fit_mirrors_the_steal_fit(self) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(7)
        rows = []
        for i in range(120):
            hold = (
                (float(rng.normal(11.6, 0.5)), float(rng.normal(4.2, 0.7)))
                if i % 2
                else (None, None)
            )
            rows.append((rng.uniform(0, 1), rng.uniform(0, 1), rng.uniform(0, 0.2), *hold))
        conn = _FakeConn(["lead_allowed_primary_ft", "lead_allowed_jump_ft"], rows)
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_pitcher_steal_params(conn, [2024], 0.5, CalibrationReport())
        assert report.sigma_pitcher_steal_outcome > 0.0
        assert report.sigma_pitcher_steal_hold > 0.0
        measured = np.array([[r[3], r[4]] for r in rows if r[3] is not None])
        expected = cal._fit_sigma(cal._zscore_matrix(measured), 0.5)
        assert report.sigma_pitcher_steal_hold == pytest.approx(expected)
        more = rows + [(0.5, 0.5, 0.1, None, None)] * 60
        again = cal._calibrate_pitcher_steal_params(
            _FakeConn(["lead_allowed_primary_ft", "lead_allowed_jump_ft"], more),
            [2024],
            0.5,
            CalibrationReport(),
        )
        assert again.sigma_pitcher_steal_hold == pytest.approx(expected)
        report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_pitcher_steal_params(
            _FakeConn([], [r[:3] + (None, None) for r in rows]), [2024], 0.5, CalibrationReport()
        )
        assert report.sigma_pitcher_steal_hold == 0.0

    def test_the_aggression_fit_drops_unmeasured_rows_and_writes_seven_weights(self) -> None:
        from similarity.engines.baserunner_similarity import AGGRESSION_FEATURES
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(8)
        rows = []
        for i in range(120):
            agg = rng.normal(size=6).tolist()
            xb = float(rng.normal(0, 0.05)) if i % 4 else None
            suc = rng.normal(size=5).tolist()
            rows.append((3000 + i % 40, 2024, 100 + i, 27.0 + rng.normal(), *agg, xb, *suc))
        conn = _FakeConn(["xb_attempt_rate_above_expected"], rows)
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_baserunner_params(conn, [2024], 0.5, CalibrationReport())
        assert report.sigma_baserunner_aggression > 0.0
        assert report.reliability_weights_baserunner_aggression is not None
        assert len(report.reliability_weights_baserunner_aggression) == len(AGGRESSION_FEATURES)
        assert any(
            "xb_attempt_rate_above_expected" in q and "NULL AS" not in q for q in conn.queries
        )
        # The aggression sigma is the one fitted on the rows with every feature.
        measured = np.array([[*r[4:11]] for r in rows if r[10] is not None], dtype=np.float64)
        expected = cal._fit_sigma(cal._zscore_matrix(measured), 0.5)
        assert report.sigma_baserunner_aggression == pytest.approx(expected)
        more = rows + [
            (
                3000 + i % 40,
                2024,
                100 + i,
                27.0,
                *rng.normal(size=6).tolist(),
                None,
                *rng.normal(size=5).tolist(),
            )
            for i in range(60)
        ]
        again = cal._calibrate_baserunner_params(
            _FakeConn(["xb_attempt_rate_above_expected"], more), [2024], 0.5, CalibrationReport()
        )
        assert again.sigma_baserunner_aggression == pytest.approx(expected)
