# Phase 3 Handoff

*Author: Product Manager (Agent 1) · Date: 2026-05-20 · Phase 2 closure*

---

## TL;DR

Phase 2 is closed. All 11 similarity engines are built, schema migrations are
in chain, both performance index gates pass against live 2024 staging, and
the test suite is green at 767 passed / 22 skipped / 0 failed. The project
is ready for Phase 3 (play-pool implementation), but **before any new feature
work**, the next agent must reconcile the documentation/test gaps listed in
§7 below. Several deliverables were marked "shipped" in `BACKLOG.md` and
`CHANGES.md` but are not actually on disk — almost certainly OneDrive sync
casualties.

---

## 1. What's actually built and verified

These exist on disk, run cleanly, and have working tests:

**Similarity engines** (`similarity/engines/*.py`, 11 files):
pitcher (GMM Wasserstein-2), batter (weighted-RBF), fielder (weighted-RBF,
per-position partitioned), baserunner extra-base (RBF), baserunner-steal
(RBF), catcher v2 (5-sub-score RBF — framing 0.45 / blocking 0.20 /
execution 0.12 / deterrence 0.08 / offense 0.15), pitcher-steal (RBF),
manager (RBF), situation (scipy KDTree over 11-dim game state),
pitch-to-pitch (FAISS L2, 10-dim fingerprint), batted-ball (FAISS L2, 3-dim
launch fingerprint with SIM-051 fall-forward).

**Database**: 12 Alembic migrations (`0001 → 0012`) and 2 DuckDB migrations
(`0001 → 0002`) — note the DuckDB chain only has two files even though
`BACKLOG.md` claims three (see §7).

**Scripts**: `scripts/backfill_odds_hash.py` (SIM-157),
`scripts/check_bat_side_coverage.py` (SIM-160),
`scripts/run_index_acceptance.py` (SIM-158/161 harness — recently patched
to use `_build_situation_query()` instead of parameterized
`IS NOT DISTINCT FROM`).

**Live performance**: SIM-085 `idx_pitches_situation` and SIM-089
`idx_pitches_pitcher_season_clean` both pass against real 2024 staging.
Most recent run: SIM-089 = 6.77 ms (50 ms budget), SIM-085 passing after
the SIM-163 script fix. Report at `docs/perf/2026-05-13-index-acceptance.md`.

**Bat-side data quality**: All 9 seasons (2017-2025) clear the 1% NULL gate
on `raw.pitches.bat_hand`. Report at
`docs/data_quality/2026-05-20-bat-side-coverage.md`. 11-13% of pitches are
to switch hitters (informational).

---

## 2. Architecture as it stands today

```
                ┌─────────────────────────────────────────────────────────┐
                │  raw.pitches (Postgres, ~700K rows/season, partitioned) │
                └─────────────────┬───────────────────────────────────────┘
                                  │ ETL: pipeline/etl/etl_historical_loader.py
                                  │ live: pipeline/live/live_ingestion_pipeline.py
                                  ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ derived.* (Postgres) — pitcher/batter/fielder/baserunner/       │
        │ catcher_season_metrics, populated by                            │
        │ pipeline/batch/player_profile_computor.py                       │
        └─────────────────┬───────────────────────────────────────────────┘
                          │ nightly
                          ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ DuckDB (file: BASEBALL_DUCKDB_PATH)                             │
        │   ├─ sim.pitch_pool       (~1M rows after pre-filter logic)     │
        │   ├─ sim.outcome_pool     (batted-ball events)                  │
        │   ├─ derived.* mirror     (catcher_season_metrics etc.)         │
        │   └─ similarity_profiles  (per-engine profile rows)             │
        └─────────────────┬───────────────────────────────────────────────┘
                          │ engine.build_index()
                          ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ Similarity engines (in-process)                                 │
        │   • 7 weighted-RBF (composite scoring)                          │
        │   • 1 GMM-W₂ (pitcher arsenal)                                  │
        │   • 1 KDTree (situation)                                        │
        │   • 2 FAISS (pitch + batted-ball geometric similarity)          │
        └─────────────────┬───────────────────────────────────────────────┘
                          │ FastAPI: api/routes/similarity.py
                          ▼
                ┌─────────────────────────────────┐
                │ Client / simulation loop (TBD)  │
                └─────────────────────────────────┘
```

The simulation loop layer is what Phase 3+ adds. There is **no
`simulation/` package on disk yet** — the closest thing is `simulator/`
which contains only `__init__.py` and `simulator/steps/__init__.py`.
Phase 3 will live partly in a new `simulation/` directory and partly in
extensions to `similarity/`.

---

## 3. Critical operational gotchas

### Postgres port — DB_HOST_PORT=5433 in .env

If a previous Postgres install on the Windows host binds 5432, Docker's
port-forward silently intercepts at the wrong process. **Symptom:**
password auth fails inside the host shell, succeeds inside the container.

The current `.env` has:

```
BASEBALL_DB_DSN=postgresql://baseball_user:baseball_pass@localhost:5433/baseball_sim
DB_HOST_PORT=5433
```

`docker-compose.yml` reads `${DB_HOST_PORT:-5432}:5432` for the db
service, so docker-compose is permanent. The shell variable also needs
`setx BASEBALL_DB_DSN "..."` in a fresh cmd window so scripts pick it up.

### OneDrive sync truncation

The repo lives at `C:\Users\grego\OneDrive\Documents\PycharmProjects\baseball_simulator_v2`.
OneDrive periodically truncates files mid-edit — most aggressively on
large `.py` files (`pipeline/batch/player_profile_computor.py` is 4000+
lines and was truncated three times during Phase 2). **Mitigation pattern:**
when re-writing large files, use bash heredocs against the
`/sessions/serene-sleepy-newton/mnt/baseball_simulator_v2/` mount, not the
Windows path.

This is also the suspected cause of the missing audit + spec docs in §7.
Files were almost certainly written; OneDrive then dropped them on next
sync without warning.

### Python 3.13 lacks POT/faiss wheels

If the user is on 3.13, the pitcher engine (needs `ot`) and FAISS engines
(`faiss-cpu`) will skip with `_POT_AVAILABLE` / `_FAISS_AVAILABLE` False.
That's by design — the smoke tests detect this and skip rather than fail.
Production should run on 3.11 or 3.12 where wheels exist.

### `IS NOT DISTINCT FROM` and prepared statements

asyncpg uses prepared statements. `$N IS NOT DISTINCT FROM NULL` cannot be
constant-folded to `IS NULL` at plan time because `$N` could be a real
value at execute. The planner picks a generic plan that bypasses partial
index columns — caught during SIM-161 as a 12x latency regression.

**Pattern to use instead** (see `scripts/run_index_acceptance.py::_build_situation_query`):
build the WHERE clause string at runtime, emitting literal `IS NULL` for
None-valued params. There's a regression test in
`tests/unit/test_perf_eng_sim158.py::TestSituationQueryBuilder` that locks
this in.

### Defensive temp-table helpers in player_profile_computor

`_ensure_fielder_temp_tables_exist()` and `_ensure_catcher_temp_tables_exist()`
create empty `CREATE TABLE IF NOT EXISTS` placeholders for the seven
fielder + three catcher temp tables before the aggregator runs. The
aggregators bail early when sample size < threshold, so without these
placeholders DuckDB raises `Catalog Error: Table _tmp_of_plays does not
exist`. Don't remove them.

---

## 4. Architecture decisions locked in Phase 2

These are decisions a Phase 3 implementer should treat as load-bearing
unless they're explicitly revisiting them:

**Engine score normalization.** RBF engines emit similarity ∈ [0,1]
directly. KDTree / FAISS engines emit Euclidean distance. The play-pool
sampler is responsible for distance→weight conversion (typically `1/d`
or top-k uniform); engines deliberately don't do this.

**Handedness handling.** Pitcher engine has a hard L/R partition (never
cross-handed). Fielder engine has hard per-position partitions
(1B/2B/3B/SS/LF/CF/RF). Catcher and batter engines do NOT partition —
batter uses a soft `bats_penalty` (0.92 for opposite hand) and an optional
`vs_hand` mode that re-weights the platoon sub-score from 0.15 to 0.35.

**Empirical Bayes priors.** n_prior values are tuned to how fast each
metric stabilizes: batters/pitchers=5, fielders/catchers/baserunners=15,
steal engines=20-25, manager=30. The final composite always gets
`× √min(α_query, α_candidate)` to discount unreliable comparisons.

**Catcher v2 sub-score split (SIM-072).** Framing 45 / Blocking 20 /
Execution 12 / Deterrence 8 / Offense 15. Deterrence uses
`steal_attempt_rate_against` as a single-feature sub-score — runners
declining to even try is itself a measurable form of value.

**Index strategy.** `idx_pitches_situation` is a 7-column partial index
on `(inning, outs, balls, strikes, on_1b, on_2b, on_3b) WHERE NOT
data_quality_flag`. `idx_pitches_pitcher_season_clean` is a partial
composite `(pitcher, season) WHERE NOT data_quality_flag`. The
`data_quality_flag = FALSE` predicate must appear in every query that
wants to use these indexes — without it, Postgres falls back to a
worse plan. The SIM-088 deletion of `idx_pitches_pitch_type` is
intentional and shouldn't be re-added.

**FAISS recency boost.** Last 2 seasons are duplicated once in the
FAISS index when `recency_boost=True` (production default). Off for
deterministic tests. See `RECENCY_BOOST_SEASONS=2` constant.

**Spray-angle normalization (SIM-051).** `pull_relative_spray_angle` is
`spray_angle` flipped by handedness so pull is always positive. The
batted-ball engine has `_select_spray_column()` that prefers it when
present and falls back to raw `spray_angle` with an INFO log. Don't
collapse the fallback path — older data still doesn't have the column.

---

## 5. File map — where everything lives

```
api/                             FastAPI routes; similarity endpoints in routes/similarity.py
db/migrations/duckdb/            2 DuckDB SQL migrations
db/migrations/versions/          12 Alembic migrations (0001 → 0012)
db/schemas/                      Canonical schema (01_postgres_schema.sql, 02_duckdb_schema.sql)
docs/architecture/               *** EMPTY — see §7 ***
docs/audit/                      *** EMPTY — see §7 ***
docs/data_quality/               2026-05-20-bat-side-coverage.md (live data, passing)
docs/perf/                       2026-05-13-index-acceptance.md (live data, passing)
docs/HANDOFF_PHASE3.md           This file
docs/similarity_visualization_spec.md     UX spec (Phase 5/6 deferred)
pipeline/batch/player_profile_computor.py  4000+ lines, nightly profile build
pipeline/etl/                    Historical + sprint_speed + venue + opening_line loaders
pipeline/live/live_ingestion_pipeline.py   Live pitch stream + GameState + odds
scripts/backfill_odds_hash.py    SIM-157 one-shot dedup
scripts/check_bat_side_coverage.py   SIM-160 audit
scripts/run_index_acceptance.py  SIM-158/161 EXPLAIN ANALYZE harness
similarity/engines/              11 engine files (see §1)
similarity/similarity_calibration.py    Cross-engine calibration utilities
similarity/similarity_diagnostics.py    Per-engine drift/health CLI
simulator/                       *** EMPTY scaffold — Phase 3 lands here ***
tests/                           23 test files; conftest at root + integration + regression
WORKFLOW.md                      End-to-end run guide (Windows cmd)
BACKLOG.md                       Sprint-by-sprint history, audit follow-ups, open tickets
CHANGES.md                       Shipped work, per-agent, per-sprint
backlog.xlsx                     Generated artifact; Phase 2 Closure Summary sheet at front
agent_team.md                    9-agent role definitions
PRODUCT_GUIDE.md                 Onboarding concepts
README.md                        Top-level orientation
.env                             Has DB_HOST_PORT=5433 (don't lose this)
docker-compose.yml               db / redis / app services
```

---

## 6. Phase 3 entry plan

The Phase 3 spec (SIM-300) is **missing on disk** — see §7. Until it's
reconstructed or rewritten, Phase 3 implementation cannot start. The
spec was supposed to define:

1. The pre-filter contract — which engines pre-filter by what
   (pitcher_id + bat_hand for pitch-to-pitch; bat_hand alone for
   batted-ball; nothing for situation since situation is itself the
   filter).
2. Sub-index materialization — how FAISS tiles are sharded on disk
   (`/data/play_pool/<season>/<pitcher_id>/<bat_hand>.faiss`).
3. Recency lifecycle — how often last-2-seasons rows are re-duplicated
   into tiles.
4. The `PlayPoolSampler` class API — four methods: `load_tile`,
   `sample_pitch`, `sample_batted_ball`, `reload_recent`.
5. Performance budget — total RAM ≤ 2 GB across play-pool + FAISS
   indexes regardless of worker count.

**Suggested Phase 3 kickoff sequence**:

1. **Reconstruct or replace SIM-300** — either rewrite the missing
   architecture spec from the points above (the audit's prioritized
   ticket list referenced it in detail) or de-scope it and embed the
   contract directly into SIM-301's first PR.
2. **SIM-118 perf benchmark harness** — without `tests/performance/`
   nothing else's SLA claims are verifiable. Treat as gate-zero.
3. **SIM-280 RAM budget + SIM-281 ProcessPool architecture decision** —
   these go before SIM-301 sizes any cache.
4. **SIM-202 run-value constants** — trivial but a prerequisite for
   Phase 4 cleanup. Slot wherever.
5. **SIM-301 play-pool cache serializer** + **SIM-302 sampler API** —
   the two flagship Phase 3 deliverables. Backend Dev lead; ML Eng
   pairs on SIM-302.
6. **SIM-220 backtesting framework** — required before any simulator
   output is verifiable. Likely spans two sprints.
7. **SIM-201 manager decision logic spec** — Baseball Analyst scopes
   in Phase 3, ships in Phase 3 or early Phase 4.

After Phase 3 ships SIM-301 + SIM-302 + SIM-303 (wire sampler into the
sim loop scaffold), Phase 4 (simulation loop) can begin.

---

## 7. Known gaps — must reconcile before Phase 3 feature work

> **UPDATE 2026-05-20 — mostly reconciled.** The Phase 3 kickoff sprint
> (`docs/SPRINT_2026-05-27_phase3_kickoff.md`) rebuilt the SIM-300 spec, the
> SIM-051 DuckDB migration `0003`, and all four missing test files
> (SIM-051 / SIM-162 / SIM-149 / SIM-150). Suite re-baselined to 833
> passing (now 927 after Phase 3 completion).
>
> **The missing `docs/audit/2026-05-21-*.md` are now superseded** by a fresh
> end-of-Phase-3 program audit: `docs/audit/2026-06-10-phase3-close-program-audit.md`
> + `docs/audit/2026-06-10-phase4-prioritized-tickets.md`. Phase 4 plan is in
> `docs/HANDOFF_PHASE4.md`.

These files are referenced in `BACKLOG.md` / `CHANGES.md` as shipped but
do not exist on disk as of this handoff. Almost certainly OneDrive sync
casualties (the project has a documented pattern of mid-edit truncation
on this folder — see §3). Each one is a P0 follow-up:

| Missing artifact | Claimed by | Impact | Suggested action |
|---|---|---|---|
| `docs/architecture/2026-05-20-play-pool.md` | SIM-300 | Phase 3 cannot start without an architecture spec. | Rewrite from the audit's ticket list, or de-scope SIM-300 and embed the contract into SIM-301. |
| `docs/audit/2026-05-21-program-audit.md` | "Audit 2026-05-21" | The 53-ticket follow-up list is unanchored; PM can't trace Tier-P0 priorities back to findings. | Re-do the audit at Phase 3 kickoff if memory of the original is gone. |
| `docs/audit/2026-05-21-prioritized-tickets.md` | "Audit 2026-05-21" | Same as above. | Same. |
| `db/migrations/duckdb/0003_*.sql` (SIM-051) | SIM-051 | The `pull_relative_spray_angle` column IS in `02_duckdb_schema.sql`, but there's no migration. Fresh DuckDB instances will get the column from the schema script; existing instances may not. | Write `0003_sim051_pull_relative_spray_angle.sql` that adds the column conditionally. |
| `tests/unit/test_data_engineer_sim051.py` | SIM-051 | No unit-test coverage of the handedness flip logic. | Write 4-5 tests: LHB pull, RHB pull, switch hitter using `bat_hand`, NULL row. |
| `tests/unit/test_data_engineer_sim162.py` | SIM-162 | No regression test that `LeagueAverageProfiles.compute()` produces non-empty inserts for all 5 entity types. | Write a single integration-style test against an in-memory DuckDB. |
| `tests/unit/test_baserunner_steal_engine.py` | SIM-149 | The only similarity engine without a dedicated unit test file. | Write 9 invariant tests (zero distance to self, ordering, partition behavior, etc.). |
| `tests/unit/test_ml_engines_sim150.py` | SIM-150 | No calibration regression coverage on catcher v2, FAISS pitch, FAISS batted-ball. | Write the three calibration tests the SIM-150 acceptance criteria require. |

**Also note**: `BACKLOG.md` and `CHANGES.md` both report "767 passed /
22 skipped" — that number predates any reconciliation. After re-creating
the missing test files, the new baseline will be different. Don't trust
the 767 figure once §7 is being worked.

---

## 8. Conventions worth preserving

- **Ticket IDs** continue sequentially in ID bands (see `BACKLOG.md` §"ID
  bands"). Next free numbers: `SIM-164+` for general, `SIM-304+` for
  Phase 3 implementation, `SIM-200/201+` reserved for Phase 4
  placeholders.
- **Migrations always paired with schema updates**. Every Alembic
  migration must be matched by a corresponding edit to
  `db/schemas/01_postgres_schema.sql`; same for DuckDB.
- **Tests live next to the agent that owns them**, named by ticket:
  `test_<role_short>_sim<NNN>.py` is the convention
  (`test_ml_engines_sim072.py`, `test_perf_eng_sim158.py`).
- **CHANGES.md grows; BACKLOG.md trims**. Once a sprint closes, ticket
  detail moves to CHANGES.md; BACKLOG.md retains a one-line table row.
- **Engine score discipline**: RBF engines return [0,1]. Geometric
  engines return distances. Never mix.
- **Windows cmd-formatted commands** in WORKFLOW.md (`^` continuation,
  `%VAR%` env-vars). Don't add bash-style examples without dual-tagging.

---

## 9. Quick-start for the next conversation

If the next session starts cold, this is the minimum context it needs:

1. Read this file (you're already doing it).
2. Read `BACKLOG.md` lines 1-40 — the new Phase 2 closure banner.
3. Read `WORKFLOW.md` for the end-to-end runbook.
4. Check `git status` and `git log --oneline -20` to see what's
   actually changed recently vs what the docs claim.
5. Run `make test` if a sanity check is wanted (`pytest tests/`
   directly works too).
6. If the user is starting Phase 3 feature work, **address §7 first**.
   If they're asking an audit question, the audit findings need to be
   rebuilt before the answer can be sourced.

The Postgres port (5433) and the OneDrive truncation pattern (§3) will
bite within the first hour of any new agent's session if they don't know
about them.

---

*End of handoff.*
