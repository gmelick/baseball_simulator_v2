# Which of the model's predictions can we trust? The bet-selection analysis plan (2026-09-14)

**Status: PROPOSED — a plan for the owner's review. Nothing here has run.** Ticket: not yet
filed; the next free ID is SIM-550. It is a companion to the joint-fit plan
(`docs/audit/2026-09-14-sim548-joint-fit-plan.md`), not a replacement: that plan makes the whole
model more accurate; this plan finds the SUBSET of its predictions worth betting.

---

## 0. The one-page summary

**The question.** Two questions, in the owner's words. First: does the model, as a whole, match
or beat the betting line? Second: which of its predictions are more reliable than others — and
when the model and the line disagree by a lot, who is right more often?

**The answer in one line.** The first question is the joint-fit plan's job (SIM-548). The
second question needs a different kind of work: the same accuracy data, cut by the things we
can observe about each prediction BEFORE the game — how far the model sits from the line, how
noisy its own number is, how much history the player has, which market, which side, which
line — and a test of whether the model knows anything the line does not. This document lists
every analysis that answers it, what each one needs, what it costs, and the traps.

**What the analyses produce.** A **trust ladder**: for every market, and inside each market for
every segment we can name, a grade — *bet-grade*, *watch*, or *off* — with the evidence for it
(a sample size, a confidence range, a hold-out season). Plus a **blended price**: for each
prediction, the probability that combines the model and the line by how much each has proven it
knows. The betting surface bets only where the blended price and the offered price differ.

**What already exists.** The accuracy comparison (`scripts/clv_backtest.py`) scores every market
against the closing line on real outcomes, paired, with confidence ranges. The skill table
(`scripts/sim548_market_skill.py`) adds bias, spread, discrimination and reliability per market.
A hypothetical return read exists at one edge threshold. A 1,000-game read of production on
2024 is running now.

**What is new.** (1) The per-prediction record gains the fields the cuts need (the line, the
date, the model's own noise, the pool depth, the player's history). (2) Ten analyses, A–J, from
the whole-model scoreboard to the value study. (3) A statistical-hygiene rule set, so slicing
the data forty ways does not produce forty lucky findings. (4) The trust ladder wired into the
bet-signal gate.

**Cost.** About two and a half weeks. Three and a half days of unattended compute (full-season
reads of 2024 and 2025 plus a seed twin), five days to build the record extension and the
analysis scripts, a week of reading and writing the ladder. The full-season reads double as
the joint-fit plan's hold-out baseline, so the compute is shared.

**What waits.** Every read of DOLLAR value (return on stake, closing-line value) waits on the
owner's ruling of 2026-09-08: no betting-value measurement until every band the lanes grade is
green. The split flipped ON on 2026-09-14 with a red strikeout band that the lane has not
re-graded. Analyses A–I are accuracy and reliability reads and do not wait; analysis J does.

---

## 1. The two questions, and why they need different work

**"Does the model beat the line?"** is a question about the AVERAGE prediction. The Brier
score (the squared gap between a probability and the 0/1 outcome; lower is better), paired
against the line's on the same games, answers it. That work exists and is the joint-fit plan's
objective.

**"Which predictions can we trust?"** is a question about the DISTRIBUTION of predictions. A
model can lose to the line on average and still beat it on a fifth of the records — and if we
can NAME that fifth before the game, that fifth is the product. The reverse is also true: a
model that beats the line on average may do it all on one market, or all on the easy records.

The second question has three parts, and each needs its own analysis:

1. **Where is the model better?** Cut the paired accuracy by segment (§4 D).
2. **Is the model's disagreement with the line information or noise?** When the model says 60%
   and the line says 50%, does the outcome land closer to 60% (the model knows something), to
   50% (the model is noise), or to 55% (the model knows half of what it claims)? (§4 B, C, G.)
3. **What price should we bet at?** The blend of the two probabilities that scores best out of
   sample is the price; the gap between it and the offered price is the bet (§4 F, J).

## 2. The rules this plan works under

- **Accuracy before value (owner ruling 2026-09-08).** Every analysis below scores
  probabilities against outcomes. The dollar reads (§4 J) wait for every graded band to be
  green and for the lane to re-grade the split's strikeout band.
- **A hold-out season for every finding.** A segment found on 2024 is a hypothesis; it becomes
  a finding when it holds on 2025. A segment found by looking at both is a hypothesis for 2026.
- **The point-in-time cutoff stands.** Every backtest game draws only from rows dated before
  the game (`set_asof`, the SIM-535 rule). No analysis relaxes it.
- **The drawn row is the play (the architecture rule).** Nothing in this plan changes a
  simulated event. The calibration layer and the blended price correct PROBABILITIES on the
  betting surface; the game page shows the simulated game.
- **The line is the reference side, never the model's favored side (SIM-538).** Every record is
  scored on a fixed side (home / over). The disagreement study (§4 B) is the one place that
  looks at the model's favored side, and it says so.
- **The frozen inputs.** A set of reads runs on one bundle, one calibration file, one weight
  set; the pairing script refuses non-twins. A weight change starts a new dataset.

## 3. The data: what we have, what the record lacks, what is not collected

### 3.1 What we have

| item | size | note |
|---|---|---|
| closing lines, game markets | 2021–2026, ~2,200–2,440 games a season, 11–15 markets a game | 2019–2020 the three full-game markets only |
| closing lines, prop markets | 2021–2026, 10–15 markets a game, ~2.0 M rows | walks, outs recorded and hits allowed: no 2024 offers at the book |
| opening lines | a matched opening row on part of the games | the coverage per market and season is unmeasured — the first read of §4 E measures it |
| the official box score | all ten seasons (SIM-545) | props grade on it |
| production reads | the first 250 games of 2024 (three flips judged there); the 1,000-game baseline running | 100 iterations a game |
| the pool window | 2023–2026 | a 2024 game draws from 2023 + 2024-before-date rows; a 2023 game would draw from nothing |

**The usable backtest seasons are 2024, 2025 and 2026 to date.** The pool window starts in
2023, and the cutoff excludes rows from the game's own date onward, so a 2021–2023 game has an
empty or thin pool. The odds for 2021–2023 serve one thing: the market's own behaviour (§4 E),
which needs no simulation.

**Records per season (2024, from the 250-game reads scaled by ten).** Starter strikeouts about
4,800; hits and total bases about 43,000 each; home runs about 43,000; the moneyline, total and
run line about 2,400 each; the first-five markets about 2,000 each; the first-inning total too
few (about 250).

### 3.2 What the per-prediction record lacks

`AccuracyRecord` (`scripts/clv_backtest.py:784`) carries `game_pk, market, market_type,
sim_prob, market_prob, outcome, player_id` and the two raw closing prices. Every cut in §4 needs
more. The extension (§5.1) adds:

| group | fields | used by |
|---|---|---|
| the game | date, season, home / away team, park, day / night, the two starters, the game's slate size | D, H |
| the line | the line value (5.5, 8.5), the side scored, the book's margin on the pair, the closing snapshot time | B, D, E |
| the opening line | the opening line value and both prices, when present; the number of snapshots | E, J |
| the model's number | the simulated mean and standard deviation of the stat, the push mass, the iteration count, the Monte-Carlo standard error of `sim_prob` | B, F, G |
| the model's footing | the effective-sample share of the draws that produced this player's plays (the pool depth for this pitcher or batter) | D, F, G |
| the player | hand, role (starter / lineup slot), plate appearances or batters faced to date this season (history depth), first-season flag | D, F |
| the outcome | the actual stat value, not only the 0/1 | B, D |

### 3.3 What is not collected today

- **A price at the moment the model can act.** The model needs both lineups; lineups post hours
  before first pitch; the line then moves to the close. The live cycle stores `current`
  snapshots on the prop cadence but no backtest has ever used them, and no historical
  bet-time snapshot exists. Without it, closing-line value proper cannot be measured on
  history. §4 J proposes the bet log that collects it from now on.
- **Umpires, weather, wind.** Known drivers of strikeouts and totals; not in the schema. A
  segment cut on them is impossible until they are ingested (a later ticket; not this plan).
- **Same-game parlay prices.** The book's price for correlated legs of one game. The model's
  joint distribution prices them coherently; the book's often does not. Not collected; §4 I
  says what would be needed.

## 4. The analyses

Each analysis states the question, the method in plain words, the output, the decision it
informs, the sample it needs and the trap. The letters are names, not an order; §7 gives the
order.

### A. The whole-model scoreboard, plus WHY it scores what it scores

**Question.** Per market: does the model beat the line, and if not, is the loss a calibration
problem (fixable by a map) or an information problem (needs the weights)?

**Method.** The paired Brier and log-loss reads exist. Add the **Brier decomposition**: every
Brier score splits into three parts — *reliability* (how far the stated probabilities sit from
the observed rates; the calibration error), *resolution* (how much the probabilities separate
the outcomes; the information), and *uncertainty* (the base rate's own variance; the same for
both forecasters). Compute the three parts for the model and for the line, per market, per
season.

**Output.** A table: per market, the model's and the line's reliability and resolution terms,
with bootstrap ranges. A market where the model's resolution matches the line's but its
reliability is worse is a **calibration-layer** job. A market where the model's resolution is
lower is a **weights** job (the joint fit). A market where the model's resolution is HIGHER
than the line's and its reliability is worse is the best case: real information, badly scaled.

**Informs.** Which markets go to the calibration layer first; which go to the fit; which are
dead. The strikeout market today reads: resolution below the line's, reliability worse (the
6.5-point low bias plus over-spread). Hits reads: resolution near the line's, a 4-point bias.

**Needs.** The existing 250-game reads today; the full-season reads for ranges tight enough to
rank markets.

**Trap.** The decomposition depends on the bin width; report it at two widths (six bins and
ten) and confirm the ranking does not move.

### B. The disagreement study — "when they disagree, who is right?"

**Question.** When the model's probability and the line's differ by a lot, does the outcome side
with the model, the line, or somewhere between?

**Method.** Three reads, from the simplest to the most useful.

1. *The favorite call.* On records where the model and the line favor OPPOSITE sides (one above
   50%, one below), count whose favorite won. This is the owner's question in its plainest
   form. It is also the weakest read: it throws away the size of the disagreement and the
   records where both favor the same side.
2. *The realization curve.* Bucket every record by the disagreement `d = sim_prob −
   market_prob` (signed; in bins of 0–2, 2–5, 5–10, 10–15, 15+ points, each sign). Per bucket:
   the records, the games, the outcome rate, the model's Brier, the line's Brier, and the
   **excess win rate on the model's side**: for each record, 1 if the outcome landed on the
   side the model favored, minus the LINE's probability of that side. Under "the line is
   right" this averages zero; under "the model is right" it averages the disagreement itself.
   The **realization slope** — the regression of the excess on the disagreement — is the one
   number: 1.0 means the model's disagreement is fully real; 0 means it is noise; 0.4 means the
   model knows 40% of what it claims; negative means the model is wrong when it disagrees.
3. *Signed and sided.* The curve above split by the DIRECTION of the disagreement (the model
   above the line versus below) and by the market's side (over / under; favorite / underdog;
   home / away). A model that is right when it says "over" and wrong when it says "under" is a
   common shape (a low bias makes every "under" call look confident).

**Output.** Per market: the favorite-call count; the realization curve as a table and a plot
with game-clustered ranges; the realization slope with its range; the same by sign and side.

**Informs.** Whether disagreement is a selection signal at all, and at what size. If the slope
is 0.4 on strikeouts and 0.0 on home runs, the strikeout market's large disagreements are
candidates and the home-run market's are noise. The slope also sets the first-cut blend weight
(C) and tells the calibration layer how much to shrink toward the line.

**Needs.** Full-season reads. A 15+-point bucket on strikeouts holds a few hundred records on a
season; the slope needs about 2,000 records per market for a range of ±0.15.

**Trap.** *The excess win rate is an edge read in spirit.* It does not price at the offered
odds, so it is an accuracy statistic; the owner may still consider it under the 2026-09-08
ruling (decision 2). Reads 1 and 2's Brier columns are pure accuracy either way. *Monte-Carlo
noise.* With 100 iterations a 50% probability carries a 5-point standard error; the small
buckets are dominated by noise, which is why the slope, not the bucket, is the summary (see G).

### C. The information test — does the model know something the line does not?

**Question.** Holding the line's probability fixed, does the model's probability predict the
outcome at all? And what is the best blend of the two?

**Method.** The **encompassing regression** (a forecast-combination test): a logistic
regression of the outcome on the log-odds of the line's probability and the log-odds of the
model's, per market. Log-odds means log(p / (1 − p)), the scale on which probabilities add. If
the model's coefficient is zero with a tight range, the line already contains everything the
model knows. If it is positive, the model adds information; the two coefficients ARE the
blend weights, and the fitted probability is the **blended price**. Fit on 2024, score on 2025
(the out-of-sample Brier of the blend versus the line alone is the value of the model in
accuracy units). A one-line extension adds the disagreement's square, which tests whether the
model's information is only in its large disagreements.

**Output.** Per market: the two coefficients with ranges; the out-of-sample Brier of line-only,
model-only, and the blend; the blend weight on the model (the share of the blended log-odds
the model contributes).

**Informs.** The single strongest read of "is there anything here". A market with a zero model
weight has no bet-grade subset, whatever the segment study says — a segment that looks good
there is a lucky cut. The blend weight is also the first version of the calibration layer that
the joint-fit plan's §7 specifies, with the line as a second input (decision 3).

**Needs.** Two seasons' full reads. Two thousand records per market resolve a model weight of
0.1.

**Trap.** *Leakage of the line into the model.* None today: the simulator never reads the odds.
Keep it so; a model that reads the line cannot be tested against it. *The line's own
calibration.* The devig (removing the book's margin) by splitting the margin equally between
sides biases extreme prices; run the regression with the power devig too (E) and report both.

### D. The segment study — conditional reliability

**Question.** On which observable slices of predictions is the model better than the line,
worse, or the same?

**Method.** For every segment in the pre-registered list below, the paired Brier gap with its
game-clustered range, the records and games, the model's bias and discrimination inside the
segment, and (if decision 2 allows) the excess win rate. Every segment is one row of the
**segment ledger** (§5.4): the hypothesis, the season it was read on, the result, the season it
was confirmed on, the grade.

**The segment dimensions (the pre-registered list; the owner adds or strikes).**

| family | segments | the reason to expect a difference |
|---|---|---|
| the market and side | each of the 30 markets; over / under; favorite / underdog; home / away | the book prices some markets sharper than others; the model's biases are sided |
| the line's geometry | the line value in bands (strikeouts 3.5–4.5 / 5.5–6.5 / 7.5+; totals 7–8 / 8.5–9.5 / 10+); the line's own price (−130 or worse / near even / +110 or better); where the line sits in the model's distribution (the model's mean minus the line, in standard deviations) | the model's tails are where a sim differs most from a market-maker's formula |
| the model's own footing | the Monte-Carlo standard error (100 iterations; low for extreme probabilities); the effective-sample share of the player's draws (pool depth: thin / normal / deep); the player's history depth this season (under 50 PA or BF / 50–200 / 200+); a first-season player | a thin pool or a thin history means the model is guessing from look-alikes it barely has |
| the player | pitcher type by strikeout rate tercile; batter hand vs the starter's hand; lineup slot 1–3 / 4–6 / 7–9; starter vs reliever-dependent markets | the pitcher factor's power and the platoon weight act differently per type |
| the game context | park (the ten most extreme parks by park factor vs the rest); day / night; the month (April / May–August / September); a doubleheader; the slate size | early-season data is thin; September lineups are odd; parks drive home runs and totals |
| the market's behaviour | the line moved from open to close by 0 / 0.5 / 1+ (or by 5+ points of probability); the direction of the move relative to the model's side; the book's margin on the pair (tight / normal / wide) | a big move means the market learned something; a wide margin means the book is unsure |
| the model's internal agreement | the number of same-game markets on which the model disagrees with the line in a consistent direction (the pitcher's strikeouts over AND his team's moneyline AND the total under); the rank of the disagreement within the day's slate | coherent disagreement across a game is harder to produce by noise |
| time | 2024 vs 2025; the four 250-game chunks of 2024; before vs after each weight flip | drift and the fit's own effect |

**Output.** The ledger; a heat map per market (segment × the Brier gap, coloured by the sign
and whether the range clears zero); the list of segments that clear the multiplicity rule
(§5.2) on 2024 AND hold on 2025.

**Informs.** The trust ladder's rows. Also the joint fit: a segment where the model is
systematically worse (thin pools; first-season players) names a weight or a minimum-cell rule
to revisit.

**Needs.** Full-season reads of two seasons. The power table (§5.3): a segment needs about 2,200
records to see a 3-point excess win rate and about 800 to see 5 points; on the strikeout market
that means at most two or three cuts per dimension; on the hit markets, ten or more.

**Trap.** *Forty cuts, two lucky ones.* §5.2's multiplicity rule and the hold-out are the
defence; a segment found on 2024 and confirmed on 2025 with the same sign is a finding, nothing
less is. *Post-hoc segments.* A segment invented after seeing the data goes into the ledger as a
hypothesis for the NEXT season, never as a finding on the season that suggested it.

### E. The market's own weaknesses

**Question.** Where is the LINE badly calibrated? The model's edge lives where the model is good
AND the line is weak; half of that is a property of the book.

**Method.** The line's reliability (the outcome rate by probability bin) per market and segment
— the same table as the model's, on the line alone. This needs no simulation, so it runs on
2021–2026 (about 12,000 games with props). Three known shapes to test: the **favorite-longshot
bias** (the book overprices longshots; the outcome rate at 20% implied is under 20%); the
**over bias** on totals and player props (the public bets overs; the book shades the over);
the **thin-market props** (doubles, triples, stolen bases, the first-inning markets — fewer
bettors, wider margins, slower to correct). Plus the **devig sensitivity**: the equal-margin
devig versus the power devig (which assigns more of the margin to the longshot); report the
line's reliability under both, and pick the one that calibrates the line best as the reference
for every other analysis.

Add the **line-movement study**: on games with a matched opening row, the size and direction of
the move by market, and whether the closing line is more accurate than the opening one (it
should be; the size of the gain says how much information arrives between open and close — the
window in which the model must act).

**Output.** The line's reliability tables by market and season; the devig choice with the
evidence; the open-to-close accuracy gain per market; the opening-line coverage per market and
season (the first measured read of it).

**Informs.** Which markets to weight in the composite and the ladder; the devig used
everywhere; whether the opening line is a second reference worth grading against.

**Needs.** The odds tables and the box score only. Cheap: an afternoon's compute.

**Trap.** *Selection in what has a line.* A book posts props on the players it expects action
on; the graded population is not every player. Report the coverage (the share of starters and
lineup spots with a line) per market so the ladder's reach is known.

### F. The learned reliability model — the blended price with features

**Question.** Can one model, given the model's probability, the line's, and the record's
features, produce a better out-of-sample probability than either — and which features make the
model's weight rise?

**Method.** Extend C: a logistic regression of the outcome on the line's log-odds, the model's
log-odds, and the model's log-odds INTERACTED with a small set of bet-time features (the
Monte-Carlo standard error, the pool depth, the history depth, the line band, the side, the
market). Each interaction coefficient says how the model's weight changes with that feature.
Fit on 2024 with cross-validation by game date (so a fold never contains a game's own
neighbours), score on 2025. Keep it to a dozen terms; a tree model (gradient boosting with
monotone constraints on the two probabilities) is the second version, only if the linear one
leaves out-of-sample accuracy on the table, and its feature importances must reproduce the
linear read before it is trusted.

**Output.** The fitted model; per market the out-of-sample Brier of the line, the model, the
plain blend (C) and the feature blend; the table of "the model's weight is X when feature = Y";
the blended price for every record.

**Informs.** The blended price is the betting surface's number (decision 3). The feature table
IS the answer to "which subset is more reliable", learned rather than cut by hand, and it
cross-checks D: a segment that D flags and F's interactions do not reproduce is suspect.

**Needs.** Two seasons; the extended record; every feature available BEFORE the game (the line
movement to the close is not; the opening-to-now movement is).

**Trap.** *A feature the game leaks.* Any feature computed after first pitch is leakage;
the extension marks each field bet-time-safe or not, and F reads only the safe ones.
*Refitting on every weight change.* The blend weights belong to one production weight set;
the joint fit's flips invalidate them; the refit is cheap (minutes) once the season reads exist,
but the season reads themselves are days.

### G. The noise study — how much of a disagreement is the model's own dice

**Question.** With 100 iterations, how much of the model's disagreement with the line is
Monte-Carlo noise, and does more iterations or a deeper pool reduce it?

**Method.** Three reads.

1. *The seed twin.* Re-run 250 games of 2024 under production with a different base seed.
   Per market, the correlation of `sim_prob` across the two seeds is the **test-retest
   reliability**. The share of the disagreement variance that is real is about the reliability
   coefficient; the rest is dice. A market with reliability 0.5 at 100 iterations has half its
   spread in noise — and the realization slope (B) cannot exceed the reliable share.
2. *The iteration ladder.* On 50 games, 100 / 400 / 1,000 iterations; the standard error of
   `sim_prob` by market and by probability; the cost curve. Says whether the live slate should
   run more iterations on the markets it bets.
3. *The pool-depth read.* The effective-sample share per draw per player (from the sampler's
   own diagnostics), joined to the record; the test-retest reliability by pool-depth tercile.

**Output.** Per market: the test-retest reliability with its range; the noise share of the
spread; the standard error by iteration count; the reliability by pool depth.

**Informs.** The minimum disagreement worth reading (below the noise floor, nothing is a
signal); the iteration count for the live slate per market; a pool-depth gate on the ladder.
Also the calibration layer: an over-spread model whose spread is half noise needs shrinkage,
not a bias shift.

**Needs.** One 250-game read (about 4 hours) plus a 50-game ladder (about 6 hours).

**Trap.** *Confusing noise with information.* A model that is over-spread because of dice looks
"confident and wrong"; the fix is more iterations or shrinkage, not a weight. The
decomposition (A) and the seed twin together tell the two apart.

### H. The stability study — does a finding survive?

**Question.** Does a market's or segment's result hold across seasons, across the 250-game
chunks, and across the weight flips?

**Method.** Every read in A–F reported per chunk of 250 games and per season; the sign
agreement of every ledger row across them; the paired change of every row across each flip
(the pairing script exists). A **rolling read** on the 2026 slate as it accumulates.

**Output.** Per ledger row: the seasons and chunks on which its sign held; a stability grade.
Per flip: which segments the flip helped or hurt.

**Informs.** The ladder's grades: a row is *bet-grade* only if it holds on both seasons and on
at least three of four chunks with the same sign; *watch* if it holds on one season; *off*
otherwise.

**Needs.** The same reads as D; no new compute.

**Trap.** *The first 250 games of 2024 have judged three flips.* They stay a read of record,
not a discovery set; the segment study reads all of 2024 and confirms on 2025.

### I. Cross-market coherence within a game

**Question.** When the model disagrees with the line on several markets of one game in a
consistent direction, is that disagreement more reliable than a lone one?

**Method.** Per game, the vector of signed disagreements across its markets; a **coherence
score** (the count of markets whose disagreement points the same way as the game's dominant
story — say, "the home starter dominates": his strikeouts over, his hits allowed under, the
home moneyline up, the total under). The realization slope (B) by coherence tercile. Also the
correlation structure the model produces between markets of one game versus the realized
correlation of outcomes (the model's joint distribution is a claim; test it).

**Output.** The realization slope by coherence; the model's versus the realized inter-market
correlations per market pair.

**Informs.** A coherence term in the reliability model (F); the case for same-game parlays
(the book prices correlated legs crudely; a model with the right joint distribution has its
largest edge there — but the parlay prices are not collected; a later ticket if the correlation
claim holds).

**Needs.** The same reads; no new compute.

**Trap.** *Coherence by construction.* The model's markets come from the same simulated games,
so its disagreements are correlated by construction; the test is against the REALIZED
outcomes, which are not.

### J. The value study — GATED

**Question.** Once the model's probabilities are trusted where the ladder says, what do the
bets return, and could they have been placed?

**Method.** Five reads, all waiting on the 2026-09-08 ruling (every band green; the split's
strikeout band re-graded).

1. *The return curve by threshold.* The hypothetical-return read (SIM-540) exists at one edge
   threshold; sweep it (1 to 15 points), per market, with a game-clustered range on the return
   per unit stake, flat stakes. Report the count of bets at each threshold; the curve's peak is
   usually a small-sample artefact, so the ladder's threshold is chosen on 2024 and READ on 2025.
2. *Excess win rate over the offered price.* The win rate of the model's side minus the
   break-even rate at the offered price. Lower variance than the return; the same sign.
3. *Staking.* Flat versus a fraction of Kelly (the stake that maximizes long-run growth; a
   quarter of it is the conventional safety margin) on the blended price; the return, the
   variance, the largest drawdown, per season. A model whose probabilities are over-spread
   over-stakes under Kelly; the blended price fixes that before the stake is computed.
4. *"The market comes to us."* On games with a matched opening line: does the close move
   toward the model's side more often than away? The share of records where the sign of
   (close − open) equals the sign of (model − open), per market. A model that predicts the
   line's movement holds information the market had not yet priced at open — the strongest
   evidence of a capturable edge that history can give.
5. *The bet log (paper trading).* From the day the live slate runs: for every model price, the
   time it was first available, the offered price then, the close, the outcome. This is the
   only true closing-line-value measurement, and it costs nothing but a table and a job. Start
   it before the ladder is finished; the data takes a season to mean anything.

**Output.** Per market and ladder row: the return curve, the excess win rate, the staking
table, the market-comes-to-us share, and — from the log — the closing-line value of every
paper bet.

**Informs.** The bet mix (which the composite objective's stake weights need, decision 1 of the
joint-fit plan); the stake rule; whether the edge is capturable at all.

**Needs.** The reads of D; the opening rows; the live slate for the log.

**Trap.** *Pricing at the fair line.* A bet transacts at the OFFERED price; the return read
uses the raw prices, never the devigged one. *The push.* The fade side's probability includes
the push mass on integer lines (the SIM-540 note); thread the push probability onto the
record before the staking read.

## 5. Statistical hygiene — the rules that keep forty cuts honest

### 5.1 The record extension (`scripts/clv_backtest.py`)

The fields of §3.2, each marked **bet-time-safe** (known before first pitch: the line, the
model's number, the pool depth, the history depth, the opening-to-now movement) or
**analysis-only** (the close, the outcome value, the open-to-close movement). The worker
already holds the game state, the odds rows and the per-iteration results; the fields are a
join, not new computation, except the effective-sample share, which the sampler must expose
per draw per player (a small plumbing change in `full_pool_sampler.py`, read-only). The report
format gains a version stamp; every analysis script refuses a report without the fields it
reads.

### 5.2 Multiplicity

The ledger holds every hypothesis before the data is cut. Within one family of segments, the
false-discovery-rate control (Benjamini–Hochberg at 10%) picks which rows are worth a
hold-out; across families the count is reported. A row is a finding only after the hold-out
season repeats its sign; a row that fails its hold-out is recorded as noise, not dropped.

### 5.3 The power table

For an excess win rate around 50% at 80% power and a two-sided 5% test: 3 points needs about
2,200 records; 5 points about 800; 2 points about 4,900. For a Brier gap: 250 games resolve
about 0.010 on strikeouts; a full season about 0.003. Every ledger row states its power at the
records it has; an underpowered row cannot be *bet-grade*, whatever its point estimate.

### 5.4 The ledger

One CSV under `docs/audit/`, one row per hypothesis: the market, the segment definition, the
family, the date filed, the season read, the paired gap and range, the excess win rate and
range, the records and games, the power, the multiplicity result, the hold-out season and
result, the grade, the date graded. Every script appends; nobody edits a past row.

### 5.5 The reads that need no correction

The whole-model reads (A, C on a market) and the pre-registered primary of each analysis are
single hypotheses; they need the hold-out, not the multiplicity control.

## 6. How the results become bets — the trust ladder

- **The ladder.** Per market, per ledger row with grade *bet-grade*: the segment definition,
  the blended-price weights (F), the minimum disagreement (G's noise floor), the confirmed
  seasons. Stored beside the calibration file; versioned to the production weight set it was
  read on.
- **The gate.** `betting/bet_signal.py`'s `BetSignalConfig` today has one edge threshold and one
  trust label per market (`trust_label` in the backtest). It gains a lookup: a record is a
  signal only if its market and segment sit on the ladder at *bet-grade*, its blended price
  differs from the offered price by more than the row's threshold, and its Monte-Carlo standard
  error is under the row's floor. *Watch* rows are shown, never bet.
- **The surface.** The game page's betting card shows the raw simulated probability, the
  blended price, the offered price, the ladder grade and the reason (the segment). A
  probability with no ladder row is shown *off*.
- **The cadence.** Every weight flip: rerun the season reads (days), refit F and the ladder
  (minutes), regrade. Every season: add the new season as the hold-out, promote the old one to
  the fit.

## 7. Sequence and cost

| step | what | compute | elapsed |
|---|---|---|---|
| 0 | the owner reviews this plan; decisions 1–5 | — | — |
| 1 | the record extension (§5.1) + the report version stamp; the ledger file | — | 2 days |
| 2 | analysis E on the odds alone (no sim): the line's reliability 2021–2026, the devig choice, the opening coverage, the open-to-close gain | 1 h | 1 day |
| 3 | the full-season reads: the rest of 2024 (games 1,001–2,472) and all of 2025, production, 100 iterations, the app down | ~60 h | 3 days, unattended |
| 4 | the seed twin (250 games of 2024, a second seed) + the 50-game iteration ladder | ~10 h | 0.5 day, unattended |
| 5 | the analysis scripts: A's decomposition, B's curves, C's regression, D's ledger runner, G's reads (one script, `scripts/sim550_reliability.py`, sub-commands) | — | 3 days |
| 6 | the reads on 2024; the ledger filled; the hold-out on 2025; H and I | 1 h | 3 days of reading and writing |
| 7 | F's reliability model; the blended price; the ladder v1; the gate | — | 2 days |
| 8 | J, when the ruling allows: the return curve, staking, market-comes-to-us; the bet log job on the live slate | 1 h | 1 day |

About two and a half weeks; three and a half days of it unattended compute. **Shared work:**
step 3's 2025 read is the baseline arm of the joint-fit plan's hold-out (the same config,
seeds and bundle — the pairing script accepts it), so the two plans pay for it once. **Order
against the joint fit:** run steps 1–7 on the CURRENT production. The instruments transfer;
the ledger's hypotheses transfer; only the numbers refit after a flip, and the refit is
minutes once the reads exist. Waiting for the fit to finish would idle the analysis for a month
and lose the read of what the current production is good at.

**Compute.** Five backtest workers need about 6 GB; the app about 2.5 GB; the VM has 9.7 GiB.
The reads run with the app down (the standing arrangement) or on a raised VM.

## 8. The owner's decisions

1. **The segment list (§4 D).** Add, strike, or accept the eight families as the
   pre-registered set. Anything added after the reads start is a next-season hypothesis.
2. **The excess win rate under the 2026-09-08 ruling (§4 B).** It is an accuracy statistic
   (no offered price enters), so this plan reads it in B and D. If the owner counts it as an
   edge read, it moves to J and the ladder grades on the Brier gap alone until the bands are
   green.
3. **The blended price as the betting surface's number (§4 C, F).** The joint-fit plan's
   calibration layer maps the model's probability alone; this plan's blend adds the line as an
   input. The blend is better for bet selection (it bets only against a line the model has
   proven it beats); the layer alone is the honest "what the model thinks". Recommended: both
   shown; the gate bets on the blend; the game page leads with the calibrated model number.
4. **The devig (§4 E).** Equal-margin (today) or the power method, chosen by which calibrates
   the line best on 2021–2026. Recommended: let E decide and use one method everywhere.
5. **The bet log (§4 J.5).** Start it now on the live slate, independent of the ruling (it
   collects prices, it measures nothing until read). Recommended: yes — the data takes a
   season to matter and nothing else can measure a capturable edge.

## 9. The definition of done

- The record carries the §3.2 fields, marked bet-time-safe or analysis-only; the report is
  version-stamped; the analysis scripts refuse an old report.
- Analysis E has run on 2021–2026: the line's reliability per market, the devig chosen, the
  opening coverage and the open-to-close gain recorded.
- The full-season production reads of 2024 and 2025 exist, twins by provenance, plus the
  seed twin and the iteration ladder.
- Analyses A–D and G–I have run on 2024 and been confirmed on 2025; every hypothesis sits in
  the ledger with its grade; the power of every row is stated.
- The reliability model (F) is fitted on 2024, scored on 2025, and its feature table reproduces
  the ledger's bet-grade rows or the disagreement is recorded.
- The trust ladder v1 exists, versioned to the production weight set; the bet-signal gate reads
  it; the game page shows the grade and the blended price.
- Analysis J has run if the ruling allows, or its gate is recorded; the bet log job runs on the
  live slate either way (decision 5).
- The procedure (the reads, the scripts, the refit after a flip) is written into
  `docs/technical/scripts-frontend.md`; `CHANGES.md` carries the results; this document carries
  the stamps and the owner's five decisions with their dates.
