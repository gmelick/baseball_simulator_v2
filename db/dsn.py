"""db/dsn.py — the one reader of the Postgres connection string for scripts and jobs.

The code holds no default database password. The deployment builds the
connection string from the secrets in ``.env``.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

__all__ = ["dsn_label", "require_dsn"]

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
