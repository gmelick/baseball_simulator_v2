# Player profiles

The `profiles/` folder builds each player's season profile into the DuckDB `derived.*`
tables. It is the second step of the program, after the data load
([Data ingestion](ingest.md)). A profile is the set of numbers a similarity engine compares
([Similarity engines](similarity.md)). One class, `PlayerProfileComputor`, runs the build.
The same class also builds the pools of real plays ([Play pools](pools.md)). The nightly
bundle build then reads both ([The bundle](bundle.md)). The table layouts are on the
[database page](database.md#profiles-derived).

| Part | Folder or file |
|---|---|
| The entry point, the run order and the season delete | [`profiles/build_profiles.py`](../../profiles/build_profiles.py) |
| The shared state: the one-cutoff guard, the leakage checks, the Savant season shift | [`profiles/base.py`](../../profiles/base.py) |
| The park factors | [`profiles/park_factors.py`](../../profiles/park_factors.py) |
| The pitcher profile and its arsenal fit | [`profiles/pitcher.py`](../../profiles/pitcher.py), [`profiles/arsenal_fit.py`](../../profiles/arsenal_fit.py) |
| The batter profile | [`profiles/batter.py`](../../profiles/batter.py) |
| The baserunner profile and the two steal tables | [`profiles/baserunner.py`](../../profiles/baserunner.py) |
| The manager profile | [`profiles/manager.py`](../../profiles/manager.py) |
| The run-expectancy matrix | [`profiles/leverage.py`](../../profiles/leverage.py) |
| The catcher profile | [`profiles/catcher.py`](../../profiles/catcher.py) |
| The fielder profile: the per-play metrics, then the season aggregate | [`profiles/fielder_plays.py`](../../profiles/fielder_plays.py), [`profiles/fielder.py`](../../profiles/fielder.py) |
| The field geometry and the defensive run values | [`profiles/defense.py`](../../profiles/defense.py) |
| The league-average rows | [`profiles/league_averages.py`](../../profiles/league_averages.py) |
| The shared SQL and the constants | [`profiles/sql.py`](../../profiles/sql.py), [`profiles/constants.py`](../../profiles/constants.py) |
| The six pool builders the class mixes in | [`pools/`](../../pools/) ([Play pools](pools.md)) |

## The terms

| Term | Meaning |
|---|---|
| **Profile** | One row per player, manager or venue per season in a `derived.*` table. A similarity engine compares two players through it. |
| **Mixin** | A class that adds methods to another class. `PlayerProfileComputor` is fourteen builder mixins on `ComputorBase`. |
| **Cutoff** (`asof_date`) | The date a row's data runs through. The nightly build stamps today. A cutoff build stamps its `--asof` date. |
| **Cutoff build** | A build with `--asof <date>`, for a backtest: a simulation of a past game. Every dated source stops at the cutoff. |
| **The one-cutoff rule** | Every engine refuses a profile table stamped at two dates. Eight tables carry the stamp (`ComputorBase.STAMPED_TABLES`). |
| **Survivor** | A row of a season that the run does not rebuild. The one-cutoff guard decides whether a survivor may take the new stamp. |
| **The Savant season shift** | A Savant board with no date joins by season. On a cutoff build, the cutoff's own season joins the prior season's row, because its own row holds later games. |
| **Empirical-Bayes shrinkage** | A blend of a player's own rate with the league's rate, by sample size: `(own × n + league × prior) / (n + prior)`. The prior is the sample at which the two weigh the same. "Empirical" means the league rate comes from the same data. |
| **League-average row** | One JSON profile per entity type and season in `derived.league_averages`. An engine shrinks a thin profile toward it. |
| **Sample floor** (`below_minimum_sample`) | TRUE when a profile's sample is under its floor in `constants.py`. The engines then fall back to the league-average row. |
| **Work table** | A `_tmp_*` table that one defence step writes. A season aggregate reads it, then drops it. |
| **Placeholder table** | An empty `CREATE TABLE IF NOT EXISTS` stand-in for a work table. A step skips its work table on a small sample; the placeholder keeps the aggregate's join working. |
| **Leakage check** | A check, after a builder's insert, that no source row carries a date after the cutoff. The checks cannot fail today (see `base.py`). |
| **Run expectancy** | The average runs from one state of outs and bases to the end of the half-inning. Three out counts times eight base states give 24 states. |
| **Positional insert** | An `INSERT` with no column list. DuckDB matches the `SELECT` to the table by position, so the column order matters. |
| **Outs above average (OAA)** | The outs a fielder makes beyond what an average fielder makes on the same chances. |

## How it fits together

### The nightly order

Ofelia, a cron for Docker containers, runs the jobs
([`deploy/ofelia/config.ini`](../../deploy/ofelia/config.ini)). Ofelia has no job
dependencies, so the jobs order themselves through marks. Each job runs inside
[`operations/jobs/with_mark.sh`](../../operations/jobs/with_mark.sh). While the job runs,
`/data/ingest_marks/<job>.running` holds its start time. At its end, `<job>.last` holds the
date and the exit code. Inside the mark,
[`operations/jobs/with_retry.sh`](../../operations/jobs/with_retry.sh) reruns the job after a
crash: exit 139, or "Fatal Python error" in the output. Any other failure stops it at once.

1. **07:00 UTC: the finals load** (`nightly-finals`).
   [`nightly_finals.sh`](../../operations/jobs/nightly_finals.sh) loads the last three days'
   final games into Postgres, with up to six attempts ([Data ingestion](ingest.md)). On
   Sunday at 08:00, `weekly-refresh` also sweeps the whole season.
2. **08:00 UTC: the rebuild** (`nightly-rebuild`).
   [`nightly_rebuild.sh`](../../operations/jobs/nightly_rebuild.sh) runs in the `jobs`
   container, with up to three attempts. It runs a wait step, then two build steps:
   1. [`wait_for_ingest.sh`](../../operations/jobs/wait_for_ingest.sh) waits 60 seconds.
      Then it waits while the finals load or the Sunday sweep runs, at most two hours. When
      the finals load did not run today, it runs it first. It never fails the rebuild.
   2. `python -m profiles.build_profiles --seasons <year>`. `PlayerProfileComputor.run`
      rebuilds the current season's profiles and pools. The entry point then runs
      `LeagueAverageProfiles.compute`.
   3. `python -m bundle.build_bundle --what all`. The bundle build stages a new version and
      writes every part into it. Then it moves the bundle link onto it and prunes the old
      versions ([The bundle](bundle.md)).
3. **The app restart.** The bundle watch in [`api/bundle_watch.py`](../../api/bundle_watch.py)
   looks at the link every 60 seconds. When the link moves, the watch waits for the run
   jobs, at most 10 minutes. Then it waits until no DuckDB writer holds the file. Then it
   stops the app, and Docker starts it again on the new bundle
   ([API](api.md#apibundle_watchpy)). The compose `app` service turns the restart on
   (`SIM_RESTART_ON_NEW_BUNDLE`). The rebuild ends at about 09:30.
4. **10:00 UTC: the backups.** `nightly-backup-postgres` dumps Postgres in the `db`
   container. `nightly-backup-data` copies the DuckDB file and the bundle in the `jobs`
   container. The data backup waits while the rebuild, the finals load or the Sunday sweep
   runs ([Operations](operations.md#the-backups)).
5. **10:30 UTC: the off-site copy** (`nightly-backup-offsite`). It waits for both backups.
   Then it copies the newest complete sets into the OneDrive folder.

### Inside one rebuild

```
Postgres raw.*  --(DuckDB ATTACH, read-only)-->  PlayerProfileComputor.run  (profiles/build_profiles.py)
                                                   |
     derived.* profiles + derived.run_expectancy_matrix  <-+   one cutoff per profile table
     sim.* pools (pools/)                                <-+   incremental per season
                                                   |
                      LeagueAverageProfiles.compute -> derived.league_averages
                                                   |
     bundle.build_bundle --what all  (DuckDB read-only)
         + the similarity engines   (the actor score tables, the pitcher matrix)
         + Postgres                 (each game's managers and each arm's rest)
         + the MLB Stats API        (the published fence distances)
                                                   |
     /data/play_pool/engine_artifacts.versions/<build id>/   <-- engine_artifacts (a link)
                                                   |
     EngineArtifacts.load (bundle/load_bundle.py): once in the app (shared memory), once in every worker
```

### Inside `PlayerProfileComputor.run`

1. `_connect` opens the DuckDB file and attaches Postgres read-only as `pg`. It applies
   the schema file when the DuckDB file has no profile tables.
2. The guard checks all eight stamped tables at the cutoff: the `--asof` date, else today.
   A refusal stops the run before any write, so it never waits behind the arsenal fit.
3. With `--full-rebuild`, `_delete_seasons` deletes the run's seasons from fourteen
   season-keyed `derived.*` tables.
4. The park factors (`park_factors.py`).
5. The pitcher profiles and the arsenal fit (`pitcher.py`, `arsenal_fit.py`): the slowest step.
6. The batter profiles (`batter.py`).
7. The baserunner profile, the runner's steal table and the pitcher's steal table
   (`baserunner.py`). The double-play model in step 11 reads the runner table.
8. The manager profiles (`manager.py`).
9. The run-expectancy matrix (`leverage.py`), into `self._re_matrix`. The double-play run
   values read it.
10. The catcher's framing, blocking and throwing, each into a work table (`catcher.py`).
11. The fielder's per-play steps (`fielder_plays.py`): the outfield catch probability, the
    infield OAA, the double plays, the bunt defense, the first-base scoop and the errors.
12. The season aggregates: the fielder rows, then the catcher rows. Then the
    uncaught-third-strike step updates the catcher rows.
13. The pools ([Play pools](pools.md)): the pitch pool, the batted-ball outcome pool, the
    stolen-base pool, the steal opportunity pool, then the advancement opportunity pool. A
    pool rebuilds only the seasons whose source changed, except on a full rebuild.
14. The outfield arm block (`fielder.py`). It reads the advancement pool, so it follows step 13.
15. The intentional-walk rates, then the plate-appearance situations ([Play pools](pools.md)).
16. `run` closes the connection, after an error too.

After `run`, the entry point runs `LeagueAverageProfiles.compute` over the same seasons,
unless you pass `--skip-league-averages`.

### A cutoff build (`--asof`)

A backtest simulates a past game, so its profiles must hold nothing recorded after that
day. `--asof <date>` makes a cutoff build:

- Each builder keeps `raw.pitches` and `raw.play_events` to `game_date` on or before the cutoff.
- The batter's swing sources keep the rows on or before the cutoff. The per-pitch tracking
  uses its pitch's date; the three swing and stance boards use their `asof_date`.
- The Savant boards with no date take the Savant season shift. The runner boards are
  sprint speed, baserunning and basestealing. The pitcher board is the pitcher running
  game. The defence boards are pop time, catcher throwing, arm strength, outs above average
  and the outfield jump.
- Each stamped builder first deletes the run's seasons and every later season. So a season
  that had not started at the cutoff leaves the table.
- Every stamped table ends stamped at the cutoff.
- The run-expectancy matrix stays in memory. The table's key is the season range, and a
  write replaces the live rows of that range.
- The pool builders take no cutoff. The sampler drops the later plays at draw time, by each
  pool row's game date ([Play pools](pools.md)).
- The park factors ignore the cutoff (SIM-654; see `park_factors.py`).

Run a cutoff build on a copy of the DuckDB file (`--duckdb-path`). It deletes every season
after the cutoff's year from the stamped tables. The nightly does not restore them: it
rebuilds the current season only, and it refuses a season stamped before its last game.

### Who runs the build

| Caller | What it runs |
|---|---|
| The `nightly-rebuild` job ([`operations/jobs/nightly_rebuild.sh`](../../operations/jobs/nightly_rebuild.sh)) | `python -m profiles.build_profiles --seasons <year>`: `run`, then the league averages |
| `make profile-computor` | The same entry point with no flags: the current season |
| A full recompute, by hand | `--seasons 2017 … 2026 --full-rebuild`. Then check `sim.pool_build_metadata.builder_version`, rebuild the bundle and refit the calibration (CLAUDE.md §2b) |
| `scripts/rebuild_pitch_pool.py`, `scripts/rebuild_steal_pool.py` | One pool builder, on an instance made with `__new__` over their own connection |
| The probes in `scripts/probes/`: `runner_profile_recompute.py`, `manager_profile_recompute.py`, `fielder_profile_recompute.py`, `outfield_arm_recompute.py`, `arsenal_reproducibility_check.py` | One group of builders. The four recompute probes then run the league averages |
| `ingest/load_bullpen_availability.py` (`_load_workload_records`) | `_connect` and `_close` only: it borrows the connection for `compute_bullpen_workload` |

**Docker.** [docker-compose.yml](../../docker-compose.yml) mounts `./profiles` into the
`app`, `live` and `jobs` services, and the image copies it too. An edit reaches those
containers without an image rebuild. A scheduled job runs the mounted copy at its next start.

---

## The package

### `profiles/__init__.py`

[Source](../../profiles/__init__.py) · 10 lines · Tests: none

The package docstring: one line per builder group, and the program's steps before and
after. Import every name from its own module.

---

## The build

### `profiles/build_profiles.py`

[Source](../../profiles/build_profiles.py) · 458 lines · Tests: [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py), [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py), [test_profile_index_recreate.py](../../tests/unit/profiles/test_profile_index_recreate.py), [test_park_factors.py](../../tests/unit/profiles/test_park_factors.py), [test_arsenal_fit_reproducible.py](../../tests/unit/profiles/test_arsenal_fit_reproducible.py), [test_point_in_time_batter.py](../../tests/unit/profiles/test_point_in_time_batter.py), [test_point_in_time_pitcher_manager.py](../../tests/unit/profiles/test_point_in_time_pitcher_manager.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py), [test_batter_physical_features.py](../../tests/unit/profiles/test_batter_physical_features.py), [test_profile_code_sets.py](../../tests/unit/profiles/test_profile_code_sets.py), [test_fielder_oaa_jump_profile.py](../../tests/unit/profiles/test_fielder_oaa_jump_profile.py), [test_fielder_placeholder_schema.py](../../tests/unit/profiles/test_fielder_placeholder_schema.py), [test_outfield_arm_block.py](../../tests/unit/profiles/test_outfield_arm_block.py), [test_ibb_rates.py](../../tests/unit/pools/test_ibb_rates.py), [test_pool_pull_relative_spray.py](../../tests/unit/pools/test_pool_pull_relative_spray.py), [test_etl_reload_game.py](../../tests/unit/ingest/test_etl_reload_game.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_foul_tip_strike_three.py](../../tests/unit/test_foul_tip_strike_three.py), [test_hit_by_pitch.py](../../tests/unit/test_hit_by_pitch.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py), [test_manager_defensive_subs.py](../../tests/unit/test_manager_defensive_subs.py), [test_manager_usage_score.py](../../tests/unit/test_manager_usage_score.py), [test_outfield_arm_recompute.py](../../tests/unit/test_outfield_arm_recompute.py), [test_pitch_result_vocabulary.py](../../tests/unit/test_pitch_result_vocabulary.py), [test_steal_draw.py](../../tests/unit/test_steal_draw.py), [test_steal_labels.py](../../tests/unit/test_steal_labels.py), [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_transition_pool.py](../../tests/unit/test_transition_pool.py), [test_uncaught_strike_three.py](../../tests/unit/test_uncaught_strike_three.py); by its source: [test_delete_seasons_coverage.py](../../tests/unit/profiles/test_delete_seasons_coverage.py)

The class, its run order, the season delete and the command line. The class is the sum
of fourteen mixins: eight profile builders in this folder and six pool builders in `pools/`.

| Function / class | What it does | Called from |
|---|---|---|
| `PlayerProfileComputor` | The build. `__init__` stores the Postgres address and the DuckDB path; `run` opens the connection. | the entry point; the rebuild scripts and the probes (the table above); `ingest/load_bullpen_availability.py` |
| `PlayerProfileComputor.run` | The run order (above). `seasons` defaults to the current year. | the entry point |
| `_connect` / `_close` | Opens the DuckDB file, attaches Postgres read-only as `pg` (`db.dsn.attach_pg`) and applies the schema; closes the connection. | `run`; `_load_workload_records`; the recompute probes |
| `_run_schema_ddl` | Runs `db/schemas/02_duckdb_schema.sql` when `derived.pitcher_season_metrics` is not readable. Without the file, it creates the two schemas only. | `_connect` |
| `_delete_seasons` | The full rebuild's delete: the run's seasons from fourteen named `derived.*` tables, children before parents. It drops the secondary indexes first. | `run` (`--full-rebuild`) |
| `_recreate_indexes` / `_ensure_index_if_not_exists` | Recreates each dropped index in a `finally`, as `CREATE ... IF NOT EXISTS`. It checks each one in `duckdb_indexes()`, and a missing index logs an ERROR. | `_delete_seasons` |
| `__main__` | `--seasons`, `--full-rebuild`, `--asof`, `--dsn`, `--duckdb-path`; then `LeagueAverageProfiles.compute` unless `--skip-league-averages`. Exit 2 with no DSN. | `operations/jobs/nightly_rebuild.sh`, `make profile-computor` |

**Flags:** `BASEBALL_DB_DSN` (required; `--dsn` overrides it). `BASEBALL_DUCKDB_PATH`
(default `/data/baseball_sim.duckdb`).

**Watch out:** the step order carries data between steps. The run-expectancy matrix comes
before the double plays. The runner table comes before the double plays. The aggregates
come before the uncaught-third-strike step and the arm block. The advancement pool comes
before the arm block. A new season-keyed `derived.*` table goes on `_delete_seasons`' list,
or [test_delete_seasons_coverage.py](../../tests/unit/profiles/test_delete_seasons_coverage.py)
fails. The delete also clears the three per-play detail tables, which no code writes. The
schema path is relative to the working directory, so run from the repository root.
`--full-rebuild` deletes no pool row: each pool rebuilds all the run's seasons instead.

### `profiles/base.py`

[Source](../../profiles/base.py) · 262 lines · Tests: through `PlayerProfileComputor`: [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py), [test_point_in_time_batter.py](../../tests/unit/profiles/test_point_in_time_batter.py), [test_point_in_time_pitcher_manager.py](../../tests/unit/profiles/test_point_in_time_pitcher_manager.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py)

`ComputorBase`: the state every mixin shares, and the helpers every builder calls. The
state is the two addresses, the DuckDB connection and the run-expectancy matrix. The pool
builders in `pools/` subclass it too.

| Function / class | What it does | Called from |
|---|---|---|
| `STAMPED_TABLES` | The eight tables that carry `asof_date`: the pitcher, batter, baserunner, runner-steal, pitcher-steal, manager, fielder and catcher tables. | `run`; [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py) holds it equal to the tables that declare the column |
| `_refuse_a_mixed_cutoff` | Raises `RuntimeError` when a table ends at two cutoffs after the rebuild. It runs before any delete or insert. The rules for each survivor follow this table. | `run`; each stamped builder |
| `_season_last_game_date` | The date of a season's last game in `raw.pitches`, or None. | `_refuse_a_mixed_cutoff` |
| `_stamp_one_cutoff` | Stamps every row of a table at the build's cutoff, survivors included. | each stamped builder, after its insert |
| `_delete_season_rows` | Deletes the run's seasons, and on a cutoff build every later season. | the pitcher, batter, baserunner and manager builders on a cutoff build; the catcher and fielder aggregates and the arsenal components on every build |
| `_savant_season_sql` | The SQL of the season an undated Savant board joins on: the prior season for the cutoff's own season. | `baserunner.py`, `catcher.py`, `fielder.py`, `_compute_dp_metrics` |
| `_ensure_placeholder_tables` | Runs each placeholder `CREATE TABLE IF NOT EXISTS`. A failure logs a warning, and the run goes on. | the catcher and fielder aggregates |
| `_play_events_outs_cte` | The SQL of the pickoff outs per pitcher and season, from `raw.play_events`: a common table expression (a named subquery in a WITH clause). A pickoff out lives in no pitch row. The relation is empty when the table is absent. | `_compute_pitcher_profiles`, `_build_pitcher_steal_metrics` |
| `_check_leakage` | Raises when a query's newest date passes the cutoff. On a failed query, no rows, or a value that is not a date, it logs and returns. | `_check_pitch_sources_leakage`; the batter's Savant checks; `_fill_outfield_arm_block` |
| `_check_pitch_sources_leakage` | Runs `_check_leakage` on `raw.pitches`, and on `raw.play_events` when asked and present. | every `_assert_*_have_no_leakage` |

The guard's rules for a survivor:

- a survivor stamped at the new cutoff passes;
- a survivor of the cutoff's own season fails: add that season to the run;
- a survivor of an earlier season passes when it has no stamp, or a stamp after its season ended;
- a survivor stamped inside its own season, before its last game, fails: a truncated build;
- a survivor stamped before its season began fails.

**Watch out:** the leakage checks cannot fail (SIM-655). Each check's query keeps only the
rows on or before the cutoff. It then raises when the newest row is after the cutoff, which
never happens. The checks never read the rows the builder's insert read. A check query that
errors logs a warning and passes. A table with no `asof_date` column makes the guard raise:
apply DuckDB migrations 0026 to 0028 first.

---

## The profiles

### `profiles/park_factors.py`

[Source](../../profiles/park_factors.py) · 237 lines · Tests: through `PlayerProfileComputor`: [test_park_factors.py](../../tests/unit/profiles/test_park_factors.py)

`ParkFactorsMixin` writes `derived.park_factors`. A factor is a venue's rate of an event
over the league's rate, so 1.0 is neutral.

| Function / class | What it does | Called from |
|---|---|---|
| `ParkFactorsMixin._compute_park_factors` | One row per venue, season and event type: HR, 1B, 2B, 3B, BB, K, GB, FB and R (runs). `regressed_factor` shrinks the overall factor toward 1.0 by empirical-Bayes shrinkage, with the prior `PARK_PRIOR_PA` (2,000 plate appearances). `factor_vs_l` and `factor_vs_r` split on the batter's side (`stand`), unshrunk. | `run` |

The readers take `regressed_factor`: `simulation/sim_kwargs.py` (`_park_factor_row`), the
situation engine and the bundle's reference tables.

**Watch out:** the park factors ignore the cutoff (SIM-654). `_compute_park_factors` takes
no `asof`, filters no date and deletes no later season. A cutoff build writes the season in
progress from every game, the later games included. It also keeps the later seasons' rows.
Backtests read `regressed_factor`, so a backtest's park factor can hold games after its
cutoff. The table has no `asof_date`, so the one-cutoff guard does not see it. A walk here
is `events = 'walk'`: the walk rates leave out intentional walks (the intentional-walk
decision, SIM-504).

### `profiles/pitcher.py`

[Source](../../profiles/pitcher.py) · 344 lines · Tests: [test_arsenal_fit_reproducible.py](../../tests/unit/profiles/test_arsenal_fit_reproducible.py); through `PlayerProfileComputor`: [test_point_in_time_pitcher_manager.py](../../tests/unit/profiles/test_point_in_time_pitcher_manager.py), [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py)

`PitcherProfilesMixin` writes `derived.pitcher_season_metrics` and
`derived.pitcher_gmm_components`, in two passes.

| Function / class | What it does | Called from |
|---|---|---|
| `PitcherProfilesMixin._compute_pitcher_profiles` | Pass 1 (SQL): the walk, strikeout, zone, chase, take, whiff and called-strike-plus-whiff rates. It also computes the batted-ball mix, ERA (earned runs per nine innings), FIP (fielding-independent pitching), xFIP (FIP with a league-average home-run rate on fly balls), home runs per nine and WHIP (walks and hits per inning). The innings count adds the pickoff outs. Pass 2: the arsenal fit of each pitcher-season with 200 or more pitches, in a process pool. Then the stamp and the leakage check. | `run`; `scripts/probes/arsenal_reproducibility_check.py` |
| `_assert_pitcher_profiles_have_no_leakage` | The leakage check on `raw.pitches` and `raw.play_events`. | `_compute_pitcher_profiles` |

**Watch out:** three pitch-code lists differ from the shared code sets (SIM-652); two are
here. The chase rate's swing list leaves out the pitchout codes Q and R, which `SQL_SWING`
counts. The called-strike-plus-whiff rate (`csw_rate`) leaves out Q, which `sql_csw`
counts. Pass 2 reads each season's pitches in key order (game, at-bat, pitch): the seeded
fit depends on the row order. FIP and xFIP use one league constant for every season
(`FIP_CONSTANT` 3.17; xFIP's home runs per fly ball, 0.105).

### `profiles/arsenal_fit.py`

[Source](../../profiles/arsenal_fit.py) · 351 lines · Tests: [test_gmm_batch_pipeline.py](../../tests/unit/profiles/test_gmm_batch_pipeline.py), [test_arsenal_fit_reproducible.py](../../tests/unit/profiles/test_arsenal_fit_reproducible.py), [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py)

The arsenal fit: one Gaussian mixture per pitcher-season. A Gaussian mixture (GMM) models
the pitches as a sum of bell-shaped clusters, roughly one per pitch type. The fit reads
eight features: velocity, induced vertical break, horizontal break, spin rate, spin axis,
the release point's x and z, and extension.

| Function / class | What it does | Called from |
|---|---|---|
| `GMM_FEATURE_NAMES`, `GMM_MIN_K`, `GMM_MAX_K`, `GMM_MIN_PITCHES_PER_K`, `GMM_IVB_CAP_SD` | The eight features; 2 to 7 components; at least 50 pitches per component; the induced vertical break capped at ±3 standard deviations. | `_fit_gmm_for_pitcher`; `GMM_FEATURE_NAMES` also `pitcher.py` |
| `_fit_gmm_for_pitcher` | Standardizes the features and fits each component count. It keeps the lowest BIC (Bayesian information criterion: the fit's quality less a penalty per parameter). It returns the `gmm_model` JSON and one row per component. It returns None under 100 clean pitches, or when every fit fails. | `_fit_gmm_batch` |
| `_resolve_gmm_workers` | One worker per CPU core less one, at most one per task, at least 1. | `_compute_pitcher_profiles` |
| `_chunk_tasks` | Deals the tasks in turn into at most n chunks, so the chunks' run times balance. | `_compute_pitcher_profiles` |
| `_fit_gmm_batch` | Fits one chunk in one worker process. Each task carries a float32 array, not a DataFrame. | the process pool in `_compute_pitcher_profiles` |
| `_flush_gmm_results` | Three set-based statements: flag the failed fits `below_minimum_sample`, write `gmm_model`, insert the component rows. | `_compute_pitcher_profiles` |
| `_label_component` | A display label from the component's speed and movement, such as "Sweeping Slider". No score reads it. | `_fit_gmm_for_pitcher` |

**Watch out:** the JSON stores the covariance in standardized units, where each feature's
variance is near 1. Never de-standardize it: the spin rate's entries grow about 14,000
times and swamp every similarity score. Two builds on different days differ in
`fit_diagnostics.fit_date`, so compare arsenals without it. A crashed worker chunk marks
every pitcher in it `below_minimum_sample`. The `jobs` container sets
`OPENBLAS_NUM_THREADS=1`, one numpy math thread per process. Without it the workers compete
for the cores, and the fit runs about four times slower.

### `profiles/batter.py`

[Source](../../profiles/batter.py) · 484 lines · Tests: through `PlayerProfileComputor`: [test_batter_physical_features.py](../../tests/unit/profiles/test_batter_physical_features.py), [test_point_in_time_batter.py](../../tests/unit/profiles/test_point_in_time_batter.py), [test_profile_code_sets.py](../../tests/unit/profiles/test_profile_code_sets.py), [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py)

`BatterProfilesMixin` writes `derived.batter_season_metrics`: one row per batter-season,
with a split against each pitcher hand.

| Function / class | What it does | Called from |
|---|---|---|
| `BatterProfilesMixin._compute_batter_profiles` | The plate discipline, whiff, contact, walk and strikeout rates. The batted-ball mix, the pull and opposite-field rates, exit velocity, and the hard-hit and barrel rates. Then the physical block: six swing measurements and four stance measurements, each overall and per pitcher hand. Every source stops at the build's date, the nightly build included. | `run` |
| `_assert_batter_profiles_have_no_leakage` | The leakage check on `raw.pitches` and the four Savant swing sources. | `_compute_batter_profiles` |

The swing measurements come from the per-pitch tracking (`raw.savant_pitch_tracking`) when
it saw at least 80% of the leaderboard's swings. Otherwise they come from the
`raw.savant_bat_tracking` and `raw.savant_swing_path` boards. A competitive swing has a bat
speed of 62 mph or more. A switch hitter's stance crosses over: against a left-handed
pitcher he bats right-handed.

**Watch out:** the contact rate counts only D, E, F and X as contact (SIM-652). Foul tips
(T), foul bunts (L, O) and foul pitchouts (R) count as swings, not as contact. Each one
lowers the rate as a miss does, in all three places. The insert is positional: the
physical columns come last, in `PHYSICAL_COLUMN_ORDER` (`sql.py`).
[test_batter_physical_features.py](../../tests/unit/profiles/test_batter_physical_features.py)
holds the live table's tail to that order.

### `profiles/baserunner.py`

[Source](../../profiles/baserunner.py) · 644 lines · Tests: through `PlayerProfileComputor`: [test_lead_distance.py](../../tests/unit/test_lead_distance.py), [test_steal_labels.py](../../tests/unit/test_steal_labels.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py), [test_point_in_time_pitcher_manager.py](../../tests/unit/profiles/test_point_in_time_pitcher_manager.py), [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py)

`BaserunnerProfilesMixin` writes three tables: the runner's season profile and the two
tables of the steal engines.

| Function / class | What it does | Called from |
|---|---|---|
| `_compute_baserunner_profiles` | `derived.baserunner_season_metrics`: the attempt and success rates in four extra-base situations, the steal rates, sprint speed, and Savant's four extra-base columns. Two situations follow a single: first to third, and second to home. The others are first to home on a double, and third to home on a fly. | `run`; `scripts/probes/runner_profile_recompute.py` |
| `_build_baserunner_steal_metrics` | `derived.baserunner_steal_metrics`, the steal-runner engine's table: the attempts and successes per runner, his chances on first and on second, and Savant's lead in feet. Every runner with a chance gets a row. | the same |
| `_build_pitcher_steal_metrics` | `derived.pitcher_steal_metrics`, the pitcher-steal engine's table: the steal attempts and steals he allows, the caught-stealing rate, the pickoff and step-off rates, and the lead he allows. | the same |
| `_play_events_disengagement_cte` | The SQL of the pickoff throws and step-offs per pitcher and season, from `raw.play_events`. | `_build_pitcher_steal_metrics` |
| `_assert_baserunner_profiles_have_no_leakage`, `_assert_baserunner_steal_profiles_have_no_leakage`, `_assert_pitcher_steal_profiles_have_no_leakage` | The leakage checks. | each builder |

**Watch out:** a double steal goes to the wrong runner (SIM-651). In
`_build_baserunner_steal_metrics`, the `attempts` step gives the pitch to the runner on
first. It counts the play as his attempt from second: `is_2b_steal` is the steal-of-third
flag. The runner on second gets nothing. The step's comment says the lead runner; the code
credits the trailing one. In `_compute_baserunner_profiles`, the `sb_stats` step names the
lead runner but reads the trailing runner's result. `stop_rate` is exactly 1 minus the
extra-base attempt rate. So the engine reads that rate twice (an open owner decision,
SIM-611). The season profile's insert is positional: Savant's columns come last, in
`XB_COLUMN_ORDER` (`constants.py`). The feed records step-offs only from 2023, so an
earlier step-off rate is 0.

### `profiles/manager.py`

[Source](../../profiles/manager.py) · 379 lines · Tests: through `PlayerProfileComputor`: [test_manager_usage_score.py](../../tests/unit/test_manager_usage_score.py), [test_manager_defensive_subs.py](../../tests/unit/test_manager_defensive_subs.py), [test_point_in_time_pitcher_manager.py](../../tests/unit/profiles/test_point_in_time_pitcher_manager.py), [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py)

`ManagerProfilesMixin` writes `derived.manager_season_metrics`: the manager engine's
usage, aggression and platoon tendencies. The batting manager owns the offensive calls. The
fielding manager owns the usage and the late defensive substitution.

| Function / class | What it does | Called from |
|---|---|---|
| `ManagerProfilesMixin._compute_manager_profiles` | Seven usage metrics, five aggression metrics and five platoon metrics. Six usage metrics come from the pitcher stints in `raw.pitches`. The seventh, `available_reliever_usage_rate`, divides the relievers used by those used plus the available arms held (`raw.game_bullpen_availability`). | `run`; `scripts/probes/manager_profile_recompute.py` |
| `_SQL_FIELDING_MANAGER`, `_SQL_LI_BUCKET`, `_SQL_LI_LEVEL` | The fielding team's manager; the leverage test. A game within one run in the seventh inning or later is high. Four or more runs apart, or the fifth inning or earlier, is low. | `_compute_manager_profiles` |
| `_assert_manager_profiles_have_no_leakage` | The leakage check on `raw.pitches`. | `_compute_manager_profiles` |

**Watch out:** the hit-and-run and double-switch columns stay NULL: Statcast flags
neither. A manager-season with no availability rows reads NULL for the seventh usage
metric. The availability table takes no date filter of its own: it reaches the output only
through a join onto the cut pitcher stints.

---

## The defence

### `profiles/leverage.py`

[Source](../../profiles/leverage.py) · 170 lines · Tests: [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py)

The run-expectancy matrix, from the plate appearances of innings 1 to 8.

| Function / class | What it does | Called from |
|---|---|---|
| `build_run_expectancy_matrix` | Returns a dict of (outs, bases) to the average runs to the end of the half-inning. `bases` is a bitmask: bit 0 first, bit 1 second, bit 2 third. A live build replaces the rows of `derived.run_expectancy_matrix` under the key of the run's season range. A cutoff build writes nothing. | `run`; `scripts/probes/fielder_profile_recompute.py` |
| `RE24_APPROX` | Fixed run expectancies for ten states: the fallback when the built matrix lacks a state. | `_compute_dp_metrics` |

**Watch out:** the nightly run builds the matrix from the current season alone, under the
key `<year>-<year>`. A half-inning's final score is the greater of two lower bounds: the
highest score seen, and the first score plus the runs on every pitch. Each bound misses
runs the other catches.

### `profiles/catcher.py`

[Source](../../profiles/catcher.py) · 787 lines · Tests: through `PlayerProfileComputor`: [test_uncaught_strike_three.py](../../tests/unit/test_uncaught_strike_three.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py), [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py), [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py)

`CatcherProfilesMixin` writes `derived.catcher_season_metrics`. Framing is the called
strikes a catcher gains beyond what the pitch location predicts. Blocking is the wild
pitches and passed balls he prevents. Three steps fill work tables; the aggregate joins them.

| Function / class | What it does | Called from |
|---|---|---|
| `_compute_catcher_framing` | Fits a called-strike model on the taken pitches: a logistic regression (a fit of a yes-or-no outcome). It reads the location, the distance outside the zone, the count and both hands. Writes `_tmp_framing`. It skips below 1,000 taken pitches. | `run` |
| `_compute_catcher_blocking` | Fits a wild-pitch and passed-ball model (Tom Tango's published method for catcher blocking). The main input is the pitch's height at the catcher. The others are the bounce, the distance from the zone centre, the lateral distance, the end speed and the break. Writes `_tmp_blocking`. It skips below 1,000 pitches. | `run` |
| `_compute_catcher_throwing` | The steal attempts faced and the caught-stealing rates by base. `steal_attempt_rate_against` divides the attempts of second and third by the steal chances; it is NULL below 100 chances. Writes `_tmp_catcher_throwing`. | `run` |
| `_aggregate_catcher_season_metrics` | Joins the three work tables and the Savant pop-time and catcher-throwing boards into `derived.catcher_season_metrics`, then drops the work tables. The insert replaces nothing, so it deletes the run's seasons first. | `run` |
| `_compute_catcher_uncaught_k3` | Updates the catcher rows with the uncaught-third-strike counts and `uncaught_k3_rate_eb`. The rate shrinks toward the season's league rate, with a prior of 2,000 third strikes. Then it runs the catcher leakage check. | `run` |
| `_CATCHER_PLACEHOLDER_DDL` | The three placeholder tables. | `_aggregate_catcher_season_metrics` |

**Watch out:** the steal chances count a plate appearance once per base state (SIM-653).
`catcher_pa_state` takes DISTINCT over the plate appearance and its runners. A plate
appearance whose runners move counts two or more times, though its comment says once. The
runner side counts each plate appearance's first pitch only. The pickoff columns are NULL.
`pop_time_std` is the constant 0.08. `pop_time_mean` falls back to a proxy from the
caught-stealing rate when Savant has no row. The uncaught third strike is a text match on
the strikeout's description (`des`).

### `profiles/fielder_plays.py`

[Source](../../profiles/fielder_plays.py) · 896 lines · Tests: through `PlayerProfileComputor`: [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py), [test_fielder_placeholder_schema.py](../../tests/unit/profiles/test_fielder_placeholder_schema.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py)

`FielderPlayMetricsMixin`: the per-play fielder metrics. Each step reads `raw.pitches`,
scores each play and writes one work table. A step writes no table when its sample is too
small.

| Function / class | What it does | Called from |
|---|---|---|
| `_compute_outfield_catch_probability` | Statcast's method. The speed needed divides the distance from the fielder's average spot by the hang time plus 0.4 s. Going back adds 1 ft/s. A logistic curve gives the catch probability. A catch earns 1 − P; a miss costs P. Writes `_tmp_of_plays`. It skips below 500 plays. | `run`; `scripts/probes/fielder_profile_recompute.py` |
| `_compute_infield_oaa` | The time-margin model: the batter-runner's time to first base minus the defense's time. The defense's time is the ball's travel, the exchange and the throw. Writes `_tmp_if_plays`. It reads `raw.sprint_speed` and does the season shift in Python. It skips below 500 plays. | the same |
| `_compute_dp_metrics` | Tom Tango's published double-play model, on ground balls with a runner on first only and fewer than two outs. It credits the fielder who fields the ball and the pivot. `dp_run_value` is the drop in run expectancy, from `self._re_matrix`. Writes `_tmp_dp_plays`. It skips below 200 chances. | the same |
| `_compute_bunt_defense` | The bunt fielding rates of the first and third basemen. Writes `_tmp_bunt_defense`. | the same |
| `_compute_first_base_scooping` | The first baseman's success on long throws from third base or shortstop. Writes `_tmp_1b_scoop`. | the same |
| `_compute_error_decomposition` | The fielding-error and throwing-error rates per fielder and position. Writes `_tmp_errors`. | the same |
| `_nearest_position` | The position whose average spot is nearest the ball, and the distance. | the outfield and infield steps |
| `_sprint_speed_map` | Each player-season's sprint speed; a NULL reads 27.0 ft/s. | the infield and double-play steps |

**Watch out:** the double-play model reads runner speed only from the runner table
(SIM-653). A batter with a Savant sprint speed but no row in
`derived.baserunner_season_metrics` gets the 27.0 ft/s default. The infield model reads
`raw.sprint_speed` directly. The work tables are plain tables in the DuckDB file, not TEMP
tables. A step that skips on a small sample does not drop an older table. So a work table
left by a crashed run reaches the aggregate.

### `profiles/fielder.py`

[Source](../../profiles/fielder.py) · 575 lines · Tests: through `PlayerProfileComputor`: [test_outfield_arm_block.py](../../tests/unit/profiles/test_outfield_arm_block.py), [test_fielder_oaa_jump_profile.py](../../tests/unit/profiles/test_fielder_oaa_jump_profile.py), [test_fielder_placeholder_schema.py](../../tests/unit/profiles/test_fielder_placeholder_schema.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py), [test_one_cutoff_per_table.py](../../tests/unit/profiles/test_one_cutoff_per_table.py), [test_outfield_arm_recompute.py](../../tests/unit/test_outfield_arm_recompute.py)

`FielderProfilesMixin` writes `derived.fielder_season_metrics`: one row per player,
position and season.

| Function / class | What it does | Called from |
|---|---|---|
| `_aggregate_fielder_season_metrics` | Joins the six work tables and four Savant boards (sprint speed, arm strength, outs above average, the outfield jump). The insert replaces nothing, so it deletes the run's seasons first. The arm columns go in NULL. It drops the work tables after. | `run`; `scripts/probes/fielder_profile_recompute.py` |
| `_fill_outfield_arm_block` | Resets the seven arm columns of the run's seasons. Then it fills six from `sim.advancement_opportunity_pool`: the chances, holds and assists, the thrown-out rate, and the prevention. The prevention compares his attempts against with the pool's rate at the same season, decision, outs and position. | `run`; `fielder_profile_recompute.py`, `scripts/probes/outfield_arm_recompute.py` |
| `_ensure_fielder_temp_tables_exist` / `_FIELDER_PLACEHOLDER_DDL` | The six placeholder tables. | `_aggregate_fielder_season_metrics` |
| `_assert_fielder_profiles_have_no_leakage` | The leakage check on `raw.pitches`. | `_aggregate_fielder_season_metrics` |

**Watch out:** `dp_success_rate` is NULL in every row, yet the fielder engine reads it (an
open owner decision, SIM-611). `dp_attempt_rate` holds the double plays turned per chance,
and `dp_attempts` is NULL. A row exists only for a player-position with a range play in
`_tmp_of_plays` or `_tmp_if_plays`. The insert is positional: the six Savant columns come
last, in `OAA_JUMP_COLUMN_ORDER` (`constants.py`). Savant's outs above average join per
position; the jump joins per player, on the outfield rows only. With no advancement pool
(a new database), the arm block logs a warning and stays NULL until the next nightly.

### `profiles/defense.py`

[Source](../../profiles/defense.py) · 238 lines · Tests: [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py), [test_simulation_constants.py](../../tests/unit/test_simulation_constants.py)

The defensive constants and the field geometry that the fielder and catcher models share.
Positions are in Statcast's hit coordinates (`hc_x`, `hc_y`).

| Function / class | What it does | Called from |
|---|---|---|
| `HC_SCALE`, `FIRST_BASE`, `SECOND_BASE`, `AVG_FIELDER_POS`, `AVG_THROW_VELO`, `INFIELD_EXCHANGE_TIME`, `OF_GOING_BACK_PENALTY_FPS`, `IF_AWAY_FROM_BAG_PENALTY_S`, `DEFAULT_SPRINT_SPEED_FPS` | 2.48 hit-coordinate units per foot; the bases; each position's average spot by batter side; the throw speeds; the 0.8 s exchange; the two penalties; the 27.0 ft/s default speed. | `fielder_plays.py` |
| `RUNS_PER_OAA_INFIELD`, `RUNS_PER_OAA_OUTFIELD`, `RUNS_PER_BLOCK_SAVED`, `RUNS_PER_STRIKE_ABOVE_AVG` | The run values from `simulation/constants.py`: 0.75, 0.90, 0.25 and 0.125. | `fielder.py`, `catcher.py` |
| `GRAVITY_FPS2`, `ZONE_HALF_WIDTH` | Gravity in ft/s²; half the plate's width in feet. | `catcher.py` |
| `_hc_to_feet`, `_euclidean_hc`, `_throw_time` | A hit-coordinate distance in feet; the straight-line distance; a throw's time (999.0 for no speed). | the outfield, infield and double-play steps |
| `_estimate_hang_time` | A batted ball's hang time: projectile motion with a drag correction, at least 0.5 s. | `_compute_outfield_catch_probability` |
| `_estimate_gb_travel_time` | A ground ball's time to a distance, slowed by the grass. None when it stops short. | `_compute_infield_oaa`, `_compute_dp_metrics` |
| `_classify_direction_of` | The direction a fielder moves: glove side, arm side, charging or deep. | the outfield and infield steps |
| `_fit_logistic_model` / `_predict_proba` | A logistic fit on standardized features, and its probabilities. None under 100 rows, or under 5 rows of either outcome. | the framing, blocking, outfield, infield and double-play steps |
| `_quick_auc` | The AUC (area under the curve: how well a model ranks the outcomes, from 0.5 for a coin flip to 1), for the log. | the outfield and infield steps |

**Watch out:** the run values are a settled design decision (CLAUDE.md §10). Change them in
`simulation/constants.py`, never here.

---

## The league averages

### `profiles/league_averages.py`

[Source](../../profiles/league_averages.py) · 308 lines · Tests: [test_league_average_profiles.py](../../tests/unit/profiles/test_league_average_profiles.py), [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py), [test_fielder_oaa_jump_profile.py](../../tests/unit/profiles/test_fielder_oaa_jump_profile.py), [test_outfield_arm_block.py](../../tests/unit/profiles/test_outfield_arm_block.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py)

`LeagueAverageProfiles` is a separate class with its own connection. It writes
`derived.league_averages`, the target the engines shrink a thin profile toward.

| Function / class | What it does | Called from |
|---|---|---|
| `LeagueAverageProfiles` / `LEAGUE_AVG_DDL` | Holds the DuckDB path. `compute` creates the table when it is absent: the schema file does not declare it. | `build_profiles.__main__`; the four recompute probes |
| `LeagueAverageProfiles.compute` | One JSON row per entity type and season: `pitcher`, `batter`, `fielder_<position>`, `baserunner`, `baserunner_steal`, `pitcher_steal`, `catcher` and `manager`. Each row averages the profiles at or above the sample floor. The manager row weights each manager by his games. A newer column joins its row only when the table carries it. | the same |

The readers: every actor engine, and `simulation/sim_kwargs.py`, which reads the manager rows.

**Watch out:** `compute` takes no cutoff. It reads the profile tables as they stand, so run
it after the build it describes. The position loop in `compute` asks for a `fielder_C` row, but the fielder table
holds no catcher, so `compute` writes no `fielder_C` row. `--skip-league-averages` keeps the
previous build's rows.

---

## The shared parts

### `profiles/sql.py`

[Source](../../profiles/sql.py) · 353 lines · Tests: [test_profile_code_sets.py](../../tests/unit/profiles/test_profile_code_sets.py), [test_barrel_definition.py](../../tests/unit/profiles/test_barrel_definition.py), [test_batter_physical_features.py](../../tests/unit/profiles/test_batter_physical_features.py), [test_point_in_time_batter.py](../../tests/unit/profiles/test_point_in_time_batter.py), [test_profile_data_additions.py](../../tests/unit/profiles/test_profile_data_additions.py), [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_foul_tip_strike_three.py](../../tests/unit/test_foul_tip_strike_three.py), [test_hit_by_pitch.py](../../tests/unit/test_hit_by_pitch.py), [test_pitch_result_vocabulary.py](../../tests/unit/test_pitch_result_vocabulary.py)

The SQL fragments the profile and pool builds share. The pitch-result fragments render
from the code sets in [`ingest/statcast_events.py`](../../ingest/statcast_events.py).

| Function / class | What it does | Called from |
|---|---|---|
| `sql_seasons` | Seasons as the inside of a SQL `IN` list: `2025, 2026`. | every builder in `profiles/`, `pools/` and `bundle/`; the rebuild scripts; `similarity/build_score_tables.py` |
| `SQL_OUTCOME_TYPE` | The pitch pool's class of one pitch: hit by pitch, ball, called strike, swinging strike, foul or in play. A two-strike foul tip or foul bunt is a swinging strike (strike three). | `pools/build_pitch_pool.py`; `scripts/rebuild_pitch_pool.py`, `scripts/pitch_pool_rebuild/` |
| `SQL_WHIFF`, `SQL_SWING`, `SQL_IN_PLAY` | The whiff, swing and in-play row filters. | `pitcher.py`, `batter.py`; `SQL_IN_PLAY` also `fielder_plays.py` |
| `SQL_OUTS_RECORDED`, `SQL_FIELDING_OUT`, `SQL_BATTER_RETIRED` | The out labels, from `events`, never from `outs_on_pitch`. | `pitcher.py`, `baserunner.py`; `fielder_plays.py` |
| `SQL_PULL_SPRAY` | The spray angle flipped by `stand`, so a negative angle is the pull side for both hands. | `batter.py` |
| `PHYSICAL_COLUMN_ORDER`, `COMPETITIVE_BAT_SPEED_MIN` | The physical block's column order; the 62 mph floor of a competitive swing. | `batter.py`; [test_batter_physical_features.py](../../tests/unit/profiles/test_batter_physical_features.py) |
| `_sql_swing_pivot`, `_sql_stance_pivot`, `_sql_swing_select`, `_sql_pitch_swing_agg`, `_sql_stance_select` | The generated SQL of the physical block. | `_compute_batter_profiles` |
| `_barrel_case_sql`, `HARD_HIT_MIN_VELO` | Statcast's sliding barrel: 98 mph or more, with a launch-angle band from 26–30° that widens to 8–50°. A hard-hit ball is 95 mph or more. | `batter.py` |
| `sql_credit_mask`, `_PUTOUT_SLOTS`, `_ASSIST_SLOTS` | A position bitmask of every fielder credited on a play. | `pools/build_batted_ball_pool.py` |
| `SQL_PITCHER_PITCH_COUNT`, `SQL_TIMES_THROUGH_ORDER` | The two fatigue columns, as window expressions over `raw.pitches`. | `pools/build_pitch_pool.py`; `SQL_PITCHER_PITCH_COUNT` also `pools/build_at_bat_situations.py` |

**Watch out:** change a code set in `ingest/statcast_events.py`, never a copy. Three lists
written inline in `pitcher.py` and `batter.py` already differ from the shared sets
(SIM-652). `SQL_OUTCOME_TYPE` has no `ELSE`: an unknown code yields NULL, and the NOT NULL
column refuses the insert. Its two-strike branch must precede its foul branch.

### `profiles/constants.py`

[Source](../../profiles/constants.py) · 67 lines · Tests: [test_player_profile_computor.py](../../tests/unit/profiles/test_player_profile_computor.py), [test_point_in_time_runner_catcher_fielder.py](../../tests/unit/profiles/test_point_in_time_runner_catcher_fielder.py), [test_fielder_oaa_jump_profile.py](../../tests/unit/profiles/test_fielder_oaa_jump_profile.py), [test_outfield_arm_block.py](../../tests/unit/profiles/test_outfield_arm_block.py), [test_fielder_oaa_jump_model.py](../../tests/unit/test_fielder_oaa_jump_model.py), [test_outfield_arm_model.py](../../tests/unit/test_outfield_arm_model.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py)

The sample floors, the shrinkage priors and the column orders of the two positional inserts.

| Function / class | What it does | Called from |
|---|---|---|
| `MIN_PITCHER_PITCHES` (200), `MIN_BATTER_PA` (100), `MIN_RUNNER_ADV_OPPS` (20), `MIN_RUNNER_SB_ATTEMPTS` (5), `MIN_FIELDER_PLAYS` (50), `MIN_CATCHER_SB_ATTEMPTS` (10), `MIN_MANAGER_GAMES` (50), `MIN_RUNNER_STEAL_ATTEMPTS` (10), `MIN_PITCHER_BASERUNNER_EVENTS` (30) | The sample floors behind each table's `below_minimum_sample`. | the builders |
| `PARK_PRIOR_PA` (2,000), `FIP_CONSTANT` (3.17) | The park factor's prior; the FIP constant. | `park_factors.py`; `pitcher.py` |
| `ARM_ALPHA_PRIOR_CHANCES` (50), `JUMP_ALPHA_PRIOR_PLAYS` (25) | The shrinkage priors of the outfield arm and the outfield jump. | the tests; no builder reads them |
| `XB_COLUMN_ORDER` / `BASERUNNER_TAIL_COLUMNS`, `OAA_JUMP_COLUMN_ORDER` / `FIELDER_TAIL_COLUMNS` | The last columns of the runner and fielder tables, in order. | the tests; `scripts/probes/runner_profile_recompute.py`, `scripts/probes/fielder_profile_recompute.py` |

**Watch out:** the builders write the two tails by hand in their `SELECT`. The tail
constants only feed the tests and the probes, so change both together. The fielder engine
keeps its own copies of the arm and jump priors (`similarity/engines/fielder_similarity.py`).
[test_outfield_arm_model.py](../../tests/unit/test_outfield_arm_model.py) and
[test_fielder_oaa_jump_model.py](../../tests/unit/test_fielder_oaa_jump_model.py) hold the
copies equal.
