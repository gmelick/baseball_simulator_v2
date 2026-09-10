# Similarity Engines

The eleven similarity engines that compare the live matchup to real history, plus calibration and backtesting.

*17 files documented — use the page outline (right sidebar) to jump to one.*

### `similarity/registry.py`

Central catalog (SimilarityEngineRegistry) mapping each of the 11 canonical similarity-engine names to its module/class, plus metadata (algorithm family, similarity-vs-distance score type). Provides lazy import so faiss/POT are only pulled in when that specific engine is resolved.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `EngineSpec.engine_class (property)` | Lazily imports the engine's module and returns the class object. | — | `importlib.import_module` |
| `SimilarityEngineRegistry.list_engines` | Returns all 11 canonical engine names in pipeline order. | `tests/unit/test_ml_engines_sim048.py` | — |
| `SimilarityEngineRegistry.get_spec` | Returns the EngineSpec for a name; raises KeyError listing valid names. | — | — |
| `SimilarityEngineRegistry.get_class` | Resolves a name to its engine class; can raise ImportError/RuntimeError if faiss/ot is missing. | — | — |
| `SimilarityEngineRegistry.create` | Constructs an engine instance for a name with given kwargs. | — | — |

**Depends on:** `similarity.engines.* (11 modules, imported lazily on demand)`

**Used by:** `tests/unit/test_ml_engines_sim048.py (its only caller)`

> **Notes for anyone changing this file:** This registry is NOT what production boot code uses. api/state.py defines its own separate ENGINE_REGISTRY dict and build_all_engines()/build_pitcher_engine() functions that duplicate this module's job. Grep confirms SimilarityEngineRegistry has zero callers outside its own unit test - treat it as a designed-but-orphaned discovery API. Before extending it, check whether api/state.py should be consolidated onto it instead of maintaining two engine catalogs.

---

### `similarity/similarity_calibration.py`

Computes population-derived ('Tier 1') calibration constants - RBF sigmas, the pitcher arsenal W2 gamma, Empirical-Bayes priors, and per-feature reliability weights - directly from live DuckDB profile tables for every one of the 8 RBF/GMM engines, and packages them into a JSON-round-trippable CalibrationReport that each engine consumes via its own apply_calibration() method.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `calibrate_sigma` | Solves the RBF sigma so the median pairwise score over sampled pairs hits a target (default 0.50); returns a caller-chosen degenerate_value when the feature matrix has no spread. | `SimilarityCalibrator._fit_sigma` | — |
| `calibrate_arsenal_gamma` | Same idea as calibrate_sigma but for the pitcher engine's squared-exponential W2-distance-to-score transform. | `SimilarityCalibrator._calibrate_arsenal_params` | `similarity.engines.pitcher_similarity.arsenal_scale_from_gamma (downstream, in the engine)` |
| `calibrate_eb_prior` | Estimates EB_N_PRIOR from a within-player vs between-player variance decomposition (James-Stein shrinkage intensity). | — | — |
| `calibrate_reliability_weights` | Derives per-feature reliability weights from season-to-season split-half correlation across multi-season players. | — | — |
| `CalibrationReport (dataclass)` | Holds every calibrated constant for all 8 engines plus lossless to_dict/to_json/from_dict/from_json/equals for persistence to /data/calibration.json. | — | — |
| `SimilarityCalibrator.calibrate_from_population` | Top-level orchestrator: opens a read-only DuckDB connection and runs all 9 per-engine _calibrate_*_params helpers, returning one CalibrationReport. | `scripts/fit_calibration.py` | — |
| `SimilarityCalibrator._fit_sigma` | Wraps calibrate_sigma with degenerate_value=0.0 so an all-NULL/no-variance DuckDB column yields the 'keep the engine's tuned module default' sentinel instead of a spurious 1.0 override. | — | — |
| `SimilarityCalibrator._calibrate_batter_params / _calibrate_pitcher_params / _calibrate_arsenal_params / _calibrate_fielder_params / _calibrate_baserunner_params / _calibrate_catcher_params / _calibrate_baserunner_steal_params / _calibrate_pitcher_steal_params / _calibrate_manager_params` | One private method per engine: SELECTs that engine's derived.*_metrics columns (guarded via information_schema so a trimmed DuckDB degrades to NULL placeholders instead of erroring) and fits sigmas/weights in the SAME feature space the live engine scores in. | — | — |

**Depends on:** `duckdb`, `numpy`, `similarity.engines.batter_similarity (feature-list constants only)`, `similarity.engines.fielder_similarity (feature-list constants, incl. IF_PIVOT_FEATURES imported at module top)`, `similarity.engines.baserunner_similarity (feature-list constants)`, `DuckDB tables: derived.batter_season_metrics, derived.pitcher_season_metrics, derived.fielder_season_metrics, derived.baserunner_season_metrics, derived.catcher_season_metrics, derived.baserunner_steal_metrics, derived.pitcher_steal_metrics, derived.manager_season_metrics`

**Used by:** `scripts/fit_calibration.py (the `make calibrate` CLI)`, `api/state.py boot path (loads the resulting /data/calibration.json and calls each engine's apply_calibration)`

> **Notes for anyone changing this file:** This module never imports or instantiates an engine class (except pulling feature-list constants) - it re-derives each engine's feature vectors independently via hand-written SQL that MUST stay in sync with that engine's own _load_profiles query and feature-list order. If an engine's SELECT columns or feature order change, the matching _calibrate_*_params method silently calibrates the wrong feature space unless updated in the same change. The 0.0 'keep default' sentinel from _fit_sigma is a load-bearing contract: every engine's apply_calibration treats field==0.0 as 'no override' - never break that without touching all 8 apply_calibration methods across the engines.

---

### `similarity/similarity_diagnostics.py`

Offline sanity-check suite for a built engine's live output: flags collapsed/inflated/no-discrimination sub-scores, checks that a player's cross-season self-pairs score above the population median, and verifies score(A,B)==score(B,A) symmetry.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `ScoreDistribution.from_array` | Computes mean/median/std/percentiles for one score dimension and sets is_collapsed/is_inflated/is_no_discrimination flags against fixed thresholds. | — | — |
| `run_batter_diagnostics / run_pitcher_diagnostics / run_fielder_diagnostics / run_baserunner_diagnostics` | Engine-specific runners: sample query profiles, collect every named sub-score across all query results, build a DiagnosticReport. | `each matching engine's `if __name__ == "__main__":` CLI block` | — |
| `run_generic_diagnostics` | Same pipeline as the four engine-specific runners but works with any engine exposing profile_ids()/query()/query_pair()/profile_count - used for catcher, baserunner_steal, pitcher_steal, and manager. | `catcher_similarity.py, baserunner_steal_similarity.py, pitcher_steal_similarity.py, manager_similarity.py __main__ blocks` | — |
| `_check_dimensional_balance / _check_cross_season / _check_cross_season_fielder / _check_symmetry_batter / _check_symmetry_fielder` | Shared check helpers reused across all engine-specific and generic runners. | — | — |
| `_run_synthetic_batter_test / _run_synthetic_pitcher_test / _run_synthetic_fielder_test / _run_synthetic_baserunner_test` | Build a fully synthetic in-memory engine (no DuckDB) to self-test the diagnostics module in isolation. | — | — |

**Depends on:** `numpy`, `(synthetic self-tests only) similarity.engines.batter_similarity / pitcher_similarity / fielder_similarity / baserunner_similarity internals`

**Used by:** `manual `python <engine_file>.py` CLI invocations only - no API or pipeline caller`

> **Notes for anyone changing this file:** Read-only: never mutates the engine it's given. If an engine's query() result dataclass changes its sub-score attribute names, run_generic_diagnostics(sub_score_names=[...]) callers must be updated to match. A 12th engine should be wired through run_generic_diagnostics rather than a new bespoke run_<x>_diagnostics function.

---

### `similarity/engines/pitcher_similarity.py`

Compares pitcher-to-pitcher (same season or cross-season) by blending GMM Wasserstein-2 arsenal distance with an RBF command-metrics score into one composite [0,1] similarity. Real-world comparison: 'how similar is this pitcher's stuff (velo/movement/spin/release mixture) and command (BB/K/CSW/chase/zone/whiff rates) to every other same-handed pitcher-season'.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `ArsenalSimilarity.distance / .score` | Wasserstein-2 optimal-transport distance between two pitchers' GMM arsenals (via POT `ot.emd2`, or a greedy fallback), converted to a [0,1] score via exp(-W2/ARSENAL_SCALE). | — | — |
| `PitcherSimilarityEngine.build` | Loads all pitcher profiles + league averages from DuckDB, applies EB shrinkage, enforces minimum GMM cluster size, standardizes arsenals into z-score space, builds per-handedness vectorized RBF matrices. | — | `derived.pitcher_season_metrics`<br>`derived.league_averages` |
| `PitcherSimilarityEngine.apply_calibration` | Converts a CalibrationReport's squared-exponential arsenal_gamma into the engine's canonical linear ARSENAL_SCALE (via arsenal_scale_from_gamma) and overrides the module-level constant so every scoring path picks it up. | `api/state.py boot (via similarity_calibration flow)` | — |
| `PitcherSimilarityEngine.query / query_pair` | Scores a query pitcher-season against every same-handed profile (query) or one specific pair (query_pair), returning SimilarityResult(s) sorted by score. | `api/routes/similarity.py (v1 pitcher endpoint)`<br>`api/routes/similarity_explorer.py (generalized /api/similarity/pitcher/*)`<br>`pipeline/batch/engine_artifacts.py::build_pitcher_sim_matrix (nightly dense pitcher_sim_matrix export)` | — |
| `ArsenalCache.row_distances / build_matrix` | SIM-075 vectorized all-vs-one W2 lookup: a single dense-matrix row slice once precomputed, instead of an O(N) per-candidate dict loop. | — | — |
| `HandednessPartition.score_all` | Vectorized batch scoring of one query against an entire L/R partition; redistributes the arsenal weight onto command when a pitcher has no GMM. | — | — |

**Depends on:** `duckdb`, `numpy`, `scipy.linalg.sqrtm`, `ot (POT, optional - lazily guarded, RuntimeError via _require_pot if absent)`, `similarity.similarity_calibration.CalibrationReport (type-only)`

**Used by:** `api/state.py::build_pitcher_engine and build_all_engines (boot, keyed 'pitcher')`, `api/routes/similarity.py`, `api/routes/similarity_explorer.py`, `pipeline/batch/engine_artifacts.py::build_pitcher_sim_matrix`, `similarity/similarity_calibration.py (COMMAND_FEATURES constant only)`

> **Notes for anyone changing this file:** POT (`ot`) has no cp313 wheel as of 2026-05, so the import is lazily guarded - build()/query() raise a clean RuntimeError via _require_pot() rather than making the whole module unimportable. The docstring's 'Architecture' section mentions a `derived.pitcher_gmm_components` table for fast SQL lookup, but _load_profiles actually reads the GMM straight out of the `gmm_model` JSON column on `derived.pitcher_season_metrics` - that second table is not queried anywhere in this file; treat the docstring line as stale. Cross-season comparisons of the SAME pitcher_id are intentionally included in query() results - only the exact (pitcher_id, season) tuple is excluded.

---

### `similarity/engines/batter_similarity.py`

Compares batter-to-batter by combining four RBF sub-scores (plate discipline, batted-ball profile, platoon splits, power) into one composite [0,1] similarity, with an optional pitcher-handedness context that reweights toward the platoon dimension. Real-world comparison: 'which batter-seasons approach and produce like this one, optionally when facing a same-handed pitcher'.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BatterSimilarityEngine.build` | Loads profiles + league averages from derived.batter_season_metrics, applies EB shrinkage, fits the normalizer, builds the single BatterPartition (batters are NOT hand-partitioned). | — | — |
| `BatterSimilarityEngine.apply_calibration` | Rebuilds the four WeightedRBFSimilarity scorers (discipline/batted_ball/platoon/power) from a CalibrationReport's sigma_* and reliability_weights_* fields, falling back to the current value when a field is 0.0/None. | — | — |
| `BatterSimilarityEngine.query` | Scores a query batter against every batter-season; vs_hand shifts sub-score weights to WEIGHT_*_PLATOON variants (platoon becomes dominant). | — | — |
| `bats_penalty / bats_penalty_vector` | Multiplicative penalty (0.92 opposite-hand, 0.97 vs switch, 1.00 same) applied because L/R batters' spray-angle distributions mirror each other. | — | — |
| `BatterPartition.score_all` | Vectorized batch RBF scoring of a query against the whole population, applying the bats penalty and the sqrt(min eb_alpha) confidence discount. | — | — |

**Depends on:** `duckdb`, `numpy`, `similarity.similarity_diagnostics.run_batter_diagnostics`, `similarity.similarity_calibration.CalibrationReport (type-only)`

**Used by:** `api/state.py build_all_engines (keyed 'batter')`, `api/routes/similarity_explorer.py`, `pipeline/batch/engine_artifacts.py::_ACTOR_SIM_ENGINES (SIM-523 actor score-matrix builder - batter matrix)`, `similarity/similarity_calibration.py (DISCIPLINE_FEATURES/BATTED_BALL_FEATURES/POWER_FEATURES/PLATOON_FEATURES constants)`

> **Notes for anyone changing this file:** Feature weighting is explicitly informed by stabilization research in the module docstring (K%/contact%/swing% stabilize fastest, LD%/BABIP excluded) - changing DISCIPLINE_FEATURES/BATTED_BALL_FEATURES ordering or membership requires re-running similarity_calibration.py (it indexes columns positionally after the fixed prefix) and regenerating regression fixtures.

---

### `similarity/engines/fielder_similarity.py`

Compares fielder-to-fielder STRICTLY within the same defensive position (1B/2B/3B/SS each own partition; LF/CF/RF each own partition - a 2B is never compared to a SS or to an OF) using position-appropriate sub-scores: infield Range/DP/Errors/Specialty, outfield Range/Arm/Star-plays/Errors. Real-world comparison: 'which same-position fielder-seasons have a similar defensive skill profile'.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `FielderSimilarityEngine.build` | Loads positional averages + profiles from derived.fielder_season_metrics for IF and OF positions separately, applies the larger EB_N_PRIOR=15 shrinkage (defensive metrics stabilize slowly), builds one PositionPartition per position. | — | — |
| `FielderSimilarityEngine.apply_calibration` | Rebuilds the 8 position-specific WeightedRBFSimilarity scorers (IF range/dp/errors/specialty, OF range/arm/stars/errors) from a CalibrationReport. | — | — |
| `FielderSimilarityEngine.query` | Scores a query (player_id, position, season) against every same-position profile; rejects an invalid position up front. | — | — |
| `FielderSimilarityEngine._get_rbfs_for_position` | Selects the correct (range, secondary, tertiary, error) RBF scorer set for IF vs OF. | — | — |
| `PositionPartition.score_all` | Vectorized batch scoring within one position group, applying middle-infielder pivot features for 2B/SS and position-specific specialty features for corner IF. | — | — |

**Depends on:** `duckdb`, `numpy`, `similarity.similarity_diagnostics.run_fielder_diagnostics`, `similarity.similarity_calibration.CalibrationReport (type-only)`

**Used by:** `api/state.py build_all_engines (keyed 'fielder')`, `api/routes/similarity_explorer.py`, `pipeline/batch/engine_artifacts.py::_ACTOR_SIM_ENGINES (SIM-523 actor score-matrix builder - one fielder matrix per position, _FIELDER_POSITIONS)`, `similarity/similarity_calibration.py (IF_*/OF_* feature-list constants, incl. IF_PIVOT_FEATURES imported at module top of similarity_calibration.py)`

> **Notes for anyone changing this file:** Position gating is a hard invariant - never widen a partition to cross positions; the docstring explains why (range direction, throw distance, DP role differ fundamentally). EB_N_PRIOR=15 (vs 5 for batter/pitcher) is an intentional, documented design decision (CLAUDE.md section 10) - don't 'fix' it to match the other engines.

---

### `similarity/engines/baserunner_similarity.py`

Compares baserunners on EXTRA-BASE advancement tendency (taking first-to-third, second-to-home, tagging up, etc.) via Speed/Aggression/Success RBF sub-scores. Real-world comparison: 'which runner-seasons take the extra base as often, and as successfully, as this one' - used for the advancement draw.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BaserunnerSimilarityEngine.build` | Loads league averages + profiles from derived.baserunner_season_metrics; the `include_below_minimum` flag (SIM-523) additionally loads THIN (below-minimum-sample) profiles so the score matrix covers every runner-season a pool holds, relying on eb_alpha to down-weight low-confidence rows instead of excluding them. | — | — |
| `BaserunnerSimilarityEngine.apply_calibration` | Rebuilds the speed/aggression/success WeightedRBFSimilarity scorers from a CalibrationReport. | — | — |
| `BaserunnerSimilarityEngine.query / query_pair` | Scores a query runner-season against the (single, unpartitioned) population. | — | — |

**Depends on:** `duckdb`, `numpy`, `similarity.similarity_diagnostics.run_baserunner_diagnostics`, `similarity.similarity_calibration.CalibrationReport (type-only)`

**Used by:** `api/state.py build_all_engines (keyed 'baserunner')`, `api/routes/similarity_explorer.py`, `pipeline/batch/engine_artifacts.py::_ACTOR_SIM_ENGINES as 'runner_adv' (feeds the advancement draw's runner factor; listed in _THIN_PROFILE_ENGINES so its thin-profile build() flag is used)`, `similarity/similarity_calibration.py (SPEED_FEATURES/AGGRESSION_FEATURES/SUCCESS_FEATURES constants)`

> **Notes for anyone changing this file:** Not partitioned by any attribute - every runner is compared to every other runner; the RBF kernel is trusted to naturally separate fast/slow profiles. sprint_speed is often NULL/unpopulated on the live DB (see similarity_calibration.py comment), which is why _fit_sigma's 0.0 sentinel matters here specifically - a naive calibrator would otherwise clobber the tuned RBF_SIGMA_SPEED=0.8171 with a spurious 1.0.

---

### `similarity/engines/baserunner_steal_similarity.py`

Compares baserunners specifically on STOLEN-BASE tendency and success (separate from extra-base advancement because it's a pre-meditated, pitcher-read-driven skill rather than a real-time fly-ball read). Real-world comparison: 'which runner-seasons attempt steals as often, and succeed as often, as this one' - feeds the steal draw's runner factor.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BaserunnerStealSimilarityEngine.build` | Loads profiles from derived.baserunner_steal_metrics; two sub-scores only (Tendency ~62%, Success ~38%) since SIM-408 removed the unmeasurable Jump/First-Step sub-score and renormalized its weight. | — | — |
| `BaserunnerStealSimilarityEngine.apply_calibration` | Rebuilds the tendency/success WeightedRBFSimilarity scorers from a CalibrationReport. | — | — |
| `BaserunnerStealSimilarityEngine.query / query_pair` | Scores a query runner-season against the population using run_generic_diagnostics-compatible SimilarityResult fields. | — | — |

**Depends on:** `duckdb`, `numpy`, `similarity.similarity_diagnostics.run_generic_diagnostics`, `similarity.similarity_calibration.CalibrationReport (type-only)`

**Used by:** `api/state.py build_all_engines (keyed 'baserunner_steal')`, `api/routes/similarity_explorer.py`, `pipeline/batch/engine_artifacts.py::_ACTOR_SIM_ENGINES as 'runner_steal' (feeds the steal draw's runner factor; also in _THIN_PROFILE_ENGINES)`, `similarity/similarity_calibration.py::_calibrate_baserunner_steal_params (TENDENCY/SUCCESS feature columns read directly by name, not via imported constants)`

> **Notes for anyone changing this file:** SIM-408 permanently removed the Jump/First-Step sub-score because Statcast never published per-runner reaction-time/burst-distance/break-angle historically - don't re-add it without a real data source. WEIGHT_TENDENCY/WEIGHT_SUCCESS are literal fractions (0.40/0.65, 0.25/0.65) preserving the ORIGINAL 3-way ratio after renormalization - keep that derivation visible if you touch the weights again.

---

### `similarity/engines/catcher_similarity.py`

Compares catchers across four defensive dimensions - Framing, Blocking, Throwing-Execution, Throwing-Deterrence (SIM-072 v2 split; the 5th 'Offense' sub-score was removed in SIM-408). Real-world comparison: 'which catcher-seasons frame/block/throw like this one, and get challenged by runners as often'.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `CatcherSimilarityEngine.build` | Loads profiles from derived.catcher_season_metrics; EB_N_PRIOR=15 (same slow-stabilization rationale as fielder). | — | — |
| `CatcherSimilarityEngine.apply_calibration` | Rebuilds the framing/blocking/throwing/deterrence WeightedRBFSimilarity scorers from a CalibrationReport. | — | — |
| `CatcherSimilarityEngine.query / query_pair` | Scores a query catcher-season against the population; `throwing_score` is the field name retained for backward compatibility from the v1 single-throwing-dimension design. | — | — |

**Depends on:** `duckdb`, `numpy`, `similarity.similarity_diagnostics.run_generic_diagnostics`, `similarity.similarity_calibration.CalibrationReport (type-only)`

**Used by:** `api/state.py build_all_engines (keyed 'catcher')`, `api/routes/similarity_explorer.py`, `pipeline/batch/engine_artifacts.py::_ACTOR_SIM_ENGINES as 'catcher' (composite score) and 'catcher_throwing' (throwing_score alone - feeds the steal draw's catcher-arm factor)`, `similarity/similarity_calibration.py::_calibrate_catcher_params (mirrors this file's _load_profiles SQL expressions exactly - framing/blocking derived-column formulas must be kept in sync between the two files)`

> **Notes for anyone changing this file:** CLAUDE.md notes the catcher RECEIVING kernel (a separate anisotropic bell-curve mechanism, SIM_CATCHER_*_SIGMA env flags) that sits in simulation/, not in this file - this engine only produces the static framing/blocking/throwing/deterrence similarity score consumed as a matrix factor, and is unrelated to that runtime receiving-ratio flag. The SIM-072 v1-to-v2 sub-score split (execution vs deterrence) was a direct response to a Baseball-Analyst pushback memo (2026-05-06) - see the docstring for the reasoning if reconsidering the split.

---

### `similarity/engines/pitcher_steal_similarity.py`

Compares pitchers on their ability to hold runners / prevent steals, using an OUTCOME-only sub-score (SB allowed per 9, CS rate when challenged, attempt rate allowed) after SIM-408 removed the unmeasurable Delivery and Pickoff/Disengagement sub-scores. Real-world comparison: 'do runners fare similarly against these two pitchers when trying to steal'.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PitcherStealSimilarityEngine.build` | Loads profiles from derived.pitcher_steal_metrics; single sub-score at weight 1.0 (WEIGHT_OUTCOME). | — | — |
| `PitcherStealSimilarityEngine.apply_calibration` | Rebuilds the single outcome WeightedRBFSimilarity scorer from a CalibrationReport. | — | — |
| `PitcherStealSimilarityEngine.query / query_pair` | Scores a query pitcher-season against the population. | — | — |

**Depends on:** `duckdb`, `numpy`, `similarity.similarity_diagnostics.run_generic_diagnostics`, `similarity.similarity_calibration.CalibrationReport (type-only)`

**Used by:** `api/state.py build_all_engines (keyed 'pitcher_steal')`, `api/routes/similarity_explorer.py`, `pipeline/batch/engine_artifacts.py::_ACTOR_SIM_ENGINES as 'pitcher_steal' (feeds the steal draw's pitcher-hold factor)`, `similarity/similarity_calibration.py::_calibrate_pitcher_steal_params`

> **Notes for anyone changing this file:** This is intentionally a SEPARATE engine from pitcher_similarity.py - do not merge them; the docstring explains the skill (holding runners) is independent of arsenal/command. Because raw.pitches has no pickoff/disengagement columns historically, don't attempt to re-add the removed sub-scores without first confirming a new data source exists.

---

### `similarity/engines/manager_similarity.py`

Compares managers on in-game tendency across Pitcher Usage (40%), Offensive Aggressiveness (35%), and Platoon/Matchup Management (25%). Real-world comparison: 'which manager-seasons pull starters, run/bunt, and platoon-substitute like this one' - feeds the manager pitching-change decision model (SIM-434) with a real per-manager profile once wired (SIM-427, still pending per CLAUDE.md).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `ManagerSimilarityEngine.build` | Loads profiles from derived.manager_season_metrics; EB_N_PRIOR=30 (managers need the largest sample of any engine to stabilize, per the docstring). | — | — |
| `ManagerSimilarityEngine.apply_calibration` | Rebuilds the usage/aggression/platoon WeightedRBFSimilarity scorers from a CalibrationReport; usage is now a genuine 7-feature fit since SIM-427 un-gated available_reliever_usage_rate. | — | — |
| `ManagerSimilarityEngine.query / query_pair` | Scores a query manager-season against the population. | — | — |

**Depends on:** `duckdb`, `numpy`, `similarity.similarity_diagnostics.run_generic_diagnostics`, `similarity.similarity_calibration.CalibrationReport (type-only)`

**Used by:** `api/state.py build_all_engines (keyed 'manager')`, `api/routes/similarity_explorer.py`, `similarity/similarity_calibration.py::_calibrate_manager_params (reads all 17 USAGE+AGGRESSION+PLATOON columns from derived.manager_season_metrics by position)`

> **Notes for anyone changing this file:** Per CLAUDE.md this engine's OUTPUT is not yet wired into the live SIM-434 manager pitching-change decision model in simulation/ - that model currently uses a league-flat default, not this engine's per-manager similarity (tracked as SIM-427, still open as of 2026-09-10). Do not assume changing this engine changes in-game manager behavior today.

---

### `similarity/engines/situation_similarity.py`

Given a game state (inning, outs, baserunners, score differential, leverage index, pitch/PA counts, park factor), finds the K nearest historical plate-appearance situations via a scipy KDTree over z-scored, importance-weighted features. Unlike every other engine, it compares GAME STATES, not players.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `SituationSimilarityEngine.build` | Loads derived.at_bat_situations (one row per historical PA), z-score normalizes, builds a scipy KDTree; RAISES RuntimeError on a zero-row index (SIM-408 hardening) rather than building a silently NaN-poisoned engine. | — | `derived.at_bat_situations` |
| `SituationSimilarityEngine.query / query_batch` | K-nearest-neighbor lookup for one or many SituationVectors, reconstructing NearestSituation rows from the columnar ColumnarSituationMeta store. | — | — |
| `ColumnarSituationMeta` | SIM-334: parallel NumPy column arrays replacing a list[NearestSituation] of Python objects - cut memory ~10x and made the index share-able read-only across processes. | — | — |

**Depends on:** `duckdb`, `numpy`, `scipy.spatial.KDTree`, `DuckDB view derived.at_bat_situations (built by pipeline/batch/player_profile_computor.py::_build_at_bat_situations)`

**Used by:** `api/state.py build_all_engines (keyed 'situation')`, `api/routes/similarity_explorer.py (POST /api/similarity/situation/query - the one distance engine with a live query route; imports SituationVector directly)`

> **Notes for anyone changing this file:** Despite being one of the '11 similarity engines' built at boot, this engine is NOT called anywhere in the production simulation loop (simulation/sim_loop.py, simulation/full_pool_sampler.py) or by pipeline/batch/engine_artifacts.py's nightly bundle builder - SIM-486 (2026-09-06) deleted the per-tile fallback architecture that used to consume it for the live pitch-outcome draw. Its only production consumer today is the SIM-439 Data Lab 'Situation Finder' exploration UI. player_profile_computor.py's docstring on _build_at_bat_situations explicitly says the computor SELECTed for this table but never built it until SIM-408 - check that table exists before assuming this engine can build.

---

### `similarity/engines/pitch_pitch_similarity.py`

Indexes every pitch in sim.pitch_pool (physics vector: velo/IVB/HB/spin/release/plate location) into a FAISS IndexFlatL2 (or optional HNSW) for fast K-nearest-neighbor lookup, keyed by pitch, not by player.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PitchPitchSimilarityEngine.build` | Loads the pitch pool, optionally applies a 2x recency-boost replication of the last 2 seasons, z-score normalizes + importance-weights features, builds the FAISS index. | — | — |
| `PitchPitchSimilarityEngine.query / query_batch` | K-nearest-neighbor pitch lookup returning NearestPitch metadata (for looking up the pitch's outcome elsewhere). | — | — |

**Depends on:** `duckdb`, `numpy`, `faiss (optional - _FAISS_AVAILABLE guard; engine __init__ raises RuntimeError if faiss missing)`

**Used by:** `api/state.py build_all_engines (keyed 'pitch_pitch')`

> **Notes for anyone changing this file:** Like situation_similarity.py, this is one of the 11 boot-built engines but has NO production simulation caller - grep across the repo finds it referenced only in api/state.py's registry and its own tests; even api/routes/similarity_explorer.py explicitly 404s score/pair queries for this engine ('distance engines ... are not player-keyed'). It predates the SIM-422/429/486 full-pool architecture that replaced per-tile FAISS lookups with the whole-pool similarity-weighted sampler in simulation/full_pool_sampler.py; treat it as legacy/orphaned unless a new caller is added.

---

### `similarity/engines/batted_ball_similarity.py`

Indexes every in-play pitch in sim.outcome_pool (exit_velo, launch_angle, spray_angle) into a FAISS index keyed by the batted ball's physical fingerprint, returning nearby outcomes (0/1/2/3/4 bases) for a launch-condition-conditioned outcome distribution.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BattedBallSimilarityEngine.build` | Loads the outcome pool, auto-detects whether the handedness-corrected pull_relative_spray_angle column exists (SIM-051) and falls back to raw spray_angle if not, builds the FAISS index. | — | `_select_spray_column` |
| `BattedBallSimilarityEngine.query / query_batch` | K-nearest-neighbor batted-ball lookup. | — | — |
| `BattedBallSimilarityEngine.outcome_distribution` | Converts K nearest neighbors' result_hits values into a {0..4: probability} distribution - explicitly documented as 'the function the simulation loop will actually call'. | — | — |

**Depends on:** `duckdb`, `numpy`, `faiss (optional - _FAISS_AVAILABLE guard)`

**Used by:** `api/state.py build_all_engines (keyed 'batted_ball')`

> **Notes for anyone changing this file:** outcome_distribution's own docstring says the simulation loop 'will actually call' it, but grep finds no caller of this class outside api/state.py and its own tests - the production batted-ball outcome draw is now done by simulation/full_pool_sampler.py's own pool-scoring logic, not this FAISS engine. Like pitch_pitch_similarity.py, this predates the SIM-422/429/486 full-pool architecture; verify with the Backend/Performance agents before assuming it is load-bearing. spray_column_used exposes which column the loader picked - useful for confirming whether SIM-051 has landed on a given DuckDB.

---

### `similarity/backtesting/__init__.py`

Package re-export surface for the two backtesting harnesses (SIM-076 recency walk-forward and SIM-220 probabilistic backtester).

**Depends on:** `similarity.backtesting.backtester`, `similarity.backtesting.recency_walk_forward`

**Used by:** `tests/unit/test_ml_engines_sim220.py and similar test modules that import from the package rather than the submodule`

> **Notes for anyone changing this file:** Pure re-export file, no logic of its own.

---

### `similarity/backtesting/backtester.py`

SIM-220 probabilistic backtester: scores a matrix of predicted outcome-class probabilities against actual labels via ECE, Brier score, log-loss, and a reliability curve, plus a walk-forward ablation harness comparing the full model to a league-average baseline (and optional per-engine ablations).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `expected_calibration_error` | Binned gap between mean predicted confidence and empirical accuracy - the model's calibration error. | — | — |
| `brier_score / log_loss` | Standard multi-class Brier score and eps-clipped cross-entropy. | — | — |
| `reliability_curve` | Per-bin (mean_confidence, empirical_accuracy, count) points for a calibration diagram. | — | — |
| `evaluate_distributions` | Convenience wrapper computing all of the above metrics at once. | — | — |
| `walk_forward_ablation` | Runs SIM-076's expanding-window folds, scoring an injectable predict() function plus a league-average floor and optional named ablation predictors, reporting each's marginal lift over the floor. | — | — |

**Depends on:** `numpy`, `similarity.backtesting.recency_walk_forward (walk_forward_folds, FEATURE_COL/OUTCOME_COL/SEASON_COL)`

**Used by:** `simulation/prop_validation.py (imports ONLY the two constants DEFAULT_N_BINS and OUTCOME_PROB_EPS - reimplements the binary win-probability/prop case itself rather than calling these functions)`, `tests/unit/test_ml_engines_sim220.py`

> **Notes for anyone changing this file:** The module's own docstring labels the higher-level functions (walk_forward_ablation, evaluate_distributions) 'DEV / OFFLINE-ONLY - there is no live pipeline caller'. Production win-probability/prop calibration runs through simulation/prop_validation.py's own binary-case reimplementation, not through this module's multi-class machinery. This is a documented design choice, not a bug - don't 'fix' prop_validation.py to call evaluate_distributions() without understanding why the binary case was split out (a win-prob/over-under is inherently binary, which the multi-class ECE does not score correctly).

---

### `similarity/backtesting/recency_walk_forward.py`

SIM-076 walk-forward harness validating that the recency_weight column (materialized by pipeline/batch/player_profile_computor.py) actually improves out-of-sample fit of a simple k-NN outcome predictor, versus an unweighted baseline.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `walk_forward_folds` | Generates expanding-window (train, test) season splits. | `similarity.backtesting.backtester.walk_forward_ablation (reused, not reimplemented)` | — |
| `recency_weighted_prediction` | A deliberately plain k-NN predictor (DEFAULT_K=8) over training rows, optionally weighting neighbors by their recency_weight column. | — | — |
| `walk_forward_recency_eval` | Runs the full harness: for each fold, predicts with and without recency weighting and reports MAE/RMSE deltas. | — | — |

**Depends on:** `numpy`

**Used by:** `similarity.backtesting.backtester (walk_forward_folds only)`, `tests/unit/test_ml_engines_sim220.py and similar tests`

> **Notes for anyone changing this file:** Whole module is explicitly labeled 'DEV / OFFLINE-ONLY - no live pipeline caller' in its own docstring. Inputs are in-memory (pandas-DataFrame-like) only - never touches a live DB - by design, so it stays trivially unit-testable. `recency_weight` itself is produced by pipeline/batch/player_profile_computor.py::recency_weight (2.0x for the most recent two seasons, x0.75/season decay, floor 0.25); this module only consumes that column, it never recomputes it.

---
