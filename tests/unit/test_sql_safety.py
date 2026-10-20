"""The read-only SQL guard of ``api/routes/sql_safety.py``.

The SQL console and the AI assistant share this guard, so the tests press it hard.
The validator accepts real SELECT and WITH queries. It rejects every write, every
DDL statement (a statement that changes the schema), every stacked statement and
every dangerous function. The executor wraps the query, caps the rows and runs it
inside a read-only transaction with a statement timeout. The role check confirms
that the console's pool logs in as a read-only role.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from api.routes.sql_safety import (
    SqlValidationError,
    check_read_only_role,
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
# The quoted-identifier bypass of the dangerous-function blocklist
# ===========================================================================


class TestQuotedIdentifierBypass:
    """A quoted identifier is executable code, not inert text.

    Postgres resolves ``"pg_read_file"`` to the same function as a bare
    ``pg_read_file``, because unquoted identifiers fold to lower case.
    ``validate_read_only_sql`` returns the original string for execution. So a
    function scan on a copy with the quoted identifiers blanked reads::

        SELECT "pg_read_file"('/etc/passwd')

    as ``SELECT              (            )``, and every entry in
    ``_DANGEROUS_TOKENS`` misses it. The ``readonly=True`` transaction still
    blocks a write, but the miss gives server-side file reads, directory listings
    and connection kills as the pool's role. So the function scan keeps the
    quoted identifiers, and these tests pin that.
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
        """The bare form stays rejected too."""
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
        """The function scan of the quoted identifiers rejects no real query.

        The last case holds a dangerous name inside a string literal. A literal is
        inert, so the query passes; only a quoted identifier executes.
        """
        assert validate_read_only_sql(sql)

    def test_quoted_statement_keyword_is_still_allowed(self) -> None:
        """A column named "delete" is an identifier, not the DELETE command.

        The statement-keyword scan runs on the blanked copy on purpose, so this
        column is not a false rejection.
        """
        assert validate_read_only_sql('SELECT "delete" FROM raw.pitches LIMIT 1')


# ===========================================================================
# The relation allowlist: every table is raw.<name> or a CTE
# ===========================================================================


class TestRelationAllowlist:
    """The console reads ``raw.*`` only.

    The statement-shape checks above, on their own, let ``SELECT * FROM
    pg_authid`` (password hashes), ``pg_stat_activity`` (other sessions' SQL),
    ``information_schema`` and ``sim.sim_runs`` through. So a parser (sqlglot)
    reads the query, and every table must be ``raw.<name>`` or a CTE in scope.
    Every case runs through :func:`validate_read_only_sql`, the one entry point
    of the console and the assistant.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM pg_authid",
            "SELECT rolpassword FROM pg_catalog.pg_authid",
            'SELECT * FROM "pg_authid"',
            'SELECT * FROM "pg_catalog"."pg_authid"',
            "SELECT * FROM pg_shadow",
            "SELECT * FROM pg_roles",
            "SELECT query FROM pg_stat_activity",
            "SELECT * FROM information_schema.columns",
            "SELECT * FROM sim.sim_runs",
            "SELECT * FROM public.x",
            "SELECT 1 UNION SELECT rolname FROM pg_catalog.pg_roles",
            "SELECT * FROM raw.pitches p JOIN sim.sim_runs s ON s.game_pk = p.game_pk",
            "SELECT * FROM raw.players WHERE player_id IN (SELECT oid FROM pg_authid)",
            "SELECT (SELECT count(*) FROM pg_roles) AS n",
            "SELECT * FROM raw.pitches WHERE pitcher > ALL (SELECT 1 FROM pg_roles)",
            "SELECT ARRAY(SELECT rolname FROM pg_roles)",
            "SELECT * FROM raw.pitches NATURAL JOIN pg_authid",
            "SELECT * FROM raw.pitches, pg_authid",
            "SELECT * FROM (raw.pitches CROSS JOIN pg_roles)",
            "SELECT * FROM raw.pitches p JOIN LATERAL (SELECT * FROM pg_roles) r ON true",
            "SELECT * FROM raw.pitches INTERSECT SELECT * FROM sim.sim_runs",
            # the self-shadowing CTE: the inner reference is the real catalog
            "WITH pg_authid AS (SELECT * FROM pg_authid) SELECT * FROM pg_authid",
            # a forward CTE reference without RECURSIVE is a real table
            "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS x) SELECT * FROM a",
            # a three-part name
            "SELECT * FROM baseball_sim.raw.pitches",
            # a schema name that differs only by quoting
            'SELECT * FROM "RAW".pitches',
            # functions in FROM
            "SELECT * FROM pg_ls_waldir()",
            "SELECT * FROM raw.pitches p, LATERAL pg_ls_waldir() f",
            "SELECT query_to_xml('select * from pg_authid', true, false, '')",
            "SELECT * FROM pg_stat_get_activity(NULL)",
            "SELECT * FROM some_unknown_srf(1)",
            "SELECT * FROM raw.pitches p, LATERAL some_unknown_srf(p.pitcher) f",
            "SELECT * FROM xmltable('/a' PASSING '<a/>' COLUMNS x int)",
            # the TABLE shorthand, which sqlglot drops inside a CTE
            "WITH a AS (TABLE pg_authid) SELECT * FROM a",
            # SELECT ... INTO creates a table
            "SELECT 1 AS x INTO raw.copy_of_one",
        ],
    )
    def test_rejected(self, sql: str) -> None:
        with pytest.raises(SqlValidationError):
            validate_read_only_sql(sql)

    def test_catalog_message_names_the_table(self) -> None:
        with pytest.raises(SqlValidationError, match=r"only raw\.\* tables may be read; found"):
            validate_read_only_sql("SELECT * FROM pg_authid")

    def test_unqualified_table_says_write_it_as_raw(self) -> None:
        with pytest.raises(SqlValidationError, match=r"write it as raw\.pitches"):
            validate_read_only_sql("SELECT * FROM pitches")

    def test_qualified_message_names_the_schema(self) -> None:
        with pytest.raises(SqlValidationError, match=r"found sim\.sim_runs"):
            validate_read_only_sql("SELECT * FROM sim.sim_runs")

    def test_parse_failure_fails_closed(self) -> None:
        with pytest.raises(SqlValidationError, match="could not parse"):
            validate_read_only_sql("SELECT FROM WHERE ((")

    def test_two_statements_rejected_by_the_parser(self) -> None:
        from api.routes.sql_safety import validate_relation_allowlist

        # The ';' scan in validate_read_only_sql catches this first; the
        # parser check must hold on its own as well.
        with pytest.raises(SqlValidationError, match="single statement"):
            validate_relation_allowlist("SELECT 1; SELECT 2")

    def test_non_query_statement_rejected_by_the_parser(self) -> None:
        from api.routes.sql_safety import validate_relation_allowlist

        with pytest.raises(SqlValidationError):
            validate_relation_allowlist("SHOW search_path")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT * FROM raw.pitches",
            'SELECT * FROM RAW."Pitches"',
            'SELECT * FROM "raw"."pitches"',
            "SELECT * FROM raw.pitches p JOIN raw.players pl ON pl.player_id = p.pitcher",
            "WITH t AS (SELECT pitcher FROM raw.pitches) SELECT * FROM t",
            # a CTE that shadows a system name, used from the main body
            "WITH pg_authid AS (SELECT 1 AS x) SELECT * FROM pg_authid",
            # an earlier sibling CTE
            "WITH a AS (SELECT 1 AS x), b AS (SELECT * FROM a) SELECT * FROM b",
            # WITH RECURSIVE sees itself
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) "
            "SELECT * FROM t",
            # a CTE seen from inside a nested subquery
            "WITH a AS (SELECT 1 AS x) SELECT * FROM (SELECT * FROM a) s",
            # a CTE on a set operation
            "WITH a AS (SELECT 1 AS x) SELECT * FROM a UNION SELECT * FROM a",
            "SELECT * FROM (SELECT pitcher FROM raw.pitches) s",
            "SELECT * FROM raw.pitches p, LATERAL unnest(ARRAY[p.balls, p.strikes]) u(v)",
            "SELECT * FROM raw.pitches p, LATERAL generate_series(1, p.balls) g(n)",
            "SELECT * FROM raw.pitches p CROSS JOIN LATERAL (SELECT p.balls + 1 AS b) s",
            "SELECT * FROM generate_series(1, 3) g(n)",
            "SELECT * FROM generate_series(1, 3) WITH ORDINALITY AS g(n, i)",
            "SELECT * FROM unnest(ARRAY[1, 2]) AS u(x)",
            "SELECT * FROM (VALUES (1), (2)) v(x)",
            "SELECT * FROM jsonb_each('{}'::jsonb)",
            "SELECT * FROM regexp_split_to_table('a b', ' ')",
            # a column named "table" is an identifier, not the TABLE shorthand
            'SELECT "table" FROM raw.pitches LIMIT 1',
            "SELECT 'table' AS word FROM raw.pitches LIMIT 1",
        ],
    )
    def test_accepted(self, sql: str) -> None:
        assert validate_read_only_sql(sql)

    def test_quoted_identifier_cases_still_pass(self) -> None:
        """Every real query of the quoted-identifier tests passes the allowlist too."""
        for sql in (
            "SELECT game_pk, release_speed FROM raw.pitches LIMIT 10",
            'SELECT "pitcher", "batter" FROM raw.pitches LIMIT 5',
            "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
            "SELECT * FROM raw.pitches WHERE des = 'In play, out(s)' LIMIT 1",
            "SELECT count(*) FROM raw.pitches -- a comment mentioning delete",
            "SELECT 'pg_read_file' AS just_a_string FROM raw.pitches LIMIT 1",
            'SELECT "delete" FROM raw.pitches LIMIT 1',
        ):
            assert validate_read_only_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT pg_ls_waldir()",
            "SELECT pg_ls_logdir()",
            "SELECT table_to_xml('raw.pitches', true, false, '')",
            "SELECT pg_get_userbyid(10)",
            "SELECT * FROM pg_show_all_settings()",
            "SELECT pg_backend_pid()",
            "SELECT * FROM pg_hba_file_rules",
            "SELECT pg_current_logfile()",
            "SELECT * FROM pg_config",
        ],
    )
    def test_new_blocklist_entries(self, sql: str) -> None:
        with pytest.raises(SqlValidationError, match="disallowed function"):
            validate_read_only_sql(sql)


# ---------------------------------------------------------------------------
# The live parse report: the console's starter queries and Postgres syntax
# ---------------------------------------------------------------------------

_STARTER_FILE = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "components"
    / "lab"
    / "starterQueries.ts"
)


def _starter_queries() -> list[str]:
    text = _STARTER_FILE.read_text(encoding="utf-8")
    return re.findall(r"sql:\s*`([^`]*)`", text)


def test_starter_queries_validate() -> None:
    """Every starter query the console ships passes the allowlist.

    This is the live parse report for sqlglot on the queries users start
    from. A starter query that fails here would greet every user with a 400.
    """
    if not _STARTER_FILE.exists():
        pytest.skip(f"{_STARTER_FILE} is not present in this checkout")
    queries = _starter_queries()
    assert len(queries) == 9, f"expected nine starter queries, found {len(queries)}"
    for sql in queries:
        assert validate_read_only_sql(sql), sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT release_speed::numeric, game_date::text FROM raw.pitches LIMIT 1",
        "SELECT COUNT(*) FILTER (WHERE type = 'X') FROM raw.pitches WHERE season = 2024",
        "SELECT pitcher, row_number() OVER (PARTITION BY season ORDER BY release_speed DESC) "
        "FROM raw.pitches WHERE season = 2024",
        "SELECT DISTINCT ON (pitcher) pitcher, release_speed FROM raw.pitches "
        "ORDER BY pitcher, release_speed DESC",
        "SELECT full_name FROM raw.players WHERE full_name ILIKE '%cole%'",
        "SELECT date_trunc('month', game_date) FROM raw.pitches WHERE season = 2024",
        "SELECT EXTRACT(year FROM game_date) FROM raw.pitches LIMIT 1",
        "SELECT * FROM raw.games WHERE game_date > DATE '2024-04-01' - INTERVAL '7 days'",
        "SELECT string_agg(pitch_type, ',' ORDER BY pitch_type) FROM raw.pitches "
        "WHERE season = 2024",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY release_speed) FROM raw.pitches "
        "WHERE season = 2024",
        "SELECT pitch_type FROM raw.pitches WHERE season = 2024 GROUP BY ROLLUP (pitch_type)",
        "SELECT pitch_type FROM raw.pitches ORDER BY pitch_type NULLS LAST "
        "OFFSET 5 FETCH FIRST 3 ROWS ONLY",
        "SELECT CASE WHEN balls = 3 THEN 'full' END, COALESCE(zone, 0), "
        "balls IS DISTINCT FROM strikes FROM raw.pitches LIMIT 1",
        "SELECT * FROM raw.pitches TABLESAMPLE SYSTEM (1) LIMIT 5",
    ],
)
def test_catalog_style_queries_validate(sql: str) -> None:
    """Postgres syntax the assistant writes parses and passes."""
    assert validate_read_only_sql(sql)


def test_sqlglot_assumptions() -> None:
    """Pin the two parser facts the allowlist relies on.

    1. ``raw`` reads as an identifier (the schema part of a table), not a
       keyword.
    2. A function in a FROM clause arrives as ``exp.Table`` whose ``this`` is
       a function node.
    """
    tree = sqlglot.parse_one("SELECT * FROM raw.pitches", read="postgres")
    table = next(tree.find_all(exp.Table))
    assert isinstance(table.args.get("db"), exp.Identifier)
    assert table.args["db"].name == "raw"
    assert table.name == "pitches"

    tree = sqlglot.parse_one("SELECT * FROM pg_ls_waldir()", read="postgres")
    table = next(tree.find_all(exp.Table))
    assert isinstance(table.this, exp.Func)


# ---------------------------------------------------------------------------
# check_read_only_role: the console's pool logs in as a read-only role
# ---------------------------------------------------------------------------


class _RolePool:
    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.sql: str | None = None

    async def fetchrow(self, sql: str):
        self.sql = sql
        return self.row


def _role_row(**overrides: object) -> dict:
    row: dict = {
        "role": "baseball_ro",
        "superuser": False,
        "sim_usage": False,
        "raw_insert": False,
        "raw_select": True,
    }
    row.update(overrides)
    return row


async def test_check_read_only_role_accepts_baseball_ro() -> None:
    pool = _RolePool(_role_row())
    check = await check_read_only_role(pool)
    assert check.role == "baseball_ro"
    assert check.acceptable is True
    assert pool.sql is not None and "to_regclass('raw.pitches')" in pool.sql


@pytest.mark.parametrize("flag", ["superuser", "sim_usage", "raw_insert"])
async def test_check_read_only_role_refuses_each_flag(flag: str) -> None:
    check = await check_read_only_role(_RolePool(_role_row(**{flag: True})))
    assert check.acceptable is False
    assert f"{flag}=True" in check.flags()


async def test_check_read_only_role_without_select_is_still_acceptable() -> None:
    check = await check_read_only_role(_RolePool(_role_row(raw_select=False)))
    assert check.acceptable is True
    assert check.raw_select is False


async def test_check_read_only_role_no_row_raises() -> None:
    with pytest.raises(RuntimeError):
        await check_read_only_role(_RolePool(None))
