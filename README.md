# MLB Baseball Simulation Platform

A pitch-by-pitch simulator of Major League Baseball games, built from real Statcast data.
For each game it plays 100 simulated games. From them it prices the player props
(strikeouts, hits, home runs and twelve more) and the game markets (moneyline, totals,
run lines and their first-inning and first-five variants). It then compares those prices
with the bookmakers' lines. The goal is to find bets with positive expected value. The
benchmark is the closing line, the market's last price before the game: the accuracy
comparison scores the model's probabilities against the closing line's on the same
outcomes.

## What it does

- **Simulates a game pitch by pitch.** Every decision in a game is a draw from a pool of
  real plays, weighted by how much the real players and situation resemble the live
  ones. The pitching change, the pickoff, the pitch, its result, the steal, the batted
  ball and the runners' extra bases are all draws. The drawn play IS the play: nothing
  adjusts it afterwards.
- **Prices fifteen player-prop markets and fifteen game markets** from the 100 games,
  and grades them against the official box score and the real line score.
- **Follows the day's games live.** The day slate reads the league schedule. Each game
  page shows the real game, the simulation, the props and the prices. A "what if" panel
  replays the rest of a game from any plate appearance with your lineup changes.
- **Includes a Data Lab**: a read-only SQL console over the raw tables, an AI assistant,
  player pages and a similarity explorer for every engine.

## How it works

1. **Data in.** Nightly jobs load each finished game from the MLB Stats API into
   Postgres (`raw.*`): every pitch, the non-pitch events, the lineups, the official box
   score and the bullpen. Other loaders bring in the Baseball Savant leaderboards and
   the bookmakers' odds.
2. **Profiles and pools.** A nightly build reads the raw tables and writes player
   profiles and pools of real plays to a DuckDB file.
3. **Similarity.** Eleven engines score how alike two pitchers, batters, fielders,
   catchers, runners, managers or situations are. The nightly build turns the scores
   into matrices.
4. **The bundle.** The nightly build exports the pools and the score matrices to a
   versioned folder of arrays on disk. The simulator reads only this bundle.
5. **The simulation.** The API queues a run of 100 games on a pool of worker
   processes. Each worker loads the bundle once and plays its games pitch by pitch.
6. **The outputs.** The runner folds the games into win probabilities, a distribution
   for each player statistic and a price for each game market. The betting code compares
   them with the bookmakers' lines.

The [architecture overview](docs/technical/architecture.md) shows the services, the data
stores and the three paths data takes. The
[loop cheat sheet](docs/technical/sim-loop-cheat-sheet.md) lists every draw and its
weights.

## Where it stands

Phases 1 to 6 are complete: the data pipeline, the eleven similarity engines, the play
pools, the simulation loop, the runner and API, and the frontend. Phase 7 (integration,
testing and deployment) is largely complete. The stack runs on one Windows laptop in
Docker, with nightly rebuilds, nightly backups and an off-site copy.

The model is still being tuned. An acceptance lane grades the simulator's frequencies
against the real plays' own totals on a balanced set of 45 games, and some bands still
read red. By the owner's ruling, no betting value is measured until every band is green.
The latest lane result and every change are in [CHANGES.md](CHANGES.md). The open work
is in [BACKLOG.xlsx](BACKLOG.xlsx), ranked.

## Quick start

You need Docker Desktop (WSL2, with about 20 GB of memory for its VM), Git, GNU Make and
Python 3.13. The commands are for Windows Command Prompt (cmd.exe).

```bat
git clone https://github.com/gmelick/baseball_simulator_v2.git
cd baseball_simulator_v2
copy .env.example .env
py scripts\gen_secrets.py --append .env
make dev-bg
make migrate
```

`gen_secrets.py` fills every password and key `.env` lacks; no password has a default.
`make dev-bg` builds and starts the stack in the background. `make migrate` applies the
database migrations.

The app can simulate only after the first data load and the first bundle build. That
takes hours; the [operator's manual](WORKFLOW.md) gives the steps in order. Then open
`http://localhost/` and log in with `AUTH_PASSWORD` from `.env`.

## Documentation

| Read | For |
|---|---|
| [Architecture](docs/technical/architecture.md) | The system on one page. |
| [Code review path](docs/technical/code-review-path.md) | An order to read the whole codebase in: a one-hour tour, then fifteen stages. |
| [File index](docs/technical/file-index.md) | Every source file, in review order, with its purpose and its tests. |
| [Technical reference](docs/technical/README.md) | One page per area: simulation, similarity, pipeline, database, betting, API, frontend, scripts, tests. |
| [WORKFLOW.md](WORKFLOW.md) | The operator's manual: bring-up, the nightly jobs, health checks, backups, troubleshooting. |
| [CLAUDE.md](CLAUDE.md) | The project guide: the owner's standing rulings, the conventions and the operating traps. |
| [agent_team.md](agent_team.md) | The nine roles of the agent team. |
| [docs/README.md](docs/README.md) | Every document in the repository, and which ones are kept current. |

## Repository layout

| Folder | What it holds |
|---|---|
| `simulation/` | The simulator: the state machine (`sim_loop.py`, `loop/`), the sampler that makes every draw (`full_pool_sampler.py`, `sampler/`), the runner and the outputs. |
| `similarity/` | The eleven similarity engines, their calibration and the backtester. |
| `pipeline/` | The loaders (`etl/`), the live game-day service (`live/`), the nightly builds (`batch/`), the odds providers and the backups (`backup/`). |
| `betting/` | The pricing math: implied probability, margin removal, edge, expected value, closing line value, the bet signal. |
| `api/` | The FastAPI app: its boot, the login, the routes, the run jobs. |
| `db/` | The Postgres and DuckDB schemas, the migrations, the database roles and the backup scripts. |
| `frontend/` | The React 18 + Vite + TypeScript app. |
| `scripts/` | The nightly shell jobs, the loaders, the pool rebuilds, the measurement tools and the probes. |
| `tests/` | The unit, regression, acceptance, integration and performance lanes. |
| `deploy/` | nginx, Prometheus and Grafana, and the job scheduler's config. |
| `docs/` | The technical reference, the decision records, the plans and the archive. |

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.13, NumPy 2, pandas, SciPy |
| Modelling | scikit-learn (the arsenal mixture models), POT (the Wasserstein distance), FAISS |
| Web | FastAPI, Pydantic v2, uvicorn |
| Data | PostgreSQL (asyncpg, SQLAlchemy, Alembic), DuckDB in-process, Redis |
| Frontend | React 18, Vite, TypeScript, Playwright |
| Operations | Docker Compose, nginx, Ofelia (the job scheduler), Prometheus, Grafana |
| Tests and CI | pytest, ruff, mypy, GitHub Actions |

## Working on the code

- Every change maps to a ticket in `BACKLOG.xlsx`. Its record goes to `CHANGES.md` when
  it closes.
- One branch per ticket, off `master`, merged within days (CLAUDE.md §7).
- Run `make test-unit`, `make lint` and `make type-check` before a commit. Run
  `make test-regression` after any engine change.
- A schema change ships a migration ([db/migrations/README.md](db/migrations/README.md)).
  An API contract change ships a fresh API snapshot. A new source file needs
  `python scripts/docs_file_index.py`.
- All prose follows the writing standard in CLAUDE.md §14: short sentences, active
  voice, plain words.
