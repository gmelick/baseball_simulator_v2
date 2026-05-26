# MLB Baseball Simulation Platform — End-to-End Workflow

*Last updated: 2026-05-21 (post-Phase-2 audit · URL fix)*

This document is the operator's manual.  It describes how to run the
platform end-to-end from a clean checkout, and how to confirm each
component is healthy.  Every command has a paired "what good looks
like" check so you can verify the state, not just whether the command
exited zero.

> **Shell.** All commands below are formatted for **Windows Command
> Prompt (cmd.exe)** — the user's primary shell.  PowerShell users can
> substitute `$env:VAR=…` for `set VAR=…` and `Invoke-WebRequest` for
> `curl`; bash users can substitute `export VAR=…` for `set VAR=…` and
> forward slashes for paths.

> **Phase note.** As of 2026-09-02 (executed 2026-05-25) the platform is at **Phase 6 —
> Frontend Build**.  Phases 1–5 are fully COMPLETE and CI-green (Python 3.11.15;
> 1814 pass / 0 fail / 89% coverage; DuckDB v10 / Alembic 0014).  The full API
> surface (games, simulate, betting, WebSocket, odds, similarity, metrics) is
> live.  Steps marked **[Phase 4+]** below are now implemented; this document
> will be refreshed in Sprint 2 (SIM-421) to reflect the full Phase-5 endpoint
> inventory and Phase-6 serving path.

---

## API endpoint reference (currently shipping)

The only similarity endpoint that's currently wired through the FastAPI
router (per `api/routes/similarity.py`) is the **pitcher distribution**
endpoint:

```
GET /api/similarity/pitcher/{pitcher_id}/{season}
  ?bins=20         (optional, 4-100, default 20)
  &top_n=20        (optional, 1-200, default 20)
```

**Note the path style.**  `pitcher_id` and `season` are *path* segments,
not query parameters.  An earlier draft of this doc had `/v1/similarity/pitcher?player_id=…`
which returns `{"detail":"Not Found"}` because that route doesn't exist.

Other endpoints (`/health`, `/ready`, `/`) are operational probes
defined in `api/main.py`.

---

## Part 1 — End-to-end workflow

### 1.1 Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker Desktop | latest | Required by `make dev`.  Ensure WSL2 backend is healthy. |
| Python | 3.11+ | `pyproject.toml` pins 3.11.  3.13 has no POT/faiss wheels yet. |
| Git for Windows | any | Includes Git Bash if you prefer bash semantics. |
| Make for Windows | GNU | `choco install make` or use the WSL2 alternative. |
| `curl` | any | Ships with Windows 10+. |
| `jq` (optional) | any | `choco install jq` — pretty-prints JSON. |

### 1.2 First-time setup

```bat
git clone <repo-url>
cd baseball_simulator_v2
copy .env.example .env
make dev-bg
make migrate
make test
```

**What good looks like.**
- `make migrate` ends with `alembic current` printing head revision (currently `0012`).
- `make test` exits 0.  After the recent fixes: 767 passed, ~22 skipped, 0 failed, 0 errors.

### 1.3 Local Python development (without Docker)

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest
```

You will still need PostgreSQL and Redis running somewhere — either
via `docker compose up db redis` or installed locally.

### 1.4 Loading data

```bat
set BASEBALL_DB_DSN=postgresql://baseball_user:baseball_pass@localhost:5432/baseball_sim

python pipeline\etl\etl_historical_loader.py --season 2024 --dsn %BASEBALL_DB_DSN%
python pipeline\etl\etl_sprint_speed_loader.py --season 2024
python pipeline\etl\venue_backfill_job.py
python pipeline\etl\opening_line_job.py
```

After raw load, materialize derived profiles + simulation pools into DuckDB:

```bat
python pipeline\batch\player_profile_computor.py ^
    --seasons 2022 2023 2024 ^
    --full-rebuild ^
    --dsn %BASEBALL_DB_DSN% ^
    --duckdb-path db\schemas\baseball_simulator.duckdb
```

### 1.5 Live ingestion pipeline

```bat
set LIVE_GAME_PKS=745000,745001
python pipeline\live\live_ingestion_pipeline.py
```

### 1.6 Hitting the API

With `make dev-bg` running:

```bat
:: Liveness / readiness
curl http://localhost:8000/health
curl http://localhost:8000/ready

:: OpenAPI surface
curl http://localhost:8000/openapi.json > openapi.json
:: With jq:
::   curl http://localhost:8000/openapi.json | jq ".paths | keys"

:: Pitcher similarity distribution (CORRECT ENDPOINT)
:: Path: /api/similarity/pitcher/{pitcher_id}/{season}
curl "http://localhost:8000/api/similarity/pitcher/605400/2024"

:: With histogram + top-N tuning:
curl "http://localhost:8000/api/similarity/pitcher/605400/2024?bins=30&top_n=10"
```

> **cmd URL gotcha.**  `&` is a command separator in cmd.exe.  Either
> escape as `^&` or wrap the URL in double quotes (the doc uses
> double-quoted URLs throughout).

### 1.7 Run a simulation **[Phase 4+]**

Phase 4 ships in sprint 2026-06-10 onward (see SIM-303).  Until then:

```bat
:: NOT YET IMPLEMENTED — Phase 4
curl -X POST "http://localhost:8000/api/games/745000/simulate"
:: Returns 404 until SIM-303 wires the simulation loop.
```

---

## Part 2 — Per-component health checks

### 2.1 PostgreSQL (raw + sim schemas)

```bat
docker compose exec db pg_isready -U %POSTGRES_USER% -d %POSTGRES_DB%
docker compose exec app alembic current

docker compose exec db psql -U %POSTGRES_USER% -d %POSTGRES_DB% -c "SELECT season, COUNT(*) AS pitch_count FROM raw.pitches GROUP BY season ORDER BY season;"

:: SIM-160 bat-side coverage audit:
set BASEBALL_DB_DSN=postgresql://baseball_user:baseball_pass@localhost:5432/baseball_sim
python scripts\check_bat_side_coverage.py --out docs\data_quality\2026-05-20-bat-side-coverage.md
echo %ERRORLEVEL%
```

**What good looks like.**  ~700 000 rows per fully-loaded season; `alembic current` prints `0012`; `check_bat_side_coverage.py` exits 0.

### 2.2 DuckDB analytical layer

```bat
type db\schemas\duckdb_schema_version.txt
:: Expected: 3

duckdb db\schemas\baseball_simulator.duckdb -c "SELECT * FROM migration_history ORDER BY applied_at;"

duckdb db\schemas\baseball_simulator.duckdb -c "SELECT player_id, season, framing_runs, blocking_runs, cs_rate, steal_attempt_rate_against, below_minimum_sample FROM derived.catcher_season_metrics WHERE player_id = 592663 ORDER BY season;"
```

**What good looks like.**  `steal_attempt_rate_against` ~ 0.03 for Realmuto.

### 2.3 Similarity engines

```bat
pytest tests\unit\test_engine_build_smoke.py -v
python similarity\engines\pitcher_similarity.py
python similarity\engines\catcher_similarity.py
pytest tests\unit\test_ml_engines_sim041.py tests\unit\test_ml_engines_sim042.py -v
pytest tests\regression\ -v --timeout=60
```

If regression-gate fails after intentional engine change:

```bat
python tests\regression\generate_fixtures.py --force
```

### 2.4 Live ingestion pipeline

```bat
pytest "tests\unit\test_live_pipeline_bugs.py::TestSIM132MockOddsVig" -v

docker compose exec db psql -U %POSTGRES_USER% -d %POSTGRES_DB% -c "SELECT COUNT(*) AS null_hashes FROM raw.game_odds WHERE odds_hash IS NULL;"
docker compose exec db psql -U %POSTGRES_USER% -d %POSTGRES_DB% -c "SELECT game_pk, source, odds_hash, COUNT(*) FROM raw.game_odds GROUP BY 1,2,3 HAVING COUNT(*) > 1 LIMIT 5;"

:: If either gate fails, run SIM-157 backfill:
set BASEBALL_DB_DSN=postgresql://baseball_user:baseball_pass@localhost:5432/baseball_sim
python scripts\backfill_odds_hash.py
```

### 2.5 FastAPI app

```bat
curl http://localhost:8000/health
:: Expected: {"status":"ok"}

curl http://localhost:8000/ready
:: Expected: {"status":"ready"}

:: Pitcher similarity (CORRECT path-style URL):
curl "http://localhost:8000/api/similarity/pitcher/605400/2024"

:: Surface inventory:
curl http://localhost:8000/openapi.json > openapi.json
:: Currently lists: /health, /ready, /api/similarity/pitcher/{pitcher_id}/{season}
:: Phase 5 will add /api/games/* and the WebSocket channel.
```

### 2.6 Redis cache

```bat
docker compose exec redis redis-cli ping
:: Expected: PONG
```

The similarity endpoint caches responses under
`simviz:pitcher:{id}:{season}:bins={b}:top_n={n}` (24h TTL).

### 2.7 Index acceptance (SIM-085 / SIM-089 / SIM-158)

```bat
set BASEBALL_DB_DSN=postgresql://staging-host:5432/baseball
python scripts\run_index_acceptance.py ^
    --season 2024 ^
    --pitcher-id 605400 ^
    --out docs\perf\2026-05-13-index-acceptance.md
echo %ERRORLEVEL%
```

### 2.8 Tests, lint, types

```bat
make lint
make format
make type-check

make test-unit
make test-regression
make test-integration
```

**Local-dev dep gotchas.**  A handful of test modules need optional deps:
`POT` (pitcher engine W₂), `scipy` (situation engine), `pybaseball`
(ETL loader), `pytest-asyncio` (integration).  `pip install -r requirements-dev.txt` covers all.

**Python 3.13 caveat.**  POT and `faiss-cpu` have no cp313 wheels yet.
On 3.13 the pitcher engine + FAISS engine smoke tests skip cleanly
rather than fail (the engines themselves raise a clean RuntimeError
when invoked).  Use 3.11 / 3.12 for full coverage.

### 2.9 CI pipelines

- `.github\workflows\ci.yml` — ruff, mypy, unit, regression, Docker build on every PR.
- `.github\workflows\integration-weekly.yml` — testcontainers integration suite, Monday 03:00 UTC.
- `.github\workflows\docker-release.yml` — push image to `ghcr.io` on main.

---

## Part 3 — End-to-end run (sprint-kickoff health checklist)

Run this from a clean checkout to confirm the full system boots and
every component is exercised once:

```bat
git clean -fdx -e .env -e db\schemas\baseball_simulator.duckdb
copy .env.example .env
make dev-bg
make migrate
make test
make test-integration

pytest tests\unit\test_engine_build_smoke.py -v

curl http://localhost:8000/health
curl http://localhost:8000/ready

:: CORRECT URL (path-style, /api/similarity, no /v1):
curl "http://localhost:8000/api/similarity/pitcher/605400/2024?bins=20&top_n=10"

set BASEBALL_DB_DSN=postgresql://baseball_user:baseball_pass@localhost:5432/baseball_sim
python scripts\check_bat_side_coverage.py --out docs\data_quality\2026-05-20-bat-side-coverage.md
python scripts\run_index_acceptance.py --season 2024 --pitcher-id 605400 ^
    --out docs\perf\2026-05-13-index-acceptance.md

make down
```

### 3.1 Single-line `&&`-chained equivalent

```bat
git clean -fdx -e .env -e db\schemas\baseball_simulator.duckdb && copy .env.example .env && make dev-bg && make migrate && make test && pytest tests\unit\test_engine_build_smoke.py -v && curl http://localhost:8000/health && curl http://localhost:8000/ready && curl "http://localhost:8000/api/similarity/pitcher/605400/2024" && python scripts\check_bat_side_coverage.py --out docs\data_quality\2026-05-20-bat-side-coverage.md && python scripts\run_index_acceptance.py --season 2024 --pitcher-id 605400 --out docs\perf\2026-05-13-index-acceptance.md && make down
```

### 3.2 Persisting `BASEBALL_DB_DSN`

```bat
setx BASEBALL_DB_DSN "postgresql://baseball_user:baseball_pass@localhost:5432/baseball_sim"
:: Open a NEW cmd window after setx — current windows don't see the new value.
echo %BASEBALL_DB_DSN%
```

---

## Part 4 — Where to look when something breaks

| Symptom | Where to look |
|---|---|
| `{"detail":"Not Found"}` on similarity curl | URL is wrong — use `/api/similarity/pitcher/{id}/{season}`, NOT `/v1/similarity/pitcher?…`. |
| `make migrate` fails | `db\migrations\versions\` — chain integrity; `alembic current` output. |
| Engine builds but scores degenerate | `similarity\similarity_diagnostics.py` reports COLLAPSED / NO_SPREAD flags. |
| Regression-gate fails after engine change | `python tests\regression\generate_fixtures.py --force` then commit. |
| API returns 5xx on similarity | `docker compose logs app` — engine load errors surface first. |
| Live pipeline misses pitches | Check `raw.etl_errors` — SIM-093 audits skipped rows. |
| Vig flake | Fixed in SIM-159; check `_VIG_LOWER`/`_VIG_UPPER` in `test_live_pipeline_bugs.py`. |
| Mock odds returns NULL hash | Run `python scripts\backfill_odds_hash.py`. |
| DuckDB schema mismatch | Re-apply `db\migrations\duckdb\000{1,2,3}_*.sql` in order and bump `duckdb_schema_version.txt`. |
| `curl` truncates URL | Escape `&` as `^&` or wrap the URL in double quotes. |
| `set VAR=value` doesn't persist | Use `setx VAR "value"` and open a new cmd window. |
| `make test` shows 21 errors | Integration tests can't reach Docker daemon — fixed in conftest.py; rebuild image with `make build`. |
| Pitcher engine smoke fails with `import ot` | Python 3.13 has no POT wheel — use 3.11/3.12 or `pip install POT` from source. |
| `asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "baseball_user"` even though `.env` and `docker compose exec db env` agree | **Stale `postgres_data` volume.**  `POSTGRES_PASSWORD` only takes effect on the volume's first init.  Reset the password in-band:<br>`docker compose exec db psql -U baseball_user -d baseball_sim -c "ALTER USER baseball_user WITH PASSWORD 'baseball_pass';"`<br>If that ALSO fails: `docker compose exec -u postgres db psql -c "ALTER USER baseball_user WITH PASSWORD 'baseball_pass';"`<br>Nuclear option (loses all data): `docker compose down -v && docker compose up -d db && make migrate`. |

---

## Companion documents

- `BACKLOG.md` — open tickets, sprint plans, audit follow-ups.
- `CHANGES.md` — every shipped sprint with per-ticket detail.
- `agent_team.md` — 9-agent role definitions.
- `PRODUCT_GUIDE.md` — newcomer onboarding.
- `docs\audit\2026-05-21-program-audit.md` — full audit findings.
- `docs\audit\2026-05-21-prioritized-tickets.md` — priority-ranked tier list.
- `docs\architecture\2026-05-20-play-pool.md` — Phase 3 sampler spec.
- `docs\similarity_visualization_spec.md` — similarity-explorer design + endpoint contract.
- `docs\perf\2026-05-13-index-acceptance.md` — SIM-158 acceptance harness.
- `docs\data_quality\2026-05-20-bat-side-coverage.md` — SIM-160 audit.
