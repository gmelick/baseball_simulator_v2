"""
pipeline/db_dsn.py
==================
SIM-443b — the one reader of the Postgres connection string for scripts and jobs.

The code carries no default database password. A script that needs Postgres
calls :func:`require_dsn`. The function returns the variable's value, or it
raises a clear error that names the variable and the template to copy from.
The deployment composes the value from the secrets in ``.env``.
"""

from __future__ import annotations

import os

__all__ = ["require_dsn"]


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
