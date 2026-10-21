# The simulation loop on one page

Every draw the simulator makes, in the order it makes them: the pool it draws from, the hard
filter, each weight and its value, and the similarity scores behind the weights. The values are
production's: the `app` service of [`docker-compose.yml`](../../docker-compose.yml). Where a code
default in [`simulation/flags.py`](../../simulation/flags.py) differs, the table says so. The
module-by-module reference is [Simulation](simulation.md).

## How to read this

- **A draw.** The simulator never decides by formula. It takes a **pool** of real plays from
  the engine-artifact bundle (the folder the nightly job builds), keeps only the rows that fit
  the live situation (the **hard filter**), gives each row a **weight**, and picks one row at
  random in proportion to the weights. **The drawn row is the play** (the owner's rulings of
  2026-08-10 and 2026-08-29).
- **The pool window.** The pools hold the last three completed seasons plus the current one
  (2023–2026 today; `RECENCY_FLOOR_SEASONS = 4` in
  [`pipeline/batch/artifacts/common.py`](../../pipeline/batch/artifacts/common.py)).
- **Recency.** Every row carries a recency weight: 2.0 for the two newest seasons, then ×0.75
  per older season, never below 0.25
  ([`pipeline/batch/profiles/pool_meta.py`](../../pipeline/batch/profiles/pool_meta.py)).
- **A similarity score** is a number from 0 (nothing alike) to 1 (the same player), read from
  a nightly matrix (§5). The draw raises it to a **power**: a higher power prefers look-alikes
  more strongly; 0 turns the factor off. **Every power is 1** (owner ruling 2026-09-16) until
  one designed experiment fits all the weights together. At power 1 the sampler skips the power
  step. §3 keeps the fitted values the experiment starts from.
- **A bandwidth** belongs to a Gaussian weight on a distance: on a single feature, a row one
  bandwidth from the live value keeps about 60% of its weight. A smaller bandwidth is stricter.
- **The backtest cutoff.** A backtest sets a cutoff at the day before the game, and every draw
  gives zero weight to later plays (`FullPoolSampler.set_asof`).
- **A row with no score.** A pool row whose player has no profile reads as neutral (1.0 at
  power 1; the mean scored weight at any other power).

## 1. The loop

```
plate appearance starts ─► ① pitching change?   (the fielding manager; first pitch of the PA)
                           ② which reliever?     (only when ① drew a change)
                           ③ intentional walk?   (no pitch thrown when it fires)
each pitch ──────────────► ④a pickoff?           (BEFORE the pitch; lead runner on 1B with 2B open,
                                                   or on 2B with 3B open)
   pickoff = third out ──► the half ends with NO pitch; the same batter leads off next inning
                           ⑤a the pitch thrown
                           ⑤b the pitch's result  (ball, called strike, swinging strike, foul,
                                                   in play, hit by pitch)
                           ④b steal?             (AFTER the result; skipped when ④a resolved)
   in play ──────────────► ⑥ the fence stage, then the fielding draw (the play and every runner)
                           ⑦ the extra bases     (up to five runner decisions)
   pitch got away ───────► ⑧ runners move up one base; a dropped third strike may put the batter on
```

The order ④a → ⑤a → ⑤b → ④b is the running game on the pitch (SIM-554). The batter's result is
drawn with no knowledge of the steal, and the steal never changes it. It needs a bundle whose
steal pool carries each pitch's class; an older bundle, or `SIM_STEAL_PITCH_CLASS=0`, runs one
draw before the pitch that answers the steal and the pickoff together.

## 2. The draws, one by one

### ① The pitching change — `pitching_change_draw`

Runs on the first pitch of every plate appearance when `SIM_MANAGER=1` and `SIM_MANAGER_DRAW=1`
(both default 0; production 1). With the draw off, no pitcher is ever changed.

| | |
|---|---|
| pool | the change opportunity pool: one row per real plate-appearance boundary with a pitcher on the mound, changed or not |
| hard filter | starter or reliever · a half-inning boundary or mid-inning · the pitch-count bucket (tens) · times through the order (1–4). Under 20 rows (`SIM_CHANGE_MIN_CELL`) the cell widens: drop the times through, then the bucket, then the boundary |
| weights | recency · a Gaussian on the z-scored situation (pitch count, batters faced, inning, outs, runners, the fielding side's margin), bandwidth **1.0** (`SIM_CHANGE_SIT_SIGMA`) · the live pitcher's **pitcher similarity** to the row's pitcher, power **1** (`SIM_CHANGE_PITCHER_POWER`) · the fielding manager's **manager-usage similarity** to the row's manager, power **1** (`SIM_ACTOR_POWER_MANAGER_USAGE`; code default 0 = off) |
| the row answers | did the manager change pitchers here |

### ② Which reliever — `relief_arm_draw`

| | |
|---|---|
| candidates | the real pen (`SIM_BULLPEN_SOURCE=box`; code default `synthetic`): the arms the MLB box score lists for the game, minus the rotation (anyone who started one of the team's last five games with 45 or more pitches) and today's starter, minus the arms already used |
| weights | each a resemblance to the arm the drawn change row brought in, rescaled to a mean of 1: **role** (a Gaussian on the difference in high-leverage entry share, bandwidth **0.1**) · **stuff** (pitcher similarity, power **1**) · **rest** (a Gaussian on days of rest capped at five, bandwidth **0.5**) · **pitched in the last two days** (a mismatch × **0.25**) · **pitches over the last three days** (a Gaussian, bandwidth **10**) · **hand** (off, weight 1.0) |
| code defaults | every reliever weight is off in code (`SIM_RELIEF_*`); with all off, the pick is positional: the first listed arm in a late high-leverage spot, else the last |

### ③ The intentional walk

On a plate appearance's first pitch, with a manager wired: one random number against the **real
rate** of the plate appearance's cell — runners × outs × seventh inning or later × within one run
— from `sim.ibb_rates` (carried in the bundle's reference data). No similarity weight.

### ④a The pickoff — `pickoff_draw` (before the pitch)

| | |
|---|---|
| pool | the steal opportunity pool: one row per real pitch with a stealable runner, attempted or not, plus the **pickoff rows** (real pickoffs thrown before any pitch of their plate appearance) |
| hard filter | the target base (second or third) and the exact outs-balls-strikes count. Every row of that count group is a candidate, pitch rows and pickoff rows alike. A group with no pickoff outcome skips the draw |
| weights | the steal draw's own (④b) **without** the manager's aggression |
| the row answers | picked off (an out; a caught stealing when the runner was breaking) · an errant throw (the runner moves up one base) · neither |
| resolves | at once, before the pitch. A pickoff that makes the third out ends the half with no pitch thrown: the result reads `pitch_outcome = "no_pitch"` and `pa_voided = "pickoff_third_out"` |

The pickoff rows sit at a 0-0 count, so the simulator's pickoffs happen mostly at 0-0.

### ⑤a The pitch thrown — `draw`

| | |
|---|---|
| pool | every pitch of the window, split by the batter's hand |
| hard filter | the **cell**: runners (8) × outs (3) × count (12) × score band (≤−3, −2 to −1, 0, +1 to +2, ≥+3) × batting side (2) = 2,880 cells (`SIM_PITCH_CELL_INDEX`, code default 0, production 1). A sub-cell under 20 rows (`SIM_PITCH_MIN_CELL`) widens: the score bands, then the sides, then the counts. Off, the draw weighs the whole pool and conditions on the count alone |
| weights | recency · the live pitcher's **pitcher similarity**, power **1** (`SIM_PITCH_PITCHER_POWER`) · the live batter's **batter similarity**, power **1** (`SIM_PITCH_BATTER_POWER`) · a Gaussian on the base-out columns (outs, runners, inning, margin), bandwidth **2.0** (`SIM_SIT_SIGMA`) · **fatigue**: a Gaussian on the gap between the live pitcher's times through the order and the row's, bandwidth **0.5** (`SIM_FATIGUE_TTO_SIGMA`, code default 0), normalized within each count so it changes which pitch is thrown, not how often |
| the row answers | the pitch thrown (with the split off, also its result) |

### ⑤b The pitch's result — `_result_draw`

Production splits the pitch into two draws (`SIM_PITCH_RESULT_SPLIT`, code default 0, production
1). The result draw runs over the same cell rows as ⑤a.

| | |
|---|---|
| weights | ⑤a's weight before its batter power · a Gaussian on the **pitch-to-pitch distance** from the pitch just thrown (velocity, breaks, spin, release, plate location; z-scored and weighted as the pitch engine weights them), bandwidth **1.0** (`SIM_RESULT_PITCH_SIGMA`) · an inverse-density correction so crowded pitch regions are not over-drawn, power **1.0** (`SIM_RESULT_DENSITY_POWER`) · the pitcher factor re-raised to power **1** (`SIM_RESULT_PITCHER_POWER`) · the batter factor at power **1** (`SIM_RESULT_BATTER_POWER`) · the catcher receiving ratio (off) |
| the row answers | the outcome class; whether the pitch got away (⑧); and, for a ball in play, the **born batted ball** — its exit velocity, launch angle, spray, distance, class and park — which ⑥ fields |
| the class | the pool build reads it from the pitch code. A foul tip or a foul bunt with two strikes is strike three (the foul-tip coding, SIM-553) |

### ④b The steal — `steal_draw` (after the result)

| | |
|---|---|
| pool | the steal opportunity pool's pitch rows (the pickoff rows are never candidates) |
| hard filter | the target base · the exact outs-balls-strikes count · the **class of the pitch ⑤b drew**. A class group answers from its own rows however few; an empty group stages no steal |
| weights | recency · a Gaussian on the score margin, bandwidth **2 runs** (a sampler constant) · the live runner's **steal-runner similarity**, power **1** · the live pitcher's **pitcher-against-the-run similarity**, power **1** · the fielding catcher's **catcher-throwing similarity**, power **1** · the batting manager's **aggression** on attempted rows only: his steal-order rate over the league mean, clamped to 0.05–4 (a weight, never a gate) |
| the row answers | went or stayed; safe or caught |
| two outs | a third strike the catcher holds is the third out first, so a staged steal is void (`steal_voided = "third_out_first"`) |

### ⑥ The batted ball — the fence stage, then `battedball_draw`

| | |
|---|---|
| pool | one row per real ball in play, with its whole base-out transition (each runner's and the batter's destination) |
| hard filters, in order | the exact base-out cell (always; the row must be legal here) · the born ball's **class** (ground ball, line drive, fly ball, popup, bunt; `SIM_BB_CLASS_FILTER`, production 1) · the **fence stage** (`SIM_FENCE_STAGE`, production 1): an air ball's carry against the live park's fence at its direction keeps the home-run rows (over) or drops them (short); a ground ball, popup or bunt is always short; the band is 0 feet (`SIM_FENCE_MARGIN`) · the **wall-margin band** (`SIM_BB_MARGIN_BAND`, production **15 ft**): a line drive or fly ball of 300+ feet the fence did not call over keeps only rows that landed as near their own park's fence; fewer than 20 survivors leave the rows unchanged |
| the carry | the born ball's distance plus the live park's carry offset minus the offset of the park it was hit in (`SIM_CARRY_OFFSET`, production 1); the offsets and the fence lines (ten-degree sectors from −55° to +55°, one line per park and season group) live in `park_geometry.json` in the bundle |
| weights | recency · the live batter's **batter similarity**, power **1** (`SIM_BB_BATTER_POWER`) · a Gaussian on the soft situation (balls, strikes, inning, margin), bandwidth **2.0** · the **platoon**: rows whose pitcher threw with the other hand × **0.6** (on with `SIM_BB_PLATOON=1`; the 0.6 is a constructor default) · the **batting side**: rows of the other side × **0.0** (`SIM_HOME_OFF_WEIGHT`, code default 1.0), so home-field advantage comes from the pool's own home and away rows · the live defender at the row's position: **fielder similarity**, power **1**, rescaled to a mean of 1 within each position · the **park**: a Gaussian on the run factor, bandwidth **0.02** (`SIM_PARK_KERNEL_SIGMA`), only for a wall-zone ball (`SIM_PARK_WALL_ZONE_ONLY=1`) · the **born ball**: a Gaussian on exit velocity, launch angle, spray and distance, z-scored on the born ball's own class, bandwidth **1.0** per feature (`SIM_BB_BORN_SIGMA`, `SIM_BB_BORN_PER_FEATURE`), times an inverse-density correction (power 1.0) |
| the row answers | the event and where every runner ends up |

### ⑦ The extra bases — `advancement_draw`

Five decisions belong to the runners, so the fielding row does not decide them: first to third on
a single, second to home on a single, first to home on a double, the tag-ups on a caught fly
ball, and the batter's stretch for an extra base. The lead runner goes first; a trailing runner
cannot pass him.

| | |
|---|---|
| pool | one pool per decision (scenario, from base, to base), one row per real opportunity, attempted or not |
| weights | recency · a Gaussian on the z-scored throw geometry (exit velocity, launch angle, spray, distance, outs), bandwidth **1.0** (a sampler constant) · the live runner's **advancement-runner similarity**, power **1** · the live fielder's **fielder similarity** at his position, power **1** |
| the row answers | went or held; safe or out; an extra base on a bad throw. A tag from third that scores makes the play a sacrifice fly |

### ⑧ Got-away and the dropped third strike

`SIM_GOT_AWAY` (code default 0, production 1). The result row's own fact: if that real pitch got
away (a wild pitch, a passed ball or an uncaught third strike), every runner moves up one base and
a runner on third scores with no RBI. No weight; the row carries it.

- **One mover per pitch.** When a steal or a pickoff moved runners on the pitch, the got-away
  moves nobody.
- **The dropped third strike.** On a swinging strike three that got away, with first base open
  or two outs (read as they stood at the pitch), the batter reaches first. The runners advance
  first, then the batter takes first. The play still counts as a strikeout for the pitcher (`k`)
  and the batter (`so`), and a run that scores on it pays no RBI.

## 3. Production values at a glance

The fitted values are the starting points of the designed experiment; they come from the
comments in `docker-compose.yml`.

| Factor | Draw | Production | Code default | Fitted value |
|---|---|---|---|---|
| pitcher similarity | ⑤a / ⑤b | power 1 / 1 | 1 / 1 | 16 / 16 |
| batter similarity | ⑤a / ⑤b / ⑥ | power 1 / 1 / 1 | 1 / 1 / 1 | 1 / 8 / 4 |
| fielder similarity | ⑥, ⑦ | power 1 | 1 | 1.2 |
| steal-runner · pitcher-against-the-run · catcher-throwing | ④a, ④b | power 1 · 1 · 1 | 1 · 1 · 1 | 12 · 12 · 2 |
| advancement-runner | ⑦ | power 1 | 1 | 20 |
| manager-usage | ① | power 1 | 0 (off) | 4 |
| reliever stuff | ② | power 1 | 0 (off) | off |
| reliever role · rest · two-day · three-day | ② | 0.1 · 0.5 · 0.25 · 10 | all off | as production |
| Gaussian bandwidths: situation · change situation · score margin · throw geometry | ⑤a and ⑥ · ① · ④ · ⑦ | 2.0 · 1.0 · 2 runs · 1.0 | the same | — |
| fatigue (times through the order) | ⑤a | 0.5 (owner decision) | 0 (off) | 0.7 ([the fatigue fit](../audit/2026-09-12-sim518-fit-plan.md)) |
| pitch-to-pitch Gaussian · density power | ⑤b | 1.0 · 1.0 | the same | — |
| born-ball Gaussian | ⑥ | 1.0, per feature | 0 (off) | — |
| park Gaussian | ⑥, wall zone only | 0.02 | 0 (off) | — |
| platoon | ⑥ | opposite hand × 0.6 | off (`SIM_BB_PLATOON=0`) | — |
| batting side | ⑥ | × 0.0 (hard) | 1.0 (off) | — |
| wall-margin band | ⑥ | 15 ft, at least 20 rows | 0 (off) | — |
| cell minimum | ⑤a / ① / ④b | 20 / 20 / none | the same | — |
| recency | every pool | 2.0, ×0.75 a season, floor 0.25 | the same | — |

## 4. Built and OFF

| Factor | Switch | Note |
|---|---|---|
| catcher receiving ratio (⑤) | `SIM_CATCHER_RECEIVING=0` | moves weight between taken balls and called strikes by the catcher's framing; the enable is open work (the catcher receiving enable, SIM-526) |
| fatigue's pitch-count term (⑤a) | `SIM_FATIGUE_PC_SIGMA=0` | |
| pitch similarity on the fielding draw (⑥) | `SIM_BB_PITCH_SIGMA=0` | the born batted ball carries the pitch's effect instead |
| batting-side weight on the pitch draw (⑤a) | `SIM_PITCH_HOME_OFF_WEIGHT=1.0` | the cell already filters the side |
| reliever hand (②) | `SIM_RELIEF_HAND_OFF_WEIGHT=1.0` | |

## 5. The similarity scores

Each engine in [`similarity/engines/`](../../similarity/engines/) turns two players' season
profiles into one score from 0 to 1. A score is built from **sub-scores**: each is an RBF kernel
(a similarity that falls off as a bell curve with distance) on the standardized gap over one
feature group, with its own bandwidth σ; fixed weights combine them. A thin profile is first
shrunk toward the league average: the shrinkage prior is the sample size at which a player keeps
half of his own number.

The nightly rebuild (`python -m pipeline.batch.engine_artifacts --what all`) scores every pair of
profiles into the bundle's matrices, with the weights and bandwidths written in the engine files.
The app's calibration report (`/data/calibration.json`) recalibrates the API's own engines; it
does not reach these matrices.

### Pitcher — `pitcher_similarity.py` (matrix `pitcher_sim_matrix`; feeds ⑤a, ⑤b, ① and ②)

- **Arsenal, weight 0.65.** A Gaussian-mixture fingerprint of every pitch he throws over eight
  dimensions: velocity, induced vertical break, horizontal break, spin rate, spin axis, release
  x, release z, extension. Two arsenals compare by the Wasserstein-2 distance W₂ (the work it
  takes to move one pitch distribution onto the other), scored as exp(−W₂ / 4.10), so the median
  pair scores 0.50.
- **Command, weight 0.35** (σ 1.0453): walk rate, strikeout rate, called-plus-whiff rate, zone
  take rate, chase rate, zone rate, whiff rate.
- Shrinkage prior: 5 pitches. Pitchers compare within the same throwing hand.
- **Not features, by owner decision (the arm-angle and spin-shape study, SIM-533):** Savant's arm
  angle, active spin and spin-axis deviation. The arsenal already reads the release point, and
  the spin numbers follow from the other dimensions; none adds more than 0.009 R² on the outcomes
  the score does not read. `tests/unit/test_arm_angle_decision.py` guards the decision.

### Batter — `batter_similarity.py` (matrix `batter`; feeds ⑤a, ⑤b and ⑥)

- **Discipline 0.32** (σ 1.0389): first-pitch take rate, chase rate, zone swing rate, contact
  rate, whiff rate, strikeout rate, walk rate.
- **Batted ball 0.28** (σ 1.0828): ground-ball rate, fly-ball rate, pull rate, opposite-field
  rate, average exit velocity, average launch angle, hard-hit rate, barrel rate.
- **Platoon 0.12** (σ 1.0866): chase, zone swing, whiff, walk, strikeout, ground-ball and barrel
  rates against each pitcher hand.
- **Power 0.08** (σ 0.9591): home-run rate, expected average, expected slugging, maximum exit
  velocity. The profile computor writes no expected average or slugging, so both read 0.0 for
  every batter.
- **Physical 0.20** (σ 1.0; the batter's physical swing features, SIM-529): swing tilt, bat
  speed, stance depth, swing length, distance off the plate, foot separation, stance angle,
  contact depth, attack angle, attack direction.
- The matrix scores each pair without a pitcher hand, so it uses the weights above. A
  population with no physical data falls back to 0.40 / 0.35 / 0.15 / 0.10 over the other four.
- A batter of the other side scores × 0.92; a switch hitter against a one-sided batter × 0.97.
  Shrinkage prior: 5 plate appearances.

### Fielder — `fielder_similarity.py` (matrices `fielder_1B` … `fielder_RF`; feeds ⑥ and ⑦)

Scored within one position only; pitchers and catchers have no fielder matrix. Shrinkage prior:
15 batted balls.

- **Infield:** range 0.45 (σ 1.0549: outs above average to the glove side, to the arm side,
  charging and going back; catch percentage added; Savant's outs above average per 100 chances)
  · double play 0.30 (σ 0.372: double plays above expected, attempt rate, success rate; the
  pivot for second basemen and shortstops) · errors 0.15 (fielding, throwing) · specialty 0.10
  (bunt fielding; the first baseman's scoop).
- **Outfield:** range 0.40 (σ 1.0614: the same five components, Savant's outs above average,
  and the outfield jump — reaction, burst, route) · arm 0.30 (σ 0.9912: throw velocity, the
  runner advances he prevents, the runners he throws out) · star catches 0.15 (σ 0.449: five-star,
  four-star and routine catch rates) · errors 0.15.
- Inside the range and arm groups, each feature's weight is its fitted year-to-year repeat
  within a position; `make calibrate` refits them and the engine file holds the copy.

### Catcher — `catcher_similarity.py` (matrix `catcher_throwing`; feeds ④a and ④b)

- The draws read the **throwing** sub-score alone (σ 0.98): pop time, caught-stealing rate, arm
  strength.
- The full score (framing 0.53, blocking 0.24, throwing 0.14, deterrence 0.09) goes into the
  `catcher` matrix, which no draw reads. Shrinkage prior: 15 called pitches.

### Steal runner — `baserunner_steal_similarity.py` (matrix `runner_steal`; feeds ④a and ④b)

- **Tendency 0.45** (σ 1.05): steal-attempt rate from first, from second.
- **Lead 0.45** (σ 0.9836): the lead off the bag before the pitch and the jump on the delivery,
  in feet, from Savant's basestealing board. A runner with no Savant row is scored on tendency
  and success alone.
- **Success 0.10** (σ 1.02): success rate from first, from second.
- Every runner who had a chance has a profile, including those who never ran (the matrix scores
  thin profiles too). Shrinkage priors: 50 chances (tendency, lead), 20 attempts (success).

### Pitcher against the run — `pitcher_steal_similarity.py` (matrix `pitcher_steal`; feeds ④a and ④b)

- **Outcome 0.35** (σ 1.10): stolen bases allowed per nine, caught-stealing rate when runners
  go, attempt rate allowed.
- **Hold 0.65** (σ 0.9897): the lead and the jump he allows, in feet, from Savant's pitcher
  running-game board. A pitcher with no Savant row is scored on the outcome group alone.
- Shrinkage prior: 25 baserunner events.

### Advancement runner — `baserunner_similarity.py` (matrix `runner_adv`; feeds ⑦)

- **Speed 0.35** (σ 0.8171): sprint speed.
- **Aggression 0.40** (σ 1.0478): extra-base attempt rates (overall, first to third, second to
  home, first to home, tag-up), the stop rate, and how often he tried above a typical runner in
  the same chances (from Savant's baserunning board; missing when he has no Savant row, and then
  left out of the distance).
- **Success 0.25** (σ 1.0): the matching success rates.
- The matrix scores thin profiles too. Shrinkage prior: 15 extra-base chances.

### Manager — `manager_similarity.py` (matrix `manager_usage`; feeds ①)

- The draw reads the **usage** sub-score alone (σ 1.0): the starter's average pitch count, the
  share of starts ended before 100 pitches, the closer's entry leverage, the high-leverage
  reliever share, opener rate, bulk-innings rate, the share of available relievers used.
- The aggression and platoon groups (weights 0.35 and 0.25 of the full score) feed no draw. The
  steal draw's aggression (④b) reads `steal_order_rate_per_1b_opp` from the manager's profile
  directly. Shrinkage prior: 30 games managed.

### Pitch-to-pitch — `pitch_pitch_similarity.py` (feeds ⑤b)

The sampler copies the engine's feature weights (`_PITCH_FEATURE_WEIGHTS` in
[`simulation/sampler/kernels.py`](../../simulation/sampler/kernels.py); a unit test pins the two
equal): velocity 1.2, induced vertical break 1.1, horizontal break 1.0, spin rate 0.7, spin axis
0.5, release x and z 0.6, extension 0.4, plate x and z 0.9.

### Engines that feed no draw

- **Situation** (`situation_similarity.py`): a nearest-neighbour index over the inning, the
  half, outs, the three bases, the margin, leverage, pitch count, times through and the park.
  The draws use their own hard cells and situation Gaussians instead.
- **Batted ball** (`batted_ball_similarity.py`): exit velocity 1.3, launch angle 1.1, spray 0.8.
  The fielding draw's born-ball Gaussian (⑥) measures the same quantities, plus distance, with
  equal weights.

## 6. Where the values live

- **Production:** the `app` service's `SIM_*` lines in `docker-compose.yml`, each with the reason
  in a comment.
- **Code defaults and meanings:** `simulation/flags.py`.
- **The unit lane:** `tests/conftest.py` pins nearly every factor off. It leaves
  `SIM_STEAL_PITCH_CLASS` unpinned: the samplers the unit tests build run the running game on
  the pitch.
- **The acceptance lane:** `PRODUCTION_FLAGS` in `tests/acceptance/conftest.py`. Tests in
  `tests/acceptance/test_band_arithmetic.py` and `tests/unit/test_fence_carry_and_margin.py`
  check that the fatigue, split, manager, running-game and fence values match the compose file.
- **The similarity weights and bandwidths:** the engine files in `similarity/engines/`.
- **How the values were fitted:** [the kernel fit](../audit/2026-08-28-sim476-fit-plan.md),
  [the play-picker redesign](../audit/2026-09-08-sim523-play-picker-redesign-plan.md),
  [the manager and pen build](../audit/2026-09-13-sim427-build-plan.md),
  [the fatigue fit](../audit/2026-09-12-sim518-fit-plan.md) and
  [the joint accuracy fit](../audit/2026-09-14-sim548-joint-fit-plan.md).
