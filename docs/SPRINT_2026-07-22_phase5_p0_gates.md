# Sprint 2026-07-22 — Phase 5 P0 Gates (Backend API & Simulation Runner) (executed 2026-05-24)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-24 · Disposition: ✅ all 9 tickets accepted after cross-validation*

First Phase-5 sprint. Lands the five P0 gates that unblock real API endpoints plus the three ⚠
hygiene bugs and the SIM-315 file-integrity carryover. The `api/` layer was greenfield going in
(all six Phase-5 endpoints unbuilt, no JSON contract, no auth, no production sim factory); this
sprint builds the foundation the P1 endpoint tickets (SIM-355→361) sit on. The PM scoped the
sprint from the Phase-4-close audit; role subagents implemented each ticket in its owning
domain; the orchestrator ran an independent cross-validation. Companion to `CHANGES.md`
(per-agent detail), `BACKLOG.md` (banners), `docs/HANDOFF_PHASE5.md` and the audit docs.

## 1. Plan and execution model

File ownership was partitioned so no two agents touched the same file — the standing serialization
hazards (`api/main.py`, `simulation/batch_runner.py`, `ci.yml`) were each assigned to a single
agent that sequenced its own tickets. Two waves of real subagents, then an orchestrator-run QA pass:

* **Wave 1 (3 agents, file-disjoint):** SIM-352+SIM-377 (Perf/ML — `batch_runner.py` + new factory);
  SIM-353 (Data — new resolver + migration check); SIM-350 (Backend/UX — serialization, new `api/` files).
* **Wave 2 (2 agents):** SIM-354+SIM-351 (Backend — `api/main.py` + new `api/auth.py`); SIM-376+SIM-315+SIM-375 (QA — `ci.yml`/`pyproject.toml`/`docker-compose.yml` + new integrity script).
* **QA cross-validation (orchestrator):** repaired mount truncations, installed the sandbox deps,
  ran the full unit+regression suite from scratch, audited each ticket against the actual files.

Subagents implemented + wrote tests but did **not** run the full suite (no FastAPI/pydantic/numpy
in their sandboxes) — verification was centralized in the QA pass, which is exactly where the two
real bugs below were caught.

## 2. Tickets and owners

| Ticket | Type | Owner(s) | Deliverable |
|---|---|---|---|
| SIM-350 | Spec | Backend + UX | `api/serialization.py` (`to_jsonable`) + `api/schemas.py` (Pydantic v2 models + `from_dataclass`) + `tests/unit/test_api_schemas.py` |
| SIM-351 | Feature | Backend + QA | `api/auth.py` (`require_api_key`, `RateLimitMiddleware`, `resolve_cors_origins`) + `api/main.py` + `tests/unit/test_api_auth.py` |
| SIM-352 | Feature | Backend + Perf + ML | `simulation/production_factory.py` (`production_machine_factory`) + `tests/unit/test_perf_eng_sim352.py` |
| SIM-353 | Feature | Data + Backend | `simulation/lineup_resolver.py` + `tests/unit/test_lineup_resolver.py` |
| SIM-354 | Gap | Backend + Data | `api/main.py` (mount `ws_router`/`odds_router` + gated live pipeline) + `tests/unit/test_api_main_wiring.py` |
| SIM-375 | Bug | Data + QA | `docker-compose.yml` (mount `./simulation`) + `Dockerfile` (`COPY simulation/`+`betting/`) + deleted `simulator/` |
| SIM-376 | Bug | QA | `pyproject.toml` (`source += api`) + `ci.yml` (`--cov=api`) |
| SIM-377 | Bug | Backend/Perf | `simulation/batch_runner.py` (`_run_one` underscore-key filter) + `tests/unit/test_perf_eng_sim377.py` |
| SIM-315 | Infra | QA | `scripts/check_file_integrity.py` + `.pre-commit-config.yaml` + `ci.yml` job + `tests/unit/test_check_file_integrity.py` |

## 3. Per-ticket result

**SIM-350 — serialization contract.** `api/serialization.py` provides `to_jsonable`, a recursive
numpy/dataclass/enum/datetime → JSON-native converter; `api/schemas.py` provides Pydantic v2
response models with `from_dataclass` converters for every Phase-4 output dataclass
(`GameSimSummary`/`ConfidenceInterval`, `BoxScore`/`PlayerStatLine`, `WinProbability`/`CalibrationMap`,
the six `snapshots` types, `PropDistribution(Set)`, `EdgeReport`/`CLV`). No source dataclass was
modified. Large per-iteration score arrays are exposed in full by default, with an explicit opt-in
trim (`include_raw_arrays=False` / `GameSimSummaryLite`). **(See §4 — QA caught a real bug here.)**

**SIM-351 — auth + rate-limit + CORS.** `api/auth.py`: `require_api_key` (X-API-Key vs `API_KEYS`,
no-op in dev / when unconfigured, 401 otherwise); a pure-stdlib sliding-window `RateLimitMiddleware`
(`RATE_LIMIT_PER_MINUTE`/`RATE_LIMIT_ENABLED`, 429 + `Retry-After`, ops paths exempt, off by default);
and `resolve_cors_origins` (`CORS_ORIGINS` → `FRONTEND_URL` → dev-only `*`) eliminating the prior
unconditional `["*"]` so production is never wildcard-with-credentials. No new third-party deps.

**SIM-352 — production DB-backed machine_factory.** `simulation/production_factory.py` adds the
picklable, dotted-ref-able `production_machine_factory(seed, spec) -> StateMachine` that builds a real
`PlayPoolSampler`-driven machine and attaches the SIM-333 shared tiles zero-copy when
`spec.shared_segments` is set. Sampler/deriver construction sits behind injectable module-level builder
hooks (`set_sampler_builder` / `use_sampler_builder`) so it is unit-testable with no live DuckDB/FAISS;
the seed threads reproducibly into both the loop rng and the sampler's k-NN rng. Full live-DB
acceptance is deferred to a real environment (sandbox has no Postgres/DuckDB) — verified here via the
project's mock/`__new__`-bypass pattern.

**SIM-353 — runtime lineup/substitution resolver.** `simulation/lineup_resolver.py` adds async
asyncpg readers (`resolve_lineup` / `resolve_game_state`) over `raw.games` + `raw.game_lineups` +
`raw.players`, plus a pure, DB-free assembly/build layer (`resolve_lineup_from_rows` → `build_game_state`)
that maps a resolved lineup into a `GameState` via its public fields (ordered `home/away_lineup`, slot
pointers, leadoff `batter_id`, defending `pitcher_id`, `bat_hand`/`throw_hand`/`season`). Substitutions
resolve by highest `sequence` per slot with optional `as_of_at_bat` rewind; pitching changes resolve
independently. **No migration needed** — `raw.game_lineups` already exists from migration 0001 with all
required columns (the audit's "deferred / may not exist" note was stale; Alembic head stays 0013).

**SIM-354 — mount the API skeleton.** `api/main.py` now includes `ws_router` (`/ws/games/{game_pk}`)
and `odds_router` (`/api/odds/*`) unconditionally (route registration needs no live connection), and the
background `LiveIngestionPipeline` is gated behind `LIVE_PIPELINE_ENABLED` (default false) in the
lifespan — wiring the `simulation_callback` re-sim hook and a clean `stop()` on shutdown — so the app
boots for tests without an MLB WebSocket.

**SIM-375 — docker-compose mount + dead package.** The dev hot-reload bind mount now points at the real
`./simulation:/app/simulation` (was the empty `./simulator` stub). `Dockerfile` updated to
`COPY simulation/` (+ `COPY betting/`, which the now-mounted API imports) so the runtime image is
importable. The dead `simulator/` package (2 `__init__.py` files, zero importers) was deleted, and its
stale references were removed from `pyproject.toml`'s ruff config.

**SIM-376 — api/ coverage gate.** `"api"` added to `[tool.coverage.run] source` (pyproject.toml) and
`--cov=api` to the CI unit-test job, so the FastAPI app is enforced by the 80% CI gate (previously only
the local Makefile measured it). Gate threshold unchanged.

**SIM-377 — `_hit_rate` TypeError fix.** `_run_one` now filters `_`-prefixed `sim_kwargs` keys out of
the `simulate_game(**...)` splat (which has a fixed signature, no `**kwargs`), establishing the
documented "underscore keys are factory-only" convention. A `GameSpec` carrying `_hit_rate` runs end-to-
end through `BatchRunner.run` with no error, and the knob still reaches the factory's resolver.

**SIM-315 — file-integrity guard (OneDrive truncation remediation, Option B).** `scripts/check_file_integrity.py`
(pure stdlib) `ast.parse`s every tracked `.py` and scans for null bytes, exiting non-zero on any
truncated/corrupt file; wired into `.pre-commit-config.yaml` (local hook) and a fast `File integrity
(SIM-315)` CI job. **It earned its keep immediately** — on its first run it flagged a real bridge
truncation of `simulation/batch_runner.py` (866 vs 913 authoritative lines). The physical OneDrive→
Documents move is already done (Greg-only); this is the durable guard half.

## 4. QA cross-validation — what the independent pass caught

Subagents could not run the suite (no pydantic/numpy/fastapi in their sandboxes), so the orchestrator's
pass was the real gate. It found and fixed:

1. **SIM-350 real bug — `to_jsonable` leaked `np.float64`.** `np.float64` subclasses Python `float`
   (and `np.str_` subclasses `str`), so numpy scalars hit the native-scalar fast path and were returned
   unconverted, leaking into the wire format. Fixed by excluding `np.generic` from that fast path so
   numpy scalars fall through to the numpy branch. Caught by the agent's own round-trip test.

2. **SIM-377 over-strong test assertion.** The agent's "high `_hit_rate` out-scores low" test asserted a
   behavioral property the no-DB stub never satisfies: per-game integer scores are byte-identical across
   hit rates because the stub's run production is driven by its own pitch-outcome rng, not the resolver.
   The knob *does* reach the factory (verified: `resolver.hit_rate` is set correctly). Rewrote the test
   to assert the knob's effect where it is actually consumed — more hits from `resolve_fielding` at higher
   `_hit_rate` — which is true and meaningful.

3. **Six file-bridge truncations repaired** on the sandbox mount (the SIM-315 hazard, present even on
   small files this session): `batch_runner.py`, `pyproject.toml`, `api/serialization.py`, `api/main.py`,
   `test_perf_eng_sim377.py`, and `ci.yml` (a whole job lost off the end — still-valid YAML, so only the
   integrity check / line-count diff caught it). Each authoritative file was complete; the mount copies
   were repaired in-place. `ci.yml`'s truncation is a reminder that the guard should grow to cover YAML.

## 5. Test results

* **Unit + regression: 1603 passing / 0 failed** (1548 unit + 55 regression) — the 1506 baseline plus
  **97 new tests** (SIM-350 21, SIM-353 24, SIM-352 13, SIM-351 9, SIM-354 11, SIM-315 12, SIM-377 7).
* **Regression golden-files:** 55 green (no engine drift).
* **Performance:** 5 benches intact (collected); full-timing execution exceeds the 45s sandbox cap as
  always — the runner is covered green by the `batch_runner` unit suites (sim332/333/352/377).
* **File integrity:** 157 `.py` files clean.
* DuckDB schema **v7** / Postgres Alembic head **0013** (unchanged — no schema change this sprint).

## 6. Disposition & carryover

All nine tickets **Closed**. P0 gates are landed: the serialization contract, lineup resolver, real
machine_factory, mounted API skeleton, and auth baseline now unblock the P1 endpoint tickets.

* **Next free ID: SIM-378.**
* **SIM-352 live-DB note:** code-complete + mock-verified; the 2s/30s SLA and a real `/simulate` run
  remain to be confirmed in an environment with live Postgres/DuckDB (folds into SIM-372).
* **Next (P1, the actual endpoints):** SIM-355 (`/api/games/{date}` + `/{game_pk}/simulate`) → SIM-356
  (snapshot persistence, Alembic 0014 / DuckDB v8) → SIM-357/SIM-358 (`/plays`+`/state`, override), plus
  SIM-359 (Redis TTL), SIM-360 (persistent pool), SIM-361 (calibration serving).
* **Follow-up flagged:** extend the SIM-315 integrity guard to YAML/TOML (this sprint's `ci.yml`/`pyproject.toml`
  truncations were `.py`-guard blind spots).
