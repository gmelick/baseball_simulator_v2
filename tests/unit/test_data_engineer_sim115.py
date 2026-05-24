"""
test_data_engineer_sim115.py
============================
Unit test for SIM-115 — prune DuckDB sim-pool indexes.

Applies migration 0005 to a DuckDB that has the full (pre-prune) index set and
asserts that the 8 pitch-pool + 9 outcome-pool write-overhead indexes are gone
while the query-path indexes (and primary keys) remain.

Run:
    pytest tests/unit/test_data_engineer_sim115.py -v
"""
from __future__ import annotations

import os
import unittest

import duckdb

MIGRATION_0005 = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "db", "migrations", "duckdb", "0005_sim115_prune_pool_indexes.sql",
)

PITCH_DROP = [
    "idx_pp_pitcher", "idx_pp_batter", "idx_pp_season", "idx_pp_game_date",
    "idx_pp_batter_season", "idx_pp_runners", "idx_pp_velo", "idx_pp_ivb",
]
PITCH_KEEP = ["idx_pp_pitcher_season", "idx_pp_outcome", "idx_pp_count"]
OUTCOME_DROP = [
    "idx_op_pitcher", "idx_op_batter", "idx_op_bb_type", "idx_op_exit_velo",
    "idx_op_launch_angle", "idx_op_spray_angle", "idx_op_runners",
    "idx_op_result_hits", "idx_op_fielded_by",
]
OUTCOME_KEEP = ["idx_op_season"]


def _seed_full_index_set():
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA sim")
    c.execute(
        "CREATE TABLE sim.pitch_pool (pitch_id BIGINT, pitcher_id INTEGER, "
        "batter_id INTEGER, season SMALLINT, game_date DATE, outcome_type VARCHAR, "
        "runners_state SMALLINT, count_balls SMALLINT, count_strikes SMALLINT, "
        "outs SMALLINT, velo FLOAT, ivb FLOAT)"
    )
    c.execute(
        "CREATE TABLE sim.outcome_pool (pitch_id BIGINT, pitcher_id INTEGER, "
        "batter_id INTEGER, season SMALLINT, bb_type VARCHAR, exit_velo FLOAT, "
        "launch_angle FLOAT, spray_angle FLOAT, runners_state SMALLINT, "
        "result_hits SMALLINT, fielded_by_position SMALLINT)"
    )
    c.execute("CREATE TABLE migration_history (migration_id VARCHAR PRIMARY KEY, description VARCHAR)")
    # full pre-prune index set
    for name, col in [
        ("idx_pp_pitcher", "pitcher_id"), ("idx_pp_batter", "batter_id"),
        ("idx_pp_season", "season"), ("idx_pp_game_date", "game_date"),
        ("idx_pp_pitcher_season", "pitcher_id, season"),
        ("idx_pp_batter_season", "batter_id, season"),
        ("idx_pp_outcome", "outcome_type"), ("idx_pp_runners", "runners_state"),
        ("idx_pp_count", "count_balls, count_strikes, outs"),
        ("idx_pp_velo", "velo"), ("idx_pp_ivb", "ivb"),
    ]:
        c.execute(f"CREATE INDEX {name} ON sim.pitch_pool({col})")
    for name, col in [
        ("idx_op_pitcher", "pitcher_id"), ("idx_op_batter", "batter_id"),
        ("idx_op_season", "season"), ("idx_op_bb_type", "bb_type"),
        ("idx_op_exit_velo", "exit_velo"), ("idx_op_launch_angle", "launch_angle"),
        ("idx_op_spray_angle", "spray_angle"), ("idx_op_runners", "runners_state"),
        ("idx_op_result_hits", "result_hits"), ("idx_op_fielded_by", "fielded_by_position"),
    ]:
        c.execute(f"CREATE INDEX {name} ON sim.outcome_pool({col})")
    return c


class TestSim115Prune(unittest.TestCase):
    def setUp(self):
        self.c = _seed_full_index_set()
        sql = open(MIGRATION_0005, encoding="utf-8").read()
        for stmt in sql.split(";"):
            s = stmt.strip()
            if s:
                self.c.execute(s)

    def _index_names(self):
        return {r[0] for r in self.c.execute("SELECT index_name FROM duckdb_indexes()").fetchall()}

    def test_dropped_indexes_gone(self):
        names = self._index_names()
        for idx in PITCH_DROP + OUTCOME_DROP:
            self.assertNotIn(idx, names, f"{idx} should have been dropped")

    def test_kept_indexes_remain(self):
        names = self._index_names()
        for idx in PITCH_KEEP + OUTCOME_KEEP:
            self.assertIn(idx, names, f"{idx} should have been kept")

    def test_migration_recorded(self):
        self.assertEqual(
            self.c.execute(
                "SELECT COUNT(*) FROM migration_history WHERE migration_id='0005'"
            ).fetchone()[0],
            1,
        )

    def test_counts(self):
        names = self._index_names()
        kept_pp = [i for i in PITCH_KEEP if i in names]
        kept_op = [i for i in OUTCOME_KEEP if i in names]
        self.assertEqual(len(kept_pp), 3)
        self.assertEqual(len(kept_op), 1)


if __name__ == "__main__":
    unittest.main()
