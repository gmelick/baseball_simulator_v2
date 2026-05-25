# Sprint 2026-08-19 — Phase 5 Betting Surface (Spread Edge · CLV/Line-Movement · Bet Signals · Odds Provider) (executed 2026-05-24)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-24 · Disposition: ✅ all 4 tickets + the betting API integration accepted after cross-validation*

Fifth Phase-5 sprint. Builds the betting surface — the translation layer from simulation outputs to
actionable, market-validated betting edge: run-line/spread edge reports, the CLV/line-movement
time-series, a +EV bet-signal recommendation contract, the real-odds-provider swap seam, and a new
`/api/betting` router exposing it all. Betting-Analyst-led; lives in `betting/` + `pipeline/` + `api/`
(low `sim_loop.py` risk). Companion to `CHANGES.md`, `BACKLOG.md`, and the prior sprint logs.

## 1. Execution model

Three waves, partitioned so no two agents touched the same file concurrently:

- **Wave 1 (3 parallel, file-disjoint):** SIM-367 (`betting/clv_engine.py`), SIM-369
  (`betting/bet_signal.py`, new), SIM-370 (`pipeline/odds_provider.py`, new + surgical pipeline wiring).
- **Wave 2 (1):** SIM-368 (`betting/line_movement.py`, new) — depends on SIM-370's odds source + the stored series.
- **Wave 3 (1 Backend+Betting):** the API integration — new `api/routes/betting.py`, `api/schemas.py` models, mounted in `api/main.py`.

## 2. Tickets and results

| Ticket | Type | Owner | Result |
|---|---|---|---|
| SIM-367 | Gap | Betting+ML | `betting/clv_engine.py`: `spread_cover_prob` + `run_line_edge_report` — cover prob `P(margin > -L)` (HOME) / `P(margin < -L)` (AWAY) from the per-iteration score-margin array (GameSimSummary or raw); strict inequalities split integer-line pushes; ±1.5 default; reuses `_build_edge_report` (`label="run_line"`). 15 tests. |
| SIM-368 | Gap | Betting+Data | `betting/line_movement.py`: `LineQuote`/`LineMovement` + pure `line_movement_from_quotes` (running implied-prob series, per-step + net deltas, steam direction, reused two-way CLV) + async `fetch_line_movement` (reads `raw.game_odds` ordered by `fetched_at`, per-(side,book) grouping, sharp-consensus flag). Lifts the single entry-vs-close CLV to the full opening→closing surface. 17 tests. |
| SIM-369 | Feature | Betting | `betting/bet_signal.py`: `bet_signals_from_edges` — gate a list of `EdgeReport`s on positive edge ≥ `min_edge` AND `ev > min_ev`, size each by fractional Kelly (`clamp(kf·max(0,(b·p−q)/b), 0, cap)`, quarter-Kelly default, 5% cap), return ranked by EV desc. `BetSignal`/`BetSignalConfig`. 17 tests. |
| SIM-370 | Feature | Data+Betting | `pipeline/odds_provider.py`: `OddsProvider` `Protocol` + `get_odds_provider()` env factory (`ODDS_PROVIDER=mock` default) + `RealOddsAPIProvider` stub (clear not-configured error) + registry; `MockOddsAPI` conforms; `LiveIngestionPipeline` fetches odds via the provider (mock unchanged). 20 tests. |
| API integration | Feature | Backend+Betting | New `api/routes/betting.py` (prefix `/api/betting`): `/games/{game_pk}/edges`, `/signals`, `/line-movement`, `/clv`. Handlers thin over the betting modules; edges/signals reuse the SIM-355 sim seam (cache-memoized BatchRunner) + win-prob; odds from injected query params else `MockOddsAPI` (flagged per market). `BetSignalModel`/`LineQuoteModel`/`LineMovementModel` in `api/schemas.py` (reuse `EdgeReportModel`); mounted in `create_app()`. 14 tests. |

## 3. QA cross-validation

- Independent full-suite run from scratch (per-pattern chunks; FAISS builders individually;
  `test_api_main_wiring.py` + `test_api_betting_sim36x.py` run split — both exceed the 45 s shell cap as
  one file because the betting edge/signal tests run real sim batches).
- Agents self-repaired the file-bridge truncations (Wave-3 repaired `api/schemas.py` + `api/main.py`;
  SIM-370 repaired the 2164-line `live_ingestion_pipeline.py`; SIM-367 repaired `clv_engine.py`). Every
  authoritative file verified complete (`api/main.py` now 490 lines, ends with `app = create_app()`).
- **Robustness guard found in review:** the no-DB rng factory yields near-zero scores, so a realistic mock
  line can put every iteration on one side → `prob_to_american(0|1)` would raise. The betting router's
  `_safe_report` skips an un-priceable 0/1 side per-market (logged) rather than 500 — this also matters in
  production for small-N / extreme-line cases.

## 4. Test results

* **Unit + regression: 1861 passing / 0 failed** (1806 unit + 55 regression) — the Sprint-4 baseline of
  1780 plus **81 new tests** (SIM-367 15 + SIM-368 17 + SIM-369 17 + SIM-370 20 + betting API 14, less a
  couple of shared collections).
* **Regression golden-files:** 55 green (no engine drift).
* **File integrity:** 183 `.py` files clean.
* DuckDB schema **v10** / Postgres Alembic head **0014** (unchanged — no schema change this sprint).

## 5. Disposition & carryover

All four tickets + the betting API integration **Closed**. The betting surface is live: per-market edge
reports (incl. run-line/spread), the CLV/line-movement time-series, ranked +EV bet signals, and the
odds-provider swap seam — exposed under `/api/betting`.

* **Next free ID: SIM-378** (unchanged).
* **Remaining Phase 5 (the last tier — testing/infra):** SIM-371 (API integration + WebSocket +
  historical-replay E2E suite — `api/websocket/` is still an empty placeholder), SIM-372 (end-to-end
  `/simulate` latency perf gate — the 2s/30s SLA over the request path), SIM-373 (nginx reverse proxy w/
  WebSocket upgrade + dev/staging/prod env configs), SIM-374 (Prometheus + Grafana monitoring). After
  these, **Phase 5 is complete** and the project moves to Phase 6 (Frontend Build).
* **Live-DB / live-provider caveats (unchanged):** the `/simulate` SLA, the 11-engine build, the replay/card
  endpoints, a fitted calibration curve, and a real odds provider all verify in a live environment.
* **Standing follow-up:** extend the SIM-315 integrity guard to YAML/TOML (still `.py`-only).
