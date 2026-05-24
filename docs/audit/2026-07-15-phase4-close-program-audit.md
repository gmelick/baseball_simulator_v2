# Program Audit — Phase 4 Close (looking ahead to Phase 5)

*Conducted 2026-05-24 (sprint date 2026-07-15) · Author: Product Manager (Agent 1)*

End-of-Phase-4 program audit. The 9 agent scopes (run as 3 role-clusters + PM consolidation)
reviewed the whole project for what Phase 5 (the Backend API & Simulation Runner) needs. The
consolidated, deduped, tiered ticket list is in `docs/audit/2026-07-15-phase5-prioritized-tickets.md`.

**Headline:** Phase 4 is complete and the simulation/output/perf/betting layers are solid, but
**the entire `api/` layer is greenfield** — all six Phase-5 endpoints are unimplemented (the
router includes are commented out in `api/main.py`), there is no JSON-serialization contract for
the numpy-bearing output dataclasses, and `BatchRunner` has **no production DB-backed factory**,
so `/simulate` cannot yet run a real game. Two cross-cutting blockers stand out: the **runtime
lineup/substitution read path** (SIM-338, never built) and **serialization** — both gate the
core endpoints. The audit also re-surfaced **SIM-315 (OneDrive)** as the top standing infra risk,
and found several ⚠ defects + a set of loop-output gaps (R/H/E, fielders, W/L/S) the frontend
needs that Phase 4 didn't produce.

---

## Agent 1 — Product Manager
- The `api/` Phase-5 routers are all commented out (`api/main.py` lines ~142-148 lifespan, ~212-216 includes); Phase 5 is almost entirely greenfield wiring on top of finished Phase-4 parts.
- Two true gates emerged independently from multiple scopes: **serialization** (SIM-350) and the **lineup resolver** (SIM-353); nothing else should start before those + a real `machine_factory` (SIM-352).
- The SIM-200/201 catcher framing/blocking placeholders remain **Held** (Phase-4 Step-3b refinements); they are not Phase-5 gating — keep Held.
- Housekeeping carryover: the dead `simulator/` package, `backlog.xlsx` regen, and **SIM-315** (the OneDrive move) — the last is the biggest standing risk and cost real time on nearly every large-file edit in Phase 4.

## Agent 5 — Backend Developer
- **Gap:** 0 of 6 Phase-5 endpoints exist; only the Phase-2 `api/routes/similarity.py` explorer is wired.
- **Gap:** `ws_router`/`odds_router`/`manual_resimulate` are fully built in `pipeline/live/live_ingestion_pipeline.py` but never mounted into `api/main.py` (they only run via the pipeline's own standalone `create_app`).
- **Gap:** no JSON serialization layer — `GameSimSummary`/`WinProbability`/`StateAtPitch`/`PlayByPlay`/`OverrideDelta`/`PropDistributionSet` are dataclasses holding numpy arrays; the only existing JSON path (`simviz`) `default=str`-stringifies arrays (not a usable contract).
- **Gap:** no snapshot storage for replay — simulated pitch-level `PlayResult` lists (what `PlayByPlay.from_play_results` consumes) are never persisted; `/plays` + `/state/{ab}/{pitch}` have no backing store.
- **⚠ Gap:** no auth / rate-limit anywhere (grep = 0 hits); CORS `["*"]`; only the per-game resim cooldown exists.
- `OverrideDelta.from_summaries` is ready — the override endpoint is pure wiring.

## Agent 6 — Performance Engineer
- **⚠ Bug:** `GameSpec._hit_rate` — `rng_driven_machine_factory` reads it but the same `sim_kwargs` dict is splatted into `simulate_game(**...)`, which has no `**kwargs`, so setting it raises `TypeError`.
- **Gap:** no production DB-backed `machine_factory` — only the no-DB `rng_driven_machine_factory` exists; `BatchRunner` cannot run a real simulation yet, so the 2s/30s SLA is unverified against real engines.
- **Gap:** ProcessPool lifecycle is built for a one-shot script — `_execute` forks a fresh pool + publishes/unlinks shared memory on every `run()`; a long-lived API needs persistent pool reuse.
- **Gap:** no end-to-end API-path perf gate (the bench measures the runner, not request latency).

## Agent 3 — ML / Modeling Engineer
- **Gap:** `CalibrationReport` (SIM-346) is not loaded server-side — `api/main.py` builds only the pitcher engine; the report has no `to_json`/`from_json`, so there's no persistence format to load at boot.
- **Gap:** `win_probability`'s `CalibrationMap` defaults to identity; the fitted reliability curve (SIM-220 seam) isn't wired into the `/simulate` path.
- **Gap:** engine warmup is single-engine; the loop needs all 11; the `base_seed`→`derive_seed` reproducibility seam isn't surfaced as a request parameter.

## Agent 4 — Data Engineer
- **⚠ Bug:** `docker-compose.yml` bind-mounts `./simulator` (the empty stub) instead of `./simulation` — Phase-5 loop/runner code won't hot-reload in dev.
- **Gap (SIM-338, confirmed):** no runtime lineup/sub read path — `GameState` takes `home_lineup`/`away_lineup` as `list[int]`, but nothing reads `raw.game_lineups`/`sim.lineup_state` to build a `GameState` for `GET /simulate`. The lineup stores are Postgres-only; the loop reads DuckDB. **The single biggest data blocker for Phase 5.**
- **Gap:** no sim-result / pitch-snapshot persistence beyond the single `sim.lineup_state.simulation_results` JSONB blob; `/state` + `/plays` need a durable table.
- **Gap:** live ingestion is built but inert (commented out in `api/main.py`); the `simulation_callback` re-sim hook has no consumer. Odds are `MockOddsAPI`-only (real-provider swap needed).

## Agent 9 — QA / DevOps
- **⚠ Bug:** the coverage gate does NOT include `api/` — `pyproject.toml` source + `ci.yml --cov` omit it (only the local Makefile adds `--cov=api`), so the intended gate isn't enforced in CI.
- **Gap:** no API integration / E2E tests — `tests/integration/` covers schema/ETL/live-upsert only; no FastAPI `TestClient`, no WebSocket test, no historical-replay E2E; `api/websocket/` is an empty placeholder.
- **Gap:** no nginx / Prometheus / Grafana in the tree; no staging/prod env separation (single `.env.example`).
- **⚠ Gap:** **SIM-315 still OPEN** — `scripts/check_file_integrity.py` and `.pre-commit-config.yaml` don't exist; git is unusable from the OneDrive tree; the truncation tax hits every large Phase-5 edit.
- Good (no action): CI (lint/mypy+api/unit+regression/secrets/docker-build + weekly testcontainers/perf) and the Docker healthchecks are solid; DuckDB correctly in-process.

## Agent 7 — UX Designer
- **Gap:** the `LinescoreGraphic` (per-inning grid + R/H/E) is unservable — the loop tracks only cumulative score; there is no per-inning run breakdown, no team hits, no team errors.
- **Gap:** `FieldSnapshot` leaves all 9 defensive positions `None` unless an external map is injected — the loop doesn't track per-position fielders.
- **Gap:** the Completed game card needs winning/losing/save pitcher attribution — absent.
- **Gap:** the boxscore is thin for the card spec (no R/SB/2B/3B for batters, no hits/runs-allowed for pitchers); no per-player 100-iteration average object (must be shaped from `PropDistributionSet` means).
- OK: play-by-play scroll + pitch drill-down (`PlayByPlay`/`StateAtPitch`) and the `OverrideDelta` view are well-shaped and serializable.

## Agent 8 — Betting / Markets Analyst
- **⚠ Gap:** no spread/run-line edge report — `clv_engine` exposes moneyline/total/prop edge, but the pipeline ingests run-line odds and the CLV framework lists spread; run-line edge from the score-margin arrays is unbuilt.
- **Gap:** no line-movement / CLV-over-time surface (CLV is a single entry-vs-close snapshot); no bet-signal / +EV market-timing endpoint shape.
- **Limitation:** prop TB is a lower bound (`h+3·hr`; 2B/3B not tracked). All 7 ingested prop markets have PMFs otherwise.
- OK: the edge/EV/CLV math is clean, deterministic, and API-serializable.

## Agent 2 — Baseball Analyst
- The R/H/E and W/L/S gaps above block a domain-correct completed-game display; per-inning runs + team hits/errors + pitcher decisions are the missing realism.
- OK: RE24 / chi-squared replay validation exists; the win-prob calibration-map seam is ready for the SIM-220 isotonic fit.

---

## Cross-cutting themes (drove the consolidation)
1. **The `api/` layer is greenfield** — all six endpoints + the serialization contract + auth are unbuilt; this is the bulk of Phase 5.
2. **Two hard gates before endpoints:** the JSON-serialization contract (SIM-350) and the runtime lineup resolver (SIM-353), plus a real DB-backed `machine_factory` (SIM-352) so `/simulate` runs real games and the SLA is verifiable.
3. **Persistence gap:** simulated pitch-level history + sim results have no durable store — `/plays`, `/state`, and replay need one (SIM-356).
4. **Loop-output gaps the frontend needs** (R/H/E, fielders, W/L/S, richer boxscore) require the *loop* to produce new data, not just API wiring (SIM-362/363/364/365).
5. **Betting surface** needs spread/run-line edge + line-movement + a bet-signal contract + a real odds provider (SIM-367/368/369/370).
6. **Infra/hygiene:** SIM-315 (OneDrive) is the top risk; plus the docker-compose mount bug, the api/ coverage gap, the `_hit_rate` TypeError, and the dead `simulator/` package.

*See the prioritized ticket list for the SIM-IDs (SIM-350→377 + SIM-315 carryover), tiers, owners, sizes, and dependencies. Phase 5 entry plan: `docs/HANDOFF_PHASE5.md`.*
