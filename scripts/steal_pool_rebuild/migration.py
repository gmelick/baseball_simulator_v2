"""Migration 0031, applied inside the rebuild's own transaction (step 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import MIGRATION, NEW_COLUMNS, _table_columns

# ---------------------------------------------------------------------------
# The migration (sim532's statement splitter)
# ---------------------------------------------------------------------------


def migration_statements(text: str) -> list[str]:
    """The statements of a migration file. Drop the comments first (a whole
    comment line, and the tail of a line after ``--``), then split on ';'."""
    lines = [line.split("--", 1)[0] for line in text.splitlines()]
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def missing_columns(con: Any) -> list[str]:
    """The migration-0031 columns the steal pool does not have yet."""
    have = _table_columns(con, "sim", "steal_opportunity_pool")
    return [c for c in NEW_COLUMNS if c not in have]


def apply_migration(con: Any, migration: Path = MIGRATION) -> int:
    """Apply migration 0031 (idempotent: ADD COLUMN IF NOT EXISTS). Returns the
    statement count."""
    statements = migration_statements(migration.read_text(encoding="utf-8"))
    for stmt in statements:
        con.execute(stmt)
    return len(statements)
