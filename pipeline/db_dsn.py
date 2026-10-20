"""
pipeline/db_dsn.py
==================
The one reader of the Postgres connection string for scripts and jobs.

The code carries no default database password. A script that needs Postgres
calls :func:`require_dsn`. The function returns the variable's value, or it
raises a clear error that names the variable and the template to copy from.
The deployment composes the value from the secrets in ``.env``.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

__all__ = ["dsn_label", "require_dsn"]

#: One ``key=value`` pair of a libpq keyword DSN (the value may be quoted).
_KEYWORD_PAIR = re.compile(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)")


def require_dsn(var: str = "BASEBALL_DB_DSN") -> str:
    """Return the connection string held in the environment variable ``var``.

    The value is stripped of surrounding whitespace. An unset or blank variable
    raises :class:`RuntimeError`; the message names the variable and
    ``.env.example``. No fallback DSN exists.
    """
    value = os.environ.get(var, "").strip()
    if not value:
        raise RuntimeError(
            f"{var} is not set. Set it to a postgresql:// connection string "
            "(see .env.example; the secrets come from scripts/gen_secrets.py). "
            "No default database password exists."
        )
    return value


def dsn_label(dsn: str) -> str:
    """Return ``host[:port]/dbname`` for a DSN, for a log line or a report.

    The label never holds the user, the password or a query string. A URL DSN
    is read with :func:`urllib.parse.urlsplit`; a libpq keyword DSN
    (``host=db password=...``) gives only its ``host``, ``port`` and
    ``dbname``. A DSN that does not parse gives ``<redacted>``.
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
