"""SIM-510 — transition columns on sim.outcome_pool + the advancement
opportunity pool.

The fielding-transition epic (SIM-510→513) makes the drawn outcome-pool row BE
the play, so the pool must carry the whole base-state transition. Three layers
under test, mirroring the seams:

  * the destination SQL helpers (``sql_runner_dest`` / ``sql_batter_dest``) —
    the encoding the SIM-511 draw applies;
  * the computor builds (``_build_outcome_pool``'s transition tail +
    ``_build_advancement_opportunity_pool``) run against the REAL DDL text
    from ``db/schemas/02_duckdb_schema.sql``, so a positional-INSERT drift
    between the DDL and the builder SELECT turns these red (the 0012 trap);
  * the artifact round-trip (``build_advancement_pool_artifact`` /
    the batted-ball transition columns -> ``EngineArtifacts.load``),
    including the legacy-bundle fallback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import numpy as np

from pipeline.batch.engine_artifacts import (
    EngineArtifacts,
    build_advancement_pool_artifact,
    build_battedball_pool_artifact,
)
from pipeline.batch.player_profile_computor import PlayerProfileComputor
from pipeline.statcast_events import sql_batter_dest, sql_runner_dest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SQL = (REPO_ROOT / "db" / "schemas" / "02_duckdb_schema.sql").read_text(encoding="utf-8")


def _real_ddl(table: str) -> str:
    """The live CREATE TABLE statement for ``table`` from the schema file.

    Deliberately not a copy: executing the real DDL text is what makes the
    positional-INSERT tests below capable of failing when the DDL and the
    builder SELECT drift apart.
    """
    start = SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = SCHEMA_SQL.index("\n);", start)
    return SCHEMA_SQL[start : end + 3]


# ---------------------------------------------------------------------------
# The destination helpers
# ---------------------------------------------------------------------------


def _dest_conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE p ("
        "on_1b INTEGER, on_2b INTEGER, on_3b INTEGER, "
        "post_on_1b INTEGER, post_on_2b INTEGER, post_on_3b INTEGER, "
        "runner_1b_scored BOOLEAN, runner_2b_scored BOOLEAN, runner_3b_scored BOOLEAN, "
        "runs_on_pitch SMALLINT, events VARCHAR, batter INTEGER)"
    )
    return c


def _dest(c, expr: str):
    return c.execute(f"SELECT {expr} FROM p").fetchone()[0]


class TestRunnerDest:
    def test_no_runner_is_null(self):
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (NULL,NULL,NULL,NULL,NULL,NULL,FALSE,FALSE,FALSE,0,'single',9)"
        )
        assert _dest(c, sql_runner_dest("1b")) is None

    def test_a_scored_runner_is_4(self):
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (NULL,22,NULL,NULL,NULL,NULL,FALSE,TRUE,FALSE,1,'single',9)"
        )
        assert _dest(c, sql_runner_dest("2b")) == 4

    def test_an_advanced_runner_reads_his_post_base(self):
        c = _dest_conn()
        c.execute("INSERT INTO p VALUES (11,NULL,NULL,NULL,NULL,11,FALSE,FALSE,FALSE,0,'single',9)")
        assert _dest(c, sql_runner_dest("1b")) == 3

    def test_a_stranded_runner_keeps_his_base(self):
        # The ETL initializes post = pre, so a runner untouched by the play
        # (an inning-ending third out included) reads as held — never retired.
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (NULL,NULL,33,NULL,NULL,33,FALSE,FALSE,FALSE,0,'field_out',9)"
        )
        assert _dest(c, sql_runner_dest("3b")) == 3

    def test_a_vanished_runner_was_retired(self):
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (11,NULL,NULL,9,NULL,NULL,FALSE,FALSE,FALSE,0,"
            "'grounded_into_double_play',9)"
        )
        assert _dest(c, sql_runner_dest("1b")) == 0

    def test_a_null_scored_flag_reads_false(self):
        c = _dest_conn()
        c.execute("INSERT INTO p VALUES (11,NULL,NULL,11,NULL,NULL,NULL,NULL,NULL,0,'single',9)")
        assert _dest(c, sql_runner_dest("1b")) == 1


class TestBatterDest:
    def test_the_batter_reads_his_post_base(self):
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (NULL,NULL,NULL,NULL,9,NULL,FALSE,FALSE,FALSE,0,'double',9)"
        )
        assert _dest(c, sql_batter_dest()) == 2

    def test_a_home_run_is_4(self):
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (NULL,NULL,NULL,NULL,NULL,NULL,FALSE,FALSE,FALSE,1,'home_run',9)"
        )
        assert _dest(c, sql_batter_dest()) == 4

    def test_runs_accounting_catches_an_inside_the_park_trip(self):
        # Two runs, one named scorer: the extra run is the batter's own trip
        # (a compound-error play). He is on no post base yet not out.
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (NULL,NULL,33,NULL,NULL,NULL,FALSE,FALSE,TRUE,2,'field_error',9)"
        )
        assert _dest(c, sql_batter_dest()) == 4

    def test_a_retired_batter_is_0(self):
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (NULL,NULL,NULL,NULL,NULL,NULL,FALSE,FALSE,FALSE,0,'field_out',9)"
        )
        assert _dest(c, sql_batter_dest()) == 0

    def test_the_batter_expression_override(self):
        c = _dest_conn()
        c.execute(
            "INSERT INTO p VALUES (NULL,NULL,NULL,77,NULL,NULL,FALSE,FALSE,FALSE,0,'single',9)"
        )
        assert _dest(c, sql_batter_dest(batter_expr="77")) == 1


# ---------------------------------------------------------------------------
# The outcome-pool builder (the transition tail + the positional guard)
# ---------------------------------------------------------------------------


def _op_conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches ("
        "game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, "
        "game_date DATE, season SMALLINT, stand VARCHAR, type VARCHAR, events VARCHAR, "
        "launch_speed FLOAT, launch_angle FLOAT, spray_angle FLOAT, bb_type VARCHAR, "
        "hit_distance_sc FLOAT, hc_x FLOAT, hc_y FLOAT, "
        "fielded_by INTEGER, fielding_error INTEGER, "
        "fielder_2 INTEGER, fielder_3 INTEGER, fielder_4 INTEGER, fielder_5 INTEGER, "
        "fielder_6 INTEGER, fielder_7 INTEGER, fielder_8 INTEGER, fielder_9 INTEGER, "
        "runs_on_pitch SMALLINT, venue_id INTEGER, data_quality_flag BOOLEAN, "
        "on_1b INTEGER, on_2b INTEGER, on_3b INTEGER, "
        "post_on_1b INTEGER, post_on_2b INTEGER, post_on_3b INTEGER, "
        "runner_1b_scored BOOLEAN, runner_2b_scored BOOLEAN, runner_3b_scored BOOLEAN, "
        "runner_1b_out_advancing BOOLEAN, runner_2b_out_advancing BOOLEAN, "
        "runner_3b_out_advancing BOOLEAN)"
    )
    c.execute("CREATE SCHEMA sim")
    c.execute(
        "CREATE TABLE sim.pitch_pool ("
        "pitch_id BIGINT, game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, "
        "game_date DATE, season SMALLINT, pitcher_id INTEGER, p_throws VARCHAR, "
        "batter_id INTEGER, stand VARCHAR, velo FLOAT, ivb FLOAT, hb FLOAT, "
        "spin_rate FLOAT, spin_axis FLOAT, release_x FLOAT, release_z FLOAT, "
        "release_ext FLOAT, plate_x FLOAT, plate_z FLOAT, zone SMALLINT, "
        "count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT, "
        "runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT, "
        "events VARCHAR, outcome_type VARCHAR)"
    )
    c.execute(_real_ddl("sim.outcome_pool"))
    c.execute(
        "CREATE TABLE sim.pool_build_metadata ("
        "pool_name VARCHAR, season SMALLINT, row_count BIGINT, "
        "source_max_game_date DATE, source_row_count BIGINT, "
        "recency_ref_season SMALLINT, builder_version VARCHAR, "
        "built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (pool_name, season))"
    )
    return c


def _op_pitch(
    c,
    pk,
    ab,
    *,
    events="single",
    type_="D",
    batter=9,
    on_1b=None,
    on_2b=None,
    on_3b=None,
    post_1b=None,
    post_2b=None,
    post_3b=None,
    sc_1b=False,
    sc_2b=False,
    sc_3b=False,
    adv_1b=False,
    adv_2b=False,
    adv_3b=False,
    runs=0,
    bb_type="ground_ball",
    la=10.0,
) -> int:
    pid = pk * 10000 + ab * 100 + 1
    c.execute(
        "INSERT INTO pg.raw.pitches VALUES "
        "(?,?,1,DATE '2024-06-01',2024,'R',?,?,95.0,?,10.0,?,150.0,100.0,100.0,"
        "906,NULL,902,903,904,905,906,907,908,909,?,15,FALSE,"
        "?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            pk,
            ab,
            type_,
            events,
            la,
            bb_type,
            runs,
            on_1b,
            on_2b,
            on_3b,
            post_1b,
            post_2b,
            post_3b,
            sc_1b,
            sc_2b,
            sc_3b,
            adv_1b,
            adv_2b,
            adv_3b,
        ],
    )
    c.execute(
        "INSERT INTO sim.pitch_pool VALUES "
        "(?,?,?,1,DATE '2024-06-01',2024,901,'R',?, 'R',95.0,10.0,5.0,2200.0,180.0,"
        "-1.0,6.0,6.5,0.1,2.5,5,1,1,0,0,1,0,?,'in_play')",
        [pid, pk, ab, batter, events],
    )
    return pid


def _op_build(c) -> None:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = c
    comp._build_outcome_pool([2024], incremental=False)


class TestTheOutcomePoolTransitionTail:
    def test_the_positional_insert_matches_the_ddl(self):
        """The INSERT has no column list, so the SELECT order must match the
        DDL exactly — the 0012 trap. Every SIM-510 column is checked BY NAME
        against a row whose values are all distinct."""
        c = _op_conn()
        _op_pitch(
            c,
            1,
            1,
            events="single",
            type_="D",
            batter=9,
            on_1b=11,
            on_2b=22,
            on_3b=33,
            post_1b=9,
            post_2b=11,
            post_3b=22,
            sc_3b=True,
            runs=1,
        )
        _op_build(c)
        row = c.execute(
            "SELECT on_1b, on_2b, on_3b, post_on_1b, post_on_2b, post_on_3b, "
            "runner_1b_scored, runner_2b_scored, runner_3b_scored, "
            "runner_1b_out_advancing, runner_2b_out_advancing, runner_3b_out_advancing, "
            "runner_1b_dest, runner_2b_dest, runner_3b_dest, batter_dest, "
            "dest_outs_consistent, venue_id, fielder_player_id, result_hits, result_outs "
            "FROM sim.outcome_pool"
        ).fetchone()
        assert row == (
            11,
            22,
            33,
            9,
            11,
            22,
            False,
            False,
            True,
            False,
            False,
            False,
            2,
            3,
            4,
            1,
            True,
            15,
            906,
            1,
            0,
        )

    def test_a_double_play_names_the_retired_runner(self):
        c = _op_conn()
        _op_pitch(
            c,
            1,
            2,
            events="grounded_into_double_play",
            type_="X",
            batter=9,
            on_1b=11,
            post_1b=None,
        )
        _op_build(c)
        row = c.execute(
            "SELECT runner_1b_dest, batter_dest, result_outs, dest_outs_consistent "
            "FROM sim.outcome_pool"
        ).fetchone()
        assert row == (0, 0, 2, True)

    def test_a_reach_on_error_carries_batter_reached_truth(self):
        c = _op_conn()
        _op_pitch(c, 1, 3, events="field_error", type_="D", batter=9, post_1b=9)
        _op_build(c)
        row = c.execute(
            "SELECT batter_dest, result_hits, result_outs, dest_outs_consistent "
            "FROM sim.outcome_pool"
        ).fetchone()
        assert row == (1, 0, 0, True)

    def test_a_batter_out_stretching_is_a_single_with_a_hidden_out(self):
        c = _op_conn()
        _op_pitch(c, 1, 4, events="single", type_="X", batter=9, post_1b=None)
        _op_build(c)
        row = c.execute(
            "SELECT batter_dest, result_hits, result_outs, dest_outs_consistent "
            "FROM sim.outcome_pool"
        ).fetchone()
        assert row == (0, 1, 1, True)

    def test_an_inconsistent_transition_is_flagged(self):
        # A single (zero outs recorded) whose runner nonetheless vanished:
        # the bodies-vs-outs guard must read FALSE.
        c = _op_conn()
        _op_pitch(c, 1, 5, events="single", type_="D", batter=9, on_1b=11, post_1b=9)
        _op_build(c)
        row = c.execute(
            "SELECT runner_1b_dest, dest_outs_consistent FROM sim.outcome_pool"
        ).fetchone()
        assert row == (0, False)


# ---------------------------------------------------------------------------
# The advancement-opportunity builder
# ---------------------------------------------------------------------------


def _adv_conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches (game_date DATE, season SMALLINT, data_quality_flag BOOLEAN)"
    )
    c.execute("CREATE SCHEMA sim")
    c.execute(_real_ddl("sim.outcome_pool"))
    c.execute(_real_ddl("sim.advancement_opportunity_pool"))
    c.execute(
        "CREATE TABLE sim.pool_build_metadata ("
        "pool_name VARCHAR, season SMALLINT, row_count BIGINT, "
        "source_max_game_date DATE, source_row_count BIGINT, "
        "recency_ref_season SMALLINT, builder_version VARCHAR, "
        "built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (pool_name, season))"
    )
    return c


_ADV_OP_COLS = (
    "pitch_id, game_pk, at_bat_number, pitch_number, game_date, season, "
    "pitcher_id, p_throws, stand, count_balls, count_strikes, runners_state, "
    "inning, score_diff, "
    "batter_id, events, exit_velo, launch_angle, spray_angle, hit_distance, "
    "bb_type, result_hits, result_outs, outs, fielder_player_id, fielded_by_position, "
    "on_1b, on_2b, on_3b, post_on_1b, post_on_2b, post_on_3b, "
    "runner_1b_out_advancing, runner_2b_out_advancing, runner_3b_out_advancing, "
    "runner_1b_dest, runner_2b_dest, runner_3b_dest, batter_dest, dest_outs_consistent"
)


def _op_row(
    c,
    pid,
    *,
    events="single",
    batter=9,
    bb_type="ground_ball",
    result_hits=1,
    result_outs=0,
    outs=0,
    on_1b=None,
    on_2b=None,
    on_3b=None,
    post_2b=None,
    post_3b=None,
    adv_1b=False,
    adv_2b=False,
    adv_3b=False,
    d1=None,
    d2=None,
    d3=None,
    bd=1,
    consistent=True,
) -> None:
    """Insert one synthetic transition row directly into sim.outcome_pool."""
    c.execute(
        f"INSERT INTO sim.outcome_pool ({_ADV_OP_COLS}) VALUES "
        "(?, 1, ?, 1, DATE '2024-06-01', 2024, 901, 'R', 'R', 1, 1, 0, 1, 0, "
        "?, ?, 95.0, 25.0, -10.0, 250.0, ?, "
        "?, ?, ?, 907, 7, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            pid,
            pid,
            batter,
            events,
            bb_type,
            result_hits,
            result_outs,
            outs,
            on_1b,
            on_2b,
            on_3b,
            post_2b,
            post_3b,
            adv_1b,
            adv_2b,
            adv_3b,
            d1,
            d2,
            d3,
            bd,
            consistent,
        ],
    )


def _adv_build(c) -> None:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = c
    comp._build_advancement_opportunity_pool([2024], incremental=False)


def _adv_rows(c, *cols: str) -> list[tuple]:
    return c.execute(
        f"SELECT {', '.join(cols)} FROM sim.advancement_opportunity_pool "
        "ORDER BY pitch_id, scenario, from_base"
    ).fetchall()


class TestTheAdvancementBuilder:
    def test_first_to_third_on_a_single(self):
        c = _adv_conn()
        # Went: dest 3. Held: dest 2. Blocked by a body on 3B: no row.
        _op_row(c, 1, on_1b=11, d1=3, post_3b=11)
        _op_row(c, 2, on_1b=11, d1=2, post_2b=11)
        _op_row(c, 3, on_1b=11, d1=2, post_2b=11, on_3b=33, d3=3, post_3b=33, bd=1)
        _adv_build(c)
        rows = _adv_rows(
            c,
            "pitch_id",
            "scenario",
            "from_base",
            "target_base",
            "runner_id",
            "attempted",
            "safe",
            "error_extra",
        )
        assert (1, 1, 1, 3, 11, True, True, False) in rows
        assert (2, 1, 1, 3, 11, False, False, False) in rows
        assert all(r[0] != 3 or r[1] != 1 for r in rows)

    def test_a_runner_thrown_out_advancing_is_an_attempted_out(self):
        c = _adv_conn()
        _op_row(c, 4, on_1b=11, d1=0, adv_1b=True, result_outs=1)
        _adv_build(c)
        rows = _adv_rows(c, "scenario", "attempted", "safe")
        assert (1, True, False) in rows

    def test_second_to_home_on_a_single(self):
        c = _adv_conn()
        # Scored: attempted+safe. A runner HOLDING 3B blocks the opportunity.
        _op_row(c, 5, on_2b=22, d2=4)
        _op_row(c, 6, on_2b=22, d2=3, post_3b=22, on_3b=33, d3=3)
        _adv_build(c)
        rows = _adv_rows(c, "pitch_id", "scenario", "attempted", "safe")
        assert (5, 2, True, True) in rows
        assert all(r[0] != 6 or r[1] != 2 for r in rows)

    def test_first_to_home_on_a_double(self):
        c = _adv_conn()
        _op_row(c, 7, events="double", result_hits=2, on_1b=11, d1=4, bd=2)
        _adv_build(c)
        rows = _adv_rows(c, "scenario", "from_base", "target_base", "attempted", "safe")
        assert (3, 1, 4, True, True) in rows

    def test_tag_up_from_third_scores_and_a_doubled_off_runner_is_no_decision(self):
        c = _adv_conn()
        # The sac fly: batter out on a fly ball, runner on 3B scores.
        _op_row(
            c,
            8,
            events="sac_fly",
            bb_type="fly_ball",
            result_hits=0,
            result_outs=1,
            on_3b=33,
            d3=4,
            bd=0,
        )
        # The liner double-off: runner erased RETURNING (dest 0, no adv flag).
        _op_row(
            c,
            9,
            events="double_play",
            bb_type="line_drive",
            result_hits=0,
            result_outs=2,
            on_3b=33,
            d3=0,
            bd=0,
        )
        _adv_build(c)
        rows = _adv_rows(c, "pitch_id", "scenario", "from_base", "attempted", "safe")
        assert (8, 4, 3, True, True) in rows
        assert all(r[0] != 9 for r in rows)

    def test_no_tag_decision_when_the_catch_ends_the_inning(self):
        c = _adv_conn()
        _op_row(
            c,
            10,
            events="field_out",
            bb_type="fly_ball",
            result_hits=0,
            result_outs=1,
            outs=2,
            on_3b=33,
            d3=3,
            post_3b=33,
            bd=0,
        )
        _adv_build(c)
        assert _adv_rows(c, "scenario") == []

    def test_the_batter_stretch_on_a_single(self):
        c = _adv_conn()
        # Stretched to 2B: attempted+safe. Held at 1B: not attempted.
        # Out stretching (dest 0 on a single): attempted, not safe.
        _op_row(c, 11, bd=2)
        _op_row(c, 12, bd=1)
        _op_row(c, 13, bd=0, result_outs=1)
        # 2B occupied ahead: no decision.
        _op_row(c, 14, bd=1, on_1b=11, d1=2, post_2b=11)
        _adv_build(c)
        rows = _adv_rows(c, "pitch_id", "scenario", "target_base", "attempted", "safe")
        s5 = [r for r in rows if r[1] == 5]
        assert (11, 5, 2, True, True) in s5
        assert (12, 5, 2, False, False) in s5
        assert (13, 5, 2, True, False) in s5
        assert all(r[0] != 14 for r in s5)

    def test_an_inconsistent_transition_row_feeds_no_decision(self):
        c = _adv_conn()
        _op_row(c, 15, on_1b=11, d1=3, post_3b=11, consistent=False)
        _adv_build(c)
        assert _adv_rows(c, "scenario") == []

    def test_the_positional_insert_matches_the_ddl(self):
        """Every advancement-pool column checked BY NAME (the 0012 trap)."""
        c = _adv_conn()
        _op_row(c, 16, on_1b=11, d1=4)  # error-extra: scored from 1B on a single
        _adv_build(c)
        row = c.execute(
            "SELECT pitch_id, game_pk, at_bat_number, pitch_number, season, "
            "scenario, from_base, target_base, runner_id, fielder_id, fielder_pos, "
            "outs, exit_velo, launch_angle, spray_angle, hit_distance, "
            "attempted, safe, error_extra "
            "FROM sim.advancement_opportunity_pool WHERE scenario = 1"
        ).fetchone()
        assert row == (
            16,
            1,
            16,
            1,
            2024,
            1,
            1,
            3,
            11,
            907,
            7,
            0,
            95.0,
            25.0,
            -10.0,
            250.0,
            True,
            True,
            True,
        )

    def test_metadata_recorded_under_the_pool_name(self):
        c = _adv_conn()
        # One single, runner 1st->3rd: TWO decisions — the runner's (scenario
        # 1) and the batter's non-attempt stretch (scenario 5, 2B open).
        _op_row(c, 17, on_1b=11, d1=3, post_3b=11)
        _adv_build(c)
        got = c.execute("SELECT pool_name, row_count FROM sim.pool_build_metadata").fetchone()
        assert got == ("advancement_opportunity_pool", 2)


# ---------------------------------------------------------------------------
# The artifact round-trip
# ---------------------------------------------------------------------------


def _minimal_pitch_pool_manifest(tmp_path) -> None:
    """load() requires the PITCH pool's files for both hands; zero rows do."""
    d = tmp_path / "pitch_pool"
    os.makedirs(d, exist_ok=True)
    with open(d / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"seasons": [2024], "counts": {"L": 0, "R": 0}}, fh)
    con = duckdb.connect(":memory:")
    for hand in ("L", "R"):
        np.save(d / f"{hand}.geom.npy", np.zeros((0, 3), dtype=np.float32))
        np.save(d / f"{hand}.sit.npy", np.zeros((0, 6), dtype=np.float32))
        con.execute(
            "COPY (SELECT 0::BIGINT AS pitch_id, 0::BIGINT AS pitcher_id, "
            "0::BIGINT AS batter_id, 0::BIGINT AS season, ''::VARCHAR AS outcome_type, "
            "1.0::FLOAT AS recency_weight WHERE 1=0) "
            f"TO '{(d / f'{hand}.meta.parquet').as_posix()}' (FORMAT parquet)"
        )
    con.close()


class TestTheArtifactRoundTrip:
    def test_advancement_export_then_load(self, tmp_path):
        c = _adv_conn()
        _op_row(c, 1, on_1b=11, d1=3, post_3b=11)
        _op_row(c, 2, on_1b=11, d1=2, post_2b=11)
        _op_row(c, 3, bd=2)
        _adv_build(c)
        counts = build_advancement_pool_artifact(c, str(tmp_path), [2024])
        # Rows 1-3 are singles: 1_1_3 from rows 1+2; 5_0_2 from rows 1+3
        # (row 2's runner occupies 2B post-play, so no stretch decision).
        assert counts["1_1_3"] == 2
        assert counts["5_0_2"] == 2
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        p = art.adv_pools["1_1_3"]
        assert p.n == 2
        # Order-insensitive: the UNION ALL insert carries no ORDER BY.
        assert sorted(zip(p.attempted.tolist(), p.safe.tolist(), strict=True)) == [(0, 0), (1, 1)]
        assert p.runner_id.tolist() == [11, 11]
        assert p.feat.shape == (2, 5)

    def test_a_legacy_bundle_has_no_advancement_pools(self, tmp_path):
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        assert art.adv_pools == {}

    def test_a_pre_migration_db_skips_the_export(self, tmp_path):
        c = duckdb.connect(":memory:")
        c.execute("CREATE SCHEMA sim")
        assert build_advancement_pool_artifact(c, str(tmp_path), [2024]) == {}
        assert not (tmp_path / "advancement_pool").exists()

    def test_the_advancement_arrays_are_shareable(self, tmp_path):
        c = _adv_conn()
        _op_row(c, 1, on_1b=11, d1=3, post_3b=11)
        _adv_build(c)
        build_advancement_pool_artifact(c, str(tmp_path), [2024])
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        shared = art.extract_shared_arrays()
        assert "adv_pool.1_1_3.feat" in shared
        assert "adv_pool.1_1_3.attempted" in shared
        new_att = np.zeros_like(art.adv_pools["1_1_3"].attempted)
        art.attach_shared_views({"adv_pool.1_1_3.attempted": new_att})
        assert art.adv_pools["1_1_3"].attempted is new_att

    def test_battedball_meta_carries_the_transition_columns(self, tmp_path):
        c = _op_conn()
        _op_pitch(
            c,
            1,
            1,
            events="single",
            type_="D",
            batter=9,
            on_1b=11,
            post_1b=9,
            post_2b=11,
        )
        _op_build(c)
        build_battedball_pool_artifact(c, str(tmp_path), [2024])
        _minimal_pitch_pool_manifest(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        pool = art.bb_pools["R"]
        assert pool.r1_dest is not None
        assert pool.r1_dest.tolist() == [2]
        assert pool.r2_dest.tolist() == [-1]  # no runner on 2B pre-pitch
        assert pool.batter_dest.tolist() == [1]
        assert pool.dest_ok.tolist() == [1]
        shared = art.extract_shared_arrays()
        assert "bb_pool.R.r1_dest" in shared
        assert "bb_pool.R.dest_ok" in shared
