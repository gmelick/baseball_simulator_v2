# Finding — why the steal draw's look-alike weights lower the number of steal attempts, and a fix

> **STATUS 2026-10-01 — PROPOSED, FILED as SIM-556 (P3) by the owner. No production code is
> changed.** Decision 2 of §6 is taken (a ticket of its own, P3); decision 1 (build it) is open.
> **Committed to `master` 2026-10-07:** this finding, the probe, its tests and its four records
> (they sat uncommitted in a worktree until then; the 10 tests pass).

**Date:** 2026-09-30
**Ticket:** SIM-556 (P3), filed 2026-10-01 in `BACKLOG.xlsx`.
**Instrument:** `scripts/steal_weight_volume_probe.py` and its tests,
`tests/unit/test_steal_weight_volume_probe.py` (10). The probe imports the running-game replay of
the running game on the pitch (SIM-554, `scripts/sim554_running_game_replay.py`, on `master`) and reads the draw through the same
seam, `FullPoolSampler.steal_weights`. It plays no games. Its copy of the production rule matches
`steal_weights` to 1e-8 on 500 pitches a season, and it reproduces the replay's four reads
(1.524, 1.225, 1.137, 1.338).
**Records:** `scripts/steal_weight_volume_probe_2026.txt` / `.json` and `..._2025.txt` / `.json`.

---

## 0. The short version

**The terms.** The steal draw picks one row from the steal opportunity pool (one row per real
pitch on which a steal was possible). A *look-alike weight* is a score from 0 to 1 for how much
the row's player resembles the live player. The draw has three: the runner, the pitcher's hold,
the catcher's arm. *Mass* is the share of the draw's total weight that a group of rows holds.

**The cause.** A look-alike weight does not keep each row's mass. Runners who steal often are
few, and they resemble few other runners. Their rows score low for most live runners. The extra
weight those rows get when a base stealer is the live runner does not pay that back. The rows of
the most active third of runners hold 33% of the draw's weight and 71% of its attempts. At
production's weights they receive 29% of the weight. That one shift is 96% of the loss.

**The size.** On 74,296 real pitches of 2026 the draw with no look-alike weight reads 1.524
steals per 100 pitches. Production (every power 1) reads 1.225: 20% under. The runner weight
carries three quarters of the loss (−14.7% alone), the pitcher weight a fifth (−4.3%), the
catcher weight the rest (−1.2%). The 2025 pitches read the same (−17.4%).

**What it is not.** It is not the rows with no score: the runner has none on 0.2% of rows, and
setting every such row neutral moves the volume by 0.4%. It is not the engines' sample-confidence
multiplier: removing it moves the volume by 0.2%.

**A second finding.** The draw's other soft weight, the bell curve on the score margin, does the
same thing the other way. It adds 4% to the attempts. Production's level is the two errors
together.

**The fix I recommend.** Give every row one more weight, the *balance weight*. It is one number
per player-season. The fit makes each player's rows receive, over a season of draws, the mass
they hold. It is a draw weight on rows. The fit reads the score matrix and the pool's row counts.
It never reads the `attempted` flag, so it is not a calibration constant. Balance the
score-margin curve the same way.

**What the fix does on the replay** (every power 1; "the pool's rate" is the draw with no soft
weight, 1.462 in 2026 and 1.484 in 2025):

| | 2026: steals per 100 | against the pool's rate | who steals | 2025: steals per 100 | against the pool's rate | who steals |
|---|---:|---:|---:|---:|---:|---:|
| real | 1.423 | −2.7% | | 1.464 | −1.4% | |
| production today | 1.225 | −16.2% | 0.710 | 1.280 | −13.7% | 0.763 |
| with both balances | 1.460 | −0.2% | 0.743 | 1.486 | +0.2% | 0.804 |

"Who steals" is the correlation between each runner's expected steals and his real ones. It
covers the runners with at least 150 opportunity pitches (417 in 2026, 434 in 2025). The fix
keeps it and raises it a little. The odds per pitch score better too (§4). The volume then holds
at every power tried. So the later fit of the powers can sharpen who steals without moving how
many.

---

## 1. The evidence

All numbers are steals per 100 opportunity pitches on the replay's own sample. The sample is
every pitcher-game of the season: every steal pitch, and one in three of the others (which then
weigh three). The point-in-time cutoff is the day before each game. 2026: 74,296 pitches, 3,083
recorded steals. 2025: 89,881 pitches, 3,835 steals.

### 1.1 Which weight carries the loss

| The draw's look-alike weights | 2026 | against no weight | 2025 | against no weight |
|---|---:|---:|---:|---:|
| none | 1.524 | | 1.549 | |
| the runner alone, power 1 | 1.299 | −14.7% | 1.337 | −13.7% |
| the pitcher alone, power 1 | 1.458 | −4.3% | 1.501 | −3.1% |
| the catcher alone, power 1 | 1.505 | −1.2% | 1.536 | −0.9% |
| all three, power 1 (production) | 1.225 | −19.6% | 1.280 | −17.4% |
| all three, 4 / 4 / 2 | 1.137 | −25.4% | 1.234 | −20.3% |
| all three, 12 / 12 / 2 | 1.338 | −12.2% | 1.418 | −8.5% |

The three losses multiply: 0.853 × 0.957 × 0.988 = 0.806, against 0.804 measured.

### 1.2 Why: the attempted rows score lower

The mean score on the rows where the runner went, against the mean on the other rows (2026; the
2025 ratios are 0.84, 0.97 and 0.99):

| Weight | Attempted rows | Other rows | Ratio |
|---|---:|---:|---:|
| runner | 0.374 | 0.446 | 0.84 |
| pitcher | 0.459 | 0.481 | 0.95 |
| catcher | 0.498 | 0.502 | 0.99 |

A weight that is 16% lower on the attempted rows removes about 16% of the attempts. That is the
whole arithmetic.

### 1.3 Where the mass goes

The draw's mass by the pool row's runner, on the 2026 pitches. The tiers are thirds of
runner-seasons by attempts per opportunity pitch (tier 3 runs most):

| | Tier 1 | Tier 2 | Tier 3 | Under 100 pitches |
|---|---:|---:|---:|---:|
| share of mass, no look-alike weight | 30.8% | 33.8% | 33.0% | 2.4% |
| share of mass, production | 32.4% | 36.9% | 28.8% | 1.9% |
| steals per 100 carried, no look-alike weight | 0.061 | 0.340 | 1.084 | 0.038 |
| steals per 100 carried, production | 0.060 | 0.349 | 0.796 | 0.020 |

Tier 3's rows lose 0.288 of the 0.299 lost.

### 1.4 The closed form: a property of the weight, not of the draw's cells

Replay the pool through ONE weight alone, with no count cells. Each player-season is the live
player in turn, as often as the pool holds his rows. The attempts the draw then predicts, over
the attempts the pool holds:

| Weight | Power 1, second base | third base | Power 4, second | third | Power 12, second | third |
|---|---:|---:|---:|---:|---:|---:|
| runner | 0.883 | 0.805 | 0.865 | 0.763 | 0.982 | 0.968 |
| pitcher | 0.972 | 0.960 | 0.964 | 0.949 | 0.984 | 0.972 |
| catcher | 0.999 | 0.991 | 0.999 | 0.987 | 0.996 | 0.988 |

This uses only the score matrix and each player's rows and attempts. The loss is zero at no
contrast (a very low power) and at full contrast (a very high power, where a runner draws only
his own rows), and largest between. That is the dip the replay saw: 1.524, 1.225, 1.137, then
1.338.

In a simulated game only ONE season's players are live. With only 2026's player-seasons live, the
three weights and the margin curve together keep 0.860 of the pool's attempts at second base and
0.755 at third (−14% and −25%). With only 2025's live they keep 0.882 and 0.781 (−12% and
−22%). The acceptance lanes of 2026-09-21 and 2026-09-22 read −9.1% and −11.2% at second, −17.9%
and −18.9% at third (`scripts/sim478_lane.txt`).

### 1.5 What was ruled out

- **Rows with no score (suspect 1).** Production gives such a row 1.0, the top weight, at power 1.
  The runner has no score on 0.19% of rows, the pitcher on 3.9%, the catcher on 2.8%. For the
  pitcher and the catcher the share is nearly the same on attempted rows and on the others. With every
  such row at the mean scored weight, production reads 1.230 against 1.225 (2025: 1.284 against
  1.280). The rule is worth making the same at every power. It is not the cause.
- **The sample-confidence multiplier.** The runner and pitcher engines multiply every pair's score
  by the square root of the smaller of the two profiles' confidence. A row whose runner had few
  chances then scores low against everyone. But rows of runners under 50 chances are 4% of rows,
  and their attempt rate is the pool's (2.0 to 2.3 per 100 at second, against 2.15 for the
  regulars). With the multiplier removed production reads 1.228.
- **The draw's structure.** The replay showed the same drop with the same weights on the pitch
  pool's rows (§11.5 of the running-game plan).

---

## 2. The second finding: the score-margin curve adds attempts

The draw also weighs a row by a bell curve on the score margin, 2 runs wide: a row counts more
when its margin is near the live one. That is a look-alike weight on the situation. Rows with the
batting team far behind hold few attempts at second base (0.2 per 100 at five runs behind,
against 2.5 in a tied game and 3.1 to 3.3 at two or three runs ahead), and those rows lose mass.
The closed form, inside every count cell: the curve alone predicts 1.042 of the pool's attempts
at second base and 1.033 at third. On the real pitches the draw with the curve and no look-alike
weight reads 1.524 (2026) and 1.549 (2025); with the curve off it reads 1.462 and 1.484. The
curve adds 4.2% and 4.4%.

The curve carries signal: with no look-alike weight, the per-pitch score of §4 is 0.7 worse with
the curve off than with it balanced, in both seasons. So the fix balances it and keeps it.

Production's level is therefore two errors of opposite sign: the look-alike weights at −20% and
the margin curve at +4%.

---

## 3. The recommended change

### 3.1 The balance weight on the three look-alike weights

For one weight at one power, let n(y) be the mass of player-season y's rows in the pool, K(x, y)
the score of x against y raised to the power, and L the player-seasons that can be live (the
season being simulated). The balance weight d(y) satisfies, for every y:

    sum over x in L of  n(x) · K(x,y) d(y) n(y) / [ sum over y' of K(x,y') d(y') n(y') ]  =  share(L) · n(y)

In words: every live player takes his turn, as often as the pool holds his rows. The rows of y
then receive their own share of the mass. The draw weighs a row by
`score ^ power × d(row's player)`. The fit is the standard alternating rescale (Sinkhorn). It
takes 9 steps at power 1 and 130 to 190 at power 12: well under a second at power 1.

- **It follows the architecture rule.** It is a draw weight on rows, and it reads no outcome. The
  pitch-result draw and the born-ball draw already carry its one-step form (the inverse-density
  weight).
- **It holds at any power.** In both seasons the volume sits within 0.7% of the pool's rate at
  1 / 1 / 1, 4 / 4 / 2 and 12 / 12 / 2 (§4). Today volume and "who steals" are tied: the best
  per-pitch score is at 4 / 4 / 2, where the volume is 25% low.
- **The live season matters.** Fitted with every player-season live (the pool replayed through
  itself), the balance reads 5% low on the 2026 pitches and exact on 2025's. The closed form
  predicts both numbers: 2026's player-seasons are not a typical sample of the pool. Fitted for
  the season being simulated, over the rows that season can draw, it is exact for that season.
- **Where it goes.** `simulation/full_pool_sampler.py`: `_steal_meta` gains each matrix's column
  per row and the mass per column; a new `_steal_balance(name, target, season)` fits d at the
  weight's current power, cached; `_steal_actor_factor` multiplies it in. A row with no score
  weighs the mean scored weight at every power (today the rule skips power 1). One switch,
  `SIM_STEAL_BALANCE`.
- **Early in a season** the live season holds few rows. The build must then add the season before
  to the live set. I did not test the threshold.
- **A standing check.** The closed form of §1.4 costs seconds. Print it in the nightly matrix
  build beside the concentration report. Fail a strict build when a weight keeps less than 0.98
  or more than 1.02 of the pool's attempts.

### 3.2 The same balance on the score-margin curve

Eleven weights (one per margin, −5 to 5) in each count cell, fitted the same way inside
`steal_weights`.

### 3.3 Alternatives not taken

| Alternative | Volume | Who steals | Why not |
|---|---|---|---|
| Rescale the attempted rows in every draw so their mass is unchanged (the catcher receiving ratio's form) | exact | lost (correlation −0.20) | the look-alike weights then decide only safe or caught |
| One step of the balance (divide by each player's mean score; the existing density weight's form) | the runner overshoots by 3% to 18% in the closed form | kept | not exact, and worse as the power rises to 4 |
| A constant on the attempted rows | right at one setting | kept | a hand-tuned number; wrong again when a power moves |
| Turn the runner weight off | near right | lost | |
| Leave it to the fit of the powers | — | — | the fit would buy volume with power: 12 / 12 / 2 is 12% low, and its per-pitch score is the worst of the four |

---

## 4. What the change does on the replay

"Against the pool's rate" compares with the draw that has no soft weight (1.462 in 2026, 1.484 in
2025). "Who steals" is as in §0. The per-pitch score is the Brier score of (no steal / stolen base
/ caught) in 1/10,000, against production today, lower is better, ± one standard error from
resampling pitcher-games.

| | 2026: per 100 | against the pool's rate | who steals | per-pitch score | 2025: per 100 | against the pool's rate | who steals | per-pitch score |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| production, 1 / 1 / 1 | 1.225 | −16.2% | 0.710 | 0 | 1.280 | −13.7% | 0.763 | 0 |
| the balance on the three weights | 1.521 | +4.0% | 0.746 | −1.25 ± 0.09 | 1.548 | +4.3% | 0.806 | −1.32 ± 0.08 |
| and the margin curve balanced | 1.460 | −0.2% | 0.743 | −1.23 ± 0.08 | 1.486 | +0.2% | 0.804 | −1.31 ± 0.07 |
| production, 4 / 4 / 2 | 1.137 | −22.3% | 0.845 | −2.83 ± 0.18 | 1.234 | −16.8% | 0.903 | −3.55 ± 0.23 |
| both balances, 4 / 4 / 2 | 1.451 | −0.7% | 0.892 | −4.34 ± 0.37 | 1.478 | −0.4% | 0.925 | −4.64 ± 0.44 |
| production, 12 / 12 / 2 | 1.338 | −8.5% | 0.926 | +21.58 ± 2.00 | 1.418 | −4.4% | 0.922 | +30.07 ± 2.04 |
| both balances, 12 / 12 / 2 | 1.464 | +0.2% | 0.933 | +9.88 ± 1.48 | 1.478 | −0.4% | 0.929 | +15.10 ± 1.56 |

- **Volume.** With both balances the draw returns the pool's rate at every power tried.
- **Who steals.** The correlation rises at every power. The spread between runners stays too
  narrow at power 1: the most active third is predicted at 1.78 per 100 against a real 3.02, the
  least active at 1.19 against 0.19. That is the power's job.
- **The per-pitch score** improves at every power. At 12 / 12 / 2 it stays worse than production
  at power 1; the balance halves that loss.
- **The safe share** rises 1.5 points: 0.790 to 0.805 in 2026 (real 0.788), 0.795 to 0.810 in
  2025 (real 0.798). The lane's band on it has a 3% floor.
- **Against the real rate** the fixed draw reads +2.6% in 2026 and +1.5% in 2025. The pool's
  four seasons ran more than either season alone, most at third base (0.50, 0.46, 0.44 and 0.39
  steals per 100 in 2023 to 2026). That is the pool window's question, not the weights'.

### 4.1 A stronger runner weight alone (the owner's question, 2026-10-01)

Steals per 100 opportunity pitches on the 2026 pitches, by the live runner's tier (thirds of
runners by real attempts; tier 3 runs most):

| | Tier 1 | Tier 2 | Tier 3 | against the pool's rate | per-pitch score |
|---|---:|---:|---:|---:|---:|
| real | 0.19 | 0.97 | 3.02 | | |
| no look-alike weight, no margin curve | 1.50 | 1.47 | 1.41 | 0 | +3.05 |
| production, 1 / 1 / 1 | 1.03 | 1.15 | 1.45 | −16.2% | 0 |
| 4 / 4 / 2, no balance | 0.65 | 0.90 | 1.77 | −22.3% | −2.83 |
| 12 / 12 / 2, no balance | 0.47 | 0.86 | 2.55 | −8.5% | +21.58 |
| both balances, 1 / 1 / 1 | 1.19 | 1.35 | 1.78 | −0.2% | −1.23 |
| both balances, 4 / 4 / 2 | 0.77 | 1.10 | 2.36 | −0.7% | −4.34 |
| both balances, 12 / 12 / 2 | 0.51 | 0.95 | 2.80 | +0.2% | +9.88 |

A stronger weight tells runners apart, as it should. It does not repair the total: the total
falls further at 4 / 4 / 2 and is still 8.5% low at 12 / 12 / 2, where each runner draws almost
only his own rows and the odds per pitch are the worst of all. At power 1 the balance lifts
every tier, because power 1 barely tells runners apart. Against the draw with no weight it is a
transfer: tier 1 goes down (1.50 to 1.19) and tier 3 goes up (1.41 to 1.78). At 4 / 4 / 2 the
balance adds 0.12 to tier 1 and 0.59 to tier 3. The two changes do different jobs: the power
separates runners, the balance holds the total. The best row tested is both balances at
4 / 4 / 2. The grid moved all three weights together; the runner weight alone at 4 with the
balance was not tested.

---

## 5. Limits

- **No lane ran.** The closed form reads one to seven points under the lanes' two red bands
  (§1.4), with the same sign and order. The fix returns the pool's rate on the replay. I expect
  both bands to pass. Only a 45 × 130 lane certifies it.
- **The replay leaves the batting manager's steal weight neutral**, as the running-game replay
  does. The player profiles are the season's, not point-in-time.
- **Other draws were not tested.** Every draw with a look-alike weight has this property: the
  pitch draw (pitcher, batter), the fielding draw, the advancement draws, the pitching change. The
  lane's other reds (walks +4.0%, hit by pitch +6.7%) are candidates. The closed form of §1.4 is
  the test: a few seconds a weight.
- **The balance weights grow with the power.** At power 1 they run from 0.74 to 3.8 (1st to 99th
  percentile). At power 4 the 99th percentile is 41, at power 12 it is 94. The build needs a cap,
  as the two existing density weights have (20). I did not test a cap.

---

## 6. Decisions for the owner

1. **Build both balances for the steal draw (§3.1 and §3.2)?** I recommend yes. They land ON with
   the cheap gates (the unit and regression lanes, a ten-game smoke of the steal channels), then
   one lane for the two steal bands.
2. **Where does the work live?** TAKEN 2026-10-01: the owner filed it as a ticket of its own,
   SIM-556, at P3. My recommendation was to build it ahead of the fit of the powers (SIM-548,
   paused), with the closed-form check of the other draws as its second step. The balance is a
   repair that holds at any power, so the fit should run on top of it.
