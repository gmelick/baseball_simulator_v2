"""
SIM-537 — point-in-time pitcher and manager profiles.

Extends SIM-534's rule (a profile's data must never postdate the cutoff it was
built for) to the two groupings whose data is entirely our own — raw.pitches,
raw.play_events, raw.game_bullpen_availability — so, per the design doc
(docs/audit/2026-09-10-savant-point-in-time-data.md §6), no Savant fallback is
needed: a plain ``game_date <= cutoff`` filter is enough.

Most of these tests follow test_point_in_time_batter.py's style: reading the
computor's actual source text and asserting the cutoff reaches every dated
source, rather than executing the SQL against a live DuckDB (which a fast
unit test does not have). A few execute the pure Python pieces directly.
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import profiles.build_profiles as ppc
from profiles.build_profiles import PlayerProfileComputor

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "db" / "migrations" / "duckdb" / "0027_sim537_pitcher_manager_asof.sql"
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
COMPUTOR = REPO / "profiles" / "build_profiles.py"

PITCHER_ENGINE = REPO / "similarity" / "engines" / "pitcher_similarity.py"
MANAGER_ENGINE = REPO / "similarity" / "engines" / "manager_similarity.py"


def _method_source(name: str) -> str:
    """One computor method's source, from its ``def`` line (the method may live
    in a mixin of ``profiles/``)."""
    src = inspect.getsource(getattr(PlayerProfileComputor, name))
    return src[src.index(f"def {name}") :]


def _computor_source() -> str:
    """The computor's whole source: the main module and its ``profiles`` package."""
    paths = [
        *sorted(COMPUTOR.parent.glob("*.py")),
        *sorted((COMPUTOR.parent.parent / "pools").glob("*.py")),
    ]
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


def _computor() -> ppc.PlayerProfileComputor:
    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    return c


# ---------------------------------------------------------------------------
# Pitcher: the cutoff reaches both dated sources
# ---------------------------------------------------------------------------


def test_pitcher_query_filters_raw_pitches_by_the_cutoff() -> None:
    body = _method_source("_compute_pitcher_profiles")
    assert "game_date <= DATE '{asof_sql}'" in body
    # Pass 1's pitch_stats CTE AND Pass 2's per-season GMM feature fetch.
    assert body.count("{date_cutoff}") == 2


def test_the_pickoff_outs_helper_takes_an_optional_cutoff() -> None:
    body = _method_source("_play_events_outs_cte")
    assert "asof_sql: str | None = None" in body
    assert "game_date <= DATE '{asof_sql}'" in body


def test_pitcher_profiles_pass_their_own_cutoff_to_the_pickoff_helper() -> None:
    body = _method_source("_compute_pitcher_profiles")
    assert "self._play_events_outs_cte(asof_sql if asof is not None else None)" in body


def test_pitcher_steal_metrics_now_passes_its_own_cutoff_to_the_helper() -> None:
    """The baserunner/catcher/fielder follow-on (still tracked as SIM-537)
    gave _build_pitcher_steal_metrics its own point-in-time pass, so it now
    threads its own cutoff into the pickoff-outs helper instead of calling
    it with none."""
    body = re.sub(r"\s+", "", _method_source("_build_pitcher_steal_metrics"))
    assert "self._play_events_outs_cte(asof_sqlifasofisnotNoneelseNone)" in body


def _pitcher_leakage_checks(c: ppc.PlayerProfileComputor) -> list[tuple[str, str]]:
    """The (source, query) pairs the pitcher leakage assertion checks."""
    c._check_leakage = MagicMock()
    c._assert_pitcher_profiles_have_no_leakage([2024], date(2024, 4, 15))
    return [(call.args[0], call.args[1]) for call in c._check_leakage.call_args_list]


def test_the_pitcher_leakage_assertion_covers_both_sources() -> None:
    checks = _pitcher_leakage_checks(_computor())
    assert [source for source, _ in checks] == ["raw.pitches", "raw.play_events"]
    for source, sql in checks:
        assert sql == (
            f"SELECT MAX(game_date) FROM pg.{source} "
            "WHERE season IN (2024) AND game_date <= DATE '2024-04-15'"
        )


def test_the_pitcher_leakage_assertion_skips_an_absent_play_events_table() -> None:
    """The table-absent case is a schema-availability skip, not a crash: the
    shared probe answers "unreadable" instead of raising."""
    c = _computor()
    c._conn.execute.side_effect = Exception("table not found")
    assert [source for source, _ in _pitcher_leakage_checks(c)] == ["raw.pitches"]


def _pitcher_deletes(asof: date | None) -> list[str]:
    """The DELETE statements one pitcher build sends for the 2024 season."""
    c = _computor()
    c._compute_pitcher_profiles([2024], asof=asof)
    sqls = [call.args[0] for call in c._conn.execute.call_args_list]
    return [s for s in sqls if s.startswith("DELETE FROM derived.")]


def test_a_pitcher_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    """The child table first (the foreign key), then the profile table, each
    with every season after the cutoff's."""
    assert _pitcher_deletes(date(2024, 4, 15)) == [
        "DELETE FROM derived.pitcher_gmm_components WHERE season IN (2024) OR season > 2024",
        "DELETE FROM derived.pitcher_season_metrics WHERE season IN (2024) OR season > 2024",
    ]


def test_a_live_pitcher_build_deletes_only_the_mixture_components() -> None:
    """INSERT OR REPLACE replaces the profile rows; the child rows go first."""
    assert _pitcher_deletes(None) == [
        "DELETE FROM derived.pitcher_gmm_components WHERE season IN (2024)"
    ]


def test_every_pitcher_profile_ends_up_stamped() -> None:
    body = _method_source("_compute_pitcher_profiles")
    # SIM-551: the whole-table stamp through the shared helper, guarded by
    # the pre-write check against a mixed cutoff.
    assert (
        'self._refuse_a_mixed_cutoff("derived.pitcher_season_metrics", seasons, asof_date)' in body
    )
    assert 'self._stamp_one_cutoff("derived.pitcher_season_metrics", asof_sql)' in body


def test_pitcher_asof_is_threaded_from_run() -> None:
    src = _computor_source()
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
    body = _method_source("_compute_manager_profiles")
    # The `p` CTE and the `staff` CTE both read raw.pitches.
    assert body.count("{date_cutoff}") == 2


def _manager_deletes(asof: date | None) -> list[str]:
    """The DELETE statements one manager build sends for the 2024 season."""
    c = _computor()
    c._compute_manager_profiles([2024], asof=asof)
    sqls = [call.args[0] for call in c._conn.execute.call_args_list]
    return [s for s in sqls if s.startswith("DELETE FROM derived.")]


def test_a_manager_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    assert _manager_deletes(date(2024, 4, 15)) == [
        "DELETE FROM derived.manager_season_metrics WHERE season IN (2024) OR season > 2024"
    ]


def test_a_live_manager_build_deletes_nothing() -> None:
    """INSERT OR REPLACE replaces the live build's rows."""
    assert _manager_deletes(None) == []


def test_the_manager_leakage_assertion_checks_raw_pitches() -> None:
    """Through the shared check, so a query that cannot run is logged and
    skipped, the rule of every other profile."""
    c = _computor()
    c._check_leakage = MagicMock()
    c._assert_manager_profiles_have_no_leakage([2024], date(2024, 4, 15))
    assert [call.args[:2] for call in c._check_leakage.call_args_list] == [
        (
            "raw.pitches",
            "SELECT MAX(game_date) FROM pg.raw.pitches "
            "WHERE season IN (2024) AND game_date <= DATE '2024-04-15'",
        )
    ]


def test_every_manager_profile_ends_up_stamped() -> None:
    body = _method_source("_compute_manager_profiles")
    assert (
        'self._refuse_a_mixed_cutoff("derived.manager_season_metrics", seasons, asof_date)' in body
    )
    assert 'self._stamp_one_cutoff("derived.manager_season_metrics", asof_sql)' in body


def test_manager_asof_is_threaded_from_run() -> None:
    src = _computor_source()
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


def test_the_schema_version_was_bumped_to_27_or_later() -> None:
    # >= rather than == 27: this proves migration 0027 landed, not that no
    # later migration may bump the version further (the baserunner/catcher/
    # fielder follow-on's migration 0028 already has, same reasoning as
    # test_point_in_time_batter.py's version test).
    assert int(VERSION_FILE.read_text(encoding="utf-8").strip()) >= 27


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
    assert "duck_columns(" in body  # the shared existence probe (db/introspect.py)
    assert "NULL AS asof_date" in body


@pytest.mark.parametrize("engine_file", [PITCHER_ENGINE, MANAGER_ENGINE])
def test_the_engine_exposes_a_public_asof_date_property(engine_file: Path) -> None:
    src = engine_file.read_text(encoding="utf-8")
    assert "def asof_date(self):" in src
    assert "return self._asof_date" in src
