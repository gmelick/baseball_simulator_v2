"""
tests/unit/test_sql_safety.py
=============================
SIM-439 — the read-only SQL guard. This is the security spine shared by the SQL
console AND the AI assistant, so it is unit-tested hard: the validator must
accept legitimate SELECT/WITH queries and reject every write / DDL / multi-
statement / dangerous-function attempt, and the executor must wrap + row-cap +
run inside a read-only transaction with a statement timeout.
"""

from __future__ import annotations

import pytest

from api.routes.sql_safety import (
    SqlValidationError,
    run_read_only_sql,
    validate_read_only_sql,
)

# ---------------------------------------------------------------------------
# validate_read_only_sql — acceptance
# ---------------------------------------------------------------------------

_OK = [
    "SELECT 1",
    "select season, count(*) from raw.pitches where season = 2024 group by season",
    "  SELECT * FROM raw.players LIMIT 5  ",
    "SELECT 1;",  # single trailing semicolon is stripped, not rejected
    "WITH t AS (SELECT pitcher FROM raw.pitches WHERE season=2024) SELECT * FROM t",
    "-- a leading comment\nSELECT 1",
    "/* block */ SELECT 1",
    # semicolons + keywords INSIDE string literals must not trip the guard
    "SELECT * FROM raw.pitches WHERE des = 'runner; then deleted' AND season = 2024",
    "SELECT * FROM raw.pitches WHERE events = 'strikeout' AND season = 2024",
    # a column literally named like a value in a string is fine
    "SELECT full_name FROM raw.players WHERE full_name ILIKE '%update your roster%'",
]


@pytest.mark.parametrize("sql", _OK)
def test_accepts_read_only(sql: str) -> None:
    out = validate_read_only_sql(sql)
    assert isinstance(out, str) and out
    assert not out.rstrip().endswith(";")  # trailing ';' is stripped for wrapping


# ---------------------------------------------------------------------------
# validate_read_only_sql — rejection
# ---------------------------------------------------------------------------

_BAD = [
    "",
    "   ",
    "INSERT INTO raw.players VALUES (1)",
    "UPDATE raw.pitches SET season = 2020",
    "DELETE FROM raw.pitches",
    "DROP TABLE raw.pitches",
    "ALTER TABLE raw.pitches ADD COLUMN x int",
    "CREATE TABLE t (x int)",
    "TRUNCATE raw.pitches",
    "GRANT SELECT ON raw.pitches TO evil",
    "COPY raw.pitches TO '/tmp/x.csv'",
    "VACUUM raw.pitches",
    "CALL some_proc()",
    "DO $$ BEGIN END $$",
    "MERGE INTO raw.pitches USING x ON true WHEN MATCHED THEN DELETE",
    "SET statement_timeout = 0",
    "BEGIN; SELECT 1",
    # stacked / multi-statement injection
    "SELECT 1; DROP TABLE raw.pitches",
    "SELECT 1; SELECT 2",
    # data-modifying CTE (starts with WITH but writes)
    "WITH t AS (INSERT INTO raw.players VALUES (1) RETURNING player_id) SELECT * FROM t",
    "WITH t AS (DELETE FROM raw.pitches RETURNING game_pk) SELECT * FROM t",
    # dangerous functions inside a legal-looking SELECT
    "SELECT pg_sleep(10)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM dblink('...', 'select 1') AS t(x int)",
    "SELECT lo_import('/etc/passwd')",
    # not a query at all
    "EXPLAIN ANALYZE SELECT 1",  # 'analyze' is blocked
]


@pytest.mark.parametrize("sql", _BAD)
def test_rejects_non_read_only(sql: str) -> None:
    with pytest.raises(SqlValidationError):
        validate_read_only_sql(sql)


def test_length_cap() -> None:
    with pytest.raises(SqlValidationError):
        validate_read_only_sql("SELECT 1 " + "-- x" * 100000, max_len=100)


def test_trailing_semicolon_stripped_for_execution() -> None:
    assert validate_read_only_sql("SELECT 1;") == "SELECT 1"
    assert validate_read_only_sql("  SELECT 1 ;  ") == "SELECT 1"


# ---------------------------------------------------------------------------
# run_read_only_sql — wrapping, row cap, read-only transaction
# ---------------------------------------------------------------------------


class _Attr:
    def __init__(self, name: str) -> None:
        self.name = name


class _Stmt:
    def __init__(self, columns: list[str], rows: list[list]) -> None:
        self._columns = columns
        self._rows = rows

    def get_attributes(self):
        return [_Attr(c) for c in self._columns]

    async def fetch(self):
        return self._rows


class _Txn:
    def __init__(self, conn: _FakeConn, readonly: bool) -> None:
        conn.readonly_seen = readonly

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Minimal asyncpg-connection stand-in that records what it was asked."""

    def __init__(self, columns: list[str], rows: list[list]) -> None:
        self._columns = columns
        self._rows = rows
        self.readonly_seen: bool | None = None
        self.executed: list[str] = []
        self.prepared_sql: str | None = None

    def transaction(self, *, readonly: bool = False, **_):
        return _Txn(self, readonly)

    async def execute(self, sql: str):
        self.executed.append(sql)

    async def prepare(self, sql: str):
        self.prepared_sql = sql
        return _Stmt(self._columns, self._rows)


async def test_executor_wraps_and_caps() -> None:
    # 5 rows, cap of 3 → truncated True, only 3 returned.
    rows = [[i, f"n{i}"] for i in range(5)]
    conn = _FakeConn(["player_id", "name"], rows[:4])  # fetch returns cap+1 (4) at most in real PG
    result = await run_read_only_sql(conn, "SELECT player_id, name FROM raw.players", max_rows=3)

    assert result["columns"] == ["player_id", "name"]
    assert result["row_count"] == 3
    assert result["truncated"] is True
    assert result["rows"][0] == [0, "n0"]
    # read-only transaction was used and a statement_timeout was set.
    assert conn.readonly_seen is True
    assert any("statement_timeout" in e for e in conn.executed)
    # the user SQL is wrapped in a bounded subquery fetching cap+1 rows.
    assert conn.prepared_sql is not None
    assert "_mlb_sub" in conn.prepared_sql
    assert "LIMIT 4" in conn.prepared_sql  # cap (3) + 1


async def test_executor_not_truncated_when_within_cap() -> None:
    conn = _FakeConn(["x"], [[1], [2]])
    result = await run_read_only_sql(conn, "SELECT x FROM raw.players", max_rows=10)
    assert result["row_count"] == 2
    assert result["truncated"] is False


async def test_executor_validates_before_db() -> None:
    conn = _FakeConn(["x"], [[1]])
    with pytest.raises(SqlValidationError):
        await run_read_only_sql(conn, "DELETE FROM raw.pitches", max_rows=10)
    # nothing was prepared/executed — the DB was never touched.
    assert conn.prepared_sql is None
    assert conn.executed == []


# ===========================================================================
# SIM-442 — quoted-identifier bypass of the dangerous-function blocklist
# ===========================================================================


class TestQuotedIdentifierBypass:
    """A quoted identifier is executable code, not inert text.

    ``_strip_sql_noise`` blanked the contents of ``"quoted identifiers"`` along
    with comments and string literals, and both blocklists were scanned against
    that blanked copy — while ``validate_read_only_sql`` returns the ORIGINAL
    string for execution. So::

        SELECT "pg_read_file"('/etc/passwd')

    was scanned as ``SELECT              (            )``, tripping no rule, and
    then executed verbatim. Postgres resolves ``"pg_read_file"`` to exactly the
    same function as bare ``pg_read_file`` (unquoted identifiers fold to lower
    case), so this defeated EVERY entry in ``_DANGEROUS_TOKENS``.

    It did not yield a write — the ``readonly=True`` transaction and the
    statement-keyword list both hold — but it yielded arbitrary server-side file
    read, directory listing and connection kill as whatever role the pool uses.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT \"pg_read_file\"('/etc/passwd')",
            "SELECT \"PG_READ_FILE\"('/etc/passwd')",
            "SELECT \"pg_ls_dir\"('/')",
            'SELECT "pg_terminate_backend"(1)',
            'SELECT "pg_sleep"(100)',
            "SELECT \"current_setting\"('data_directory')",
            'SELECT * FROM raw.pitches WHERE "pg_sleep"(10) IS NULL',
            "WITH x AS (SELECT \"pg_read_file\"('/etc/passwd') AS c) SELECT * FROM x",
        ],
    )
    def test_quoted_dangerous_function_is_rejected(self, sql: str) -> None:
        with pytest.raises(SqlValidationError, match="disallowed function"):
            validate_read_only_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT pg_ls_dir('/')",
            "SELECT pg_sleep(100)",
        ],
    )
    def test_unquoted_form_still_rejected(self, sql: str) -> None:
        """The original protection must not have regressed."""
        with pytest.raises(SqlValidationError, match="disallowed function"):
            validate_read_only_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT game_pk, release_speed FROM raw.pitches LIMIT 10",
            'SELECT "pitcher", "batter" FROM raw.pitches LIMIT 5',
            "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
            "SELECT * FROM raw.pitches WHERE des = 'In play, out(s)' LIMIT 1",
            "SELECT count(*) FROM raw.pitches -- a comment mentioning delete",
            "SELECT 'pg_read_file' AS just_a_string FROM raw.pitches LIMIT 1",
        ],
    )
    def test_legitimate_queries_still_pass(self, sql: str) -> None:
        """Closing the bypass must not start rejecting real queries.

        Note the last case: a dangerous NAME inside a string LITERAL is still
        inert and must remain allowed — only quoted IDENTIFIERS are executable.
        """
        assert validate_read_only_sql(sql)

    def test_quoted_statement_keyword_is_still_allowed(self) -> None:
        """A column named "delete" is an identifier, not the DELETE command.

        The statement-keyword scan deliberately still runs on the blanked copy,
        so this must not become a false rejection.
        """
        assert validate_read_only_sql('SELECT "delete" FROM raw.pitches LIMIT 1')
