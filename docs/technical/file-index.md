<!-- file-index-key: 2dd0d16fd613b872 -->
# File index

Every source and config file in the repository, in the order of the [code review path](code-review-path.md). Data files (test fixtures, script inputs, logos, fonts) and the documents are not listed.

- `scripts/docs_file_index.py` writes this page from `git ls-files`. Do not edit it by hand. Regenerate it with `python scripts/docs_file_index.py`.
- **What it is** is the first sentence of the file's own docstring or header comment. A poor description needs a better docstring, not an edit here.
- **Tested by** counts the test files that import the file, and names the most specific three.
- A component's CSS module rides on the component's row (marked *+ css*).
- `tests/unit/test_docs_file_index.py` fails when a file is added, removed or moved to another stage, or its first sentence changes. The line counts and the test lists refresh whenever the page is written.

| Stage | Files | Lines |
|---|--:|--:|
| [1. The game's vocabulary](#stage-1) | 7 | 1,876 |
| [2. One pitch: the loop](#stage-2) | 9 | 3,546 |
| [3. The draws: the sampler](#stage-3) | 13 | 4,583 |
| [4. The sampler's data: the bundle](#stage-4) | 15 | 4,772 |
| [5. The similarity scores](#stage-5) | 30 | 14,375 |
| [6. Profiles and pools: the DuckDB layer](#stage-6) | 52 | 11,558 |
| [7. Getting data in: the raw tables](#stage-7) | 56 | 12,996 |
| [8. Running many games and reading the results](#stage-8) | 18 | 9,685 |
| [9. Odds and betting](#stage-9) | 16 | 6,281 |
| [10. The API](#stage-10) | 42 | 14,155 |
| [11. Game day: the live service](#stage-11) | 18 | 6,023 |
| [12. The frontend](#stage-12) | 82 | 27,244 |
| [13. Operations: the stack, the nightly jobs, the backups](#stage-13) | 41 | 7,129 |
| [14. Measurement and maintenance tools](#stage-14) | 78 | 39,500 |
| [15. Tests and CI](#stage-15) | 315 | 126,560 |
| **All** | **792** | **290,283** |

<a id="stage-1"></a>

## 1. The game's vocabulary

The state of a game, the count machine, the constants and the flags every other file uses. The [review path](code-review-path.md#stage-1) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`simulation/game_state.py`](../../simulation/game_state.py) | 779 | The game state and the play result: the data the simulation loop reads and writes. | 72: `test_game_state_contract.py`, `test_sim_jobs.py`, `test_api_games.py` +69 |
| [`simulation/loop/values.py`](../../simulation/loop/values.py) | 290 | The loop's constants, value objects and count machine. | 21: `test_pool_chain.py`, `test_run_ledger.py`, `test_fence_stage.py` +18 |
| [`simulation/constants.py`](../../simulation/constants.py) | 159 | The simulator's shared vocabularies and defensive run values. | 3: `test_simulation_constants.py`, `test_run_resolution.py`, `test_validate_props.py` |
| [`simulation/flags.py`](../../simulation/flags.py) | 544 | The registry of the `SIM_*` environment flags. | 2: `test_flag_registry.py`, `test_script_flag_names.py` |
| [`simulation/__init__.py`](../../simulation/__init__.py) | 8 | simulation — the full-pool sampler and the pitch-by-pitch game loop. | 8: `test_cache_codec.py`, `test_replay_store.py`, `test_flag_registry.py` +5 |
| [`simulation/loop/__init__.py`](../../simulation/loop/__init__.py) | 23 | The parts of the pitch-by-pitch simulator, one module per responsibility. | — |
| [`simulation/loop/base.py`](../../simulation/loop/base.py) | 73 | The type declarations the loop's mixins share. | — |

<a id="stage-2"></a>

## 2. One pitch: the loop

The state machine that plays one pitch, one plate appearance and one game. The [review path](code-review-path.md#stage-2) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`simulation/sim_loop.py`](../../simulation/sim_loop.py) | 1,198 | The pitch-by-pitch game simulator: the state machine and the full-game driver. | 42: `test_run_ledger.py`, `test_steal_draw.py`, `bench_simulation.py` +39 |
| [`simulation/loop/terminals.py`](../../simulation/loop/terminals.py) | 297 | The plate appearances that end without a ball in play: the walk, the hit by pitch, the intentional walk, the strikeout and the dropped third strike. | — |
| [`simulation/loop/running_game.py`](../../simulation/loop/running_game.py) | 510 | The running game: the steal, the pickoff and the got-away advance. | — |
| [`simulation/loop/in_play.py`](../../simulation/loop/in_play.py) | 475 | The ball in play: the fielding draw, the drawn row's transition and the advancement draws. | — |
| [`simulation/loop/manager.py`](../../simulation/loop/manager.py) | 282 | The manager's decisions: the leverage index, the intentional walk and the pitching change. | — |
| [`simulation/loop/credit.py`](../../simulation/loop/credit.py) | 225 | The box-score credit of a completed plate appearance. | — |
| [`simulation/loop/boxscore.py`](../../simulation/loop/boxscore.py) | 174 | The per-game box score and the result of one simulated game. | 13: `test_boxscore_extended.py`, `test_cache_codec.py`, `test_inning_grid.py` +10 |
| [`simulation/run_resolution.py`](../../simulation/run_resolution.py) | 254 | simulation.run_resolution — the run value of one play from its two base-out states. | 4: `test_run_resolution.py`, `test_run_ledger.py`, `test_baseball_sniff.py` +1 |
| [`simulation/start_state.py`](../../simulation/start_state.py) | 131 | Start a simulated game at a plate appearance of the real game (the "what if"). | — |

<a id="stage-3"></a>

## 3. The draws: the sampler

Every decision is a similarity-weighted draw from a filtered pool of real plays. The draws happen here. The [review path](code-review-path.md#stage-3) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`simulation/full_pool_sampler.py`](../../simulation/full_pool_sampler.py) | 73 | The full-pool similarity sampler: every draw of the simulator. | 27: `test_steal_draw.py`, `test_fence_stage.py`, `test_relief_draw.py` +24 |
| [`simulation/sampler/__init__.py`](../../simulation/sampler/__init__.py) | 23 | The parts of the full-pool sampler, one module per draw. | — |
| [`simulation/sampler/base.py`](../../simulation/sampler/base.py) | 483 | The sampler's shared machinery. | — |
| [`simulation/sampler/kernels.py`](../../simulation/sampler/kernels.py) | 72 | The sampler's module-level helpers. | 1: `test_pitch_result_split.py` |
| [`simulation/sampler/pitch.py`](../../simulation/sampler/pitch.py) | 563 | The pitch draw. | — |
| [`simulation/sampler/pitch_result.py`](../../simulation/sampler/pitch_result.py) | 372 | The pitch-result draw, the catcher receiving ratio and the readers of the last drawn pitch. | — |
| [`simulation/sampler/fielding.py`](../../simulation/sampler/fielding.py) | 801 | The fielding (batted-ball) draw. | — |
| [`simulation/sampler/fence.py`](../../simulation/sampler/fence.py) | 369 | The fence stage, the carry and the wall-margin band. | — |
| [`simulation/sampler/advancement.py`](../../simulation/sampler/advancement.py) | 144 | The advancement draws: the five discretionary extra-base decisions. | — |
| [`simulation/sampler/steal.py`](../../simulation/sampler/steal.py) | 362 | The steal and pickoff draws over the steal opportunity pool. | — |
| [`simulation/sampler/manager.py`](../../simulation/sampler/manager.py) | 440 | The pitching-change draw and the reliever draw. | — |
| [`simulation/filter_cells.py`](../../simulation/filter_cells.py) | 193 | simulation/filter_cells.py — the hard-filter cell algebra of the pitch draw. | 3: `test_flag_registry.py`, `test_pitch_cell_index.py`, `test_fatigue_kernel_scan.py` |
| [`simulation/synthetic_bundle.py`](../../simulation/synthetic_bundle.py) | 688 | An in-memory engine-artifact bundle for every no-DB path. | 27: `test_synthetic_bundle.py`, `test_run_ledger.py`, `bench_simulation.py` +24 |

<a id="stage-4"></a>

## 4. The sampler's data: the bundle

The nightly build that exports the pools and the score matrices the sampler reads. The [review path](code-review-path.md#stage-4) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`pipeline/batch/engine_artifacts.py`](../../pipeline/batch/engine_artifacts.py) | 875 | The engine-artifact bundle: the nightly builder and the per-worker loader. | 37: `test_engine_artifacts_realism.py`, `test_engine_artifacts_shared_memory.py`, `test_steal_draw.py` +34 |
| [`pipeline/batch/artifacts/common.py`](../../pipeline/batch/artifacts/common.py) | 118 | What every artifact builder shares: the pool columns, the default paths, the season window, the game-date column and the plain-array writer. | 4: `test_filter_cells.py`, `test_pool_label_check.py`, `test_bundle_no_pickle.py` +1 |
| [`pipeline/batch/artifacts/pools.py`](../../pipeline/batch/artifacts/pools.py) | 419 | The in-memory pools the sampler draws from, and the arrays of each that the parent process shares with the workers. | 6: `test_actor_powers.py`, `test_steal_pool_data.py`, `test_uncaught_strike_three.py` +3 |
| [`pipeline/batch/artifacts/pitch_pool.py`](../../pipeline/batch/artifacts/pitch_pool.py) | 227 | The per-hand pitch pool: one geometry, situation and metadata file set per batter hand. | 2: `test_profile_data_additions.py`, `test_engine_artifacts_realism.py` |
| [`pipeline/batch/artifacts/battedball_pool.py`](../../pipeline/batch/artifacts/battedball_pool.py) | 409 | The per-hand batted-ball pool the fielding draw reads. | 7: `test_fence_stage.py`, `test_fielding_draw.py`, `test_transition_pool.py` +4 |
| [`pipeline/batch/artifacts/steal_pool.py`](../../pipeline/batch/artifacts/steal_pool.py) | 240 | The steal opportunity pools, one per target base. | 1: `test_steal_pool_data.py` |
| [`pipeline/batch/artifacts/advancement_pool.py`](../../pipeline/batch/artifacts/advancement_pool.py) | 129 | The advancement opportunity pools, one per runner decision. | 1: `test_transition_pool.py` |
| [`pipeline/batch/artifacts/manager_pool.py`](../../pipeline/batch/artifacts/manager_pool.py) | 334 | The pitching-change opportunity pool the manager draw reads, with the relievers' role sidecar. | — |
| [`pipeline/batch/artifacts/pitcher_sim.py`](../../pipeline/batch/artifacts/pitcher_sim.py) | 89 | The pitcher-by-pitcher similarity matrix the pitch draw weights by. | — |
| [`pipeline/batch/artifacts/actor_embeddings.py`](../../pipeline/batch/artifacts/actor_embeddings.py) | 146 | The actor embeddings: each player's standardized profile vector by season. | 2: `test_profile_data_additions.py`, `test_engine_artifacts_realism.py` |
| [`pipeline/batch/artifacts/park_geometry.py`](../../pipeline/batch/artifacts/park_geometry.py) | 778 | The park geometry document: every park's effective fence by spray sector and season group, and the carry model the fence stage reads. | 3: `test_park_geometry_builder.py`, `test_fence_check.py`, `test_fence_stage.py` |
| [`pipeline/batch/artifacts/receiving.py`](../../pipeline/batch/artifacts/receiving.py) | 184 | The catcher receiving document: each catcher's framing and blocking multipliers on taken pitches. | 1: `test_receiving_ratio.py` |
| [`pipeline/batch/artifacts/reference.py`](../../pipeline/batch/artifacts/reference.py) | 81 | The reference tables the workers read from the bundle: the intentional-walk rates and the venue run factors. | — |
| [`pipeline/batch/bundle_versions.py`](../../pipeline/batch/bundle_versions.py) | 207 | pipeline/batch/bundle_versions.py — the engine-artifact bundle as published versions. | 1: `test_bundle_versions.py` |
| [`pipeline/batch/pool_chain.py`](../../pipeline/batch/pool_chain.py) | 536 | The count chain of the pitch pool, and the label check against real plate appearances. | 2: `test_pool_chain.py`, `test_pool_label_check.py` |

<a id="stage-5"></a>

## 5. The similarity scores

The eleven engines that score how alike two players or two situations are, and their calibration. The [review path](code-review-path.md#stage-5) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`similarity/engines/rbf_kernel.py`](../../similarity/engines/rbf_kernel.py) | 51 | The weighted Gaussian kernel the profile engines score a sub-score with. | — |
| [`similarity/engines/pitcher_similarity.py`](../../similarity/engines/pitcher_similarity.py) | 1,544 | The pitcher similarity engine: how alike two pitcher-seasons are. | 9: `test_pitcher_similarity.py`, `test_pitcher_engine_calibration.py`, `test_pitcher_similarity_contract.py` +6 |
| [`similarity/engines/arsenal_cache.py`](../../similarity/engines/arsenal_cache.py) | 437 | The cache of pairwise arsenal distances for the pitcher engine. | 3: `test_arsenal_cache_vectorized.py`, `test_pitcher_similarity.py`, `test_pitcher_engine_calibration.py` |
| [`similarity/engines/batter_similarity.py`](../../similarity/engines/batter_similarity.py) | 1,350 | Batter-to-batter similarity: one score in [0, 1] between two batter-seasons. | 4: `test_batter_similarity.py`, `test_batter_physical_features.py`, `test_engine_build_smoke.py` +1 |
| [`similarity/engines/fielder_similarity.py`](../../similarity/engines/fielder_similarity.py) | 1,576 | The fielder engine: how alike two fielders are at one position. | 5: `test_fielder_similarity.py`, `test_fielder_oaa_jump_model.py`, `test_engine_build_smoke.py` +2 |
| [`similarity/engines/catcher_similarity.py`](../../similarity/engines/catcher_similarity.py) | 753 | The catcher engine: how alike two catcher-seasons are behind the plate. | 5: `test_catcher_engine_throwing.py`, `test_catcher_similarity_scores.py`, `test_engine_build_smoke.py` +2 |
| [`similarity/engines/baserunner_steal_similarity.py`](../../similarity/engines/baserunner_steal_similarity.py) | 831 | The steal-runner engine: how alike two runner-seasons are as base stealers. | 7: `test_baserunner_steal_engine.py`, `test_baserunner_steal_scores.py`, `test_lead_distance.py` +4 |
| [`similarity/engines/pitcher_steal_similarity.py`](../../similarity/engines/pitcher_steal_similarity.py) | 623 | The pitcher-hold engine: how alike two pitcher-seasons are at holding runners. | 4: `test_pitcher_steal_similarity.py`, `test_lead_distance.py`, `test_engine_build_smoke.py` +1 |
| [`similarity/engines/baserunner_similarity.py`](../../similarity/engines/baserunner_similarity.py) | 903 | The extra-base runner engine: how alike two runner-seasons are on the bases. | 5: `test_baserunner_extra_base_similarity.py`, `test_lead_distance.py`, `test_engine_build_smoke.py` +2 |
| [`similarity/engines/manager_similarity.py`](../../similarity/engines/manager_similarity.py) | 690 | Manager-to-manager similarity: one score in [0, 1] between two manager-seasons. | 5: `test_manager_similarity.py`, `test_manager_usage_score.py`, `test_engine_regression.py` +2 |
| [`similarity/engines/pitch_pitch_similarity.py`](../../similarity/engines/pitch_pitch_similarity.py) | 515 | The pitch-to-pitch similarity engine: the nearest real pitches to a pitch. | 3: `test_pitch_pitch_engine.py`, `test_engine_build_smoke.py`, `test_engine_calibration_regression.py` |
| [`similarity/engines/situation_similarity.py`](../../similarity/engines/situation_similarity.py) | 772 | The situation engine: the historical plate appearances nearest a game state. | 4: `test_situation_similarity.py`, `test_situation_engine_queries.py`, `test_situation_metadata_store.py` +1 |
| [`similarity/engines/batted_ball_similarity.py`](../../similarity/engines/batted_ball_similarity.py) | 520 | Batted-ball-to-batted-ball similarity: a nearest-neighbor search over FAISS. | 3: `test_batted_ball_engine.py`, `test_engine_build_smoke.py`, `test_engine_calibration_regression.py` |
| [`similarity/similarity_calibration.py`](../../similarity/similarity_calibration.py) | 127 | The population calibration of the similarity engines' constants. | 6: `test_lead_distance.py`, `test_outfield_arm_model.py`, `test_manager_usage_score.py` +3 |
| [`similarity/calibration/fitting.py`](../../similarity/calibration/fitting.py) | 375 | The population fits behind every calibrated engine constant. | 5: `test_outfield_arm_model.py`, `test_fielder_oaa_jump_model.py`, `test_calibration_reconciliation.py` +2 |
| [`similarity/calibration/report.py`](../../similarity/calibration/report.py) | 344 | The calibration report: every fitted engine constant, its text summary and its lossless JSON form. | 9: `test_calibration_report_json.py`, `test_lead_distance.py`, `test_validate_props.py` +6 |
| [`similarity/calibration/base.py`](../../similarity/calibration/base.py) | 60 | The helpers every per-engine calibrator mixin shares. | — |
| [`similarity/calibration/__init__.py`](../../similarity/calibration/__init__.py) | 13 | The calibration of the similarity engines, one module per part. | — |
| [`similarity/calibration/baserunner.py`](../../similarity/calibration/baserunner.py) | 159 | The baserunner engine's calibration (extra bases on a ball in play): its three sigmas, its prior and its reliability weights. | — |
| [`similarity/calibration/baserunner_steal.py`](../../similarity/calibration/baserunner_steal.py) | 75 | The stolen-base engine's calibration: the tendency, success and lead sigmas. | — |
| [`similarity/calibration/batter.py`](../../similarity/calibration/batter.py) | 195 | The batter engine's calibration: its five sigmas, its prior and its reliability weights. | — |
| [`similarity/calibration/catcher.py`](../../similarity/calibration/catcher.py) | 80 | The catcher engine's calibration: its four defensive sigmas. | — |
| [`similarity/calibration/fielder.py`](../../similarity/calibration/fielder.py) | 380 | The fielder engine's calibration: the infield and outfield sigmas and reliability weights. | — |
| [`similarity/calibration/manager.py`](../../similarity/calibration/manager.py) | 73 | The manager engine's calibration: the usage, aggression and platoon sigmas. | — |
| [`similarity/calibration/pitcher.py`](../../similarity/calibration/pitcher.py) | 114 | The pitcher engine's calibration: the command sigma, the prior and the arsenal anchor. | — |
| [`similarity/calibration/pitcher_steal.py`](../../similarity/calibration/pitcher_steal.py) | 74 | The pitcher steal-prevention engine's calibration: the outcome and hold sigmas. | — |
| [`similarity/similarity_diagnostics.py`](../../similarity/similarity_diagnostics.py) | 821 | The sanity checks on a built similarity engine's scores. | 1: `test_baserunner_steal_scores.py` |
| [`similarity/backtesting/__init__.py`](../../similarity/backtesting/__init__.py) | 46 | Offline validation harnesses: walk-forward backtests on in-memory data. | — |
| [`similarity/backtesting/backtester.py`](../../similarity/backtesting/backtester.py) | 540 | Score predicted outcome distributions: calibration, accuracy and a walk-forward ablation. | 1: `test_probabilistic_backtester.py` |
| [`similarity/backtesting/recency_walk_forward.py`](../../similarity/backtesting/recency_walk_forward.py) | 334 | A walk-forward check of the recency weight on a k-nearest-neighbour prediction. | 1: `test_recency_walk_forward.py` |

<a id="stage-6"></a>

## 6. Profiles and pools: the DuckDB layer

The nightly computor that turns raw pitches into player profiles and pools of real plays. The [review path](code-review-path.md#stage-6) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`pipeline/batch/player_profile_computor.py`](../../pipeline/batch/player_profile_computor.py) | 435 | The nightly profile build: the player profiles, the defensive metrics and the simulation pools. | 28: `test_player_profile_computor.py`, `test_ibb_rates.py`, `test_steal_draw.py` +25 |
| [`pipeline/batch/profiles/base.py`](../../pipeline/batch/profiles/base.py) | 305 | The state and the shared helpers every profile-computor mixin uses. | — |
| [`pipeline/batch/profiles/constants.py`](../../pipeline/batch/profiles/constants.py) | 61 | The profile build's sample floors, shrinkage priors and positional-insert column orders. | 7: `test_lead_distance.py`, `test_outfield_arm_block.py`, `test_outfield_arm_model.py` +4 |
| [`pipeline/batch/profiles/sql.py`](../../pipeline/batch/profiles/sql.py) | 378 | The SQL fragments the profile and pool builds share: the pitch-result classes, the swing and stance columns, the out labels, the barrel band, the fielding-credit mask and the fatigue window expressions. | 10: `test_hit_by_pitch.py`, `test_barrel_definition.py`, `test_draw_conditioning.py` +7 |
| [`pipeline/batch/profiles/pitcher.py`](../../pipeline/batch/profiles/pitcher.py) | 397 | The pitcher season profiles, with the arsenal mixture fit. | 1: `test_arsenal_fit_reproducible.py` |
| [`pipeline/batch/profiles/gmm.py`](../../pipeline/batch/profiles/gmm.py) | 369 | The pitcher arsenal mixture fit: one Gaussian mixture per pitcher-season, fitted in a process pool, and the writes of its results. | 3: `test_gmm_batch_pipeline.py`, `test_player_profile_computor.py`, `test_arsenal_fit_reproducible.py` |
| [`pipeline/batch/profiles/batter.py`](../../pipeline/batch/profiles/batter.py) | 504 | The batter season profiles, with the physical swing and stance block. | — |
| [`pipeline/batch/profiles/fielder.py`](../../pipeline/batch/profiles/fielder.py) | 621 | The fielder season profile: the aggregate of the per-play metrics and the outfield arm block. | — |
| [`pipeline/batch/profiles/fielder_plays.py`](../../pipeline/batch/profiles/fielder_plays.py) | 925 | The per-play fielder metrics: outfield catch probability, infield outs above average, double plays, bunt defense, first-base scooping and errors. | — |
| [`pipeline/batch/profiles/defense.py`](../../pipeline/batch/profiles/defense.py) | 248 | The defensive constants and the field-geometry helpers the fielder and catcher metrics use. | 2: `test_simulation_constants.py`, `test_player_profile_computor.py` |
| [`pipeline/batch/profiles/catcher.py`](../../pipeline/batch/profiles/catcher.py) | 851 | The catcher metrics: framing, blocking, throwing by base, the season aggregate and the uncaught-strike-three slice of the receiving profile. | — |
| [`pipeline/batch/profiles/baserunner.py`](../../pipeline/batch/profiles/baserunner.py) | 689 | The baserunner season profiles and the two steal-engine tables. | — |
| [`pipeline/batch/profiles/manager.py`](../../pipeline/batch/profiles/manager.py) | 508 | The manager season profiles and the per-game bullpen workload. | — |
| [`pipeline/batch/profiles/park.py`](../../pipeline/batch/profiles/park.py) | 260 | The park factors, regressed toward league average. | — |
| [`pipeline/batch/profiles/leverage.py`](../../pipeline/batch/profiles/leverage.py) | 221 | The leverage index and the run-expectancy matrix. | 2: `test_player_profile_computor.py`, `test_point_in_time_runner_catcher_fielder.py` |
| [`pipeline/batch/profiles/league_averages.py`](../../pipeline/batch/profiles/league_averages.py) | 311 | The league-average profile rows: one row per entity type and season, read by the similarity engines as the shrinkage target. | 4: `test_outfield_arm_block.py`, `test_league_average_profiles.py`, `test_player_profile_computor.py` +1 |
| [`pipeline/batch/profiles/pools.py`](../../pipeline/batch/profiles/pools.py) | 1,113 | The simulation pools the sampler draws from, and the tables built beside them. | — |
| [`pipeline/batch/profiles/pool_meta.py`](../../pipeline/batch/profiles/pool_meta.py) | 209 | The sim-pool build metadata: the builder version, the recency weights, the incremental-rebuild gate, the steal-pool migration guard and the index DDL helper. | 10: `test_hit_by_pitch.py`, `test_park_factors.py`, `test_steal_pool_data.py` +7 |
| [`db/schemas/02_duckdb_schema.sql`](../../db/schemas/02_duckdb_schema.sql) | 1,482 | BASEBALL SIMULATION SYSTEM — DuckDB Schema | — |
| [`db/schemas/duckdb_schema_version.txt`](../../db/schemas/duckdb_schema_version.txt) | 1 | The DuckDB schema version: the number of the newest file under db/migrations/duckdb/. | — |
| [`db/migrations/duckdb/0001_initial_schema.sql`](../../db/migrations/duckdb/0001_initial_schema.sql) | 23 | DuckDB Migration 0001: initial_schema | — |
| [`db/migrations/duckdb/0002_catcher_attempt_rate_against.sql`](../../db/migrations/duckdb/0002_catcher_attempt_rate_against.sql) | 32 | DuckDB Migration 0002: catcher_attempt_rate_against | — |
| [`db/migrations/duckdb/0003_sim051_pull_relative_spray_angle.sql`](../../db/migrations/duckdb/0003_sim051_pull_relative_spray_angle.sql) | 49 | SUPERSEDED IN PART BY DuckDB MIGRATION 0014 (SIM-440), 2026-07-27. | — |
| [`db/migrations/duckdb/0004_sim076_recency_weight.sql`](../../db/migrations/duckdb/0004_sim076_recency_weight.sql) | 40 | DuckDB Migration 0004: recency_weight + pool_build_metadata | — |
| [`db/migrations/duckdb/0005_sim115_prune_pool_indexes.sql`](../../db/migrations/duckdb/0005_sim115_prune_pool_indexes.sql) | 42 | DuckDB Migration 0005: prune sim-pool indexes (SIM-115) | — |
| [`db/migrations/duckdb/0006_sim337_reconcile_pool_indexes.sql`](../../db/migrations/duckdb/0006_sim337_reconcile_pool_indexes.sql) | 94 | SUPERSEDED IN PART BY DuckDB MIGRATION 0014 (SIM-440), 2026-07-27. | — |
| [`db/migrations/duckdb/0007_sim336_sim345_park_factors_data_layer.sql`](../../db/migrations/duckdb/0007_sim336_sim345_park_factors_data_layer.sql) | 74 | SUPERSEDED IN PART BY DuckDB MIGRATION 0014 (SIM-440), 2026-07-27. | — |
| [`db/migrations/duckdb/0008_sim356_play_stream.sql`](../../db/migrations/duckdb/0008_sim356_play_stream.sql) | 79 | DuckDB Migration 0008: pitch-level play-stream / snapshot store (SIM-356) | — |
| [`db/migrations/duckdb/0009_sim357_state_snapshots.sql`](../../db/migrations/duckdb/0009_sim357_state_snapshots.sql) | 62 | DuckDB Migration 0009: pitch-level field/state snapshot store (SIM-357) | — |
| [`db/migrations/duckdb/0010_sim362_364_game_card.sql`](../../db/migrations/duckdb/0010_sim362_364_game_card.sql) | 63 | DuckDB Migration 0010: per-game card store (linescore + decisions) (SIM-362/364) | — |
| [`db/migrations/duckdb/0011_sim408_engine_schema_reconciliation.sql`](../../db/migrations/duckdb/0011_sim408_engine_schema_reconciliation.sql) | 135 | DuckDB Migration 0011: SIM-408 engine ↔ DuckDB schema reconciliation | — |
| [`db/migrations/duckdb/0012_sim411_413_425b_battedball_realism_cols.sql`](../../db/migrations/duckdb/0012_sim411_413_425b_battedball_realism_cols.sql) | 51 | DuckDB Migration 0012: SIM-411/413/425b batted-ball realism columns | — |
| [`db/migrations/duckdb/0013_sim427_manager_available_reliever_usage.sql`](../../db/migrations/duckdb/0013_sim427_manager_available_reliever_usage.sql) | 29 | DuckDB Migration 0013: SIM-427 manager USAGE capstone — available_reliever_usage_rate | — |
| [`db/migrations/duckdb/0014_sim440_bat_side_semantics.sql`](../../db/migrations/duckdb/0014_sim440_bat_side_semantics.sql) | 60 | DuckDB Migration 0014: correct the pull_relative_spray_angle column comment | — |
| [`db/migrations/duckdb/0015_sim468_steal_opportunity_pool.sql`](../../db/migrations/duckdb/0015_sim468_steal_opportunity_pool.sql) | 68 | DuckDB Migration 0015: SIM-468 — sim.steal_opportunity_pool | — |
| [`db/migrations/duckdb/0016_sim504_pitcher_disengagement_rates.sql`](../../db/migrations/duckdb/0016_sim504_pitcher_disengagement_rates.sql) | 22 | 0016 — SIM-504 item 3: pitcher disengagement rates (schema v15 -&gt; v16) | — |
| [`db/migrations/duckdb/0017_sim507_pickoff_channel.sql`](../../db/migrations/duckdb/0017_sim507_pickoff_channel.sql) | 33 | 0017 — SIM-507: the pickoff channel (schema v16 -&gt; v17) | — |
| [`db/migrations/duckdb/0018_sim510_transition_columns.sql`](../../db/migrations/duckdb/0018_sim510_transition_columns.sql) | 109 | 0018 — SIM-510: transition columns on sim.outcome_pool + the advancement opportunity pool (schema v17 -&gt; v18) | — |
| [`db/migrations/duckdb/0019_sim491_bat_home.sql`](../../db/migrations/duckdb/0019_sim491_bat_home.sql) | 28 | 0019 — SIM-491 (the SIM-412 rebuild): the batting side on sim.outcome_pool (schema v18 -&gt; v19) | — |
| [`db/migrations/duckdb/0020_sim515_ibb_rates.sql`](../../db/migrations/duckdb/0020_sim515_ibb_rates.sql) | 43 | 0020 — SIM-515: the measured IBB rate table (schema v19 -&gt; v20) | — |
| [`db/migrations/duckdb/0021_sim517_uncaught_k3.sql`](../../db/migrations/duckdb/0021_sim517_uncaught_k3.sql) | 29 | 0021 — SIM-517 part A: the uncaught-strike-3 receiving columns (schema v20 -&gt; v21) | — |
| [`db/migrations/duckdb/0022_sim517_pitch_pool_catcher.sql`](../../db/migrations/duckdb/0022_sim517_pitch_pool_catcher.sql) | 28 | 0022 — SIM-517 part B: the catcher + got-away columns on the pitch pool (schema v21 -&gt; v22) | — |
| [`db/migrations/duckdb/0023_sim518_pitch_pool_conditioning.sql`](../../db/migrations/duckdb/0023_sim518_pitch_pool_conditioning.sql) | 38 | 0023 — SIM-518: the draw-conditioning columns on the pitch pool (schema v22 -&gt; v23) | — |
| [`db/migrations/duckdb/0024_sim523_part_g_data_adds.sql`](../../db/migrations/duckdb/0024_sim523_part_g_data_adds.sql) | 43 | 0024 — SIM-523 part G: the data additions the play-picker redesign asked for (schema v23 -&gt; v24) | — |
| [`db/migrations/duckdb/0025_sim529_batter_physical_features.sql`](../../db/migrations/duckdb/0025_sim529_batter_physical_features.sql) | 80 | 0025 — SIM-529: the physical swing and stance columns on the batter profile (schema v24 -&gt; v25) | — |
| [`db/migrations/duckdb/0026_sim534_profile_asof.sql`](../../db/migrations/duckdb/0026_sim534_profile_asof.sql) | 27 | 0026 — SIM-534: the cutoff stamp on the batter profile (schema v25 -&gt; v26) | — |
| [`db/migrations/duckdb/0027_sim537_pitcher_manager_asof.sql`](../../db/migrations/duckdb/0027_sim537_pitcher_manager_asof.sql) | 21 | 0027 — SIM-537: the cutoff stamp on pitcher and manager profiles (schema v26 -&gt; v27) | — |
| [`db/migrations/duckdb/0028_sim537_baserunner_catcher_fielder_asof.sql`](../../db/migrations/duckdb/0028_sim537_baserunner_catcher_fielder_asof.sql) | 47 | 0028 — SIM-537: the cutoff stamp on the last five profile tables (schema v27 -&gt; v28) | — |
| [`db/migrations/duckdb/0029_sim531_lead_distance.sql`](../../db/migrations/duckdb/0029_sim531_lead_distance.sql) | 67 | 0029 — SIM-531: lead distance in the steal and baserunning profiles (schema v28 -&gt; v29) | — |
| [`db/migrations/duckdb/0030_sim532_fielder_oaa_jump.sql`](../../db/migrations/duckdb/0030_sim532_fielder_oaa_jump.sql) | 52 | 0030 — SIM-532: Savant's outs above average and the outfield jump on the fielder profile (schema v29 -&gt; v30) | — |
| [`db/migrations/duckdb/0031_sim554_steal_pool_pitch_class.sql`](../../db/migrations/duckdb/0031_sim554_steal_pool_pitch_class.sql) | 44 | 0031 — SIM-554: the pitch class and the pickoff rows on the steal opportunity pool (schema v30 -&gt; v31) | — |
| [`db/migrations/duckdb/0032_sim561_play_stream_context.sql`](../../db/migrations/duckdb/0032_sim561_play_stream_context.sql) | 58 | 0032 — SIM-561: who, when and the score on each replayed pitch (schema v31 -&gt; v32) | — |

<a id="stage-7"></a>

## 7. Getting data in: the raw tables

The loaders that pull MLB, Statcast and Savant data into the Postgres raw tables. The [review path](code-review-path.md#stage-7) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`db/schemas/01_postgres_schema.sql`](../../db/schemas/01_postgres_schema.sql) | 1,352 | BASEBALL SIMULATION SYSTEM — PostgreSQL Schema | — |
| [`pipeline/statcast_events.py`](../../pipeline/statcast_events.py) | 493 | The Statcast `events` out-label vocabulary. | 6: `test_out_label.py`, `test_steal_labels.py`, `test_transition_pool.py` +3 |
| [`pipeline/etl/etl_historical_loader.py`](../../pipeline/etl/etl_historical_loader.py) | 1,825 | The historical loader: MLB Stats API games into Postgres `raw.*`. | 11: `test_etl_historical_loader.py`, `test_etl_flow.py`, `test_play_events.py` +8 |
| [`pipeline/etl/historical/base.py`](../../pipeline/etl/historical/base.py) | 84 | The state every part of the historical loader shares. | 1: `test_etl_connection_pool.py` |
| [`pipeline/etl/historical/schedule.py`](../../pipeline/etl/historical/schedule.py) | 51 | The schedule helpers: the date windows of a range load, the final-game filter and the circuit-breaker limit. | 3: `test_etl_schedule_final_filter.py`, `test_etl_hardening.py`, `test_etl_reload_game.py` |
| [`pipeline/etl/historical/game_values.py`](../../pipeline/etl/historical/game_values.py) | 168 | The game, lineup and player values the loader reads from the feed: the manager, the height, the wind, the game status and the starting lineup rows. | 4: `test_etl_hardening.py`, `test_starting_position.py`, `test_etl_starting_lineups.py` +1 |
| [`pipeline/etl/historical/rows.py`](../../pipeline/etl/historical/rows.py) | 429 | The pitch row: the required fields, the feed-to-column map, the row builder and the two-tier validation. | 4: `test_etl_hardening.py`, `test_etl_reload_game.py`, `test_release_speed_bounds.py` +1 |
| [`pipeline/etl/historical/reference_rows.py`](../../pipeline/etl/historical/reference_rows.py) | 420 | The reference rows a game needs before its pitches: the teams, the managers, the game row and the starting lineups. | — |
| [`pipeline/etl/historical/venues.py`](../../pipeline/etl/historical/venues.py) | 95 | The venue of a game, read from the feed, with the overrides for the neutral-site games that ship without one. | 1: `test_venue_resolution.py` |
| [`pipeline/etl/historical/savant.py`](../../pipeline/etl/historical/savant.py) | 50 | The parser for the JSON that Baseball Savant embeds in its HTML pages. | 1: `test_etl_hardening.py` |
| [`pipeline/etl/historical/writes.py`](../../pipeline/etl/historical/writes.py) | 555 | The loader's writes: the pitch rows, the play events, the error ledger, the ingest outcome and the freshness and quality reports. | 2: `test_etl_reload_game.py`, `test_etl_historical_loader.py` |
| [`pipeline/etl/play_events.py`](../../pipeline/etl/play_events.py) | 403 | Extract the non-pitch play events `raw.pitches` cannot represent. | 1: `test_play_events.py` |
| [`pipeline/etl/boxscore_ingest.py`](../../pipeline/etl/boxscore_ingest.py) | 710 | The official per-player box score as the prop ground truth (SIM-545, part of the unpriced prop markets, SIM-421). | 5: `test_boxscore_ingest.py`, `test_boxscore_reader.py`, `test_starting_position.py` +2 |
| [`pipeline/etl/coercion.py`](../../pipeline/etl/coercion.py) | 62 | MLB Baseball Simulation Platform | 1: `test_etl_historical_loader.py` |
| [`pipeline/etl/savant_loader.py`](../../pipeline/etl/savant_loader.py) | 448 | One loader for every Baseball Savant leaderboard in `savant_boards.BOARDS`. | 4: `test_savant_loader.py`, `test_lead_distance.py`, `test_point_in_time_batter.py` +1 |
| [`pipeline/etl/savant_boards.py`](../../pipeline/etl/savant_boards.py) | 555 | The declarative registry of Baseball Savant leaderboards this platform ingests. | 6: `test_lead_distance.py`, `test_savant_loader.py`, `test_arm_angle_decision.py` +3 |
| [`pipeline/etl/savant_pitch_tracking_loader.py`](../../pipeline/etl/savant_pitch_tracking_loader.py) | 283 | The per-PITCH tracking measurements, loaded one day at a time. | 1: `test_point_in_time_batter.py` |
| [`pipeline/etl/etl_sprint_speed_loader.py`](../../pipeline/etl/etl_sprint_speed_loader.py) | 297 | MLB Baseball Simulation Platform | — |
| [`pipeline/etl/mlb_venue_dimensions.py`](../../pipeline/etl/mlb_venue_dimensions.py) | 156 | The MLB Stats API's PUBLISHED fence distances, the floor under a new park's fence line (the fence certification plan, SIM-478, `docs/audit/2026-09-20-sim478-480-fence-certification-plan.md`, §4 and §5.1). | 2: `test_fence_stage.py`, `test_park_geometry_builder.py` |
| [`pipeline/etl/venue_backfill_job.py`](../../pipeline/etl/venue_backfill_job.py) | 349 | Backfill NULL venue_id rows on raw.games. | 1: `test_venue_id_fallback.py` |
| [`pipeline/bullpen_usage.py`](../../pipeline/bullpen_usage.py) | 303 | The pen's facts from the official box score and the MLB API's per-game bullpen listing, for the pitching-change draw and the reliever draw. | 1: `test_bullpen_usage.py` |
| [`pipeline/db_dsn.py`](../../pipeline/db_dsn.py) | 65 | The one reader of the Postgres connection string for scripts and jobs. | 1: `test_db_dsn.py` |
| [`db/introspect.py`](../../db/introspect.py) | 119 | Ask a database whether a table or a column exists. | 2: `test_db_introspect.py`, `test_outfield_arm_block.py` |
| [`alembic.ini`](../../alembic.ini) | 150 | A generic, single database configuration. | — |
| [`db/migrations/env.py`](../../db/migrations/env.py) | 114 | Alembic environment configuration. | — |
| [`db/migrations/script.py.mako`](../../db/migrations/script.py.mako) | 28 | The template Alembic copies for a new migration file. | — |
| [`db/migrations/versions/0001_initial_schema.py`](../../db/migrations/versions/0001_initial_schema.py) | 552 | initial_schema | — |
| [`db/migrations/versions/0002_sim082_lineup_state_live_game_unique_index.py`](../../db/migrations/versions/0002_sim082_lineup_state_live_game_unique_index.py) | 41 | sim082_lineup_state_live_game_unique_index | — |
| [`db/migrations/versions/0003_sim083_etl_freshness_and_game_odds_to_schema.py`](../../db/migrations/versions/0003_sim083_etl_freshness_and_game_odds_to_schema.py) | 147 | sim083_etl_freshness_and_game_odds_to_schema | — |
| [`db/migrations/versions/0004_sim134_prop_odds_prop_stat_column_and_index.py`](../../db/migrations/versions/0004_sim134_prop_odds_prop_stat_column_and_index.py) | 118 | prop_odds: rename prop_type-&gt;prop_stat, add CHECK constraint, compound index | — |
| [`db/migrations/versions/0005_sim085_pitches_situation_index.py`](../../db/migrations/versions/0005_sim085_pitches_situation_index.py) | 47 | sim085_pitches_situation_index | — |
| [`db/migrations/versions/0006_sim086_games_venue_id_nullable.py`](../../db/migrations/versions/0006_sim086_games_venue_id_nullable.py) | 49 | sim086_games_venue_id_nullable | — |
| [`db/migrations/versions/0007_sim087_release_speed_threshold.py`](../../db/migrations/versions/0007_sim087_release_speed_threshold.py) | 81 | sim087_release_speed_threshold | — |
| [`db/migrations/versions/0008_sim088_drop_pitches_pitch_type_index.py`](../../db/migrations/versions/0008_sim088_drop_pitches_pitch_type_index.py) | 41 | sim088_drop_pitches_pitch_type_index | — |
| [`db/migrations/versions/0009_sim089_pitches_pitcher_season_clean_index.py`](../../db/migrations/versions/0009_sim089_pitches_pitcher_season_clean_index.py) | 52 | sim089_pitches_pitcher_season_clean_index | — |
| [`db/migrations/versions/0010_sim092_game_odds_dedup.py`](../../db/migrations/versions/0010_sim092_game_odds_dedup.py) | 66 | sim092_game_odds_dedup | — |
| [`db/migrations/versions/0011_sim093_etl_errors_table.py`](../../db/migrations/versions/0011_sim093_etl_errors_table.py) | 82 | sim093_etl_errors_table | — |
| [`db/migrations/versions/0012_sim157_game_odds_full_unique.py`](../../db/migrations/versions/0012_sim157_game_odds_full_unique.py) | 83 | sim157_game_odds_full_unique | — |
| [`db/migrations/versions/0013_sim340_prop_odds_dedup.py`](../../db/migrations/versions/0013_sim340_prop_odds_dedup.py) | 74 | sim340_prop_odds_dedup | — |
| [`db/migrations/versions/0014_sim356_sim_run_history.py`](../../db/migrations/versions/0014_sim356_sim_run_history.py) | 86 | sim356_sim_run_history | — |
| [`db/migrations/versions/0015_sim433_game_bullpen_availability.py`](../../db/migrations/versions/0015_sim433_game_bullpen_availability.py) | 120 | sim433_game_bullpen_availability | — |
| [`db/migrations/versions/0016_sim440_pitch_quality_velocity_band.py`](../../db/migrations/versions/0016_sim440_pitch_quality_velocity_band.py) | 110 | sim440_pitch_quality_velocity_band | — |
| [`db/migrations/versions/0017_sim441_etl_hardening.py`](../../db/migrations/versions/0017_sim441_etl_hardening.py) | 174 | sim441_etl_hardening | — |
| [`db/migrations/versions/0018_sim502_play_events.py`](../../db/migrations/versions/0018_sim502_play_events.py) | 169 | sim502_play_events | — |
| [`db/migrations/versions/0019_sim528_savant_raw_tables.py`](../../db/migrations/versions/0019_sim528_savant_raw_tables.py) | 218 | sim528_savant_raw_tables | — |
| [`db/migrations/versions/0020_sim534_pitch_tracking_and_asof.py`](../../db/migrations/versions/0020_sim534_pitch_tracking_and_asof.py) | 120 | sim534_pitch_tracking_and_asof | — |
| [`db/migrations/versions/0021_sim534_swing_board_asof.py`](../../db/migrations/versions/0021_sim534_swing_board_asof.py) | 60 | sim534_swing_board_asof | — |
| [`db/migrations/versions/0022_sim421_prop_odds_market_vocabulary.py`](../../db/migrations/versions/0022_sim421_prop_odds_market_vocabulary.py) | 104 | sim421_prop_odds_market_vocabulary | — |
| [`db/migrations/versions/0023_sim545_game_player_stats.py`](../../db/migrations/versions/0023_sim545_game_player_stats.py) | 127 | sim545_game_player_stats | — |
| [`db/migrations/versions/0024_sim421_game_odds_all_markets.py`](../../db/migrations/versions/0024_sim421_game_odds_all_markets.py) | 114 | sim421_game_odds_all_markets | — |
| [`db/migrations/versions/0025_sim427_game_bullpen.py`](../../db/migrations/versions/0025_sim427_game_bullpen.py) | 61 | sim427_game_bullpen | — |
| [`db/migrations/versions/0026_sim531_savant_running_game.py`](../../db/migrations/versions/0026_sim531_savant_running_game.py) | 114 | sim531_savant_running_game | — |
| [`db/migrations/versions/0027_sim532_savant_oaa_and_jump.py`](../../db/migrations/versions/0027_sim532_savant_oaa_and_jump.py) | 112 | sim532_savant_oaa_and_jump | — |
| [`db/migrations/versions/0028_sim555_odds_book_line_stamp.py`](../../db/migrations/versions/0028_sim555_odds_book_line_stamp.py) | 84 | sim555_odds_book_line_stamp | — |
| [`db/migrations/versions/0029_sim519_live_slate.py`](../../db/migrations/versions/0029_sim519_live_slate.py) | 160 | sim519_live_slate | — |
| [`db/migrations/versions/0030_whatif_runs.py`](../../db/migrations/versions/0030_whatif_runs.py) | 66 | whatif_runs | — |

<a id="stage-8"></a>

## 8. Running many games and reading the results

The worker pool and the code that turns 100 games into win odds, prop distributions and market prices. The [review path](code-review-path.md#stage-8) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`simulation/sim_kwargs.py`](../../simulation/sim_kwargs.py) | 1,183 | The ONE builder that turns a resolved GameState into `simulate_game` kwargs, plus the venue park-factor resolver it depends on. | 15: `test_sim_kwargs.py`, `test_sim_jobs.py`, `test_fence_stage.py` +12 |
| [`simulation/lineup_resolver.py`](../../simulation/lineup_resolver.py) | 1,021 | simulation.lineup_resolver — the lineup and substitution resolver. | 9: `test_lineup_resolver.py`, `test_api_games.py`, `test_api_betting.py` +6 |
| [`simulation/production_factory.py`](../../simulation/production_factory.py) | 633 | The production machine factory for the batch runner. | 18: `test_ibb_rates.py`, `test_fence_stage.py`, `test_pool_cutoff.py` +15 |
| [`simulation/batch_runner.py`](../../simulation/batch_runner.py) | 1,261 | The parallel Monte-Carlo batch runner. | 31: `test_batch_runner.py`, `test_batch_runner_stress.py`, `test_batch_runner_hit_rate.py` +28 |
| [`simulation/results.py`](../../simulation/results.py) | 374 | The aggregate of N simulated games of one matchup. | 19: `test_clv_engine.py`, `test_api_schemas.py`, `test_cache_codec.py` +16 |
| [`simulation/linescore.py`](../../simulation/linescore.py) | 258 | The per-inning linescore and each team's runs / hits / errors (R/H/E). | 5: `test_linescore.py`, `test_inning_grid.py`, `test_api_game_card.py` +2 |
| [`simulation/pitcher_decisions.py`](../../simulation/pitcher_decisions.py) | 333 | simulation.pitcher_decisions — the winning, losing and save pitchers of one game. | 4: `test_pitcher_decisions.py`, `test_play_pitcher_decisions.py`, `test_api_game_card.py` +1 |
| [`simulation/prop_distributions.py`](../../simulation/prop_distributions.py) | 530 | The **prop-distribution aggregator**: one probability distribution per player and prop over the N games of a Monte-Carlo run. | 19: `test_prop_distributions.py`, `test_sim_jobs.py`, `test_api_games.py` +16 |
| [`simulation/game_market_distributions.py`](../../simulation/game_market_distributions.py) | 395 | The simulator prices EVERY game market the book posts, not only the full-game moneyline, run line and total (the owner's ruling of 2026-09-12, SIM-421). | 5: `test_inning_grid.py`, `test_odds_readers.py`, `test_segment_edges.py` +2 |
| [`simulation/win_probability.py`](../../simulation/win_probability.py) | 394 | The **calibrated game win probability**. | 10: `test_win_probability.py`, `test_clv_engine.py`, `test_api_betting.py` +7 |
| [`simulation/whatif.py`](../../simulation/whatif.py) | 179 | Managerial changes on a start state (the game page's "what if"). | 1: `test_game_page_whatif.py` |
| [`simulation/play_recorder.py`](../../simulation/play_recorder.py) | 260 | Capture the ordered PlayResult stream of ONE game. | 10: `test_play_recorder.py`, `test_cache_codec.py`, `test_inning_grid.py` +7 |
| [`simulation/snapshots.py`](../../simulation/snapshots.py) | 524 | The data shapes of the field graphic, the play-by-play and the override. | 8: `test_play_snapshots.py`, `test_sim_store.py`, `test_api_schemas.py` +5 |
| [`simulation/cache_codec.py`](../../simulation/cache_codec.py) | 72 | The JSON codec of the Redis sim cache. | 1: `test_cache_codec.py` |
| [`simulation/prop_validation.py`](../../simulation/prop_validation.py) | 818 | Validate the simulator's PROBABILITY outputs against real outcomes, and FIT the win-probability reliability curve. | 3: `test_validate_props.py`, `test_boxscore_reader.py`, `test_accuracy_comparison_props.py` |
| [`simulation/validation/__init__.py`](../../simulation/validation/__init__.py) | 36 | The end-to-end check of the simulator's run distribution. | — |
| [`simulation/validation/replay_chi_squared.py`](../../simulation/validation/replay_chi_squared.py) | 489 | The historical-replay harness: a chi-squared goodness-of-fit (GOF) test of the simulator's runs per team-game. | 1: `test_replay_chi_squared.py` |
| [`db/sim_store.py`](../../db/sim_store.py) | 925 | The durable store of the sim results and the pitch snapshots (SIM-356). | 5: `test_sim_store.py`, `test_replay_store.py`, `test_api_game_card.py` +2 |

<a id="stage-9"></a>

## 9. Odds and betting

Where the bookmakers' prices come from, and how the model's numbers become edges. The [review path](code-review-path.md#stage-9) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`pipeline/odds_provider.py`](../../pipeline/odds_provider.py) | 797 | The odds provider seam and the odds vocabulary. | 24: `test_odds_provider_pin.py`, `test_odds_provider_seam.py`, `test_odds_provider_retry.py` +21 |
| [`pipeline/bettingpros_odds_provider.py`](../../pipeline/bettingpros_odds_provider.py) | 445 | The real odds provider: an :class:`~pipeline.odds_provider.OddsProvider` backed by the BettingPros v3 API (https://api.bettingpros.com/v3). | 10: `test_odds_matcher.py`, `test_odds_writers.py`, `test_odds_vocabulary.py` +7 |
| [`pipeline/bettingpros/base.py`](../../pipeline/bettingpros/base.py) | 39 | The state every part of the BettingPros provider shares. | — |
| [`pipeline/bettingpros/events.py`](../../pipeline/bettingpros/events.py) | 612 | The event match: an MLB game resolved to its BettingPros event, with the caches of found and failed lookups, and the offer reads of a matched event. | — |
| [`pipeline/bettingpros/markets.py`](../../pipeline/bettingpros/markets.py) | 60 | The BettingPros market ids of every game market and every prop market. | 2: `test_odds_game_markets.py`, `test_odds_vocabulary.py` |
| [`pipeline/bettingpros/parsing.py`](../../pipeline/bettingpros/parsing.py) | 203 | The parsing of a BettingPros offer: the sides a market needs, one book's line, the opener, the vendor's stamp and the player-name key. | 4: `test_odds_game_markets.py`, `test_bettingpros_provider.py`, `test_historical_odds_load.py` +1 |
| [`pipeline/bettingpros/rows.py`](../../pipeline/bettingpros/rows.py) | 591 | The odds rows built from an event's offers: one row per book and market, the first-inning twin rule and the first-five exclusions. | — |
| [`pipeline/bettingpros/rules.py`](../../pipeline/bettingpros/rules.py) | 79 | The vendor-data rules: which first-five entries are mislabelled first-inning bets, and how close a twin's prices must be. | 2: `test_odds_census_sql.py`, `test_odds_one_book_per_row.py` |
| [`pipeline/bettingpros/schedule.py`](../../pipeline/bettingpros/schedule.py) | 155 | The MLB schedule facts the event match reads: the slates a game was played on, its double-header number and its postponed start. | 1: `test_odds_matcher.py` |
| [`pipeline/bettingpros/transport.py`](../../pipeline/bettingpros/transport.py) | 168 | The HTTP reads of the BettingPros and MLB APIs, with the retry policy. | — |
| [`pipeline/odds_row_guard.py`](../../pipeline/odds_row_guard.py) | 460 | The load guard: refuse an odds row that cannot be one bet (one row per book, SIM-555). | 6: `test_odds_matcher.py`, `test_odds_writers.py`, `test_odds_census_sql.py` +3 |
| [`pipeline/etl/opening_line_job.py`](../../pipeline/etl/opening_line_job.py) | 652 | Nightly Opening Line Ingestion Job MLB Baseball Simulation Platform | 3: `test_etl_flow.py`, `test_odds_writers.py`, `test_odds_vocabulary.py` |
| [`betting/clv_engine.py`](../../betting/clv_engine.py) | 881 | The edge engine: the simulator's probabilities against the market's prices. | 14: `test_clv_engine.py`, `test_clv_engine_run_line.py`, `test_bet_signal.py` +11 |
| [`betting/bet_signal.py`](../../betting/bet_signal.py) | 268 | Bet signals: the gate, the stake and the rank over a list of edge reports. | 1: `test_bet_signal.py` |
| [`betting/line_movement.py`](../../betting/line_movement.py) | 803 | The line-movement series: one side's price at one book, from the opening to the close. | 3: `test_line_movement.py`, `test_live_run_lines.py`, `test_odds_live_pages.py` |
| [`betting/__init__.py`](../../betting/__init__.py) | 68 | The betting layer: it compares the simulator's probabilities with the market's prices. | 1: `test_odds_live_pages.py` |

<a id="stage-10"></a>

## 10. The API

The FastAPI app: its boot, the login, the routes the frontend calls and the run jobs. The [review path](code-review-path.md#stage-10) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`api/main.py`](../../api/main.py) | 487 | The FastAPI app of the MLB Baseball Simulation Platform: the app factory and its lifespan. | 6: `test_api_main_wiring.py`, `test_sim_kwargs.py`, `test_api_auth_session.py` +3 |
| [`api/startup.py`](../../api/startup.py) | 457 | The startup and shutdown steps of the app's lifespan (`api/main.py`) that no test patches. | — |
| [`api/env_check.py`](../../api/env_check.py) | 68 | The environment check the app runs first at startup. | 1: `test_route_auth_coverage.py` |
| [`api/middleware.py`](../../api/middleware.py) | 49 | The app's own response middleware. | — |
| [`api/state.py`](../../api/state.py) | 471 | The builders of the resources the lifespan (`api/main.py`) attaches to `app.state`: | 6: `test_api_state.py`, `test_sql_console_pool.py`, `test_similarity_endpoint.py` +3 |
| [`api/auth.py`](../../api/auth.py) | 655 | The API's security primitives: the login check, the rate limit, the latency window and the CORS allowlist. | 12: `test_api_auth.py`, `test_api_auth_session.py`, `test_route_auth_coverage.py` +9 |
| [`api/errors.py`](../../api/errors.py) | 59 | The app's exception handling: a structured error envelope for a 500. | 1: `test_api_error_envelope.py` |
| [`api/serialization.py`](../../api/serialization.py) | 140 | The array-safe JSON helper of the API's response models. | 6: `test_api_schemas.py`, `test_cache_codec.py`, `test_inning_grid.py` +3 |
| [`api/sim_jobs.py`](../../api/sim_jobs.py) | 365 | One simulation run per game: the job registry. | 2: `test_sim_jobs.py`, `test_game_page_whatif.py` |
| [`api/bundle_watch.py`](../../api/bundle_watch.py) | 153 | The app picks up a nightly rebuild by restarting itself. | 3: `test_api_main_wiring.py`, `test_bundle_versions.py`, `test_bundle_worker_and_boot.py` |
| [`api/routes/__init__.py`](../../api/routes/__init__.py) | 12 | The API's routers. | 8: `test_segment_edges.py`, `test_backup_age_gauge.py`, `test_game_type_filter.py` +5 |
| [`api/routes/_common.py`](../../api/routes/_common.py) | 112 | The small helpers that several route modules share. | 2: `test_api_data_health.py`, `test_read_only_routes.py` |
| [`api/routes/games.py`](../../api/routes/games.py) | 1,677 | The game routes (prefix `/api/games`) and the simulation seam they share. | 22: `test_api_games.py`, `test_api_games_plays.py`, `test_sim_jobs.py` +19 |
| [`api/routes/games_slate.py`](../../api/routes/games_slate.py) | 650 | The day slate and the status card of one game. | 2: `test_api_games.py`, `test_slate_endpoint.py` |
| [`api/routes/games_feed.py`](../../api/routes/games_feed.py) | 397 | The real game: the league's per-game feed and the live service's stored state. | 2: `test_api_games.py`, `test_game_feed.py` |
| [`api/routes/games_runs.py`](../../api/routes/games_runs.py) | 172 | The reads of a game's simulation runs (the durable `sim.sim_runs` rows). | — |
| [`api/routes/games_models.py`](../../api/routes/games_models.py) | 592 | The request and response models of the game routes (`/api/games`). | 4: `test_api_games.py`, `test_game_feed.py`, `test_inning_grid.py` +1 |
| [`api/routes/betting.py`](../../api/routes/betting.py) | 610 | The betting routes (prefix `/api/betting`). | 8: `test_api_betting.py`, `test_segment_edges.py`, `test_api_end_to_end.py` +5 |
| [`api/routes/betting_pricing.py`](../../api/routes/betting_pricing.py) | 891 | The edge math of /edges and /signals: the prices of each market and the edge report of each side. | 2: `test_segment_edges.py`, `test_odds_live_pages.py` |
| [`api/routes/betting_stored_odds.py`](../../api/routes/betting_stored_odds.py) | 467 | The stored price source of the betting routes: every book's stored line for a game. | 3: `test_segment_edges.py`, `test_odds_last_seen.py`, `test_odds_live_pages.py` |
| [`api/routes/betting_lines.py`](../../api/routes/betting_lines.py) | 172 | The line-movement and CLV reads of a game's stored odds. | — |
| [`api/routes/betting_card.py`](../../api/routes/betting_card.py) | 177 | The book's pregame lines for a slate card. | 1: `test_slate_card_odds.py` |
| [`api/routes/auth.py`](../../api/routes/auth.py) | 181 | The browser session's login, status and logout. | 1: `test_route_auth_coverage.py` |
| [`api/routes/metrics.py`](../../api/routes/metrics.py) | 633 | The Prometheus metrics endpoint. | 3: `test_metrics_endpoint.py`, `test_backup_age_gauge.py`, `test_route_auth_coverage.py` |
| [`api/schemas/__init__.py`](../../api/schemas/__init__.py) | 96 | The API's response models: the wire contract of the simulation and betting outputs. | 6: `test_sim_jobs.py`, `test_api_schemas.py`, `test_inning_grid.py` +3 |
| [`api/schemas/_base.py`](../../api/schemas/_base.py) | 20 | The base of every response model. | — |
| [`api/schemas/game_card.py`](../../api/schemas/game_card.py) | 181 | The game card of a stored game: the linescore (`simulation.linescore`) and the pitcher decisions (`simulation.pitcher_decisions`). | — |
| [`api/schemas/live.py`](../../api/schemas/live.py) | 57 | The live service's stored game state (`GET /api/games/{game_pk}/live`). | — |
| [`api/schemas/markets.py`](../../api/schemas/markets.py) | 313 | The models of `betting`: CLV (closing-line value), the edge report, the bet signal and the line movement. | — |
| [`api/schemas/props.py`](../../api/schemas/props.py) | 180 | The per-player prop models: the boxscore card (each player's prop means over a run) and the prop-edge response (one player's prop distribution and edge). | — |
| [`api/schemas/snapshots.py`](../../api/schemas/snapshots.py) | 267 | The models of `simulation.snapshots`: a player reference, the field at a pitch, the play-by-play and the baseline-versus-override delta. | — |
| [`api/schemas/summary.py`](../../api/schemas/summary.py) | 171 | The models of `simulation.results`: the confidence interval and the game summary. | — |
| [`api/routes/similarity.py`](../../api/routes/similarity.py) | 490 | The pitcher similarity explorer. | 2: `test_similarity_endpoint.py`, `test_route_auth_coverage.py` |
| [`api/routes/similarity_explorer.py`](../../api/routes/similarity_explorer.py) | 726 | The similarity explorer across all eleven engines. | 2: `test_similarity_explorer_adapters.py`, `test_route_auth_coverage.py` |
| [`api/routes/ai_assistant.py`](../../api/routes/ai_assistant.py) | 367 | The Data Lab's AI assistant (optional; on only with ANTHROPIC_API_KEY). | 3: `test_sql_console_pool.py`, `test_route_auth_coverage.py`, `test_pitch_result_vocabulary.py` |
| [`api/routes/analytics.py`](../../api/routes/analytics.py) | 469 | The Data Lab's summary analytics over raw.pitches. | 4: `test_game_type_filter.py`, `test_read_only_routes.py`, `test_route_auth_coverage.py` +1 |
| [`api/routes/data_health.py`](../../api/routes/data_health.py) | 118 | The data-freshness route of the UI. | 3: `test_api_data_health.py`, `test_read_only_routes.py`, `test_route_auth_coverage.py` |
| [`api/routes/players.py`](../../api/routes/players.py) | 155 | The player name typeahead and a player's identity, for the Data Lab and the similarity UI. | 2: `test_read_only_routes.py`, `test_route_auth_coverage.py` |
| [`api/routes/schema_introspect.py`](../../api/routes/schema_introspect.py) | 95 | The read-only schema tree for the Data Lab's schema browser and the AI assistant. | 2: `test_read_only_routes.py`, `test_route_auth_coverage.py` |
| [`api/routes/sql_runner.py`](../../api/routes/sql_runner.py) | 104 | The Data Lab's SQL console: read-only ad-hoc SQL. | 3: `test_read_only_routes.py`, `test_sql_console_pool.py`, `test_route_auth_coverage.py` |
| [`api/routes/sql_safety.py`](../../api/routes/sql_safety.py) | 699 | The one read-only SQL path of the Data Lab. | 4: `test_sql_safety.py`, `test_api_main_wiring.py`, `test_read_only_routes.py` +1 |
| [`api/__init__.py`](../../api/__init__.py) | 0 | Package marker. | 3: `test_trusted_proxy.py`, `test_bundle_versions.py`, `test_bundle_worker_and_boot.py` |

<a id="stage-11"></a>

## 11. Game day: the live service

The service that follows today's games, writes the posted lineups and pushes updates. The [review path](code-review-path.md#stage-11) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`pipeline/mlb_schedule.py`](../../pipeline/mlb_schedule.py) | 327 | The league schedule client (the live slate, SIM-519). | 1: `test_schedule_client.py` |
| [`pipeline/mlb_game_feed.py`](../../pipeline/mlb_game_feed.py) | 681 | The league's per-game live feed → the slate card's detail (the live slate, SIM-519). | 2: `test_game_feed.py`, `test_game_page_whatif.py` |
| [`api/league_feed.py`](../../api/league_feed.py) | 65 | The app's reader of the league's public endpoints. | — |
| [`pipeline/live/run_live.py`](../../pipeline/live/run_live.py) | 71 | The live service's entry point (the live slate, SIM-519; decision D2). | — |
| [`pipeline/live/live_ingestion_pipeline.py`](../../pipeline/live/live_ingestion_pipeline.py) | 1,147 | The live ingestion pipeline: the day's games, their live state and their odds. | 23: `test_live_bridge.py`, `test_odds_writers.py`, `test_live_pipeline.py` +20 |
| [`pipeline/live/ingestion/base.py`](../../pipeline/live/ingestion/base.py) | 137 | The state every part of the live pipeline shares. | — |
| [`pipeline/live/ingestion/constants.py`](../../pipeline/live/ingestion/constants.py) | 74 | The live pipeline's settings: the MLB endpoints, the poll and reconnect intervals, the cache lifetimes, the re-simulation cooldown and the odds cadences. | 4: `test_odds_writers.py`, `test_live_pipeline.py`, `test_odds_vocabulary.py` +1 |
| [`pipeline/live/ingestion/mlb_websocket.py`](../../pipeline/live/ingestion/mlb_websocket.py) | 166 | The WebSocket parts: the client of MLB's live game socket and the manager of the app's own subscribers. | 2: `test_odds_writers.py`, `test_live_pipeline.py` |
| [`pipeline/live/ingestion/game_state_builder.py`](../../pipeline/live/ingestion/game_state_builder.py) | 499 | The game-state builder: one live game's feed turned into the state the simulator starts from. | 2: `test_live_pipeline.py`, `test_live_pipeline_bugs.py` |
| [`pipeline/live/ingestion/closing_rows.py`](../../pipeline/live/ingestion/closing_rows.py) | 296 | The closing rows: at first pitch, each book's last pre-game offer is stamped as the closing line. | 1: `test_odds_closing_rows.py` |
| [`pipeline/live/ingestion/game_odds.py`](../../pipeline/live/ingestion/game_odds.py) | 212 | The game odds of the live cycle: the fetch, the load guard, the writes and the closing-line marks. | — |
| [`pipeline/live/ingestion/mock_odds.py`](../../pipeline/live/ingestion/mock_odds.py) | 343 | The mock odds source: deterministic lines for tests and for a stack with no odds vendor key. | 10: `test_odds_writers.py`, `test_live_pipeline.py`, `test_segment_edges.py` +7 |
| [`pipeline/live/ingestion/odds_routes.py`](../../pipeline/live/ingestion/odds_routes.py) | 52 | The odds routes the API mounts: one game's odds and today's slate of odds. | — |
| [`pipeline/live/ingestion/odds_rows.py`](../../pipeline/live/ingestion/odds_rows.py) | 376 | The odds rows the live cycle writes: the JSON form of an offer, the staleness test, the insert statements and their arguments. | 2: `test_odds_writers.py`, `test_odds_last_seen.py` |
| [`pipeline/live/ingestion/prop_odds.py`](../../pipeline/live/ingestion/prop_odds.py) | 437 | The player-prop odds of the live cycle: the players each market needs, the fetch, the writes and the closing-line marks. | 1: `test_odds_vocabulary.py` |
| [`pipeline/live/lineup_writer.py`](../../pipeline/live/lineup_writer.py) | 363 | The published-lineup writer (the live slate, SIM-519). | 1: `test_published_lineup_writer.py` |
| [`pipeline/live/broadcast.py`](../../pipeline/live/broadcast.py) | 160 | The bridge between the live service and the browsers (the live slate, SIM-519). | 1: `test_live_bridge.py` |
| [`pipeline/live/bullpen_availability_ingest.py`](../../pipeline/live/bullpen_availability_ingest.py) | 617 | Per-game bullpen availability ingestion. | 1: `test_bullpen_availability.py` |

<a id="stage-12"></a>

## 12. The frontend

The React app: the day slate, the game page, the Data Lab and the similarity explorer. The [review path](code-review-path.md#stage-12) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`frontend/index.html`](../../frontend/index.html) | 14 | The HTML page that loads the React app. | — |
| [`frontend/src/main.tsx`](../../frontend/src/main.tsx) | 23 | The frontend's entry point: it loads the bundled fonts and the styles, then mounts the React app. | — |
| [`frontend/src/App.tsx`](../../frontend/src/App.tsx) | 126 | AppShell — reads the auth state from the AuthProvider. | — |
| [`frontend/src/contexts/AuthContext.tsx`](../../frontend/src/contexts/AuthContext.tsx) | 126 | AuthContext.tsx — the auth state (authenticated, loading) and the auth actions (login, logout) for the whole component tree. | — |
| [`frontend/src/api/http.ts`](../../frontend/src/api/http.ts) | 75 | http.ts — the JSON request helpers every API client uses, and the error they throw. | — |
| [`frontend/src/api/typed.ts`](../../frontend/src/api/typed.ts) | 37 | typed.ts — the typed API surface, read from the generated OpenAPI schema. | — |
| [`frontend/src/api/assistant.ts`](../../frontend/src/api/assistant.ts) | 96 | assistant.ts — the streaming client (server-sent events) for the AI assistant (api/routes/ai_assistant.py). | — |
| [`frontend/src/api/auth.ts`](../../frontend/src/api/auth.ts) | 50 | auth.ts — the browser's auth API client. | — |
| [`frontend/src/api/betting.ts`](../../frontend/src/api/betting.ts) | 139 | betting.ts — the client of the betting routes (/api/betting): the edges, the signals and the slate card's lines. | — |
| [`frontend/src/api/games.ts`](../../frontend/src/api/games.ts) | 693 | games.ts — the browser's client of the game routes (/api/games). | — |
| [`frontend/src/api/lab.ts`](../../frontend/src/api/lab.ts) | 171 | lab.ts — the browser's client for the Data Lab: the summary analytics, the data freshness, the schema tree, the read-only SQL runner and the AI-assistant capability probe. | — |
| [`frontend/src/api/players.ts`](../../frontend/src/api/players.ts) | 25 | players.ts — the name typeahead and the player identity client (api/routes/players.py). | — |
| [`frontend/src/api/schema.d.ts`](../../frontend/src/api/schema.d.ts) | 5,379 | The TypeScript types of every API route and response (generated from openapi.json by `npm run gen:api`). | — |
| [`frontend/src/api/similarity.ts`](../../frontend/src/api/similarity.ts) | 153 | similarity.ts — the client for the Similarity Explorer (api/routes/similarity_explorer.py). | — |
| [`frontend/src/api/unauthorized.ts`](../../frontend/src/api/unauthorized.ts) | 22 | unauthorized.ts — the one signal that a data request came back 401 (no valid credential). | — |
| [`frontend/src/pages/DaySummaryPage.tsx`](../../frontend/src/pages/DaySummaryPage.tsx) *+ css* | 196 | DaySummaryPage.tsx — the Daily Diamond day slate. | — |
| [`frontend/src/components/slate/CardDetail.tsx`](../../frontend/src/components/slate/CardDetail.tsx) *+ css* | 514 | CardDetail.tsx — the open Daily Diamond card. | — |
| [`frontend/src/components/slate/CardField.tsx`](../../frontend/src/components/slate/CardField.tsx) *+ css* | 91 | CardField.tsx — the design's "On the field" diagram. | — |
| [`frontend/src/components/slate/DatePicker.tsx`](../../frontend/src/components/slate/DatePicker.tsx) *+ css* | 126 | DatePicker.tsx — the design's date control: ‹ and › step a day, the date button opens a month calendar, and "Today" shows when the slate is on another day. | — |
| [`frontend/src/components/slate/TeamBlock.tsx`](../../frontend/src/components/slate/TeamBlock.tsx) *+ css* | 43 | TeamBlock.tsx — one club on a Daily Diamond card: the logo, the colour chip with the abbreviation, the name and the record. | — |
| [`frontend/src/components/slate/cardText.ts`](../../frontend/src/components/slate/cardText.ts) | 54 | cardText.ts — the words on a Daily Diamond card's header: the state tag, the line under the score and our sim line. | — |
| [`frontend/src/components/slate/format.ts`](../../frontend/src/components/slate/format.ts) | 108 | format.ts — the Daily Diamond text helpers. | — |
| [`frontend/src/components/slate/slate.ts`](../../frontend/src/components/slate/slate.ts) | 25 | slate.ts — the slate's order and refresh interval. | — |
| [`frontend/src/pages/GamePage.tsx`](../../frontend/src/pages/GamePage.tsx) *+ css* | 444 | GamePage.tsx — the game page at /game/:gamePk. | — |
| [`frontend/src/components/games/ActualPlayByPlay.tsx`](../../frontend/src/components/games/ActualPlayByPlay.tsx) *+ css* | 133 | ActualPlayByPlay.tsx — the real game's play-by-play (the game page). | — |
| [`frontend/src/components/games/BettingCard.tsx`](../../frontend/src/components/games/BettingCard.tsx) *+ css* | 298 | BettingCard.tsx — the game page's betting card. | — |
| [`frontend/src/components/games/BoxscorePanel.tsx`](../../frontend/src/components/games/BoxscorePanel.tsx) *+ css* | 345 | BoxscorePanel.tsx — the per-player projections of the game page. | — |
| [`frontend/src/components/games/GameCard.tsx`](../../frontend/src/components/games/GameCard.tsx) *+ css* | 105 | GameCard.tsx — the Daily Diamond slate card. | — |
| [`frontend/src/components/games/PlayByPlayList.tsx`](../../frontend/src/components/games/PlayByPlayList.tsx) *+ css* | 208 | PlayByPlayList.tsx — the pitch-by-pitch scroll of a simulated game, grouped into plate appearances (PAs). | — |
| [`frontend/src/components/games/PropDistributionChart.tsx`](../../frontend/src/components/games/PropDistributionChart.tsx) *+ css* | 114 | PropDistributionChart.tsx — a compact bar chart of a prop's PMF (its probability of each outcome). | — |
| [`frontend/src/components/games/SimulationCard.tsx`](../../frontend/src/components/games/SimulationCard.tsx) *+ css* | 208 | SimulationCard.tsx — one simulation run per game. | — |
| [`frontend/src/components/games/WhatIfPanel.tsx`](../../frontend/src/components/games/WhatIfPanel.tsx) *+ css* | 527 | WhatIfPanel.tsx — go back to a plate appearance of the real game, make managerial changes, and simulate the rest of the game from there. | — |
| [`frontend/src/components/graphics/LinescoreGraphic.tsx`](../../frontend/src/components/graphics/LinescoreGraphic.tsx) *+ css* | 98 | LinescoreGraphic.tsx — the classic baseball linescore: a per-inning run grid and each team's Runs / Hits / Errors totals. | — |
| [`frontend/src/components/graphics/index.ts`](../../frontend/src/components/graphics/index.ts) | 3 | The graphics components' barrel export. | — |
| [`frontend/src/hooks/useAsync.ts`](../../frontend/src/hooks/useAsync.ts) | 40 | useAsync.ts — a small data-fetch hook. | — |
| [`frontend/src/hooks/useGameSocket.ts`](../../frontend/src/hooks/useGameSocket.ts) | 181 | useGameSocket.ts — the hook for the per-game live WebSocket. | — |
| [`frontend/src/pages/AssistantPage.tsx`](../../frontend/src/pages/AssistantPage.tsx) | 205 | AssistantPage.tsx — the Data Lab's AI assistant chat. | — |
| [`frontend/src/pages/ConsolePage.tsx`](../../frontend/src/pages/ConsolePage.tsx) | 271 | ConsolePage.tsx — the Data Lab's read-only SQL console. | — |
| [`frontend/src/pages/DataLabLayout.tsx`](../../frontend/src/pages/DataLabLayout.tsx) | 39 | DataLabLayout.tsx — the Data Lab shell: a persistent segmented tab control (Summary · SQL Console · AI Assistant) over a nested &lt;Outlet/&gt;. | — |
| [`frontend/src/pages/EngineExplorerPage.tsx`](../../frontend/src/pages/EngineExplorerPage.tsx) | 302 | EngineExplorerPage.tsx — one similarity engine's explorer. | — |
| [`frontend/src/pages/EngineIndexPage.tsx`](../../frontend/src/pages/EngineIndexPage.tsx) | 86 | EngineIndexPage.tsx — the Similarity Explorer's engine chooser. | — |
| [`frontend/src/pages/PlayerDetailPage.tsx`](../../frontend/src/pages/PlayerDetailPage.tsx) | 91 | PlayerDetailPage.tsx — the cross-engine hub for one player: the identity, the seasons on file, and a card per relevant similarity engine. | — |
| [`frontend/src/pages/SituationFinderPage.tsx`](../../frontend/src/pages/SituationFinderPage.tsx) | 155 | SituationFinderPage.tsx — the honest home for the KDTree situation engine (a nearest-neighbour search over game states): it compares GAME STATES, not players, and returns a DISTANCE (lower = more similar), not a 0–1 s... | — |
| [`frontend/src/pages/SummaryPage.tsx`](../../frontend/src/pages/SummaryPage.tsx) | 291 | SummaryPage.tsx — the descriptive dashboard over raw.pitches. | — |
| [`frontend/src/pages/assistant.module.css`](../../frontend/src/pages/assistant.module.css) | 198 | assistant.module.css — the Data Lab's AI assistant page. | — |
| [`frontend/src/pages/console.module.css`](../../frontend/src/pages/console.module.css) | 245 | console.module.css — the Data Lab's SQL Console page. | — |
| [`frontend/src/pages/dataLab.module.css`](../../frontend/src/pages/dataLab.module.css) | 140 | dataLab.module.css — the shared styles of the Data Lab pages. | — |
| [`frontend/src/pages/similarity.module.css`](../../frontend/src/pages/similarity.module.css) | 213 | similarity.module.css — the shared styles of the Similarity Explorer pages. | — |
| [`frontend/src/components/auth/LoginPage.tsx`](../../frontend/src/components/auth/LoginPage.tsx) *+ css* | 91 | LoginPage.tsx — the full-page login screen. | — |
| [`frontend/src/components/charts/charts.module.css`](../../frontend/src/components/charts/charts.module.css) | 204 | charts.module.css — the hand-rolled chart system. | — |
| [`frontend/src/components/charts/index.tsx`](../../frontend/src/components/charts/index.tsx) | 321 | charts/index.tsx — one hand-rolled chart system for the Data Lab and the | — |
| [`frontend/src/components/lab/ResultGrid.tsx`](../../frontend/src/components/lab/ResultGrid.tsx) *+ css* | 114 | ResultGrid.tsx — a dense, sortable result table. | — |
| [`frontend/src/components/lab/StatTile.tsx`](../../frontend/src/components/lab/StatTile.tsx) *+ css* | 39 | StatTile.tsx — a tile with a label, a big monospace number and an optional context line. | — |
| [`frontend/src/components/lab/csv.ts`](../../frontend/src/components/lab/csv.ts) | 20 | csv.ts — the client-side CSV export of a result set. | — |
| [`frontend/src/components/lab/starterQueries.ts`](../../frontend/src/components/lab/starterQueries.ts) | 153 | starterQueries.ts — the curated, ready-to-run example queries that seed the | — |
| [`frontend/src/components/lab/states.tsx`](../../frontend/src/components/lab/states.tsx) *+ css* | 82 | states.tsx — the shared loading / empty / error / engine-warming / not-configured states of the Data Lab and the Similarity Explorer. | — |
| [`frontend/src/components/similarity/CompTable.tsx`](../../frontend/src/components/similarity/CompTable.tsx) *+ css* | 82 | CompTable.tsx — the ranked comps grid: the rank, the player (→ /player/:id), the season, a composite bar, a mini-bar per sub-score and the sample size that drives the confidence discount. | — |
| [`frontend/src/components/similarity/ComponentScoreBars.tsx`](../../frontend/src/components/similarity/ComponentScoreBars.tsx) *+ css* | 77 | ComponentScoreBars.tsx — the core breakdown: one weighted bar per component sub-score (the fill is the raw 0..1 value; a faint tick marks the sub-score's weight), a composite header and an honest footnote. | — |
| [`frontend/src/components/similarity/PlayerSearchInput.tsx`](../../frontend/src/components/similarity/PlayerSearchInput.tsx) *+ css* | 145 | PlayerSearchInput.tsx — the debounced name type-ahead over /api/players/search (a pg_trgm search of raw.players). | — |
| [`frontend/src/components/ui/Badge.tsx`](../../frontend/src/components/ui/Badge.tsx) *+ css* | 61 | Badge — the design system's compact status indicator. | — |
| [`frontend/src/components/ui/Card.tsx`](../../frontend/src/components/ui/Card.tsx) *+ css* | 71 | Card — the design system's surface container, with an optional title and a header slot. | — |
| [`frontend/src/components/ui/Drawer.tsx`](../../frontend/src/components/ui/Drawer.tsx) *+ css* | 63 | Drawer — the design system's slide-over panel from the right edge (a full-width bottom sheet on mobile). | — |
| [`frontend/src/components/ui/Panel.tsx`](../../frontend/src/components/ui/Panel.tsx) *+ css* | 106 | Panel — the design system's content section. | — |
| [`frontend/src/components/ui/index.ts`](../../frontend/src/components/ui/index.ts) | 18 | The design system's primitives — the barrel export. | — |
| [`frontend/src/styles/global.css`](../../frontend/src/styles/global.css) | 284 | global.css — the base reset, the app shell and a few utility classes. | — |
| [`frontend/src/styles/tokens.css`](../../frontend/src/styles/tokens.css) | 208 | tokens.css — the design system's tokens. | — |
| [`frontend/src/teams.ts`](../../frontend/src/teams.ts) | 59 | teams.ts — the 30 MLB clubs by MLB team id. | — |
| [`frontend/src/vite-env.d.ts`](../../frontend/src/vite-env.d.ts) | 6 | / &lt;reference types="vite/client" /&gt; | — |
| [`frontend/e2e/gamepage.spec.ts`](../../frontend/e2e/gamepage.spec.ts) | 313 | gamepage.spec.ts — the game page's real game, what-if panel and collapsible panels. | — |
| [`frontend/e2e/simulation.spec.ts`](../../frontend/e2e/simulation.spec.ts) | 152 | simulation.spec.ts — one simulation run per game. | — |
| [`frontend/e2e/slate.spec.ts`](../../frontend/e2e/slate.spec.ts) | 339 | slate.spec.ts — the Daily Diamond slate. | — |
| [`frontend/e2e/smoke.spec.ts`](../../frontend/e2e/smoke.spec.ts) | 327 | smoke.spec.ts — the frontend's cross-browser smoke tests. | — |
| [`frontend/.eslintrc.cjs`](../../frontend/.eslintrc.cjs) | 26 | The ESLint rules for the frontend source. | — |
| [`frontend/Dockerfile`](../../frontend/Dockerfile) | 40 | Frontend image — SIM-401 | — |
| [`frontend/Dockerfile.dockerignore`](../../frontend/Dockerfile.dockerignore) | 23 | The files the frontend image build leaves out. | — |
| [`frontend/openapi.json`](../../frontend/openapi.json) | 10,394 | The API contract snapshot (generated by scripts/export_openapi.py; the frontend's types come from it). | — |
| [`frontend/package.json`](../../frontend/package.json) | 40 | The frontend's npm manifest: its dependencies and its scripts (dev, build, lint, e2e, gen:api). | — |
| [`frontend/playwright.config.ts`](../../frontend/playwright.config.ts) | 38 | Playwright config — SIM-400 | — |
| [`frontend/scripts/fetch-logos.mjs`](../../frontend/scripts/fetch-logos.mjs) | 23 | fetch-logos.mjs — SIM-519 Part T. | — |
| [`frontend/tsconfig.json`](../../frontend/tsconfig.json) | 31 | The TypeScript compiler settings for the frontend source. | — |
| [`frontend/tsconfig.node.json`](../../frontend/tsconfig.node.json) | 11 | The TypeScript settings for the Vite config file. | — |
| [`frontend/vite.config.ts`](../../frontend/vite.config.ts) | 57 | The Vite build and dev-server settings, including the proxy to the API. | — |

<a id="stage-13"></a>

## 13. Operations: the stack, the nightly jobs, the backups

How the services run, what runs every night, and how the data is backed up. The [review path](code-review-path.md#stage-13) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`docker-compose.yml`](../../docker-compose.yml) | 790 | MLB Baseball Simulation Platform — Docker Compose | — |
| [`Dockerfile`](../../Dockerfile) | 181 | MLB Baseball Simulation Platform — Dockerfile | — |
| [`Makefile`](../../Makefile) | 321 | MLB Baseball Simulation Platform — Makefile | — |
| [`.env.example`](../../.env.example) | 272 | MLB Baseball Simulation Platform — Environment Variables | — |
| [`.env.staging.example`](../../.env.staging.example) | 137 | MLB Baseball Simulation Platform — STAGING environment template | — |
| [`.env.production.example`](../../.env.production.example) | 128 | MLB Baseball Simulation Platform — PRODUCTION environment template | — |
| [`deploy/nginx/nginx.conf`](../../deploy/nginx/nginx.conf) | 293 | MLB Baseball Simulation Platform — nginx reverse proxy | — |
| [`deploy/ofelia/config.ini`](../../deploy/ofelia/config.ini) | 167 | Ofelia scheduler config — the nightly and weekly data jobs (docker-native cron). | — |
| [`scripts/with_mark.sh`](../../scripts/with_mark.sh) | 45 | scripts/with_mark.sh — mark an ingest job as running, run it, record its exit. | — |
| [`scripts/with_retry.sh`](../../scripts/with_retry.sh) | 57 | scripts/with_retry.sh — run a command again after a crash. | — |
| [`scripts/wait_for_ingest.sh`](../../scripts/wait_for_ingest.sh) | 62 | scripts/wait_for_ingest.sh — the nightly rebuild's first step: make sure the finals load has run today before the rebuild reads Postgres. | — |
| [`scripts/nightly_finals.sh`](../../scripts/nightly_finals.sh) | 51 | scripts/nightly_finals.sh — load the last few days' final games. | — |
| [`scripts/nightly_ingest.sh`](../../scripts/nightly_ingest.sh) | 75 | scripts/nightly_ingest.sh — nightly data ingestion chain. | — |
| [`scripts/nightly_closing_lines.sh`](../../scripts/nightly_closing_lines.sh) | 69 | scripts/nightly_closing_lines.sh — the nightly closing pass. | — |
| [`scripts/nightly_rebuild.sh`](../../scripts/nightly_rebuild.sh) | 35 | scripts/nightly_rebuild.sh — rebuild the profiles and the engine bundle for the current season (steps 2 and 3 of scripts/nightly_ingest.sh). | — |
| [`scripts/weekly_refresh.sh`](../../scripts/weekly_refresh.sh) | 29 | scripts/weekly_refresh.sh — sweep the current season for any final game the nightly window missed. | — |
| [`pipeline/backup/__init__.py`](../../pipeline/backup/__init__.py) | 8 | pipeline/backup — the backups of the data stores and their off-site copy. | 3: `test_backup_status.py`, `test_backup_offsite.py`, `test_backup_data_stores.py` |
| [`pipeline/backup/common.py`](../../pipeline/backup/common.py) | 91 | pipeline/backup/common.py — the helpers the backup modules share. | 1: `test_backup_offsite.py` |
| [`pipeline/backup/data_stores.py`](../../pipeline/backup/data_stores.py) | 915 | pipeline/backup/data_stores.py — back up, verify and restore the DuckDB analytics file and the engine-artifact bundle. | 2: `test_backup_data_stores.py`, `test_backup_offsite.py` |
| [`pipeline/backup/offsite.py`](../../pipeline/backup/offsite.py) | 991 | pipeline/backup/offsite.py — the off-site copy of the backup sets. | 1: `test_backup_offsite.py` |
| [`pipeline/backup/status.py`](../../pipeline/backup/status.py) | 121 | pipeline/backup/status.py — which backup sets exist, and how old they are. | 4: `test_backup_status.py`, `test_backup_offsite.py`, `test_backup_age_gauge.py` +1 |
| [`db/backup/pg_backup.sh`](../../db/backup/pg_backup.sh) | 305 | SIM-525: the nightly Postgres backup (the scheduled backups of the data stores). | — |
| [`db/backup/pg_restore.sh`](../../db/backup/pg_restore.sh) | 147 | SIM-525: restore one Postgres backup set into an EMPTY database. | — |
| [`scripts/backup_drill.py`](../../scripts/backup_drill.py) | 535 | The restore drill for the scheduled backups. | 1: `test_backup_drill.py` |
| [`db/roles/setup_roles.sh`](../../db/roles/setup_roles.sh) | 79 | SIM-443b: create the two least-privilege Postgres roles and grant them. | — |
| [`db/roles/setup_roles.sql`](../../db/roles/setup_roles.sql) | 72 | SIM-443b: the two least-privilege roles and their grants (idempotent). | — |
| [`scripts/gen_secrets.py`](../../scripts/gen_secrets.py) | 208 | Generate the stack's secrets. | 1: `test_gen_secrets.py` |
| [`deploy/monitoring/grafana/baseball_sim_dashboard.json`](../../deploy/monitoring/grafana/baseball_sim_dashboard.json) | 162 | The Grafana dashboard of the app's metrics. | — |
| [`deploy/monitoring/grafana/dashboards.yml`](../../deploy/monitoring/grafana/dashboards.yml) | 24 | Grafana dashboard provider — MLB Baseball Simulation Platform (SIM-374) | — |
| [`deploy/monitoring/grafana/datasource.yml`](../../deploy/monitoring/grafana/datasource.yml) | 22 | Grafana datasource provisioning — MLB Baseball Simulation Platform (SIM-374) | — |
| [`deploy/monitoring/prometheus.yml`](../../deploy/monitoring/prometheus.yml) | 64 | Prometheus scrape configuration — MLB Baseball Simulation Platform (SIM-374) | — |
| [`deploy/monitoring/secrets/app_api_key.example`](../../deploy/monitoring/secrets/app_api_key.example) | 1 | An example of the API key file Prometheus reads to scrape the app. | — |
| [`pyproject.toml`](../../pyproject.toml) | 127 | MLB Baseball Simulation Platform — pyproject.toml | — |
| [`requirements.txt`](../../requirements.txt) | 111 | The Python packages the app image installs. | — |
| [`requirements-dev.txt`](../../requirements-dev.txt) | 48 | The extra Python packages for tests, lint and type checks. | — |
| [`requirements-docs.txt`](../../requirements-docs.txt) | 5 | The packages that build the technical reference website (MkDocs). | — |
| [`mkdocs.yml`](../../mkdocs.yml) | 68 | The MkDocs settings that build the technical reference website from docs/technical/. | — |
| [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) | 24 | pre-commit configuration | — |
| [`.gitignore`](../../.gitignore) | 179 | MLB Baseball Simulation Platform — .gitignore | — |
| [`.gitattributes`](../../.gitattributes) | 24 | Normalize all text files to LF in the git object store. | — |
| [`.dockerignore`](../../.dockerignore) | 86 | .dockerignore — files excluded from Docker build context | — |

<a id="stage-14"></a>

## 14. Measurement and maintenance tools

The scripts that measure the simulator, rebuild the pools and backfill data. The [review path](code-review-path.md#stage-14) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`scripts/sim_stats.py`](../../scripts/sim_stats.py) | 666 | scripts/sim_stats.py — the scaled Monte-Carlo box-score harness. | 2: `test_fence_counters.py`, `test_smoke_outs_gate.py` |
| [`scripts/balanced_game_set.py`](../../scripts/balanced_game_set.py) | 383 | scripts/balanced_game_set.py — the BALANCED certifying game set (owner ruling 2026-09-09). | — |
| [`scripts/accuracy_comparison.py`](../../scripts/accuracy_comparison.py) | 601 | The sim-vs-closing-line accuracy comparison: the platform's validation of the simulator against the market. | 4: `test_accuracy_comparison.py`, `test_accuracy_comparison_props.py`, `test_odds_readers.py` +1 |
| [`scripts/accuracy/__init__.py`](../../scripts/accuracy/__init__.py) | 21 | The stages of the sim-vs-closing-line accuracy comparison. | 4: `test_sim_kwargs.py`, `test_odds_readers.py`, `test_accuracy_comparison.py` +1 |
| [`scripts/accuracy/confidence.py`](../../scripts/accuracy/confidence.py) | 249 | How far a row of a report can be trusted: the cluster bootstrap interval and the stated minimum sample size. | 3: `test_accuracy_comparison.py`, `test_accuracy_minimum_sample.py`, `test_accuracy_hypothetical_return.py` |
| [`scripts/accuracy/grading.py`](../../scripts/accuracy/grading.py) | 779 | What really happened, and one accuracy record per priced market. | 5: `test_odds_readers.py`, `test_segment_markets.py`, `test_rescore_runlines.py` +2 |
| [`scripts/accuracy/hypothetical_return.py`](../../scripts/accuracy/hypothetical_return.py) | 370 | The hypothetical dollar return: an opt-in companion table (`--report-hypothetical-return`) that shows a probability disagreement in money. | 1: `test_accuracy_hypothetical_return.py` |
| [`scripts/accuracy/pricing.py`](../../scripts/accuracy/pricing.py) | 573 | The closing prices: the graded book's row of each market, the best price a bettor could take, and the margin that prices a one-sided bet. | 4: `test_odds_readers.py`, `test_segment_markets.py`, `test_accuracy_comparison.py` +1 |
| [`scripts/accuracy/records.py`](../../scripts/accuracy/records.py) | 262 | The accuracy record, and the market vocabulary every stage shares. | 8: `test_odds_readers.py`, `test_segment_markets.py`, `test_rescore_runlines.py` +5 |
| [`scripts/accuracy/replay.py`](../../scripts/accuracy/replay.py) | 787 | Each game's replay and scoring, in one process or across a pool of worker processes. | 4: `test_sim_kwargs.py`, `test_odds_readers.py`, `test_accuracy_comparison.py` +1 |
| [`scripts/accuracy/report.py`](../../scripts/accuracy/report.py) | 368 | The accuracy table: the records rolled into one row per market, and the text table the run prints. | 5: `test_odds_readers.py`, `test_rescore_runlines.py`, `test_accuracy_comparison.py` +2 |
| [`scripts/accuracy/selection.py`](../../scripts/accuracy/selection.py) | 201 | The game selection: which games a run scores, and the frozen inputs it scores them with. | 1: `test_accuracy_comparison.py` |
| [`scripts/pair_accuracy.py`](../../scripts/pair_accuracy.py) | 411 | scripts/pair_accuracy.py — SIM-518: pair two accuracy-comparison reports. | 3: `test_pair_accuracy.py`, `test_odds_readers.py`, `test_accuracy_fit_instruments.py` |
| [`scripts/designed_experiment.py`](../../scripts/designed_experiment.py) | 544 | scripts/designed_experiment.py — SIM-548 §5.3: the DESIGNED EXPERIMENT on the full simulator. | 2: `test_designed_experiment.py`, `test_accuracy_fit_instruments.py` |
| [`scripts/designed_experiment_run.sh`](../../scripts/designed_experiment_run.sh) | 37 | The designed experiment on the full simulator (the accuracy fit, SIM-548; the review's §4 design): four factors as a full 2^4 — the pitch draw's pitcher power 16 / 4, the result draw's pitcher power 16 / 4, the result... | — |
| [`scripts/fence_check.py`](../../scripts/fence_check.py) | 1,444 | scripts/fence_check.py — the certification of the outfield fence (SIM-478/479/480): the fence stage's decision against every real air ball of the pool window, with the thresholds of the plan's §7 (docs/audit/2026-09-2... | 1: `test_fence_check.py` |
| [`scripts/pool_window_census.py`](../../scripts/pool_window_census.py) | 306 | scripts/pool_window_census.py — measure the three pool-window options + the pool-referenced frequency centres (the owner's questions of 2026-08-20). | — |
| [`scripts/validate_props.py`](../../scripts/validate_props.py) | 611 | Validate the simulator's win probabilities + prop PMFs (probability mass functions: the probability of each count) against REAL completed-game outcomes, write a validation report, and (optionally) fit the win-probabil... | 3: `test_validate_props.py`, `test_accuracy_comparison_props.py`, `test_calibration_reconciliation.py` |
| [`scripts/fit_calibration.py`](../../scripts/fit_calibration.py) | 296 | Fit + persist a `CalibrationReport` over the real DuckDB profiles and (optionally) check that it lands every engine's median similarity near the 0.50 target. | — |
| [`scripts/calibrate_markets.py`](../../scripts/calibrate_markets.py) | 565 | scripts/calibrate_markets.py — SIM-548 §5.4 / §7: the per-market CALIBRATION LAYER — a map from the simulator's probability to a calibrated one, fitted on one set of games and validated on another it never saw. | 3: `test_odds_readers.py`, `test_market_calibration.py`, `test_accuracy_fit_instruments.py` |
| [`scripts/market_skill.py`](../../scripts/market_skill.py) | 628 | scripts/market_skill.py — SIM-548 §5.1: the per-market SKILL table of one production read, and the composite objective between two reads. | 3: `test_odds_readers.py`, `test_market_calibration.py`, `test_accuracy_fit_instruments.py` |
| [`scripts/offline_draw_fit.py`](../../scripts/offline_draw_fit.py) | 1,059 | scripts/offline_draw_fit.py — SIM-548 §5.2: the OFFLINE JOINT FIT of the pitch draws' weights against the real outcomes of held-out plays (v2, 2026-09-15, after the instruments' review). | — |
| [`scripts/rebuild_pitch_pool.py`](../../scripts/rebuild_pitch_pool.py) | 494 | scripts/rebuild_pitch_pool.py — the pitch-pool rebuild on the foul-tip coding (SIM-553), end to end. | — |
| [`scripts/pitch_pool_rebuild/__init__.py`](../../scripts/pitch_pool_rebuild/__init__.py) | 8 | The steps of the pitch-pool rebuild (`scripts/rebuild_pitch_pool.py`). | — |
| [`scripts/pitch_pool_rebuild/checks.py`](../../scripts/pitch_pool_rebuild/checks.py) | 409 | Step 3, the checks a to e inside the rebuild's transaction, the rollback and the restore. | — |
| [`scripts/pitch_pool_rebuild/common.py`](../../scripts/pitch_pool_rebuild/common.py) | 224 | The constants and the small helpers every step of the pitch-pool rebuild shares: the paths, the files of a whole pitch pool, the expected class moves, the exit codes. | — |
| [`scripts/pitch_pool_rebuild/export.py`](../../scripts/pitch_pool_rebuild/export.py) | 633 | Steps 4 to 6: the bundle copy, the pitch-pool export, the round-trip and the rollback text. | — |
| [`scripts/pitch_pool_rebuild/preflight.py`](../../scripts/pitch_pool_rebuild/preflight.py) | 645 | Step 0, the pre-flight: every reason the write path must not start, and the drift line (information only). | — |
| [`scripts/rebuild_steal_pool.py`](../../scripts/rebuild_steal_pool.py) | 458 | scripts/rebuild_steal_pool.py — the steal-pool rebuild for the running game on the pitch (SIM-554), end to end. | 1: `test_steal_pool_rebuild.py` |
| [`scripts/steal_pool_rebuild/__init__.py`](../../scripts/steal_pool_rebuild/__init__.py) | 9 | The steps of the steal-pool rebuild (`scripts/rebuild_steal_pool.py`). | — |
| [`scripts/steal_pool_rebuild/bundle.py`](../../scripts/steal_pool_rebuild/bundle.py) | 230 | The bundle's steal pool: whether it is whole, the rollback copy (step 4) and the hashes that prove the export touched nothing else. | 1: `test_steal_pool_rebuild.py` |
| [`scripts/steal_pool_rebuild/checks.py`](../../scripts/steal_pool_rebuild/checks.py) | 400 | Step 3, the checks a to h inside the rebuild's transaction, and the rollback. | 1: `test_steal_pool_rebuild.py` |
| [`scripts/steal_pool_rebuild/common.py`](../../scripts/steal_pool_rebuild/common.py) | 158 | The constants and the small helpers every step of the steal-pool rebuild shares: the paths, the files of a whole steal pool, the labels, the exit codes. | 1: `test_steal_pool_rebuild.py` |
| [`scripts/steal_pool_rebuild/export.py`](../../scripts/steal_pool_rebuild/export.py) | 351 | Steps 5 to 7: the export, the round-trip, the steal band centres and the rollback text. | 1: `test_steal_pool_rebuild.py` |
| [`scripts/steal_pool_rebuild/migration.py`](../../scripts/steal_pool_rebuild/migration.py) | 35 | Migration 0031, applied inside the rebuild's own transaction (step 2). | 1: `test_steal_pool_rebuild.py` |
| [`scripts/steal_pool_rebuild/pickoffs.py`](../../scripts/steal_pool_rebuild/pickoffs.py) | 142 | Step 1: the snapshot, and the pickoff rows counted from the play records (never from the builder's SQL). | — |
| [`scripts/steal_pool_rebuild/preflight.py`](../../scripts/steal_pool_rebuild/preflight.py) | 225 | Step 0, the pre-flight: every reason the write path must not start. | 1: `test_steal_pool_rebuild.py` |
| [`scripts/load_historical_odds.py`](../../scripts/load_historical_odds.py) | 1,268 | The historical odds loader. | 2: `test_odds_closing_rows.py`, `test_odds_game_markets.py` |
| [`scripts/load_official_boxscores.py`](../../scripts/load_official_boxscores.py) | 344 | The official box-score backfill: fetch the MLB Stats API box score for every completed game and store one row per player who appeared in `raw.game_player_stats`. | 2: `test_boxscore_ingest.py`, `test_game_bullpen_listing.py` |
| [`scripts/reload_games.py`](../../scripts/reload_games.py) | 374 | reload_games.py — corrective reload for SPECIFIC games, or for every gap it can find. | — |
| [`scripts/resumable_sweep.py`](../../scripts/resumable_sweep.py) | 287 | resumable_sweep.py — drive the corrective reload sweep to completion despite a stochastic interpreter crash. | 1: `test_sweep_streaming.py` |
| [`scripts/behaviour_fingerprint.py`](../../scripts/behaviour_fingerprint.py) | 546 | Prove that a refactor leaves the simulator's output unchanged. | — |
| [`scripts/accuracy_baseline_1000.sh`](../../scripts/accuracy_baseline_1000.sh) | 28 | The 1,000-game accuracy baseline of production (the accuracy fit, SIM-548; the split ON at 16 / 16 / 8 and the fatigue weight at 0.5 since 2026-09-14): the simulator's Brier score against the closing line's, per marke... | — |
| [`scripts/accuracy_split_arm.sh`](../../scripts/accuracy_split_arm.sh) | 24 | The pitch / pitch-result split as one paired accuracy arm (the accuracy fit, SIM-548). | — |
| [`scripts/boxscore_audit.py`](../../scripts/boxscore_audit.py) | 475 | The validation study for the official box-score ground truth: how far each per-player total the platform derives from `raw.pitches` sits from the official box score. | 1: `test_boxscore_reader.py` |
| [`scripts/check_file_integrity.py`](../../scripts/check_file_integrity.py) | 176 | The file-integrity guard: it catches a `.py` file that a file bridge truncated or filled with NUL bytes. | 1: `test_check_file_integrity.py` |
| [`scripts/docs_file_index.py`](../../scripts/docs_file_index.py) | 840 | Write the file index: every source file, in the order of the code review path. | 1: `test_docs_file_index.py` |
| [`scripts/export_openapi.py`](../../scripts/export_openapi.py) | 45 | Export the FastAPI OpenAPI schema to `frontend/openapi.json`. | — |
| [`scripts/genexpr_crash_probe.py`](../../scripts/genexpr_crash_probe.py) | 434 | The SIM-445 crash class — a probe that re-runs the workload that segfaulted, with and without the database. | — |
| [`scripts/measure_filter_cells.py`](../../scripts/measure_filter_cells.py) | 1,378 | Measure how full the 2,880 hard-filter cells are (the filter-cell occupancy measurement, SIM-451). | 2: `test_filter_cells.py`, `test_pitch_cell_index.py` |
| [`scripts/running_game_replay.py`](../../scripts/running_game_replay.py) | 1,473 | scripts/running_game_replay.py — SIM-554: the running-game replay. | 1: `test_running_game_replay.py` |
| [`scripts/trace_game.py`](../../scripts/trace_game.py) | 183 | scripts/trace_game.py — a per-pitch trace of one simulated game. | — |
| [`scripts/probes/actor_concentration_check.py`](../../scripts/probes/actor_concentration_check.py) | 86 | scripts/probes/actor_concentration_check.py — SIM-523 part F: the concentration check at the FITTED powers. | — |
| [`scripts/probes/arm_angle_probe.py`](../../scripts/probes/arm_angle_probe.py) | 1,223 | scripts/probes/arm_angle_probe.py — the evidence behind the arm-angle decision (SIM-533): what Savant's arm angle, active spin and spin-axis deviation add to the pitcher model. | 1: `test_arm_angle_decision.py` |
| [`scripts/probes/arsenal_reproducibility_check.py`](../../scripts/probes/arsenal_reproducibility_check.py) | 230 | scripts/probes/arsenal_reproducibility_check.py — two pitcher profile builds of one season from the same data, compared by content: do they write the same arsenals (SIM-580)? | — |
| [`scripts/probes/backfill_odds_hash.py`](../../scripts/probes/backfill_odds_hash.py) | 279 | One-shot backfill that fills `raw.game_odds.odds_hash` for every legacy row that predates SIM-092 (which began populating the column at write time but left history alone). | 1: `test_backfill_odds_hash.py` |
| [`scripts/probes/backfill_start_positions.py`](../../scripts/probes/backfill_start_positions.py) | 352 | Repair the starters' positions in `raw.game_lineups` (SIM-559). | — |
| [`scripts/probes/book_sharpness.py`](../../scripts/probes/book_sharpness.py) | 921 | scripts/probes/book_sharpness.py — SIM-555: which book's closing line forecasts the real outcome best? | 1: `test_book_sharpness.py` |
| [`scripts/probes/bullpen_check.py`](../../scripts/probes/bullpen_check.py) | 227 | scripts/probes/bullpen_check.py — SIM-427 part 4b's check: the real pen against reality. | — |
| [`scripts/probes/catcher_receiving_probe.py`](../../scripts/probes/catcher_receiving_probe.py) | 198 | scripts/probes/catcher_receiving_probe.py — SIM-523 part E: the catcher receiving RATIO, on vs off. | — |
| [`scripts/probes/check_bat_side_coverage.py`](../../scripts/probes/check_bat_side_coverage.py) | 188 | scripts/probes/check_bat_side_coverage.py - SIM-160 Audit raw.pitches.bat_hand coverage per season. | — |
| [`scripts/probes/conditional_rate_probe.py`](../../scripts/probes/conditional_rate_probe.py) | 1,100 | scripts/probes/conditional_rate_probe.py — SIM-523 part F: the conditional-rate FIT instrument. | 1: `test_outfield_arm_recompute.py` |
| [`scripts/probes/fatigue_kernel_scan.py`](../../scripts/probes/fatigue_kernel_scan.py) | 749 | scripts/probes/fatigue_kernel_scan.py — SIM-518: the OFFLINE fatigue-kernel scan. | 1: `test_fatigue_kernel_scan.py` |
| [`scripts/probes/fatigue_probe.py`](../../scripts/probes/fatigue_probe.py) | 582 | scripts/probes/fatigue_probe.py — SIM-518: the IN-LOOP fatigue probe (plan §5.2). | 1: `test_odds_readers.py` |
| [`scripts/probes/fielder_profile_recompute.py`](../../scripts/probes/fielder_profile_recompute.py) | 728 | scripts/probes/fielder_profile_recompute.py — Savant's per-position outs above average and the outfield jump in the fielder profile (SIM-532), run-book step 4: the DuckDB migration, the fielder chain, the league rows,... | 1: `test_fielder_profile_recompute.py` |
| [`scripts/probes/manager_probe.py`](../../scripts/probes/manager_probe.py) | 915 | scripts/probes/manager_probe.py — SIM-427: the manager probe (plan part 4f). | 1: `test_odds_readers.py` |
| [`scripts/probes/manager_profile_recompute.py`](../../scripts/probes/manager_profile_recompute.py) | 142 | scripts/probes/manager_profile_recompute.py — SIM-427 part 4a: the manager profiles and the manager league-average row, recomputed on their own (not the five-hour full rebuild). | — |
| [`scripts/probes/odds_book_census.py`](../../scripts/probes/odds_book_census.py) | 1,872 | scripts/probes/odds_book_census.py — SIM-555: the read-only census of the vendor's books, the gate before the re-load. | 2: `test_odds_book_census.py`, `test_odds_census_sql.py` |
| [`scripts/probes/odds_census.sql`](../../scripts/probes/odds_census.sql) | 459 | scripts/probes/odds_census.sql | — |
| [`scripts/probes/outfield_arm_recompute.py`](../../scripts/probes/outfield_arm_recompute.py) | 837 | scripts/probes/outfield_arm_recompute.py — the outfield arm block (SIM-550), run-book step 2: the block rebuilt from our own advancement pool, the league rows, and the verify block. | 1: `test_outfield_arm_recompute.py` |
| [`scripts/probes/rescore_graded_book.py`](../../scripts/probes/rescore_graded_book.py) | 682 | scripts/probes/rescore_graded_book.py — SIM-555: re-price the fixed-line records of stored accuracy reports against the graded book, beside the originals. | 1: `test_rescore_graded_book.py` |
| [`scripts/probes/rescore_runlines.py`](../../scripts/probes/rescore_runlines.py) | 460 | scripts/probes/rescore_runlines.py — SIM-549: re-price the run-line records of stored accuracy reports, beside the originals. | 2: `test_rescore_runlines.py`, `test_odds_readers.py` |
| [`scripts/probes/retire_consensus_odds.py`](../../scripts/probes/retire_consensus_odds.py) | 419 | scripts/probes/retire_consensus_odds.py — SIM-555: archive, then delete, the old `consensus` odds rows, one season at a time. | 1: `test_retire_consensus_odds.py` |
| [`scripts/probes/run_index_acceptance.py`](../../scripts/probes/run_index_acceptance.py) | 380 | Runs the EXPLAIN (ANALYZE, BUFFERS) acceptance gates for the SIM-085 and SIM-089 indexes against a populated staging database, captures the plan text and per-query latency, and emits a Markdown report fragment that th... | 1: `test_index_acceptance_harness.py` |
| [`scripts/probes/runner_profile_recompute.py`](../../scripts/probes/runner_profile_recompute.py) | 368 | scripts/probes/runner_profile_recompute.py — SIM-531 run-book step 4: the three runner-side profile tables and the league averages, recomputed on their own (not the five-hour full rebuild). | — |
| [`scripts/probes/running_game_census.py`](../../scripts/probes/running_game_census.py) | 1,067 | scripts/probes/running_game_census.py — SIM-554: where the simulator's steals and pickoffs land. | 1: `test_running_game_census.py` |
| [`scripts/probes/steal_weight_volume_probe.py`](../../scripts/probes/steal_weight_volume_probe.py) | 1,161 | scripts/probes/steal_weight_volume_probe.py — why the steal draw's look-alike weights lower the number of steal attempts, and what restores it. | 1: `test_steal_weight_volume_probe.py` |
| [`scripts/probes/wall_zone_probe.py`](../../scripts/probes/wall_zone_probe.py) | 418 | scripts/probes/wall_zone_probe.py — the wall-play check (SIM-478/479/480, plan §5.4 and §7 C). | 1: `test_fence_counters.py` |

<a id="stage-15"></a>

## 15. Tests and CI

How the code is tested, and what CI runs on every push. The [review path](code-review-path.md#stage-15) says what to look for.

| File | Lines | What it is | Tested by |
|---|--:|---|---|
| [`tests/conftest.py`](../../tests/conftest.py) | 382 | Root test configuration: shared fixtures and pytest hooks. | — |
| [`tests/script_loader.py`](../../tests/script_loader.py) | 60 | Load a script from `scripts/` as a module, for a test. | 45: `test_sim_kwargs.py`, `test_gen_secrets.py`, `test_backup_drill.py` +42 |
| [`tests/__init__.py`](../../tests/__init__.py) | 1 | Package marker. | — |
| [`tests/regression/__init__.py`](../../tests/regression/__init__.py) | 10 | Package marker. | — |
| [`tests/regression/conftest.py`](../../tests/regression/conftest.py) | 323 | Fixtures that build deterministic synthetic similarity engines for the regression gate (the engine invariant tests). | — |
| [`tests/regression/regression_config.py`](../../tests/regression/regression_config.py) | 52 | Tolerance constants for the similarity engine regression gate (the engine invariant tests). | 1: `test_engine_regression.py` |
| [`tests/regression/test_engine_regression.py`](../../tests/regression/test_engine_regression.py) | 443 | The similarity engine INVARIANT gate. | — |
| [`tests/acceptance/__init__.py`](../../tests/acceptance/__init__.py) | 10 | SIM-450 — the production-configuration acceptance-band lane. | 4: `test_band_arithmetic.py`, `test_pool_label_check.py`, `test_production_bands.py` +1 |
| [`tests/acceptance/bands.py`](../../tests/acceptance/bands.py) | 1,412 | The reference rates, the band rule, and the contract that governs both (the acceptance lane, SIM-450). | 4: `test_production_bands.py`, `test_band_arithmetic.py`, `test_pool_label_check.py` +1 |
| [`tests/acceptance/conftest.py`](../../tests/acceptance/conftest.py) | 954 | The fixtures that run the simulator in the PRODUCTION configuration (the acceptance lane, SIM-450). | 4: `test_fence_counters.py`, `test_band_arithmetic.py`, `test_pool_label_check.py` +1 |
| [`tests/acceptance/test_band_arithmetic.py`](../../tests/acceptance/test_band_arithmetic.py) | 1,494 | The band rule of the acceptance lane (SIM-450), tested on synthetic inputs. | — |
| [`tests/acceptance/test_pool_label_check.py`](../../tests/acceptance/test_pool_label_check.py) | 259 | SIM-553 — the standing label check: the pitch pool's count chain against real play. | — |
| [`tests/acceptance/test_production_bands.py`](../../tests/acceptance/test_production_bands.py) | 720 | The acceptance lane (SIM-450): the PRODUCTION simulator against reference rates. | — |
| [`tests/unit/test_accuracy_comparison.py`](../../tests/unit/test_accuracy_comparison.py) | 1,160 | Unit tests for the sim-vs-closing-line accuracy comparison (`scripts/accuracy_comparison.py`). | — |
| [`tests/unit/test_accuracy_comparison_markets.py`](../../tests/unit/test_accuracy_comparison_markets.py) | 111 | Unit tests for the plain data in `scripts/accuracy/records.py`: the 15-market prop vocabulary map (the original seven plus the eight of the prop-market work, SIM-421) and the market trust labels. | — |
| [`tests/unit/test_accuracy_comparison_props.py`](../../tests/unit/test_accuracy_comparison_props.py) | 713 | The backtest wiring for the fifteen prop markets (the prop-market work, SIM-421) and the official box-score ground truth (the owner's ruling to grade props on the box, SIM-545), in `scripts/accuracy_comparison.py` and... | — |
| [`tests/unit/test_accuracy_fit_instruments.py`](../../tests/unit/test_accuracy_fit_instruments.py) | 462 | The instruments of the joint-fit plan (the accuracy fit, SIM-548), on synthetic inputs (no DB, no bundle): | — |
| [`tests/unit/test_accuracy_hypothetical_return.py`](../../tests/unit/test_accuracy_hypothetical_return.py) | 406 | Unit tests for the hypothetical dollar return, an OPT-IN companion report to the accuracy comparison in `scripts/accuracy_comparison.py`. | — |
| [`tests/unit/test_accuracy_minimum_sample.py`](../../tests/unit/test_accuracy_minimum_sample.py) | 547 | Unit tests for the stated MINIMUM SAMPLE SIZE of the accuracy reports that need one (`scripts/accuracy/confidence.py`): the sim-vs-closing-line accuracy comparison and the hypothetical dollar return. | — |
| [`tests/unit/test_actor_matrices.py`](../../tests/unit/test_actor_matrices.py) | 630 | The nightly ACTOR SCORE MATRICES and the sampler factors that read them (the play-picker redesign plan: docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md). | — |
| [`tests/unit/test_actor_powers.py`](../../tests/unit/test_actor_powers.py) | 445 | SIM-523 part F — the FIT's knobs (plan: docs/audit/2026-09-08-sim523-play-picker- redesign-plan.md, part F). | — |
| [`tests/unit/test_api_auth.py`](../../tests/unit/test_api_auth.py) | 113 | The CORS allowlist and the latency middleware of `api/auth.py`. | — |
| [`tests/unit/test_api_auth_session.py`](../../tests/unit/test_api_auth_session.py) | 378 | The browser session login (`api/auth.py` and `api/routes/auth.py`). | — |
| [`tests/unit/test_api_betting.py`](../../tests/unit/test_api_betting.py) | 442 | The betting routes under `/api/betting`. | — |
| [`tests/unit/test_api_data_health.py`](../../tests/unit/test_api_data_health.py) | 122 | The data-freshness route (`api/routes/data_health.py`). | — |
| [`tests/unit/test_api_error_envelope.py`](../../tests/unit/test_api_error_envelope.py) | 72 | The app's exception handler and its structured error envelope (`api/errors.py`). | — |
| [`tests/unit/test_api_game_card.py`](../../tests/unit/test_api_game_card.py) | 473 | The loop-output routes of api/routes/games.py: the game card, the fielders, the box. | — |
| [`tests/unit/test_api_games.py`](../../tests/unit/test_api_games.py) | 1,416 | The game routes under `/api/games`: the slate, /simulate, the override, the props. | — |
| [`tests/unit/test_api_games_plays.py`](../../tests/unit/test_api_games_plays.py) | 333 | The /plays and /state replay routes of api/routes/games.py, and the replay store. | — |
| [`tests/unit/test_api_main_wiring.py`](../../tests/unit/test_api_main_wiring.py) | 580 | The app wiring of api/main.py: the routers, the lifespan, CORS and the rate limit. | — |
| [`tests/unit/test_api_plays_pagination.py`](../../tests/unit/test_api_plays_pagination.py) | 132 | The pagination of GET /api/games/{game_pk}/plays (`api/routes/games.py`). | — |
| [`tests/unit/test_api_realism_wiring.py`](../../tests/unit/test_api_realism_wiring.py) | 109 | The wiring that carries the realism inputs into the `simulate_game` GameSpec. | — |
| [`tests/unit/test_api_schemas.py`](../../tests/unit/test_api_schemas.py) | 365 | The API's response models (:mod:`api.schemas`) and :mod:`api.serialization`. | — |
| [`tests/unit/test_api_state.py`](../../tests/unit/test_api_state.py) | 313 | The boot-time builders in api/state.py that the lifespan attaches to `app.state`. | — |
| [`tests/unit/test_arm_angle_decision.py`](../../tests/unit/test_arm_angle_decision.py) | 333 | The guard for the pitcher arm-angle and spin-shape decision. | — |
| [`tests/unit/test_arsenal_cache_vectorized.py`](../../tests/unit/test_arsenal_cache_vectorized.py) | 370 | The matrix row lookup of the arsenal distance cache. | — |
| [`tests/unit/test_arsenal_fit_reproducible.py`](../../tests/unit/test_arsenal_fit_reproducible.py) | 204 | The pitcher arsenal fit gives the same arsenal whatever order the pitch rows arrive in. | — |
| [`tests/unit/test_arsenal_w2_standardization.py`](../../tests/unit/test_arsenal_w2_standardization.py) | 351 | The arsenal W₂ distance reads a GMM's mean and covariance on one scale. | — |
| [`tests/unit/test_backfill_odds_hash.py`](../../tests/unit/test_backfill_odds_hash.py) | 145 | Unit tests for the odds_hash backfill script (`scripts/probes/backfill_odds_hash.py`). | — |
| [`tests/unit/test_backup_age_gauge.py`](../../tests/unit/test_backup_age_gauge.py) | 570 | The backup-age gauge on /metrics (the scheduled backups, SIM-525). | — |
| [`tests/unit/test_backup_data_stores.py`](../../tests/unit/test_backup_data_stores.py) | 607 | SIM-525 — the backups of the DuckDB file and the engine bundle (pipeline/backup/data_stores.py). | — |
| [`tests/unit/test_backup_drill.py`](../../tests/unit/test_backup_drill.py) | 772 | The restore drill for the scheduled backups. | — |
| [`tests/unit/test_backup_offsite.py`](../../tests/unit/test_backup_offsite.py) | 800 | SIM-568 — the off-site copy of the backup sets (pipeline/backup/offsite.py). | — |
| [`tests/unit/test_backup_schedule.py`](../../tests/unit/test_backup_schedule.py) | 382 | The scheduled backups of the three data stores: the wiring. | — |
| [`tests/unit/test_backup_status.py`](../../tests/unit/test_backup_status.py) | 78 | SIM-525 — the reader of the backup sets (pipeline/backup/status.py). | — |
| [`tests/unit/test_barrel_definition.py`](../../tests/unit/test_barrel_definition.py) | 203 | Validates :func:`pipeline.batch.profiles.sql._barrel_case_sql`, the DuckDB boolean expression that classifies a batted ball as a *barrel*. | — |
| [`tests/unit/test_base_invariants.py`](../../tests/unit/test_base_invariants.py) | 369 | The base-state invariants and the runner-conservation check on `Bases`. | — |
| [`tests/unit/test_baseball_sniff.py`](../../tests/unit/test_baseball_sniff.py) | 484 | The **baseball sniff-test suite**: does the loop produce sane baseball? | — |
| [`tests/unit/test_baserunner_extra_base_similarity.py`](../../tests/unit/test_baserunner_extra_base_similarity.py) | 123 | The baserunner extra-base similarity engine's core scores: range, self-exclusion, order, symmetry, the three sub-scores and their weights. | — |
| [`tests/unit/test_baserunner_steal_engine.py`](../../tests/unit/test_baserunner_steal_engine.py) | 405 | The scoring contract of the steal-runner similarity engine. | — |
| [`tests/unit/test_baserunner_steal_scores.py`](../../tests/unit/test_baserunner_steal_scores.py) | 254 | The baserunner steal engine's scores, and the generic similarity diagnostics (`similarity_diagnostics.run_generic_diagnostics`) run over that engine. | — |
| [`tests/unit/test_batch_runner.py`](../../tests/unit/test_batch_runner.py) | 284 | Tests of the parallel batch runner, :mod:`simulation.batch_runner`. | — |
| [`tests/unit/test_batch_runner_concurrency.py`](../../tests/unit/test_batch_runner_concurrency.py) | 320 | The **concurrency STRESS test** for the parallel batch runner (:mod:`simulation.batch_runner`). | — |
| [`tests/unit/test_batch_runner_hit_rate.py`](../../tests/unit/test_batch_runner_hit_rate.py) | 149 | The batch runner's underscore-prefix convention for `sim_kwargs`. | — |
| [`tests/unit/test_batted_ball_engine.py`](../../tests/unit/test_batted_ball_engine.py) | 286 | The batted-ball similarity engine (a FAISS nearest-neighbour index). | — |
| [`tests/unit/test_batter_physical_features.py`](../../tests/unit/test_batter_physical_features.py) | 450 | The batter model's physical group (the swing and stance features), and the fielder and catcher arm measurements that the same profile build fills. | — |
| [`tests/unit/test_batter_similarity.py`](../../tests/unit/test_batter_similarity.py) | 485 | The batter-to-batter similarity engine. | 1: `test_batter_physical_features.py` |
| [`tests/unit/test_bet_signal.py`](../../tests/unit/test_bet_signal.py) | 370 | The bet-signal module: which edges become +EV bets, and how much to stake. | — |
| [`tests/unit/test_bettingpros_provider.py`](../../tests/unit/test_bettingpros_provider.py) | 557 | Unit tests for the SIM-405 BettingPros odds provider. | — |
| [`tests/unit/test_book_sharpness.py`](../../tests/unit/test_book_sharpness.py) | 338 | `scripts/probes/book_sharpness.py`: the odds-against-outcome read that ranks the books (plan test 17), on a synthetic season. | — |
| [`tests/unit/test_boxscore_extended.py`](../../tests/unit/test_boxscore_extended.py) | 533 | The extended per-player box score: the batting fields `b2` (doubles), `b3` (triples), `r` (runs scored) and `sb` (stolen bases), the pitching fields `h_allowed` and `r_allowed`, and the exact total-bases formula of `p... | — |
| [`tests/unit/test_boxscore_ingest.py`](../../tests/unit/test_boxscore_ingest.py) | 751 | The official box-score ground truth (SIM-545): the parser over a REAL captured payload, the persist seams over in-memory fakes, and the fetch seam's retry. | — |
| [`tests/unit/test_boxscore_reader.py`](../../tests/unit/test_boxscore_reader.py) | 485 | The official box-score ground truth (SIM-545): the reader `real_props_from_boxscore_rows` on rows parsed from the real captured payload, the `pair_props_for_validation` prop-tuple keyword, and the audit script's deriv... | — |
| [`tests/unit/test_bullpen_availability.py`](../../tests/unit/test_bullpen_availability.py) | 639 | SIM-433 — per-game bullpen availability + IL ingestion. | — |
| [`tests/unit/test_bullpen_usage.py`](../../tests/unit/test_bullpen_usage.py) | 127 | SIM-427 — the pen's facts from the box (`pipeline/bullpen_usage.py`). | — |
| [`tests/unit/test_bundle_no_pickle.py`](../../tests/unit/test_bundle_no_pickle.py) | 134 | The engine-artifact bundle loads without a pickle (SIM-565). | — |
| [`tests/unit/test_bundle_versions.py`](../../tests/unit/test_bundle_versions.py) | 190 | SIM-563 — the engine-artifact bundle is published as a version behind a link. | — |
| [`tests/unit/test_bundle_worker_and_boot.py`](../../tests/unit/test_bundle_worker_and_boot.py) | 304 | The workers read only the bundle, and the app restarts on a new bundle version. | — |
| [`tests/unit/test_cache_codec.py`](../../tests/unit/test_cache_codec.py) | 331 | tests/unit/test_cache_codec.py -- the Redis cache stores JSON, never a pickle. | — |
| [`tests/unit/test_calibration_fit_and_apply.py`](../../tests/unit/test_calibration_fit_and_apply.py) | 462 | Tests of how a fitted `CalibrationReport` reaches the engines. | — |
| [`tests/unit/test_calibration_reconciliation.py`](../../tests/unit/test_calibration_reconciliation.py) | 239 | The similarity calibrator and the `validate_props` script against the live DuckDB / Postgres schema. | — |
| [`tests/unit/test_calibration_report_json.py`](../../tests/unit/test_calibration_report_json.py) | 333 | The calibration report's JSON form, the win-probability map and the engine build. | — |
| [`tests/unit/test_catcher_engine_throwing.py`](../../tests/unit/test_catcher_engine_throwing.py) | 302 | Tests of the catcher engine's throwing and deterrence sub-scores. | — |
| [`tests/unit/test_catcher_similarity_scores.py`](../../tests/unit/test_catcher_similarity_scores.py) | 145 | The catcher similarity engine's scores: range, symmetry, self-exclusion, the four defensive sub-scores and the empirical-Bayes prior. | — |
| [`tests/unit/test_check_file_integrity.py`](../../tests/unit/test_check_file_integrity.py) | 150 | Unit tests for `scripts/check_file_integrity.py`, the file-integrity guard. | — |
| [`tests/unit/test_clv_engine.py`](../../tests/unit/test_clv_engine.py) | 482 | The closing-line-value and edge engine, :mod:`betting.clv_engine`. | — |
| [`tests/unit/test_clv_engine_run_line.py`](../../tests/unit/test_clv_engine_run_line.py) | 259 | The run line (spread) EdgeReport of :mod:`betting.clv_engine`. | — |
| [`tests/unit/test_db_dsn.py`](../../tests/unit/test_db_dsn.py) | 61 | SIM-443b — the connection-string reader has no default password. | — |
| [`tests/unit/test_db_introspect.py`](../../tests/unit/test_db_introspect.py) | 93 | The existence probe (`db/introspect.py`), on a real in-memory DuckDB. | — |
| [`tests/unit/test_delete_seasons_coverage.py`](../../tests/unit/test_delete_seasons_coverage.py) | 94 | The profile computor's `_delete_seasons` lists every season-keyed `derived.*` table, so a full rebuild leaves no stale rows behind. | — |
| [`tests/unit/test_designed_experiment.py`](../../tests/unit/test_designed_experiment.py) | 275 | The designed-experiment runner (scripts/designed_experiment.py), on synthetic reports (no DB, no bundle). | — |
| [`tests/unit/test_docs_alembic_head.py`](../../tests/unit/test_docs_alembic_head.py) | 58 | Doc guard: the Alembic head the living docs cite is the newest migration on disk. | — |
| [`tests/unit/test_docs_duckdb_version.py`](../../tests/unit/test_docs_duckdb_version.py) | 66 | Doc guard: the DuckDB schema version the living docs cite is the one on disk. | — |
| [`tests/unit/test_docs_file_index.py`](../../tests/unit/test_docs_file_index.py) | 64 | Doc guard: the file index lists every source file in its review stage. | — |
| [`tests/unit/test_draw_conditioning.py`](../../tests/unit/test_draw_conditioning.py) | 666 | The draw-conditioning inputs: the pool columns that carry them and the draw weights that read them. | — |
| [`tests/unit/test_dropped_third_strike_credits.py`](../../tests/unit/test_dropped_third_strike_credits.py) | 380 | The box-score credits on the dropped-third-strike play. | — |
| [`tests/unit/test_engine_artifacts_realism.py`](../../tests/unit/test_engine_artifacts_realism.py) | 343 | SIM-411 / SIM-413 / SIM-425b — the batted-ball-artifact realism-column plumbing. | — |
| [`tests/unit/test_engine_artifacts_shared_memory.py`](../../tests/unit/test_engine_artifacts_shared_memory.py) | 345 | The EngineArtifacts shared-memory publish/attach contract. | — |
| [`tests/unit/test_engine_build_smoke.py`](../../tests/unit/test_engine_build_smoke.py) | 252 | Smoke tests of each similarity engine's build(). | — |
| [`tests/unit/test_engine_calibration_regression.py`](../../tests/unit/test_engine_calibration_regression.py) | 325 | The sigma fit of the RBF kernel on three engines' feature matrices, and the FAISS lookup of the two nearest-neighbour engines. | — |
| [`tests/unit/test_etl_connection_pool.py`](../../tests/unit/test_etl_connection_pool.py) | 212 | Before SIM-090, `HistoricalDataLoader._get_conn()` opened a fresh `psycopg2.connect(self.dsn)` and `conn.close()`-d it on every single DB round-trip. | — |
| [`tests/unit/test_etl_ensure_rows.py`](../../tests/unit/test_etl_ensure_rows.py) | 369 | The historical loader's per-table prerequisite writers: _ensure_teams, _ensure_players, _ensure_managers and _ensure_game. | — |
| [`tests/unit/test_etl_error_ledger.py`](../../tests/unit/test_etl_error_ledger.py) | 182 | The ETL error ledger: `raw.etl_errors` holds one row per pitch the historical loader skips, the loader writes it on the pitch insert's cursor, and `reprocess_errored_games` lists the games to load again. | — |
| [`tests/unit/test_etl_hardening.py`](../../tests/unit/test_etl_hardening.py) | 757 | Covers the fixes that are NOT about the reload path (those live in `test_etl_reload_game.py`): | — |
| [`tests/unit/test_etl_historical_loader.py`](../../tests/unit/test_etl_historical_loader.py) | 1,677 | Targets: - Pure type-coercion helpers (to_float / to_int / to_bool / to_str, now shared in pipeline/etl/coercion.py) - _build_row_dict column rename + coercion - _validate_row two-tier validation rules - _parse_height... | 1: `test_play_events.py` |
| [`tests/unit/test_etl_reload_game.py`](../../tests/unit/test_etl_reload_game.py) | 1,232 | Covers: * `HistoricalDataLoader.reload_game` — DELETE + re-INSERT in ONE transaction, the only way to repair already-loaded `raw.pitches` rows (the INSERT carries `ON CONFLICT … DO NOTHING`, so plain `load_game` write... | — |
| [`tests/unit/test_etl_schedule_final_filter.py`](../../tests/unit/test_etl_schedule_final_filter.py) | 33 | Unit tests for the historical loader's future/non-Final game guard (_schedule_game_is_final). | — |
| [`tests/unit/test_etl_starting_lineups.py`](../../tests/unit/test_etl_starting_lineups.py) | 102 | Unit tests for SIM-409's starting-lineup builder (_build_starting_lineup_rows), which turns a feed/live boxscore into raw.game_lineups rows. | — |
| [`tests/unit/test_fatigue_kernel_scan.py`](../../tests/unit/test_fatigue_kernel_scan.py) | 165 | The arithmetic of the offline fatigue-kernel scan (scripts/probes/fatigue_kernel_scan.py). | — |
| [`tests/unit/test_fence_carry_and_margin.py`](../../tests/unit/test_fence_carry_and_margin.py) | 779 | Tests of the fence certification's three amendments on the fielding draw. | 1: `test_fence_counters.py` |
| [`tests/unit/test_fence_check.py`](../../tests/unit/test_fence_check.py) | 849 | SIM-478/479/480 — the fence certification script's metrics on fixtures with KNOWN answers (`scripts/fence_check.py`). | — |
| [`tests/unit/test_fence_counters.py`](../../tests/unit/test_fence_counters.py) | 454 | The fence counters in the harness, the wall-play probe's arithmetic and the acceptance lane's JSON record. | — |
| [`tests/unit/test_fence_season_groups.py`](../../tests/unit/test_fence_season_groups.py) | 284 | The fence stage reads a version-2 park geometry (the fence certification, SIM-478/480): an eleven-sector grid (spray -55 to +55 degrees) and, per venue, SEASON GROUPS keyed `"FIRST-LAST"` (a fence that moved gets a ne... | — |
| [`tests/unit/test_fence_stage.py`](../../tests/unit/test_fence_stage.py) | 597 | Tests of the PARK GEOMETRY, the CARRY model and the FENCE STAGE. | — |
| [`tests/unit/test_fielder_map.py`](../../tests/unit/test_fielder_map.py) | 403 | The per-position fielder map: the builder in `simulation/lineup_resolver.py` that turns a resolved lineup into the `{defensive-slot name: player_id}` map `FieldSnapshot.from_game_state` consumes. | — |
| [`tests/unit/test_fielder_oaa_jump_model.py`](../../tests/unit/test_fielder_oaa_jump_model.py) | 1,086 | Savant's per-position outs above average (OAA) and the outfield jump in the fielder model and its calibration. | — |
| [`tests/unit/test_fielder_oaa_jump_profile.py`](../../tests/unit/test_fielder_oaa_jump_profile.py) | 562 | SIM-532 — Savant's outs above average and the outfield jump on the fielder profile: the DuckDB migration, the canonical schema, the aggregator and the league rows. | — |
| [`tests/unit/test_fielder_placeholder_schema.py`](../../tests/unit/test_fielder_placeholder_schema.py) | 205 | SIM-542 — the fielder temp-table placeholders must name the columns the aggregator actually reads. | — |
| [`tests/unit/test_fielder_profile_recompute.py`](../../tests/unit/test_fielder_profile_recompute.py) | 625 | The recompute script for Savant's per-position outs above average and the outfield jump in the fielder profile. | — |
| [`tests/unit/test_fielder_similarity.py`](../../tests/unit/test_fielder_similarity.py) | 1,358 | The fielder-to-fielder similarity engine, similarity/engines/fielder_similarity.py. | — |
| [`tests/unit/test_fielding_and_baserunning_steps.py`](../../tests/unit/test_fielding_and_baserunning_steps.py) | 351 | The loop's **fielding** step and its **baserunning + steals** step (:class:`simulation.sim_loop.StateMachine`), plus the dropped-third-strike edge. | — |
| [`tests/unit/test_fielding_draw.py`](../../tests/unit/test_fielding_draw.py) | 550 | The FIELDING draw's filters and weights (plan: docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md, part C). | — |
| [`tests/unit/test_filter_cells.py`](../../tests/unit/test_filter_cells.py) | 1,061 | Unit tests for the filter-cell measurement (`scripts/measure_filter_cells.py`). | — |
| [`tests/unit/test_flag_registry.py`](../../tests/unit/test_flag_registry.py) | 138 | The `SIM_*` flag registry (`simulation/flags.py`). | — |
| [`tests/unit/test_foul_tip_strike_three.py`](../../tests/unit/test_foul_tip_strike_three.py) | 303 | A two-strike foul tip or foul bunt is strike three in the pitch pool. | — |
| [`tests/unit/test_game_bullpen_listing.py`](../../tests/unit/test_game_bullpen_listing.py) | 171 | The per-game bullpen listing (Alembic 0025, `raw.game_bullpen`). | — |
| [`tests/unit/test_game_feed.py`](../../tests/unit/test_game_feed.py) | 214 | The card detail from the league's per-game feed. | — |
| [`tests/unit/test_game_page_grading.py`](../../tests/unit/test_game_page_grading.py) | 51 | The game page's betting box: each side graded on the real final. | — |
| [`tests/unit/test_game_page_whatif.py`](../../tests/unit/test_game_page_whatif.py) | 582 | The game page's "what if": the real plays, the state at a plate appearance, managerial changes, and simulating the rest of the game from that point. | — |
| [`tests/unit/test_game_sim_summary.py`](../../tests/unit/test_game_sim_summary.py) | 254 | The **multi-iteration aggregation contract** :class:`simulation.results.GameSimSummary`. | — |
| [`tests/unit/test_game_state_contract.py`](../../tests/unit/test_game_state_contract.py) | 297 | Tests of the `GameState` / `PlayResult` dataclass contract. | — |
| [`tests/unit/test_game_type_filter.py`](../../tests/unit/test_game_type_filter.py) | 492 | The Data Lab reads the regular season alone by default (the Data Lab game-type filter, SIM-572). | — |
| [`tests/unit/test_gen_secrets.py`](../../tests/unit/test_gen_secrets.py) | 148 | The secrets generator writes the eight secrets; `--append` and `--check` never print one. | — |
| [`tests/unit/test_gmm_batch_pipeline.py`](../../tests/unit/test_gmm_batch_pipeline.py) | 157 | Unit tests for SIM-113 — GMM batch pipeline performance. | — |
| [`tests/unit/test_historical_odds_load.py`](../../tests/unit/test_historical_odds_load.py) | 465 | SIM-435 — unit tests for (1) the BettingPros provider's CLOSING-line parsing and (2) the historical-odds loader (scripts/load_historical_odds.py). | — |
| [`tests/unit/test_hit_by_pitch.py`](../../tests/unit/test_hit_by_pitch.py) | 132 | Tests that a hit by pitch (HBP) is its own pitch outcome, never a walk. | — |
| [`tests/unit/test_ibb_rates.py`](../../tests/unit/test_ibb_rates.py) | 170 | The measured intentional-walk (IBB) rate table. | — |
| [`tests/unit/test_incomplete_lineup_api.py`](../../tests/unit/test_incomplete_lineup_api.py) | 238 | A known game whose lineup rows do not make a playable game answers 503, not 404. | — |
| [`tests/unit/test_index_acceptance_harness.py`](../../tests/unit/test_index_acceptance_harness.py) | 140 | Pure-string unit tests for the index-acceptance harness (`scripts/probes/run_index_acceptance.py`). | — |
| [`tests/unit/test_ingest_wait.py`](../../tests/unit/test_ingest_wait.py) | 163 | The nightly rebuild waits for the finals load. | — |
| [`tests/unit/test_inning_grid.py`](../../tests/unit/test_inning_grid.py) | 273 | The inning grid on the per-game result and the cached summary. | — |
| [`tests/unit/test_invalid_state_detection.py`](../../tests/unit/test_invalid_state_detection.py) | 450 | The **invalid-state detection harness** for the simulation loop. | — |
| [`tests/unit/test_lead_distance.py`](../../tests/unit/test_lead_distance.py) | 1,525 | Tests of lead distance in the stolen-base and baserunning models. | — |
| [`tests/unit/test_league_average_profiles.py`](../../tests/unit/test_league_average_profiles.py) | 384 | Regression test for Data Engineer ticket SIM-162 — LeagueAverageProfiles. | — |
| [`tests/unit/test_line_movement.py`](../../tests/unit/test_line_movement.py) | 364 | The line-movement time series and its CLV (closing line value). | — |
| [`tests/unit/test_linescore.py`](../../tests/unit/test_linescore.py) | 261 | Tests of the per-inning linescore and the team R/H/E in simulation.linescore. | — |
| [`tests/unit/test_lineup_resolver.py`](../../tests/unit/test_lineup_resolver.py) | 621 | The runtime lineup / substitution resolver (`simulation/lineup_resolver.py`): it reads the lineup store to BUILD a `GameState`. | — |
| [`tests/unit/test_live_bridge.py`](../../tests/unit/test_live_bridge.py) | 258 | SIM-519 Part C — the live service: the Redis bridge, the heartbeat, the poll window, the Preview-to-Live hook and the vendor reads off the loop. | — |
| [`tests/unit/test_live_connection_manager.py`](../../tests/unit/test_live_connection_manager.py) | 51 | The live service's WebSocket connection manager: `broadcast` iterates a snapshot of a game's subscribers, so a client that connects during a send cannot break the loop. | — |
| [`tests/unit/test_live_game_state_builder.py`](../../tests/unit/test_live_game_state_builder.py) | 136 | The live service's game-state builder: the pitcher role it infers from a box line, and the play history it keeps across refreshes. | — |
| [`tests/unit/test_live_pipeline.py`](../../tests/unit/test_live_pipeline.py) | 935 | SIM-348 — Live-ingestion-pipeline coverage. | — |
| [`tests/unit/test_live_pipeline_bugs.py`](../../tests/unit/test_live_pipeline_bugs.py) | 575 | Permanent regression suite for the three P0 bugs fixed in this sprint. | — |
| [`tests/unit/test_live_pipeline_setup.py`](../../tests/unit/test_live_pipeline_setup.py) | 66 | What the live ingestion pipeline sets up at construction: the set of completed games a poll skips (hydrated from `raw.games` at boot), and a simulation callback that must be an async function. | — |
| [`tests/unit/test_live_resimulate_cooldown.py`](../../tests/unit/test_live_resimulate_cooldown.py) | 37 | The live service's manual resimulate endpoint is rate limited: a Redis cooldown key per game, and an HTTP 429 envelope with `retry_after_seconds` while the key lives. | — |
| [`tests/unit/test_live_run_lines.py`](../../tests/unit/test_live_run_lines.py) | 471 | The live betting pages price a run line by its shape. | — |
| [`tests/unit/test_manager_decisions.py`](../../tests/unit/test_manager_decisions.py) | 251 | The manager decisions of the simulation loop's pre-pitch hook (`simulation/loop/manager.py`): the leverage index and the intentional walk. | — |
| [`tests/unit/test_manager_defensive_subs.py`](../../tests/unit/test_manager_defensive_subs.py) | 168 | `defensive_sub` is a per-pitch column on `raw.pitches`, but how MANY pitch rows carry the flag is a property of the ETL parser, not of the manager: | — |
| [`tests/unit/test_manager_draw.py`](../../tests/unit/test_manager_draw.py) | 656 | The manager decisions at the START of a plate appearance: the pitching change as a DRAW from an opportunity pool (the play-picker redesign plan: docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md). | — |
| [`tests/unit/test_manager_model.py`](../../tests/unit/test_manager_model.py) | 383 | The manager plumbing around the pitching-change draw: the **pitch count** and the **SIM_MANAGER gate** (the manager wiring in the production factory and the `simulate_game` passthrough). | — |
| [`tests/unit/test_manager_profile_snapshot.py`](../../tests/unit/test_manager_profile_snapshot.py) | 461 | Tests that the API's runs read each manager's profile from the boot snapshot. | — |
| [`tests/unit/test_manager_similarity.py`](../../tests/unit/test_manager_similarity.py) | 134 | The manager similarity engine's scores: range, symmetry, the three sub-scores, the usage weight and the empirical-Bayes prior. | — |
| [`tests/unit/test_manager_usage_score.py`](../../tests/unit/test_manager_usage_score.py) | 89 | The manager profile's USAGE columns: computed from real pitching changes, never NULL placeholders, and carried through the engine and the calibrator. | — |
| [`tests/unit/test_market_calibration.py`](../../tests/unit/test_market_calibration.py) | 255 | The calibration layer's fit and the composite objective (no DB, no bundle): | — |
| [`tests/unit/test_matchup_weight_cache.py`](../../tests/unit/test_matchup_weight_cache.py) | 340 | The full-pool weight cache has THREE lifetimes, not one. | — |
| [`tests/unit/test_metrics_endpoint.py`](../../tests/unit/test_metrics_endpoint.py) | 216 | The Prometheus metrics route (`api/routes/metrics.py`) and the monitoring config. | — |
| [`tests/unit/test_migration_chain.py`](../../tests/unit/test_migration_chain.py) | 40 | The first eleven Alembic migrations form one unbroken chain: each names the one before it as its `down_revision`. | — |
| [`tests/unit/test_nginx_resolve.py`](../../tests/unit/test_nginx_resolve.py) | 79 | nginx re-resolves the app's name while it runs. | — |
| [`tests/unit/test_no_default_password.py`](../../tests/unit/test_no_default_password.py) | 272 | Text guards on the deployment configuration. | — |
| [`tests/unit/test_no_pitch_readers.py`](../../tests/unit/test_no_pitch_readers.py) | 574 | Every reader of a per-pitch result handles the no-pitch result. | — |
| [`tests/unit/test_odds_book_census.py`](../../tests/unit/test_odds_book_census.py) | 1,153 | `scripts/probes/odds_book_census.py`: the sample, the gate math on synthetic rows, one game read through the provider with a stubbed vendor, and the re-grade of a saved census. | — |
| [`tests/unit/test_odds_book_vocabulary.py`](../../tests/unit/test_odds_book_vocabulary.py) | 211 | The odds book vocabulary: the stored book labels, the kinds, the bettable books and the graded-book order. | — |
| [`tests/unit/test_odds_census_sql.py`](../../tests/unit/test_odds_census_sql.py) | 187 | `scripts/probes/odds_census.sql`: read-only, and its literals in step with the book vocabulary, the first-five exclusion and the load guard's limits. | — |
| [`tests/unit/test_odds_closing_rows.py`](../../tests/unit/test_odds_closing_rows.py) | 963 | Every live game's closing rows (the segment-market work, SIM-546). | — |
| [`tests/unit/test_odds_game_markets.py`](../../tests/unit/test_odds_game_markets.py) | 566 | Every game market the book posts (the prop-market work, SIM-421, owner ruling 2026-09-12). | — |
| [`tests/unit/test_odds_hash_dedup.py`](../../tests/unit/test_odds_hash_dedup.py) | 224 | The live pipeline stores a game-odds quote once: `raw.game_odds` carries an `odds_hash` (SHA-256 of the quote) and a partial unique index on (game_pk, source, odds_hash), and `_persist_odds` inserts with ON CONFLICT …... | — |
| [`tests/unit/test_odds_last_seen.py`](../../tests/unit/test_odds_last_seen.py) | 122 | SIM-519 Part G — odds currency: the last-seen stamp. | — |
| [`tests/unit/test_odds_live_pages.py`](../../tests/unit/test_odds_live_pages.py) | 927 | The live betting pages read one book per odds row and name the book. | — |
| [`tests/unit/test_odds_loader_retry.py`](../../tests/unit/test_odds_loader_retry.py) | 642 | SIM-555 (2026-10-01) — the loader's retry option and its incomplete games. | — |
| [`tests/unit/test_odds_matcher.py`](../../tests/unit/test_odds_matcher.py) | 1,246 | SIM-555 (2026-10-01) — the matcher reads the PLAYED schedule entry of a game. | — |
| [`tests/unit/test_odds_one_book_per_row.py`](../../tests/unit/test_odds_one_book_per_row.py) | 1,901 | SIM-555 (2026-09-28) — every book's prices stored, one book per row, and a load guard. | — |
| [`tests/unit/test_odds_provider_ingestion.py`](../../tests/unit/test_odds_provider_ingestion.py) | 446 | SIM-340 — Real odds provider + prop ingestion (multi-book, sharp flag, cadence) | — |
| [`tests/unit/test_odds_provider_pin.py`](../../tests/unit/test_odds_provider_pin.py) | 139 | The test lane's odds provider pin (SIM-567). | — |
| [`tests/unit/test_odds_provider_retry.py`](../../tests/unit/test_odds_provider_retry.py) | 696 | SIM-555 (2026-10-01) — the BettingPros provider retries a transient read failure and counts every failed read it catches. | — |
| [`tests/unit/test_odds_provider_seam.py`](../../tests/unit/test_odds_provider_seam.py) | 269 | SIM-370 — Real odds/prop provider swap behind MockOddsAPI. | — |
| [`tests/unit/test_odds_readers.py`](../../tests/unit/test_odds_readers.py) | 1,176 | The odds readers (tests 15 and 16 of the one-book-per-row plan, SIM-555: `docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md` §5.5, §7). | — |
| [`tests/unit/test_odds_vocabulary.py`](../../tests/unit/test_odds_vocabulary.py) | 971 | SIM-421 — the prop-bet types the market already offers but the platform did not price: the odds vocabulary, the BettingPros provider cache, the loaders and the CHECK-constraint migration. | — |
| [`tests/unit/test_odds_writers.py`](../../tests/unit/test_odds_writers.py) | 1,691 | SIM-555 (2026-09-28) — the writers store every book's row, guarded and batched. | — |
| [`tests/unit/test_one_cutoff_per_table.py`](../../tests/unit/test_one_cutoff_per_table.py) | 276 | SIM-551 — one cutoff per profile table. | — |
| [`tests/unit/test_openapi_snapshot.py`](../../tests/unit/test_openapi_snapshot.py) | 188 | The frontend's API snapshot matches the API (the API snapshot rule, SIM-573). | — |
| [`tests/unit/test_out_label.py`](../../tests/unit/test_out_label.py) | 277 | SIM-501a/c — the events-based out label, and the guard on `outs_on_pitch`. | — |
| [`tests/unit/test_outfield_arm_block.py`](../../tests/unit/test_outfield_arm_block.py) | 636 | SIM-550 — the outfield arm block, rebuilt from our own advancement pool. | — |
| [`tests/unit/test_outfield_arm_model.py`](../../tests/unit/test_outfield_arm_model.py) | 1,067 | Tests of the fielder engine's outfield arm group and its calibration. | — |
| [`tests/unit/test_outfield_arm_recompute.py`](../../tests/unit/test_outfield_arm_recompute.py) | 908 | The outfield arm block's recompute script and the fit probe's fielder read. | — |
| [`tests/unit/test_outs_identity.py`](../../tests/unit/test_outs_identity.py) | 306 | Every out on a pitcher's line: the identity over whole games. | — |
| [`tests/unit/test_pair_accuracy.py`](../../tests/unit/test_pair_accuracy.py) | 174 | Pairing two accuracy-comparison reports (scripts/pair_accuracy.py). | — |
| [`tests/unit/test_park_factor_snapshot.py`](../../tests/unit/test_park_factor_snapshot.py) | 220 | Tests that the app holds no handle on the statistics DuckDB file. | — |
| [`tests/unit/test_park_factors.py`](../../tests/unit/test_park_factors.py) | 482 | Unit tests for SIM-336 (park-factor UNPIVOT/factor_overall fix + real L/R splits + pool-neutralization policy) and SIM-345 (data-layer fixes: incremental watermark `>=` + row-count guard, canonical cross-pool recency_... | — |
| [`tests/unit/test_park_geometry_builder.py`](../../tests/unit/test_park_geometry_builder.py) | 870 | The fence certification (SIM-478/479/480; the plan at docs/audit/2026-09-20-sim478-480-fence-certification-plan.md, §4 and §5.1): the version-2 park-geometry builder's pure pieces on toy numpy balls (no DuckDB), and t... | — |
| [`tests/unit/test_persistent_process_pool.py`](../../tests/unit/test_persistent_process_pool.py) | 286 | The **persistent (warm) ProcessPool + shared-memory lifecycle** of :class:`simulation.batch_runner.BatchRunner` for the long-lived API. | — |
| [`tests/unit/test_pg_backup_script.py`](../../tests/unit/test_pg_backup_script.py) | 500 | The scheduled Postgres backup and its restore (db/backup). | — |
| [`tests/unit/test_pitch_cell_index.py`](../../tests/unit/test_pitch_cell_index.py) | 432 | The pitch-draw CELL INDEX (plan: docs/audit/2026-09-04-sim467-518-plan.md §5). | — |
| [`tests/unit/test_pitch_pitch_engine.py`](../../tests/unit/test_pitch_pitch_engine.py) | 274 | The pitch-to-pitch similarity engine (a FAISS nearest-neighbour index). | — |
| [`tests/unit/test_pitch_result_split.py`](../../tests/unit/test_pitch_result_split.py) | 729 | Tests of the PITCH / PITCH-RESULT split (the play-picker redesign, SIM-523). | — |
| [`tests/unit/test_pitch_result_vocabulary.py`](../../tests/unit/test_pitch_result_vocabulary.py) | 784 | The Data Lab reads each pitch result from the pitch-result code (the Data Lab vocabulary fix, SIM-571). | — |
| [`tests/unit/test_pitcher_decisions.py`](../../tests/unit/test_pitcher_decisions.py) | 328 | Tests of the winning / losing / save pitcher attribution. | — |
| [`tests/unit/test_pitcher_engine_calibration.py`](../../tests/unit/test_pitcher_engine_calibration.py) | 396 | The pitcher engine's arsenal calibration and its no-arsenal path. | — |
| [`tests/unit/test_pitcher_outs_credit.py`](../../tests/unit/test_pitcher_outs_credit.py) | 479 | Every out on a pitcher's line (the plan's tests 1 to 18). | — |
| [`tests/unit/test_pitcher_sim_matrix.py`](../../tests/unit/test_pitcher_sim_matrix.py) | 233 | tests/unit/test_pitcher_sim_matrix.py — the dense pitcher similarity matrix. | — |
| [`tests/unit/test_pitcher_similarity.py`](../../tests/unit/test_pitcher_similarity.py) | 895 | Tests of the pitcher engine (`similarity/engines/pitcher_similarity.py`) and its arsenal cache (`similarity/engines/arsenal_cache.py`). | — |
| [`tests/unit/test_pitcher_similarity_contract.py`](../../tests/unit/test_pitcher_similarity_contract.py) | 87 | The pitcher engine's shape contract: `_score_pair` returns three scores (composite, arsenal, command), `SimilarityResult` carries no release-point score (the release point lives inside the GMM components), and `Arsena... | — |
| [`tests/unit/test_pitcher_steal_similarity.py`](../../tests/unit/test_pitcher_steal_similarity.py) | 137 | The pitcher steal-prevention similarity engine's scores and its group weights. | — |
| [`tests/unit/test_play_events.py`](../../tests/unit/test_play_events.py) | 456 | SIM-502: the non-pitch play events `raw.pitches` cannot represent. | — |
| [`tests/unit/test_play_pitcher_decisions.py`](../../tests/unit/test_play_pitcher_decisions.py) | 767 | The play names its own pitcher, and the win module reads it. | — |
| [`tests/unit/test_play_recorder.py`](../../tests/unit/test_play_recorder.py) | 176 | Tests of simulation/play_recorder.py: the capture of ONE game's ordered PlayResult stream, without changing the loop. | — |
| [`tests/unit/test_play_snapshots.py`](../../tests/unit/test_play_snapshots.py) | 311 | The field/baserunner state and pitch-level play-by-play snapshot contracts in `simulation.snapshots`. | — |
| [`tests/unit/test_player_profile_computor.py`](../../tests/unit/test_player_profile_computor.py) | 979 | Covers: - Module-level constants & dataclass-like config - Pure math/physics helpers (compute_leverage_index, _hc_to_feet, _euclidean_hc, _throw_time, _estimate_hang_time, _estimate_gb_travel_time, _classify_direction... | — |
| [`tests/unit/test_player_stat_lines.py`](../../tests/unit/test_player_stat_lines.py) | 364 | Tests of the per-player stat lines the state machine keeps. | — |
| [`tests/unit/test_point_in_time_batter.py`](../../tests/unit/test_point_in_time_batter.py) | 260 | SIM-534 — point-in-time batter profiles, and the guards against leakage. | — |
| [`tests/unit/test_point_in_time_pitcher_manager.py`](../../tests/unit/test_point_in_time_pitcher_manager.py) | 242 | SIM-537 — point-in-time pitcher and manager profiles. | — |
| [`tests/unit/test_point_in_time_runner_catcher_fielder.py`](../../tests/unit/test_point_in_time_runner_catcher_fielder.py) | 561 | SIM-537 — point-in-time baserunner, catcher, and fielder profiles. | — |
| [`tests/unit/test_pool_chain.py`](../../tests/unit/test_pool_chain.py) | 835 | The count chain of the pitch pool and the label check. | — |
| [`tests/unit/test_pool_cutoff.py`](../../tests/unit/test_pool_cutoff.py) | 284 | The play pools must not offer plays that had not happened yet (the cutoff). | — |
| [`tests/unit/test_pool_index_prune.py`](../../tests/unit/test_pool_index_prune.py) | 144 | Unit test for migration 0005, which prunes the DuckDB sim-pool indexes. | — |
| [`tests/unit/test_pool_index_reconcile.py`](../../tests/unit/test_pool_index_reconcile.py) | 213 | Unit test for migration 0006, which reconciles the DuckDB sim-pool indexes to the play-pool query contract (SIM-111, section 6.2) and adds `stand`-bearing composites. | — |
| [`tests/unit/test_pool_pull_relative_spray.py`](../../tests/unit/test_pool_pull_relative_spray.py) | 237 | Unit tests for SIM-051 — handedness-corrected `pull_relative_spray_angle` on `sim.outcome_pool`, as corrected by SIM-440. | — |
| [`tests/unit/test_pool_recency_and_incremental_rebuild.py`](../../tests/unit/test_pool_recency_and_incremental_rebuild.py) | 209 | Unit tests for SIM-076 (recency_weight + pool_build_metadata) and SIM-095 (incremental pool rebuild) helpers. | — |
| [`tests/unit/test_probabilistic_backtester.py`](../../tests/unit/test_probabilistic_backtester.py) | 324 | The calibration and accuracy metrics of the probabilistic backtester. | — |
| [`tests/unit/test_profile_code_sets.py`](../../tests/unit/test_profile_code_sets.py) | 215 | SIM-501: the profile SQL's pitch-code sets must agree with the pool build. | 1: `test_out_label.py` |
| [`tests/unit/test_profile_data_additions.py`](../../tests/unit/test_profile_data_additions.py) | 229 | SIM-523 part G — the DATA additions (plan part G): | — |
| [`tests/unit/test_profile_index_recreate.py`](../../tests/unit/test_profile_index_recreate.py) | 99 | Unit tests for the SIM-419 hardening of the DuckDB profile-rebuild index recreate. | — |
| [`tests/unit/test_projections_on_pool.py`](../../tests/unit/test_projections_on_pool.py) | 369 | The game page's projections run on the worker pool and name each player. | — |
| [`tests/unit/test_prop_distributions.py`](../../tests/unit/test_prop_distributions.py) | 326 | The **prop-distribution aggregator** :mod:`simulation.prop_distributions`. | — |
| [`tests/unit/test_prop_wrappers.py`](../../tests/unit/test_prop_wrappers.py) | 382 | The seven prop markets beyond the eight core props in :mod:`simulation.prop_distributions` (the fifteen prop markets, SIM-421). | — |
| [`tests/unit/test_published_lineup_writer.py`](../../tests/unit/test_published_lineup_writer.py) | 392 | SIM-519 Part B — the published-lineup writer. | — |
| [`tests/unit/test_read_only_routes.py`](../../tests/unit/test_read_only_routes.py) | 449 | The Data Lab read routes run only on the read-only pool. | — |
| [`tests/unit/test_realism_consumers.py`](../../tests/unit/test_realism_consumers.py) | 315 | The realism CONSUMERS of the batted-ball draw (flag-on mechanism tests). | — |
| [`tests/unit/test_receiving_kernel_consumers.py`](../../tests/unit/test_receiving_kernel_consumers.py) | 212 | The got-away pitch: the sampler's read of the drawn row and its resolution in the loop. | — |
| [`tests/unit/test_receiving_ratio.py`](../../tests/unit/test_receiving_ratio.py) | 401 | Tests of the catcher RECEIVING ratio on taken pitches, which ships OFF. | — |
| [`tests/unit/test_recency_walk_forward.py`](../../tests/unit/test_recency_walk_forward.py) | 208 | The recency walk-forward validation harness. | — |
| [`tests/unit/test_release_speed_bounds.py`](../../tests/unit/test_release_speed_bounds.py) | 154 | The release-speed bounds: the historical loader's validator and the database trigger both accept 50 to 110 mph (a slow curve, an eephus, the real high-velocity tail) and flag anything outside it. | — |
| [`tests/unit/test_relief_draw.py`](../../tests/unit/test_relief_draw.py) | 700 | Tests of each team's real manager for the pitching change. | — |
| [`tests/unit/test_replay_chi_squared.py`](../../tests/unit/test_replay_chi_squared.py) | 348 | Tests of the historical-replay harness and its chi-squared goodness-of-fit. | — |
| [`tests/unit/test_replay_store.py`](../../tests/unit/test_replay_store.py) | 570 | The game page's linescore and play-by-play show a simulated game. | — |
| [`tests/unit/test_rescore_graded_book.py`](../../tests/unit/test_rescore_graded_book.py) | 343 | `scripts/probes/rescore_graded_book.py`: the re-pricing of a tiny fake report. | — |
| [`tests/unit/test_rescore_runlines.py`](../../tests/unit/test_rescore_runlines.py) | 280 | The re-score of stored accuracy reports (`scripts/probes/rescore_runlines.py`). | — |
| [`tests/unit/test_retire_consensus_odds.py`](../../tests/unit/test_retire_consensus_odds.py) | 318 | `scripts/probes/retire_consensus_odds.py`: the refusal, the dry run and the archive-then-delete, on a stub connection (never a real database). | — |
| [`tests/unit/test_retry_wrapper.py`](../../tests/unit/test_retry_wrapper.py) | 84 | The crash-retry wrapper (`scripts/with_retry.sh`). | — |
| [`tests/unit/test_rng_streams.py`](../../tests/unit/test_rng_streams.py) | 279 | Tests that the loop rng and the full-pool rng are INDEPENDENT streams. | — |
| [`tests/unit/test_route_auth_coverage.py`](../../tests/unit/test_route_auth_coverage.py) | 321 | The API requires a login on every data route, in every environment. | — |
| [`tests/unit/test_run_ledger.py`](../../tests/unit/test_run_ledger.py) | 717 | The run-value ledger is TOLD its two base-out states. | — |
| [`tests/unit/test_run_resolution.py`](../../tests/unit/test_run_resolution.py) | 232 | The Statcast events vocabulary and the RE24 run resolution. | — |
| [`tests/unit/test_runner_run_credit.py`](../../tests/unit/test_runner_run_credit.py) | 383 | The per-runner R credit on a terminal pitch (the prop-market work, SIM-421). | — |
| [`tests/unit/test_running_game_census.py`](../../tests/unit/test_running_game_census.py) | 405 | The running-game census (`scripts/probes/running_game_census.py`): it reads where the simulator's steals and pickoffs land, two arms side by side. | — |
| [`tests/unit/test_running_game_on_the_pitch.py`](../../tests/unit/test_running_game_on_the_pitch.py) | 1,134 | The running game on the pitch: the loop's order and its rules. | — |
| [`tests/unit/test_running_game_replay.py`](../../tests/unit/test_running_game_replay.py) | 281 | The running-game replay's arithmetic, on synthetic inputs (no DB, no bundle). | — |
| [`tests/unit/test_sampler_hot_path_cache.py`](../../tests/unit/test_sampler_hot_path_cache.py) | 165 | tests/unit/test_sampler_hot_path_cache.py — the sampler's per-PA hot-path cache. | — |
| [`tests/unit/test_savant_loader.py`](../../tests/unit/test_savant_loader.py) | 285 | SIM-528 — the shared Baseball Savant loader. | — |
| [`tests/unit/test_savant_oaa_jump_boards.py`](../../tests/unit/test_savant_oaa_jump_boards.py) | 427 | SIM-532 — the loader side of the fielder's hand split and the outfield jump. | — |
| [`tests/unit/test_schedule_client.py`](../../tests/unit/test_schedule_client.py) | 201 | SIM-519 Part A — the schedule client over recorded league responses. | — |
| [`tests/unit/test_schema_pitch_indexes.py`](../../tests/unit/test_schema_pitch_indexes.py) | 114 | The indexes on `raw.pitches`, in the canonical schema and in their migrations: the composite situation index (the situation similarity engine), the (pitcher, season) clean-row index (the profile computor's hot path),... | — |
| [`tests/unit/test_script_flag_names.py`](../../tests/unit/test_script_flag_names.py) | 67 | Every `SIM_*` name the operational scripts use is a registered flag (`simulation/flags.py`). | — |
| [`tests/unit/test_secrets_baseline.py`](../../tests/unit/test_secrets_baseline.py) | 79 | The secrets baseline: `.env.example` documents the required variables, `.gitignore` keeps `.env` out of git, `python-dotenv` is a requirement, the API refuses to boot without its variables, CI rejects a committed `.en... | — |
| [`tests/unit/test_segment_edges.py`](../../tests/unit/test_segment_edges.py) | 708 | The twelve segment and team markets on /edges and /signals. | — |
| [`tests/unit/test_segment_markets.py`](../../tests/unit/test_segment_markets.py) | 612 | The simulator prices every game market the book posts. | — |
| [`tests/unit/test_shared_memory_attach.py`](../../tests/unit/test_shared_memory_attach.py) | 285 | The **shared-memory zero-copy ATTACH** of the read-only payload (the engine-artifact arrays) into worker processes: :func:`simulation.batch_runner.publish_shared_arrays` (parent) + :func:`~simulation.batch_runner._wor... | — |
| [`tests/unit/test_shell_dsn_guards.py`](../../tests/unit/test_shell_dsn_guards.py) | 67 | The nightly shell scripts carry no default DSN. | — |
| [`tests/unit/test_sim_jobs.py`](../../tests/unit/test_sim_jobs.py) | 419 | The simulation run jobs: the runner's chunked job, the registry and the routes. | 1: `test_game_page_whatif.py` |
| [`tests/unit/test_sim_kwargs.py`](../../tests/unit/test_sim_kwargs.py) | 1,497 | The shared `simulate_game` kwargs builder + the park-factor resolver. | — |
| [`tests/unit/test_sim_store.py`](../../tests/unit/test_sim_store.py) | 467 | SIM-356 -- unit tests for db/sim_store.py (durable sim-result + pitch-snapshot persistence) and its two migrations (Alembic 0014 / DuckDB 0008). | — |
| [`tests/unit/test_similarity_endpoint.py`](../../tests/unit/test_similarity_endpoint.py) | 561 | The pitcher similarity explorer endpoint (api/routes/similarity.py). | — |
| [`tests/unit/test_similarity_explorer_adapters.py`](../../tests/unit/test_similarity_explorer_adapters.py) | 81 | The Similarity Explorer's engine adapters (`api/routes/similarity_explorer.py`). | — |
| [`tests/unit/test_simulate_game_driver.py`](../../tests/unit/test_simulate_game_driver.py) | 353 | The **full-game driver** `simulate_game()` and its game-level loop control (regulation / walk-off / extra innings / ghost runner / deterministic seeding) in `simulation/sim_loop.py`. | — |
| [`tests/unit/test_simulate_game_requires_lineup.py`](../../tests/unit/test_simulate_game_requires_lineup.py) | 127 | `simulate_game` refuses a game with no batting order BEFORE the first pitch. | — |
| [`tests/unit/test_simulation_constants.py`](../../tests/unit/test_simulation_constants.py) | 125 | Unit tests for the constants module (`simulation/constants.py`). | — |
| [`tests/unit/test_situation_engine_queries.py`](../../tests/unit/test_situation_engine_queries.py) | 250 | The situation engine's KDTree queries (a KDTree is a search tree over the situation features): k results sorted by distance, batch queries equal to single ones, and the feature vector. | — |
| [`tests/unit/test_situation_metadata_store.py`](../../tests/unit/test_situation_metadata_store.py) | 396 | The columnar metadata store of the situation engine. | — |
| [`tests/unit/test_situation_sigma_env.py`](../../tests/unit/test_situation_sigma_env.py) | 111 | The pitch draw's situation bandwidth has an environment variable. | — |
| [`tests/unit/test_situation_similarity.py`](../../tests/unit/test_situation_similarity.py) | 531 | The situation engine, similarity/engines/situation_similarity.py. | — |
| [`tests/unit/test_situational_decisions.py`](../../tests/unit/test_situational_decisions.py) | 135 | Whole games with and without a manager profile complete validly. | — |
| [`tests/unit/test_slate_card_odds.py`](../../tests/unit/test_slate_card_odds.py) | 189 | The card-odds route (`api/routes/betting_card.py`). | — |
| [`tests/unit/test_slate_endpoint.py`](../../tests/unit/test_slate_endpoint.py) | 394 | The schedule-driven slate route and the /status card (`api/routes/games_slate.py`). | — |
| [`tests/unit/test_smoke_outs_gate.py`](../../tests/unit/test_smoke_outs_gate.py) | 267 | The outs check in the smoke harness (tests 29 and 30 of the pitcher-outs plan, `docs/audit/2026-10-04-sim557-pitcher-outs-credit-plan.md`). | — |
| [`tests/unit/test_sql_console_pool.py`](../../tests/unit/test_sql_console_pool.py) | 239 | The SQL console and the assistant run only on the read-only pool. | — |
| [`tests/unit/test_sql_safety.py`](../../tests/unit/test_sql_safety.py) | 575 | The read-only SQL guard of `api/routes/sql_safety.py`. | — |
| [`tests/unit/test_starter_resolution.py`](../../tests/unit/test_starter_resolution.py) | 438 | Tests that each side's starting pitcher comes from the official box, and that a game with a missing starter is refused (the real starter on both sides, SIM-558). | — |
| [`tests/unit/test_starting_position.py`](../../tests/unit/test_starting_position.py) | 359 | SIM-559: the lineup loader stores each starter's STARTING position (`allPositions[0]`), not the last position he held (`position`). | — |
| [`tests/unit/test_state_machine.py`](../../tests/unit/test_state_machine.py) | 310 | The GameState-driven plate-appearance / half-inning **state machine** (:class:`simulation.sim_loop.StateMachine`). | — |
| [`tests/unit/test_steal_draw.py`](../../tests/unit/test_steal_draw.py) | 767 | The steal opportunity pool and the steal draw. | — |
| [`tests/unit/test_steal_labels.py`](../../tests/unit/test_steal_labels.py) | 168 | SIM-506 — the steal labels read BOTH homes of a steal outcome. | — |
| [`tests/unit/test_steal_pool_data.py`](../../tests/unit/test_steal_pool_data.py) | 831 | SIM-554 — the data under the running game on the pitch. | — |
| [`tests/unit/test_steal_pool_rebuild.py`](../../tests/unit/test_steal_pool_rebuild.py) | 1,133 | The steal-pool rebuild (`scripts/rebuild_steal_pool.py` and its steps in `scripts/steal_pool_rebuild/`), on in-memory fixtures (no live DB, no bundle). | — |
| [`tests/unit/test_steal_run_credit.py`](../../tests/unit/test_steal_run_credit.py) | 125 | Tests that a steal run earns no RBI (Rule 9.04(b)) and stays EARNED (9.16(a)). | — |
| [`tests/unit/test_steal_weight_volume_probe.py`](../../tests/unit/test_steal_weight_volume_probe.py) | 116 | The steal-weight volume probe's arithmetic, on synthetic inputs (no DB, no bundle). | — |
| [`tests/unit/test_steal_weights_seam.py`](../../tests/unit/test_steal_weights_seam.py) | 99 | The steal draw's weights as a seam. | — |
| [`tests/unit/test_sweep_streaming.py`](../../tests/unit/test_sweep_streaming.py) | 198 | `scripts/resumable_sweep.py` must STREAM its child's output. | — |
| [`tests/unit/test_synthetic_bundle.py`](../../tests/unit/test_synthetic_bundle.py) | 229 | The in-memory bundle every no-DB path draws from (`simulation/synthetic_bundle.py`). | — |
| [`tests/unit/test_transition_draw.py`](../../tests/unit/test_transition_draw.py) | 502 | The transition fielding draw + the five-scenario advancement draws (the owner's rulings of 2026-08-19). | — |
| [`tests/unit/test_transition_pool.py`](../../tests/unit/test_transition_pool.py) | 753 | SIM-510 — transition columns on sim.outcome_pool + the advancement opportunity pool. | — |
| [`tests/unit/test_trusted_proxy.py`](../../tests/unit/test_trusted_proxy.py) | 167 | The rate limiter behind nginx counts a request against the real client's address. | — |
| [`tests/unit/test_uncaught_strike_three.py`](../../tests/unit/test_uncaught_strike_three.py) | 218 | SIM-517 part A — the uncaught-strike-3 slice of the catcher receiving profile (`PlayerProfileComputor._compute_catcher_uncaught_k3`). | — |
| [`tests/unit/test_validate_props.py`](../../tests/unit/test_validate_props.py) | 529 | Validate prop PMFs (a probability for each count) and fit the win-probability reliability curve over synthetic, deterministic outcomes; derive real prop actuals from per-PA `events`. | — |
| [`tests/unit/test_venue_id_fallback.py`](../../tests/unit/test_venue_id_fallback.py) | 143 | A game with no venue is stored with `venue_id` NULL, never a 0 sentinel: `raw.games.venue_id` is nullable (migration 0006), the live pipeline's `_upsert_game_record` binds None when the feed has no venue, and the venu... | — |
| [`tests/unit/test_venue_resolution.py`](../../tests/unit/test_venue_resolution.py) | 162 | SIM-447: venue resolution for games whose feed omits `gameData.venue`. | — |
| [`tests/unit/test_weighted_rbf_kernel.py`](../../tests/unit/test_weighted_rbf_kernel.py) | 69 | The shared `WeightedRBFSimilarity` kernel, an RBF kernel (radial basis function: a similarity that falls off with the weighted distance between two profiles): an identical pair scores 1, every score is in [0, 1], the... | — |
| [`tests/unit/test_win_probability.py`](../../tests/unit/test_win_probability.py) | 336 | Tests of the calibrated game win probability, :func:`simulation.win_probability.win_probability`. | — |
| [`tests/unit/test_worker_forkserver.py`](../../tests/unit/test_worker_forkserver.py) | 51 | tests/unit/test_worker_forkserver.py — the worker pool's start method. | — |
| [`tests/unit/test_worker_prewarm.py`](../../tests/unit/test_worker_prewarm.py) | 293 | The worker contracts the cold-worker `/simulate` speed rests on. | — |
| [`tests/integration/__init__.py`](../../tests/integration/__init__.py) | 7 | Package marker. | — |
| [`tests/integration/conftest.py`](../../tests/integration/conftest.py) | 274 | Provides session-scoped testcontainers fixtures for PostgreSQL 15 and Redis 7. | 2: `test_etl_flow.py`, `test_schema_migrations.py` |
| [`tests/integration/test_api_end_to_end.py`](../../tests/integration/test_api_end_to_end.py) | 670 | The API + WebSocket + historical-replay END-TO-END test suite. | — |
| [`tests/integration/test_batch_runner_stress.py`](../../tests/integration/test_batch_runner_stress.py) | 460 | The stress / concurrency / leak suite for the persistent BatchRunner and the /simulate request path. | — |
| [`tests/integration/test_etl_flow.py`](../../tests/integration/test_etl_flow.py) | 443 | Verifies the end-to-end write path for the ETL freshness tracking system against the *canonical* schema created by Alembic migration 0003: | — |
| [`tests/integration/test_live_game_upsert.py`](../../tests/integration/test_live_game_upsert.py) | 232 | Drives the real `LiveIngestionPipeline._upsert_game_record()` (pipeline/live/live_ingestion_pipeline.py) against the migrated schema. | — |
| [`tests/integration/test_live_pipeline_upsert.py`](../../tests/integration/test_live_pipeline_upsert.py) | 265 | Drives the real `LiveIngestionPipeline._upsert_lineup_state()` (pipeline/live/live_ingestion_pipeline.py) against the migrated schema: | — |
| [`tests/integration/test_schema_migrations.py`](../../tests/integration/test_schema_migrations.py) | 493 | Verifies that `alembic upgrade head` produces the complete production schema in a fresh PostgreSQL 15 database. | — |
| [`tests/performance/__init__.py`](../../tests/performance/__init__.py) | 1 | Performance benchmark suite. | — |
| [`tests/performance/bench_api_simulate.py`](../../tests/performance/bench_api_simulate.py) | 343 | The end-to-end `GET /api/games/{game_pk}/simulate` latency perf gate. | — |
| [`tests/performance/bench_simulation.py`](../../tests/performance/bench_simulation.py) | 635 | The performance benchmark harness. | — |
| [`tests/fixtures/mlb_game_feed/record_fixtures.py`](../../tests/fixtures/mlb_game_feed/record_fixtures.py) | 203 | Record trimmed league game feeds for the tests that parse the feed. | — |
| [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) | 750 | MLB Baseball Simulation Platform — CI Pipeline (SIM-146) | — |
| [`.github/workflows/acceptance-nightly.yml`](../../.github/workflows/acceptance-nightly.yml) | 343 | MLB Baseball Simulation Platform — Production-config acceptance bands (SIM-450) | — |
| [`.github/workflows/docker-release.yml`](../../.github/workflows/docker-release.yml) | 89 | MLB Baseball Simulation Platform — Docker Release (SIM-146) | — |
| [`.github/workflows/frontend-release.yml`](../../.github/workflows/frontend-release.yml) | 84 | MLB Baseball Simulation Platform — Frontend Docker Release (SIM-401) | — |
| [`.github/workflows/integration-weekly.yml`](../../.github/workflows/integration-weekly.yml) | 81 | MLB Baseball Simulation Platform — Weekly Integration Tests (SIM-146) | — |
| [`.github/workflows/perf-weekly.yml`](../../.github/workflows/perf-weekly.yml) | 110 | MLB Baseball Simulation Platform — Weekly Performance Benchmarks (SIM-118) | — |
