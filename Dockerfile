# =============================================================================
# MLB Baseball Simulation Platform — Dockerfile
# Multi-stage build: build stage installs deps; runtime stage is lean.
#
# Build:  docker build -t baseball-sim .
# Run:    docker run --env-file .env -p 8000:8000 baseball-sim
#
# CI smoke test:
#   docker run --rm baseball-sim python -c 'import api.main'
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1 — builder
# Installs all Python dependencies into a prefix so the runtime stage
# only copies the compiled packages, not build tools.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

# Prevent .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install system build deps needed to compile native extensions.
#
# SIM-445: libpq-dev is REQUIRED — psycopg2 is now SOURCE-BUILT rather than
# installed from the psycopg2-binary wheel, so it links the SYSTEM libpq and
# libssl instead of bundling its own copies. The binary wheel put FOUR OpenSSL
# objects in one address space (its own libssl/libcrypto plus the system pair
# loaded by `requests`/`ssl`), which psycopg2's own docs warn against for
# production. See requirements.txt for the full reasoning.
#
# faiss-cpu needs libgomp.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python deps first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2 — runtime
# Minimal image: only the installed packages + application source.
# No build tools, no package manager caches.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Tell Python where to find the application source (installed packages
    # are copied from the builder into /usr/local, which is already on PATH).
    PYTHONPATH=/app

# libgomp is needed at runtime for faiss-cpu.
# SIM-445: libpq5 is the RUNTIME half of the source-built psycopg2 — the compiled
# extension links the system libpq and cannot import without it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application source (respects .dockerignore)
COPY api/         ./api/
COPY pipeline/    ./pipeline/
COPY similarity/  ./similarity/
COPY simulation/  ./simulation/
COPY betting/     ./betting/
COPY db/          ./db/
COPY scripts/     ./scripts/
COPY alembic.ini  ./alembic.ini

# Non-root user for security (principle of least privilege).
#
# Two ownership / filesystem concerns handled here:
#
# 1. /data: created with appuser ownership so that when the duckdb_data
#    named volume mounts onto it (see docker-compose.yml), Docker copies
#    the appuser-owned directory into the new volume on first init.
#    Without this, the volume inherits Docker's default root ownership
#    and the non-root container can't write /data/baseball_sim.duckdb.
#
# 2. /home/appuser: DuckDB's INSTALL/LOAD of the postgres extension
#    requires a writable home directory to cache the downloaded extension
#    binary. Previously the user was created with --no-create-home, which
#    crashed any DuckDB operation that touched extensions with
#    "Can't find the home directory at '/home/appuser'".
RUN useradd --system --uid 1001 --create-home --home-dir /home/appuser appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data /home/appuser
USER appuser

# Health check — liveness probe via the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

EXPOSE 8000

# Default: run the FastAPI app with uvicorn.
# Override CMD in docker-compose for worker count / reload flags.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]


# ---------------------------------------------------------------------------
# Stage 3 — dev (extends runtime with pytest / ruff / mypy / etc.)
#
# Used by docker-compose for ``make test``, ``make lint``, ``make format``,
# ``make type-check``.  The lean ``runtime`` stage above stays the production
# default so prod images don't ship pytest + dev tooling (smaller, smaller
# attack surface, faster pulls).
#
# Layered as a separate stage rather than baking dev deps into runtime so
# the runtime tag remains trustworthy for ``docker push`` / production use.
# ---------------------------------------------------------------------------
FROM runtime AS dev

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Both requirement files must land in the same directory because
# requirements-dev.txt starts with ``-r requirements.txt`` — pip resolves
# that path relative to the file it's reading, not relative to cwd.
COPY requirements.txt     /tmp/requirements.txt
COPY requirements-dev.txt /tmp/requirements-dev.txt
# pytest + pytest-asyncio + ruff + mypy + httpx + testcontainers + ...
# The -r requirements.txt inside requirements-dev.txt re-resolves runtime
# deps, but pip is a no-op when versions already match.
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt \
 && rm /tmp/requirements.txt /tmp/requirements-dev.txt

# Test files aren't part of the runtime image — copy them into the dev image
# so the ``app`` service can run pytest without bind-mounting the host.
COPY tests/        /app/tests/
COPY pyproject.toml /app/pyproject.toml

# Files that test_backend_sim101_to_106_148_153.py::TestSim153SecretsBaseline
# asserts ship with the image (SIM-153 secrets-baseline acceptance gates).
# These were previously stripped by `.dockerignore` (`.env.*`, `*.md`) or
# never COPY'd by this stage — now restored so `make test` is self-contained.
COPY .env.example      /app/.env.example
COPY .gitignore        /app/.gitignore
COPY requirements.txt  /app/requirements.txt
COPY README.md         /app/README.md
COPY BACKLOG.md        /app/BACKLOG.md

# CI workflows — SIM-153 test asserts the secrets-check job lives here.
COPY .github/  /app/.github/

# Operator scripts (SIM-157 backfill, SIM-158 index acceptance, SIM-160
# bat_side audit).  test_data_engineer_sim157.py + test_perf_eng_sim158.py
# both `importlib`-load these by path and need them inside the image.
COPY scripts/  /app/scripts/

# Documentation tree — kept in the dev image so docs-existence tests
# (e.g., SIM-313, SIM-314 follow-ups) can verify the index is current.
COPY docs/     /app/docs/

# Drop privileges back to the non-root user for tests + tooling.
USER appuser

# Tests get the same default CMD as runtime; ``make test`` overrides with
# ``pytest ...`` via docker-compose run.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
