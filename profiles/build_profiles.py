"""Run the nightly build: the player profiles, the defensive metrics and the simulation pools.

``PlayerProfileComputor`` reads Postgres ``raw.*`` through DuckDB's postgres extension
(attached read-only as ``pg``). It writes the ``derived.*`` profile tables and the ``sim.*``
pools into the DuckDB file. The builders are mixins, one per group, in ``profiles/`` and
``pools/``. This module holds the class, the run order, the season delete and the entry point.

The run order (``PlayerProfileComputor.run``):

1. The park factors.
2. The pitcher profiles (the arsenal mixture fit is the slowest step).
3. The batter profiles.
4. The baserunner profiles and the two steal-engine tables.
5. The manager profiles.
6. The run-expectancy matrix, the catcher and fielder metrics, and their
   season aggregates.
7. The simulation pools, then the outfield arm block (it reads the advancement
   pool), the intentional-walk rates and the plate-appearance situations.

The entry point then computes the league averages (``LeagueAverageProfiles``). Usage::

    python -m profiles.build_profiles --seasons 2026
    python -m profiles.build_profiles --seasons 2017 ... 2026 --full-rebuild

A partial rebuild must include the season in progress: every engine refuses a
profile table stamped at two dates (the one-cutoff rule).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from pathlib import Path

import duckdb

from db.dsn import attach_pg
from db.introspect import duck_relation_readable
from pools.build_advancement_pool import AdvancementPoolMixin
from pools.build_at_bat_situations import AtBatSituationsMixin
from pools.build_batted_ball_pool import BattedBallPoolMixin
from pools.build_ibb_rates import IbbRatesMixin
from pools.build_pitch_pool import PitchPoolMixin
from pools.build_steal_pool import StealPoolMixin
from profiles.base import ComputorBase
from profiles.baserunner import BaserunnerProfilesMixin
from profiles.batter import BatterProfilesMixin
from profiles.catcher import CatcherProfilesMixin
from profiles.fielder import FielderProfilesMixin
from profiles.fielder_plays import FielderPlayMetricsMixin
from profiles.league_averages import LeagueAverageProfiles
from profiles.leverage import build_run_expectancy_matrix
from profiles.manager import ManagerProfilesMixin
from profiles.park_factors import ParkFactorsMixin
from profiles.pitcher import PitcherProfilesMixin
from profiles.sql import sql_seasons

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("player_profile_computor")


class PlayerProfileComputor(
    ParkFactorsMixin,
    PitcherProfilesMixin,
    BatterProfilesMixin,
    BaserunnerProfilesMixin,
    ManagerProfilesMixin,
    CatcherProfilesMixin,
    FielderPlayMetricsMixin,
    FielderProfilesMixin,
    PitchPoolMixin,
    BattedBallPoolMixin,
    StealPoolMixin,
    AdvancementPoolMixin,
    IbbRatesMixin,
    AtBatSituationsMixin,
    ComputorBase,
):
    """Run the profile build: every builder mixin, in dependency order.

    DuckDB runs the aggregation over the attached Postgres tables. scikit-learn
    fits the pitcher arsenal mixtures in Python. Every result goes to the DuckDB
    file at ``duckdb_path``.
    """

    def __init__(self, pg_dsn: str, duckdb_path: str) -> None:
        """Store the two addresses; ``run`` opens the connection.

        ``pg_dsn`` is the Postgres address, e.g.
        ``postgresql://user:pass@localhost:5432/baseball``. DuckDB creates the
        file at ``duckdb_path`` when it does not exist.
        """
        self._pg_dsn = pg_dsn
        self._duckdb_path = duckdb_path
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._re_matrix: dict = {}  # run() fills it before the defensive metrics

    def _connect(self) -> None:
        """Open the DuckDB file, attach Postgres as ``pg`` and apply the schema."""
        log.info("Opening DuckDB at %s …", self._duckdb_path)
        self._conn = duckdb.connect(self._duckdb_path)
        attach_pg(self._conn, self._pg_dsn)
        log.info("PostgreSQL attached as 'pg'.")
        self._run_schema_ddl()

    def _close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _run_schema_ddl(self) -> None:
        """Create the DuckDB tables from ``db/schemas/02_duckdb_schema.sql`` when absent.

        The file is the one canonical schema: the derived, sim and per-play
        detail tables. Every statement is ``CREATE ... IF NOT EXISTS``, so a
        rerun is safe. The path is relative to the working directory, so run
        from the repository root. Without the file, only the two schemas are
        created, and the tables must already exist.
        """
        if duck_relation_readable(self._conn, "derived.pitcher_season_metrics"):
            log.info("DuckDB schema already present — skipping DDL.")
            return

        schema_file = Path.cwd() / "db" / "schemas" / "02_duckdb_schema.sql"
        if schema_file.exists():
            ddl = schema_file.read_text()
            self._conn.execute(ddl)
            log.info("Schema DDL applied from 02_duckdb_schema.sql.")
        else:
            self._conn.execute("CREATE SCHEMA IF NOT EXISTS derived;")
            self._conn.execute("CREATE SCHEMA IF NOT EXISTS sim;")
            log.warning("02_duckdb_schema.sql not found. Tables must already exist in DuckDB.")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(
        self,
        seasons: list[int] | None = None,
        full_rebuild: bool = False,
        asof: date | None = None,
    ) -> None:
        """Run the nightly build for ``seasons`` (default: the current year).

        ``full_rebuild`` deletes the seasons' derived rows first and rebuilds
        every pool in full. ``asof`` cuts every source off at that date, for a
        backtest; None means everything known today.
        """
        if seasons is None:
            seasons = [date.today().year]

        self._connect()
        try:
            t0 = time.time()
            log.info("=== Player Profile & Defensive Metrics Pre-computation ===")
            log.info("  Seasons: %s | full_rebuild=%s", seasons, full_rebuild)

            # Check every stamped table first: a run that the batter step
            # refuses must not first spend an hour per season on the pitcher
            # fits. Each builder repeats its own check, because scripts call
            # the builders one at a time.
            for table in self.STAMPED_TABLES:
                self._refuse_a_mixed_cutoff(table, seasons, asof or date.today())

            if full_rebuild:
                self._delete_seasons(seasons)

            # ── The profiles ─────────────────────────────────────────────────
            self._compute_park_factors(seasons)
            self._compute_pitcher_profiles(seasons, asof=asof)  # the slowest step
            self._compute_batter_profiles(seasons, asof=asof)
            # The infield and double-play metrics below read the sprint speeds.
            self._compute_baserunner_profiles(seasons, asof=asof)
            self._build_baserunner_steal_metrics(seasons, asof=asof)
            self._build_pitcher_steal_metrics(seasons, asof=asof)
            # The per-game bullpen workload is not built here: the
            # bullpen-availability load computes it against the live roster
            # (compute_bullpen_workload in ingest/load_bullpen_availability.py).
            self._compute_manager_profiles(seasons, asof=asof)

            # ── The defensive metrics ────────────────────────────────────────
            # The run-expectancy matrix comes first: the double-play run value reads it.
            self._re_matrix = build_run_expectancy_matrix(self._conn, seasons, asof=asof)

            # The catcher: framing, blocking, throwing (each fills a temp table
            # the season aggregate reads).
            self._compute_catcher_framing(seasons, asof=asof)
            self._compute_catcher_blocking(seasons, asof=asof)
            self._compute_catcher_throwing(seasons, asof=asof)

            # The outfield. The arm block reads the advancement pool, so
            # _fill_outfield_arm_block runs after the pools below.
            self._compute_outfield_catch_probability(seasons, asof=asof)

            # The infield.
            self._compute_infield_oaa(seasons, asof=asof)
            self._compute_dp_metrics(seasons, asof=asof)
            self._compute_bunt_defense(seasons, asof=asof)
            self._compute_first_base_scooping(seasons, asof=asof)
            self._compute_error_decomposition(seasons, asof=asof)

            # The season aggregates: the temp tables into
            # derived.fielder_season_metrics and derived.catcher_season_metrics.
            self._aggregate_fielder_season_metrics(seasons, asof=asof)
            self._aggregate_catcher_season_metrics(seasons, asof=asof)
            # The uncaught-strike-three slice of the receiving profile (an
            # UPDATE over the catcher rows aggregated above).
            self._compute_catcher_uncaught_k3(seasons, asof=asof)

            # ── The simulation pools ─────────────────────────────────────────
            # A pool rebuilds only the seasons whose source changed, except on
            # a full rebuild.
            incremental = not full_rebuild
            self._build_pitch_pool(seasons, incremental=incremental)
            self._build_outcome_pool(seasons, incremental=incremental)
            self._build_stolen_base_pool(seasons, incremental=incremental)
            self._build_steal_opportunity_pool(seasons, incremental=incremental)
            # The advancement pool reads sim.outcome_pool, so it comes after it.
            self._build_advancement_opportunity_pool(seasons, incremental=incremental)
            # The outfield arm block: an UPDATE over the fielder rows
            # aggregated above, from the advancement pool.
            self._fill_outfield_arm_block(seasons, asof=asof)
            # The intentional-walk rates: a full recompute from this run's
            # seasons only (the nightly passes one season; SIM-649).
            self._build_ibb_rates(seasons)

            # The plate-appearance situations the situation engine reads
            # (INSERT OR REPLACE, so no incremental gating).
            self._build_at_bat_situations(seasons)

            elapsed = time.time() - t0
            log.info("=== Completed in %.1f seconds ===", elapsed)

        finally:
            self._close()

    # ------------------------------------------------------------------
    # The season delete of a full rebuild
    # ------------------------------------------------------------------

    def _delete_seasons(self, seasons: list[int]) -> None:
        """Remove the rows of ``seasons`` from every season-keyed derived table.

        Every table is listed by name. A new ``derived.*`` table with a
        ``season`` column goes on this list, or on
        ``_EXCLUDED_FROM_DELETE_SEASONS`` in
        ``tests/unit/profiles/test_delete_seasons_coverage.py`` with its
        reason; that test fails otherwise.

        Left out on purpose:
          - derived.run_expectancy_matrix, keyed by season range, not season.
          - the sim.* pools: each pool's build deletes its own seasons.
        """
        season_list = sql_seasons(seasons)
        tables = [
            # Children before parents (the foreign keys).
            "derived.pitcher_gmm_components",
            "derived.pitcher_season_metrics",
            "derived.batter_season_metrics",
            "derived.fielder_season_metrics",
            "derived.baserunner_season_metrics",
            "derived.baserunner_steal_metrics",
            "derived.pitcher_steal_metrics",
            "derived.catcher_season_metrics",
            "derived.manager_season_metrics",
            "derived.park_factors",
            # Its build is INSERT OR REPLACE, but the table is season-keyed, so
            # a full rebuild clears it.
            "derived.at_bat_situations",
            # The per-play detail tables.
            "derived.outfield_play_detail",
            "derived.infield_play_detail",
            "derived.dp_play_detail",
        ]
        # Drop the secondary indexes before the bulk DELETE, because a large
        # DELETE on indexed tables is slow, and recreate them after. Three
        # things keep the indexes from going missing:
        #   * the recreate runs in ``finally``, so a DELETE error still reaches it;
        #   * the recreate is DROP IF EXISTS, then CREATE ... IF NOT EXISTS, so
        #     a half-applied earlier run cannot fail it;
        #   * each recreate is checked against duckdb_indexes(), and a missing
        #     index is logged as an ERROR.
        saved_indexes: list[tuple[str, str]] = []
        for tbl in tables:
            schema_name, table_name = tbl.split(".")
            try:
                rows = self._conn.execute(f"""
                    SELECT index_name, sql
                    FROM duckdb_indexes()
                    WHERE schema_name = '{schema_name}'
                      AND table_name  = '{table_name}'
                      AND is_primary  = false
                """).fetchall()
            except Exception:
                rows = []
            for idx_name, idx_sql in rows:
                saved_indexes.append((f"{schema_name}.{idx_name}", idx_sql))
                self._conn.execute(f"DROP INDEX IF EXISTS {schema_name}.{idx_name}")

        if saved_indexes:
            log.info("  Dropped %d secondary indexes before DELETE.", len(saved_indexes))

        try:
            for tbl in tables:
                self._conn.execute(f"DELETE FROM {tbl} WHERE season IN ({season_list})")
        finally:
            failed = self._recreate_indexes(saved_indexes)
            if saved_indexes:
                log.info(
                    "  Recreated %d/%d secondary indexes after DELETE.",
                    len(saved_indexes) - len(failed),
                    len(saved_indexes),
                )
            if failed:
                log.error(
                    "  %d secondary index(es) FAILED to recreate and are now MISSING: %s. "
                    "Queries still work but lose this index's speedup — investigate and "
                    "rebuild before relying on profile-read latency.",
                    len(failed),
                    ", ".join(failed),
                )

        log.info("Cleared existing rows for seasons %s.", seasons)

    def _recreate_indexes(self, saved_indexes: list[tuple[str, str]]) -> list[str]:
        """Recreate the dropped secondary indexes; return the names that failed.

        Each recreate is ``DROP INDEX IF EXISTS``, then ``CREATE INDEX IF NOT
        EXISTS`` (:func:`_ensure_index_if_not_exists`), so a half-applied
        earlier run cannot fail it. A check against ``duckdb_indexes()`` then
        reports an index that failed silently.
        """
        failed: list[str] = []
        for idx_qualified_name, idx_sql in saved_indexes:
            schema_name, _, idx_name = idx_qualified_name.partition(".")
            stmt = _ensure_index_if_not_exists(idx_sql)
            try:
                self._conn.execute(f"DROP INDEX IF EXISTS {idx_qualified_name}")
                self._conn.execute(stmt)
            except Exception as exc:  # noqa: BLE001 — recorded + escalated by caller
                log.warning("  Could not recreate index %s: %s", idx_qualified_name, exc)
                failed.append(idx_qualified_name)
                continue
            # Check the index is present (a recreate can succeed without creating it).
            try:
                present = self._conn.execute(
                    "SELECT 1 FROM duckdb_indexes() WHERE schema_name = ? AND index_name = ?",
                    [schema_name, idx_name],
                ).fetchone()
            except Exception:  # noqa: BLE001 — can't verify → don't false-alarm
                present = (True,)
            if not present:
                failed.append(idx_qualified_name)
        return failed


def _ensure_index_if_not_exists(create_index_sql: str) -> str:
    """Return a ``CREATE [UNIQUE] INDEX`` statement with ``IF NOT EXISTS`` added.

    The clause lets a rerun, or a half-applied earlier run, recreate an index
    that is already there. A statement that already has the clause, or is not
    a CREATE INDEX form this function knows, comes back unchanged; the caller
    drops the index IF EXISTS first anyway.
    """
    stmt = create_index_sql.strip()
    low = stmt.lower()
    if "if not exists" in low:
        return stmt
    for prefix in ("create unique index ", "create index "):
        if low.startswith(prefix):
            return stmt[: len(prefix)] + "IF NOT EXISTS " + stmt[len(prefix) :]
    return stmt


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description=(
            "Compute player profile + defensive metrics from Postgres and "
            "write them into the DuckDB analytical store that the similarity "
            "engines read from."
        ),
    )
    parser.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=None,
        help="One or more seasons to (re)compute. Defaults to current year.",
    )
    parser.add_argument(
        "--full-rebuild",
        action="store_true",
        help="Delete and rewrite all derived metrics for the requested seasons.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("BASEBALL_DB_DSN"),
        help="Postgres DSN. Defaults to BASEBALL_DB_DSN env var.",
    )
    parser.add_argument(
        "--duckdb-path",
        default=os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb"),
        help="Path to the DuckDB output file.",
    )
    parser.add_argument(
        "--skip-league-averages",
        action="store_true",
        help="Skip the LeagueAverageProfiles.compute() pass.",
    )
    parser.add_argument(
        "--asof",
        type=date.fromisoformat,
        help=(
            "Build the profiles as of this date. Every source is cut off there, "
            "so a backtest cannot use data recorded after the day it simulates. "
            "Unset means everything known today."
        ),
    )
    args = parser.parse_args()
    seasons = args.seasons or [date.today().year]

    if not args.dsn:
        sys.stderr.write("ERROR: no Postgres DSN. Set BASEBALL_DB_DSN or pass --dsn.\n")
        sys.exit(2)

    log.info(
        "Starting player profile compute - dsn=%s duckdb=%s seasons=%s full_rebuild=%s",
        args.dsn.split("@", 1)[-1] if "@" in args.dsn else args.dsn,
        args.duckdb_path,
        seasons,
        args.full_rebuild,
    )

    computor = PlayerProfileComputor(
        pg_dsn=args.dsn,
        duckdb_path=args.duckdb_path,
    )
    computor.run(seasons=seasons, full_rebuild=args.full_rebuild, asof=args.asof)
    log.info("Player profile compute complete.")

    if not args.skip_league_averages:
        log.info("Computing league averages ...")
        LeagueAverageProfiles(args.duckdb_path).compute(seasons)
        log.info("League averages complete.")
