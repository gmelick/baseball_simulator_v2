"""
test_data_engineer_sim336.py
============================
Unit tests for SIM-336 (park-factor UNPIVOT/factor_overall fix + real L/R
splits + pool-neutralization policy) and SIM-345 (data-layer fixes:
incremental watermark `>=` + row-count guard, canonical cross-pool
recency_ref_season, recency_weight NOT NULL parity, stand/bat_hand pool
contract).

Mirrors the in-memory / temp-DuckDB idiom of
tests/unit/test_data_engineer_sim076_sim095.py — all DuckDB access is against
an in-memory connection seeded with synthetic rows; no Postgres.

Run:
    pytest tests/unit/test_data_engineer_sim336.py -v
"""
from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest.mock import MagicMock

import duckdb

import pipeline.batch.player_profile_computor as ppc

MIGRATION_0007 = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "db", "migrations", "duckdb",
    "0007_sim336_sim345_park_factors_data_layer.sql",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pf_conn():
    """In-memory DuckDB with a synthetic pg.raw.pitches + derived.park_factors."""
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches ("
        " venue_id INTEGER, season SMALLINT, p_throws VARCHAR, stand VARCHAR,"
        " events VARCHAR, bb_type VARCHAR, launch_speed FLOAT,"
        " at_bat_number INTEGER, game_pk INTEGER, runs_on_pitch FLOAT,"
        " data_quality_flag BOOLEAN)"
    )
    c.execute("CREATE SCHEMA derived")
    c.execute(
        "CREATE TABLE derived.park_factors ("
        " venue_id INTEGER NOT NULL, season SMALLINT NOT NULL,"
        " factor_type VARCHAR(5) NOT NULL, factor_overall FLOAT NOT NULL,"
        " factor_vs_l FLOAT, factor_vs_r FLOAT,"
        " sample_pa INTEGER NOT NULL DEFAULT 0, regressed_factor FLOAT NOT NULL,"
        " PRIMARY KEY (venue_id, season, factor_type))"
    )
    return c


def _seed_park_pas(c):
    """Seed two venues where venue 1 is HR-friendly and skews vs RHB.

    Each row is a PA-terminal pitch (events non-null). venue 1 has a higher HR
    rate than venue 2 (so factor_hr > 1.0 there), and within venue 1, RHB hit
    HRs at a higher rate than LHB (so factor_vs_r > factor_vs_l for HR).
    """
    rows = []
    pk = 0

    def add(venue, stand, event):
        nonlocal pk
        pk += 1
        rows.append([venue, 2024, "R", stand, event, None, None, 1, pk, 0.0, False])

    # Venue 1: 100 PAs. RHB: 30 HR / 70 outs. LHB: 10 HR / 90 outs → vs_r >> vs_l
    for _ in range(30):
        add(1, "R", "home_run")
    for _ in range(70):
        add(1, "R", "field_out")
    for _ in range(10):
        add(1, "L", "home_run")
    for _ in range(90):
        add(1, "L", "field_out")
    # Venue 2: 200 PAs, HR-poor. RHB: 5 HR / 95. LHB: 5 HR / 95.
    for _ in range(5):
        add(2, "R", "home_run")
    for _ in range(95):
        add(2, "R", "field_out")
    for _ in range(5):
        add(2, "L", "home_run")
    for _ in range(95):
        add(2, "L", "field_out")
    # A handful of switch-hitter PAs at venue 1 (stand='S') — should NOT pollute
    # the L/R splits but still raise the overall sample.
    for _ in range(4):
        add(1, "S", "home_run")

    c.executemany(
        "INSERT INTO pg.raw.pitches VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )


def _run_park_factors(c, seasons):
    """Invoke the real _compute_park_factors against the in-memory conn."""
    obj = ppc.PlayerProfileComputor.__new__(ppc.PlayerProfileComputor)
    obj._conn = c
    ppc.PlayerProfileComputor._compute_park_factors(obj, seasons)


# ---------------------------------------------------------------------------
# SIM-336 — park factors
# ---------------------------------------------------------------------------

class TestParkFactorsCompute(unittest.TestCase):
    def setUp(self):
        self.c = _pf_conn()
        _seed_park_pas(self.c)
        _run_park_factors(self.c, [2024])

    def test_rows_written_for_all_factor_types(self):
        types = {
            r[0]
            for r in self.c.execute(
                "SELECT DISTINCT factor_type FROM derived.park_factors"
            ).fetchall()
        }
        self.assertEqual(
            types, {"HR", "1B", "2B", "3B", "BB", "K", "GB", "FB", "R"}
        )

    def test_factor_overall_non_null_and_correct(self):
        # Venue 1 HR rate = 44/204; venue 2 = 10/200. League = 54/404.
        # factor_overall(HR, venue1) = (44/204) / (54/404)
        row = self.c.execute(
            "SELECT factor_overall FROM derived.park_factors "
            "WHERE venue_id=1 AND season=2024 AND factor_type='HR'"
        ).fetchone()
        self.assertIsNotNone(row[0])
        expected = (44 / 204) / (54 / 404)
        self.assertAlmostEqual(float(row[0]), expected, places=4)
        # HR-friendly park ⇒ overall HR factor > 1.0
        self.assertGreater(float(row[0]), 1.0)

    def test_lr_splits_are_real_not_null(self):
        row = self.c.execute(
            "SELECT factor_vs_l, factor_vs_r FROM derived.park_factors "
            "WHERE venue_id=1 AND season=2024 AND factor_type='HR'"
        ).fetchone()
        self.assertIsNotNone(row[0], "factor_vs_l must be a real split, not NULL")
        self.assertIsNotNone(row[1], "factor_vs_r must be a real split, not NULL")

    def test_rhb_split_exceeds_lhb_split_for_hr(self):
        # Venue 1: RHB HR rate (30/100) > LHB HR rate (10/100); the splits are
        # each centered on their own-hand league rate but the gap must persist.
        row = self.c.execute(
            "SELECT factor_vs_l, factor_vs_r FROM derived.park_factors "
            "WHERE venue_id=1 AND season=2024 AND factor_type='HR'"
        ).fetchone()
        self.assertGreater(float(row[1]), float(row[0]))

    def test_switch_hitters_excluded_from_splits_but_counted_overall(self):
        # The 4 switch-hitter HR PAs raise venue-1 sample_pa to 204 (counted)
        # but the vs-L / vs-R HR numerators only see 10 / 30 (stand-gated).
        sample = self.c.execute(
            "SELECT sample_pa FROM derived.park_factors "
            "WHERE venue_id=1 AND season=2024 AND factor_type='HR'"
        ).fetchone()[0]
        self.assertEqual(sample, 204)
        # vs_r split uses 30/100 over league vs-R HR rate; verify the L split
        # uses only the 10 LHB HRs (not 14) by recomputing the exact ratio.
        l_split = self.c.execute(
            "SELECT factor_vs_l FROM derived.park_factors "
            "WHERE venue_id=1 AND season=2024 AND factor_type='HR'"
        ).fetchone()[0]
        # venue1 vs-L HR rate = 10/100; league vs-L HR rate = (10+5)/(100+100)
        expected_l = (10 / 100) / (15 / 200)
        self.assertAlmostEqual(float(l_split), expected_l, places=4)

    def test_regressed_factor_shrinks_toward_one(self):
        # Regressed factor must sit between the raw factor and 1.0.
        raw, reg = self.c.execute(
            "SELECT factor_overall, regressed_factor FROM derived.park_factors "
            "WHERE venue_id=1 AND season=2024 AND factor_type='HR'"
        ).fetchone()
        self.assertTrue(1.0 < float(reg) < float(raw))

    def test_neutral_factor_close_to_one_overall_league(self):
        # Across both venues, the PA-weighted HR factor must average ~1.0.
        rows = self.c.execute(
            "SELECT factor_overall, sample_pa FROM derived.park_factors "
            "WHERE factor_type='HR' AND season=2024"
        ).fetchall()
        wsum = sum(float(f) * pa for f, pa in rows)
        n = sum(pa for _, pa in rows)
        self.assertAlmostEqual(wsum / n, 1.0, places=2)


class TestParkFactorsIdempotent(unittest.TestCase):
    def test_rerun_replaces_not_duplicates(self):
        c = _pf_conn()
        _seed_park_pas(c)
        _run_park_factors(c, [2024])
        _run_park_factors(c, [2024])
        n = c.execute(
            "SELECT COUNT(*) FROM derived.park_factors "
            "WHERE venue_id=1 AND factor_type='HR'"
        ).fetchone()[0]
        self.assertEqual(n, 1)


# ---------------------------------------------------------------------------
# SIM-345 — watermark >= + row-count guard
# ---------------------------------------------------------------------------

def _wm_conn():
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches (season SMALLINT, game_date DATE, "
        "data_quality_flag BOOLEAN)"
    )
    c.execute("CREATE SCHEMA sim")
    c.execute(
        "CREATE TABLE sim.pitch_pool (pitch_id BIGINT, season SMALLINT, "
        "recency_weight FLOAT)"
    )
    c.execute(
        "CREATE TABLE sim.pool_build_metadata ("
        "pool_name VARCHAR, season SMALLINT, row_count BIGINT, "
        "source_max_game_date DATE, source_row_count BIGINT, "
        "recency_ref_season SMALLINT, builder_version VARCHAR, "
        "built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (pool_name, season))"
    )
    return c


class TestWatermarkRowCountGuard(unittest.TestCase):
    def test_same_date_late_row_triggers_rebuild(self):
        c = _wm_conn()
        # Build state: 2 source rows, watermark 2024-09-15.
        c.executemany(
            "INSERT INTO pg.raw.pitches VALUES (?, ?, FALSE)",
            [[2024, dt.date(2024, 9, 15)], [2024, dt.date(2024, 9, 15)]],
        )
        c.executemany("INSERT INTO sim.pitch_pool VALUES (?, 2024, 2.0)", [[1], [2]])
        ppc._record_pool_build(c, "pitch_pool", [2024], ref_season=2024)

        # Nothing changed → fresh, skipped.
        self.assertEqual(
            ppc._seasons_needing_rebuild(c, "pitch_pool", [2024]), []
        )

        # A LATE doubleheader row lands on the SAME game_date (watermark does
        # NOT advance). The strict `>` watermark would have skipped this; the
        # row-count guard must catch it.
        c.execute("INSERT INTO pg.raw.pitches VALUES (2024, DATE '2024-09-15', FALSE)")
        self.assertEqual(
            ppc._seasons_needing_rebuild(c, "pitch_pool", [2024]), [2024]
        )

    def test_unchanged_season_stays_fresh(self):
        c = _wm_conn()
        c.execute("INSERT INTO pg.raw.pitches VALUES (2024, DATE '2024-09-15', FALSE)")
        c.execute("INSERT INTO sim.pitch_pool VALUES (1, 2024, 2.0)")
        ppc._record_pool_build(c, "pitch_pool", [2024], 2024)
        # No source change at all → must NOT rebuild (guard does not over-trigger).
        self.assertEqual(ppc._seasons_needing_rebuild(c, "pitch_pool", [2024]), [])

    def test_advancing_date_triggers_rebuild(self):
        c = _wm_conn()
        c.execute("INSERT INTO pg.raw.pitches VALUES (2024, DATE '2024-09-15', FALSE)")
        c.execute("INSERT INTO sim.pitch_pool VALUES (1, 2024, 2.0)")
        ppc._record_pool_build(c, "pitch_pool", [2024], 2024)
        c.execute("INSERT INTO pg.raw.pitches VALUES (2024, DATE '2024-09-20', FALSE)")
        self.assertEqual(ppc._seasons_needing_rebuild(c, "pitch_pool", [2024]), [2024])

    def test_record_pool_build_stores_source_row_count(self):
        c = _wm_conn()
        c.executemany(
            "INSERT INTO pg.raw.pitches VALUES (?, ?, FALSE)",
            [[2024, dt.date(2024, 9, 1)], [2024, dt.date(2024, 9, 30)],
             [2024, dt.date(2024, 9, 30)]],
        )
        c.execute("INSERT INTO sim.pitch_pool VALUES (1, 2024, 2.0)")
        ppc._record_pool_build(c, "pitch_pool", [2024], 2024)
        row = c.execute(
            "SELECT source_max_game_date, source_row_count FROM "
            "sim.pool_build_metadata WHERE pool_name='pitch_pool' AND season=2024"
        ).fetchone()
        self.assertEqual(row[0], dt.date(2024, 9, 30))
        self.assertEqual(row[1], 3)


# ---------------------------------------------------------------------------
# SIM-345 — canonical cross-pool recency_ref_season
# ---------------------------------------------------------------------------

class TestCanonicalRefSeason(unittest.TestCase):
    def test_max_of_requested_when_no_metadata(self):
        c = _wm_conn()
        self.assertEqual(
            ppc._canonical_ref_season(c, [2022, 2023, 2024]), 2024
        )

    def test_adopts_newest_recorded_ref_across_pools(self):
        c = _wm_conn()
        # outcome_pool already advanced its reference to 2025.
        c.execute(
            "INSERT INTO sim.pool_build_metadata "
            "(pool_name, season, row_count, source_max_game_date, "
            " source_row_count, recency_ref_season, builder_version) "
            "VALUES ('outcome_pool', 2025, 1, DATE '2025-09-01', 1, 2025, 'x')"
        )
        # A later pool built with only [2024] must still adopt 2025 → consistent.
        self.assertEqual(ppc._canonical_ref_season(c, [2024]), 2025)

    def test_no_metadata_table_falls_back_to_max(self):
        c = duckdb.connect(":memory:")  # no sim.pool_build_metadata
        self.assertEqual(ppc._canonical_ref_season(c, [2023, 2024]), 2024)


# ---------------------------------------------------------------------------
# SIM-345 — stand vs bat_hand pool contract
# ---------------------------------------------------------------------------

class TestStandBatHandContract(unittest.TestCase):
    def test_pool_stand_resolves_to_bat_hand(self):
        """The pool `stand` is the RESOLVED batting side: bat_hand when L/R,
        else the declared stand. We exercise the CASE expression directly in
        DuckDB to lock the contract."""
        c = duckdb.connect(":memory:")
        c.execute(
            "CREATE TABLE t (stand VARCHAR, bat_hand VARCHAR)"
        )
        c.executemany(
            "INSERT INTO t VALUES (?, ?)",
            [
                ["R", "R"],   # RHB → R
                ["L", "L"],   # LHB → L
                ["S", "R"],   # switch hitter batting R this PA → R (not S)
                ["S", "L"],   # switch hitter batting L this PA → L (not S)
                ["S", "S"],   # unresolved switch → fall back to declared stand
                ["L", None],  # bat_hand missing → fall back to declared stand
            ],
        )
        rows = c.execute(
            "SELECT CASE WHEN bat_hand IN ('L','R') THEN bat_hand ELSE stand END "
            "AS pool_stand FROM t"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["R", "L", "R", "L", "S", "L"])


# ---------------------------------------------------------------------------
# SIM-345 — recency_weight NOT NULL parity + migration 0007
# ---------------------------------------------------------------------------

class TestMigration0007(unittest.TestCase):
    def _base_db(self):
        c = duckdb.connect(":memory:")
        c.execute("CREATE SCHEMA sim")
        for tbl in ("pitch_pool", "outcome_pool", "stolen_base_pool"):
            # Mirror migration 0004's NULLABLE recency_weight (the parity gap).
            c.execute(
                f"CREATE TABLE sim.{tbl} (pitch_id BIGINT, season SMALLINT, "
                f"recency_weight FLOAT DEFAULT 1.0)"
            )
            c.execute(f"INSERT INTO sim.{tbl} VALUES (1, 2024, NULL)")
        c.execute("CREATE SCHEMA derived")
        c.execute(
            "CREATE TABLE derived.park_factors ("
            " venue_id INTEGER, season SMALLINT, factor_type VARCHAR,"
            " factor_overall FLOAT, factor_vs_l FLOAT, factor_vs_r FLOAT,"
            " sample_pa INTEGER, regressed_factor FLOAT)"
        )
        c.execute(
            "CREATE TABLE sim.pool_build_metadata ("
            "pool_name VARCHAR, season SMALLINT, row_count BIGINT, "
            "source_max_game_date DATE, recency_ref_season SMALLINT, "
            "builder_version VARCHAR, built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (pool_name, season))"
        )
        c.execute(
            "CREATE TABLE migration_history "
            "(migration_id VARCHAR PRIMARY KEY, description VARCHAR)"
        )
        return c

    def test_migration_applies_and_enforces_parity(self):
        c = self._base_db()
        sql = open(MIGRATION_0007, encoding="utf-8").read()
        c.execute(sql)  # DuckDB handles multi-statement + comments natively

        # source_row_count added to metadata.
        meta_cols = [
            r[1] for r in c.execute(
                "PRAGMA table_info('sim.pool_build_metadata')"
            ).fetchall()
        ]
        self.assertIn("source_row_count", meta_cols)

        # recency_weight backfilled + NOT NULL on every pool.
        for tbl in ("pitch_pool", "outcome_pool", "stolen_base_pool"):
            val = c.execute(f"SELECT recency_weight FROM sim.{tbl}").fetchone()[0]
            self.assertEqual(val, 1.0)
            notnull = [
                r[3] for r in c.execute(f"PRAGMA table_info('sim.{tbl}')").fetchall()
                if r[1] == "recency_weight"
            ][0]
            self.assertTrue(notnull, f"{tbl}.recency_weight must be NOT NULL")

        # migration recorded.
        self.assertEqual(
            c.execute(
                "SELECT COUNT(*) FROM migration_history WHERE migration_id='0007'"
            ).fetchone()[0],
            1,
        )

    def test_migration_idempotent(self):
        c = self._base_db()
        sql = open(MIGRATION_0007, encoding="utf-8").read()
        c.execute(sql)
        c.execute(sql)  # second run must be a no-op
        n = c.execute(
            "SELECT COUNT(*) FROM migration_history WHERE migration_id='0007'"
        ).fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
