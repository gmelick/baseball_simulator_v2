# Build plan — the first-five run line scored as two separate bets in the accuracy comparison (SIM-549)

> **STATUS 2026-09-25 — BUILT, REVIEWED, RUN AND CLOSED.** Built on the owner's instruction
> "implement the tech design for SIM-549", with the decisions of §10 as taken on 2026-09-23. The
> re-scored 1,000-game baseline reads the first-five run line exactly as §2.3 forecast: 986
> records, the simulator behind the line by +0.0066, the line's bias −0.006. The review added two
> guards the plan did not have and corrected six of its statements; §11 is the build record.
> The readable page https://claude.ai/artifact/BY2ypcrDS8C3ttANsPYxdD shows the design as
> approved (2026-09-23); this file is the record.
>
> *(2026-09-23, as approved: one shape rule in the accuracy comparison, one new market key per
> run-line market for the away bet, a re-score of the stored reports. No simulator change, no
> data change, no restart.)*
>
> **The store is right and the scorer is wrong, and the scorer's error is worth 0.035 of
> Brier on the one market.** On this market the book's closing row is often two separate
> bets — the home team leads by two or more after five, the away team leads by two or more —
> and the accuracy comparison prices them as the two sides of one bet. That inflates the
> line's probability by a fifth and makes the simulator look like it beats the line by
> 0.028 of Brier on the 1,000-game baseline. Priced from its own line, with the book's margin
> taken from the same game's first-five total, the line is unbiased and the simulator is
> behind it by 0.007, the same standing as its other game markets. The shape lives in 24% of
> the first-five closing rows, 80% of 2024's, and in 3% of the full-game and 10% of the
> first-inning run lines, which the plan covers with the same rule.

**Date:** 2026-09-23
**Ticket:** SIM-549 (P2) in `BACKLOG.xlsx`. Next free ID: SIM-553.
**Evidence:** the accuracy comparison as it stands at commit f8281fb (`scripts/clv_backtest.py`,
`simulation/game_market_distributions.py`, `betting/clv_engine.py`); the three instruments that
read its reports (`scripts/sim548_market_skill.py`, `scripts/sim518_pair_accuracy.py`,
`scripts/sim548_design.py`); every closing run line in `raw.game_odds` (16,842 games, 2019
to 2026) against the official inning grid in `raw.games`; the four stored reports of the
1,000-game 2024 baseline (`scripts/sim548_accuracy_split.json`,
`scripts/sim548_baseline_2024_chunk{2,3,4}.json`).
**Builds on:** the segment-market scorer of 2026-09-12 (SIM-421, `CHANGES.md`); the
skill-table read of 2026-09-15 that found the shape and excluded the row (SIM-548,
`CHANGES.md`; `docs/audit/2026-09-14-sim548-joint-fit-plan.md` §11).

---

## 0. The short version

**What the ticket asks for.** The accuracy comparison scores the first-five run line as two
records per game — home −1.5 and away −1.5 — each priced from its own line with the book's
margin removed by the margin of the same game's two-way first-five markets, never by pairing
it with the other team's price. A unit test on a real payload holds this. The skill table's
guard accepts the market again. The 2024 baseline's rows are re-scored from the stored
reports' prices without re-running the simulator. The changelog records the fix.

**What the code and the data say.**

- The scorer reads the home spread and the two prices, and prices the two as one bet
  (`devig_two_way`). It never reads the away spread, so it cannot tell a pair from two bets.
- The store holds both shapes. A pair is a row whose away spread is the negative of the home
  spread (home −0.5 / away +0.5). Two separate bets share a sign (home −1.5 / away −1.5: both
  lose on a tie or a one-run lead) or carry two different lines (home +0.5 / away −1.5). On
  the first-five run line 3,289 of 13,715 closing rows are not pairs (24%); in 2024 it is
  1,946 of 2,433 (80%), because that season's closing snapshot carries the ±1.5 pair on four
  games in five. The full-game run line has the shape on 503 rows (3%) and the first-inning
  run line on 1,137 (10%). The scorer treats every one of them as a pair.
- The margin a one-sided price carries is the same size as the game's two-way margin. On
  3,216 regular-season games with the shape, the raw implied probability of the home bet
  sits 3.6% above the real cover rate and the away bet 7.7% above; divided by the same
  game's first-five total margin (mean 1.0585), the home bet reads 0.438 against a rate of
  0.448 and the away bet 0.375 against 0.369, and the reliability holds bin by bin from 0.22
  to 0.84. The three-way first-five moneyline's margin (1.22, the tie is priced) over-corrects
  by a tenth and must not enter. Pairing the two prices, the method today, reads 0.537
  against 0.448.
- On the 1,000-game baseline the row reads: 986 records, the line's mean probability 15.8
  points above the outcome rate, the simulator "beating" the line by 0.028 of Brier.
  Re-scored on the stored prices (the home record; 789 of the 986 are the one-sided shape),
  the line's bias is −0.6 points, its discrimination rises from 0.56 to 0.63, and the
  simulator is behind it by 0.007 — between the first-five total (+0.007) and the run line
  (+0.005). The artefact is the whole of the "beat".
- The stored reports hold the home record's simulator probability and both prices, and the
  same game's first-five total prices on 975 of the 986 games, so the home record re-prices
  from the report itself. They hold no per-iteration first-five margin, so the away record
  cannot be built from them; it joins from the next run.

**The plan.** One shape rule — a pair if and only if the away spread is the negative of the
home spread — in one run-line scorer that both the full-game and the segment paths call. A
pair keeps today's one record. Two separate bets give two records, one per side, each priced
from its own line, its margin removed by the same game's two-way total of the same segment
(then the full-game total, the moneyline pair, a flat 1.05). The away bet's record carries its
own market key (`f5_runline_away`), so the record's fields and the three instruments' pairing
keys do not change; the report gains a per-market shape tally and a warning line. A small script re-prices the home records of every stored
report beside the original. Nine tests, one on the ticket's real payload. No weight, no
simulator change, no restart.

**Decisions** in §10: taken on 2026-09-23 — 1, 3 and 4 as recommended, 2 as the alternative
(distinct market keys).

---

## 1. The mechanism

**How the row is stored.** `raw.game_odds` keeps one row per (game, market, line type) with
`home_spread` / `home_spread_ml` and `away_spread` / `away_spread_ml`. The loader
(`pipeline/bettingpros_odds_provider.py` `_fill_runline`) writes each selection's own line and
price under its team; it does not assume a mirror. Game 744796's closing row is home −1.5 at
+350 and away −1.5 at +150; its opening row was home +0.5 at −115 and away −0.5 at −110, a
pair. Both rows are faithful to what the book listed.

**How the scorer prices it today** (`scripts/clv_backtest.py` `score_segment_market_accuracy`,
the run-line kind of `_SEGMENT_COLUMNS`):

```python
_SEGMENT_COLUMNS["runline"] = ("home_spread_ml", "away_spread_ml", "home_spread")   # the away spread is never read
cp = _closing_prices(odds, market_type, side_col, other_col, line_col)             # side = home price, other = away price, line = home spread
sim_prob = market_probability(runs, market_type, line=cp.line)                      # P(home margin + home spread > 0)  — right
market_prob = float(devig_two_way(cp.side, cp.other)[0])                            # q_home / (q_home + q_away)         — wrong on two separate bets
outcome = market_outcome(actual, market_type, line=cp.line)                         # 1 if the home margin beat the spread — right
```

On game 744796: q_home = 100/450 = 0.222, q_away = 100/250 = 0.400, their sum 0.622. The
pairing divides by 0.622 and reports 0.357 as the line's probability that the home team leads
by two or more after five. The bet itself says 0.222 with the book's margin still in it;
the same game's first-five total (−109 / −121) carries a margin of 1.069, and 0.222 / 1.069 =
0.208. The game ended 4–4 after five: both bets lost.

**The full-game path** (`score_game_accuracy`, the run-line block) does the same through
`run_line_edge_report` and a `TwoWayMarket` built from the two prices. It reads the home spread
only, so its 503 non-pair rows get the same treatment.

**The guard that found it.** The skill table (`scripts/sim548_market_skill.py`) adds the two
prices' implied probabilities per record and refuses a market whose mean sits outside
1.00–1.15 (`OVERROUND_OK`). A pair adds to about 1.05; the first-five run line's records add to
0.76 on the 2024 baseline, and the row is printed with "LINE SUSPECT … excluded".

---

## 2. The evidence

### 2.1 The shape of the stored run lines (closing rows, latest per game)

| Market | Rows | Pairs | Not pairs | The common non-pair shapes (home, away) | Prices' implied sum, pairs / not pairs |
|---|---:|---:|---:|---|---|
| full-game run line | 16,842 | 16,339 (97.0%) | 503 (3.0%) | (+1.5, +1.5) 150; (−1.5, −1.5) 110; (−1.0, +1.5) 61; (−1.5, +1.0) 46 | 1.044 / 1.075 (p10 0.76, p90 1.34) |
| first-five run line | 13,715 | 10,426 (76.0%) | 3,289 (24.0%) | (−1.5, −1.5) 1,833; (+0.5, +0.5) 580; (+1.5, +1.5) 173; (−0.5, −0.5) 144; (+0.5, −1.5) 91; (−0.5, +1.5) 88 | 1.062 / 0.864 (p10 0.62, p90 1.24) |
| first-inning run line | 11,555 | 10,418 (90.2%) | 1,137 (9.8%) | (+0.5, +0.5) 753; (+1.0, +1.0) 191; (−0.5, +1.5) 51; (−1.5, +0.5) 42 | 1.057 / 1.410 (p10 1.05, p90 1.64) |

The non-pair share by season on the first-five run line: 2021 915, 2022 127, 2023 76, **2024
1,946 (80%)**, 2025 88, 2026 137. The closing snapshot of 2024 carries the (−1.5, −1.5) pair on
1,807 games; the opening snapshot of the same games carries it on 596 and a mirror pair on
1,672. The 2024 baseline is therefore the season where the defect bites hardest: 789 of its
986 first-five run-line records are one-sided (746 of them the ±1.5 shape).

The rule the ticket names, "the two spreads have the same sign", misses the two-line rows
((+0.5, −1.5), (−0.5, +1.5), (−1.0, +1.5) …, about 300 rows across the three markets) and
the pick'em rows (0, 0), which ARE a pair. The rule that separates every row: **a pair if and
only if the away spread equals the negative of the home spread.**

### 2.2 The margin on a one-sided price (regular-season Final games with a first-five total)

For every non-pair row I graded each side on the official grid and compared the price's
implied probability with the real cover rate. The ratio implied / rate is the margin the
price carries; a two-way pair's proportional de-vig divides by about 1.05.

**The first-five run line, 3,216 rows.** Home cover rate 0.448 (±0.009), away 0.369.

| Method for the market's probability | Home: implied / ratio / Brier | Away: implied / ratio / Brier |
|---|---|---|
| paired as two sides of one bet (today) | 0.537 / **1.199** / 0.2507 | — |
| the raw price, margin left in | 0.464 / 1.036 / 0.2194 | 0.398 / 1.077 / 0.2207 |
| **÷ the same game's first-five total margin** | **0.438 / 0.978 / 0.2192** | **0.375 / 1.016 / 0.2195** |
| ÷ a flat 1.05 | 0.442 / 0.987 / 0.2191 | — |
| ÷ the same game's full-game total margin | 0.443 / 0.989 / 0.2192 | 0.380 / 1.028 / 0.2196 |
| ÷ the same game's moneyline margin | 0.446 / 0.995 / 0.2192 | 0.382 / 1.034 / 0.2197 |
| ÷ the same game's three-way first-five moneyline margin | 0.399 / 0.890 / 0.2236 | 0.341 / 0.924 / 0.2200 |
| ÷ the mean of the first-five total and the (scaled) three-way moneyline, the ticket's words | 0.505 / 1.126 / 0.2235 | — |

By shape, divided by the same game's first-five total margin: (−1.5, −1.5), 1,802 rows —
home 0.326 against a rate of 0.328, away 0.281 against 0.284; (+0.5, +0.5), 559 rows — 0.625
against 0.630, 0.518 against 0.513; (+1.5, +1.5), 168 rows — 0.789 against 0.845; (−0.5,
−0.5), 144 rows — 0.465 against 0.507. The reliability of the corrected home probability,
in bins: 0.224 → 0.217 (226 rows), 0.306 → 0.307 (1,042), 0.411 → 0.433 (869), 0.581 →
0.582 (622), 0.701 → 0.713 (327), 0.838 → 0.868 (106). The control, the 10,271 pairs under
today's de-vig: 0.498 against a rate of 0.509.

**The margins themselves** (implied sums, closing rows): full-game total 1.048 (sd 0.009),
moneyline 1.041, **first-five total 1.0585 (sd 0.017; p10 1.045, p90 1.070)**, first-inning
total 1.051, the three-way first-five moneyline 1.218 (sd 0.163), the three-way first-inning
moneyline 1.197. A three-way margin is not a two-way margin: it prices the tie.

**The full-game run line, 416 one-sided rows:** today 0.493 against a rate of 0.512; divided
by the first-five total margin 0.503 (away 0.517 against 0.514). **The first-inning run line,
1,071 rows:** today 0.483 against a rate of 0.721 (the pairing halves it); the (+0.5, +0.5)
rows, 753, read 0.774 against 0.777 and 0.757 against 0.762 once corrected. The (+1.0, +1.0)
rows, 131 graded, read 0.465 against 0.885 under every method — see §3, finding 6.

### 2.3 The row on the 1,000-game 2024 baseline, today and re-scored

The four stored reports hold 986 first-five run-line records over 986 games. I joined each to
its closing row in the store (every one found; every stored price equal to the record's),
classified the shape, and re-priced the one-sided home records from their own price and the
same game's first-five total margin — from the report's own first-five total record on 972
records, from the store on 14 (mean margin 1.062, sd 0.014). The pairs (197) keep today's
value. The simulator's probability and the outcome do not change.

| Rows | n | Outcome rate | Brier sim / mkt (base) | Gap sim − mkt | Bias sim / mkt | Spread sim / mkt | AUC sim / mkt |
|---|---:|---:|---|---:|---|---|---|
| all, today | 986 | 0.373 | 0.2295 / 0.2571 (0.2339) | **−0.0276** | −0.019 / **+0.158** | 0.108 / 0.089 | 0.60 / 0.56 |
| all, re-scored | 986 | 0.373 | 0.2295 / 0.2229 (0.2339) | **+0.0066** | −0.019 / −0.006 | 0.108 / 0.099 | 0.60 / 0.63 |
| pairs only (unchanged) | 197 | 0.523 | 0.2648 / 0.2513 (0.2495) | +0.0135 | −0.043 / −0.024 | 0.094 / 0.050 | 0.48 / 0.52 |
| one-sided, today | 789 | 0.336 | 0.2207 / 0.2586 (0.2231) | −0.0379 | −0.013 / +0.203 | 0.086 / 0.095 | 0.57 / 0.60 |
| one-sided, re-scored | 789 | 0.336 | 0.2207 / 0.2158 (0.2231) | +0.0049 | −0.013 / −0.001 | 0.086 / 0.079 | 0.57 / 0.61 |

The "all, today" row is the skill table's excluded row of 2026-09-15 to the digit (986,
0.230, 0.257, −0.0276, 0.60 / 0.56, −0.019 / +0.158). Re-scored, the line is calibrated
(0.263 → 0.242, 0.344 → 0.365, 0.433 → 0.430 across its bins) and the simulator is behind it
by the same 0.005–0.007 it is behind the first-five total and the run line. The simulator's
own numbers on this market are unchanged by the fix: a bias of −1.9 points, a spread of
0.108 against the line's 0.099, and a 0.170 probability that comes true 4% of the time (23
records).

### 2.4 What the stored reports hold, and do not

A record carries `game_pk, market, market_type, sim_prob, market_prob, outcome, player_id,
market_side_price, market_other_price, sim_prob_raw` — no spread, no side. The report's
`params` carry the provenance the skill table's merge checks; its `counters` the game tallies;
its `accuracy_comparison` the rows the backtest itself printed. Nothing per iteration is
kept: the segment runs (`SegmentRuns`) are built in the worker and discarded. So the stored
reports can re-price the home record (the sim's number stays, the market's number is
recomputed from the stored price and the report's own first-five total prices), and they
cannot yield the away record's simulator probability at all.

### 2.5 Where a second record per game matters

Three instruments key records by (game, market, player): the composite objective
(`sim548_market_skill.py` `composite`), the paired-arm read (`sim518_pair_accuracy.py` `_key`)
and, through the skill table's loader, the design runner. A second first-five run-line record
per game would overwrite the first in those dictionaries. The design runner itself sums Brier
per (market, game) and is unaffected; the calibration layer groups by market and is
unaffected; the backtest's own aggregation groups by market and is unaffected; the
hypothetical return prices each record on its own and needs no change beyond the missing
fade price (§5). With decision 2 the away record has its own market key, so none of those
keys changes.

---

## 3. Findings that shape the plan

1. **The rule is the mirror test, not the sign test.** "Same sign" misses the two-line rows
   and wrongly splits the pick'em pair. A pair if and only if `away_spread == -home_spread`;
   everything else is two bets, each priced from its own line. A row with a missing away
   spread (three rows in the store, all with missing prices too) is skipped as today.
2. **The reference margin must be a two-way margin from the same segment.** The store's
   first-five moneyline is three-way (the tie is priced; margin 1.22), so the ticket's
   "first-five total and first-five moneyline" would divide by about 1.14 and under-price
   both sides by a tenth. The first-five total is the one two-way first-five market, present
   on every game that has the run line (13,723 first-five totals against 13,715 first-five
   run lines, 2021 to 2026), and it calibrates
   the one-sided price within a point on both sides and bin by bin. Order of fallbacks: the
   same segment's two-way total, then the full-game total (0.989 / 1.028), then the moneyline
   pair (0.995 / 1.034), then a flat 1.05 (0.987), never a three-way market.
3. **The same shape lives in the full-game and first-inning run lines.** 503 and 1,137 closing
   rows. The full-game scorer has the identical defect on its 503 (0.493 against 0.512
   today). One shared run-line scorer for the three markets costs the same as one for the
   first five and removes a second copy of the arithmetic.
4. **The re-score can fix the home record and cannot create the away record.** The stored
   report carries no per-iteration first-five margin. The home record's market probability
   is the whole of the artefact on the row (the simulator's number and the outcome are
   right), so the re-scored reports give the correct read of the market as it stands; the
   away record adds a second bet per one-sided game from the next run of the backtest, and
   the row's record count grows by about 80% on a 2024 set.
5. **Two records per game need a distinct identity.** Decision 2 (2026-09-23): the away bet's
   record carries its own market key — `f5_runline_away`, `runline_away`, `f1_runline_away` —
   with the market type unchanged. No record field is added, the three pairing keys stay
   (game, market, player), and every stored report loads as it is; the away bet is its own
   row in every table and its own market in the composite objective.
6. **The first-inning (+1.0, +1.0) rows read as mis-stored.** 768 rows on 764 games, all
   loaded on 2026-09-13 (the 2025 re-load): home +1 at −1,600 with away +1 at +820, and the
   like. The home side covers 88.5% of the time against a 46.5% implied probability under
   every method; the away side 83.2% against 53.7%. Two prices that far apart on two bets
   that both win on a tie are not the two bets the lines describe. This is a loader or
   selection question on one market of one season, outside this ticket; the 2024 baseline
   has no such row. The shape tally (§5.2) prints them so they are not forgotten.

---

## 4. Data changes

None in Postgres or DuckDB. The store is right; nothing is re-loaded.

**The report JSON** (`scripts/clv_backtest.py` output) changes in three places, all
backward-compatible:

- `accuracy_records[*]` gain no field. The away bet's record carries `market =
  "<market_type>_away"` (`f5_runline_away`, `runline_away`, `f1_runline_away`) and the
  market's own `market_type`; the home bet's record keeps the market type as its key, the
  same key the pairs use, so every existing row, table and market list is unchanged.
- `counters.market_shapes` — per run-line market: `{"pairs": n, "one_sided": n,
  "skipped": n, "paired_overround_mean": x}`; and per two-way market the paired prices'
  mean implied sum, so the guard the skill table runs is printed by the backtest itself.
- `params.run_line_scoring = "sim549.1"` — the stamp the skill table's merge refuses to mix
  with an unstamped report; a re-scored report carries the same stamp plus
  `params.rescored = {"from": <path>, "markets": [...], "records": n, "date": ...}`.

**The stored reports** re-scored beside their originals (never in place), suffix `.sim549.json`:
`sim548_accuracy_split`, `sim548_baseline_2024_chunk{2,3,4}` (the 1,000-game baseline);
`sim518_accuracy_tto05` / `tto07` and `sim518_accuracy_pair_tto05` / `tto07` (the fatigue
arms); `sim427_accuracy_on` / `off` / `pair` (the manager arms); `sim548_accuracy_pair_split`.
The derived tables `sim548_baseline_1000_skill.{txt,json}` and `sim548_skill_first{250,500}`
are regenerated from the re-scored reports. The design runner's arm reports do not exist yet
(the design stopped at run 3 of 21 and is paused).

---

## 5. Code changes, file by file

### 5.1 `simulation/game_market_distributions.py` — the away side of a run line

`cover_probabilities` already returns `(p_home_covers, p_away_covers, p_push)` at a home
spread. The two public entry points learn a side, default `home`, so an away bet at its own
spread is one call:

```python
def market_probability(runs, market_type, *, line=None, side="home"):
    ...
    if kind == "runline":
        if line is None: raise ValueError(...)
        if side == "away":
            # an away bet at away spread A covers when away + A > home, i.e. home − away < A:
            # the away leg of cover_probabilities at the mirrored home spread −A
            return cover_probabilities(runs, market_type, -float(line))[1]
        return cover_probabilities(runs, market_type, line)[0]

def market_outcome(actual, market_type, *, line=None, side="home"):
    ...
    if kind == "runline":
        adjusted = margin + (float(line) if side == "home" else -float(line))
        if adjusted == 0.0: return None                       # a push, on an integer spread
        return int(adjusted > 0) if side == "home" else int(adjusted < 0)
```

`line` is always the record's OWN spread (the home spread for the home side, the away spread
for the away side). A total-kind market ignores `side`.

### 5.2 `scripts/clv_backtest.py` — one run-line scorer, the record, the tally

```python
# AccuracyRecord: unchanged (decision 2). The away bet's key is its own market:
def _run_line_market_key(market_type: str, side: str) -> str:
    return market_type if side == "home" else f"{market_type}_away"      # "f5_runline" / "f5_runline_away"
MARKET_TRUST |= {"runline_away": "caution", "f5_runline_away": "unvalidated", "f1_runline_away": "unvalidated"}

@dataclass(frozen=True, slots=True)
class ClosingPrices:
    side: float; other: float; line: float | None = None
    other_line: float | None = None                 # SIM-549: the away spread on a run line

def _closing_prices(odds, market_type, side_col, other_col, line_col, other_line_col=None): ...

DEFAULT_ONE_SIDED_MARGIN = 1.05                      # the closing two-way margin's league mean (total 1.048, f5 total 1.059)
_SEGMENT_TOTAL_OF = {"game": "total", "f5": "f5_total", "f1": "f1_total"}

def _run_line_is_pair(home_spread: float, away_spread: float | None) -> bool:
    return away_spread is not None and float(away_spread) == -float(home_spread)

def _reference_margin(odds, segment) -> tuple[float, str]:
    """The book's margin on the same game's two-way markets, the same segment first;
    never a three-way market (its margin prices the tie)."""
    for mt, a, b in ((_SEGMENT_TOTAL_OF[segment], "over_ml", "under_ml"), ("total", "over_ml", "under_ml"), ("moneyline", "home_ml", "away_ml")):
        cp = _closing_prices(odds, mt, a, b, None)
        if cp is not None:
            return _implied(cp.side) + _implied(cp.other), mt
    return DEFAULT_ONE_SIDED_MARGIN, "flat"

def score_run_line_market(game_pk, market_type, cp, *, sim_cover, actual_margin, odds) -> list[AccuracyRecord]:
    """sim_cover(side, line) → the simulator's cover probability of that side at ITS line;
    actual_margin = the real home − away margin over the segment."""
    if _run_line_is_pair(cp.line, cp.other_line):    # today's path, unchanged: one record, the home side, the pair de-vig
        outcome = _cover_outcome(actual_margin, cp.line, "home")
        if outcome is None: return []
        return [AccuracyRecord(game_pk, market_type, market_type, sim_cover("home", cp.line),
                               float(devig_two_way(cp.side, cp.other)[0]), outcome,
                               market_side_price=cp.side, market_other_price=cp.other)]
    margin, _src = _reference_margin(odds, GAME_MARKET_SEGMENT[market_type])
    out = []
    for side, price, line in (("home", cp.side, cp.line), ("away", cp.other, cp.other_line)):
        outcome = _cover_outcome(actual_margin, line, side)
        if outcome is None: continue                 # a push on an integer spread, that side only
        out.append(AccuracyRecord(game_pk, _run_line_market_key(market_type, side), market_type, sim_cover(side, line),
                                  min(_implied(price) / margin, 1.0), outcome,
                                  market_side_price=price, market_other_price=None))   # no single fade price: the return never fades it
    return out
```

`score_game_accuracy`'s run-line block becomes one call with `sim_cover = lambda side, line:
spread_cover_prob(summary, line if side == "home" else -line, MarketSide[side.upper()])` and
`actual_margin = home_score − away_score`; `score_segment_market_accuracy`'s run-line kind
becomes the same call with `sim_cover = lambda side, line: market_probability(runs,
market_type, line=line, side=side)` and the official grid's segment margin. The `_SEGMENT_COLUMNS`
row for the run-line kind gains `"away_spread"` as its fourth column. Every other record
(moneyline, total, yes / no, three-way, props) is built exactly as today.

**The tally and the warning**, computed from the records at the end of the run (no worker
plumbing): per run-line market the pairs, the one-sided records, the skipped rows; per market
the paired records' mean implied sum; a `log.warning` when a market's paired sum sits outside
1.00–1.15 or a run-line market has one-sided rows, so the next market with this shape is
named at load, not found at read time. Written under `counters.market_shapes`;
`params["run_line_scoring"] = "sim549.1"`.

### 5.3 The instruments and the guard

```python
# scripts/sim548_market_skill.py — _provenance_key() and GAME_MARKET_ORDER; the pairing keys are unchanged (decision 2)
... "run_line_scoring": p.get("run_line_scoring"),    # a merge of a stamped and an unstamped report refuses
GAME_MARKET_ORDER = [..., "runline", "runline_away", ..., "f5_runline", "f5_runline_away", ..., "f1_runline", "f1_runline_away", ...]
# scripts/sim518_pair_accuracy.py — _key(): unchanged; the away record pairs on its own key
```

The skill table's `_mean_overround` reads only records that carry both prices — after the
change, the pairs — so the guard keeps its band and its meaning and the row enters on its
own. Its note text drops the ticket reference. `score_hypothetical_return` needs no change:
a one-sided record has no fade price and `_favored_side_prob_and_price` already returns
`None` for the fade; the reference side is priced at the offered one-sided price, which is
the bet that existed.

### 5.4 `scripts/sim549_rescore_runlines.py` — the stored reports, re-priced

```python
# for each report path: load; collect the run-line records' games; read their CLOSING rows
# (DISTINCT ON the latest fetched_at, the three run-line markets + the two-way totals/moneyline)
# from raw.game_odds over BASEBALL_DB_DSN (psycopg2, as scripts/sim_stats.py does)
for r in records:
    if r["market_type"] not in ("runline", "f5_runline", "f1_runline"): continue
    row = closing[(r["game_pk"], r["market_type"])]
    assert _implied(row.home_spread_ml) == _implied(r["market_side_price"])            # the stored price IS the record's price
    if _run_line_is_pair(row.home_spread, row.away_spread): continue                       # a pair: today's number stands
    margin = report's own total record of the segment (its two prices) or _reference_margin(row)   # the report first, the store second
    r |= {"market_prob": min(_implied(row.home_spread_ml) / margin, 1.0), "market_other_price": None}   # the key stays the market type
report["accuracy_comparison"] = aggregate_accuracy_comparison(records, n_bootstrap=params.bootstrap_samples, seed=params.bootstrap_seed)
report["params"] |= {"run_line_scoring": "sim549.1", "rescored": {...}}
write <path>.sim549.json  (never in place); print per report: records re-priced by market, the row before / after
```

The away record is not created (§3, finding 4). The script refuses a report already stamped.

### 5.5 Documents

`docs/technical/pipeline-betting-db.md` (the backtest's game-market rows: the shape rule and
the reference margin); `docs/technical/scripts-frontend.md` (the new script);
`scripts/clv_backtest.py`'s module docstring ("De-vig + edge math"); `CHANGES.md`.

---

## 6. Weights, bandwidth, power

None. Nothing here touches the simulator, a draw, a weight or a calibration map. The
simulator's probability on every record is byte-identical before and after; the change is
to the market's number on one-sided run-line records and to the records' identity.

---

## 7. Tests

| Test | Holds |
|---|---|
| `test_first_five_run_line_two_bets_on_the_real_payload` (`tests/unit/test_sim421_segment_markets.py`) | game 744796's closing rows (home −1.5 at +350, away −1.5 at +150, the first-five total −109 / −121) and its official grid (4–4 after five): two records, keys `f5_runline` and `f5_runline_away`, both with market type `f5_runline`; the home market probability 0.2222 / 1.0690 = 0.2079, the away 0.4000 / 1.0690 = 0.3742; `market_other_price` `None` on both; both outcomes 0 (a tie is a loss for both); the simulator's two probabilities from a five-iteration fixture whose first-five margins are +2, +1, 0, −2, −3: home 0.2, away 0.4 |
| `test_run_line_pair_is_unchanged` | (−0.5, +0.5): one record, the pair de-vig, the same numbers as today's test |
| `test_run_line_mirror_rule` | (0, 0) is a pair; (+0.5, −1.5) and (−1.0, +1.5) are two bets, each at its own line; a missing away spread skips the row |
| `test_one_sided_push_drops_that_side_only` | home −1 / away 0 on a one-run home lead: the home record only, with a push on the away side |
| `test_reference_margin_order` | the same segment's total, then the full-game total, then the moneyline, then 1.05; a three-way market never used |
| `test_full_game_run_line_uses_the_same_rule` (`tests/unit/test_sim538_accuracy_comparison.py`) | `score_game_accuracy` on (+1.5, +1.5): two records priced from their own lines through `spread_cover_prob` on both sides |
| `test_market_probability_and_outcome_take_a_side` (`test_sim421_segment_markets.py`) | the away probability at spread A equals the away leg at −A; the outcome mirrors; a total ignores the side |
| `test_away_record_is_its_own_market` (`tests/unit/test_sim548_instruments.py`) | the home and away records of one game survive `composite` and `pair_records` as two markets; the skill table prints `f5_runline_away` on its own row after `f5_runline`; `trust_label` knows the three away keys |
| `test_rescore_script_on_a_toy_report` (`tests/unit/test_sim549_rescore.py`) | a report with one pair and one one-sided record plus an `f5_total` record: the one-sided record's market probability re-priced from the report's own total, the pair untouched, the sim probability untouched, the stamp written, a stamped report refused; the output beside the input |
| `test_skill_table_accepts_the_market_after_the_fix` (`test_sim548_instruments.py`) | records with `market_other_price` `None` leave the overround mean; the row enters |

The existing `test_skill_table_excludes_a_market_whose_prices_are_not_a_pair` stays as the
guard's own test. `test_yes_no_and_first_to_score_and_run_line_records` keeps its pair.

---

## 8. Run book

```text
# 1. the code lands; ruff, mypy, the unit lane (the four test files above); no engine change, no regression lane, no acceptance lane, no smoke — the simulator is untouched
# 2. the re-score, in the container (scripts/ is not bind-mounted; mount it):
#    docker compose run --rm -v "$PWD/scripts:/app/scripts" app python scripts/sim549_rescore_runlines.py \
#        scripts/sim548_accuracy_split.json scripts/sim548_baseline_2024_chunk2.json scripts/sim548_baseline_2024_chunk3.json scripts/sim548_baseline_2024_chunk4.json \
#        scripts/sim518_accuracy_tto05.json scripts/sim518_accuracy_tto07.json scripts/sim518_accuracy_pair_tto05.json scripts/sim518_accuracy_pair_tto07.json \
#        scripts/sim427_accuracy_on.json scripts/sim427_accuracy_off.json scripts/sim427_accuracy_pair.json scripts/sim548_accuracy_pair_split.json
#    → twelve <name>.sim549.json files; the script prints each report's first-five run-line row before and after
# 3. the skill table over the four re-scored baseline chunks → scripts/sim548_baseline_1000_skill.{txt,json} (the row enters; the composite's market list grows by one)
# 4. record the re-scored row in CHANGES.md against the 2026-09-15 table; delete the SIM-549 row from BACKLOG.xlsx
# 5. the next backtest run (any arm) carries the away records and the shape tally; nothing to schedule for this ticket
```

Expected on step 3 (from §2.3): the first-five run line n 986, gap about +0.007 (its
bootstrap range comes with the run), the line's bias −0.006, the note "behind the line".

---

## 9. What could go wrong (ranked)

1. **The reference margin is an estimate, not the book's own.** The one-sided price's margin
   is measured, not read: 0.978 / 1.016 against the rate on 3,216 rows, sd of the reference
   1.4%. A per-record error of about ±1.5% in the market's probability is a tenth of the
   Brier gaps the reads resolve. If the book's margin on an alternate line ever diverges from
   its main-line margin, the shape tally's paired sum and the row's bias show it.
2. **The away bet is its own market in the composite objective.** With equal weights the
   first-five run line carries two markets' weight once the away rows exist (from the next
   run); the `--weights` option can halve both. Each row is one bet on one side, so the
   fixed-side convention holds row by row.
3. **A merge of a re-scored report with an unstamped one.** The provenance key refuses it
   (§5.3); `--force` prints the difference.
4. **The first-inning (+1.0, +1.0) rows** (§3, finding 6) enter the first-inning row as two
   bets priced from their lines and are wrong under every method. The 2024 baseline has
   none; a 2025 read would carry 191 of them in 2,440 games, and the tally names them.
5. **A reader of an old report** sees no away rows and no stamp; the skill table prints the
   stamp's absence in the provenance line, so a pre-fix report is never mistaken for a
   re-scored one.

---

## 10. Decisions for the owner

1. **The reference margin — TAKEN 2026-09-23, as recommended:** the same game's two-way total of the same segment
   (the first-five total for the first five; the full-game total for the full game; the
   first-inning total for the first inning), then the full-game total, then the moneyline
   pair, then a flat 1.05; a three-way market never. Evidence §2.2: the first-five total
   calibrates both sides within a point and bin by bin. The alternatives not taken: the ticket's "first-five
   total and first-five moneyline" (the moneyline is three-way in the store; the mean
   over-corrects by a tenth); a flat 1.05 for every record (as good on the mean, blind to
   the game); the measured one-sided ratio itself (a fitted constant on the outcome, which
   is the thing being graded).
2. **The record's identity — TAKEN 2026-09-23, the alternative:** distinct market keys. The
   away bet's record carries `f5_runline_away` (`runline_away`, `f1_runline_away`) with the
   market type unchanged; no field joins the record, no pairing key changes, and the away bet
   is its own row in every table and its own market in the composite objective. The
   recommendation not taken: two optional fields on `AccuracyRecord` (`side`, `line`) and the
   side in the three pairing keys.
3. **The scope of the re-score — TAKEN 2026-09-23, as recommended:** re-price the home record in every stored
   report, written beside the original with a stamp, and regenerate the two derived skill
   tables; the away record joins from the next backtest run. The alternatives not taken: re-run the
   1,000-game baseline for the away records now (four arms of about 3.4 hours each on a
   paused design's machine); leave the stored reports and read the row only from new runs.
4. **The scope of the rule — TAKEN 2026-09-23, as recommended:** all three run-line markets through one scorer
   (the full-game scorer has the same defect on 3% of its rows, the first-inning on 10%).
   The alternative not taken: the first-five run line only, the ticket's words, leaving the full-game
   block's copy of the arithmetic as it is.

Recorded, not asked: the store is not touched and nothing is re-loaded; the skill table's
guard keeps its band; the hypothetical return prices a one-sided record at the offered price
and never fades it; the first-inning (+1.0, +1.0) rows of 2025 are a loader question I have
not filed — say the word and it gets a row; every similarity power is 1 and nothing here is a
weight.

---

## 11. Build record (2026-09-25)

### 11.1 What was built

- `simulation/game_market_distributions.py`: `market_probability` and `market_outcome` take
  `side="home" | "away"` for a run line (§5.1); `line` is the side's own spread.
- `scripts/clv_backtest.py`: `ClosingPrices.other_line`; `run_line_is_pair` (the mirror rule),
  `reference_margin`, `run_line_market_key`, `score_run_line_market` (one scorer for the three
  run lines, called by the full-game and the segment paths), `market_shape_tally`,
  `one_sided_line_check`, `warn_market_shapes`; the three away keys in `MARKET_TRUST`;
  `params.run_line_scoring = "sim549.1"` and `counters.market_shapes` in every report; one
  console line per run line. A pair's record is byte-identical to the old code's (the review
  compared 60,000 random pair rows, degenerate probabilities and integer spreads included).
- `scripts/sim548_market_skill.py` and `scripts/sim518_pair_accuracy.py`: the stamp in the
  provenance checks; the away keys in the market order.
- `scripts/sim549_rescore_runlines.py` (§5.4). Tests: 24 new in the three files of §7 that
  existed, and the new `tests/unit/test_sim549_rescore.py` (10).

### 11.2 What the build added to the plan, and why

1. **The reference-margin band (1.00–1.15).** A reference market whose two prices add outside it
   is skipped for the next. Every stored "total" outside the band holds two prices that cannot
   be the two sides of one line. The fallbacks it triggers: 24 first-five rows, 37
   first-inning, 4 full-game.
2. **The full-game run line keeps its refusal of a certain simulator probability**
   (`open_sim_prob_only`). The old edge report refused a probability of exactly 0 or 1; the
   segment path never did. The shared scorer keeps both behaviours, so every pair stays
   byte-identical.
3. **A guard on a one-sided line's own calibration.** The review's critic found that the change
   switched off the skill table's price-sum guard for the rows it re-shapes: a one-sided
   record has no fade price. On 2025 that guard had excluded the first-inning run line, whose
   (+1.0, +1.0) rows are mis-stored (§3 finding 6). The new check: a run line's one-sided
   records whose line sits more than 4 standard errors from their own outcome rate (30 records
   or more) are LINE SUSPECT and excluded; `warn_market_shapes` names them at load. Run on
   every season's real closing rows (2021–2026, both run lines, both sides): it excludes the
   2025 first-inning run line alone (home z −6.6, away z −4.4; the line says 0.68 and 0.69,
   the bets came true 0.76 and 0.75) and passes every other row (|z| ≤ 1.5).
4. **The re-score's two flags.** `--accept-problems` writes a report with a missing closing row
   or a price the store does not hold, and lists them in `params.rescored.problems`;
   `--overwrite` replaces an existing output. One `--force` did both, and stamped a report whose
   unpriced records kept the old number without saying so.
5. **The provenance tells a re-scored report from a fresh run.** A re-scored report holds the
   run lines' home bets only; the skill table and the paired read refuse to mix it with a fresh
   stamped run (`run_line_rescored` / `params.rescored`).
6. **The tally's names.** A game with one record, not two, is `one_record_games`: a push, a
   degenerate probability, or — in every re-scored game — the away bet the re-score cannot
   build. The sum of a two-way market's two implied probabilities is the "margin" throughout.
7. **`.gitignore`** names the eight 11 MB re-scored reports: each carries the run's DSN.

### 11.3 Corrections to this plan

1. **§8, the run book.** Four of the twelve files it lists (`sim518_accuracy_pair_tto05` /
   `tto07`, `sim427_accuracy_pair`, `sim548_accuracy_pair_split`) are paired reads, not reports:
   they hold no records. They are regenerated with `scripts/sim518_pair_accuracy.py` over the
   re-scored reports (`<name>.sim549.json`, with their `.txt`). The paired simulator-side
   differences are identical in every bucket.
2. **§4, the design's arm reports DO exist.** `scripts/sim548_design_20260915/run01.json` and
   `run02.json` were kept when the design paused. They are re-scored beside themselves. The
   design read uses the simulator's Brier only, so it is unaffected. But when the design resumes,
   the fresh arms carry the three away markets and run01 / run02 do not, so the design reads
   the away markets on no common game: re-run the two arms, or read the home bets only (the
   joint-fit plan's decision).
3. **§5.3, the hypothetical return.** `_favored_side_prob_and_price` returns `None` whenever
   either price is missing, so a one-sided record leaves the hypothetical return entirely (as a
   three-way record always did). The plan said its reference side is priced. The old record
   entered it priced wrong: its "fade" was the other team's separate bet.
4. **§7, `test_one_sided_push_drops_that_side_only`.** On a one-run home lead the HOME −1 pushes
   and the away 0 record stays; on a tie the away 0 pushes. The built test pins both.
5. **§2.2, the first-inning and full-game figures** were measured over the first-five total's
   margin. With the built rule (the segment's own total): the first-inning (+0.5, +0.5) rows
   read 0.765 / 0.748 against rates of 0.777 / 0.762, about a point low (inside §9.1's ±1.5%);
   the full-game two-bet rows 0.504 / 0.522 against 0.498 / 0.534, the line's Brier 0.2454 →
   0.2322 on them.
6. **A fresh run's table moves on untouched markets.** `aggregate_accuracy_comparison` divides
   alpha by the number of market keys and seeds each row by its sorted index. A fresh run adds
   up to three away keys, so every row's minimum sample and some rows' bootstrap ranges differ
   from a pre-fix run by construction. Compare arms with the paired read, not the backtest's
   own table.

### 11.4 The run book, as run (2026-09-25, in the app container)

1. The code: ruff, ruff format and mypy clean; the unit, regression and band-arithmetic lanes.
2. The re-score of the eight reports and the design's two arms (`--overwrite` on the second
   pass, after the tally's names changed): no problems in any report; every price matched the
   store; the reference margin came from the first-five total on 784 of the baseline's 789
   one-sided first-five records and from the full-game total on the other 5; on the 785 games
   where the report held the same total record, the two margins agreed on every one. The four paired reads
   regenerated.
3. The derived tables regenerated in place: `sim548_baseline_1000_skill.{txt,json}`,
   `sim548_skill_first{250,500}.json`, `sim548_market_calibration_2024split.{txt,json}`. Only the
   run-line rows moved (every other number within 10⁻¹⁰, the container's floating point against
   the host's). The 1,000-game first-five run line: n 986, gap +0.0066 [+0.0022, +0.0110], the
   line's bias −0.006, AUC 0.60 / 0.63 — "behind the line", and it enters the table. The
   full-game run line moves from +0.0052 to +0.0057; the calibration layer's first-five check
   reads the line at 0.2270, not 0.2566 (its maps fit the simulator's probability and do not
   change).
4. Closed: `CHANGES.md`; the SIM-549 row deleted from `BACKLOG.xlsx`.

### 11.5 Found next to the change (not built; for the owner)

1. **The 2025 first-inning (+1.0, +1.0) rows** (§3 finding 6) are still in the store. The new
   guard keeps them out of any read; the loader question stays open and unfiled.
2. **The live /line-movement and /clv routes** (`betting/line_movement.py`,
   `api/routes/betting.py`) still price a run line's two separate bets as one pair, and the
   /edges route takes the away line as the negative of the home line. Outside this ticket.
   **DONE 2026-09-25 (owner request, `CHANGES.md`):** the rule moved into
   `betting/clv_engine.py` and the three live pages use it — /edges and /signals take an
   `away_run_line` and report `run_line_pricing`; the line-movement series reports the
   shape and refuses a CLV across a moved spread; the game page says when a run line is two
   separate bets.
