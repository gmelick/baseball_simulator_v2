"""
SIM-537 — point-in-time pitcher and manager profiles.

Extends SIM-534's rule (a profile's data must never postdate the cutoff it was
built for) to the two groupings whose data is entirely our own — raw.pitches,
raw.play_events, raw.game_bullpen_availability — so, per the design doc
(docs/audit/2026-09-10-savant-point-in-time-data.md §6), no Savant fallback is
needed: a plain ``game_date <= cutoff`` filter is enough.

Most of these tests follow test_sim534_point_in_time.py's style: reading the
computor's actual source text and asserting the cutoff reaches every dated
source, rather than executing the SQL against a live DuckDB (which a fast
unit test does not have). A few execute the pure Python pieces directly.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import pipeline.batch.player_profile_computor as ppc

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migrations" / "duckdb" / "0027_sim537_pitcher_manager_asof.sql"
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
COMPUTOR = REPO / "pipeline" / "batch" / "player_profile_computor.py"
PITCHER_ENGINE = REPO / "similarity" / "engines" / "pitcher_similarity.py"
MANAGER_ENGINE = REPO / "similarity" / "engines" / "manager_similarity.py"


def _computor() -> ppc.PlayerProfileComputor:
    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    return c


# ---------------------------------------------------------------------------
# Pitcher: the cutoff reaches both dated sources
# ---------------------------------------------------------------------------


def test_pitcher_query_filters_raw_pitches_by_the_cutoff() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _compute_pitcher_profiles")
    end = src.index("def _assert_pitcher_profiles_have_no_leakage")
    body = src[start:end]
    assert "game_date <= DATE '{asof_sql}'" in body
    # Pass 1's pitch_stats CTE AND Pass 2's per-season GMM feature fetch.
    assert body.count("{date_cutoff}") == 2


def test_the_pickoff_outs_helper_takes_an_optional_cutoff() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _play_events_outs_cte")
    end = src.index("def _play_events_disengagement_cte")
    body = src[start:end]
    assert "asof_sql: str | None = None" in body
    assert "game_date <= DATE '{asof_sql}'" in body


def test_pitcher_profiles_pass_their_own_cutoff_to_the_pickoff_helper() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _compute_pitcher_profiles")
    end = src.index("def _assert_pitcher_profiles_have_no_leakage")
    body = src[start:end]
    assert "self._play_events_outs_cte(asof_sql if asof is not None else None)" in body


def test_pitcher_steal_metrics_still_calls_the_helper_with_no_cutoff() -> None:
    """SIM-537 must not change _build_pitcher_steal_metrics's behaviour — that
    grouping has not had its own point-in-time pass yet."""
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _build_pitcher_steal_metrics")
    end_candidates = [
        src.index("\n    def ", start + 1),
    ]
    end = min(e for e in end_candidates if e > start)
    body = src[start:end]
    assert "self._play_events_outs_cte()" in body


def test_the_pitcher_leakage_assertion_covers_both_sources() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _assert_pitcher_profiles_have_no_leakage")
    end = src.index("def _compute_batter_profiles")
    body = src[start:end]
    assert '"raw.pitches"' in body
    assert '"raw.play_events"' in body
    # The table-absent case must be a schema-availability skip, not a crash.
    assert "except Exception" in body


def test_a_pitcher_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _compute_pitcher_profiles")
    end = src.index("def _assert_pitcher_profiles_have_no_leakage")
    body = src[start:end]
    assert "DELETE FROM derived.pitcher_gmm_components" in body
    assert "DELETE FROM derived.pitcher_season_metrics" in body
    assert body.count("season > {asof_date.year}") >= 1


def test_every_pitcher_profile_ends_up_stamped() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _compute_pitcher_profiles")
    end = src.index("def _assert_pitcher_profiles_have_no_leakage")
    body = src[start:end]
    assert "SET asof_date = DATE '{asof_sql}'" in body
    assert "WHERE asof_date IS NULL" in body


def test_pitcher_asof_is_threaded_from_run() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    assert "self._compute_pitcher_profiles(seasons, asof=asof)" in src


# ---------------------------------------------------------------------------
# Pickoff-outs CTE: execute it directly against a stub connection
# ---------------------------------------------------------------------------


def test_pickoff_outs_cte_with_no_cutoff_omits_the_date_filter() -> None:
    c = _computor()
    c._conn.execute.return_value = None  # table "exists" (no exception raised)
    sql = c._play_events_outs_cte()
    assert "game_date" not in sql


def test_pickoff_outs_cte_with_a_cutoff_adds_the_date_filter() -> None:
    c = _computor()
    c._conn.execute.return_value = None
    sql = c._play_events_outs_cte("2024-04-15")
    assert "game_date <= DATE '2024-04-15'" in sql


def test_pickoff_outs_cte_falls_back_when_the_table_is_absent() -> None:
    c = _computor()
    c._conn.execute.side_effect = Exception("table not found")
    sql = c._play_events_outs_cte("2024-04-15")
    assert "WHERE FALSE" in sql
    assert "game_date" not in sql


# ---------------------------------------------------------------------------
# Manager: the cutoff reaches raw.pitches; the availability join needs no
# filter of its own (it can only match through the already-filtered staff CTE)
# ---------------------------------------------------------------------------


def test_manager_query_filters_raw_pitches_by_the_cutoff() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _compute_manager_profiles")
    end = src.index("def _assert_manager_profiles_have_no_leakage")
    body = src[start:end]
    # The `p` CTE and the `staff` CTE both read raw.pitches.
    assert body.count("{date_cutoff}") == 2


def test_a_manager_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _compute_manager_profiles")
    end = src.index("def _assert_manager_profiles_have_no_leakage")
    body = src[start:end]
    assert "DELETE FROM derived.manager_season_metrics" in body
    assert "season > {asof_date.year}" in body


def test_every_manager_profile_ends_up_stamped() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _compute_manager_profiles")
    end = src.index("def _assert_manager_profiles_have_no_leakage")
    body = src[start:end]
    assert "SET asof_date = DATE '{asof_sql}'" in body
    assert "WHERE asof_date IS NULL" in body


def test_manager_asof_is_threaded_from_run() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    assert "self._compute_manager_profiles(seasons, asof=asof)" in src


# ---------------------------------------------------------------------------
# Migration and schema version
# ---------------------------------------------------------------------------


def test_migration_0027_adds_only_the_two_stamps() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    added = re.findall(r"^ALTER TABLE (\S+) ADD COLUMN IF NOT EXISTS (\S+)", sql, re.M)
    assert added == [
        ("derived.pitcher_season_metrics", "asof_date"),
        ("derived.manager_season_metrics", "asof_date"),
    ]


def test_the_schema_version_was_bumped_to_27() -> None:
    assert VERSION_FILE.read_text(encoding="utf-8").strip() == "27"


def test_the_fresh_install_schema_carries_both_columns() -> None:
    schema = (REPO / "db" / "schemas" / "02_duckdb_schema.sql").read_text(encoding="utf-8")
    pitcher_start = schema.index("CREATE TABLE IF NOT EXISTS derived.pitcher_season_metrics")
    pitcher_end = schema.index(");", pitcher_start)
    assert "asof_date" in schema[pitcher_start:pitcher_end]

    manager_start = schema.index("CREATE TABLE IF NOT EXISTS derived.manager_season_metrics")
    manager_end = schema.index(");", manager_start)
    assert "asof_date" in schema[manager_start:manager_end]


# ---------------------------------------------------------------------------
# The engines refuse a mixed cutoff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine_file", [PITCHER_ENGINE, MANAGER_ENGINE])
def test_the_engine_refuses_a_mixed_cutoff_set(engine_file: Path) -> None:
    src = engine_file.read_text(encoding="utf-8")
    start = src.index("def _load_profiles")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    assert "asof_values: set = set()" in body
    assert "if len(asof_values) > 1:" in body
    assert "self._asof_date = next(iter(asof_values), None)" in body


@pytest.mark.parametrize("engine_file", [PITCHER_ENGINE, MANAGER_ENGINE])
def test_the_engine_optional_column_falls_back_gracefully(engine_file: Path) -> None:
    """A database that has not run migration 0027 yet must still build — the
    same graceful-optional pattern the batter engine already uses for xba/xslg."""
    src = engine_file.read_text(encoding="utf-8")
    start = src.index("def _load_profiles")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    assert "information_schema.columns" in body
    assert "NULL AS asof_date" in body


@pytest.mark.parametrize("engine_file", [PITCHER_ENGINE, MANAGER_ENGINE])
def test_the_engine_exposes_a_public_asof_date_property(engine_file: Path) -> None:
    src = engine_file.read_text(encoding="utf-8")
    assert "def asof_date(self):" in src
    assert "return self._asof_date" in src
