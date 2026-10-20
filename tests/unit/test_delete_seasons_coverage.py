"""The profile computor's ``_delete_seasons`` lists every season-keyed
``derived.*`` table, so a full rebuild leaves no stale rows behind."""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]

_DUCKDB_SCHEMA_PATH = _ROOT / "db" / "schemas" / "02_duckdb_schema.sql"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# The tables _delete_seasons() leaves out on purpose:
#   - sim.* pools: each build method that writes one deletes its own rows.
#   - derived.run_expectancy_matrix: keyed by season_range, not season.
_EXCLUDED_FROM_DELETE_SEASONS: set[str] = {
    "derived.run_expectancy_matrix",
}


def _parse_derived_tables_with_season() -> set[str]:
    """Return the fully qualified ``derived.*`` tables in 02_duckdb_schema.sql
    that have a ``season`` column: a table rebuilt season by season."""
    sql = _read(_DUCKDB_SCHEMA_PATH)
    tables: set[str] = set()
    # Each CREATE TABLE [IF NOT EXISTS] derived.<name> ( ... ); block.
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(derived\.\w+)\s*\((.*?)\);",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(sql):
        full_name = m.group(1).lower()
        body = m.group(2)
        if re.search(r"^\s*season\s+(SMALLINT|INTEGER)\b", body, re.MULTILINE | re.IGNORECASE):
            tables.add(full_name)
    return tables


def _parse_delete_seasons_table_list() -> list[str]:
    """Read the table list literal out of player_profile_computor.py::_delete_seasons."""
    path = _ROOT / "pipeline" / "batch" / "player_profile_computor.py"
    text = _read(path)
    m = re.search(
        r"def\s+_delete_seasons\(self,\s*seasons:\s*list\[int\]\)\s*->\s*None:.*?tables\s*=\s*\[(.*?)\]",
        text,
        re.DOTALL,
    )
    assert m, "Could not locate the tables = [...] literal in _delete_seasons()"
    raw = m.group(1)
    names = [
        s.strip().strip('"').strip("'")
        for s in re.findall(r'"([^"]+)"|\'([^\']+)\'', raw)
        for s in [s[0] or s[1]]
    ]
    return [n for n in names if n]


class TestDeleteSeasonsCoverage:
    """A full rebuild deletes every ``derived.*`` table with a ``season`` column."""

    def test_play_detail_tables_in_delete_list(self) -> None:
        """The three per-play detail tables are in the list."""
        listed = set(_parse_delete_seasons_table_list())
        for required in (
            "derived.outfield_play_detail",
            "derived.infield_play_detail",
            "derived.dp_play_detail",
        ):
            assert required in listed, (
                f"SIM-091: {required} missing from _delete_seasons() table list. "
                "Without it, full_rebuild=True leaves stale defensive metric data."
            )

    def test_all_season_keyed_derived_tables_in_delete_list(self) -> None:
        """A new season-keyed ``derived.*`` table fails this test until
        _delete_seasons() lists it, so a full rebuild leaves no stale rows."""
        schema_tables = _parse_derived_tables_with_season()
        delete_listed = set(_parse_delete_seasons_table_list())
        expected = schema_tables - _EXCLUDED_FROM_DELETE_SEASONS

        missing = sorted(expected - delete_listed)
        assert not missing, (
            "SIM-091 regression: the following derived.* tables have a "
            "`season` column but are not in _delete_seasons():\n  "
            + "\n  ".join(missing)
            + "\n\nAdd them to the `tables` list in player_profile_computor.py "
            "or, if intentional, add them to _EXCLUDED_FROM_DELETE_SEASONS "
            "in this test with a comment explaining why."
        )
