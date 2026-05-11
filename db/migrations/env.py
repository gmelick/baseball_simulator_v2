"""
Alembic environment configuration.
Reads the database URL from the BASEBALL_DB_DSN environment variable so that
no credentials are hard-coded in version-controlled files.

Usage:
    export BASEBALL_DB_DSN="postgresql+psycopg2://user:pass@localhost/baseball"
    alembic upgrade head
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Alembic Config object — provides access to values within alembic.ini
# ---------------------------------------------------------------------------

config = context.config

# Inject BASEBALL_DB_DSN env var into the SQLAlchemy URL before anything else
# reads it.  This lets CI, Docker, and dev environments all use the same
# alembic.ini without modification.
#
# DSN format coercion: the app (asyncpg) wants ``postgresql://...``; SQLAlchemy
# + psycopg2 (used here by alembic) wants ``postgresql+psycopg2://...``.
# Auto-add the driver suffix if it's missing so contributors only need to set
# ONE env var (``BASEBALL_DB_DSN``) regardless of which consumer reads it.
# This eliminates the docker-compose ${VAR:-default} substitution dance for
# the migrate service that broke on some platforms.
_dsn = os.environ.get("BASEBALL_DB_DSN")
if _dsn:
    if _dsn.startswith("postgresql://"):
        _dsn = "postgresql+psycopg2://" + _dsn[len("postgresql://") :]
    config.set_main_option("sqlalchemy.url", _dsn)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Metadata for autogenerate support.
# If/when SQLAlchemy ORM models are added under db/models/, import their
# Base.metadata here so Alembic can diff against live schema automatically.
# ---------------------------------------------------------------------------
# from db.models import Base
# target_metadata = Base.metadata
target_metadata = None


# ---------------------------------------------------------------------------
# Offline mode — generates SQL script without a live DB connection
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine. The generated
    SQL can be reviewed and applied manually (useful for production deployments
    where direct DB access from CI is restricted).
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Include raw / sim schemas in autogenerate scope
        include_schemas=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — applies migrations directly against a live DB connection
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an Engine and associates a connection with the context.
    NullPool is used so Alembic does not hold idle connections after finishing.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
        )

        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------------
# Dispatch — alembic chooses offline vs online based on the invocation.
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
