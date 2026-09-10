# Simulation Engine

The pitch-by-pitch simulator: the state machine, the similarity-weighted sampler, the batch runner, and everything that turns one plate appearance into a play.

*20 files documented — use the page outline (right sidebar) to jump to one.*

### `simulation/sim_loop.py`

The pitch-by-pitch state machine and full-game driver. Owns the count machine (balls/strikes/outs), the 8-step per-pitch flow (pre-pitch manager hooks, pitch draw, count advance, fielding/baserunning resolution, run commit, end-of-PA/half-inning roll), and the SIM-434 manager model (pull/reliever/pinch-hit/sac-bunt/IBB/hit-and-run). This is the biggest and most-touched file in the subsystem.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `StateMachine.step_pitch` | Advances the game by exactly one pitch: runs the start-of-PA and pre-pitch manager hooks, draws (or accepts an injected) pitch outcome, advances the count via advance_count, resolves a steal on non-terminal or terminal pitches, then on terminal pitches resolves in-play/walk/HBP/strikeout and rolls the PA/half-inning. | `simulate_game`<br>`simulation.play_recorder.RecordingMachine.step_pitch`<br>`simulation.play_recorder.RecordingStateMachine.step_pitch` | `StateMachine._start_of_pa_hook`<br>`StateMachine._pre_pitch_hook`<br>`StateMachine._full_pool_outcome`<br>`advance_count`<br>`StateMachine._resolve_steal_outcome`<br>`StateMachine._resolve_got_away_advance`<br>`StateMachine._resolve_in_play`<br>`StateMachine._resolve_walk`<br>`StateMachine._resolve_strikeout`<br>`StateMachine._end_of_pa` |
| `StateMachine._full_pool_outcome` | Draws the pitch outcome for the current count from the FullPoolSampler: refreshes the per-half-inning pitcher weight and per-PA batter/situation weight only when their keys (pitcher+hand+catcher, batter+base-out) change, then calls fp.draw(balls, strikes). | `StateMachine.step_pitch` | `FullPoolSampler.new_half_inning`<br>`FullPoolSampler.new_plate_appearance`<br>`FullPoolSampler.draw`<br>`FullPoolSampler.last_pitch_got_away` |
| `StateMachine._full_pool_fielding` | On an in-play pitch, builds the batted-ball draw context (batter, situation, platoon/home/park/fielder/pitch-geometry/born-ball extras gated by flags) and calls the sampler's battedball_new_pa + battedball_draw + last_transition to build a FieldingSignal carrying the drawn row's whole base-out transition. | `StateMachine._resolve_in_play` | `FullPoolSampler.battedball_new_pa`<br>`FullPoolSampler.battedball_draw`<br>`FullPoolSampler.last_transition`<br>`StateMachine._live_fielder_at_drawn_position` |
| `StateMachine._steal_opportunity_draw` | THE STEAL DECISION. Called from _pre_pitch_hook (before the pitch is thrown) whenever a stealable lead runner exists (1B open-2B, or 2B open-3B). Computes a manager-aggression weight (neutral 1.0 with no manager wired) and calls FullPoolSampler.steal_draw against sim.steal_opportunity_pool (loaded into EngineArtifacts.steal_pools); the returned row's attempted/success/pickoff labels are staged via stage_steal or as a StealResolution(pickoff=True). | `StateMachine._pre_pitch_hook` | `FullPoolSampler.has_steal_pool`<br>`FullPoolSampler.steal_draw`<br>`StateMachine.stage_steal` |
| `StateMachine._pre_pitch_hook` | Runs before every pitch is thrown (called from step_pitch): decides IBB, pitch-out, the steal green-light, hit-and-run, then auto-stages a steal via _steal_opportunity_draw if none is already staged. Sets state.manager.* per-pitch signals. | `StateMachine.step_pitch` | `StateMachine._should_issue_ibb`<br>`StateMachine._maybe_hit_and_run`<br>`StateMachine._steal_opportunity_draw`<br>`StateMachine.compute_leverage`<br>`StateMachine._tendency`<br>`StateMachine._manager_rng` |
| `StateMachine._resolve_steal_outcome` | THE STEAL OUTCOME COMMIT. Called from step_pitch in step 7, on both non-terminal and terminal pitches (before the batted-ball/walk/K resolution). Consumes the pending StealResolution staged by _steal_opportunity_draw or a test's stage_steal call, moves the runner (or removes him on a caught stealing), and routes the run/out delta through _commit_run_delta -> run_resolution.resolve_runs. A caught stealing can be the half-inning's third out. | `StateMachine.step_pitch` | `StateMachine._snapshot_bases`<br>`StateMachine._move_runner`<br>`StateMachine._clear_base`<br>`StateMachine._commit_run_delta`<br>`StateMachine._resolve_pickoff`<br>`StateMachine._box_line` |
| `StateMachine._commit_run_delta` | The single place a resolved play becomes a run value + base-out delta. Asserts the pre/post base-out conservation identity (Bases.assert_transition), calls run_resolution.resolve_runs (RE24 delta), and commits runs/outs onto the GameState and PlayResult. Every resolver (steal, walk, strikeout, in-play, pickoff, got-away) routes through this. | `StateMachine._resolve_steal_outcome`<br>`StateMachine._resolve_pickoff`<br>`StateMachine._resolve_got_away_advance`<br>`StateMachine._resolve_walk`<br>`StateMachine._resolve_strikeout`<br>`StateMachine._resolve_in_play_transition` | `Bases.assert_transition`<br>`simulation.run_resolution.resolve_runs`<br>`StateMachine._record_outs` |
| `StateMachine._resolve_in_play_transition` | Applies the drawn batted-ball row's whole base-out transition to the live runners (the drawn row IS the play, SIM-511), then runs the SIM-512 discretionary advancement draws and commits via _commit_run_delta. This is the single in-play resolution path (no fallback since SIM-486). | `StateMachine._resolve_in_play` | `StateMachine._normalized_dests`<br>`StateMachine._seat`<br>`StateMachine._run_advancement_draws`<br>`StateMachine._commit_run_delta` |
| `StateMachine._run_advancement_draws` | For a single/double/tag-up-eligible fly out, draws each of the five discretionary extra-base scenarios (lead-first, can't-pass) from FullPoolSampler.advancement_draw over sim's per-decision advancement opportunity pools, applying attempted/safe/error_extra outcomes to the bases. | `StateMachine._resolve_in_play_transition` | `FullPoolSampler.advancement_draw`<br>`StateMachine._live_fielder_at_drawn_position` |
| `simulate_game` | The full-game driver (SIM-320): spawns two independent rng streams (loop + full-pool) from one seed, builds/wires the StateMachine and initial GameState, seeds the manager bullpen/rest maps, then loops step_pitch calls applying regulation/walk-off/extra-innings/ghost-runner rules until the game ends. Returns a GameSimResult with the accumulated BoxScore. | `simulation.batch_runner._run_one`<br>`simulation.play_recorder.record_game_plays`<br>`simulation.validation.replay_chi_squared.simulate_run_distribution`<br>`simulation.validation.replay_chi_squared.replay_historical_games`<br>`scripts/sim_stats.py and many diagnostic/probe scripts` | `spawn_rng_streams`<br>`StateMachine.step_pitch`<br>`_place_ghost_runner` |
| `StateMachine._maybe_pull_starter / _pick_reliever / _change_pitcher` | SIM-434/523: decide whether to pull the current pitcher (formula gated by pitch-count floor/ceiling + manager tendency + fatigue, or, when SIM_MANAGER_DRAW is on, a pitching_change_draw from the opportunity pool) and pick a reliever from the defending team's bullpen (scored mode using leverage/platoon/effectiveness/rest, or legacy positional mode). | `StateMachine._end_of_pa_hook` | `StateMachine._pitching_change_by_draw`<br>`FullPoolSampler.pitching_change_draw`<br>`score_reliever`<br>`platoon_factor`<br>`tto_effectiveness` |

**Depends on:** `simulation.constants`, `simulation.game_state`, `simulation.run_resolution`, `simulation.full_pool_sampler (duck-typed FullPoolSampler passed in)`

**Used by:** `simulation.batch_runner`, `simulation.production_factory`, `simulation.play_recorder`, `simulation.validation.replay_chi_squared`, `api/routes/games.py`, `api/routes/betting.py`, `db/sim_store.py`, `scripts/sim_stats.py, clv_backtest.py, validate_props.py and most sim*_*.py probe scripts`

**Environment flags read here:** `SIM_MANAGER (read via simulation.production_factory._manager_enabled, consumed here as `manager is None` gate)`, `SIM_GOT_AWAY (via simulation.sim_loop._env_flag -> self._got_away)`, `SIM_BB_PLATOON (via _env_flag -> self._bb_platoon)`

> **Notes for anyone changing this file:** The steal-attempt worked example: the decision is made in StateMachine._steal_opportunity_draw, called from StateMachine._pre_pitch_hook, itself called from step_pitch's pre-pitch section (before the pitch is drawn). It draws from sim.steal_opportunity_pool (loaded as EngineArtifacts.steal_pools per target base '2'/'3', built by pipeline/batch/engine_artifacts.py from migration 0015). The engines that score the draw are the baserunner (runner_steal), pitcher_steal, and catcher (catcher_throwing) actor score matrices via FullPoolSampler._steal_actor_factor/steal_draw. The outcome (safe/caught/pickoff) is committed in StateMachine._resolve_steal_outcome, called from step_pitch's step-7 section on BOTH non-terminal and terminal pitches (a caught stealing can end the half-inning). Manager aggression enters only as a WEIGHT on already-attempted rows, never a gate (owner rule, SIM-474). There is ONE in-play path since SIM-486 -- a missing/legacy bundle raises rather than falling back to a different simulator. resolve_runs must always return method=='re24_delta'; sim_loop asserts this. GameState.bases is mutated in place by several resolvers, so every ledger call snapshots pre_bases via _snapshot_bases BEFORE mutating (a past bug read the post-mutation state as 'before'). Env flags that gate the manager (SIM_MANAGER, SIM_MANAGER_DRAW) and realism nudges are read once at StateMachine construction time in production_factory, not here, except SIM_GOT_AWAY/SIM_BB_PLATOON which sim_loop reads directly via `_env_flag`.

---

### `simulation/full_pool_sampler.py`

The similarity-weighted sampler core: scores an entire play pool (pitch pool, batted-ball pool, steal-opportunity pool, advancement pools, pitching-change pool) by the applicable similarity engines and draws from the resulting weighted distribution -- no top-K, no formulas. Every sim decision (pitch, batted ball, steal, advancement, pitching change) is one draw through this class.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `FullPoolSampler.new_half_inning / new_plate_appearance / draw` | The pitch-draw lifecycle: new_half_inning caches the pitcher factor (once per pitcher/hand/catcher change), new_plate_appearance assembles the per-PA weight (batter factor x situation RBF x optional fatigue/side/receiving weights) split into 12 count-bucket CDFs, and draw(balls,strikes) does an O(1) weighted pick from the live count's bucket (optionally through the SIM-467 pitch-draw cell index and the SIM-523 pitch/result split). | `simulation.sim_loop.StateMachine._full_pool_outcome` | `FullPoolSampler._f_pitcher`<br>`FullPoolSampler._f_batter`<br>`FullPoolSampler._f_situation_baseout`<br>`FullPoolSampler._new_plate_appearance_cell`<br>`FullPoolSampler._result_draw` |
| `FullPoolSampler.steal_draw` | Draws ONE row from a target base's steal-opportunity pool (EngineArtifacts.steal_pools), hard-filtered to the exact (outs, balls, strikes) cell, weighted by recency, a soft score-diff Gaussian kernel, the runner/pitcher/catcher actor score-matrix factors (via _steal_actor_factor), and a manager-aggression multiplier applied only to attempted rows. Returns (attempted, success, pickoff_out, pickoff_advancing, pickoff_error) or None if the pool/cell is absent. | `simulation.sim_loop.StateMachine._steal_opportunity_draw` | `FullPoolSampler._steal_meta`<br>`FullPoolSampler._steal_actor_factor`<br>`FullPoolSampler._matrix_gather` |
| `FullPoolSampler.battedball_new_pa / battedball_draw / last_transition` | The in-play draw: hard-filters the batted-ball pool to the live base-out cell (raising if the bundle lacks SIM-510 transition columns), applies the batter/situation/platoon/home/park/fielder/pitch-geometry/born-ball soft weights, then battedball_draw picks a row and last_transition exposes its per-runner destinations. | `simulation.sim_loop.StateMachine._full_pool_fielding` | `FullPoolSampler._transition_meta`<br>`FullPoolSampler._bb_class_rows`<br>`FullPoolSampler._fence_rows`<br>`FullPoolSampler._bb_batter_factor`<br>`FullPoolSampler._f_live_fielder`<br>`FullPoolSampler._f_pitch_similarity`<br>`FullPoolSampler._f_born_similarity` |
| `FullPoolSampler.advancement_draw` | Draws ONE row from a (scenario, from_base, target_base) advancement-opportunity pool, weighted by recency, a throw-geometry Gaussian kernel, the runner (runner_adv) and fielder-arm actor score-matrix factors. Returns (attempted, safe, error_extra). | `simulation.sim_loop.StateMachine._run_advancement_draws` | `FullPoolSampler._adv_meta`<br>`FullPoolSampler._steal_actor_factor` |
| `FullPoolSampler.pitching_change_draw` | SIM-523 part D: draws whether the manager changes pitchers at a PA boundary from the opportunity pool's hard cell (starter/reliever, half-inning boundary, pitch-count bucket, times-through), weighted by recency + a situation Gaussian + the pitcher-similarity factor. Gated by SIM_MANAGER_DRAW. | `simulation.sim_loop.StateMachine._pitching_change_by_draw` | `FullPoolSampler._change_meta`<br>`FullPoolSampler._change_pitcher_factor` |
| `FullPoolSampler._matrix_gather` | The single actor-score-matrix lookup: reads the live actor's row from a nightly-built EngineArtifacts.actor_sim matrix, gathers it onto each pool row's embedding index, raises it to the actor's fitted power (SIM_ACTOR_POWER_<NAME>), and applies the SIM-523-part-F draw-neutral rule for unscored rows. | `FullPoolSampler._f_batter`<br>`FullPoolSampler._steal_actor_factor`<br>`FullPoolSampler._f_live_fielder` | — |

**Depends on:** `pipeline.batch.engine_artifacts (EngineArtifacts, HandPool, carry_predict, recv_block_cell, recv_zone_group)`, `simulation.filter_cells`

**Used by:** `simulation.sim_loop.StateMachine (via duck-typed full_pool_sampler attribute)`, `simulation.production_factory (builds + configures the production instance)`, `simulation.synthetic_bundle.synthetic_sampler (test/no-DB instance)`

**Environment flags read here:** `None read directly -- every knob (actor_power, pitch_result_split, catcher_receiving, bb_class_filter, fence_stage, pitch_cell_index, home_off_weight, park_sigma, fatigue sigmas, etc.) is a plain instance attribute set from outside by simulation.production_factory's apply_*_env functions.`

> **Notes for anyone changing this file:** This is where the steal engines actually feed the draw: steal_draw pulls the runner factor from actor_sim['runner_steal'], the pitcher factor from actor_sim['pitcher_steal'], and the catcher factor from actor_sim['catcher_throwing'] -- all via _steal_actor_factor -> _matrix_gather, keyed by the baserunner/pitcher_steal/catcher embeddings loaded onto EngineArtifacts.actor_emb. has_steal_pool() gates the whole feature: false when EngineArtifacts.steal_pools is empty (a pre-migration-0015 bundle), in which case sim_loop's _steal_opportunity_draw is a no-op and zero steals are attempted. Every draw method returns None on a missing pool/cell rather than raising, EXCEPT the batted-ball transition path (battedball_new_pa) and the pitch-cell-index's widening ladder at its last level, both of which raise loudly (SIM-486/SIM-467) because a missing base-out cell is treated as a data defect, not a degrade-gracefully case. Every power/sigma knob defaults to the value that makes the associated feature an exact no-op (power 1.0 = raw score, sigma 0.0 = off), so flipping a flag off is always byte-identical to before that flag existed.

---

### `simulation/game_state.py`

The dataclass contract every other module in the subsystem is built against: GameState (mutable count/outs/bases/score/inning/lineup/manager context), Bases (base-occupancy + runner identities with SIM-500 conservation guards), ManagerContext, and PlayResult (the structured per-pitch output, including run-resolution provenance and steal/pickoff fields).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `Bases.assert_transition` | SIM-500's core invariant check: asserts that runners_before + batter_reached == runners_after + runners_scored + runners_retired, and (with batter_id given) that no runner is invented or left on base against batter_reached=0. Every resolver in sim_loop calls this indirectly via StateMachine._commit_run_delta before committing a play. | `simulation.sim_loop.StateMachine._commit_run_delta` | `Bases.assert_consistent` |
| `GameState.bat_hand_for` | Resolves a batter's hand for the sampler pre-filter, mapping a switch hitter ('S') to the opposite of the current pitcher's throw_hand. | `simulation.sim_loop.StateMachine._advance_batting_order`<br>`simulation.sim_loop.StateMachine._set_half_matchup`<br>`simulation.lineup_resolver (indirectly, via GameState fields it sets)` | — |
| `GameState.assert_invariants` | Runs every committed-state guard together (count, outs, score, base consistency) after each committed pitch. | `simulation.sim_loop.StateMachine.step_pitch`<br>`simulation.sim_loop.simulate_game` | `GameState.assert_count_valid`<br>`GameState.assert_outs_valid`<br>`GameState.assert_score_valid`<br>`Bases.assert_consistent` |

**Used by:** `Every other file in simulation/ (game_state is the shared contract)`, `api/schemas.py, api/routes/games.py (build/read GameState)`, `simulation.lineup_resolver (constructs GameState)`, `simulation.snapshots, linescore, pitcher_decisions (read GameState/PlayResult fields)`

> **Notes for anyone changing this file:** Bases mutates in place; multiple sim_loop resolvers mutate state.bases before their own ledger call, which is why sim_loop always snapshots pre_bases via _snapshot_bases at the very top of a resolver, above any mutation -- a documented past bug (SIM-499) read a mutated 'before' state. GameState carries no separate home/away pitcher-of-record; the current fielding pitcher is always pitcher_id, and pitcher_decisions.py derives who-fielded-when purely from half + pitcher_id transitions in the committed PlayResult stream. runners_state's 3-bit encoding (bit0=1B,bit1=2B,bit2=3B) is shared byte-for-byte with run_resolution's RE24 matrix keys and the play pool's own bitmask columns -- changing this encoding would break every consumer silently.

---

### `simulation/run_resolution.py`

The single, context-aware run-value ledger (SIM-312/SIM-499): converts a resolved play's before/after base-out states into an RE24 (run-expectancy delta) run value. Replaces the old context-free per-event linear-weight table, which is now deleted.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_runs` | Given pre_outs/pre_runners_state, post_outs/post_runners_state and result_runs (all five required -- no partial call is accepted), returns RunResolution(runs = RE(post) - RE(pre) + result_runs, method='re24_delta', ...). Raises ValueError on any missing argument or an impossible transition (removed outs, negative runs). | `simulation.sim_loop.StateMachine._commit_run_delta` | `re24_value`<br>`simulation.constants.resolve_event_to_canonical` |
| `re24_value` | Looks up the base-out run-expectancy for (outs, runners_state) in the bundled RE24_MATRIX (or an injected matrix), returning 0.0 at 3+ outs. | `resolve_runs` | — |

**Depends on:** `simulation.constants`

**Used by:** `simulation.sim_loop (the only caller -- every run value in the whole simulator flows through here)`

> **Notes for anyone changing this file:** resolve_runs is a hard gate: sim_loop._commit_run_delta raises an AssertionError if rr.method != 're24_delta', so the old linear-weight fallback can never silently reach a committed run value again. The bundled RE24_MATRIX is a static ~2024 MLB table; in production this can be swapped via re24_from_rows() for the live derived.run_expectancy_matrix table, but nothing in this subsystem currently wires that swap -- resolve_runs's default matrix=None always uses the bundled table unless a caller passes one explicitly.

---

### `simulation/results.py`

SIM-327's multi-iteration aggregation contract: turns a list of per-game GameSimResult objects into one GameSimSummary (win rates, score means/medians, raw per-iteration arrays, Wald confidence intervals). Also re-exports GameSimResult/PlayerStatLine/BoxScore from sim_loop so downstream consumers have one import home.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `GameSimSummary.from_results` | Aggregates N GameSimResults: computes home/away/tie win rates, score means/medians, raw score arrays, and Wald normal-approximation CIs on the win rate and the means. | `simulation.batch_runner.BatchRunner.run`<br>`simulation.win_probability.win_probability (when given a raw list)`<br>`api/routes/games.py, betting.py`<br>`scripts/*` | `_proportion_ci`<br>`_mean_ci` |

**Depends on:** `simulation.sim_loop (GameSimResult, BoxScore, PlayerStatLine)`

**Used by:** `simulation.batch_runner`, `simulation.win_probability`, `simulation.prop_distributions (imports ConfidenceInterval)`, `api routes, scripts`

> **Notes for anyone changing this file:** Every CI here is a closed-form Wald interval (not a bootstrap) -- deterministic, O(1), documented as such. n<2 collapses a score-mean CI to a zero-width interval at the single observation (an honest degenerate answer, not an error).

---

### `simulation/win_probability.py`

SIM-330: turns a GameSimSummary (or raw result list) into a CALIBRATED home/away win probability via tie handling (SPLIT/DROP), Beta/Laplace smoothing (so 0/N never collapses to a hard 0.0), and an optional fitted reliability-curve CalibrationMap.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `win_probability` | The main entry point: aggregates to a GameSimSummary if given a raw list, folds ties per TieHandling, applies Beta(alpha,alpha) posterior-mean smoothing, then applies calibration_map.apply(p) and computes a Wald CI at the effective N. | `api/routes/games.py, api/routes/betting.py`<br>`scripts/validate_props.py, clv_backtest.py` | `simulation.results.GameSimSummary.from_results`<br>`CalibrationMap.apply`<br>`_smoothed_proportion_ci` |
| `CalibrationMap.from_report` | Builds a monotone piecewise-linear p->p map from a CalibrationReport's reliability_curve anchor points (produced by simulation.prop_validation.fit_reliability_curve and written via write_reliability_curve_to_calibration_report); falls back to the identity map when the curve is missing/too short. | `api startup / calibration-loading code (outside this subsystem, in api/ or similarity/similarity_calibration.py)` | — |

**Depends on:** `simulation.results (ConfidenceInterval, GameSimResult, GameSimSummary)`

**Used by:** `api/routes/games.py, betting.py`, `scripts/validate_props.py`

> **Notes for anyone changing this file:** The identity CalibrationMap is the module-level default (IDENTITY_CALIBRATION); a fitted curve only reaches this module via a CalibrationReport object passed in by the caller -- this file never reads /data/calibration.json itself. The betting-relevant win probability (the CLV pipeline's gold-standard input) flows entirely through this module, so a change to alpha/tie_handling here directly changes what betting/clv_engine.py compares against closing lines.

---

### `simulation/prop_distributions.py`

SIM-329's prop-PMF aggregator: turns N per-game BoxScores into, for each player and each prop (K/BB/ER/OUTS for pitchers, H/HR/RBI/TB for batters), a full integer-support probability mass function with over/under query helpers -- the input the CLV/edge engine prices a book line against.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PropDistributionSet.from_boxscores / from_results` | Builds every player's per-prop PropDistribution across N games, zero-filling a player's prop for any game they did not appear in so the PMF denominator is always the full N. | `api/routes/betting.py, games.py`<br>`scripts/validate_props.py, clv_backtest.py` | `PropDistribution.from_samples` |
| `PropDistribution.p_over / p_under / p_push` | The betting over/under primitives at the sportsbook convention (half-integer line = no push; integer line pushes on exact match). | `betting/clv_engine.py, bet_signal.py (outside this subsystem)`<br>`simulation.prop_validation.validate_prop_over_under` | — |

**Depends on:** `simulation.results (BoxScore, ConfidenceInterval, GameSimResult, PlayerStatLine)`

**Used by:** `simulation.prop_validation`, `betting/*`, `api routes`, `scripts/validate_props.py, clv_backtest.py`

> **Notes for anyone changing this file:** Total bases (TB) is now EXACT (SIM-365: h + b2 + 2*b3 + 3*hr) -- TB_IS_LOWER_BOUND is False; older docs/tickets calling TB a lower bound predate the b2/b3 boxscore fields. A player who never had any pitching or batting activity across the whole run is entirely absent from PropDistributionSet.by_player (never a spurious all-zero PMF).

---

### `simulation/snapshots.py`

Pure, additive dataclass contracts + builders for the Phase-5/6 API/UI: FieldSnapshot (the field-graphic state), PlayByPlay/PlayByPlayEntry (the pitch-level /plays scroll), StateAtPitch (a point-in-time snapshot), OverrideDelta (a baseline-vs-override comparison). No mutation, no DB.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `FieldSnapshot.from_game_state` | Builds a field-graphic snapshot (defense positions, batter, baserunners, count/outs/score) from a live GameState plus an optional defense_positions map. | `api/routes/games.py (the /state and /card endpoints)`<br>`StateAtPitch.from_game_state` | — |
| `PlayByPlay.from_play_results` | Groups a flat, ordered PlayResult stream (from play_recorder.record_game_plays) into plate appearances using the pa_terminal flag, producing one PlayByPlayEntry per pitch. | `api/routes/games.py (the /plays endpoint)` | `PlayByPlayEntry.from_play_result` |

**Depends on:** `simulation.game_state (GameState, Half, PlayResult)`

**Used by:** `api/routes/games.py`, `simulation.play_recorder (indirectly, as the consumer of its output)`

> **Notes for anyone changing this file:** Player labels are optional and injected (Mapping[int,str]); with none given, PlayerRef falls back to '#<id>' so contracts render standalone in tests with no join to a players table.

---

### `simulation/linescore.py`

Pure derivation of the classic per-inning R/H/E linescore grid from an ordered PlayResult stream -- no DB, no loop mutation.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `linescore_from_plays` | Walks the PlayResult stream, attributing each play's runs/hits/errors to the (inning, half) it was played in (detecting and correcting the half-inning-roll misattribution edge case where next_state already names the NEXT half), and produces the full Linescore with per-inning InningLine cells (None = unplayed half, e.g. a skipped bottom-9th or a walk-off). | `api/routes/games.py (the /linescore endpoint)` | `_is_hit` |

**Depends on:** `simulation.game_state (Half, PlayResult)`

**Used by:** `api/routes/games.py`

> **Notes for anyone changing this file:** Errors are charged to the FIELDING side, not the batting side -- a common source of confusion when reading this module cold. A play with outs_recorded>0 whose committed next_state.outs==0 signals a half-inning-ending play, and this module must re-derive which half it actually happened in (the committed state already points at the next half) -- this same edge case is documented in play_recorder.py's _snapshot_next_state comment about a similar half-inning boundary trap.

---

### `simulation/pitcher_decisions.py`

Pure derivation of MLB W/L/Save pitcher-of-record decisions from an ordered PlayResult stream, including the SIM-414 Rule 9.17(b) sub-5-IP-starter reassignment and the standard three-run save-situation heuristic.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `decisions_from_plays` | Walks the play stream tracking each side's pitcher-of-record and the running score, finds the earliest play after which the eventual winner's lead never relinquishes (the decisive lead-taking play), assigns win/loss to the pitchers of record at that instant, reassigns the win to the best reliever if the starter didn't reach 15 outs, then determines the save from the finisher's save-situation exposure. | `api/routes/games.py (the boxscore/decisions endpoint)` | `_defending_team` |

**Depends on:** `simulation.game_state (GameState, Half, PlayResult, Team)`

**Used by:** `api/routes/games.py`

> **Notes for anyone changing this file:** GameState carries only ONE pitcher_id (the current fielding pitcher); this module infers home vs away pitcher-of-record purely from half at each committed play, so it has no dependency on home_pitcher_id/away_pitcher_id being populated -- it works off the committed PlayResult stream alone.

---

### `simulation/play_recorder.py`

Captures the ordered PlayResult stream of one game without touching sim_loop.py, via a non-invasive RecordingMachine wrapper (or a RecordingStateMachine subclass) that intercepts step_pitch and appends its result.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `record_game_plays` | Resolves a picklable machine_factory (default the no-DB rng_driven_machine_factory), wraps the built machine in RecordingMachine, drives it through simulate_game at a given seed, and returns (GameSimResult, ordered list[PlayResult]). | `api/routes/games.py (the /plays, /state, /linescore endpoints that need the full pitch stream)` | `simulation.batch_runner._resolve_dotted`<br>`simulation.sim_loop.simulate_game` |
| `RecordingMachine.step_pitch` | Delegates to the wrapped machine's step_pitch, deep-copies the returned PlayResult.next_state (so later mutations of the live GameState don't retroactively corrupt earlier recorded plays), and appends the result. | `simulation.sim_loop.simulate_game (via the wrapped machine object)` | `_snapshot_next_state` |

**Depends on:** `simulation.batch_runner (GameSpec, _resolve_dotted)`, `simulation.game_state (PlayResult)`, `simulation.sim_loop (GameSimResult, StateMachine, simulate_game)`

**Used by:** `api/routes/games.py`, `simulation.snapshots.PlayByPlay (consumes its output)`, `simulation.linescore, pitcher_decisions (consume its output)`

> **Notes for anyone changing this file:** The deep-copy-per-pitch in _snapshot_next_state exists because step_pitch hands back the SAME live GameState object as PlayResult.next_state -- without the copy, every recorded play would end up pointing at the game's FINAL state once the game ends (a real defect found via the SIM-486 synthetic-bundle replay lane). This is O(pitches-per-game) deep copies per recorded game -- a few hundred -- acceptable for on-demand replay but not something to call in a hot Monte-Carlo loop.

---

### `simulation/batch_runner.py`

The parallel N-iteration Monte-Carlo fan-out/fan-in: derives per-iteration seeds, maps simulate_game across a ProcessPoolExecutor (or synchronously in-process), aggregates to a GameSimSummary, and memoizes it in an optional Redis/in-memory cache. Owns the SIM-333 shared-memory zero-copy attach and the SIM-360 persistent warm pool.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BatchRunner.run` | The public entry point: computes the cache key, checks the cache, derives N seeds via derive_seed, executes via _execute, aggregates via GameSimSummary.from_results, caches, and returns a BatchResult. | `api/routes/games.py (the /simulate endpoint)`<br>`scripts/sim_stats.py, clv_backtest.py, validate_props.py` | `BatchRunner._execute`<br>`simulation.results.GameSimSummary.from_results` |
| `_run_one` | The per-worker unit of work (module-level so it pickles by reference): resolves spec.machine_factory (a dotted-ref string) if set, builds a fresh StateMachine in-process, filters out '_'-prefixed factory-only sim_kwargs, and calls simulate_game. | `BatchRunner._execute / _map_pool (submitted to the ProcessPoolExecutor)` | `_resolve_dotted`<br>`simulation.sim_loop.simulate_game` |
| `BatchRunner.prewarm` | SIM-402: submits one warm task per worker (bounded by a shared semaphore so only a few workers run the heavy full-pool warm concurrently) to eliminate the cold-fan-out stall where a fresh n-iteration request left every worker cold on its first game. | `api/main.py lifespan startup` | `_prewarm_worker`<br>`simulation.production_factory.warm_worker_cache` |
| `rng_driven_machine_factory` | The picklable, no-DB machine factory: builds a StateMachine over simulation.synthetic_bundle.league_artifacts(), with an optional _hit_rate sim-kwarg reshaping the in-play mix. Used by every always-on test and any script that wants a full game with no DuckDB. | `simulation.play_recorder.DEFAULT_FACTORY_REF (the default record_game_plays factory)`<br>`many unit tests` | `simulation.synthetic_bundle.league_artifacts`<br>`simulation.full_pool_sampler.FullPoolSampler` |

**Depends on:** `simulation.results (GameSimResult, GameSimSummary)`, `simulation.sim_loop (simulate_game)`, `simulation.synthetic_bundle (via rng_driven_machine_factory)`

**Used by:** `api/routes/games.py (production path via simulation.production_factory.production_machine_factory as the GameSpec.machine_factory)`, `simulation.play_recorder`, `scripts/sim_stats.py and most probe scripts`

**Environment flags read here:** `SIM_MP_START_METHOD (multiprocessing start method, default forkserver)`, `SIM_RUNNER_WORKERS (read elsewhere to set BatchRunner(max_workers=...); this file reads os.cpu_count() as the default ceiling)`, `SIM_PREWARM_MAX_CONCURRENT (caps concurrent heavy prewarm tasks)`

> **Notes for anyone changing this file:** A worker NEVER receives a live sampler/StateMachine across the process boundary -- only a picklable GameSpec (a dotted machine_factory string + sim_kwargs dict); this is the SIM-281 D1 design invariant and the reason production_factory exists as a separate, importable-by-dotted-path module. The persistent warm pool (SIM-360) is reused across BatchRunner.run() calls when reuse_pool=True (the default); a worker-count CHANGE between calls tears down and recreates the pool, but the shared-memory segments are never republished (they outlive the pool). Changing the multiprocessing start method away from forkserver reintroduces the SIM-430 COW-memory-balloon defect on Linux hosts with a large parent process.

---

### `simulation/production_factory.py`

The production, DB-backed machine factory: builds the REAL FullPoolSampler over the on-disk engine-artifact bundle (cached once per worker process), reads every SIM_* env-flag knob onto it, wires the SIM-434 manager model when SIM_MANAGER is on, and loads sim.ibb_rates. There is no fallback -- a missing/corrupt bundle raises loudly.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `production_machine_factory` | The GameSpec.machine_factory production entry point: builds/reuses the cached FullPoolSampler, conditionally attaches the SIM-434 default manager profile + generic per-team bullpen (gated by SIM_MANAGER) and the SIM-515 IBB rate table, and returns the StateMachine. | `api/routes/games.py (as the dotted machine_factory string on the production GameSpec)`<br>`simulation.batch_runner._run_one (via _resolve_dotted)` | `_build_full_pool_sampler`<br>`_manager_enabled`<br>`_load_ibb_rates`<br>`_default_bullpen_for_spec / _BULLPEN_BUILDER` |
| `_build_full_pool_sampler` | Loads (or reuses a per-process-cached) EngineArtifacts bundle from disk (splicing in SIM-403b shared-memory views when published), builds a FullPoolSampler over it, and applies every env-flag knob via apply_actor_matrix_env / apply_result_split_env / apply_fielding_env / apply_manager_env plus the home/park/fatigue/cell-index settings read inline. Raises RuntimeError (no fallback) if the bundle cannot load. | `production_machine_factory`<br>`warm_worker_cache` | `pipeline.batch.engine_artifacts.EngineArtifacts.load`<br>`apply_actor_matrix_env`<br>`apply_result_split_env`<br>`apply_fielding_env`<br>`apply_manager_env` |
| `warm_worker_cache` | Builds and fully pre-warms (triggers every lazy per-hand index build, including the steal-opportunity cell index) this process's cached FullPoolSampler, so a worker's FIRST real game runs at the amortized per-game cost instead of paying the cold-load cost on the request path. | `simulation.batch_runner.BatchRunner.prewarm / _prewarm_worker` | `_build_full_pool_sampler`<br>`_warm_sampler` |

**Depends on:** `simulation.batch_runner (GameSpec)`, `simulation.filter_cells (DEFAULT_MIN_CELL)`, `simulation.sim_loop (StateMachine)`, `pipeline.batch.engine_artifacts (EngineArtifacts, lazily imported)`, `simulation.sim_kwargs (open_sim_duckdb, lazily imported)`

**Used by:** `api/routes/games.py (the production sim path)`, `simulation.batch_runner (as the GameSpec.machine_factory dotted target)`

**Environment flags read here:** `SIM_MANAGER (gates the manager model)`, `SIM_MANAGER_DRAW (pitching change as a draw vs the formula)`, `SIM_HOME_OFF_WEIGHT`, `SIM_PARK_KERNEL_SIGMA`, `SIM_FATIGUE_PC_SIGMA`, `SIM_FATIGUE_TTO_SIGMA`, `SIM_BB_PITCH_SIGMA`, `SIM_PITCH_HOME_OFF_WEIGHT`, `SIM_PITCH_CELL_INDEX`, `SIM_PITCH_MIN_CELL`, `SIM_ACTOR_POWER_<NAME> (every actor score-matrix power, incl. fielder per-position)`, `SIM_CATCHER_RECEIVING`, `SIM_PITCH_RESULT_SPLIT`, `SIM_RESULT_PITCH_SIGMA, SIM_RESULT_DENSITY_POWER, SIM_PITCH_PITCHER_POWER, SIM_RESULT_PITCHER_POWER, SIM_RESULT_BATTER_POWER, SIM_PITCH_BATTER_POWER`, `SIM_BB_BORN_SIGMA, SIM_BB_BORN_DENSITY_POWER`, `SIM_BB_CLASS_FILTER, SIM_BB_BATTER_POWER, SIM_PARK_WALL_ZONE_ONLY, SIM_WALL_ZONE_DISTANCE, SIM_FENCE_STAGE, SIM_FENCE_MARGIN`, `SIM_CHANGE_SIT_SIGMA, SIM_CHANGE_PITCHER_POWER, SIM_CHANGE_MIN_CELL`, `BASEBALL_PLAY_POOL_DIR (artifact directory root)`

> **Notes for anyone changing this file:** This is the ONE place every SIM_* full-pool sampler flag is parsed and applied -- anyone adding a new sampler knob must wire it here or it silently stays at its default. The per-process caches (_CACHED_FULL_POOL_SAMPLER, _CACHED_IBB_RATES) persist for the life of the worker process; tests MUST call reset_caches() in setup/teardown or one test's cached sampler leaks into the next. warm_worker_cache's per-hand warm loop explicitly includes ('steal_pools', '_steal_meta') so a pre-warmed worker's first game does not pay the steal-opportunity cell-index build cost mid-game.

---

### `simulation/lineup_resolver.py`

Builds a runtime GameState (lineups, current pitchers, per-position defense maps, bat/throw hands) from Postgres raw.game_lineups / raw.games / raw.players, applying substitution semantics (highest-applicable-sequence-wins per batting slot, independently resolved pitcher slot). Layered so the substitution/assembly logic is DB-free and unit-testable.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_lineup_from_rows` | Pure assembly: reduces raw lineup rows (both teams) to a ResolvedLineup by applying _pick_team_slots per side, honoring an optional as_of_at_bat rewind point. | `resolve_lineup` | `_pick_team_slots` |
| `build_game_state` | Pure builder: turns a ResolvedLineup into a fresh top-of-the-1st GameState -- lineups, slot pointers, the defending pitcher, the leadoff batter, bat/throw-hand maps, both starters, and both teams' per-position defense maps (via build_team_defense_map). | `resolve_game_state`<br>`api/routes/games.py (indirectly via resolve_game_state)` | `build_team_defense_map` |
| `build_team_defense_map` | Maps one resolved team's current batting-slot occupants + its resolved pitcher onto {defensive-slot-name: player_id} -- the source of the home_defense/away_defense maps that feed the full-pool sampler's fielder score-matrix factors and the catcher receiving factor. | `build_game_state`<br>`build_defense_map`<br>`build_defense_map_for_state` | — |
| `resolve_lineup / resolve_game_state` | Async orchestrators: fetch_game_sides + fetch_lineup_rows + fetch_player_hands from Postgres, then delegate all logic to the pure functions above. | `api/routes/games.py` | `fetch_game_sides`<br>`fetch_lineup_rows`<br>`fetch_player_hands`<br>`resolve_lineup_from_rows`<br>`build_game_state` |

**Depends on:** `simulation.game_state (GameState, Half, Team)`

**Used by:** `api/routes/games.py (the live game-state build for /simulate and related endpoints)`

> **Notes for anyone changing this file:** Raises LineupNotIngestedError (a LineupResolutionError subclass) specifically when raw.game_lineups has zero rows for a known game -- callers must distinguish this from 'game unknown' (404) since it maps to a 503 + Retry-After (the lineup may simply not be published yet, ~15 min before first pitch). build_team_defense_map accepts BOTH the canonical position NAME ('SS','C',...) and the legacy numeric scorebook code ('1'..'9') in position_code -- a past bug matched only the numeric form against real-data name-format rows, silently dropping every fielder except the explicitly-added pitcher (breaking the catcher receiving factor and the fielder score matrices).

---

### `simulation/sim_kwargs.py`

The single builder that turns a resolved GameState into simulate_game kwargs (SIM-449), plus the venue park-factor resolver and its UNRESOLVED-sentinel machinery (SIM-452/453) that prevents a silent park-blind simulation from looking identical to a genuinely neutral one.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `sim_kwargs_from_state` | The ONE definition of the simulate_game kwargs dict (lineups, season, pitcher_id, bat_hand(s), throw_hands, both catchers, both defense maps, park_run_factor, venue_id, max_innings). Raises UnresolvedParkFactorError unless the state's park_run_factor has been resolved (or the caller explicitly passes allow_unavailable_park_factor=True). | `build_sim_kwargs`<br>`api/routes/games.py`<br>`scripts/sim_stats.py, clv_backtest.py, validate_props.py` | `park_factor_is_resolved`<br>`venue_id_of_state` |
| `resolve_park_run_factor` | Two-source lookup: the game's venue_id from Postgres raw.games, then its regressed run factor from DuckDB derived.park_factors (factor_type='R'). Returns a plain float only on a real, sane (0.5-2.0) row; every other branch returns the UNRESOLVED_PARK_FACTOR sentinel carrying a reason and logs once per reason. | `resolve_park_factor_onto_state` | `park_factor_unavailable`<br>`_primed_park_factor_connection` |
| `build_sim_kwargs` | The one production entry point: resolves the park factor onto the state, then calls sim_kwargs_from_state with allow_unavailable_park_factor set per on_unavailable_park_factor ('proceed' logs+continues; 'raise' refuses). | `api/routes/games.py (4 routes)`<br>`api/routes/betting.py`<br>`scripts/clv_backtest.py, validate_props.py` | `resolve_park_factor_onto_state`<br>`sim_kwargs_from_state` |

**Depends on:** `simulation.game_state (indirectly, via duck-typed GameState-like objects)`

**Used by:** `api/routes/games.py, betting.py`, `scripts/clv_backtest.py, validate_props.py, sim_stats.py`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH (DuckDB file path for open_sim_duckdb / prime_park_factor_source)`

> **Notes for anyone changing this file:** This module exists specifically because two callers (the API route and scripts/sim_stats.py) once built the kwargs dict by hand and one of them silently dropped home_defense/away_defense/park_run_factor -- the only three keys that feed the fielder score matrices and the park kernel -- making any A/B test of those flags compare two identical no-ops. SIM_KWARG_KEYS is the frozenset a unit test pins the builder's output against so a future dropped key fails CI instead of silently no-op'ing a production feature again. The UNRESOLVED_PARK_FACTOR sentinel is a float subclass equal to 1.0 but distinguishable via isinstance -- this is deliberate so any consumer that only reads the number keeps working while a consumer that cares can tell 'nobody looked' from 'looked and it's neutral'.

---

### `simulation/synthetic_bundle.py`

SIM-486: builds a small, entirely in-memory EngineArtifacts bundle (pitch pools, a transition-carrying batted-ball pool, advancement pools, steal-opportunity pools) in the exact shape the production loader produces, so every no-DB test and the no-DB batch factory run the SAME production in-play/steal/advancement code paths as production, just over synthetic rows.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `league_artifacts` | The default no-DB bundle: a league-average pitch outcome mix over every count bucket, a league in-play event mix over every base-out cell (via inplay_rows), and (by default) the SIM-512 advancement pools ON, so a no-DB game has a realistic run environment. | `simulation.batch_runner.rng_driven_machine_factory`<br>`simulation.validation.replay_chi_squared._league_artifacts`<br>`many unit tests` | `synthetic_artifacts`<br>`inplay_rows` |
| `canonical_transition` | Computes the FORCED (station-to-station) base movement for one event in one base-out cell -- the synthetic row's transition; the five DISCRETIONARY movements are left to the real SIM-512 advancement draws, exactly like production rows. | `inplay_rows`<br>`fixed_play_artifacts` | — |
| `steal_pools` | Builds a synthetic steal-opportunity pool per target base with three canonical rows (went-safe, went-caught, stayed) per (outs,balls,strikes) cell, weighted to a given attempt/success rate -- this is the in-memory analogue of sim.steal_opportunity_pool that lets a unit test exercise the real _steal_opportunity_draw/steal_draw code path deterministically. | `synthetic_artifacts` | `_rate_pool_rows` |
| `synthetic_sampler` | Convenience: builds a FullPoolSampler over a given (or default league) synthetic bundle with a given rng seed. | `simulation.validation.replay_chi_squared._default_state_machine`<br>`many unit tests` | `simulation.full_pool_sampler.FullPoolSampler` |

**Depends on:** `pipeline.batch.engine_artifacts (AdvancementPool, BattedBallPool, EngineArtifacts, HandPool, StealPool -- the exact production dataclass shapes)`

**Used by:** `simulation.batch_runner.rng_driven_machine_factory`, `simulation.validation.replay_chi_squared`, `the entire unit test suite's no-DB path`

> **Notes for anyone changing this file:** Before SIM-486 there was a second, per-tile in-play resolution path used only by tests and the no-DB factory, so the test suite certified an advancement model production never actually ran -- four production defects survived eight weeks unseen as a result. This module is the fix: it feeds the SAME sim_loop/full_pool_sampler in-play code, just with fabricated rows, so a test failure here means a real production code-path bug, not a test-double bug.

---

### `simulation/constants.py`

The canonical outcome-key vocabulary (CANONICAL_OUTCOME_KEYS) every consumer keys on (linescore, sim store, acceptance bands), the Statcast-raw-events-to-canonical alias map, and the centralized defensive run-value constants used by the fielder/catcher engines. The old per-outcome linear run-value table (RUN_VALUES) was removed -- value comes only from run_resolution's RE24 delta.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_event_to_canonical` | Maps any Statcast raw events string (or an already-canonical key) to its canonical outcome key; returns None for unknown/non-terminal tokens (e.g. sim_loop's 'in_progress' marker). | `simulation.run_resolution.resolve_runs`<br>`simulation.sim_loop.StateMachine._accumulate_pa (fallback when canonical_event is None)` | — |

**Used by:** `simulation.run_resolution`, `simulation.sim_loop`, `linescore/other consumers via the canonical vocabulary`, `tests/acceptance/bands.py`

> **Notes for anyone changing this file:** field_error became its OWN canonical outcome (SIM-511) rather than aliasing to 'single' as it used to -- a reach-on-error is NOT a hit. An import-time assertion enforces that every STATCAST_EVENT_ALIASES value is itself a CANONICAL_OUTCOME_KEYS member, so a typo'd alias target fails at import rather than silently leaking a non-canonical key downstream.

---

### `simulation/filter_cells.py`

The ONE shared definition of the pitch-draw hard-filter cell algebra (base occupancy x outs x count x score band x batting side = 2,880 cells), used identically by the sampler's cell index (SIM-467), the thin-cell widening ladder (SIM-475), and the offline measurement script.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `cell_key / decode_cell` | Encode/decode a situation into/from its 0..2879 cell id, most-significant-dimension-first (so sorting by cell id groups by base state). decode_cell is the exact inverse of cell_key. | `simulation.full_pool_sampler.FullPoolSampler._cell_meta (uses the same encode inline rather than calling cell_key directly, but shares SCORE_BAND_EDGES/N_* constants)`<br>`scripts/measure_filter_cells.py` | `score_band`<br>`count_bucket` |
| `score_band / score_band_array` | Maps a (clamped +/-5) score differential to one of 5 bands by counting how many SCORE_BAND_EDGES it exceeds; score_band_array is the vectorized numpy form used per-pool-row. | `simulation.full_pool_sampler.FullPoolSampler._cell_meta / _new_plate_appearance_cell` | — |

**Used by:** `simulation.full_pool_sampler (N_BASE/N_OUTS/N_COUNT/N_BAND/N_HOME/DEFAULT_MIN_CELL/score_band_array/score_band)`, `simulation.production_factory (DEFAULT_MIN_CELL)`, `scripts/measure_filter_cells.py, sim523_kernel_scan.py`

> **Notes for anyone changing this file:** A cell id computed under a different edge definition is NOT comparable with any prior SIM-451 measurement report -- SCORE_BAND_EDGES is validated at import time to be strictly increasing precisely so nobody can silently reorder it and desync a report from the live code.

---

### `simulation/prop_validation.py`

SIM-407: validates the simulator's probability outputs (win probability, prop over/unders) against real outcomes, and FITS the win-probability reliability curve (fit_reliability_curve) that the SIM-406 calibration report otherwise leaves empty/identity. Also derives real per-player prop actuals directly from raw.pitches events labels.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `fit_reliability_curve` | Bins predicted home-win probabilities against observed home-win rates and emits the [[predicted_p, observed_p], ...] anchor list win_probability.CalibrationMap.from_report turns into a monotone win-prob map -- closes the SIM-406->407 handoff. | `build_validation_report` | `binary_reliability_curve` |
| `validate_prop_over_under` | Scores a prop's PMF p_over(line) forecast against realized over/under outcomes with binary ECE/Brier/log-loss and a reliability curve. | `build_validation_report` | `binary_ece`<br>`binary_brier`<br>`binary_log_loss`<br>`binary_reliability_curve` |
| `real_props_from_pa_events` | Aggregates per-completed-PA (batter_id, pitcher_id, event_label) tuples from raw.pitches into real per-player H/HR/TB (batter) and K/BB (pitcher) totals -- the ground truth the sim's prop PMFs are validated against. | `scripts/validate_props.py` | — |
| `write_reliability_curve_to_calibration_report` | Writes the fitted win-prob curve into an on-disk SIM-406 CalibrationReport JSON file (the file the API boots calibration from), closing the loop on disk. | `scripts/validate_props.py` | `reliability_curve_for_calibration_report`<br>`similarity.similarity_calibration.CalibrationReport` |

**Depends on:** `similarity.backtesting.backtester (DEFAULT_N_BINS, OUTCOME_PROB_EPS -- reused so the binary and multi-class layers agree)`, `simulation.prop_distributions (PropDistribution, type-check only)`

**Used by:** `scripts/validate_props.py (the make validate-props entry point)`

> **Notes for anyone changing this file:** RBI, ER and OUTS are deliberately NOT derivable from raw.pitches events alone (they need per-PA runs-driven-in, earned/unearned attribution, or a per-event out count the label doesn't carry) -- DERIVABLE_BATTER_PROPS/DERIVABLE_PITCHER_PROPS intentionally exclude them so the validation never silently fabricates a wrong 'actual'. Intentional walks are excluded from the BB actual (the sim's BB PMF models only unintentional walks, since the loop's IBB is a separate managerial decision) -- a mismatch here would unfairly penalize the sim's high BB tail.

---

### `simulation/validation/replay_chi_squared.py`

SIM-325's end-to-end validation harness: replays a set of games (real or the SIM-324 calibrated no-DB reference model) through simulate_game and runs a chi-squared goodness-of-fit test comparing the simulated per-team-game run distribution to an actual/reference one, with low-expected-count tail pooling and a p>0.05 gate.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `replay_and_test` | The top-level acceptance entry point: replays historical_games, extracts their actual run totals, and runs chi_squared_gof against them. | `tests/unit/test_qa_sim325.py` | `replay_historical_games`<br>`chi_squared_gof` |
| `chi_squared_gof` | Bins simulated + reference run totals (0..max_bin, tail-open), pools adjacent bins until every expected count clears min_expected (the Cochran rule), and runs scipy.stats.chisquare, returning a ChiSquaredResult with passed = p_value > alpha. | `replay_and_test` | `bin_run_totals`<br>`pool_low_expected_bins`<br>`scipy.stats.chisquare` |
| `simulate_run_distribution / replay_historical_games` | Drive simulate_game N times (a distinct fixed seed each) via an injectable state_machine_factory (defaulting to the calibrated no-DB league machine over simulation.synthetic_bundle), returning the flat list of simulated per-team-game run totals. | `chi_squared_gof callers / tests` | `simulation.sim_loop.simulate_game`<br>`_default_state_machine` |

**Depends on:** `simulation.game_state (GameState)`, `simulation.sim_loop (GameSimResult, StateMachine, simulate_game)`, `simulation.synthetic_bundle (league_artifacts, synthetic_sampler)`

**Used by:** `tests/unit/test_qa_sim325.py`

> **Notes for anyone changing this file:** The real-data seam is HistoricalGame + a production state_machine_factory: with the real Statcast DB available, a caller builds HistoricalGame rows from actual box scores and wires a production factory, and the SAME chi-squared machinery validates against real history with no code change here -- only the sandbox path defaults to the no-DB calibrated reference model for self-consistency testing.

---
