# The bundle

The `bundle/` folder packages the play pools and the score tables into the engine-artifact
bundle. The bundle is a folder of arrays and small documents on disk. The sampler reads it
instead of a database. The bundle build is the third nightly step, after the
[player profiles](profiles.md) and the [play pools](pools.md). The sampler that reads the
bundle is on the [simulation page](simulation.md). The engines behind the scores are on the
[similarity page](similarity.md).

Two other parts touch the bundle. The app pins the version it boots with and restarts
itself on a new one ([API](api.md#apibundle_watchpy)). The nightly data backup copies the
current version ([Operations](operations.md#operationsbackupback_up_data_storespy)).

| Part | Folder or file |
|---|---|
| The nightly build: stage a version, write every artifact, publish, prune | [`bundle/build_bundle.py`](../../bundle/build_bundle.py) |
| The versioned link: stage, publish, discard, prune, the in-use markers | [`bundle/versions.py`](../../bundle/versions.py) |
| The column lists, the season window and the helpers the exports and loaders share | [`bundle/common.py`](../../bundle/common.py) |
| One export and one loader per pool | [`export_pitch_pool.py`](../../bundle/export_pitch_pool.py), [`export_batted_ball_pool.py`](../../bundle/export_batted_ball_pool.py), [`export_steal_pool.py`](../../bundle/export_steal_pool.py), [`export_advancement_pool.py`](../../bundle/export_advancement_pool.py), [`export_pitching_change_pool.py`](../../bundle/export_pitching_change_pool.py) |
| The pitcher score table and the actor embeddings | [`export_pitcher_scores.py`](../../bundle/export_pitcher_scores.py), [`export_actor_embeddings.py`](../../bundle/export_actor_embeddings.py) |
| The park geometry, the catcher receiving document and the reference tables | [`park_geometry.py`](../../bundle/park_geometry.py), [`catcher_receiving.py`](../../bundle/catcher_receiving.py), [`reference_tables.py`](../../bundle/reference_tables.py) |
| The loaded bundle, and the pool classes a worker draws from | [`load_bundle.py`](../../bundle/load_bundle.py), [`worker_pools.py`](../../bundle/worker_pools.py) |
| The in-memory bundle of the no-database tests | [`synthetic_bundle.py`](../../bundle/synthetic_bundle.py) |
| The actor score tables and the concentration check | [`similarity/build_score_tables.py`](../../similarity/build_score_tables.py) |

## The terms

| Term | Meaning |
|---|---|
| **Bundle** | The engine-artifact bundle: the folder of arrays and documents that the sampler reads. It holds `.npy` files (one numpy array each) and `.npz` files (a zipped set of arrays). It also holds parquet files (a column-wise table file) and JSON documents. |
| **Artifact** | One part of the bundle: a pool, a score table, an embedding file or a document. |
| **Version** | One complete bundle folder, `engine_artifacts.versions/<build id>/`. The build id is the UTC start time, `YYYYMMDDTHHMMSSZ`. |
| **The link** | `engine_artifacts`, a relative symbolic link (a file that points to a folder) to the current version. `bundle_link()` gives its path, `$BASEBALL_PLAY_POOL_DIR/engine_artifacts`. |
| **Staging** | The build copies the current version into a new folder and writes there. No reader sees the new folder yet. |
| **Publish** | The build writes `.published` into the new version. Then it moves the link onto the version in one atomic step. A reader sees the old version or the new one, never a mix. |
| **Prune** | The build deletes old versions. It keeps the newest three published versions, the current one and every version in use. |
| **Pin** | At boot, the app resolves the link and records the version in `BASEBALL_ENGINE_ARTIFACT_DIR`. Every worker reads that version. |
| **In-use marker** | `.in_use.<host name>`: the file the app writes into its pinned version, so the prune keeps it. |
| **The window** | The seasons the pools cover: the four newest seasons in `sim.pitch_pool` (`RECENCY_FLOOR_SEASONS`). Today that is the last three completed seasons plus the current one. |
| **`game_ymd`** | A pool row's game date as a `YYYYMMDD` integer. The sampler drops every row after a backtest's game date. |
| **Shared view** | A read-only numpy array over a shared-memory segment. The app fills each segment once; every worker attaches it instead of reading the file. |
| **Score table** | An actor score matrix: one engine's 0-to-1 similarity between every two player-seasons of one role, with a key index. A draw multiplies the live player's row into its weights. |
| **Embedding** | One player-season's vector of numeric profile metrics, keyed `id:season`. The sampler uses the key order to map a pool row's player onto a score-table column. |
| **Concentration check** | The build's check that no score table leans a draw on the live player's own staff beyond its natural share. |

## How it fits together

**1. The nightly build.** The `nightly-rebuild` job runs
[`operations/jobs/nightly_rebuild.sh`](../../operations/jobs/nightly_rebuild.sh) at 08:00 UTC.
Its second step is `python -m bundle.build_bundle --what all`. `make engine-artifacts` runs
the same command.

1. `main` finds the link through `bundle_link()`.
2. With no `--out-dir`, `stage_bundle_version` copies the current version into a new folder.
   The build writes into that folder.
3. `_build` opens the DuckDB file read-only. The seasons are `--seasons`, else the window.
4. `_build` writes the parts that `--what` names, in the order below.
5. When every step succeeds, `publish_bundle_version` moves the link onto the new version.
   `prune_bundle_versions` then deletes the old versions.
6. When any step raises, an interrupt included, `discard_bundle_version` deletes the new
   folder. The link never moved.

With `--out-dir`, the build writes into that folder in place. It stages nothing, publishes
nothing and prunes nothing.

| Order | `--what` | Function | Writes |
|---|---|---|---|
| 1 | `pool` | `build_pitch_pool_artifact` | `pitch_pool/` |
| 2 | `pool` | `build_battedball_pool_artifact` | `battedball_pool/`, and `pitch_pool/{L,R}.bb_row.npy` |
| 3 | `pool` | `build_steal_pool_artifact` | `steal_pool/` |
| 4 | `pool` | `build_advancement_pool_artifact` | `advancement_pool/` |
| 5 | `actors` | `build_actor_embeddings` | `<actor>_emb.npz` |
| 6 | `park` | `build_park_geometry` | `park_geometry.json`, `venue_dimensions.json` |
| 7 | `manager` | `build_pitching_change_pool` | `manager_pool/` |
| 8 | `receiving` | `build_receiving_profiles` | `receiving.json` |
| 9 | `reference` | `build_reference_tables` | `ibb_rates.json`, `venue_run_factors.json` |
| 10 | `pitcher_sim` | `build_pitcher_sim_matrix` | `pitcher_sim.npz` |
| 11 | `actors_sim` | `build_actor_sim_matrices` | `actor_sim/` |

`--what all` runs every row. Staging copies the current version first. So a partial build
publishes a version that keeps every file it did not rebuild.

**Docker.** [docker-compose.yml](../../docker-compose.yml) mounts `./bundle` into the `app`,
`live` and `jobs` services, and the image copies it too. An edit reaches those containers
without an image rebuild. The nightly job runs the mounted copy at its next start.

### The bundle on disk

| File or folder | What it holds | Written by | Read by |
|---|---|---|---|
| `pitch_pool/{L,R}.geom.npy`, `{L,R}.sit.npy` | Per batter hand, one row per real pitch: the ten pitch-geometry columns (speed, movement, spin, release point, plate location) and the six situation columns (balls, strikes, outs, runners, inning, score difference). | `build_pitch_pool_artifact` | `load_pitch_pools`: the pitch draw and the pitch-result draw |
| `pitch_pool/{L,R}.meta.parquet` | The same rows' pitch id, pitcher, batter, season, outcome type and recency weight. Optional: the catcher, the got-away flag, the batting side, the pitch count, the times through the order, the zone and `game_ymd`. | `build_pitch_pool_artifact` | `load_pitch_pools` |
| `pitch_pool/{L,R}.bb_row.npy` | For each pitch row, the row of its batted ball in the batted-ball pool, or −1. | `build_battedball_pool_artifact` | `load_pitch_pools`: the pitch-result draw, where a ball is born |
| `battedball_pool/{L,R}.geom.npy`, `.sit.npy`, `.pgeom.npy` | Per batter hand, one row per batted ball: exit velocity, launch angle and pull-relative spray; the situation; the ten geometry columns of the pitch that produced the ball. | `build_battedball_pool_artifact` | `load_battedball_pools`: the fielding draw, the fence stage, the advancement draws |
| `battedball_pool/{L,R}.meta.parquet` | The event, the hits, outs and runs, the recency weight, the pitcher's hand, the park and the fielder. Optional: the runner destinations, the thrown-out-advancing flags, the air-ball flag, the raw spray and distance, the batting side, the pitcher, the batted-ball class, the alignment, the putout and assist positions and `game_ymd`. | `build_battedball_pool_artifact` | `load_battedball_pools` |
| `battedball_pool/{L,R}.pitch_id.npy` | Each batted-ball row's pitch id. | `build_battedball_pool_artifact` | no loader; the pitch-pool rebuild scripts (`scripts/pitch_pool_rebuild/`) |
| `steal_pool/{2,3}.sit.npy`, `{2,3}.meta.parquet` | Per target base, one row per pitch with a stealable runner, plus the pickoff rows: count, outs and score difference; runner, pitcher and catcher; `attempted`, `success`, the three pickoff flags, the pitch class, the pickoff-row flag and `game_ymd`. | `build_steal_pool_artifact` | `load_steal_pools`: the pickoff and steal draws |
| `advancement_pool/<key>.feat.npy`, `<key>.meta.parquet` | Per runner decision (eight keys): exit velocity, launch angle, spray, distance and outs; runner, fielder, position, `attempted`, `safe`, `error_extra`, the recency weight and `game_ymd`. | `build_advancement_pool_artifact` | `load_advancement_pools`: the advancement draws |
| `manager_pool/change.sit.npy`, `change.meta.parquet` | One row per plate-appearance boundary while a pitcher is on the mound, changed or not: pitch count, batters faced, inning, outs, runners, score difference; the pitcher, the incoming arm, the change flags, the fielding manager, the incoming arm's rest and hand, `game_ymd`. | `build_pitching_change_pool` | `load_change_pool`: the pitching-change draw and the reliever draw |
| `manager_pool/roles.parquet` | Per incoming arm and season: his entries, his share of high-leverage entries and his mean entry inning. | `build_pitching_change_pool` | `load_change_pool`: the reliever draw's role weight |
| `manifest.json` in each pool folder | The seasons, the row counts and the column names. The change pool's manifest adds its change rates and its coverage. | each pool export | four loaders check that it exists; `scripts/accuracy/selection.py` records its time |
| `pitcher_sim.npz` | The pitcher score table: the profile index and the scores as JSON text, and the same scores as a dense float32 matrix. | `build_pitcher_sim_matrix` | `load_pitcher_sim`: the pitch draw, the pitching-change draw and the reliever draw |
| `actor_sim/<name>.npz`, `actor_sim/manifest.json` | One score table and its key index per role; the seasons and each table's size. | `build_actor_sim_matrices` | `_load_actor_sim`: every draw that weights by an actor |
| `actor_sim/concentration.json` | The concentration check's report. | `build_actor_sim_matrices` | no program; you read it after a build |
| `<actor>_emb.npz` | For batter, catcher, fielder, baserunner, manager and pitcher_steal: each player-season's metrics vector, the keys, each feature's mean and standard deviation, and the feature names. | `build_actor_embeddings` | `load_actor_embeddings`: the sampler's map from pool rows to score-table columns |
| `park_geometry.json` | Each park's effective fence by spray sector and season group, the carry model and each park's carry offset. | `build_park_geometry` | `EngineArtifacts.load`: the fence stage; `scripts/fence_check.py` |
| `venue_dimensions.json` | The MLB Stats API's published fence distances, merged over the last copy. | `write_venue_dimensions` ([ingest](ingest.md)), called by `build_park_geometry` | `build_park_geometry` |
| `park_geometry_overrides.json` | Optional fence corrections a person writes by hand. | a person | `build_park_geometry` |
| `receiving.json` | The league called-strike and got-away rates, and each catcher-season's framing multipliers and blocking ratio. | `build_receiving_profiles` | `EngineArtifacts.load`: the catcher receiving ratio |
| `ibb_rates.json`, `venue_run_factors.json` | The intentional-walk rate per base-out cell; the regressed run factor per park and season. | `build_reference_tables` | `EngineArtifacts.load`: the worker factory (`simulation/production_factory.py`) |
| `.published`, `.in_use.<host>` | The time of the publish; the app's in-use marker. | `publish_bundle_version`; `mark_in_use` | `prune_bundle_versions`; the backup reads `.published` |

### The load path in a worker

1. At boot, the app pins the version (`pin_bundle_version` in
   [`api/bundle_watch.py`](../../api/bundle_watch.py)).
2. The app loads the bundle once with `EngineArtifacts.load` and calls
   `extract_shared_arrays` ([`api/startup.py`](../../api/startup.py)).
3. `BatchRunner` copies each array once into a named shared-memory segment
   (`publish_shared_arrays` in [`simulation/batch_runner.py`](../../simulation/batch_runner.py)).
4. The runner starts its workers through forkserver by default (`SIM_MP_START_METHOD`):
   each worker starts from a lean server process. At start, `_worker_init` attaches every
   segment as a read-only shared view.
5. At its first game, a worker's factory picks the folder (`_artifact_dir` in
   [`simulation/production_factory.py`](../../simulation/production_factory.py)). It takes the
   `_pool_dir` run argument, else `BASEBALL_ENGINE_ARTIFACT_DIR`, else the link.
6. `EngineArtifacts.load(folder, shared_views=views)` takes each shared array from its view.
   It reads the rest from disk: the text columns, the key indexes and the documents. The
   text columns are the outcome type, the event and the pitcher's hand.
7. A failed load raises `RuntimeError`. There is no fallback simulator. The worker keeps the
   loaded bundle and its sampler for its life.

A failed preload at boot never stops the app. The runner then starts without shared
arrays, and each worker reads the whole bundle from disk.

### The version pin and the restart

The app sets `BASEBALL_ENGINE_ARTIFACT_DIR` before its worker pool starts. A worker that
respawns after a rebuild therefore reads the same version as the parent's shared arrays.
The app also writes its in-use marker, so the prune keeps its version. The shutdown removes
the marker.

With `SIM_RESTART_ON_NEW_BUNDLE` on (the compose `app` service sets it), a task looks at the
link every 60 seconds. On a new version it waits for the run jobs (at most 600 seconds),
then for any DuckDB writer. It then stops the app, and Docker starts it again on the new
version. The [API page](api.md#apibundle_watchpy) describes the watch.

The nightly data backup copies the current version, without the in-use markers. A restore
copies a set into the versions folder and publishes it. The restore drill loads the copy
with `EngineArtifacts.load` ([Operations](operations.md#operationsbackupback_up_data_storespy)).

---

## The package

### `bundle/__init__.py`

[Source](../../bundle/__init__.py) · 10 lines · Tests: none

The package docstring: what the bundle is, its parts, and the steps before and after.
Import every name from its own module. Two tests read the source of every module in the
folder. [test_bundle_no_pickle.py](../../tests/unit/bundle/test_bundle_no_pickle.py) fails
on a load that allows a pickle.
[test_arm_angle_decision.py](../../tests/unit/test_arm_angle_decision.py) fails on a read
of the per-pitch arm angle.

---

## The build and the versions

### `bundle/build_bundle.py`

[Source](../../bundle/build_bundle.py) · 166 lines · Tests: none

The nightly entry point. It stages a version, writes the parts, publishes the version and
prunes the old ones.

| Function / class | What it does | Called from |
|---|---|---|
| `main` | Parses the arguments. With no `--out-dir`, it stages a version, runs `_build`, publishes and prunes. On any error it discards the staged folder and re-raises. Returns 0. | `operations/jobs/nightly_rebuild.sh`, `make engine-artifacts`, this module's `__main__` |
| `_build` | Opens the DuckDB file read-only and writes the parts `--what` names, in the fixed order of the build table above. | `main` |

| Argument | What it does |
|---|---|
| `--duckdb-path` | The DuckDB file to read. |
| `--out-dir` | Writes the bundle in place there, with no staging and no publish. |
| `--seasons` | Overrides the window. |
| `--what` | `pool` (the default), `pitcher_sim`, `actors`, `actors_sim`, `park`, `manager`, `receiving`, `reference` or `all`. |
| `--strict-concentration` | Fails the build when the concentration check fails. |
| `--pitcher-sim-limit N` | Scores only N profiles per score table, for a quick check. |
| `--matrix NAME` | Rebuilds only the named score table in the `actors_sim` step; repeatable. |

**Flags:** `BASEBALL_PLAY_POOL_DIR` (the link's folder; default `/data/play_pool`).
`BASEBALL_DUCKDB_PATH` (the `--duckdb-path` default; `/data/baseball_sim.duckdb`).
`BASEBALL_DB_DSN` (the change pool's one Postgres read).

**Watch out:**

- `--what` defaults to `pool`, not `all`. A bare run publishes a version with new pools and
  every other file copied from the current version.
- `--pitcher-sim-limit N` cuts the pitcher score table and every actor score table to N
  profiles. Without `--out-dir`, the same run publishes that truncated bundle, and the app
  restarts onto it. Pass the limit only with a scratch `--out-dir` (refuse the limit
  without one, SIM-656).
- The nightly job runs without `--strict-concentration`, so a failed concentration check
  only logs a warning.
- `--matrix` acts in the `actors_sim` step only. It rebuilds the named tables and skips the
  concentration check.
- A build into a fresh `--out-dir` starts with no `park_geometry_overrides.json` and no
  `venue_dimensions.json`. Only a staged build copies them from the current version.

### `bundle/versions.py`

[Source](../../bundle/versions.py) · 218 lines · Tests: [test_bundle_versions.py](../../tests/unit/bundle/test_bundle_versions.py), [test_api_main_wiring.py](../../tests/unit/test_api_main_wiring.py)

Each build becomes a version behind one link. A worker opens the link's path; the link
resolves it to the current version. So a worker that starts during a rebuild never reads a
half-written file.

| Function / class | What it does | Called from |
|---|---|---|
| `bundle_link` | The link's path, `<pool_dir>/engine_artifacts`. `pool_dir` defaults to `$BASEBALL_PLAY_POOL_DIR`, else `/data/play_pool`. | `build_bundle.main`, `api/startup.py`, `api/bundle_watch.py`, `simulation/production_factory.py`, `operations/backup/back_up_data_stores.py`, `scripts/accuracy/selection.py`, the pool-rebuild scripts |
| `versions_root` / `resolve_bundle_dir` | The folder that holds the versions (`engine_artifacts.versions`); the real folder the link points to. | this module; `api/bundle_watch.py`; `back_up_data_stores.py` |
| `stage_bundle_version` | Creates `engine_artifacts.versions/<build id>/` and copies the current version into it. The copy leaves out the in-use markers, the `tmp_*` files and `.published`. With no current bundle, the folder starts empty. | `build_bundle.main` |
| `publish_bundle_version` | Converts a legacy folder first. Writes `.published` into the version, then moves a temporary relative link onto the link with `os.replace`. | `build_bundle.main`, `back_up_data_stores.restore_bundle` |
| `discard_bundle_version` | Deletes a staged version that failed. | `build_bundle.main` |
| `prune_bundle_versions` | Keeps the newest `KEEP_VERSIONS` (3) published versions, the current one and every version with an in-use marker. Deletes an unpublished folder older than the current version: a build whose process died. Keeps a newer unpublished folder, which may be a build in progress. | `build_bundle.main` |
| `convert_legacy_bundle` | Moves a real folder at the link's path to `engine_artifacts.versions/legacy-<build id>` and links to it. | `publish_bundle_version`, `api/bundle_watch.pin_bundle_version` |
| `mark_in_use` / `unmark_in_use` | Writes or removes `.in_use.<tag>` in a version. The app's tag is its host name. | `pin_bundle_version`; `api/startup.close_resources` |

**Flags:** `BASEBALL_PLAY_POOL_DIR` (default `/data/play_pool`).

**Watch out:** never edit a published version in place: a worker may read it at any time.
Build a new version instead. A segfault skips the discard step; the next prune deletes the
dead folder. Two builds in one second get `<build id>-2`, `-3` and so on.

---

## The shared pieces

### `bundle/common.py`

[Source](../../bundle/common.py) · 191 lines · Tests: [test_pool_label_check.py](../../tests/acceptance/test_pool_label_check.py), [test_bundle_no_pickle.py](../../tests/unit/bundle/test_bundle_no_pickle.py), [test_engine_artifacts_shared_memory.py](../../tests/unit/bundle/test_engine_artifacts_shared_memory.py), [test_pitcher_sim_matrix.py](../../tests/unit/bundle/test_pitcher_sim_matrix.py), [test_filter_cells.py](../../tests/unit/test_filter_cells.py), [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py)

The helpers the exports and the loaders share. Each column reader takes the shared view
when the parent published one, and reads the file otherwise.

| Function / class | What it does | Called from |
|---|---|---|
| `_GEOM_COLS` / `_SIT_COLS` | The ten pitch-geometry columns and the six situation columns, in array order. | the pitch and batted-ball exports; `scripts/pitch_pool_rebuild/preflight.py` |
| `RECENCY_FLOOR_SEASONS` / `last_n_seasons` | The window: 4 seasons. `last_n_seasons` returns the four newest seasons of `sim.pitch_pool`, oldest first. | `build_bundle._build`; `scripts/rebuild_pitch_pool.py`, `scripts/rebuild_steal_pool.py`, `scripts/measure_filter_cells.py`; the two preflight modules |
| `DEFAULT_DUCKDB_PATH` | `$BASEBALL_DUCKDB_PATH`, else `/data/baseball_sim.duckdb`. | `build_bundle.main` |
| `ymd_select` / `GAME_YMD_SQL` | The SELECT fragment that exports `game_date` as the `game_ymd` integer, or a blank when the table has no `game_date`. | the pitch, batted-ball, steal and advancement exports |
| `join_rows` | For each key, the index of its first match in the targets, else −1. A negative key never matches. | `export_batted_ball_pool.pitch_join_rows` |
| `_savez_plain` | Writes a compressed `.npz` and refuses an object array, naming the file and the member. | `build_pitcher_sim_matrix`, `build_actor_embeddings`, `build_score_tables._write_actor_sim` |
| `float_columns` | Stacks columns of a DuckDB result into one float32 matrix. NULL and NaN read as 0. | every pool export |
| `write_json` / `read_json` | Writes a JSON document; reads one, or None when the file is absent. | the exports; `read_json`: `EngineArtifacts.load`, `build_park_geometry`, `reference_tables` |
| `take_view` | The shared view by name, else `np.load` of the file. | every pool loader |
| `meta_column` / `optional_meta_column` / `recency_column` / `game_ymd_column` | A metadata column as a typed array, from the shared view when one exists. `optional_meta_column` returns None when an older bundle lacks the column. `recency_column` reads a NULL weight as 1.0. `game_ymd_column` reads NULL as 0, and None without the column. | the pool loaders |

**Flags:** `BASEBALL_DUCKDB_PATH` (default `/data/baseball_sim.duckdb`).

**Watch out:**

- The bundle never holds a pickle, and every load passes `allow_pickle=False`. A pickle is
  numpy's form for an object array, and loading one can run code. The bundle holds only
  floats, integers and JSON text, so it needs none. `_savez_plain` refuses an object array
  at build time. [test_bundle_no_pickle.py](../../tests/unit/bundle/test_bundle_no_pickle.py)
  parses `bundle/` and `similarity/build_score_tables.py`, and fails on a load without the
  keyword. Put a text column in a parquet file, never in an `.npy` file.
- `POOL_WINDOW` in `tests/acceptance/bands.py` must name as many seasons as
  `RECENCY_FLOOR_SEASONS`.

### `bundle/worker_pools.py`

[Source](../../bundle/worker_pools.py) · 360 lines · Tests: [test_engine_artifacts_realism.py](../../tests/unit/bundle/test_engine_artifacts_realism.py), [test_engine_artifacts_shared_memory.py](../../tests/unit/bundle/test_engine_artifacts_shared_memory.py), [test_pitcher_sim_matrix.py](../../tests/unit/bundle/test_pitcher_sim_matrix.py), [test_profile_data_additions.py](../../tests/unit/profiles/test_profile_data_additions.py), [test_accuracy_fit_instruments.py](../../tests/unit/test_accuracy_fit_instruments.py), [test_actor_matrices.py](../../tests/unit/test_actor_matrices.py), [test_actor_powers.py](../../tests/unit/test_actor_powers.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_fence_carry_and_margin.py](../../tests/unit/test_fence_carry_and_margin.py), [test_fence_season_groups.py](../../tests/unit/test_fence_season_groups.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py), [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py), [test_manager_draw.py](../../tests/unit/test_manager_draw.py), [test_matchup_weight_cache.py](../../tests/unit/test_matchup_weight_cache.py), [test_outs_identity.py](../../tests/unit/test_outs_identity.py), [test_pitch_cell_index.py](../../tests/unit/test_pitch_cell_index.py), [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py), [test_pitcher_outs_credit.py](../../tests/unit/test_pitcher_outs_credit.py), [test_realism_consumers.py](../../tests/unit/test_realism_consumers.py), [test_receiving_kernel_consumers.py](../../tests/unit/test_receiving_kernel_consumers.py), [test_receiving_ratio.py](../../tests/unit/test_receiving_ratio.py), [test_relief_draw.py](../../tests/unit/test_relief_draw.py), [test_running_game_on_the_pitch.py](../../tests/unit/test_running_game_on_the_pitch.py), [test_sampler_hot_path_cache.py](../../tests/unit/test_sampler_hot_path_cache.py), [test_situation_sigma_env.py](../../tests/unit/test_situation_sigma_env.py), [test_steal_draw.py](../../tests/unit/test_steal_draw.py), [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_steal_weights_seam.py](../../tests/unit/test_steal_weights_seam.py), [test_transition_draw.py](../../tests/unit/test_transition_draw.py), [test_uncaught_strike_three.py](../../tests/unit/test_uncaught_strike_three.py)

The pool classes a worker draws from, and the columns of each pool that the workers share.
An optional column is None on a bundle exported before the column existed. Its reader then
stays neutral.

| Function / class | What it does | Called from |
|---|---|---|
| `HandPool` | One batter hand's pitch pool: geometry, situation, pitcher, batter, season, outcome type, recency. Optional: catcher, got-away, batting side, pitch count, times through the order, batted-ball row, zone, `game_ymd`. | `load_pitch_pools`, `synthetic_bundle.pitch_pool`; the pitch and pitch-result draws |
| `BattedBallPool` | One batter hand's batted-ball pool: geometry, situation, batter, season, event, hits, outs, recency, and the optional columns of the parquet. | `load_battedball_pools`, `synthetic_bundle.battedball_pool`; the fielding draw, the fence stage, the advancement draws |
| `StealPool` | One target base's steal opportunity pool. A missing pickoff column becomes all zeros, so the draw stages no pickoff. | `load_steal_pools`, `synthetic_bundle.steal_pools`, `simulation/sampler/steal.py`, `scripts/running_game_replay.py` |
| `AdvancementPool` | One runner decision's opportunity pool, attempted or not. | `load_advancement_pools`, `synthetic_bundle.advancement_pools` |
| `ChangePool` | The pitching-change opportunity pool. | `load_change_pool`, `synthetic_bundle.change_pool` |
| `STEAL_PITCH_CLASSES` / `STEAL_PITCH_CLASS_CODE` | The six pitch classes and their codes, 1 to 6. Code 0 means no class: a pickoff row. | `simulation/sampler/steal.py`, `synthetic_bundle`, `scripts/steal_pool_rebuild/` |
| `steal_pitch_class_codes` | Maps the exported class names to int8 codes, once per distinct name. An unknown name reads 0 and logs a warning. | `load_steal_pools` |
| `_HAND_POOL_SHAREABLE_ATTRS` and the four other `_*_SHAREABLE_ATTRS` tuples | The numeric columns of each pool that the parent publishes into shared memory. | `EngineArtifacts.extract_shared_arrays` |

**Watch out:**

- `game_ymd` is a shareable column of the change pool only. Every worker reads the other
  four pools' dates from parquet. That costs memory per worker and changes no number (put
  every pool's game date in shared memory, SIM-657).
- `STEAL_PITCH_CLASSES` copies `simulation.game_state.PITCH_OUTCOMES`, so this module needs
  no import of the simulation package.
  [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py) holds the two equal.

---

## The pool exports

Each export reads the play pools in DuckDB `sim.*` over the window and writes one folder.
The same module holds the loader that reads the folder back. The pool tables are on the
[play pools page](pools.md).

### `bundle/export_pitch_pool.py`

[Source](../../bundle/export_pitch_pool.py) · 171 lines · Tests: [test_engine_artifacts_realism.py](../../tests/unit/bundle/test_engine_artifacts_realism.py), [test_profile_data_additions.py](../../tests/unit/profiles/test_profile_data_additions.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py), [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py)

The pitch pool, one file set per batter hand.

| Function / class | What it does | Called from |
|---|---|---|
| `build_pitch_pool_artifact` | Per hand, writes `geom.npy` and `sit.npy` from one query, then `meta.parquet` through a DuckDB `COPY`. It probes the optional columns, so an older table still exports. An unknown draw-conditioning value exports as neutral: batting side −1, pitch count −1, times through the order 0. Returns the rows per hand. | `build_bundle._build`, `scripts/pitch_pool_rebuild/export.py` |
| `load_pitch_pools` | Builds both hands' `HandPool`. Each numeric column comes from its shared view when one exists. The worker always reads the parquet, because the outcome type is text. It reads `bb_row.npy` when the file exists. | `EngineArtifacts.load` |
| `_PP_CONDITIONING_COLS` | The three draw-conditioning columns. The export writes them only when all three exist. | `build_pitch_pool_artifact` |

**Watch out:**

- The arrays and the parquet come from two scans with no ORDER BY. Their rows line up only
  through DuckDB's stored order. A pool build can change that order. The pitch-result draw
  lowers a candidate's weight where similar pitches crowd the set. It picks the reference
  rows for that density weight by position. So the density weights move between pool
  builds (export the pitch pool in pitch order, SIM-635).
- Run the batted-ball export after this one. It writes `bb_row.npy` from this export's
  parquet.

### `bundle/export_batted_ball_pool.py`

[Source](../../bundle/export_batted_ball_pool.py) · 379 lines · Tests: [test_engine_artifacts_realism.py](../../tests/unit/bundle/test_engine_artifacts_realism.py), [test_profile_data_additions.py](../../tests/unit/profiles/test_profile_data_additions.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_fence_carry_and_margin.py](../../tests/unit/test_fence_carry_and_margin.py), [test_fence_season_groups.py](../../tests/unit/test_fence_season_groups.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py), [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py), [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py), [test_transition_pool.py](../../tests/unit/test_transition_pool.py)

The batted-ball pool the fielding draw reads, one file set per batter hand. The module also
writes the join from each pitch row to its ball.

| Function / class | What it does | Called from |
|---|---|---|
| `build_battedball_pool_artifact` | Per hand, exports the `sim.outcome_pool` rows with an exit velocity, a launch angle and a spray angle. One query writes `geom.npy`, `sit.npy`, `pgeom.npy` and `pitch_id.npy`; a `COPY` writes `meta.parquet`. It writes `pitch_pool/{hand}.bb_row.npy` when the pitch pool's parquet exists. It keeps every row. | `build_bundle._build` |
| `pitch_join_rows` | For every row of the pitch pool's parquet, the batted-ball row of its ball, else −1. It joins on the pitch id through `join_rows`. | `build_battedball_pool_artifact`, `scripts/pitch_pool_rebuild/export.py`, `scripts/pitch_pool_rebuild/preflight.py` |
| `BB_CLASS` | The batted-ball class codes: ground ball 1, line drive 2, fly ball 3, popup 4, every bunt 5, unknown 0. | the export's `bb_class` column |
| `load_battedball_pools` | Builds both hands' `BattedBallPool`. It selects only the columns the parquet holds, so an older bundle still loads. | `EngineArtifacts.load` |
| `_bb_fielders` | The fielder ids at positions 2 to 9, from the shared view or the parquet's `fielder_2` to `fielder_9`. | `load_battedball_pools` |

**Watch out:**

- The geometry arrays and the parquet come from two scans with no ORDER BY. Their rows
  line up only through DuckDB's stored order. The fielding draw picks its density reference
  rows by position, so its density weights move between pool builds. The simulated mix of
  hits then moves with no change in code or data. The fix is to export the batted-ball pool
  in pitch order (SIM-636).
- `pgeom.npy` keeps NaN; `geom.npy` and `sit.npy` turn NaN into 0. The pitch-similarity
  kernel treats a missing value as neutral, and a 0.0 would be an outlier.
- The export keeps the rows whose runner transition fails the outs check. The fielding
  draw's cell index drops them (`dest_ok = 0`).

### `bundle/export_steal_pool.py`

[Source](../../bundle/export_steal_pool.py) · 213 lines · Tests: [test_steal_draw.py](../../tests/unit/test_steal_draw.py), [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_steal_pool_rebuild.py](../../tests/unit/test_steal_pool_rebuild.py)

The steal opportunity pools: target "2" is the steal of second base, target "3" the steal
of third. One row is a pitch where a steal was possible, attempted or not. The pickoff rows
sit in the same pool.

| Function / class | What it does | Called from |
|---|---|---|
| `build_steal_pool_artifact` | Refuses, before it writes a file, a table without the `pitch_class` and `is_pickoff_row` columns of DuckDB migration 0031. Writes each target's files under `tmp_` names. Moves them into place when both targets succeed, then writes the manifest. | `build_bundle._build`, `scripts/steal_pool_rebuild/export.py` |
| `_write_steal_pool_targets` | Writes one target's `sit.npy` and `meta.parquet`. Both queries sort by `pitch_id`, so row i of one file is row i of the other. Warns when a pitch row has no class. | `build_steal_pool_artifact` |
| `load_steal_pools` | Builds the two `StealPool`s and maps the class names to codes. Raises `ValueError` when a target's two files hold different row counts. | `EngineArtifacts.load` |

**Watch out:** a pitch row with no class comes from a season no builder has rebuilt since
migration 0031. One such row makes the whole bundle run the single pre-pitch steal draw.
The export still succeeds and only warns. A table without the two columns fails the whole
build, and the build discards the staged version.

### `bundle/export_advancement_pool.py`

[Source](../../bundle/export_advancement_pool.py) · 122 lines · Tests: [test_transition_pool.py](../../tests/unit/test_transition_pool.py)

The advancement opportunity pools: one pool per runner decision, attempted or not. A key
reads `"{scenario}_{from_base}_{target_base}"`. The five scenarios:

1. first to third on a single;
2. second to home on a single;
3. first to home on a double;
4. a tag-up on a caught ball;
5. the batter's try for an extra base.

| Function / class | What it does | Called from |
|---|---|---|
| `build_advancement_pool_artifact` | Per key, writes `feat.npy` from one query and `meta.parquet` from a `COPY`. Returns `{}` and warns when the database has no `sim.advancement_opportunity_pool`. | `build_bundle._build` |
| `load_advancement_pools` | Builds an `AdvancementPool` per key that has a parquet. Returns `{}` without the manifest. | `EngineArtifacts.load` |
| `_ADV_KEYS` / `_ADV_FEAT_COLS` | The eight keys; the five kernel features (exit velocity, launch angle, spray, distance, outs). | the export and the loader; `scripts/pitch_pool_rebuild/common.py` |

**Watch out:** the features and the parquet come from two scans with no ORDER BY, as in the
pitch pool. Their rows line up only through DuckDB's stored order. The two export-order
tickets name only the pitch and batted-ball pools (SIM-635, SIM-636). With no advancement
pool, the advancement draw stages no extra-base try.

### `bundle/export_pitching_change_pool.py`

[Source](../../bundle/export_pitching_change_pool.py) · 317 lines · Tests: [test_manager_draw.py](../../tests/unit/test_manager_draw.py)

The pitching-change opportunity pool for the manager draw. A row is a plate-appearance
boundary while a pitcher is on the mound, changed or not. The rows with no change give the
draw its denominator. A row describes the pitcher of the PREVIOUS plate appearance on that
side. It holds his pitches and batters faced so far, and whether he started.

| Function / class | What it does | Called from |
|---|---|---|
| `build_pitching_change_pool` | Builds the pool in a temporary table. `sim.pitch_pool` gives each plate appearance's pitcher, pitches and recency; `derived.at_bat_situations` gives the boundary. Writes `change.sit.npy`, `change.meta.parquet`, `roles.parquet` and the manifest. `game_context` picks the source of the manager and rest columns. `True` reads Postgres; a triple supplies the values directly; `None` leaves every row unknown. | `build_bundle._build` |
| `_default_game_context` | The one Postgres read (`fetch_game_context_sync` in [`pipeline/bullpen_usage.py`](../../pipeline/bullpen_usage.py)): each game's two managers, each appearance's rest and each arm's throwing hand. Returns None when `BASEBALL_DB_DSN` is unset or the read fails. | `build_pitching_change_pool` |
| `load_change_pool` | Builds the `ChangePool` and the role table, keyed `"pitcher_id:season"`. A date column with no positive value reads as absent, so a date cutoff refuses the pool. | `EngineArtifacts.load` |

**Flags:** `BASEBALL_DB_DSN` (the Postgres read; without it the pool still builds).

**Watch out:**

- Without the DSN, or after a failed read, the manager and rest columns read −1 (unknown).
  The log warns, and the manifest's `manager_coverage` and `rest_coverage` show it.
- `roles.parquet` comes from a GROUP BY with no ORDER BY. So two builds write its rows in
  different orders. No reader uses the order. But a byte comparison of two bundles always
  differs (write the role file in a fixed row order, SIM-597).
- Every read of the temporary table sorts by game and at-bat number. So the two pool files
  share one row order.

---

## The documents

### `bundle/park_geometry.py`

[Source](../../bundle/park_geometry.py) · 739 lines · Tests: [test_park_geometry_builder.py](../../tests/unit/bundle/test_park_geometry_builder.py), [test_fence_check.py](../../tests/unit/test_fence_check.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py)

Builds `park_geometry.json`, version 2. A sector is a 10-degree band of the field-side spray
angle; eleven sectors cover −55 to +55 degrees. A park's effective fence in a sector is the
carry a ball needs to leave the park. The pieces are pure functions over numpy arrays, so
`scripts/fence_check.py` rebuilds a line from any subset of the balls.

| Function / class | What it does | Called from |
|---|---|---|
| `build_park_geometry` | Loads the air balls and refreshes `venue_dimensions.json`. Reads `park_geometry_overrides.json`, builds the document and fits the carry model on the home runs. Writes `park_geometry.json`. | `build_bundle._build` |
| `build_geometry_document` | The document without the carry model: the grid, the league line, and each park's lines per season group with their sources and support. It also holds the league offset, the priors, the moves, the overrides and the carry offsets. | `build_park_geometry`, `scripts/fence_check.py` |
| `load_air_balls` | The window's fly balls and line drives with a park, a distance and a spray angle. | `build_park_geometry` |
| `sector_line` | THE LINE RULE: the midpoint of the home runs' 10th percentile and the other air balls' 99th percentile. Each side needs 10 balls (`PARK_MIN_HR`, `PARK_MIN_KEPT`). | `_line_of`, `scripts/fence_check.py` |
| `detect_moves` | THE CHANGE DETECTOR: splits a park's seasons where a sector's line moved. A move is a gap of 10 feet or more between the early and late lines, each on 10 home runs. | `build_geometry_document`, `scripts/fence_check.py` |
| `_thin_rule` | THE THIN RULE: blends a thin sector's own line toward its prior at a weight of 10 home runs (`PARK_PRIOR_N`). Without an own line, the prior stands, else the league line. | `build_geometry_document` |
| `published_at` / `league_offsets` / `prior_lines` | The published fence interpolated at a sector's middle angle; the league's median gap between the effective and the published fence; one park's prior line. | `build_geometry_document` |
| `carry_offsets` | THE CARRY OFFSET: per park, its home-run carry against the league's at the same exit velocity, launch angle and spray. A park under 100 home runs gets none. The offsets centre on the median park. | `build_geometry_document` |
| `carry_design` / `carry_predict` | The carry model: a quadratic in exit velocity and launch angle; the modelled carry of one ball. | `build_park_geometry`; `carry_predict`: `simulation/sampler/fence.py` |
| `sector_of` / `park_sector` / `sector_midpoint` | The sector of each spray angle; the sector of one angle (only tests call it); a sector's middle angle. | `build_geometry_document`, `scripts/fence_check.py` |
| `group_key` / `parse_group_key` / `group_for_season` | A season group's key, `"FIRST-LAST"`; its two seasons; the group that holds a season, else the latest. | the builder; `parse_group_key`: `simulation/sampler/fence.py`; `group_for_season`: `scripts/fence_check.py` |
| `_apply_overrides` | Applies the hand-written corrections over the built lines. | `build_geometry_document` |

**Watch out:** `build_park_geometry` calls the MLB Stats API through `write_venue_dimensions`.
When the API is down, the last `venue_dimensions.json` stands. The change detector misses a
wall that moved in the window's last season. It sees the move once that season holds 10
home runs in the moved sector.

### `bundle/catcher_receiving.py`

[Source](../../bundle/catcher_receiving.py) · 181 lines · Tests: [test_receiving_ratio.py](../../tests/unit/test_receiving_ratio.py)

The catcher receiving document. The receiving ratio acts on taken pitches only. A
called-strike row takes the catcher's framing multiplier: his called-strike rate over the
league rate in the row's zone group. A ball row takes the mirror weight, so the taken
pitches keep their total weight. Blocking works the same way on the got-away rows.

| Function / class | What it does | Called from |
|---|---|---|
| `build_receiving_profiles` | Over the window's taken pitches, writes `receiving.json`. It holds the league called-strike rate per zone group and the got-away rate per blocking cell, per season and pooled. It also holds each catcher-season's framing multipliers and blocking ratio, shrunk toward the league. | `build_bundle._build` |
| `recv_zone_group` | The zone group of every pitch: 1 the heart (zone 5), 2 the in-zone edge, 3 outside (zones 11–14), 0 unknown. | `simulation/sampler/pitch_result.py` |
| `recv_block_cell` | The blocking cell of every pitch, 0 to 7: four pitch-height buckets times in the zone or not. −1 when the geometry or zone is unknown. | `simulation/sampler/pitch_result.py` |
| `RECV_FRAME_PRIOR` / `RECV_BLOCK_PRIOR` | The shrinkage: 200 league-average taken pitches per zone group; 5 expected got-aways per catcher. | `build_receiving_profiles` |

**Watch out:** the pitch-result draw reads this document only with `SIM_CATCHER_RECEIVING`
on. Production runs it off. The league rates are per season, because the called-strike rate
outside the zone moves between seasons. The blocking ratio never uses the raw got-away
rate, which measures the staff's wildness.

### `bundle/reference_tables.py`

[Source](../../bundle/reference_tables.py) · 92 lines · Tests: [test_bundle_worker_and_boot.py](../../tests/unit/bundle/test_bundle_worker_and_boot.py)

The intentional-walk rates and the venue run factors. They ride the bundle so a worker
never opens the DuckDB file, whose lock a rebuild may hold.

| Function / class | What it does | Called from |
|---|---|---|
| `build_reference_tables` | Writes `ibb_rates.json` from `sim.ibb_rates` and `venue_run_factors.json` from the regressed run factors in `derived.park_factors`. Each file is a sorted `{"rows": [...]}` document. A table the database lacks writes no file. Each file moves into place from a `tmp_` name. | `build_bundle._build` |
| `ibb_rates_from_rows` | The intentional-walk share per (runners, outs, late, close) cell. None when no row has an opportunity. | `EngineArtifacts.load`, `production_factory._load_ibb_rates` |
| `venue_run_factors_from_rows` | The `(venue_id, season)` to factor map, keeping the factors from 0.5 to 2.0. None when none is left. | `EngineArtifacts.load`, `production_factory._load_venue_run_factors` |
| `_read_reference_rows` | The `rows` of one file, or None when the file is absent. | `EngineArtifacts.load` |
| `IBB_RATES_FILE` / `VENUE_RUN_FACTORS_FILE` | The two file names. | `EngineArtifacts.load` |

---

## The pitcher score table and the embeddings

### `bundle/export_pitcher_scores.py`

[Source](../../bundle/export_pitcher_scores.py) · 91 lines · Tests: through `EngineArtifacts.load`: [test_pitcher_sim_matrix.py](../../tests/unit/bundle/test_pitcher_sim_matrix.py)

The pitcher score table the pitch draw weights by.

| Function / class | What it does | Called from |
|---|---|---|
| `build_pitcher_sim_matrix` | Builds the pitcher engine and scores each pitcher profile of the window against the same-hand profiles. Writes `pitcher_sim.npz`: the index and the scores as JSON text, and a dense float32 matrix. A pair the engine did not score holds 0.0. The full run takes about an hour. `limit` scores only the first N profiles. | `build_bundle._build` |
| `load_pitcher_sim` | Returns the index and the dense matrix, the matrix from its shared view when one exists. It never parses the large `sims` JSON. | `EngineArtifacts.load` |

**Watch out:** with `limit`, every row after the first N stays 0.0. The dictionary copy
(`sims`) stays in the file for an older loader. A worker never reads it: it costs gigabytes
per process.

### `bundle/export_actor_embeddings.py`

[Source](../../bundle/export_actor_embeddings.py) · 141 lines · Tests: [test_engine_artifacts_realism.py](../../tests/unit/bundle/test_engine_artifacts_realism.py), [test_profile_data_additions.py](../../tests/unit/profiles/test_profile_data_additions.py)

The actor embeddings: each player-season's metrics vector from the profile tables
([Player profiles](profiles.md)).

| Function / class | What it does | Called from |
|---|---|---|
| `build_actor_embeddings` | For six actors, reads `derived.<table>` and writes `<actor>_emb.npz`: the keys, the vectors (NaN as 0), each feature's mean and standard deviation, and the feature names. The features are the numeric columns, without the `sample_*` columns and the excluded ones. | `build_bundle._build` |
| `load_actor_embeddings` | Loads each actor's file. The keys become a key-to-row index. The vectors, means and deviations come from the shared views when all three exist. | `EngineArtifacts.load` |
| `_ACTOR_TABLES` / `_EMBEDDING_EXCLUDE` | The actor-to-table map (batter, catcher, fielder, baserunner, manager, pitcher_steal); the catcher columns that never enter an embedding. | `build_actor_embeddings` |

**Watch out:** a fielder key is `player_id:position:season`; every other key is
`id:season`. A fielder key without the position would merge a player's positions. The
catcher's got-away columns (passed balls, wild pitches, blocks) stay out. As a similarity
feature, they measure his staff's wildness, not his blocking.

---

## The score tables: `similarity/build_score_tables.py`

The bundle build calls this module in its last step (`--what actors_sim`). The module lives
in `similarity/` because it drives the engines, but it writes into the bundle.

### `similarity/build_score_tables.py`

[Source](../../similarity/build_score_tables.py) · 393 lines · Tests: [test_bundle_no_pickle.py](../../tests/unit/bundle/test_bundle_no_pickle.py), [test_actor_matrices.py](../../tests/unit/test_actor_matrices.py), [test_actor_powers.py](../../tests/unit/test_actor_powers.py)

The actor score tables and the concentration check. The build runs each engine's own query
for every profile of the window. It packs the scores into a dense float32 matrix. The
diagonal holds 1.0, and a pair the engine did not score holds NaN. The sampler reads a NaN
as neutral.
Production sets every table's power to 1, so a draw uses the raw score.

| Score table | Engine and score | Read by |
|---|---|---|
| `batter` | the batter engine's composite | the pitch draw, the fielding draw |
| `fielder_1B` to `fielder_RF` (seven tables) | the fielder engine's composite, one table per position | the fielding draw, the advancement draws |
| `catcher_throwing` | the catcher engine's throwing score | the pickoff and steal draws |
| `runner_steal` | the steal-runner engine's composite, thin profiles included | the pickoff and steal draws |
| `pitcher_steal` | the composite of the engine for the pitcher against the run | the pickoff and steal draws |
| `runner_adv` | the baserunner engine's composite, thin profiles included | the advancement draws |
| `manager_usage` | the manager engine's usage score | the pitching-change draw |
| `catcher` | the catcher engine's composite | no draw; the concentration check reads it |

**The concentration check.** A catcher-season's staff is the pitchers whose pitches he
caught that season. A fielder-season's staff is the pitchers whose batted balls he fielded
at that position. The check takes his row of the score table over that season's pool rows.
The weighted own-staff share is the score weight on his staff's rows over the total weight.
The unweighted share is the plain fraction of those rows. Their ratio is 1 when the table
adds no lean toward his own team. The report gives each table's ratio at its 90th
percentile (p90) over the live profiles. It also gives the effective-sample share: the
effective sample size, (Σw)² / Σw², over the row count.

| Function / class | What it does | Called from |
|---|---|---|
| `build_actor_sim_matrices` | Builds each engine once over the window; the two runner engines also score their thin profiles. Writes one `<name>.npz` per table, then `manifest.json`. Runs the concentration check and writes `concentration.json`. `limit` keeps the first N profiles of each table. `only` rebuilds the named tables, merges their sizes into the manifest and skips the check. | `build_bundle._build` |
| `_matrix_from_queries` | Scores every profile against the others with the engine's own query. | `build_actor_sim_matrices` |
| `_write_actor_sim` | Saves one table and its key index through `_savez_plain`. | `build_actor_sim_matrices` |
| `concentration_report` | The check over the tables on disk: the catcher tables over the pitch pool, the fielder tables over the batted-ball pool. Per table: the median, p90 and largest own-staff ratio, and the median effective-sample share. | `build_actor_sim_matrices`, `scripts/probes/actor_concentration_check.py` |
| `concentration_share` | One live profile's three shares: weighted own-staff, unweighted own-staff and effective-sample. An unscored pair weighs 1.0. | `concentration_report` |
| `CONCENTRATION_MAX_RATIO` | 3.0, the largest p90 own-staff ratio. Above it, a strict build raises `RuntimeError`; any other build logs a warning. | `build_actor_sim_matrices`; the help text of `--strict-concentration` |
| `_ACTOR_SIM_ENGINES` / `_ACTOR_SIM_SPECS` / `_ENGINE_LOADER` | Role to engine class; table to engine, score and key columns; the importer a test swaps for a fake engine. | `build_actor_sim_matrices` |

**Watch out:**

- The check covers only the catcher and fielder tables, whose pool rows carry a pitching
  staff. The other tables have no check. The manager table leans on the live manager's own
  rows by design.
- The nightly build runs without `--strict-concentration`. A failed check is a log
  warning; read `concentration.json` after a build.
- The tests patch `_ENGINE_LOADER` and `concentration_report` in this module. Keep both
  here.
- `_key_of` joins a list, not a generator. This guards against the stochastic segfault (the
  SIM-445 crash class, CLAUDE.md §2a). It is a precaution, not a proven fix.

---

## The loader

### `bundle/load_bundle.py`

[Source](../../bundle/load_bundle.py) · 231 lines · Tests: [test_engine_artifacts_realism.py](../../tests/unit/bundle/test_engine_artifacts_realism.py), [test_engine_artifacts_shared_memory.py](../../tests/unit/bundle/test_engine_artifacts_shared_memory.py), [test_pitcher_sim_matrix.py](../../tests/unit/bundle/test_pitcher_sim_matrix.py), [test_profile_data_additions.py](../../tests/unit/profiles/test_profile_data_additions.py), [test_accuracy_fit_instruments.py](../../tests/unit/test_accuracy_fit_instruments.py), [test_actor_matrices.py](../../tests/unit/test_actor_matrices.py), [test_actor_powers.py](../../tests/unit/test_actor_powers.py), [test_api_main_wiring.py](../../tests/unit/test_api_main_wiring.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_fence_carry_and_margin.py](../../tests/unit/test_fence_carry_and_margin.py), [test_fence_season_groups.py](../../tests/unit/test_fence_season_groups.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py), [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py), [test_filter_cells.py](../../tests/unit/test_filter_cells.py), [test_manager_draw.py](../../tests/unit/test_manager_draw.py), [test_matchup_weight_cache.py](../../tests/unit/test_matchup_weight_cache.py), [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py), [test_pitch_cell_index.py](../../tests/unit/test_pitch_cell_index.py), [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py), [test_pitcher_outs_credit.py](../../tests/unit/test_pitcher_outs_credit.py), [test_realism_consumers.py](../../tests/unit/test_realism_consumers.py), [test_receiving_kernel_consumers.py](../../tests/unit/test_receiving_kernel_consumers.py), [test_receiving_ratio.py](../../tests/unit/test_receiving_ratio.py), [test_relief_draw.py](../../tests/unit/test_relief_draw.py), [test_rng_streams.py](../../tests/unit/test_rng_streams.py), [test_running_game_on_the_pitch.py](../../tests/unit/test_running_game_on_the_pitch.py), [test_sampler_hot_path_cache.py](../../tests/unit/test_sampler_hot_path_cache.py), [test_situation_sigma_env.py](../../tests/unit/test_situation_sigma_env.py), [test_steal_draw.py](../../tests/unit/test_steal_draw.py), [test_steal_pool_data.py](../../tests/unit/test_steal_pool_data.py), [test_steal_weights_seam.py](../../tests/unit/test_steal_weights_seam.py), [test_transition_draw.py](../../tests/unit/test_transition_draw.py), [test_transition_pool.py](../../tests/unit/test_transition_pool.py)

`EngineArtifacts`: the loaded bundle a worker draws from. The app loads it once to fill the
shared memory. Every worker loads it again over the shared views.

| Function / class | What it does | Called from |
|---|---|---|
| `EngineArtifacts` | The loaded bundle: both hands' pitch and batted-ball pools; the steal, advancement and change pools; the reliever role table; the pitcher score table and its index; the actor score tables and embeddings; the park geometry; the receiving document; the reference tables. A missing part loads as empty or None, and its factor stays neutral. | `simulation/production_factory._build_full_pool_sampler`, `api/startup.py`, `synthetic_bundle.synthetic_artifacts`; `FullPoolSampler` reads it |
| `EngineArtifacts.load` | Reads one folder, one loader per artifact, through an in-memory DuckDB connection for the parquet files. With `shared_views`, each shared array comes from its view and skips the disk. | `_build_full_pool_sampler`, `api/startup.py`, `back_up_data_stores.load_bundle` (the restore drill); `scripts/behaviour_fingerprint.py` wraps it |
| `EngineArtifacts.extract_shared_arrays` | Returns every shareable numeric array, keyed `"<prefix>.<sub>.<attr>"` (for example `pool.R.geom`, `actor_sim.batter.matrix`, `pitcher_sim.matrix`). The values are the loaded buffers, not copies. | `api/startup.py`, which passes the result to `BatchRunner(shared_arrays=...)` |
| `_load_actor_sim` | Loads each score table that `actor_sim/manifest.json` names, the matrix from its shared view when one exists. | `EngineArtifacts.load` |

**Flags:** none. The caller picks the folder (`BASEBALL_ENGINE_ARTIFACT_DIR`; see the load
path above).

**Watch out:**

- `has_reference` is one flag for two files: it is True when either reference file exists.
  A bundle with only one file skips the DuckDB fallback for the other. Then either no
  intentional walk fires or the park kernel stays neutral. The builder writes both files
  together, so this is unlikely (one flag per reference file, SIM-657).
- `load` never reads the pitcher score dictionary, about 2 GB per worker. The sampler reads
  the dense matrix.
- The text columns (`HandPool.outcome_type`, `BattedBallPool.event`, `p_throws`) cannot go
  through shared memory. Every worker reads them from disk.

---

## The in-memory bundle

### `bundle/synthetic_bundle.py`

[Source](../../bundle/synthetic_bundle.py) · 645 lines · Tests: [test_band_arithmetic.py](../../tests/acceptance/test_band_arithmetic.py), [bench_simulation.py](../../tests/performance/bench_simulation.py), [test_synthetic_bundle.py](../../tests/unit/bundle/test_synthetic_bundle.py), [test_pool_cutoff.py](../../tests/unit/pools/test_pool_cutoff.py), [test_baseball_sniff.py](../../tests/unit/test_baseball_sniff.py), [test_boxscore_extended.py](../../tests/unit/test_boxscore_extended.py), [test_dropped_third_strike_credits.py](../../tests/unit/test_dropped_third_strike_credits.py), [test_fence_stage.py](../../tests/unit/test_fence_stage.py), [test_fielding_and_baserunning_steps.py](../../tests/unit/test_fielding_and_baserunning_steps.py), [test_invalid_state_detection.py](../../tests/unit/test_invalid_state_detection.py), [test_manager_decisions.py](../../tests/unit/test_manager_decisions.py), [test_manager_draw.py](../../tests/unit/test_manager_draw.py), [test_manager_model.py](../../tests/unit/test_manager_model.py), [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py), [test_outs_identity.py](../../tests/unit/test_outs_identity.py), [test_pitcher_outs_credit.py](../../tests/unit/test_pitcher_outs_credit.py), [test_play_pitcher_decisions.py](../../tests/unit/test_play_pitcher_decisions.py), [test_player_stat_lines.py](../../tests/unit/test_player_stat_lines.py), [test_relief_draw.py](../../tests/unit/test_relief_draw.py), [test_rng_streams.py](../../tests/unit/test_rng_streams.py), [test_run_ledger.py](../../tests/unit/test_run_ledger.py), [test_runner_run_credit.py](../../tests/unit/test_runner_run_credit.py), [test_running_game_on_the_pitch.py](../../tests/unit/test_running_game_on_the_pitch.py), [test_simulate_game_driver.py](../../tests/unit/test_simulate_game_driver.py), [test_simulate_game_requires_lineup.py](../../tests/unit/test_simulate_game_requires_lineup.py), [test_situational_decisions.py](../../tests/unit/test_situational_decisions.py), [test_starter_resolution.py](../../tests/unit/test_starter_resolution.py)

A small `EngineArtifacts` built from a few numpy arrays, in the production loader's shape.
The no-database tests and the batch runner's no-database factory draw from it through the
production sampler and the production loop. Only the rows are synthetic. A batted-ball row
states only the forced movement of the runners; the advancement draws decide the rest.

| Function / class | What it does | Called from |
|---|---|---|
| `synthetic_artifacts` | The general builder: both hands' pitch pools, one batted-ball pool for both hands, and optional advancement, steal and change pools. | `league_artifacts`, `fixed_play_artifacts`, the tests |
| `league_artifacts` | The league-average bundle: every count, every base-out cell, the advancement pools and the change pool. A game drawn from it plays realistic baseball. | `batch_runner.rng_driven_machine_factory`, `simulation/validation/replay_chi_squared.py`, `scripts/behaviour_fingerprint.py`, `synthetic_sampler`, the tests |
| `fixed_play_artifacts` | One event in every base-out cell with its forced movement, and no advancement pools: a deterministic play for a unit test. | the tests |
| `synthetic_sampler` | A `FullPoolSampler` over a bundle, the league bundle by default. | `replay_chi_squared.py`, the tests |
| `pitch_pool` / `battedball_pool` / `inplay_rows` / `canonical_transition` | A pitch pool with one weighted row per (count, outcome); a batted-ball pool from `PlayRow` rows; one weighted row per (base-out cell, event) over the 24 cells; an event's forced movement from a cell. | `synthetic_artifacts`, `fixed_play_artifacts`, the tests |
| `advancement_pools` / `steal_pools` / `change_pool` | Three-row opportunity pools weighted to given rates; the steal pools by pitch class (or the single pre-pitch shape), with optional pickoff rows; the change pool over every cell the draw reads. | `synthetic_artifacts`, the tests |
| `LEAGUE_PITCH_MODEL` / `LEAGUE_INPLAY_MODEL` / `DEFAULT_ADVANCEMENT_RATES` / `DEFAULT_CHANGE_RATES` | The league mixes and rates the builders use by default. | the builders; `LEAGUE_INPLAY_MODEL`: `batch_runner._inplay_model_for` |

**Watch out:**

- Do not add a test-only draw path. The suite certifies the code production runs only
  because the tests draw through the production sampler.
- The bundle has no pitcher score table, no actor score table, no embedding, no park
  geometry and no receiving document. Every actor factor stays neutral, and the fence stage
  stays off.
- Its pitch pool has no `bb_row`, so no ball is born on the pitch. The in-play path draws
  the ball from the batted-ball pool.
