"""
test_data_engineer_sim076_sim095.py
===================================
Unit tests for SIM-076 (recency_weight + pool_build_metadata) and
SIM-095 (incremental pool rebuild) helpers.

Covered:
  * recency_weight()         — pure-Python recency formula
  * _recency_weight_sql()    — SQL parity with the Python formula (run in DuckDB)
  * _seasons_needing_rebuild — incremental staleness selection (SIM-095)
  * _record_pool_build       — per-(pool,season) watermark upsert (SIM-076)
  * DuckDB migration 0004    — adds recency_weight + sim.pool_build_metadata

Run:
    pytest tests/unit/test_data_engineer_sim076_sim095.py -v
"""

from __future__ import annotations

import datetime as dt
import os
import unittest

import duckdb

import pipeline.batch.player_profile_computor as ppc

MIGRATION_0004 = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "db",
    "migrations",
    "duckdb",
    "0004_sim076_recency_weight.sql",
)


def _conn_with_pg():
    """A DuckDB conn with an attached `pg` catalog emulating pg.raw.pitches."""
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches (season SMALLINT, game_date DATE, data_quality_flag BOOLEAN)"
    )
    c.execute("CREATE SCHEMA sim")
    c.execute(
        "CREATE TABLE sim.pitch_pool (pitch_id BIGINT, season SMALLINT, recency_weight FLOAT)"
    )
    c.execute(
        "CREATE TABLE sim.pool_build_metadata ("
        "pool_name VARCHAR, season SMALLINT, row_count BIGINT, "
        "source_max_game_date DATE, source_row_count BIGINT, "
        "recency_ref_season SMALLINT, "
        "builder_version VARCHAR, built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (pool_name, season))"
    )
    return c


class TestRecencyWeightFormula(unittest.TestCase):
    def test_recent_two_seasons_peak(self):
        self.assertEqual(ppc.recency_weight(2025, 2025), 2.0)
        self.assertEqual(ppc.recency_weight(2024, 2025), 2.0)

    def test_geometric_decay(self):
        self.assertAlmostEqual(ppc.recency_weight(2023, 2025), 1.5, places=6)  # 2*0.75^1
        self.assertAlmostEqual(ppc.recency_weight(2022, 2025), 1.125, places=6)  # 2*0.75^2

    def test_floor(self):
        self.assertEqual(ppc.recency_weight(2005, 2025), ppc.RECENCY_FLOOR)

    def test_monotonic_non_increasing(self):
        ws = [ppc.recency_weight(s, 2025) for s in range(2025, 2010, -1)]
        self.assertEqual(ws, sorted(ws, reverse=True))


class TestRecencySqlParity(unittest.TestCase):
    def test_sql_matches_python(self):
        c = duckdb.connect(":memory:")
        c.execute("CREATE TABLE t (season SMALLINT)")
        seasons = list(range(2010, 2026))
        c.executemany("INSERT INTO t VALUES (?)", [[s] for s in seasons])
        ref = 2025
        expr = ppc._recency_weight_sql("season", ref)
        rows = c.execute(f"SELECT season, {expr} AS w FROM t ORDER BY season").fetchall()
        for season, w in rows:
            self.assertAlmostEqual(w, ppc.recency_weight(season, ref), places=5)


class TestSeasonsNeedingRebuild(unittest.TestCase):
    def test_skips_fresh_rebuilds_stale_and_missing(self):
        c = _conn_with_pg()
        # source data: 2023 latest game 2023-09-01, 2024 latest 2024-09-15
        c.executemany(
            "INSERT INTO pg.raw.pitches VALUES (?, ?, FALSE)",
            [[2023, dt.date(2023, 9, 1)], [2024, dt.date(2024, 9, 15)]],
        )
        # metadata: 2023 built up to its latest date AND row count (fresh);
        # 2024 built to an older date (stale). SIM-345: source_row_count must
        # match the live source count for a season to count as fresh — 2023 has
        # exactly 1 source row, recorded here as 1.
        c.execute(
            "INSERT INTO sim.pool_build_metadata "
            "(pool_name, season, row_count, source_max_game_date, source_row_count, recency_ref_season, builder_version) "
            "VALUES ('pitch_pool', 2023, 10, DATE '2023-09-01', 1, 2024, 'x'), "
            "('pitch_pool', 2024, 10, DATE '2024-08-01', 1, 2024, 'x')"
        )
        stale = ppc._seasons_needing_rebuild(c, "pitch_pool", [2023, 2024, 2025])
        self.assertNotIn(2023, stale)  # fresh
        self.assertIn(2024, stale)  # source advanced
        self.assertIn(2025, stale)  # never built

    def test_missing_metadata_table_rebuilds_all(self):
        c = duckdb.connect(":memory:")  # no sim.pool_build_metadata
        self.assertEqual(ppc._seasons_needing_rebuild(c, "pitch_pool", [2024, 2025]), [2024, 2025])


class TestRecordPoolBuild(unittest.TestCase):
    def test_upserts_watermark_and_counts(self):
        c = _conn_with_pg()
        c.executemany(
            "INSERT INTO pg.raw.pitches VALUES (?, ?, FALSE)",
            [[2024, dt.date(2024, 9, 1)], [2024, dt.date(2024, 9, 30)]],
        )
        c.executemany("INSERT INTO sim.pitch_pool VALUES (?, 2024, 2.0)", [[1], [2], [3]])
        ppc._record_pool_build(c, "pitch_pool", [2024], ref_season=2024)
        row = c.execute(
            "SELECT row_count, source_max_game_date, recency_ref_season, builder_version "
            "FROM sim.pool_build_metadata WHERE pool_name='pitch_pool' AND season=2024"
        ).fetchone()
        self.assertEqual(row[0], 3)  # row_count
        self.assertEqual(row[1], dt.date(2024, 9, 30))  # watermark = max source game_date
        self.assertEqual(row[2], 2024)  # recency ref
        self.assertEqual(row[3], ppc.POOL_BUILDER_VERSION)

    def test_idempotent_reupsert(self):
        c = _conn_with_pg()
        c.execute("INSERT INTO pg.raw.pitches VALUES (2024, DATE '2024-09-01', FALSE)")
        c.execute("INSERT INTO sim.pitch_pool VALUES (1, 2024, 2.0)")
        ppc._record_pool_build(c, "pitch_pool", [2024], 2024)
        ppc._record_pool_build(c, "pitch_pool", [2024], 2024)
        n = c.execute(
            "SELECT COUNT(*) FROM sim.pool_build_metadata WHERE pool_name='pitch_pool'"
        ).fetchone()[0]
        self.assertEqual(n, 1)


class TestMigration0004(unittest.TestCase):
    def test_migration_adds_column_and_table(self):
        c = duckdb.connect(":memory:")
        c.execute("CREATE SCHEMA sim")
        for tbl in ("pitch_pool", "outcome_pool", "stolen_base_pool"):
            c.execute(f"CREATE TABLE sim.{tbl} (pitch_id BIGINT, season SMALLINT)")
        c.execute(
            "CREATE TABLE migration_history (migration_id VARCHAR PRIMARY KEY, description VARCHAR)"
        )
        sql = open(MIGRATION_0004, encoding="utf-8").read()
        for stmt in sql.split(";"):
            s = stmt.strip()
            if s:
                c.execute(s)
        cols = [r[1] for r in c.execute("PRAGMA table_info('sim.pitch_pool')").fetchall()]
        self.assertIn("recency_weight", cols)
        # pool_build_metadata created
        self.assertEqual(
            c.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema='sim' AND table_name='pool_build_metadata'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            c.execute(
                "SELECT COUNT(*) FROM migration_history WHERE migration_id='0004'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
