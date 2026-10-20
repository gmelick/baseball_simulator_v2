"""The nightly profile build: the player profiles, the defensive metrics and the
simulation pools.

``PlayerProfileComputor`` reads Postgres ``raw.*`` through DuckDB's postgres
extension (attached read-only) and writes the ``derived.*`` profile tables and
the ``sim.*`` pools into the DuckDB file. The builders live in
``pipeline/batch/profiles/``, one mixin per group. This module keeps the class,
the run order, the season delete and the entry point.

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

The entry point then computes the league averages. Usage::

    python -m pipeline.batch.player_profile_computor --seasons 2026
    python -m pipeline.batch.player_profile_computor --seasons 2017 ... 2026 --full-rebuild

A partial rebuild must include the season in progress: every engine refuses a
profile table stamped at two dates (the one-cutoff rule, SIM-551).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

import duckdb

from db.introspect import duck_relation_readable
from pipeline.batch.profiles.base import ComputorBase
from pipeline.batch.profiles.baserunner import BaserunnerProfilesMixin
from pipeline.batch.profiles.batter import BatterProfilesMixin
from pipeline.batch.profiles.catcher import CatcherProfilesMixin
from pipeline.batch.profiles.constants import (
    ARM_ALPHA_PRIOR_CHANCES as ARM_ALPHA_PRIOR_CHANCES,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.constants import (
    BASERUNNER_TAIL_COLUMNS as BASERUNNER_TAIL_COLUMNS,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.constants import (
    FIELDER_TAIL_COLUMNS as FIELDER_TAIL_COLUMNS,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.constants import (
    JUMP_ALPHA_PRIOR_PLAYS as JUMP_ALPHA_PRIOR_PLAYS,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.defense import (
    RUNS_PER_BLOCK_SAVED as RUNS_PER_BLOCK_SAVED,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.defense import (
    RUNS_PER_OAA_INFIELD as RUNS_PER_OAA_INFIELD,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.defense import (
    RUNS_PER_OAA_OUTFIELD as RUNS_PER_OAA_OUTFIELD,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.defense import (
    RUNS_PER_STRIKE_ABOVE_AVG as RUNS_PER_STRIKE_ABOVE_AVG,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.fielder import FielderProfilesMixin
from pipeline.batch.profiles.fielder_plays import FielderPlayMetricsMixin
from pipeline.batch.profiles.league_averages import LeagueAverageProfiles
from pipeline.batch.profiles.leverage import build_run_expectancy_matrix
from pipeline.batch.profiles.manager import ManagerProfilesMixin
from pipeline.batch.profiles.park import ParkFactorsMixin
from pipeline.batch.profiles.pitcher import PitcherProfilesMixin
from pipeline.batch.profiles.pool_meta import (
    POOL_BUILDER_VERSION as POOL_BUILDER_VERSION,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.pool_meta import _ensure_index_if_not_exists
from pipeline.batch.profiles.pool_meta import (
    _source_state as _source_state,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.pool_meta import (
    recency_weight as recency_weight,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.pools import SimPoolsMixin
from pipeline.batch.profiles.sql import (
    PHYSICAL_COLUMN_ORDER as PHYSICAL_COLUMN_ORDER,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.sql import (
    SQL_OUTCOME_TYPE as SQL_OUTCOME_TYPE,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.sql import (
    SQL_PITCHER_PITCH_COUNT as SQL_PITCHER_PITCH_COUNT,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.sql import (
    SQL_SWING as SQL_SWING,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.sql import (
    SQL_TIMES_THROUGH_ORDER as SQL_TIMES_THROUGH_ORDER,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.sql import (
    SQL_WHIFF as SQL_WHIFF,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.sql import (
    _sql_stance_select as _sql_stance_select,  # re-export: removed in the integration pass
)
from pipeline.batch.profiles.sql import (
    _sql_swing_select as _sql_swing_select,  # re-export: removed in the integration pass
)
from pipeline.statcast_events import (
    BALL_TYPES as BALL_TYPES,  # re-export: removed in the integration pass
)
from pipeline.statcast_events import (
    CALLED_STRIKE_TYPES as CALLED_STRIKE_TYPES,  # re-export: removed in the integration pass
)
from pipeline.statcast_events import (
    FOUL_TYPES as FOUL_TYPES,  # re-export: removed in the integration pass
)
from pipeline.statcast_events import (
    STRIKE_THREE_FOUL_TYPES as STRIKE_THREE_FOUL_TYPES,  # re-export: removed in the integration pass
)
from pipeline.statcast_events import (
    WHIFF_TYPES as WHIFF_TYPES,  # re-export: removed in the integration pass
)

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
    SimPoolsMixin,
    ComputorBase,
):
    """Runs the profile build: every builder mixin, in dependency order.

    The aggregation runs in DuckDB over the attached Postgres tables. The
    pitcher arsenal mixtures are fitted in Python (scikit-learn). Every result
    goes to the DuckDB file at ``duckdb_path``.
    """

    def __init__(self, pg_dsn: str, duckdb_path: str) -> None:
        """
        Parameters
        ----------
        pg_dsn : str
            PostgreSQL DSN, e.g. "postgresql://user:pass@localhost:5432/baseball"
        duckdb_path : str
            Path to the DuckDB file. Created if it does not exist.
        """
        self._pg_dsn = pg_dsn
        self._duckdb_path = duckdb_path
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._re_matrix: dict = {}  # populated before defensive steps

    def _connect(self) -> None:
        log.info("Opening DuckDB at %s …", self._duckdb_path)
        self._conn = duckdb.connect(self._duckdb_path)
        self._conn.execute("INSTALL postgres; LOAD postgres;")
        self._conn.execute(f"ATTACH '{self._pg_dsn}' AS pg (TYPE postgres, READ_ONLY);")
        log.info("PostgreSQL attached as 'pg'.")
        self._run_schema_ddl()

    def _close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _run_schema_ddl(self) -> None:
        """
        Creates all DuckDB tables via 02_duckdb_schema.sql (the single
        canonical schema that includes derived, sim, and per-play detail tables).
        Idempotent — uses CREATE TABLE IF NOT EXISTS throughout.
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
        """
        Run the full nightly pre-computation job.

        Parameters
        ----------
        seasons : list[int]
            Seasons to (re)compute. Defaults to current year.
        full_rebuild : bool
            If True, deletes and rewrites all derived metrics for the seasons.
        """
        if seasons is None:
            seasons = [datetime.today().year]

        self._connect()
        try:
            t0 = time.time()
            log.info("=== Player Profile & Defensive Metrics Pre-computation ===")
            log.info("  Seasons: %s | full_rebuild=%s", seasons, full_rebuild)

            # Check every stamped table first, so a run that the batter step
            # would refuse does not spend an hour per season on the pitcher
            # fits before it stops. Each builder repeats its own check: the
            # scripts call the builders one at a time.
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
            # The per-game bullpen workload (_compute_bullpen_workload) is not
            # built here: the bullpen-availability ingest computes it against
            # the live roster, so a nightly copy would never be read.
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
            # A pool rebuilds only the seasons whose source changed, unless
            # this is a full rebuild.
            incremental = not full_rebuild
            self._build_pitch_pool(seasons, incremental=incremental)
            self._build_outcome_pool(seasons, incremental=incremental)
            self._build_stolen_base_pool(seasons, incremental=incremental)
            self._build_steal_opportunity_pool(seasons, incremental=incremental)
            # Reads sim.outcome_pool, so it follows that rebuild.
            self._build_advancement_opportunity_pool(seasons, incremental=incremental)
            # The outfield arm block: an UPDATE over the fielder rows
            # aggregated above, from the advancement pool.
            self._fill_outfield_arm_block(seasons, asof=asof)
            # The intentional-walk rates: a full recompute every run (the
            # table is small and covers the pool window).
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
        ``tests/unit/test_profile_indexes_and_etl_fallbacks.py`` with its
        reason; that test fails otherwise.

        Left out on purpose:
          - derived.run_expectancy_matrix, keyed by season range, not season.
          - the sim.* pools: each pool's build deletes its own seasons.
        """
        season_list = ", ".join(str(s) for s in seasons)
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
        # Drop the secondary indexes before the bulk DELETE (a large DELETE on
        # indexed tables is slow) and recreate them after. The tables are never
        # left without their indexes:
        #   * the recreate runs in ``finally``, so a DELETE error still reaches it;
        #   * the recreate is idempotent (DROP IF EXISTS, then CREATE ... IF NOT
        #     EXISTS), so a half-applied earlier run cannot fail it;
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

        Each recreate is ``DROP INDEX IF EXISTS`` and then ``CREATE INDEX IF NOT
        EXISTS`` (:func:`_ensure_index_if_not_exists`), so a half-applied
        earlier run cannot fail it. Each is then checked against
        ``duckdb_indexes()``, so an index that silently failed is reported.
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


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    import argparse
    import os
    import sys
    from datetime import datetime

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

    if not args.dsn:
        sys.stderr.write("ERROR: no Postgres DSN. Set BASEBALL_DB_DSN or pass --dsn.\n")
        sys.exit(2)

    log.info(
        "Starting player profile compute - dsn=%s duckdb=%s seasons=%s full_rebuild=%s",
        args.dsn.split("@", 1)[-1] if "@" in args.dsn else args.dsn,
        args.duckdb_path,
        args.seasons,
        args.full_rebuild,
    )

    computor = PlayerProfileComputor(
        pg_dsn=args.dsn,
        duckdb_path=args.duckdb_path,
    )
    computor.run(seasons=args.seasons, full_rebuild=args.full_rebuild, asof=args.asof)
    log.info("Player profile compute complete.")

    if not args.skip_league_averages:
        log.info("Computing league averages ...")
        LeagueAverageProfiles(args.duckdb_path).compute(args.seasons or [datetime.today().year])
        log.info("League averages complete.")
