# SIM-408 finding — engine ↔ DuckDB schema divergence (live bring-up, 2026-05-29)

*Author: Backend / Data Engineering. Surfaced during the SIM-402 live-env bring-up
(`docker compose up app`), when the lifespan first built all 11 engines against the
real `/data/baseball_sim.duckdb`.*

## Symptom

`build_all_engines: 7/11 engines built`. Four engines are skipped and one builds
empty:

| Engine | Live error |
|---|---|
| `baserunner_steal` | `Table ... baserunner_steal_metrics does not exist` (did you mean `baserunner_season_metrics`) |
| `pitcher_steal` | `Table ... pitcher_steal_metrics does not exist` (did you mean `pitcher_season_metrics`) |
| `catcher` | `Table "csm" does not have a column named "catcher_id"` (candidate: `player_id`) |
| `manager` | `Table "msm" does not have a column named "sample_starter_decisions"` (candidate: `sample_games`) |
| `situation` | `at_bat_situations does not exist` → **0 situations indexed** → `Mean of empty slice` / `Degrees of freedom <= 0` (degenerate, NaN normalization) |

## Root cause — NOT stale column typos

This is **not** a set of renamable column typos. The engines query a schema that the
profile computor + `db/schemas/02_duckdb_schema.sql` never produced. The engines have
only ever been verified against their unit-test **mock** tables; the live 11-engine
build over the computor's real output (**SIM-408**) was never run, so the divergence
stayed latent.

**Catcher — illustrative (verified end-to-end):**

- Engine `SELECT` (`similarity/engines/catcher_similarity.py:519`) wants:
  `catcher_id, sample_pitches_received, sample_block_opps, sample_steal_attempts_against,
  strike_rate_vs_expected, runs_saved_framing, shadow_zone_strike_rate,
  heart_zone_strike_rate, block_success_rate, passed_ball_rate_per_9,
  wild_pitch_prevention_rate, pop_time_avg, exchange_time_avg, arm_strength_mph,
  k_rate, walk_rate, avg_exit_velo, hard_hit_rate, ...`
- Computor INSERT (`pipeline/batch/player_profile_computor.py:4018`) + schema
  (`02_duckdb_schema.sql:472`) produce:
  `player_id, pitches_received_total, called_pitches, called_strikes,
  expected_called_strikes, strikes_above_average, framing_runs,
  framing_low/high/inside/outside, expected_pbwp, actual_pbwp,
  blocks_above_average, blocking_runs, blocks_aa_*, pop_time_mean, pop_time_std,
  arm_strength_mean, sb_attempts_faced, cs_total, cs_rate, sb_allowed_rate,
  sb_attempts_2b/3b, cs_rate_2b/3b, steal_attempt_rate_against, pickoff_*,
  below_minimum_sample`.
- **Overlap is only `season`, `cs_rate`, `steal_attempt_rate_against`,
  `below_minimum_sample`.** The engine's framing/blocking/throwing *rates*, the
  shadow/heart **zone** features, the `sample_*` counts, and **all four offense
  columns** (`k_rate`, `walk_rate`, `avg_exit_velo`, `hard_hit_rate`) have no
  equivalent in the computed table. The catcher engine cannot be reconstructed from
  what the computor currently produces.

**Manager:** `sample_starter_decisions` (and likely further columns past the binder's
first error) is absent; the table has `sample_games` + a different tendency vocabulary.
Same class of divergence (compounded by SIM-427, which already flags manager metrics as
needing a raw-Statcast rebuild).

**Steal engines:** `baserunner_steal_metrics` / `pitcher_steal_metrics` are not created
by the schema or the computor at all — the steal-specific aggregates were never
materialized (only the `*_season_metrics` base tables exist).

**Situation:** `at_bat_situations` (the per-PA situation fact table the KDTree indexes)
is not built, so the engine indexes 0 rows and normalizes against an empty matrix.

## Why this was not hot-fixed in code

Resolving it correctly is a data-layer reconciliation + a **DuckDB rebuild**, which
cannot be run or verified in the dev sandbox (no raw Statcast, multi-hour recompute):

1. It needs a **canonical-direction decision** per engine — extend the computor to
   emit the engine-expected columns/tables, **or** trim the engines to what the
   computor computes. That changes either the feature design or the data build; both
   need the live DB to validate.
2. Engine query/feature changes shift the **regression golden fixtures**
   (`tests/regression/`), which must be regenerated against *real* rebuilt data — a
   blind edit would either break the engine-drift gate or, worse, silently produce
   wrong similarity scores (strictly worse than the current clean skip).

The registry already degrades safely (skips failed engines; 7/11 boots fine), and the
**production full-pool sim is unaffected** — it draws from the `engine_artifacts`
bundle (`/data/play_pool/engine_artifacts`, which loaded fine: 41 arrays / 166 MB), not
these live engines. The gap is the live **engine suite** (similarity API + per-tile
path) and engine-backed steal/catcher/manager realism.

## Recommended SIM-408 plan (needs the live DB)

1. **Decide canonical direction.** Recommended: treat the engines + their unit tests as
   the intended feature contract and extend the **computor** to emit those
   columns/tables (preserves the designed similarity features); fall back to trimming
   an engine only where a feature is genuinely uncomputable from Statcast.
2. **catcher / manager:** reconcile the column vocabularies and add the missing computed
   columns (catcher offense + zone framing rates; manager starter-decision counts).
3. **baserunner_steal / pitcher_steal:** add steal-metric table builders to the computor
   (SB/CS aggregates from raw Statcast). Overlaps SIM-427 (manager/bullpen-from-raw).
4. **situation:** materialize `at_bat_situations` (per-PA situation fact rows) in the
   nightly build.
5. **Rebuild** the DuckDB profiles → **regenerate regression fixtures** on the rebuilt
   data → verify the lifespan logs `build_all_engines: 11/11`.

### Small safe hardening (optional, independent of the rebuild)

Make `SituationSimilarityEngine` **skip** (raise → registry marks it failed, like the
steal engines) when it loads fewer than its minimum rows, instead of building a
degenerate index that emits `Mean of empty slice` / `Degrees of freedom <= 0`
RuntimeWarnings and a NaN normalization. This avoids a hollow engine silently feeding
NaN situation similarities; it does not fix the underlying missing-table gap.
