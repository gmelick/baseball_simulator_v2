# Simulation Engine

The pitch-by-pitch simulator: the state machine, the similarity-weighted sampler, the batch runner, and everything that turns one plate appearance into a play.

*The simulator's two large classes are split into mixins: `StateMachine` (`simulation/sim_loop.py` and `simulation/loop/`) and `FullPoolSampler` (`simulation/full_pool_sampler.py` and `simulation/sampler/`). Each class keeps its name and its module; its methods live in one module per responsibility, so a reader finds a method by what it does. Use the page outline (right sidebar) to jump to a file.*

### `simulation/sim_loop.py` and `simulation/loop/`

The pitch-by-pitch state machine and the full-game driver. Every decision is a similarity-weighted draw from a hard-filtered pool, and the drawn row IS the play (the owner's rulings of 2026-08-10 and 2026-08-29). `StateMachine` is the one class; the mixins hold methods only and share the machine's `self.rng` and sampler, so the random-number order is the one the core sets.

| Module | What it holds |
|---|---|
| `simulation/sim_loop.py` | `StateMachine`'s core: `__init__`, `step_pitch`, `_record_outs` (right after `step_pitch`), `_full_pool_outcome` (the pitch draw), the run ledger (`_snapshot_bases`, `_check_bases`, `_commit_run_delta`), `_end_of_pa`, `advance_half_inning`, `_advance_batting_order`, `_set_half_matchup`. The full-game driver `simulate_game` and its helpers `_place_ghost_runner`, `_game_over_after_half`, `_is_walkoff_live`. It imports `resolve_runs` (tests patch it here) and re-exports `PlayerStatLine` and `_DOUBLE_PLAY_EVENTS` for callers outside the simulation area. |
| `simulation/loop/values.py` | The constants (`EVENT_*`, `STEAL_SAFE` / `STEAL_CAUGHT`, `REGULATION_INNINGS`, `LOOP_STREAM` / `FULL_POOL_STREAM`, `_DOUBLE_PLAY_EVENTS`, `_NEXT_BASE`, `_LATE_INNING`, `_HIGH_LEVERAGE`, `_MAX_INNINGS`), `spawn_rng_streams`, `times_through_order`, `_venue_of`, the value objects `FieldingSignal`, `StealResolution`, `RunningGameTally`, `CountAdvance`, and the count machine `advance_count`. |
| `simulation/loop/boxscore.py` | `PlayerStatLine`, `BoxScore`, `GameSimResult`. |
| `simulation/loop/in_play.py` | `InPlayMixin`: the fielding draw (`_full_pool_fielding`, `_live_fielder_at_drawn_position`), the drawn row's transition (`_normalized_dests`, `_seat`, `_resolve_in_play_transition`), the advancement draws (`_run_advancement_draws`) and `_resolve_in_play`. |
| `simulation/loop/running_game.py` | `RunningGameMixin`: the got-away advance, the steal and pickoff resolution (`_resolve_steal_outcome`, `_resolve_pickoff`, `_move_runner`, `_clear_base`), the pickoff and steal draws (`_steal_order_active`, `_lead_stealable_runner`, `_running_game_keys`, `_tally_pickoff`, `_pickoff_before_the_pitch`, `_steal_aggression`, `_steal_opportunity_draw`) and `stage_steal`. |
| `simulation/loop/terminals.py` | `TerminalsMixin`: the walk and hit by pitch (`_resolve_walk`), the intentional walk (`_issue_intentional_walk`), the strikeout (`_resolve_strikeout`), the dropped third strike (`_dropped_third_strike`, `_force_on_reach`). |
| `simulation/loop/manager.py` | `ManagerMixin`: `compute_leverage`, `_manager_rng`, `_pre_pitch_hook`, `_should_issue_ibb`, `_start_of_pa_hook`, the pitching change (`_maybe_pull_starter`, `_pitching_change_by_draw`, `_change_pitcher`, `_pick_reliever`). |
| `simulation/loop/credit.py` | `CreditMixin`: the box-score credit of a plate appearance (`_accumulate_pa`, `_current_batter_id`, `_box_line`) and the three canonical-event sets `_NON_AB_CANONICAL`, `_HIT_CANONICAL`, `_BB_CANONICAL`. |
| `simulation/loop/base.py` | `LoopMixin`: the attributes and cross-mixin methods the mixins use, declared for the type checker only (an empty class at run time). |

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `StateMachine.step_pitch` | Advances the game by exactly one pitch. It rejects an injected outcome outside `PITCH_OUTCOMES`, runs the pitching change on a plate appearance's first pitch (`_start_of_pa_hook`) and the pre-pitch hook (the intentional walk; without the running game on the pitch, the single pre-pitch steal draw). In the running game on the pitch (`_steal_order_active`) it draws and resolves the pickoff BEFORE the pitch (`_pickoff_before_the_pitch`); a pickoff that makes the third out returns a no-pitch result (`pitch_outcome = NO_PITCH`, `pa_voided = "pickoff_third_out"`: no pitch count, no event, no plate appearance; the same batter leads off the next inning). Otherwise it counts the pitch, draws (or accepts an injected) outcome, draws the steal among real pitches of the same count and class, advances the count (`advance_count`), resolves the staged steal, then on a terminal pitch resolves the ball in play, the walk, the hit by pitch or the strikeout and rolls the plate appearance. With two outs a third strike the catcher holds voids a staged steal (`steal_voided = "third_out_first"`, the owner's decision of 2026-10-01). After a steal on a strikeout pitch it hands the dropped-third-strike rule the bases and outs from before the steal. The result names the pitcher on the mound and his side (`PlayResult.pitcher_id`, `PlayResult.fielding_team`), set after the manager's hooks. | `simulate_game`<br>`simulation.play_recorder.RecordingMachine.step_pitch`<br>`simulation.play_recorder.RecordingStateMachine.step_pitch` | `_start_of_pa_hook`<br>`_pre_pitch_hook`<br>`_steal_order_active`<br>`_pickoff_before_the_pitch`<br>`advance_half_inning`<br>`_full_pool_outcome`<br>`_steal_opportunity_draw`<br>`advance_count`<br>`_resolve_steal_outcome`<br>`_resolve_got_away_advance`<br>`_resolve_in_play`<br>`_resolve_walk`<br>`_resolve_strikeout`<br>`_end_of_pa` |
| `StateMachine._full_pool_outcome` | Draws the pitch outcome for the live count from the sampler. It refreshes the half-inning pitcher weight only when (pitcher, hand, season, catcher) changes and the plate-appearance weight only when (batter, season, base-out state, conditioning inputs) changes, then calls `fp.draw(balls, strikes)`. With `SIM_GOT_AWAY` on it reads the drawn row's got-away fact. | `step_pitch` | `FullPoolSampler.new_half_inning`<br>`FullPoolSampler.new_plate_appearance`<br>`FullPoolSampler.draw`<br>`FullPoolSampler.last_pitch_got_away` |
| `StateMachine._full_pool_fielding` | On an in-play pitch, builds the fielding draw's context (the batter, the situation, and the gated platoon, batting-side, park, fielder, drawn-pitch and born-ball inputs) and calls the sampler's `battedball_new_pa`, `battedball_draw` and `last_transition` to build a `FieldingSignal` carrying the drawn row's whole base-out transition. A bundle without the transition columns raises. | `_resolve_in_play` | `FullPoolSampler.battedball_new_pa`<br>`FullPoolSampler.battedball_draw`<br>`FullPoolSampler.last_transition`<br>`_live_fielder_at_drawn_position` |
| `StateMachine._steal_opportunity_draw` | The steal decision, for the lead stealable runner (on first with second open, or on second with third open). In the running game on the pitch `step_pitch` calls it AFTER the pitch with `pitch_class`, the class of the pitch that came out: the sampler draws among real pitches of that count and that class, and the row stages a steal only. Without the class (the switch off, a bundle without classes) `_pre_pitch_hook` calls it before the pitch, and the row may carry a pickoff outcome instead, staged as `StealResolution(pickoff=True)`. The batting manager's aggression (`_steal_aggression`) weighs attempted rows only. It passes `pitch_class` only when it is not None, so a duck-typed sampler without it keeps working. | `step_pitch`<br>`_pre_pitch_hook` | `FullPoolSampler.has_steal_pool`<br>`FullPoolSampler.steal_draw`<br>`stage_steal`<br>`_lead_stealable_runner`<br>`_running_game_keys`<br>`_steal_aggression` |
| `StateMachine._pre_pitch_hook` | Runs before every pitch: the intentional walk (`_should_issue_ibb`, a draw at the measured rate of the plate appearance's cell from `sim.ibb_rates`, once per plate appearance on its first pitch), then, without the running game on the pitch and with no steal staged, the single pre-pitch steal draw. | `step_pitch` | `_should_issue_ibb`<br>`_steal_order_active`<br>`_steal_opportunity_draw`<br>`compute_leverage`<br>`_manager_rng` |
| `StateMachine._pickoff_before_the_pitch` | Draws the pickoff for the lead stealable runner through `FullPoolSampler.pickoff_draw` (the count group: the pitch rows and the pickoff rows; no manager weight) and resolves an outcome at once through `_resolve_pickoff`. An out retires the runner (a caught stealing when he was breaking); an errant throw moves him up one base; no steal credit either way. True when a pickoff outcome happened. Every draw and outcome counts on `running_game_tally`. | `step_pitch` | `FullPoolSampler.pickoff_draw`<br>`_lead_stealable_runner`<br>`_running_game_keys`<br>`_resolve_pickoff`<br>`_tally_pickoff` |
| `StateMachine._steal_order_active` | True only when the sampler's `steal_pitch_class` is exactly True (`SIM_STEAL_PITCH_CLASS`) AND its `has_steal_classes()` is exactly True (a bundle exported after DuckDB migration 0031). Otherwise the loop runs the single pre-pitch draw, so a duck-typed or mocked sampler keeps it. | `step_pitch`<br>`_pre_pitch_hook` | `FullPoolSampler.has_steal_classes` |
| `RunningGameTally` (`machine.running_game_tally`) | A slotted dataclass, one per machine, that counts what the running game did across the machine's games; it changes no play. Fields: `pickoff_draws`, `pickoff_outs`, `pickoff_errors`, `pickoffs_by_count` {(balls, strikes): n}, `no_pitch_third_outs`, `voided` {reason: n}, `d3k_reaches`, `d3k_runners_moved`. The running-game census (`scripts/probes/running_game_census.py`) reads it. | `_pickoff_before_the_pitch`<br>`_steal_opportunity_draw`<br>`step_pitch`<br>`_resolve_strikeout` | — |
| `StateMachine._resolve_steal_outcome` | The steal commit, on both non-terminal and terminal pitches (before the ball in play, the walk or the strikeout). It consumes the staged `StealResolution`, moves the runner (or removes him on a caught stealing) and commits the run or out through `_commit_run_delta`. A caught stealing can be the third out. A steal of home credits the runner's box run here and records him in `PlayResult.box_run_credited`, so `_accumulate_pa` skips exactly him; a caught runner gets NO `baserunner_advances` entry (0 means scored). A staged pickoff goes to `_resolve_pickoff`. | `step_pitch` | `_snapshot_bases`<br>`_move_runner`<br>`_clear_base`<br>`_commit_run_delta`<br>`_resolve_pickoff`<br>`_box_line` |
| `StateMachine._resolve_strikeout` | Resolves strike three. On a dropped third strike (a swinging strike three that got away, with first open or two outs) the batter reaches first; the play commits as a strikeout (the pitcher's `k`, the batter's `so`) and its forced run pays no RBI. When the pitch got away and no steal or pickoff moved anyone on it, the got-away advance runs first, on the reach too (the owner's decision of 2026-10-01): every runner moves up one base, and a runner on third scores with no RBI. | `step_pitch` | `_dropped_third_strike`<br>`_resolve_got_away_advance`<br>`_force_on_reach`<br>`_commit_run_delta` |
| `StateMachine._commit_run_delta` | The one place a resolved play becomes a run value and a base-out delta. It checks the runner-conservation identity (`Bases.assert_transition`), calls `run_resolution.resolve_runs` (the run-expectancy delta, RE24) and commits the runs and the outs onto the GameState and the PlayResult. Every resolver routes through it, and the caller supplies the state before the play (`pre_outs`, `pre_bases`). | `_resolve_steal_outcome`<br>`_resolve_pickoff`<br>`_resolve_got_away_advance`<br>`_resolve_walk`<br>`_resolve_strikeout`<br>`_resolve_in_play_transition` | `Bases.assert_transition`<br>`simulation.run_resolution.resolve_runs`<br>`_record_outs` |
| `StateMachine._record_outs` | Records `n` outs (the transient third out allowed, a fourth rejected). It is the ONE writer of a pitcher's outs on his box line (`PlayerStatLine.outs_recorded`): every out, credited to `state.pitcher_id` before the half-inning rolls, a caught stealing or a pickoff out on a pitch that does not end the plate appearance included. A unit test reads the source of `sim_loop.py` and every `simulation/loop/` module to hold that no other code adds to a box line's `outs_recorded`. | `_commit_run_delta` | `GameState.record_out`<br>`GameState.assert_outs_valid`<br>`_box_line` |
| `StateMachine._accumulate_pa` | The box-score credit of one completed plate appearance: the batter's AB / H / HR / 2B / 3B / SO / RBI, every scorer's run, and the pitcher's BF / K / BB / H / ER / R. The outs are `_record_outs`'s. A no-pitch result never reaches it. | `_end_of_pa` | `BoxScore.line` |
| `PlayerStatLine` / `BoxScore.pitchers` | One player's box line, and the box score's filter of the lines with pitching activity: an out, a batter faced, a K, a BB or an ER, so a reliever who faces one batter and retires nobody is listed. | `_accumulate_pa`<br>`_record_outs`<br>`simulation.prop_distributions`<br>`api/schemas.py (the box score model)` | — |
| `StateMachine._resolve_in_play_transition` | Applies the drawn batted-ball row's whole base-out transition to the live runners (the drawn row IS the play), runs the discretionary advancement draws and commits through `_commit_run_delta`. The one in-play path. | `_resolve_in_play` | `_normalized_dests`<br>`_seat`<br>`_run_advancement_draws`<br>`_commit_run_delta` |
| `StateMachine._run_advancement_draws` | On a single, a double or a caught fly ball, draws the five discretionary extra-base scenarios (lead runner first, no passing) from `FullPoolSampler.advancement_draw` and applies each attempted / safe / error-extra outcome to the bases. A tag from third that scores relabels the play a sacrifice fly. | `_resolve_in_play_transition` | `FullPoolSampler.advancement_draw`<br>`_live_fielder_at_drawn_position` |
| `simulate_game` | The full-game driver: spawns two independent random streams (the loop's and the sampler's) from one seed, builds or wires the machine and the initial GameState, seeds the pen, the rest map, the managers and their profiles, then calls `step_pitch` under the regulation, walk-off and extra-innings (ghost runner) rules until the game ends. A no-pitch result (`PlayResult.no_pitch`) does not count toward `total_pitches`. It raises `ValueError` before the first pitch when a side has no batting order or the game names one side's starting pitcher and not the other's. It returns a `GameSimResult` with the box score, `outs_played` (the outs the play stream holds; the pitchers' box lines equal it) and the inning grid. `start_state` starts the game at a plate appearance of the real game. | `simulation.batch_runner._run_one`<br>`simulation.play_recorder.record_game_plays`<br>`simulation.validation.replay_chi_squared`<br>`scripts/sim_stats.py and the probes` | `spawn_rng_streams`<br>`StateMachine.step_pitch`<br>`_place_ghost_runner`<br>`simulation.start_state` |
| `GameSimResult.home_by_inning / away_by_inning` | The inning grid: each team's runs per inning, one cell per inning. `0` marks a half played without a run; `None` marks a half never played (the scoreboard "x"). The two lists have the same length. `simulate_game` reads the score at the start of each half and appends the batting side's runs when the half ends, after the runs are credited. A walk-off half, a half cut by the inning or pitch ceiling, and a half ended by a no-pitch pickoff each get a cell. The grid reads the state only and draws no random number. Both fields are `None` on a result built by hand. The segment and team markets are priced from these cells. | `simulation.results.GameSimSummary.from_results` | — |
| `StateMachine._maybe_pull_starter / _pick_reliever / _change_pitcher` | The pitching change, once per plate appearance on its first pitch. With `manager_draw` on (`SIM_MANAGER_DRAW`), ONE draw from the change opportunity pool's hard cell at the pool's own rate, the fielding manager's usage similarity a weight (`manager_key`; power `SIM_ACTOR_POWER_MANAGER_USAGE`). A drawn change picks the entering arm by `FullPoolSampler.relief_arm_draw` over the live pen; with every reliever weight off, or no sampler, the pick is positional (the first arm in a high-leverage late spot, else the last). With the draw off nobody is changed. `_change_pitcher` records the change in `manager_decisions`. | `_start_of_pa_hook` | `_pitching_change_by_draw`<br>`FullPoolSampler.pitching_change_draw`<br>`FullPoolSampler.relief_arm_draw` |
| `StateMachine._steal_aggression` | The batting manager's weight on the steal draw's attempted rows: his measured `steal_order_rate_per_1b_opp` over the season's league mean (`derived.league_averages`, entity 'manager'), clamped to [0.05, 4.0]; exactly 1.0 with no manager, no profile or no league row. | `_steal_opportunity_draw` | — |

**Depends on:** `simulation.constants`, `simulation.flags`, `simulation.game_state`, `simulation.run_resolution`, `simulation.start_state`, `simulation.full_pool_sampler (a duck-typed sampler passed in)`

**Used by:** `simulation.batch_runner`, `simulation.production_factory`, `simulation.play_recorder`, `simulation.results`, `simulation.validation.replay_chi_squared`, `api/routes/games.py`, `api/routes/betting.py`, `db/sim_store.py`, `scripts/sim_stats.py`, `scripts/accuracy_comparison.py`, `scripts/validate_props.py` and the probes

**Environment flags read here:** `SIM_GOT_AWAY` and `SIM_BB_PLATOON` (read once per machine in `StateMachine.__init__` through `simulation.flags.read_bool`). The factory turns `SIM_MANAGER` into the `manager` gate and `SIM_MANAGER_DRAW` into `machine.manager_draw`; `SIM_STEAL_PITCH_CLASS` reaches the loop through the sampler's `steal_pitch_class`.

> **Notes for anyone changing this file:** A new method goes in the mixin of its responsibility; the core stays in `sim_loop.py`. Four tests read source: `test_run_ledger.py` (the snapshot above each mutator, through `inspect.getsource` of the method), `test_pitcher_outs_credit.py` (the one outs writer, over `sim_loop.py` and `simulation/loop/`), `test_game_page_whatif.py` (patches `sim_loop._place_ghost_runner`) and the acceptance bands' citations of `sim_loop.py`. Every ledger call snapshots `pre_bases` with `_snapshot_bases` BEFORE any change to `state.bases`: several resolvers change the bases in place. `resolve_runs` must return `method == 're24_delta'`; the ledger asserts it. Every reader of a per-pitch `PlayResult` must skip a no-pitch result (`PlayResult.no_pitch`): it has no pitch, no count change and no plate appearance, but it carries the pickoff's out. There is ONE in-play path: a bundle without the transition columns raises.

---

### `simulation/full_pool_sampler.py` and `simulation/sampler/`

The similarity-weighted sampler: it scores a hard-filtered play pool (the pitch pool, the batted-ball pool, the steal opportunity pool, the advancement pools, the change opportunity pool) by the applicable similarity engines and draws one row by that weight: no top-K, no formulas. Every simulator decision is one draw through `FullPoolSampler`. The class keeps its constructor in `full_pool_sampler.py`, which calls one `_init_*()` per mixin; the mixins share `self.rng`.

| Module | What it holds |
|---|---|
| `simulation/full_pool_sampler.py` | `FullPoolSampler` and its constructor (`artifacts`, `rng`, `sit_sigma`, `platoon_off_weight`, `home_off_weight`). |
| `simulation/sampler/kernels.py` | The module-level helpers: `_repower_neutral` and `_raise_neutral` (a factor at a power, unscored rows draw-neutral), `_PITCH_FEATURE_WEIGHTS` (the pitch engine's weights, pinned to the engine by a test), `_group_rows`, `_count_cell`. |
| `simulation/sampler/base.py` | `BaseMixin`: `_pool_meta`, the as-of cutoff (`set_asof`, `_asof_mask`, `_apply_asof`), the draw-neutral means (`_pitcher_prof_score`, `_neutral_mean_pitcher`, `_batter_matrix_on`, `_batter_cols`, `_batter_unscored`, `_batter_live_scores`, `_neutral_mean_batter`, `_batter_factor_rows`), the actor matrices (`_actor_matrix`, `_emb_to_mat`, `_matrix_gather`), `_emb_z`, `_actor_factor`. Its `TYPE_CHECKING` block declares the cross-mixin names. |
| `simulation/sampler/pitch.py` | `PitchMixin`: the pitch factors (`_f_pitcher`, `_f_batter`, `_f_situation_baseout`), `new_half_inning`, `new_plate_appearance`, the cell index (`_cell_meta`, `_subcell_rows`, `_new_plate_appearance_cell`, `cell_index_stats`), fatigue (`_fatigue_rows`, `_f_fatigue`) and `draw`. |
| `simulation/sampler/pitch_result.py` | `PitchResultMixin`: the pitch-result draw (`_pp_geom_z_stats`, `_f_result_pitch`, `_result_inv_density`, `result_weights`, `_result_draw`), the last-pitch readers (`last_result_row`, `last_born_batted_ball`, `last_pitch_got_away`, `last_pitch_geom`), the receiving ratio (`_receiving_on`, `_recv_factor`, `_apply_receiving`). |
| `simulation/sampler/fielding.py` | `FieldingMixin`: the batted-ball draw (`battedball_new_pa`, `battedball_draw`, `last_transition`, `last_battedball_fielder`, `has_battedball`, `has_transition`, `_transition_meta`), its factors (batter, park, live fielder, platoon), the class filter, the born-ball and pitch-similarity kernels. |
| `simulation/sampler/fence.py` | `FenceMixin`: the fence stage (`in_wall_zone`, `_fence_line`, `fence_at`, `fence_decision`, `_fence_rows`, `_bb_hr_mask`), the carry (`_carry_offsets`, `carry_shift`, `carry_of`, `_born_in_live_air`), the wall-margin band (`_bb_margins`, `_bb_margin_rows`, `_bb_margin_stage`). |
| `simulation/sampler/steal.py` | `StealMixin`: `has_steal_pool`, `_steal_meta`, `has_steal_classes`, `_steal_pick`, `steal_draw`, `pickoff_draw`, `steal_weights`. Its warning logs as `simulation.full_pool_sampler`. |
| `simulation/sampler/advancement.py` | `AdvancementMixin`: `has_advancement`, `_adv_meta`, `advancement_draw`. |
| `simulation/sampler/manager.py` | `ManagerMixin`: the change draw (`_change_meta`, `_change_rows`, `pitching_change_draw`, `_change_manager_factor`, `_change_pitcher_factor`, `last_change_row`), the reliever draw (`relief_weights_on`, `relief_arm_draw`) and `_pitcher_prof_scores` (uncached; zeros where `_pitcher_prof_score` returns None, so keep both). |

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `FullPoolSampler.new_half_inning / new_plate_appearance / draw` | The pitch-draw lifecycle: `new_half_inning` caches the half-inning base (the pitcher factor at the pitch power, times recency, the cutoff applied) and stages the catcher; `new_plate_appearance` assembles the plate appearance's weight (the batter factor times the situation kernel times the gated fatigue, batting-side and receiving weights) as twelve count-bucket CDFs (cumulative distribution functions), over the whole pool or, with the cell index on, the plate appearance's cell; `draw(balls, strikes)` picks one row in the live count's bucket, then the result row when the pitch-result split is on. | `StateMachine._full_pool_outcome` | `_f_pitcher`<br>`_f_batter`<br>`_f_situation_baseout`<br>`_new_plate_appearance_cell`<br>`_result_draw` |
| `FullPoolSampler.steal_draw` | Draws ONE row of a target base's steal opportunity pool, weighted by `steal_weights`. Without `pitch_class` the hard filter is the (outs, balls, strikes) group of pitch rows, and it returns (attempted, success, pickoff_out, pickoff_advancing, pickoff_error), or None when the pool or the group is absent. With `pitch_class`, `steal_pitch_class` on and a pool with classes, the hard filter is the (outs, balls, strikes, class) group of pitch rows; the group answers from its own rows however few, an empty group returns None (no fallback), and the three pickoff fields read False. | `StateMachine._steal_opportunity_draw` | `_steal_meta`<br>`steal_weights`<br>`_steal_pick` |
| `FullPoolSampler.pickoff_draw` | The pickoff draw before the pitch: ONE row of the (target, outs, balls, strikes) count group, every row a candidate (the pitch rows and the pickoff rows), weighted by `steal_weights` with no manager weight -> (out, advancing, error). None, with no random number used, when the pool has no classes, the group is absent, or the group holds no pickoff outcome. | `StateMachine._pickoff_before_the_pitch` | `_steal_meta`<br>`steal_weights`<br>`_steal_pick` |
| `FullPoolSampler.steal_weights` | The one weight path of the steal and pickoff draws -> (pool, rows, w), or None when the pool or the group is absent; it uses no random number. The weight is recency x the score-margin Gaussian (`steal_score_sigma`, 2 runs) x the runner (`runner_steal`), pitcher (`pitcher_steal`) and catcher (`catcher_throwing`) score-matrix factors x the manager's aggression on attempted rows. The running-game replay reads it whole, so the replay and the draws cannot disagree. | `steal_draw`<br>`pickoff_draw`<br>`scripts/running_game_replay.py` | `_steal_meta`<br>`_actor_factor` |
| `FullPoolSampler._steal_meta / has_steal_classes` | The per-target precompute of the steal pool's groups. `cells`: (outs, balls, strikes) -> the pitch rows (the pickoff rows, marked by `is_pickoff_row`, always left out). Only when EVERY pitch row carries a class: `pickoff_cells` (the count group, every row), `pickoff_live` (the count groups that hold a pickoff outcome) and `class_cells`: (outs, balls, strikes, class code) -> the pitch rows of one class. A pool where some pitch rows have no class keeps the single draw over all its pitch rows and logs a warning. `has_steal_classes()` (cached) is True only when every target's pool is fully classed. | `steal_weights`<br>`pickoff_draw`<br>`StateMachine._steal_order_active`<br>`simulation.production_factory.warm_worker_cache` | `_group_rows`<br>`_count_cell` |
| `FullPoolSampler.battedball_new_pa / battedball_draw / last_transition` | The in-play draw. The filters: the live base-out cell (always; a bundle without the transition columns raises), the born ball's class (`SIM_BB_CLASS_FILTER`), the fence stage (`SIM_FENCE_STAGE`: the born ball's carry against the live park's fence at its direction and the season's fence group decides home run, no home run or the band) and the wall-margin band (`SIM_BB_MARGIN_BAND`: a wall-zone air ball the fence did not call over draws only rows whose margin to their own park's fence is within the band of the born ball's margin to the live fence). With `SIM_CARRY_OFFSET` the born ball's distance is first re-read in the live park's air (`carry_of`, the document's `carry_offset_ft`); one working copy (`_born_in_live_air`) feeds every reader. The weights: the batter factor, the situation kernel and recency, then the gated platoon, batting-side, park, live-fielder, drawn-pitch and born-ball weights; the born kernel measures on the born ball's class with the exponent per feature under `SIM_BB_BORN_PER_FEATURE`. Counters: `fence_counts` (over, short, band, passed, no matching rows, not an air ball), `bb_margin_counts` (applied, fallback, skipped), `bb_class_counts`, `bb_zero_weight_count`. | `StateMachine._full_pool_fielding` | `_transition_meta`<br>`_bb_class_rows`<br>`_fence_rows`<br>`_bb_margin_stage`<br>`_bb_batter_factor`<br>`_f_live_fielder`<br>`_f_pitch_similarity`<br>`_f_born_similarity` |
| `FullPoolSampler.advancement_draw` | Draws ONE row of a (scenario, from base, target base) advancement pool, weighted by recency, a throw-geometry Gaussian, the runner (`runner_adv`) and live-fielder (`fielder_<POS>`) score-matrix factors -> (attempted, safe, error_extra). | `StateMachine._run_advancement_draws` | `_adv_meta`<br>`_actor_factor` |
| `FullPoolSampler.pitching_change_draw` | Draws whether the manager changes pitchers at a plate-appearance boundary from the change pool's hard cell (starter or reliever, half-inning boundary, pitch-count bucket, times through the order), widened below `change_min_cell`, weighted by recency, a situation Gaussian, the pitcher-similarity factor and the live manager's usage-similarity row (`_change_manager_factor`, power `actor_power['manager_usage']`, 0 = off; an unscored row is draw-neutral). It remembers the drawn row for `relief_arm_draw` and `last_change_row`; `change_ess_stats` accumulates the cell's effective-sample share per draw (the manager power's starvation guard). | `StateMachine._pitching_change_by_draw` | `_change_meta`<br>`_change_pitcher_factor`<br>`_change_manager_factor` |
| `FullPoolSampler.relief_arm_draw` | Draws the entering arm's index in the live pen's candidates, by each candidate's resemblance to the arm the last drawn change row brought in: role (`relief_role_sigma`), stuff (`relief_pitcher_power`), rest (`relief_rest_sigma`), pitched in the last two days (`relief_pitched2d_off_weight`), pitches over three days (`relief_pitches3d_sigma`) and hand (`relief_hand_off_weight`). Every weight off -> None (the loop keeps its positional pick); a candidate without a fact is neutral for that weight. `relief_draw_counts` = [draws, uniform draws]. | `StateMachine._pick_reliever` | `_pitcher_prof_scores`<br>`EngineArtifacts.change_roles` |
| `FullPoolSampler._matrix_gather` / `_actor_factor` | The one actor-score-matrix lookup: the live actor's row of a nightly `EngineArtifacts.actor_sim` matrix, gathered onto each pool row's embedding index and raised to the actor's power (`SIM_ACTOR_POWER_<NAME>`); an unscored row is neutral (1.0 at power 1, the mean scored weight at any other power). `_actor_factor(matrix, live_key, emb_rows, rows)` wraps it for the steal and advancement draws. | `_f_batter`<br>`_f_live_fielder`<br>`_actor_factor`<br>`steal_weights`<br>`advancement_draw` | — |

**Depends on:** `pipeline.batch.engine_artifacts (EngineArtifacts, HandPool, StealPool, STEAL_PITCH_CLASS_CODE, carry_predict, parse_group_key, recv_block_cell, recv_zone_group)`, `simulation.filter_cells`, `simulation.constants (FIELD_POSITION_NAMES)`

**Used by:** `simulation.sim_loop.StateMachine (a duck-typed full_pool_sampler attribute)`, `simulation.production_factory (builds and configures the production instance)`, `simulation.synthetic_bundle.synthetic_sampler (the no-database instance)`, `simulation.batch_runner`

**Environment flags read here:** none directly. Every switch, power and bandwidth (`actor_power`, `pitch_result_split`, `catcher_receiving`, `steal_pitch_class`, `bb_class_filter`, `fence_stage`, `pitch_cell_index`, `home_off_weight`, `park_sigma`, the fatigue sigmas, ...) is a plain attribute that `simulation.production_factory`'s `apply_*_env` functions set.

> **Notes for anyone changing this file:** A new draw gets its own mixin (or joins the one of its responsibility), its attributes in that mixin's `_init_*`, and any name another mixin reads declared in `BaseMixin`'s `TYPE_CHECKING` block. `test_pool_cutoff.py` counts `self._apply_asof(` over the whole sampler source (5 draws read a dated pool). `has_steal_pool()` gates the steal and pickoff draws; `has_steal_classes()` gates the running game on the pitch. Every draw returns None on a missing pool or group, EXCEPT the batted-ball draw (`battedball_new_pa`) and the pitch cell index's last widening level, which raise: a missing base-out cell is a data defect. Every switch, power and bandwidth defaults to the value that makes its factor a no-op (a power of 1.0, a sigma of 0.0, a weight of 1.0), except `steal_pitch_class`, which defaults to True (the owner's ruling of 2026-09-16: every change lands ON).

---

### `simulation/game_state.py`

The data contract of the subsystem. `GameState` is the mutable state of one game: the count, the outs, the bases, the score, the inning, both batting orders, both defenses, the managers and the pens. `Bases` holds the base occupancy and the runner identities, with the conservation guards. `ManagerContext` holds the manager's per-pitch signals. `PlayResult` is the result of one pitch: the outcome, the event, the run value and its provenance, the running-game fields and the no-pitch result.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `Bases.assert_transition` | The conservation check of one play: runners_before + batter_reached == runners_after + runners_scored + runners_retired. With `batter_id` it also checks that the batter is not on base before the play and that every runner after it is a runner from before or the batter (an identity swap). It checks a transition; it cannot derive one. | `simulation.sim_loop.StateMachine._commit_run_delta` (every resolver commits through it) | `Bases.assert_consistent` |
| `GameState.bat_hand_for` | The batter's hand for the draws' hand filter. A switch hitter ('S') bats opposite the current pitcher's `throw_hand`; an unknown batter keeps the current `bat_hand`. | `StateMachine._advance_batting_order`<br>`StateMachine._set_half_matchup`<br>`simulation.sim_loop.simulate_game`<br>`simulation.start_state` | — |
| `NO_PITCH` / `PlayResult.no_pitch` / `pa_voided` / `steal_voided` | `NO_PITCH = "no_pitch"` is the `pitch_outcome` of a result with no pitch thrown: a pickoff before the pitch that made the third out (`pa_voided = "pickoff_third_out"`). It is not in `PITCH_OUTCOMES`, so `advance_count` never sees it; `no_pitch` is True on such a result. `steal_voided = "third_out_first"` marks a staged steal voided because, with two outs, a third strike the catcher held is the third out first. A reader that counts pitches, plate appearances or count transitions skips a no-pitch result; the result still carries the pickoff's out (`outs_recorded` 1), which the linescore and the pitcher decisions need. | `StateMachine.step_pitch` (sets them)<br>`simulate_game` (reads `no_pitch`)<br>`simulation.snapshots` (skips the result) | — |
| `PlayResult.pitcher_id` / `PlayResult.fielding_team` | The pitcher on the mound for this result and his side (`Team`); both default to None. `step_pitch` sets both after the manager's hooks, so a reliever who enters on this pitch is the one named. The intentional-walk result and the no-pitch result carry them too. Read them, not `next_state`, for who pitched a play: on the play that ends a half-inning, `next_state` already names the next half's pitcher. | `StateMachine.step_pitch`<br>`simulation.loop.terminals.TerminalsMixin._issue_intentional_walk` | — |
| `GameState.assert_invariants` | Runs the committed-state guards together: the count, the outs, the score and the base consistency (without a `batter_id`). | `StateMachine.step_pitch`<br>`StateMachine._end_of_pa`<br>`StateMachine.advance_half_inning`<br>`simulate_game` | `GameState.assert_count_valid`<br>`GameState.assert_outs_valid`<br>`GameState.assert_score_valid`<br>`Bases.assert_consistent` |

**Used by:** every other module in `simulation/` (the shared contract), `api/schemas.py` and `api/routes/games.py` (build and read a GameState), `simulation.lineup_resolver` (constructs a GameState), `simulation.snapshots`, `simulation.linescore` and `simulation.pitcher_decisions` (read GameState and PlayResult fields)

> **Notes for anyone changing this file:** `Bases` changes in place, and several resolvers change `state.bases` before their own ledger call. So every resolver snapshots `pre_bases` with `_snapshot_bases` before any change: a before-state read after the change mis-values the play (a sac fly read +0.76 against a true -0.13, SIM-499). `pitcher_id` is the current fielding pitcher; `home_pitcher_id` / `away_pitcher_id` hold each side's current pitcher, and `home_starter_id` / `away_starter_id` each side's starter. `pitcher_decisions.py` reads who pitched each play from the play's own `pitcher_id` / `fielding_team`, and from the next state's half and `pitcher_id` only on a play that does not carry them. The 3-bit `runners_state` encoding (bit0 = 1B, bit1 = 2B, bit2 = 3B) is shared with the RE24 matrix keys of `run_resolution` and the play pool's bitmask columns; a change to it breaks every consumer silently.

---

### `simulation/run_resolution.py`

The run value of one play: the run-expectancy delta (RE24: the expected runs from each of the 24 base-out states to the end of the inning) between the state before the play and the state after it, plus the runs that scored. The caller supplies both states; nothing here derives one.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_runs` | Given `pre_outs` / `pre_runners_state`, `post_outs` / `post_runners_state` and `result_runs` (all five required), returns `RunResolution(runs = RE(post) - RE(pre) + result_runs, method='re24_delta', ...)`. Raises `ValueError` on a missing argument or an impossible transition (negative outs, removed outs, negative runs). | `simulation.sim_loop.StateMachine._commit_run_delta` | `re24_value`<br>`simulation.constants.resolve_event_to_canonical` |
| `re24_value` | The run expectancy of (outs, runners_state) in the bundled `RE24_MATRIX` (or a matrix passed in); 0.0 at three or more outs. | `resolve_runs` | — |

**Depends on:** `simulation.constants`

**Used by:** `simulation.sim_loop` (the one caller: every run value in the simulator comes from here)

> **Notes for anyone changing this file:** `_commit_run_delta` raises an AssertionError when `rr.method != 're24_delta'`, so no other method can reach a committed run value. The bundled `RE24_MATRIX` is a static table for a ~2024 MLB run environment. `re24_from_rows()` builds a matrix of the same shape from the `derived.run_expectancy_matrix` rows, but no caller passes a `matrix`: the simulator always uses the bundled table.

---

### `simulation/results.py`

The aggregate of N simulated games of one matchup: a list of per-game `GameSimResult` objects becomes one `GameSimSummary` (the win shares, the score means and medians, the raw per-iteration arrays, Wald (normal-approximation) confidence intervals and the inning grids). It re-exports `GameSimResult`, `PlayerStatLine` and `BoxScore` from `simulation.loop.boxscore`, so a consumer has one import home.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `GameSimSummary.from_results` | Aggregates N results: the home, away and tie shares, the score means and medians, the raw score arrays, the Wald intervals on the win share and the means, and the inning grids. | `simulation.batch_runner.BatchRunner.run / run_job`<br>`simulation.win_probability.win_probability` (given a raw list) | `_proportion_ci`<br>`_mean_ci` |
| `GameSimSummary.inning_grids` | `(home_cells, away_cells)` per iteration, in input order, read off each result's `home_by_inning` / `away_by_inning`. `SegmentRuns.from_inning_grids` reads this light form. It rides in the summary through the runner's cache, stored as JSON (`to_json` / `from_json`, `simulation/cache_codec.py`), so the edge route prices the twelve segment and team markets without simulating again. It is None for the whole summary when any one result lacks a grid. It is the LAST field of the slotted dataclass: a pickled summary with no grid slot restores, and `__getattr__` reads `inning_grids` as None (every other missing name still raises). The wire models do not carry the grid. | `simulation.game_market_distributions.segment_runs_from_summary`<br>`api/routes/games.py` (`_sim_summary_lite_from_stored` drops it) | — |

**Depends on:** `simulation.loop.boxscore` (`GameSimResult`, `BoxScore`, `PlayerStatLine`)

**Used by:** `simulation.batch_runner`, `simulation.win_probability`, `simulation.prop_distributions` (imports `ConfidenceInterval`), `simulation.cache_codec`, the API routes and the scripts

> **Notes for anyone changing this file:** Every interval is a closed-form Wald interval, not a bootstrap: deterministic and O(1). With n < 2 a score-mean interval collapses to zero width at the one observation (an honest degenerate answer, not an error).

---

### `simulation/win_probability.py`

The calibrated home and away win probability. It turns a `GameSimSummary` (or a raw result list) into a probability through tie handling (SPLIT or DROP), Beta smoothing (so 0/N never gives a hard 0.0; the Jeffreys prior, alpha 0.5, by default) and an optional fitted reliability-curve `CalibrationMap`.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `win_probability` | Aggregates a raw list to a `GameSimSummary`, folds the ties by `TieHandling`, takes the Beta(alpha, alpha) posterior mean, applies `calibration_map.apply(p)` and computes a Wald interval at the effective N. | `api/routes/betting.py` (`_summary_and_winprob`)<br>`scripts/validate_props.py`<br>`scripts/accuracy_comparison.py` | `simulation.results.GameSimSummary.from_results`<br>`CalibrationMap.apply`<br>`_smoothed_proportion_ci` |
| `CalibrationMap.from_report` | Builds a monotone piecewise-linear p -> p map from a calibration report's `reliability_curve` anchor points (fitted by `simulation.prop_validation.fit_reliability_curve`, written by `write_reliability_curve_to_calibration_report`). A missing or too-short curve gives the identity map. | `api/main.py` (the lifespan)<br>`api/state.py`<br>`scripts/accuracy_comparison.py` | — |

**Depends on:** `simulation.results` (`ConfidenceInterval`, `GameSimResult`, `GameSimSummary`)

**Used by:** `api/routes/betting.py`, `betting/clv_engine.py` (the `WinProbability` type), `scripts/validate_props.py`, `scripts/accuracy_comparison.py`

> **Notes for anyone changing this file:** The identity map (`IDENTITY_CALIBRATION`) is the default. A fitted curve reaches this module only as a calibration report the caller passes in; this file never reads `/data/calibration.json`. The betting edge reads its win probability from here, so a change to `alpha` or the tie policy changes every moneyline edge the closing-line value (CLV) comparison grades.

---

### `simulation/prop_distributions.py`

The prop aggregator: N per-game box scores become, for each player and each prop, a full probability mass function (PMF) over the integers, with over/under queries. The betting layer prices a book line against these PMFs. Fifteen props (the fifteen prop markets the book posts, SIM-421): pitchers K / BB / ER / OUTS / H_ALLOWED, batters H / HR / RBI / TB / 1B / 2B / 3B / R / SB / HRR.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `PITCHER_PROPS / BATTER_PROPS / ALL_PROPS` | The prop-name tuples: `("K", "BB", "ER", "OUTS", "H_ALLOWED")` and `("H", "HR", "RBI", "TB", "1B", "2B", "3B", "R", "SB", "HRR")`. The four core props come FIRST in each, because the frontend renders a player's prop chips in tuple order. `simulation.prop_validation`'s `BOXSCORE_*` tuples and `pipeline/odds_provider.py`'s `PROP_STAT_TO_MODEL_PROP` name the same fifteen; unit tests (`test_odds_vocabulary.py`, `test_prop_wrappers.py`, `test_segment_markets.py`) keep the three in step. | `api/routes/games.py` (`_VALID_PROP_NAMES = frozenset(ALL_PROPS)`)<br>tests | — |
| `_PROP_EXTRACTORS` (+ `_total_bases`, `_singles`, `_hits_runs_rbi`) | One extractor per prop off a single-game `PlayerStatLine`. TB = h + b2 + 2·b3 + 3·hr (exact); 1B = h - b2 - b3 - hr; HRR = h + r + rbi summed PER GAME and then made a PMF (a convolution of the three marginal PMFs treats them as independent, but a home run is all three at once); H_ALLOWED reads the pitcher's `h_allowed`, a field apart from the batter's `h`. | `PropDistributionSet.from_boxscores` | — |
| `PropDistributionSet.from_boxscores / from_results` | Builds every player's PMF per prop across N games. A game the player missed counts as zero, so the denominator is always N. Batting activity (an AB, a hit, an RBI, a run, a steal, a double or a triple) gives the batter props, so a pinch runner who only scores or steals owns them. Pitching activity (an out, a batter faced, a K, a BB, an ER or a hit allowed) gives the pitcher props, so a reliever who faces one batter and retires nobody owns them. | `simulation.batch_runner.BatchRunner.run_prop_set / run_job` (`from_boxscores`)<br>`scripts/validate_props.py`<br>`scripts/accuracy_comparison.py` (`from_results`) | `PropDistribution.from_samples`<br>`_PROP_EXTRACTORS` |
| `PropDistribution.p_over / p_under / p_push` | The over/under at the sportsbook convention: a half-integer line has no push; an integer line pushes on an exact match. | `betting/clv_engine.py`<br>`api/routes/games.py`<br>`simulation.prop_validation.validate_prop_over_under` | — |

**Depends on:** `simulation.results` (`BoxScore`, `ConfidenceInterval`, `GameSimResult`, `PlayerStatLine`)

**Used by:** `simulation.batch_runner`, `simulation.cache_codec`, `simulation.prop_validation`, `betting/clv_engine.py`, `api/routes/games.py`, `api/schemas.py`, `scripts/validate_props.py`, `scripts/accuracy_comparison.py`

> **Notes for anyone changing this file:** A player with no pitching or batting activity across the whole run is absent from `PropDistributionSet.by_player` (never an all-zero PMF). The activity net here is WIDER than `BoxScore.batters` (an AB, a hit or an RBI) and `BoxScore.pitchers` (an out, a batter faced, a K, a BB or an ER) in `simulation/loop/boxscore.py`; only a hit allowed separates the two pitcher nets. A new prop needs a tuple entry (appended, never re-ordered), an extractor and the matching odds-vocabulary entry in `pipeline/odds_provider.py`.

---

### `simulation/snapshots.py`

Pure data shapes and builders for the replay surface: `FieldSnapshot` (the field graphic), `PlayByPlay` / `PlayByPlayEntry` (the per-pitch `/plays` scroll), `PitchContext` (the state before one pitch), `StateAtPitch` (a point-in-time snapshot) and `OverrideDelta` (a baseline-against-override comparison). No mutation, no database.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `FieldSnapshot.from_game_state` | Builds the field-graphic state (the defense, the batter, the runners, the count, the outs and the score) from a GameState and an optional `defense_positions` map; without the map the nine slots are empty. | `StateAtPitch.from_game_state` | — |
| `PlayByPlay.from_play_results` | Groups a flat, ordered PlayResult stream into plate appearances by `pa_terminal` (or a pitch that ends the half), one `PlayByPlayEntry` per pitch. The optional `contexts` (the recorder's `PitchContext` per result) give each entry its inning, half, outs before and batter; the pitcher and the score after come from the result. A no-pitch result makes no entry, so `sequence` stays contiguous; when the voided plate appearance already held pitches, the next pitch starts a new at-bat. `thrown_pitches(results)` returns the results that hold a pitch, one per entry, in order. | `api/routes/games.py` (`_build_replay_artifacts`) | `PlayByPlayEntry.from_play_result` |

**Depends on:** `simulation.game_state` (`NO_PITCH`, `GameState`, `Half`, `PlayResult`)

**Used by:** `api/routes/games.py`, `simulation.play_recorder` (`PitchContext`)

> **Notes for anyone changing this file:** Player labels are optional (a `Mapping[int, str]`); with none, `PlayerRef` shows `#<id>`, so a shape renders alone in a test with no join to a players table. `_build_replay_artifacts` pairs `thrown_pitches(plays)` with the entries one-to-one for the per-pitch state snapshots, and gives the linescore and the decisions the whole stream.

---

### `simulation/game_market_distributions.py`

The simulator prices EVERY game market the book posts (the owner's ruling of 2026-09-12, SIM-421). Per-iteration inning grids become the probability each segment and team market needs: the first-inning and first-five moneyline (three-way), total and run line, each side's team totals, the first team to score and "a run in the first inning". Each market kind keeps the convention the full-game markets use in `betting/clv_engine.py`.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `SegmentRuns` | The per-iteration run totals every market settles on: each team's full-game, first-inning and first-five runs, and which team scored first (`HOME_FIRST` / `AWAY_FIRST` / `NOBODY_FIRST`). Built by `from_inning_grids` (`(home_cells, away_cells)` per iteration, the light form a worker carries) or `from_official_grid` (one iteration from `raw.games.inning_scores`, the real outcome). An unplayed half ("x") counts as zero runs. `segment_runs_from_summary(summary)` builds one from a `GameSimSummary`'s `inning_grids`, or returns None when the summary has none. | `scripts/accuracy_comparison.py`<br>`api/routes/betting.py` (`_build_edge_reports`, through `segment_runs_from_summary`; the settlement, through `from_official_grid`) | `pipeline/odds_provider.py` (`GAME_MARKET_KIND` / `SEGMENT` / `SIDE`) |
| `total_probabilities / side_probabilities / cover_probabilities` | (over, under, push) for a total-kind market at a line (`first_inning_run` is the total kind at 0.5); (home, away, draw) for a side market over its segment (a completed full game has no tie; the first team to score has none); (home covers, away covers, push) for a run line at the HOME spread, as `betting.clv_engine.spread_cover_prob` does. | `market_probability`<br>`api/routes/betting.py` (`side_probabilities`) | — |
| `market_probability(runs, market_type, *, line, side="home")` | The simulator's probability of the FIXED reference side (HOME on a side or run-line market, OVER on a total): the number the accuracy comparison pairs with the closing line. A run line also takes `side="away"`, with `line` the away team's own spread: the away bet of a run line the book lists as two separate bets (the run-line scoring, SIM-549). | `scripts/accuracy_comparison.py` | — |
| `market_outcome(actual, market_type, *, line, side="home")` | The 0/1 label of the reference side on ONE real result, or None on a push (a total on the line, an adjusted margin of zero, a tied segment on a two-way side market). A tied segment on a THREE-way market is a home loss, not a push: the draw is a priced outcome. A run line's away bet at its own spread (`side="away"`) covers when away − home + line > 0. | `scripts/accuracy_comparison.py`<br>`api/routes/betting.py` (the settlement)<br>`scripts/probes/book_sharpness.py` | — |

**Depends on:** `pipeline/odds_provider.py`, `numpy`

**Used by:** `scripts/accuracy_comparison.py`, `api/routes/betting.py`

> **Notes for anyone changing this file:** Pure numpy over per-iteration arrays: no database, no sampler, no random draw. The API's `/edges` and `/signals` read this module: the runner's cached summary carries each iteration's inning grid (`GameSimSummary.inning_grids`), and the route prices the twelve segment and team markets from it. None of those probabilities is calibrated; only the full-game moneyline goes through the reliability curve.

---

### `simulation/linescore.py`

The per-inning runs, hits and errors (R/H/E) grid, derived from one game's ordered PlayResult stream. No database, no change to the loop.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `linescore_from_plays` | Walks the stream and credits each play's runs, hits and errors to the (inning, half) it was played in. A play whose `next_state` already names the next half (it recorded an out and the committed outs are back at 0) goes back to the half it ended. It returns the `Linescore` with one `InningLine` per inning (None = an unplayed half, e.g. a skipped bottom of the ninth or a walk-off). | `api/routes/games.py` (`_build_replay_artifacts`; `/linescore` and `/card` read the stored card)<br>`scripts/accuracy_comparison.py` | `_is_hit` |

**Depends on:** `simulation.game_state` (`Half`, `PlayResult`)

**Used by:** `api/routes/games.py`, `scripts/accuracy_comparison.py`

> **Notes for anyone changing this file:** An error is charged to the FIELDING side, not the batting side. A play with `outs_recorded > 0` whose committed `next_state.outs == 0` ended a half-inning, and this module re-derives which half it happened in. A no-pitch result (a pickoff third out before the pitch) is such a play: it carries the out and no run, hit or error. A play with no `next_state` is skipped entirely.

---

### `simulation/pitcher_decisions.py`

The winning, losing and save pitchers of one game, derived from its ordered PlayResult stream: the MLB pitcher-of-record rules, the five-inning starter rule (MLB Rule 9.17(b)) and the save's three-run lead clause.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `decisions_from_plays` | Walks the stream, tracking each side's pitcher of record and the running score. It finds the decisive play (the earliest play after which the eventual winner never again falls to a tie or behind), gives the win and the loss to the pitchers of record at that play, moves the win to the reliever with the most outs when the starter recorded fewer than 15, and then decides the save from the finisher's leads of three runs or fewer. A play that names its own side and pitcher (`PlayResult.fielding_team`, `PlayResult.pitcher_id`) is read first, so the last out of each half goes to the pitcher who recorded it; a play without them reads the next state. On a stream that names its sides, a side that has fielded no play by the decisive play has its starter on record (the away side that leads for good from the top of the 1st). | `api/routes/games.py` (`_build_replay_artifacts`; `/decisions` and `/card` read the stored card) | `_play_side_and_pitcher`<br>`_side_pitcher_outs`<br>`_defending_team` |

**Depends on:** `simulation.game_state` (`GameState`, `Half`, `PlayResult`, `Team`)

**Used by:** `api/routes/games.py`

> **Notes for anyone changing this file:** The module reads the committed PlayResult stream alone; it never reads `home_pitcher_id` / `away_pitcher_id`. The next state is a trap on the play that ends a half-inning: it already names the next half and the other side's pitcher. Read alone, it gives the last out of each half to the wrong pitcher: measured on the old reading, the winning starter's out count was wrong in 55% of decided games and the win changed hands in 4% (SIM-557). Read the play's own `pitcher_id` / `fielding_team` first. The loss goes to the pitcher on the mound at the decisive play, not to the pitcher charged with the go-ahead run.

---

### `simulation/play_recorder.py`

Captures the ordered PlayResult stream of one game without changing the loop. A `RecordingMachine` wrapper (or the `RecordingStateMachine` subclass) intercepts `step_pitch` and keeps each result and the state before it.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `record_game` / `record_game_plays` | `record_game` resolves a dotted machine factory (default the no-DB `rng_driven_machine_factory`), wraps the built machine in `RecordingMachine`, drives it through `simulate_game` at a seed and returns a `RecordedGame(result, plays, contexts)`; `contexts` holds a `PitchContext` per step. `record_game_plays` returns the first two. `record_spec(spec, seed)` is the worker pool's unit of work for one recorded game. | `simulation.batch_runner.BatchRunner.record_game` (through `record_spec`)<br>`api/routes/games.py` (`_record_and_build`, in process)<br>`scripts/validate_props.py`, `scripts/accuracy_comparison.py`, `scripts/behaviour_fingerprint.py` | `simulation.batch_runner._resolve_dotted`<br>`simulation.sim_loop.simulate_game` |
| `RecordingMachine.step_pitch` | Takes the state before the pitch (`PitchContext`: inning, half, outs, batter), calls the wrapped machine's `step_pitch`, deep-copies the result's `next_state` and keeps the result and the context. | `simulation.sim_loop.simulate_game` (through the wrapped machine) | `_snapshot_next_state` |

**Depends on:** `simulation.batch_runner` (`GameSpec`, `_resolve_dotted`), `simulation.game_state` (`PlayResult`), `simulation.loop.boxscore` (`GameSimResult`), `simulation.sim_loop` (`StateMachine`, `simulate_game`), `simulation.snapshots` (`PitchContext`)

**Used by:** `simulation.batch_runner`, `api/routes/games.py`, the scripts above. `simulation.snapshots`, `simulation.linescore` and `simulation.pitcher_decisions` read its output.

> **Notes for anyone changing this file:** `step_pitch` hands back the LIVE GameState as `PlayResult.next_state`, so `_snapshot_next_state` deep-copies it per pitch; without the copy every recorded play points at the final state. The recorder keeps every result, a no-pitch result included: the linescore and the decisions need the pickoff's out, and the play-by-play skips it on its own. The cost is one deep copy per pitch (a few hundred a game): fine for a replay, wrong for a hot Monte-Carlo loop.

---

### `simulation/batch_runner.py`

The parallel Monte-Carlo fan-out and fan-in. It derives one seed per game, maps `simulate_game` across a `ProcessPoolExecutor` (or runs in this process), aggregates to a `GameSimSummary` and caches it in Redis or in memory. It owns the shared-memory attach (the bundle's big arrays published once, attached by every worker with no copy) and the persistent warm pool.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `BatchRunner.run` | Computes the cache key, checks the cache, derives N seeds (`derive_seed`), runs the games (`_execute`), aggregates (`GameSimSummary.from_results`), caches the summary for `SIM_RESULT_TTL_S` (60 s) and returns a `BatchResult`. | `api/routes/games.py` (`_run_batch`, which the betting routes also call) | `BatchRunner._execute`<br>`simulation.results.GameSimSummary.from_results` |
| `BatchRunner.run_job` | Plays a run job's games in chunks of `max_workers * 2`, calls `on_progress` after each chunk and stops on `should_cancel`; returns a `JobOutcome` with the summary and every player's prop distributions from the one batch. No cache. | `api/sim_jobs.py` | `BatchRunner._execute`<br>`GameSimSummary.from_results`<br>`PropDistributionSet.from_boxscores` |
| `BatchRunner.record_game` | Records ONE game with its plays (`play_recorder.record_spec`) on the live warm pool, whatever its size, and submits under the pool lock (a batch that resizes the pool cannot shut it down between the read and the submit); in this process when there is no pool. The API records its replay game here, so the API process never loads the sim bundle (that cost about 2 GB and 15 s on the first call). | `api/routes/games.py` (`_persist_replay_artifacts`) | `simulation.play_recorder.record_spec` |
| `BatchRunner.run_prop_set` | Runs the batch like `run` (the same seeds, the same pool) and returns `PropDistributionSet.from_boxscores` over the per-game box scores. Caches a SEEDED set for `SIM_PROP_SET_TTL_S` (900 s) under its own key; an unseeded call always runs the games. | `api/routes/games.py` (`_build_prop_set`: `/boxscore` and `/props`) | `BatchRunner._execute`<br>`simulation.prop_distributions.PropDistributionSet.from_boxscores` |
| `_run_one` | The per-worker unit of work (module-level, so it pickles by reference): resolves `spec.machine_factory`, builds a fresh StateMachine in the worker, drops the `_`-prefixed factory-only keys and calls `simulate_game`. | `BatchRunner._execute` / `_map_pool` (submitted to the pool) | `_resolve_dotted`<br>`simulation.sim_loop.simulate_game` |
| `BatchRunner.prewarm` | Submits one warm task per worker, bounded by a shared semaphore so only a few run the heavy warm at once, so no worker is cold on its first game (a cold 10-game request stalled about 500 s). | `api/main.py` (`_background_prewarm`) | `_prewarm_worker`<br>`simulation.production_factory.warm_worker_cache` |
| `rng_driven_machine_factory` | The picklable no-DB machine factory: a StateMachine over `simulation.synthetic_bundle.league_artifacts()`; the factory-only `_hit_rate` key reshapes the in-play mix. Every always-on test and any script that wants a full game with no DuckDB uses it. | `simulation.play_recorder.DEFAULT_FACTORY_REF`<br>`scripts/behaviour_fingerprint.py`<br>many unit tests | `simulation.synthetic_bundle.league_artifacts`<br>`simulation.full_pool_sampler.FullPoolSampler` |

**Depends on:** `simulation.cache_codec`, `simulation.flags`, `simulation.prop_distributions`, `simulation.results`, `simulation.sim_loop` (`simulate_game`), `simulation.synthetic_bundle` (in `rng_driven_machine_factory`)

**Used by:** `api/main.py`, `api/routes/games.py`, `api/routes/betting.py`, `api/sim_jobs.py` (the production path names `simulation.production_factory:production_machine_factory` as the `GameSpec.machine_factory`), `simulation.play_recorder`, `simulation.production_factory`

**Environment flags read here:** `SIM_MP_START_METHOD` (the pool's start method, default forkserver) and `SIM_PREWARM_MAX_CONCURRENT` (the cap on concurrent heavy warms), both at import. `SIM_RUNNER_WORKERS` is read in `api/main.py` and passed as `BatchRunner(max_workers=...)`; unset, the runner uses `min(os.cpu_count() - 1, 10)`.

> **Notes for anyone changing this file:** A worker NEVER receives a live sampler or StateMachine across the process boundary, only a picklable `GameSpec` (a dotted factory string and a kwargs dict); that is why `production_factory` is a separate module a dotted path can import. The warm pool is reused across `run()` calls when `reuse_pool=True` (the default); a change in the worker count shuts the pool down and builds a new one, and the shared-memory segments outlive it. Keep forkserver: a forked worker inherits the parent's ~6 GB of engines, while a forkserver worker holds about 575 MB private (2026-09-15; the worker start method, SIM-430).

---

### `simulation/production_factory.py`

The production machine factory: it builds the REAL `FullPoolSampler` over the on-disk engine-artifact bundle (cached once per worker process), sets every `SIM_*` switch, power and bandwidth on it, wires the manager gate when `SIM_MANAGER` is on and loads the intentional-walk (IBB) rates. There is no fallback: a missing or corrupt bundle raises.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `production_machine_factory` | The `GameSpec.machine_factory` entry point: builds or reuses the cached sampler, sets the point-in-time cutoff (`_asof_ymd`, or None to clear the last game's), attaches the manager gate (`_DEFAULT_MANAGER_PROFILE`, an empty marker; the real profiles ride on the game state) with the IBB rate table and the placeholder pen per team (the pen a game uses when the request passes none), sets `machine.manager_draw` from `SIM_MANAGER_DRAW` and returns the StateMachine. | `simulation.batch_runner._run_one` and `simulation.play_recorder.record_game` (through `_resolve_dotted`; the API names it on the production GameSpec) | `_build_full_pool_sampler`<br>`_manager_enabled`<br>`_load_ibb_rates`<br>`_BULLPEN_BUILDER` (default `_default_bullpen_for_spec`) |
| `_build_full_pool_sampler` | Loads (or reuses the per-process cached) `EngineArtifacts` bundle, with the parent's shared-memory views spliced in when published, builds a `FullPoolSampler` and sets every flag: the home, park, fatigue, pitch-similarity, situation and cell-index settings inline, then `apply_actor_matrix_env`, `apply_result_split_env`, `apply_fielding_env`, `apply_manager_env` and `apply_running_game_env`. A bundle with the reference data supplies the IBB rates and the venue run factors. Raises `RuntimeError` when the bundle cannot load. | `production_machine_factory`<br>`warm_worker_cache` | `pipeline.batch.engine_artifacts.EngineArtifacts.load`<br>`apply_sit_sigma_env`<br>the five `apply_*_env` functions |
| `warm_worker_cache` | Builds this process's cached sampler and runs its lazy one-time precomputes (`_warm_sampler`: the pitch pools, the batted-ball pools and the steal pools' cell indexes), so a worker's FIRST real game runs at the usual cost. Never raises; False on a missing or corrupt bundle. | `simulation.batch_runner._prewarm_worker` (from `BatchRunner.prewarm`) | `_build_full_pool_sampler`<br>`_warm_sampler` |

**Depends on:** `simulation.batch_runner` (`GameSpec`), `simulation.flags`, `simulation.sim_loop` (`StateMachine`), `pipeline.batch.engine_artifacts` (lazily), `simulation.sim_kwargs` (`open_sim_duckdb`, lazily, for a bundle without the reference data)

**Used by:** `simulation.batch_runner` (as the `GameSpec.machine_factory` dotted target), `api/routes/games.py` (`PRODUCTION_FACTORY_REF`)

**Environment flags read here:** `SIM_MANAGER` (the manager gate), `SIM_MANAGER_DRAW` (the pitching change as a draw), `SIM_SIT_SIGMA`, `SIM_HOME_OFF_WEIGHT`, `SIM_PARK_KERNEL_SIGMA`, `SIM_FATIGUE_PC_SIGMA`, `SIM_FATIGUE_TTO_SIGMA`, `SIM_BB_PITCH_SIGMA`, `SIM_PITCH_HOME_OFF_WEIGHT`, `SIM_PITCH_CELL_INDEX`, `SIM_PITCH_MIN_CELL`, `SIM_ACTOR_POWER_<NAME>` (every actor power, the per-position fielder powers and the manager's usage power included), `SIM_CATCHER_RECEIVING`, `SIM_PITCH_RESULT_SPLIT`, `SIM_RESULT_PITCH_SIGMA`, `SIM_RESULT_DENSITY_POWER`, `SIM_PITCH_PITCHER_POWER`, `SIM_PITCH_BATTER_POWER`, `SIM_RESULT_PITCHER_POWER`, `SIM_RESULT_BATTER_POWER`, `SIM_BB_BORN_SIGMA`, `SIM_BB_BORN_DENSITY_POWER`, `SIM_BB_CLASS_FILTER`, `SIM_BB_BATTER_POWER`, `SIM_PARK_WALL_ZONE_ONLY`, `SIM_WALL_ZONE_DISTANCE`, `SIM_FENCE_STAGE`, `SIM_FENCE_MARGIN`, `SIM_CARRY_OFFSET`, `SIM_BB_BORN_PER_FEATURE`, `SIM_BB_MARGIN_BAND`, `SIM_BB_MARGIN_MIN_ROWS`, `SIM_CHANGE_SIT_SIGMA`, `SIM_CHANGE_PITCHER_POWER`, `SIM_CHANGE_MIN_CELL`, the six `SIM_RELIEF_*` reliever-draw weights, `SIM_STEAL_PITCH_CLASS` (the running game on the pitch; default ON, unlike the other sampler flags), `BASEBALL_ENGINE_ARTIFACT_DIR` (the bundle version the app pinned) and `BASEBALL_PLAY_POOL_DIR` (the artifact root)

> **Notes for anyone changing this file:** This is the ONE place every sampler flag is parsed and applied: a new sampler setting must be wired here, or it silently stays at its default. The per-process caches (`_CACHED_FULL_POOL_SAMPLER`, `_CACHED_IBB_RATES`) last for the life of the worker; a test calls `reset_caches()` in setup or teardown, or one test's cached sampler leaks into the next. The cached sampler serves every game of the worker, so the factory calls `set_asof` on every build, with None when the request has no cutoff.

---

### `simulation/lineup_resolver.py`

Builds the GameState a game starts from: the lineups, the starting pitchers, the per-position defense maps, the bat and throw hands, each side's manager and the real pen with its recent usage. It reads Postgres `raw.game_lineups`, `raw.games`, `raw.players`, `raw.game_bullpen` and `raw.game_player_stats`. The substitution rule is "the highest applicable sequence wins" per batting slot, with the pitcher slot resolved apart. The assembly logic is free of the database, so a unit test drives it with canned rows.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_lineup_from_rows` | Pure assembly: reduces both teams' lineup rows to a `ResolvedLineup` with `_pick_team_slots`, honouring an optional `as_of_at_bat` rewind point. `box_starters` (team id -> the pitcher the official box credits with the start) replaces the lineup's sequence-1 pitcher row on each side it names; a recorded pitching change (a pitcher row with sequence above 1) still wins. | `resolve_lineup` | `_pick_team_slots` |
| `fetch_box_starters` | Reads each side's starter from `raw.game_player_stats` (the rows with `p_started`) as `{team_id: player_id}`. Empty for a game with no box yet. A side with two started rows is left out with a warning; a failed read logs a warning and returns an empty map, so the lineup's pitcher row stands. | `resolve_lineup` | — |
| `build_game_state` | Pure builder: a `ResolvedLineup` becomes a fresh top-of-the-1st GameState (the lineups, the slot pointers, the fielding pitcher, the leadoff batter, the hand maps, both starters and both defense maps). Raises `LineupIncompleteError` when EITHER side has no pitcher (`_require_pitcher`). | `resolve_game_state` | `build_team_defense_map`<br>`_require_pitcher` |
| `build_team_defense_map` | Maps one team's current batting-slot occupants and its pitcher onto `{position name: player_id}`: the source of the `home_defense` / `away_defense` maps that feed the fielder score-matrix factors and the catcher factors, and of the field graphic's `defense_positions`. | `build_game_state`<br>`build_defense_map`<br>`build_defense_map_for_state` | — |
| `resolve_lineup / resolve_game_state` | The async orchestrators: `fetch_game_sides`, `fetch_lineup_rows`, `fetch_box_starters` and `fetch_player_hands` read Postgres; the pure functions above do the logic. `resolve_lineup` also reads the two managers and the date off `raw.games` and calls `_attach_bullpens`. `fallback_rows` (the game page's what-if) stand in for the lineup table when it holds no row for the game. | `api/routes/games.py`<br>`scripts/sim_stats.py`, `scripts/behaviour_fingerprint.py` | `fetch_game_sides`<br>`fetch_lineup_rows`<br>`fetch_box_starters`<br>`fetch_player_hands`<br>`resolve_lineup_from_rows`<br>`build_game_state`<br>`_attach_bullpens` |
| `_attach_bullpens / bullpen_source_enabled` | With `SIM_BULLPEN_SOURCE=box` (production; the code default 'synthetic' is the unit lane's), fills the `ResolvedLineup`'s `bullpen` (Team int -> arms), `pitcher_rest_days` and `pitcher_recent_usage` (days of rest, pitched in the last two days, pitches over three) from the MLB box's per-game listing through `pipeline.bullpen_usage.fetch_pen_for_game` (the listing minus the rotation's last-five-games starters minus today's starter), merges the pen arms' throwing hands and sets `bullpen_source='box'`. A missing listing on either side, a failed read or the switch off leaves 'synthetic' and empty maps; the factory's placeholder pen then stands in. | `resolve_lineup` | `pipeline.bullpen_usage.fetch_pen_for_game`<br>`fetch_player_hands` |

**Depends on:** `simulation.flags`, `simulation.game_state` (`GameState`, `Half`, `Team`)

**Used by:** `api/routes/games.py` (the game state for `/simulate` and the related routes), `scripts/sim_stats.py`, `scripts/behaviour_fingerprint.py`

**Environment flags read here:** `SIM_BULLPEN_SOURCE`

> **Notes for anyone changing this file:** `LineupNotIngestedError` (a `LineupResolutionError` subclass) means `raw.game_lineups` has no row for a known game; the API answers 503 with Retry-After, not 404 (the lineup may not be published yet). `LineupIncompleteError` (also a subclass) is a known game whose rows do not make a playable game: a side with no pitcher, an empty batting order, or rows that resolve to no slot; the API answers it the same way. Catch the two subclasses before the base class, or a data gap on a real game reads as "not found". `build_team_defense_map` accepts both the position NAME ('SS', 'C', ...) and the numeric scorebook code ('1'..'9') in `position_code`. The starting pitcher comes from the official box first: the lineup's pitcher row alone was wrong or missing in 397 of 45,484 team-games of 2017-2026 (the starter resolution, SIM-558). The lineup table stores the position each starter STARTED at (the start-position repair, SIM-559).

---

### `simulation/sim_kwargs.py`

The one builder that turns a resolved GameState into the `simulate_game` kwargs, plus the venue park-factor resolver and its unresolved sentinel, which keeps a park-blind run from looking like a neutral one.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `sim_kwargs_from_state` | The ONE definition of the kwargs dict: the lineups, the season, `pitcher_id`, the hands, both starting pitchers, both catchers, both defense maps, `park_run_factor`, `venue_id`, `max_innings` and the eight manager keys (both manager ids, `bullpen`, `pitcher_rest_days`, `pitcher_recent_usage`, `bullpen_source`, `manager_profiles` {0: away, 1: home}, `manager_league_profile`): the 24 keys of `SIM_KWARG_KEYS`. Raises `UnresolvedParkFactorError` unless the park factor is resolved or the caller passes `allow_unavailable_park_factor=True`. | `build_sim_kwargs`<br>`api/routes/games.py`<br>`scripts/sim_stats.py`, `scripts/accuracy_comparison.py`, `scripts/validate_props.py` and the probes | `park_factor_is_resolved`<br>`venue_id_of_state` |
| `resolve_park_run_factor` | A two-source lookup: the game's `venue_id` from Postgres `raw.games`, then its regressed run factor from `derived.park_factors` (`factor_type='R'`), through a DuckDB connection or, with `con=None`, the primed in-memory snapshot. A plain float only for a real row inside 0.5-2.0; every other branch returns `UNRESOLVED_PARK_FACTOR` with a reason and logs once per reason. | `resolve_park_factor_onto_state` | `resolve_venue_id`<br>`_park_factor_row`<br>`_refresh_park_factor_snapshot_if_changed`<br>`park_factor_unavailable` |
| `resolve_manager_profiles_onto_state` | Reads each side's manager tendency profile (`derived.manager_season_metrics`, the season, then the season before) and the season's manager league means (`derived.league_averages`, entity 'manager') onto the state. With `con=None` it returns at once. Read-only; never raises; every consumer is neutral on an empty dict. | `build_sim_kwargs`<br>`tests/acceptance/conftest.py`<br>`scripts/sim_stats.py`, `scripts/accuracy_comparison.py` | — |
| `build_sim_kwargs` | The API's entry point: resolves the park factor, the manager profiles and the backtest cutoff (`resolve_asof_ymd`) onto the state, then calls `sim_kwargs_from_state` with `allow_unavailable_park_factor` set by `on_unavailable_park_factor` ('proceed' logs and continues; 'raise' refuses). | `api/routes/games.py` (`_resolved_sim_kwargs`: every game route and the betting card)<br>`scripts/behaviour_fingerprint.py` | `resolve_park_factor_onto_state`<br>`resolve_manager_profiles_onto_state`<br>`resolve_asof_ymd`<br>`sim_kwargs_from_state` |

**Depends on:** `simulation.game_state` (indirectly, through objects shaped like a GameState)

**Used by:** `api/main.py` (primes the park-factor snapshot), `api/bundle_watch.py` (`open_sim_duckdb`), `api/routes/games.py`, `simulation.production_factory` (`open_sim_duckdb`), `scripts/accuracy_comparison.py`, `scripts/validate_props.py`, `scripts/sim_stats.py`, `scripts/behaviour_fingerprint.py`

**Environment flags read here:** `BASEBALL_DUCKDB_PATH` (the DuckDB file of `open_sim_duckdb` and `prime_park_factor_source`)

> **Notes for anyone changing this file:** One builder exists because a hand-built copy of the kwargs in `scripts/sim_stats.py` dropped `home_defense`, `away_defense` and `park_run_factor`, and the harness read two identical no-ops as "no effect" (the one builder, SIM-449). A unit test pins the builder's output to `SIM_KWARG_KEYS`, so a dropped key fails CI. `UNRESOLVED_PARK_FACTOR` is a float subclass equal to 1.0 that `isinstance` tells apart: a reader of the number keeps working, and a reader that cares can tell "nobody looked" from "looked and it is neutral". `prime_park_factor_source` reads the run factors into memory and closes the file, so a rebuild can take the writer lock; a lookup re-reads them when the file's mtime changes.

---

### `simulation/cache_codec.py`

The JSON codec of the Redis sim cache (`batch_runner`'s `RedisCache`, key prefix `sim443:`). The cache stores JSON only, never a pickle: a pickle load runs code, so a pickle in Redis would let anyone who can write to Redis run code in the API (the security hardening, SIM-443f). Every value is an envelope `{"t": <tag>, "v": <payload>}`.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `encode` | Writes a value as the JSON text of a typed envelope: tag `summary` for a `GameSimSummary` (its `to_json`), `prop_set` for a `PropDistributionSet` (its `to_json`), `json` for any plain JSON value. It has no `default=` hook, so a value JSON cannot hold raises `TypeError` and the cache skips the write. | `simulation.batch_runner.RedisCache.set` | `GameSimSummary.to_json`<br>`PropDistributionSet.to_json` |
| `decode` | Reads an envelope back. It never raises: any text it cannot read (a pickle, a truncated value, a wrong tag) is a miss, and the caller recomputes. | `simulation.batch_runner.RedisCache.get` | `GameSimSummary.from_json`<br>`PropDistributionSet.from_json` |

**Depends on:** `simulation.results`, `simulation.prop_distributions`

**Used by:** `simulation.batch_runner`

---

### `simulation/synthetic_bundle.py`

Builds a small in-memory `EngineArtifacts` bundle (the pitch pools, a batted-ball pool with transitions, the advancement pools, the steal opportunity pools and the change opportunity pool) in the exact shape the production loader produces. Every no-DB test and the no-DB batch factory run the production sampler and loop over synthetic rows.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `league_artifacts` | The default no-DB bundle: a league-average pitch mix over every count bucket, a league in-play mix over every base-out cell (`inplay_rows`), the advancement pools ON and the change pool present by default, so a no-DB game has a realistic run environment. | `simulation.batch_runner.rng_driven_machine_factory`<br>`simulation.validation.replay_chi_squared._league_artifacts`<br>many unit tests | `synthetic_artifacts`<br>`inplay_rows` |
| `canonical_transition` | The FORCED (station-to-station) movement of one event in one base-out cell: the synthetic row's transition. The advancement draws decide the five discretionary movements, as for a production row. | `inplay_rows`<br>`fixed_play_artifacts` | — |
| `steal_pools` | A synthetic steal opportunity pool per target base, so a unit test runs the real pickoff and steal draws. The default (`by_class=True`): three rows (went safe, went caught, stayed) per (outs, balls, strikes, class) group for each of the six pitch classes, the attempt rate on `STEALABLE_CLASSES` (ball, called strike, swinging strike) and zero on the foul, the ball in play and the hit by pitch; `class_rates` overrides it per class. Each class's three rows weigh 1/6, so a count group's pitch rows weigh 1. `pickoff_rows` adds that many pickoff rows (no class, never attempted, `pickoff_weight` each; `pickoff_kind` = out, advancing or error) to the count groups `pickoff_counts` names (default: every count). `by_class=False` builds the single pre-pitch draw's shape: three rows per (outs, balls, strikes) cell and `pitch_class` None. | `synthetic_artifacts` / `fixed_play_artifacts` (`steal=`, `steal_kw=`) | `_rate_pool_rows` |
| `change_pool` | The no-DB change opportunity pool: three weighted rows (changed / changed / stayed) in every hard cell the draw reads, at `DEFAULT_CHANGE_RATES` per (role, boundary, pitch-count bucket) or a caller's rates. Every row carries one manager id and a rested right-handed incoming arm, so the manager weight and the reliever weights have something neutral to read. `league_artifacts(manager=True)` (the default) carries it, so a no-DB game with the gate and the draw on changes pitchers. | `synthetic_artifacts`<br>unit tests | — |
| `synthetic_sampler` | A `FullPoolSampler` over a given (or the default league) synthetic bundle with a given seed. | `simulation.validation.replay_chi_squared._default_state_machine`<br>many unit tests | `simulation.full_pool_sampler.FullPoolSampler` |

**Depends on:** `pipeline.batch.engine_artifacts` (`AdvancementPool`, `BattedBallPool`, `ChangePool`, `EngineArtifacts`, `HandPool`, `StealPool`, `STEAL_PITCH_CLASSES`, `STEAL_PITCH_CLASS_CODE`: the production shapes)

**Used by:** `simulation.batch_runner` (`rng_driven_machine_factory`, `LEAGUE_INPLAY_MODEL`), `simulation.validation.replay_chi_squared`, the no-DB path of the whole unit suite

> **Notes for anyone changing this file:** The tests draw through the same sampler and loop as production, so a failure here is a production code-path bug, not a test-double bug. A separate test-only in-play path once hid four production defects for eight weeks (SIM-486).

---

### `simulation/constants.py`

The canonical outcome-key vocabulary (CANONICAL_OUTCOME_KEYS) every consumer keys on (linescore, sim store, acceptance bands), the Statcast-raw-events-to-canonical alias map, the position table `FIELD_POSITION_NAMES` (the scorekeeping number -> the position name the fielder embedding and the defense maps key by; the loop and the sampler share it) and the defensive run values the profile computor reads. There is no run value per outcome: a play's value comes only from run_resolution's RE24 delta.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `resolve_event_to_canonical` | Maps any Statcast raw events string (or an already-canonical key) to its canonical outcome key; returns None for unknown/non-terminal tokens (e.g. sim_loop's 'in_progress' marker). | `simulation.run_resolution.resolve_runs`<br>`simulation.loop.credit.CreditMixin._accumulate_pa (fallback when canonical_event is None)` | — |
| `FIELD_POSITION_NAMES` | The scorekeeping position number (1-9) -> the position name ('P', 'C', '1B', ... 'RF'). | `simulation.loop.in_play`<br>`simulation.sampler.fielding`<br>`simulation.sampler.advancement` | — |

**Used by:** `simulation.run_resolution`, `simulation.sim_loop`, `linescore/other consumers via the canonical vocabulary`, `tests/acceptance/bands.py`

> **Notes for anyone changing this file:** field_error is its OWN canonical outcome: a reach on an error is NOT a hit. An import-time assertion enforces that every STATCAST_EVENT_ALIASES value is itself a CANONICAL_OUTCOME_KEYS member, so a typo'd alias target fails at import rather than silently leaking a non-canonical key downstream.

---

### `simulation/filter_cells.py`

The ONE definition of the pitch draw's hard-filter cell (base occupancy x outs x count x score band x batting side = 2,880 cells). The sampler's cell index, its widening ladder for a thin cell and the offline measurement script (`scripts/measure_filter_cells.py`, the filter-cell census of SIM-451) all use it.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `cell_key / decode_cell` | Encodes a situation into its 0..2879 cell id, most significant dimension first (so sorting by cell id groups the cells by base state); `decode_cell` is the exact inverse. | `scripts/measure_filter_cells.py`<br>tests (the sampler's cell index encodes inline with the same constants) | `score_band`<br>`count_bucket` |
| `score_band / score_band_array` | Maps a score difference (clamped to +/-5) to one of 5 bands: the number of `SCORE_BAND_EDGES` it exceeds. `score_band_array` is the vectorized form over a pool column. | `simulation.sampler.pitch.PitchMixin._cell_meta / _new_plate_appearance_cell`<br>`scripts/probes/fatigue_kernel_scan.py` | — |

**Used by:** `simulation.sampler.pitch` (`DEFAULT_MIN_CELL`, `N_BAND`, `N_BASE`, `N_COUNT`, `N_OUTS`, `score_band`, `score_band_array`), `scripts/measure_filter_cells.py`, `scripts/probes/fatigue_kernel_scan.py`

> **Notes for anyone changing this file:** A cell id computed under other edges is NOT comparable with an earlier census report. `SCORE_BAND_EDGES` is checked at import to be strictly increasing, so nobody can reorder it and split a report from the live code. `DEFAULT_MIN_CELL` (20, overridden by `SIM_PITCH_MIN_CELL`) is the measured minimum: on the W1 pool window (2023-2026), 0.25% of left-handed and 0.17% of right-handed draws land in a cell under 20 rows, and the score-band step recovers 99% of them (the filter-cell census, SIM-451, 2026-10-03).

---

### `simulation/prop_validation.py`

Validates the simulator's probability outputs (the win probability, the prop over/unders) against real outcomes, and FITS the win-probability reliability curve (`fit_reliability_curve`) that the calibration report carries. A reliability curve maps each predicted probability to the rate at which the event really happened. The module also holds the two prop ground-truth readers: the official box score (`raw.game_player_stats`; every priced market; the owner's decision to grade props on the box, SIM-545) and the `raw.pitches` event labels (H / HR / TB and K / BB only; the fallback for a game with no box-score rows).

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `fit_reliability_curve` | Bins the predicted home-win probabilities against the observed home-win rates and emits the `[[predicted_p, observed_p], ...]` anchor list that `win_probability.CalibrationMap.from_report` turns into a monotone map. | `build_validation_report` | `binary_reliability_curve` |
| `validate_prop_over_under` | Scores a prop PMF's `p_over(line)` forecast against the real over/under outcomes with the binary expected calibration error (ECE), the Brier score, the log-loss and a reliability curve. | `build_validation_report` | `binary_ece`<br>`binary_brier`<br>`binary_log_loss`<br>`binary_reliability_curve` |
| `real_props_from_boxscore_rows(rows)` | Turns one game's `raw.game_player_stats` rows (asyncpg Records or plain dicts) into `(batter_actuals, pitcher_actuals)`, `{player_id: {prop: int}}`. A batter total exists ONLY for a row with `played_bat` true (every `BOXSCORE_BATTER_PROPS` value: 1B = h - b2 - b3 - hr, HRR = r + h + rbi, TB from the box's own `tb`), a pitcher total ONLY for `played_pitch` true (K / BB / ER from `p_k` / `p_bb` / `p_er`, OUTS = `p_outs`, H_ALLOWED = `p_h`). A player who did not play is ABSENT from both: the book voids his bet, and every scorer skips him. | `scripts/validate_props.py`<br>`scripts/accuracy_comparison.py`<br>`scripts/boxscore_audit.py` | `_row_int` |
| `real_props_from_pa_events` | Sums per-plate-appearance (batter_id, pitcher_id, event_label) tuples from `raw.pitches` into real H / HR / TB (batter) and K / BB (pitcher) totals: the FALLBACK ground truth for a game with no box-score rows. | `scripts/validate_props.py`<br>`scripts/accuracy_comparison.py` | — |
| `BOXSCORE_BATTER_PROPS / BOXSCORE_PITCHER_PROPS` vs `DERIVABLE_BATTER_PROPS / DERIVABLE_PITCHER_PROPS` | The props each ground truth can grade: the box score grades all ten batter and five pitcher props; the event label grades H / HR / TB and K / BB only. `DEFAULT_PROP_LINES` carries a default over/under line for all fifteen. | `pair_props_for_validation`<br>`scripts/accuracy_comparison.py` | — |
| `pair_props_for_validation(pset, batter_actuals, pitcher_actuals, prop_pairs_by_line, *, prop_lines=None, batter_props=DERIVABLE_BATTER_PROPS, pitcher_props=DERIVABLE_PITCHER_PROPS)` | Pairs a game's sim PMFs with the real totals for every (player, prop) both sides hold. Pass the `BOXSCORE_*` tuples with box-score actuals to pair every market. A prop absent from a player's totals is a skip, never a KeyError, so a run with both sources pairs cleanly. | `scripts/validate_props.py` (`_pair_game_props`) | `DEFAULT_PROP_LINES` |
| `write_reliability_curve_to_calibration_report` | Writes the fitted win-probability curve into the calibration report JSON file the API boots its calibration from. | `scripts/validate_props.py` | `reliability_curve_for_calibration_report`<br>`similarity.similarity_calibration.CalibrationReport` |

**Depends on:** `similarity.backtesting.backtester` (`DEFAULT_N_BINS`, `OUTCOME_PROB_EPS`, reused so the binary and multi-class layers agree), `simulation.prop_distributions` (`PropDistribution`, for the type checker only)

**Used by:** `scripts/validate_props.py` (the `make validate-props` entry point), `scripts/accuracy_comparison.py`, `scripts/boxscore_audit.py`

> **Notes for anyone changing this file:** RBI, ER and OUTS cannot come from the `raw.pitches` event labels alone: they need the runs driven in per plate appearance, the earned/unearned split or a per-event out count, which a label does not carry. `DERIVABLE_BATTER_PROPS` / `DERIVABLE_PITCHER_PROPS` leave them out, so the event-label path never invents a wrong "actual"; the official box score supplies them and replaces the event label whenever a game has rows. The walk counts an intentional walk in all three places: the sim's BB line (`_BB_CANONICAL` in `simulation/loop/credit.py` holds `walk` and `intentional_walk`; `prop_distributions.py` reads `ln.bb`), the box's `p_bb`, and the event-label fallback (`_WALK_EVENTS = {"walk", "intent_walk"}`; a test in `tests/unit/test_validate_props.py` pins the set to `_BB_CANONICAL` through the Statcast alias table). The fallback still under-counts a pitcher's walks by his intentional ones, because `raw.pitches` holds no `intent_walk` row (an intentional walk is signalled, not thrown; its rows live in `raw.play_events`). So the box is the matching ground truth, and the fallback is a five-prop stand-in.

---

### `simulation/validation/replay_chi_squared.py`

The end-to-end check of the run distribution. It replays a set of games (real ones, or the no-DB league model) through `simulate_game` and compares the simulated runs per team-game with an actual or reference distribution by a chi-squared goodness-of-fit (GOF) test. It pools the sparse high-run tail until every expected count is large enough; the gate is p > 0.05.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `replay_and_test` | Replays the `HistoricalGame` rows, takes their actual run totals and runs `chi_squared_gof` against them. | `tests/unit/test_replay_chi_squared.py` | `replay_historical_games`<br>`chi_squared_gof` |
| `chi_squared_gof` | Bins the simulated and reference run totals (0..max_bin, the last bin open), pools adjacent bins until every expected count reaches `min_expected` (the Cochran rule) and runs `scipy.stats.chisquare`; returns a `ChiSquaredResult` with `passed = p_value > alpha`. | `replay_and_test`<br>tests | `bin_run_totals`<br>`pool_low_expected_bins`<br>`scipy.stats.chisquare` |
| `simulate_run_distribution / replay_historical_games` | Run `simulate_game` once per game (a fixed seed each) through an injectable `state_machine_factory` (default: the production machine over the synthetic league bundle) and return the simulated runs per team-game. `replay_historical_games` keeps the home side's runs, one team-game per row. | `replay_and_test` (`replay_historical_games`)<br>tests | `simulation.sim_loop.simulate_game`<br>`_default_state_machine` |

**Depends on:** `simulation.game_state` (`GameState`), `simulation.loop.boxscore` (`GameSimResult`), `simulation.sim_loop` (`StateMachine`, `simulate_game`), `simulation.synthetic_bundle` (`league_artifacts`, `synthetic_sampler`)

**Used by:** `tests/unit/test_replay_chi_squared.py` (no production caller)

> **Notes for anyone changing this file:** For real data, build `HistoricalGame` rows from the box scores and pass a production `state_machine_factory`; the same chi-squared code then tests the loop against real history. With no database the tests run a self-consistency check against the same league model. The `reference_totals` path treats the reference sample as exact: it does not count that sample's own noise.

---

### `simulation/flags.py`

The registry of the `SIM_*` environment flags. One row per flag in `FLAGS`: its name, type, code default, the work that owns it and a one-line meaning. Code in `api/` and `simulation/` reads a flag only through the readers here; the batch runner reads its two at import. The production values live in the `app` service of `docker-compose.yml`; the unit tests pin their own in `tests/conftest.py`.

| Function / class | What it does | Called from | Depends on |
|---|---|---|---|
| `FLAGS` / `Flag` | Every registered flag, by name; a frozen row of name, kind (`bool`, `int`, `float`, `str`), default, owner and meaning. | `tests/unit/test_flag_registry.py`<br>`simulation/production_factory.py` (`DEFAULT_SIT_SIGMA`) | — |
| `read_bool(name, env=None)` | An on/off flag: `1 true yes on` read on, `"" 0 false no off` read off (any case, trimmed); unset reads the default; any other value raises `FlagValueError`. | `simulation/production_factory.py`, `simulation/sim_loop.py`, `api/bundle_watch.py` | — |
| `read_float` / `read_int` / `read_str` | A number (unset or unparsable keeps the default and logs a warning; an int written as a decimal reads its whole part) or text (as set, not trimmed). | `simulation/production_factory.py`, `simulation/batch_runner.py`, `simulation/lineup_resolver.py`, `api/main.py`, `api/routes/games.py`, `api/sim_jobs.py`, `api/state.py` | — |
| `actor_power_overrides(env=None)` | The `SIM_ACTOR_POWER_<ACTOR>` family: `{actor: power}` for every power the environment sets; a per-position fielder power keeps its position in capitals. | `simulation/production_factory.apply_actor_matrix_env` | — |
| `is_registered(name)` | Whether a name is a registered flag or an actor power. | `tests/unit/test_flag_registry.py` | — |

**Used by:** every `SIM_*` read in `api/` and `simulation/`. `tests/unit/test_flag_registry.py` fails when code reads, or compose sets, an unregistered `SIM_*` name.

> **Notes for anyone changing this file:** a new flag is a new row. Give it the work that owns it in words and the ticket in parentheses, and say what an unset flag means. The code defaults are not production's: compose sets production.

---
