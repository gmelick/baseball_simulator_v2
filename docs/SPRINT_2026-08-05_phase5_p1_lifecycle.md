# Sprint 2026-08-05 — Phase 5 P1 Lifecycle (Persistent Pool + Calibration Serving) (executed 2026-05-24)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-24 · Disposition: ✅ both tickets accepted after cross-validation — **Phase 5 P1 tier COMPLETE***

Third Phase-5 sprint. Closes the two remaining P1 lifecycle tickets that make the long-lived API
production-shaped: a persistent ProcessPool/shared-memory lifecycle for the simulation runner, and
server-side calibration + all-11-engine startup. With this, the **entire Phase 5 P1 tier (SIM-355→361)
is done**. The PM scoped the sprint; role specialists implemented (Performance, then ML); the
orchestrator ran the cross-validation. Companion to `CHANGES.md`, `BACKLOG.md`, and the prior sprint
logs (`docs/SPRINT_2026-07-22...`, `docs/SPRINT_2026-07-29...`).

## 1. Scope

SIM-360 (Perf) + SIM-361 (ML) — both touch the `api/main.py` lifespan, so they were run as **sequential
single-agent waves** (Perf → ML) to avoid concurrent edits to the same file rather than in parallel.

## 2. Tickets and owners

| Ticket | Type | Owner | Deliverable |
|---|---|---|---|
| SIM-360 | Perf | Performance Engineer | Persistent `ProcessPoolExecutor` reuse in `BatchRunner` + `app.state.sim_runner` lifespan + `/simulate` reuse |
| SIM-361 | Feature | ML Engineer | `CalibrationReport` JSON persistence + startup load → `CalibrationMap`; build all 11 engines at startup |

## 3. Per-ticket result

**SIM-360 — persistent ProcessPool + shared-memory lifecycle.** `BatchRunner` previously forked a fresh
`ProcessPoolExecutor` on every `run()` (via `_execute`'s `with ProcessPoolExecutor(...)`), paying
fork + shared-mem publish/unlink per request — wrong for a long-lived API. Now a `reuse_pool=True`
(default) runner lazily creates ONE warm pool and **reuses it across `run()` calls** (the SIM-333 shared-mem
segments are published once and reused); the pool is recreated only if a later call needs a different
`max_workers`, and `close()` shuts it down (`shutdown(wait=True)`) before unlinking segments (idempotent).
The `max_workers <= 1` in-process path and all one-shot / context-manager callers are unchanged
(determinism preserved). The API lifespan builds ONE long-lived `BatchRunner` as `app.state.sim_runner`
(worker count via `SIM_RUNNER_WORKERS`, default 1 = in-process so a fresh deployment stays deterministic)
and `close()`s it on shutdown; `/simulate` + `/with_override` reuse it (falling back to a transient runner
when absent, so existing tests pass). 10 new tests (real multiprocessing, asserting pool-reuse *identity*);
sim332/sim333 regression green.

**SIM-361 — calibration serving + 11-engine startup.** Added lossless JSON persistence to `CalibrationReport`
(`to_json`/`from_json` + `to_dict`/`from_dict`/`equals`, numpy-aware, schema-versioned) and a
`reliability_curve` carrier field (the seam the SIM-220 backtester fills). `CalibrationMap.from_report`
builds a monotone piecewise-linear win-prob calibration map from a fitted reliability curve (identity when
absent). `api/state.build_all_engines` builds all 11 similarity engines (driven by `ENGINE_REGISTRY`,
keyed by name) with a per-engine try/except so one bad profile table is skipped (logged), not fatal — and
is fully mockable (injectable registry/loader) for no-DB testing. The lifespan now attaches
`app.state.engines` (all 11; `app.state.pitcher_engine` kept fail-fast for the existing similarity route's
contract) and `app.state.calibration_map` (loaded from `CALIBRATION_REPORT_PATH`, defaulting to identity).
28 new tests; affected suites (api_state / sim330 / sim346 / wiring, 76 tests) green.

## 4. QA cross-validation

- Independent full-suite run from scratch (per-pattern chunks; FAISS builders individually; `test_api_main_wiring.py`
  now exceeds the sandbox's 45 s shell cap as one file — heavy `create_app()` imports — so it's run split).
- Both agents self-managed the file-bridge truncations (every `api/main.py` / `batch_runner.py` /
  `similarity_calibration.py` / `games.py` edit truncated the mount and was repaired in place with `head` +
  heredoc + `cat >`); every authoritative file verified complete (`api/main.py` ends with `app = create_app()`,
  now 477 lines).
- Live-build caveat: the real 11-engine build needs DuckDB profiles (absent in the sandbox), so it's verified
  via mocks; the resilient skip-on-failure + key-set logic is fully tested. Live verification folds into the
  SIM-352/SIM-372 live-environment work.

## 5. Test results

* **Unit + regression: 1702 passing / 0 failed** (1647 unit + 55 regression) — the Sprint-2 baseline of 1661
  plus **41 new tests** (SIM-360 10 + SIM-361 28 + 3 api wiring/games).
* **Regression golden-files:** 55 green (no engine drift).
* **File integrity:** 167 `.py` files clean.
* DuckDB schema **v9** / Postgres Alembic head **0014** (unchanged — no schema change this sprint).

## 6. Disposition & carryover

Both tickets **Closed**. **Phase 5 P1 (SIM-355→361) is COMPLETE** — the API has its full endpoint surface
plus a persistent runner pool and server-side calibration/engine startup.

* **Next free ID: SIM-378** (unchanged).
* **Live-DB caveats (code-complete, verify in a real env):** the `/simulate` 2s/30s SLA over the production
  factory with the warm pool (→ SIM-372); the 11-engine build (needs DuckDB profiles); the replay endpoints
  (`REPLAY_PERSISTENCE_ENABLED=true`); a fitted `CalibrationReport` from SIM-220 to exercise a non-identity map.
* **Next (P2):** the loop-output gaps the frontend needs — SIM-362 (per-inning R/H/E), SIM-363 (fielders),
  SIM-364 (W/L/S pitcher attribution), SIM-365 (richer boxscore + prop-TB fix), SIM-366 (boxscore-average API
  shape) — these touch the Phase-4 loop (`sim_loop.py`, the top truncation-risk file), so sequence them
  carefully; then the betting surface (SIM-367–370) and testing/infra (SIM-371–374).
* **Standing follow-up:** extend the SIM-315 integrity guard to YAML/TOML (still `.py`-only).
