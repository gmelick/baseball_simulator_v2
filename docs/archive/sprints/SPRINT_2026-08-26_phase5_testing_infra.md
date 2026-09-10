# Sprint 2026-08-26 — Phase 5 Testing & Infra (E2E · SLA Gate · nginx · Monitoring) — **PHASE 5 COMPLETE** (executed 2026-05-24)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-24 · Disposition: ✅ all 4 tickets accepted after cross-validation — **PHASE 5 (Backend API & Simulation Runner) IS COMPLETE***

Sixth and final Phase-5 sprint. Closes the testing/deploy/infra tier: a real end-to-end API + WebSocket +
historical-replay test suite, the `/simulate` latency SLA gate, an nginx reverse proxy with the three env
tiers, and Prometheus + Grafana monitoring with a `/metrics` endpoint. With this, **all of SIM-350→377 +
the SIM-315 carryover are closed** and Phase 5 is done. Companion to `CHANGES.md`, `BACKLOG.md`, the prior
five sprint logs, and `docs/HANDOFF_PHASE5.md` (the entry plan).

## 1. Execution model

Two waves (file-disjoint), then orchestrator CI wiring + QA:
- **Wave 1 (3 parallel):** SIM-371 (`tests/integration/test_api_e2e_sim371.py`), SIM-372 (`tests/performance/bench_api_simulate_sim372.py`), SIM-373 (`deploy/nginx/` + docker-compose nginx service + env tiers).
- **Wave 2 (1):** SIM-374 (Prometheus/Grafana configs + docker-compose monitoring services + `api/routes/metrics.py`), building on SIM-373's compose.
- **Orchestrator:** added the CI jobs (avoiding `ci.yml` contention between the agents) + ran the cross-validation.

## 2. Tickets and results

| Ticket | Type | Owner | Result |
|---|---|---|---|
| SIM-371 | Test | QA+BA | `tests/integration/test_api_e2e_sim371.py` — TestClient E2E (no testcontainers): full game-card flow (date→simulate→plays/state/linescore/decisions/boxscore/card with cross-endpoint consistency), override flow, betting flow (edges→signals→line-movement→clv), a real WebSocket connect/ping-pong/disconnect against `ws_router`, and a deterministic historical-replay reproducibility gate. 12 tests. |
| SIM-372 | Perf+Test | Perf | `tests/performance/bench_api_simulate_sim372.py` — pytest-benchmark times the FULL `GET /{game_pk}/simulate` request path (single + 100-iter batch) via TestClient; `SINGLE_GAME_SLA_S=2.0`/`BATCH_SLA_S=30.0` enforced as a soft note in-sandbox, hard under `PERF_STRICT=1` on target hardware (auto-included in `perf-weekly.yml`). |
| SIM-373 | Infra | QA | `deploy/nginx/nginx.conf` (REST→app:8000 + `/ws/` HTTP/1.1 Upgrade for WebSockets, gzip, timeouts) + `nginx` service in docker-compose + `.env.staging.example`/`.env.production.example` (auth+rate-limit on, locked CORS, real workers/odds) + `deploy/README.md`. |
| SIM-374 | Infra | QA | `GET /metrics` (`api/routes/metrics.py`, Prometheus text exposition; `prometheus_client` optional with a stdlib fallback) — sim latency / API p95 / pipeline freshness series; `deploy/monitoring/prometheus.yml` + Grafana datasource/dashboard; `prometheus`+`grafana` services in docker-compose. 9 tests. |

**CI wiring (orchestrator):** added an `e2e` job to `ci.yml` (runs the TestClient E2E suite on every push — no live DB needed); SIM-372's bench is auto-picked up by the existing `perf-weekly.yml` (`pytest tests/performance` with `PERF_STRICT=1`), so the SLA is hard-gated on dedicated hardware.

## 3. QA cross-validation — what the independent pass caught (two real truncation casualties)

- **`docker-compose.yml` lost its `migrate` service + the top-level `volumes:` + `networks:` sections** (a file-bridge truncation during SIM-373's edit; the agent's read saw a stale view and reported it intact). The YAML still *parsed* (so a naive check would miss it), but `db`/`redis` referenced now-undefined volumes and every service referenced an undefined network. The orchestrator restored `migrate` + `volumes` + `networks` from the authoritative file — final compose has all 7 services (db/redis/app/nginx/migrate/prometheus/grafana) + 5 volumes + the network.
- **`ci.yml` lost its `file-integrity` + `docker-build-check` jobs** off the end during the `e2e`-job insertion (again still-valid YAML). Restored — final ci.yml has all 8 jobs.
- Both reinforce the standing lesson: the `.py`-only SIM-315 guard does **not** catch YAML truncation; after any infra-file edit, re-parse AND diff the structure (service/job/volume lists), not just "does it parse."

## 4. Test results

* **Unit + regression: 1870 passing / 0 failed** (1815 unit + 55 regression) — the Sprint-5 baseline of 1861
  plus 9 new (SIM-374 metrics). PLUS the new **12-test E2E integration suite** (SIM-371) and the **`/simulate`
  perf bench** (SIM-372, `--benchmark-disable` collectable + soft-gated).
* **Regression golden-files:** 55 green.
* **File integrity:** 187 `.py` files clean. All infra configs parse (ci.yml 8 jobs, perf-weekly, docker-compose
  7 services, prometheus.yml, grafana datasource + dashboard JSON).
* DuckDB schema **v10** / Postgres Alembic head **0014** (unchanged — no schema change this sprint).

## 5. 🏁 PHASE 5 — Backend API & Simulation Runner — COMPLETE

All 28 Phase-5 tickets (SIM-350→377) + the SIM-315 carryover closed across six sprints:
- **Sprint 1 — P0 gates:** SIM-350 serialization, 351 auth, 352 machine_factory, 353 lineup resolver, 354 skeleton + 375/376/377 + 315.
- **Sprint 2 — P1 endpoints + persistence:** SIM-355 `/games`+`/simulate`, 356 persistence, 357 `/plays`+`/state`, 358 override, 359 Redis TTL.
- **Sprint 3 — P1 lifecycle:** SIM-360 persistent pool, 361 calibration + 11-engine startup.
- **Sprint 4 — P2 loop outputs:** SIM-362 R/H/E, 363 fielders, 364 W/L/S, 365 boxscore+exact-TB, 366 boxscore-card API.
- **Sprint 5 — betting surface:** SIM-367 spread edge, 368 CLV/line-movement, 369 bet-signals, 370 odds provider + `/api/betting`.
- **Sprint 6 — testing/infra:** SIM-371 E2E/WS suite, 372 SLA gate, 373 nginx, 374 monitoring.

The `api/` layer (greenfield at Phase-5 entry) now serves: `/api/games/{date}`, `/{game_pk}/simulate`,
`/with_override`, `/plays`, `/state/{at_bat}/{pitch}`, `/linescore`, `/decisions`, `/boxscore`, `/card`,
`/api/betting/{edges,signals,line-movement,clv}`, `/ws/games/{game_pk}`, `/api/odds/*`, `/api/similarity/*`,
`/metrics`, `/health`, `/ready` — behind auth/rate-limit/CORS, a persistent ProcessPool runner, Redis caching,
durable sim/snapshot/game-card persistence, server-side calibration + 11 engines, an nginx reverse proxy, and
Prometheus/Grafana monitoring. Suite grew 1506 → **1870** across the phase.

## 6. Carryover & next (Phase 6 — Frontend Build)

* **Next free ID: SIM-378.**
* **Recommended Phase-6 entry (matching the project's per-phase pattern):** a 9-agent program audit to file the
  Phase-6 (Frontend Build) ticket list — the UX Designer's wireframes/design-system + the React-vs-vanilla
  decision (deferred to the Phase-6 kickoff per `agent_team.md`), building on the now-complete API contract.
  Entry plan would be `docs/HANDOFF_PHASE6.md`.
* **Live-environment verification debt (carry into Phase 6 / a staging bring-up — all code-complete, mock/unit-verified):**
  the `/simulate` 2s/30s SLA over the real DB-backed production factory (SIM-372 hard gate); the 11-engine build
  (needs DuckDB profiles); the replay/card endpoints (`REPLAY_PERSISTENCE_ENABLED=true` + a writable replay DuckDB);
  a fitted `CalibrationReport` from SIM-220; a real odds provider behind the SIM-370 seam; and an actual
  `docker compose up` of the full nginx + app + monitoring stack.
* **Standing follow-up:** extend the SIM-315 integrity guard to YAML/TOML — this sprint's two truncation
  casualties (`docker-compose.yml`, `ci.yml`) were both `.py`-guard blind spots that only a structure-diff caught.
