# SIM-408 reconciliation plan — engine ↔ DuckDB schema (turn-key)

*Author: Data Engineering + Backend. Companion to the diagnosis in
`docs/audit/2026-05-29-sim408-engine-schema-divergence.md`. This doc is the
**execution spec**: per-engine canonical-direction decisions, the concrete
column/table actions, and the rebuild → fixtures → verify checklist. It is
written to be run at the live box (raw Statcast + a multi-hour DuckDB rebuild),
which the dev sandbox cannot do.*

## Status & locked decisions (2026-05-29)

**Landed on `fix/sim402-live-bringup` (code-side; verified in-sandbox, 🔴 pending the live rebuild):**

- **situation** ✅ — `derived.at_bat_situations` built (commit `cc7fb60`).
- **baserunner_steal** ✅ — `derived.baserunner_steal_metrics` + JUMP sub-score
  TRIMmed (commit `cc7fb60`).
- **pitcher_steal** ✅ — `derived.pitcher_steal_metrics`, reduced to an
  **outcome-only** engine (Delivery biomech + Pickoff have no raw source) (commit
  `ede86c7`).
- Migration **0011** carries those 3 tables; `duckdb_schema_version` 10 → 11.

**Remaining — decisions locked, work specced (turn-key):**

- **catcher** (largest; EXTEND-heavy). **Decision: TRIM the Offense sub-score**
  (the catcher's own batting — `k_rate`/`walk_rate`/`avg_exit_velo`/
  `hard_hit_rate`); renormalize the 4 defensive weights (framing/blocking/
  throwing/deterrence) over 0.85. EXTEND the 4 defensive sub-scores:
  - **Engine-derivable** (rewrite the `_load` SELECT to compute from existing
    `catcher_season_metrics` columns): `strike_rate_vs_expected` =
    `(called_strikes − expected_called_strikes)/called_pitches`;
    `runs_saved_framing` = `framing_runs`; `pop_time_avg` = `pop_time_mean`;
    `arm_strength_mph` = `arm_strength_mean`; `cs_rate`,
    `steal_attempt_rate_against` already exist.
  - **Needs new computor columns** (the framing/blocking passes don't emit
    these): `shadow_zone_strike_rate` + `heart_zone_strike_rate` (bucket called
    pitches by `raw.pitches.zone` — heart = zones 1–9, shadow = 11–14 — in
    `_compute_catcher_framing`); `block_success_rate` + `wild_pitch_prevention_rate`
    (need `block_opps`); `passed_ball_rate_per_9` (needs innings caught). Add
    these to the framing/blocking temp tables + `_aggregate_catcher_season_metrics`
    + the schema, OR TRIM the ones whose denominators aren't worth materializing.
  - **TRIM** `exchange_time_avg` (not separable from `pop_time` in the feed).
  - Update `conftest`/`generate_fixtures`/`regression_config`/`test_engine_regression`
    + `test_ml_engines_sim066_071` (drop offense + exchange_time); regenerate
    `catcher.json`.

- **manager**. **Decision: build aggression + platoon now; GATE Usage on
  SIM-427.** The aggression (`steal_order_rate_per_1b_opp`,
  `hit_and_run_rate_per_opportunity`, `sac_bunt_rate_high/low_leverage`,
  `squeeze_play_rate_per_3b_opp`) + platoon (`pinch_hit_rate_*`,
  `defensive_sub_rate_late_innings`, `double_switch_rate_per_reliever_change`,
  `platoon_advantage_exploitation_rate`) tendencies are computable from the play
  stream (substitution flags + base/out state). The Usage block
  (`starter_avg_pitch_count`, `starter_pull_pct_before_100`,
  `closer_entry_leverage_index`, `high_leverage_reliever_rate`,
  `opener_usage_rate`, `bulk_innings_rate`) needs the per-(team,season)
  bullpen-roster-with-roles that **SIM-427** must build from raw Statcast — emit
  those columns as NULL with `below_minimum_sample` semantics until then, keeping
  the engine's 3-sub-score architecture intact. The existing
  `_compute_manager_profiles` produces a different vocabulary
  (`ph_rate_*`/`sp_pitch_count_mean`); it must be rewritten to the engine's
  `sample_starter_decisions` + usage/aggression/platoon column set (+ schema +
  fixture/test updates).

Both remaining engines are large multi-method computor changes; the registry
degrades safely (10/11 or 9/11 boots fine) so they can land incrementally.

## 0. The decision rule (canonical direction)

For each failing engine, pick ONE direction:

- **EXTEND-COMPUTOR** — add the engine-expected columns/table to
  `pipeline/batch/player_profile_computor.py` (+ `db/schemas/02_duckdb_schema.sql`).
  Use this whenever the feature is **computable from raw Statcast**; it preserves
  the designed similarity feature set + the engine unit-test contract.
- **TRIM-ENGINE** — drop the feature from the engine's `SELECT`/feature list (and
  its unit-test mock) when the feature is **not derivable from Statcast** (biomech
  timings, scout grades). Trimming is strictly better than emitting a fabricated
  column.
- **NEW-BUILDER** — materialize a table the computor never built at all.

Default bias: **EXTEND-COMPUTOR** where computable; **TRIM** only the genuinely
uncomputable features; flag anything that overlaps the data-blocked SIM-427
(manager/bullpen-from-raw) so it isn't double-built.

## 1. Per-engine summary

| Engine | Live failure | Table state | Direction | Blocks |
|---|---|---|---|---|
| `situation` | `at_bat_situations` missing → 0 rows → NaN | **table absent** | NEW-BUILDER (fully computable) | — |
| `catcher` | `csm` has no `catcher_id` (+ ~30 cols absent) | table exists, wrong vocab | EXTEND-COMPUTOR (mostly) + TRIM (scout grades) | SIM-406 |
| `manager` | `msm` has no `sample_starter_decisions` (+ 16 tendency cols) | table exists, wrong vocab | EXTEND-COMPUTOR; overlaps **SIM-427** | SIM-406 |
| `baserunner_steal` | `baserunner_steal_metrics` missing | **table absent** | NEW-BUILDER + TRIM (biomech) | SIM-406 |
| `pitcher_steal` | `pitcher_steal_metrics` missing | **table absent** | NEW-BUILDER + TRIM (biomech) | SIM-406 |

The other 7 engines build fine (pitcher, batter, fielder, baserunner-advance,
pitch-pitch, batted-ball, + league_averages). The **production full-pool sim is
unaffected** — it draws from the `engine_artifacts` bundle, not these live
engines (per the finding doc).

## 2. `situation` — NEW-BUILDER (lowest risk, do first)

Engine `SELECT` (`similarity/engines/situation_similarity.py:474`) reads
`derived.at_bat_situations abs` joined to `derived.park_factors`:
`play_id, game_pk, inning, top_or_bottom, outs_when_up, on_1b, on_2b, on_3b,
home_score, away_score, leverage_index, pitcher_pitch_count, batter_pa_count,
season, venue_id, at_bat_number`.

**Every field is derivable** from the existing pitch stream / play pool — these
are pre-pitch game-state facts already reconstructed elsewhere in the sim. Action:
add a nightly materialization of one row per PA (at the first pitch of the PA) to
the computor, schema-version it, and index it. No raw-Statcast gap. This alone
restores the situation engine (and removes the degenerate-empty path; see §6).

## 3. `catcher` — EXTEND-COMPUTOR (largest divergence)

Engine wants (`catcher_similarity.py:519`): `catcher_id` + framing/blocking/throwing
**rates** (`strike_rate_vs_expected, runs_saved_framing, shadow_zone_strike_rate,
heart_zone_strike_rate, block_success_rate, passed_ball_rate_per_9,
wild_pitch_prevention_rate, pop_time_avg, exchange_time_avg, arm_strength_mph`),
the `sample_*` counts, and **4 offense cols** (`k_rate, walk_rate, avg_exit_velo,
hard_hit_rate`).

Computor produces (`player_profile_computor.py:4018`, `02_duckdb_schema.sql:472`):
`player_id` + framing/blocking **runs/counts** (`framing_runs, strikes_above_average,
blocks_above_average, blocking_runs, pop_time_mean, arm_strength_mean, cs_rate, …`).
**Overlap = `season, cs_rate, steal_attempt_rate_against, below_minimum_sample` only.**

Actions:
1. **`catcher_id`**: alias/emit `player_id AS catcher_id` (or rename in the engine —
   cheap, but the engine name is the contract elsewhere; prefer aliasing in the view).
2. **Framing/blocking/throwing rates**: the computor has the raw counts
   (`called_pitches, called_strikes, expected_called_strikes, framing_runs,
   pop_time_mean, arm_strength_mean`). Derive the engine's *rate* columns from them
   (e.g. `strike_rate_vs_expected = (called_strikes - expected_called_strikes)/called_pitches`;
   `pop_time_avg = pop_time_mean`). **Computable — EXTEND.**
3. **Shadow/heart zone strike rates**: needs per-pitch zone tagging in the framing
   pass. **Computable from Statcast `zone`/plate coords — EXTEND** (add the two zone
   buckets to the framing aggregation).
4. **4 offense cols** (`k_rate, walk_rate, avg_exit_velo, hard_hit_rate`): these are
   the catcher's *batting* profile — already computed for `batter_season_metrics`.
   **EXTEND** by joining the batter profile on `player_id`, OR **TRIM** if the
   catcher engine shouldn't mix offense into a defensive-similarity score (Baseball
   Analyst call — recommend TRIM unless the offense sub-score is intended).
5. **Scout-grade timings** (`exchange_time_avg`, `arm_strength_mph` if not in the
   Statcast feed for the season): **TRIM** any that aren't in the historical feed.

## 4. `manager` — EXTEND-COMPUTOR (overlaps SIM-427)

Engine wants (`manager_similarity.py:407`) on `derived.manager_season_metrics`:
`manager_id, season, sample_games, sample_starter_decisions` + **6 usage**
(`starter_avg_pitch_count, starter_pull_pct_before_100, closer_entry_leverage_index,
high_leverage_reliever_rate, opener_usage_rate, bulk_innings_rate`) + **5 aggression**
(`steal_order_rate_per_1b_opp, hit_and_run_rate_per_opportunity,
sac_bunt_rate_high_leverage, sac_bunt_rate_low_leverage, squeeze_play_rate_per_3b_opp`)
+ **5 platoon** (`pinch_hit_rate_*, defensive_sub_rate_late_innings,
double_switch_rate_per_reliever_change, platoon_advantage_exploitation_rate`)
+ `below_minimum_sample`. Binder fails at `sample_starter_decisions`.

The aggression + platoon tendencies are computable from the play stream. The
**usage** block (starter pull, closer/reliever roles) needs the per-(team,season)
**bullpen-roster-with-roles** that **SIM-427 already flags as missing from
`derived.*`** (no team/role/GS in `pitcher_season_metrics`; must be built from raw
Statcast). Action: build the manager-metrics table in the computor; the usage
sub-block is **gated on the SIM-427 roster build** — do them together, or ship the
aggression/platoon columns first and leave usage NULL-with-`below_minimum_sample`
until the roster exists. Do **not** double-build the roster.

## 5. `baserunner_steal` / `pitcher_steal` — NEW-BUILDER + TRIM

Neither `derived.baserunner_steal_metrics` nor `derived.pitcher_steal_metrics`
exists. Split each engine's feature list by computability:

**baserunner_steal** (`baserunner_steal_similarity.py:454`):
- **Computable (EXTEND/NEW-BUILDER)**: `sample_steal_attempts, sample_first_base_opps,
  steal_attempt_rate, steal_attempt_rate_2b` + success-group rates (SB/CS aggregates
  straight from raw Statcast events).
- **Biomech, NOT in Statcast (TRIM)**: `lead_distance_tendency,
  disengagement_response_rate, reaction_time_ms, burst_distance_ft, break_angle_deg`.

**pitcher_steal** (`pitcher_steal_similarity.py:397`):
- **Computable**: `sample_baserunner_events, sample_steal_attempts_against, throws`,
  pickoff/outcome rates (`pickoff_attempt_rate, pickoff_success_rate,
  disengagement_rate_per_pa`, SB-allowed/CS rates).
- **Biomech, NOT in Statcast (TRIM)**: `delivery_time_to_plate_s,
  stretch_delivery_time_s, lhp_first_to_home_time_s, quick_pitch_rate,
  slide_step_usage_rate`.

After trimming, build the two `*_steal_metrics` tables from the SB/CS/pickoff event
aggregates. (The production sim already does a manager-independent steal decision
from `runner_rate`/`catcher_framing` in the full-pool path, so this is about the
live **engine suite** + similarity API, not the production steal realism.)

## 6. Safe hardening already landed (independent of the rebuild)

`SituationSimilarityEngine.build()` now **raises on a zero-row index** instead of
fitting an empty matrix (`similarity/engines/situation_similarity.py`), so
`api.state.build_all_engines` *skips* it (honest 7/11) rather than registering a
NaN-poisoned engine. This is the optional hardening flagged in the finding doc;
once §2 materializes `at_bat_situations`, the engine builds normally. Unit tests
updated to the new contract (`tests/unit/test_situation_similarity.py`).

## 7. Execution checklist (at the live box)

1. Land the computor/schema changes (§2–§5) behind a **new DuckDB migration**
   (`db/migrations/duckdb/00NN_*.sql`) and **bump `db/schemas/duckdb_schema_version.txt`**
   (the version-file gotcha in CLAUDE.md §7).
2. Rebuild profiles: `make profile-computor` → `make play-pool-cache`.
3. **Regenerate regression fixtures** (when an engine's features/weights change):
   `python tests/regression/generate_fixtures.py --force`. NOTE: the regression
   fixtures are built from **synthetic, seeded** profiles (`conftest.py` /
   `generate_fixtures.py` — NOT the real DuckDB), so regeneration is
   deterministic and runs **in-sandbox** — it does NOT need the live rebuild.
   (This corrects an earlier assumption; the steal-engine fixtures were
   regenerated in-sandbox.) Regenerate only the engine(s) you changed.
4. Boot the app; confirm the lifespan logs `build_all_engines: 11/11`.
5. Run `make test-regression` + `make test-unit`; update any engine unit-test mocks
   touched by a TRIM.
6. Re-run `scripts/diag_actor_cols.py` (introspects `derived.*` column vocab) to
   confirm the produced columns match each engine's `SELECT`.

## 8. Risk / rollback

- The registry already degrades safely (7/11 boots); this work is **additive** —
  if a rebuild stalls, the app still serves on the 7 engines + the full-pool sim.
- The only hard gate is the **regression fixtures**: never commit regenerated
  fixtures from anything but the real rebuilt DB.
- Sequence by risk: **situation (§2) → steal builders (§5) → catcher (§3) →
  manager (§4, gated on SIM-427)**. Each is independently shippable.
