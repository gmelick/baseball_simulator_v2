"""
tests/unit/test_engine_artifacts_realism_sim411_413_425b.py
===========================================================
SIM-411 / SIM-413 / SIM-425b — the batted-ball-artifact realism-column plumbing.

These three realism tickets each need one more per-row fact in the batted-ball
artifact, and all three are unblocked by ONE cheap outcome-pool rebuild:

  * SIM-411 (park factor)          → ``venue_id`` per batted ball.
  * SIM-413 (pitcher-hand platoon) → ``p_throws`` per batted ball (already on
                                     sim.outcome_pool; this just exports it).
  * SIM-425b (fielder RBF)         → ``fielded_by_position`` (already present) +
                                     ``fielder_player_id`` (new) so the resolver can
                                     read the pool fielder's defensive quality.

This module covers the CODE half (no rebuild / no live DB):
  * ``build_battedball_pool_artifact`` exports the four realism columns;
  * ``EngineArtifacts.load`` reads them onto :class:`BattedBallPool`;
  * ``EngineArtifacts.load`` is BACK-COMPATIBLE with a pre-0012 artifact that
    lacks the columns (the fields come back ``None``, no error);
  * the SIM-425b fielder-embedding key is now ``player_id:position:season`` so a
    multi-position fielder is no longer collapsed (last-wins), while every other
    actor keeps its ``player_id:season`` key;
  * the new numeric columns ride the SIM-403b shared-memory seam (object-dtype
    ``p_throws`` does not, exactly like ``event``).
"""

from __future__ import annotations

import json
import os

import duckdb
import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    BattedBallPool,
    EngineArtifacts,
    build_actor_embeddings,
    build_battedball_pool_artifact,
    build_pitch_pool_artifact,
)

# ===========================================================================
# Synthetic DuckDB pools (in-memory; just enough columns for the builders)
# ===========================================================================

_SEASON = 2024

# All columns build_battedball_pool_artifact reads, plus the migration-0012 cols.
_OUTCOME_POOL_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.outcome_pool (
    pitch_id BIGINT, batter_id INTEGER, season SMALLINT, stand VARCHAR,
    exit_velo FLOAT, launch_angle FLOAT, pull_relative_spray_angle FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT,
    events VARCHAR, result_hits SMALLINT, result_outs SMALLINT,
    result_runs SMALLINT, recency_weight FLOAT,
    -- migration 0012 realism columns
    p_throws VARCHAR, venue_id INTEGER, fielded_by_position SMALLINT,
    fielder_player_id INTEGER
);
"""

_PITCH_POOL_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.pitch_pool (
    pitch_id BIGINT, pitcher_id INTEGER, batter_id INTEGER, season SMALLINT,
    stand VARCHAR, outcome_type VARCHAR, recency_weight FLOAT,
    velo FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT,
    release_x FLOAT, release_z FLOAT, release_ext FLOAT, plate_x FLOAT, plate_z FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT
);
"""


def _seed_pools(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_OUTCOME_POOL_DDL)
    con.execute(_PITCH_POOL_DDL)
    # Two batted balls per hand, with distinguishable realism facts.
    rows = [
        # pitch_id, batter, stand, ev, la, spray, events, hits, outs, p_throws,
        # venue, fpos, fielder_id
        (1, 700001, "L", 98.0, 12.0, 10.0, "single", 1, 0, "R", 3313, 6, 500006),
        (2, 700002, "L", 80.0, 45.0, -5.0, "field_out", 0, 1, "L", 15, 8, 500008),
        (3, 700003, "R", 102.0, 25.0, 20.0, "home_run", 4, 0, "R", 3313, 0, None),
        (4, 700004, "R", 88.0, -3.0, 2.0, "field_out", 0, 1, "L", 15, 4, 500004),
    ]
    for pid, bat, stand, ev, la, spray, ev_name, hits, outs, throws, venue, fpos, fid in rows:
        con.execute(
            "INSERT INTO sim.outcome_pool VALUES (?,?,?,?, ?,?,?, 0,0,0, 0,1,0, "
            "?,?,?, 0, 1.0, ?,?,?,?)",
            [
                pid,
                bat,
                _SEASON,
                stand,
                ev,
                la,
                spray,
                ev_name,
                hits,
                outs,
                throws,
                venue,
                fpos,
                fid,
            ],
        )
        # Mirror an in_play pitch into pitch_pool so build_pitch_pool_artifact + load() work.
        con.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?, ?,'in_play',1.0, "
            "93.0,12.0,5.0,2200.0,180.0, 1.0,6.0,6.5,0.0,2.5, 0,0,0, 0,1,0)",
            [pid, 600000 + pid, bat, _SEASON, stand],
        )


def _build_full_artifact(out_dir: str) -> None:
    """Build a complete (pitch + batted-ball) artifact from a synthetic DB."""
    con = duckdb.connect(":memory:")
    try:
        _seed_pools(con)
        build_pitch_pool_artifact(con, out_dir, [_SEASON])
        build_battedball_pool_artifact(con, out_dir, [_SEASON])
    finally:
        con.close()


# ===========================================================================
# build_battedball_pool_artifact — the meta parquet carries the realism columns
# ===========================================================================


class TestBuilderExportsRealismColumns:
    def test_meta_parquet_has_all_four_columns(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed_pools(con)
            build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        meta = os.path.join(str(tmp_path), "battedball_pool", "L.meta.parquet")
        assert os.path.exists(meta)
        probe = duckdb.connect(":memory:")
        try:
            cols = {
                str(d[0])
                for d in probe.execute(f"SELECT * FROM read_parquet('{meta}') LIMIT 0").description
            }
        finally:
            probe.close()
        for c in ("p_throws", "venue_id", "fielded_by_position", "fielder_player_id"):
            assert c in cols, f"{c} missing from batted-ball meta parquet"

    def test_meta_values_round_trip(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed_pools(con)
            build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        meta = os.path.join(str(tmp_path), "battedball_pool", "L.meta.parquet")
        probe = duckdb.connect(":memory:")
        try:
            got = probe.execute(
                "SELECT batter_id, p_throws, venue_id, fielded_by_position, "
                f"fielder_player_id FROM read_parquet('{meta}') ORDER BY batter_id"
            ).fetchall()
        finally:
            probe.close()
        # The two L-hand batted balls keep their facts.
        assert got[0] == (700001, "R", 3313, 6, 500006)
        assert got[1] == (700002, "L", 15, 8, 500008)


# ===========================================================================
# EngineArtifacts.load — reads the realism columns onto BattedBallPool
# ===========================================================================


class TestLoaderReadsRealismColumns:
    def test_load_populates_realism_fields(self, tmp_path):
        _build_full_artifact(str(tmp_path))
        art = EngineArtifacts.load(str(tmp_path))
        bb = art.bb_pools["R"]
        assert bb.p_throws is not None and bb.venue_id is not None
        assert bb.fielder_pos is not None and bb.fielder_id is not None
        assert bb.p_throws.dtype == object
        assert bb.venue_id.dtype == np.int64
        assert bb.fielder_pos.dtype == np.int8
        # Home run row → no fielder credited → filled with 0.
        order = np.argsort(bb.batter_id)
        fids = bb.fielder_id[order]
        assert 0 in {int(x) for x in fids}  # the HR row's NULL fielder → 0

    def test_load_back_compat_legacy_artifact_yields_none(self, tmp_path):
        """A pre-0012 artifact (meta parquet WITHOUT the realism columns) must
        still load — the fields come back None, the consumer stays neutral."""
        _build_full_artifact(str(tmp_path))
        bb_dir = os.path.join(str(tmp_path), "battedball_pool")
        # Rewrite both meta parquets with ONLY the legacy columns.
        rw = duckdb.connect(":memory:")
        try:
            for hand in ("L", "R"):
                p = os.path.join(bb_dir, f"{hand}.meta.parquet")
                rw.execute(
                    "COPY (SELECT batter_id, season, events, result_hits, "
                    "result_outs, result_runs, recency_weight "
                    f"FROM read_parquet('{p}')) TO '{p}' (FORMAT parquet)"
                )
        finally:
            rw.close()
        art = EngineArtifacts.load(str(tmp_path))
        bb = art.bb_pools["L"]
        # Existing fields still load; realism fields are absent → None.
        assert bb.n == 2
        assert bb.p_throws is None
        assert bb.venue_id is None
        assert bb.fielder_pos is None
        assert bb.fielder_id is None


# ===========================================================================
# SIM-425b — the fielder-embedding key fix (player_id:position:season)
# ===========================================================================

_ACTOR_DDL = """
CREATE SCHEMA IF NOT EXISTS derived;
CREATE TABLE derived.batter_season_metrics    (player_id INTEGER, season SMALLINT, m DOUBLE);
CREATE TABLE derived.catcher_season_metrics   (player_id INTEGER, season SMALLINT, m DOUBLE);
CREATE TABLE derived.baserunner_season_metrics(player_id INTEGER, season SMALLINT, m DOUBLE);
CREATE TABLE derived.manager_season_metrics   (player_id INTEGER, season SMALLINT, m DOUBLE);
CREATE TABLE derived.fielder_season_metrics   (player_id INTEGER, position VARCHAR, season SMALLINT, m DOUBLE);
"""


def _seed_actors(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_ACTOR_DDL)
    for t in (
        "batter_season_metrics",
        "catcher_season_metrics",
        "baserunner_season_metrics",
        "manager_season_metrics",
    ):
        con.execute(f"INSERT INTO derived.{t} VALUES (123, {_SEASON}, 1.5)")
    # One fielder who played TWO positions in the same season (the collapse case).
    con.execute(
        f"INSERT INTO derived.fielder_season_metrics VALUES "
        f"(999, 'SS', {_SEASON}, 2.0), (999, '2B', {_SEASON}, 3.0)"
    )


def _load_keys(out_dir: str, actor: str) -> list[str]:
    z = np.load(os.path.join(out_dir, f"{actor}_emb.npz"), allow_pickle=True)
    return list(json.loads(str(z["keys"])))


class TestFielderEmbeddingKey:
    def test_multi_position_fielder_not_collapsed(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed_actors(con)
            counts = build_actor_embeddings(con, str(tmp_path))
        finally:
            con.close()
        # Both (player 999, SS/2B) rows survive as distinct keys.
        assert counts["fielder"] == 2
        fkeys = _load_keys(str(tmp_path), "fielder")
        assert sorted(fkeys) == [f"999:2B:{_SEASON}", f"999:SS:{_SEASON}"]

    def test_non_fielder_actor_keeps_player_season_key(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed_actors(con)
            build_actor_embeddings(con, str(tmp_path))
        finally:
            con.close()
        # The batter consumer looks up "player:season" — that format is unchanged.
        bkeys = _load_keys(str(tmp_path), "batter")
        assert bkeys == [f"123:{_SEASON}"]


# ===========================================================================
# SIM-403b — the new numeric columns ride the shared-memory seam; p_throws does not
# ===========================================================================


def _bb_with_realism(n: int = 3) -> BattedBallPool:
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.arange(n, dtype=np.int64) + 700_000,
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(["single", "field_out", "home_run"][:n], dtype=object),
        result_hits=np.array([1, 0, 4], dtype=np.int8)[:n],
        result_outs=np.array([0, 1, 0], dtype=np.int8)[:n],
        recency=np.full(n, 0.9, dtype=np.float32),
        p_throws=np.asarray(["R", "L", "R"][:n], dtype=object),
        venue_id=np.array([3313, 15, 3313], dtype=np.int64)[:n],
        fielder_pos=np.array([6, 8, 0], dtype=np.int8)[:n],
        fielder_id=np.array([500006, 500008, 0], dtype=np.int64)[:n],
    )


class TestSharedMemoryNewColumns:
    def test_numeric_realism_cols_are_shared(self):
        art = EngineArtifacts(pools={}, bb_pools={"L": _bb_with_realism()})
        out = art.extract_shared_arrays()
        for attr in ("venue_id", "fielder_pos", "fielder_id"):
            assert f"bb_pool.L.{attr}" in out

    def test_object_p_throws_not_shared(self):
        art = EngineArtifacts(pools={}, bb_pools={"L": _bb_with_realism()})
        out = art.extract_shared_arrays()
        assert not any("p_throws" in k for k in out)

    def test_none_realism_cols_skipped(self):
        """A legacy BB pool (realism fields None) emits no realism shared keys."""
        bb = _bb_with_realism()
        bb.venue_id = None
        bb.fielder_pos = None
        bb.fielder_id = None
        art = EngineArtifacts(pools={}, bb_pools={"L": bb})
        out = art.extract_shared_arrays()
        assert not any(k.endswith((".venue_id", ".fielder_pos", ".fielder_id")) for k in out)

    def test_attach_round_trips_numeric_cols(self):
        art = _bb_with_realism()
        bundle = EngineArtifacts(pools={}, bb_pools={"L": art})
        shareable = bundle.extract_shared_arrays()
        dst = EngineArtifacts(pools={}, bb_pools={"L": _bb_with_realism()})
        # Zero out dst then splice the views back in.
        dst.bb_pools["L"].venue_id = np.zeros(3, dtype=np.int64)
        dst.attach_shared_views(shareable)
        np.testing.assert_array_equal(dst.bb_pools["L"].venue_id, art.venue_id)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
