# Simulation Engine

The `simulation/` package plays baseball games pitch by pitch. Its input is a game state (two
lineups, two starting pitchers, the defenses, the pens, the park) and an engine-artifact bundle (a
folder of real-play pools and similarity matrices that the nightly job writes). Its output is one
result per game (a score, a box score, an inning grid) and, over many games, a summary, every
player's prop distributions and the price of every game market. The API calls it through the
batch runner; the scripts call it directly.

Every decision in a game is a **draw**: the code keeps the rows of a real-play pool that fit the
live situation (the **hard filter**), weighs each row by how much its situation and its players
resemble the live ones, and picks one row at random by that weight. The drawn row IS the play
(the owner's rulings of 2026-08-10 and 2026-08-29). The [loop cheat sheet](sim-loop-cheat-sheet.md)
lists every draw, its filters and its weights on one page.

## How it fits together

The request path, from an API route to a priced game:

1. `lineup_resolver.resolve_game_state` reads the game from Postgres and builds a fresh
   `GameState`: both batting orders, both starters, both defenses, the managers and the pen.
2. `sim_kwargs.build_sim_kwargs` looks up the park factor, the managers' profiles and the
   backtest cutoff, and turns the state into plain keyword arguments (the "sim kwargs").
3. `batch_runner.BatchRunner.run` (or `run_job`, `run_prop_set`) derives one seed per game and
   sends each game to a worker process as a small `GameSpec`.
4. In the worker, `production_factory.production_machine_factory` builds a `StateMachine`
   around a `FullPoolSampler`. The worker loads the bundle once and keeps the sampler warm.
5. `sim_loop.simulate_game` drives the machine. It calls `StateMachine.step_pitch` until the
   game ends, and it applies the game rules: nine innings, the walk-off, the extra-innings
   runner on second.
6. Each `step_pitch` runs the manager's decisions (`loop/manager.py`), the pickoff draw
   (`loop/running_game.py`), the pitch draw (`_full_pool_outcome`, which reads
   `sampler/pitch.py` and `sampler/pitch_result.py`), the steal draw, the count machine
   (`loop/values.advance_count`) and the play's resolution: a walk or a strikeout
   (`loop/terminals.py`), or a ball in play (`loop/in_play.py`, which reads
   `sampler/fielding.py`, `sampler/fence.py` and `sampler/advancement.py`).
7. Every resolved play commits through `StateMachine._commit_run_delta`, which checks that no
   runner appears or vanishes, values the play with `run_resolution.resolve_runs` and records
   the outs. `loop/credit.py` writes the box score at the end of each plate appearance.
8. The worker returns a `GameSimResult` per game. The parent aggregates them into a
   `results.GameSimSummary` and a `prop_distributions.PropDistributionSet`.
9. The consumers price the run: `win_probability` (the moneyline), `game_market_distributions`
   (the segment and team markets) and the betting layer (`betting/clv_engine.py`).

Three other paths share the same loop:

- **The replay path.** `play_recorder.record_game` keeps every `PlayResult` of one game.
  `snapshots`, `linescore` and `pitcher_decisions` turn that stream into the game page's
  play-by-play, linescore and pitcher decisions.
- **The what-if path.** `start_state` starts a game at a plate appearance of the real game,
  and `whatif.apply_changes` applies the user's substitutions to that start.
- **The no-database path.** `synthetic_bundle` builds a small in-memory bundle, and
  `batch_runner.rng_driven_machine_factory` builds the production machine over it. The unit
  tests run the production loop and sampler this way.

## The loop

The loop is one class, `StateMachine`. Its core lives in `sim_loop.py`; its other methods live in
mixins (classes that hold methods only) in `simulation/loop/`, one module per job. The mixins
share the machine's random generator (`self.rng`) and its sampler.

### `simulation/sim_loop.py`

[Source](../../simulation/sim_loop.py) · 1198 lines · Tests: [test_state_machine.py](../../tests/unit/test_state_machine.py), [test_simulate_game_driver.py](../../tests/unit/test_simulate_game_driver.py), [test_run_ledger.py](../../tests/unit/test_run_ledger.py), [test_pitcher_outs_credit.py](../../tests/unit/test_pitcher_outs_credit.py), [test_rng_streams.py](../../tests/unit/test_rng_streams.py)

The simulator's core: the pitch step, the pitch draw, the run ledger (the one place a play
becomes runs and outs), the end of a plate appearance, the half-inning roll and the full-game
driver. It exports `StateMachine`, `simulate_game`, `BoxScore` and `GameSimResult`.

| Function / class | What it does | Called from |
|---|---|---|
| `StateMachine.step_pitch` | Advances the game by one pitch, in this order: the pitching change on a plate appearance's first pitch; the intentional walk; the pickoff draw before the pitch; the pitch draw; the steal draw after the pitch; the count machine; the staged steal; then the walk, strikeout or ball in play; then the end of the plate appearance. A pickoff that makes the third out returns a result with no pitch (`pitch_outcome = NO_PITCH`, `pa_voided = "pickoff_third_out"`). With two outs, a third strike the catcher holds voids a staged steal (`steal_voided = "third_out_first"`). A caller may inject `pitch_outcome` to drive the count machine with no sampler. | `simulate_game`, `play_recorder.RecordingMachine.step_pitch` |
| `StateMachine._full_pool_outcome` | The pitch draw. It refreshes the pitcher weight (`FullPoolSampler.new_half_inning`) only when the pitcher, the batter's hand, the season or the catcher changes, and the plate-appearance weight (`new_plate_appearance`) when the batter or the base-out state changes. It passes the fatigue inputs and the batting side only when their weights are on. Then it calls `draw(balls, strikes)` and, with `SIM_GOT_AWAY` on, reads the drawn row's got-away fact. | `step_pitch` |
| `StateMachine._commit_run_delta` | The run ledger. It takes the state before the play from the caller (`pre_outs`, `pre_bases`), checks runner conservation (`Bases.assert_transition`), values the play with `resolve_runs` (the run-expectancy change plus the runs scored), adds the runs to the score and records the outs. A drawn multi-out play is clamped to the outs left in the inning. | every resolver in `loop/` |
| `StateMachine._record_outs` | Records outs and credits each one to the pitcher on the mound. It is the only writer of a pitcher's `outs_recorded`. | `_commit_run_delta` |
| `StateMachine._end_of_pa` | Credits the box score, counts the batter faced, moves the batting order, then resets the count or rolls the half-inning. | `step_pitch`, `TerminalsMixin._issue_intentional_walk` |
| `StateMachine.advance_half_inning` | Clears the bases, resets the count and the outs, flips the half and points the matchup at the new half's batter and pitcher (`_set_half_matchup`). With a manager wired, it saves and restores each pitcher's own pitch count. | `step_pitch`, `_end_of_pa` |
| `simulate_game` | The full-game driver. It spawns two independent random streams from one seed (the loop's and the sampler's), builds the starting state, seeds the pen, the managers and their profiles, applies a start state when given, and steps pitches until the game ends. It refuses a game with no batting order on a side, or with one side's starter and not the other's. It builds the inning grid and returns a `GameSimResult`. | `batch_runner._run_one`, `play_recorder.record_game`, `validation.replay_chi_squared`, `scripts/sim_stats.py` and the probes |
| `_game_over_after_half` / `_is_walkoff_live` / `_place_ghost_runner` | The game rules: the bottom of the ninth is skipped when the home team leads; a run that puts the home team ahead in the bottom of the ninth or later ends the game at once; every extra half-inning starts with the batting order's previous batter on second. | `simulate_game` |

**Flags:** `SIM_GOT_AWAY` and `SIM_BB_PLATOON`, read once per machine in `StateMachine.__init__` (default off; production on).

**Watch out:** Every resolver copies the bases with `_snapshot_bases` before it moves anyone,
because several resolvers change `state.bases` in place. `test_run_ledger.py` reads each
resolver's source to check that order. `test_pitcher_outs_credit.py` reads `sim_loop.py` and
every `simulation/loop/` module to check that only `_record_outs` adds to `outs_recorded`. The
inning guard (`max_innings`, 12 on the API's runs) is checked only after a pitch that does not
roll the half, so a game tied after the 12th plays the first pitch of the 13th before it stops.
Such a game can end tied; `GameSimSummary.tie_pct` counts it.

### `simulation/loop/values.py`

[Source](../../simulation/loop/values.py) · 290 lines · Tests: [test_state_machine.py](../../tests/unit/test_state_machine.py), [test_rng_streams.py](../../tests/unit/test_rng_streams.py), [test_hit_by_pitch.py](../../tests/unit/test_hit_by_pitch.py)

The loop's constants, its value objects and the count machine. Nothing here touches a
`GameState`.

| Function / class | What it does | Called from |
|---|---|---|
| `advance_count` | The count machine, a pure function: a ball adds a ball (ball four is a walk); a called or swinging strike adds a strike (strike three is a strikeout); a foul adds a strike below two strikes; a hit by pitch and a ball in play end the plate appearance. | `StateMachine.step_pitch` |
| `spawn_rng_streams` | Two independent random generators from one per-game seed (`numpy.random.SeedSequence.spawn`), so a (game, seed) pair replays exactly. | `simulate_game` |
| `times_through_order` | The pitcher's time through the order from his batters faced (1 before any). | `StateMachine._full_pool_outcome` |
| `FieldingSignal` | One drawn batted ball: its event, its counts, the live fielder, the geometry and the row's whole base-out transition. | `InPlayMixin` |
| `StealResolution` | One staged steal or pickoff: the runner, the bases, safe or out, and whether it is a pickoff. | `RunningGameMixin` |
| `RunningGameTally` | Counts of pickoffs, no-pitch third outs, voided steals and dropped-third-strike reaches. It changes no play; `scripts/probes/running_game_census.py` reads it. | `RunningGameMixin`, `TerminalsMixin`, `step_pitch` |

### `simulation/loop/boxscore.py`

[Source](../../simulation/loop/boxscore.py) · 174 lines · Tests: [test_player_stat_lines.py](../../tests/unit/test_player_stat_lines.py), [test_boxscore_extended.py](../../tests/unit/test_boxscore_extended.py), [test_outs_identity.py](../../tests/unit/test_outs_identity.py)

The per-game box score and the game result. `results.py` re-exports all three classes.

| Function / class | What it does | Called from |
|---|---|---|
| `PlayerStatLine` | One player's batting line (`ab`, `h`, `hr`, `rbi`, `b2`, `b3`, `r`, `sb`, `cs`, `so`) and pitching line (`outs_recorded`, `bf`, `k`, `bb`, `er`, `h_allowed`, `r_allowed`). `k` is the pitcher's strikeouts; a batter's strikeouts go to `so`. | `CreditMixin`, `prop_distributions` |
| `BoxScore` | The lines by player id. `batters` and `pitchers` filter them by activity. | `CreditMixin`, `prop_distributions` |
| `GameSimResult` | One game's result: the score, the innings, the final state, the walk-off and extra-innings flags, the seed, the pitch total, `outs_played`, the box score and the inning grid (`home_by_inning` / `away_by_inning`; `None` marks a half never played). | `simulate_game`, `results.GameSimSummary.from_results` |

### `simulation/loop/credit.py`

[Source](../../simulation/loop/credit.py) · 225 lines · Tests: [test_player_stat_lines.py](../../tests/unit/test_player_stat_lines.py), [test_runner_run_credit.py](../../tests/unit/test_runner_run_credit.py), [test_dropped_third_strike_credits.py](../../tests/unit/test_dropped_third_strike_credits.py)

`CreditMixin`: the box-score credit of a completed plate appearance. Two credits happen
elsewhere, when the play happens: a pitcher's outs (`_record_outs`) and the steal credits
(`loop/running_game.py`).

| Function / class | What it does | Called from |
|---|---|---|
| `_accumulate_pa` | Credits the batter (at-bat, hits by type, strikeout, RBI) and the pitcher (batter faced, strikeout, walk, hit allowed, earned and total runs), and a run to every runner whose `baserunner_advances` entry reads 0. A run on an error play, or after the inning should have ended, is unearned. A run that scored on a steal, a got-away pitch or a dropped-third-strike reach pays no RBI. | `StateMachine._end_of_pa` |
| `_box_line` | A player's line, created on first use. | every credit site |
| `_current_batter_id` | The batter who just finished, read from the offense's lineup slot. | `_accumulate_pa` |

### `simulation/loop/in_play.py`

[Source](../../simulation/loop/in_play.py) · 475 lines · Tests: [test_transition_draw.py](../../tests/unit/test_transition_draw.py), [test_fielding_and_baserunning_steps.py](../../tests/unit/test_fielding_and_baserunning_steps.py), [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py)

`InPlayMixin`: the ball in play. The fielding draw picks one row of the batted-ball pool; that
row's event, outs and runner destinations become the play. Five extra-base movements are the
runners' own choices, so the advancement draws decide them instead of the row.

| Function / class | What it does | Called from |
|---|---|---|
| `_resolve_in_play` | Snapshots the bases, runs the fielding draw and applies its row. Without a sampler the in-play pitch stays unresolved. | `StateMachine.step_pitch` |
| `_full_pool_fielding` | Builds the fielding draw's inputs (the batter, the situation, the pitcher's hand, the batting side, the park factor, the fielding team's defense, the born batted ball, the live park) and draws a row. It raises when the bundle carries no transition columns. | `_resolve_in_play` |
| `_resolve_in_play_transition` | Seats the batter, then the runners, at the row's destinations; clamps the five discretionary movements to one base at a time (`_normalized_dests`); runs the advancement draws; and commits once through the run ledger. | `_resolve_in_play` |
| `_run_advancement_draws` | The five discretionary decisions: first to third on a single, second to home on a single, first to home on a double, the tag-ups on a caught fly, and the batter's stretch. The lead runner goes first and a trailer cannot pass him. A tag from third that scores turns the play into a sacrifice fly. | `_resolve_in_play_transition` |
| `_live_fielder_at_drawn_position` | The live defender at the drawn row's position, from the fielding team's defense map. | `_full_pool_fielding`, `_run_advancement_draws` |
| `_seat` | Puts a runner on his base, or the nearest open base, so a rare colliding row never loses a runner. | `_resolve_in_play_transition`, `_run_advancement_draws` |

### `simulation/loop/running_game.py`

[Source](../../simulation/loop/running_game.py) · 510 lines · Tests: [test_running_game_on_the_pitch.py](../../tests/unit/test_running_game_on_the_pitch.py), [test_steal_draw.py](../../tests/unit/test_steal_draw.py), [test_steal_run_credit.py](../../tests/unit/test_steal_run_credit.py)

`RunningGameMixin`: the steal, the pickoff and the got-away advance (a wild pitch or a passed
ball). In production the pickoff draw runs before the pitch and the steal draw after it, among
real pitches of the same count and class (the running game on the pitch, SIM-554). Without the
class, one draw before the pitch answers both questions.

| Function / class | What it does | Called from |
|---|---|---|
| `_steal_order_active` | True when the sampler's `steal_pitch_class` is on and every steal pool carries the pitch class. Both reads must be exactly `True`, so a mock sampler keeps the single draw. | `step_pitch`, `ManagerMixin._pre_pitch_hook` |
| `_pickoff_before_the_pitch` | Draws the pickoff for the lead stealable runner and resolves it at once. | `step_pitch` |
| `_steal_opportunity_draw` | Draws one row of the steal pool for the lead stealable runner (on first with second open, or on second with third open). With `pitch_class` it stages a steal only; without it the row may carry a pickoff. The batting manager's aggression weighs attempted rows. | `step_pitch`, `ManagerMixin._pre_pitch_hook` |
| `_steal_aggression` | The batting manager's steal-order rate over the season's league mean, clamped to [0.05, 4.0]; 1.0 with no manager or no profile. | `_steal_opportunity_draw` |
| `_resolve_steal_outcome` | Resolves the staged steal: a safe runner moves up (a steal of home scores a run with no RBI), a caught runner is out. It credits the stolen base or the caught stealing. | `step_pitch` |
| `_resolve_pickoff` | A pickoff out removes the runner (a caught stealing when he was breaking); an errant throw moves him up one base. | `_resolve_steal_outcome`, `_pickoff_before_the_pitch` |
| `_resolve_got_away_advance` | Every runner moves up one base and a runner on third scores with no RBI. | `step_pitch`, `TerminalsMixin._resolve_strikeout` |
| `stage_steal` | Stages a steal for the same pitch (or, from a test, the next pitch). | `_steal_opportunity_draw`, tests |

### `simulation/loop/terminals.py`

[Source](../../simulation/loop/terminals.py) · 297 lines · Tests: [test_hit_by_pitch.py](../../tests/unit/test_hit_by_pitch.py), [test_dropped_third_strike_credits.py](../../tests/unit/test_dropped_third_strike_credits.py), [test_uncaught_strike_three.py](../../tests/unit/test_uncaught_strike_three.py)

`TerminalsMixin`: the plate appearances that end without a ball in play.

| Function / class | What it does | Called from |
|---|---|---|
| `_resolve_walk` | Puts the batter on first and pushes forced runners; a hit by pitch uses the same mechanics under its own event. | `step_pitch`, `_issue_intentional_walk` |
| `_issue_intentional_walk` | Issues the walk the manager signalled, with no pitch thrown (`pitch_outcome` reads `"ball"`), and ends the plate appearance. | `step_pitch` |
| `_resolve_strikeout` | Strike three. On a dropped third strike (a swinging strike three that got away, with first open or two outs) the batter reaches first and no out is recorded; the play still counts as a strikeout for both players. When the pitch got away and no steal or pickoff moved anyone, the runners advance first. | `step_pitch` |
| `_dropped_third_strike` | The rule: a swinging strike three, first base open or two outs, and the drawn row's got-away fact. It reads first base and the outs as they stood at the pitch. | `_resolve_strikeout` |
| `_force_on_reach` | Seats the batter on first and pushes forced runners after a dropped third strike. | `_resolve_strikeout` |

### `simulation/loop/manager.py`

[Source](../../simulation/loop/manager.py) · 282 lines · Tests: [test_manager_draw.py](../../tests/unit/test_manager_draw.py), [test_relief_draw.py](../../tests/unit/test_relief_draw.py), [test_manager_decisions.py](../../tests/unit/test_manager_decisions.py), [test_ibb_rates.py](../../tests/unit/test_ibb_rates.py)

`ManagerMixin`: the leverage index, the intentional walk and the pitching change. With no manager
wired (`StateMachine.manager is None`), every hook does nothing.

| Function / class | What it does | Called from |
|---|---|---|
| `compute_leverage` | A leverage index (how much the spot can swing the win probability; 1.0 is average) from the inning, the score margin and the base-out state, clamped to [0.05, 6.0]. | `_pre_pitch_hook`, `_start_of_pa_hook` |
| `_pre_pitch_hook` | Runs the intentional walk, then, without the running game on the pitch, the single pre-pitch steal draw. | `step_pitch` |
| `_should_issue_ibb` | On a plate appearance's first pitch, one random number against the real rate of its cell (runners, outs, seventh inning or later, within one run) from `sim.ibb_rates`. | `_pre_pitch_hook` |
| `_start_of_pa_hook` / `_maybe_pull_starter` | On a plate appearance's first pitch, the pitching change: with `manager_draw` on, one draw from the change pool. | `step_pitch` |
| `_pitching_change_by_draw` | Passes the boundary's facts (the pitcher, starter or not, a new half or not, pitches, batters faced, inning, outs, runners, the fielding side's margin, the fielding manager) to `FullPoolSampler.pitching_change_draw`. | `_maybe_pull_starter` |
| `_change_pitcher` / `_pick_reliever` | Brings in the arm the reliever draw picks from the live pen, and records the change in `manager_decisions`. With every reliever weight off, the pick is positional: the first listed arm in a late high-leverage spot, else the last. | `_maybe_pull_starter` |

### `simulation/loop/base.py`

[Source](../../simulation/loop/base.py) · 73 lines · Tests: none

`LoopMixin`: the attributes and methods the mixins read from each other, declared for the type
checker only. At run time the class is empty.

### `simulation/loop/__init__.py`

[Source](../../simulation/loop/__init__.py) · 23 lines · Tests: none

The package docstring: one line per mixin module.

## The sampler

`FullPoolSampler` makes every draw. Its constructor lives in `full_pool_sampler.py`; its draws live
in mixins in `simulation/sampler/`, one module per draw. Every switch, power and bandwidth is a
plain attribute that `production_factory` sets from the `SIM_*` flags. Each defaults to the value
that makes its factor do nothing (a power of 1.0, a bandwidth of 0.0, a weight of 1.0), except
`steal_pitch_class`, which defaults to on. Every draw takes its random number from `self.rng`.

### `simulation/full_pool_sampler.py`

[Source](../../simulation/full_pool_sampler.py) · 73 lines · Tests: [test_pitch_cell_index.py](../../tests/unit/test_pitch_cell_index.py), [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py), [test_steal_draw.py](../../tests/unit/test_steal_draw.py), [test_transition_draw.py](../../tests/unit/test_transition_draw.py)

`FullPoolSampler(artifacts, rng, *, sit_sigma=2.0, platoon_off_weight=0.6, home_off_weight=1.0)`
over one `EngineArtifacts` bundle. The constructor calls one `_init_*` per mixin.

### `simulation/sampler/kernels.py`

[Source](../../simulation/sampler/kernels.py) · 72 lines · Tests: [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py)

Module-level helpers.

| Function / class | What it does | Called from |
|---|---|---|
| `_repower_neutral` / `_raise_neutral` | Raise a weight factor to a power. A row whose actor has no score gets the mean scored weight, so it stays neutral at any power. Power 1.0 returns the input unchanged. | `sampler/pitch.py`, `sampler/pitch_result.py` |
| `_PITCH_FEATURE_WEIGHTS` | The pitch-to-pitch engine's ten feature weights; a unit test pins them to the engine's. | `sampler/pitch_result.py` |
| `_group_rows` / `_count_cell` | Group the steal pool's rows by count. | `sampler/steal.py` |

### `simulation/sampler/base.py`

[Source](../../simulation/sampler/base.py) · 483 lines · Tests: [test_pool_cutoff.py](../../tests/unit/test_pool_cutoff.py), [test_actor_matrices.py](../../tests/unit/test_actor_matrices.py), [test_actor_powers.py](../../tests/unit/test_actor_powers.py), [test_pitcher_sim_matrix.py](../../tests/unit/test_pitcher_sim_matrix.py)

`BaseMixin`: what more than one draw reads.

| Function / class | What it does | Called from |
|---|---|---|
| `_pool_meta` | The pitch pool's one-time precompute per batter hand: each row's pitcher and batter index, the twelve count buckets, the outcome and taken-pitch arrays, the base-out columns. | `sampler/pitch.py`, `sampler/pitch_result.py` |
| `set_asof` / `_apply_asof` | The point-in-time cutoff of a backtest (a YYYYMMDD date): every draw gives zero weight to plays after it. It raises when a cutoff is set on a pool with no dates. | `production_factory.production_machine_factory`; every draw |
| `_matrix_gather` / `_actor_factor` | The live actor's row of a nightly score matrix (`EngineArtifacts.actor_sim`), gathered onto each pool row and raised to the actor's power (`actor_power`). An unscored row reads 1.0 at power 1, and the mean scored weight at any other power. Power 0 turns the actor off. | `sampler/pitch.py`, `sampler/fielding.py`, `sampler/steal.py`, `sampler/advancement.py` |
| `_pitcher_prof_score` / `_neutral_mean_pitcher` / `_batter_factor_rows` / `_neutral_mean_batter` | The pitcher factor's score vector from the dense pitcher matrix, the batter factor on a set of rows, and the mean scored weights that keep unscored rows neutral. | `sampler/pitch.py`, `sampler/pitch_result.py` |

**Watch out:** `test_pool_cutoff.py` counts `self._apply_asof(` across the sampler's source and
expects five draw sites (the pitch base, the batted ball, the pitching change, the steal, the
advancement). A new draw over a dated pool must apply the cutoff and update that count.

### `simulation/sampler/pitch.py`

[Source](../../simulation/sampler/pitch.py) · 563 lines · Tests: [test_pitch_cell_index.py](../../tests/unit/test_pitch_cell_index.py), [test_matchup_weight_cache.py](../../tests/unit/test_matchup_weight_cache.py), [test_draw_conditioning.py](../../tests/unit/test_draw_conditioning.py), [test_sampler_hot_path_cache.py](../../tests/unit/test_sampler_hot_path_cache.py)

`PitchMixin`: the pitch draw. A row's weight is the pitcher factor times recency (fixed for the
half-inning), times the batter factor and the base-out situation kernel (fixed for the plate
appearance), times the gated extras. The weight splits into twelve count buckets once per plate
appearance; each pitch is one draw in the live count's bucket.

| Function / class | What it does | Called from |
|---|---|---|
| `new_half_inning` | Caches the half-inning base weight (the pitcher factor at `pitch_pitcher_power`, times recency, the cutoff applied) and stages the fielding catcher. | `StateMachine._full_pool_outcome` |
| `new_plate_appearance` | Builds the plate appearance's weight: the base, the batter factor at `pitch_batter_power`, a Gaussian on the base-out columns (outs, runners, inning, margin; bandwidth `sit_sigma`), and the fatigue, batting-side and receiving weights when on. With `pitch_cell_index` on it builds over the live cell only (`_new_plate_appearance_cell`). | `StateMachine._full_pool_outcome` |
| `_subcell_rows` | The rows of one count sub-cell of the live cell (runners, outs, score band, batting side). A sub-cell under `pitch_min_cell` rows widens in a fixed order: the score bands, then the sides, then the counts. An empty base-out cell raises. | `_new_plate_appearance_cell` |
| `_f_fatigue` / `_fatigue_rows` | Gaussians on the gap between the live pitcher's pitch count and times through the order and the row's, normalized to a mean of 1 within each count bucket, so they change which pitch is drawn at a count, never how often. | `new_plate_appearance` |
| `draw` | Draws one row in the live count's bucket. With the split on, the result draw then picks the play among the same rows. An empty bucket returns `"ball"`. | `StateMachine._full_pool_outcome` |
| `cell_index_stats` | The draws per widening level, for the acceptance lane's report. | `tests/acceptance/conftest.py` |

**Flags (set by `production_factory`):** `SIM_SIT_SIGMA` (2.0), `SIM_PITCH_CELL_INDEX` (default off; production on), `SIM_PITCH_MIN_CELL` (20), `SIM_FATIGUE_TTO_SIGMA` (default 0; production 0.5), `SIM_FATIGUE_PC_SIGMA` (0), `SIM_PITCH_HOME_OFF_WEIGHT` (1.0, off), `SIM_PITCH_PITCHER_POWER` and `SIM_PITCH_BATTER_POWER` (1.0).

### `simulation/sampler/pitch_result.py`

[Source](../../simulation/sampler/pitch_result.py) · 372 lines · Tests: [test_pitch_result_split.py](../../tests/unit/test_pitch_result_split.py), [test_receiving_ratio.py](../../tests/unit/test_receiving_ratio.py), [test_receiving_kernel_consumers.py](../../tests/unit/test_receiving_kernel_consumers.py)

`PitchResultMixin`: the pitch / pitch-result split, the catcher receiving ratio and the readers
of the last drawn row. With the split on, the pitch draw picks the pitch thrown and the result
draw picks the play among the same candidate rows, weighted toward rows whose pitch resembles the
drawn one.

| Function / class | What it does | Called from |
|---|---|---|
| `result_weights` | The result draw's weight: the plate appearance's raw weight, times a Gaussian on the pitch engine's distance to the drawn pitch (`result_pitch_sigma`), times an inverse-density correction so crowded pitch regions are not over-drawn (`result_density_power`), times the pitcher and batter factors raised again to their result powers, times the receiving ratio. | `_result_draw`, `scripts/offline_draw_fit.py`, `scripts/running_game_replay.py` |
| `_result_draw` | Draws the result row; falls back to the pitch row when nothing can condition the draw. | `PitchMixin.draw` |
| `last_pitch_got_away` | The last result row's got-away fact (a wild pitch, a passed ball or an uncaught third strike). | `StateMachine._full_pool_outcome` |
| `last_born_batted_ball` | The last result row's own batted ball (exit velocity, launch angle, spray, distance, class, park), through the bundle's pitch-to-batted-ball join. | `InPlayMixin._full_pool_fielding` |
| `last_pitch_geom` | The pitch thrown's geometry, for the fielding draw's pitch-similarity kernel. | `InPlayMixin._full_pool_fielding` |
| `_recv_factor` / `_apply_receiving` | The catcher receiving ratio: on taken pitches, moves weight between called strikes and balls by the catcher's framing and blocking, then rescales the taken rows to their old total. | `PitchMixin.new_plate_appearance`, `result_weights` |

**Flags:** `SIM_PITCH_RESULT_SPLIT` (default off; production on), `SIM_RESULT_PITCH_SIGMA` (1.0), `SIM_RESULT_DENSITY_POWER` (1.0), `SIM_RESULT_PITCHER_POWER` and `SIM_RESULT_BATTER_POWER` (1.0), `SIM_CATCHER_RECEIVING` (off; production off).

### `simulation/sampler/fielding.py`

[Source](../../simulation/sampler/fielding.py) · 801 lines · Tests: [test_fielding_draw.py](../../tests/unit/test_fielding_draw.py), [test_transition_draw.py](../../tests/unit/test_transition_draw.py), [test_realism_consumers.py](../../tests/unit/test_realism_consumers.py)

`FieldingMixin`: the fielding (batted-ball) draw. The filters, in order: the exact base-out cell
(always), the born ball's class, the fence stage, the wall-margin band. The drawn row carries its
whole base-out transition.

| Function / class | What it does | Called from |
|---|---|---|
| `battedball_new_pa` | Builds the candidate rows and their weights: the batter factor at `bb_batter_power`, a Gaussian on the soft situation (balls, strikes, inning, margin), recency and the cutoff; then the platoon weight on opposite-hand pitcher rows, the batting-side weight, the park kernel (only for a wall-zone ball when `park_wall_zone_only` is on), the live fielder factor, the pitch-similarity kernel and the born-ball kernel with its density correction. When every weight underflows, the draw runs uniform over the filtered rows. It raises on a bundle without transition columns or an empty base-out cell. | `InPlayMixin._full_pool_fielding` |
| `battedball_draw` / `last_transition` / `last_battedball_fielder` | Draw one row; read its transition (each runner's and the batter's destination, the thrown-out-advancing flags, the throw geometry) and its fielder. | `InPlayMixin` |
| `_f_live_fielder` | The live defender at each row's position, scored against the row's fielder by that position's matrix, rescaled to a mean of 1 within each position so the factor never moves balls between positions. | `battedball_new_pa` |
| `_bb_class_rows` | The cell's rows of the born ball's class (ground ball, line drive, fly ball, popup, bunt); the whole cell when the class is empty. | `battedball_new_pa` |
| `_f_born_similarity` / `_born_inv_density` | A Gaussian on the distance between the born ball and each row (exit velocity, launch angle, spray, distance), measured on the born ball's class, divided by each row's local density. | `battedball_new_pa` |

**Flags:** `SIM_BB_CLASS_FILTER` (default off; production on), `SIM_BB_BATTER_POWER` (1.0), `SIM_BB_BORN_SIGMA` (default 0; production 1.0), `SIM_BB_BORN_DENSITY_POWER` (1.0), `SIM_BB_BORN_PER_FEATURE` (default off; production on), `SIM_BB_PITCH_SIGMA` (0), `SIM_HOME_OFF_WEIGHT` (default 1.0; production 0.0, a hard match on the batting side), `SIM_PARK_KERNEL_SIGMA` (default 0; production 0.02), `SIM_PARK_WALL_ZONE_ONLY` (default off; production on). The platoon weight (0.6) is a constructor default, not a flag.

### `simulation/sampler/fence.py`

[Source](../../simulation/sampler/fence.py) · 369 lines · Tests: [test_fence_stage.py](../../tests/unit/test_fence_stage.py), [test_fence_carry_and_margin.py](../../tests/unit/test_fence_carry_and_margin.py), [test_fence_season_groups.py](../../tests/unit/test_fence_season_groups.py), [test_fence_counters.py](../../tests/unit/test_fence_counters.py)

`FenceMixin`: the fence stage, the carry and the wall-margin band. Before the fielding draw, the
born ball's carry meets the live park's fence at its direction (the park geometry document,
`park_geometry.json` in the bundle).

| Function / class | What it does | Called from |
|---|---|---|
| `fence_decision` | 0 over the fence (the draw keeps home-run rows), 1 short (it drops them), 2 inside the band, 3 no decision. A ground ball, popup or bunt is always short. | `_fence_rows` |
| `fence_at` | The live park's fence, in feet of carry, at a spray angle in a season (ten-degree sectors from −55° to +55°; a park's line per season group; the league line where the park has none). | `fence_decision`, `_bb_margin_stage` |
| `carry_of` / `carry_shift` / `_born_in_live_air` | The born ball's carry: its distance (or the bundle's carry model on exit velocity and launch angle), plus the live park's carry offset minus the offset of the park it was hit in. One working copy feeds the class filter, the fence, the band and the born kernel. | `FieldingMixin.battedball_new_pa` |
| `_bb_margin_stage` | For a wall-zone air ball (a line drive or fly ball of `wall_zone_distance` feet or more) the fence did not call over: keep only rows whose margin to their own park's fence lies within `bb_margin_band` feet of the born ball's margin to the live fence. Fewer than `bb_margin_min_rows` survivors leave the rows unchanged. | `FieldingMixin.battedball_new_pa` |

**Flags:** `SIM_FENCE_STAGE` (default off; production on), `SIM_FENCE_MARGIN` (0, a decisive fence), `SIM_CARRY_OFFSET` (default off; production on), `SIM_BB_MARGIN_BAND` (default 0; production 15), `SIM_BB_MARGIN_MIN_ROWS` (20), `SIM_WALL_ZONE_DISTANCE` (300).

### `simulation/sampler/steal.py`

[Source](../../simulation/sampler/steal.py) · 362 lines · Tests: [test_steal_draw.py](../../tests/unit/test_steal_draw.py), [test_steal_weights_seam.py](../../tests/unit/test_steal_weights_seam.py), [test_running_game_on_the_pitch.py](../../tests/unit/test_running_game_on_the_pitch.py)

`StealMixin`: the steal and pickoff draws over the steal opportunity pool (one row per real pitch
with a stealable runner, plus the pickoff rows: real pickoffs thrown before any pitch of their
pair). The pools are keyed by the target base.

| Function / class | What it does | Called from |
|---|---|---|
| `steal_draw` | With `pitch_class`: one row among the real pitches of the same target base, outs, balls, strikes and class; an empty group stages no steal. Without it: one row of the count group, which may carry a pickoff. Returns (attempted, success, pickoff out, pickoff advancing, pickoff error). | `RunningGameMixin._steal_opportunity_draw` |
| `pickoff_draw` | One row of the count group, the pitch rows and the pickoff rows together, with no manager weight. It skips a group that holds no pickoff outcome (no random number used). | `RunningGameMixin._pickoff_before_the_pitch` |
| `steal_weights` | The one weight path of both draws: recency, a Gaussian on the score margin (`steal_score_sigma`, 2 runs), the runner, pitcher and catcher-throwing matrix factors, and the manager's aggression on attempted rows. It uses no random number. | `steal_draw`, `pickoff_draw`, `scripts/running_game_replay.py` |
| `_steal_meta` / `has_steal_classes` | The per-target group indexes. The class groups exist only when every pitch row carries a class; one unclassed target keeps the single draw for both targets and logs a warning. | `steal_weights`, `RunningGameMixin._steal_order_active` |

**Flags:** `SIM_STEAL_PITCH_CLASS` (default on; production on). `SIM_ACTOR_POWER_RUNNER_STEAL`, `SIM_ACTOR_POWER_PITCHER_STEAL` and `SIM_ACTOR_POWER_CATCHER_THROWING` (production 1).

### `simulation/sampler/advancement.py`

[Source](../../simulation/sampler/advancement.py) · 144 lines · Tests: [test_transition_draw.py](../../tests/unit/test_transition_draw.py), [test_run_ledger.py](../../tests/unit/test_run_ledger.py)

`AdvancementMixin`: the advancement draws. Each (scenario, from base, target base) is its own pool
of real opportunities, attempted or not.

| Function / class | What it does | Called from |
|---|---|---|
| `advancement_draw` | Weighs the pool by recency, a Gaussian on the z-scored throw geometry (exit velocity, launch angle, spray, distance, outs; `adv_feat_sigma` 1.0), the runner's matrix score and the live fielder's score at his position. Returns (attempted, safe, error extra base), or None without a pool. | `InPlayMixin._run_advancement_draws` |

**Flags:** `SIM_ACTOR_POWER_RUNNER_ADV` and `SIM_ACTOR_POWER_FIELDER` (production 1).

### `simulation/sampler/manager.py`

[Source](../../simulation/sampler/manager.py) · 440 lines · Tests: [test_manager_draw.py](../../tests/unit/test_manager_draw.py), [test_relief_draw.py](../../tests/unit/test_relief_draw.py)

`ManagerMixin` (the sampler's): the pitching-change draw and the reliever draw.

| Function / class | What it does | Called from |
|---|---|---|
| `pitching_change_draw` | One row of the change pool's hard cell (starter or reliever, a half-inning boundary or not, the pitch-count bucket of ten, times through the order), widened below `change_min_cell` rows. Weights: recency, a Gaussian on the z-scored situation (`change_sit_sigma`), the live pitcher's similarity to the row's pitcher, and the live manager's usage similarity to the row's manager. The row's `changed` flag is the decision. | `StateMachine._pitching_change_by_draw` |
| `relief_arm_draw` | Picks the entering arm from the live pen by its resemblance to the arm the drawn change row brought in: role, stuff, days of rest, pitched in the last two days, pitches over three days and hand. Every weight off returns None. | `StateMachine._pick_reliever` |
| `_change_manager_factor` | The manager-usage matrix row gathered onto the cell's rows, at `actor_power["manager_usage"]` (0 = off, the code default). | `pitching_change_draw` |
| `last_change_row` | The last drawn change row, for inspection. | tests |

**Flags:** `SIM_CHANGE_SIT_SIGMA` (1.0), `SIM_CHANGE_PITCHER_POWER` (1.0), `SIM_CHANGE_MIN_CELL` (20), `SIM_ACTOR_POWER_MANAGER_USAGE` (default 0; production 1), `SIM_RELIEF_ROLE_SIGMA` (default 0; production 0.1), `SIM_RELIEF_PITCHER_POWER` (default 0; production 1), `SIM_RELIEF_REST_SIGMA` (default 0; production 0.5), `SIM_RELIEF_PITCHED2D_OFF_WEIGHT` (default 1.0; production 0.25), `SIM_RELIEF_PITCHES3D_SIGMA` (default 0; production 10), `SIM_RELIEF_HAND_OFF_WEIGHT` (1.0, off).

### `simulation/sampler/__init__.py`

[Source](../../simulation/sampler/__init__.py) · 23 lines · Tests: none

The package docstring: one line per draw module.

## Game state and values

### `simulation/game_state.py`

[Source](../../simulation/game_state.py) · 779 lines · Tests: [test_game_state_contract.py](../../tests/unit/test_game_state_contract.py), [test_base_invariants.py](../../tests/unit/test_base_invariants.py), [test_invalid_state_detection.py](../../tests/unit/test_invalid_state_detection.py), [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py)

The data the loop reads and writes. `GameState` is one game's mutable state: the count, the outs,
the bases, the score, both batting orders and their slot pointers, both defenses, the managers,
the pens and the per-pitcher counters. `PlayResult` is the result of one pitch.

| Function / class | What it does | Called from |
|---|---|---|
| `Bases` | The runners by player id. `runners_state` is the 3-bit mask (bit 0 first, bit 1 second, bit 2 third) that the run-expectancy table and the pools share. | the whole loop |
| `Bases.assert_transition` | The runner-conservation check of one play: runners before plus the batter who reached equal runners after plus runners who scored or were put out. With `batter_id` it also catches an invented or swapped runner. | `StateMachine._commit_run_delta` |
| `GameState.bat_hand_for` | The batter's hand for the draws: a switch hitter bats opposite the pitcher. | `StateMachine`, `simulate_game`, `start_state` |
| `GameState.assert_invariants` | The committed-state checks: the count, the outs, the score, the bases. | `StateMachine`, `simulate_game` |
| `PlayResult` | One pitch: the outcome, the event, the run value and its source, the batted ball, the fielder, the runners' movements, the running-game outcomes, the pitcher on the mound (`pitcher_id`, `fielding_team`) and the committed next state. | every reader of the play stream |
| `NO_PITCH` / `PlayResult.no_pitch` | The outcome of a result with no pitch thrown (a pickoff third out). It is not one of `PITCH_OUTCOMES`. | `step_pitch`, `simulate_game`, `snapshots` |

**Watch out:** Every reader that counts pitches, plate appearances or count changes must skip a
no-pitch result; the result still carries the pickoff's out. On the play that ends a
half-inning, `next_state` already names the next half's pitcher, so a reader asks the play's own
`pitcher_id` and `fielding_team` who pitched it.

### `simulation/run_resolution.py`

[Source](../../simulation/run_resolution.py) · 254 lines · Tests: [test_run_resolution.py](../../tests/unit/test_run_resolution.py), [test_run_ledger.py](../../tests/unit/test_run_ledger.py)

The run value of one play: run expectancy after, minus run expectancy before, plus the runs that
scored. Run expectancy (RE24) is the average runs from each of the 24 base-out states to the end
of the inning.

| Function / class | What it does | Called from |
|---|---|---|
| `resolve_runs` | Takes both base-out states and the runs scored (all five values required) and returns a `RunResolution` with `method = "re24_delta"`. It raises on a missing value or an impossible change (negative outs, outs removed, negative runs). | `StateMachine._commit_run_delta` |
| `re24_value` / `RE24_MATRIX` | The bundled 24-state table for a recent (about 2024) run environment; 0.0 at three outs. | `resolve_runs` |
| `re24_from_rows` | Builds a table of the same shape from `derived.run_expectancy_matrix` rows. No caller passes one today. | tests |

### `simulation/constants.py`

[Source](../../simulation/constants.py) · 159 lines · Tests: [test_simulation_constants.py](../../tests/unit/test_simulation_constants.py), [test_run_resolution.py](../../tests/unit/test_run_resolution.py)

The shared vocabularies: the thirteen canonical plate-appearance outcomes, the map from Statcast's
event strings onto them (`resolve_event_to_canonical`), the position numbers (`FIELD_POSITION_NAMES`)
and the defensive run values the profile computor reads. A reach on an error (`field_error`) is
its own outcome, never a hit. An import-time check makes every alias target a canonical key.

| Function / class | What it does | Called from |
|---|---|---|
| `resolve_event_to_canonical` | A Statcast event or canonical key to its canonical key; None for an unknown token. | `run_resolution.resolve_runs`, `CreditMixin._accumulate_pa` |
| `FIELD_POSITION_NAMES` | Scorebook number (1-9) to position name. | `loop/in_play.py`, `sampler/fielding.py`, `sampler/advancement.py` |
| `DEFENSIVE_RUN_VALUES` | Runs saved per out above average (0.75 infield, 0.90 outfield), per block (0.25) and per framing strike (0.125). | `pipeline/batch/profiles/defense.py` |

### `simulation/flags.py`

[Source](../../simulation/flags.py) · 544 lines · Tests: [test_flag_registry.py](../../tests/unit/test_flag_registry.py), [test_script_flag_names.py](../../tests/unit/test_script_flag_names.py)

The registry of every `SIM_*` flag: its name, type, code default, the work that owns it and a
one-line meaning. Code reads a flag only through these readers.

| Function / class | What it does | Called from |
|---|---|---|
| `FLAGS` / `Flag` | One row per flag. | `production_factory`, the registry test |
| `read_bool` | On: `1`, `true`, `yes`, `on`. Off: empty, `0`, `false`, `no`, `off`. Unset reads the default; anything else raises `FlagValueError`. | `production_factory`, `sim_loop`, `api/` |
| `read_float` / `read_int` / `read_str` | A number keeps its default when unset or unparsable (with a warning); text reads as set. | `production_factory`, `batch_runner`, `lineup_resolver`, `api/` |
| `actor_power_overrides` | The `SIM_ACTOR_POWER_<ACTOR>` family as `{actor: power}`; a per-position fielder power keeps its position. | `production_factory.apply_actor_matrix_env` |

**Watch out:** The code defaults are not production's. The production values live in the `app`
service of `docker-compose.yml`; `tests/conftest.py` pins the unit lane's. The registry test
fails when code reads, or compose sets, an unregistered name.

### `simulation/filter_cells.py`

[Source](../../simulation/filter_cells.py) · 193 lines · Tests: [test_pitch_cell_index.py](../../tests/unit/test_pitch_cell_index.py), [test_fatigue_kernel_scan.py](../../tests/unit/test_fatigue_kernel_scan.py)

The one definition of the pitch draw's hard-filter cell: runners (8) × outs (3) × count (12) ×
score band (5) × batting side (2) = 2,880 cells. The score bands are ≤−3, −2 to −1, 0, +1 to +2
and ≥+3, on a margin clamped to ±5. `DEFAULT_MIN_CELL` (20) is the measured floor below which a
cell widens.

| Function / class | What it does | Called from |
|---|---|---|
| `score_band` / `score_band_array` | A margin to its band. | `sampler/pitch.py` |
| `cell_key` / `decode_cell` / `count_bucket` | Encode a situation to its cell id and back. | `scripts/measure_filter_cells.py` |

**Watch out:** A cell id computed under other band edges does not compare with an earlier census
(`scripts/measure_filter_cells.py`). The band edges are checked at import.

## Running many games

### `simulation/batch_runner.py`

[Source](../../simulation/batch_runner.py) · 1261 lines · Tests: [test_batch_runner.py](../../tests/unit/test_batch_runner.py), [test_persistent_process_pool.py](../../tests/unit/test_persistent_process_pool.py), [test_shared_memory_attach.py](../../tests/unit/test_shared_memory_attach.py), [test_worker_forkserver.py](../../tests/unit/test_worker_forkserver.py), [test_worker_prewarm.py](../../tests/unit/test_worker_prewarm.py)

The parallel Monte-Carlo runner: it plays one matchup N times across worker processes and
aggregates the results. A worker never receives a live sampler; it receives a picklable
`GameSpec` (the sim kwargs and a dotted path to a machine factory) and builds its own machine.

| Function / class | What it does | Called from |
|---|---|---|
| `BatchRunner.run` | Derives N seeds (`derive_seed`: base seed plus the game's index), plays the games (`_execute`), aggregates a `GameSimSummary` and caches it for 60 seconds. | `api/routes/games.py` (`_run_batch`, which the betting routes also use) |
| `BatchRunner.run_job` | Plays a run job's games in chunks, reports progress, stops on a cancel, and returns the summary and every prop distribution from the one batch. | `api/sim_jobs.py` |
| `BatchRunner.run_prop_set` | Plays the games and returns the prop distributions; caches a seeded set for 15 minutes. | `api/routes/games.py` (`_build_prop_set`) |
| `BatchRunner.record_game` | Records one game with its plays on a worker, so the API process never loads the bundle. | `api/routes/games.py` |
| `BatchRunner.prewarm` | Starts every worker and warms its sampler cache before the first request, a few at a time (`SIM_PREWARM_MAX_CONCURRENT`), within a 120-second deadline. | `api/main.py` (`_background_prewarm`) |
| `_run_one` | The worker's unit of work: build the machine through the factory, drop the factory-only (`_`-prefixed) keys, call `simulate_game`. | `_execute` |
| `publish_shared_arrays` / `_worker_init` | The parent copies the bundle's big read-only arrays into shared memory once; each worker attaches them with no copy. Only the parent unlinks the segments. | `BatchRunner._pool_kwargs` |
| `make_cache` / `RedisCache` / `InMemoryCache` | The cache: Redis when a server answers, else an in-process cache. Redis values are typed JSON (`cache_codec`), never a pickle, under the key prefix `sim443:`. | `BatchRunner.__init__` |
| `rng_driven_machine_factory` | The no-database factory: the production `StateMachine` and `FullPoolSampler` over `synthetic_bundle.league_artifacts()`. The factory-only `_hit_rate` key reshapes the in-play mix. | `play_recorder` (its default), tests, `scripts/behaviour_fingerprint.py` |

**Flags:** `SIM_MP_START_METHOD` (forkserver) and `SIM_PREWARM_MAX_CONCURRENT` (default 2; production 4), read once at import. `api/startup.py` reads `SIM_RUNNER_WORKERS`; unset, the runner uses min(CPU count − 1, 10).

**Watch out:** Keep the forkserver start method: a forked worker inherits the app's engines
(about 6 GB) and its memory grows toward that, while a forkserver worker loads only the bundle it
needs. `tests/unit/test_cache_codec.py` fails if this module imports `pickle`.

### `simulation/production_factory.py`

[Source](../../simulation/production_factory.py) · 633 lines · Tests: [test_actor_powers.py](../../tests/unit/test_actor_powers.py), [test_manager_model.py](../../tests/unit/test_manager_model.py), [test_ibb_rates.py](../../tests/unit/test_ibb_rates.py), [test_situation_sigma_env.py](../../tests/unit/test_situation_sigma_env.py), [test_worker_prewarm.py](../../tests/unit/test_worker_prewarm.py)

The production machine factory. It builds a `FullPoolSampler` over the on-disk bundle once per
worker process, sets every `SIM_*` switch on it, and attaches the manager when `SIM_MANAGER` is on.
There is no fallback: a missing or broken bundle raises.

| Function / class | What it does | Called from |
|---|---|---|
| `production_machine_factory` | Builds or reuses the cached sampler, sets the backtest cutoff (or clears the last game's), attaches the manager marker, the intentional-walk rates and the placeholder pen, sets `manager_draw` from `SIM_MANAGER_DRAW`, and returns the `StateMachine`. | `batch_runner._run_one`, `play_recorder.record_game` (the API names it as `PRODUCTION_FACTORY_REF`) |
| `_build_full_pool_sampler` | Loads the bundle (with the parent's shared arrays spliced in), builds the sampler, and applies every flag: inline for the home, park, fatigue, pitch-similarity, situation and cell-index settings, then `apply_actor_matrix_env`, `apply_result_split_env`, `apply_fielding_env`, `apply_manager_env` and `apply_running_game_env`. The intentional-walk rates and the park run factors come from the bundle's reference data. | `production_machine_factory`, `warm_worker_cache` |
| `warm_worker_cache` / `_warm_sampler` | Builds the sampler and runs its lazy one-time indexes, so a worker's first game runs at full speed. Never raises. | `batch_runner._prewarm_worker`, `scripts/accuracy/replay.py` |
| `_artifact_dir` | The bundle folder: a `_pool_dir` key, else the version the app pinned (`BASEBALL_ENGINE_ARTIFACT_DIR`), else `BASEBALL_PLAY_POOL_DIR/engine_artifacts`. | `_build_full_pool_sampler` |
| `placeholder_reliever_number` | The number (1-6) of a placeholder pen arm, read from its negative id. | `api/routes/games.py` |
| `reset_caches` | Clears this process's cached sampler and rates; tests call it. | tests |

**Flags:** `SIM_MANAGER` and `SIM_MANAGER_DRAW` (default off; production on), and every sampler flag listed in the sampler sections above.

**Watch out:** Every new sampler setting must be wired here, or it silently stays at its code
default. The cached sampler serves every game of the worker, so the factory calls `set_asof` on
every build, with None when the request has no cutoff.

### `simulation/lineup_resolver.py`

[Source](../../simulation/lineup_resolver.py) · 1021 lines · Tests: [test_lineup_resolver.py](../../tests/unit/test_lineup_resolver.py), [test_starter_resolution.py](../../tests/unit/test_starter_resolution.py), [test_starting_position.py](../../tests/unit/test_starting_position.py), [test_fielder_map.py](../../tests/unit/test_fielder_map.py), [test_incomplete_lineup_api.py](../../tests/unit/test_incomplete_lineup_api.py)

Reads a game's lineups from Postgres (`raw.games`, `raw.game_lineups`, `raw.players`,
`raw.game_player_stats`, `raw.game_bullpen`) and builds the starting `GameState`. The logic is
split from the database reads, so a unit test drives it with canned rows.

| Function / class | What it does | Called from |
|---|---|---|
| `resolve_lineup` | Reads the game, every lineup row, the box score's starters and the players' hands, then assembles them. `fallback_rows` (the what-if's feed rows) stand in when the table holds no row for the game. Last it attaches the real pens. | `resolve_game_state`, `api/routes/games.py` (`_resolve_lineup_best_effort`) |
| `resolve_game_state` | `resolve_lineup` followed by `build_game_state`: a game id to a starting state. | `api/routes/games.py` (`_resolve_state_or_error`), `scripts/sim_stats.py`, `scripts/behaviour_fingerprint.py` |
| `resolve_lineup_from_rows` | The pure assembly: each batting slot's occupant is the highest-sequence row that has taken effect (`as_of_at_bat` rewinds substitutions). The box score's starter wins over the lineup's sequence-1 pitcher row; a recorded pitching change wins over both. | `resolve_lineup` |
| `build_game_state` | A fresh top-of-the-1st `GameState`: the lineups, the leadoff batter, both pitchers, the hands, both defenses and catchers, the managers and the pen. | `resolve_game_state` |
| `build_team_defense_map` / `build_defense_map_for_state` | One team's `{position: player id}` map from its current occupants and its pitcher; the second picks the fielding team of a state's half. | `build_game_state`; `api/routes/games.py` (`_build_replay_artifacts`) |
| `_attach_bullpens` | With `SIM_BULLPEN_SOURCE=box`, the pen from the box score's per-game listing minus the rotation and today's starter (`pipeline.bullpen_usage.fetch_pen_for_game`), with each arm's rest and recent usage. Otherwise the synthetic pen stays. | `resolve_lineup` |
| `LineupNotIngestedError` / `LineupIncompleteError` | A known game with no lineup rows yet, and a known game whose rows do not make a playable game (no pitcher on a side, an empty batting order). The API answers both with 503 and `Retry-After`. | `api/routes/games.py` |

**Flags:** `SIM_BULLPEN_SOURCE` (default `synthetic`; production `box`).

**Watch out:** Catch the two error subclasses before `LineupResolutionError`, or a data gap on a
real game reads as "game not found".

### `simulation/sim_kwargs.py`

[Source](../../simulation/sim_kwargs.py) · 1,183 lines · Tests: [test_sim_kwargs.py](../../tests/unit/test_sim_kwargs.py), [test_park_factor_snapshot.py](../../tests/unit/test_park_factor_snapshot.py), [test_pool_cutoff.py](../../tests/unit/test_pool_cutoff.py), [test_api_realism_wiring.py](../../tests/unit/test_api_realism_wiring.py), [test_manager_profile_snapshot.py](../../tests/unit/test_manager_profile_snapshot.py)

The one builder that turns a resolved `GameState` into sim kwargs, plus the park-factor lookup and
the managers' profiles. One builder serves the API and every script, so the two cannot send
different inputs. A script passes its own read-only connection. The API passes none: the app
reads the park factors and the manager tables into one in-memory snapshot at boot, closes the
file, and every lookup reads that snapshot. The app keeps the `park_factor` names for the
prime, the refresh and the source, because the middleware header and the tests name them; the
prime carries the manager profiles too (SIM-575).

| Function / class | What it does | Called from |
|---|---|---|
| `build_sim_kwargs` | Resolves the park factor, the managers' profiles and the backtest cutoff (the day before the game) onto the state, then calls `sim_kwargs_from_state`. With no park-factor source it builds anyway and logs a warning (`"proceed"`), or refuses (`"raise"`). | `api/routes/games.py` (`_resolved_sim_kwargs`), `scripts/validate_props.py`, `scripts/behaviour_fingerprint.py` |
| `sim_kwargs_from_state` | The kwargs dict: the keys of `SIM_KWARG_KEYS` (lineups, season, hands, pitchers, catchers, defenses, park factor, venue, `max_innings` 12, the managers, the pen, the profiles) plus `_asof_ymd` when the state carries a cutoff. It refuses an unresolved park factor unless the caller allows it. | `build_sim_kwargs`, `scripts/accuracy/replay.py`, `scripts/validate_props.py`, `scripts/sim_stats.py`, the probes |
| `UNRESOLVED_PARK_FACTOR` | A float equal to 1.0 that `isinstance` tells apart from a looked-up 1.0, so "nobody looked" never reads as "neutral park". | `api/routes/games.py`, `resolve_park_run_factor` |
| `resolve_park_run_factor` | The venue from `raw.games`, then its regressed run factor from `derived.park_factors` (through a connection, or the in-memory snapshot). | `resolve_park_factor_onto_state` |
| `prime_park_factor_source` / `refresh_park_factor_snapshot` | Read the run factors (`derived.park_factors`), the manager-seasons (`derived.manager_season_metrics`) and the manager rows of `derived.league_averages` into memory in one open, then close the DuckDB file. The lookup re-reads all three when the file changes, at most once a minute. A missing or unreadable manager table leaves the manager snapshot empty and logs one warning per cause in the process; the park factors stay available. A failed re-read keeps both old snapshots. `ParkFactorSource` counts the kept manager-seasons (`n_manager_profiles`) and league seasons (`n_manager_league_seasons`). | `api/main.py` (the prime); `resolve_park_run_factor` (the refresh) |
| `resolve_manager_profiles_onto_state` | Each side's manager tendencies (`derived.manager_season_metrics`, this season then the last; a row below the minimum sample is skipped, a None column dropped) and the season's league means (`derived.league_averages`). It reads through a connection, or through the primed snapshot when the caller passes None. With nothing primed it leaves both sides `{}`, so the unit lane never reads a file. `build_sim_kwargs` resolves the park factor first, so the manager read sees a snapshot refreshed after a rebuild. | `build_sim_kwargs` |
| `ManagerProfileSnapshot` | The manager half of the boot snapshot: `profiles` maps (manager, season) to that manager-season's tendency columns; `league` maps a season to its league means. An empty snapshot means the prime ran and found no manager rows. | `prime_park_factor_source`, `resolve_manager_profiles_onto_state` |
| `open_sim_duckdb` | Opens the analytics file read-only (`BASEBALL_DUCKDB_PATH`, default `/data/baseball_sim.duckdb`), or returns None. | `production_factory`, `api/bundle_watch.py`, scripts |

**Watch out:** `test_sim_kwargs.py` pins the builder's output to `SIM_KWARG_KEYS`, so a dropped
key fails the unit lane. A new kwarg needs a matching `simulate_game` parameter, because
`simulate_game` takes no `**kwargs`.

### `simulation/start_state.py`

[Source](../../simulation/start_state.py) · 131 lines · Tests: [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py)

Starts a game at a plate appearance of the real game (the game page's what-if). A start state is
plain data: the inning, outs, runners and score; both batting orders and their next slots; each
side's pitcher, starter and defense; the pitch counts and batters faced; the hands; the unused
pen; and the real runs of each completed half. `pipeline.mlb_game_feed.state_at_pa` builds it.

| Function / class | What it does | Called from |
|---|---|---|
| `apply_start_state` | Writes the start state onto a fresh state. | `simulate_game` |
| `apply_start_bullpen` | Sets each side's unused arms, after the game's own pen. | `simulate_game` |
| `start_grid` | The inning grid so far and the score when the current half began, so a game started midway still sums to its final score. | `simulate_game` |

### `simulation/whatif.py`

[Source](../../simulation/whatif.py) · 179 lines · Tests: [test_game_page_whatif.py](../../tests/unit/test_game_page_whatif.py)

Applies the user's managerial moves to a start state: pinch hitters, a new pitcher, pinch runners
and defensive changes. Every entering player must be on the start state's eligible lists, so a
player who left cannot return.

| Function / class | What it does | Called from |
|---|---|---|
| `apply_changes` | Returns a new start state with the moves applied; raises `IllegalChange` with the reason for a move the rules or the roster do not allow. Before the first pitch either side may change its lineup or starter. | `api/routes/games.py` (`start_what_if`) |
| `describe_changes` | One plain sentence per move, for the page. | `api/routes/games.py` |

## Outputs

### `simulation/results.py`

[Source](../../simulation/results.py) · 374 lines · Tests: [test_game_sim_summary.py](../../tests/unit/test_game_sim_summary.py), [test_inning_grid.py](../../tests/unit/test_inning_grid.py), [test_cache_codec.py](../../tests/unit/test_cache_codec.py)

The aggregate of N games of one matchup. It re-exports `GameSimResult`, `BoxScore` and
`PlayerStatLine`, so a consumer imports all four from here.

| Function / class | What it does | Called from |
|---|---|---|
| `GameSimSummary.from_results` | The home, away and tie shares; the score means and medians; the raw per-game score arrays (not a histogram); normal-approximation (Wald) intervals; and each game's inning grid (`inning_grids`, None when any result lacks one). | `BatchRunner.run` / `run_job`, `win_probability` |
| `GameSimSummary.to_json` / `from_json` | The JSON form the Redis cache stores. | `cache_codec` |
| `ConfidenceInterval` | A point estimate with its low and high ends, level and method. | `prop_distributions`, `win_probability` |

### `simulation/prop_distributions.py`

[Source](../../simulation/prop_distributions.py) · 530 lines · Tests: [test_prop_distributions.py](../../tests/unit/test_prop_distributions.py), [test_prop_wrappers.py](../../tests/unit/test_prop_wrappers.py), [test_projections_on_pool.py](../../tests/unit/test_projections_on_pool.py)

One probability mass function (PMF: the probability of each whole-number value) per player and
prop over the N games. Fifteen props: pitchers K, BB, ER, OUTS, H_ALLOWED; batters H, HR, RBI, TB,
1B, 2B, 3B, R, SB, HRR.

| Function / class | What it does | Called from |
|---|---|---|
| `PropDistributionSet.from_boxscores` | Builds every player's PMFs; a game the player missed counts as zero, so the denominator is always N. A player gets the pitcher props if he ever pitched and the batter props if he ever batted or ran. HRR is summed per game, never convolved, because one home run is a hit, a run and an RBI at once. `from_results` reads the box scores off a list of results. | `BatchRunner.run_prop_set` / `run_job`; `from_results`: `scripts/validate_props.py`, `scripts/accuracy/replay.py` |
| `PropDistribution.p_over` / `p_under` / `p_push` | The sportsbook convention: a half-point line cannot push; a whole-number line pushes on an exact match. | `betting/clv_engine.py`, `api/routes/games.py`, `prop_validation` |
| `PITCHER_PROPS` / `BATTER_PROPS` / `ALL_PROPS` | The prop names, core props first; the frontend shows the chips in this order. | `api/routes/games.py` (`_VALID_PROP_NAMES`) |

**Watch out:** A new prop needs a tuple entry (appended, never reordered), an extractor and the
matching odds vocabulary entry in `pipeline/odds_provider.py`.

### `simulation/game_market_distributions.py`

[Source](../../simulation/game_market_distributions.py) · 395 lines · Tests: [test_segment_markets.py](../../tests/unit/test_segment_markets.py), [test_segment_edges.py](../../tests/unit/test_segment_edges.py), [test_game_page_grading.py](../../tests/unit/test_game_page_grading.py)

Prices every game market the book posts from the per-game inning grids: the full-game, first-inning
and first-five moneyline, total and run line; each team's full-game and first-five total; the
first team to score; and a run in the first inning. An unplayed half counts as zero runs.

| Function / class | What it does | Called from |
|---|---|---|
| `SegmentRuns` | Per game: each team's full-game, first-inning and first-five runs, and who scored first. `from_inning_grids` builds it from a summary's grids; `from_official_grid` from one real game. | `segment_runs_from_summary`, `api/routes/betting.py`, `api/routes/betting_pricing.py`, `scripts/accuracy/` |
| `market_probability` | The probability of the market's fixed reference side (home, or over) at a line; a run line also takes the away bet at its own spread. | `scripts/accuracy/grading.py` |
| `market_outcome` | The 0/1 result of the reference side on one real game, or None on a push. A tied segment loses for home on a three-way market. | `api/routes/betting.py`, `scripts/accuracy/grading.py`, `scripts/probes/book_sharpness.py` |
| `total_probabilities` / `side_probabilities` / `cover_probabilities` | (over, under, push), (home, away, draw) and (home covers, away covers, push). | `market_probability`, `api/routes/betting_pricing.py` |
| `segment_runs_from_summary` | The `SegmentRuns` of a summary, or None when it has no grids. | `api/routes/betting_pricing.py` |

**Watch out:** None of these probabilities is calibrated; only the full-game moneyline goes
through the reliability curve.

### `simulation/win_probability.py`

[Source](../../simulation/win_probability.py) · 394 lines · Tests: [test_win_probability.py](../../tests/unit/test_win_probability.py), [test_calibration_report_json.py](../../tests/unit/test_calibration_report_json.py)

The calibrated home and away win probability of a run.

| Function / class | What it does | Called from |
|---|---|---|
| `win_probability` | Folds ties in (half a win each by default), smooths with a Beta prior (Jeffreys, α 0.5, so 0 of N never reads 0.0), applies the calibration map and adds a Wald interval. | `api/routes/betting.py`, `scripts/validate_props.py`, `scripts/accuracy/replay.py` |
| `CalibrationMap.from_report` | A monotone piecewise-linear map from the calibration report's reliability curve; the identity map without one. | `api/startup.py`, `scripts/accuracy/` |

### `simulation/linescore.py`

[Source](../../simulation/linescore.py) · 258 lines · Tests: [test_linescore.py](../../tests/unit/test_linescore.py), [test_inning_grid.py](../../tests/unit/test_inning_grid.py)

The linescore (runs per inning, plus each team's runs, hits and errors) of one recorded game.

| Function / class | What it does | Called from |
|---|---|---|
| `linescore_from_plays` | Credits each play to the half it happened in (a play that ended a half moves back to it), charges errors to the fielding side, and marks an unplayed half as None. | `api/routes/games.py` (`_build_replay_artifacts`) |

### `simulation/pitcher_decisions.py`

[Source](../../simulation/pitcher_decisions.py) · 333 lines · Tests: [test_pitcher_decisions.py](../../tests/unit/test_pitcher_decisions.py), [test_play_pitcher_decisions.py](../../tests/unit/test_play_pitcher_decisions.py)

The winning, losing and save pitchers of one recorded game, by the MLB rules: the pitcher of
record when the lead is taken for good, the five-inning rule for a starter (Rule 9.17(b)) and the
three-run save clause (Rule 9.19).

| Function / class | What it does | Called from |
|---|---|---|
| `decisions_from_plays` | Walks the play stream, reading each play's own pitcher and side first, and returns a `PitcherDecisions`. | `api/routes/games.py` (`_build_replay_artifacts`) |

### `simulation/play_recorder.py`

[Source](../../simulation/play_recorder.py) · 260 lines · Tests: [test_play_recorder.py](../../tests/unit/test_play_recorder.py), [test_replay_store.py](../../tests/unit/test_replay_store.py)

Keeps the ordered `PlayResult` stream of one game without changing the loop. A wrapper
(`RecordingMachine`) sits around the factory's machine and keeps each result and the state before
it.

| Function / class | What it does | Called from |
|---|---|---|
| `record_game` / `record_game_plays` | Builds the machine through a dotted factory (the no-database factory by default), wraps it, plays one game and returns a `RecordedGame` (the result, the plays, the state before each play); `record_game_plays` returns the first two. | `record_spec`, `scripts/behaviour_fingerprint.py`; `record_game_plays`: `scripts/validate_props.py`, `scripts/accuracy/replay.py` |
| `record_spec` | `record_game` for a `GameSpec`: the worker pool's unit of work. | `BatchRunner.record_game` |
| `RecordingMachine.step_pitch` | Calls the inner machine, deep-copies the result's next state and keeps it. | `simulate_game` |

**Watch out:** `step_pitch` returns the live state as `next_state`; without the deep copy every
recorded play points at the final state.

### `simulation/snapshots.py`

[Source](../../simulation/snapshots.py) · 524 lines · Tests: [test_play_snapshots.py](../../tests/unit/test_play_snapshots.py), [test_api_games_plays.py](../../tests/unit/test_api_games_plays.py), [test_no_pitch_readers.py](../../tests/unit/test_no_pitch_readers.py)

The data shapes of the game page's replay and their pure builders.

| Function / class | What it does | Called from |
|---|---|---|
| `PlayByPlay.from_play_results` | One entry per pitch, grouped into plate appearances; a no-pitch result makes no entry. | `api/routes/games.py` (`_build_replay_artifacts`) |
| `FieldSnapshot.from_game_state` / `StateAtPitch` | The field graphic: the nine fielders, the batter, the runners, the count, the outs and the score. | `api/routes/games.py` |
| `PitchContext` | The state before one pitch (inning, half, outs, batter). | `play_recorder` |
| `OverrideDelta.from_summaries` | A baseline run against an override run. | `api/routes/games.py` (`simulate_with_override_endpoint`) |
| `thrown_pitches` | The results that hold a pitch, one per play-by-play entry. | `api/routes/games.py` |

### `simulation/cache_codec.py`

[Source](../../simulation/cache_codec.py) · 72 lines · Tests: [test_cache_codec.py](../../tests/unit/test_cache_codec.py)

The JSON codec of the Redis sim cache. Every value is an envelope `{"t": tag, "v": payload}` with
the tag `summary`, `prop_set` or `json`. A pickle load runs code, so the cache never stores one
(the security hardening, SIM-443f).

| Function / class | What it does | Called from |
|---|---|---|
| `encode` | Writes the envelope; raises `TypeError` on a value JSON cannot hold, and the cache skips the write. | `batch_runner.RedisCache.set` |
| `decode` | Reads it back; any text it cannot read is a cache miss. | `batch_runner.RedisCache.get` |

## Support

### `simulation/synthetic_bundle.py`

[Source](../../simulation/synthetic_bundle.py) · 688 lines · Tests: [test_synthetic_bundle.py](../../tests/unit/test_synthetic_bundle.py), [test_baseball_sniff.py](../../tests/unit/test_baseball_sniff.py), [test_simulate_game_driver.py](../../tests/unit/test_simulate_game_driver.py)

A small in-memory bundle in the exact shape the production loader produces: the pitch pools, a
batted-ball pool with transitions, the advancement pools, the steal pools and the change pool. The
tests and the no-database factory run the production sampler and loop over synthetic rows.

| Function / class | What it does | Called from |
|---|---|---|
| `league_artifacts` | The league-average bundle: a realistic pitch mix in every count, a league in-play mix in every base-out cell, the advancement pools and the change pool. | `batch_runner.rng_driven_machine_factory`, `validation.replay_chi_squared`, tests |
| `fixed_play_artifacts` | One event in every base-out cell, for a deterministic play through the production in-play path. | tests |
| `synthetic_artifacts` | The general builder the two above wrap. | tests |
| `steal_pools` / `change_pool` | Synthetic steal pools (by pitch class, with optional pickoff rows) and a change pool, so a test runs the real running-game and pitching-change draws. | `synthetic_artifacts`, tests |
| `synthetic_sampler` | A `FullPoolSampler` over a synthetic bundle. | `validation.replay_chi_squared`, tests |

**Watch out:** A failure through this bundle is a production code-path bug: the rows are fake, the
code is the real one.

### `simulation/prop_validation.py`

[Source](../../simulation/prop_validation.py) · 818 lines · Tests: [test_validate_props.py](../../tests/unit/test_validate_props.py), [test_boxscore_reader.py](../../tests/unit/test_boxscore_reader.py)

Checks the simulator's probabilities against real outcomes and fits the win-probability
reliability curve (the map from each predicted probability to the rate at which the event really
happened).

| Function / class | What it does | Called from |
|---|---|---|
| `binary_reliability_curve` / `binary_ece` / `binary_brier` / `binary_log_loss` | The metrics of a yes/no forecast: the expected calibration error (ECE, the average gap between forecast and outcome rate), the Brier score and the log-loss. | `scripts/accuracy/report.py`, `scripts/accuracy_comparison.py`, `validate_prop_over_under` |
| `fit_reliability_curve` | Bins the predicted home-win probabilities and returns the `[[predicted, observed], ...]` anchors the calibration map reads. | `build_validation_report` |
| `real_props_from_boxscore_rows` | Real prop totals from the official box score (`raw.game_player_stats`), for every priced market. A player who did not play is absent. | `scripts/validate_props.py`, `scripts/accuracy/grading.py`, `scripts/boxscore_audit.py` |
| `real_props_from_pa_events` | Real H, HR, TB, K and BB from the `raw.pitches` event labels: the fallback for a game with no box score. | `scripts/validate_props.py`, `scripts/accuracy/grading.py` |
| `pair_props_for_validation` / `validate_prop_over_under` / `pit_values` / `pmf_coverage` | Pair the PMFs with the real totals and score them. | `build_validation_report` |
| `write_reliability_curve_to_calibration_report` | Writes the fitted curve into the calibration report file the API loads at boot. | `scripts/validate_props.py` |

### `simulation/validation/replay_chi_squared.py`

[Source](../../simulation/validation/replay_chi_squared.py) · 489 lines · Tests: [test_replay_chi_squared.py](../../tests/unit/test_replay_chi_squared.py)

The historical-replay harness: it replays games through `simulate_game` and compares the simulated
runs per team-game with a real or reference distribution by a chi-squared goodness-of-fit test.
It pools the sparse high-run tail until every expected count is large enough; the gate is
p > 0.05.

| Function / class | What it does | Called from |
|---|---|---|
| `replay_and_test` | Replays `HistoricalGame` rows and tests them against their real runs. | tests |
| `chi_squared_gof` | Bins, pools and runs `scipy.stats.chisquare`; returns a `ChiSquaredResult`. | `replay_and_test`, tests |
| `simulate_run_distribution` / `replay_historical_games` | One game per row at a fixed seed, through an injectable machine factory (default: the production machine over the synthetic league bundle). | `replay_and_test`, tests |

### `simulation/validation/__init__.py`

[Source](../../simulation/validation/__init__.py) · 36 lines · Tests: [test_replay_chi_squared.py](../../tests/unit/test_replay_chi_squared.py)

Re-exports the harness's public names.

### `simulation/__init__.py`

[Source](../../simulation/__init__.py) · 8 lines · Tests: none

The package docstring.
