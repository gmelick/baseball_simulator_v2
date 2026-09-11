"""
SIM-542 — the fielder temp-table placeholders must name the columns the
aggregator actually reads.

``_ensure_fielder_temp_tables_exist`` creates an empty stand-in for any
``_tmp_*`` table a per-play builder (``_compute_outfield_catch_probability``,
``_compute_infield_oaa``, ``_compute_dp_metrics``, ``_compute_bunt_defense``,
``_compute_first_base_scooping``, ``_compute_error_decomposition``) has not
yet created this run — most often because that builder returned early on a
too-small sample. The placeholder exists ONLY so
``_aggregate_fielder_season_metrics``'s JOINs do not crash on a missing
table; every column name it declares must match what the real builder
produces and what the aggregator's SQL actually reads, or the JOIN succeeds
while the outer SELECT fails to bind.

Two placeholders had drifted from their real builders — ``_tmp_errors``
(``fielding_errors``/``throwing_errors``/``total_plays`` instead of
``fielding_error_count``/``throwing_error_count``/``total_plays_fielded``,
and ``fielding_error_rate``/``throwing_error_rate`` missing outright) and
``_tmp_bunt_defense`` (``bunt_outs``/``bunt_success_rate`` instead of
``bunt_outs_recorded``/``bunt_fielding_rate``). Both were silently correct
in production ONLY because their real builders have no sample-size guard
and always run before the aggregator in ``run()``'s call order, so the real
table already exists by the time ``_ensure_fielder_temp_tables_exist`` runs
and its ``CREATE TABLE IF NOT EXISTS`` is a no-op. A caller that exercises
the aggregator standalone — a test, a future refactor, a reordered
``run()`` — hits the placeholder directly and gets
``Binder Error: Values list "e" does not have a column named
"fielding_error_count"``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

from pipeline.batch.player_profile_computor import PlayerProfileComputor

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = (REPO / "db" / "schemas" / "02_duckdb_schema.sql").read_text(encoding="utf-8")


def _real_ddl(table: str) -> str:
    start = SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = SCHEMA_SQL.index("\n);", start)
    return SCHEMA_SQL[start : end + 3]


def _computor() -> PlayerProfileComputor:
    c = PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    return c


# ---------------------------------------------------------------------------
# The placeholders declare exactly the columns each real builder produces
# that the aggregator's SQL actually reads (checked directly against the
# CREATE TABLE text — no live DB needed for this half).
# ---------------------------------------------------------------------------

#: table -> the columns _aggregate_fielder_season_metrics's SQL references
#: from it (via a JOIN key or a SELECT column) — the minimum a placeholder
#: must carry, under the exact names the query uses.
_REQUIRED_COLUMNS = {
    "_tmp_of_plays": {
        "fielder_id",
        "position",
        "season",
        "catch_probability",
        "caught",
        "oaa_credit",
        "direction_cat",
        "star_rating",
    },
    "_tmp_if_plays": {
        "fielder_id",
        "position",
        "season",
        "out_probability",
        "out_recorded",
        "oaa_credit",
        "direction_category",
    },
    "_tmp_dp_plays": {
        "initiator_id",
        "initiator_position",
        "pivot_id",
        "pivot_position",
        "season",
        "dp_probability",
        "dp_turned",
        "dp_run_value",
    },
    "_tmp_errors": {
        "fielder_id",
        "position",
        "season",
        "fielding_error_count",
        "throwing_error_count",
        "fielding_error_rate",
        "throwing_error_rate",
        "error_rate",
    },
    "_tmp_bunt_defense": {
        "fielder_id",
        "position",
        "season",
        "bunt_opportunities",
        "bunt_outs_recorded",
        "bunt_fielding_rate",
    },
    "_tmp_1b_scoop": {
        "fielder_id",
        "position",
        "season",
        "scoop_opportunities",
        "scoop_success_rate",
    },
}


def _placeholder_ddl_text() -> str:
    c = _computor()
    executed: list[str] = []
    c._conn.execute.side_effect = lambda sql: executed.append(sql)
    c._ensure_fielder_temp_tables_exist()
    return "\n".join(executed)


@pytest.mark.parametrize("table,required", sorted(_REQUIRED_COLUMNS.items()))
def test_placeholder_declares_every_column_the_aggregator_reads(table: str, required: set) -> None:
    ddl_text = _placeholder_ddl_text()
    start = ddl_text.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = ddl_text.index(")", start)
    columns_text = ddl_text[start:end]
    for column in required:
        assert column in columns_text, (
            f"{table}'s placeholder is missing (or misnames) the column "
            f"'{column}', which _aggregate_fielder_season_metrics reads from it"
        )


# ---------------------------------------------------------------------------
# The end-to-end proof: run the REAL aggregator against nothing but the
# placeholders (every builder returned early / never ran) and confirm the
# INSERT's SELECT binds successfully. This is exactly the call that raised
# BinderException before the SIM-542 fix.
# ---------------------------------------------------------------------------


class TestAggregatorAgainstBarePlaceholders:
    def _conn(self) -> duckdb.DuckDBPyConnection:
        c = duckdb.connect(":memory:")
        c.execute("ATTACH ':memory:' AS pg")
        c.execute("CREATE SCHEMA pg.raw")
        c.execute(
            "CREATE TABLE pg.raw.sprint_speed (player_id INTEGER, season SMALLINT, "
            "sprint_speed DOUBLE)"
        )
        c.execute(
            "CREATE TABLE pg.raw.savant_arm_strength (player_id INTEGER, season SMALLINT, "
            "arm_overall DOUBLE, arm_1b DOUBLE, arm_2b DOUBLE, arm_3b DOUBLE, arm_ss DOUBLE, "
            "arm_lf DOUBLE, arm_cf DOUBLE, arm_rf DOUBLE)"
        )
        c.execute(
            "CREATE TABLE pg.raw.savant_baserunning (player_id INTEGER, season SMALLINT, "
            "n_opp_xb INTEGER, n_att_xb INTEGER, rate_att_xb DOUBLE, n_out INTEGER, "
            "est_rate_att_generic_fielder DOUBLE, fielder_runs_hold DOUBLE, "
            "fielder_runs_advances DOUBLE, fielder_runs_thrown_out DOUBLE)"
        )
        c.execute("CREATE SCHEMA derived")
        c.execute(_real_ddl("derived.fielder_season_metrics"))
        return c

    def test_live_build_binds_with_every_builder_skipped(self) -> None:
        """Every per-play builder returned early (or was never called) —
        _ensure_fielder_temp_tables_exist's placeholders are the ONLY
        source of the six _tmp_* tables. The INSERT must still bind."""
        c = self._conn()
        comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
        comp._conn = c
        comp._aggregate_fielder_season_metrics([2024], asof=None)
        assert c.execute("SELECT COUNT(*) FROM derived.fielder_season_metrics").fetchone() == (0,)

    def test_as_of_build_binds_with_every_builder_skipped(self) -> None:
        """Same proof for a point-in-time (SIM-537) build — the season-shift
        substitution touches the same Savant joins."""
        from datetime import date

        c = self._conn()
        comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
        comp._conn = c
        comp._aggregate_fielder_season_metrics([2024], asof=date(2024, 6, 1))
        assert c.execute("SELECT COUNT(*) FROM derived.fielder_season_metrics").fetchone() == (0,)
