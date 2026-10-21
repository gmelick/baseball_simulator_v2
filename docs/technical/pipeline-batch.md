# Nightly builds and backups

The nightly batch turns the raw Postgres tables into what the simulator reads. It
writes the player profiles and the play pools into the DuckDB file, then exports the
engine-artifact bundle (the files every simulation worker loads) onto disk. After the
build, two backup jobs copy the data stores, and a third job copies the backups into
the owner's OneDrive folder. The input is Postgres `raw.*`, which the ingest jobs load
([Data ingestion](pipeline-ingest.md)). The scheduler's jobs, the `make` targets and the
one-off rebuild scripts call this code. The run book (how to run, check and recover
each step) is the operator manual, [WORKFLOW.md](../../WORKFLOW.md).

## How it fits together

### The nightly order

The scheduler is Ofelia, a cron for Docker containers. Its jobs live in
[`deploy/ofelia/config.ini`](../../deploy/ofelia/config.ini). Ofelia has no job
dependencies, so the jobs order themselves through marks: every job runs inside
[`scripts/with_mark.sh`](../../scripts/with_mark.sh), which writes
`/data/ingest_marks/<job>.running` while the job runs and `<job>.last` when it ends.
[`scripts/with_retry.sh`](../../scripts/with_retry.sh) reruns a job after a crash.

1. **07:00 UTC — the finals load** (`nightly-finals`) loads the last three days' final
   games into Postgres. On Sunday at 08:00, `weekly-refresh` sweeps the whole season.
2. **08:00 UTC — the rebuild** (`nightly-rebuild`) runs
   [`scripts/nightly_rebuild.sh`](../../scripts/nightly_rebuild.sh) in the `jobs`
   container. Its first step, [`scripts/wait_for_ingest.sh`](../../scripts/wait_for_ingest.sh),
   waits for the finals load's mark, and runs the load itself when it has not run today.
3. **The profile build:** `python -m pipeline.batch.player_profile_computor --seasons <year>`.
   `PlayerProfileComputor.run` rebuilds the profile tables and the pools for the season
   in progress. The entry point then runs `LeagueAverageProfiles.compute`.
4. **The bundle build:** `python -m pipeline.batch.engine_artifacts --what all`. `main`
   stages a new bundle version, `_build` writes every artifact into it,
   `publish_bundle_version` moves the link onto it, and `prune_bundle_versions` deletes
   old versions.
5. **The app picks it up.** The bundle watch in
   [`api/bundle_watch.py`](../../api/bundle_watch.py) sees the link move. The app waits
   for its run jobs (at most 10 minutes) and for any DuckDB writer, then restarts. The
   new boot pins the new version ([API](api.md)).
6. **10:00 UTC — the backups.** `nightly-backup-postgres` runs
   [`db/backup/pg_backup.sh`](../../db/backup/pg_backup.sh) in the `db` container.
   `nightly-backup-data` runs `python -m pipeline.backup.data_stores` in the `jobs`
   container; it waits while the rebuild, the finals load or the Sunday sweep runs.
7. **10:30 UTC — the off-site copy** (`nightly-backup-offsite`) runs
   `python -m pipeline.backup.offsite`. It waits for both backup jobs, then copies the
   newest complete sets into the OneDrive folder.

[`scripts/nightly_ingest.sh`](../../scripts/nightly_ingest.sh) runs steps 1 to 4 in one
command, for a run by hand. The scheduler does not use it.

### Inside one rebuild

```
Postgres raw.*  --(DuckDB ATTACH, read-only)-->  PlayerProfileComputor.run
                                                   |
     derived.* profiles + derived.run_expectancy  <-+   one cutoff per profile table
     sim.* pools                                  <-+   incremental per season
                                                   |
                      LeagueAverageProfiles.compute -> derived.league_averages
                                                   |
     engine_artifacts --what all  (DuckDB read-only)
         + the similarity engines   (the actor matrices, the pitcher matrix)
         + Postgres                 (each game's managers and each arm's rest)
         + the MLB Stats API        (the published fence distances)
                                                   |
     /data/play_pool/engine_artifacts.versions/<build id>/   <-- engine_artifacts (a link)
                                                   |
     EngineArtifacts.load: once in the app (shared memory), once in every worker
```

## The profile build

The profile build reads Postgres through DuckDB's `postgres` extension, attached
read-only as the catalog `pg`. It writes into the DuckDB file
(`BASEBALL_DUCKDB_PATH`, default `/data/baseball_sim.duckdb`). The similarity engines
read the `derived.*` tables ([Similarity engines](similarity.md)); the pool builds read
`raw.pitches` again and some `derived.*` columns.

Three rules govern every run:

- **One cutoff per profile table** (SIM-551). Eight `derived.*` tables carry
  `asof_date`, the date a row's data runs through (`ComputorBase.STAMPED_TABLES`). Every
  engine refuses a table stamped at two dates. So every builder calls
  `_refuse_a_mixed_cutoff` BEFORE it deletes or inserts, and `_stamp_one_cutoff` after
  its insert stamps the WHOLE table. `run` checks all eight tables first, so a refusal
  costs seconds, not the hour of the pitcher fit. A partial rebuild must include the
  season in progress.
- **A bare run builds the current season only.** A full historical rebuild passes
  `--seasons 2017 … 2026 --full-rebuild`. `--full-rebuild` deletes the seasons' rows
  from every season-keyed `derived.*` table first (`_delete_seasons`).
- **An as-of build** (`--asof <date>`) cuts every source off at that date, so a backtest
  never sees data recorded after the day it simulates. Each builder's
  `_assert_*_have_no_leakage` check raises when a source contributed a later row.

### `pipeline/batch/player_profile_computor.py`

[Source](../../pipeline/batch/player_profile_computor.py) · 435 lines · Tests: [test_player_profile_computor.py](../../tests/unit/test_player_profile_computor.py), [test_one_cutoff_per_table.py](../../tests/unit/test_one_cutoff_per_table.py), [test_delete_seasons_coverage.py](../../tests/unit/test_delete_seasons_coverage.py), [test_profile_index_recreate.py](../../tests/unit/test_profile_index_recreate.py)

The class `PlayerProfileComputor`, its run order, the season delete and the command
line. The class is the sum of one mixin per profile group (the modules below).

| Function / class | What it does | Called from |
|---|---|---|
| `PlayerProfileComputor.run` | The run order: (1) park factors; (2) pitcher profiles with the arsenal fit; (3) batter profiles; (4) baserunner profiles and the two steal-engine tables; (5) manager profiles; (6) the run-expectancy matrix, then the catcher and fielder metrics and their season aggregates; (7) the pools, the outfield arm block, the intentional-walk rates and the plate-appearance situations. | the command line, `scripts/rebuild_pitch_pool.py`, `scripts/rebuild_steal_pool.py` |
| `_connect` / `_run_schema_ddl` | Opens DuckDB, attaches Postgres read-only, and applies `db/schemas/02_duckdb_schema.sql` when the schema is absent. | `run` |
| `_delete_seasons` | The full rebuild's delete over a named list of season-keyed tables. It drops the secondary indexes first and recreates them in a `finally` (`_recreate_indexes`), checking each one in `duckdb_indexes()`. | `run` |
| `__main__` | `--seasons`, `--full-rebuild`, `--asof`, `--dsn` (default `BASEBALL_DB_DSN`), `--duckdb-path`; then `LeagueAverageProfiles.compute` unless `--skip-league-averages`. | `scripts/nightly_rebuild.sh`, `make profile-computor` |

**Watch out:** The step order carries data between steps. The run-expectancy matrix
must precede the double-play value; the baserunner profiles (sprint speed) must precede
the infield metrics; the advancement pool must precede the outfield arm block. A new
season-keyed `derived.*` table goes on `_delete_seasons`' list, or
`test_delete_seasons_coverage.py` fails.

### `pipeline/batch/profiles/` — the builder mixins

| Module | Lines | What it builds | Reads | Tests |
|---|---|---|---|---|
| [`base.py`](../../pipeline/batch/profiles/base.py) | 305 | `ComputorBase`: the shared state; the one-cutoff guard (`_refuse_a_mixed_cutoff`, `_stamp_one_cutoff`, `STAMPED_TABLES`); the leakage check (`_check_leakage`); the `raw.play_events` relations — pickoff outs per pitcher, pickoff throws and step-offs, and each pickoff outcome mapped to the steal pool's runner pair (`_pickoff_outcomes_cte`). Each relation is empty when `raw.play_events` is absent. | `raw.pitches`, `raw.play_events` | [test_one_cutoff_per_table.py](../../tests/unit/test_one_cutoff_per_table.py) |
| [`park.py`](../../pipeline/batch/profiles/park.py) | 260 | `derived.park_factors`: each venue's rate of each event (HR, 1B, 2B, 3B, R, K, BB, GB, FB) over the league's, split by the batter's side, shrunk toward 1.0 (`PARK_PRIOR_PA` = 2,000 plate appearances for half weight). | `raw.pitches` | [test_park_factors.py](../../tests/unit/test_park_factors.py) |
| [`pitcher.py`](../../pipeline/batch/profiles/pitcher.py) | 390 | `derived.pitcher_season_metrics` and `derived.pitcher_gmm_components`. Pass 1 (SQL) computes the command and result metrics; pass 2 fits the arsenal mixture per pitcher-season. | `raw.pitches`, `raw.play_events` | [test_point_in_time_pitcher_manager.py](../../tests/unit/test_point_in_time_pitcher_manager.py) |
| [`gmm.py`](../../pipeline/batch/profiles/gmm.py) | 369 | The arsenal fit: a Gaussian mixture (a blend of bell curves, one per pitch shape) per pitcher-season over eight features (velocity, vertical and horizontal break, spin rate, spin axis, release x / z, extension), standardized, with K chosen by BIC (a fit score that penalizes extra components) from 2 to 7. A process pool runs the fits, one worker per core less one; `_flush_gmm_results` writes them in three set-based statements. | the pitch rows `pitcher.py` passes | [test_gmm_batch_pipeline.py](../../tests/unit/test_gmm_batch_pipeline.py) |
| [`batter.py`](../../pipeline/batch/profiles/batter.py) | 504 | `derived.batter_season_metrics`, with the physical swing (bat speed, swing length, tilt, attack angle and direction, contact depth) and stance block. The per-pitch Savant export is the first source (it cuts at any date); the leaderboard is the fallback. | `raw.pitches`, `raw.savant_bat_tracking`, `raw.savant_swing_path`, `raw.savant_batting_stance`, `raw.savant_pitch_tracking` | [test_batter_physical_features.py](../../tests/unit/test_batter_physical_features.py), [test_point_in_time_batter.py](../../tests/unit/test_point_in_time_batter.py) |
| [`baserunner.py`](../../pipeline/batch/profiles/baserunner.py) | 689 | `derived.baserunner_season_metrics` (the extra-base attempt and success rates, steal rates, sprint speed, Savant's extra-base columns); `derived.baserunner_steal_metrics` (the steal engine's table, with lead distance); `derived.pitcher_steal_metrics` (the hold-runner engine's table: pickoff throws, step-offs, steals allowed). | `raw.pitches`, `raw.play_events`, `raw.sprint_speed`, `raw.savant_baserunning`, `raw.savant_basestealing`, `raw.savant_pitcher_running_game` | [test_lead_distance.py](../../tests/unit/test_lead_distance.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/test_point_in_time_runner_catcher_fielder.py) |
| [`manager.py`](../../pipeline/batch/profiles/manager.py) | 508 | `derived.manager_season_metrics`: usage (starter pitch counts, the quick hook, openers, closer entry leverage, available-reliever usage), aggression and platoon tendencies. Offensive decisions go to the batting manager; the defensive substitution to the fielding manager. Also `_compute_bullpen_workload`, which `run` does not call: the bullpen-availability ingest calls it against the live roster. | `raw.pitches`, `raw.game_bullpen_availability` | [test_manager_usage_score.py](../../tests/unit/test_manager_usage_score.py), [test_manager_defensive_subs.py](../../tests/unit/test_manager_defensive_subs.py) |
| [`leverage.py`](../../pipeline/batch/profiles/leverage.py) | 221 | `compute_leverage_index` (a simplified leverage index from inning, score, runners and outs) and `build_run_expectancy_matrix`: the average runs from each of the 24 base-out states to the end of the half-inning, innings 1–8. A live build writes `derived.run_expectancy_matrix` (keyed by season range); an as-of build returns the matrix only. | `raw.pitches` | [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/test_point_in_time_runner_catcher_fielder.py) |
| [`catcher.py`](../../pipeline/batch/profiles/catcher.py) | 851 | `derived.catcher_season_metrics`: framing (a called-strike probability model), blocking (a passed-ball and wild-pitch model), throwing by base, then the season aggregate and the uncaught-strike-three slice. Each step fills a temporary table the aggregate reads. | `raw.pitches`, `raw.savant_catcher_throwing`, `raw.savant_poptime` | [test_uncaught_strike_three.py](../../tests/unit/test_uncaught_strike_three.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/test_point_in_time_runner_catcher_fielder.py) |
| [`fielder_plays.py`](../../pipeline/batch/profiles/fielder_plays.py) | 925 | The per-play fielder metrics, each into a temporary table: outfield catch probability and outs above average (OAA, plays made beyond what an average fielder makes), infield OAA from a time-margin model, double plays (their run value reads the run-expectancy matrix), bunt defense, first-base scooping, and fielding and throwing errors. | `raw.pitches`, `raw.sprint_speed`, `derived.baserunner_season_metrics` | [test_player_profile_computor.py](../../tests/unit/test_player_profile_computor.py) |
| [`fielder.py`](../../pipeline/batch/profiles/fielder.py) | 621 | `derived.fielder_season_metrics`: the per-play temporary tables combined per (fielder, position, season), with Savant's arm strength, OAA and outfield jump. `_fill_outfield_arm_block` then fills the outfield arm columns from the advancement pool: each outfielder's chances, the attempts against him, the runners he threw out, and his prevention against the pool's own attempt rate at his position. | the temporary tables, `raw.savant_arm_strength`, `raw.savant_outs_above_average`, `raw.savant_outfield_jump`, `sim.advancement_opportunity_pool` | [test_outfield_arm_block.py](../../tests/unit/test_outfield_arm_block.py), [test_fielder_oaa_jump_profile.py](../../tests/unit/test_fielder_oaa_jump_profile.py), [test_fielder_placeholder_schema.py](../../tests/unit/test_fielder_placeholder_schema.py) |
| [`defense.py`](../../pipeline/batch/profiles/defense.py) | 248 | The field-geometry helpers the fielder and catcher models share: hit-chart distance in feet, throw time, hang time, ground-ball travel time, the direction a fielder moves, and a scaled logistic fit. | — | [test_player_profile_computor.py](../../tests/unit/test_player_profile_computor.py), [test_simulation_constants.py](../../tests/unit/test_simulation_constants.py) |
| [`league_averages.py`](../../pipeline/batch/profiles/league_averages.py) | 311 | `LeagueAverageProfiles.compute`: one `derived.league_averages` row per entity type and season (pitcher, batter, `fielder_<position>`, baserunner, baserunner_steal, pitcher_steal, catcher, manager). The engines shrink a thin profile toward it; the loop's steal weight reads the manager row's mean steal rate. | the `derived.*` profile tables | [test_league_average_profiles.py](../../tests/unit/test_league_average_profiles.py) |
| [`sql.py`](../../pipeline/batch/profiles/sql.py) | 378 | The shared SQL: `SQL_OUTCOME_TYPE` (the pitch pool's class, rendered from the code sets in `pipeline/statcast_events.py`), the swing and stance selects, the out labels, the barrel band, the fielding-credit mask, and the fatigue columns (`SQL_PITCHER_PITCH_COUNT`, `SQL_TIMES_THROUGH_ORDER`). | — | [test_profile_code_sets.py](../../tests/unit/test_profile_code_sets.py), [test_foul_tip_strike_three.py](../../tests/unit/test_foul_tip_strike_three.py), [test_barrel_definition.py](../../tests/unit/test_barrel_definition.py) |
| [`constants.py`](../../pipeline/batch/profiles/constants.py) | 61 | The sample floors (`MIN_*`), the shrinkage priors (`ARM_ALPHA_PRIOR_CHANCES` 50 and `JUMP_ALPHA_PRIOR_PLAYS` 25, which the fielder engine also reads; `PARK_PRIOR_PA` 2,000), the FIP constant, and the column orders of the two positional inserts (`BASERUNNER_TAIL_COLUMNS`, `FIELDER_TAIL_COLUMNS`). | — | [test_fielder_oaa_jump_model.py](../../tests/unit/test_fielder_oaa_jump_model.py), [test_outfield_arm_model.py](../../tests/unit/test_outfield_arm_model.py) |

**Watch out:**

- `derived.baserunner_season_metrics` and `derived.fielder_season_metrics` take a
  POSITIONAL insert (no column list). The Savant columns go last, in the order of the
  tail constants in `constants.py`; a unit test holds the live table's tail to them.
- `SQL_OUTCOME_TYPE` has no `ELSE`: an unknown pitch code yields NULL, and the NOT NULL
  column refuses the insert. A two-strike foul tip or foul bunt is a swinging strike
  (strike three). Change the code sets in `pipeline/statcast_events.py`, never a copy.

## The pool builds

The pools are the rows the sampler draws from: real plays, filtered hard by the
situation and weighted by similarity at draw time ([Simulation](simulation.md),
[the loop cheat sheet](sim-loop-cheat-sheet.md)). `SimPoolsMixin` builds them in step 7
of `run`, after every profile table, because some pools copy profile columns.

| Table | One row per | Built by | Read by |
|---|---|---|---|
| `sim.pitch_pool` | pitch (the quality rows of `raw.pitches`) | `_build_pitch_pool` | the pitch-pool artifact, the change-pool export, `pool_chain`, the concentration report |
| `sim.outcome_pool` | ball in play, with the fielding outcome and the base-out transition | `_build_outcome_pool` | the batted-ball artifact, the park geometry, `_build_advancement_opportunity_pool` |
| `sim.stolen_base_pool` | steal attempt, with the actors' profile columns copied in | `_build_stolen_base_pool` | no reader outside the build |
| `sim.steal_opportunity_pool` | pitch with a stealable runner, attempted or not, plus one pickoff row per pickoff before a pitch of its pair | `_build_steal_opportunity_pool` | the steal-pool artifact |
| `sim.advancement_opportunity_pool` | discretionary runner-advancement decision, attempted or not | `_build_advancement_opportunity_pool` | the advancement artifact, the outfield arm block |
| `sim.ibb_rates` | cell (runners, outs, late, close) | `_build_ibb_rates` | the reference artifact (`ibb_rates.json`) |
| `derived.at_bat_situations` | plate appearance, the state before its first pitch | `_build_at_bat_situations` | the situation engine, the change-pool export |
| `sim.pool_build_metadata` | (pool, season) build record | `_record_pool_build` | the incremental gate |

### `pipeline/batch/profiles/pools.py`

[Source](../../pipeline/batch/profiles/pools.py) · 1,113 lines · Tests: [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_transition_pool.py](../../tests/unit/test_transition_pool.py), [test_ibb_rates.py](../../tests/unit/test_ibb_rates.py), [test_hit_by_pitch.py](../../tests/unit/test_hit_by_pitch.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py)

`SimPoolsMixin`: one method per table above.

| Function / class | What it does | Called from |
|---|---|---|
| `_build_pitch_pool` | Copies the quality pitch rows with the base state, the score difference, the class (`SQL_OUTCOME_TYPE`), the previous pitch, the catcher (`fielder_2`), the got-away flag, the batting side, the pitch count and times through the order, a `pitch_id` and the recency weight. | `run`, `scripts/rebuild_pitch_pool.py` |
| `_build_outcome_pool` | The in-play subset of the pitch pool with the batted-ball detail, the fielder who fielded it, the alignment, the putout and assist positions, and each runner's destination. | `run` |
| `_build_steal_opportunity_pool` | One row per pitch where the lead runner could steal (first with second open → target 2; second with third open → target 3), with `attempted` beside `success`, so one draw answers "does he go" and "safe or caught". Each row carries its pitch's class (`pitch_class`). A pickoff outcome before any pitch of its pair becomes a pickoff row: `is_pickoff_row`, `pitch_id` the negative play-record id, count 0-0, no class (the running game on the pitch, SIM-554). | `run`, `scripts/rebuild_steal_pool.py` |
| `_build_advancement_opportunity_pool` | One row per discretionary advancement in five scenarios, attempted or not, from the outcome pool's transitions. Rows that fail the outs-accounting check are left out. | `run` |
| `_build_ibb_rates` | The measured intentional-walk rate per cell: `raw.play_events` intentional walks over every plate appearance that entered the cell. A full recompute every run. | `run` |
| `_build_stolen_base_pool` | Every steal attempt, with runner, catcher and pitcher columns copied from the profile tables. | `run` |
| `_build_at_bat_situations` | One row per plate appearance at its first pitch, with the leverage index computed in SQL. `INSERT OR REPLACE`, so no incremental gate. | `run` |

**Watch out:** The steal and advancement pools take POSITIONAL inserts: the `SELECT`
order must match the DDL in `db/schemas/02_duckdb_schema.sql`. The steal build refuses
a table without migration 0031's columns before it deletes anything
(`_require_steal_pool_0031`); `scripts/rebuild_steal_pool.py --apply-migration` applies
0031 and rebuilds in one transaction.

### `pipeline/batch/profiles/pool_meta.py`

[Source](../../pipeline/batch/profiles/pool_meta.py) · 209 lines · Tests: [test_pool_recency_and_incremental_rebuild.py](../../tests/unit/test_pool_recency_and_incremental_rebuild.py), [test_steal_pool_rebuild.py](../../tests/unit/test_steal_pool_rebuild.py)

The pool build's bookkeeping: the builder version, the recency weight and the
incremental gate.

| Function / class | What it does | Called from |
|---|---|---|
| `POOL_BUILDER_VERSION` | The version stamped on every (pool, season) build, `sim554.1` today. A season built by another version counts as stale. | `_seasons_needing_rebuild`, `_record_pool_build` |
| `recency_weight` / `_recency_weight_sql` | A row's sampling weight by age: 2.0 for the newest two seasons, then × 0.75 per older season, floored at 0.25. The SQL form writes the same values into each pool's `recency_weight` column. | the pool builders |
| `_canonical_ref_season` | The one reference season every pool shares: the newest of this run's seasons and every recorded reference. | the pool builders |
| `_seasons_needing_rebuild` | A season is fresh only when its newest game date and its source row count are unchanged AND the running builder version built it. Otherwise it rebuilds. | the pool builders |
| `_record_pool_build` | Upserts the build record: the source watermark, the row count, the reference season, the builder version. | the pool builders |
| `_require_steal_pool_0031` | Raises unless the steal pool carries `pitch_class` and `is_pickoff_row`. | `_build_steal_opportunity_pool`, `scripts/rebuild_steal_pool.py` |
| `_ensure_index_if_not_exists` | Rewrites a `CREATE INDEX` statement with `IF NOT EXISTS`. | `_recreate_indexes` |

**Watch out:** Bump `POOL_BUILDER_VERSION` on any change to a pool's `SELECT`. The
watermark sees only the source data, so without the bump an incremental run keeps the
old rows.

### `pipeline/batch/pool_chain.py`

[Source](../../pipeline/batch/pool_chain.py) · 536 lines · Tests: [test_pool_chain.py](../../tests/unit/test_pool_chain.py), [test_pool_label_check.py](../../tests/acceptance/test_pool_label_check.py)

The count chain and the label check. The count chain is the loop's count machine as
arithmetic: at each of the twelve counts, the pitch pool's class shares move the count
or end the plate appearance. It gives the walk, strikeout, hit-by-pitch and in-play
rates, and the pitches per plate appearance, that a faithful sampler of the pool
produces. The acceptance bands certify the simulator against these rates. The label
check compares the chain with real plate appearances from `raw.pitches`. It exists
because the bands read the same labels as the simulator, so a labelling defect in the
pool build would pass them.

| Function / class | What it does | Called from |
|---|---|---|
| `solve_chain` | The absorbing chain: the absorption shares and the expected pitches from 0-0. | `chain_rates`, `scripts/pool_window_census.py` |
| `pool_count_matrix` / `chain_rates` | The 12 × 6 count-by-class volume of `sim.pitch_pool WHERE pred` (plain or recency-weighted), and its chain. `pa_only` keeps only the plate appearances that end in a batting event, the label check's population. | `scripts/pool_window_census.py`, `scripts/rebuild_pitch_pool.py`, the acceptance test |
| `attach_pg` / `real_pa_rates` / `pa_group_count` | Attach Postgres; the real per-plate-appearance rates; the count of plate appearances the pool side reads (it must equal the real side's). | the same |
| `label_check` / `format_*` | The relative gap per channel, PASS within `LABEL_CHECK_TOLERANCE` (0.5%). | the same |

**Watch out:** Do not widen the 0.5% tolerance to clear a red. Read `SQL_OUTCOME_TYPE`
first: the two defects the check exists for were both labelling errors there.

## The engine-artifact bundle

The simulator runs in process-pool workers. A worker cannot receive the app's engine
objects, so everything the sampler needs reaches it as files on disk: the bundle. The
builder reads the DuckDB file read-only and writes one folder of files; `EngineArtifacts`
loads that folder in the app and in every worker.

The season window is `RECENCY_FLOOR_SEASONS` = 4: the newest four seasons in the pitch
pool, which today means the last three completed seasons plus the current one (owner
ruling 2026-08-20). Every pool row carries its game date as a `game_ymd` integer, so the
sampler can drop plays after a backtest's game date at draw time.

### The bundle on disk

| Path in the bundle | What it holds | Built by | Read by |
|---|---|---|---|
| `pitch_pool/{L,R}.geom.npy`, `.sit.npy`, `.meta.parquet`, `.bb_row.npy`, `manifest.json` | Per batter hand: the ten pitch features, the six situation columns, the metadata (ids, class, recency weight, catcher, got-away, batting side, pitch count, times through the order, zone, `game_ymd`), and each pitch row's batted-ball row (−1 when none). | `build_pitch_pool_artifact`, then `build_battedball_pool_artifact` (the join) | the pitch draw and the pitch-result draw |
| `battedball_pool/{L,R}.geom.npy`, `.sit.npy`, `.pgeom.npy`, `.pitch_id.npy`, `.meta.parquet` | Per batter hand: exit velocity, launch angle and pull-relative spray; the situation; the features of the pitch that produced the ball; the metadata (the event, the result deltas, the park, the fielder, the runner destinations, the batted-ball class, the alignment, the putout and assist positions). | `build_battedball_pool_artifact` | the fielding draw and the fence stage |
| `steal_pool/{2,3}.sit.npy`, `.meta.parquet` | Per target base: the count, outs and score; the runner, pitcher and catcher; `attempted`, `success`, the pickoff outcome, `pitch_class`, `is_pickoff_row`. | `build_steal_pool_artifact` | the pickoff draw and the steal draw |
| `advancement_pool/<scenario>_<from>_<to>.feat.npy`, `.meta.parquet` | Per decision (eight keys): the ball's geometry and the outs; the runner, the fielder, `attempted`, `safe`, `error_extra`. | `build_advancement_pool_artifact` | the advancement draws |
| `manager_pool/change.sit.npy`, `change.meta.parquet`, `roles.parquet` | One row per plate-appearance boundary while a pitcher is on the mound, changed or not, with the fielding manager and the entering arm's rest; the relievers' role sidecar. | `build_pitching_change_pool` | the pitching-change draw and the reliever draw |
| `pitcher_sim.npz` | The pitcher-by-pitcher similarity: a dense float32 matrix and its index. | `build_pitcher_sim_matrix` | the pitch draw's pitcher weight, the pitching-change draw and the reliever draw |
| `actor_sim/<name>.npz`, `manifest.json`, `concentration.json` | The actor score matrices (below) and the concentration report. | `build_actor_sim_matrices` | the draws that weight by an actor |
| `<actor>_emb.npz` | Each actor's profile vector per season and its key order, for batter, catcher, fielder, baserunner, manager and pitcher_steal. | `build_actor_embeddings` | the sampler, to map pool rows onto matrix columns |
| `park_geometry.json` | Each park's effective fence by spray sector and season group, the carry model and the carry offset per park. | `build_park_geometry` | the fence stage |
| `venue_dimensions.json`, `park_geometry_overrides.json` | The published fence distances from the MLB Stats API; hand-curated fence corrections. | `build_park_geometry` writes the first; a person writes the second | `build_park_geometry` |
| `receiving.json` | The league called-strike and got-away rates, and each catcher-season's framing and blocking multipliers. | `build_receiving_profiles` | the catcher receiving ratio (`SIM_CATCHER_RECEIVING`: default off; production off) |
| `ibb_rates.json`, `venue_run_factors.json` | The intentional-walk rate per cell and the regressed run factor per venue and season. | `build_reference_tables` | the worker factory (`simulation/production_factory.py`) |

### `pipeline/batch/engine_artifacts.py`

[Source](../../pipeline/batch/engine_artifacts.py) · 875 lines · Tests: [test_actor_matrices.py](../../tests/unit/test_actor_matrices.py), [test_engine_artifacts_shared_memory.py](../../tests/unit/test_engine_artifacts_shared_memory.py), [test_bundle_no_pickle.py](../../tests/unit/test_bundle_no_pickle.py), [test_engine_artifacts_realism.py](../../tests/unit/test_engine_artifacts_realism.py)

The command line, the actor score matrices and the loader class. Each other artifact's
builder and loader live in `artifacts/`.

**The actor score matrices.** Every actor factor in a draw is its similarity engine's
0-to-1 score for the live player against the player on each pool row, raised to a power
(the play-picker redesign, SIM-523). The builder runs each engine's own query for every
profile pair of the window and packs the scores into a dense matrix: diagonal 1.0, an
unscored pair NaN (neutral in the sampler). The baserunner engines also score their
thin profiles, so the matrices cover every runner in the pools.

| Matrix | Engine score | Read by |
|---|---|---|
| `batter` | the batter engine's composite | the pitch draw, the fielding draw |
| `fielder_<1B…RF>` | the fielder engine's composite, one matrix per position | the fielding draw, the advancement draws |
| `catcher_throwing` | the catcher engine's throwing sub-score | the pickoff and steal draws |
| `runner_steal` | the steal-runner engine's composite | the pickoff and steal draws |
| `pitcher_steal` | the hold-runner engine's composite | the pickoff and steal draws |
| `runner_adv` | the advancement-runner engine's composite | the advancement draws |
| `manager_usage` | the manager engine's usage sub-score | the pitching-change draw |
| `catcher` | the catcher engine's composite | no draw; the concentration report checks it |

| Function / class | What it does | Called from |
|---|---|---|
| `main` / `_build` | `--what pool \| pitcher_sim \| actors \| actors_sim \| park \| manager \| receiving \| reference \| all`. Without `--out-dir` it stages a version, builds, publishes and prunes; a failed build discards the staged folder. With `--out-dir` it writes in place. `--matrix NAME` rebuilds one actor matrix and merges its size into the manifest. | `scripts/nightly_rebuild.sh`, `make engine-artifacts`, the rebuild scripts |
| `build_actor_sim_matrices` | Builds every matrix above from the engines, writes the manifest, then the concentration report. | `_build` |
| `_matrix_from_queries` | Scores every profile against every other through the engine's query. | `build_actor_sim_matrices` |
| `concentration_report` / `concentration_share` | For each catcher-season (pitch pool) and fielder-season (batted-ball pool): his matrix row's weighted share of draw weight on his own staff's rows, over the unweighted share. A p90 ratio above `CONCENTRATION_MAX_RATIO` (3.0) logs a warning, or fails the build with `--strict-concentration`. | `build_actor_sim_matrices` |
| `EngineArtifacts` | The loaded bundle: both hands' pitch and batted-ball pools, the steal, advancement and change pools, the pitcher matrix, the actor matrices and embeddings, the park geometry, the receiving document and the reference tables. A missing part loads empty or None, and its factor stays neutral. | `simulation/production_factory.py`, `api/startup.py` |
| `EngineArtifacts.load` | One loader per artifact. With `shared_views`, the large arrays come from shared memory and their disk reads are skipped. | `api/startup.py`, `simulation/production_factory.py`, `pipeline/backup/data_stores.py` |
| `extract_shared_arrays` / `attach_shared_views` | The app extracts the large numeric arrays once, and the runner publishes them into shared memory. A worker passes those views to `load`. `attach_shared_views` swaps a loaded bundle's arrays for views; only tests call it. | `api/startup.py` (extract) |

**Watch out:**

- The tests patch `_ENGINE_LOADER`, `concentration_report` and `_matrix_from_queries` in
  THIS module. Keep them here.
- The batted-ball export must follow the pitch-pool export: it writes the pitch rows'
  join file (`pitch_pool/{hand}.bb_row.npy`) from the pitch pool's metadata.
- The nightly build runs without `--strict-concentration`, so a concentration red is a
  log warning only.

### `pipeline/batch/artifacts/` — one module per artifact

Each module holds one artifact's builder and its loader.

| Module | Lines | What it holds | Tests |
|---|---|---|---|
| [`common.py`](../../pipeline/batch/artifacts/common.py) | 118 | The pool columns (`_GEOM_COLS`, `_SIT_COLS`), the default paths (`BASEBALL_PLAY_POOL_DIR`, `BASEBALL_DUCKDB_PATH`), the window (`RECENCY_FLOOR_SEASONS`, `last_n_seasons`), the `game_ymd` column, `join_rows`, `take_view` (a shared view, else a disk read) and `_savez_plain`, which refuses an object array so the bundle always loads with `allow_pickle=False`. | [test_bundle_no_pickle.py](../../tests/unit/test_bundle_no_pickle.py), [test_pool_cutoff.py](../../tests/unit/test_pool_cutoff.py) |
| [`pools.py`](../../pipeline/batch/artifacts/pools.py) | 419 | The in-memory pool classes `HandPool`, `BattedBallPool`, `StealPool`, `AdvancementPool`, `ChangePool`; the attribute lists each shares through shared memory; the steal pool's class codes (`STEAL_PITCH_CLASSES`, 1–6 in the order of `simulation.game_state.PITCH_OUTCOMES`, 0 for no class) and `steal_pitch_class_codes`. | [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_running_game_on_the_pitch.py](../../tests/unit/test_running_game_on_the_pitch.py) |
| [`pitch_pool.py`](../../pipeline/batch/artifacts/pitch_pool.py) | 227 | The per-hand pitch pool. Optional columns (catcher, conditioning, zone) are probed, and an unknown value exports as the neutral code. | [test_engine_artifacts_realism.py](../../tests/unit/test_engine_artifacts_realism.py), [test_profile_data_additions.py](../../tests/unit/test_profile_data_additions.py) |
| [`battedball_pool.py`](../../pipeline/batch/artifacts/battedball_pool.py) | 409 | The per-hand batted-ball pool, the pitch-row join and the producing pitch's features (NaN kept, so a missing value is neutral). It keeps every row; the fielding draw's cell index drops the rows whose transition failed the outs check. | [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py), [test_transition_pool.py](../../tests/unit/test_transition_pool.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py) |
| [`steal_pool.py`](../../pipeline/batch/artifacts/steal_pool.py) | 240 | The steal pools per target base. It refuses a DuckDB without migration 0031 before it writes a file, writes each target under temporary names, and moves them into place only when both targets succeed. The loader refuses a target whose two files disagree on the row count. | [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py) |
| [`advancement_pool.py`](../../pipeline/batch/artifacts/advancement_pool.py) | 129 | The eight advancement pools, keyed scenario_from_target. | [test_transition_pool.py](../../tests/unit/test_transition_pool.py) |
| [`manager_pool.py`](../../pipeline/batch/artifacts/manager_pool.py) | 334 | The pitching-change opportunity pool, built at export time from the pitch pool and `derived.at_bat_situations`, plus the relievers' role sidecar (entries, high-leverage share, mean entry inning). Its one Postgres read (`pipeline.bullpen_usage.fetch_game_context_sync`, through `BASEBALL_DB_DSN`) adds each game's managers and each entering arm's rest; without it those columns read unknown (−1). | [test_manager_draw.py](../../tests/unit/test_manager_draw.py), [test_relief_draw.py](../../tests/unit/test_relief_draw.py) |
| [`pitcher_sim.py`](../../pipeline/batch/artifacts/pitcher_sim.py) | 89 | The pitcher-by-pitcher matrix from `PitcherSimilarityEngine`, about an hour nightly. The dense matrix rides shared memory; the dictionary copy in the file is for an older loader. | [test_pitcher_sim_matrix.py](../../tests/unit/test_pitcher_sim_matrix.py) |
| [`actor_embeddings.py`](../../pipeline/batch/artifacts/actor_embeddings.py) | 146 | Each actor's numeric profile columns per (id, season) — per (id, position, season) for fielders — with the mean and spread. The catcher's got-away columns are left out. | [test_engine_artifacts_realism.py](../../tests/unit/test_engine_artifacts_realism.py), [test_profile_data_additions.py](../../tests/unit/test_profile_data_additions.py) |
| [`park_geometry.py`](../../pipeline/batch/artifacts/park_geometry.py) | 778 | The fence document, version 2: eleven 10° sectors over ±55°; a fence line per park and season group (`detect_moves` splits a park's seasons where a wall moved); a published-distance prior for a thin sector (`_thin_rule`); the overrides; the carry model (distance from exit velocity and launch angle, fitted on home runs) and the carry offset per park (`carry_offsets`). `scripts/fence_check.py` reruns the pure functions to certify a rebuild. | [test_park_geometry_builder.py](../../tests/unit/test_park_geometry_builder.py), [test_fence_check.py](../../tests/unit/test_fence_check.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py) |
| [`receiving.py`](../../pipeline/batch/artifacts/receiving.py) | 184 | The catcher receiving document, built from the pitch pool: framing multipliers per zone group (heart, edge, outside) and a blocking ratio on got-away rows, both shrunk toward the league (priors 200 taken pitches and 5 expected got-aways). `recv_zone_group` and `recv_block_cell` are shared with the sampler. | [test_receiving_ratio.py](../../tests/unit/test_receiving_ratio.py) |
| [`reference.py`](../../pipeline/batch/artifacts/reference.py) | 81 | `ibb_rates.json` from `sim.ibb_rates` and `venue_run_factors.json` from the regressed run factors in `derived.park_factors`. A worker without them reads DuckDB instead. | [test_bundle_worker_and_boot.py](../../tests/unit/test_bundle_worker_and_boot.py) |

**Watch out:** `build_park_geometry` calls the MLB Stats API for the published fence
distances. When the API is down, the last `venue_dimensions.json` stands.

### `pipeline/batch/bundle_versions.py`

[Source](../../pipeline/batch/bundle_versions.py) · 207 lines · Tests: [test_bundle_versions.py](../../tests/unit/test_bundle_versions.py), [test_bundle_worker_and_boot.py](../../tests/unit/test_bundle_worker_and_boot.py)

The bundle as published versions. `/data/play_pool/engine_artifacts` is a symbolic link
to one folder under `engine_artifacts.versions/<build id>/`. Every reader opens the
link's path; the link resolves it to the current version. So a worker that respawns during a rebuild never reads a
half-written file.

| Function / class | What it does | Called from |
|---|---|---|
| `stage_bundle_version` | Creates a new version folder seeded with a copy of the current version, so a partial build (`--what park`) and the hand-written files carry over. The in-use markers are not copied. | `engine_artifacts.main` |
| `publish_bundle_version` | Writes the `.published` marker and moves a temporary link onto the link with `os.replace`, which is atomic. | `engine_artifacts.main`, `data_stores.restore_bundle` |
| `discard_bundle_version` | Deletes a staged version that failed. | `engine_artifacts.main` |
| `prune_bundle_versions` | Keeps the newest `KEEP_VERSIONS` (3) published versions, the current one, and every version with an in-use marker. It deletes an unpublished folder older than the current version (a build that died). | `engine_artifacts.main` |
| `mark_in_use` / `unmark_in_use` | The `.in_use.<host>` marker a running app writes into the version it pinned. | `api/bundle_watch.py`, `api/startup.py` |
| `convert_legacy_bundle` / `resolve_bundle_dir` | Turns a real folder at the link path into the first version; resolves the link to its folder. | `api/bundle_watch.py`, `publish_bundle_version`, `data_stores` |

**Watch out:** Never edit a published version in place. Build a new version instead.

## The backups

Three stores are backed up nightly: the Postgres statistics database, the DuckDB file
and the bundle. A *set* is one backup of one store, a folder named by the run's UTC
start time (`%Y%m%dT%H%M%SZ`). A set is complete only when its `manifest.json` exists;
the writer writes the manifest last. The Postgres job is a shell script in the `db`
container ([Database](database.md)); the two Python jobs below run in the `jobs`
container as `appuser`. The backup sets land in `/backups` (the host folder
`BACKUP_DIR`), the off-site copies in `/offsite` (the host folder
`OFFSITE_BACKUP_DIR`, a OneDrive folder).

### `pipeline/backup/__init__.py`

[Source](../../pipeline/backup/__init__.py) · 8 lines · Tests: none

The package docstring: which module does what.

### `pipeline/backup/status.py`

[Source](../../pipeline/backup/status.py) · 121 lines · Tests: [test_backup_status.py](../../tests/unit/test_backup_status.py), [test_backup_age_gauge.py](../../tests/unit/test_backup_age_gauge.py)

Which sets exist and how old they are. Pure standard library, because the API imports it.

| Function / class | What it does | Called from |
|---|---|---|
| `SetInfo` | One complete set: store, set id, path, creation time, bytes. | the backup modules |
| `complete_sets` / `newest_complete_set` / `newest_complete_sets` | Every complete set of a store (oldest first); the newest; the newest of each store (`postgres`, `duckdb`, `bundle`). | `data_stores`, `offsite`, `api/routes/metrics.py` |
| `age_hours` | Hours since a set's creation; −1.0 when there is none. | `api/routes/metrics.py` (the gauges `baseball_sim_backup_age_hours` and `baseball_sim_offsite_backup_age_hours`) |

### `pipeline/backup/common.py`

[Source](../../pipeline/backup/common.py) · 91 lines · Tests: [test_backup_offsite.py](../../tests/unit/test_backup_offsite.py)

The helpers both backup jobs share, pure standard library: `env_float`, `human_bytes`,
`sha256_file` (in 8 MiB reads), `fsync_path`, `copy_file_synced`, and `fresh_marks` (the
jobs whose `.running` mark exists and is younger than the stale limit).

### `pipeline/backup/data_stores.py`

[Source](../../pipeline/backup/data_stores.py) · 915 lines · Tests: [test_backup_data_stores.py](../../tests/unit/test_backup_data_stores.py), [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py), [test_backup_drill.py](../../tests/unit/test_backup_drill.py)

Back up, verify and restore the DuckDB file and the bundle. The module docstring is the
design. Exit codes: 0 every store succeeded; 1 a store failed (the other was still
attempted); 2 a refusal before any write.

| Function / class | What it does | Called from |
|---|---|---|
| `run_backup` | Refuses when the backup root is missing or read-only; deletes dead `*.partial` sets; checks free space; waits for the writer jobs; backs up each store under one set id; prunes each store to `BACKUP_KEEP` (3) sets only when every store succeeded. A crash retry within an hour skips a store that already has its set (`recent_sets`); `--new-set` turns that off. | `main` |
| `wait_for_marks` | Waits while the rebuild, the finals load or the Sunday sweep runs (their `.running` marks), up to two hours. | `run_backup` |
| `backup_duckdb` | Opens the file read-only, counts every table, copies the file and its `.wal` while it holds the handle, then opens the copy and requires the same counts. Adds the small files (`backup_small_files`: the calibration report, the prop validation, the `*.last` marks). | `run_backup` |
| `backup_bundle` | Copies the published version behind the link into `bundle/<set>/<build id>/`, without the in-use markers. | `run_backup` |
| `free_space_ok` | Needs max(`BACKUP_MIN_FREE_GB` 10 GiB, `BACKUP_FREE_FACTOR` 1.5 × the newest set) free. When short, deletes the oldest set while more than two remain, then refuses. | `run_backup` |
| `verify_set` / `check_hashes` | Checks every file's size and sha256, then the DuckDB copy's counts or a bundle load. | `main --verify-set`, `scripts/backup_drill.py` |
| `restore_duckdb` / `restore_bundle` | Puts a DuckDB set back with `os.replace`; copies a bundle set back as a new version and publishes it. Both need `--yes`. | `main` |
| `status_rows` / `print_status` | The newest set of each store, as text or JSON. | `main --status`, `make backup-status` |

### `pipeline/backup/offsite.py`

[Source](../../pipeline/backup/offsite.py) · 991 lines · Tests: [test_backup_offsite.py](../../tests/unit/test_backup_offsite.py), [test_backup_schedule.py](../../tests/unit/test_backup_schedule.py)

The off-site copy (SIM-568). It copies the newest complete sets of each store from
`/backups` to `/offsite`; the OneDrive client uploads them, and this module cannot see
that upload. Pure standard library. Exit codes as above.

| Function / class | What it does | Called from |
|---|---|---|
| `run_copy` / `_copy_locked` | Refuses when a root is missing, the off-site root is read-only, or the two roots are one folder; takes the copy lock; waits for the backup jobs; checks free space once; per store, deletes dead off-site folders, copies each wanted set that is not mirrored (oldest first), then prunes. | `main` |
| `take_lock` / `lock_holder` / `release_lock` | One copy run at a time (`offsite-copy.lock` in the mark folder). | `run_copy` |
| `busy_reasons` / `wait_until_quiet` | The data job's `nightly-backup` mark and the Postgres job's lock; waits up to two hours, then copies anyway (a complete set is always safe to copy). | `_copy_locked` |
| `copy_set` | Copies one set file by file, the manifest last, with no `.partial` folder and no rename (OneDrive holds new files open). A rerun keeps each file that already matches. | `mirror_store` |
| `is_mirrored` | True when the off-site manifest equals the local one byte for byte and every listed file has its size. | `plan_store`, `status_rows` |
| `prune_offsite` | Keeps the newest `keep` complete off-site sets and never prunes a set the local side still wants. | `mirror_store` |
| `resolve_keep` | `--keep`, else `OFFSITE_KEEP`, else `BACKUP_KEEP`, else 3. | `main` |

**Watch out:** `status.py`, `common.py` and `offsite.py` import no duckdb, and their
docstrings forbid it: the API imports `status.py`. The API
reads the backup root from `BACKUPS_ROOT`; the backup jobs read `BACKUP_ROOT`. Both
default to `/backups`.
