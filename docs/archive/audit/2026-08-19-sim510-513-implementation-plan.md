# SIM-510→513 — the implementation plan (2026-08-19)

**Status: PLAN. The design is closed.** The owner's decisions live in
`docs/audit/2026-08-18-sim510-513-fielding-transition-design.md` — read that first, do not
relitigate it. The ticket rows live under the 2026-08-18 banner in `BACKLOG.md` (lines 21-24).
This plan adds what those two documents do not carry: the execution sequence, the exact code
sites as of today, the flag strategy, the per-ticket tests and gates, and five findings from
the 2026-08-19 pre-implementation review.

## The epic in one paragraph

The simulation loop resolves a ball in play with hand-tuned formulas today. The epic replaces
them with two similarity-weighted draws, per the standing 2026-08-10 architecture rule. Draw
one (SIM-511) picks a real historical play — a "transition row" — that is legal in the live
base-out state. The drawn row IS the play: it supplies the event, the out count, WHO is out,
and all forced movement. Draw two (SIM-512) fires only in five discretionary scenarios where a
runner chooses to take an extra base. SIM-510 builds the data both draws need. SIM-513 deletes
the old code and certifies the loop on the 2025 bands.

## Five findings from the 2026-08-19 review

1. **No widening in the fielding draw — owner ruling 2026-08-19.** The design doc's "thin
   cells use the SIM-475 widening order" line is amended (inline, dated). The base-out filter
   is essential, so it never relaxes. There are only 24 base-out cells (8 runner
   configurations × 3 out states) and all are common. The count is soft conditioning, never a
   hard filter — widening would only matter if the count were hard-filtered too, and it is
   not. An empty cell is a data defect: the draw raises, it never widens. (Background from
   the review: no widening machinery exists in live code anyway — `steal_draw` returns `None`
   on a miss; only the measurement script `scripts/measure_filter_cells.py` exists. SIM-475
   stays out of this epic entirely.)
2. **SIM-511+512 land as ONE combined change — owner ruling 2026-08-19. No flag.** The
   station-to-station interim state never reaches `master`, so the flag question disappears.
   The owner's rationale, kept on record: the advancement draw is the realism win — a flat
   league-average send rate would send slow runners as often as fast ones, while the draw
   conditions on the runner's speed, his aggression, and the specific fielder's arm, which is
   what decides a real advance. The deletions listed in the SIM-511/512 rows happen IN the
   combined landing. The SIM-511 checkpoint smoke (station-to-station, runs low) is a
   mid-build checkpoint, never a landing.
3. **The `constants.py:177` alias needs a replacement, not a bare delete.** The
   `field_error → single` alias sits inside `STATCAST_EVENT_ALIASES`, which feeds
   `RUN_VALUES` lookups. An import-time assert requires every alias target to be a
   `RUN_VALUES` key, and strict mode raises `UnknownEventError` on an unmapped event. Promote
   `field_error` to its own canonical key (batter-reaches semantics) in the same change that
   drops the alias. **2026-08-19 addendum — the owner asked whether the run-value table can
   go: the NUMBERS can, the VOCABULARY cannot.** Production run accounting never reads the
   linear weights: the ledger accepts only the RE24-delta method (real pre/post states plus
   the runs that scored), and the context-free linear-weight path is consumed by tests alone
   (verified 2026-08-19 — grep across the repo). The keys and the alias map ARE load-bearing:
   the ledger's `canonical_event`, the linescore's hit counting, `db/sim_store`, and the
   acceptance lane's channel bucketing all key on them. Removal recipe (owner confirmed
   2026-08-19 — IN the combined landing's scope): replace `RUN_VALUES` with a plain
   canonical-key set, delete `run_value_for_event` and the linear-weight branch of
   `resolve_runs` (a state-free call becomes an error), update the SIM-312/319 tests.
   `DEFENSIVE_RUN_VALUES` (fielder/catcher engine features, §10) and the RE24 matrix (the
   ledger's accounting) are different tables and stay.
4. **Bookkeeping drift — FIXED 2026-08-19 (edits in the working tree, not yet committed).**
   (a) The old SIM-462/SIM-473 rows now read ⛔ SUPERSEDED by SIM-510/511/512. (b) The BACKLOG
   header stamp now reads 2026-08-19 / next free ID → SIM-514. (c) The SIM-494 row now
   carries the 2025 DP centre 0.7459 beside its 2023-era 0.8160. Caution kept on record: two
   July docs reuse the numbers SIM-462/473 for unrelated betting/frontend tickets; only the
   2026-08-10 sim-loop rows were touched.
5. **Validation lives in SIM-491, not SIM-513 — owner ruling 2026-08-19.** SIM-513 retires
   the legacy paths and certifies the loop; it contains NO nudge re-validation. SIM-491 owns
   re-validating the realism flags against the new draws — SIM-412 home-field (reads 0.5077;
   re-tune vs the 0.5428 centre), SIM-411 park, SIM-425b fielder-RBF — one at a time, ≥400
   sims × ≥20 games, AFTER SIM-513 lands. SIM-491's depends-on gains SIM-513 (BACKLOG row
   updated 2026-08-19).

## Build order

SIM-510 (data, additive, no behavior change) → SIM-511+512 (ONE combined landing: the
fielding draw + the advancement draw, legacy code deleted in the same change) → SIM-513
(certify + cleanup). SIM-511 and SIM-512 are each roughly a SIM-507-sized build, so the
combined landing is large — build it in reviewable internal commits. SIM-472
(pitch-similarity into the batted-ball draw) stays open separately; it is not part of this
epic.

---

## SIM-510 — transition columns + the advancement opportunity pools (M)

Owner: Data Engineer. Depends on nothing. Everything is additive; production behavior does
not change.

1. **Schema.** DuckDB migration `db/migrations/duckdb/0018_sim510_transition_columns.sql`
   (schema v17 → v18). Say "DuckDB" everywhere: Alembic also has an 0018 (`raw.play_events`)
   in its own series.
   - `ALTER sim.outcome_pool ADD COLUMN IF NOT EXISTS`: pre-play runner ids
     (`on_1b/2b/3b`), post-play ids (`post_on_1b/2b/3b`), per-runner scored and
     out-advancing flags, who-was-out. New columns LAST — the positional-INSERT trap.
   - `CREATE TABLE IF NOT EXISTS sim.advancement_opportunity_pool`: ONE table with a
     `scenario` discriminator (mirrors the steal pool's `target_base`). Columns: scenario,
     attempted, outcome (safe / out / error-extra-base), runner_id, the row's `fielder_id`,
     spray / EV / distance, outs, season, game keys, `recency_weight`. Non-attempt rows are
     the point (the SIM-468 denominator lesson).
   - Mirror both in `db/schemas/02_duckdb_schema.sql`. Bump
     `db/schemas/duckdb_schema_version.txt` 17 → 18. Update the version test in
     `tests/unit/test_sim_store.py`.
2. **The five scenarios and their labels** (from `raw.pitches` pre/post state — the columns
   exist; the baserunner profiles already read them):

   | # | Scenario | Opportunity condition | Attempted when | Outcome from |
   |---|---|---|---|---|
   | 1 | 1st → 3rd on a single | runner on 1B, event = single | post state beyond 2B | scored / out-advancing / post base |
   | 2 | 2nd → home on a single | runner on 2B, event = single | post state beyond 3B | same |
   | 3 | 1st → home on a double | runner on 1B, event = double | post state beyond 3B | same |
   | 4 | Tag up on a fly out | air out, runner on 2B or 3B | runner advanced | same |
   | 5 | Batter stretch | event = single or double | batter's post base > hit class | same |

3. **Builders** (`pipeline/batch/player_profile_computor.py`): extend the outcome-pool SELECT
   with the new columns (order = DDL order); add the opportunity-pool builder. Bump
   `POOL_BUILDER_VERSION` `sim509.1` → `sim510.1`.
4. **Artifacts.** Export in `pipeline/batch/engine_artifacts.py` (clone the
   `steal_pool/{2,3}` npy + meta.parquet layout, keyed by scenario), add
   `EngineArtifacts.{extract,attach}_shared_views` entries, and load in
   `simulation/full_pool_sampler.py`. The new outcome-pool columns must also flow into the
   batted-ball pool export that `battedball_draw` reads.
5. **Tests.** Clone `test_the_positional_insert_matches_the_ddl` for BOTH tables. Builder
   unit tests on synthetic rows. The version-file test.
6. **Rebuild + validate at scale.** Chain: profile computor → play_pool_cache →
   engine_artifacts (`--seasons 2017 … 2026 --full-rebuild`; detached
   `docker compose run -d`; verify `sim.pool_build_metadata.builder_version`). Validate on
   HUNDREDS of games: per-scenario attempt and success rates vs published MLB baserunning
   rates; who-was-out totals vs `raw.pitches` event labels; every DP row must name a
   doubled-off runner.
7. **Confirm the 24 base-out cells (feeds SIM-511).** Count outcome-pool rows per runner
   configuration × outs cell. The owner expects every cell to be common. If one reads thin,
   investigate the data — never widen (finding 1).

Exit gate: pools rebuilt and validated at scale; unit + regression lanes green (nothing
behavioral changed); the 24 base-out cell counts confirmed.

## SIM-511 — the transition fielding draw (L)

Owner: Backend Developer + ML Engineer. Baseball Analyst reviews the transition semantics.
Depends on SIM-510. Lands WITH SIM-512 as one combined change (finding 2) — this section is
the first half of that build.

1. **No widening (finding 1).** The hard filter is the base-out cell alone — 24 cells,
   never relaxed. The count, batter, and situation stay soft kernel conditioning. An empty
   cell raises (clone the situation engine's zero-row guard); it never widens.
2. **The draw.** Hard filter = runner configuration × outs, on top of the existing soft
   conditioning (batter/situation kernel, platoon). The drawn row returns the full
   transition: event, `result_outs`, who is out, per-runner post state.
3. **Normalization at draw time (the double-count guard, design decision 1).** Strip the five
   discretionary movements back to station-to-station. The batter's base is conservative
   (single = 1B, double = 2B — decision 6).
4. **The loop.** `_full_pool_fielding` (`sim_loop.py:1496`) consumes the transition;
   `_resolve_in_play` applies the row's movement instead of `_advance_runners`' uniform push
   and `_full_pool_out_advancement`. The phantom-DP guard (~1536-1548), the
   `outs = 0 if rh else 1` inference (~1550), and the SIM-473 discard note (~1525) are
   DELETED in the combined landing.
5. **`runners_retired` becomes real** at `_commit_run_delta` (~2777). Ship the SIM-494
   out-count-versus-bodies check as a regression test IN THE SAME CHANGE — the SIM-494
   detection note's explicit instruction.
6. **SIM-496, both halves.** The row carries batter-reached truth, and `field_error` becomes
   a canonical outcome key (finding 3). A drawn error puts the batter on base and is not a
   hit in the box score.
7. **Fielder's choice.** The row states who is out, so the `_OUT_EVENTS` batter-out
   assumption stops mattering on the flag-on path (the batter reaches on 90.5% of FC rows).
8. **Tests.** The unit lane pins the production flags OFF (`tests/conftest.py`), so these
   tests enable the full-pool sampler path explicitly — the steal-draw tests are the model.
   Cover: drawn-row legality in every base-out state; a DP removes the named runner; FC
   batter reaches; ROE reaches; a liner doubles off the runner; compound errors; force-play
   third-out timing from the row.
9. **Checkpoint smoke (mid-build, not a landing).** 20-40 games × 200 sims. Expect: DP rises
   toward ~0.75/team-game, `ROE_reached` > 0, and R LOW — station-to-station until SIM-512
   completes the combined change. Do not read R as a verdict here.

Checkpoint gate (nothing lands yet): the new unit tests green; the checkpoint smoke shows
the DP/ROE mechanics working.

## SIM-512 — the five-scenario advancement draw (L)

Owner: ML Engineer + Backend Developer. Baseball Analyst reviews Rule 5.08 timing and the
sac-fly labeling. Depends on SIM-510 and SIM-511 — the second half of the combined landing
(finding 2).

1. **The draw.** `advancement_draw(scenario, …)` over the SIM-510 opportunity pools.
   Weights: runner similarity (sprint speed), the LIVE fielder's arm vs the row's fielder,
   a spray/EV/distance kernel, outs, recency. Returns hold / advance-safe / advance-out /
   advance-plus-error.
2. **Loop wiring.** After the fielding transition applies, enumerate the applicable
   scenarios. Resolve lead-first with can't-pass occupancy. If the lead runner does not
   attempt, no trailing draws — EXCEPT the batter-stretch draw, which stands alone
   (advance-on-the-throw is folded into it; accepted approximation).
3. **Rule 5.08 timing.** Every advancement out is a tag play. A run that crossed before a
   trailing runner's tag-out counts. Lead-first resolution gives the ordering nearly free.
4. **The sac fly.** A tag from 3B that scores on a fly out labels the play `sacrifice_fly`
   post-hoc (AB/RBI per SIM-312).
5. **Deleted in the combined landing:** `_tag_rate` (~1611, the 0.92), the 0.28/0.30/0.35
   constants in `_full_pool_out_advancement` (~1625), `_extra_advance` (~1887),
   `_advance_rate`, plus the SIM-511 list above.
6. **Tests.** Per-scenario draws; can't-pass occupancy; the short-circuit; Rule 5.08 timing
   cases; seed determinism.
7. **Landing smoke + fixtures.** Anchors: 1st→3rd-on-a-single rate vs MLB (~28-30%), the
   sac-fly rate, the advance-out rate; R recovers from the mid-build checkpoint. Regenerate
   any golden fixture the combined landing changes — this landing is the intentional model
   change.

Exit gate (the combined landing): unit + regression green with regenerated fixtures; the
landing smoke shows the five scenarios firing at plausible MLB rates.

## SIM-513 — retire the legacy paths + certify (M + lane time)

Owner: Backend Developer. QA cross-validates — QA never self-certifies. Depends on SIM-511
and SIM-512.

1. **Cleanup.** The legacy code went with the combined landing (finding 2). Delete the dead
   remains here: `_apply_sac_fly_bias` and the manager's sac-fly-intent plumbing (SIM-349's
   hit-and-run logic stays — only the sac-fly intent is superseded).
2. **No validation here (finding 5).** SIM-491 owns the nudge re-validation, after this
   ticket lands. Hand it the mechanism question: SIM-425b flips out↔single BEFORE the draw,
   and a flipped event must stay consistent with a drawn transition row — the flip may need
   to become a re-draw or a conditioning weight.
3. **Certification.** Confirm the golden fixtures were regenerated at the combined landing
   (`python tests/regression/generate_fixtures.py --force` — intentional model change). Run
   the 12×471 lane (`SIM_ACCEPTANCE=1 SIM_ACCEPTANCE_ITERS=471 SIM_ACCEPTANCE_TIMEOUT=16200
   SIM_RUNNER_WORKERS=6`, production flags, detached container, `tests/` + `scripts/`
   volume-mounted). Delete each strict xfail (DP, H, ROE_reached) when its band lands — an
   XPASS reds the lane otherwise.
4. **Read the WHOLE scoreboard.** DP and ROE move R in BOTH directions. Re-check the
   CS/3B/ROE overdispersion (sim sd > sd_ref) — correct DP/ROE mechanics may be its cause.
   For every green channel, check where its probe reads: at the draw or after resolution
   (the SIM-496 lesson).
5. **Housekeeping.** Verify-and-close the stale SIM-455/SIM-484 filings. Confirm the
   SIM-462/473 supersession landed (finding 4). Update CHANGES.md, BACKLOG.md, the sprint
   doc, and the CLAUDE.md §2/§11 defect bullets (DP/ROE join the resolved list).

Exit gate: the 12×471 lane on the 2025 bands with DP and ROE_reached inside their bands
(centres 0.7459 and 0.2078) and no fresh regression on R/H/K. BB (+12.8%) and the
power-side highs stay SIM-429's — parked by the owner until this epic lands. The nudge
re-validation follows as SIM-491.

---

## Standing gates on every ticket

`make test-unit` + `make lint` + `make type-check` before commit; `make test-regression`
after any engine/model change; validate data work on hundreds of games; a DuckDB schema
change ships the numbered SQL + the version-file bump together; the DuckDB write-lock — a
running lane blocks profile-computor writes.

## Risks

1. **Cell truth.** The base-out hard filter splits the pool across 24 cells (8 runner
   configurations × 3 out states). The owner expects all to be common; the SIM-510 count
   confirms it. A thin cell is a data question to investigate — never a reason to widen.
2. **Nudge interaction** (SIM-425b flips vs transition rows) may force a mechanism rework —
   now SIM-491's question, flagged there.
3. **One large landing.** SIM-511+512 together is a big review surface. Mitigations:
   reviewable internal commits, the mid-build checkpoint smoke, and unit tests that enable
   the production path explicitly — the unit lane pins production flags OFF, and the
   production path once had ZERO test references (the AUD-QA-PRODPATH audit row).
4. **Perf.** The transition draw replaces the event draw 1:1 and the advancement draw fires
   on a minority of plays, so cost should be near-neutral — but confirm on the SIM-436
   measure before the lane (the ~38 s n=100 budget has no slack).

## Estimates

| Ticket | Size | Wall-clock beyond build |
|---|---|---|
| SIM-510 | M | pool rebuild (minutes-hours) + scale validation |
| SIM-511+512 | L + L (one combined landing) | checkpoint + landing smoke; fixture regen |
| SIM-513 | M | the 12×471 lane (~3.5 h) |

SIM-491's nudge sweeps (≥400 sims × ≥20 games, one flag at a time) follow after SIM-513 —
the SIM-412 home-field re-tune rides there. The full home_win_pct certification (12×2,168,
~8-16 h) stays a separate open item.
