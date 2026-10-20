"""
SIM-551 — one cutoff per profile table.

THE DEFECT
----------
On 2026-09-11 a rebuild of the four pool seasons stamped the batter rows it
wrote at its own date, and the six seasons it did not touch kept the stamp of
the day before. The stamping step filled only the rows with NO stamp, so the
two dates stayed side by side, and the batter engine (which refuses a mixed
set) failed at every boot for five days: ``build_all_engines: 10/11``.

THE GUARD
---------
Two shared helpers on the computor, called by every builder that writes a
stamped table:

* ``_refuse_a_mixed_cutoff`` runs BEFORE the delete and the insert. It raises
  when a season the build leaves alone could not honestly carry the new
  stamp — the cutoff's own season, or a truncated point-in-time build — so a
  refusal leaves the table untouched.
* ``_stamp_one_cutoff`` runs after the insert and stamps the WHOLE table.

Every test here runs the helpers against a real in-memory DuckDB, with a
stand-in ``pg.raw.pitches`` where the season's last game date matters.
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path

import duckdb
import pytest

from pipeline.batch.player_profile_computor import PlayerProfileComputor

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"

TABLE = "derived.batter_season_metrics"
D_0910 = date(2026, 9, 10)
D_0911 = date(2026, 9, 11)
D_0917 = date(2026, 9, 17)


def _computor(con: duckdb.DuckDBPyConnection) -> PlayerProfileComputor:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = con
    return comp


def _conn(*, with_pitches: bool = True) -> duckdb.DuckDBPyConnection:
    """A stamped table (the columns the guard reads) and, optionally, the
    ``pg.raw.pitches`` source with 2024's last game on 2024-10-30."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA derived")
    con.execute(f"CREATE TABLE {TABLE} (batter_id INTEGER, season SMALLINT, asof_date DATE)")
    con.execute("ATTACH ':memory:' AS pg")
    con.execute("CREATE SCHEMA pg.raw")
    if with_pitches:
        con.execute("CREATE TABLE pg.raw.pitches (season SMALLINT, game_date DATE)")
        con.execute("INSERT INTO pg.raw.pitches VALUES (2024, DATE '2024-10-30')")
    return con


def _seed(con: duckdb.DuckDBPyConnection, rows: list[tuple[int, date | None]]) -> None:
    """``rows`` = (season, stamp); two batters per season."""
    for i, (season, stamp) in enumerate(rows):
        for k in (1, 2):
            con.execute(f"INSERT INTO {TABLE} VALUES (?, ?, ?)", [10 * i + k, season, stamp])


def _stamps(con: duckdb.DuckDBPyConnection) -> dict[int, date | None]:
    return dict(con.execute(f"SELECT season, MAX(asof_date) FROM {TABLE} GROUP BY 1").fetchall())


# ---------------------------------------------------------------------------
# The defect, replayed: a partial rebuild on a later day
# ---------------------------------------------------------------------------


def test_a_partial_rebuild_on_a_later_day_leaves_one_stamp() -> None:
    """The live table on 2026-09-16: 2017-2022 as of 09-10, 2023-2026 as of
    09-11. A rebuild of the four pool seasons as of 09-17 passes the check and,
    after the insert, the whole-table stamp leaves ONE date."""
    con = _conn()
    _seed(con, [(s, D_0910) for s in range(2017, 2023)] + [(s, D_0911) for s in range(2023, 2027)])
    comp = _computor(con)
    seasons = [2023, 2024, 2025, 2026]

    comp._refuse_a_mixed_cutoff(TABLE, seasons, D_0917)
    # The builder's own insert: the four seasons rewritten at the new cutoff.
    con.execute(f"UPDATE {TABLE} SET asof_date = DATE '2026-09-17' WHERE season >= 2023")
    comp._stamp_one_cutoff(TABLE, D_0917.isoformat())

    distinct = con.execute(f"SELECT COUNT(DISTINCT asof_date) FROM {TABLE}").fetchone()[0]
    assert distinct == 1
    assert set(_stamps(con).values()) == {D_0917}


def test_the_old_fill_null_step_is_what_left_two_stamps() -> None:
    """The regression the guard replaces: stamping only the NULL rows leaves
    the untouched seasons at the earlier date."""
    con = _conn()
    _seed(con, [(2022, D_0910), (2026, D_0911)])
    con.execute(f"UPDATE {TABLE} SET asof_date = DATE '2026-09-11' WHERE asof_date IS NULL")
    assert con.execute(f"SELECT COUNT(DISTINCT asof_date) FROM {TABLE}").fetchone()[0] == 2
    _computor(con)._stamp_one_cutoff(TABLE, D_0911.isoformat())
    assert con.execute(f"SELECT COUNT(DISTINCT asof_date) FROM {TABLE}").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# The refusals — each one before any write
# ---------------------------------------------------------------------------


def test_a_survivor_of_the_cutoffs_own_season_is_refused() -> None:
    """Rebuilding 2024 alone on 2026-09-17 would leave the 2026 rows (built
    as of 09-11) at another date: stale, and not as of the cutoff."""
    con = _conn()
    _seed(con, [(2024, D_0911), (2026, D_0911)])
    with pytest.raises(RuntimeError, match=r"season 2026 carries 2 rows as of 2026-09-11"):
        _computor(con)._refuse_a_mixed_cutoff(TABLE, [2024], D_0917)
    assert _stamps(con) == {2024: D_0911, 2026: D_0911}, "a refusal must not write"


def test_a_truncated_survivor_is_refused() -> None:
    """A 2024 row stamped 2024-04-15 came from a point-in-time build: it holds
    April only. Rebuilding the other seasons whole would make the engine score
    a truncated season against complete ones."""
    con = _conn()
    _seed(con, [(2024, date(2024, 4, 15)), (2025, D_0911), (2026, D_0911)])
    with pytest.raises(RuntimeError, match=r"season 2024 .* truncated build"):
        _computor(con)._refuse_a_mixed_cutoff(TABLE, [2025, 2026], D_0917)


def test_a_same_year_stamp_after_the_last_game_is_a_whole_season() -> None:
    """A live build in the off-season stamps 2024's rows 2024-11-20, after its
    last game (2024-10-30): the season is whole, so it passes."""
    con = _conn()
    _seed(con, [(2024, date(2024, 11, 20)), (2026, D_0911)])
    _computor(con)._refuse_a_mixed_cutoff(TABLE, [2026], D_0917)


def test_a_same_year_stamp_with_no_source_to_ask_is_refused() -> None:
    """When ``raw.pitches`` cannot say where the season ended, the guard does
    not guess: it refuses and names the season to add."""
    con = _conn(with_pitches=False)
    _seed(con, [(2024, date(2024, 11, 20)), (2026, D_0911)])
    with pytest.raises(RuntimeError, match=r"season 2024 .*last game unknown"):
        _computor(con)._refuse_a_mixed_cutoff(TABLE, [2026], D_0917)


def test_a_stamp_before_the_season_began_is_refused() -> None:
    con = _conn()
    _seed(con, [(2024, date(2023, 6, 1)), (2026, D_0911)])
    with pytest.raises(RuntimeError, match=r"season 2024 .*before the season began"):
        _computor(con)._refuse_a_mixed_cutoff(TABLE, [2026], D_0917)


# ---------------------------------------------------------------------------
# What passes, and what the cutoff path leaves to its own delete
# ---------------------------------------------------------------------------


def test_unstamped_rows_of_past_seasons_pass_and_get_stamped() -> None:
    """The pitcher, catcher and fielder tables today: every row NULL. A
    rebuild of the current season stamps the whole table at one date."""
    con = _conn()
    _seed(con, [(s, None) for s in range(2017, 2027)])
    comp = _computor(con)
    comp._refuse_a_mixed_cutoff(TABLE, [2026], D_0917)
    con.execute(f"UPDATE {TABLE} SET asof_date = DATE '2026-09-17' WHERE season = 2026")
    comp._stamp_one_cutoff(TABLE, D_0917.isoformat())
    assert set(_stamps(con).values()) == {D_0917}


def test_a_survivor_already_at_the_cutoff_passes() -> None:
    con = _conn()
    _seed(con, [(2025, D_0917), (2026, D_0917)])
    _computor(con)._refuse_a_mixed_cutoff(TABLE, [2024], D_0917)


def test_a_cutoff_build_ignores_seasons_after_the_cutoff_year() -> None:
    """As of 2024-04-15 the 2025 and 2026 rows cannot exist; the builder's
    own DELETE removes them, so the check does not judge them. The 2023
    survivor is a whole season and passes."""
    con = _conn()
    _seed(con, [(2023, D_0910), (2025, D_0911), (2026, D_0911)])
    _computor(con)._refuse_a_mixed_cutoff(TABLE, [2024], date(2024, 4, 15))


def test_a_cutoff_build_refuses_a_live_row_of_the_cutoffs_own_season() -> None:
    """The hole the fill-NULL step never closed: as of 2024-04-15, a 2024 row
    from a live build holds the whole of 2024 — it leaks the future. Only
    rebuilding 2024 at the cutoff can fix it."""
    con = _conn()
    _seed(con, [(2024, D_0911)])
    with pytest.raises(RuntimeError, match=r"season 2024 carries 2 rows as of 2026-09-11"):
        _computor(con)._refuse_a_mixed_cutoff(TABLE, [2023], date(2024, 4, 15))


def test_an_empty_season_list_is_a_no_op() -> None:
    con = _conn()
    _seed(con, [(2024, D_0910), (2026, D_0911)])
    _computor(con)._refuse_a_mixed_cutoff(TABLE, [], D_0917)


def test_a_table_without_the_stamp_column_names_the_migration() -> None:
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA derived")
    con.execute(f"CREATE TABLE {TABLE} (batter_id INTEGER, season SMALLINT)")
    with pytest.raises(RuntimeError, match=r"migrations \(0026-0028"):
        _computor(con)._refuse_a_mixed_cutoff(TABLE, [2026], D_0917)


# ---------------------------------------------------------------------------
# The wiring: every stamped table, both helpers, and the fail-fast in run()
# ---------------------------------------------------------------------------


def test_the_stamped_table_list_matches_the_schema() -> None:
    """Every table that declares ``asof_date`` in the canonical schema is on
    the list, and nothing else is."""
    ddl = SCHEMA_SQL.read_text(encoding="utf-8")
    declared = set()
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+\.\w+) \((.*?)\n\);", ddl, re.S):
        if re.search(r"^\s*asof_date\s+DATE", m.group(2), re.M):
            declared.add(m.group(1))
    assert declared == set(PlayerProfileComputor.STAMPED_TABLES)


def test_every_builder_checks_before_writing_and_stamps_the_whole_table() -> None:
    src = inspect.getsource(PlayerProfileComputor)
    for table in PlayerProfileComputor.STAMPED_TABLES:
        assert f'self._refuse_a_mixed_cutoff("{table}", seasons, asof_date)' in src, table
        assert f'self._stamp_one_cutoff("{table}", asof_sql)' in src, table
    assert "WHERE asof_date IS NULL" not in src


def test_the_check_runs_before_the_first_write_in_every_builder() -> None:
    """The guard is only a guard if it comes before the builder's DELETE."""
    builders = {
        "_compute_pitcher_profiles": "derived.pitcher_season_metrics",
        "_compute_batter_profiles": "derived.batter_season_metrics",
        "_compute_baserunner_profiles": "derived.baserunner_season_metrics",
        "_build_baserunner_steal_metrics": "derived.baserunner_steal_metrics",
        "_build_pitcher_steal_metrics": "derived.pitcher_steal_metrics",
        "_compute_manager_profiles": "derived.manager_season_metrics",
        "_aggregate_fielder_season_metrics": "derived.fielder_season_metrics",
        "_aggregate_catcher_season_metrics": "derived.catcher_season_metrics",
    }
    for name, table in builders.items():
        body = inspect.getsource(getattr(PlayerProfileComputor, name))
        check = body.index(f'self._refuse_a_mixed_cutoff("{table}"')
        # The statements, not the docstrings (one docstring says "INSERT OR
        # REPLACE" in prose): a write is a DELETE or an INSERT that names a
        # derived table.
        writes = [
            m.start()
            for m in re.finditer(r"(DELETE FROM|INSERT(?: OR REPLACE)?(?: INTO)?) derived\.", body)
        ]
        assert writes, name
        assert check < min(writes), name


def test_run_checks_every_table_before_any_section_writes() -> None:
    body = inspect.getsource(PlayerProfileComputor.run)
    loop = body.index("for table in self.STAMPED_TABLES:")
    assert "self._refuse_a_mixed_cutoff(table, seasons, asof or date.today())" in body
    assert loop < body.index("self._delete_seasons(seasons)")
    assert loop < body.index("self._compute_park_factors(seasons)")
