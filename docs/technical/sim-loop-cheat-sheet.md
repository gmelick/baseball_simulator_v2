# The simulation loop on one page — every draw, its weights, and the similarity scores behind them

*Written 2026-09-14 for the owner. Values are what production runs today (`docker-compose.yml`,
the `app` service) after the manager-draw flip of 2026-09-13. Code: `simulation/sim_loop.py`
(the loop), `simulation/full_pool_sampler.py` (the draws), `similarity/engines/` (the scores).*

## How to read this

The simulator never uses a formula to decide what happens. Every decision is a **draw**: it
takes a **pool** of real plays (2023–2026, about 2.8 million pitches), keeps only the rows
that are legal for the live situation (the **hard filter**), gives each remaining row a
**weight**, and draws one row at random in proportion to the weights. **The drawn row is the
play.** A weight is either a **similarity score** — a 0-to-1 number from one of the engines in
§3 saying how alike the live player and the row's player are — raised to a fitted **power**
(a higher power makes the draw prefer look-alikes more strongly; 0 turns the factor off), or a
**— every power is 1 since 2026-09-16** (owner ruling: the powers are fitted together in the
sweep of the tunable parameters; at 1 the sampler skips the power step, and the fitted values
the 1s replace are kept in §2's summary table) —
**bandwidth** on a distance (a Gaussian: rows farther than about one bandwidth from the live
value fade out; a smaller bandwidth is stricter). Every pool row also carries a **recency
weight**: 2.0 for the two most recent seasons, ×0.75 per older season, floor 0.25.

## 1. The loop

For each half-inning, for each plate appearance, for each pitch:

```
plate appearance starts ─► ① pitching change?  (the fielding manager)
                           ② which reliever?    (if a change was drawn)
                           ③ intentional walk?
each pitch ──────────────► ④ steal / pickoff?   (a runner on first with second open, or on second with third open)
                           ⑤a the pitch thrown  (weighted toward the pitcher's look-alikes, power 1; was 16)
                           ⑤b the pitch's result (ON since 2026-09-14: a second, batter-weighted draw among rows whose
                                                  pitch resembles ⑤a's; ball, called strike, whiff, foul, in play, hit by pitch)
   if in play ───────────► ⑥ the batted ball's fate: the fence stage, then the fielding draw
                           ⑦ the runners' extra bases (up to five discretionary decisions)
   if the pitch got away ► ⑧ runners advance one base (wild pitch / passed ball / dropped third strike)
```

## 2. The draws, one by one

### ① The pitching change — `pitching_change_draw` (ON since 2026-09-13)

| | |
|---|---|
| pool | one row per real plate-appearance boundary while a pitcher was on the mound, changed or not (693,774 rows) |
| hard filter | starter or reliever; a half-inning boundary or mid-inning; the pitch-count bucket (tens); times through the order (1–4). Under 20 rows the cell widens: drop times-through, then the bucket, then the boundary |
| weights | recency · a Gaussian on the situation (pitch count, batters faced, inning, outs, runners, the fielding side's margin; z-scored, **bandwidth 1.0**) · the live pitcher's **pitcher similarity** to the row's pitcher (**power 1**) · the live manager's **manager-usage similarity** to the row's manager (**power 1**; fitted 4) |
| the row answers | did the manager change pitchers here (yes / no) |
| the cutoff | in a backtest, rows dated after the game are excluded |

### ② Which reliever — `relief_arm_draw` (ON since 2026-09-13)

| | |
|---|---|
| candidates | the real pen: the arms the MLB box listed as available for the game, minus the rotation (a start of 45+ pitches in the team's last five games) and today's starter, minus the arms already used |
| weights, each a resemblance to the arm the drawn row brought in | **role** — Gaussian on the difference in high-leverage entry share, **bandwidth 0.1** · **rest** — Gaussian on days since the last outing (capped at five), **bandwidth 0.5 days** · **pitched in the last two days** — a mismatch multiplies by **0.25** · **pitches over the last three days** — Gaussian, **bandwidth 10 pitches** · stuff (pitcher similarity, **off**) · hand (**off**) |
| every weight off / no sampler | the positional pick: the first listed arm in a high-leverage late spot, else the last |

### ③ The intentional walk (SIM-515)

A coin flip at the **real rate** of the plate appearance's cell: runners × outs × late (7th inning
or later) × close (within one run), from `sim.ibb_rates`. No similarity weight.

### ④ The steal / pickoff — `steal_draw`

| | |
|---|---|
| pool | one row per real pitch on which a steal was possible, attempted or not (~2.4 million) |
| hard filter | the target base (second or third) and the exact outs-balls-strikes count |
| weights | recency · a Gaussian on the score margin (**bandwidth 2 runs**) · the live runner's **steal-runner similarity** to the row's runner (**power 1**; fitted 12) · the live pitcher's **pitcher-steal similarity** (**power 1**; fitted 12) · the live catcher's **catcher-throwing similarity** (**power 1**; fitted 2) · the batting manager's **aggression** on attempted rows only: his measured steal rate over the league mean, clamped 0.05–4 (a weight, never a gate) |
| the row answers | went or stayed; safe or caught; picked off; a pickoff throw that got away |

### ⑤ The pitch — two draws by design (`draw` through the cell index; `_result_draw`)

The design (the play-picker redesign, part B, 2026-09-08) splits the pitch into two draws.
**⑤a the pitch thrown** is weighted toward the live pitcher's look-alikes. **⑤b the pitch's
result** is a second draw among rows whose pitch resembles the one just thrown, weighted toward
the live batter. It is ⑤b's row, not ⑤a's, whose batted ball feeds the fielding draw. Both
draws run in production (`SIM_PITCH_RESULT_SPLIT=1` since 2026-09-14, by owner decision): the
split was fitted on the pool's own conditionals (pitcher 16 in both draws, batter 8 in the
result draw), held OFF while the pool-totals lane read strikeouts 2.4% short (2026-09-09), and
flipped on the accuracy comparison's read (the starter strikeout market −0.0155 Brier on 250
games of 2024, the moneyline informative for the first time; the strikeout probabilities sit
6.5 points low — the calibration layer's job). With the switch off, ⑤a's row supplies both the
pitch and its result.

| | |
|---|---|
| pool | every pitch of 2023–2026, split into two pools by the **batter's hand** (the one hard filter besides the cell) |
| hard filter | the **cell**: runners (8) × outs (3) × count (12) × score band (5: ≤−3, −2..−1, 0, +1..+2, ≥+3) × batting side (2) = 2,880 cells; under **20 rows** the cell widens: the score band first, then the side, then the count |
| ⑤a the pitch thrown — weights (production) | recency · the live pitcher's **pitcher similarity** to the row's pitcher (**power 1** since 2026-09-16, `SIM_PITCH_PITCHER_POWER`; fitted 16, in production 2026-09-14 → 16; power 1 reads nearly flat) · the live batter's **batter similarity** (**power 1**) · a Gaussian on the base-out situation (outs, runners, inning, margin; **bandwidth 2.0**) · the **fatigue weight**: a Gaussian on the gap between the live pitcher's times through the order and the row's (**bandwidth 0.5**, `SIM_FATIGUE_TTO_SIGMA`; landed 2026-09-14 by owner decision; the pitch-count term `SIM_FATIGUE_PC_SIGMA` OFF) · the batting side within the cell (weight 1.0 = neutral; the cell already filters it) |
| ⑤b the pitch's result — weights (production) | over the same cell rows: the per-plate-appearance weight × a Gaussian on the **pitch-to-pitch distance** from the pitch just thrown (the pitch engine's metric over velocity, breaks, spin, release and location; **bandwidth 1.0**, `SIM_RESULT_PITCH_SIGMA`) × a density correction, so crowded pitch regions are not over-drawn (`SIM_RESULT_DENSITY_POWER` 1.0) × the pitcher re-raised to **power 1** (`SIM_RESULT_PITCHER_POWER`; fitted 16) × the batter at **power 1** (fitted 8; (`SIM_RESULT_BATTER_POWER`) × the catcher receiving ratio (off) |
| also built and OFF | the fatigue weight's pitch-count term · the catcher receiving ratio |
| the row answers | ⑤a: the pitch thrown. ⑤b: the pitch's outcome — and, for a ball in play, the **born batted ball**: its exit velocity, launch angle, spray and distance, which the fielding draw then fields |

### ⑥ The batted ball — the fence stage, then `battedball_draw`

| | |
|---|---|
| the fence stage (ON) | for an air ball carrying 300+ feet, the born ball's carry against the live park's real fence decides home run / not before any draw (`park_geometry.json`; the band is 0 feet) |
| pool | one row per real ball in play with its whole base-out transition |
| hard filters | the exact base-out cell (the drawn row must be legal here) · the born ball's **class** (ground ball, line drive, fly ball, popup, bunt) · the **batting side** (the home weight is 0.0: rows of the other side are excluded — this is where home-field advantage comes from) |
| weights | recency · a Gaussian on the born ball (exit velocity, launch angle, spray, distance; z-scored, **bandwidth 1.0**) · the live batter's **batter similarity** (**power 1**; fitted 4) · a Gaussian on the situation's soft dimensions (balls, strikes, inning, margin; bandwidth 2.0) · the **platoon**: rows whose pitcher hand does not match the live matchup × **0.6** · the live defender at the row's fielded position: **fielder similarity** to the row's fielder (**power 1**; fitted 1.2, per position) · the park: a Gaussian on the run factor (**bandwidth 0.02**), only for wall-zone balls |
| the row answers | the event and where every runner ends up (the transition) |

### ⑦ The extra bases — `advancement_draw` (five decisions)

First-to-third on a single; second-to-home on a single; first-to-home on a double; the tag-up
from third (and second) on a fly out; the batter's stretch for the extra base. Lead runner
first; a trailing runner cannot pass.

| | |
|---|---|
| pool | one row per real opportunity for that exact decision, attempted or not |
| hard filter | the decision itself (scenario, from-base, to-base) |
| weights | recency · a Gaussian on the throw geometry (exit velocity, launch angle, spray, distance, outs; z-scored, **bandwidth 1.0**) · the live runner's **advancement-runner similarity** (**power 1**; fitted 20) · the live fielder's arm against the row fielder's: **fielder similarity** at the fielded position (**power 1**; fitted 1.2) |
| the row answers | went or held; safe or out; an extra base on a bad throw |

### ⑧ Got-away and dropped third strike (ON)

The drawn pitch row's own fact: if that real pitch got away (wild pitch / passed ball /
uncaught third strike), the runners advance one base and a striking-out batter may reach
(first base open, or two outs). No weight; the row carries it.

### Summary of the fitted values in production

**Every similarity POWER is 1 since 2026-09-16** (owner ruling: the powers are fitted
together in the sweep of the tunable parameters; the table keeps the fitted values the 1s
replace, because the sweep starts from them). The bandwidths and mismatch weights stand.

| factor | where | value | fitted against |
|---|---|---|---|
| pitcher similarity | pitch draw / result draw | power 1 / 1 (fitted 16 / 16) | the pool's own per-pitcher conditionals (redesign part F; flipped 2026-09-14 on the accuracy comparison) |
| batter similarity | pitch draw / result draw / fielding draw | power 1 / 1 / 1 (fitted 1 / 8 / 4) | the pool's mix by batter |
| fielder similarity (per position) | fielding + advancement | power 1 (fitted 1.2) | the pool's per-opportunity conditionals |
| steal-runner / pitcher-steal / catcher-throwing | steal draw | 1 / 1 / 1 (fitted 12 / 12 / 2) | the pool's steal conditionals |
| advancement-runner | advancement draws | 1 (fitted 20) | the pool's advancement conditionals |
| manager-usage | pitching change | 1 (fitted 4) | the managers' own pull depth by tercile (2026-09-13) |
| reliever weights | reliever draw | role 0.1 · rest 0.5 · two-day 0.25 · three-day 10 | the pool's entering arms (2026-09-13) |
| situation Gaussians | pitch / fielding / change / steal / advancement | 2.0 / 2.0 / 1.0 / 2.0 / 1.0 | code defaults (the change draw's fitted 1.0) |
| born-ball Gaussian | fielding draw | 1.0 | the pool's conditionals (redesign part F) |
| park Gaussian | fielding draw, wall zone only | 0.02 | SIM-476 |
| platoon (pitcher hand) | fielding draw | opposite-hand rows × 0.6 | SIM-413 default |
| batting side | fielding draw | hard (weight 0.0) | owner ruling 2026-08-30 |
| fatigue (times through the order) | pitch draw | bandwidth 0.5; the pitch-count term OFF | the pool's own within-pitcher effect at the third time through (fitted 0.7; the owner landed 0.5 on 2026-09-14 — the accuracy comparison reads both flat) |
| recency | every pool | 2.0 / ×0.75 per season / floor 0.25 | SIM-076 |
| cell minimum | pitch draw / change draw | 20 rows / 20 rows | the SIM-451 census |
| pitch-to-pitch Gaussian + density correction | result draw | bandwidth 1.0 / power 1.0 | the pool's conditionals (redesign part F) |
| reliever stuff (pitcher similarity in the reliever draw) | reliever draw | power 1 since 2026-09-16 (was OFF at 0) | — |
| OFF | the fatigue weight's pitch-count term, catcher receiving, pitch-similarity on the fielding draw, reliever hand | | |

## 3. The similarity scores

Each engine turns two players' season profiles into one number from 0 (nothing alike) to 1
(the same player). The number is built from **sub-scores**, each a Gaussian ("RBF") on the
standardized difference of a feature group with its own bandwidth (σ), combined with fixed
weights. Feature-level numbers in brackets are the within-group weights. Thin profiles are
shrunk toward the league average before scoring (the "EB prior": how many games of evidence
it takes to trust a player's own number). Nightly, each engine scores every pair of profiles
into a matrix the draws look up.

### Pitcher — `pitcher_similarity.py` (feeds the pitch draw, the pitching-change draw, reliever "stuff")

- **Arsenal, weight 0.65** — a Gaussian-mixture fingerprint of every pitch he throws over
  eight dimensions: velocity, induced vertical break, horizontal break, spin rate, spin axis,
  release x, release z, release extension. Two arsenals are compared by the Wasserstein-2
  distance (how much "work" it takes to move one pitch distribution onto the other), turned
  into a score by exp(−W₂ / 4.10), calibrated so the median pair scores 0.50.
- **Command, weight 0.35** — RBF (σ 1.05) over: walk rate, strikeout rate, called-plus-whiff
  rate (CSW), zone take rate, chase rate, zone rate, whiff rate.
- Shrinkage prior 5. Note: this composite is what the pitch draw raises to power 1 today; the
  strikeout market's blindness (SIM-548) is the question of whether this score, at any power,
  carries a pitcher's strikeout ability into the draw.

### Batter — `batter_similarity.py` (the pitch draw at power 1, the fielding draw at power 1 — fitted 4)

- **Discipline 0.32** (σ 1.04): first-pitch take rate, chase rate, zone swing rate, contact
  rate, whiff rate, strikeout rate, walk rate.
- **Batted ball 0.28** (σ 1.08): ground-ball rate, fly-ball rate, pull rate, opposite-field
  rate, average exit velocity, average launch angle, hard-hit rate, barrel rate.
- **Platoon 0.12** (σ 1.09): the same-hand versions of chase, zone swing, whiff, walk,
  strikeout, ground-ball and barrel rates (weight 0.28 when the matchup is a platoon one).
- **Power 0.08** (σ 0.96): home-run rate, expected batting average, expected slugging, max
  exit velocity.
- **Physical 0.20** (σ 1.00; SIM-529): swing tilt, bat speed, stance depth, swing length,
  distance off the plate, foot separation, stance angle, contact depth, attack angle, attack
  direction. *Stored, but the nightly batter matrix production reads was built before these
  were added, so the draws run on the older four-group weights (0.40 / 0.35 / 0.15 / 0.10)
  until the matrix is rebuilt.*
- Opposite-hand batters are penalized ×0.92; switch hitters resolve to the hand in play.

### Fielder — `fielder_similarity.py` (per position; the fielding and advancement draws at power 1 — fitted 1.2)

- **Infield**: range 0.45 (σ 1.03: outs above average to the glove side, arm side, charging,
  going back; catch percentage added) · double play 0.30 (σ 0.37: DP above expected, DP
  attempt rate, DP success rate; the pivot for second basemen) · errors 0.15 (fielding,
  throwing) · specialty 0.10 (bunt fielding; the first baseman's scoop rate).
- **Outfield**: range 0.40 (the same five OAA components) · arm 0.30 (σ 0.99, fitted
  2026-09-17: the throw velocity from Savant's arm-strength board at weight 0.844; the
  advancement prevention at 0.137 and the thrown-out rate at 0.240 from our own advancement
  pool, per fielder × position, the two rates shrunk on the arm's own chances, prior 50) ·
  star catches 0.15 (five-star, four-star, routine catch rates) · errors 0.15.
  *The outfield arm block (SIM-550) landed on 2026-09-17, code and data: the fill from the
  advancement pool replaced the runner-view figures the September Savant join wrote, the
  three outfield matrices were rebuilt. The weights are the fitted year-to-year repeats
  WITHIN a position; the plan's 0.60 for the prevention pooled the positions and was mostly
  the position label.*
- Shrinkage prior 15; never scored across positions.

### Catcher — `catcher_similarity.py` (the throwing sub-score feeds the steal draw at power 1 — fitted 2; the full score fed the retired receiving kernel)

- **Framing 0.53** (σ 0.92): called-strike rate above expected, framing runs, shadow-zone
  strike rate, heart-zone strike rate.
- **Blocking 0.24** (σ 1.05): block success rate, passed balls per nine, wild-pitch prevention.
- **Throwing 0.14** (σ 0.98): pop time, caught-stealing rate, arm strength.
- **Deterrence 0.09** (σ 1.00): the steal-attempt rate against him.
- Shrinkage prior 15.

### Steal runner — `baserunner_steal_similarity.py` (the steal draw at power 1 — fitted 12)

*SIM-531 landed on 2026-09-16, code and data: the boards loaded, the profiles rebuilt, the
bandwidths fitted (lead 0.9836), the `runner_steal` matrix rebuilt at 2,585 profiles.*

- **Tendency 0.45** (σ 1.05): steal-attempt rate from first, from second.
- **Lead 0.45** (σ 0.9836, fitted 2026-09-16; SIM-531):
  the runner's lead off the bag before the pitch, in feet, and his jump — the extra distance
  he gets on the pitcher's delivery — from Savant's Basestealing Run Value board at n=1
  (every runner with one chance). The jump repeats year to year at 0.80–0.85; the lead at
  0.73. A runner with no Savant row is scored over tendency and success alone, never
  against a lead of 0.0 ft.
- **Success 0.10** (σ 1.02): steal success rate from first, from second — it repeats at
  only 0.13–0.26, which is why its weight fell from 0.38.
- Every runner who HAD a chance carries a profile, including the ones who never went. The
  confidence, the tendency and the lead shrink on the runner's chances — plate appearances
  begun on first plus those begun on second, never below his attempts (prior 50), so no
  profile reads confidence 0; the success rate shrinks on attempts (prior 20). A runner
  with no measured lead is not shrunk on the lead at all (his NaN stays out of the
  normalizer). The `baserunner_steal` league-average row the shrinkage needs never
  existed before the SIM-531 recompute of 2026-09-16 wrote it.

### Pitcher against the run — `pitcher_steal_similarity.py` (the steal draw at power 1 — fitted 12)

*SIM-531 landed on 2026-09-16, code and data: the hold bandwidth fitted (0.9897), the
`pitcher_steal` matrix rebuilt at 2,390 profiles.*

- **Outcome 0.35** (σ 1.10): stolen bases allowed per nine, caught-stealing rate when
  challenged, the attempt rate allowed. (Delivery and pickoff mechanics were trimmed in
  SIM-408 — the data does not carry them.)
- **Hold 0.65** (σ 0.9897, fitted 2026-09-16; SIM-531): the lead the pitcher
  allows before the pitch and the jump he gives up on the delivery, in feet, from Savant's
  Pitcher Running Game board at n=1. The jump allowed repeats at 0.89–0.90 — the most
  stable number either steal model holds. A pitcher with no Savant row is scored on the
  outcome group alone.
- Shrinkage prior 25 on baserunner events, both groups (an unmeasured hold is not shrunk);
  the `pitcher_steal` league-average row never existed before the SIM-531 recompute of
  2026-09-16 wrote it.

### Advancement runner — `baserunner_similarity.py` (the advancement draws at power 1 — fitted 20)

- **Speed 0.35** (σ 0.82): sprint speed.
- **Aggression 0.40** (σ 1.05): extra-base attempt rate overall, first-to-third,
  second-to-home, first-to-home, tag-up; the stop rate; and (SIM-531) how often the runner
  tried for the extra base ABOVE how often a typical runner would have tried in the same
  chances — Savant's baserunning board supplies the expectation (repeats at 0.75–0.77
  against 0.50–0.59 for the raw rate). A runner with no Savant row has no value there: the
  value stays missing through the shrinkage, and the kernel drops the feature from the
  distance for that pair instead of reading it as a match. (SIM-531 landed on
  2026-09-16, code and data; the `runner_adv` matrix was rebuilt at 2,486 profiles, and the
  calibration file now carries seven aggression weights — the new feature's is the largest,
  0.565.)
- **Success 0.25** (σ 1.00): the matching success rates.
- Shrinkage prior 15.

### Manager — `manager_similarity.py` (the USAGE sub-score alone feeds the pitching-change draw at power 1 — fitted 4)

- **Usage 0.40** (σ 1.00): the starter's average pitch count, the share of starts ended
  before 100 pitches, the closer's entry leverage, the high-leverage reliever share, opener
  rate, bulk-innings rate, available-reliever usage rate.
- **Aggression 0.35** (σ 1.05): steal orders per opportunity, hit-and-run rate, sacrifice
  bunts in high and low leverage, squeeze rate. (The steal weight reads the first of these
  directly, over the league mean.)
- **Platoon 0.25** (σ 1.02): pinch-hit rates (same hand, high leverage), late defensive
  substitutions, double switches, platoon exploitation.
- Shrinkage prior 30 (games).

### Pitch-to-pitch — `pitch_pitch_similarity.py` (⑤b's pitch-to-pitch Gaussian, ON with the split since 2026-09-14)

Velocity 1.2, induced vertical break 1.1, horizontal break 1.0, spin rate 0.7, spin axis
0.5, release x / z 0.6, extension 0.4, plate x / z 0.9 — the weighted distance between the
pitch ⑤a drew and each candidate row's pitch, inside a Gaussian of bandwidth 1.0.

### The two engines that do not feed a draw today

- **Situation** — `situation_similarity.py`: a nearest-neighbour tree over inning, half, outs,
  the three bases, margin, leverage, pitch count, times through, park factor (weights 0.4–1.2).
  The draws use their own hard cells and situation Gaussians instead.
- **Batted ball** — `batted_ball_similarity.py`: exit velocity 1.3, launch angle 1.1, spray
  angle 0.8. The fielding draw's born-ball Gaussian z-scores the same quantities (plus
  distance) with equal weights rather than this engine's.

## 4. Where the values live

- Production values: `docker-compose.yml`, the `app` service's `SIM_*` lines (with the reason
  each was set, in the comments).
- The acceptance lane runs the same values: `tests/acceptance/conftest.py` `PRODUCTION_FLAGS`
  (a test holds it equal to the compose file).
- The unit lane pins every factor OFF: `tests/conftest.py`.
- How each value was fitted: `docs/audit/2026-08-28-sim476-fit-plan.md` (the kernels),
  `docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md` (the powers),
  `docs/audit/2026-09-13-sim427-build-plan.md` (the manager and reliever weights),
  `docs/audit/2026-09-12-sim518-fit-plan.md` (fatigue, landed at 0.5), and the plan that will
  re-fit against the betting-line accuracy comparison,
  `docs/audit/2026-09-14-sim548-accuracy-fit-plan.md`.
