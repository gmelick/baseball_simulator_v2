# Similarity Engines

The eleven similarity engines that compare a player, a game state, a pitch or a batted ball
with real history, plus their calibration, their sanity checks and the backtesting library.

*21 files documented — use the page outline (right sidebar) to jump to one.*

**How the simulator uses them.** The simulator never queries an engine at game time. The
nightly bundle build (`pipeline/batch/engine_artifacts.py`) builds seven player engines,
scores every profile against every other, and writes the scores as dense matrices
(`actor_sim/`, plus `pitcher_sim.npz` from the pitcher engine). The sampler reads those
matrices as draw weights. The app also builds all eleven engines at boot
(`api/state.py`, `ENGINE_REGISTRY` and `build_all_engines`) for the similarity routes and
the Data Lab, and applies `/data/calibration.json` to them. The bundle build does NOT apply
the calibration report: the matrices use each engine's module defaults, so a refitted value
reaches the simulator only when someone copies it into the module constant.

**The shared shape of a player engine.** Each player engine loads one profile per player
and season from a `derived.*_metrics` table, shrinks small samples toward the league mean
(empirical-Bayes shrinkage, `EmpiricalBayesShrinkage`: weight `n / (n + N_PRIOR)` on the
player's own data), z-scores the features, and scores a pair as a weighted average of
sub-scores. Each sub-score is a Gaussian kernel (an RBF: `exp(-d² / 2σ²)` over the
reliability-weighted squared distance `d²`). The composite is multiplied by
`sqrt(min(alpha_a, alpha_b))`, so a thin profile scores lower. `query(...)` scores one
profile against every other; `query_pair(a, b)` scores one pair. Every player engine refuses a
profile table stamped at two `asof_date` cutoffs (the one-cutoff rule, SIM-551) and exposes
the cutoff as `asof_date`.

---

### `similarity/similarity_calibration.py`

`SimilarityCalibrator` fits every engine's calibrated constants from the profile
population in one DuckDB file: the kernel sigmas, the pitcher's arsenal anchor, the
empirical-Bayes priors and the reliability weights. It combines the per-engine mixins of
`similarity/calibration/`.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `SimilarityCalibrator.calibrate_from_population(seasons, target_median_score=0.50, arsenal_w2_distances=None)` | Opens the file read-only and runs the nine `_calibrate_*_params` methods in order; returns one `CalibrationReport`. A calibrator whose table is absent leaves its fields at 0.0, so its engine keeps its defaults. | `scripts/fit_calibration.py` (`make calibrate`) | the mixins in `similarity/calibration/` |

**Depends on:** `similarity.calibration.*`, `duckdb`

**Used by:** `scripts/fit_calibration.py`. Outside callers still import `CalibrationReport`
and `calibrate_sigma` from here (`api/state.py`, `simulation/prop_validation.py`,
`scripts/fit_calibration.py`, two B5 tests); the module re-exports `calibrate_sigma` until
the clean-up's integration pass re-points them.

> **Notes for anyone changing this file:** Every module of the calibration logs as
> `"similarity_calibration"`, and the report module imports the fielder engine at import
> time (which runs its `logging.basicConfig`); keep both. The 0.0 "keep the default"
> sentinel is a contract: every engine's `apply_calibration` treats a field at 0.0 (or
> None) as no override.

---

### `similarity/calibration/` (`fitting.py`, `report.py`, `base.py` and eight mixins)

The calibration in parts. Each per-engine mixin re-reads that engine's profile columns with
its own SQL and fits in the same feature space the engine scores in.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `fitting.calibrate_sigma` | The sigma that puts the median random pair at the target score; returns `degenerate_value` when the matrix has no spread. | `CalibratorBase._fit_sigma` | — |
| `fitting.calibrate_arsenal_gamma` | The gamma of `exp(-gamma · W₂²)` that puts the median pitcher pair (W₂, the Wasserstein-2 distance between two pitch mixtures) at the target. | `PitcherCalibratorMixin._calibrate_arsenal_params` | — |
| `fitting.calibrate_eb_prior` | The empirical-Bayes prior strength: the median of within-player over between-player variance, clamped to [5, 200]. | the batter, pitcher and baserunner mixins | — |
| `fitting.calibrate_reliability_weights` | One weight per feature: its correlation between consecutive seasons of one player (or one player at one position), clamped to [0.1, 1]. | the batter, fielder and baserunner mixins | — |
| `fitting.keep_defaults_on_thin_entries` / `reliable_pair_count` | A feature measured on fewer than `RELIABILITY_MIN_PAIRS` (20) season pairs keeps its engine default weight. | `FielderCalibratorMixin` | — |
| `report.CalibrationReport` | Every fitted value, a text `summary()`, and a lossless `to_json` / `from_json` / `equals` (numpy arrays tagged). Also carries the win-probability reliability curve that `simulation.prop_validation` writes. | the app's boot (`api.state.load_calibration_report`), every engine's `apply_calibration`, `simulation/win_probability.py` | — |
| `base.CalibratorBase` | `_has_columns` (the column probe), `_zscore_matrix`, `_fit_sigma` (the sigma or 0.0, the keep-the-default sentinel). | every mixin | `db.introspect.duck_columns` |
| `batter.py`, `pitcher.py`, `fielder.py`, `baserunner.py`, `catcher.py`, `baserunner_steal.py`, `pitcher_steal.py`, `manager.py` | One mixin per engine, each with one `_calibrate_<engine>_params` method (the pitcher mixin also has `_calibrate_arsenal_params`). A block that some rows lack (the batter's swing and stance, the fielder's Savant range and jump, the outfield arm, the runner's Savant attempt rate, the steal leads) loads NULL as NaN and fits over the measured rows; under 20 such rows the engine keeps its default. | `SimilarityCalibrator.calibrate_from_population` | the engines' feature lists |

**Depends on:** `numpy`, the engines' feature-list constants, the eight `derived.*_metrics`
tables.

**Used by:** `similarity/similarity_calibration.py`; the engines import `CalibrationReport`
for their type hints and the pitcher engine's docstring points at `calibrate_arsenal_gamma`.

> **Notes for anyone changing this file:** The mixins read columns by position after a
> fixed prefix. When an engine's SELECT or feature order changes, change its mixin in the
> same commit, or the fit lands in the wrong feature space without an error.
> `CalibrationReport.summary()` prints the reliability blocks from `_RELIABILITY_BLOCKS`;
> it has no row for the batter's physical weights.

---

### `similarity/similarity_diagnostics.py`

The sanity checks on a built engine's scores: a collapsed, inflated or flat sub-score,
the balance between sub-scores, cross-season self-similarity, and symmetry.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `ScoreDistribution.from_array` | The spread of one score and its three flags (median under 0.05, over 0.95, standard deviation under 0.05). | every runner | — |
| `run_batter_diagnostics / run_fielder_diagnostics / run_baserunner_diagnostics` | Engine-specific runners: sample query profiles, collect every sub-score, run the checks. | the batter, fielder and baserunner engines' `__main__` blocks | — |
| `run_generic_diagnostics(engine, sub_score_names, ...)` | The same for any engine keyed by `(player_id, season)` with named sub-scores. | the catcher, manager and both steal engines' `__main__` blocks | — |
| `_check_dimensional_balance / _check_cross_season / _check_cross_season_fielder / _check_symmetry / _check_symmetry_fielder` | The shared checks. | the runners | — |

**Depends on:** `numpy`

**Used by:** the engines' `__main__` blocks only; no API or pipeline caller.

> **Notes for anyone changing this file:** It never changes the engine it reads. A new
> engine joins through `run_generic_diagnostics`, not a new runner.

---

### `similarity/engines/rbf_kernel.py`

`WeightedRBFSimilarity`, the weighted Gaussian kernel the batter, fielder, catcher,
manager and both steal engines score their sub-scores with. The weights are normalized to
sum to 1.0, so the exponent is the weighted AVERAGE squared distance per feature and a
sub-score's typical score does not depend on its feature count. A NaN difference counts as
zero distance.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `WeightedRBFSimilarity(sigma, reliability_weights)` | `score(x, y)` for one pair, `score_batch(query, candidates)` for one query against a matrix. | the six engines' partitions and `apply_calibration` | — |

**Used by:** the six engines above (each re-imports the name, so
`<engine module>.WeightedRBFSimilarity` still resolves).

> **Notes for anyone changing this file:** The baserunner engine keeps its own MASKED
> kernel (a feature missing on either side drops out of the distance and its
> normalization); do not merge the two.

---

### `similarity/engines/pitcher_similarity.py`

Compares pitcher-seasons (same season or across seasons) by blending the arsenal score
(the W₂ distance between two pitchers' pitch mixtures, each a Gaussian mixture model, GMM)
and a kernel over seven command rates, 0.65 / 0.35. Left- and right-handers live in
separate partitions and never match.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `ArsenalSimilarity.distance / .score` | The W₂ optimal-transport distance between two GMMs (POT's `ot.emd2`, or a greedy match when POT is missing), and the score `exp(-W₂ / ARSENAL_SCALE)`. | the partition, `_score_pair`, `arsenal_cache.py` | `scipy.linalg.sqrtm`, `ot` |
| `PitcherSimilarityEngine.build` | Loads the profiles and league averages, shrinks the command rates, merges GMM components under 30 pitches, standardizes the arsenals into z-score space, builds the two partitions. | `api/state.py`, `pipeline/batch/engine_artifacts.py::build_pitcher_sim_matrix` | `derived.pitcher_season_metrics`<br>`derived.league_averages` |
| `PitcherSimilarityEngine.apply_calibration` | Turns the report's `arsenal_gamma` and median W₂ into the linear `ARSENAL_SCALE` (`arsenal_scale_from_gamma`) and rewrites the module global, so every scoring path reads it. | `api.state.apply_calibration_to_engines` | — |
| `PitcherSimilarityEngine.query / query_pair` | Scores one pitcher-season against every same-hand profile, or one pair. | `api/routes/similarity.py`, `api/routes/similarity_explorer.py`, `build_pitcher_sim_matrix` (the nightly `pitcher_sim_matrix`) | — |
| `HandednessPartition.score_all` | The vectorized scoring of one query against a partition; a pitcher with no GMM moves the arsenal weight onto command. | `query` | `ArsenalCache.row_distances` |

**Depends on:** `duckdb`, `numpy`, `scipy`, `ot` (POT; the import is optional, and without
it `ArsenalSimilarity.distance` uses a greedy transport), `similarity/engines/arsenal_cache.py`

**Used by:** `api/state.py` (`build_pitcher_engine`, `build_all_engines`),
`api/routes/similarity.py`, `api/routes/similarity_explorer.py`,
`pipeline/batch/engine_artifacts.py::build_pitcher_sim_matrix`,
`similarity/calibration/pitcher.py` (`COMMAND_FEATURES`), `scripts/fit_calibration.py`.

> **Notes for anyone changing this file:** The file stays whole (1,700 lines) because
> `ArsenalSimilarity.score`, `HandednessPartition.score_all` and `_score_pair` read the
> module global `ARSENAL_SCALE`, which `apply_calibration` rewrites; the arsenal cache, which
> does not read it, lives in `arsenal_cache.py`. A query includes the same pitcher's other
> seasons; only the exact `(pitcher_id, season)` is excluded.
>
> **The arm-angle decision (SIM-533, owner decision 2026-09-19).** Savant's arm angle,
> active spin and spin-axis deviation are NOT features. The record is
> `docs/audit/2026-09-18-sim533-pitcher-arm-angle-spin-shape-plan.md` and the decision block
> above `GMM_FEATURE_NAMES`. The release point is already three of the arsenal's eight
> dimensions (release x, release z, extension). Savant's arm angle is the release point
> measured from the shoulder: `atan2(release z − shoulder z, |release x − shoulder x|)`
> reproduces Savant's column exactly (r 1.000). Our per-pitcher release point gives 73% of
> its variance, 81% with the pitcher's height; the rest is the shoulder's posture, which the
> ball's flight does not carry. The spin-shape numbers are formulas on columns the arsenal
> holds: the measured axis IS our `spin_axis` (0.992), the inferred axis is a formula on our
> movement (0.987), active spin follows movement × velocity / spin rate (r 0.94). On 67,941
> same-hand pairs of 2024, no candidate adds more than 0.009 R² on the four outcomes the
> score does not read, and the shoulder remainder adds 0.0005 on all nine. The full
> arm-angle gap adds 0.026 there with a negative coefficient (−0.17): given the score, a
> larger gap goes with a SMALLER outcome gap, so the W₂'s equal weight on its release
> dimensions may be too heavy. A per-dimension weight is a candidate for the comprehensive
> sweep (SIM-548), not a feature. Savant's per-pitch arm angle sits unread in
> `raw.savant_pitch_tracking`; `tests/unit/test_arm_angle_decision.py` holds that no
> computor, engine or artifact builder reads it, and `scripts/probes/arm_angle_probe.py`
> re-runs the numbers. Board traps: the arm-angle board takes `season=`, ignores `year=`
> and then serves the current season; all three boards start in 2020.

---

### `similarity/engines/arsenal_cache.py`

`ArsenalCache`, the pitcher engine's cache of pairwise W₂ distances. A distance costs about
0.5 ms, and a query scores every same-hand pitcher, so the cache keeps each one: lazily
(`get_or_compute`), or all at once (`precompute`, serially or on a process pool). It stores
the raw distance, so a new `ARSENAL_SCALE` needs no recompute.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `ArsenalCache.row_distances / build_matrix` | The all-vs-one lookup: one row slice of a dense matrix once the population is filled, else one row built against the dict. | `HandednessPartition.score_all` | — |
| `ArsenalCache.precompute(profiles, n_workers)` | Fills every pair; `n_workers > 1` farms chunks to `_compute_w2_chunk` on a process pool. | `PitcherSimilarityEngine.precompute_arsenal_cache` | `_serialize_gmm` / `_deserialize_gmm` |
| `ArsenalCache.finite_distances` | Every cached finite distance: the input of `calibrate_arsenal_gamma`. | `scripts/fit_calibration.py` (through the engine) | — |

**Depends on:** `numpy`; `similarity.engines.pitcher_similarity` (the arsenal model,
imported at call time because the engine imports this module).

**Used by:** `similarity/engines/pitcher_similarity.py` (which re-imports the name).

> **Notes for anyone changing this file:** The cache logs as `"pitcher_similarity"`.
> `_compute_w2_chunk` is a process-pool target: keep it at module level.

---

### `similarity/engines/batter_similarity.py`

Compares batter-seasons on five kernel sub-scores: plate discipline, batted-ball profile,
platoon splits, power, and the physical block of swing and stance (SIM-529). The weights
are 0.32 / 0.28 / 0.12 / 0.08 / 0.20; with the opposing pitcher's hand given, the platoon
sub-score leads (0.24 / 0.20 / 0.28 / 0.08 / 0.20). With no physical data the weights fall
back to the four-way split, so the scores do not move before the profiles carry it.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BatterSimilarityEngine.build` | Loads the profiles and league averages, shrinks (prior 5), fits the normalizer, builds one partition (batters are not split by hand). | `api/state.py`, the bundle build | `derived.batter_season_metrics` |
| `BatterSimilarityEngine.apply_calibration` | Rebuilds the five kernels from the report's sigmas and reliability weights; a 0.0 or None field keeps the current value. | `api.state.apply_calibration_to_engines` | — |
| `BatterSimilarityEngine.query` | Scores one batter-season against every other; `vs_hand` switches to the platoon weights. | the bundle build (the `batter` matrix), the API | — |
| `bats_penalty / bats_penalty_vector` | A multiplier for the batters' own hands: 0.92 opposite hands, 0.97 against a switch hitter, 1.00 the same. | `BatterPartition.score_all` | — |

**Used by:** `api/state.py`, `api/routes/similarity_explorer.py`,
`pipeline/batch/engine_artifacts.py` (the `batter` actor matrix),
`similarity/calibration/batter.py` (the feature lists).

> **Notes for anyone changing this file:** The calibration reads the batter columns by
> position after a fixed prefix; change `similarity/calibration/batter.py` with any change
> to the feature lists.

---

### `similarity/engines/fielder_similarity.py`

Compares fielder-seasons strictly within one position: each of 1B, 2B, 3B, SS, LF, CF and RF
is its own partition. The infield blends range, double plays, errors and the
position's specialty (0.45 / 0.30 / 0.15 / 0.10); the outfield blends range, arm, star
catches and errors (0.40 / 0.30 / 0.15 / 0.15).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `FielderSimilarityEngine.build` | Loads the positional averages and the profiles, shrinks at prior 15 (defensive metrics stabilize slowly; CLAUDE.md §10), builds one partition per position. The outfield range group is nine features: our five components, Savant's outs above average per 100 of our chances, and the three parts of the jump (reaction, burst, route; SIM-532). The infield range group adds Savant's figure as a sixth. The outfield arm group is three features: the throw velocity, the advancement prevention and the thrown-out rate (SIM-550). The jump parts shrink on the jump's own plays (prior 25) and the arm rates on the arm's own chances (prior 50). A missing measurement loads as NaN and becomes the league mean in the shrink. | `api/state.py`, the bundle build | `derived.fielder_season_metrics`<br>`derived.league_averages` |
| `FielderSimilarityEngine.apply_calibration` | Rebuilds the eight position-group kernels from the report. | `api.state.apply_calibration_to_engines` | — |
| `FielderSimilarityEngine.query` | Scores one `(player_id, position, season)` against the same position; refuses an unknown position. | the bundle build (one `fielder_<pos>` matrix per position), the API | — |
| `PositionPartition.score_all` | The vectorized scoring inside one position; 2B and SS carry the pivot features in their double-play group. | `query` | — |

**Used by:** `api/state.py`, `api/routes/similarity_explorer.py`,
`pipeline/batch/engine_artifacts.py`, `similarity/calibration/fielder.py`, and
`similarity/calibration/report.py` (which imports `IF_PIVOT_FEATURES` at import time).

> **Notes for anyone changing this file:** Never widen a partition across positions. The
> outfield arm block is filled by `_fill_outfield_arm_block` in the profile computor from
> `sim.advancement_opportunity_pool`; an absent league arm key reads as NaN, never 0.0 (a 0
> thrown-out rate is the weakest arm, not a neutral value). Savant's split by batter hand
> was measured and left out: within a position it repeats year to year at only 0.04 to
> 0.14 for outfielders; the two columns stay stored and unread.

---

### `similarity/engines/baserunner_similarity.py`

Compares runners on taking the extra base on a ball in play (first to third, second to
home, tagging up): speed, aggression and success, 0.35 / 0.40 / 0.25. Runners are not
partitioned.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BaserunnerSimilarityEngine.build` | Loads the profiles; `include_below_minimum` also loads the thin profiles, so the matrix covers every runner-season the advancement pool holds (the confidence discount weighs them down). | `api/state.py`, the bundle build (as `runner_adv`) | `derived.baserunner_season_metrics` |
| `WeightedRBFSimilarity` (this file's own) | The MASKED kernel: a feature missing on either side drops out of the distance and its normalization. | `BaserunnerPartition.score_all`, `_score_pair` | — |
| `BaserunnerSimilarityEngine.apply_calibration` | Rebuilds the three kernels; a reliability vector of the wrong length is refused with a warning. | `api.state.apply_calibration_to_engines` | — |

**Used by:** `api/state.py`, `api/routes/similarity_explorer.py`,
`pipeline/batch/engine_artifacts.py` (the `runner_adv` matrix, the advancement draw's runner
factor), `similarity/calibration/baserunner.py`.

> **Notes for anyone changing this file:** The aggression group's seventh feature, the
> runner's extra-base attempt rate above Savant's expectation (SIM-531), loads NULL as NaN:
> 0.0 is a real reading there. `RBF_SIGMA_SPEED` is a tuned 0.8171; the calibration's 0.0
> sentinel protects it when `sprint_speed` is unpopulated.

---

### `similarity/engines/baserunner_steal_similarity.py`

Compares runners on stealing: tendency, lead and success, 0.45 / 0.45 / 0.10, each the
group's share of its year-to-year repeat (SIM-531). The lead group is the runner's lead off
the bag and his jump on the delivery, from Savant's basestealing board.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BaserunnerStealSimilarityEngine.build` | Loads the profiles (the thin ones too for the matrix); tendency and lead shrink on first-base opportunities (prior 50), success on attempts (prior 20). | `api/state.py`, the bundle build (as `runner_steal`) | `derived.baserunner_steal_metrics` |
| `StealPartition.score_all / query_pair` | A pair measured on the lead on both sides blends three groups; otherwise tendency and success, renormalized, and `lead_score` is None. | `query`, `query_pair` | — |
| `BaserunnerStealSimilarityEngine.apply_calibration` | Rebuilds the three kernels; `sigma_baserunner_steal_lead` 0.0 keeps `RBF_SIGMA_LEAD`. | `api.state.apply_calibration_to_engines` | — |

**Used by:** `api/state.py`, `api/routes/similarity_explorer.py`,
`pipeline/batch/engine_artifacts.py` (the steal draw's runner factor),
`similarity/calibration/baserunner_steal.py`.

> **Notes for anyone changing this file:** `RBF_SIGMA_LEAD` (0.9836) is the value
> `make calibrate` fitted; the bundle build does not read the report, so a refit is copied
> in by hand.

---

### `similarity/engines/catcher_similarity.py`

Compares catcher-seasons on four defensive sub-scores: framing, blocking, the execution of
the throw, and deterrence (how often runners try), 0.529 / 0.235 / 0.141 / 0.094.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `CatcherSimilarityEngine.build` | Loads the profiles; shrinks at prior 15, as the fielder does. | `api/state.py`, the bundle build | `derived.catcher_season_metrics` |
| `CatcherSimilarityEngine.apply_calibration` | Rebuilds the four kernels from the report's sigmas. | `api.state.apply_calibration_to_engines` | — |
| `CatcherSimilarityEngine.query / query_pair` | Scores one catcher-season against every other. `throwing_score` is the execution sub-score. | the bundle build (`catcher`, and `catcher_throwing` for the steal draw's catcher-arm factor) | — |

**Used by:** `api/state.py`, `api/routes/similarity_explorer.py`,
`pipeline/batch/engine_artifacts.py`, `similarity/calibration/catcher.py` (which repeats
this file's derived framing and blocking expressions; keep them in step).

> **Notes for anyone changing this file:** The catcher receiving ratio in the simulator
> (`SIM_CATCHER_RECEIVING`) is a separate mechanism and does not read this engine.

---

### `similarity/engines/pitcher_steal_similarity.py`

Compares pitchers on holding runners: the outcome (steals allowed per nine, the
caught-stealing rate, the attempt rate allowed) and the hold (the lead and the jump the
pitcher allows, from Savant), 0.35 / 0.65 (SIM-531).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PitcherStealSimilarityEngine.build` | Loads the profiles; both groups shrink on baserunner events (prior 25). | `api/state.py`, the bundle build | `derived.pitcher_steal_metrics` |
| `PitcherStealPartition.score_all / query_pair` | A pair measured on the hold on both sides blends the two; otherwise the outcome score alone, and `hold_score` is None. | `query`, `query_pair` | — |

**Used by:** `api/state.py`, `api/routes/similarity_explorer.py`,
`pipeline/batch/engine_artifacts.py` (the steal draw's pitcher factor),
`similarity/calibration/pitcher_steal.py`.

> **Notes for anyone changing this file:** This engine is separate from the pitcher
> engine on purpose: holding runners is a skill apart from the arsenal and command.

---

### `similarity/engines/manager_similarity.py`

Compares manager-seasons on pitcher usage, offensive aggressiveness and platoon
management, 0.40 / 0.35 / 0.25. The usage sub-score is the weight of the pitching-change
draw: the bundle build writes it as the `manager_usage` matrix (the real-manager draw,
SIM-427).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `ManagerSimilarityEngine.build` | Loads the profiles; shrinks at prior 30 (managers stabilize slowest). | `api/state.py`, the bundle build | `derived.manager_season_metrics` |
| `ManagerSimilarityEngine.apply_calibration` | Rebuilds the three kernels from the report. | `api.state.apply_calibration_to_engines` | — |
| `ManagerSimilarityEngine.query / query_pair` | Scores one manager-season against every other. | the bundle build (`usage_score`), the API | — |

**Used by:** `api/state.py`, `api/routes/similarity_explorer.py`,
`pipeline/batch/engine_artifacts.py`, `similarity/calibration/manager.py` (which reads the
seventeen columns by position).

---

### `similarity/engines/situation_similarity.py`

Finds the K nearest historical plate-appearance situations (inning, outs, runners, score
difference, leverage, pitch and PA counts, park factor) with a KD-tree (a tree over
z-scored, importance-weighted features). It compares game states, not players.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `SituationSimilarityEngine.build` | Loads the situations, z-scores them, builds the tree; refuses an empty index. | `api/state.py` | `derived.at_bat_situations` |
| `SituationSimilarityEngine.query / query_batch` | The K-nearest lookup for one or many `SituationVector`s. | `api/routes/similarity_explorer.py` (the Data Lab's situation finder) | `scipy.spatial.KDTree` |
| `ColumnarSituationMeta` | The neighbours' metadata as parallel NumPy columns, read-only shareable across processes. | `query` | — |

**Used by:** `api/state.py`, `api/routes/similarity_explorer.py`. The simulator does not
read it.

---

### `similarity/engines/pitch_pitch_similarity.py`

Indexes every pitch of `sim.pitch_pool` by its physics (velocity, movement, spin, release,
plate location) in a FAISS index (a nearest-neighbour index over vectors) for K-nearest
lookups.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PitchPitchSimilarityEngine.build` | Loads the pitch pool, z-scores and weights the features, builds the index; `recency_boost=True` (off by default) replicates the last two seasons. | `api/state.py` | `faiss` |
| `PitchPitchSimilarityEngine.query / query_batch` | The K-nearest pitch lookup. | — | — |

**Used by:** `api/state.py` only (built at boot). No route queries it, and the simulator
does not read it.

---

### `similarity/engines/batted_ball_similarity.py`

Indexes every ball in play of `sim.outcome_pool` by exit velocity, launch angle and spray
angle in a FAISS index; `outcome_distribution` turns the K nearest into a distribution of
bases.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BattedBallSimilarityEngine.build` | Loads the pool, picks `pull_relative_spray_angle` when the column exists (else `spray_angle`), builds the index. | `api/state.py` | `faiss` |
| `BattedBallSimilarityEngine.query / query_batch / outcome_distribution` | The K-nearest lookup and the bases distribution. | — | — |

**Used by:** `api/state.py` only (built at boot). The simulator's fielding draw scores the
whole pool itself (`simulation/full_pool_sampler.py`).

---

### `similarity/backtesting/__init__.py`

Re-exports the two backtesting modules' public names.

**Used by:** `tests/unit/test_probabilistic_backtester.py`,
`tests/unit/test_recency_walk_forward.py`.

---

### `similarity/backtesting/backtester.py`

An offline validation library for probability forecasts over discrete outcomes: the
expected calibration error (ECE, the binned gap between the predicted confidence and the
hit rate), the multi-class Brier score, the log-loss and the reliability curve, plus a
walk-forward ablation that scores a model against a league-average floor.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `expected_calibration_error / brier_score / log_loss / reliability_curve / evaluate_distributions` | The scores of an `(n, c)` probability matrix against `(n,)` labels. | tests only | — |
| `walk_forward_ablation` | The expanding-window folds, a predictor against the league-average floor and named ablations. | tests only | `walk_forward_folds` |
| `DEFAULT_N_BINS`, `OUTCOME_PROB_EPS` | The bin count and the probability floor. | `simulation/prop_validation.py` | — |

**Used by:** `simulation/prop_validation.py` (the two constants), the tests.

> **Notes for anyone changing this file:** No program or script calls the functions; the
> module keeps them as an offline library on purpose. The production win-probability and
> prop calibration is binary and lives in `simulation/prop_validation.py`.

---

### `similarity/backtesting/recency_walk_forward.py`

A walk-forward harness that checks whether the pool's `recency_weight` column improves a
plain k-nearest-neighbour outcome predictor out of sample.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `walk_forward_folds` | Expanding-window (train, test) season splits. | `backtester.walk_forward_ablation` | — |
| `recency_weighted_prediction` | A k-NN predictor (k 8), optionally weighting neighbours by `recency_weight`. | `walk_forward_recency_eval` | — |
| `walk_forward_recency_eval` | Per fold, the prediction with and without the weight, and the MAE / RMSE change. | tests only | — |

**Depends on:** `numpy`; in-memory inputs only, never a database.

**Used by:** `similarity/backtesting/backtester.py` (`walk_forward_folds`), the tests.
