# SIM-548 — fit the draw weights against the sim-vs-closing-line accuracy comparison, the strikeout market first: the plan (2026-09-14)

> **Design superseded 2026-09-14 (same day) by the joint-fit plan,**
> `docs/audit/2026-09-14-sim548-joint-fit-plan.md` — one model, every draw, every market:
> a composite objective, an offline joint fit, a designed experiment on the full simulator,
> the hold-out, and a per-market calibration layer. That document is a DRAFT for the owner's
> review. This file stays as the record of the finding (§2) and of Part A4's read (the split
> arm, stamped under Part A); its one-weight ladder (§3, Parts A–D) is no longer the design.


**Owner summary.** None of the simulator's weights was ever chosen to make its probabilities
more accurate against what happens in real games. Every weight was fitted so that the
simulator's frequencies, split by the thing the weight is about, land on the play pool's own
frequencies — a fit to the *structure of the data*, not to *prediction accuracy* — and then
certified on the pool-totals bands. The betting-line comparison you made the judge on
2026-09-12 has so far judged two changes (the manager draw: better; the fatigue weight: flat).
It has fitted nothing.

Reading that comparison closely tonight shows where the model is blind. On the starter
strikeout market the simulator's probability of clearing the line has no bias (it says 48.6%
on average and 48.5% of overs happen) but **no information**: when it says 16% the over
happens 56% of the time, when it says 88% it happens 46%. Its Brier score (the squared error
of a probability against the 0-or-1 outcome) is 0.281 against 0.245 for the closing line and
0.250 for a coin flip — the simulator's strikeout probabilities are worse than saying 50%
every time. The reason is measurable: across 89 starter-games the simulator's projected
strikeouts per start correlate +0.07 with the real count and +0.05 with the pitcher's own
season strikeout rate, where the closing line correlates +0.45 and the season rate +0.54.
**The simulator's strikeout projection does not know who is pitching.** The redesign found
the cause on 2026-09-09 and recorded it: at the fitted power of 1 the pitcher's similarity
factor in the pitch draw is nearly flat, so every starter draws a league-average mix of
pitches. The stronger fit it built (the pitch / pitch-result split at pitcher power 16) was
turned off because it moved a pool-totals band — the metric you have since retired.

The batter side is a different picture: on hits and total bases the simulator's probability
IS informative (when it says 27% the hit happens 27%; at 69%, 62%), it beats a coin flip, and
it trails the line by a fraction of a point with a small upward bias. So the plan is not "tune
everything against the lines". It is: **use the accuracy comparison as the selection
criterion for the few weights that can move a market, one ladder at a time, checked on a
held-out season, starting with the pitcher's weight in the pitch draw — because that is where
the model is blind and the market is not.**

The cost on this machine is real and stated below: one candidate arm is 2.5 hours (250 games
× 100 iterations on the five workers the Docker VM allows), a five-step ladder is 12.5 hours,
the hold-out check five more. The first cycle — the pitcher weight — is about two days of
compute; the batter side another day; the game markets after that.

---

## 1. The rules this plan works under

- **The architecture rule (owner, 2026-08-10 / 08-29):** every decision is a
  similarity-weighted draw from a hard-filtered pool; the drawn row is the play; every factor
  is a weight or OFF. This plan changes no mechanism. It changes how a weight's VALUE is
  chosen: by the accuracy comparison instead of by the pool's own conditionals.
- **The judge (owner, 2026-09-12):** the sim-vs-closing-line accuracy comparison
  (`scripts/clv_backtest.py`, SIM-538) and its paired read (`scripts/sim518_pair_accuracy.py`).
  The pool-totals bands stay as a *diagnostic* — they tell us what a weight did to the game's
  shape — but they no longer decide anything (the SIM-527 / SIM-429 closure of 2026-09-10).
- **The target is reality, not the market.** The comparison scores the simulator's
  probability against the outcome. The market's score sits beside it as the yardstick. A
  weight is chosen because it lowers the simulator's error against outcomes, never because it
  moves the simulator toward the market's number — a model that copies the market can never
  beat it.
- **Held out.** Every weight is chosen on 2024 games and confirmed on 2025 games it never saw.
  A value that wins on 2024 and loses on 2025 is noise and is not landed.
- **One weight at a time, on a frozen bundle.** The pairing script refuses reports whose
  bundle, calibration file, seed, iterations or game list differ; no artifact rebuild and no
  `make calibrate` runs during a ladder (and `make calibrate` must be followed by the
  reliability-curve write-back — the trap of 2026-09-13).
- **No golden files (owner, 2026-09-10).** The reads are outcome tests on real games.

## 2. The starting state, verified 2026-09-13/14

**Production** (the flip of 2026-09-13): the manager draw at power 4, the real pen, the
reliever draw; the pitch draw at pitcher power 1 (`SIM_PITCH_PITCHER_POWER`) and batter power
1 (`SIM_ACTOR_POWER_BATTER`) through the cell index; the split OFF; fatigue OFF; the
calibration curve restored.

**The accuracy comparison on the first 250 Final games of 2024, 100 iterations, production
as it stands** (`scripts/sim427_accuracy_on.json`). Brier score, lower is better; "skill" =
the coin flip's Brier (the outcome variance) minus the arm's, positive = better than a coin flip:

| market | n | sim Brier | market Brier | coin flip | sim skill | market skill | sim bias (p − outcome) | sim spread | market spread |
|---|---|---|---|---|---|---|---|---|---|
| starter strikeouts | 482 | 0.281 | 0.245 | 0.250 | **−0.031** | +0.005 | 0.000 | 0.170 | 0.053 |
| hits | 4,421 | 0.238 | 0.234 | 0.247 | +0.009 | +0.013 | +0.039 | 0.131 | 0.097 |
| total bases | 4,423 | 0.239 | 0.235 | 0.242 | +0.004 | +0.007 | +0.041 | 0.115 | 0.090 |
| home runs | 4,309 | 0.098 | 0.096 | 0.097 | −0.001 | +0.001 | +0.019 | 0.037 | 0.048 |
| moneyline | 250 | 0.248 | 0.244 | 0.250 | +0.002 | +0.006 | −0.039 | 0.037 | 0.080 |
| game total | 242 | 0.256 | 0.250 | 0.250 | −0.006 | −0.001 | +0.024 | 0.087 | 0.018 |

Read across a row: strikeouts have no bias and a wide spread that means nothing (the
reliability table in the summary); hits and total bases have real skill and a 4-point upward
bias; the moneyline is under-spread (the sim is less sure of the winner than the market, and
should not be); the total is over-spread and uninformative.

**The pitcher-blindness, on the balanced 45 games** (89 starter-games, 40 iterations,
`scripts/sim427_probe_on.json` against `raw.game_player_stats`): the simulator's projected
strikeouts per start have a standard deviation of 0.52 across starters against 2.57 for the
real counts; correlation with the real count +0.07, with the pitcher's own season strikeout
rate +0.05; the closing line's correlation with the real count +0.45; the season rate's +0.54.
Outs recorded: correlation −0.10. The manager draw fixed how LONG the average starter pitches,
not WHICH starter strikes people out.

**Where the pitcher enters the pitch draw.** `FullPoolSampler._f_pitcher`: the live pitcher's
similarity to each pool row's pitcher (the pitcher engine's score) raised to
`pitch_pitcher_power`; at power 1 the redesign's part F read every identity factor flat (the
scores cluster near the median 0.5). The split (`SIM_PITCH_RESULT_SPLIT`) draws the pitch at
pitcher power 16 and its result at 16 / batter 8, fitted on the pool's own per-pitcher
conditionals on 2026-09-09, OFF since because it read strikeouts per plate appearance −2.4%
on the pool-totals lane.

**Compute.** The Docker VM has 6 CPUs and 9.7 GiB; five backtest workers of ~1.2 GB each.
Production runs a 2024 game in 35 s of wall-clock across the five (100 iterations); 250 games
= 2 h 30. A probe arm (45 games × 40) is 35 minutes. The odds re-load for 2025 was still
writing on 2026-09-13; the hold-out set needs it finished (Part C).

## 3. The design on one page

For each candidate weight, in this order:

1. **The screen (35 minutes).** The manager probe's prediction-shift read
   (`scripts/sim427_manager_probe.py`, or the fatigue probe's) at the ladder's extreme value
   against production: the change in the starter's strikeout mean and in the probability of
   clearing the closing line, per starter-game. A weight that moves no market probability
   cannot move the accuracy read (the fatigue weight: +0.000 ± 0.012) and is not laddered.
   The screen also reads the new **discrimination** number: the correlation of the
   simulator's projected strikeouts with the real count over the 89 starter-games (today
   +0.07; the line's +0.45).
2. **The ladder on 2024 (2.5 h per arm).** Each value of the weight runs as one paired arm
   of `scripts/clv_backtest.py` on the fixed 250-game 2024 list (the first 250 Final games by
   game id, 150 dates, `--max-games 250`), 100 iterations, seed 0, the frozen bundle. The
   production report is the baseline for every arm. The pairing reads, per market, the
   paired Brier difference with its game-clustered range, plus the per-market skill table
   (bias, spread, reliability by bin) from a new small script (§4, Part 0).
3. **The choice.** On the market the weight is for (strikeouts for the pitcher weight), take
   the value with the best paired Brier improvement; among values whose ranges overlap the
   best, take the smallest power / largest bandwidth (the SIM-476 habit — the least sharpening
   that does the work). Every other market must read not-worse (its range covers zero). The
   pool-totals bands are read for the record, not for the decision.
4. **The hold-out (5 h once per cycle).** The chosen value and production run on 250 games
   of 2025 the ladder never saw. The choice stands if the sign of the strikeout-market
   improvement holds and no market is worse beyond its range. If it does not hold, the value
   is not landed and the ladder's read is recorded as noise.
5. **The flip.** The value into the compose file and the lane's `PRODUCTION_FLAGS` (the
   compose-equals-lane test holds them together), `CHANGES.md`, the plan's stamp, the board.

The numbers that bound the reads (from the 250-game runs so far): the per-record Brier
difference on the strikeout market has a spread of about 0.11, so 250 games (about 480
starter records) resolve a 10-point calibration change at two standard errors and a Brier
difference of about 0.010; the effect we are hunting is the market's whole 0.036 lead. Two
first-five markets moving at two standard errors in opposite directions in the fatigue read
is the reminder that thirty rows read at once will always show one — the choice is made on
the named market, the rest are guards.

## 4. The parts

### Part 0 — the instrument (half a day)

`scripts/sim548_market_skill.py`: reads one or two accuracy reports and prints, per market,
the table of §2 (n, the sim's and the market's Brier, the coin-flip Brier, the two skills, the
sim's bias, the two spreads, the correlation of the sim's probability with the market's, and
the reliability table by sim-probability bin); with two reports it adds the paired columns
the pairing script already prints. Also into the manager probe's `report`: the
discrimination read (the correlation of the sim's projected strikeouts and outs with the real
counts, and with the pitcher's own season rate) — the numbers of §2 computed by hand tonight.
Tests: the arithmetic on a hand-made report. This part costs no simulation.

### Part A — the pitcher's weight in the pitch draw (the first cycle; ~15 h of compute)

1. **Screen** `SIM_PITCH_PITCHER_POWER` at 16 against production: the strikeout shift and the
   discrimination read. Expected: the correlation of projected with real strikeouts rises well
   above +0.07 — if it does not, the pitcher score itself carries no strikeout information and
   the plan moves to A3 before any ladder.
2. **Ladder** the power over 1 (production), 2, 4, 8, 16 — four new arms, 10 hours — and
   read the strikeout market first, then hits / total bases / home runs / the moneyline / the
   total as guards. Record for each arm the effective-sample share of the pitch cell (the
   starvation guard the change draw already reports; add the same counter to the pitch draw)
   and the pool-totals bands (the diagnostic).
3. **The results-aware pitcher score** (the SIM-527 proposal): if the pitcher engine's score
   does not carry strikeout information at any power, the engine's composite is the limit,
   not the power. Then the pitch draw's pitcher factor takes a second term — the pitcher's own
   strikeout and walk rates against the row pitcher's (a results similarity, from the profiles
   the engine already reads) — as one more weight with its own power, and the ladder runs on
   that. This is the only mechanism change in the plan, and it stays inside the rule: a
   similarity weight on the draw.
4. **The split** (`SIM_PITCH_RESULT_SPLIT` at 16 / 16 / 8) as one more arm beside the ladder:
   it was built for exactly this and judged by the retired metric.
5. **Hold-out** (Part C) and **flip**.

Gate: on 2024 the strikeout market's paired Brier improves beyond its range and no guard
market worsens beyond its range; on 2025 the sign holds. The discrimination read is reported
with it (the aim is a correlation with the real count in the region of the line's +0.45; the
plan does not promise a number).

> **A4 RAN 2026-09-14 (05:38–09:33 UTC, 3 h 55 — the split's second draw costs ~60% over
> production's 2 h 30).** `scripts/sim548_accuracy_split.sh`: the split ON at 16 / 16 / 8
> (bandwidth 1.0, density 1.0) over production as the compose file set it that morning — the
> owner landed the fatigue weight at 0.5 the same day, so the baseline is the fatigue-0.5
> arm's own report (`scripts/sim518_accuracy_tto05.json`: the same 250 games, seeds, bundle,
> fatigue). Pairing `scripts/sim548_accuracy_pair_split.{txt,json}` (39,709 records). Brier,
> split minus baseline, negative = the split more accurate, the game-clustered range:
>
> | market | n | dBrier | range | baseline vs market | split vs market |
> |---|---|---|---|---|---|
> | **starter strikeouts** | 482 | **−0.0155** | **[−0.0258, −0.0050]** | +0.0324 | +0.0168 |
> | — starts under 75 pitches | 75 | −0.0454 | [−0.0655, −0.0232] | +0.0287 | −0.0166 |
> | — starts of 75-99 | 327 | −0.0083 | [−0.0219, +0.0044] | +0.0191 | +0.0108 |
> | — starts of 100+ | 80 | −0.0171 | [−0.0447, +0.0080] | +0.0899 | +0.0728 |
> | earned runs | 470 | +0.0004 | [−0.0063, +0.0074] | −0.0062 | −0.0059 |
> | hits / total bases / home runs | 4,4xx | −0.0001 / +0.0001 / −0.0001 | ranges cover zero | | |
> | moneyline | 250 | −0.0050 | [−0.0119, +0.0021] | +0.0078 | +0.0029 |
> | run line | 250 | −0.0030 | [−0.0123, +0.0063] | | |
> | game total | 242 | −0.0004 | [−0.0108, +0.0096] | | |
> | first-five total | 244 | **+0.0113** | **[+0.0010, +0.0224]** | −0.0024 | +0.0089 |
> | first-five moneyline | 250 | −0.0056 | [−0.0160, +0.0053] | | |
> | every record pooled | 39,709 | −0.0002 | [−0.0012, +0.0009] | | |
>
> *The skill read (the Part 0 table, computed inline; `scripts/sim548_market_skill.py` is
> still to write):* on strikeouts the baseline's probabilities carry no information (AUC
> 0.510 — the chance a real over drew the higher probability; the coin flip is 0.500; the
> closing line 0.584) and are over-spread (0.175; the line 0.053); the split's carry some
> (AUC 0.548; correlation with the outcome +0.088 against +0.017; the line's +0.139), are
> less over-spread (0.144), and are **biased low by 6.5 points** (the mean P(over) 6.5 points
> under the over rate; the baseline −1.9) — the split's strikeout shortfall (SIM-527) shows
> here as a bias, and the Brier gain comes despite it. The reliability by bin turns monotone
> from the 0.2 bin up (0.28 → 0.43, 0.42 → 0.46, 0.55 → 0.53, 0.69 → 0.65; the baseline read
> 0.16 → 0.63 and 0.89 → 0.62). **The moneyline becomes informative too:** AUC 0.494 → 0.578
> (correlation +0.014 → +0.160), spread 0.033 → 0.050 (the line 0.080), Brier 0.252 → 0.247
> against the line's 0.244. Home runs: AUC 0.546 → 0.576. Hits / total bases: unchanged
> (the batter's power in the result draw does not move them).
>
> **Against the gate.** The strikeout market improves beyond its range (three standard
> errors). One guard worsens beyond its range: the first-five total (+0.0113, two standard
> errors). That guard's baseline is the outlier of the four reports on this game set — its
> beat over the market reads −0.0024 where production before fatigue read +0.0094, the 0.7
> arm +0.0027 and the split +0.0089 — so the split's first-five total sits where production
> before fatigue sat, and the "worsening" is the 0.5 arm's luck on that row (§5 of the fatigue
> plan flagged it as one of three two-standard-error rows among forty). The plan's own rule
> for this case is the hold-out: **Part C on 2025 decides** (2,438 Final games of 2025 carry a
> closing strikeout line — the re-load is past 2025). The next arms: the 2025 baseline
> (production, fatigue 0.5; 2 h 30) and the 2025 split arm (3 h 55).
>
> **FLIPPED ON 2026-09-14 (later the same day) by owner decision, without the 2025 hold-out.**
> The compose file and the lane carry the split at 16 / 16 / 8; the strikeout probabilities'
> 6.5-point low bias goes to the calibration layer (the joint-fit plan, §7). The owner also
> ordered the 1,000-game accuracy baseline of the flipped production (`CHANGES.md` 2026-09-14).
>
> **What A4 says about A1-A3.** The split raises the pitcher's power to 16 in both draws and
> the batter's to 8 in the result draw at once; it is one point of the (p, b) surface the
> owner asked about (the interaction question, 2026-09-14), not a ladder. The discrimination
> it buys (AUC 0.55 against the line's 0.58) says the pitcher engine's score does carry
> strikeout information when its power is raised — A3's premise ("the composite is the
> limit") is not yet proven; A2's ladder over `SIM_PITCH_PITCHER_POWER` alone, and the grid on
> (pitch pitcher power × result batter power), are what separate the pitcher's contribution
> from the split's other parts. The 6.5-point low bias is the results-aware score's case
> (A3) or a calibration-layer question — recorded, not tuned here.

### Part B — the batter's weight in the pitch draw and the fielding draw (the second cycle; ~13 h)

Hits and total bases carry skill and a +4-point bias; home runs carry none. Ladder
`SIM_ACTOR_POWER_BATTER` (1 today) over 0.5, 1, 2, 4 and `SIM_BB_BATTER_POWER` (4 today) over
2, 4, 8, reading hits / total bases / home runs / singles / doubles first; the moneyline and
total as guards. The bias is read separately from the skill: a weight that sharpens but keeps
the +4 points is not the whole answer, and a bias that survives every power is a pool-level
question (the hits-per-opportunity band) to be recorded, not tuned here.

### Part C — the 2025 hold-out set (5 h per cycle)

The first 250 Final games of 2025 by game id with closing lines loaded (the 2025 re-load must
have finished; verify the count of games with strikeout lines ≥ 250 before the first
hold-out). Production runs on it once (the baseline every cycle reuses while the bundle is
frozen); each cycle's chosen value runs on it once.

### Part D — the game markets (the third cycle; sized after A and B)

The moneyline is under-spread (the sim less sure than the market of a winner it calls
correctly 60% of the time at 50%) and the total is over-spread and uninformative. Both are
downstream of the same pitch draw, so A and B move them first; what remains is read after
those cycles and, if a weight is implicated (the park kernel, the home weight, the fielding
draw's born-ball bandwidth), it gets the same ladder-and-hold-out treatment.

### Part E — the runbook

`docs/technical/scripts-frontend.md` gains the procedure (screen → ladder → hold-out →
flip, the commands, the frozen-bundle rule, the calibration-curve write-back); the compose
comments cite the accuracy read that chose each value; `CLAUDE.md` §2b says the weights'
fits are now chosen by the accuracy comparison and confirmed on a held-out season.

## 5. Traps

- **Choosing noise.** 250 games resolve about 0.010 of Brier on the strikeout market; a
  ladder of five will show a best value by chance if the true effect is small. The hold-out is
  the only defence; a value that does not hold on 2025 is not landed, however good 2024 looked.
- **Thirty rows at once.** The guards are read as "worse beyond its range", and one guard at
  two standard errors among thirty is expected; two guards in the same direction on the same
  segment on both seasons is not.
- **Starving the cell.** A high pitcher power concentrates the pitch draw on a few rows of the
  cell; the effective-sample share must be reported per arm, and a value that lands with a
  share under 0.1 needs the minimum-cell rule re-read (SIM-451's census).
- **The bundle and the calibration file move under the run.** No `--what` rebuild, no
  recompute, no `make calibrate` during a ladder; the pairing script's provenance check is
  the stop, not the operator's memory. `make calibrate` drops the reliability curve — write it
  back from `/data/prop_validation.json` at once, or the moneyline reads uncalibrated.
- **The odds tables change under the hold-out.** The 2025 re-load must be finished before the
  2025 baseline runs; a price that differs between two arms makes the pairing refuse.
- **The market as the target.** Every read is against outcomes. The correlation of the
  simulator's probability with the market's (0.22 on strikeouts, 0.82 on hits) is reported as
  a description, never optimised.
- **Compute.** Two days per cycle on this VM at five workers. Giving Docker Desktop more CPUs
  (the host has 32 logical cores; the VM 6) would cut it in proportion up to the ~7 workers
  the 9.7 GiB allows; more memory for the VM is the other half of that lever.

## 6. Sequence and cost

| step | compute | wall-clock |
|---|---|---|
| Part 0 — the instrument | none | half a day |
| A1 — the screen at power 16 | 35 min | same day |
| A2 — the ladder (2, 4, 8, 16) | 4 × 2.5 h | 10 h |
| A4 — the split arm | 2.5 h | 2.5 h |
| C — the 2025 baseline + A's winner | 2 × 2.5 h | 5 h |
| A — flip, docs | none | half a day |
| B — the batter ladders (7 arms) + hold-out (1 arm) | 8 × 2.5 h | 20 h |
| D — the game markets | sized after A and B | — |

The first cycle answers the question that matters most — can the simulator's strikeout
projection be made to know the pitcher, and does that beat the line's lead — in about three
days including the hold-out.

## 7. The definition of done

The pitcher's weight in the pitch draw has a value chosen by the accuracy comparison on 2024
games and confirmed on 2025 games, and production runs it; the strikeout market's paired
Brier difference against the 2026-09-13 production is negative beyond its range on both
seasons, no guard market is worse beyond its range on both, and the discrimination read is
recorded. The same for the batter's weights on the hit markets. Each landed value's compose
line cites the read that chose it; the lane's flags match; the runbook carries the procedure.
The read that says a weight CANNOT reach the market (Part A3's finding, if it comes) is a
valid end for that weight, recorded with the number.
