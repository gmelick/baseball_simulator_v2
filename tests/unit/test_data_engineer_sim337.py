"""
test_data_engineer_sim337.py
============================
Unit test for SIM-337 - reconcile DuckDB sim-pool indexes to the SIM-111
play-pool query contract (section 6.2) and add `stand`-bearing composites.

LIVE BUG context: SIM-115 (migration 0005) pruned the sim-pool indexes but
contradicted the contract it was supposed to act on. On sim.pitch_pool it KEPT
idx_pp_outcome + idx_pp_count (the contract says DROP - those columns are
projected / bulk-read, never filtered) and DROPPED idx_pp_pitcher +
idx_pp_season + idx_pp_game_date (the contract says KEEP). Neither pool got a
`stand` index even though `stand` (batter handedness) is half the pitch
pre-filter and the sole batted-ball pre-filter.

This test seeds a DuckDB at the realistic post-0005 (shipped) state, applies
migration 0006, and asserts the resulting index set on sim.pitch_pool /
sim.outcome_pool matches the contract:
  * pitch_pool : idx_pp_pitcher_season, idx_pp_pitcher, idx_pp_season,
                 idx_pp_game_date, idx_pp_pitcher_stand_season  (KEEP/RESTORE/ADD)
                 idx_pp_outcome, idx_pp_count                   (DROPPED)
  * outcome_pool: idx_op_season, idx_op_stand_season            (KEEP/ADD)

It also asserts every DROP INDEX statement in 0006 is schema-qualified (sim.),
because an unqualified DROP silently no-ops (indexes live in their table's
schema) - which is exactly how the original contradiction could go unnoticed.

Run:
    pytest tests/unit/test_data_engineer_sim337.py -v
"""
from __future__ import annotations

import os
import re
import unittest

import duckdb

MIGRATION_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "migrations", "duckdb",
)
MIGRATION_0006 = os.path.join(MIGRATION_DIR, "0006_sim337_reconcile_pool_indexes.sql")

PITCH_KEEP = [
    "idx_pp_pitcher_season",
    "idx_pp_pitcher",
    "idx_pp_season",
    "idx_pp_game_date",
    "idx_pp_pitcher_stand_season",
]
PITCH_DROP = [
    "idx_pp_outcome",
    "idx_pp_count",
]
OUTCOME_KEEP = [
    "idx_op_season",
    "idx_op_stand_season",
]


def _seed_post_0005_state():
    """Realistic starting point: the DB as SIM-115 (0005) actually shipped it."""
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA sim")
    c.execute(
        "CREATE TABLE sim.pitch_pool (pitch_id BIGINT, pitcher_id INTEGER, "
        "batter_id INTEGER, season SMALLINT, game_date DATE, stand VARCHAR, "
        "outcome_type VARCHAR, runners_state SMALLINT, count_balls SMALLINT, "
        "count_strikes SMALLINT, outs SMALLINT, velo FLOAT, ivb FLOAT)"
    )
    c.execute(
        "CREATE TABLE sim.outcome_pool (pitch_id BIGINT, pitcher_id INTEGER, "
        "batter_id INTEGER, season SMALLINT, stand VARCHAR, bb_type VARCHAR, "
        "exit_velo FLOAT, launch_angle FLOAT, spray_angle FLOAT, "
        "runners_state SMALLINT, result_hits SMALLINT, fielded_by_position SMALLINT)"
    )
    c.execute(
        "CREATE TABLE migration_history (migration_id VARCHAR PRIMARY KEY, description VARCHAR)"
    )
    c.execute("CREATE INDEX idx_pp_pitcher_season ON sim.pitch_pool(pitcher_id, season)")
    c.execute("CREATE INDEX idx_pp_outcome        ON sim.pitch_pool(outcome_type)")
    c.execute("CREATE INDEX idx_pp_count          ON sim.pitch_pool(count_balls, count_strikes, outs)")
    c.execute("CREATE INDEX idx_op_season         ON sim.outcome_pool(season)")
    c.execute("INSERT INTO migration_history VALUES ('0005', 'prune sim-pool indexes SIM-115')")
    return c


def _apply(conn, path):
    # Execute the whole script at once, exactly like the production migration path
    # (duckdb baseball_simulator.duckdb < 0006_*.sql). DuckDB's parser handles
    # multiple statements and comments - including semicolons inside comments -
    # natively, so we do NOT naively split on ';'.
    sql = open(path, encoding="utf-8").read()
    conn.execute(sql)


class TestSim337Reconcile(unittest.TestCase):
    def setUp(self):
        self.c = _seed_post_0005_state()
        _apply(self.c, MIGRATION_0006)

    def _index_names(self):
        return {r[0] for r in self.c.execute(
            "SELECT index_name FROM duckdb_indexes()"
        ).fetchall()}

    def _indexes_on(self, table):
        return {r[0] for r in self.c.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = ?", [table]
        ).fetchall()}

    def test_pitch_pool_kept_and_added_present(self):
        names = self._indexes_on("pitch_pool")
        for idx in PITCH_KEEP:
            self.assertIn(idx, names, f"{idx} should be present on pitch_pool after 0006")

    def test_outcome_pool_kept_and_added_present(self):
        names = self._indexes_on("outcome_pool")
        for idx in OUTCOME_KEEP:
            self.assertIn(idx, names, f"{idx} should be present on outcome_pool after 0006")

    def test_pitch_pool_contract_drops_gone(self):
        names = self._index_names()
        for idx in PITCH_DROP:
            self.assertNotIn(idx, names, f"{idx} should have been dropped per contract section 6.2")

    def test_stand_composites_added(self):
        names = self._index_names()
        self.assertIn("idx_pp_pitcher_stand_season", names,
                      "pitch pre-filter (C) must be served by a stand-bearing composite")
        self.assertIn("idx_op_stand_season", names,
                      "batted-ball pre-filter (E) must be served by a stand-bearing composite")

    def test_pitch_pool_exact_index_set(self):
        managed = {
            "idx_pp_pitcher_season", "idx_pp_pitcher", "idx_pp_season",
            "idx_pp_game_date", "idx_pp_pitcher_stand_season",
            "idx_pp_outcome", "idx_pp_count",
        }
        present = self._indexes_on("pitch_pool") & managed
        self.assertEqual(present, set(PITCH_KEEP),
                         "pitch_pool managed index set must equal the contract KEEP set")

    def test_outcome_pool_exact_index_set(self):
        managed = {
            "idx_op_season", "idx_op_stand_season",
            "idx_op_pitcher", "idx_op_batter", "idx_op_bb_type",
            "idx_op_exit_velo", "idx_op_launch_angle", "idx_op_spray_angle",
            "idx_op_runners", "idx_op_result_hits", "idx_op_fielded_by",
        }
        present = self._indexes_on("outcome_pool") & managed
        self.assertEqual(present, set(OUTCOME_KEEP),
                         "outcome_pool managed index set must equal the contract KEEP set")

    def test_migration_recorded(self):
        self.assertEqual(
            self.c.execute(
                "SELECT COUNT(*) FROM migration_history WHERE migration_id='0006'"
            ).fetchone()[0],
            1,
        )

    def test_idempotent_reapply(self):
        before = self._index_names()
        _apply(self.c, MIGRATION_0006)
        self.assertEqual(self._index_names(), before)

    def test_all_drops_are_schema_qualified(self):
        sql = open(MIGRATION_0006, encoding="utf-8").read()
        code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
        drops = re.findall(r"DROP\s+INDEX\s+IF\s+EXISTS\s+([^\s;]+)", code, re.IGNORECASE)
        self.assertTrue(drops, "expected at least one DROP INDEX in 0006")
        for target in drops:
            self.assertTrue(
                target.lower().startswith("sim."),
                f"DROP INDEX target {target!r} must be schema-qualified (sim.) - "
                "an unqualified DROP silently no-ops",
            )


if __name__ == "__main__":
    unittest.main()
