> Stale snapshot — superseded; repo moved off OneDrive; migration numbers here are obsolete (live head is Alembic 0015 / DuckDB v13). See CLAUDE.md for current status.

# Phase 5 Handoff — Backend API & Simulation Runner

*Author: Product Manager (Agent 1) · Date: 2026-05-24 · Phase 4 closure*

---

## TL;DR

**Phase 4 (Core Simulation Loop) is COMPLETE** (5 sprints, SIM-310→349 + SIM-220). The
simulator runs full games end-to-end and emits every output contract the API needs. Test
suite: **1505 unit+regression passing / 1 skipped / 0 failed**; performance **5 passed / 0
skipped**; DuckDB schema **v7** (Postgres Alembic head **0013**). All six audit ⚠ live bugs are
fixed.

**Phase 5 = the backend API + WebSocket + the 100-iteration runner endpoint + the managerial-
override endpoint.** The simulation/aggregation/perf/betting layers it wraps already exist;
Phase 5 is application-layer wiring (FastAPI / Redis / WebSocket), not new simulation logic.

---

## 1. What Phase 4 leaves you (build on these, don't rebuild)

- **Loop:** `simulation/sim_loop.py` — `simulate_game(state, seed) -> GameSimResult`; full
  8-step loop + manager/situational decisions (SIM-323/349).
- **Contracts:** `simulation/game_state.py` (`GameState`/`PlayResult`/`ManagerContext`),
  `simulation/results.py` (`GameSimResult`, `GameSimSummary.from_results`, `BoxScore`/`PlayerStatLine`).
- **Runner:** `simulation/batch_runner.py` — `BatchRunner` (`ProcessPoolExecutor(min(cpu-1,10))`,
  per-game seed isolation, Redis-TTL cache with in-memory fallback, SIM-333 shared-memory attach).
  This is the substrate for the `GET /simulate` 100-iteration endpoint.
- **Outputs:** `simulation/win_probability.py` (SIM-330), `simulation/prop_distributions.py`
  (SIM-329 PMFs), `simulation/snapshots.py` (SIM-331 `FieldSnapshot`/`PlayByPlay`/`StateAtPitch`/
  **`OverrideDelta`** — the override endpoint's diff contract), `betting/clv_engine.py` (SIM-339).
- **Validation:** `similarity/backtesting/backtester.py` (SIM-220),
  `simulation/validation/replay_chi_squared.py` (SIM-325).
- **Live ingestion (already exists):** `pipeline/live/live_ingestion_pipeline.py` —
  `GameStateBuilder`, `ConnectionManager`, odds/prop persistence + closing-line marking, the
  re-sim trigger + cooldown; `ws_router`/`odds_router` live here. `api/websocket/` is an empty
  placeholder. (Now real-tested by SIM-348.)

## 2. Phase 5 scope (from `agent_team.md` Backend Developer)

REST + WebSocket endpoints (Phase 5 routers are currently commented out in `api/`):
- `GET /api/games/{date}` — all games for a date
- `GET /api/games/{game_pk}/simulate` — run 100 simulations from current state (→ `BatchRunner` → `GameSimSummary`)
- `GET /api/games/{game_pk}/state/{at_bat}/{pitch}` — point-in-time state (→ SIM-331 `StateAtPitch`)
- `GET /api/games/{game_pk}/plays` — pitch-level play-by-play (→ SIM-331 `PlayByPlay`)
- `POST /api/games/{game_pk}/simulate/with_override` — re-sim with a modified roster (→ SIM-331 `OverrideDelta`, baseline-delta-comparable)
- `WS /ws/games/{game_pk}` — live game-state push channel
Plus: Redis TTL caching (sim 60s / pool 5-min / odds 5-min — the `SimCache` from SIM-332 is the
pattern), game-state snapshot storage for pitch-level replay, and the betting/CLV surface.

## 3. Recommended first step: a Phase-5 program audit

As at the Phase-3→4 boundary, run a **9-agent program audit** to file the Phase 5 ticket list
(next free ID is **SIM-350**) before coding — the agent_team endpoints above are the scope, but
sizes/owners/deps/gates should be a filed, tiered list. Likely P0 gates: an API contract/router
skeleton, the `GameSimSummary`→JSON serialization contract, and the auth/rate-limit baseline.

## 4. Gotchas carried into Phase 5 (read before touching the tree)

- **OneDrive truncation (SIM-315) is the #1 hazard and still OPEN.** It corrupted large files on
  nearly every edit across Phase 4 (`sim_loop.py` ~2,680 lines, the 4,400-line computor,
  `pyproject.toml`, `.gitignore`). Mitigation recipe: edit via the file tools, then repair the
  sandbox MOUNT copy (`head -n <last-good>` + append the authoritative tail; `tr -d '\000'`),
  `py_compile` to verify. **Prioritize actioning SIM-315 early in Phase 5.**
- **Sandbox tests:** Python 3.10 (project targets 3.11); install deps incl. `pytest-asyncio`
  (the API tests are async, `asyncio_mode=auto`); create the `datetime.UTC` shim + a writable
  pyc dir (both cleared when the sandbox cycles); run the suite in per-pattern chunks (the full
  not-slow run exceeds the 45 s shell cap). See `docs/` sprint logs / project memory.
- **Migrations:** Postgres → Alembic (head 0013); DuckDB → numbered SQL + bump
  `db/schemas/duckdb_schema_version.txt` (now 7). `DROP INDEX` must be schema-qualified.
- **`backlog.xlsx`** is regenerated from `BACKLOG.md` and is often locked open — don't assume
  it's writable; note it needs regen.

## 5. Small Phase-4 follow-ups (not gating)

- `simulation/batch_runner.py` `GameSpec._hit_rate` is a dead knob (the factory reads it but
  `simulate_game` rejects it as a kwarg) — fix or remove (SIM-347 finding).
- SIM-329 prop TB is a lower bound (`h + 3·hr`) until 2B/3B are tracked in the `BoxScore`.
- The empty `simulator/` package (vs the real `simulation/`) can be removed for clarity.

---

*End of handoff.*
