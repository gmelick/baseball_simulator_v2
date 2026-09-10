# Sprint 2026-08-12 — Phase 5 P2 Loop Outputs (R/H/E · Fielders · W/L/S · Boxscore) (executed 2026-05-24)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-24 · Disposition: ✅ all 5 tickets accepted after cross-validation*

Fourth Phase-5 sprint. Delivers the loop-output gaps the frontend game cards need — data the
Phase-4 loop didn't surface: the per-inning linescore + team R/H/E, the 9 defensive fielders, the
winning/losing/save pitcher decisions, richer per-player boxscore stats (2B/3B/R/SB + pitcher
hits/runs-allowed) with an exact prop-TB, and the per-player 100-iteration boxscore-average API
shape. Companion to `CHANGES.md`, `BACKLOG.md`, and the prior sprint logs.

## 1. Design — derive, don't re-surger the loop

The audit framed these as "the loop must produce R/H/E, fielders, W/L/S." The key risk: four of the
five tickets nominally touch `simulation/sim_loop.py` — the ~2680-line file that truncates on nearly
every file-tool edit. **So the sprint was designed to DERIVE most outputs from the already-recorded
`PlayResult` stream (the SIM-355/357 play recorder) rather than thread new tracking through the loop:**

- SIM-362 (linescore), SIM-364 (W/L/S): pure derivations from a `PlayResult` stream (each play's
  `next_state` carries inning/half/score) — new modules, **zero `sim_loop.py` edits**.
- SIM-363 (fielders): `FieldSnapshot.from_game_state` already accepts a `defense_positions` map; the
  ticket just *builds* that map from the resolved lineup — **zero `sim_loop.py` edits**.
- SIM-366 (boxscore-average API): pure API wiring over `PropDistributionSet` means.

Only **SIM-365** genuinely needed loop surgery (the per-game BoxScore must accumulate 2B/3B/R/SB for
the prop distributions to aggregate them), so it was isolated to its own agent. This confined the
`sim_loop.py` truncation risk to one workstream and let the other four run safely in parallel.

## 2. Execution model

- **Wave 1 (4 parallel, file-disjoint):** SIM-362 (`simulation/linescore.py`), SIM-363
  (`simulation/lineup_resolver.py` extension), SIM-364 (`simulation/pitcher_decisions.py`), SIM-365
  (`sim_loop.py` + `prop_distributions.py` — the loop surgery).
- **Wave 2 (1 Backend+UX agent):** SIM-366 + the API exposure — `api/schemas.py` models, `api/routes/games.py`
  endpoints, `db/sim_store.py` game-card store (DuckDB migration 0010), wiring linescore/decisions into
  the record→persist flow and the defense map into the persisted `StateAtPitch` snapshots.
- **Orchestrator QA:** full-suite cross-validation; fixed the version-file/test consistency the Wave-2
  agent missed (below).

## 3. Tickets and results

| Ticket | Type | Owner | Result |
|---|---|---|---|
| SIM-362 | Gap | Backend+BA | `simulation/linescore.py`: `linescore_from_plays` → `Linescore` (per-inning away/home grid + team R/H/E; errors charged to the fielding side; reach-on-error excluded from hits; unplayed/walk-off halves render None; extra innings). 11 tests. |
| SIM-363 | Gap | Backend+ML | `simulation/lineup_resolver.py`: `build_defense_map_for_state` → `{P,C,1B..RF: player_id}` for the fielding side (codes 1-9; pitcher from `TeamLineup.pitcher_id`; DH/pinch skipped; subs → current occupant). Feeds `FieldSnapshot`. 19 tests. |
| SIM-364 | Gap | BA+Backend | `simulation/pitcher_decisions.py`: `decisions_from_plays` → `PitcherDecisions` (W/L from the permanent lead's pitcher of record; Rule-9.19 ≤3-run save heuristic for the finisher; ties/walk-offs handled). 14 tests. |
| SIM-365 | Improvement | BA+Backend | `PlayerStatLine` gains `b2/b3/r/sb` + `h_allowed/r_allowed`, accumulated in `_accumulate_pa` + `_resolve_steal_outcome`; `prop_distributions._total_bases` is now exact (`h+b2+2·b3+3·hr`), `TB_IS_LOWER_BOUND=False`. 22 tests; sim328/sim329 green. |
| SIM-366 | Feature | Backend+UX | `BoxscoreCardModel.from_prop_set` (per-player prop means) + `GET /{game_pk}/boxscore`; `LinescoreModel`/`PitcherDecisionsModel` + `GET /{game_pk}/linescore`+`/decisions`+`/card`; fielders populated in persisted snapshots so `/state` returns the 9 slots; DuckDB `sim.game_cards` store (migration 0010). 12 tests. |

## 4. QA cross-validation — what the independent pass caught

- **DuckDB version drift:** the Wave-2 agent added migration `0010_sim362_364_game_card.sql` but left
  `db/schemas/duckdb_schema_version.txt` at 9 (and the `test_sim_store` sanity test still asserted 9).
  The orchestrator bumped the version file to **10** and updated the test — restoring the migrations↔version
  invariant the project's migration workflow requires.
- **sim329 test:** SIM-365's exact-TB fix correctly retired the `TB_IS_LOWER_BOUND` limitation; the
  agent updated the one `test_backend_sim329` assertion that hard-coded the old contract (accepted).
- **Truncation:** the agents self-repaired numerous mount truncations (SIM-365 did ~6 on `sim_loop.py`,
  now 2744 lines, tail intact; Wave-2 repaired `games.py`/`schemas.py`/`sim_store.py`). Every authoritative
  file verified complete; integrity guard clean on 174 `.py` files.
- **Derivation note (not a defect):** the no-DB `rng_driven_machine_factory` test driver emits scoreless
  ties, so end-to-end linescores/decisions over it are empty — the derivation logic is proven against
  hand-built `PlayResult` streams with real scores (Wave-1 unit tests). Real values appear once the
  production factory runs over live data.

## 5. Test results

* **Unit + regression: 1780 passing / 0 failed** (1725 unit + 55 regression) — the Sprint-3 baseline of
  1702 plus **78 new tests** (SIM-362 11 + SIM-363 19 + SIM-364 14 + SIM-365 22 + SIM-366 12).
* **Regression golden-files:** 55 green (no engine drift).
* **File integrity:** 174 `.py` files clean.
* DuckDB schema **v10** (migrations 0008 play_stream + 0009 state_snapshots + 0010 game_cards); Postgres
  Alembic head **0014**.

## 6. Disposition & carryover

All five tickets **Closed**. The frontend game cards are now servable: linescore + R/H/E, the 9
fielders in the field graphic, W/L/S on the completed card, exact total bases, and the per-player
100-iteration boxscore averages.

* **Next free ID: SIM-378** (unchanged).
* **Remaining Phase 5 (P2/P3):** the betting surface — SIM-367 (run-line/spread `EdgeReport`), SIM-368
  (CLV/line-movement time-series), SIM-369 (bet-signal/+EV endpoint), SIM-370 (real odds provider behind
  `MockOddsAPI`); then testing/infra — SIM-371 (API/WS/replay E2E suite), SIM-372 (`/simulate` SLA perf
  gate), SIM-373 (nginx), SIM-374 (Prometheus/Grafana).
* **Live-DB caveats (unchanged):** `/simulate` SLA + the 11-engine build + the replay/card endpoints over
  real data verify in a live environment.
* **Standing follow-up:** extend the SIM-315 integrity guard to YAML/TOML (still `.py`-only — it would
  have caught neither the version-file drift nor prior `.toml`/`.yaml` truncations).
