"""
api/routes/sql_safety.py
========================
The one read-only SQL path of the Data Lab.

Both the SQL Console (``api/routes/sql_runner.py``) and the AI Assistant
(``api/routes/ai_assistant.py``) execute ad-hoc SQL through the *same two
functions here* so the assistant is physically incapable of doing anything the
console can't:

  * :func:`validate_read_only_sql` — a defense-in-depth validator that accepts a
    single ``SELECT`` / ``WITH … SELECT`` statement and rejects everything else
    (writes, DDL, multi-statement/stacked injection, and a blocklist of
    dangerous functions) BEFORE the database is ever touched.
  * :func:`run_read_only_sql` — validates, then executes inside a **read-only
    transaction** with a ``statement_timeout`` and a hard **row cap**, against
    the (very large, ~10⁷-row) ``raw.pitches`` Postgres store.

The guarantees, in layers:
  1. Validate before the DB (SELECT/WITH only, no ``;``, no write keyword even
     inside a CTE, no dangerous functions, length cap).
  2. ``conn.transaction(readonly=True)`` — any write that slips past validation
     fails at the Postgres engine (``read-only transaction``).
  3. ``SET LOCAL statement_timeout`` — caps runaway scans on the huge table.
  4. Hard row cap enforced *in SQL* (the user query is wrapped in a bounded
     subquery) so a ``SELECT *`` can never materialise millions of rows.
  5. asyncpg's single-statement ``fetch`` refuses multiple statements outright.
  6. The relation allowlist (SIM-443d): a parser (sqlglot) reads the query, and
     every table it names must be ``raw.<table>`` or a CTE of the query. The
     system catalogs (``pg_authid``, ``pg_stat_activity``,
     ``information_schema``) and every other schema are rejected before the
     database is touched. See :func:`validate_relation_allowlist`.
  7. The read-only role (SIM-443e): the console and the assistant run ONLY on
     the ``BASEBALL_DB_RO_DSN`` pool, a role that can read ``raw.*`` and nothing
     else. The lifespan (``api/startup.py``) checks the role with
     :func:`check_read_only_role` and refuses a superuser. With no such pool the
     routes answer 503; they never fall back to the main pool. The Data Lab read
     routes (schema, analytics, players, data freshness) also run only on this
     pool. The 503 text, ``RO_POOL_UNAVAILABLE_DETAIL``, lives in
     ``api/routes/_common.py``; this module re-exports it.

The query must name its tables as ``raw.<table>``. An unqualified name is
rejected even though the pool's ``search_path`` is ``raw``, so the rule never
depends on a session setting.

This module has no route and no engine, so the validator is unit-tested
without an HTTP layer or a live DB (``tests/unit/test_sql_safety.py``). It
imports one constant from ``api.routes._common``, which pulls in FastAPI's
``HTTPException`` and nothing else.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

# Imported at module top on purpose. The relation allowlist is a security
# control, so a missing package must fail the boot, never switch the check off.
import sqlglot
from sqlglot import exp

# The 503 text for a missing read-only pool lives in ``_common.py`` (the
# read-only Data Lab routes use it too); this module re-exports it.
from api.routes._common import RO_POOL_UNAVAILABLE_DETAIL
from api.serialization import to_jsonable

# ---------------------------------------------------------------------------
# The tunables (the environment can set each; .env.example documents them)
# ---------------------------------------------------------------------------

#: Hard ceiling on rows returned to the client. The console + assistant may ask
#: for fewer, never more.
DEFAULT_MAX_ROWS = int(os.environ.get("SQL_RUNNER_MAX_ROWS", "1000"))
#: statement_timeout applied to every ad-hoc query (milliseconds).
DEFAULT_TIMEOUT_MS = int(os.environ.get("SQL_RUNNER_TIMEOUT_MS", "5000"))
#: Reject queries longer than this (characters) — a cheap DoS / paste guard.
MAX_SQL_LEN = int(os.environ.get("SQL_RUNNER_MAX_LEN", "20000"))

# Absolute ceilings the env overrides can never exceed (safety rails).
_HARD_MAX_ROWS = 5000
_HARD_MAX_TIMEOUT_MS = 30000


class SqlValidationError(ValueError):
    """Raised when a query is not a safe single read-only statement.

    A ``ValueError`` subclass so callers that only ``except ValueError`` still
    catch it; the route layer maps it to an HTTP 400 with the message as
    ``detail``.
    """


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Data-modifying / DDL / session keywords that must never appear ANYWHERE in the
# statement — not even inside a CTE (Postgres allows a data-modifying CTE such as
# ``WITH t AS (INSERT … RETURNING …) SELECT …`` which would write despite the
# statement "starting" with WITH). Matched as whole words on the comment/string
# -stripped text, so a string literal like 'called_strike' or a value containing
# these words is unaffected.
_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "merge",
    "upsert",
    "truncate",
    "create",
    "alter",
    "drop",
    "grant",
    "revoke",
    "comment",
    "copy",
    "call",
    "do",
    "vacuum",
    "analyze",
    "reindex",
    "cluster",
    "refresh",
    "lock",
    "set",
    "reset",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "prepare",
    "execute",
    "deallocate",
    "listen",
    "notify",
    "discard",
)

# Dangerous functions / server-side file & network access, blocked even inside a
# legal SELECT.
_DANGEROUS_TOKENS = (
    r"pg_sleep",
    r"pg_read_file",
    r"pg_read_binary_file",
    # The whole pg_ls_* family (pg_ls_dir, pg_ls_waldir, pg_ls_logdir,
    # pg_ls_tmpdir, pg_ls_archive_statusdir), not pg_ls_dir alone.
    r"pg_ls_",
    r"pg_stat_file",
    r"lo_import",
    r"lo_export",
    r"dblink",
    r"pg_terminate_backend",
    r"pg_cancel_backend",
    r"pg_reload_conf",
    r"current_setting",
    r"set_config",
    r"txid_",
    # Functions that read a table or run SQL text by NAME, or that report
    # server internals. query_to_xml / table_to_xml / cursor_to_xml
    # take a table name or a query string, so they read a relation that the
    # relation allowlist never sees. None of these substrings occurs in a
    # raw.* column name.
    r"_to_xml",
    r"pg_stat_get_",
    r"pg_show_all_",
    r"pg_hba_file_rules",
    r"pg_ident_file_mappings",
    r"pg_config",
    r"pg_get_",
    r"pg_current_logfile",
    r"pg_backend_pid",
)

# ---------------------------------------------------------------------------
# The relation allowlist (the SQL console hardening, SIM-443d)
# ---------------------------------------------------------------------------

#: The one schema the console may read.
ALLOWED_SCHEMA = "raw"

#: Set-returning functions that may stand in a FROM clause. Each one computes
#: rows from its arguments and reads no table. Any other function in a FROM
#: clause is rejected (fail closed). sqlglot names generate_series
#: ``exploding_generate_series`` when it parses it into a FROM clause.
_TABLE_FUNCTIONS_ALLOWED = frozenset(
    {
        "generate_series",
        "exploding_generate_series",
        "generate_subscripts",
        "unnest",
        "json_each",
        "json_each_text",
        "jsonb_each",
        "jsonb_each_text",
        "json_array_elements",
        "json_array_elements_text",
        "jsonb_array_elements",
        "jsonb_array_elements_text",
        "json_object_keys",
        "jsonb_object_keys",
        "regexp_split_to_table",
        "regexp_matches",
        "string_to_table",
    }
)


def _strip_sql_noise(sql: str, *, keep_identifiers: bool = False) -> str:
    """Blank out comments and string literals, preserving offsets.

    ``-- line comments``, ``/* block comments */``, ``'single-quoted strings'``
    (SQL ``''`` escapes handled) and ``$tag$ dollar quoted $tag$`` bodies are
    always replaced by spaces of equal length, so a semicolon or the word
    ``delete`` inside a literal never triggers a false rejection.

    ``keep_identifiers`` controls the treatment of ``"quoted identifiers"``:

    * ``False`` (default) — blanked like a literal. Correct for the statement
      keyword scan, where a column that happens to be named ``"select"`` is an
      identifier and not the SELECT command, so blanking avoids a false reject.
    * ``True`` — the identifier TEXT is preserved (quotes replaced by spaces).
      Required for the dangerous-function scan, because a quoted identifier IS
      executable code: Postgres resolves ``"pg_read_file"`` to the very same
      function as bare ``pg_read_file``. See :func:`_scan_texts`.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]
        # line comment
        if two == "--":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue
        # block comment (non-nested is sufficient for the safety scan)
        if two == "/*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(" " * (j - i))
            i = j
            continue
        # single-quoted string
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # '' escape
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
            continue
        # dollar-quoted string ($tag$ … $tag$)
        if ch == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j == -1 else j + len(tag)
                out.append(" " * (j - i))
                i = j
                continue
        # Double-quoted identifier.
        #
        # SECURITY: with keep_identifiers the CONTENT is preserved, not
        # blanked. A quoted identifier is executable code, not inert text like a
        # string literal or a comment: Postgres resolves `"pg_read_file"` to the
        # same function as bare `pg_read_file`, because unquoted identifiers
        # fold to lower case.
        #
        # A blanked copy would turn `SELECT "pg_read_file"('/etc/passwd')` into
        # `SELECT              (            )`, which trips no rule, while
        # `validate_read_only_sql` executes the ORIGINAL string: every entry in
        # _DANGEROUS_TOKENS (file reads, pg_ls_dir, pg_terminate_backend,
        # pg_sleep) would pass.
        #
        # Only the quote characters are replaced, so offsets are preserved and
        # `keep_identifiers=False` still yields the blanked form for the
        # statement-keyword scan (see _scan_texts).
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            span = sql[i:j]
            if keep_identifiers:
                # Replace only the delimiters; the identifier text stays visible
                # to the dangerous-function scan.
                out.append(
                    " " + span[1:-1].replace('"', " ") + " " if len(span) >= 2 else " " * len(span)
                )
            else:
                out.append(" " * (j - i))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def validate_read_only_sql(sql: str, *, max_len: int = MAX_SQL_LEN) -> str:
    """Validate that ``sql`` is a single read-only statement; return it cleaned.

    The returned string is the executable form — the original SQL trimmed with a
    single trailing ``;`` removed (comments / string literals preserved so the
    query still runs as written). Raises :class:`SqlValidationError` otherwise.

    Rejects: empty input; anything not beginning with ``SELECT`` or ``WITH``;
    any statement containing a data-modifying / DDL / session keyword (even
    inside a CTE); multiple statements (an inner ``;``); a blocklist of dangerous
    functions; anything over ``max_len`` characters; and, last, any query that
    reads a table outside ``raw.*`` (:func:`validate_relation_allowlist`).
    """
    if not isinstance(sql, str):
        raise SqlValidationError("query must be a string")

    raw = sql.strip()
    if not raw:
        raise SqlValidationError("empty query")
    if len(raw) > max_len:
        raise SqlValidationError(f"query too long ({len(raw)} chars; limit {max_len})")

    # Scan a comment/literal-stripped copy so string contents never trip a rule.
    core = _strip_sql_noise(raw).strip()
    if core.endswith(";"):
        core = core[:-1].rstrip()
    if ";" in core:
        raise SqlValidationError(
            "only a single statement is allowed — remove the ';' and run one SELECT at a time"
        )
    if not core:
        raise SqlValidationError("query has no executable statement (comments only?)")

    first = re.match(r"[(\s]*([A-Za-z_]+)", core)
    keyword = first.group(1).lower() if first else ""
    if keyword not in ("select", "with"):
        raise SqlValidationError(
            f"only read-only SELECT / WITH queries are allowed (got '{keyword.upper() or '?'}')"
        )

    lowered = core.lower()
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(kw)}(?![A-Za-z0-9_])", lowered):
            raise SqlValidationError(
                f"disallowed keyword '{kw.upper()}' — this console is read-only (SELECT only)"
            )

    # SECURITY: the scan for dangerous FUNCTIONS runs on a copy that keeps the
    # quoted-identifier text.
    #
    # The statement-keyword scan above runs on the blanked copy on purpose:
    # `SELECT "delete" FROM t` is a column reference, not a DELETE, and
    # Postgres never executes it as one. But a quoted FUNCTION name executes:
    # `"pg_read_file"` and `pg_read_file` are the same function, since unquoted
    # identifiers fold to lower case. A scan of the blanked copy would let
    # `SELECT "pg_read_file"('/etc/passwd')` past every entry in
    # _DANGEROUS_TOKENS while the ORIGINAL string executes.
    ident_core = _strip_sql_noise(raw, keep_identifiers=True).lower()
    for tok in _DANGEROUS_TOKENS:
        if re.search(tok, lowered) or re.search(tok, ident_core):
            raise SqlValidationError(f"disallowed function '{tok}' is not permitted")

    # Executable form: strip a single trailing semicolon from the ORIGINAL.
    executable = raw
    if executable.endswith(";"):
        executable = executable[:-1].rstrip()

    # The parser-backed relation check runs last, after the cheap scans, on
    # the exact text that executes.
    validate_relation_allowlist(executable)
    return executable


def _fold(ident: Any) -> str:
    """Return an identifier's name as Postgres resolves it.

    An unquoted identifier folds to lower case; a quoted one keeps its case.
    """
    if not isinstance(ident, exp.Identifier):
        return str(ident or "")
    return ident.name if ident.quoted else ident.name.lower()


def _cte_name(cte: exp.Expression) -> str:
    alias = cte.args.get("alias")
    ident = alias.this if isinstance(alias, exp.TableAlias) else alias
    return _fold(ident)


def _ctes_in_scope(node: exp.Expression) -> set[str]:
    """Return the CTE names a table reference at ``node`` can see.

    The walk goes up the tree. At a ``WITH`` that holds the reference, a
    non-recursive ``WITH`` shows only the CTEs before the one that holds it
    (Postgres resolves a forward or self reference to a real table); a
    ``WITH RECURSIVE`` shows all of them. At any other ancestor, a ``WITH``
    attached to it (the main body's view) shows all its CTEs. So
    ``WITH pg_authid AS (SELECT * FROM pg_authid) ...`` sees no CTE at the
    inner reference, and the check rejects it.
    """
    names: set[str] = set()
    child: exp.Expression = node
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            recursive = bool(parent.args.get("recursive"))
            for cte in parent.expressions:
                if not recursive and cte is child:
                    break
                names.add(_cte_name(cte))
        else:
            for value in parent.args.values():
                if isinstance(value, exp.With) and value is not child:
                    names.update(_cte_name(cte) for cte in value.expressions)
        child = parent
        parent = parent.parent
    return names


def _function_name(func: exp.Expression) -> str:
    """Return the lower-case name of a function node from sqlglot."""
    if isinstance(func, exp.Anonymous):
        return str(func.name).lower()
    if isinstance(func, exp.Func):
        return func.sql_name().lower()
    return type(func).__name__.lower()


def _check_function_relation(func: exp.Expression) -> None:
    name = _function_name(func)
    if name not in _TABLE_FUNCTIONS_ALLOWED:
        raise SqlValidationError(
            f"the function {name}() may not be used as a table; "
            f"only {ALLOWED_SCHEMA}.* tables may be read"
        )


def _dotted(table: exp.Table) -> str:
    parts = [_fold(table.args.get(key)) for key in ("catalog", "db", "this") if table.args.get(key)]
    return ".".join(parts) or "?"


def validate_relation_allowlist(sql: str) -> None:
    """Reject a query that reads any table outside ``raw.*``.

    sqlglot parses ``sql`` in the Postgres dialect. The check fails closed:

    * a parse error, or anything other than exactly one statement, is rejected;
    * the statement must be a query (``SELECT``, a set operation, or a
      parenthesised query). sqlglot returns a generic command node for syntax
      it does not support, and such a node carries no table nodes to check;
    * ``SELECT ... INTO`` is rejected (it creates a table);
    * the ``TABLE name`` shorthand is rejected: sqlglot drops the name inside
      a CTE (``WITH a AS (TABLE pg_authid) ...``), so the parse cannot see it;
    * every table must be ``raw.<name>`` (no catalog part), or a bare name that
      is a CTE in scope at that point;
    * a function in a FROM clause (plain or ``LATERAL``) must be on
      ``_TABLE_FUNCTIONS_ALLOWED``.

    ``find_all`` walks the whole tree, so CTE bodies, subqueries, set
    operations, scalar subqueries and ``LATERAL`` bodies are all checked.
    Raises :class:`SqlValidationError`; returns None when the query passes.
    """
    # The TABLE shorthand. The scan runs on a copy with comments, strings and
    # quoted identifiers blanked, so a column named "table" stays legal.
    if re.search(r"(?<![A-Za-z0-9_$])table(?![A-Za-z0-9_$])", _strip_sql_noise(sql).lower()):
        raise SqlValidationError(
            f"the TABLE shorthand is not supported; write SELECT * FROM {ALLOWED_SCHEMA}.<table>"
        )

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as exc:  # noqa: BLE001 — any parser failure rejects the query
        first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]
        raise SqlValidationError(f"could not parse the query ({first_line})") from exc

    parsed = [s for s in statements if s is not None]
    if len(parsed) != 1:
        raise SqlValidationError("only a single statement is allowed")
    root = parsed[0]
    if not isinstance(root, exp.Query):
        raise SqlValidationError("only read-only SELECT / WITH queries are allowed")
    if root.find(exp.Into) is not None:
        raise SqlValidationError("SELECT ... INTO is not allowed — this console is read-only")

    for lateral in root.find_all(exp.Lateral):
        body = lateral.this
        if isinstance(body, (exp.Subquery, exp.Query)):
            continue  # its tables are checked by the walk below
        if isinstance(body, exp.Func):
            _check_function_relation(body)
            continue
        raise SqlValidationError("unsupported LATERAL form; use LATERAL (SELECT ...)")

    for table in root.find_all(exp.Table):
        target = table.this
        if isinstance(target, exp.Func):
            if table.args.get("db") or table.args.get("catalog"):
                raise SqlValidationError(
                    f"a schema-qualified function may not be used as a table; "
                    f"only {ALLOWED_SCHEMA}.* tables may be read"
                )
            _check_function_relation(target)
            continue
        if not isinstance(target, exp.Identifier):
            raise SqlValidationError(
                f"unsupported FROM item; only {ALLOWED_SCHEMA}.* tables may be read"
            )
        db = table.args.get("db")
        if table.args.get("catalog"):
            raise SqlValidationError(
                f"only {ALLOWED_SCHEMA}.* tables may be read; found {_dotted(table)}"
            )
        if db is not None:
            if _fold(db) == ALLOWED_SCHEMA:
                continue
            raise SqlValidationError(
                f"only {ALLOWED_SCHEMA}.* tables may be read; found {_dotted(table)}"
            )
        name = _fold(target)
        if name in _ctes_in_scope(table):
            continue
        raise SqlValidationError(
            f"only {ALLOWED_SCHEMA}.* tables may be read; found {name} "
            f"(write it as {ALLOWED_SCHEMA}.{name})"
        )


# ---------------------------------------------------------------------------
# The read-only role check (the console's read-only pool, SIM-443e)
# ---------------------------------------------------------------------------

# One round trip. to_regnamespace / to_regclass return NULL for a missing
# schema or table, the privilege functions return NULL for a NULL oid, and
# COALESCE turns that into false, so a fresh database never errors.
_ROLE_CHECK_SQL = """
SELECT current_user::text AS role,
       COALESCE((SELECT r.rolsuper FROM pg_catalog.pg_roles r
                 WHERE r.rolname = current_user), false) AS superuser,
       COALESCE(has_schema_privilege(to_regnamespace('sim')::oid, 'USAGE'), false)
           AS sim_usage,
       COALESCE(has_table_privilege(to_regclass('raw.pitches')::oid, 'INSERT'), false)
           AS raw_insert,
       COALESCE(has_table_privilege(to_regclass('raw.pitches')::oid, 'SELECT'), false)
           AS raw_select
"""


@dataclass(frozen=True)
class ReadOnlyRoleCheck:
    """What the SQL console's database role can do.

    ``acceptable`` is True when the role is not a superuser, cannot use the
    ``sim`` schema, and cannot insert into ``raw.pitches``. ``raw_select`` is
    reported but does not decide: a role that cannot read is useless, not
    dangerous.
    """

    role: str
    superuser: bool
    sim_usage: bool
    raw_insert: bool
    raw_select: bool

    @property
    def acceptable(self) -> bool:
        return not (self.superuser or self.sim_usage or self.raw_insert)

    def flags(self) -> str:
        return (
            f"superuser={self.superuser}, sim_usage={self.sim_usage}, "
            f"raw_insert={self.raw_insert}, raw_select={self.raw_select}"
        )


async def check_read_only_role(pool: Any) -> ReadOnlyRoleCheck:
    """Read the role of ``pool`` and what it may do, in one query.

    ``pool`` is an asyncpg Pool (or a stand-in with ``fetchrow``). Errors from
    the driver propagate; the caller treats any error as a refusal.
    """
    row = await pool.fetchrow(_ROLE_CHECK_SQL)
    if row is None:
        raise RuntimeError("the role check returned no row")
    return ReadOnlyRoleCheck(
        role=str(row["role"]),
        superuser=bool(row["superuser"]),
        sim_usage=bool(row["sim_usage"]),
        raw_insert=bool(row["raw_insert"]),
        raw_select=bool(row["raw_select"]),
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _clamp(value: int, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


async def _execute(conn: Any, wrapped_sql: str, timeout_ms: int) -> tuple[list[str], list[Any]]:
    """Prepare + fetch inside a read-only transaction; return (columns, records).

    Uses ``conn.prepare`` so column names are available even for a zero-row
    result. ``SET LOCAL statement_timeout`` binds the timeout to this
    transaction only.
    """
    async with conn.transaction(readonly=True):
        await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        stmt = await conn.prepare(wrapped_sql)
        columns = [a.name for a in stmt.get_attributes()]
        records = await stmt.fetch()
    return columns, records


async def run_read_only_sql(
    pool: Any,
    sql: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Validate then execute ``sql`` read-only and return a JSON-safe payload.

    Returns ``{columns, rows, row_count, truncated, elapsed_ms}`` where ``rows``
    is a list of row-lists (cells passed through :func:`to_jsonable` so numpy /
    driver types never leak). ``truncated`` is True when the result hit the row
    cap. Raises :class:`SqlValidationError` on a rejected query; asyncpg errors
    (syntax, timeout, read-only-write) propagate to the caller for a clean 400.

    ``pool`` may be an asyncpg Pool (has ``.acquire``) or, in tests, a stand-in
    connection object exposing ``transaction`` / ``execute`` / ``prepare``
    directly.
    """
    validated = validate_read_only_sql(sql)
    cap = _clamp(max_rows, 1, _HARD_MAX_ROWS, DEFAULT_MAX_ROWS)
    to = _clamp(timeout_ms, 100, _HARD_MAX_TIMEOUT_MS, DEFAULT_TIMEOUT_MS)

    # Enforce the row cap IN SQL so an unbounded SELECT can't materialise
    # millions of rows. The leading newline before ')' protects the wrapper from
    # a trailing line-comment in the user's SQL swallowing the paren. We fetch
    # cap+1 so we can report truncation honestly.
    wrapped = f"SELECT * FROM (\n{validated}\n) AS _mlb_sub LIMIT {cap + 1}"

    start = time.perf_counter()
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            columns, records = await _execute(conn, wrapped, to)
    else:  # test stub: pool IS a connection
        columns, records = await _execute(pool, wrapped, to)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    truncated = len(records) > cap
    kept = records[:cap]
    rows = [[to_jsonable(cell) for cell in rec] for rec in kept]

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": round(elapsed_ms, 1),
    }


__all__ = [
    "SqlValidationError",
    "validate_read_only_sql",
    "validate_relation_allowlist",
    "run_read_only_sql",
    "check_read_only_role",
    "ReadOnlyRoleCheck",
    "ALLOWED_SCHEMA",
    "RO_POOL_UNAVAILABLE_DETAIL",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_TIMEOUT_MS",
]
