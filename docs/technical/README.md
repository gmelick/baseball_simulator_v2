# Technical reference

This is the code reference for the MLB baseball simulation platform. It tells you what
each part of the code does, where a piece of logic lives, what calls it and what it
depends on. You can find and change one piece of logic without reading the whole
codebase.

## Start here

1. **[Architecture](architecture.md)**: the system on one page. The services, the data
   stores, the process model, and the three paths data takes: a simulation request,
   game day and the nightly builds.
2. **[Code review path](code-review-path.md)**: an order to read the whole codebase in.
   A one-hour tour of one simulation request, then fifteen stages from the core
   outward.
3. **[File index](file-index.md)**: every source file, in the order of the review path,
   with its size, its purpose and the tests that import it. A script writes it.
4. **[Loop cheat sheet](sim-loop-cheat-sheet.md)**: every draw the simulator makes, its
   filters, its weights and their production values, on one page.

## The area pages

| Page | Covers |
|---|---|
| [Simulation](simulation.md) | The pitch-by-pitch state machine, the sampler that makes every draw, the runner that plays 100 games, and the outputs: win odds, prop distributions, market prices. |
| [Similarity engines](similarity.md) | The eleven engines that score how alike two players or situations are, how a score becomes a draw weight, calibration and backtesting. |
| [Pipeline: the nightly builds](pipeline-batch.md) | The profile computor, the pools of real plays, the engine-artifact bundle the sampler reads, and the backups. |
| [Pipeline: getting data in](pipeline-ingest.md) | The historical loaders, the Savant loaders, the odds providers and the live game-day service. |
| [Database](database.md) | Every Postgres and DuckDB table, the migrations, the database roles, backup and restore. |
| [Betting](betting.md) | The pricing math: implied probability, margin removal, edge, expected value, closing line value, the bet signal. |
| [API](api.md) | The FastAPI app: its boot, the login, every route, the run jobs. |
| [Frontend](frontend.md) | The React app: its pages, components, API client, login and design tokens. |
| [Scripts](scripts.md) | The nightly jobs, the loaders, the pool rebuilds, the measurement tools and the probes. |
| [Tests and CI](tests.md) | The test lanes, what each proves, how to run it, and what CI runs. |

The operator's manual is [WORKFLOW.md](../../WORKFLOW.md) at the repository root. The
project guide, with the owner's standing rulings, is [CLAUDE.md](../../CLAUDE.md).
[docs/README.md](../README.md) lists every document in the repository and says which
ones are kept current.

## How to read these pages

They are plain Markdown. Read them in the Claude app, in VS Code or on GitHub. In all
three, a link to a source file opens the file.

The same pages also build into a searchable website:

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

Then open `http://127.0.0.1:8000/`. The website does not serve the source files, so a
link to a source file goes nowhere there.

## Keeping the pages current

The pages describe the code as it is today. History belongs in `CHANGES.md`.

- **A new, moved or deleted source file:** run `python scripts/docs_file_index.py`.
  The unit test `tests/unit/test_docs_file_index.py` fails until you do. If no stage
  claims the file, add a pattern to `STAGES` in that script.
- **A file's purpose:** the file index shows the first sentence of the file's own
  docstring or header comment. Fix the docstring, then rerun the script.
- **A new module or function that holds a rule:** add it to its area page, in the
  page's own format.
- **A schema version:** the pages state it as `Alembic head **NNNN**` and
  `DuckDB schema **vNN**`. Two tests fail when a page lags the newest migration.
- **An API contract change:** ship a fresh `frontend/openapi.json` and `schema.d.ts`
  in the same commit (see the [API](api.md) page).
- **A page that no longer matches the code:** rewrite the page from the code. Do not
  patch a stale page line by line.
