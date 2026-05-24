"""
test_data_engineer_sim162.py
============================
Regression test for Data Engineer ticket SIM-162 — LeagueAverageProfiles.

`LeagueAverageProfiles.compute(seasons)`
(pipeline/batch/player_profile_computor.py) reads five `derived.*` season-metric
tables and writes one league-average row per (entity_type, season) into
`derived.league_averages`.  Only rows with `below_minimum_sample = FALSE` feed
the averages.

The five entity types and their source tables:
    pitcher    <- derived.pitcher_season_metrics
    batter     <- derived.batter_season_metrics
    fielder    <- derived.fielder_season_metrics   (one row per position:
                  fielder_C, fielder_1B, ... fielder_RF)
    baserunner <- derived.baserunner_season_metrics
    catcher    <- derived.catcher_season_metrics

This test builds a real temporary DuckDB file with the minimal schema and a few
synthetic rows for each source table, runs compute() end-to-end, and asserts
that EVERY entity type produced at least one non-empty league-average insert.
The point is to lock that compute() never silently produces an empty insert for
any entity type (the failure mode that motivated SIM-162).

Run with:
    pytest tests/unit/test_data_engineer_sim162.py -v
"""

from __future__ import annotations

import os
import tempfile
import unittest

import duckdb


SEASONS = [2023, 2024]


def _build_source_tables(path: str) -> None:
    """Create the minimal derived.* schema + synthetic rows that compute()
    needs to populate every entity type.

    For each metric table we insert a couple of qualifying rows
    (below_minimum_sample = FALSE) per season, plus one disqualified row
    (below_minimum_sample = TRUE) to confirm the filter is exercised but does
    not starve the average.
    """
    conn = duckdb.connect(path)
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS derived")

        # --- pitcher_season_metrics ------------------------------------------
        conn.execute("""
            CREATE TABLE derived.pitcher_season_metrics (
                pitcher_id           INTEGER,
                season               SMALLINT,
                bb_rate              FLOAT,
                k_rate               FLOAT,
                csw_rate             FLOAT,
                zone_rate            FLOAT,
                chase_rate           FLOAT,
                era                  FLOAT,
                fip                  FLOAT,
                ground_ball_rate     FLOAT,
                fly_ball_rate        FLOAT,
                below_minimum_sample BOOLEAN
            )
        """)

        # --- batter_season_metrics -------------------------------------------
        conn.execute("""
            CREATE TABLE derived.batter_season_metrics (
                batter_id             INTEGER,
                season                SMALLINT,
                first_pitch_take_rate FLOAT,
                o_swing_rate          FLOAT,
                z_swing_rate          FLOAT,
                whiff_rate            FLOAT,
                contact_rate          FLOAT,
                walk_rate             FLOAT,
                k_rate                FLOAT,
                avg_exit_velo         FLOAT,
                hard_hit_rate         FLOAT,
                below_minimum_sample  BOOLEAN
            )
        """)

        # --- fielder_season_metrics ------------------------------------------
        conn.execute("""
            CREATE TABLE derived.fielder_season_metrics (
                fielder_id           INTEGER,
                position             VARCHAR,
                season               SMALLINT,
                outs_above_average   FLOAT,
                error_rate           FLOAT,
                arm_hold_rate        FLOAT,
                dp_run_value         FLOAT,
                below_minimum_sample BOOLEAN
            )
        """)

        # --- baserunner_season_metrics ---------------------------------------
        conn.execute("""
            CREATE TABLE derived.baserunner_season_metrics (
                runner_id                INTEGER,
                season                   SMALLINT,
                sprint_speed             FLOAT,
                extra_base_attempt_rate  FLOAT,
                extra_base_success_rate  FLOAT,
                sb_success_rate          FLOAT,
                below_minimum_sample     BOOLEAN
            )
        """)

        # --- catcher_season_metrics ------------------------------------------
        conn.execute("""
            CREATE TABLE derived.catcher_season_metrics (
                catcher_id           INTEGER,
                season               SMALLINT,
                framing_runs         FLOAT,
                blocking_runs        FLOAT,
                cs_rate              FLOAT,
                below_minimum_sample BOOLEAN
            )
        """)

        for season in SEASONS:
            # pitchers: two qualifying + one disqualified
            conn.executemany(
                "INSERT INTO derived.pitcher_season_metrics VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (1, season, 0.08, 0.24, 0.30, 0.50, 0.28, 3.80, 3.90,
                     0.44, 0.36, False),
                    (2, season, 0.10, 0.20, 0.28, 0.48, 0.26, 4.20, 4.10,
                     0.40, 0.40, False),
                    (3, season, 0.99, 0.01, 0.01, 0.01, 0.01, 9.99, 9.99,
                     0.01, 0.01, True),   # disqualified — must be excluded
                ],
            )

            # batters
            conn.executemany(
                "INSERT INTO derived.batter_season_metrics VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (10, season, 0.45, 0.30, 0.65, 0.24, 0.78, 0.09, 0.22,
                     89.0, 0.38, False),
                    (11, season, 0.50, 0.28, 0.68, 0.22, 0.80, 0.11, 0.20,
                     90.5, 0.42, False),
                    (12, season, 0.01, 0.99, 0.01, 0.99, 0.01, 0.00, 0.99,
                     50.0, 0.01, True),   # disqualified
                ],
            )

            # fielders — one qualifying row for every position so each
            # fielder_<pos> entity gets a non-empty group.
            positions = ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")
            fielder_rows = []
            for i, pos in enumerate(positions):
                fielder_rows.append(
                    (100 + i, pos, season, 2.5, 0.012, 0.30, 1.1, False)
                )
                fielder_rows.append(
                    (200 + i, pos, season, -1.0, 0.020, 0.25, -0.5, False)
                )
            conn.executemany(
                "INSERT INTO derived.fielder_season_metrics VALUES "
                "(?,?,?,?,?,?,?,?)",
                fielder_rows,
            )

            # baserunners
            conn.executemany(
                "INSERT INTO derived.baserunner_season_metrics VALUES "
                "(?,?,?,?,?,?,?)",
                [
                    (30, season, 27.5, 0.35, 0.55, 0.78, False),
                    (31, season, 28.8, 0.42, 0.60, 0.82, False),
                    (32, season, 20.0, 0.01, 0.01, 0.01, True),  # disqualified
                ],
            )

            # catchers
            conn.executemany(
                "INSERT INTO derived.catcher_season_metrics VALUES "
                "(?,?,?,?,?,?)",
                [
                    (40, season, 5.0, 2.0, 0.30, False),
                    (41, season, -3.0, 1.0, 0.22, False),
                    (42, season, -9.9, -9.9, 0.01, True),  # disqualified
                ],
            )
    finally:
        conn.close()


class TestLeagueAverageProfilesCompute(unittest.TestCase):
    """SIM-162: compute() must produce non-empty inserts for all 5 entity
    types."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.remove(self._db_path)  # let DuckDB create it fresh
        _build_source_tables(self._db_path)

    def tearDown(self):
        for p in (self._db_path, self._db_path + ".wal"):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _run_compute(self):
        from pipeline.batch.player_profile_computor import LeagueAverageProfiles

        LeagueAverageProfiles(self._db_path).compute(SEASONS)

    def _fetch_league_averages(self):
        conn = duckdb.connect(self._db_path)
        try:
            return conn.execute(
                "SELECT entity_type, season, profile_json "
                "FROM derived.league_averages ORDER BY entity_type, season"
            ).fetchall()
        finally:
            conn.close()

    def test_compute_runs_end_to_end(self):
        # Should not raise.
        self._run_compute()
        rows = self._fetch_league_averages()
        self.assertGreater(len(rows), 0, "compute() produced no rows at all")

    def test_every_entity_type_has_nonempty_insert(self):
        self._run_compute()
        rows = self._fetch_league_averages()
        entity_types = {r[0] for r in rows}

        # Pitcher, batter, baserunner, catcher are single entity types; the
        # fielder is expanded per position into fielder_<pos>.
        required_simple = {"pitcher", "batter", "baserunner", "catcher"}
        for et in required_simple:
            self.assertIn(
                et, entity_types,
                f"entity type {et!r} produced NO league-average insert "
                f"(SIM-162 regression). Got: {sorted(entity_types)}",
            )

        # At least one fielder_* entity must exist (the 5th entity type).
        fielder_entities = {e for e in entity_types if e.startswith("fielder_")}
        self.assertTrue(
            fielder_entities,
            "fielder entity type produced NO league-average insert "
            f"(SIM-162 regression). Got: {sorted(entity_types)}",
        )

    def test_each_entity_type_present_for_every_season(self):
        self._run_compute()
        rows = self._fetch_league_averages()
        # (entity_type -> set of seasons)
        seen: dict[str, set[int]] = {}
        for entity_type, season, _json in rows:
            seen.setdefault(entity_type, set()).add(int(season))

        for entity_type, seasons in seen.items():
            for s in SEASONS:
                self.assertIn(
                    s, seasons,
                    f"{entity_type} missing season {s}: a season was silently "
                    f"dropped (SIM-162 regression).",
                )

    def test_profile_json_is_non_null_and_populated(self):
        self._run_compute()
        rows = self._fetch_league_averages()
        self.assertTrue(rows)
        for entity_type, season, profile_json in rows:
            self.assertIsNotNone(
                profile_json,
                f"{entity_type}/{season} has NULL profile_json",
            )
            # JSON_OBJECT(...) renders to a non-trivial string with at least
            # one key/value pair.
            self.assertIn(
                ":", str(profile_json),
                f"{entity_type}/{season} profile_json looks empty: "
                f"{profile_json!r}",
            )

    def test_all_eight_fielder_positions_present(self):
        # The fielder entity is expanded into 8 positional sub-types; all
        # eight should be non-empty given we supplied a qualifying row per
        # position.
        self._run_compute()
        rows = self._fetch_league_averages()
        fielder_entities = {r[0] for r in rows if r[0].startswith("fielder_")}
        expected = {
            f"fielder_{p}"
            for p in ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")
        }
        self.assertEqual(
            expected, fielder_entities,
            f"missing fielder positions: {expected - fielder_entities}",
        )


if __name__ == "__main__":
    unittest.main()
