# SIM-548 — one model, every draw, every market: the joint fit of the draw weights against the betting-line accuracy comparison (plan v2, 2026-09-14)

**Status: IN EXECUTION since 2026-09-15 (the owner: "implement your plan"); the §9 decisions run on the
recommended options until the owner says otherwise. The stamps are §11.** This document replaces the design sections of
`docs/audit/2026-09-14-sim548-accuracy-fit-plan.md` (the one-weight-at-a-time ladder); that
file stays as the record of Part A4's read (the split arm) and of the finding that started the
work. Nothing here runs until the owner has read it.

**Written in answer to four questions from the owner on 2026-09-14:** does a one-weight ladder
miss interactions between weights (yes); can every weight be fitted against every market at
once (yes, in layers — this plan); should each bet type get its own weight set (no — one model,
one objective, a per-market correction on the finished probabilities); and how do we know we have
not fitted to the games we scored on (a hold-out season on every flip, and a fresh game set for
the search).

---

## 0. What this plan decides, on one page

1. **One model.** Production runs one simulator with one set of draw weights. Every market is
   priced from the same simulated games. No per-market weight sets (§7 says why).
2. **One objective, written down.** "The most accurate game" is a number: the composite of the
   paired accuracy change over every market that has enough records, each market standardized
   by its own noise (§3). A change is judged on that number, with guards.
3. **Three layers of fitting, cheapest first.** (a) An offline joint fit that scores every draw's
   weights against the real outcomes of held-out plays, over a full grid, in minutes per setting
   — this is where interactions between weights are measured. (b) A designed experiment on the
   full simulator for the handful of weights the offline fit says matter most, which measures
   the game-level markets and the interactions the offline score cannot see. (c) A hold-out
   season that every flip must clear.
4. **A calibration layer on top.** Market-specific correction lives in a per-market map from the
   simulator's probability to a calibrated one, fitted after the weights and refitted whenever
   they change. It corrects probabilities, never plays. The split's 6.5-point low bias on
   strikeouts is its first job (the old strikeout-shortfall and prop-calibration tickets,
   SIM-527 and SIM-429, were closed on 2026-09-10; this work runs under SIM-548).
5. **The weights of every draw are in scope**, including the ones fitted to the pool's own
   frequencies and the ones the owner landed by decision. The joint fit may propose a different
   value for any of them; the owner decides what lands.
6. **Cost.** About a week to build the instruments; one day of compute for the offline fit; three
   to four days of unattended compute for the designed experiment; seven hours for the hold-out;
   the flip and the calibration refit in a day. The arms need the Docker VM to themselves or a
   larger VM (§9).

---

## 1. The rules this plan works under

- **The architecture rule (2026-08-10 / 2026-08-29).** Every decision is a similarity-weighted
  draw from a hard-filtered pool. The drawn row is the play. Every factor is a draw weight or
  OFF. This plan fits weights; it adds no formula and no post-draw adjustment of a play.
- **The calibration boundary (CLAUDE.md §1: "uncalibrated numbers must not reach users").** A map
  from a finished probability to a calibrated probability is not a play adjustment. The
  win-probability reliability curve already is one. This plan extends that layer to the prop and
  segment markets (§7). It never touches a simulated event.
- **The grade for a weight is the accuracy comparison** (`scripts/clv_backtest.py`: the
  simulator's probability and the closing line's against what happened, per record; the Brier
  score, the squared gap between a probability and the 0/1 outcome, lower is better; paired
  between two arms on the same games and seeds, with a game-clustered bootstrap range). The
  pool-totals bands stay the realism guard, read for the record on every arm, not the objective.
- **No betting-value measurement in this plan (owner ruling 2026-09-08).** The closing-line-value
  re-measure and any edge read wait for every band green. Accuracy is not value; this plan
  measures accuracy.
- **A hold-out on every flip.** A value chosen on the games it was scored on can win by luck.
  Nothing lands without a second season the search never saw.
- **The frozen inputs.** No artifact rebuild, no recompute, no `make calibrate` while a set of
  arms runs; the pairing script refuses reports whose bundle or calibration file differ. After
  any `make calibrate`, the reliability curve is written back from `/data/prop_validation.json`
  (the trap found 2026-09-13).
- **Compute is shared.** The Docker VM has 6 CPUs and 9.7 GiB. Five backtest workers take about
  6 GB; the warm app about 2.5 GB; the two databases about 1.1 GB. An arm runs with the app down,
  or with fewer workers, or on a larger VM (§9, decision 6).

---

## 2. Where we stand (verified 2026-09-14)

**Production.** The manager draw at power 4 with the real pen and the reliever draw
(2026-09-13); the fatigue weight at a times-through-the-order bandwidth of 0.5 (the owner's
decision, 2026-09-14); **the split ON at pitcher 16 / 16, batter 8 (the owner's decision, later
on 2026-09-14, without the hold-out)** — the pitch draw at pitcher power 16 and batter power 1
through the cell index, the result draw over the same rows; the fielding draw at its
2026-09-09 fits; the steal and advancement draws at their fitted powers. The full inventory is
§4. A 1,000-game accuracy baseline of this production is running (games 1–1000 of 2024;
`CHANGES.md` 2026-09-14); its per-market skill table is the starting point of §3.

**What the accuracy comparison has judged, on the first 250 Final games of 2024, 100 iterations
per game (Brier, the change minus its baseline; negative = more accurate; the range in brackets).**

| change | starter strikeouts | moneyline | the rest | verdict |
|---|---|---|---|---|
| the manager draw + real pen (vs the formula) | −0.0156 [−0.0255, −0.0066] | −0.0062 [−0.0120, −0.0005] | flat | flipped |
| the fatigue weight 0.5 (vs production) | −0.0033 [−0.0098, +0.0032] | +0.0038 [−0.0016, +0.0093] | flat; three rows at two standard errors among forty | landed by owner decision |
| the fatigue weight 0.7 (vs production) | +0.0004 [−0.0060, +0.0069] | +0.0021 [−0.0030, +0.0074] | flat; the three rows do not repeat | not landed |
| the split 16 / 16 / 8 (vs production with fatigue 0.5) | **−0.0155 [−0.0258, −0.0050]** | −0.0050 [−0.0119, +0.0021] | flat; the first-five total +0.0113 [+0.0010, +0.0224] against an outlier baseline row | flipped by owner decision (no hold-out) |

**What the strikeout market looks like today** (the skill read on the same 250 games). Production's
strikeout probabilities carry no information: the chance that a real over drew the higher
probability (the AUC; a coin flip scores 0.500) is 0.510 against the closing line's 0.584; they
are over-spread (a standard deviation of 0.175 against the line's 0.053) and read worse than a
coin flip (Brier 0.278; the line 0.245; the coin flip 0.250). With the split ON they carry some
information (AUC 0.548, Brier 0.262), are less over-spread (0.144) and sit **6.5 points too low**
(the mean probability of the over is 6.5 points under the over rate). The moneyline turns
informative with the split too (AUC 0.494 → 0.578). Hits and total bases carry real skill either
way, with a 4-point upward bias; home runs and the game total carry none.

**The odds.** Closing lines on 16,845 Final games: 2021–2026 complete for every market the book
posts (about 2,200–2,440 games a season, 11–15 game markets and 10–15 prop markets a game, 2.0
million prop rows); 2019–2020 the three full-game markets only (the book has no prop or segment
markets that far back). The 2025 season carries 2,440 games with a closing strikeout line — the
hold-out set exists. The loads are finished; nothing writes to the odds tables now.

**The instruments in hand.** `scripts/clv_backtest.py` (the comparison; stamps the bundle, the
calibration hash, the fatigue, manager and split settings into each report),
`scripts/sim518_pair_accuracy.py` (pairs two reports; the per-market table with ranges; the
pitch-depth strata), the runner scripts for the arms (`sim427_accuracy_pair.sh`,
`sim518_accuracy_fatigue.sh`, `sim548_accuracy_split.sh`), the in-loop probes
(`sim427_manager_probe.py`, `sim518_fatigue_probe.py`) and the offline scans of the redesign
(`sim523_power_scan.py`, `sim523_kernel_scan.py`), which already compute a draw's weights from
the pool without running games.

**Compute.** Production runs a 2024 game in 35 s of wall-clock across five workers at 100
iterations: 250 games in 2 h 30. With the split ON, 3 h 55 (the second draw per pitch). A probe
arm (45 games × 40 iterations) is 35 minutes.

---

## 3. The objective: "the most accurate game", defined

**The unit.** For one market on one game set, the paired accuracy change of an arm against its
baseline is the mean over records of (Brier of the arm − Brier of the baseline), with a
game-clustered bootstrap range. Call its point estimate Δ and its standard error s.

**The composite.** For an arm, over the set M of markets that clear the record minimum (§3.3):

    Z(arm) = Σ_m  w_m · (Δ_m / s_m)  /  sqrt( Σ_m w_m² )

Each market enters as its own change in units of its own noise (a z-score), so a prop with 4,400
records and a game market with 250 weigh by what they can resolve, not by their record count.
The weights w_m say how much the fund cares about each market. Negative Z = the arm is more
accurate on the whole. Z is computed inside the same game-clustered bootstrap as the per-market
reads, so it carries its own range; the markets share games, and the bootstrap over games keeps
that dependence honest.

**The market weights (owner decision 1).** Two candidates:

- *Equal per market* (w_m = 1). Simple, no data needed, the default in this plan.
- *Stake-weighted* (w_m = the fund's intended share of stake on market m). Better matched to the
  business, but it needs a stated bet mix, and it lets one market dominate the search. Not
  available until the bet mix is written down.

The choice changes which trade-offs the search accepts; it does not change the instruments.

**The guards.** A change lands only if, on both the fitting season and the hold-out season, no
market worsens beyond its range in the same direction on both. One market at two standard errors
on one season among forty rows is what chance produces (the fatigue arms showed exactly this); the
same market, the same direction, both seasons is not. The composite decides among candidates that
clear the guards.

**The realism guard.** The pool-totals bands (the balanced 45 × 130 lane) are read on the chosen
setting before the flip. A setting that turns a band red is reported with the band's number; the
owner decides whether accuracy buys that red. The rule of 2026-09-08 (no betting-value measurement
until every band is green) is unchanged and is about value, not accuracy.

### 3.3 Which markets enter the composite

A market enters when it clears the record minimum the pairing script already applies (the
SIM-539 minimum per bucket) on the game set in use. On 250 games today that admits the fifteen
prop markets except triples (too few records that resolve), and every game market except the
first-inning total (25 records). The reliability and skill table (§5.1) is reported for every
market regardless.

### 3.4 What the composite does not do

It does not weigh a market by the money it could return; that is the closing-line-value question
the ruling of 2026-09-08 defers. It does not reward a market that is accurate but uninformative
(a probability that equals the base rate on every record scores the coin flip's Brier); the skill
table (§5.1) reports discrimination separately so a flat model is visible even when its Brier is
fine.

---

## 4. The weights: the full inventory

Every value below is a draw weight or a bandwidth in production today. "Search" is the range the
offline fit scans (powers and bandwidths in log steps; ON / OFF for switches). "Markets" are the
ones the weight can reach at all; the screen (§6.1) confirms per weight.

| draw | weight | today | how it was fitted | search | markets it can reach |
|---|---|---|---|---|---|
| pitch thrown (⑤a) | pitcher similarity power `SIM_PITCH_PITCHER_POWER` | 16 (1 until 2026-09-14) | pool conditionals (the split's fit) | 1, 2, 4, 8, 16, 32 | strikeouts, walks*, earned runs, hits allowed*, moneyline, totals |
| | batter similarity power `SIM_ACTOR_POWER_BATTER` | 1 | pool conditionals | 0.5, 1, 2, 4 | hits, total bases, singles, doubles, home runs, runs |
| | situation bandwidth (outs, runners, inning, margin) | 2.0 | code default | 1, 2, 4 | totals, first-five, first-inning |
| | fatigue: times-through bandwidth `SIM_FATIGUE_TTO_SIGMA` | 0.5 | owner decision (fitted 0.7) | OFF, 0.35, 0.5, 0.7, 1.0 | strikeouts, earned runs, totals |
| | fatigue: pitch-count bandwidth `SIM_FATIGUE_PC_SIGMA` | OFF | measured: imports selection | OFF only | — |
| pitch's result (⑤b) | the split switch `SIM_PITCH_RESULT_SPLIT` | ON (2026-09-14, owner decision) | fitted 16/16/8 on pool conditionals; the 250-game accuracy read; no hold-out | OFF, ON | strikeouts, moneyline, home runs |
| | pitch-to-pitch bandwidth `SIM_RESULT_PITCH_SIGMA` | 1.0 | pool conditionals | 0.5, 0.7, 1.0, 1.4, 2.0 | strikeouts, walks*, the in-play mix |
| | density correction power `SIM_RESULT_DENSITY_POWER` | 1.0 | pool conditionals | 0, 0.5, 1.0 | the in-play mix |
| | pitcher power in the result draw `SIM_RESULT_PITCHER_POWER` | 16 | pool conditionals | 4, 8, 16, 32 | strikeouts |
| | batter power in the result draw `SIM_RESULT_BATTER_POWER` | 8 | pool conditionals | 2, 4, 8, 16 | hits, total bases, home runs |
| | catcher receiving ratio `SIM_CATCHER_RECEIVING` | OFF | built; enable = SIM-526 | OFF only here | walks*, strikeouts |
| fielding (⑥) | born-ball bandwidth `SIM_BB_BORN_SIGMA` | 1.0 | pool conditionals | 0.5, 0.7, 1.0, 1.4 | hits, doubles, home runs, runs, totals |
| | born-ball density power `SIM_BB_BORN_DENSITY_POWER` | 1.0 | pool conditionals | 0, 1 | the hit mix |
| | batter power `SIM_BB_BATTER_POWER` | 4 | pool conditionals | 1, 2, 4, 8 | hits, total bases, home runs |
| | fielder power (per position) `SIM_ACTOR_POWER_FIELDER` | 1.2 | pool conditionals; concentration limit at 2 | 0, 1, 1.2, 1.6 | hits, runs, totals |
| | park bandwidth (wall zone) `SIM_PARK_KERNEL_SIGMA` | 0.02 | SIM-476 | 0.01, 0.02, 0.05 | home runs, totals |
| | pitcher-hand platoon (opposite-hand rows ×) | 0.6 | SIM-413 default | 0.4, 0.6, 0.8, 1.0 | hits, home runs |
| | class filter `SIM_BB_CLASS_FILTER`, fence stage | ON | the redesign | ON only | — |
| steal (④) | steal-runner power | 12 | pool conditionals | 4, 8, 12, 20 | stolen bases, runs |
| | pitcher-against-the-run power | 12 | pool conditionals | 4, 8, 12, 20 | stolen bases |
| | catcher-throwing power | 2 | pool conditionals | 1, 2, 4 | stolen bases |
| | margin bandwidth | 2.0 | code default | 1, 2, 4 | stolen bases |
| | manager aggression (the batting manager's rate over the league mean, on attempted rows) | 1 (a weight, not a power) | measured rates | as is | stolen bases |
| extra bases (⑦) | advancement-runner power | 20 | pool conditionals | 8, 12, 20, 32 | runs, RBI, totals |
| | situation bandwidth | 1.0 | code default | 0.5, 1, 2 | runs, totals |
| pitching change (①) | manager-usage power `SIM_ACTOR_POWER_MANAGER_USAGE` | 4 | the managers' pull depth by tercile | 2, 4, 8 | strikeouts, outs*, earned runs, moneyline |
| | situation bandwidth `SIM_CHANGE_SIT_SIGMA` | 1.0 | fitted 2026-09-13 | 0.5, 1, 2 | strikeouts, earned runs |
| | cell minimum `SIM_CHANGE_MIN_CELL` | 20 | census | 20 only | — |
| reliever (②) | role bandwidth `SIM_RELIEF_ROLE_SIGMA` | 0.1 | the pool's entering arms | 0.05, 0.1, 0.2 | earned runs, totals, first-five |
| | rest bandwidth `SIM_RELIEF_REST_SIGMA` | 0.5 | the pool's entering arms | 0.25, 0.5, 1 | earned runs, totals |
| | pitched-in-two-days weight `SIM_RELIEF_PITCHED2D_OFF_WEIGHT` | 0.25 | the pool's entering arms | 0.1, 0.25, 0.5 | totals |
| | pitches-in-three-days bandwidth `SIM_RELIEF_PITCHES3D_SIGMA` | 10 | the pool's entering arms | 5, 10, 20 | totals |
| | stuff power `SIM_RELIEF_PITCHER_POWER`, hand weight | OFF / 1.0 | measured: no gain | OFF, 1, 2 / 1.0, 0.5 | earned runs, totals |
| every draw | recency (power / per-season decay / floor) | 2.0 / ×0.75 / 0.25 | SIM-076 | 1, 2, 3 on the power | every market |
| pitch draw | cell minimum `SIM_PITCH_MIN_CELL` | 20 | census (SIM-451) | 20 only | — |

\* Walks, outs recorded and hits allowed have no 2024 lines at the book; they are graded by the
event-label reader for the record but do not enter the composite.

Thirty-odd numbers. Not all are worth a search: the offline fit's first pass (§6.2) reads each
weight's sensitivity — how much the score moves across its range with the others at production —
and the designed experiment takes the six with the largest, plus any pair the offline surface
shows interacting. The rest keep their values and are re-read only when their neighbours move.

---

## 5. The instruments

### 5.1 The market skill table — `scripts/sim548_market_skill.py` (half a day)

Extends the pairing script's read. Per market, per arm: the Brier score, the market's, the coin
flip's; the skill (coin flip minus arm); the bias (mean probability minus outcome rate); the
spread; the AUC (the chance a real "yes" drew the higher probability); the correlation of the
probability with the outcome; the reliability by bin (mean probability against the outcome
rate in six bins). Plus the composite Z of §3 with its bootstrap range, and the guard check.
Reads two or more reports and prints one table; writes JSON. The reads above (§2) were computed
by hand from the paired records; this script makes them a one-line command.

### 5.2 The offline joint fit — `scripts/sim548_offline_fit.py` (the core; about a week for all five draws, the pitch draws in two days)

**The idea.** Replay real plays one at a time through the simulator's own draw machinery — the
same code, the same cells, the same widening, the same weights — without playing games. For each
real play, size every candidate row's ticket under a weight setting, read the whole jar by
outcome (the share of total weight on each outcome is the draw's probability of it), and score
the real outcome. Average over plays; one number per setting; every setting in a grid; the grid
table shows the best cell and whether the best value of one weight moves with another.

**Fidelity rule.** The script drives a `FullPoolSampler` built by the production factory over the
production bundle with the production environment, and calls the sampler's own entry points
(`new_half_inning`, `new_plate_appearance`, the result-draw weighting, `battedball_draw`'s
candidate assembly, `steal_draw`'s, the advancement draw's, `pitching_change_draw`'s) with the
real play's state. It reads the sampler's assembled weights; it re-implements nothing. A
re-implementation would fit a different model. The point-in-time cutoff (`set_asof`, the same
rule the backtest applies) excludes every row from the play's own date onward, so the play's own
row and its game never score themselves.

**The test sets (2024 for the fit; 2025 for the offline hold-out; both cheap).**

| draw | the test plays | what is scored (the outcome categories) | how many |
|---|---|---|---|
| pitch thrown (⑤a) | pitches thrown by starting pitchers in games with a strikeout line, sampled across dates, counts and pitchers | the pitch's type and zone (the pitch draw's own answer) | 20,000 |
| pitch's result (⑤b) | the same pitches | ball, called strike, whiff, foul, in play, hit by pitch — scored two ways: with the real pitch as the anchor (clean, cheap), and through ⑤a's own mixture on a 5,000-pitch subsample (the chain; measures the ⑤a→⑤b interaction) | 20,000 / 5,000 |
| fielding (⑥) | real balls in play with a born ball (exit velocity, launch angle, spray, distance) | the fielding outcome: out, single, double, triple, home run, error | 20,000 |
| steal (④) | real steal opportunities from the opportunity pool | stayed, went and safe, went and caught, picked off | 20,000 |
| extra bases (⑦) | real advancement opportunities on balls in play | held, took the extra base, thrown out | 10,000 |
| pitching change (①) | real plate-appearance starts from the change opportunity pool, with the real manager | changed, stayed | 20,000 |
| reliever (②) | real changes with the real pen listing | which arm entered (the pen's rows) | 5,000 |

The single-draw production path (the split OFF) scores ⑤a's row on the result categories, so the
same pitches grade both configurations.

**The score.** The Brier score over the outcome categories (the sum of squared gaps between the
probability of each category and its 0/1 truth). Proper, so the true probabilities minimize its
expectation; the same rule the comparison uses on the markets.

**The grid.** Per draw, every combination of its weights from §4's search columns, at the other
draws' production values (the draws are separate jars, so a weight in one draw does not change
another draw's jar; the chain ⑤a→⑤b is the exception and is scored as such). The pitch draws'
grid is the largest (about 6 × 4 × 3 × 5 for ⑤a and 5 × 3 × 4 × 4 for ⑤b, times the switch);
at seconds per setting it is an hour. The steal and advancement grids are minutes.

**The outputs.** Per draw: the grid table (score per setting with a bootstrap range over plays);
the sensitivity of each weight (the score's swing across its range); the interaction read (for
each pair, the best value of one at each value of the other); the top three settings; the
effective-sample share at each setting (the starvation guard); the 2025 offline hold-out score
of the top three. Per draw, one JSON and one text report.

**What it cannot see.** The game loop: how pitches chain into plate appearances and innings, the
manager's decisions in sequence, the runners' state, the iteration noise of a 100-game-sim
probability. The offline score ranks candidates; the full simulator confirms them.

### 5.3 The designed experiment runner — `scripts/sim548_design.py` (one day)

Takes a list of arms (each a dict of environment settings), a game set, the iteration count and
the worker count; runs each arm through `scripts/clv_backtest.py` in sequence inside one
container (the app down, or a larger VM); pairs every arm against the design's baseline arm;
computes the per-market reads and the composite; fits a quadratic response surface (main
effects, two-way interactions, curvature) per market and for the composite, with bootstrap
ranges on every coefficient; prints the interaction terms that clear their ranges. Resumable
(an arm whose report exists is skipped). Writes the design, the reports, the fitted surface and
a one-page summary.

### 5.4 The calibration-layer fitter — `scripts/sim548_calibrate_markets.py` (§7; one day)

Fits the per-market probability map on one season's paired records, validates it on another,
writes it into `/data/calibration.json` beside the win-probability curve, and reports the
before/after Brier, bias and reliability per market.

---

## 6. The procedure

### 6.1 Phase 1 — the screen (one day of probes, in parallel with the build)

For each weight in §4, one probe arm at the extreme of its search range against production (45
games × 40 iterations, 35 minutes; `sim427_manager_probe.py`'s prediction-shift read): does it
move any market probability at all? A weight that moves nothing (the fatigue weight's +0.000 ±
0.012 was such a read) is recorded and dropped from the search. The screen also reads the
effective-sample share at the extreme (the starvation guard; a share under 0.1 caps the range).

### 6.2 Phase 2 — the offline joint fit (one day of compute, one day of reading)

Run §5.2 for every draw on 2024. Read: the sensitivity ranking; the interaction tables (the
owner's question, answered draw by draw); the top three settings per draw and their 2025
offline hold-out score. Choose the **design set**: the six weights with the largest sensitivity,
plus any pair whose interaction read moves the best value of one across the other's range.
Expected members, from what we know: the pitch draw's pitcher power, the split switch and its
batter power, the pitch-to-pitch bandwidth, the fielding draw's batter power and born-ball
bandwidth, the manager-usage power. Stamped in this document before Phase 3 starts.

### 6.3 Phase 3 — the designed experiment on the full simulator (three to four days of compute)

**The game set (owner decision 3).** A fresh 250 games of 2024 — the next 250 Final games by
game id after the first 250, which have now judged three flips. Every arm runs on the same
games, seeds and bundle; the baseline (production) is one of the arms.

**The design.** For six weights at two levels each — the offline fit's best value and
production's — a 16-arm fractional factorial (every main effect clear of the two-way
interactions; the two-way interactions aliased in pairs, which the offline surface breaks) plus
four centre points (production, repeated with different seeds, which measures the noise floor
directly): 20 arms. At 2 h 30 to 3 h 55 each, 60 to 75 hours. If a two-way interaction clears its
range and its alias cannot be resolved from the offline surface, a 16-arm fold-over follows
(another 50 to 60 hours) — decision point 5.

**The read.** Per market and for the composite: the main effect of each weight with its range;
the interaction terms; the surface's best setting inside the searched box; the guards at that
setting. The pool-totals bands on the best setting (one lane, 45 × 130, six hours).

**The choice.** Among settings that clear the guards, the one with the best composite Z; among
settings whose ranges overlap the best, the least sharpening (the smallest powers, the widest
bandwidths — the standing habit since SIM-476).

### 6.4 Phase 4 — the hold-out (seven hours)

The chosen setting and production, on the first 250 Final games of 2025 with closing strikeout
lines, 100 iterations, the same bundle. The choice stands if the composite Z keeps its sign with
its range clear of zero, the primary market of the cycle (strikeouts, this cycle) keeps its
sign, and no guard worsens beyond its range on both seasons in the same direction. Otherwise the
2024 read is recorded as noise, the setting is not landed, and the next candidate from Phase 3
gets its own hold-out (a candidate per seven hours; at most three per cycle).

### 6.5 Phase 5 — the flip and the calibration refit (one day)

One commit, the standing pattern: the values into the compose file and the lane's
`PRODUCTION_FLAGS` (the compose-equals-lane tests hold them together; the unit lane stays pinned
OFF); the cheat sheet's values; `CHANGES.md` with the tables; the stamp here; the board line. Then
§7: refit the calibration layer on the new production (2024 fit, 2025 check), write it, restart
the app, take the warm `/simulate` timing.

### 6.6 Phase 6 — the standing procedure

After the first cycle the instruments make a re-fit cheap. The offline fit re-runs when the
pool window rolls a season or a new factor lands (a day); the designed experiment re-runs only
for the weights the offline read moves (a few arms, not twenty); the calibration layer refits
with every flip. The cycle's cadence is the owner's — quarterly is the natural one; the pool
window changes once a year.

---

## 7. The calibration layer, and why not per-market weights

**What exists.** The win-probability map is a fitted reliability curve applied at boot (SIM-407/432).
The prop markets have a validation read (`make validate-props`: expected calibration error per
prop) but no applied correction: the over/under probability of a prop is read straight from the
simulated distribution. The strikeout-prop calibration ticket (SIM-429) and the split's
strikeout-shortfall ticket (SIM-527) were both CLOSED on 2026-09-10 when the pool-totals metric
stopped deciding; the shortfall has now reappeared under the accuracy comparison as the
6.5-point low bias, and this layer is the work that answers it — under SIM-548, no new ticket.

**What it is.** Per market, a map from the simulator's probability p to a calibrated p′. Two
candidates: a two-parameter logistic map (p′ = the logistic of a + b·logit p — a shift and a
scale; five hundred records fit it) and isotonic regression (a monotone step map; needs
thousands). The pitcher markets get the logistic map (482 strikeout records on 250 games; two
thousand on a season), the batter markets either. Fitted on 2024's paired records of the
current production, validated on 2025 (the before/after Brier, bias and reliability), stored in
`/data/calibration.json` beside the win-probability curve, applied where the API prices the
market (`prop_distributions` for the props, `game_market_distributions` for the segment markets,
the win-probability map as today). The accuracy comparison reports both the raw and the
calibrated probability, so a weight change is still judged on the raw one (the layer must not
hide a weight's effect).

**The rule.** The layer corrects probabilities, never plays. The simulated game, its box score and
its play-by-play are untouched; the calibrated number is what the betting surface shows. The
layer is refitted after every weight change and never fitted on the same season it is graded on.

**Why not per-market weight sets.** Three reasons, in the order they bite:

1. *One game, many stories.* A strikeout model and a total model produce different games for
   the same matchup: a starter projected for nine strikeouts by one and a total priced as if he
   struck out five by the other. Bets across the markets of one game — and any correlated or
   same-game combination — need one coherent game. The game page would show two.
2. *Thirty simulators.* Fifteen prop markets and fifteen game markets: thirty weight sets,
   thirty times the compute on a live slate, thirty hold-outs, thirty ways to overfit.
3. *The physics.* The weights say how baseball works — how much a pitcher's look-alikes tell you
   about the next pitch. Real information helps every market at once; the record so far agrees
   (the manager draw improved strikeouts and the moneyline together; the split improved
   strikeouts, the moneyline and home-run discrimination together and left the batter props
   flat). A conflict between markets comes through the noise budget — sharpening on one factor
   thins the jar for every channel — and the composite objective prices that trade-off.

The per-market need that is real — a market whose probabilities are informative but shifted or
over-spread — is exactly what the calibration layer fixes at a fraction of the cost.

---

## 8. Traps, pre-armed

- **Choosing noise.** 250 games resolve about 0.010 of Brier on strikeouts; twenty arms on one
  game set will show a best one by chance. The centre points measure the noise floor; the
  hold-out is the defence; nothing lands on a 2024 read alone.
- **Forty rows at once.** One guard at two standard errors on one season is expected. Both
  seasons, same market, same direction is the rule.
- **The interaction alias.** A 16-arm fractional design confounds two-way interactions in
  pairs. The offline surface says which member of a pair is live; when it cannot, the fold-over
  (decision 5) resolves it. Do not read an aliased interaction as one weight's.
- **Starving the jar.** Every power thins the effective rows; the powers multiply. The
  effective-sample share is reported per draw per setting, offline and in the arms; a setting
  under 0.1 on any draw needs the minimum-cell rule re-read before it lands.
- **The offline fit's blind spots.** It cannot see the game loop, the manager in sequence, or
  iteration noise. Treat its ranking as a proposal; the full simulator decides. If the offline
  best and the full-simulator best disagree, the full simulator wins and the disagreement is
  recorded (it says the game loop matters for that weight).
- **Fidelity drift.** The offline script must call the sampler's entry points; a unit test
  compares its jar for one play against the sampler's own draw weights for the same state
  (bit-identical), so a sampler change cannot leave the offline fit scoring a different model.
- **The inputs move under a run.** No rebuild, recompute or `make calibrate` during a set of
  arms; the pairing script refuses non-twins. After `make calibrate`, write the reliability curve
  back at once.
- **The compute budget.** The arms need the VM. With the app up, five workers do not fit
  (§9, decision 6). A stopped app during a three-day design is an operational cost the owner
  must accept, or the VM must grow.
- **The 2024 game set has judged three flips.** The design runs on a fresh 250; the hold-out on
  2025. The first 250 stay as the record they are.
- **The calibration layer hiding a weight.** The comparison reports raw and calibrated
  probabilities; weights are judged on raw. A layer refit is never bundled into a weight arm.
- **The fatigue weight.** It is in the inventory at the owner's landed value. If the offline fit
  or the design proposes another value (including OFF), that is a decision for the owner, with
  the numbers, not a silent change.

---

## 9. Sequence, cost and the owner's decision points

| step | what | compute | elapsed |
|---|---|---|---|
| 0 | the owner reviews this plan; decisions 1–4 | — | — |
| 1 | build §5.1 (skill table) and §5.3 (design runner) | — | 1.5 days |
| 2 | build §5.2 for the pitch draws; the fidelity test | — | 2 days |
| 3 | Phase 1 screens (in parallel with 2) | 15 probes × 35 min | 1 day |
| 4 | Phase 2 offline fit, pitch draws; read; choose the design set | 2 h | 1 day |
| 5 | build §5.2 for the fielding, steal, extra-bases, change and reliever draws | — | 3 days |
| 6 | Phase 2 offline fit, the other draws; read; confirm the design set | 2 h | 1 day |
| 7 | Phase 3: 20 arms on a fresh 250 of 2024 | 60–75 h | 3–4 days |
| 8 | the pool-totals lane on the best setting | 6 h | 0.5 day |
| 9 | Phase 4: the 2025 hold-out | 7 h | 0.5 day |
| 10 | Phase 5: the flip; build §5.4; the calibration refit; the records | 2 h | 1 day |
| — | optional: the fold-over (decision 5); a second hold-out candidate | 50–60 h / 7 h | 3 days / 0.5 day |

About three weeks elapsed, of which nine to twelve days are unattended compute.

**The owner's decisions, in the order they arrive.**

1. **The market weights in the composite** (§3): equal per market, or stake-weighted — and if the
   latter, the bet mix.
2. **The guard rule** (§3): "no market worse beyond its range on both seasons in the same
   direction" — or a stricter one.
3. **The design's game set** (§6.3): the next 250 games of 2024 (recommended), or the first 250
   again.
4. **The scope of the search** (§4): every weight (recommended, with the screen dropping the
   inert ones), or the pitch draws alone first.
5. **The fold-over** (§6.3): run it only if an aliased interaction clears its range (recommended),
   always, or never.
6. **The compute arrangement** (§1, §8): the app down for the design's three to four days
   (recommended — the slate is not live yet), four workers with the app up (about 25% slower),
   or raise the Docker VM's memory to 12 GiB in Docker Desktop's settings (the host has 15.5 GiB)
   and run five workers with the app up.
7. **The calibration map's form** (§7): the two-parameter logistic map for every market
   (recommended for the first cycle), or isotonic where the records allow.
8. **The landing of any changed value that the owner set by decision** (the fatigue bandwidth):
   with the numbers, when they arrive.

---

## 10. The definition of done

- The composite objective is implemented and reported on every pairing, with its range.
- The offline joint fit exists for all five draws, passes the fidelity test, and has produced a
  grid, a sensitivity ranking and an interaction table per draw on 2024, with the top three
  settings scored on 2025 offline.
- The designed experiment has run on a fresh 250-game 2024 set with centre points; its fitted
  surface (main effects, interactions, curvature) is recorded per market and for the composite.
- The chosen setting has cleared the 2025 hold-out under §3's rules, or every candidate's
  failure is recorded and production is unchanged.
- The pool-totals lane on the chosen setting is recorded.
- The chosen setting is in production (compose = lane; the cheat sheet; `CHANGES.md`; the
  board), and the app runs it with its warm timing recorded.
- The calibration layer is fitted on 2024, validated on 2025, applied to every priced market and
  reported before/after; the accuracy comparison reports raw and calibrated probabilities.
- The standing procedure (§6.6) is written into `docs/technical/scripts-frontend.md` with the
  commands, and this document carries every stamp.
- The owner's eight decisions are recorded here with their dates.


---

## 11. Stamps

- **2026-09-15 04:00 UTC — execution starts (owner instruction).** The eight decisions of §9 run
  on the recommended options. The Docker VM now has 16 CPUs and 19.5 GiB (was 6 / 9.7): arms no
  longer need the app down; decision 6 is moot. The 1,000-game baseline of production
  (`CHANGES.md` 2026-09-15) is the starting table for §3.
- **§5.1 built 2026-09-14, §5.4 / §5.2 (the pitch draws) / §5.3 built 2026-09-15**
  (`scripts/sim548_market_skill.py`, `sim548_calibrate_markets.py`, `sim548_offline_fit.py`,
  `sim548_design.py`; `tests/unit/test_sim548_instruments.py`). `FullPoolSampler.result_weights`
  factored out for the fidelity rule. The backtest records `sim_prob_raw` on the moneyline.
- **The calibration layer, first read (§7; fitted on games 1–500 of 2024, read on 501–1000):**
  the strikeout map (a +0.08, b 0.15) takes the market from 0.0183 behind the line to level
  (−0.0017 [−0.0053, +0.0019]); the batter props pass with b 0.4–0.7; the totals collapse to the
  base rate and do not pass; the moneyline needs raw probabilities (the next run). Not applied
  to production: the 2025 check is owed, and the layer refits after the weights land.
- **Phase 2 (the offline fit, the pitch draws) launched 06:22 UTC** — 8,000 pitches, 78 settings.
  First read: at production the draws use 2% (pitch) and 1% (result) of the jar's rows; the
  single-draw jar scores 0.819 at pitcher power 16 and 0.756 at power 1 (the same pitches).
- **The pool-totals lane on today's production launched 04:04 UTC** (the realism guard; the
  configuration was never graded as one).
- **Filed on the way:** the first-five run line is two separate bets priced as one by the
  segment scorer (SIM-549); the team-total lines read as suspect (the line's own AUC 0.46–0.48).
- **2026-09-15 16:00–17:00 UTC — the review and the corrections.** A Docker restart killed both
  overnight runs; the lane was relaunched (16:05 UTC) and the offline fit rewritten as v2 with a
  checkpoint (16:38 UTC). The instruments were reviewed by a four-reviewer / three-refuter
  workflow (`docs/audit/2026-09-15-sim548-instruments-review.md`) and corrected the same day
  (`CHANGES.md` 2026-09-15): the design runner's game-set, repeat-seed and alias-group defects;
  the calibration fitter's Newton and isotonic defects; the composite's range; `SIM_SIT_SIGMA`.
  The review's design verdict amends §5.2 and §6.3: the offline fit scores plate appearances
  (the count chain) and starts (the sum of P(strikeout) against real strikeouts) beside pitches,
  because the per-pitch Brier is variance-dominated and cannot see between-pitcher
  discrimination; the designed experiment runs FOUR factors as a full 2^4 (the pitch draw's
  pitcher power 16 / 4, the result draw's pitcher power 16 / 4, the result draw's batter power
  8 / 2, the fatigue bandwidth 0.5 / off) with three baseline repeats (seeds strided by the
  iteration count), two true centre arms and two supplementary split-OFF arms — not six at
  resolution IV. The v1 partial read (57 settings): per pitch every ladder prefers its flattest
  value (pitcher power 1–4 ≈ 0.756 against 0.819 at 16; the result draw best at 4 / 2 = 0.755
  against 0.862 at 16 / 8; fatigue off 0.797 against 0.819), the best batter value the same at
  every pitcher value, the costs compounding. The v2 run adds the levels that decide.
- **2026-09-16 02:20 UTC — Phase 2 read (the offline fit v2, the pitch draws;
  `scripts/sim548_offline_v2_20260915.{txt,json}`).** 120 starts of 2024 (10,578 pitches, 2,644
  plate appearances, 89 pitchers), 78 settings, every one replayed through the production
  sampler. Three levels, three answers:
  1. *Per pitch and per plate appearance, flatter wins everywhere.* The pitch draw's 6-outcome
     Brier reads 0.814 at production's pitcher power 16, 0.773 at 8, 0.762 at 4 and 0.759 at 1;
     the result draw reads 0.862 at 16 / 8, 0.812 at 8 / 8, 0.771 at 8 / 2 and 0.758 at 4 / 2;
     fatigue off reads 0.789 / 0.831 against 0.814 / 0.862 at 0.5 (0.35 is the worst bandwidth
     on every measure). The plate-appearance 4-way Brier agrees (production 0.483 / 0.504; the
     best result setting 0.461). The effective row counts explain it: production's draws rest
     on 45 rows (pitch) and 17 rows (result) of about 700 admissible; power 8 lifts them to 137
     and 34; the flattest settings to 300 and 170.
  2. *Between pitchers, sharp wins.* The per-pitcher strikeout share against the season's own
     share (the strikeout market's signal) reads a correlation of +0.19 with a spread ratio of
     0.66 at pitch pitcher power 1 — the pitcher-blind defect that opened this ticket — +0.34 /
     0.71 at 4, +0.42 / 0.84 at 8, +0.47 / 1.14 at 16 and +0.48 / 1.48 at 32. The spread ratio
     crosses 1.0 (neither compressed nor exaggerated) between powers 8 and 16. The result draw
     reads the same shape: +0.31 / 0.95 at pitcher 4, +0.41 / 1.05 at 8, +0.44 / 1.31 at 16;
     batter 2 raises it to +0.49 / 1.24 (8 / 2: +0.46 / 0.91).
  3. *Per start, the read is flat within its noise.* The sum of P(strikeout) against the real
     strikeout count correlates +0.42 at production, +0.43–0.44 at pitch pitcher power 1–8, +0.41
     at 32; the result draw's pitcher power 8 lifts it to +0.49 (+0.44 at 8 / 4, +0.43 at 8 / 2);
     fatigue off +0.47. With 120 starts the standard error is about 0.08, so none of these
     separate. The spread ratio of the start-level sums (their standard deviation over the real
     counts') sits at 0.31–0.46 everywhere; a perfect forecast sits below 1.0 there because the
     real count carries its own noise, so this ratio ranks settings and does not grade them.
  *Interactions:* at the pitch and plate-appearance levels the best value of one power is the
  same at every level of the other (pitch pitcher power 1 at every batter power; result pitcher
  power 4 at every result batter power), so the ranking does not depend on the partner. At the
  start level the best value moves with the partner, but inside the noise. The offline fit
  therefore cannot pick the levels on its own: the per-pitch score and the between-pitcher
  score pull in opposite directions, and the start-level score that would arbitrate is
  underpowered. It does confirm the design: the region where discrimination is right (powers
  8–16) and the region where the per-pitch score is right (1–8) meet at the design's centre
  arms (8 / 8 / 4), and the design's low levels (4 / 4 / 2) are the flattest settings the
  discrimination read still tolerates. Fatigue off is the one setting better on every level.
  The full-simulator design (running; 21 arms at ten workers, about 3.4 hours each) decides.
  The 2025 hold-out (`--holdout-season 2025 --top 3`) and the profile-season leak check
  (`--profile-season 2023`) follow the design, when the memory is free.
- **2026-09-16 04:42 UTC — PAUSED by owner decision.** The owner runs this fit once the model
  is in its final state and works on other tickets first. The design container was stopped at
  run 3 of 21 (runs 1 and 2 are complete and kept: `scripts/sim548_design_20260915/run01.json`,
  `run02.json`; the partial run 3 is discarded). The app is back up (04:44 UTC, healthy;
  calibration applied, the reliability curve present). Everything committed (`5f6a0ef`).
  **State preserved for the resume:** the bundle is unchanged since the two finished runs
  (`bundle_provenance` in each report carries its file times — a resume must compare them);
  the offline fit's checkpoint (`sim548_offline_v2_20260915.npz`) and its read (§11 above);
  the calibration-layer read; the 1,000-game production baseline
  (`scripts/sim548_baseline_1000_skill.txt`); the fresh design game set
  (`scripts/sim548_games_2024_design.txt`, games 1001–1250 — unseen by every fit so far;
  keep it unseen).
  **The resume order (decided 2026-09-16 with the owner's pool-window question):**
  1. The pool-window test first (about one day): export a second bundle with a ten-season
     window beside the current one (`last_n_seasons(n=10)` in `engine_artifacts.py`; the
     pool tables hold all ten seasons; the actor matrices must cover the added
     pitcher-seasons); rerun `sim548_offline_fit.py` on the same 120 starts against both
     bundles across the pitcher-power ladder; add the row-age read (at equal similarity, do
     the 2017–2019 rows predict 2024 pitches worse than the 2023 rows?). The recency decay
     (2.0 / ×0.75 per season / floor 0.25, `player_profile_computor.recency_weight`, set by
     hand in May and never fitted) joins the weight inventory of §4 if the window widens.
     Reason: the accuracy comparison's leak guard leaves a 2024 game about 1.4 seasons of
     rows against live production's 3.7, so the starvation read is worse than live; a wider
     window also moves the best powers up, which changes what the design should run on.
  2. Then the design on the chosen pool (`scripts/sim548_design_run.sh`; if the pool is
     unchanged, runs 1–2 stand and the script skips them; if it changed, delete the folder
     and start clean — never mix bundles in one design).
  3. Then the rest of §6 unchanged: the analysis, the split-OFF pair, the pool-totals lane,
     the 2025 hold-out, the flip decision, the calibration-layer refit and its app wiring,
     the stale win-probability curve decision.
  **What the pause does not change:** production runs the split at 16 / 16 / 8 and fatigue
  0.5 (both owner decisions); the strikeout market sits 0.022 behind the line with the line's
  discrimination (the 1,000-game read); the app does not read the `market_calibration` key.
  **Warning for the other tickets in the meantime:** any change to the pool, the bundle,
  the profiles, `calibration.json` or the draw code before the resume invalidates runs 1–2
  and the offline read's absolute numbers (their shape survives). That is expected — the
  owner wants the fit on the final model — but the resume must then start every measurement
  clean rather than reuse these.
