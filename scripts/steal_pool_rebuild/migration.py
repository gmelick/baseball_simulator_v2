"""DuckDB migration 0031, applied inside the rebuild's own transaction (step 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db.introspect import duck_columns
from pools.build_steal_pool import STEAL_POOL_0031_COLUMNS

from .common import MIGRATION

# ---------------------------------------------------------------------------
# The migration (the same statement splitter as scripts/probes/fielder_profile_recompute.py)
# ---------------------------------------------------------------------------


def migration_statements(text: str) -> list[str]:
    """The statements of a migration file. Drop the comments first (a whole
    comment line, and the tail of a line after ``--``), then split on ';'."""
    lines = [line.split("--", 1)[0] for line in text.splitlines()]
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def missing_columns(con: Any) -> list[str]:
    """The migration-0031 columns the steal pool does not have yet (both when the table is absent)."""
    have = duck_columns(con, "sim", "steal_opportunity_pool", this_file_only=True)
    return [c for c in STEAL_POOL_0031_COLUMNS if c not in have]


def apply_migration(con: Any, migration: Path = MIGRATION) -> int:
    """Apply migration 0031 (idempotent: ADD COLUMN IF NOT EXISTS). Returns the
    statement count."""
    statements = migration_statements(migration.read_text(encoding="utf-8"))
    for stmt in statements:
        con.execute(stmt)
    return len(statements)
