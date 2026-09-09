# The play-picker redesign (SIM-523): one loop, engine scores, fitted powers — the plan (2026-09-08)

**Owner summary.** The simulator picks every play by drawing one real play from a
pool, weighted by how much the live players and situation resemble the play's.
This week's tests showed the catcher weight dominates that draw and reads the
catcher's pitching staff rather than his skill. The owner's rulings of 2026-09-08
replace the three repair options on the identity-kernel ticket with one redesign:
the loop below, every weight an engine's 0-to-1 score, each raised to a fitted
power in a fixed order (pitcher first, catcher receiving last), the catcher
receiving factor rebuilt as a small ball-strike ratio that ships OFF, and one
new check that no factor may concentrate the draw on a player's own team.

Everything here follows the two standing rules: every decision is a
similarity-weighted draw from a hard-filtered pool, never a formula; and the
drawn row is the play, with no adjustment after the draw.

---

## 1. The rulings this plan implements (owner, 2026-09-08)

1. **Ordering.** In the pitch and pitch-result draws the pitcher's weight is the
   largest and the catcher receiving weight the smallest; batter, then recency,
   sit between. The powers are fitted empirically under that constraint.
2. **Scores come from the engines.** Every actor factor is that engine's
   composite 0-to-1 score (the pitcher factor already is). The sampler stops
   building its own bell-curve kernels from raw profile numbers.
3. **The loop** in section 3 is the loop: manager decisions at each new plate
   appearance, then per pitch the steal draw, the pitch draw, the pitch-result
   draw; on a ball in play the park geometry check, the fielding draw, the runner
   advancement draws.
4. **The batted ball is born in the pitch-result draw** (the batter draw) and
   every later stage consumes it.
5. **Catcher receiving** is a ball-strike ratio on taken pitches, mass-preserving
   inside the taken group. It ships OFF. Fitting and enabling it is its own
   ticket (SIM-526). The owner's reason, recorded verbatim in spirit: it is one
   mechanic that does not fit the design as cleanly, because it enters the batter
   result but only when the batter does not make contact.
6. **Batter hand is a hard filter on the fielding draw** (defenses position by
   it), and the batter's batted-ball profile is a fielding-draw weight for the
   same reason, with sprint speed.
7. **The fence work is unparked** (the park-geometry table, the trajectory model,
   the fence-resolution stage — SIM-478/479/480) as the park geometry step.
8. **No betting-value measurement until every graded statistic is green**
   (ruling of the same day, recorded in CLAUDE.md §2b).

---

## 2. The evidence (all measured 2026-09-08, scripts in `scripts/sim523_*.py`)

| Test | Result |
|---|---|
| Factor strength (710 plate appearances, production config) | pitcher factor keeps 91% of the pool in play; batter 99.96%; situation 77%; recency 95%; **catcher receiving 2.5%**, zeroing 78% of rows and putting 62% of its weight on the nearest 1%. All four without the catcher: 66%. All five: 1.7%. |
| Test 4, weight on the live catcher's own rows | kernel off 0.56% (pool share 0.55%); **kernel on 8.87%**, and 10.9% on his staff's rows |
| Test 2, swap the catcher for a twin with the same skill numbers on another staff (4 games × 60 per arm) | walks 6.92 → 6.83, hit-by-pitch draws 0.879 → 0.854 per game, called-strike share 0.311 → 0.313 — moves, at low power |
| Test 1, hold the pitcher and swap the catcher (295 real pairs) | no catcher effect above sampling noise on walks, strikeouts, hit-by-pitch or called strikes at this resolution |
| Test 3, two-way decomposition | inconclusive: catchers and pitchers are nested in teams, so the split is weakly identified; do not cite its numbers |
| The got-away rate as a staff measurement (309 catcher-seasons) | correlates with the staff's walks per PA (+0.18) and hit-by-pitch (+0.08); the wildest quarter of catcher-seasons has 6-7% more walks and hit-by-pitches than the tamest; mean above median |
| Lane 1 (12×500, cell index on, receiving on) | pitches per PA +5.5% red, walks −4.6% red, runs −4.2% red |
| Lane 2 (12×500, cell index on, receiving off) | runs, walks, pitches and every other band pass; strikeouts −2.1% against a 2.0% floor; home-win rate underpowered by design |

Read together: the catcher weight is the strongest factor in the draw by forty
times, its got-away inputs are a staff measurement, and the hit-by-pitch shift
is the fingerprint of selecting wilder or tamer staffs, which is not a thing a
catcher's skill can do.

---

## 3. The loop, with every draw's filters and inputs ranked

**Each new plate appearance.**

1. **Manager decisions** — pinch hit, pitching change, defensive substitution,
   intentional walk. Draws at the pool's own rates for the situation, weighted by
   the manager profile (real per-team profiles: SIM-427; league-flat today).

**Each pitch.**

2. **Steal draw** (with a stealable runner). Filters: target base, outs, count.
   Inputs: runner speed and tendency; pitcher hold; catcher arm; score; manager
   aggression; recency. Unchanged from today.
3. **Pitch draw — which pitch is thrown.** Filters: batter hand, pitcher hand,
   count, runners, outs, score band, home or road. Inputs: pitcher similarity
   (arsenal and tendencies); batter similarity, small; recency. Output: a real
   pitch — type, velocity, movement, location.
4. **Pitch-result draw — what happens to it.** Same filters. Inputs: the pitch,
   through the pitch-to-pitch engine; batter similarity; pitcher similarity,
   smaller; recency; catcher receiving, last, taken pitches only, OFF until
   SIM-526. Output: ball, called strike, swinging strike, foul, hit-by-pitch, or
   in play with the row's own batted ball (exit velocity, launch angle, spray,
   distance, type).

**When the ball is in play.**

5. **Park geometry.** The ball's carry and direction against the live park's
   fence at that angle: home run, off the wall, or catchable. A certain outcome
   supersedes the fielding draw.
6. **Fielding draw.** Filters: base-out cell, batter hand, batted-ball class.
   Inputs: batted-ball similarity; the fielders on the chain, each against the
   live defender at that position; park, for balls in the wall zone; the batter's
   batted-ball profile (positioning); batter sprint speed; recency. Output: the
   play — outs and every runner's destination.
7. **Runner advancement draws.** Filters: scenario and bases. Inputs: the ball;
   runner legs and decisions; fielder arm; recency. Unchanged mechanics,
   re-pointed at the ball from step 4.

Then the count and bases update; back to step 2, or step 1 when the plate
appearance ends.

---

## 4. The parts (each closable on its own)

### Part A — nightly score matrices for every actor (the pitcher pattern)

The artifact build emits one score matrix per actor type from its engine's
composite score: catcher by catcher; fielder by fielder per position (nine);
batter by batter; runner by runner and pitcher-hold by pitcher-hold for the steal
draw. Sizes: catcher trivial, fielders ~80 MB across positions, batters ~100 MB,
the steal pair under 50 MB each; all ride the shared-memory seam. At draw time
every factor is a row lookup and a gather; no distances or exponentials on the
play path. If the fitted powers are fixed at build time, the matrices store the
powered scores. **The concentration check runs at build time:** for every
player-season, the share of a cell's weight his own team's rows would receive
under his row of the matrix; a matrix that exceeds the floor fails the build.
Files: `pipeline/batch/engine_artifacts.py` (builders + loader + shareable lists),
`simulation/full_pool_sampler.py` (gathers replace `_f_batter`,
`_f_catcher_receiving`, `_f_live_fielder`, `_steal_actor_factor`).

**Part A LANDED 2026-09-08 (code + the live matrices; switch OFF in production).**
What shipped: `build_actor_sim_matrices` in `pipeline/batch/engine_artifacts.py`
(`--what actors_sim`, in `all`) writes `actor_sim/{batter, catcher, catcher_throwing,
runner_steal, runner_adv, pitcher_steal, fielder_<POS>×7}.npz` (a JSON index + a dense
float32 matrix, diagonal 1.0, unscored NaN), `manifest.json` and `concentration.json`;
`--strict-concentration` fails the build when any matrix's p90 own-staff ratio exceeds
3.0. The loader reads them into `EngineArtifacts.actor_sim` and the shared-memory seam
publishes every matrix (`actor_sim.<name>.matrix`). The sampler's `actor_matrices`
switch (`SIM_ACTOR_MATRICES`, default off) routes the batter factor, the steal draw's
runner / pitcher-hold / catcher-throwing factors, the advancement draw's runner and
fielder factors and the batted-ball draw's per-position fielder factor through one row
lookup + one gather, raised to `actor_power[name]` (`SIM_ACTOR_POWER_<NAME>`, 1.0 until
part F). A live actor or pool row the matrix lacks is neutral; a bundle without the
matrix falls back to the kernel; OFF is byte-identical (30 unit tests,
`tests/unit/test_sim523_actor_matrices.py`). The receiving kernel is untouched (part E
replaces it). The live build (2023-2026) and its concentration report are recorded in
`CHANGES.md` under this date. Not done here: the powers (part F) and the lane
certification that flips the switch.

### Part B — the pitch / pitch-result split (steps 3 and 4)

Today one draw does both. Split it: the pitch draw weights by pitcher (arsenal,
tendencies), batter (small) and recency and returns a pitch row; the result draw
weights the same cell's rows by the pitch-to-pitch score to the drawn pitch,
batter, pitcher (smaller) and recency and returns the result row. When the result
is in play, the row's batted ball is read by pitch id from the outcome pool (the
join SIM-463 already exports in reverse). Both draws run inside the cell index
(`simulation/filter_cells.py`), so each scans a few hundred rows.
Files: `simulation/full_pool_sampler.py`, `simulation/sim_loop.py`
(`_full_pool_outcome`), `pipeline/batch/engine_artifacts.py` (the pitch-id join).

**Part B LANDED 2026-09-08 (code + the live pitch-id join; switch OFF in production).**
What shipped: the sampler's `pitch_result_split` (`SIM_PITCH_RESULT_SPLIT`, default off)
turns `draw` into two draws over the same count sub-cell: the PITCH draw from the per-PA
weight (the batter factor re-raised to `pitch_batter_power`), then the RESULT draw from
that weight times the pitch-to-pitch score to the drawn pitch — a Gaussian on the pitch
engine's own weighted, z-scored metric (`result_pitch_sigma`, 1.0; the engine's feature
weights are pinned by a test) — with the pitcher and batter factors re-raised to
`result_pitcher_power` / `result_batter_power`. Every power is 1.0 until part F. The
result row is the play: its outcome, its got-away fact, and its own batted ball through
the new pitch-id join `HandPool.bb_row` (`pitch_pool/<hand>.bb_row.npy`, written by the
batted-ball export from the pitch pool's meta parquet; shareable; None on an older
bundle) — `last_born_batted_ball()` returns exit velocity, launch angle, spray, distance
and the air flag. The thrown pitch stays readable as `last_pitch_geom()`. The born ball
reaches the fielding draw behind `bb_born_sigma` (`SIM_BB_BORN_SIGMA`, 0 = off): a
Gaussian on the z-scored batted-ball features over the base-out cell — the seed of part
C's step 6. A pitch with no complete geometry, or bandwidth 0, makes the result the pitch
row; an incomplete candidate row draws at the average weight. **The density
correction:** the live probe measured the kernel's lean toward the dense strike zone at
neutral powers (walks −37%); every candidate is now divided by its own local density under
the same kernel (`result_density_power`, 1.0; 0 = off), and the split then reproduces the
single draw's pitch-result mix within noise — the check that the result draw conditions
without biasing. 29 unit tests (`tests/unit/test_sim523_result_split.py`). Live: the pools
re-exported with the join
(29 s; every batted-ball row found its pitch: 466,179 of 481,905 in-play pitches), the
app restarted on it. Found and fixed on the way: a migration-0023 pool exported before
its rebuild carries an all-unknown batting side, which made the cell index widen past
the score band on every draw; an all-unknown side column now counts as absent. The
A/B probe at powers 1.0 is recorded in `CHANGES.md` under this date. Not done here: the
powers and bandwidth (part F) and the lane certification that flips the switch.

### Part C — the fielding split (steps 5 and 6)

Step 5 unparks the fence work: `derived.park_geometry` (fence distance and height
by spray sector, ~360 rows, hand-curated — SIM-478), the trajectory model
(SIM-479; Statcast distance is where the ball was fielded or stopped, so
wall balls need the model, validated on home runs first), the fence-resolution
stage (SIM-480). Step 6 replaces today's single batted-ball draw: filters on the
base-out cell, batter hand and batted-ball class; weights by batted-ball
similarity (exit velocity, launch angle, spray, distance), the chain fielders,
park for wall-zone balls, the batter's spray profile, sprint speed, recency. The
drawn row carries the transition as today (the SIM-511 mechanics stay).
Files: `simulation/full_pool_sampler.py` (`battedball_new_pa` → two stages),
`simulation/sim_loop.py` (`_full_pool_fielding`), a new park-geometry table and
migration, `pipeline/batch/player_profile_computor.py` (sprint speed into the
fielder profile).

**Part C, how it is cut (2026-09-08).** Four closable slices, in order:

* **C1 — the fielding draw's filters and weights (step 6).** The batted-ball CLASS
  (ground ball / line drive / fly ball / popup / bunt; `BattedBallPool.bb_class`,
  exported from `bb_type`) joins the base-out cell and the batter hand as the third
  hard filter (`SIM_BB_CLASS_FILTER`; an empty class falls back to the cell and is
  counted). The born ball's similarity (part B's `SIM_BB_BORN_SIGMA`) is the first
  weight; the credited fielder against the live defender at that position stays as
  today (the CHAIN needs the fielding credits of part G); the park kernel applies only
  to a ball in the wall zone (`SIM_PARK_WALL_ZONE_ONLY`, `SIM_WALL_ZONE_DISTANCE`);
  the batter's profile weight stays; batter sprint speed is a new kernel on the
  baserunner embedding's speed (`SIM_BB_SPEED_SIGMA`); recency stays. Everything off
  by default.
* **C2 — the park geometry from the pool (SIM-478).** Per venue and spray sector,
  the fence distance the pool's own home runs and wall balls reveal, written into
  the artifact bundle (the DuckDB writer lock, SIM-524, keeps the table for later),
  with the hand-curated corrections as an overlay file.
* **C3 — the carry model (SIM-479).** The ball's carry from exit velocity and
  launch angle, fitted on the pool's home runs and fly balls, validated on home runs
  first (the reported distance of a home run is its landing point).
* **C4 — the fence stage (SIM-480).** Before the fielding draw: the born ball's
  carry and direction against the live park's fence at that sector — over it, a
  certain home run that supersedes the draw; short of it, the draw without the pool's
  home-run rows; at it, the draw among wall-zone rows. `SIM_FENCE_STAGE`, off.

**Part C BUILT 2026-09-08 (all four slices; every switch OFF in production).**

* C1 — `BattedBallPool.bb_class` (exported from `bb_type`; shareable; None on an older
  bundle); `bb_class_filter` (`SIM_BB_CLASS_FILTER`) hard-filters the base-out cell to
  the born ball's class, falling back to the cell when the class is empty there
  (`bb_class_counts`); `bb_speed_sigma` (`SIM_BB_SPEED_SIGMA`) is a Gaussian on the
  z-scored sprint speed of the live batter against each row's batter, read from the
  baserunner embedding, mean-1 normalized, neutral where a batter has no speed;
  `park_wall_zone_only` (`SIM_PARK_WALL_ZONE_ONLY`, `SIM_WALL_ZONE_DISTANCE`) restricts the
  park kernel to a born ball that is an air ball carrying at least the wall-zone
  distance. The credited fielder's factor and the batter's profile factor stay as they
  were; the chain fielders wait for part G's credits. The born kernel carries the same
  DENSITY CORRECTION part B put on the result draw (`bb_born_density_power`,
  `SIM_BB_BORN_DENSITY_POWER`, 1.0): the probe measured the raw kernel's lean toward the
  ordinary, weakly hit ball (hits −7%, doubles −23%, home runs −17% at bandwidth 0.5).
  20 tests (`tests/unit/test_sim523_fielding_split.py`).
* C2 — `build_park_geometry` (`--what park`, in `all`) writes `park_geometry.json`: per
  venue and 10-degree spray sector (nine over the fair field), the effective fence as
  the pool reveals it — the midpoint of the home runs' 10th-percentile carry and the
  kept air balls' 99th-percentile carry, with the league sector line where a venue
  sector lacks ten of either, and `park_geometry_overrides.json` merged on top for
  hand-curated corrections. A tall wall shows as a longer required carry, so one
  number per sector is the effective fence. Live: 40 venues; Fenway's left-field line
  reads 346 ft and its right-field line 363, Yankee Stadium's right-field line 338,
  Coors' center 421-423, Minute Maid's lines 341-344 and its center 418.
* C3 — the carry model: a quadratic in exit velocity and launch angle fitted on the
  pool's 21,424 home runs (mean absolute error 13.6 ft, root-mean-square 17.3), the
  fallback when a born ball has no distance. Finding: the pool's kept air balls carry
  right up to the home-run line in every sector, so the reported distance IS the ball's
  carry (a projected landing), not where it was fielded; the model is a fallback, not
  the primary carry.
* C4 — the fence stage: `fence_stage` (`SIM_FENCE_STAGE`, `SIM_FENCE_MARGIN` 0 ft — a
  DECISIVE fence: the probe measured a 10-foot band losing ~30% of home runs, because a
  born home run inside the band drew a home run only 21% of the time under a born kernel
  whose distance bandwidth pulls in shorter plays; at margin 0 the drawn home-run share
  sits within noise of the born balls' own). With
  the live venue (`GameState.park`, now carried by `simulate_game(venue_id=)`, the kwargs
  contract's new `venue_id`, and the park-factor resolver), the born ball's carry meets
  the live fence at its direction: over by the margin, the draw runs among the cell's
  home-run rows only (a certain home run); short by the margin, or not an air ball, the
  home-run rows leave; inside the band every row stays; no geometry, venue or
  direction, the stage passes (`fence_counts`). 20 tests
  (`tests/unit/test_sim523_fence_stage.py`). The probe and its ablation arms are
  recorded in `CHANGES.md` under this date.

### Part D — manager decisions at plate-appearance start (step 1)

Align the manager hooks with the loop order: every decision a draw at the pool's
rate for the situation, the profile a weight. The real per-team profiles are
SIM-427's plan; this part only fixes the order and the draw form.

**Part D BUILT 2026-09-08 (the order fixed; the pitching change as a draw, switch OFF).**

* **The order.** The manager decisions — the pitching change, the pinch hit, the bunt
  setup — now run ONCE, on a plate appearance's first pitch (`_start_of_pa_hook`), before
  the intentional-walk and steal decisions. They used to run at the END of the previous
  plate appearance and again at the half-inning roll, so a between-innings pull was
  evaluated twice. `GameState` gains the two starters (kept apart from the current
  pitchers, which a pull overwrites) and the half's plate-appearance count.
* **The draw form (SIM-427 §4's design, built here without the DuckDB table).** The
  pitching-change OPPORTUNITY pool — one row per plate-appearance boundary while a
  pitcher is on the mound, changed or not — is built at artifact time from the situation
  table (the half, the outs, the bases, the scores) joined to the pitch pool (the pitcher
  per plate appearance): `build_pitching_change_pool` (`--what manager`, in `all`),
  `ChangePool` in the bundle (shareable). Live: 678,014 boundaries over 9,242 games; the
  pool's own change rate 8.9% per boundary — 28.7% at a half-inning boundary, 3.5%
  mid-inning; starters 4.5%, relievers 15.4%. `pitching_change_draw` hard-filters the
  cell (starter or reliever, a half-inning boundary or mid-inning, the pitch-count
  bucket of ten, times through the order; widened below `change_min_cell` in a fixed
  order and counted), weights by recency, a Gaussian on the z-scored soft columns
  (`SIM_CHANGE_SIT_SIGMA`), the live pitcher's similarity to each row's pitcher
  (`SIM_CHANGE_PITCHER_POWER`) and an optional per-row manager weight (SIM-427's real
  profiles; flat today); the drawn row's `changed` flag IS the decision — no floor, no
  ceiling. Behind `SIM_MANAGER_DRAW` (OFF) the pull follows the draw; without a pool the
  SIM-434 formula stays. The reliever choice stays the positional pick (SIM-427 5b/5c
  map the drawn row's incoming arm onto a real pen; `last_change_row` exposes it). The
  intentional walk was already a cell draw (SIM-515); the pinch hit and the bunt setup
  stay as they are until SIM-427 5d deletes or rebuilds them. 19 tests
  (`tests/unit/test_sim523_manager_draw.py`); the probe is
  `scripts/sim523_manager_probe.py` and its numbers are in `CHANGES.md` under this date.

### Part E — the catcher receiving ratio factor, built OFF

For taken rows in the result draw's cell: called-strike rows × the live catcher's
framing multiplier at the row's zone (his called-strike rate above the zone's
league rate); ball rows × the mirror, (1 − league rate × multiplier) / (1 −
league rate); swung-at rows × 1; then the taken group rescaled so its total
weight is unchanged. Blocking the same way on the got-away rows among taken rows,
using blocks above expectation, never the raw got-away rate. A unit test asserts
the taken group's total weight and every swung-at weight are unchanged for any
catcher. Ships behind `SIM_CATCHER_RECEIVING` = off; today's bell-curve kernel and
its two sigmas are deleted when this lands. **Fitting and enabling: SIM-526.**

**Part E BUILT 2026-09-09 (the receiving ratio, switch OFF; the bell-curve kernel deleted).**
The bundle carries `receiving.json` (`build_receiving_profiles`, `--what receiving`, in
`all`), derived from the pool itself so the rows' zones and the catchers' rates share one
definition: the league called-strike rate among taken pitches per SEASON and zone group (the heart,
zone 5; the in-zone edge, zones 1-4 and 6-9; outside, zones 11-14) and the league got-away
rate per season and blocking cell (the pitch-height bucket times in-zone or not), with the
pooled rates as the fallback for a row season the document lacks — per season because the
outside called-strike rate fell from 7.1-7.3% in 2023-24 to 4.1-4.5% in 2025-26, so a pooled
rate called every 2024 catcher a good framer and every 2026 catcher a poor one (the per-season
medians all read 0.98-1.00); per catcher-season
the framing multiplier per group (his rate over the league's, shrunk toward the league
with 200 taken pitches of prior weight, clamped so the ball rows' mirror stays
non-negative) and the blocking ratio (his got-aways over the league's expected for the
pitches he received — got-aways above expectation, never the raw rate — shrunk with five
expected got-aways of prior weight). The pitch pool now exports the zone
(`HandPool.zone`). The sampler's `_recv_factor` (cached per hand and catcher) puts on
every TAKEN row: a called strike × the multiplier at its group, a ball × the mirror
(1 − L·m) / (1 − L), a got-away × the blocking ratio, the other taken rows × its mirror;
swung-at, hit-by-pitch and unknown-zone rows read 1. `_apply_receiving` then rescales the
taken group of each candidate set so its total weight is unchanged — the factor moves
ball-or-strike WITHIN the taken pitches and never the swing-or-take split (the unit test
the plan asked for: the taken group's total weight and every swung-at weight are
unchanged for any catcher, on the whole-pool path, the cell path and the result draw).
With the split on it weights the result draw only, last; off, the single draw.
`SIM_CATCHER_RECEIVING` (OFF; fitting and enabling is SIM-526). Deleted: the SIM-517
bell-curve kernel (`_f_catcher_receiving` and its data helpers), `catcher_framing_sigma`,
`catcher_block_sigma`, the two env names, their pins and lane arms, and five kernel
tests; the got-away resolution stays. 13 tests (`tests/unit/test_sim523_receiving_ratio.py`);
the probe is `scripts/sim523_receiving_probe.py`, its numbers in `CHANGES.md`.

### Part F — the fit

Powers per factor, per draw, fitted against the pool's own conditional rates
(the SIM-476 method) under the ordering constraint of ruling 1, with the
concentration check as a second pass/fail. The pitcher factor's flatness (91% of
the pool in play) is the first fit target: its power must rise until the sim's
strikeout, walk and contact rates conditioned on the live pitcher's profile match
the pool's own. Then the 12×500 lane; then the compose flags and the lane's
production set change in one commit.

**Part F FITTED 2026-09-09 (lane-read, the flip waits on a grading ruling).** The instruments: `scripts/sim523_fit_probe.py` (the loop instrumented — per
live actor, the outcome mix by quintile against his OWN rows, standardized to his count
mix; the steal / advancement draws by tier; the fielding draw's event mix per born class;
the factor strengths; the candidate set's effective rows), `scripts/sim523_power_scan.py`
and `scripts/sim523_kernel_scan.py` (the same conditionals computed offline from the pool
for a ladder of powers / bandwidths), `scripts/sim523_fit_compare.py`,
`scripts/sim523_concentration.py`. The finding: at power 1 every identity factor was nearly
flat (the sim's quintile spread over the pool's own: pitcher whiff 6%, batter 3%, the steal
runner kernel 18%). The ceilings: the pitcher engine's arsenal-only score (SIM-067) reaches
~70% of a pitcher's whiff spread and ~43% of his ball spread at power 20; the batter's
composite ~87%; the runner matrices are unusable (two thirds of steal rows unscored) so the
runner factors keep their kernels with the bandwidth fitted; the fielder matrix is flat at
power 1 and breaches the 3.0 own-staff limit at 2, so the fielder factor keeps its σ=0.5
kernel; the sprint-speed kernel has no data (the runner embedding's speed is zero on every
row — part G). The fit: the pitcher factor at 16 in the pitch draw AND the result draw (the
"smaller in the result draw" idea backfires — the result draw carries the pitcher's outcome
identity), the batter at 8 in the result draw and 1 in the pitch draw (the ordering ruling
holds: effective shares 16% / 21% / 95%), the pitch-to-pitch bandwidth 1.0 with the density
correction 1.0, pitcher-steal 12, catcher-throwing 2, the runner kernels 0.25 (steal and
advancement), the born-ball kernel 1.0 (density 1.0), the class filter, the wall-zone rule
and the fence stage ON; the concentration check passes (worst p90 2.34). Code: the pitch
draw's pitcher power (the result power absolute), the batter power on the single-draw
path, per-runner bandwidths, the draw-neutral rule for unscored rows at a power (a 3%
unscored share would otherwise dominate at power 12), the batter matrix reaching the cell
path (part A's omission), power 0 = the kernel. The composition read: an identity-faithful
sim of the fixed 12-game set moves its walks toward its own actors (−5% per game at the
fit), which the pool-total grade cannot tell from a defect — see `CHANGES.md`.

### Part G — data additions

- Fielding credits (the chain): check whether `raw.play_events` carries every
  fielder credited on a play; if not, an ETL addition (~6 h re-sweep).
- Sprint speed into the fielder profile (one join; already ingested).
- The got-away rates out of any profile used for selection.
- The batting side on the pitch pool: built (migration 0023), lands with the
  conditioning rebuild (SIM-469), which waits on the forkserver lock (SIM-524).

**Part G BUILT 2026-09-09 (the data adds; the SIM-469 rebuild ran).** Checked against
the data first: the raw sprint-speed table was EMPTY (the "already ingested" premise was
wrong) — the loader ran for 2023-2026 and both profiles carry the speed (migration 0024;
the embeddings re-exported); `raw.play_events` does not carry the credits (it holds
pickoffs, step-offs, balks and intentional walks only) but the loader already writes every
putout / assist / throwing-error credit onto the pitch row, complete on 99.996% of balls in
play, so the chain lands as a JOIN, not a re-sweep: the outcome pool's eight-position
alignment plus putout / assist position masks (`sql_credit_mask`, builder sim523g.1),
exported to `BattedBallPool.fielders` + masks; the catcher embedding drops its
got-away-derived columns (`_EMBEDDING_EXCLUDE`) — no draw selected on them, now none can;
the batting side, the pitch count and the times through the order reached the pitch pool
through the SIM-469 rebuild inside the same app-stopped window
(`scripts/sim523_part_g_rebuild.py`). The chain FACTOR in the fielding draw (step 6's
consumer) and the sprint-speed kernel's fit are the follow-ons; part F fitted the runner
kernels on a zero speed column, so their bandwidth wants a re-check. Measurements in
`CHANGES.md`.

---

## 5. Sequencing

| Order | Part | Needs | Lane? |
|---|---|---|---|
| 1 | The immediate config: receiving kernel OFF in production, cell index ON (lane 2 certified this: 83/85, strikeouts −2.1% marginal) | owner go | done (lane 2) |
| 2 | A — score matrices + the concentration check | — | no |
| 3 | B — the pitch/result split | A, the cell index | yes |
| 4 | F (first pass) — the pitcher power | B | yes |
| 5 | C — the fielding split + fence work | A; the geometry table | yes |
| 6 | G — data additions; E — the receiving ratio, OFF | the SIM-469 rebuild | no |
| 7 | D — manager order | SIM-427 | with SIM-427 |
| 8 | SIM-526 — fit and enable receiving | E, F | yes |

Order 1 is a production change and waits for the owner's explicit go. Orders 2-5
are the redesign proper; each lands gated off, byte-identical off, and certifies
on the lane before its flag flips.

---

## 6. Costs

| Part | Effort | Machine |
|---|---|---|
| A | 2 days | nightly build +5-10 min |
| B | 3 days | one lane |
| C | 4 days + the geometry table's curation | one lane; the trajectory validation |
| D | 1 day | — |
| E | 1 day | — |
| F | 2 days per pass | one lane per pass |
| G | 1-2 days (+ ~6 h if the credits need a re-sweep) | — |

Roughly three to four working weeks plus four lanes at the new lane speed (~1.5 h
each).

---

## 7. Open questions

1. Are fielding credits in `raw.play_events`? (Decides whether "the chain" is
   data or inference.)
2. The trajectory model's validation set: home runs first, then wall balls.
3. The batter-hand filter halves the fielding sample (~25,000 rows per base-out
   cell per hand today); acceptable, to be confirmed by the widening counts.
4. The strikeouts −2.1% residual under lane 2 is a per-count fit question, not a
   blocker; it rides Part F.
