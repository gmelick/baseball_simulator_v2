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
FROM python:3.11-slim AS builder

# Prevent .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install system build deps needed to compile native extensions
# (psycopg2-binary ships wheels, but faiss-cpu needs libgomp)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
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
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Tell Python where to find the packages installed in stage 1
    PYTHONPATH=/app \
    PATH="/install/bin:$PATH"

# libgomp is needed at runtime for faiss-cpu
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application source (respects .dockerignore)
COPY api/         ./api/
COPY pipeline/    ./pipeline/
COPY similarity/  ./similarity/
COPY simulator/   ./simulator/
COPY db/          ./db/
COPY alembic.ini  ./alembic.ini

# Non-root user for security (principle of least privilege)
RUN useradd --system --uid 1001 --no-create-home appuser \
 && chown -R appuser:appuser /app
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

COPY requirements-dev.txt /tmp/requirements-dev.txt
# pytest + pytest-asyncio + ruff + mypy + httpx + testcontainers + ...
# The -r requirements.txt inside requirements-dev.txt re-resolves runtime
# deps, but pip is a no-op when versions already match.
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt \
 && rm /tmp/requirements-dev.txt

# Test files aren't part of the runtime image — copy them into the dev image
# so the ``app`` service can run pytest without bind-mounting the host.
COPY tests/        /app/tests/
COPY pyproject.toml /app/pyproject.toml

# Drop privileges back to the non-root user for tests + tooling.
USER appuser

# Tests get the same default CMD as runtime; ``make test`` overrides with
# ``pytest ...`` via docker-compose run.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

