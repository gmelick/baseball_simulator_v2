"""
tests/unit/test_sim523_part_g.py
=================================
SIM-523 part G — the DATA additions (plan part G):

  * the fielding CHAIN: ``sql_credit_mask`` turns the loader's putout / assist
    slot columns into a position bitmask against the row's alignment; the
    outcome-pool export writes the alignment + the two masks when the columns
    exist, and the loader reads them back (None on an older artifact);
  * the got-away rates out of the catcher EMBEDDING (the selection surface).
"""

from __future__ import annotations

import json
import os

import duckdb
import numpy as np

from pipeline.batch.engine_artifacts import (
    _BB_POOL_SHAREABLE_ATTRS,
    _EMBEDDING_EXCLUDE,
    EngineArtifacts,
    build_actor_embeddings,
    build_battedball_pool_artifact,
    build_pitch_pool_artifact,
)
from pipeline.batch.player_profile_computor import (
    _ASSIST_SLOTS,
    _PUTOUT_SLOTS,
    POOL_BUILDER_VERSION,
    sql_credit_mask,
)

_SEASON = 2024


class TestCreditMask:
    def _con(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")
        con.execute(
            "CREATE TABLE rp (pitcher_id INTEGER, "
            + ", ".join(f"fielder_{k} INTEGER" for k in range(2, 10))
            + ", "
            + ", ".join(f"{c} INTEGER" for c in _PUTOUT_SLOTS + _ASSIST_SLOTS)
            + ")"
        )
        # the alignment: catcher 20, 1B 30, 2B 40, 3B 50, SS 60, LF 70, CF 80, RF 90
        align = "20, 30, 40, 50, 60, 70, 80, 90"
        rows = [
            # a 6-4-3 double play: putouts by 2B (40) and 1B (30), assists by SS (60) and 2B (40)
            f"(10, {align}, 40, 30, NULL, 60, 40, NULL, NULL, NULL)",
            # a 3-1 groundout: the pitcher (10) covers first (putout), the 1B assists
            f"(10, {align}, 10, NULL, NULL, 30, NULL, NULL, NULL, NULL)",
            # a hit: no credit slots
            f"(10, {align}, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            # a fly out to CF (80)
            f"(10, {align}, 80, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            # a credit that matches no alignment slot (a substitute the row does not carry)
            f"(10, {align}, 999, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
        ]
        con.execute("INSERT INTO rp VALUES " + ", ".join(rows))
        return con

    def test_the_masks_follow_the_alignment(self):
        con = self._con()
        try:
            po = sql_credit_mask("", _PUTOUT_SLOTS, "pitcher_id")
            asst = sql_credit_mask("", _ASSIST_SLOTS, "pitcher_id")
            got = con.execute(f"SELECT ({po}) AS po, ({asst}) AS a FROM rp").fetchall()
        finally:
            con.close()
        assert got[0] == (
            (1 << 4) + (1 << 3),
            (1 << 6) + (1 << 4),
        )  # 6-4-3: putouts 2B+1B, assists SS+2B
        assert got[1] == ((1 << 1), (1 << 3))  # the pitcher's putout (bit 1), the 1B's assist
        assert got[2] == (0, 0)  # a hit
        assert got[3] == ((1 << 8), 0)  # CF
        assert got[4] == (0, 0)  # an unmatched credit counts nothing

    def test_the_prefix_reaches_every_column(self):
        sql = sql_credit_mask("rp.", ("field_putout_1",), "pp.pitcher_id")
        assert (
            "rp.field_putout_1 = rp.fielder_9" in sql and "rp.field_putout_1 = pp.pitcher_id" in sql
        )
        assert sql.count("CASE WHEN") == 9

    def test_the_builder_version_moved(self):
        assert POOL_BUILDER_VERSION == "sim523g.1"


_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.outcome_pool (
    pitch_id BIGINT, batter_id INTEGER, season SMALLINT, stand VARCHAR,
    exit_velo FLOAT, launch_angle FLOAT, pull_relative_spray_angle FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT,
    events VARCHAR, result_hits SMALLINT, result_outs SMALLINT,
    result_runs SMALLINT, recency_weight FLOAT,
    p_throws VARCHAR, venue_id INTEGER, fielded_by_position SMALLINT,
    fielder_player_id INTEGER{extra}
);
"""
_PITCH_POOL_DDL = """
CREATE TABLE sim.pitch_pool (
    pitch_id BIGINT, pitcher_id INTEGER, batter_id INTEGER, season SMALLINT,
    stand VARCHAR, outcome_type VARCHAR, recency_weight FLOAT,
    velo FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT,
    release_x FLOAT, release_z FLOAT, release_ext FLOAT, plate_x FLOAT, plate_z FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT
);
"""
_CHAIN_COLS = (
    ", "
    + ", ".join(f"fielder_{k} INTEGER" for k in range(2, 10))
    + ", putout_pos_mask SMALLINT, assist_pos_mask SMALLINT"
)


def _seed(con: duckdb.DuckDBPyConnection, chain: bool) -> None:
    con.execute(_DDL.format(extra=_CHAIN_COLS if chain else ""))
    con.execute(_PITCH_POOL_DDL)
    for pid in (11, 12):
        con.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?, 'R','in_play',1.0, "
            "93.0,12.0,5.0,2200.0,180.0, 1.0,6.0,6.5,0.0,2.5, 0,0,0, 0,1,0)",
            [pid, 600000, 700000, _SEASON],
        )
    base = f"(?, 700000, {_SEASON}, 'R', 95.0, 12.0, 10.0, 0,0,0, 0,1,0, ?, ?, ?, 0, 1.0, 'R', 15, ?, ?"
    rows = [
        (11, "field_out", 0, 1, 6, 600, 20, 30, 40, 50, 60, 70, 80, 90, (1 << 3), (1 << 6)),
        (12, "single", 1, 0, 8, 800, 20, 30, 40, 50, 60, 70, 80, 90, 0, 0),
    ]
    for r in rows:
        vals = list(r[:6])
        if chain:
            con.execute(
                "INSERT INTO sim.outcome_pool VALUES " + base + ", ?,?,?,?,?,?,?,?, ?, ?)",
                vals + list(r[6:]),
            )
        else:
            con.execute("INSERT INTO sim.outcome_pool VALUES " + base + ")", vals)


class TestTheChainOnTheArtifact:
    def test_export_and_load_carry_the_chain(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con, chain=True)
            build_pitch_pool_artifact(con, str(tmp_path), [_SEASON])
            build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        with open(tmp_path / "battedball_pool" / "manifest.json", encoding="utf-8") as fh:
            assert json.load(fh)["chain"] is True
        bb = EngineArtifacts.load(str(tmp_path)).bb_pools["R"]
        assert bb.fielders is not None and bb.fielders.shape == (2, 8)
        assert bb.fielders.dtype == np.int64 and bb.fielders[0].tolist() == [
            20,
            30,
            40,
            50,
            60,
            70,
            80,
            90,
        ]
        assert bb.putout_mask is not None and bb.putout_mask.tolist() == [1 << 3, 0]
        assert bb.assist_mask is not None and bb.assist_mask.tolist() == [1 << 6, 0]
        assert bb.putout_mask.dtype == np.int16

    def test_an_older_pool_loads_without_the_chain(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con, chain=False)
            build_pitch_pool_artifact(con, str(tmp_path), [_SEASON])
            build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        with open(tmp_path / "battedball_pool" / "manifest.json", encoding="utf-8") as fh:
            assert json.load(fh)["chain"] is False
        bb = EngineArtifacts.load(str(tmp_path)).bb_pools["R"]
        assert bb.fielders is None and bb.putout_mask is None and bb.assist_mask is None

    def test_the_chain_rides_the_shared_seam(self):
        for name in ("fielders", "putout_mask", "assist_mask"):
            assert name in _BB_POOL_SHAREABLE_ATTRS


class TestGotAwayOutOfTheCatcherEmbedding:
    def test_the_catcher_embedding_drops_the_got_away_columns(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            con.execute("CREATE SCHEMA derived")
            for t in (
                "batter_season_metrics",
                "baserunner_season_metrics",
                "manager_season_metrics",
                "pitcher_steal_metrics",
            ):
                con.execute(
                    f"CREATE TABLE derived.{t} (player_id INTEGER, season SMALLINT, x DOUBLE)"
                )
                con.execute(f"INSERT INTO derived.{t} VALUES (1, {_SEASON}, 1.0)")
            con.execute(
                "CREATE TABLE derived.fielder_season_metrics "
                "(player_id INTEGER, position VARCHAR, season SMALLINT, outs_above_average DOUBLE, sprint_speed DOUBLE)"
            )
            con.execute(
                f"INSERT INTO derived.fielder_season_metrics VALUES (1, 'SS', {_SEASON}, 2.0, 28.5)"
            )
            con.execute(
                "CREATE TABLE derived.catcher_season_metrics (player_id INTEGER, season SMALLINT, "
                "cs_rate DOUBLE, pop_time_mean DOUBLE, expected_pbwp DOUBLE, actual_pbwp INTEGER, "
                "blocks_above_average DOUBLE, blocking_runs DOUBLE, uncaught_k3_rate_eb DOUBLE)"
            )
            con.execute(
                f"INSERT INTO derived.catcher_season_metrics VALUES (1, {_SEASON}, 0.3, 1.95, 30.0, 25, 5.0, 1.2, 0.01)"
            )
            build_actor_embeddings(con, str(tmp_path))
        finally:
            con.close()
        z = np.load(os.path.join(str(tmp_path), "catcher_emb.npz"), allow_pickle=True)
        feats = list(json.loads(str(z["features"])))
        assert feats == ["cs_rate", "pop_time_mean"]
        for name in _EMBEDDING_EXCLUDE["catcher"]:
            assert name not in feats
        fz = np.load(os.path.join(str(tmp_path), "fielder_emb.npz"), allow_pickle=True)
        assert "sprint_speed" in list(json.loads(str(fz["features"])))
