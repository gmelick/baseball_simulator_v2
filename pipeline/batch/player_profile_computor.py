"""
player_profile_computor.py
==========================
Step 1.4 — Player Profile & Defensive Metrics Pre-computation
MLB Baseball Simulation Platform

Reads from PostgreSQL raw.pitches (and raw.games / raw.managers for context)
and writes all derived metrics + simulation pools to DuckDB.

Architecture
------------
  PostgreSQL (raw.*)
       │
       │  DuckDB postgres extension attaches directly — no full data load into Python
       ▼
  DuckDB (in-memory aggregation engine)
       │
       ├── derived.pitcher_season_metrics   (GMM model fit in Python via sklearn)
       ├── derived.pitcher_gmm_components   (one row per GMM component)
       ├── derived.batter_season_metrics
       ├── derived.fielder_season_metrics
       ├── derived.baserunner_season_metrics
       ├── derived.catcher_season_metrics
       ├── derived.manager_season_metrics
       ├── derived.park_factors
       │
       ├── sim.pitch_pool                   (full rebuild — quality-filtered raw.pitches)
       ├── sim.outcome_pool                 (in-play subset with batted ball detail)
       └── sim.stolen_base_pool             (SB attempts, denormalized from derived.*)

Run order matters:
  1. Park factors           (no dependencies)
  2. Pitcher profiles       (GMM — most expensive step)
  3. Batter profiles
  4. Fielder profiles
  5. Baserunner profiles
  6. Catcher profiles
  7. Manager profiles
  8. sim.pitch_pool         (sim pools go last)
  9. sim.outcome_pool
  10. sim.stolen_base_pool  (denormalizes from derived.* — must be after step 7)

Nightly usage
-------------
  # Recompute current season only (fast — ~5 min)
  computor = PlayerProfileComputor(
      pg_dsn="postgresql://user:pass@localhost:5432/baseball",
      duckdb_path="/data/baseball_sim.duckdb",
  )
  computor.run(seasons=[2025])

  # Full historical rebuild (slow — ~60 min for 3 seasons)
  computor.run(seasons=[2022, 2023, 2024, 2025], full_rebuild=True)

Dependencies
------------
  pip install duckdb psycopg2-binary scikit-learn numpy pandas scipy
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


# ---------------------------------------------------------------------------
# Main computor class
# ---------------------------------------------------------------------------


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
    """
    Orchestrates the full Step 1.4 nightly batch job.

    All heavy aggregation runs in DuckDB via the postgres extension.
    GMM fitting for pitcher profiles runs in Python (sklearn).
    Defensive metrics (framing, blocking, OAA, DP, arm) are computed
    and aggregated into derived.fielder/catcher_season_metrics inline.
    All results are written to the DuckDB file at duckdb_path.
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

            # SIM-551: check every stamped table up front, so a run that the
            # batter section would refuse does not first spend an hour per
            # season on the pitcher GMMs. Each builder repeats its own check
            # (the scripts call the builders one at a time).
            for table in self.STAMPED_TABLES:
                self._refuse_a_mixed_cutoff(table, seasons, asof or date.today())

            if full_rebuild:
                self._delete_seasons(seasons)

            # ── Non-defensive profiles (ordered for dependency satisfaction) ──
            self._compute_park_factors(seasons)  # 1. no dependencies
            self._compute_pitcher_profiles(seasons, asof=asof)  # 2. GMM — most expensive
            self._compute_batter_profiles(seasons, asof=asof)  # 3.
            self._compute_baserunner_profiles(
                seasons, asof=asof
            )  # 4. infield/DP need sprint speeds
            self._build_baserunner_steal_metrics(
                seasons, asof=asof
            )  # 4b. SIM-408 steal-engine table
            self._build_pitcher_steal_metrics(seasons, asof=asof)  # 4c. SIM-408 pitcher-steal table
            # SIM-433: per-game bullpen rest/workload (_compute_bullpen_workload) is
            # NOT derived here — the bullpen-availability ingest builds its own computor
            # and recomputes it against the live roster/IL, so running it in the nightly
            # rebuild would only pay for a discarded query.
            self._compute_manager_profiles(seasons, asof=asof)  # 5.

            # ── Defensive metrics ────────────────────────────────────────────
            # RE matrix first — the DP run value depends on it
            self._re_matrix = build_run_expectancy_matrix(self._conn, seasons, asof=asof)

            # Catcher: framing → blocking → throwing (temp tables consumed in order)
            self._compute_catcher_framing(seasons, asof=asof)
            self._compute_catcher_blocking(seasons, asof=asof)
            self._compute_catcher_throwing(seasons, asof=asof)

            # Outfield. The arm block is NOT built here: it reads the
            # advancement pool, so `_fill_outfield_arm_block` runs after the
            # pools below (step 6b).
            self._compute_outfield_catch_probability(seasons, asof=asof)

            # Infield
            self._compute_infield_oaa(seasons, asof=asof)
            self._compute_dp_metrics(seasons, asof=asof)
            self._compute_bunt_defense(seasons, asof=asof)
            self._compute_first_base_scooping(seasons, asof=asof)
            self._compute_error_decomposition(seasons, asof=asof)

            # Season-level aggregation: temp tables → derived.fielder/catcher_season_metrics
            self._aggregate_fielder_season_metrics(seasons, asof=asof)
            self._aggregate_catcher_season_metrics(seasons, asof=asof)
            # SIM-517: the strike-3 slice of the receiving profile (an UPDATE
            # over the freshly aggregated catcher rows).
            self._compute_catcher_uncaught_k3(seasons, asof=asof)

            # ── Simulation pools (last — denormalize from derived.*) ──────────
            # SIM-095: incremental rebuild unless a full_rebuild was requested.
            incremental = not full_rebuild
            self._build_pitch_pool(seasons, incremental=incremental)
            self._build_outcome_pool(seasons, incremental=incremental)
            self._build_stolen_base_pool(seasons, incremental=incremental)
            self._build_steal_opportunity_pool(seasons, incremental=incremental)
            # SIM-510: reads sim.outcome_pool, so it must follow that rebuild.
            self._build_advancement_opportunity_pool(seasons, incremental=incremental)
            # 6b. SIM-550: the outfield arm block, from the advancement pool
            # (an UPDATE over the fielder rows aggregated above).
            self._fill_outfield_arm_block(seasons, asof=asof)
            # SIM-515: the measured IBB rate table (a full recompute — the
            # table is tiny and window-scoped, so no incremental gating).
            self._build_ibb_rates(seasons)

            # SIM-408: per-PA situation facts for the Step 2.9 situation KDTree
            # engine (idempotent INSERT OR REPLACE, so no incremental gating).
            self._build_at_bat_situations(seasons)

            elapsed = time.time() - t0
            log.info("=== Completed in %.1f seconds ===", elapsed)

        finally:
            self._close()

    # ------------------------------------------------------------------
    # Teardown helpers
    # ------------------------------------------------------------------

    def _delete_seasons(self, seasons: list[int]) -> None:
        """Remove existing rows for the given seasons from all derived + sim tables.

        SIM-091: Per-play detail tables (outfield/infield/dp) are explicitly
        enumerated below.  When you add a new derived.* table with a `season`
        column, also add it here OR add it to _EXCLUDED_FROM_DELETE_SEASONS in
        tests/unit/test_profile_indexes_and_etl_fallbacks.py with a comment — the
        SIM-091 regression test enforces this.

        Intentionally omitted:
          - derived.run_expectancy_matrix — keyed by season_range, not season.
          - sim.* pools — each has its own DELETE in its own build method.
        """
        season_list = ", ".join(str(s) for s in seasons)
        tables = [
            # FK-ordered: children before parents
            "derived.pitcher_gmm_components",
            "derived.pitcher_season_metrics",
            "derived.batter_season_metrics",
            "derived.fielder_season_metrics",
            "derived.baserunner_season_metrics",
            "derived.baserunner_steal_metrics",  # SIM-408 steal-engine table
            "derived.pitcher_steal_metrics",  # SIM-408 pitcher-steal-engine table
            "derived.catcher_season_metrics",
            "derived.manager_season_metrics",
            "derived.park_factors",
            # SIM-408: per-PA situation facts (own INSERT OR REPLACE build, but
            # season-keyed so full_rebuild must clear it — SIM-091 requires it here)
            "derived.at_bat_situations",
            # Per-play detail tables (SIM-091 explicit enumeration)
            "derived.outfield_play_detail",
            "derived.infield_play_detail",
            "derived.dp_play_detail",
        ]
        # SIM-419: drop secondary indexes before the bulk DELETE (a large DELETE
        # against indexed tables is slow), then recreate them afterwards.
        # Hardened so the tables are NEVER left de-indexed:
        #   * the DELETE + recreate run in try/finally — a DELETE error still
        #     triggers the recreate (previously an error skipped it entirely);
        #   * recreate is idempotent (DROP IF EXISTS + injected IF NOT EXISTS in
        #     _recreate_indexes), so a partially-applied prior run can't fail it;
        #   * recreate failures are collected + verified against duckdb_indexes()
        #     and escalated to ERROR (previously a silent WARNING), so a
        #     now-missing index is surfaced rather than swallowed.
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
        """Idempotently recreate dropped secondary indexes; return names that FAILED.

        SIM-419: each recreate is ``DROP INDEX IF EXISTS`` + a ``CREATE INDEX IF
        NOT EXISTS`` (injected via :func:`_ensure_index_if_not_exists`) so it is
        safe even if a prior run left the index half-applied, then verified
        against ``duckdb_indexes()`` so a silently-missing index is reported.
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
            # Verify the index is actually present now (don't trust a silent no-op).
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
            "SIM-534: build the batter profiles as of this date. Every source is "
            "cut off there, so a backtest cannot use data recorded after the day "
            "it simulates. Unset means everything known today."
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
