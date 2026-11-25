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
CONTAINING the cutoff joins the PRIOR season's row.

The outfield arm block no longer reads a Savant board (SIM-550): it is
filled after the pools from sim.advancement_opportunity_pool, our own dated
table, so the fill takes the plain ``game_date <= cutoff`` filter and the
run-value guard the board needed is gone.

Most tests follow test_point_in_time_pitcher_manager.py's style: reading the
computor's actual source text and asserting the cutoff reaches every dated
source, rather than executing the SQL against a live DuckDB (which a fast
unit test does not have). Where a shared helper renders the SQL (the cutoff
delete, the Savant season shift, the leakage queries), the test reads the
statements a method runs against a recording stub connection instead.
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path

import pytest

from profiles.build_profiles import PlayerProfileComputor
from profiles.leverage import build_run_expectancy_matrix

REPO = Path(__file__).resolve().parents[3]
MIGRATION = (
    REPO / "db" / "migrations" / "duckdb" / "0028_sim537_baserunner_catcher_fielder_asof.sql"
)
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
COMPUTOR = REPO / "profiles" / "build_profiles.py"
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


def _computor_source() -> str:
    """The computor's whole source: the main module and its ``profiles`` package."""
    paths = [
        *sorted(COMPUTOR.parent.glob("*.py")),
        *sorted((COMPUTOR.parent.parent / "pools").glob("*.py")),
    ]
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


def _method_source(name: str) -> str:
    """One computor method's source, from its ``def`` line (the method may live
    in a mixin of ``profiles/``)."""
    src = inspect.getsource(getattr(PlayerProfileComputor, name))
    return src[src.index(f"def {name}") :]


def _nowhitespace(text: str) -> str:
    """Strip all whitespace — for checking a call's exact argument list
    survives ANY line-wrap style ruff might choose."""
    return re.sub(r"\s+", "", text)


CUTOFF = date(2024, 6, 15)
#: The raw.pitches leakage query of a 2024 build as of CUTOFF.
PITCHES_LEAK_SQL = (
    "SELECT MAX(game_date) FROM pg.raw.pitches "
    "WHERE season IN (2024) AND game_date <= DATE '2024-06-15'"
)


class _Recorder:
    """A stub DuckDB connection: it records every statement and returns no rows."""

    def __init__(self, play_events: bool = True) -> None:
        self.sql: list[str] = []
        self._play_events = play_events

    def execute(self, sql: str, params: object = None) -> _Recorder:
        self.sql.append(sql)
        if not self._play_events and "play_events" in sql and "LIMIT 0" in sql:
            raise RuntimeError("raw.play_events is absent")
        return self

    def fetchone(self) -> tuple:
        return (None,)

    def fetchall(self) -> list:
        return []


def _rendered(method: str, *args, play_events: bool = True, **kwargs) -> list[str]:
    """Every statement ``method`` runs, each with its whitespace collapsed."""
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = _Recorder(play_events)
    getattr(comp, method)(*args, **kwargs)
    return [" ".join(sql.split()) for sql in comp._conn.sql]


def _insert(statements: list[str]) -> str:
    """The one INSERT among ``statements``."""
    (insert,) = [sql for sql in statements if sql.startswith("INSERT")]
    return insert


def _shifted(col: str) -> str:
    """The season-shift CASE of a 2024 cutoff build, as rendered."""
    return f"(CASE WHEN {col} = 2024 THEN {col} - 1 ELSE {col} END)"


def test_the_savant_season_helper_shifts_only_a_cutoff_build() -> None:
    shift = PlayerProfileComputor._savant_season_sql
    assert shift("bsm.season", None) == "bsm.season"
    assert shift("bsm.season", CUTOFF) == _shifted("bsm.season")


# ---------------------------------------------------------------------------
# Baserunner: the cutoff reaches raw.pitches; the sprint speed join has no
# date of its own, so it is made safe by a season-shift substitution.
# ---------------------------------------------------------------------------


def test_baserunner_profiles_filter_raw_pitches_by_the_cutoff() -> None:
    body = _method_source("_compute_baserunner_profiles")
    assert "game_date <= DATE '{asof_sql}'" in body
    assert body.count("{date_cutoff}") == 1


def test_baserunner_profiles_season_shift_the_sprint_speed_join() -> None:
    cut = _insert(_rendered("_compute_baserunner_profiles", [2024], asof=CUTOFF))
    assert f"ss.season = {_shifted('ap.season')}" in cut
    live = _insert(_rendered("_compute_baserunner_profiles", [2024], asof=None))
    assert "ss.season = ap.season " in live


def test_a_baserunner_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    cut = _rendered("_compute_baserunner_profiles", [2024], asof=CUTOFF)
    assert (
        "DELETE FROM derived.baserunner_season_metrics WHERE season IN (2024) OR season > 2024"
        in cut
    )
    live = _rendered("_compute_baserunner_profiles", [2024], asof=None)
    assert not any(sql.startswith("DELETE") for sql in live)


def test_every_baserunner_profile_ends_up_stamped() -> None:
    body = _method_source("_compute_baserunner_profiles")
    assert "AS asof_date" in body
    # SIM-551: the whole-table stamp through the shared helper, guarded by
    # the pre-write check against a mixed cutoff.
    assert (
        'self._refuse_a_mixed_cutoff("derived.baserunner_season_metrics", seasons, asof_date)'
        in body
    )
    assert 'self._stamp_one_cutoff("derived.baserunner_season_metrics", asof_sql)' in body


def test_the_baserunner_leakage_assertion_covers_raw_pitches() -> None:
    statements = _rendered("_assert_baserunner_profiles_have_no_leakage", [2024], CUTOFF)
    assert statements == [PITCHES_LEAK_SQL]


def test_baserunner_asof_is_threaded_from_run() -> None:
    assert "self._compute_baserunner_profiles(seasons,asof=asof)" in _nowhitespace(
        _computor_source()
    )


# ---------------------------------------------------------------------------
# Baserunner steal: single dated source, explicit INSERT column list.
# ---------------------------------------------------------------------------


def test_baserunner_steal_metrics_filter_raw_pitches_by_the_cutoff() -> None:
    body = _method_source("_build_baserunner_steal_metrics")
    assert "game_date <= DATE '{asof_sql}'" in body
    assert body.count("{date_cutoff}") == 1


def test_a_baserunner_steal_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    cut = _rendered("_build_baserunner_steal_metrics", [2024], asof=CUTOFF)
    assert (
        "DELETE FROM derived.baserunner_steal_metrics WHERE season IN (2024) OR season > 2024"
        in cut
    )
    live = _rendered("_build_baserunner_steal_metrics", [2024], asof=None)
    assert not any(sql.startswith("DELETE") for sql in live)


def test_every_baserunner_steal_profile_ends_up_stamped() -> None:
    body = _method_source("_build_baserunner_steal_metrics")
    # asof_date is named in the explicit INSERT column list, not appended
    # positionally, so this checks the column list carries it.
    assert re.search(r"steal_success_rate_2b, below_minimum_sample,\s*\n\s*asof_date", body)
    assert (
        'self._refuse_a_mixed_cutoff("derived.baserunner_steal_metrics", seasons, asof_date)'
        in body
    )
    assert 'self._stamp_one_cutoff("derived.baserunner_steal_metrics", asof_sql)' in body


def test_baserunner_steal_asof_is_threaded_from_run() -> None:
    assert "self._build_baserunner_steal_metrics(seasons,asof=asof)" in _nowhitespace(
        _computor_source()
    )


# ---------------------------------------------------------------------------
# Pitcher steal: two dated sources — raw.pitches, and raw.play_events via
# both CTE helpers, which now both accept a cutoff.
# ---------------------------------------------------------------------------


def test_pitcher_steal_metrics_filter_raw_pitches_by_the_cutoff() -> None:
    body = _method_source("_build_pitcher_steal_metrics")
    assert "game_date <= DATE '{asof_sql}'" in body
    assert body.count("{date_cutoff}") == 1


def test_pitcher_steal_metrics_threads_its_cutoff_to_both_cte_helpers() -> None:
    body = _nowhitespace(_method_source("_build_pitcher_steal_metrics"))
    assert "self._play_events_outs_cte(asof_sqlifasofisnotNoneelseNone)" in body
    assert "self._play_events_disengagement_cte(" in body
    assert body.count("asof_sqlifasofisnotNoneelseNone") == 2


def test_the_disengagement_helper_takes_an_optional_cutoff() -> None:
    body = _method_source("_play_events_disengagement_cte")
    assert "asof_sql: str | None = None" in body
    assert "game_date <= DATE '{asof_sql}'" in body


def test_disengagement_cte_with_no_cutoff_omits_the_date_filter() -> None:
    from unittest.mock import MagicMock

    import profiles.build_profiles as ppc

    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    c._conn.execute.return_value = None
    sql = c._play_events_disengagement_cte()
    assert "game_date" not in sql


def test_disengagement_cte_with_a_cutoff_adds_the_date_filter() -> None:
    from unittest.mock import MagicMock

    import profiles.build_profiles as ppc

    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    c._conn.execute.return_value = None
    sql = c._play_events_disengagement_cte("2024-04-15")
    assert "game_date <= DATE '2024-04-15'" in sql


def test_disengagement_cte_falls_back_when_the_table_is_absent() -> None:
    from unittest.mock import MagicMock

    import profiles.build_profiles as ppc

    c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
    c._conn = MagicMock()
    c._conn.execute.side_effect = Exception("table not found")
    sql = c._play_events_disengagement_cte("2024-04-15")
    assert "WHERE FALSE" in sql
    assert "game_date" not in sql


def test_a_pitcher_steal_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    cut = _rendered("_build_pitcher_steal_metrics", [2024], asof=CUTOFF)
    assert (
        "DELETE FROM derived.pitcher_steal_metrics WHERE season IN (2024) OR season > 2024" in cut
    )
    live = _rendered("_build_pitcher_steal_metrics", [2024], asof=None)
    assert not any(sql.startswith("DELETE") for sql in live)


def test_the_pitcher_steal_leakage_assertion_covers_both_sources() -> None:
    play_events_sql = PITCHES_LEAK_SQL.replace("pg.raw.pitches", "pg.raw.play_events")
    statements = _rendered("_assert_pitcher_steal_profiles_have_no_leakage", [2024], CUTOFF)
    assert PITCHES_LEAK_SQL in statements
    assert play_events_sql in statements
    # An absent raw.play_events is skipped, not checked.
    absent = _rendered(
        "_assert_pitcher_steal_profiles_have_no_leakage", [2024], CUTOFF, play_events=False
    )
    assert PITCHES_LEAK_SQL in absent
    assert play_events_sql not in absent


def test_pitcher_steal_asof_is_threaded_from_run() -> None:
    assert "self._build_pitcher_steal_metrics(seasons, asof=asof)" in _computor_source()


# ---------------------------------------------------------------------------
# Catcher: framing/blocking/throwing/uncaught-K3 read only raw.pitches; the
# aggregator's two Savant joins (pop time, arm strength) have no date column.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,expected_cutoffs",
    [
        ("_compute_catcher_framing", 1),
        ("_compute_catcher_blocking", 1),
        ("_compute_catcher_throwing", 2),
        ("_compute_catcher_uncaught_k3", 1),
    ],
)
def test_catcher_methods_filter_raw_pitches_by_the_cutoff(
    method: str, expected_cutoffs: int
) -> None:
    body = _method_source(method)
    assert "game_date <= DATE" in body
    assert body.count("{date_cutoff}") == expected_cutoffs


def test_a_catcher_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    cut = _rendered("_aggregate_catcher_season_metrics", [2024], asof=CUTOFF)
    assert (
        "DELETE FROM derived.catcher_season_metrics WHERE season IN (2024) OR season > 2024" in cut
    )
    live = _rendered("_aggregate_catcher_season_metrics", [2024], asof=None)
    assert "DELETE FROM derived.catcher_season_metrics WHERE season IN (2024)" in live


def test_catcher_aggregator_season_shifts_the_two_savant_joins() -> None:
    col = "COALESCE(f.season, b.season, t.season)"
    cut = _insert(_rendered("_aggregate_catcher_season_metrics", [2024], asof=CUTOFF))
    assert f"spt.season = {_shifted(col)}" in cut
    assert f"sct.season = {_shifted(col)}" in cut
    live = _insert(_rendered("_aggregate_catcher_season_metrics", [2024], asof=None))
    assert f"spt.season = {col}" in live
    assert f"sct.season = {col}" in live


def test_every_catcher_profile_ends_up_stamped() -> None:
    body = _method_source("_aggregate_catcher_season_metrics")
    assert "below_minimum_sample, updated_at, asof_date" in body
    assert (
        'self._refuse_a_mixed_cutoff("derived.catcher_season_metrics", seasons, asof_date)' in body
    )
    assert 'self._stamp_one_cutoff("derived.catcher_season_metrics", asof_sql)' in body


def test_the_catcher_leakage_assertion_covers_raw_pitches() -> None:
    statements = _rendered("_assert_catcher_profiles_have_no_leakage", [2024], CUTOFF)
    assert statements == [PITCHES_LEAK_SQL]


def test_catcher_asof_is_threaded_from_run() -> None:
    src = _computor_source()
    for call in [
        "self._compute_catcher_framing(seasons, asof=asof)",
        "self._compute_catcher_blocking(seasons, asof=asof)",
        "self._compute_catcher_throwing(seasons, asof=asof)",
        "self._aggregate_catcher_season_metrics(seasons, asof=asof)",
        "self._compute_catcher_uncaught_k3(seasons, asof=asof)",
    ]:
        assert call in src, call


# ---------------------------------------------------------------------------
# Fielder: six per-play methods read only raw.pitches (two of them —
# infield OAA and DP — also do a Python-side sprint-speed lookup); the
# aggregator's four Savant joins (sprint speed, arm strength, and SIM-532's
# outs above average and outfield jump) have no date column. The outfield arm block
# (SIM-550) is filled after the pools from sim.advancement_opportunity_pool,
# which carries game_date, so the fill filters by the cutoff directly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "_compute_outfield_catch_probability",
        "_compute_infield_oaa",
        "_compute_dp_metrics",
        "_compute_bunt_defense",
        "_compute_first_base_scooping",
        "_compute_error_decomposition",
    ],
)
def test_fielder_per_play_methods_filter_raw_pitches_by_the_cutoff(
    method: str,
) -> None:
    body = _method_source(method)
    assert "game_date <= DATE" in body
    assert body.count("{date_cutoff}") == 1


def test_infield_oaa_season_shifts_its_sprint_speed_lookup() -> None:
    body = _method_source("_compute_infield_oaa")
    assert "def _speed_lookup_season(row_season: int) -> int:" in body
    assert "return asof_date.year - 1" in body
    assert "_speed_lookup_season(int(r[" in body


def test_dp_metrics_season_shifts_its_sprint_speed_join() -> None:
    """The join renders through the shared helper (its own test is above)."""
    body = _method_source("_compute_dp_metrics")
    assert 'self._savant_season_sql("bsm.season", asof)' in body
    assert "ss.season = {sprint_speed_season}" in body


def test_a_fielder_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    cut = _rendered("_aggregate_fielder_season_metrics", [2024], asof=CUTOFF)
    assert (
        "DELETE FROM derived.fielder_season_metrics WHERE season IN (2024) OR season > 2024" in cut
    )
    live = _rendered("_aggregate_fielder_season_metrics", [2024], asof=None)
    assert "DELETE FROM derived.fielder_season_metrics WHERE season IN (2024)" in live


def test_fielder_aggregator_season_shifts_all_four_savant_joins() -> None:
    """Sprint speed, arm strength, outs above average and the outfield jump
    (the last two joined by SIM-532; neither board has a date control). The
    baserunning board's join left with SIM-550: the arm block now comes
    from our own dated pool."""
    cut = _insert(_rendered("_aggregate_fielder_season_metrics", [2024], asof=CUTOFF))
    assert cut.count(f"season = {_shifted('c.season')}") == 4
    live = _insert(_rendered("_aggregate_fielder_season_metrics", [2024], asof=None))
    for alias in ("ss", "sas", "soaa", "sj"):
        assert f"{alias}.season = {_shifted('c.season')}" in cut, alias
        assert re.search(rf"\b{alias}\.season = c\.season(\s|$)", live), alias
    body = _method_source("_aggregate_fielder_season_metrics")
    assert "savant_baserunning" not in body
    assert "of_arm_runs_in_cutoff_season" not in body  # the run-value guard is gone


def test_the_arm_block_fill_filters_the_pool_by_the_cutoff_and_not_without() -> None:
    """SIM-550: the pool carries game_date, so the fill takes the plain
    ``game_date <= cutoff`` filter — no season shift — and a live build
    (asof None) applies no date filter at all."""
    body = _method_source("_fill_outfield_arm_block")
    assert "FROM sim.advancement_opportunity_pool" in body
    assert (
        'date_cutoff = f"AND game_date <= DATE \'{asof.isoformat()}\'" if asof is not None else ""'
        in body
    )
    # Once in the pool CTE, once in the leakage query.
    assert body.count("{date_cutoff}") == 2
    assert "savant_season" not in body


def test_the_arm_block_leakage_check_names_the_pool() -> None:
    body = _method_source("_fill_outfield_arm_block")
    assert '"sim.advancement_opportunity_pool"' in body
    assert "SELECT MAX(game_date) FROM sim.advancement_opportunity_pool" in body
    assert "asof or date.today()" in body


def test_every_fielder_profile_ends_up_stamped_last() -> None:
    """asof_date follows sprint_speed, and the six SIM-532 Savant columns
    follow asof_date in ``OAA_JUMP_COLUMN_ORDER`` — the INSERT carries no
    column list, the trap sprint_speed already documents. The SELECT tail
    must read ``FIELDER_TAIL_COLUMNS`` in order."""
    from profiles.constants import FIELDER_TAIL_COLUMNS

    body = _method_source("_aggregate_fielder_season_metrics")
    sprint_idx = body.index("ss.sprint_speed AS sprint_speed")
    asof_idx = body.index("DATE '{asof_sql}' AS asof_date")
    assert asof_idx > sprint_idx
    # ``\b`` so "AS savant_oaa" cannot match inside "AS savant_oaa_per_100".
    positions = [re.search(rf"AS {re.escape(col)}\b", body).start() for col in FIELDER_TAIL_COLUMNS]
    assert positions == sorted(positions)
    assert positions[0] > sprint_idx
    # Nothing follows the tail before the FROM.
    assert body.index("FROM combined_oaa c") > positions[-1]
    assert (
        'self._refuse_a_mixed_cutoff("derived.fielder_season_metrics", seasons, asof_date)' in body
    )
    assert 'self._stamp_one_cutoff("derived.fielder_season_metrics", asof_sql)' in body


def test_the_fielder_leakage_assertion_covers_raw_pitches() -> None:
    statements = _rendered("_assert_fielder_profiles_have_no_leakage", [2024], CUTOFF)
    assert statements == [PITCHES_LEAK_SQL]


def test_fielder_asof_is_threaded_from_run() -> None:
    src = _computor_source()
    for call in [
        "self._compute_outfield_catch_probability(seasons, asof=asof)",
        "self._compute_infield_oaa(seasons, asof=asof)",
        "self._compute_dp_metrics(seasons, asof=asof)",
        "self._compute_bunt_defense(seasons, asof=asof)",
        "self._compute_first_base_scooping(seasons, asof=asof)",
        "self._compute_error_decomposition(seasons, asof=asof)",
        "self._aggregate_fielder_season_metrics(seasons, asof=asof)",
        "self._fill_outfield_arm_block(seasons, asof=asof)",
    ]:
        assert call in src, call
    # SIM-550: the dead extraction is gone, and the fill runs after the pool
    # it reads.
    assert "_compute_outfield_arm_metrics" not in src
    run_body = _method_source("run")
    assert run_body.index("self._build_advancement_opportunity_pool(") < run_body.index(
        "self._fill_outfield_arm_block("
    )


# ---------------------------------------------------------------------------
# The run-expectancy matrix: feeds dp_run_value inside _compute_dp_metrics,
# so it needs the same cutoff even though no engine reads it directly.
# ---------------------------------------------------------------------------


def test_run_expectancy_matrix_takes_an_optional_cutoff() -> None:
    body = inspect.getsource(build_run_expectancy_matrix)
    assert "asof: date | None = None" in body
    assert body.count("{date_cutoff}") == 2


def test_run_expectancy_matrix_skips_persisting_a_cutoff_build() -> None:
    body = inspect.getsource(build_run_expectancy_matrix)
    assert "if asof is not None:" in body
    assert "not persisted" in body


def test_run_expectancy_matrix_asof_is_threaded_from_run() -> None:
    assert "build_run_expectancy_matrix(self._conn, seasons, asof=asof)" in _computor_source()


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


def test_the_schema_version_was_bumped_past_28() -> None:
    """0028 bumped the version to 28; SIM-531's 0029 has since moved it to 29
    (tests/unit/test_sim_store.py pins the exact number, and
    tests/unit/test_docs_duckdb_version.py holds it to the newest migration)."""
    assert int(VERSION_FILE.read_text(encoding="utf-8").strip()) >= 28


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
    assert "duck_columns(" in body  # the shared existence probe (db/introspect.py)
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
