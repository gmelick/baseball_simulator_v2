"""
api/routes/sql_safety.py
========================
SIM-439 — the single, shared, read-only SQL path for the Data Lab.

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

Layered guarantees (see ``docs`` / the SIM-439 build spec):
  1. Validate before the DB (SELECT/WITH only, no ``;``, no write keyword even
     inside a CTE, no dangerous functions, length cap).
  2. ``conn.transaction(readonly=True)`` — any write that slips past validation
     fails at the Postgres engine (``read-only transaction``).
  3. ``SET LOCAL statement_timeout`` — caps runaway scans on the huge table.
  4. Hard row cap enforced *in SQL* (the user query is wrapped in a bounded
     subquery) so a ``SELECT *`` can never materialise millions of rows.
  5. asyncpg's single-statement ``fetch`` refuses multiple statements outright.
  6. Optional belt-and-suspenders: point ``BASEBALL_DB_RO_DSN`` at a
     ``GRANT SELECT``-only Postgres role and even a validator bypass cannot
     write (wired in ``api/main.py``'s lifespan → ``app.state.pg_pool_ro``).

``raw.*`` lives in PostgreSQL; ``derived.*`` / ``sim.*`` live in the in-process
DuckDB and are NOT reachable through this pool, so a cross-database join simply
fails — the assistant's system prompt and the schema browser only expose
``raw.*``.

This module is pure (no FastAPI, no engines) so the validator is unit-tested
without an HTTP layer or a live DB (``tests/unit/test_sql_safety.py``).
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from api.serialization import to_jsonable

# ---------------------------------------------------------------------------
# Tunables (env-overridable; documented in .env.example)
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
    r"pg_ls_dir",
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
        # SECURITY (SIM-442): the CONTENT is preserved, not blanked. A quoted
        # identifier is executable code, not inert text like a string literal or
        # a comment — Postgres resolves `"pg_read_file"` to exactly the same
        # function as bare `pg_read_file`, because unquoted identifiers fold to
        # lower case.
        #
        # Blanking it let `SELECT "pg_read_file"('/etc/passwd')` through: the
        # scanned copy became `SELECT              (            )`, which trips
        # no rule, while `validate_read_only_sql` executes the ORIGINAL string.
        # That bypassed EVERY entry in _DANGEROUS_TOKENS — arbitrary file read,
        # pg_ls_dir, pg_terminate_backend, pg_sleep — as whatever role the pool
        # connects with.
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
    functions; and anything over ``max_len`` characters.
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

    # SECURITY (SIM-442): scan for dangerous FUNCTIONS on a copy that preserves
    # quoted-identifier text.
    #
    # The statement-keyword scan above deliberately runs on the blanked copy:
    # `SELECT "delete" FROM t` is a column reference, not a DELETE, and Postgres
    # would never execute it as one. But a quoted FUNCTION name is genuinely
    # executable — `"pg_read_file"` and `pg_read_file` are the same function,
    # since unquoted identifiers fold to lower case. Scanning the blanked copy
    # let `SELECT "pg_read_file"('/etc/passwd')` past every entry in
    # _DANGEROUS_TOKENS while the ORIGINAL string went on to execute.
    ident_core = _strip_sql_noise(raw, keep_identifiers=True).lower()
    for tok in _DANGEROUS_TOKENS:
        if re.search(tok, lowered) or re.search(tok, ident_core):
            raise SqlValidationError(f"disallowed function '{tok}' is not permitted")

    # Executable form: strip a single trailing semicolon from the ORIGINAL.
    executable = raw
    if executable.endswith(";"):
        executable = executable[:-1].rstrip()
    return executable


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
    "run_read_only_sql",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_TIMEOUT_MS",
]
