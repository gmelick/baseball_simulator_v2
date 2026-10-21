# Similarity Engines

The `similarity/` package holds the eleven similarity engines. Each engine measures how alike
two things are: two players, two game states, two pitches or two batted balls. The nightly
bundle build turns the player engines' scores into score matrices, and the simulator's draws
read those matrices as weights. The app also builds all eleven engines at boot for the
Similarity Explorer. The package also holds the calibration fit (`make calibrate`), the
engines' sanity checks and an offline backtesting library.

## The eleven engines

A "power" below is the exponent the simulator puts on a score before it multiplies the
score into a draw weight. Every power is 1 in production (owner ruling 2026-09-16: every
similarity power stays 1 until the comprehensive sweep of the tunable parameters fits them
together). The flag defaults live in [flags.py](../../simulation/flags.py); the production
values live in the `app` service of [docker-compose.yml](../../docker-compose.yml).

| Engine (module) | What it compares, and how | Feature groups (weight) | The draw it feeds today |
|---|---|---|---|
| Pitcher ([pitcher_similarity.py](../../similarity/engines/pitcher_similarity.py)) | Two pitcher-seasons of the same throwing hand. The arsenal score is the W₂ distance (the Wasserstein-2 distance: the least cost to move one pitch mixture onto the other) between two Gaussian mixture models. The command score is an RBF kernel (a score that falls off as a bell curve with distance). | Arsenal 0.65 (velocity, induced vertical break, horizontal break, spin rate, spin axis, release x, release z, extension); command 0.35 (seven plate-discipline rates) | The pitch draw (`SIM_PITCH_PITCHER_POWER`, default 1), the pitch-result draw (`SIM_RESULT_PITCHER_POWER`, default 1), the pitching-change draw (`SIM_CHANGE_PITCHER_POWER`, default 1) and the reliever draw's "stuff" weight (`SIM_RELIEF_PITCHER_POWER`, default 0 = off; production 1). Matrix: `pitcher_sim.npz`. |
| Batter ([batter_similarity.py](../../similarity/engines/batter_similarity.py)) | Two batter-seasons, one pool for every batter, RBF kernels. A bats penalty separates a left-handed batter from a right-handed one. | Discipline 0.32, batted ball 0.28, platoon 0.12, power 0.08, physical swing and stance 0.20 | The pitch draw (`SIM_ACTOR_POWER_BATTER` and `SIM_PITCH_BATTER_POWER`), the pitch-result draw (`SIM_RESULT_BATTER_POWER`) and the fielding draw (`SIM_BB_BATTER_POWER`); all default 1. Matrix: `batter`. |
| Fielder ([fielder_similarity.py](../../similarity/engines/fielder_similarity.py)) | Two fielder-seasons at the same position only, RBF kernels. | Infield: range 0.45, double play 0.30, errors 0.15, specialty 0.10. Outfield: range 0.40, arm 0.30, star plays 0.15, errors 0.15 | The fielding draw and the advancement draws (`SIM_ACTOR_POWER_FIELDER`, default 1; `SIM_ACTOR_POWER_FIELDER_<POS>` overrides one position). Matrices: `fielder_1B` … `fielder_RF`. |
| Advancement runner ([baserunner_similarity.py](../../similarity/engines/baserunner_similarity.py)) | Two runner-seasons on taking the extra base on a ball in play, with a masked RBF kernel. | Speed 0.35, aggression 0.40, success 0.25 | The advancement draws (`SIM_ACTOR_POWER_RUNNER_ADV`, default 1). Matrix: `runner_adv`. |
| Steal runner ([baserunner_steal_similarity.py](../../similarity/engines/baserunner_steal_similarity.py)) | Two runner-seasons as base stealers, RBF kernels. | Tendency 0.45, lead 0.45, success 0.10 | The pickoff draw and the steal draw (`SIM_ACTOR_POWER_RUNNER_STEAL`, default 1). Matrix: `runner_steal`. |
| Catcher ([catcher_similarity.py](../../similarity/engines/catcher_similarity.py)) | Two catcher-seasons on defense, RBF kernels. | Framing ≈0.529, blocking ≈0.235, throwing ≈0.141, deterrence ≈0.094 | The throwing sub-score alone feeds the pickoff and steal draws (`SIM_ACTOR_POWER_CATCHER_THROWING`, default 1; matrix `catcher_throwing`). The build also writes the composite as the matrix `catcher`; no draw reads it. |
| Pitcher against the run ([pitcher_steal_similarity.py](../../similarity/engines/pitcher_steal_similarity.py)) | Two pitcher-seasons on holding runners, RBF kernels. | Outcome 0.35, hold 0.65 | The pickoff draw and the steal draw (`SIM_ACTOR_POWER_PITCHER_STEAL`, default 1). Matrix: `pitcher_steal`. |
| Manager ([manager_similarity.py](../../similarity/engines/manager_similarity.py)) | Two manager-seasons, RBF kernels. | Pitcher usage 0.40, offensive aggressiveness 0.35, platoon 0.25 | The usage sub-score alone weights the pitching-change draw (`SIM_ACTOR_POWER_MANAGER_USAGE`, default 0 = off; production 1). Matrix: `manager_usage`. |
| Situation ([situation_similarity.py](../../similarity/engines/situation_similarity.py)) | Game states before a plate appearance, by a KD-tree (a search tree that splits the space one feature at a time). It returns a distance, not a 0–1 score. | Eleven state features (inning, half, outs, three runner flags, score difference, leverage, pitch count, plate appearances, park run factor) | No draw. The Situation Finder page reads it. |
| Pitch-to-pitch ([pitch_pitch_similarity.py](../../similarity/engines/pitch_pitch_similarity.py)) | Single pitches by their physics, in a FAISS index (a library for fast nearest-neighbour search). It returns a distance. | Ten physics features (the eight arsenal dimensions plus plate x and plate z) | No draw calls the engine. The pitch-result draw scores pitch likeness with a copy of this engine's feature weights (`_PITCH_FEATURE_WEIGHTS` in [kernels.py](../../simulation/sampler/kernels.py); a unit test pins the copy). |
| Batted ball ([batted_ball_similarity.py](../../similarity/engines/batted_ball_similarity.py)) | Batted balls by their launch, in a FAISS index. It returns a distance. | Exit velocity, launch angle, pull-relative spray angle | No draw. The fielding draw scores the batted-ball pool itself. |

## How it fits together

The simulator never queries an engine during a game. A score reaches a draw through files on
disk:

1. The nightly bundle build runs `python -m pipeline.batch.engine_artifacts --what all`
   ([engine_artifacts.py](../../pipeline/batch/engine_artifacts.py); `make engine-artifacts`).
   It builds each engine fresh from DuckDB over the bundle's seasons: the four newest
   seasons of `sim.pitch_pool` (`RECENCY_FLOOR_SEASONS`).
2. `build_pitcher_sim_matrix` ([pitcher_sim.py](../../pipeline/batch/artifacts/pitcher_sim.py))
   queries the pitcher engine once per profile. It writes the dense float32
   `pitcher_sim_matrix` into `pitcher_sim.npz`; a pair the engine did not score holds 0.0.
3. `build_actor_sim_matrices` (engine_artifacts.py) queries seven engines the same way and
   writes one matrix per role into `actor_sim/`: `manager_usage`, `batter`, `catcher`,
   `catcher_throwing`, `runner_steal`, `runner_adv`, `pitcher_steal` and one `fielder_<POS>`
   per position. A key is `id:season` (a fielder's is `id:POS:season`). The diagonal is
   1.0, and an unscored pair is NaN. The two runner engines also score their thin
   profiles (`include_below_minimum=True`), so the matrices cover every runner the pools
   hold.
4. The same step writes `concentration.json`. For each catcher and fielder it compares the
   draw weight his matrix row puts on his own team's rows with that team's share of the
   pool. A p90 ratio above 3.0 (`CONCENTRATION_MAX_RATIO`) fails a build run with
   `--strict-concentration`; otherwise it logs a warning.
5. Each simulation worker loads the bundle. The sampler gathers the live player's matrix
   row onto the player of each candidate pool row (`_matrix_gather` and `_actor_factor` in
   [sampler/base.py](../../simulation/sampler/base.py); `_f_pitcher` in
   [sampler/pitch.py](../../simulation/sampler/pitch.py)). It raises the scores to the
   factor's power and multiplies them into the row weights.
6. The power rules: at power 1 the sampler uses the raw score and skips the power step. A
   power of 0 turns the factor off. At power 1 an unscored row weighs 1.0; at any other
   power it weighs the mean of the scored rows, so it stays draw-neutral.
7. The build applies no calibration report. The matrices use each engine's module
   constants, so a refitted value reaches the simulator only when someone copies it into
   the constant. The app applies `/data/calibration.json` to its own engines at boot
   (`load_calibration` in [startup.py](../../api/startup.py) calls
   `apply_calibration_to_engines` in [state.py](../../api/state.py)).

The app path: the lifespan builds the pitcher engine as `app.state.pitcher_engine` for the
pitcher distribution route, and all eleven engines as `app.state.engines` (`build_all_engines`
and `ENGINE_REGISTRY` in [state.py](../../api/state.py)). An engine that fails to build is
logged and left out. The Similarity Explorer ([similarity_visualization_spec.md](../similarity_visualization_spec.md))
reads both.

The calibration path: `make calibrate` runs [fit_calibration.py](../../scripts/fit_calibration.py),
which calls `SimilarityCalibrator.calibrate_from_population` and writes
`/data/calibration.json`. The file reaches only the app's engines (step 7).

## The shared shape of a profile engine

The eight player and manager engines share one design:

- **One profile per player and season**, read from a `derived.*` table. The load skips a row
  that the profile computor flagged `below_minimum_sample`, unless the build asks for thin
  profiles.
- **One cutoff per table** (SIM-551). Each engine reads the table's `asof_date` (the date the
  data runs through), refuses a table stamped at two dates, and exposes the date as
  `asof_date`.
- **Empirical-Bayes shrinkage** (a pull toward the season's league mean by sample size). The
  player keeps the share α = n / (n + N_PRIOR) of his own value. The league row comes from
  `derived.league_averages`.
- **Z-scored features** (each value minus the population mean, over its standard deviation).
- **Sub-scores as weighted RBF kernels**: exp(−Σ wᵢ·dᵢ² / 2σ²), with the reliability weights
  wᵢ normalized to sum to 1 ([rbf_kernel.py](../../similarity/engines/rbf_kernel.py)).
- **The composite**: the weighted sum of the sub-scores, times the pair confidence
  √min(α_a, α_b), clipped to [0, 1]. A thin profile scores low against everyone.
- **`query(...)`** scores one profile against every profile in its partition and leaves out
  only the exact query key; the player's other seasons stay in. **`query_pair(a, b)`**
  scores one pair.
- **`apply_calibration(report)`** reads the engine's fields of the report. A field at 0.0 or
  None keeps the module default.

## Calibration

### `similarity/similarity_calibration.py`

[Source](../../similarity/similarity_calibration.py) · 127 lines · Tests: [test_calibration_fit_and_apply.py](../../tests/unit/test_calibration_fit_and_apply.py), [test_calibration_reconciliation.py](../../tests/unit/test_calibration_reconciliation.py), [test_manager_usage_score.py](../../tests/unit/test_manager_usage_score.py)

`SimilarityCalibrator` fits every engine's calibrated constants from the profile population
of one DuckDB file. The fit needs no outcome data: each value comes from the spread of the
profiles. The class combines the eight mixins of `similarity/calibration/`.

| Function / class | What it does | Called from |
|---|---|---|
| `SimilarityCalibrator.calibrate_from_population(seasons, target_median_score=0.50, arsenal_w2_distances=None)` | Opens the file read-only and runs the batter, pitcher, arsenal, fielder, advancement-runner, catcher, steal-runner, pitcher-steal and manager fits in that order. Returns one `CalibrationReport`. A missing table leaves its engine's fields at 0.0. Without W₂ distances the arsenal fields stay 0.0. | `scripts/fit_calibration.py` (`make calibrate`) |

### `similarity/calibration/fitting.py`

[Source](../../similarity/calibration/fitting.py) · 375 lines · Tests: [test_calibration_reconciliation.py](../../tests/unit/test_calibration_reconciliation.py), [test_engine_calibration_regression.py](../../tests/unit/test_engine_calibration_regression.py), [test_outfield_arm_model.py](../../tests/unit/test_outfield_arm_model.py)

The population fits. Each function reads a feature matrix and returns one fitted value.

| Function / class | What it does | Called from |
|---|---|---|
| `calibrate_sigma` | Samples random pairs and returns the kernel bandwidth σ that puts the median pair at the target score (0.50). Returns `degenerate_value` when the pairs have no spread. | `CalibratorBase._fit_sigma` |
| `calibrate_arsenal_gamma` | The γ of exp(−γ·W₂²) that puts the median pitcher pair at the target; 0.173 when no distance is finite. | `PitcherCalibratorMixin._calibrate_arsenal_params` |
| `calibrate_eb_prior` | The shrinkage prior strength: the median ratio of within-player to between-player variance, clamped to [5, 200]; 50 with fewer than 20 rows. | the batter, pitcher and advancement-runner mixins |
| `calibrate_reliability_weights` | One weight per feature: its correlation between consecutive seasons of one player, clamped to [0.1, 1]. A fielder id carries the position, so a pair never crosses positions. | the batter, fielder and advancement-runner mixins |
| `keep_defaults_on_thin_entries` / `reliable_pair_count` | A feature that fewer than `RELIABILITY_MIN_PAIRS` (20) season pairs measure keeps its engine default weight and logs a warning. | `FielderCalibratorMixin` |

### `similarity/calibration/report.py`

[Source](../../similarity/calibration/report.py) · 344 lines · Tests: [test_calibration_report_json.py](../../tests/unit/test_calibration_report_json.py), [test_calibration_fit_and_apply.py](../../tests/unit/test_calibration_fit_and_apply.py), [test_validate_props.py](../../tests/unit/test_validate_props.py)

`CalibrationReport`, the dataclass of every fitted value. It also carries the
win-probability reliability curve, which the prop validation writes.

| Function / class | What it does | Called from |
|---|---|---|
| `CalibrationReport` | The sigmas, priors, reliability weights, arsenal anchor (`arsenal_gamma`, `arsenal_median_w2`), the reliability curve and the fit's metadata. A field at 0.0 or None means "keep the default". | `api.state.load_calibration_report`, every engine's `apply_calibration`, `simulation.win_probability.CalibrationMap.from_report`, `simulation.prop_validation.write_reliability_curve_to_calibration_report` |
| `summary()` | The report as text, with the reliability blocks of `_RELIABILITY_BLOCKS`. | `scripts/fit_calibration.py` |
| `to_json` / `from_json` / `equals` | A lossless JSON form; numpy arrays are tagged. An unknown key is ignored and a missing key takes its default. | `scripts/fit_calibration.py`, `api.state.load_calibration_report` |

**Watch out:** the module imports `IF_PIVOT_FEATURES` from the fielder engine at import time,
which also runs that engine's `logging.basicConfig`.

### `similarity/calibration/base.py`

[Source](../../similarity/calibration/base.py) · 60 lines · Tests: [test_calibration_fit_and_apply.py](../../tests/unit/test_calibration_fit_and_apply.py), [test_engine_calibration_regression.py](../../tests/unit/test_engine_calibration_regression.py)

`CalibratorBase`, the helpers every mixin shares: `_has_columns` (the column probe, through
`db.introspect.duck_columns`), `_zscore_matrix` (NaN-aware) and `_fit_sigma`. `_fit_sigma`
returns 0.0, the engines' keep-the-default value, when a matrix is empty or has no spread,
so a degenerate fit never overrides a tuned default.

### `similarity/calibration/` (the per-engine mixins)

[Source](../../similarity/calibration/) · Tests: [test_calibration_fit_and_apply.py](../../tests/unit/test_calibration_fit_and_apply.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py), [test_fielder_oaa_jump_model.py](../../tests/unit/test_fielder_oaa_jump_model.py)

Each mixin re-reads its engine's profile columns with its own SQL and fits in the same
feature space the engine scores in.

| Module | Lines | What it fits |
|---|---|---|
| [`__init__.py`](../../similarity/calibration/__init__.py) | 13 | The package map; no code. |
| [`batter.py`](../../similarity/calibration/batter.py) | 195 | The five batter sigmas, the prior and the reliability weights. The platoon block fits over batters with 30 or more plate appearances against right-handers; under 20 such rows it takes the discipline sigma. The physical block fits over the rows that carry all ten features. |
| [`pitcher.py`](../../similarity/calibration/pitcher.py) | 114 | The command sigma, the prior, and the arsenal anchor from the W₂ distances the caller passes. `sigma_results` stays 0.0: the engine has no results sub-score. |
| [`fielder.py`](../../similarity/calibration/fielder.py) | 380 | The infield and outfield sigmas and reliability weights. NULL loads as NaN, and a pair is two consecutive seasons at one position. The range blocks keep the default weight of a thin feature. |
| [`baserunner.py`](../../similarity/calibration/baserunner.py) | 159 | The advancement runner's three sigmas, prior and weights. The aggression block fits over the rows that carry Savant's attempt rate above expectation. |
| [`catcher.py`](../../similarity/calibration/catcher.py) | 80 | The four catcher sigmas. |
| [`baserunner_steal.py`](../../similarity/calibration/baserunner_steal.py) | 75 | The steal runner's tendency, success and lead sigmas. The lead fits over rows with both lead features. |
| [`pitcher_steal.py`](../../similarity/calibration/pitcher_steal.py) | 74 | The outcome and hold sigmas. The hold fits over rows with both hold features. |
| [`manager.py`](../../similarity/calibration/manager.py) | 73 | The usage, aggression and platoon sigmas. |

The catcher, steal and manager engines get sigmas only; their reliability weights stay the
module constants.

**Watch out:** the mixins read columns by position after a fixed prefix. When an engine's
SELECT or feature order changes, change its mixin in the same commit, or the fit lands in
the wrong feature space with no error. The catcher mixin repeats the engine's framing and
blocking SQL expressions; keep the two in step. `scripts/fit_calibration.py` writes the
whole report file, so a run drops the win-probability curve until
`scripts/validate_props.py --write-calibration` writes it back.

## The scoring engines

### `similarity/engines/rbf_kernel.py`

[Source](../../similarity/engines/rbf_kernel.py) · 51 lines · Tests: [test_fielder_similarity.py](../../tests/unit/test_fielder_similarity.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py), [test_weighted_rbf_kernel.py](../../tests/unit/test_weighted_rbf_kernel.py)

`WeightedRBFSimilarity(sigma, reliability_weights)`, the kernel the batter, fielder, catcher,
manager and both steal engines score their sub-scores with. It normalizes the weights to sum
to 1, so the exponent is the weighted average squared distance per feature. A sub-score's
typical value then does not depend on its feature count. A NaN difference counts as zero
distance. `score(x, y)` scores one pair; `score_batch(query, candidates)` scores one query
against a matrix.

**Watch out:** the advancement-runner engine keeps its own masked kernel, which drops a
feature missing on either side. Do not merge the two.

### `similarity/engines/pitcher_similarity.py`

[Source](../../similarity/engines/pitcher_similarity.py) · 1,534 lines · Tests: [test_pitcher_similarity.py](../../tests/unit/test_pitcher_similarity.py), [test_pitcher_similarity_contract.py](../../tests/unit/test_pitcher_similarity_contract.py), [test_pitcher_engine_calibration.py](../../tests/unit/test_pitcher_engine_calibration.py), [test_arsenal_w2_standardization.py](../../tests/unit/test_arsenal_w2_standardization.py), [test_arm_angle_decision.py](../../tests/unit/test_arm_angle_decision.py)

Compares two pitcher-seasons of the same throwing hand. Each pitcher-season's arsenal is a
Gaussian mixture model (GMM: one Gaussian per pitch cluster) over eight dimensions, fitted by
the profile computor. The arsenal score is exp(−W₂ / `ARSENAL_SCALE`), with the scale 4.10 by
default (the median pair at 0.50; CLAUDE.md §10). The command score is an RBF kernel over
seven rates (walk, strikeout, called-plus-swinging-strike, zone take, chase, zone and whiff
rates). The composite is 0.65 × arsenal + 0.35 × command, times the pair confidence. The
shrinkage prior is 5 pitches, and the load keeps profiles of 200 pitches or more. Data:
`derived.pitcher_season_metrics` and `derived.league_averages`.

| Function / class | What it does | Called from |
|---|---|---|
| `PitcherSimilarityEngine.build(seasons=None)` | Loads the league averages and the profiles, shrinks the command rates, merges GMM components under 30 pitches (`enforce_min_cluster_size`), moves every GMM into z-score space (`standardize_gmm`) and builds the two hand partitions. None loads every season. | `api.state.build_pitcher_engine`, `api.state.build_all_engines`, `build_pitcher_sim_matrix`, `scripts/fit_calibration.py` |
| `PitcherSimilarityEngine.query(pitcher_id, season, n=None)` | Scores one pitcher-season against every same-hand profile, highest first. | `api/routes/similarity.py`, `api/routes/similarity_explorer.py`, `build_pitcher_sim_matrix` |
| `PitcherSimilarityEngine.query_pair` | Scores one pair. | `api/routes/similarity_explorer.py` |
| `PitcherSimilarityEngine.apply_calibration` | Converts the report's `arsenal_gamma` and median W₂ into the linear scale (`arsenal_scale_from_gamma`: scale = 1 / (γ · median)) and rewrites the module global `ARSENAL_SCALE`. It reads only the arsenal fields. | `api.state.apply_calibration_to_engines` |
| `HandednessPartition.score_all` | Scores one query against a partition in one vectorized pass. A pair without both GMMs moves the arsenal weight onto the command score (× 1 / 0.35). | `query` |
| `ArsenalSimilarity.distance` / `score` | The W₂ distance between two GMMs: the Bures-Wasserstein closed form per component pair, then optimal transport through POT's `ot.emd2` (a greedy transport when POT is missing). | `ArsenalCache`, `_score_pair` |

**Watch out:** `ARSENAL_SCALE` is a module global on purpose. `apply_calibration` rewrites it,
and `ArsenalSimilarity.score`, `HandednessPartition.score_all` and `_score_pair` read it at
call time, so a new scale reaches every pitcher engine in the process. The report's
`sigma_command` and `eb_n_prior_pitcher` are fitted but not applied to this engine. The
module runs `logging.basicConfig` at import.

**The arm-angle and spin-shape decision (SIM-533, owner decision 2026-09-19).** Savant's arm
angle, active spin and spin-axis deviation are NOT features. The release point is already
three of the arsenal's eight dimensions, and the arm angle is the release point measured
from the shoulder; the rest is the shoulder's posture, which the ball's flight does not
carry. The spin-shape numbers are formulas on columns the arsenal already holds. On 67,941
same-hand pairs of 2024, no candidate added more than 0.009 R² on the outcomes the score
does not read. A weight per arsenal dimension is a candidate for the comprehensive sweep
(SIM-548), not a feature. The record is the decision block above `GMM_FEATURE_NAMES` and
[the study plan](../audit/2026-09-18-sim533-pitcher-arm-angle-spin-shape-plan.md).
`tests/unit/test_arm_angle_decision.py` holds that no computor, engine or artifact builder
reads Savant's arm angle, and `scripts/probes/arm_angle_probe.py` re-runs the numbers.

### `similarity/engines/arsenal_cache.py`

[Source](../../similarity/engines/arsenal_cache.py) · 437 lines · Tests: [test_arsenal_cache_vectorized.py](../../tests/unit/test_arsenal_cache_vectorized.py), [test_pitcher_similarity.py](../../tests/unit/test_pitcher_similarity.py), [test_pitcher_engine_calibration.py](../../tests/unit/test_pitcher_engine_calibration.py)

`ArsenalCache`, the pitcher engine's store of pairwise W₂ distances. One distance costs about
0.5 ms, and a query scores every same-hand pitcher, so the cache keeps each distance once. It
stores the raw distance, not the score, so a new `ARSENAL_SCALE` needs no recompute.

| Function / class | What it does | Called from |
|---|---|---|
| `row_distances(query, profiles)` | The query's whole row of raw distances: 0.0 for itself, inf for a pair without both GMMs. One slice of a dense matrix when one covers the population; otherwise the row is built from the dictionary, and each missing pair is computed and kept. | `HandednessPartition.score_all` |
| `precompute(profiles, n_workers)` | Fills every pair, serially or on a process pool (`_compute_w2_chunk`). | `PitcherSimilarityEngine.precompute_arsenal_cache` |
| `finite_distances()` | Every cached finite distance: the input of `calibrate_arsenal_gamma`. | `PitcherSimilarityEngine.apply_calibration` (a fallback median) |
| `save` / `load` | A pickle file of the distances. | `precompute_arsenal_cache(save_path=...)` saves; `PitcherSimilarityEngine.build(arsenal_cache_path=...)` loads |

**Watch out:** `load` unpickles, which can run code from the file: load only a file this
engine wrote. `_compute_w2_chunk` is a process-pool target, so it stays at module level. The
cache logs as `pitcher_similarity`.

### `similarity/engines/batter_similarity.py`

[Source](../../similarity/engines/batter_similarity.py) · 1,350 lines · Tests: [test_batter_similarity.py](../../tests/unit/test_batter_similarity.py), [test_batter_physical_features.py](../../tests/unit/test_batter_physical_features.py), [test_engine_build_smoke.py](../../tests/unit/test_engine_build_smoke.py)

Compares batter-seasons on five sub-scores:

- **Discipline (0.32):** first-pitch take, chase, zone swing, contact, whiff, strikeout and
  walk rates.
- **Batted ball (0.28):** ground-ball and fly-ball rates, pull and opposite-field rates
  relative to the batter's side, average exit velocity and launch angle, hard-hit and barrel
  rates.
- **Platoon (0.12):** discipline rates plus the ground-ball and barrel rates, split by
  pitcher hand and compared side for side.
- **Power (0.08):** home-run rate, xBA, xSLG and maximum exit velocity.
- **Physical swing and stance (0.20; SIM-529):** ten Statcast measurements of how the batter
  swings and stands (bat speed, swing tilt, swing length, stance depth and more), each held
  overall and per pitcher hand. Savant publishes them from 2023 on.

With a pitcher hand given (`vs_hand`), the platoon group leads: 0.24 / 0.20 / 0.28 / 0.08 /
0.20. When no batter in the built population has physical data, `sub_score_weights` drops
the physical group and uses the four-group split (0.40 / 0.35 / 0.15 / 0.10), so the
composite keeps its spread. The bats penalty multiplies the composite: 0.92 for opposite
sides, 0.97 against a switch hitter, 1.00 for the same side. The shrinkage prior is 5 plate
appearances; the load keeps profiles of 100 or more. Data: `derived.batter_season_metrics`.

| Function / class | What it does | Called from |
|---|---|---|
| `BatterSimilarityEngine.build` | Loads, shrinks, fits the normalizer and builds one partition for every batter. | `api.state.build_all_engines`, `build_actor_sim_matrices` |
| `BatterSimilarityEngine.query(batter_id, season, n=None, vs_hand=None)` | Scores one batter-season against every other. | `build_actor_sim_matrices` (the `batter` matrix), `api/routes/similarity_explorer.py` |
| `BatterSimilarityEngine.apply_calibration` | Rebuilds the five kernels from the report's sigmas and reliability weights. | `api.state.apply_calibration_to_engines` |
| `bats_penalty` / `bats_penalty_vector` | The handedness multiplier. | `BatterPartition.score_all`, `_score_pair` |

**Watch out:** the profile computor writes no xBA or xSLG, so both read 0.0 for every batter
and the power group runs on the home-run rate and the maximum exit velocity. The
calibration reads the batter columns by position; `_N_BASE_COLUMNS` is computed from the
feature lists, so change `similarity/calibration/batter.py` with any list change.

### `similarity/engines/fielder_similarity.py`

[Source](../../similarity/engines/fielder_similarity.py) · 1,576 lines · Tests: [test_fielder_similarity.py](../../tests/unit/test_fielder_similarity.py), [test_fielder_oaa_jump_model.py](../../tests/unit/test_fielder_oaa_jump_model.py), [test_outfield_arm_model.py](../../tests/unit/test_outfield_arm_model.py)

Compares fielder-seasons strictly within one position; each of 1B, 2B, 3B, SS, LF, CF and RF
is its own partition, and a player's seasons at two positions are two profiles.

- **Infield:** range 0.45 (our outs above average to the glove side, to the arm side,
  charging and going deep, the catch percentage added, and Savant's outs above average per
  100 of our chances), double play 0.30 (2B and SS add the pivot), errors 0.15 (fielding
  and throwing), specialty 0.10 (bunt defense; the scoop at 1B).
- **Outfield:** range 0.40 (the same six features plus the three parts of Savant's outfield
  jump: reaction, burst, route; SIM-532), arm 0.30 (throw velocity, advancement prevention,
  thrown-out rate; SIM-550), star plays 0.15 (catch rates on 5-star, 4-star and routine
  chances), errors 0.15.

The shrinkage prior is 15 batted balls, because defensive metrics stabilize slowly
(CLAUDE.md §10). The jump parts shrink on their own plays (prior 25) and the two arm rates on
the arm's chances (prior 50). Data: `derived.fielder_season_metrics`; the positional league
rows come from `derived.league_averages`.

| Function / class | What it does | Called from |
|---|---|---|
| `FielderSimilarityEngine.build` | Loads the positional averages and the profiles, shrinks them and builds one partition per position. | `api.state.build_all_engines`, `build_actor_sim_matrices` |
| `FielderSimilarityEngine.query(player_id, position, season, n=None)` | Scores one fielder-season against the same position. An unknown position or profile returns an empty list. | `build_actor_sim_matrices` (one `fielder_<POS>` matrix per position), `api/routes/similarity_explorer.py` |
| `FielderSimilarityEngine.apply_calibration` | Rebuilds the eight position-group kernels from the report. | `api.state.apply_calibration_to_engines` |
| `PositionPartition.score_all` | The vectorized scoring inside one position. | `query` |

**Watch out:** never widen a partition across positions. Savant's outs above average and the
three jump parts load as NaN when a league row lacks them; an absent measurement is never
0.0. The outfield arm block comes from `sim.advancement_opportunity_pool` through the profile
computor.

### `similarity/engines/baserunner_similarity.py`

[Source](../../similarity/engines/baserunner_similarity.py) · 903 lines · Tests: [test_baserunner_extra_base_similarity.py](../../tests/unit/test_baserunner_extra_base_similarity.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py), [test_calibration_reconciliation.py](../../tests/unit/test_calibration_reconciliation.py)

The advancement-runner engine: compares runners on taking the extra base on a ball in play.
Speed 0.35 (sprint speed); aggression 0.40 (the overall and four per-situation attempt rates,
the stop rate, and Savant's attempt rate above expectation, SIM-531); success 0.25 (the
overall and per-situation success rates). Runners share one partition. The prior is 15
extra-base chances. Data: `derived.baserunner_season_metrics`.

| Function / class | What it does | Called from |
|---|---|---|
| `BaserunnerSimilarityEngine.build(seasons=None, *, include_below_minimum=False)` | Loads the profiles; the flag also loads the thin ones. | `api.state.build_all_engines`, `build_actor_sim_matrices` (as `runner_adv`, thin profiles in) |
| `WeightedRBFSimilarity` (this module's own) | The masked kernel: a feature missing on either side leaves the distance and the weight total. | `BaserunnerPartition.score_all`, `_score_pair` |
| `BaserunnerSimilarityEngine.apply_calibration` | Rebuilds the three kernels; refuses a reliability vector of the wrong length with a warning. | `api.state.apply_calibration_to_engines` |

**Watch out:** Savant's attempt rate above expectation loads NULL as NaN, because 0.0 is a
real reading there. `RBF_SIGMA_SPEED` is a tuned 0.8171; the calibration's 0.0 value keeps
it when sprint speed is empty.

### `similarity/engines/baserunner_steal_similarity.py`

[Source](../../similarity/engines/baserunner_steal_similarity.py) · 831 lines · Tests: [test_baserunner_steal_engine.py](../../tests/unit/test_baserunner_steal_engine.py), [test_baserunner_steal_scores.py](../../tests/unit/test_baserunner_steal_scores.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py)

The steal-runner engine. Tendency 0.45 (attempts per plate appearance begun on first, and at
third per plate appearance begun on second); lead 0.45 (the lead off the bag and the jump on
the delivery, in feet, from Savant's basestealing board; SIM-531); success 0.10. Each weight
is the group's share of its year-to-year repeat (owner decision 2026-09-16). A pair without a
lead on both sides scores on tendency and success, renormalized, and `lead_score` is None.
The pair confidence uses chances (prior 50), and the success group shrinks on attempts
(prior 20). Data: `derived.baserunner_steal_metrics`.

| Function / class | What it does | Called from |
|---|---|---|
| `BaserunnerStealSimilarityEngine.build(seasons=None, *, include_below_minimum=False)` | Loads the profiles; the flag also loads the thin ones. | `api.state.build_all_engines`, `build_actor_sim_matrices` (as `runner_steal`, thin profiles in) |
| `StealPartition.score_all` / `query_pair` | Blends three groups when both sides carry a lead; otherwise two. | `query`, `query_pair` |
| `BaserunnerStealSimilarityEngine.apply_calibration` | Rebuilds the three kernels. | `api.state.apply_calibration_to_engines` |

**Watch out:** `RBF_SIGMA_LEAD` (0.9836) is the value `make calibrate` fitted. The bundle build
reads no report, so a refit is copied in by hand.

### `similarity/engines/catcher_similarity.py`

[Source](../../similarity/engines/catcher_similarity.py) · 753 lines · Tests: [test_catcher_similarity_scores.py](../../tests/unit/test_catcher_similarity_scores.py), [test_catcher_engine_throwing.py](../../tests/unit/test_catcher_engine_throwing.py), [test_engine_calibration_regression.py](../../tests/unit/test_engine_calibration_regression.py)

Compares catcher-seasons on four defensive sub-scores: framing (strikes above expectation per
called pitch, framing runs, shadow-zone and heart-zone strike rates), blocking, throwing
execution (pop time, caught-stealing rate, arm strength; the result field `throwing_score`)
and deterrence (steal attempts against per opportunity). The weights keep the ratio
45 : 20 : 12 : 8. The prior is 15 called pitches. The load keeps the rows the profile
computor does not flag `below_minimum_sample`; the computor flags a catcher who faced fewer
than 10 steal attempts. Data: `derived.catcher_season_metrics`.

| Function / class | What it does | Called from |
|---|---|---|
| `CatcherSimilarityEngine.build` | Loads and shrinks the profiles. | `api.state.build_all_engines`, `build_actor_sim_matrices` |
| `CatcherSimilarityEngine.query` / `query_pair` | Scores one catcher-season against every other. | `build_actor_sim_matrices` (`catcher` and `catcher_throwing`), `api/routes/similarity_explorer.py` |
| `CatcherSimilarityEngine.apply_calibration` | Rebuilds the four kernels from the report's sigmas. | `api.state.apply_calibration_to_engines` |

**Watch out:** the catcher receiving ratio (`SIM_CATCHER_RECEIVING`, default 0, production 0;
its enable is SIM-526) is a separate mechanism built from `receiving.json`. It does not read
this engine.

### `similarity/engines/pitcher_steal_similarity.py`

[Source](../../similarity/engines/pitcher_steal_similarity.py) · 623 lines · Tests: [test_pitcher_steal_similarity.py](../../tests/unit/test_pitcher_steal_similarity.py), [test_lead_distance.py](../../tests/unit/test_lead_distance.py)

The pitcher-against-the-run engine. Outcome 0.35 (stolen bases allowed per nine innings,
the caught-stealing rate when runners go, the attempt rate allowed); hold 0.65 (the lead and
the jump the pitcher allows, in feet, from Savant's pitcher running-game board; SIM-531). A
pair without a hold measurement on both sides scores on the outcome alone, and `hold_score`
is None. Both groups shrink on baserunner events (prior 25). It is apart from the main
pitcher engine because holding runners is a skill of its own. Data:
`derived.pitcher_steal_metrics`.

| Function / class | What it does | Called from |
|---|---|---|
| `PitcherStealSimilarityEngine.build` | Loads and shrinks the profiles. | `api.state.build_all_engines`, `build_actor_sim_matrices` |
| `PitcherStealPartition.score_all` / `query_pair` | Blends the two groups when both sides carry a hold. | `query`, `query_pair` |
| `PitcherStealSimilarityEngine.apply_calibration` | Rebuilds the kernels from the report's sigmas. | `api.state.apply_calibration_to_engines` |

### `similarity/engines/manager_similarity.py`

[Source](../../similarity/engines/manager_similarity.py) · 690 lines · Tests: [test_manager_similarity.py](../../tests/unit/test_manager_similarity.py), [test_manager_usage_score.py](../../tests/unit/test_manager_usage_score.py)

Compares manager-seasons. Pitcher usage 0.40 (starter pitch count, starts ended before 100
pitches, the closer's entry leverage, relievers' share of high-leverage pitches, opener and
bulk-relief rates, the share of available relievers used); offensive aggressiveness 0.35;
platoon and matchups 0.25. The prior is 30 games managed; the load keeps managers with 50 or
more. Data: `derived.manager_season_metrics`.

| Function / class | What it does | Called from |
|---|---|---|
| `ManagerSimilarityEngine.build` | Loads and shrinks the profiles. | `api.state.build_all_engines`, `build_actor_sim_matrices` |
| `ManagerSimilarityEngine.query` / `query_pair` | Scores one manager-season against every other. The result carries `usage_score`, which has no pair confidence. | `build_actor_sim_matrices` (`manager_usage`), `api/routes/similarity_explorer.py` |
| `ManagerSimilarityEngine.apply_calibration` | Rebuilds the three kernels from the report. | `api.state.apply_calibration_to_engines` |

**Watch out:** the hit-and-run rate and the double-switch rate read 0.0 for every manager; the
data cannot detect either. The calibration reads the seventeen columns by position.

## The distance engines

### `similarity/engines/situation_similarity.py`

[Source](../../similarity/engines/situation_similarity.py) · 772 lines · Tests: [test_situation_similarity.py](../../tests/unit/test_situation_similarity.py), [test_situation_engine_queries.py](../../tests/unit/test_situation_engine_queries.py), [test_situation_metadata_store.py](../../tests/unit/test_situation_metadata_store.py)

Finds the K historical plate appearances whose state before the first pitch is nearest a
query state. It z-scores eleven features, scales each by the square root of its weight, and
indexes them in a KD-tree. The score difference is clipped to ±5. Data:
`derived.at_bat_situations`.

| Function / class | What it does | Called from |
|---|---|---|
| `SituationSimilarityEngine.build(seasons=None)` | Loads the situations, z-scores them and builds the tree. Refuses an empty load; warns under 1,000 rows. | `api.state.build_all_engines` |
| `query(vector, k=50)` / `query_batch` | The K-nearest lookup; raises `RuntimeError` before `build`. | `api/routes/similarity_explorer.py` (`POST /api/similarity/situation/query`) |
| `ColumnarSituationMeta` | Each row's metadata as parallel NumPy columns, compact and shareable read-only. | `query` |

### `similarity/engines/pitch_pitch_similarity.py`

[Source](../../similarity/engines/pitch_pitch_similarity.py) · 515 lines · Tests: [test_pitch_pitch_engine.py](../../tests/unit/test_pitch_pitch_engine.py), [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py), [test_engine_build_smoke.py](../../tests/unit/test_engine_build_smoke.py)

Indexes every pitch of `sim.pitch_pool` by ten physics features in FAISS (`IndexFlatL2`, an
exact search; `index_kind="hnsw"` builds an approximate graph index). The weights
(`FEATURE_WEIGHTS`) are hand-set.

| Function / class | What it does | Called from |
|---|---|---|
| `PitchPitchSimilarityEngine.build(seasons=None, recency_boost=False)` | Loads the pool, z-scores and weights the features, builds the index. The boost adds a second copy of the last two seasons' pitches. | `api.state.build_all_engines` |
| `query` / `query_batch` | The K-nearest pitch lookup. | no route calls it |

**Watch out:** the sampler keeps a copy of `FEATURE_WEIGHTS` so it never imports FAISS.
`tests/unit/test_pitch_result_split.py` holds the copy equal to the engine's weights.

### `similarity/engines/batted_ball_similarity.py`

[Source](../../similarity/engines/batted_ball_similarity.py) · 520 lines · Tests: [test_batted_ball_engine.py](../../tests/unit/test_batted_ball_engine.py), [test_engine_build_smoke.py](../../tests/unit/test_engine_build_smoke.py)

Indexes the batted balls of `sim.outcome_pool` that carry all three launch measures, in FAISS.
Each neighbour carries its real outcome (`result_hits`: 0 = no hit, 1 = single … 4 = home run).

| Function / class | What it does | Called from |
|---|---|---|
| `BattedBallSimilarityEngine.build(seasons=None, recency_boost=False)` | Picks `pull_relative_spray_angle` when the table has it (else `spray_angle`, with a log line) and builds the index. | `api.state.build_all_engines` |
| `query` / `query_batch` / `outcome_distribution` | The K-nearest lookup, and the share of each outcome among the K nearest. | no route calls them |

## Sanity checks

### `similarity/similarity_diagnostics.py`

[Source](../../similarity/similarity_diagnostics.py) · 821 lines · Tests: [test_baserunner_steal_scores.py](../../tests/unit/test_baserunner_steal_scores.py)

Checks a built engine's scores without changing the engine: a collapsed, inflated or flat
score, the balance between sub-scores, a player's similarity to his own other seasons, and
symmetry.

| Function / class | What it does | Called from |
|---|---|---|
| `ScoreDistribution.from_array` | The spread of one score and three flags: collapsed (median under 0.05), inflated (median over 0.95), no discrimination (standard deviation under 0.05). | every runner |
| `run_batter_diagnostics` / `run_fielder_diagnostics` / `run_baserunner_diagnostics` | Engine-specific runners: sample query profiles, collect every sub-score, run the checks. | the batter, fielder and advancement-runner engines' `__main__` blocks |
| `run_generic_diagnostics(engine, sub_score_names, ...)` | The same for any engine keyed by (player, season) with named sub-scores. | the catcher, manager and steal-runner engines' `__main__` blocks |

## Backtesting

### `similarity/backtesting/`

[Source](../../similarity/backtesting/) · Tests: [test_probabilistic_backtester.py](../../tests/unit/test_probabilistic_backtester.py), [test_recency_walk_forward.py](../../tests/unit/test_recency_walk_forward.py)

An offline validation library on in-memory data; no database, no live caller. The
production win-probability and prop calibration runs on the binary path of
[prop_validation.py](../../simulation/prop_validation.py), which imports only the constants
`DEFAULT_N_BINS` and `OUTCOME_PROB_EPS` from here.

| Module | Lines | What it holds |
|---|---|---|
| [`__init__.py`](../../similarity/backtesting/__init__.py) | 46 | Re-exports the two modules' public names. |
| [`backtester.py`](../../similarity/backtesting/backtester.py) | 540 | Scores of an (n, c) probability matrix against n labels: `expected_calibration_error` (the binned gap between stated confidence and hit rate), `brier_score`, `log_loss`, `reliability_curve`, `evaluate_distributions`. `walk_forward_ablation` scores a model against a league-average baseline on expanding-window folds. |
| [`recency_walk_forward.py`](../../similarity/backtesting/recency_walk_forward.py) | 334 | `walk_forward_folds` (expanding-window season splits), `recency_weighted_prediction` (a k-nearest-neighbour average, k 8, with or without the pool's `recency_weight`) and `walk_forward_recency_eval` (the error change the weight brings, per fold). |
