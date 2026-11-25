"""db/dsn.py — the one reader of the Postgres connection string for scripts and jobs.

The code holds no default database password. The deployment builds the
connection string from the secrets in ``.env``. :func:`attach_pg` attaches
Postgres to a DuckDB connection as the read-only catalog ``pg``.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

__all__ = ["attach_pg", "dsn_label", "require_dsn"]

#: One ``key=value`` pair of a libpq keyword DSN (the value may be quoted).
_KEYWORD_PAIR = re.compile(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)")


def require_dsn(var: str = "BASEBALL_DB_DSN") -> str:
    """Return the connection string in the variable ``var``, stripped.

    Raise RuntimeError, naming the variable and ``.env.example``, when the
    variable is unset or blank. No fallback exists.
    """
    value = os.environ.get(var, "").strip()
    if not value:
        raise RuntimeError(
            f"{var} is not set. Set it to a postgresql:// connection string "
            "(see .env.example; the secrets come from operations/gen_secrets.py). "
            "No default database password exists."
        )
    return value


def dsn_label(dsn: str) -> str:
    """Return ``host[:port]/dbname`` for a log line, never the user or the password.

    The function reads a URL or a libpq keyword string (``host=db ...``).
    A string that does not parse gives ``<redacted>``.
    """
    text = str(dsn).strip()
    if "://" in text:
        try:
            parts = urlsplit(text)
            host = parts.hostname or ""
            port = str(parts.port) if parts.port else ""
        except ValueError:
            return "<redacted>"
        dbname = parts.path.rsplit("/", 1)[-1]
    else:
        found = {key.lower(): value.strip("'") for key, value in _KEYWORD_PAIR.findall(text)}
        host = found.get("host", "")
        port = found.get("port", "")
        dbname = found.get("dbname", "")
    if not host and not dbname:
        return "<redacted>"
    return f"{host}{':' + port if port else ''}/{dbname}"


def _redact(text: str, dsn: str) -> str:
    """Replace the DSN (and its password) in ``text``; DuckDB quotes the DSN in an ATTACH error."""
    body = str(text)
    if dsn:
        body = body.replace(dsn, "<dsn>")
        # A URL DSN (scheme://user:password@host/...): drop the password on its own too.
        head = dsn.split("@", 1)[0]
        if ":" in head.split("//", 1)[-1]:
            password = head.split("//", 1)[-1].split(":", 1)[1]
            if password:
                body = body.replace(password, "<password>")
    return body


def attach_pg(con: Any, dsn: str | None = None) -> None:
    """Attach Postgres read-only as the catalog ``pg``; a no-op when ``pg`` is attached.

    ``dsn`` defaults to ``BASEBALL_DB_DSN``; with neither, the attach raises
    ``RuntimeError`` naming the variable (there is no default password). The
    attach works on a read-only DuckDB connection. An attach failure raises
    ``RuntimeError`` with the DSN's password redacted.
    """
    attached = con.execute(
        "SELECT database_name FROM duckdb_databases() WHERE database_name = 'pg'"
    ).fetchall()
    if attached:
        return
    dsn = dsn or require_dsn()
    try:
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres, READ_ONLY);")
    except Exception as exc:  # noqa: BLE001 - any attach failure is reported the same way
        raise RuntimeError(f"Postgres attach failed: {_redact(str(exc), dsn)}") from None
