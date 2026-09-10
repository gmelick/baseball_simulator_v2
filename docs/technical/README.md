# Technical Reference — start here

This is the code map for the MLB baseball simulation platform: what each file does, what calls it, and what it depends on. It exists so that if you need to change one piece of logic — say, the steal-attempt sampling logic — you can find it, see what calls it, and see what it depends on, without reading the whole codebase.

## How to read this

These are plain Markdown files — read them directly here, on GitHub, or in any editor. No setup needed.

For a nicer, searchable website built from these same pages, run from the repo root:

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

then open the printed `http://127.0.0.1:8000/` link. `mkdocs build` produces a static `site/` folder you can host anywhere.

## How this is organized

| Subsystem | Covers | Reference |
|---|---|---|
| **API Layer** | The FastAPI application: routes, request/response schemas, auth, and the engine-build lifespan. | [api.md](api.md) |
| **Simulation Engine** | The pitch-by-pitch simulator: the state machine, the similarity-weighted sampler, the batch runner, and everything that turns one plate appearance into a play. | [simulation.md](simulation.md) |
| **Similarity Engines** | The eleven similarity engines that compare the live matchup to real history, plus calibration and backtesting. | [similarity.md](similarity.md) |
| **Data Pipeline, Betting, and Database** | ETL and live ingestion, the nightly profile/artifact builders, the CLV and betting-signal engines, and the migration system. | [pipeline-betting-db.md](pipeline-betting-db.md) |
| **Operational Scripts and Frontend** | The scripts that are still run operationally (not one-off probes), plus a map of the frontend structure. | [scripts-frontend.md](scripts-frontend.md) |

## Worked example: "where is the steal-attempt sampling logic?"

This is the kind of question this reference is built to answer quickly. Short answer: `FullPoolSampler.steal_draw` in [`simulation.md`](simulation.md#simulationfull_pool_samplerpy) decides whether a runner attempts a steal and whether it succeeds. It is called from `StateMachine._steal_opportunity_draw` in `simulation/sim_loop.py`, and it draws from the steal-opportunity pool built by the nightly artifact pipeline, weighted by the runner, pitcher, and catcher similarity engines. See the full entry in `simulation.md` for the exact call chain, the pool it reads, and every helper function involved.

## Conventions used on these pages

- Every file gets a one-paragraph **purpose**, a table of its important functions, and, where relevant, the environment-variable flags it reads.
- **Called from** lists the other functions in the codebase that call this one, found by searching the whole repository, not just this file's own subsystem.
- **Depends on** lists what this function calls into or relies on.
- A **Notes** callout flags anything a new engineer must know before changing the file — a gotcha, an invariant, or a non-obvious coupling to another part of the system.
- This reference describes the code as of 2026-09-10. Re-generate the affected page after any change to the functions or call chains it documents.
