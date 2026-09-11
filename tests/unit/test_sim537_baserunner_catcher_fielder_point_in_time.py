"""
SIM-537 — point-in-time baserunner, catcher, and fielder profiles.

Extends SIM-534's rule (a profile's data must never postdate the cutoff it
was built for) to the last five profile tables: baserunner, baserunner
steal, pitcher steal, catcher, and fielder. Two of the sources these
groupings read — raw.pitches and raw.play_events — are our own data, so a
plain ``game_date <= cutoff`` filter is enough, the same as SIM-534/537's
earlier work.

Five more sources have no per-row date at all: raw.sprint_speed,
raw.savant_poptime, raw.savant_catcher_throwing, raw.savant_arm_strength,
and raw.savant_baserunning — only a season number. These follow the design
doc's alternative instead (docs/audit/2026-09-10-savant-point-in-time-data.md
§5): a season that ENDED before the cutoff joins its own row; the season
CONTAINING the cutoff joins the PRIOR season's row. One column,
of_arm_runs, correlates too weakly year-to-year (0.254) to substitute even
the prior season — it is left NULL for the cutoff season instead.

Most tests follow test_sim537_point_in_time.py's style: reading the
computor's actual source text and asserting the cutoff reaches every dated
source, rather than executing the SQL against a live DuckDB (which a fast
unit test does not have).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO / "db" / "migrations" / "duckdb" / "0028_sim537_baserunner_catcher_fielder_asof.sql"
)
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
COMPUTOR = REPO / "pipeline" / "batch" / "player_profile_computor.py"
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"

BASERUNNER_ENGINE = REPO / "similarity" / "engines" / "baserunner_similarity.py"
BASERUNNER_STEAL_ENGINE = REPO / "similarity" / "engines" / "baserunner_steal_similarity.py"
PITCHER_STEAL_ENGINE = REPO / "similarity" / "engines" / "pitcher_steal_similarity.py"
CATCHER_ENGINE = REPO / "similarity" / "engines" / "catcher_similarity.py"
FIELDER_ENGINE = REPO / "similarity" / "engines" / "fielder_similarity.py"

ALL_ENGINES = [
    BASERUNNER_ENGINE,
    BASERUNNER_STEAL_ENGINE,
    PITCHER_STEAL_ENGINE,
    CATCHER_ENGINE,
    FIELDER_ENGINE,
]


def _src() -> str:
    return COMPUTOR.read_text(encoding="utf-8")


def _method_body(src: str, name: str, next_marker: str) -> str:
    start = src.index(f"def {name}")
    end = src.index(next_marker, start)
    return src[start:end]


def _nowhitespace(text: str) -> str:
    """Strip all whitespace — for checking a call's exact argument list
    survives ANY line-wrap style ruff might choose."""
    return re.sub(r"\s+", "", text)


# ---------------------------------------------------------------------------
# Baserunner: the cutoff reaches raw.pitches; the sprint speed join has no
# date of its own, so it is made safe by a season-shift substitution.
# ---------------------------------------------------------------------------


def test_baserunner_profiles_filter_raw_pitches_by_the_cutoff() -> None:
    body = _method_body(
        _src(), "_compute_baserunner_profiles", "def _assert_baserunner_profiles_have_no_leakage"
    )
    assert "game_date <= DATE '{asof_sql}'" in body
    assert body.count("{date_cutoff}") == 1


def test_baserunner_profiles_season_shift_the_sprint_speed_join() -> None:
    body = _method_body(
        _src(), "_compute_baserunner_profiles", "def _assert_baserunner_profiles_have_no_leakage"
    )
    assert "CASE WHEN ap.season = {asof_date.year} THEN ap.season - 1 ELSE ap.season END" in body
    assert 'else "ap.season"' in body
    assert "ss.season = {sprint_speed_season}" in body


def test_a_baserunner_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    body = _method_body(
        _src(), "_compute_baserunner_profiles", "def _assert_baserunner_profiles_have_no_leakage"
    )
    assert "DELETE FROM derived.baserunner_season_metrics" in body
    assert "season > {asof_date.year}" in body


def test_every_baserunner_profile_ends_up_stamped() -> None:
    body = _method_body(
        _src(), "_compute_baserunner_profiles", "def _assert_baserunner_profiles_have_no_leakage"
    )
    assert "AS asof_date" in body
    assert "SET asof_date = DATE '{asof_sql}'" in body
    assert "WHERE asof_date IS NULL" in body


def test_the_baserunner_leakage_assertion_covers_raw_pitches() -> None:
    src = _src()
    start = src.index("def _assert_baserunner_profiles_have_no_leakage")
    body = src[start : start + 1200]
    assert "raw.pitches" in body
    assert "game_date <=" in body


def test_baserunner_asof_is_threaded_from_run() -> None:
    assert "self._compute_baserunner_profiles(seasons,asof=asof)" in _nowhitespace(_src())


# ---------------------------------------------------------------------------
# Baserunner steal: single dated source, explicit INSERT column list.
# ---------------------------------------------------------------------------


def test_baserunner_steal_metrics_filter_raw_pitches_by_the_cutoff() -> None:
    body = _method_body(
        _src(),
        "_build_baserunner_steal_metrics",
        "def _assert_baserunner_steal_profiles_have_no_leakage",
    )
    assert "game_date <= DATE '{asof_sql}'" in body
    assert body.count("{date_cutoff}") == 1


def test_a_baserunner_steal_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    body = _method_body(
        _src(),
        "_build_baserunner_steal_metrics",
        "def _assert_baserunner_steal_profiles_have_no_leakage",
    )
    assert "DELETE FROM derived.baserunner_steal_metrics" in body
    assert "season > {asof_date.year}" in body


def test_every_baserunner_steal_profile_ends_up_stamped() -> None:
    body = _method_body(
        _src(),
        "_build_baserunner_steal_metrics",
        "def _assert_baserunner_steal_profiles_have_no_leakage",
    )
    # asof_date is named in the explicit INSERT column list, not appended
    # positionally, so this checks the column list carries it.
    assert re.search(r"steal_success_rate_2b, below_minimum_sample,\s*\n\s*asof_date", body)
    assert "SET asof_date = DATE '{asof_sql}'" in body


def test_baserunner_steal_asof_is_threaded_from_run() -> None:
    assert "self._build_baserunner_steal_metrics(seasons,asof=asof)" in _nowhitespace(_src())


# ---------------------------------------------------------------------------
# Pitcher steal: two dated sources — raw.pitches, and raw.play_events via
# both CTE helpers, which now both accept a cutoff.
# ---------------------------------------------------------------------------


def test_pitcher_steal_metrics_filter_raw_pitches_by_the_cutoff() -> None:
    body = _method_body(
        _src(), "_build_pitcher_steal_metrics", "def _assert_pitcher_steal_profiles_have_no_leakage"
    )
    assert "game_date <= DATE '{asof_sql}'" in body
    assert body.count("{date_cutoff}") == 1


def test_pitcher_steal_metrics_threads_its_cutoff_to_both_cte_helpers() -> None:
    body = _nowhitespace(
        _method_body(
            _src(),
            "_build_pitcher_steal_metrics",
            "def _assert_pitcher_steal_profiles_have_no_leakage",
        )
    )
    assert "self._play_events_outs_cte(asof_sqlifasofisnotNoneelseNone)" in body
    assert "self._play_events_disengagement_cte(" in body
    assert body.count("asof_sqlifasofisnotNoneelseNone") == 2


def test_the_disengagement_helper_takes_an_optional_cutoff() -> None:
    src = _src()
    start = src.index("def _play_events_disengagement_cte")
    end = src.index("def _pickoff_outcomes_cte")
    body = src[start:end]
    assert "asof_sql: str | None = None" in body
    assert "game_date <= DATE '{asof_sql}'" in body


def test_disengagement_cte_with_no_cutoff_omits_the_date_filter() -> None:
    from unittest.mock import MagicMock

    import pipeline.batch.player_profile_computor as ppc

    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    c._conn.execute.return_value = None
    sql = c._play_events_disengagement_cte()
    assert "game_date" not in sql


def test_disengagement_cte_with_a_cutoff_adds_the_date_filter() -> None:
    from unittest.mock import MagicMock

    import pipeline.batch.player_profile_computor as ppc

    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    c._conn.execute.return_value = None
    sql = c._play_events_disengagement_cte("2024-04-15")
    assert "game_date <= DATE '2024-04-15'" in sql


def test_disengagement_cte_falls_back_when_the_table_is_absent() -> None:
    from unittest.mock import MagicMock

    import pipeline.batch.player_profile_computor as ppc

    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    c._conn.execute.side_effect = Exception("table not found")
    sql = c._play_events_disengagement_cte("2024-04-15")
    assert "WHERE FALSE" in sql
    assert "game_date" not in sql


def test_a_pitcher_steal_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    body = _method_body(
        _src(), "_build_pitcher_steal_metrics", "def _assert_pitcher_steal_profiles_have_no_leakage"
    )
    assert "DELETE FROM derived.pitcher_steal_metrics" in body
    assert "season > {asof_date.year}" in body


def test_the_pitcher_steal_leakage_assertion_covers_both_sources() -> None:
    src = _src()
    start = src.index("def _assert_pitcher_steal_profiles_have_no_leakage")
    end = src.index("def _compute_bullpen_workload")
    body = src[start:end]
    assert '"raw.pitches"' in body
    assert '"raw.play_events"' in body
    assert "except Exception" in body


def test_pitcher_steal_asof_is_threaded_from_run() -> None:
    assert "self._build_pitcher_steal_metrics(seasons, asof=asof)" in _src()


# ---------------------------------------------------------------------------
# Catcher: framing/blocking/throwing/uncaught-K3 read only raw.pitches; the
# aggregator's two Savant joins (pop time, arm strength) have no date column.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,next_marker,expected_cutoffs",
    [
        ("_compute_catcher_framing", "def _compute_catcher_blocking", 1),
        ("_compute_catcher_blocking", "def _compute_catcher_throwing", 1),
        ("_compute_catcher_throwing", "def _compute_outfield_catch_probability", 2),
        ("_compute_catcher_uncaught_k3", "def _assert_catcher_profiles_have_no_leakage", 1),
    ],
)
def test_catcher_methods_filter_raw_pitches_by_the_cutoff(
    method: str, next_marker: str, expected_cutoffs: int
) -> None:
    body = _method_body(_src(), method, next_marker)
    assert "game_date <= DATE" in body
    assert body.count("{date_cutoff}") == expected_cutoffs


def test_a_catcher_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    body = _method_body(
        _src(), "_aggregate_catcher_season_metrics", "def _compute_catcher_uncaught_k3"
    )
    assert "DELETE FROM derived.catcher_season_metrics" in body
    assert "season > {asof_date.year}" in body


def test_catcher_aggregator_season_shifts_the_two_savant_joins() -> None:
    body = _method_body(
        _src(), "_aggregate_catcher_season_metrics", "def _compute_catcher_uncaught_k3"
    )
    assert "CASE WHEN {catcher_season_col} = {asof_date.year}" in body
    assert "spt.season    = {savant_season}" in body
    assert "sct.season    = {savant_season}" in body


def test_every_catcher_profile_ends_up_stamped() -> None:
    body = _method_body(
        _src(), "_aggregate_catcher_season_metrics", "def _compute_catcher_uncaught_k3"
    )
    assert "below_minimum_sample, updated_at, asof_date" in body
    assert "SET asof_date = DATE '{asof_sql}'" in body


def test_the_catcher_leakage_assertion_covers_raw_pitches() -> None:
    src = _src()
    start = src.index("def _assert_catcher_profiles_have_no_leakage")
    body = src[start : start + 1200]
    assert "raw.pitches" in body
    assert "game_date <=" in body


def test_catcher_asof_is_threaded_from_run() -> None:
    src = _src()
    for call in [
        "self._compute_catcher_framing(seasons, asof=asof)",
        "self._compute_catcher_blocking(seasons, asof=asof)",
        "self._compute_catcher_throwing(seasons, asof=asof)",
        "self._aggregate_catcher_season_metrics(seasons, asof=asof)",
        "self._compute_catcher_uncaught_k3(seasons, asof=asof)",
    ]:
        assert call in src, call


# ---------------------------------------------------------------------------
# Fielder: seven per-play methods read only raw.pitches (two of them —
# infield OAA and DP — also do a Python-side sprint-speed lookup); the
# aggregator's three Savant joins have no date column, and of_arm_runs gets
# a stricter NULL-out because its year-to-year correlation is too weak
# (0.254) to substitute even the prior season.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,next_marker",
    [
        ("_compute_outfield_catch_probability", "def _compute_outfield_arm_metrics"),
        ("_compute_outfield_arm_metrics", "def _compute_infield_oaa"),
        ("_compute_infield_oaa", "def _compute_dp_metrics"),
        ("_compute_dp_metrics", "def _compute_bunt_defense"),
        ("_compute_bunt_defense", "def _compute_first_base_scooping"),
        ("_compute_first_base_scooping", "def _compute_error_decomposition"),
        ("_compute_error_decomposition", "def _aggregate_fielder_season_metrics"),
    ],
)
def test_fielder_per_play_methods_filter_raw_pitches_by_the_cutoff(
    method: str, next_marker: str
) -> None:
    body = _method_body(_src(), method, next_marker)
    assert "game_date <= DATE" in body
    assert body.count("{date_cutoff}") == 1


def test_infield_oaa_season_shifts_its_sprint_speed_lookup() -> None:
    body = _method_body(_src(), "_compute_infield_oaa", "def _compute_dp_metrics")
    assert "def _speed_lookup_season(row_season: int) -> int:" in body
    assert "return asof_date.year - 1" in body
    assert "_speed_lookup_season(int(r[" in body


def test_dp_metrics_season_shifts_its_sprint_speed_join() -> None:
    body = _method_body(_src(), "_compute_dp_metrics", "def _compute_bunt_defense")
    assert "CASE WHEN bsm.season = {asof_date.year} THEN bsm.season - 1 ELSE bsm.season END" in body
    assert "ss.season = {sprint_speed_season}" in body


def test_a_fielder_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    body = _method_body(
        _src(), "_aggregate_fielder_season_metrics", "def _assert_fielder_profiles_have_no_leakage"
    )
    assert "DELETE FROM derived.fielder_season_metrics" in body
    assert "season > {asof_date.year}" in body


def test_fielder_aggregator_season_shifts_all_three_savant_joins() -> None:
    body = _method_body(
        _src(), "_aggregate_fielder_season_metrics", "def _assert_fielder_profiles_have_no_leakage"
    )
    assert "CASE WHEN c.season = {asof_date.year} THEN c.season - 1 ELSE c.season END" in body
    assert body.count("season = {savant_season}") == 3


def test_of_arm_runs_is_nulled_for_the_season_containing_the_cutoff() -> None:
    body = _method_body(
        _src(), "_aggregate_fielder_season_metrics", "def _assert_fielder_profiles_have_no_leakage"
    )
    assert "of_arm_runs_in_cutoff_season" in body
    assert 'f"c.season = {asof_date.year}" if asof is not None else "FALSE"' in body
    assert "AND NOT ({of_arm_runs_in_cutoff_season})" in body


def test_every_fielder_profile_ends_up_stamped_last() -> None:
    """asof_date must be the LAST column — the INSERT carries no column
    list, same trap sprint_speed already documents."""
    body = _method_body(
        _src(), "_aggregate_fielder_season_metrics", "def _assert_fielder_profiles_have_no_leakage"
    )
    sprint_idx = body.index("ss.sprint_speed AS sprint_speed")
    asof_idx = body.index("DATE '{asof_sql}' AS asof_date")
    assert asof_idx > sprint_idx
    assert "SET asof_date = DATE '{asof_sql}'" in body


def test_the_fielder_leakage_assertion_covers_raw_pitches() -> None:
    src = _src()
    start = src.index("def _assert_fielder_profiles_have_no_leakage")
    body = src[start : start + 1200]
    assert "raw.pitches" in body
    assert "game_date <=" in body


def test_fielder_asof_is_threaded_from_run() -> None:
    src = _src()
    for call in [
        "self._compute_outfield_catch_probability(seasons, asof=asof)",
        "self._compute_outfield_arm_metrics(seasons, asof=asof)",
        "self._compute_infield_oaa(seasons, asof=asof)",
        "self._compute_dp_metrics(seasons, asof=asof)",
        "self._compute_bunt_defense(seasons, asof=asof)",
        "self._compute_first_base_scooping(seasons, asof=asof)",
        "self._compute_error_decomposition(seasons, asof=asof)",
        "self._aggregate_fielder_season_metrics(seasons, asof=asof)",
    ]:
        assert call in src, call


# ---------------------------------------------------------------------------
# The run-expectancy matrix: feeds dp_run_value inside _compute_dp_metrics,
# so it needs the same cutoff even though no engine reads it directly.
# ---------------------------------------------------------------------------


def test_run_expectancy_matrix_takes_an_optional_cutoff() -> None:
    src = _src()
    start = src.index("def build_run_expectancy_matrix")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    assert "asof: date | None = None" in body
    assert body.count("{date_cutoff}") == 2


def test_run_expectancy_matrix_skips_persisting_a_cutoff_build() -> None:
    src = _src()
    start = src.index("def build_run_expectancy_matrix")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    assert "if asof is not None:" in body
    assert "not persisted" in body


def test_run_expectancy_matrix_asof_is_threaded_from_run() -> None:
    assert "build_run_expectancy_matrix(self._conn, seasons, asof=asof)" in _src()


# ---------------------------------------------------------------------------
# Migration and schema version
# ---------------------------------------------------------------------------


def test_migration_0028_adds_only_the_five_stamps() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    added = re.findall(r"^ALTER TABLE (\S+) ADD COLUMN IF NOT EXISTS (\S+)", sql, re.M)
    assert added == [
        ("derived.baserunner_season_metrics", "asof_date"),
        ("derived.baserunner_steal_metrics", "asof_date"),
        ("derived.pitcher_steal_metrics", "asof_date"),
        ("derived.catcher_season_metrics", "asof_date"),
        ("derived.fielder_season_metrics", "asof_date"),
    ]


def test_the_schema_version_was_bumped_to_28() -> None:
    assert VERSION_FILE.read_text(encoding="utf-8").strip() == "28"


@pytest.mark.parametrize(
    "table",
    [
        "derived.baserunner_season_metrics",
        "derived.baserunner_steal_metrics",
        "derived.pitcher_steal_metrics",
        "derived.catcher_season_metrics",
        "derived.fielder_season_metrics",
    ],
)
def test_the_fresh_install_schema_carries_asof_date(table: str) -> None:
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    start = schema.index(f"CREATE TABLE IF NOT EXISTS {table}")
    # A plain `schema.index(");", start)` can land inside a column comment
    # that happens to contain the literal text "...);" mid-sentence (the
    # catcher table's uncaught-K3 comment does). The real closing paren is
    # always on its own line, so anchor on that instead.
    end = re.search(r"\n\);", schema[start:]).start() + start
    assert "asof_date" in schema[start:end]


def test_run_expectancy_matrix_gets_no_new_column() -> None:
    """It is keyed by season_range, not season — see the migration's own
    rationale for why a cutoff build skips persisting to it instead."""
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    start = schema.index("CREATE TABLE IF NOT EXISTS derived.run_expectancy_matrix")
    end = re.search(r"\n\);", schema[start:]).start() + start
    assert "asof_date" not in schema[start:end]


# ---------------------------------------------------------------------------
# The five engines refuse a mixed cutoff and expose the resolved one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine_file", ALL_ENGINES)
def test_the_engine_refuses_a_mixed_cutoff_set(engine_file: Path) -> None:
    src = engine_file.read_text(encoding="utf-8")
    start = src.index("def _load_profiles")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    assert "asof_values: set = set()" in body
    assert "if len(asof_values) > 1:" in body
    assert "self._asof_date = next(iter(asof_values), None)" in body


@pytest.mark.parametrize("engine_file", ALL_ENGINES)
def test_the_engine_optional_column_falls_back_gracefully(engine_file: Path) -> None:
    """A database that has not run migration 0028 yet must still build."""
    src = engine_file.read_text(encoding="utf-8")
    start = src.index("def _load_profiles")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    assert "information_schema.columns" in body
    assert "NULL AS asof_date" in body


@pytest.mark.parametrize("engine_file", ALL_ENGINES)
def test_the_engine_exposes_a_public_asof_date_property(engine_file: Path) -> None:
    src = engine_file.read_text(encoding="utf-8")
    assert "def asof_date(self):" in src
    assert "return self._asof_date" in src


@pytest.mark.parametrize("engine_file", ALL_ENGINES)
def test_the_engine_initializes_asof_date_in_init(engine_file: Path) -> None:
    src = engine_file.read_text(encoding="utf-8")
    start = src.index("def __init__(self, duckdb_path: str)")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    assert "self._asof_date = None" in body
