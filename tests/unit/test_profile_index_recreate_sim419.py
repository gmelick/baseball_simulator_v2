"""
test_profile_index_recreate_sim419.py
=====================================
Unit tests for the SIM-419 hardening of the DuckDB profile-rebuild index
recreate. Exercises the pure helper (_ensure_index_if_not_exists) and
_recreate_indexes against a real in-memory DuckDB (the computor is built via
__new__ so no Postgres DSN is needed — the project's no-DB test pattern).
"""

from __future__ import annotations

import duckdb

from pipeline.batch.player_profile_computor import (
    PlayerProfileComputor,
    _ensure_index_if_not_exists,
)


# --------------------------------------------------------------------------- helper
def test_inject_if_not_exists_plain():
    out = _ensure_index_if_not_exists("CREATE INDEX idx_foo ON s.t(a, b)")
    assert out == "CREATE INDEX IF NOT EXISTS idx_foo ON s.t(a, b)"


def test_inject_if_not_exists_unique():
    out = _ensure_index_if_not_exists("CREATE UNIQUE INDEX idx_u ON s.t(a)")
    assert out == "CREATE UNIQUE INDEX IF NOT EXISTS idx_u ON s.t(a)"


def test_inject_if_not_exists_idempotent_when_present():
    sql = "CREATE INDEX IF NOT EXISTS idx_foo ON s.t(a)"
    assert _ensure_index_if_not_exists(sql) == sql


def test_inject_if_not_exists_passthrough_unknown():
    assert _ensure_index_if_not_exists("SELECT 1") == "SELECT 1"


# --------------------------------------------------------------------------- recreate
def _computor_with_conn(conn: duckdb.DuckDBPyConnection) -> PlayerProfileComputor:
    c = PlayerProfileComputor.__new__(PlayerProfileComputor)  # bypass __init__/DSN
    c._conn = conn
    return c


def test_recreate_indexes_restores_a_dropped_index():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA derived")
    conn.execute("CREATE TABLE derived.t (season INTEGER, x INTEGER)")
    conn.execute("CREATE INDEX idx_t_x ON derived.t(x)")
    # Capture its stored DDL, then drop it (mirrors the pre-DELETE step).
    sql = conn.execute("SELECT sql FROM duckdb_indexes() WHERE index_name = 'idx_t_x'").fetchone()[
        0
    ]
    conn.execute("DROP INDEX IF EXISTS derived.idx_t_x")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM duckdb_indexes() WHERE index_name = 'idx_t_x'"
        ).fetchone()[0]
        == 0
    )

    c = _computor_with_conn(conn)
    failed = c._recreate_indexes([("derived.idx_t_x", sql)])

    assert failed == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM duckdb_indexes() WHERE index_name = 'idx_t_x'"
        ).fetchone()[0]
        == 1
    )


def test_recreate_indexes_is_idempotent_when_index_still_present():
    """A recreate must succeed even if the index was NOT dropped first."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA derived")
    conn.execute("CREATE TABLE derived.t (season INTEGER, x INTEGER)")
    conn.execute("CREATE INDEX idx_t_x ON derived.t(x)")
    sql = conn.execute("SELECT sql FROM duckdb_indexes() WHERE index_name = 'idx_t_x'").fetchone()[
        0
    ]

    c = _computor_with_conn(conn)
    # Index still present — DROP IF EXISTS + IF NOT EXISTS must not raise.
    failed = c._recreate_indexes([("derived.idx_t_x", sql)])
    assert failed == []


def test_recreate_indexes_reports_failure():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA derived")
    conn.execute("CREATE TABLE derived.t (season INTEGER, x INTEGER)")
    c = _computor_with_conn(conn)
    # A bogus DDL (references a non-existent column) can't recreate → reported.
    failed = c._recreate_indexes(
        [("derived.idx_bad", "CREATE INDEX idx_bad ON derived.t(does_not_exist)")]
    )
    assert failed == ["derived.idx_bad"]
