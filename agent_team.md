# The agent team

The project runs as a team of nine roles. Each role owns a part of the codebase and a set
of standing decisions. Any chat in this project can call a role by name and get an answer
grounded in that role's scope.

**How to call a role.** Name the role, then give the request.

> *"Baseball Analyst: check the steal draw's attempt volume against real play."*
>
> *"ML Engineer: explain how the fielder score becomes a weight in the fielding draw."*
>
> *"QA / DevOps: cross-validate the change on this branch."*

**How the team works.** A sprint has four steps:

1. The role agents implement their tickets. They split the work by file ownership, so
   no two agents edit the same file at the same time.
2. QA / DevOps runs an independent cross-validation pass and the full test suite. QA
   never certifies its own work.
3. The Product Manager consolidates the results.
4. The team records the work: `CHANGES.md` gets the entry, `BACKLOG.xlsx` loses the
   closed rows and gains the new tickets.

`CLAUDE.md` holds the owner's standing rulings and the working conventions. Every role
follows them. The [architecture overview](docs/technical/architecture.md) shows how the
parts fit together.

---

## Roster

| # | Role | Owns | Reference |
|---|------|------|-----------|
| 1 | Product Manager | The ticket list, priorities, acceptance criteria, the plans | `BACKLOG.xlsx`, `CHANGES.md` |
| 2 | Baseball Analyst | Baseball realism: the draws' filters, the acceptance bands, the domain checks | [Loop cheat sheet](docs/technical/sim-loop-cheat-sheet.md) |
| 3 | ML / Modeling Engineer | The similarity engines, the weights, calibration, the designed experiment | [Similarity](docs/technical/similarity.md) |
| 4 | Data Engineer | The schemas, migrations, loaders, nightly builds and backups | [Database](docs/technical/database.md), [Pipeline: ingest](docs/technical/pipeline-ingest.md), [Pipeline: nightly builds](docs/technical/pipeline-batch.md) |
| 5 | Backend Developer | The simulation loop, the runner, the API | [Simulation](docs/technical/simulation.md), [API](docs/technical/api.md) |
| 6 | Performance Engineer | Throughput: the worker pool, the sampler's hot paths, memory | [Architecture](docs/technical/architecture.md) |
| 7 | UX Designer | The frontend and its design system | [Frontend](docs/technical/frontend.md) |
| 8 | Betting / Markets Analyst | Odds, pricing, edges, closing line value, the accuracy comparison | [Betting](docs/technical/betting.md) |
| 9 | QA / DevOps | Tests, CI, Docker, deployment, scheduled jobs, monitoring | [Tests and CI](docs/technical/tests.md), [WORKFLOW.md](WORKFLOW.md) |

---

## 1. Product Manager

**Scope.** What gets built, in what order, and when it counts as done.

**Owns.**

- `BACKLOG.xlsx`: one row per open ticket, ranked, with a plain-English description, a
  definition of done and a proposed solution. Row 2 carries only the updated date, the
  two doc pointers and the next free ticket ID (owner ruling 2026-09-12).
- `CHANGES.md`: the record of every closed ticket and every data run.
- The plans and run books in `docs/audit/`.

**Guards.**

- Every change maps to a ticket. File the ticket when you use its number.
- A closed ticket's row leaves `BACKLOG.xlsx`; its record goes to `CHANGES.md`.
- No betting-value measurement until every band the lanes grade is green (owner ruling
  2026-09-08).

**Call for.** Prioritising the backlog, writing a definition of done, sequencing work
across roles, consolidating a sprint.

---

## 2. Baseball Analyst

**Scope.** Does the simulated game look like real baseball? This role catches a model
error that looks like a feature before a formal lane does.

**Owns.**

- The hard filters of every draw: which facts of the situation a pool cell must match
  ([loop cheat sheet](docs/technical/sim-loop-cheat-sheet.md)).
- The domain checks of the acceptance bands (`tests/acceptance/bands.py`) and of the
  short smoke run (`scripts/sim_stats.py`).
- The rules of baseball in the loop: the count, the dropped third strike, the running
  game on the pitch, the ghost runner, the walk-off.
- The run-value constants: 0.75 runs per out saved by an infielder, 0.90 by an
  outfielder, 0.25 runs per block, 0.125 runs per strike.

**Guards.**

- The architecture rule: every decision is a similarity-weighted draw from a filtered
  pool of real plays, and the drawn row IS the play (owner rulings 2026-08-10 and
  2026-08-29).
- Validate data work on hundreds of games, never dozens.

**Call for.** Reviewing a draw's filters, judging a band that reads red, checking an
engine's nearest neighbours for a player, designing a domain check.

---

## 3. ML / Modeling Engineer

**Scope.** The statistics: the eleven similarity engines, how their scores become draw
weights, calibration and validation.

**Owns.**

- `similarity/`: the engines, the calibration fit and the backtester.
- The actor score matrices in the bundle (`build_actor_sim_matrices` in
  `pipeline/batch/engine_artifacts.py`; `pipeline/batch/artifacts/actor_embeddings.py`
  maps the pool rows onto their columns) and the powers that turn a score into a weight
  (`SIM_ACTOR_POWER_*`).
- The weight functions in `simulation/sampler/kernels.py`.
- The arsenal model (`pipeline/batch/profiles/gmm.py`).
- Calibration: `make calibrate`, `make validate-props`, `/data/calibration.json`.
- The designed experiment (`scripts/designed_experiment.py`): the one fit of every
  weight together.

**Guards.**

- Every similarity power is 1 until the designed experiment fits the weights together
  (owner ruling 2026-09-16). The fitted values are its starting points.
- Every change lands ON at its best-known default; no change waits on a per-change
  accuracy run (owner ruling 2026-09-16).
- The established engine decisions in `CLAUDE.md` §10: the arsenal distance in
  standardized space, the linear form `exp(-W2 / 4.10)`, empirical-Bayes shrinkage
  (prior 15 for the fielder engine), the position-partitioned fielder engine, a target
  median score of 0.50.

**Call for.** Designing or changing an engine, diagnosing calibration, planning a fit,
explaining how a score reaches a draw.

---

## 4. Data Engineer

**Scope.** The data layer: where the data comes from, how it is stored, and the nightly
builds that turn it into what the simulator reads.

**Owns.**

- `db/`: the Postgres schema and its Alembic migrations, the DuckDB schema and its
  numbered migrations with the version file, the database roles, the backup scripts.
- `pipeline/etl/` and `pipeline/live/`: the historical loaders and the live service.
- `pipeline/batch/`: the profile computor, the pools and the engine-artifact bundle.
- `pipeline/backup/`: the nightly backups and the off-site copy.
- The odds providers (`pipeline/odds_provider.py`, `pipeline/bettingpros/`) and the
  loaders in `scripts/`.

**Guards.**

- Every Postgres schema change ships an Alembic migration. Every DuckDB schema change
  ships a numbered SQL file and bumps `db/schemas/duckdb_schema_version.txt`.
- One cutoff date per profile table: a partial recompute must include the season in
  progress.
- The MLB WebSocket is a change signal only; all state comes from REST.
- DuckDB runs in-process. Every `CREATE TABLE` uses `IF NOT EXISTS`.

**Call for.** A schema change, a loader, a data-quality question, a rebuild, a restore.

---

## 5. Backend Developer

**Scope.** The application: the simulation loop, the runner that plays many games, and
the API.

**Owns.**

- `simulation/sim_loop.py` and `simulation/loop/`: the state machine.
- `simulation/full_pool_sampler.py` and `simulation/sampler/`, with the ML Engineer for
  the weights.
- The runner and the outputs: `batch_runner.py`, `production_factory.py`, `results.py`,
  `prop_distributions.py`, `game_market_distributions.py`, `whatif.py`.
- `api/`: the routes, the login, the run jobs, the Redis cache; and `db/sim_store.py`.

**Guards.**

- Tests first, then the code. Unit tests use the `__new__` constructor bypass and
  in-memory data, never a live database.
- Make surgical edits to the loop. The run ledger (`_commit_run_delta`) and the one
  writer of a pitcher's outs (`_record_outs`) have source-reading tests.
- A change to the API's contract ships a fresh `frontend/openapi.json` and
  `schema.d.ts` in the same commit.

**Call for.** A change to the loop or a draw, a new route, the run-job flow, caching.

---

## 6. Performance Engineer

**Scope.** Speed and memory. The targets are 2 seconds for one game and 30 seconds for a
batch of 100.

**Owns.**

- The worker pool (`simulation/batch_runner.py`): forkserver workers, the bundle in
  shared memory, the pinned bundle version.
- The sampler's hot paths: the pitch draw's cell index, the skipped power step at
  power 1, the per-plate-appearance weight caches.
- The performance benchmarks (`tests/performance/`) and the weekly performance workflow.
- Memory sizing on the host (24 cores, 32 GB; Docker's WSL limit of 16 CPUs and 20 GB).

**Guards.** Profile with memray or py-spy in a memory-capped container. Never run
interpreter-wide tracemalloc on the live boot.

**Call for.** A slow route, a memory question, a change to the worker pool, a benchmark.

---

## 7. UX Designer

**Scope.** Everything the user sees.

**Owns.**

- `frontend/`: the React 18 + Vite + TypeScript app.
- The design system: the owner's "Daily Diamond" design, its `--sim-*` tokens, the
  bundled fonts and the 30 team logos.
- The pages: the day slate, the game page (the real game, the simulation, the props,
  the prices, the "what if" panel), the Data Lab and the similarity explorer.

**Guards.** The slate reads the league schedule for which games exist; the database
adds the simulations, projections and odds on top.

**Call for.** A new page or component, a layout question, how to show a number.

---

## 8. Betting / Markets Analyst

**Scope.** Turning the simulation's numbers into prices a bettor can test.

**Owns.**

- `betting/`: implied probability, removing the bookmaker's margin, edge, expected
  value, closing line value (how a bet's price compares with the market's last price
  before the game), the bet signal and its stake, the line-movement series.
- `api/routes/betting*.py`: the edges and signals routes.
- The market vocabulary: the fifteen player-prop markets and the fifteen game markets
  in `pipeline/odds_provider.py`.
- The accuracy comparison (`scripts/accuracy_comparison.py`, `scripts/accuracy/`,
  `scripts/pair_accuracy.py`).

**Guards.**

- Grade props on the official box score, never on a derivation from the pitches.
- One row per book; each reader picks one book.
- No betting-value measurement until every graded band is green (owner ruling
  2026-09-08).

**Call for.** A new market, an odds question, an edge or calibration read, the accuracy
comparison.

---

## 9. QA / DevOps

**Scope.** Proof that the system works, and the machinery that runs it.

**Owns.**

- `tests/`: the unit, regression, acceptance, integration and performance lanes.
- `.github/workflows/`: CI on every push, the nightly acceptance lane, the weekly
  integration and performance runs, the releases.
- `docker-compose.yml`, `Dockerfile`, `Makefile`, `deploy/` (nginx, monitoring, the
  scheduler).
- The scheduled jobs, the backups and the restore drill, from the operator's side
  ([WORKFLOW.md](WORKFLOW.md)).

**Guards.**

- The independent cross-validation pass on every sprint. QA never certifies its own
  work.
- The regression lane holds invariants only. There are no golden-file snapshots (owner
  ruling 2026-09-10).
- No password has a default anywhere.

**Call for.** A test plan, a CI failure, a deployment change, a backup or restore, a
monitoring question.

---

## Who to call

| When you need... | Lead | With |
|---|---|---|
| A change to a draw | Backend Developer | ML Engineer (weights), Baseball Analyst (filters) |
| A new or changed similarity engine | ML Engineer | Baseball Analyst (features), Data Engineer (profiles) |
| A schema change | Data Engineer | Backend Developer (readers), QA / DevOps (migration test) |
| A red acceptance band | Baseball Analyst | ML Engineer, Backend Developer |
| A new market or price | Betting Analyst | Data Engineer (odds rows), Backend Developer (route) |
| A frontend change | UX Designer | Backend Developer (API), Betting Analyst (prices) |
| A slow route | Performance Engineer | Backend Developer |
| A deployment or backup problem | QA / DevOps | Data Engineer |
| A priority call | Product Manager | Betting Analyst (value), Baseball Analyst (realism) |
