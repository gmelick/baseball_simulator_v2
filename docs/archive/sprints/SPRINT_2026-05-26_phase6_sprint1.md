# Sprint 2026-05-26 — Phase 6 Sprint 1: P0 Kickoff Gates (executed 2026-05-25 → 2026-05-26)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-26 · Disposition: ✅ all 13 P0 tickets accepted after cross-validation*

First Phase-6 sprint. Lands the **13 P0 kickoff gates (SIM-378→390)** — the frontend foundation
plus the new backend contracts the UI cannot start without — and the carry-in p95-metric fix
(SIM-410). Phase 6 was greenfield going in: `frontend/` was empty, there was no build tooling or
API→UI serving path, the pre-existing Phase-6 tickets had phantom dependencies, and several UI-facing
backend contracts (enriched games list, aggregate card, live read path, prop edges) did not exist.
This sprint builds the foundation the P1 frontend build (SIM-391→401) sits on. Companion to
`CHANGES.md` (per-agent detail), `BACKLOG.md` (banners), `docs/HANDOFF_PHASE6.md`, and the audit docs.

## 1. Tickets and owners

| Ticket | Type | Owner(s) | Deliverable |
|---|---|---|---|
| SIM-378 | Spec/ADR | UX + Backend + QA | `docs/architecture/2026-09-02-adr-frontend-framework.md` — **React 18 + Vite** chosen |
| SIM-379 | Infra | UX + QA | `frontend/` scaffold (React 18 + Vite + TS strict) + `frontend` CI job (Node 20) |
| SIM-380 | Feature | UX | Design system: `tokens.css` + `global.css` + Card/Panel/Badge primitives (CSS Modules) |
| SIM-381 | Gap | Backend + UX | StaticFiles mount + nginx SPA fallback (API→frontend serving path) |
| SIM-382 | Gap | PM | Backfilled 8 phantom parent tickets (SIM-108/109/112/122–126); re-mapped SIM-127–131 deps |
| SIM-383 | Feature | Backend + Data | Enriched `GET /api/games/{date}` — team names/abbrevs, venue, season W/L records (CTE join) |
| SIM-384 | Feature | Backend | `GET /api/games/{game_pk}/status` aggregate card + `GameStatus` 3-state enum |
| SIM-385 | Feature | Backend | Typed + documented WebSocket event schema (`api/websocket/schemas.py`, Pydantic v2) |
| SIM-386 | Feature | Data + Backend | `GET /api/games/{game_pk}/live` — live read path off `sim.lineup_state` + `load_live_game_state` |
| SIM-387 | Bug | Backend + ML | Threaded `calibration_map` into `win_probability()` at the `/api/betting` edge/CLV call sites |
| SIM-388 | Feature | Backend | `SubstitutionSlot` model + `substitutions[]` array on `RosterOverride` + 3-step `_apply_override` |
| SIM-389 | Security | Backend + QA | httpOnly cookie session (`api/routes/auth.py`) + `require_auth` enforcement + CORS wildcard fix |
| SIM-390 | Feature | Backend + Betting | `GET /api/games/{game_pk}/props/{player_id}/{prop}` — full PMF + optional over/under + edge report |
| SIM-410 | Improvement | Backend + QA | `LatencyMiddleware` wires rolling p95 into `app.state.api_p95_seconds` → `/metrics` (carry-in) |

## 2. Critical-path sequencing

The P0 tier was delivered in dependency order along the Phase-6 critical path:

```
SIM-378 (ADR) → 379/380/381 (scaffold + design system + serving path)
             → 382 (dep backfill) + 383/384/385/387/389 (backend contracts + bug/security fixes)
             → 386 (live read path; depends on 384) + 388 (multi-sub) + 390 (prop edges; depends on 387)
```

## 3. New backend contracts (the UI's now-buildable surface)

- **`GET /api/games/{date}`** (SIM-383) — bare integer IDs replaced with team names/abbreviations,
  venue name + city, and season-to-date W/L records via a `team_records` CTE (UNION ALL of home +
  away perspectives, `status = 'Final' AND game_date < $1`). Backs the Day Summary cards (SIM-391).
- **`GET /api/games/{game_pk}/status`** (SIM-384) — identity + 3-state status enum
  (scheduled/live/final/postponed, mapped from 8 raw `raw.games.status` values) + best-effort sim
  summary + reserved `odds` field. One call for a 3-state game card.
- **`GET /api/games/{game_pk}/live`** (SIM-386) — surfaces the live `game_state` JSONB from
  `sim.lineup_state` (written by the :8001 pipeline) on the main API: inning/half/outs/count/score/
  baserunners/lineups/rosters + `updated_at` staleness marker. Backs the LinescoreGraphic +
  BaseballFieldGraphic (SIM-392). 404 when not live.
- **`GET /api/games/{game_pk}/props/{player_id}/{prop}`** (SIM-390) — exposes the previously-dead
  `prop_edge_report` / `PropDistribution.p_over`: full integer-support PMF + optional
  `p_over`/`p_under`/`p_push` (sportsbook convention) + optional full edge/EV/CLV `edge_report` when
  market odds are supplied. Backs the per-player distribution view (SIM-394) + betting card (SIM-395).
- **WebSocket schema** (SIM-385) — Pydantic v2 event models replace untyped raw dicts, giving the
  frontend a live contract.
- **Multi-substitution override** (SIM-388) — `substitutions[]` array lets the override UI stage
  targeted single-player swaps without the full batting order. Unblocks SIM-128 / SIM-398.

## 4. Defects + security fixes closed

- **SIM-387** — the "gold-standard" CLV was computed off an **uncalibrated** win probability;
  `calibration_map` is now threaded through `win_probability()` at the betting edge/CLV call sites.
- **SIM-389** — `require_api_key` was wired to **zero** routes and dev CORS was `*` + credentials;
  added an httpOnly-cookie browser session model, `require_auth` enforcement on expensive routes,
  and removed the CORS wildcard.
- **SIM-410** (carry-in) — the `/metrics` p95 gauge was an unwired placeholder; `LatencyMiddleware`
  now maintains a rolling 200-request window and publishes p95 to `app.state.api_p95_seconds`.

## 5. Verification (QA cross-validation)

- `pytest tests/unit/test_api_games.py` → **61/61 passed** (16 baseline + 7 SIM-383 + 12 SIM-384 +
  9 SIM-388 + 12 SIM-390 + 5 SIM-386).
- Full unit suite (excluding the FAISS/DuckDB-dependent tests that need deps only present in CI):
  **1826 passed / 8 skipped / 0 failed**.
- `ruff check` + `ruff format --check` + `mypy` clean on all touched modules.

## 6. Documentation + bookkeeping

- `CHANGES.md` — Sprint 1 table + per-ticket detail sections (SIM-382/383/384/386/388/390 + carry-ins).
- `BACKLOG.md` — sprint banner updated to **Sprint 1 COMPLETE**; P0 rows marked Closed 2026-05-26.
- `backlog.xlsx` — all 13 P0 tickets + SIM-410 set to `Closed` in the `Full Backlog` + `Phase 6 Build` sheets.
- **Next free ticket ID: SIM-421.**

## 7. Next — Sprint 2 (P1 frontend build)

Sprint 2 builds the components on the now-buildable contracts, starting with the Day Summary page
(SIM-391: date nav + game-count badge + 3-state cards) and the LinescoreGraphic + BaseballFieldGraphic
(SIM-392), then the Game page (SIM-393) and per-player boxscore (SIM-394). The data/ML/perf prerequisite
track (SIM-402→409) runs alongside and must be live-env verified before its numbers reach users.
