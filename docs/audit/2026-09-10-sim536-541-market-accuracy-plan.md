# Market-accuracy validation — the build plan (SIM-536 → SIM-541)

**Date:** 2026-09-10
**Status:** proposed, not yet started. Ticket numbers below are provisional — `BACKLOG.xlsx`
was locked (open in Excel) when this plan was written, so the rows have not been added yet
and the next-free-ID pointer has not been advanced past SIM-536. Do not reuse SIM-536–541
for anything else until that row confirms they are filed.

**Owner decision this plan implements:** replace Closing Line Value (comparing the price at
bet-entry to the price at close) with a different metric — comparing the simulator's own
probability to the closing line's probability, both graded against what actually happened in
the game. Two reasons, both concrete: the simulator cannot produce a probability before both
lineups are announced, which happens after the betting line opens, so "entry vs. close" was
never comparing the model against a moment it could have acted at; and the odds data collected
today has closing lines for most games but not always a matched opening line, so a method that
needs only the closing line uses far more of the data already collected.

---

## 1. What "market-accuracy validation" means, precisely

For a market where a closing line exists (game moneyline, total, run line, or a player prop):

1. Take the simulator's own probability for the outcome (already produced by the sim; no new
   math needed).
2. Take the closing line's probability for the same outcome, with the bookmaker's built-in
   margin removed (**de-vig** — turning a quoted price into a true, fair probability; the math
   for this already exists, see §2).
3. Score BOTH of those probabilities against what actually happened in the game (win/loss,
   actual total, actual prop count), using a **proper scoring rule** — a scoring method where a
   forecaster cannot improve their score by hedging toward a "safe" prediction, only by being
   more accurate. Two standard ones, both already built (§2): the **Brier score** (the average
   squared error between the predicted probability and the 0-or-1 outcome) and **log loss**
   (which punishes a confident wrong prediction far more than a cautious one).
4. Because both scores come from the SAME game and the SAME real outcome, compare them as a
   **paired** difference (sim's score minus the market's score, per game/bet) rather than as
   two separate averages. A paired comparison needs a smaller sample to detect a real
   difference than comparing two independent groups would.
5. Report the average paired difference with a confidence interval, not a bare percentage.

A model that is more accurate than the closing line under this test cannot get there by being
bland — mirroring the pool average scores no better than the market's own price under a proper
scoring rule. This is the fix for the pool-totals bands rewarding low differentiation.

**What this deliberately does NOT claim to prove on its own:** that the edge was
*capturable* — i.e. that there was a real moment to transact at a worse price than the model
implied. Beating the closing line's accuracy is necessary for a real edge to exist, but proving
you could have banked it needs a live price snapshot at the actual moment the model can act
(right after lineups post), which is not being collected today. Treat that as a later,
separate validation once such snapshots exist — it does not block this plan.

## 2. What already exists — no new math required for the core comparison

- `betting/clv_engine.py` — `devig_two_way` / `devig_multiway`: turns quoted American odds
  into a fair, margin-free probability. Reusable as-is.
- `simulation/prop_validation.py` — `binary_brier` / `binary_log_loss`: takes an array of
  predicted probabilities and an array of real 0/1 outcomes, returns the score. Reusable as-is
  for BOTH the sim's score and the market's score — feed the same real-outcome array through
  it twice, once with the sim's probabilities and once with the de-vigged closing
  probabilities.
- `scripts/clv_backtest.py` — the per-game replay (resolve lineup → run N iterations → build a
  `GameSimSummary` / `WinProbability` / `PropDistributionSet`) and the odds readers
  (`raw.game_odds`, `raw.prop_odds`) are both fully reusable; only the entry-vs-close CLV step
  at the end needs replacing with the new paired-scoring step.
- `SIM-534` (point-in-time batter profiles) and `SIM-535` (point-in-time, date-filtered play
  pools) — **already landed**, 2026-09-10. A cutoff now flows end-to-end from
  `build_sim_kwargs` (the entry point `clv_backtest.py` already calls) through
  `production_machine_factory` into the sampler, which zeros the weight of any pool row dated
  after the cutoff (set to the day before the game, so the simulator can never copy the very
  plays it is predicting). This was the single largest piece of remaining engineering risk in
  last message's plan, and it is done — the pools genuinely stop being a leakage vector, with
  no extra cost for a season-long backtest.

## 3. What is still genuinely missing

### 3a. A confirmed, still-open data bug: wrong-game odds

`pipeline/bettingpros_odds_provider.py:138` matches odds to a game using a UTC timestamp
instead of the official local game date (the historical game loader already uses the correct
field elsewhere). An earlier internal check against the live 2024 backfill found roughly
26–43% of Central/Mountain/Pacific-time games paired with the *next calendar day's* game
between the same two teams. The same bug is in `pipeline/live/bullpen_availability_ingest.py`.
This has to be fixed before any comparison — sim-vs-close or classic CLV — can be trusted,
because it corrupts which odds get attached to which game in the first place.

### 3b. Point-in-time coverage is not complete

SIM-534/535 covers batter profiles and the play pools. Per SIM-534's own commit notes, still
season-only with no date control at all: sprint speed, outs above average, arm strength, pop
time, most baserunning metrics, and the catcher metrics. Pitcher, fielder, catcher, and
manager profiles have not had the SIM-534 treatment. This is now a smaller remaining leak than
it was — the pools were the largest exposure and they're fixed — but it is not zero, and any
number produced before this closes should say so.

### 3c. No new orchestration for the paired comparison

Nothing today runs the sim's probability and the de-vigged closing probability through
`binary_brier`/`binary_log_loss` side by side and reports the paired difference. This is a
genuinely small build (§2), not a research problem — the pieces exist, they have never been
wired together this way.

### 3d. No statistical rigor on any existing scoreboard

`clv_backtest.py` reports a raw percentage and a mean, no confidence interval, no minimum
sample size before a result is treated as a read rather than noise. This is the reason
CLAUDE.md already treats the one historical CLV number as uninformative.

### 3e. Odds coverage is probably narrower than what's actually collected

Per the owner: the odds scraper captures closing lines on most bets, more broadly than the
paired opening+closing set `SIM-435` backfilled and `clv_backtest.py` currently reads. Nobody
has audited exactly what the scraper captures today versus what the backtest path actually
consumes. This needs a look before assuming the sample size is small — it may already be much
larger than it looks.

### 3f. `SIM-429`'s current wording is now stale

The open ticket describes "re-measure closing-line value" in the old entry-vs-close sense.
It should be split: the strikeout/walk prop-calibration refit it also names is real,
independent work and should stay; the re-measurement piece should be retired in `SIM-429`
and owned by the tickets below instead.

## 4. Proposed tickets

All descriptions below are the plain-English form meant for `BACKLOG.xlsx`. Priorities
reflect that 536 blocks trusting any per-game odds at all, and 538–539 are the actual new
metric; 540–541 improve it once it exists.

---

**SIM-536 (P1) — Fix odds being matched to the wrong game**

*Description:* The system that loads real historical betting odds sometimes attaches the
wrong game's odds to a game. It figures out which game a set of odds belongs to using a
timestamp recorded in a global time zone, but it should use the official date of the game
where it was played. For games that start late at night in Central, Mountain, or Pacific
time, this can shift the recorded date to the next day, so the loader pairs that game with a
different game between the same two teams the following day. A check found this may affect
roughly a quarter to nearly half of games outside the Eastern time zone.

*Definition of done:* The odds loader matches every game using its official local date, not a
global-time-zone timestamp. Doubleheaders are told apart by their scheduled start time. A
loaded odds record is rejected if its recorded time is more than about two hours from the
game's actual first pitch. The already-loaded 2024 season odds are re-checked and any
mismatched games are re-loaded.

*Proposed solution:* Change the matching field from the global-time timestamp to the game's
official local date in `pipeline/bettingpros_odds_provider.py` (and the same fix in
`pipeline/live/bullpen_availability_ingest.py`, which has the identical bug). Add the
first-pitch-time sanity check. Re-run the historical backfill and diff the results against
what is currently loaded.

---

**SIM-537 (P1) — Extend point-in-time cutoffs to the remaining player profiles**

*Description:* Two recent changes made sure the simulator, when replaying a game from the
past, cannot use information recorded after that game — first for the batter's own
statistics, then for the pool of real plays the simulator draws from. Several other
player-measurement types were not part of either fix: a fielder's or catcher's throwing
strength, sprint speed, pop time, and most base-running measurements are still calculated
from an entire season at once, with no way to cut them off at a specific date. That means a
simulation of an early-season game can still be quietly informed by that same player's
performance from later in the season.

*Definition of done:* Every player-measurement input the simulator reads for pitchers,
fielders, catchers, and baserunners can be built as of a specific date, using the same rule
already applied to batters: a season that ended before the cutoff counts in full, and only
the season containing the cutoff is trimmed to what happened through that date. The system
refuses to run with a mismatched cutoff across different measurement types.

*Proposed solution:* Apply the same pattern `SIM-534` already built for batters — a
per-source "as of" date column, a cutoff rule building on top of it, and a check after
building that confirms nothing dated after the cutoff was used — to the remaining profile
builders (fielder, catcher, baserunner, pitcher, manager).

---

**SIM-538 (P1) — Build the sim-vs-closing-line accuracy comparison**

*Description:* Build the actual new way of checking whether the simulator's predictions are
good: for every game or player prop where a final ("closing") betting price is available,
compare the simulator's own predicted probability and the market's probability (with the
bookmaker's built-in margin removed) against what actually happened, and see which one was
more accurate. This replaces trying to catch a betting line's price movement, which does not
work well for this platform because the simulator cannot make a prediction until both
teams' starting lineups are announced — which is after betting lines first open.

*Definition of done:* For a slate of completed games with closing odds, the tool reports, per
market and player-prop type: how many games/props were compared, how much more (or less)
accurate the simulator was than the market on average, and whether that difference is likely
real or could be chance (see `SIM-539`).

*Proposed solution:* Reuse the existing game-replay and odds-reading code from
`scripts/clv_backtest.py` unchanged. Reuse the existing "remove the bookmaker's margin" math
from `betting/clv_engine.py` and the existing accuracy-scoring math from
`simulation/prop_validation.py`. The only new code is the piece that runs both the simulator's
number and the market's number through the same accuracy score and reports the paired
difference. Keep the existing entry-vs-close report available as a secondary, clearly-labeled
diagnostic rather than deleting it — it may become useful again once real-time betting-price
snapshots are collected at the moment the simulator can actually act.

---

**SIM-539 (P1) — Add real statistical confidence to the comparison**

*Description:* Right now, when the platform reports a betting-accuracy result, it reports a
single number with no sense of whether that number is reliable or just noise. A result from a
small number of games can look like real skill purely by chance.

*Definition of done:* Every reported accuracy comparison (SIM-538's, and the existing
CLV scoreboard) comes with a range showing how confident the platform is in that number, and
the platform states, per market, the smallest number of games needed before a result should
be trusted at all.

*Proposed solution:* Because the simulator's score and the market's score come from the exact
same games, use a paired statistical test (for example, a paired bootstrap or a paired t-test
on the per-game score difference) rather than treating the two as unrelated samples — this
needs fewer games to detect a real difference. Set and document a minimum sample size per
market before publishing a headline number for it.

---

**SIM-540 (P2) — Report the hypothetical dollar return alongside the accuracy score**

*Description:* An accuracy score is useful for the team but harder for a non-technical reader
to judge. Add a companion report: whenever the simulator's probability disagrees with the
market's probability by more than a set amount, calculate what the return would have been on
a bet placed at the closing price, and add up that hypothetical return across many games.

*Definition of done:* The comparison tool can optionally output an estimated return on
investment, with a confidence range, for a chosen disagreement threshold, alongside the
accuracy score from `SIM-538`.

*Proposed solution:* Reuse the existing `expected_value` calculation in
`betting/clv_engine.py`; aggregate it only over the bets that clear the disagreement
threshold, using the same real-outcome data `SIM-538` already reads.

---

**SIM-541 (P2) — Find out how much closing-line odds data is actually available, and use all of it**

*Description:* The current historical odds data only covers games where both an opening and
a closing price were captured and matched. The platform's odds-collection process is believed
to capture closing prices on many more games than that, without always having a matching
opening price. Since the new accuracy comparison only needs the closing price, this may
unlock a much bigger set of games to test against — nobody has checked.

*Definition of done:* A clear count of how many additional games/props have a usable closing
price today that the current backtest does not use, and the comparison tool (SIM-538) updated
to use all of them, not only the subset that also has a matched opening price.

*Proposed solution:* Audit the current odds-scraping and storage path end to end to see what
is actually captured versus what `raw.game_odds`/`raw.prop_odds` currently expose to the
backtest. Relax the backtest's game-selection query to require only a closing price, not both.

---

**Not a new ticket — retire the stale part of `SIM-429`:** once SIM-536–541 are filed, edit
the existing `SIM-429` row to drop everything about re-measuring closing-line value in the
old entry-vs-close sense (owned by the tickets above now) and keep only the strikeout/walk
prop-calibration refit, which is unrelated, still valid work.

## 5. Suggested order

1. **SIM-536** first, alone — nothing else is trustworthy until odds are matched to the right
   game.
2. **SIM-537** and **SIM-538** can run in parallel — SIM-538 does not need to wait for full
   profile coverage, but any number it produces before SIM-537 lands should say plainly which
   inputs were still season-only.
3. **SIM-539** lands with or immediately after SIM-538 — do not publish a headline number
   without it.
4. **SIM-540** and **SIM-541** follow once SIM-538 exists to build on.
5. Retire the stale half of **SIM-429** as soon as the new tickets are filed, not at the end.

## 6. A ruling this plan implies, for the owner to confirm

CLAUDE.md §2b currently gates any betting-value measurement on every pool-realism band being
green. That gate should stay, but it is not sufficient on its own anymore: a green set of
bands says the simulator's overall rates look realistic, not that its odds are matched to the
right games or that its inputs are leak-free. Suggested addition once this plan is adopted:

> No betting-value measurement is trustworthy until (a) every pool band is green, AND (b) the
> odds-matching fix (SIM-536) has shipped and the historical data has been re-checked, AND (c)
> the point-in-time coverage gaps named in SIM-537 are closed or explicitly accepted as a
> documented, bounded caveat on the result.

This plan does not apply that edit — it is written here for the owner to accept or amend.
