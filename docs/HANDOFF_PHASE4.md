# Phase 4 Handoff

*Author: Product Manager (Agent 1) · Date: 2026-06-10 · Phase 3 closure + program audit*

---

## TL;DR

Phase 3 (Play Pool Architecture) is **complete**: the SIM-300 spec, SIM-301 nightly
cache, SIM-302 sampler, SIM-303 sim-loop scaffold, plus SIM-048 (engine registry),
SIM-076 (recency weighting + `pool_build_metadata`), SIM-095 (incremental rebuild),
SIM-111 (query contracts), SIM-115 (index prune), and SIM-056 (foul-weighting design)
all shipped. Test suite is green at **927 unit+regression passing / 1 skipped / 0 failed**;
DuckDB schema is at **version 5** (migrations 0001→0005). A full 9-agent program audit ran
at Phase-3 close and filed **41 Phase 4 tickets** (SIM-220 + SIM-310–349) in
`docs/audit/2026-06-10-phase4-prioritized-tickets.md`.

**Phase 4 = the core simulation loop.** Before writing loop code, land the Tier-P0 gates
(§6) — especially the loop spec (SIM-310) and the `GameState` contract (SIM-311) — and fix
the **six live bugs** (§4). The play-pool *read path* exists but is wired only as a
single-pitch scaffold; turning it into a full game simulator is the bulk of Phase 4.

---

## 1. What's built and verified (Phases 1–3)

- **11 similarity engines** (`similarity/engines/*.py`) + **`similarity/registry.py`**
  (SIM-048) — unified lookup with family + score_type (RBF/GMM = similarity [0,1];
  KDTree/FAISS = distance).
- **Play pool**: `pipeline/batch/play_pool_cache.py` (SIM-301, nightly FAISS tiles +
  `.meta` + `.rowids.npy`, idempotent), `simulation/play_pool_sampler.py` (SIM-302, the
  four-method `PlayPoolSampler`), `simulation/sim_loop.py` (SIM-303 **scaffold** —
  `PlateAppearanceSimulator.simulate_pitch` only).
- **Sim pools** (`sim.pitch_pool`, `sim.outcome_pool`, `sim.stolen_base_pool`) with
  `recency_weight` (SIM-076), built by the computor with incremental rebuild (SIM-095) and
  `sim.pool_build_metadata` watermarks.
- **Recency walk-forward harness** `similarity/backtesting/recency_walk_forward.py`
  (SIM-076) — reusable; SIM-220 will extend it.
- **Perf design (complete, mechanism not yet built)**: time budget
  (`docs/perf/2026-06-03-sim-loop-time-budget.md`), RAM budget
  (`docs/perf/2026-05-27-ram-budget.md`), parallelism ADR
  (`docs/architecture/2026-05-27-parallelism.md`), FAISS index design
  (`docs/architecture/2026-06-03-faiss-index-design.md`), bench harness
  (`tests/performance/bench_simulation.py`, Phase 4/5 benches still skip-stubs).
- **Run-value constants** `simulation/constants.py` (SIM-202) — but see bug SIM-312.

---

## 2. Architecture at the Phase 3 / Phase 4 boundary

```
 DuckDB sim.pitch_pool / outcome_pool / stolen_base_pool  (+ recency_weight, pool_build_metadata)
        │  nightly: pipeline/batch/player_profile_computor.py  (incremental, SIM-095)
        ▼
 /data/play_pool/<season>/<pitcher_id>/<bat_hand>.faiss  (SIM-301 tiles + .meta + .rowids.npy)
        │  hot-load
        ▼
 simulation/play_pool_sampler.py  (SIM-302: load_tile / sample_pitch / sample_batted_ball / reload_recent)
        │  ← distance→weight conversion lives HERE (engines stay distance-pure)
        ▼
 simulation/sim_loop.py  (SIM-303 SCAFFOLD — one pitch today)   ◀── PHASE 4 builds the full loop here
        │
        ▼  (Phase 4) GameState machine → 8 steps → simulate_game() → 100-iter ProcessPool runner (Phase 5 API)
```

Phase 4 turns the scaffold into a full game: a mutable `GameState`, the 8-step loop
(game-state → pitch selection → outcome → fielding → baserunning → state update → loop
control, plus steal + manager decisions), terminal PA/inning/game logic, and the
aggregation/output contracts the UI (Phase 6) and betting/CLV consume.

---

## 3. Critical operational gotchas (read before touching the tree)

### OneDrive truncation — the #1 hazard (SIM-315 proposes fixing this)
The repo lives under OneDrive (`C:\Users\grego\OneDrive\...`). Editing **large files
(>~1,500 lines)** via the file tools repeatedly delivers a **truncated** copy to the
sandbox mount (and sometimes injects null bytes), while the authoritative file is intact.
`git` is currently unusable from the working tree (`.git/config` → "Invalid argument").
Mitigations until SIM-315 lands: for large files, edit via the file tools then **repair the
mount copy** by `head -n <last-good-line>` + appending the authoritative tail via a bash
heredoc and `py_compile` to verify; for small files a bash heredoc to the mount is the most
reliable single write; `tr -d '\000'` recovers a null-byte-corrupted file.

### Sandbox test environment (Python 3.10; project targets 3.11+)
Install: `pytest pytest-benchmark scipy scikit-learn duckdb faiss-cpu pot asyncpg
psycopg2-binary fastapi httpx redis sqlalchemy aiohttp websockets` (`--break-system-packages`).
Create `/tmp/sbshim/sitecustomize.py` backfilling `datetime.UTC`. Run tests with
`PATH="$HOME/.local/bin:$PATH" PYTHONPATH=/tmp/sbshim PYTHONPYCACHEPREFIX=/tmp/pyc3
python3 -m pytest <paths> -p no:cacheprovider -W ignore`. The full suite exceeds the 45 s
shell limit — **run in chunks**.

### DuckDB `DROP INDEX` must be schema-qualified
Indexes on `sim.*` tables live in the `sim` schema; an unqualified `DROP INDEX IF EXISTS
idx_…` silently **no-ops**. Use `DROP INDEX IF EXISTS sim.idx_…` (caught by the SIM-115
test).

### Postgres port 5433 (carried from Phase 3)
`.env` has `DB_HOST_PORT=5433` / `BASEBALL_DB_DSN=…@localhost:5433/…`. Don't lose it.

---

## 4. Six live bugs found by the audit — fix as touched (don't defer)

1. **SIM-312** — `RUN_VALUES` keys don't match the pool's Statcast-raw `events` vocabulary,
   so `RUN_VALUES.get(event, 0.0)` silently scores common outs as 0.0 runs. Prefer sampled
   `result_hits/outs/runs` deltas + the RE24 matrix; linear weights as fallback.
2. **SIM-313** — `PlayPoolSampler` ignores `recency_weight` (uses pure `1/(d+ε)`) despite
   the SIM-111 §8 contract that it should multiply the distance-weight. Recency is currently
   a no-op on the read side.
3. **SIM-322** — GMM component `mean` is stored in original units but `covariance` already
   standardized; the engine standardizes the covariance again → inconsistent scales in the
   Wasserstein-2 term.
4. **SIM-336** — park-factor builder writes `factor_vs_l/_vs_r` as NULL and references
   `factor_overall` before the UNPIVOT produces it; park effects also unwired (and risk
   double-counting on an already-park-influenced pool).
5. **SIM-337** — SIM-115 index pruning contradicts the SIM-111 query contract (kept
   outcome/count, dropped the single pitcher index); reconcile + add `stand` composites.
6. **SIM-346** — pitcher no-arsenal fallback is a ×1.0 no-op; `arsenal_gamma` (squared) vs
   `ARSENAL_SCALE` (linear) are inconsistent; `CalibrationReport` is computed but never
   wired into the engines (median can drift off 0.50).

---

## 5. Architecture decisions locked in Phase 3 (treat as load-bearing)

- **Score discipline**: RBF/GMM engines emit similarity [0,1]; KDTree/FAISS emit distance.
  The *sampler* is the only place distance→weight conversion happens.
- **Recency weight**: 2.0 for the most-recent two seasons, ×0.75/season decay, floor 0.25,
  relative to the build reference season. Materialized as `recency_weight` per pool row;
  the Python `recency_weight()` mirrors the SQL.
- **FAISS**: per-tile `IndexFlatL2` (tiles are small after the mandatory pitcher_id+stand
  pre-filter); IVFFlat only above a 50k-vector/index crossover (SIM-114).
- **Parallelism**: `ProcessPoolExecutor(max_workers=min(CPU-1, 10))` + read-only tiles
  attached via `multiprocessing.shared_memory` (SIM-281) — **mechanism still to build**
  (SIM-332/333).
- **RAM**: ≤ 2 GB total resident regardless of worker count; shared read-only payload
  ≈ 290 MB (SIM-280).
- **Migrations** always paired with a schema update (Alembic for Postgres, numbered SQL +
  `duckdb_schema_version.txt` bump for DuckDB).

---

## 6. Phase 4 entry plan

Full list + sizes/owners/deps in `docs/audit/2026-06-10-phase4-prioritized-tickets.md`.

1. **Land the P0 gates** — SIM-310 (loop spec), SIM-311 (GameState/PlayResult contract),
   SIM-314 (ID collision), SIM-315 (OneDrive fix), and fix bugs SIM-312/SIM-313 (+ the
   quick SIM-322/SIM-337). Nothing else should start before SIM-310 + SIM-311.
2. **Build the loop** — SIM-316 (state machine) → SIM-317 (real fingerprints) →
   SIM-318 (outcome + foul rule) / SIM-319 (fielding + baserunning + steals) →
   SIM-320 (`simulate_game()`). SIM-321 (cross-engine fusion) feeds 317/318.
3. **Build the validation spine alongside the loop** — SIM-220 (backtester:
   ECE/Brier/log-loss + ablation), SIM-324 (sniff tests), SIM-325 (chi-squared replay),
   SIM-326 (invalid-state harness). Output is unverifiable without these.
4. **Output contracts + perf mechanisms** — SIM-327 (`GameSimResult`), SIM-328 (per-player
   aggregates), SIM-330 (win prob), SIM-329 (prop PMFs), SIM-331 (field/PBP);
   SIM-332 (ProcessPool runner) → SIM-333 (shared-memory attach), SIM-334/335.
5. **Betting** — SIM-339 (CLV engine), SIM-340 (real odds + props) once outputs exist.
6. **Hygiene/tech-debt** — SIM-341–349 opportunistically.

**Critical path:** SIM-310 → SIM-311 → SIM-316 → SIM-317 → {SIM-318, SIM-319} → SIM-320 →
{SIM-220, SIM-327, SIM-332}.

After SIM-320 + SIM-332 + the validation spine pass, Phase 5 (backend API + 100-iteration
runner + override endpoint + WebSocket) can begin.

---

## 7. File map (Phase 4-relevant)

```
simulation/sim_loop.py              SIM-303 scaffold — Phase 4 builds the loop here
simulation/play_pool_sampler.py     SIM-302 sampler (read path)
simulation/constants.py             RUN_VALUES (see bug SIM-312)
simulator/                          *** empty steps/ scaffold — consolidate w/ simulation/ ***
similarity/registry.py              SIM-048 engine registry
similarity/backtesting/recency_walk_forward.py   SIM-076 harness (SIM-220 extends)
similarity/engines/                 11 engines (+ similarity_calibration.py, _diagnostics.py)
pipeline/batch/player_profile_computor.py  pools + recency + incremental (+ park-factor bug SIM-336)
pipeline/live/live_ingestion_pipeline.py   live stream + odds (Mock); ws_router/odds_router live here
api/                                FastAPI; Phase 5 routers commented out; api/websocket/ empty
db/schemas/02_duckdb_schema.sql     sim pools + recency_weight + pool_build_metadata
db/migrations/duckdb/0001..0005     DuckDB migrations (version file = 5)
docs/audit/2026-06-10-*             this audit (program findings + prioritized tickets)
docs/perf/, docs/architecture/      the Phase 3 design docs (budget/RAM/parallelism/FAISS/play-pool/contracts)
BACKLOG.md / CHANGES.md / backlog.xlsx   backlog (Full Backlog has all 41 new tickets)
```

---

## 8. Conventions worth preserving

- Ticket IDs continue sequentially; next free is **SIM-350**. The Phase 4 audit used
  SIM-220 + SIM-310–349.
- Tests named `test_<role_short>_sim<NNN>.py`; engine tests use the in-memory `__new__`
  pattern; migrations paired with schema updates.
- `CHANGES.md` grows (per-sprint, per-agent); `BACKLOG.md` trims to one-line rows + banners;
  each sprint gets a `docs/SPRINT_*.md` log; `backlog.xlsx` is regenerated from BACKLOG.md
  and is sometimes open/locked — don't assume it's writable.
- Run the suite in chunks with the shimmed env (§3). Add `simulation/` to the coverage gate
  before the loop grows (SIM-343).

---

## 9. Quick-start for the next conversation

1. Read this file and `docs/audit/2026-06-10-phase4-prioritized-tickets.md`.
2. Read `simulation/sim_loop.py` (the scaffold) + `simulation/play_pool_sampler.py`.
3. Don't start loop code until SIM-310 (spec) + SIM-311 (GameState contract) exist.
4. Fix the six §4 bugs as you touch their areas.
5. Mind the OneDrive truncation (§3) and the schema-qualified `DROP INDEX` rule — both will
   bite within the first hour otherwise.

---

*End of handoff.*
