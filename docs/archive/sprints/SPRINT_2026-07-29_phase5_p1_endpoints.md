# Sprint 2026-07-29 — Phase 5 P1 Endpoints + Persistence (executed 2026-05-24)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-24 · Disposition: ✅ all 5 tickets accepted after cross-validation*

Second Phase-5 sprint. Builds the core REST surface on top of the Sprint-1 P0 gates: the
100-iteration `/simulate` runner, the games-by-date list, the managerial-override re-sim, durable
sim-result + pitch-snapshot persistence, the pitch-level `/plays` + `/state` replay endpoints, and
Redis TTL caching. The PM scoped the sprint; role subagents implemented each ticket in its owning
domain; the orchestrator ran the cross-validation. Companion to `CHANGES.md` (per-agent detail),
`BACKLOG.md` (banners), `docs/SPRINT_2026-07-22_phase5_p0_gates.md` (Sprint 1).

## 1. Scope

The P1 endpoint + persistence tier: **SIM-355, SIM-356, SIM-357, SIM-358, SIM-359**. The two
remaining P1 lifecycle tickets — **SIM-360** (persistent ProcessPool + shared-memory) and **SIM-361**
(CalibrationReport serving + 11-engine startup build) — were deferred to Sprint 3 (both are perf/ML
lifecycle work that touches `api/main.py`'s lifespan and is cleaner done together).

## 2. Plan and execution model

File ownership partitioned so no two agents touched the same file concurrently; the cross-ticket
dependency (SIM-357 needs SIM-356's persistence API + the play recorder) was handled by a two-wave
sequence:

* **Wave 1 (parallel, disjoint files):**
  - Data agent → **SIM-356** (`db/sim_store.py` + Alembic 0014 + DuckDB 0008/v8) — persistence only, no router.
  - Backend agent → **SIM-355 + SIM-358 + SIM-359** (`api/routes/games.py` run-sim endpoints + caching, mounted in `api/main.py`) + the non-invasive **play recorder** (`simulation/play_recorder.py`).
* **Wave 2 (Backend, depends on both):**
  - **SIM-357** (`/plays` + `/state` read endpoints + the record→persist flow) on top of SIM-356's API and the recorder.
* **Orchestrator integration + QA:** wired `app.state.sim_duckdb` into the `api/main.py` lifespan (gated, default-off) to make SIM-357's endpoints functional in production; then ran the full suite from scratch and audited each ticket against the files.

Subagents wrote + ran their own tests; the orchestrator's pass was the authoritative gate. Each
agent self-managed the file-bridge truncations (every `api/main.py` / `games.py` / `sim_store.py`
edit truncated the mount and was repaired in place with `head` + heredoc + `cat >`, never `mv`).

## 3. Tickets and owners

| Ticket | Type | Owner(s) | Deliverable |
|---|---|---|---|
| SIM-355 | Feature | Backend | `GET /api/games/{date}` + `GET /{game_pk}/simulate` (lineup resolver → production factory → `BatchRunner.run(100)` → `GameSimSummaryModel`) in `api/routes/games.py`; mounted in `api/main.py` |
| SIM-356 | Feature | Data | `db/sim_store.py` + Alembic 0014 (`sim.sim_runs`) + DuckDB 0008 (`sim.play_stream`, schema v8) |
| SIM-357 | Feature | Backend | `GET /{game_pk}/plays` + `GET /{game_pk}/state/{at_bat}/{pitch}` + record→persist flow; DuckDB 0009 (`sim.state_snapshots`, schema v9); `app.state.sim_duckdb` lifespan wiring |
| SIM-358 | Feature | Backend | `POST /{game_pk}/simulate/with_override` (baseline + override sims → `OverrideDelta`) |
| SIM-359 | Feature | Backend | Redis TTL caching: `app.state.sim_cache` (Redis-optional, InMemory fallback); `/simulate` 60s, listing 300s |

## 4. Per-ticket result

**SIM-355 — /games/{date} + /simulate.** New `api/routes/games.py` (prefix `/api/games`). `GET /{date}`
lists `raw.games` for a date. `GET /{game_pk}/simulate?n_iterations=100&base_seed=` builds the GameState
via the SIM-353 lineup resolver, assembles a `GameSpec` over the production machine factory, runs the
SIM-332 `BatchRunner`, and returns a `GameSimSummaryModel` (SIM-350). The machine factory is an
overridable testability seam (`PRODUCTION_FACTORY_REF` / `resolve_factory_ref` reading `app.state.sim_factory_ref`
→ `$SIM_MACHINE_FACTORY_REF` → the default), so tests inject the no-DB rng factory. The run is offloaded
via `asyncio.to_thread` to keep the event loop responsive. Mounted in `api/main.py`.

**SIM-356 — persistence.** `db/sim_store.py` exposes a mockable read/write API: Postgres (asyncpg)
`store_sim_run`/`load_latest_sim_run`/`load_sim_run`/`list_sim_runs` over `sim.sim_runs` (JSONB summary
history, Alembic 0014); DuckDB `store_play_stream`/`load_play_stream` over `sim.play_stream` (one row per
pitch, schema 1:1 with `PlayByPlayEntry`, DuckDB migration 0008, schema version 7→8). Replaces the
ephemeral `sim.lineup_state.simulation_results` blob. The play-row schema is documented in the module so
SIM-357 writes matching rows. 19 tests (real in-memory DuckDB round-trip + stubbed asyncpg + migration sanity).

**SIM-357 — /plays + /state + record→persist.** Two read endpoints serve the persisted data: `/plays`
rebuilds a `PlayByPlay` from `load_play_stream`; `/state/{at_bat}/{pitch}` returns a `StateAtPitch` from
the persisted per-pitch snapshot. The write path: `/simulate` best-effort records ONE representative game
at the run's `base_seed` (via the play recorder), persists its play-stream + per-pitch `StateAtPitch`
snapshots (built from each `PlayResult.next_state`) + the sim-run summary — wrapped so a persistence
failure never breaks `/simulate`. Added a state-snapshot store to `db/sim_store.py` + DuckDB migration 0009
(`sim.state_snapshots`, schema version 8→9). 14 tests. **The play recorder** (`simulation/play_recorder.py`,
delivered with Wave 1) is a non-invasive `RecordingMachine` wrapper + `record_game_plays(...)` that captures
a game's `PlayResult` stream **without touching the 2680-line `sim_loop.py`**. 10 tests.

**SIM-358 — /simulate/with_override.** `POST /{game_pk}/simulate/with_override` takes a `RosterOverride`
body (lineup/pitcher/bat-hand swaps), runs a baseline + an override sim at the **same base_seed** for
comparability, and returns both summaries plus an `OverrideDelta` (SIM-331) serialized via `OverrideDeltaModel`.

**SIM-359 — Redis TTL caching.** `app.state.sim_cache = make_cache()` (RedisCache if a server answers,
else InMemoryCache — no-op-safe). The games router's `BatchRunner` memoizes sim summaries at
`SIM_RESULT_TTL_S` (60s) and the date listing at `POOL_QUERY_TTL_S` (300s); `?use_cache=false` disables
per request; every cache call is wrapped so a cache hiccup never breaks a response.

## 5. QA cross-validation — what the independent pass did

- **Integration completion:** the SIM-357 agent (correctly, per file ownership) left the `app.state.sim_duckdb`
  attach undone in `api/main.py`. The orchestrator added a gated, best-effort lifespan attach behind
  `REPLAY_PERSISTENCE_ENABLED` (default off) — completing the production wiring without risking DuckDB's
  single-writer constraint against the read-only similarity engine by default. (Live-DB verification of the
  replay endpoints folds into the Sprint-1 SIM-352 / future SIM-372 live-environment work.)
- **Six file-bridge truncations** were repaired across the sprint (the agents handled their own; the
  orchestrator repaired `api/main.py` after the lifespan edit). Every authoritative file was complete.
- **Full suite from scratch:** unit + regression in per-pattern chunks, FAISS builders individually.

## 6. Test results

* **Unit + regression: 1661 passing / 0 failed** (1606 unit + 55 regression) — the Sprint-1 baseline of
  1603 plus **58 new tests** (SIM-356 19, SIM-355/358/359 14, recorder 10, SIM-357 15 incl. the 0009 sanity).
* **Regression golden-files:** 55 green (no engine drift).
* **File integrity:** 165 `.py` files clean.
* DuckDB schema **v9** (migrations 0008 play_stream + 0009 state_snapshots); Postgres Alembic head **0014**
  (`sim.sim_runs`).

## 7. Disposition & carryover

All five tickets **Closed**. The core endpoint surface is live: `/games/{date}`, `/simulate`,
`/simulate/with_override`, `/plays`, `/state/{at_bat}/{pitch}`, with caching + durable persistence.

* **Next free ID: SIM-378** (unchanged — no new tickets filed).
* **Live-DB caveats (code-complete, verify in a real environment):** `/simulate` over the production factory
  (the SIM-352 SLA, → SIM-372) and the replay endpoints once a writable replay DuckDB is wired
  (`REPLAY_PERSISTENCE_ENABLED=true` + a dedicated DuckDB file to avoid the single-writer clash).
* **Remaining P1 (Sprint 3):** SIM-360 (persistent ProcessPool + shared-memory lifecycle) + SIM-361
  (CalibrationReport JSON persistence + API-startup load + all-11-engine build). Then P2: the loop-output
  gaps (SIM-362–365: R/H/E, fielders, W/L/S, richer boxscore) and the betting surface (SIM-367–370).
* **Standing follow-up:** extend the SIM-315 integrity guard to YAML/TOML (still `.py`-only).
