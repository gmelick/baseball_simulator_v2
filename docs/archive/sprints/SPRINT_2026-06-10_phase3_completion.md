# Sprint 2026-06-10 — Phase 3 Completion (executed 2026-05-21)

*Author: Product Manager (Agent 1, orchestrator) · Closed 2026-05-21 · Disposition: ✅ all 6 tickets accepted after independent QA cross-validation*

This sprint closes out **Phase 3 — Play Pool Architecture**. The flagship deliverables
(SIM-300 spec, SIM-301 cache, SIM-302 sampler, SIM-303 sim-loop wiring) landed in prior
sprints; this one completes the remaining play-pool chain: the engine registry, sampling
recency weighting + incremental pool rebuild, the query-column contracts, the sim-pool
index strategy, and the foul-ball weighting design.

## 1. Tickets and owners (dependency-ordered)

| Ticket | Owner(s) | Deliverable |
|---|---|---|
| SIM-048 | ML Engineer | `similarity/registry.py` (SimilarityEngineRegistry) + tests |
| SIM-076 | Data Engineer + ML Engineer | recency_weight on the 3 sim pools + `pool_build_metadata` + migration 0004 + computor population + walk-forward harness |
| SIM-095 | Data Engineer | incremental pool rebuild (skip fresh seasons) |
| SIM-111 | Backend Developer + Data Engineer | `docs/architecture/2026-05-21-play-pool-query-contracts.md` |
| SIM-115 | Data Engineer + Performance Engineer | migration 0005 — prune sim-pool indexes |
| SIM-056 | Baseball Analyst | `docs/architecture/2026-05-21-foul-ball-weighting.md` |

The chain is rooted at SIM-048 (registry) → SIM-076 (recency) → {SIM-111 → SIM-115/SIM-056,
SIM-095}. SIM-074 and SIM-076/SIM-095 all edit the 4,300-line profile computor, so the
computor changes were made surgically by the orchestrator (the documented OneDrive
truncation makes agent edits to that file unreliable); new-file work (registry, harness,
docs) was delegated to role subagents.

## 2. Per-ticket result

**SIM-048 — SimilarityEngineRegistry.** `similarity/registry.py` registers all 11 engines
by canonical name with an `EngineSpec` (family + score_type + description). Score-discipline
encoded: the 8 RBF/GMM engines are `similarity` ([0,1]); the 3 geometric engines
(situation KDTree, pitch-to-pitch FAISS, batted-ball FAISS) are `distance`. Imports are
lazy/guarded so the registry imports cleanly even without faiss/ot. 10 tests.

**SIM-076 — recency weighting.** `recency_weight FLOAT DEFAULT 1.0` added to
`sim.pitch_pool`/`outcome_pool`/`stolen_base_pool` (schema + DuckDB migration 0004, version
→ 5) and a new `sim.pool_build_metadata` table. The computor's three pool builders now emit
`recency_weight` via `_recency_weight_sql()` (mirrored by the pure-Python `recency_weight()`):
2.0 for the most-recent two seasons, ×0.75/season decay, floor 0.25, relative to the build's
reference season — matching the engines' recency-boost strategy. `_record_pool_build()`
writes the per-(pool,season) watermark. A reusable walk-forward recency-validation harness
lives at `similarity/backtesting/recency_walk_forward.py` (SIM-220 will feed it real data).
19 tests (10 helper/migration + 9 harness).

**SIM-095 — incremental rebuild.** `_seasons_needing_rebuild()` compares
`pool_build_metadata.source_max_game_date` against the current source watermark and returns
only changed/missing seasons; the builders accept `incremental=` and `run()` passes
`incremental = not full_rebuild`, so the nightly job rebuilds only seasons whose source
advanced (full rebuild still available via `--full-rebuild`).

**SIM-111 — query column contracts.** `docs/architecture/2026-05-21-play-pool-query-contracts.md`
formalizes, per pool, the columns the sampler + sim loop rely on, the pre-filter keys
(pitch: pitcher_id+stand; batted-ball: stand; SB: participant ids), the access-pattern →
index mapping that drove SIM-115, the `recency_weight` semantics, and a column stability
contract.

**SIM-115 — index pruning.** Migration 0005 drops 8 write-overhead indexes on
`sim.pitch_pool` and 9 on `sim.outcome_pool` (schema-qualified `sim.` names — an unqualified
`DROP INDEX` silently no-ops because the indexes live in the `sim` schema; caught by the
test), keeping the query-path set: `idx_pp_pitcher_season`, `idx_pp_outcome`, `idx_pp_count`,
`idx_op_season`. Rationale: DuckDB's columnar zone maps cover bulk-scan columns, and the
play-pool path pre-filters by pitcher then reads by PK. 4 tests.

**SIM-056 — foul-ball weighting design.** `docs/architecture/2026-05-21-foul-ball-weighting.md`
(+ illustrative `docs/data/foul_rate_by_count.csv`) designs count-stratified foul-frequency
weighting: a count→foul-frequency factor applied at the sim loop's outcome-determination
step (the sampler stays count-blind and distance-pure), plus the non-negotiable rule that a
two-strike foul leaves the strike count unchanged, and a validation plan (foul-rate-by-count
and pitches-per-PA vs league reference).

## 3. Verification

Independent QA pass (run in chunks; the full suite exceeds the 45 s sandbox limit):
**unit 872 + regression 55 = 927 passed / 1 skipped / 0 failed**; performance 3 passed /
2 skipped. Up from the 870 pre-sprint baseline; the 4 new test files add 33 tests
(sim048 10, sim076_sim095 10, sim076_harness 9, sim115 4). Engine score discipline intact;
the `recency_weight` column is last in each pool table and each builder SELECT (so the
`SELECT *` insert stays aligned); `player_profile_computor.py` imports cleanly. No defects.

### Environment notes (carried from prior sprints)
OneDrive truncated the large computor + schema/migration files on the sandbox mount during
editing; authoritative files verified complete via the file tools, mount copies rebuilt for
the QA run. Tests run with `PYTHONPATH=/tmp/sbshim` (datetime.UTC shim),
`PYTHONPYCACHEPREFIX=/tmp/pyc3` (avoids stale locked bytecode), and `-p no:cacheprovider`.

## 4. Phase 3 status

**Phase 3 (Play Pool Architecture) is COMPLETE.** Done across sprints: SIM-300 (spec),
SIM-301 (nightly cache), SIM-302 (sampler), SIM-303 (sim-loop wiring), SIM-048 (registry),
SIM-076 (recency), SIM-095 (incremental rebuild), SIM-111 (query contracts), SIM-115 (index
strategy), SIM-056 (foul weighting design). The remaining "Phase 3 Gate" backlog rows are
NOT play-pool work and stay open by design: SIM-127/128/129 (frontend — Phase 6, need
wireframes), SIM-107 (live-pipeline tests — needs the SIM-152 conftest), SIM-120 (needs the
Phase 4 `simulate_game()`).

## 5. Open follow-ups / next (Phase 4)

1. **Phase 4 simulation loop** — flesh out the SIM-303 scaffold (manager decisions,
   fielding/baserunning resolution, full 8-step loop, count state machine incl. the SIM-056
   two-strike-foul rule).
2. **SIM-220 backtesting framework** — consumes the SIM-302 distribution API and the SIM-076
   walk-forward harness.
3. **SIM-201 manager decision logic.**
4. Housekeeping: tidy the DuckDB-tolerated trailing comma at `player_profile_computor.py`
   pitch_pool builder; remove the stray `tests/unit/test_data_engineer_sim085_to_091.py.tmp`.

---

*End of sprint log.*
