"""
Integration test fixtures — SIM-145
====================================
Provides session-scoped testcontainers fixtures for PostgreSQL 15 and Redis 7.
Each container starts once per test session, drastically reducing overhead
versus per-test containers.

Alembic migrations are applied to the PostgreSQL container on startup so every
test in this suite runs against the canonical schema.

Usage
-----
Test functions declare the fixtures they need:

    def test_something(pg_engine, redis_client):
        ...

Async tests must be marked ``@pytest.mark.asyncio`` and import
``async_redis_client`` instead.

Requires
--------
    testcontainers[postgres,redis]>=4.0
    sqlalchemy>=2.0
    asyncpg>=0.29
    redis>=5.0
    alembic>=1.13

All of the above are in requirements-dev.txt.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Optional imports — gracefully skip if testcontainers isn't installed
# (e.g., in a CI job that only runs unit tests)
# ---------------------------------------------------------------------------
try:
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TESTCONTAINERS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POSTGRES_IMAGE = "postgres:15-alpine"
_REDIS_IMAGE = "redis:7-alpine"
_POSTGRES_DB = "baseball_sim_test"
_POSTGRES_USER = "test_user"
_POSTGRES_PASSWORD = "test_pass"  # nosec — test only, never production


# ---------------------------------------------------------------------------
# Session-scoped PostgreSQL container
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_container():
    """Spin up a PostgreSQL 15 container for the entire test session.

    Yields the running ``PostgresContainer`` instance.  Alembic migrations are
    applied before the fixture is handed to any test — every test therefore
    starts with the full production schema.

    Skips automatically if testcontainers is not installed.
    """
    if not _TESTCONTAINERS_AVAILABLE:
        pytest.skip("testcontainers not installed — skipping integration tests")

    with PostgresContainer(
        image=_POSTGRES_IMAGE,
        dbname=_POSTGRES_DB,
        username=_POSTGRES_USER,
        password=_POSTGRES_PASSWORD,
    ) as postgres:
        # ----------------------------------------------------------------
        # Apply Alembic migrations so the schema is ready before any test
        # ----------------------------------------------------------------
        dsn = postgres.get_connection_url()
        # testcontainers returns a psycopg2 URL; Alembic also needs psycopg2
        psycopg2_dsn = dsn.replace("postgresql://", "postgresql+psycopg2://", 1)

        env = {**os.environ, "BASEBALL_DB_DSN": psycopg2_dsn}
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Alembic migrations failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )

        yield postgres


@pytest.fixture(scope="session")
def pg_engine(pg_container) -> Generator[sa.Engine, None, None]:
    """Return a SQLAlchemy Engine connected to the test PostgreSQL instance.

    Session-scoped: one engine per test session, connection pool shared
    across all integration tests.
    """
    dsn = pg_container.get_connection_url()
    engine = sa.create_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_connection(pg_engine) -> Generator[sa.Connection, None, None]:
    """Yield a transactional connection that auto-rolls back after each test.

    This isolates every test: writes made during the test are never committed
    to the shared container, so tests cannot interfere with each other.
    """
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# Session-scoped Redis container
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def redis_container():
    """Spin up a Redis 7 container for the entire test session.

    Skips automatically if testcontainers is not installed.
    """
    if not _TESTCONTAINERS_AVAILABLE:
        pytest.skip("testcontainers not installed — skipping integration tests")

    with RedisContainer(image=_REDIS_IMAGE) as redis:
        yield redis


@pytest.fixture()
def redis_client(redis_container):
    """Return a synchronous Redis client pointed at the test container.

    Each test gets a fresh client.  The Redis database is flushed before
    each test so state doesn't bleed between tests.
    """
    import redis as redis_lib

    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    client = redis_lib.Redis(host=host, port=int(port), decode_responses=True)
    client.flushall()  # clean slate per test
    yield client
    client.close()


@pytest_asyncio.fixture()
async def async_redis_client(redis_container) -> AsyncGenerator:
    """Return an async Redis client for use in ``@pytest.mark.asyncio`` tests.

    Flushes all keys before yielding so state doesn't bleed between tests.
    """
    import redis.asyncio as aioredis

    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    client = aioredis.Redis(host=host, port=int(port), decode_responses=True)
    await client.flushall()
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------
# Async PostgreSQL (asyncpg) fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def asyncpg_pool(pg_container):
    """Return an asyncpg connection pool for async integration tests.

    Yields the pool and closes it after the test.
    """
    import asyncpg

    dsn = pg_container.get_connection_url()
    # asyncpg uses the plain postgresql:// scheme (no driver prefix)
    asyncpg_dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)

    pool = await asyncpg.create_pool(asyncpg_dsn, min_size=1, max_size=5)
    yield pool
    await pool.close()


# ---------------------------------------------------------------------------
# Helper: assert table exists in the test database
# ---------------------------------------------------------------------------


def assert_table_exists(conn: sa.Connection, schema: str, table: str) -> None:
    """Raise AssertionError if ``schema.table`` does not exist in the DB."""
    row = conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": schema, "table": table},
    ).fetchone()
    assert row is not None, (
        f"Expected table {schema}.{table} to exist but it was not found. "
        "Check that Alembic migrations ran successfully."
    )
