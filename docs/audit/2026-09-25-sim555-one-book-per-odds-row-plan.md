# Build plan — every book's prices stored, one book per row, one graded book, and a guard against impossible rows (SIM-555)

> **STATUS 2026-10-01 — BUILT AND COMMITTED; the census PASSED (after three rule changes it found); every
> season 2019-2026 is re-loaded and checked (run book steps 1-7 done; the re-load survived several host blue
> screens caused by the Riot Vanguard driver `vgk.sys`); step 8, retiring the old rows, and the close-out
> remain — §12.** VERSION 2 was
> APPROVED the same day: all six decisions of §10 taken as recommended by the owner ("accept your
> recommendations for all of the open decisions"). §12 records the build: what landed, where it departs
> from this plan and why, and the state of the run book. The readable page is https://claude.ai/artifact/M2Rv8uM3M9zt5Yc21kFCC1 (v3, approved); this
> file is the record.
>
> **Version 2 follows two owner rulings of 2026-09-28.** First, closing line value (CLV: whether
> the opening price beat the close) is not a metric the model is judged by. So the design no
> longer needs a market's opening and closing rows to come from the same book, and the
> line-movement and CLV pages get the label filter and nothing more; the graded closing row
> is chosen for consistency across games, by a fixed preference list, not by who opened the
> market. Second, the platform tracks every book's price. The payload the provider already
> downloads carries every book's line for every side and keeps one; version 2 stores one
> labelled row per book, so the edge page can name the book with the best price, the
> accuracy comparison can measure which book is the sharpest, and every backtest can price
> at the best available line. On the 15 probe games the best price beats one book's by about
> a point of implied probability per side and halves the book's cut. §11 lists what changed
> from version 1.
>
> **The store cannot say which book any price came from, and the provider mixes books
> inside a row.** Every one of the 359,524 game-odds rows and 4,035,864 prop rows says
> `book = 'consensus'`. The provider picks each side's closing price on its own: the line
> with the newest update stamp across every book, ties to the last-listed book. On the 15
> games read for this design that rule split the two sides of a full-game closing row across
> two books on 7 of 45 rows, and in 2025–2026 it took bet365 on 13 of 15 moneylines because
> bet365 is listed last. The fix is a provider that returns one row per book, a stored book
> label and stamp, a load guard that refuses a row no book could have posted, a first-five
> rule set for the entries that carry first-inning prices, a graded-book preference the
> readers apply, and a re-load of eight seasons.

**Date:** 2026-09-25 (version 1); 2026-09-28 (version 2)
**Ticket:** SIM-555 (P1) in `BACKLOG.xlsx`. Next free ID: SIM-556.
**Evidence:** the scope note `docs/audit/2026-09-25-sim555-odds-book-mixing-scope.md` (the
census of every stored row, a 40-game payload sample, the code audit); the provider, the
loader, the live cycle, the accuracy comparison and the line-movement reader at commit
d44855b; a read-only probe of 15 games (2023–2026, 6 markets each, 107 vendor reads) run
for this design; the vendor's market and book catalogues; the real inning grids of 17,810
Final games (`raw.games.inning_scores`); the unmerged `wave1-remediation` branch's provider;
the database volume (926 GB free).
**Builds on:** the run-line shape work (SIM-549, 2026-09-25); the segment markets (SIM-421,
2026-09-12); the July 2026 bug register rows 1.8, SIM-bettingpros-1, SIM-oddsprovider-1,
B-N5 and 1.EX.devig-books, which this plan closes; the bet-selection analysis plan of
2026-09-14 (its analyses run on the real opportunity set once every book is stored).

---

## 0. The short version

**What the ticket asks for, as amended on 2026-09-28.** Every new odds row holds one book's
prices for all its sides and names the book. Every book the vendor lists is stored, one row
each. A load guard refuses, logs and counts a row that cannot be one bet. No first-five row
carries first-inning prices. Every closing line is stamped before first pitch, or the ticket
records why a later stamp is accepted. The live cycle stores each book under its own name. A
re-load replaces the 2019–2026 odds; the duplicate 2024 rows and, after a verification, the
old `consensus` rows are gone; until then every reader filters to the new label. A census
finds no mixed row. A read-only fetch of several hundred games traces every row to one
book. The accuracy baselines and the calibration layer run again, graded against one book
chosen by a fixed preference. The three defects of unknown cause each get an explanation,
starting from our own request parameters. *Struck by the owner's ruling:* "a market's
opening row and closing row come from the same book"; "the line-movement page reads its
start from the opening row and its end from the closing row; the live CLV refuses a total's
CLV when the total line moved".

**What the code and the data say.**

- **The provider has no notion of a book.** `_pick_line` runs once per side. For the
  closing line it scans every book's lines and keeps the newest `updated` stamp, ties to the
  last-listed book. The loader never passes a book, so every row is labelled `consensus`. On
  the 15 probe games the rule put the two sides of a full-game closing row in two different
  books on 7 of 45 rows, and chose bet365 for 13 of the 15 moneylines of 2025–2026.
- **The payload already carries every book.** One `/offers` read per (event, market) lists
  every book's line on every side: 8 to 11 sportsbooks on the full-game markets, 6 to 9 on
  the first-five run line, 4 to 9 on a strikeout prop, plus the vendor's blended line (book
  0) and, since 2025, a few daily-fantasy apps, an exchange and three prediction markets.
  The provider keeps one line per side and discards the rest.
- **Shopping the price is worth about a point a side.** On the probe's full-game markets
  the best sportsbook price beats DraftKings' by 1.0 (moneyline), 1.3 (run line) and 0.9
  (total) points of implied probability on average, up to 3.5; on strikeouts by 1.5, up to
  10. The two-side margin, the book's cut, falls from 4.4–4.7% at one book to 1.9–3.0% at
  the best of the market; on strikeouts from 6.3% to 3.5%. A point of implied probability is
  a point of edge.
- **The update stamp changed meaning in 2025.** In 2023–2024 the newest stamp of an offer
  sits 0.1 to 45 minutes BEFORE the scheduled start (a strikeout prop up to six hours), with
  7 to 13 distinct stamps per offer: the stamps are the times the lines changed. In
  2025–2026 the newest stamp sits 0.1 to 5.0 minutes AFTER the scheduled start on all 42
  offers, and on 19 of them every book's line carries that one stamp: the vendor now stamps
  a snapshot at game time. A guard that keeps only lines stamped before the scheduled start
  drops every 2025–2026 close. A guard at 15 minutes after the scheduled start drops none of
  the 90 and still refuses an in-play line.
- **Our market ids are the vendor's.** The catalogue reads 283 as "Fifth Inning Run Line",
  period `inning-5`; 282 "First Inning Run Line", `inning-1`; 281 "First 5 Innings Total
  Runs"; 279 "Fifth Inning Moneyline". The first-inning prices inside the first-five rows are
  not a wrong request. They sit in two books' entries within the vendor's first-five market.
- **Two books' first-five entries are first-inning bets.** FanDuel's first-five closing lines
  are flagged `is_off` on all 15 games; on 2025-06-01 its first-five OPENER copies its own
  first-inning opener (+1.5 at −800 / −1.5 at +520). DraftKings' first-five entries are real
  in 2024 (−1.5 at +280 / +150; +100 / +600) and first-inning bets on all six 2025–2026 games
  (both teams +0.5 at −380 / −500; both −1.5 at +750 / +550; the 2025-06-01 entry a copy of
  its first-inning entry within 0.01). With every book stored, those rows are refused by
  name and by shape, and the graded row falls to the next book on the list.
- **The real outcomes say which shapes are impossible.** Over 17,810 Final games of
  2019–2026 the segment is tied after one inning 53.3% of the time and after five 15.2%; a
  team leads by two or more after five 33.0% (home) / 27.3% (away), and never below 13.6% in
  any favourite bucket; after one inning 12.1% / 9.4%. So a first-five "both teams +0.5" pair
  whose prices add above 1.40 implies a tie above 35% and is a first-inning bet; a first-five
  tie priced above 0.35 is a first-inning tie. A −1.5 bet at +400 to +650 can be either
  segment (a heavy underdog's first-five bet or a favourite's first-inning bet), so no price
  rule separates the ±1.5 rows: the book census does.
- **The store carries the damage.** First-five run-line OPENING rows with a −1.5 bet at +400
  or longer or a +1.5 bet at −800 or shorter: 1,552 of 2,439 in 2025 (64%), 1,294 of 2,322
  in 2022, 336 in 2023; closing rows 676 in 2022, 226 in 2023, 125 in 2024. First-five
  moneyline rows whose tie price implies more than 0.35: 2,125 closing rows in 2022, 607 in
  2023, 205 in 2025 (649 opening). The equal-spread rows priced like a pair number 1,186
  (+1 / +1, 2025) plus 12 unclassified. The 2024 season holds 3,575 duplicate game rows and
  39,572 duplicate prop rows from its two loads.
- **The storage is affordable.** One row per book turns 2 rows per (game, market) into
  about 10 on the game markets and about 7 on the props: roughly 2 million game rows and
  20 million prop rows, 8 to 10 GB against today's 1.9 GB. The database volume has 926 GB
  free. The fetch time does not change: the rows come from the payload already in hand.

**The plan.** The provider returns one row per book for a (game, market, line type), each
row one book's prices for every side, labelled `book = 'bp:<id>'` and stamped
(`book_line_at`, migration 0028). The opening row is the opener's (the feed carries one
opener per side); the closing rows are every book's. The vendor's blended line is stored as
`bp:0` and never graded; daily-fantasy apps, exchanges and prediction markets are stored and
told apart by a kind table in the vocabulary, so a reader can include or skip them. A pure
guard module refuses a row that cannot be one bet: spreads of different size, equal non-zero
spreads priced like a pair (1.00–1.10), an over and an under at different lines, a missing
side, a first-five "win or tie" pair or tie price that implies a first-inning tie, a closing
line stamped more than 15 minutes after the scheduled start. The first-five markets also
drop a book whose entry copies its own first-inning entry, and DraftKings from 2025-03-01, a
dated exclusion the 300-game census confirms before the re-load. The loader and the live
cycle log and count every refusal. Every reader filters to the label; the accuracy
comparison grades against ONE book, the first on a fixed preference list that has a valid
closing row for the market, the same list on every game, and records beside each record the
best sportsbook price at the graded line. After the 2024–2025 load, a pure odds-versus-outcome
read ranks the books by forecast quality on the fixed-line markets, and the owner sets the
list from it; until then DraftKings leads. The live cycle stores every book's current line
for the day's games, so the edge page can name the book with the best price. The re-load
runs detached, 2024 and 2025 first, then 2026 and 2023, then 2019–2022 (about 73 hours in
all, the app up); after the census passes, the old rows are archived and deleted. Seventeen
tests. No simulator change.

**Decisions** in §10: six, all taken as recommended on 2026-09-28.

---

## 1. The mechanism

**How a row is written today.** `scripts/load_historical_odds.py` asks the provider for one
`get_odds(game_pk, line_type, market_type)` dict per (line type, market) and one
`get_prop_odds` dict per (player, market, line type), and persists each through the live
pipeline's `_persist_odds` / `_persist_prop_odds` (the SIM-092 dedup: `odds_hash` over the
row's prices and labels, `ON CONFLICT DO NOTHING`). The loader never passes `book`, so the
provider's default `book="consensus"` reaches the row. `raw.game_odds.book` is a
`VARCHAR(50)` with no check constraint; `fetched_at` is the only time on the row.

**How the provider picks a price** (`pipeline/bettingpros_odds_provider.py:517–566`):

```python
def _pick_line(self, selection, line_type):                 # ONE SIDE at a time
    if line_type == "opening":
        ol = selection.get("opening_line") or {}            # the opener's price: {line, cost, book_id, created}
        return cost, line                                    # the book id is dropped
    if line_type == "closing":
        for book in selection["books"]:                      # every book, book 0 (the consensus blend) included
            for ln in book["lines"]:
                if best_line is None or ln["updated"] >= best_updated:   # newest stamp; a tie goes to the LAST-listed book
                    best_line = ln
        return cost, line                                    # the book id and the stamp are dropped; every other book is discarded
    ...                                                      # "current": the line flagged best, else main, else the first
```

`_fill_moneyline`, `_fill_runline`, `_fill_total`, `_fill_yes_no` and `get_prop_odds` call
it per selection and write the side's price into the row. A total's `total_line` is the
LAST selection's line (the under's), so the over's price can belong to another line. Nothing
reads `is_off` or `active`. Nothing compares the two sides' books or lines. Nothing keeps
the other books.

**The feed's shape** (one `/offers` payload per event and market; captured under
`tests/fixtures/bettingpros/`): an offer holds selections (home, away, draw; over, under);
a selection holds `opening_line` (`line`, `cost`, `book_id`, `created`) and `books`, each
with `id` and `lines`; a line holds `line`, `cost`, `updated`, `main`, `best`, `active`,
`is_off`. Every fixture and every probe payload lists ONE line per book per selection. One
payload therefore holds one opening row (the opener's) and one closing row per book.

**Who reads the rows.** The accuracy comparison (`scripts/clv_backtest.py`
`_fetch_game_odds` / `_fetch_prop_odds`) takes the latest row per (market, line type) by
`fetched_at`, closing only, no book filter, and de-vigs the two prices as one bet. The
line-movement reader (`betting/line_movement.py` `fetch_line_movement`) reads every row of a
(game, market) ordered by `fetched_at`, groups by `book` (one group: `consensus`), and calls
the first row the open and the last the close. Three scripts read the store directly with
the same latest-row rule (`sim549_rescore_runlines.py`, `sim427_manager_probe.py`,
`sim518_fatigue_probe.py`). The live prop cycle (`PROP_BOOKS`) would write one price set
four times under four names, two of which the vendor does not carry; it is off by default.

---

## 2. The evidence

### 2.1 The probe: 15 games, six markets, what each rule would have picked

The probe (`scratchpad/sim555/bp_probe.py`, read-only) took one Final game per month
(April, June, August, September) of 2023–2026 with a stored closing moneyline, and read the
moneyline, run line, total, first-inning run line, first-five run line and one strikeout
prop of each: 90 offers, 107 vendor reads at one a second.

| Season | Offers | An opener on every side | The opener quotes every side at the close | Newest stamp after the scheduled start | Every line at one stamp |
|---|---:|---:|---:|---:|---:|
| 2023 | 24 | 24 | 22 | 0 | 2 |
| 2024 | 24 | 24 | 23 | 0 | 4 |
| 2025 | 24 | 24 | 23 | 24 | 11 |
| 2026 | 18 | 18 | 16 | 18 | 8 |

The opener by market: moneyline / run line / total FanDuel 13, DraftKings 2; strikeouts
FanDuel 9, DraftKings 5, BetMGM 1; first-inning run line FanDuel 9, Fanatics 5, DraftKings 1;
first-five run line FanDuel 7, DraftKings 3, bet365 2, BetMGM 2, Fanatics 1. The six offers
whose opener does not quote the close are all first-five run lines opened by FanDuel, whose
closing lines are flagged `is_off`. (Version 1 anchored the rows on the opener; version 2
stores every book and grades one by preference, so the opener matters only for the opening
row, which the feed carries from that one book.)

**The old rule's closing pick** (the book each side came from): 2023–2024 a different book
almost every game (PartyCasino, SugarHouse, Caesars, FanDuel, BetRivers, theScore, DraftKings);
2025–2026 bet365 on 13 of 15 moneylines, because the snapshot stamp ties and bet365 is
listed last. The two sides split across two books on 7 of the 45 full-game closing rows
(a run line 2023-09-08 DraftKings / FanDuel; a moneyline and a total 2025-04-08 bet365 /
PartyCasino; run lines 2025-06-01, 2025-09-12 and 2026-08-01; a total 2025-08-19).

### 2.2 The update stamp: two regimes

| Season | Newest stamp minus the scheduled start (moneyline) | Distinct stamps per offer | Reading |
|---|---|---|---|
| 2023 | −9.4, −2.3, −7.8, −28.7 min | 7–10 | the times the lines changed |
| 2024 | −22.2, −45.5, −23.2, −4.5 min | 9–12 | the same |
| 2025 | +0.2, +0.7, +1.4, +3.5 min | 1–4 | a snapshot at game time |
| 2026 | +1.3, +0.1, +0.2 min | 1–4 | the same |

The MLB live feed did not return a first-pitch time for these games, so the comparison is
with the scheduled start. In 2025–2026 no offer's newest stamp sits more than 5.0 minutes
after it, and the lines at that stamp are the pre-game lines of every book. An in-play
line would carry a stamp minutes to hours later. A grace of 15 minutes after the scheduled
start keeps every close in the sample and refuses an in-play line.

### 2.3 The real segment outcomes (17,810 Final games, 2019–2026)

| Event | Rate | The price a book posts for it |
|---|---:|---|
| the score is tied after one inning | 0.533 | both teams "+0.5" at about −400 each; the tie at about −110 |
| the score is tied after five innings | 0.152 | both teams "+0.5" at −105 to −140; the tie at about +400 |
| home leads by two or more after one inning | 0.121 | −1.5 at +540 to +710 |
| away leads by two or more after one inning | 0.094 | −1.5 at +650 to +900 |
| home leads by two or more after five | 0.330 (0.145 to 0.600 across the moneyline buckets) | −1.5 at +100 to +550 |
| away leads by two or more after five | 0.273 (0.136 to 0.522) | −1.5 at +100 to +600 |

Two shapes separate cleanly. A first-five row that prices both teams "+0.5" as favourites
(the implied probabilities add above 1.40) says the tie is more likely than 35%: impossible
after five innings, usual after one. A first-five tie priced above 0.35 is a first-inning
tie. The −1.5 bets do not separate: a real first-five −1.5 on a heavy underdog and a real
first-inning −1.5 on a favourite both sit at +400 to +650. The scope note's "smaller implied
probability below 0.20" reads the ±1.5 pairs; on a pair the favourite's side tells the
segment, but a book can list the two sides as separate bets at any line, so the design does
not rest a hard rule on it.

### 2.4 The two books whose first-five entries are first-inning bets

| Game | FanDuel's first-five entry | DraftKings' first-five entry | DraftKings' first-inning entry |
|---|---|---|---|
| 2024-08-14 | +0.5 −102 / −0.5 −128, `is_off` | −1.5 +280 / −1.5 +150 (real) | none |
| 2024-09-22 | +1.5 −118 / −1.5 −110, `is_off` | −1.5 +100 / −1.5 +600 (real) | none |
| 2025-04-08 | +1 −1300 / +1 +730, `is_off` | +0.5 −380 / +0.5 −500 | +0.5 −360 / +0.5 −475 |
| 2025-06-01 | +1.5 −750 / −1.5 +490, `is_off`; the OPENER +1.5 −800 / −1.5 +520 | +0.5 −295 / +0.5 −600 | +0.5 −295 / +0.5 −550 |
| 2025-08-19 | +1.5 −1250 / −1.5 +710, `is_off` | −1.5 +750 / −1.5 +550 (the opener) | none |
| 2025-09-12 | +1.5 −900 / −1.5 +570, `is_off` | +0.5 −340 / +0.5 −350 (the opener) | none |
| 2026-04-08 | −0.5 +122 / +0.5 −160, `is_off` | +0.5 −400 / +0.5 −400 | none |
| 2026-06-28 | −0.5 +104 / +0.5 −135, `is_off` | +0.5 −400 / +0.5 −370 | none |

FanDuel's first-five closing lines are `is_off` on every game of the sample; its first-five
opener is sometimes a copy of its first-inning opener (2025-06-01) and sometimes a real
first-five line (2026-04-08, 2026-06-28). DraftKings' first-five entries are real through
2024 and first-inning bets on every 2025–2026 game read: two carry its own first-inning
entry within 0.01 of the same price, four have no first-inning entry to compare with, and
their shapes (both "+0.5" as −340 to −500 favourites; both −1.5 at +550 and +750) are
first-inning shapes by §2.3. bet365 lists the first-five run line at 0 / 0 (a pick'em: the
home team wins the first five, the away team wins them, a tie refunds), a pair by the
SIM-549 rule.

### 2.5 The store today: what the guard and the first-five rules would touch

Latest row per (game, market, line type), every season:

| Rule | full-game run line (close / open) | first-five run line | first-inning run line |
|---|---|---|---|
| spreads of different size | 243 / 0 | 558 / 66 | 157 / 0 |
| equal non-zero spreads (any price) | 260 / 32 | 2,731 / 2,571 | 980 / 799 |
| of those, priced like a pair (1.00–1.10) | — | 418 opening rows of 2025 | 577 opening + 191 closing rows of 2025 |
| a −1.5 at +400 or longer, or a +1.5 at −800 or shorter | — | 2022: 676 / 1,294; 2023: 226 / 336; 2024: 125 / 23; 2025: 14 / 1,552; 2026: 7 / 147 | — |

First-five moneyline rows whose tie price implies above 0.35: 2022 2,125 closing / 2,303
opening; 2023 607 / 609; 2025 205 / 649; 2026 8 / 10; 2021 and 2024 none. Total-kind rows
(totals, team totals, a run in the first inning) with a side missing: 3 of 16,845 full-game
totals; props 2024–2025: 5,180 of 799,552 latest closing rows. Three-way rows without a tie
price: 67 first-inning, 35 first-five. The two odds tables hold 211 MB and 1,670 MB.

### 2.6 The vendor's catalogue

`/v3/markets?sport=MLB` (45 markets): 122 Moneyline, 175 Total Runs, 176 Run Line (period
`game`); 278 First Inning Moneyline and 279 Fifth Inning Moneyline; 280 First Inning Total
Runs and 281 First 5 Innings Total Runs; 282 First Inning Run Line and 283 Fifth Inning Run
Line (periods `inning-1` / `inning-5`); 277 Total Runs by Team, 286 Team to Score First, 369
Run in the First Inning?, 407 Fifth Inning Team Total Runs; 285 Strikeouts, 404 Hits
Allowed, 405 Outs Recorded, 408 Walks Allowed. Every id the provider requests is the
vendor's id for that market. The vendor's own page title for market 283 reads "MLB First
Inning Run Line", a label slip that sits beside the mislabelled entries. `/v3/books` lists
59 books: the sportsbooks (FanDuel 10, DraftKings 12, Caesars 13, Fanatics 14, SugarHouse
15, BetRivers 18, BetMGM 19, bet365 24, PartyCasino 27, theScore 33, Hard Rock 49, and
others without MLB lines), the vendor's blend (0), the daily-fantasy apps (Underdog 36,
PrizePicks 37, Fliff 39, Sleeper 63), an exchange (ProphetX 38, Novig 60) and the prediction
markets (Kalshi 68, Polymarket 73, Polymarket US 75). Pinnacle (2) is listed and quotes no
MLB line in any sample.

### 2.7 What storing every book buys, and what it costs (version 2)

The books quoting each probe offer, and the gain from taking the best sportsbook price at
the side's most common line instead of one book's (the vendor's blend, the daily-fantasy
apps, the exchange and the prediction markets excluded):

| Market | Sportsbooks quoting (mean, range) | Best price beats DraftKings' by, implied-probability points (mean / max) | Beats FanDuel's by | Two-side margin: one book vs best of market |
|---|---|---|---|---|
| moneyline | 9.3 (8–11) | 1.00 / 3.50 | 0.76 / 2.48 | 4.36% vs 2.35% |
| run line | 9.6 (8–11) | 1.33 / 3.34 | 1.43 / 3.49 | 4.50% vs 1.88% |
| total | 9.6 (8–11) | 0.89 / 3.33 | 1.00 / 3.26 | 4.70% vs 2.96% |
| strikeouts | 6.5 (4–9) | 1.52 / 10.01 | 1.54 / 6.32 | 6.33% vs 3.50% |

A point of implied probability is a point of edge; shopping the price roughly halves the
book's cut. (The first-five run line's figure is not shown: DraftKings' 2025–2026 entries
are first-inning bets, §2.4, and the comparison is meaningless until they are refused.)

**The storage.** Today's tables hold 2 rows per (game, market) and per (game, player, prop):
one opening, one closing. Version 2 holds one opening row (the opener's) and one closing row
per book: about 10 rows per game market (9 sportsbooks plus the blend; up to 16 with the
other kinds) and about 7 per prop. At today's bytes per row (590 on the game table, 415 on
the prop table, indexes included): about 2 million game rows (1.2 GB) and 20 million prop
rows (8 GB), 8 to 10 GB in all against 1.9 GB today. The database volume has 926 GB free;
every Docker volume together holds 14.5 GB. The vendor reads do not change: the rows come
from the payload already in hand. The inserts do: 22 million single-row inserts at about a
millisecond each add roughly six hours to the 73-hour re-load, so the writers batch the rows
of one offer in one statement.

---

## 3. Findings that shape the plan

1. **One row per book is the natural unit.** The feed lists one line per book per
   selection, so a book's row is one book's prices for every side by construction. Storing
   every book keeps every option the market offered; the graded row is a reader's choice,
   changeable without a re-load.
2. **The graded book is a preference, applied the same way on every game.** With CLV out of
   the frame the closing row needs no relation to the opening row. One fixed list gives the
   accuracy comparison the same benchmark book game after game; the first book on the list
   with a valid closing row for the market is the graded row. The order is measured, not
   assumed: once 2024–2025 are loaded, a pure odds-versus-outcome read on the fixed-line
   markets ranks the books by forecast quality, and the owner sets the list from it.
3. **The opening row stays, unanchored.** The feed's opener is one book per side; its row is
   stored under its own label. Nobody needs it to match the graded closing book.
4. **The stamp is worth a column.** Storing `book_line_at` lets the census re-run from the
   store, records the 2025 snapshot regime on every row, and makes the 15-minute guard
   auditable. The hash does not take the stamp, so an unchanged line re-loaded later still
   deduplicates.
5. **The first-five rows need three rules and a dated exclusion, not a price band.** The
   twin rule catches FanDuel's opener and DraftKings when it posts both; the tie rules catch
   the first-inning shapes with no twin; the ±1.5 rows overlap in price, so DraftKings'
   first-five entries are excluded from 2025-03-01 by name, and the 300-game census before
   the re-load confirms the date and finds any other book. With every book stored, a refused
   row costs nothing: the graded row falls to the next book on the list.
6. **The guard is a pure function both writers call, per row.** The loader and the live cycle
   share the persist path; a guard module beside the provider keeps the rules in one place,
   testable without a payload, with the counts in the loader's summary.
7. **The readers filter by label and pick by preference from the first day of the re-load.**
   The comparison takes the first preferred book's row; a half-loaded season would otherwise
   mix old and new rows game by game. A stamp in the report's provenance
   (`odds_row_version = "sim555.2"`, with the preference list) keeps the skill table from
   merging reads graded against different books.
8. **The best price rides on the record.** Each accuracy record carries the best sportsbook
   price at the graded line for its side, and the book. The bet-selection analyses and any
   edge read then price at what a bettor could take, not at one book's number.
9. **The re-load's order follows the readers.** The 1,000-game baseline and the paired reads
   are 2024; the hold-out is 2025; those two seasons load first.
10. **The stored accuracy reports cannot all be re-scored from disk.** A market with no line
    re-prices from the new graded row; a total, a run line or a prop whose line moved needs
    the simulator again. The re-score script does the first and counts the second.

---

## 4. Data changes

**Migration 0028** (`db/migrations/versions/0028_sim555_odds_book_line_stamp.py`):

```sql
ALTER TABLE raw.game_odds ADD COLUMN IF NOT EXISTS book_line_at TIMESTAMPTZ NULL;   -- the vendor's stamp on the row's line
ALTER TABLE raw.prop_odds ADD COLUMN IF NOT EXISTS book_line_at TIMESTAMPTZ NULL;   -- (the opener's 'created' on an opening row, the line's 'updated' otherwise)
CREATE INDEX IF NOT EXISTS idx_game_odds_market_book ON raw.game_odds (game_pk, market_type, line_type, book);   -- the graded-row and best-price reads
CREATE INDEX IF NOT EXISTS idx_prop_odds_market_book ON raw.prop_odds (game_pk, player_id, prop_stat, line_type, book);
CREATE TABLE IF NOT EXISTS raw.game_odds_archive (LIKE raw.game_odds INCLUDING DEFAULTS);  -- the retired consensus rows (decision 5)
CREATE TABLE IF NOT EXISTS raw.prop_odds_archive (LIKE raw.prop_odds INCLUDING DEFAULTS);
-- downgrade: drop the two columns, the two indexes and the two archive tables (the archives are copies)
```

No DuckDB change. No constraint on `book`: the label `bp:<id>` fits `VARCHAR(50)`. No
kind column: the book's kind (sportsbook, blend, daily fantasy, exchange, prediction market)
is a lookup on its id in the vocabulary. The dedup hash keys `book`, so a new row never
collides with a `consensus` row and two books' rows never collide with each other.

**The rows.** For each (game, market, line type): the opening row from the opener, and one
closing row per book the payload lists (the blend as `bp:0`), each carrying `book`,
`book_line_at`, `is_sharp_book = false`, and only the requested market's columns (the three
full-game markets no longer share one row). A refused row is not written; the loader's
summary counts refusals by rule, by market and by book. The live cycle writes the same
shape on its cadence for the day's games; the dedup keeps it to real changes.

**The re-load** (about 73 hours of vendor reads plus the batched inserts, detached, the app
up; the DuckDB lock is not involved): 2024 and 2025 first, then 2026 and 2023, then
2019–2022. Every reader filters to `book LIKE 'bp:%'` from the start, so a half-loaded
season never mixes. **After the census on the new rows passes,**
`scripts/sim555_retire_consensus_rows.py` copies every `consensus` row into the archive
tables and deletes it (359,524 + 4,035,864 rows, per season in one transaction each), which
also removes the 3,575 + 39,572 duplicate 2024 rows.

**The stored accuracy reports** stay as they are; `scripts/sim555_rescore_reports.py` writes
`<name>.sim555.json` beside each (the fixed-line markets re-priced against the graded book,
the moved-line records counted), as the run-line re-score did.

---

## 5. Code changes, file by file

### 5.1 `pipeline/odds_provider.py` — the book vocabulary (one source, as for the markets)

```python
# SIM-555 (2026-09-28): the books the platform names. Ids are BettingPros' (/v3/books).
BOOK_NAMES: dict[int, str] = {0: "BettingPros Consensus", 10: "FanDuel", 12: "DraftKings", 13: "Caesars",
    14: "Fanatics", 15: "SugarHouse", 18: "BetRivers", 19: "BetMGM", 24: "bet365", 27: "PartyCasino",
    33: "theScore Bet", 36: "Underdog", 37: "PrizePicks", 38: "ProphetX", 39: "Fliff", 49: "Hard Rock",
    60: "Novig", 63: "Sleeper", 68: "Kalshi", 73: "Polymarket", 75: "Polymarket US"}
BOOK_KIND: dict[int, str] = {0: "blend", 36: "dfs", 37: "dfs", 39: "dfs", 63: "dfs", 38: "exchange", 60: "exchange",
    68: "prediction", 73: "prediction", 75: "prediction"}          # as built: the 11 sportsbooks listed by name, more kinds
                                                                    # from /v3/books; an unlisted id is "unknown", never bettable (§12)
BOOK_IDS_BY_NAME: dict[str, int] = {"fanduel": 10, "draftkings": 12, "caesars": 13, "fanatics": 14, "sugarhouse": 15,
    "betrivers": 18, "betmgm": 19, "bet365": 24, "partycasino": 27, "thescore": 33, "hardrock": 49}
BOOK_LABEL_PREFIX = "bp:"
STORED_BOOK_FILTER_SQL = "book LIKE 'bp:%'"          # every reader appends this until the consensus rows are gone

#: SIM-555 (decision 1): the graded book, in order. The accuracy comparison takes the first book on the list with a
#: valid closing row for the market, the same list on every game. Set from the sharpness read (§5.6) after the
#: 2024-2025 load; DraftKings leads until then.
GRADED_BOOK_PREFERENCE: tuple[int, ...] = (12, 10, 19, 13, 14, 18, 15, 24, 33, 49)
#: the kinds whose rows count as a price a bettor can take (decision 6)
BETTABLE_KINDS: frozenset[str] = frozenset({"sportsbook"})

def book_label(book_id: int) -> str: return f"{BOOK_LABEL_PREFIX}{int(book_id)}"          # 'bp:10'
def book_id_from_label(label: str) -> int | None: ...                                        # 'bp:10' -> 10; 'consensus' -> None
def book_kind(label: str) -> str: ...                                                        # 'bp:0' -> 'blend'; 'bp:12' -> 'sportsbook'
def book_display_name(label: str) -> str: ...                                                # 'bp:10' -> 'FanDuel'; unknown -> the label
def graded_row_order_sql() -> str: ...   # "array_position(ARRAY['bp:12','bp:10',...]::varchar[], book) NULLS LAST, fetched_at DESC"
```

The `OddsProvider` protocol gains two methods with defaults, so the mock still conforms:
`get_odds_by_book(game_pk, *, line_type, market_type) -> list[dict]` (one dict per book, the
`get_odds` shape) and `get_prop_odds_by_book(game_pk, player_id, prop_stat, *, line_type)
-> list[dict]`. `get_odds` / `get_prop_odds` keep their signatures and return the first row
of the by-book list in `GRADED_BOOK_PREFERENCE` order (the single-row callers and the mock
are untouched).

### 5.2 `pipeline/bettingpros_odds_provider.py` — one row per book

```python
#: the first-five market -> the first-inning market a mislabelled entry copies (§2.4)
F5_TWIN_MARKETS: dict[int, int] = {283: 282, 281: 280, 279: 278}
#: a book whose first-five entries are first-inning bets from this date (§2.4; the census confirms it — decision 3)
F5_EXCLUDED_BOOKS: dict[int, date] = {12: date(2025, 3, 1)}
TWIN_PRICE_TOLERANCE = 0.03      # implied-probability gap under which a first-five entry "copies" the first-inning one

def _usable(ln) -> bool:   # a line the book is taking
    return not ln.get("is_off") and ln.get("active") is not False and ln.get("cost") is not None

def _book_line(sel, book_id) -> dict | None:   # the book's one usable line on the selection (the feed lists one; a second is logged and ignored)

def _opener(sel) -> tuple[int | None, dict]: ol = sel.get("opening_line") or {}; return ol.get("book_id"), ol

def _books_quoting(selections) -> list[int]:
    """Every book id with a usable line on EVERY priced side, in the payload's order."""

def _pick_line(self, selection, line_type, book_id) -> tuple[float | None, float | None, str | None]:
    """(cost, line, stamp) of ONE book on one selection. Replaces the per-side scan."""
    if line_type == "opening":
        bid, ol = _opener(selection)
        return (_opt_float(ol.get("cost")), _opt_float(ol.get("line")), ol.get("created")) if bid == book_id else (None, None, None)
    ln = _book_line(selection, book_id)
    return (None, None, None) if ln is None else (_opt_float(ln.get("cost")), _opt_float(ln.get("line")), ln.get("updated"))

def _f5_exclusions(self, event_id, market_id, selections, game_date) -> frozenset[int]:
    """Books whose first-five entry is a first-inning bet: the dated exclusions, plus every book whose
    lines equal its own first-inning lines on both sides with prices within TWIN_PRICE_TOLERANCE.
    The opener counts too: an opener that copies its first-inning opener gives no opening row."""
    if market_id not in F5_TWIN_MARKETS: return frozenset()
    f1 = self._selections(event_id, F5_TWIN_MARKETS[market_id])      # cached per (event, market); one more read per game
    ...

def get_odds_by_book(self, game_pk, *, line_type="current", market_type="moneyline") -> list[dict]:
    """One row per book. 'opening': one row, the opener's (None if the sides name two openers, or the
    opener is excluded). 'closing' / 'current': one row per book quoting every side, the blend included,
    the excluded books left out. Each row: the market's own columns, book='bp:<id>', book_id, book_line_at
    (the opener's 'created'; else the newest stamp of the row's sides), scheduled_start (for the guard, not
    stored), over_line / under_line on a total-kind market (for the guard; the row keeps total_line)."""

def get_odds(self, game_pk, *, line_type="current", market_type="moneyline", book="consensus", is_sharp_book=False) -> dict:
    """The protocol's single row: `book` names a book ('draftkings', 'bp:12') -> that book's row or an empty row;
    'consensus' (the default) -> the first row of get_odds_by_book in GRADED_BOOK_PREFERENCE order."""
```

`get_prop_odds_by_book` / `get_prop_odds` follow the same pattern over the offer's two
selections. The three full-game markets fill only their own columns (`LEGACY_GAME_MARKET_TYPES`
and its branch are deleted). A three-way row whose book lists no tie leaves `draw_ml` empty.
The "current" scan for the best price is deleted; `prefer_book_id` on the constructor stays
as a pin for every call.

### 5.3 `pipeline/odds_row_guard.py` — new: the load guard

```python
"""SIM-555: refuse a row that cannot be one bet. Pure; both writers call it on every row before persisting."""
PAIR_PRICE_BAND = (1.00, 1.10)      # equal non-zero spreads whose prices add like a pair: one book's mislabelled entry (1,186 + 12 rows today)
F5_TIE_IMPLIED_MAX = 0.35           # a first-five tie cannot exceed this (real 0.152; a first-inning tie 0.533)
F5_WIN_OR_TIE_SUM_MAX = 1.40        # both teams "+0.5": the two implied probabilities add to 1 + P(tie) + the margin
CLOSING_STAMP_GRACE = timedelta(minutes=15)   # the vendor's snapshot sits 0-5 min after the scheduled start since 2025

def refuse_reason(row: Mapping[str, Any]) -> str | None:
    kind, segment = GAME_MARKET_KIND.get(mt), GAME_MARKET_SEGMENT.get(mt)      # a prop row is kind "total", segment "game"
    if kind == "runline":
        if None in (hs, as_, hml, aml): return "a side is missing"
        if abs(hs) != abs(as_): return f"spreads of different size ({hs:+g} / {as_:+g})"
        if hs == as_ != 0 and PAIR_PRICE_BAND[0] <= p(hml) + p(aml) <= PAIR_PRICE_BAND[1]: return "equal spreads priced like a pair"
        if segment == "f5" and hs == as_ == 0.5 and p(hml) + p(aml) > F5_WIN_OR_TIE_SUM_MAX: return "a first-five win-or-tie pair implying a first-inning tie"
    elif kind in ("total", "yes_no"):
        if None in (over, under, line): return "a side is missing"
        if row.get("over_line") != row.get("under_line"): return "the over and the under at different lines"
    elif kind == "three_way":
        if None in (home, away): return "a side is missing"
        if segment == "f5" and draw is not None and p(draw) > F5_TIE_IMPLIED_MAX: return "a first-five tie priced like a first-inning tie"
    elif kind == "moneyline" and None in (home, away): return "a side is missing"
    if line_type == "closing" and stamp and start and stamp > start + CLOSING_STAMP_GRACE:
        return f"stamped {minutes:.0f} minutes after the scheduled start"
    return None

class RefusalTally: counts by (reason, market, book); `summary()` for the loader's log; `warn_if_share_above(0.05)`
```

### 5.4 The writers

```python
# scripts/load_historical_odds.py — _load_game_odds / _load_prop_odds take the by-book lists:
for row in provider.get_odds_by_book(game_pk, line_type=lt, market_type=mt):
    reason = refuse_reason(row)
    if reason: tally.add(reason, mt, row["book"]); log.info("refused game %s %s/%s %s: %s", ...); continue
    kept.append(row)
await persist_many(game_pk, kept)                 # one statement per offer (executemany), the SIM-092 dedup unchanged
# the final log line prints tally.summary() and the rows written per book; a warning when refusals exceed 5% of the rows offered
# --book NAME restricts a run to one book's rows (a smoke, or a top-up)

# pipeline/live/live_ingestion_pipeline.py
# PROP_BOOKS deleted: the cycles call get_odds_by_book / get_prop_odds_by_book and persist every kept row on the cadence
# _persist_odds / _persist_prop_odds: the INSERT takes book_line_at; a _persist_many variant batches one offer's rows
# _odds_hash / _prop_odds_hash: unchanged (the stamp stays out of the hash; book is in it, so two books never collide)
```

### 5.5 The readers

```python
# scripts/clv_backtest.py — _fetch_game_odds / _fetch_prop_odds: the graded row per (market, line type):
#   SELECT DISTINCT ON (market_type, line_type) ... WHERE game_pk = $1 AND line_type IN ('opening','closing')
#     AND book LIKE 'bp:%' AND book <> 'bp:0' AND <book kind = sportsbook>
#   ORDER BY market_type, line_type, array_position(<GRADED_BOOK_PREFERENCE labels>, book) NULLS LAST, fetched_at DESC
#   plus, per graded closing row, the best bettable price at the SAME line for the reference side and its book
#   (one more read per game: every sportsbook's closing row of the market)
# AccuracyRecord gains two optional fields with None defaults: market_best_price, market_best_book (the pairing keys unchanged)
# the report's provenance: odds_row_version = "sim555.2" and the preference list; sim548_market_skill.py and
#   sim518_pair_accuracy.py refuse to merge or pair reports graded against different lists (the RUN_LINE_SCORING_VERSION pattern)
# --benchmark-book bp:0 grades against the blend instead (a second benchmark; never the default)
# scripts/sim549_rescore_runlines.py, sim427_manager_probe.py, sim518_fatigue_probe.py: the same graded-row read
# betting/line_movement.py: the SQL filters to the label; the series stays one per (side, book); LineQuote.book_name /
#   LineMovement.book_name = book_display_name(book); the API models and the chart title use it. Nothing else (the owner's ruling).
# api/routes/betting.py — /edges and /signals: the market prices come from the best bettable row of the day's stored lines
#   when the game has any (the mock otherwise); the response names the book with the best price on each side
```

### 5.6 The scripts

- **`scripts/sim555_book_probe.py`** (from the scratchpad probe): a read-only census of N
  games (default 300, stratified by season and month): per market, the books quoting, the
  opener's coverage, the stamp's distance from the scheduled start by season, the first-five
  twin / tie / exclusion counts BY BOOK, and what the guard would refuse, by rule and book.
  The gate before the re-load (§8).
- **`scripts/sim555_book_sharpness.py`** (new in version 2): after the 2024–2025 load, for
  each book and each fixed-line market (the moneylines, first to score, a run in the first
  inning), the closing line's Brier score and log score against the real outcome over every
  game the book quoted, with a game-clustered range, and the same on the shared-line subset
  of the totals and props. No simulator. Ranks the books; the owner sets
  `GRADED_BOOK_PREFERENCE` from it (decision 1).
- **`scripts/sim555_retire_consensus_rows.py`**: per season, `INSERT INTO ..._archive SELECT
  ... WHERE book = 'consensus'` then `DELETE`, one transaction; prints the counts; refuses to
  run while any game of the season lacks a `bp:` closing moneyline that had a `consensus` one.
- **`scripts/sim555_rescore_reports.py`**: for each report, re-prices the fixed-line records
  from the graded book's closing rows and counts, per market, the records whose line moved.
- **`scripts/sim555_odds_census.sql`**: the scope note's Appendix A plus the other rules, per
  book, run after each season (the read is "zero rows").

### 5.7 Documents

`CHANGES.md`; the technical docs (`docs/technical/pipeline-betting-db.md` rows for the
provider's by-book methods, the guard module, the vocabulary's book tables;
`docs/technical/scripts-frontend.md` the loader and the sharpness read); the July register
rows 1.8, SIM-bettingpros-1, SIM-oddsprovider-1, B-N5 and 1.EX.devig-books annotated "closed
by SIM-555"; the SIM-555 row deleted at close; `WORKFLOW.md`'s loader lines carry `--book`.

---

## 6. Weights, bandwidth, power

None. This ticket touches no draw and no similarity weight. The simulator is not run.

---

## 7. Tests

Seventeen, in `tests/unit/test_sim555_one_book_per_row.py` (new) and the six files that pin
today's pick, rewritten on purpose:

1. `test_one_row_per_book`: the moneyline fixture gives ten closing rows (nine sportsbooks and
   the blend), each labelled `bp:<id>`, each side from that book, and one opening row from
   FanDuel.
2. `test_no_shared_opener_writes_no_opening_row`: the first-inning moneyline fixture (openers
   10 and 13) returns no opening row; the closing rows are unaffected.
3. `test_a_book_quoting_one_side_gives_no_row`; `test_the_blend_is_stored_and_kinded`: `bp:0`
   present, `book_kind` "blend", never returned by `get_odds`.
4. `test_get_odds_returns_the_first_preferred_book`: DraftKings' row; with `book="fanduel"`
   FanDuel's; with an unknown name an empty row.
5. `test_an_is_off_line_is_not_a_quote`: the first-five run-line fixture (FanDuel `is_off`)
   gives no FanDuel closing row and a theScore row.
6. `test_the_row_carries_the_stamp`: the opener's `created` on the opening row, the newest
   `updated` of the row's sides on a closing row.
7. `test_the_total_keeps_one_line_for_both_sides`: `over_line` / `under_line` on the row; the
   guard refuses a mismatch.
8. `test_the_guard_refuses_equal_spreads_priced_like_a_pair`: the appendix-B payload (Hard
   Rock 1 / 1 at −400 / +260) refused; FanDuel's ±1.5 kept; a real "+1.5 / +1.5" at −200 /
   −200 (adds 1.33) kept.
9. `test_the_guard_refuses_spreads_of_different_size`.
10. `test_the_first_five_rules`: the twin (the 2025-06-01 FanDuel opener) gives no opening
    row; the "+0.5 / +0.5 at −400 / −400" row refused; a tie at −110 on the first-five
    moneyline refused; DraftKings' row absent from 2025-03-01 and present on 2024-08-14.
11. `test_the_closing_stamp_grace`: a line stamped 5 minutes after the scheduled start kept,
    one stamped 40 minutes after refused with the minutes in the reason.
12. `test_a_three_way_row_keeps_draw_empty_when_the_book_lists_no_tie`.
13. `test_the_three_full_game_markets_fill_only_their_own_columns`.
14. `test_the_loader_batches_one_offer_and_counts_refusals_by_book`;
    `test_the_live_cycle_persists_every_book`.
15. `test_the_graded_row_read`: a stub connection with rows from four books returns the first
    preferred book's row, skips the blend and a prediction market, and reports the best
    bettable price at the graded line with its book.
16. `test_every_reader_filters_to_the_label`: the SQL strings contain the filter; a report
    without `odds_row_version` does not merge with one that has it; two lists do not pair.
17. `test_the_sharpness_read_ranks_books_without_the_simulator`: on a synthetic season a
    book whose prices track the outcomes ranks above one whose prices do not.

The six existing files: `test_sim435_historical_odds.py` (seven tests on the per-side scan
become tests on the by-book rows), `test_bettingpros_odds_provider_sim405.py` (the "best
current line" expectations become the preferred book's), `test_sim421_game_markets.py`
(`test_closing_line_is_the_latest_update_across_books` becomes the by-book read),
`test_odds_provider_sim370.py` (the vocabulary exports; the protocol's two new methods),
`test_sim433_bullpen_availability.py` and the line-movement tests (the `consensus` default in
`_coerce_quote` stays for hand-built quotes).

---

## 8. Run book

```
# 1. the code lands; ruff, mypy; the unit lane (the seven test files); no engine change, no regression lane, no smoke — the simulator is untouched
# 2. the migration (two nullable columns, two indexes, two empty tables; the app stays up):
make migrate                                   # alembic current -> 0028
# 3. the census before the re-load (read-only, ~40 min at one vendor read a second):
MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts" app python scripts/sim555_book_probe.py --games 300 --out scripts/sim555_probe_300.json
#    gate: (a) every two-way offer quoted by at least one book on the preference list; (b) after the first-five rules, first-inning-shaped
#    rows < 0.5% per market, line type and book; (c) no book fails (b) — a book off F5_EXCLUDED_BOOKS that fails, or a listed book
#    flagged before its date, names its fix; else stop, extend the list or move the date, re-run the probe (decision 3). An item whose
#    read failed reads UNKNOWN: re-read the listed games with --game-pks. (Wording as built, §12.)
# 4. the re-load, detached, one season at a time (the app up; ~4.4 s a game for the game markets, ~13.6 s for the props; the inserts batched):
MSYS_NO_PATHCONV=1 docker compose run -d --name sim555_load_2024 -v "$PWD/scripts:/app/scripts" app python scripts/load_historical_odds.py --seasons 2024 --provider bettingpros   # ~12 h
MSYS_NO_PATHCONV=1 docker compose run -d --name sim555_load_2025 -v "$PWD/scripts:/app/scripts" app python scripts/load_historical_odds.py --seasons 2025 --provider bettingpros   # ~12 h
#    (as built: --provider bettingpros is required — ODDS_PROVIDER is not set in the app container and the loader refuses the mock
#    unasked; the scripts mount stands in for an image rebuild)
#    after each season: the census SQL on the bp: rows per book (zero mixed rows, zero equal-spread rows priced like a pair); the rows per
#    book and market against the probe's counts; the coverage of the graded row per market against the consensus rows (a market that loses
#    more than 3% of its games is a finding); the loader's refusal tally by book in the log; the table sizes
# 5. the sharpness read on 2024-2025 (minutes; no simulator) -> the owner sets GRADED_BOOK_PREFERENCE (decision 1):
MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts" app python scripts/sim555_book_sharpness.py --seasons 2024 2025 --out scripts/sim555_book_sharpness.json
# 6. the rest of the re-load: 2026, 2023, 2022, 2021, then 2020 --no-props, 2019 --no-props (~49 h)
# 7. the re-score of the stored reports (minutes): the fixed-line markets re-priced against the graded book; the moved-line records counted;
#    the owner then decides the full re-run of the 1,000-game baseline (~16 h, the app stopped) and the calibration layer's refit
MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts" app python scripts/sim555_rescore_reports.py scripts/sim548_accuracy_split.sim549.json scripts/sim548_baseline_2024_chunk{2,3,4}.sim549.json ...
#    (as built: feed the .sim549.json versions — the run-line re-scores of 2026-09-25 — not the originals)
# 8. retire the consensus rows (after the census passes on every season; ~20 min; then VACUUM the two tables):
MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts" app python scripts/sim555_retire_consensus_rows.py --seasons 2019 2020 2021 2022 2023 2024 2025 2026
# 9. close: CHANGES.md; the SIM-555 row deleted; the technical docs; the register rows annotated; the filter stays in the readers (harmless)
```

---

## 9. What could go wrong (ranked)

1. **A third book's first-five entries are first-inning bets.** The 15-game probe saw two.
   The 300-game census by book is the gate; a book that fails joins the dated exclusion list
   before a row is written. With every book stored, a wrong exclusion costs one book's rows,
   not a market.
2. **The graded book's coverage thins on some markets.** DraftKings' first-five rows are
   refused from 2025 and FanDuel's are off, so the graded first-five run line falls to BetMGM
   or Caesars; the census reports the graded row's coverage per market, and a market below
   97% of its old coverage is a finding.
3. **The preference list is set before the sharpness read.** DraftKings leads by size until
   the read on 2024–2025 ranks the books; a later change of order is a reader constant and a
   re-score, not a re-load, but the reports made in between carry the old list in their
   provenance and do not merge with the new.
4. **The 15-minute grace lets a late pre-game snapshot through, or an early in-play line.**
   The sample's newest stamps sit 0.1–5.0 minutes after the scheduled start; a delayed game
   whose snapshot came 20 minutes late would lose its close, an in-play line 10 minutes in
   would pass. `book_line_at` is stored, so either shows in the census and the grace can move.
5. **The inserts are the new cost.** Twenty-two million rows; single-row inserts would add
   about six hours, so the writers batch one offer's rows; the tables grow to 8–10 GB, and
   the two new indexes keep the graded-row read as fast as today's.
6. **A re-fetch is not reproducible over time.** The scope note's replay reproduced 199 of
   200 closing rows, so the store's history is intact, but a line the vendor drops later is
   gone. The archive keeps the old rows.
7. **The vendor's rate limit is not published.** The 2026-09 loads ran at about 3.4 reads a
   second without a refusal; the probe ran at one a second. The loader resumes with
   `--skip-loaded-since` if a run dies.
8. **The accuracy baselines change.** The closing prices move from bet365's (2025–26) to the
   graded book's; the moneyline and totals' market probabilities move by a point or two; the
   skill table's rows must be re-read, and the calibration layer refitted, before the sweep.
9. **The three-way opening rows shrink.** The tie names another book on 30 of 40 first-inning
   and 18 of 40 first-five sampled offers (scope §8); those rows keep home and away from the
   shared opener and an empty tie, which the comparison scores as it does for the 102 rows
   that lack a tie today.

---

## 10. Decisions for the owner

**Taken 2026-09-28: all six as recommended** (the graded book by a fixed preference list measured by
the sharpness read; the stamp and the 15-minute grace; the first-five rules with the dated DraftKings exclusion
and the census gate; every book, every season, 2024 and 2025 first; the old rows archived then deleted; the
sportsbooks only as bettable). The build constants follow: `GRADED_BOOK_PREFERENCE = (12, 10, 19, 13, 14, 18,
15, 24, 33, 49)` until the read; `CLOSING_STAMP_GRACE = 15 min`; `F5_EXCLUDED_BOOKS = {12: 2025-03-01}`;
`BETTABLE_KINDS = {"sportsbook"}`.

1. **The graded book.** *Taken, as recommended:* one fixed preference list applied on every game (the
   first book on it with a valid closing row for the market is the graded row), DraftKings
   first until the sharpness read on 2024–2025 ranks the books, then the order the read
   gives. Alternatives: the vendor's blend (`bp:0`), often the stronger forecaster but not
   bettable and a mix of lines on the segment markets; or version 1's opener rule, which ties
   the graded book to whoever opened the market.
2. **The stamp.** *Taken, as recommended:* store the chosen line's stamp in `book_line_at` (migration
   0028) and refuse a closing line stamped more than 15 minutes after the scheduled start; it
   refuses nothing in the sample and records why a 2025–2026 close sits after the start (the
   vendor's snapshot). Alternatives: the remediation branch's strict guard (before the
   scheduled start), which drops every 2025–2026 close; or no column and no guard, which
   leaves the census unrepeatable from the store.
3. **The first-five rules.** *Taken, as recommended:* the twin rule, the two tie rules, and the dated
   exclusion of DraftKings' first-five entries from 2025-03-01, with the 300-game census by
   book as the gate before the re-load. Alternative: a price band on the ±1.5 bets, which
   overlaps the real first-five range (+400 to +650) and would refuse real underdog lines.
4. **Every book, every season.** *Taken, as recommended:* store one row per book for the game markets
   and the props on all eight seasons (8–10 GB; the inserts batched), 2024 and 2025 first
   (about 24 hours), then 2026 and 2023, then 2019–2022 (about 73 hours in all, detached, the
   app up); the stored reports re-scored for the fixed-line markets, the full re-run of the
   1,000-game baseline (about 16 hours) only on the owner's call. Alternatives: every book
   for the game markets and the graded book only for the props (about 2 GB, no best-price on
   props); or 2024–2026 only, with 2019–2023 left on the old rows behind the filter.
5. **The old rows.** *Taken, as recommended:* archive every `consensus` row into the two archive
   tables and delete it, season by season, once the census passes on the new rows; the
   archives can be dropped later. Alternatives: delete without an archive; or keep the rows
   behind the filter (1.9 GB nobody reads).
6. **What counts as a bettable price.** *Taken, as recommended:* the sportsbooks only, for the best
   price on a record and on the edge page; the daily-fantasy apps price at fixed pick'em
   terms, the exchange and the prediction markets carry thin liquidity and a different fee.
   Every kind is stored and told apart by the vocabulary, so the choice is a reader constant.
   Alternative: the exchange and the prediction markets included.

**Recorded, not asked:** the opening row is the opener's and needs no relation to the
graded closing book (the owner's ruling of 2026-09-28); the live cycle stores every book on
its cadence with the dedup keeping it to real changes, and `PROP_BOOKS` with its sharp flag
goes; `mark_closing_lines`' one-row `LIMIT 1` and the opening-line job's missing `odds_hash`
stay as they are (neither has a production caller; recorded in the scope note); the
accuracy reports carry `odds_row_version` and the preference list so the skill table never
merges reads graded against different books; every reader keeps the label filter after the
retirement (harmless); the backlog row's definition of done still carries the two struck
lines until the row is edited; no simulator flag, weight or power changes.

---

## 11. Changes from version 1 (2026-09-28)

- **Every book's row is stored**, not one anchored book's. The provider gains by-book methods;
  the loader and the live cycle persist every kept row, batched; migration 0028 adds two
  indexes; the storage grows from 1.9 GB to 8–10 GB (§2.7, §4).
- **The graded row is a reader's preference**, the same list on every game, measured by a
  new sharpness read (§5.6), not the opener's book. Version 1's anchor rule, its fixed
  fallback list and its "two labels when the opener cannot close" are gone.
- **The two CLV lines of the definition of done are struck** by the owner's ruling. The
  line-movement page gets the label filter and the book name and nothing more; the total's
  moved-line CLV rule and the open-to-close ordering are dropped.
- **The accuracy record carries the best bettable price and its book**; `/edges` and
  `/signals` price from the best stored row and name the book; the vocabulary gains the book
  kinds; a sixth decision asks which kinds are bettable.
- **The best-price gain is measured** (§2.7): about a point of implied probability a side,
  the book's cut halved.
- Unchanged: the guard, the stamp and its grace, the first-five rules and the dated
  exclusion, the census gate, the re-load's order, the retirement of the old rows, the
  readers' filter and the report stamp.

---

## 12. Build record (2026-09-28)

**What landed.** The code of §4–§7, in five parts built in parallel against one contract, each
part reviewed by an adversarial reviewer and fixed, then one review across the parts (four
lenses, two skeptics per finding) and its fixes:

- **The core.** `pipeline/odds_provider.py` carries the book vocabulary (the names, the kinds,
  the labels, `GRADED_BOOK_PREFERENCE`, `BETTABLE_KINDS`, `ODDS_ROW_VERSION = "sim555.2"`) and
  the two writer helpers `odds_rows_by_book` / `prop_rows_by_book`.
  `pipeline/bettingpros_odds_provider.py` returns one row per book (`get_odds_by_book` /
  `get_prop_odds_by_book`); `_pick_line`, the per-side scan, and the all-three-markets
  full-game row are gone. `pipeline/odds_row_guard.py` is the load guard. Migration 0028 is
  APPLIED to the live database (2026-09-28; `alembic current` = 0028).
- **The writers.** The loader, the live pipeline's odds cycles and the nightly opening-line
  job write through one batched, guarded path that carries `book_line_at`.
- **The readers.** The accuracy comparison grades one book's row by the preference list and
  carries the best sportsbook price on every record; the skill table, the paired read and
  the calibration layer refuse to mix reports graded against different rows; the three
  probe scripts read the same graded row.
- **The live pages.** The line-movement and CLV pages read the one-book rows and name the
  book; `/edges` and `/signals` gain the stored price source.
- **The scripts.** `sim555_book_probe.py` (the census), `sim555_book_sharpness.py`,
  `sim555_retire_consensus_rows.py`, `sim555_rescore_reports.py`, `sim555_odds_census.sql`.
- **Tests.** 297 in five new files (`tests/unit/test_sim555_*.py`: the plan's tests 1–17 and
  every review finding), the old pins rewritten on purpose, an integration test that runs
  migration 0028 on real Postgres. The unit lane reads 4,864 passed; the regression lane,
  ruff and mypy are clean.

**Where the build departs from this plan, and why.**

1. **A three-way opening row keeps its tie only from the same opener.** Test 2 of §7 said the
   first-inning moneyline fixture (home and away opened by Caesars, the tie by FanDuel) gives
   no opening row; §9 item 9 said such a row keeps home and away and an empty tie. The build
   follows §9: the tie is optional, the two teams are not.
2. **`LEGACY_GAME_MARKET_TYPES` stays**, as an alias of the new `FULL_GAME_MARKET_TYPES`. The
   provider's all-three-markets branch is deleted; the mock keeps its row, and three readers
   use the name.
3. **The book kinds are a whitelist.** A read of the vendor's `/v3/books` found eight ids the
   plan's table lacked: pick'em apps (44, 45, 53, 69, 70) and prediction markets (74, 76, 78).
   The plan's "every other id: sportsbook" would have counted them as bettable. Now the eleven
   sportsbooks are listed by name and an unlisted id is kind `unknown`, never bettable; the
   readers keep the listed sportsbooks (`bettable_labels()`). Pinnacle (2), listed with no MLB
   line, stays unlisted.
4. **The twin rule is tighter on the moneyline.** A moneyline's line is always 1, so a
   near-even game's two-way first-five and first-inning prices sit within 0.03 at many
   books; the census read on three games dropped a real Caesars first-five moneyline that
   way. The moneyline pair (279 / 278) now counts as a copy only at IDENTICAL prices; the run
   line and the total keep 0.03. A first-five tie the first-inning entry lacks, or a tie
   beyond the tolerance, also rules a copy out. A failed first-inning read gives the
   first-five market no rows (a warning), never unchecked rows.
5. **The graded read puts a tie-less three-way row last.** On the first-inning and first-five
   moneylines, a book whose row lists no tie is graded only when no book lists the tie
   (`INCOMPLETE_THREE_WAY_LAST_SQL`); the backtest skips a three-way row without a tie, so the
   first book on the list could otherwise empty the market.
6. **A run line listed as two bets is priced over its OWN book's margin** (its segment total,
   its full-game total, its moneyline), else the flat 1.05 — never another book's margin, so
   no price is divided by another book's cut.
7. **`/edges` and `/signals` read pre-game prices only.** The plan's "the day's stored lines"
   became: each book's latest closing row, plus its latest `current` row while the game is
   still `Preview` in `raw.games` (a closing row beats a current row). An in-play or
   after-the-game `current` row is never read. The fair probability comes from one book's
   row (the first on the preference list); each side's offered price is the best sportsbook
   price at that line; the response names both books; `odds_source` reads `injected`,
   `stored` or `mock`.
8. **The live pipeline stores pre-game lines.** Before this ticket it stored odds only for
   live games. Now each `Preview` game on the schedule poll gets every book's current line of
   every game market and prop, at most once per `PREGAME_ODDS_CADENCE_S` (600 s) per game;
   the in-play cycle stays at 60 s, and all fifteen game markets ride it (the per-pitch
   persist of one full-game row is gone). The game's `raw.games` row is written before its
   first pre-game odds row (the foreign key). The broadcast and Redis payloads are
   JSON-safe (the provider's rows carry datetimes). `PROP_BOOKS` is deleted.
9. **The nightly opening-line job writes through the same path.** It could not write at all
   before: `raw.game_odds.odds_hash` is NOT NULL since migration 0012 and the job wrote no
   hash (a defect older than this ticket).
10. **The loader refuses the mock unasked.** `ODDS_PROVIDER` is not set in the app container,
    so a run without `--provider bettingpros` used to load deterministic mock prices under the
    real tables. The resume flag counts only the run's own line types, only `bp:` rows, from
    at least two books on an every-book run, and reads `raw.game_odds` on a `--no-props` run.
11. **A failed lookup is cached for one time-to-live.** The provider used to cache a failed
    game, event or player lookup for the process lifetime (a long-lived process never saw a
    game listed later); the first fix cached nothing, which made one failed read cost about
    410 retries per game. Now a failure is kept for one cache time-to-live (30 s live, 600 s
    in the loader).
12. **The census gate reads what the loader stores.** Gate (b) grades the rows the loader
    writes; a row the load guard refuses is never stored, so it is reported beside the gate
    (`guard_refused`), not graded. Gate (c) also checks that the dated DraftKings exclusion is
    borne out: the probe keeps DraftKings' rows from its date, marks them, and grades them. A
    gate item whose read failed reads UNKNOWN.
13. **Two extra census checks.** A full-game team total at a line of 1 or below (section 6b),
    and the graded-book and coverage sections follow the tie-last rule.
14. **The census added two guard rules and narrowed the dated exclusion** (below): a
    three-way row adding below 0.98 is refused; a first-five total at 1.5 or below is refused;
    DraftKings' dated exclusion covers the first-five run line and total, not the moneyline.

**The three defects of unknown cause (the definition of done's last line).**

- **The 2022–2024 team totals at a line of 1** are Caesars' lines flagged `is_off` (traced on
  game 716358): the old provider read every line, the new one skips a line flagged `is_off`
  or inactive. Fixed by `_usable` for the books' lines, and — the 2024 census found — by the
  guard's `team_total_placeholder_line` for the opener field, which carries Caesars' line of 1
  with no flag (below). Census section 6b is the check.
- **First-five run lines priced like the first inning** are FanDuel's `is_off` first-five
  lines and its first-five opener copying its own first-inning opener, and DraftKings'
  2025–2026 first-five entries. Fixed by `_usable`, the twin rule and the dated exclusion; the
  300-game census is the check.
- **First-five tie prices priced like the first inning** are the same entries' ties, plus
  books whose first-five tie copies a first-inning tie. Fixed by the twin rule and the guard's
  first-five tie rule (above 0.35 implied is refused); the census reports the refusals by book.

Each explanation starts from our own request: every market id the provider asks for is the
vendor's id for that market (§2.6); the defects sit in the vendor's entries for two books and
in a flag the old provider did not read.

**Known limits, left for later (each found by the reviews, each dormant or outside this
ticket's scope today).**

- **A price that returns is not recorded.** The dedup hash has no time in it, so a book's
  `current` line that moves A → B → A keeps B as its newest row, and a quote the book
  withdraws keeps its last row. `/edges` reads a Preview game's newest current rows, so it
  could offer B or a withdrawn price. The fix is a last-seen stamp (a migration). Dormant:
  the live pipeline is off (`LIVE_PIPELINE_ENABLED` unset). Belongs with the live-slate epic
  (SIM-519).
- **The live pipeline's vendor reads block its event loop** (the provider is synchronous).
  The in-play cycle always did this; the pre-game cycle widens it to the whole slate every
  ten minutes. Dormant for the same reason; SIM-519.
- **The schedule poll uses the machine's date** (older than this ticket); SIM-519.
- **The line-movement page's two-bet margin falls back to any book's** when the quote's own
  book posted no total or moneyline (SIM-549's rule; the owner's ruling kept this page's
  change to the filter and the book name).
- **The loader has no retry with backoff and no failed-read count**; a vendor timeout drops
  that market for that game. The per-season census coverage read (a market that loses more
  than 3% of its games) is the backstop.

**The run book, as run.** Step 1 (the code; the lanes green) and step 2 (migration 0028,
applied 2026-09-28) are done. The app and the web front end were rebuilt the same day (the
app takes `betting/` and `scripts/` from its image). Step 3, the census:
300 games stratified by season (2019-2026) and month, 3,303 vendor reads in 55 minutes,
read-only (`scripts/sim555_probe_300.json` / `.log`).

- **Coverage.** A book on the preference list quotes 1,916 of 1,924 two-way offers; the 8
  others are one game (746572, 2024-04-21) whose start moved about a day and a half, so the
  event matcher (two-hour limit, SIM-536) declines it — a matching limit, not a book rule.
  Sportsbooks per offer: 7.7 on the moneyline (1-11), 7.9 on the run line, 6.0 on the
  first-five run line, 5.6 on a strikeout prop. An opener on every side: 98-100% of the
  full-game offers, 79% of the first-five run lines. The graded book: DraftKings on 292 of 299
  moneylines and run lines and 187 of 214 strikeout props; FanDuel or DraftKings on the
  first-inning moneyline (DraftKings lists no first-inning tie on most games, so the tie-last
  rule grades FanDuel).
- **The stamp.** 2019-2024: the newest closing stamp sits before the scheduled start on every
  offer (median 5-46 minutes before). 2025-2026: after the start on every offer (median +1.2 /
  +0.5 minutes, at most +5.2), one shared stamp on 49-71% of the offers. The 15-minute grace
  keeps every close; no closing line was refused for its stamp.
- **The guard, as first built:** 288 of 21,320 rows refused (1.4%): 246 first-five ties priced
  like a first inning (FanDuel 92, BetRivers 53, the blend 46, SugarHouse 41), 26 equal
  spreads priced like a pair (FanDuel's first-inning '+1 / +1', 23), 12 spreads of different
  size, 4 totals at two lines.
- **The first-five rules:** the twin rule dropped Caesars' first-five moneyline on 27 games
  (2021-2024: identical to its first-inning moneyline, price for price) and Hard Rock's on 16
  (2025-2026); DraftKings' first-five run line on 11; the opener twin 54 opening rows.
- **The gate, as first run: (b) and (c) FAILED,** and the failures were read row by row
  (`census_dig.py` in the session scratchpad):
  1. **FanDuel's 2025 first-five moneyline** pairs FIRST-INNING team prices with a first-five
     tie (2025-07-03: +210 / +410 / tie +560; its first-inning row +190 / +430 / −110). Its
     three prices add to 0.67-0.94: no book sells every outcome for less than the stake. Over
     the 300 games the only three-way rows adding below 1.00 are those five and two Kalshi
     rows at 0.989 / 0.998. NEW GUARD RULE: a three-way row adding below 0.98
     (`THREE_WAY_SUM_MIN`) is refused.
  2. **First-five totals at a first-inning line:** Fanatics (1.5, 2021-2022), Fliff (0.5, 2025)
     and DraftKings (0.5, 2025); the old store holds 112 such rows (2021: 78, 2022: 32, 2025:
     4). NEW GUARD RULE: a first-five both-team total at 1.5 or below
     (`F5_TOTAL_LINE_MAX`) is refused.
  3. **DraftKings' dated exclusion was too wide.** On the first-five run line 43 of its 54
     rows from 2025-03-01 are first-inning-shaped, and on the total both of its 2 rows sit at
     0.5: keep. On the first-five moneyline only 5 of 71 are flagged and the guard refuses all
     5: the exclusion there threw away 66 real rows. NOW PER MARKET:
     `F5_EXCLUDED_MARKETS = {12: {283, 281}}`.
  4. **Two probe artifacts:** single Caesars and theScore rows flagged only because their
     three-way peers on those games were FanDuel's bad row and one other book (a median of
     two); and flags on books a bettor cannot use (Kalshi, Polymarket US, the blend's mixed
     2022-2023 first-five run line), which are stored but never graded or offered. The gate
     now grades the bettable books and prints the others beside it.
- **The re-grade** (`--regate`, the same payloads, the current rules, no vendor read;
  `scripts/sim555_probe_300_regated.json`): 306 of 21,391 rows refused (1.4%); **(b) 0
  failing, (c) supported on both markets it names; every UNKNOWN is game 746572.** The gate
  passes.
- **A loader smoke** (2 games of 2024, `--provider bettingpros`): 175 game rows and 1,913
  prop rows in 32 s, 0 refusals, one opening row per market and one closing row per book; the
  census SQL reads zero rows on every rule; the graded read gives DraftKings on the full-game
  markets and strikeouts; the live line-movement page shows one series per book with its name.

**Step 4 started 2026-09-29 02:06 UTC:** the 2024 and 2025 re-loads run detached in parallel
(`sim555_load_2024` / `sim555_load_2025`, logs `scripts/sim555_load_<season>.log`, about 16 s a
game, about 11 hours each). Steps 5-9 follow: the census SQL per season, the sharpness read on
2024-2025 and the owner's preference list, the rest of the seasons (~49 h), the re-score of the
stored reports, the retirement of the `consensus` rows, the close.

**The host crashed 11 minutes in, and the loads were relaunched restart-safe.** At
02:16:45 UTC Windows stopped on a blue screen (bugcheck 0x7E, a driver's unhandled access
violation; the dump is `C:\WINDOWS\Minidump\092826-35515-01.dmp`) and rebooted. Windows
logged 32 unexpected reboots in the 30 days before, so an 11-hour load will likely meet
another. Docker came back at 02:25 and restarted the services, but the two loader containers
have no restart policy and stayed down. They had written 40 games of 2024 and 26 of 2025; the
database recovered cleanly (migration 0028 in place) and the working tree was intact. The
loads were relaunched at 02:52 UTC through `scripts/_sim555_load_<season>.sh` with the Docker
restart policy `unless-stopped`: after a reboot Docker restarts the container, and a restart
passes `--skip-loaded-since 2026-09-29T02:00:00+00:00`, so the loader skips the games already
done. The first pass re-reads every game (the dedup keeps the 66 games' rows single). Each
start and restart writes a `=== START` / `=== RESTART` line to the log, and the end writes
`EXIT <code>`; the container then idles (`sleep infinity`) until someone removes it
(`docker rm -f sim555_load_<season>`). **A top-up follows every restart:** the game in flight
at a crash may hold part of its prop rows and the resume skips it; after the season, find it
(the game with the newest one-book row fetched before each `=== RESTART` line) and load it
again.

**The second crash, its cause, and the loads ON HOLD (2026-09-29 03:30 UTC).** The host
blue-screened again at 03:02 UTC, the same fault at the same offset. Windows Error Reporting
names the driver: `AV_vgk!unknown_function` — `vgk.sys`, **Riot Vanguard**, the kernel
anti-cheat driver of Valorant and League of Legends, which loads at boot. Every blue screen of
the month with a named cause is `vgk` (28 of 28; codes 0x7E, 0x1E, 0x50; 2026-08-29 to
2026-09-29). Both crashes of 2026-09-28/29 hit while the two loaders ran (the 55-minute census,
at a third of their read rate, did not), so the loads likely trigger it. The restart after
the second crash also FAILED: Docker started the loaders while Postgres was still replaying its
log (`the database system is starting up`), the loader exited 1, and the first wrapper then
idled instead of retrying. Two fixes: the loader gained `--done-file PATH` (it skips the games
the file lists and appends a game only once its last row is written, so a game a crash cuts off
is loaded again in full — this replaces `--skip-loaded-since` and the top-up rule above); the
wrappers `scripts/_sim555_load_<season>.sh` now wait for the database (up to 10 minutes) and
retry a failed run (up to 30 times, a minute apart; exit 2 is not retried). Before the hold the
loads had written 48 games of 2024 and 33 of 2025. The loader containers are REMOVED and the
loads wait for the owner: remove or disable Riot Vanguard first (recommended), or relaunch and
accept the crashes. The relaunch, per season:
`MSYS_NO_PATHCONV=1 docker compose run -d --name sim555_load_<season> -v "$PWD/scripts:/app/scripts" app bash /app/scripts/_sim555_load_<season>.sh`
then `docker update --restart unless-stopped sim555_load_<season>`.

**Relaunched 2026-09-29 03:34 UTC by owner decision ("Relaunch now"), Vanguard still
installed:** both seasons run with the fixed wrappers and the restart policy; the done-lists
(`scripts/sim555_load_<season>.done`) were filling within two minutes. Expect more blue screens;
each costs the reboot plus the database's recovery, and the loader resumes on its own.

**The 2024 season: loaded and checked (2026-09-29).** The re-load of 2024 finished at 13:05
UTC (2,472 games, 226,497 game rows and 2,714,488 prop rows written; the guard refused 120 of
2,941,105 rows offered). The census SQL on its rows reads zero on every rule but one; the
graded row keeps more than 99.5% of the old games on every market (the worst, the first-five
total, loses 0.41%; the limit is 3%); DraftKings is graded on 99.6% of the moneylines. **The one
finding, section 6b:** 883 full-game team-total rows at a line of 1 — every Caesars OPENING team
total of 2024 (876; prices such as -182 / -1200, or +700 to score twice for a team the market
expects to score four) and 7 of the blend's closing rows. The explanation above ("fixed by
`_usable`") was only half right: the provider skips a line flagged `is_off` among the books'
lines, but the opening price comes from the vendor's opener field, which carries no such flag.
The guard gained a rule, `team_total_placeholder_line`: a full-game team total at a line of 1 or
below is refused. A line of 1.5 is real (DraftKings 2025-06-19 at 1.5, -160, beside 2.5 at +100
to +125 elsewhere; theScore's 2026-08-29 opener at 1.5, -600, beside 3.5), so the rule stops at 1;
for two minutes the loaders ran with a first draft at 1.5, which refused no row. The 2025 and
2026 loaders were restarted onto the rule (their done-lists skip the finished games). **The 883
rows already written still sit in `raw.game_odds`:** moving them to `raw.game_odds_archive` was
refused by the session's permission check (it deletes rows) and waits for the owner. They are
inert meanwhile: opening rows are not graded, the blend is never graded; census 6b reads them
until they move. **2026 started** 2026-09-29 13:12 UTC in 2024's slot (2,037 games).

**The 2025 season: loaded and checked (2026-09-29).** 2025 finished at 15:19 UTC (all 2,477 games on its
done-list; the last attempt refused 1,441 of 476,070 rows, 0.3%). A clean, user-initiated Windows restart
at 13:47 UTC (not a crash) restarted Docker; the wrapper waited for the database and resumed from 2,133
finished games, as designed. Its census reads zero on every rule, team totals included; the graded row
keeps at least 99.89% of the old games on every market; DraftKings is graded on every moneyline, run line
and total, BetMGM on the first-five run line and total (DraftKings excluded there), Caesars / FanDuel /
theScore on the first-five moneyline (DraftKings lists no first-five tie), BetRivers on the first team to
score. Vanguard's driver is not loaded since that restart.

**The 883 placeholder rows moved (2026-09-29, by the owner).** `raw.game_odds_archive` now holds exactly
the 876 Caesars opening team totals and the 7 blend closing rows of 2024 at a line of 1; none is left in
`raw.game_odds`, so 2024's census section 6b reads zero. The archive's readers take only `consensus` rows,
so these rows stay out of every read. **2023 started** 2026-09-29 17:48 UTC in 2025's slot (2,471 games);
its first games already show the guard refusing Caesars' 2023 opening team totals at 1.

**Run book steps 5 and 7: the book order and the re-score (2026-09-29).**

*The sharpness read* (`scripts/sim555_book_sharpness.py --seasons 2024 2025`, output
`scripts/sim555_book_sharpness.txt` / `.json`) suggested (12, 33, 15, 18, 13, 10, 19, 27, 24, 14, 49).
A verification workflow (three checkers, one recommender, two skeptics) found: the read's numbers
reproduce exactly from the database; **DraftKings first is solid** (it beats nine of the ten other books
on the games both quoted, each gap outside its 90% range); **positions 2-9 are noise** — the pooled read is
95.6% 2024 data and its order is the 2024 "run in the first inning" order; on the moneyline alone the
order nearly reverses between 2024 and 2025. So the order behind DraftKings uses the evidence that does
separate books: BetMGM is a measurably better benchmark than theScore on the player props DraftKings
lacks (home runs, doubles, runs, RBIs; gaps outside their ranges), and theScore's 2024 closing prices are
stale (a median 140 minutes before the latest book's close; FanDuel 24). **The order set:**
`GRADED_BOOK_PREFERENCE = (12, 19, 10, 33, 18, 24, 13, 49, 14, 15, 27)` — DraftKings, BetMGM, FanDuel,
theScore, BetRivers, bet365, Caesars, Hard Rock, Fanatics, then the two copies, SugarHouse (BetRivers'
lines) and PartyCasino (BetMGM's). DraftKings still grades 97-100% of the full-game markets and the
strikeout prop; the order decides the first-five markets (DraftKings lists no first-five tie and is
excluded on the first-five run line and total from 2025), first team to score (DraftKings never quotes
it), the 2024 first-inning moneyline and the props DraftKings lacks. Re-read the order once 2023 and
2026 are in.

*A third guard finding.* The verification found three-way moneylines that pair the TWO-WAY team prices
(the bet that refunds a tie) with a separate tie price: theScore's first-inning -130 / +100 with a tie
at -145 adds to 1.66. Real three-way rows add to 1.02-1.15, and their team prices leave room for the tie
(about 0.92 over five innings, 0.51 over one). Two rules joined the guard: `three_way_two_way_team_prices`
(the two team prices alone add to 1.00 or more) and `three_way_sum_above_max` (the three add above 1.18).
They refuse theScore's, Caesars', FanDuel's and Fliff's 2025 rows of that shape and the blend's; DraftKings
loses one outlier row. The 2023 and 2026 loaders were restarted onto them. **Already stored and waiting
for the owner's move to the archive:** 100 rows of 2023, 2,328 of 2024 (almost all the blend's, never
graded), 4,555 of 2025, 2,305 of 2026. The 2024 graded three-way rows (FanDuel's) are clean, so the
2024 re-score below is unaffected.

*The re-score* (`scripts/sim555_rescore_reports.py --drop-not-reloaded` over the four reports of the
1,000-game baseline, the `.sim549.json` versions; the skill table `scripts/sim548_baseline_1000_skill.sim555.txt`
against `scripts/sim548_baseline_1000_skill.txt`). Two games (745169, 745175) had no new row: the event
matcher declines them (the vendor's nearest event sits 6 hours and 49 days away), so their old prices
likely belonged to another game; the new flag drops such a game's records, as a fresh run does. The
fixed-line markets, re-priced against the graded book:

| market | old gap (sim - line) | new gap [90% range] | line's AUC old / new |
|---|---|---|---|
| moneyline | +0.0112 | +0.0107 [+0.0048, +0.0171] | 0.605 / 0.604 |
| first-five moneyline | +0.0073 | +0.0079 [+0.0028, +0.0129] | 0.606 / 0.605 |
| first-inning moneyline | +0.0022 (level) | **+0.0075 [+0.0033, +0.0116] (behind)** | 0.532 / 0.579 |
| a run in the first inning | +0.0076 | +0.0077 [+0.0018, +0.0144] | 0.538 / 0.545 |

The first-inning moneyline was the old mixed row's artefact: its tie price came from another bet, and
against one book's real three-way row the line is clearly sharper, so the simulator falls behind it.
The other fixed-line markets barely move. **The line-carrying markets keep their old prices** (a record
whose graded line moved needs the simulator again); the graded line moved on 35% of the first-five totals,
27% of total bases, 19-22% of team totals and totals, 21% of first-inning totals, 16% of first-five run
lines, 15% of strikeouts, 7% of hits and 5% of run lines. The team totals still read "beats the line"
with the line's AUC below 0.5 (0.48 / 0.46, worse than chance): the old consensus team-total rows, the
placeholders at a line of 1 among them. Only the full re-run of the baseline (about 16 hours, the app
stopped) grades those markets on the new rows, and the calibration layer's refit follows it — the
owner's call (§10 decision 4).

**The 2026 season: loaded and checked; 2019-2022 started (2026-09-30).** 2026 finished at 00:55 UTC
(all 2,037 games on its done-list; 25 have no vendor event). Its census reads zero on every rule but
section 5b: 2,305 mixed three-way rows, exactly the 2026 rows `scripts/sim555_archive_mixed_three_way.sql`
moves, all written before the rule landed. The graded row covers 2,012 games on every market (2,010 on
the first-five moneyline), against 1,943 old games. At 02:18 UTC four loaders started beside 2023:
2022 and 2021 with props, 2020 and 2019 with `--no-props` (new wrappers `scripts/_sim555_load_<season>.sh`,
the same restart policy). The four earlier seasons' logs show no 429 (rate-limit) reply from the vendor;
its only errors are short 502/503 bursts, so five loaders at once are expected to hold. The no-props
loaders run about 2.5 s a game.

*A limit found in the logs.* A vendor error is not retried inside a game: a failed schedule read or
offers page leaves that game or market without rows, and the done-list still lists the game (2023:
one schedule time-out: game 717171 holds 20 old rows and no `bp:` row; 2024-2025: 86-101 short bursts
of failed prop pages). About 25-41 games a season have no vendor event at all. Before step 8 each
season needs a gap sweep: the games with an old row and no `bp:` row, split into the ones the vendor
has no event for and the ones an error cut off. An error's game is loaded again by deleting its line
from the done-list and restarting the season's container (`docker restart sim555_load_<season>`).

**2023, 2020 and 2019: loaded and checked; the gap sweep (2026-09-30).** 2020 finished at 02:49 UTC
(951 games), 2023 at 03:10 (2,471), 2019 at 03:44 (2,466). 2022 took one segmentation fault (exit 139,
the known random crash class) at 05:12; the wrapper restarted it a minute later and it resumed from its
done-list. The censuses of 2023, 2020 and 2019 read zero on every rule but 2023's section 5b: 100 mixed
three-way rows, exactly the 2023 rows the archive file moves. 2020 and 2019 hold only the three
full-game markets, as the old rows did, and the graded row covers every old game but one (2019's
moneyline). One coverage finding, explained: 2023's first-five moneyline grades 604 fewer games than
the old rows (24.9%). 603 of them are games whose old row carried a first-inning tie price (above 0.35
implied); every book's first-five tie on those games is the first-inning one, so the guard refuses
them and no valid first-five price exists. The 604th is 717171.
*The gap sweep* (games with an old row and no `bp:` row, 2019, 2020 and 2023-2026) found six games.
One is an error's: 717171, re-loaded at 06:50 UTC (80 game rows from 10 books, 938 prop rows). Four
are games the matcher declines (2024: 745169, 745175, 746572, 746773) and one has no vendor event
(2024: 746755); these five can never get a `bp:` row, so step 8 must retire them from a named list.
*The prop gaps* (a game and market with old prop rows and no `bp:` row) are one to eight games per
market and season, with one exception: triples, 420 games of 2023 (March to early May) and 22-27 games
in each later season. Every old row in those games is one-sided (an over price, no under: 5,887 of
5,887 in 2023), the vendor's "to hit a triple" price. The new rows store only two-sided prices, so
these games have no triples row by design.

**The mixed three-way rows moved; 2021 loaded and checked (2026-09-30).** At the owner's instruction
`scripts/sim555_archive_mixed_three_way.sql` ran at 06:56 UTC: 9,288 rows moved (2023: 100, 2024:
2,328, 2025: 4,555, 2026: 2,305; 2021-2022 loaded under the rule and held none), and
`raw.game_odds_archive` holds 10,171 rows. The censuses of 2023-2026 re-ran after the move: every
rule reads zero in all four seasons. 2021 finished at 06:56 UTC (2,466 games); its census reads zero
on every rule, and every market keeps at least 97.75% of its old games.
*A coverage finding the move opened.* 2025's first-five moneyline now grades 2,140 of 2,440 old
games; 303 are lost (12.4%, above the 3% line of §9). The moved 2025 rows (theScore 1,186, Caesars
316, FanDuel 218, Fliff 31, the blend 1,863) pair a two-way team price (the two add to 1.03-1.09)
with the book's own first-five tie (about 0.175 implied); on the 303 games no other book lists a tie,
and every other book's row is two-way. The rows were not one bet, so the move is right, but the
scorer grades a three-way market only from a row that lists the tie. A possible repair, not built:
build the three-way price from a book's two-way pair and its separate tie price (the tie from the tie
price, the split of the rest from the pair). 2024 (the baseline's season) loses 6 games (0.25%); 2023
loses 603, all with a first-inning tie in the old row (above); 2026 loses 4.

**2022 loaded and checked; the re-load of every season is COMPLETE (2026-09-30).** 2022 finished at
10:14 UTC (2,470 games; one segmentation fault at 05:12, absorbed by the wrapper). Its census reads zero
on every rule. One coverage finding, explained like 2023's: the first-five moneyline grades 238 of
2,335 old games. 2,083 of the lost games had a first-inning tie price in the old row (10 of them have
no `bp:` first-five row at all); the guard refused
10,774 first-five moneyline rows of 2022 for a first-inning tie (the vendor's 2022 first-five tie is the
first-inning one on almost every book). Every other 2022 market keeps at least 97.7% of its old games.
*The game-and-market gap sweep* (a game with `bp:` rows, and a market with an old closing row but no
`bp:` closing row; the first-inning and first-five moneylines left out) found 86 gaps in 78 games.
43 carry a guard refusal in the log (2021's first-five run lines with spreads of different size, 39;
two late stamps; two first-inning run lines priced like a pair). The games of the other 43 gaps, 35
games, were re-loaded at 13:35 UTC (done-list lines deleted, one short loader per season, the backups in
the session scratchpad): 16 of the 35 games closed (vendor errors, 663403's total among them). The 19 games left give the same result on a
second load, and every one sampled has a defective old row: 2024's five team-total games held only the
Caesars placeholders at 1 (archived); 2021's 632738 / 634643 and 2024's 745263 hold moneyline prices in
the run-line and total rows (the old mixed row); 2021's first-team-to-score rows hold one side's price;
2022's first-five run lines hold spreads with no prices. None of the 19 has a valid new row to find.
All loader containers are removed.

**The six games re-examined: five are a matcher defect, one has no close (2026-10-01).** The step-8
list (games with an old closing moneyline and no `bp:` one) holds six games. A read-only probe of the
MLB schedule and the vendor (`six_games_probe.py` / `two_games_probe.py` in the session scratchpad)
found that only one of them truly has no price to load:
- **745175, 746572, 746773 (2024): postponed games made up later.** The MLB schedule lists such a game
  twice: the postponed entry (the original date and start) and the played entry. The provider reads
  `data["dates"][0]["games"][0]`, the POSTPONED entry, so it measures the vendor's events against the
  original start (49 days, 42 hours and 18 hours away) and the two-hour limit declines them. Measured
  against the played start, each has a vendor event at exactly that start, and 9-10 books' closing
  moneylines pass the guard. (bet365's row on 745175 and 746572 is stamped at the ORIGINAL date: a price
  of the game that was never played; the guard has no rule for a close stamped weeks early.)
- **745169 (2024): a start moved earlier.** The game was played at 1:15 pm (18:15 UTC); the vendor kept
  the original 7:15 pm on its event. It is the only Rockies-Cardinals event that day. Read through that
  event, six books' closes are stamped before the real first pitch and pass; four are later (in play)
  and the guard refuses them.
- **746755 (2024): a suspended game.** It started on 08-27 and resumed on 08-28; the vendor moved its
  event to the resume time on the 08-28 slate, which the provider never reads for an 08-27 game. Read
  through that event, seven books' closes are stamped before the original first pitch and pass.
- **567323 (2019): no close before first pitch.** The event matches exactly (game 1 of a double-header,
  first pitch on time, 20:40 UTC), but every book's closing stamp is 68-206 minutes after it, as late as
  the end of the game. No pre-game close exists at the vendor, so this game alone needs a named
  exemption in step 8.
The defect reaches well beyond these five: the loaders' logs show the matcher declining a same-team
event on 24-69 games a season (350 in 2019-2026; 2024: 35, against 34 postponed-and-made-up games on
MLB's 2024 schedule). Those games had no odds before SIM-555 either. A fix (read the played schedule
entry; for a suspended game also read the resume date's slate; accept the only same-team event on the
day when the schedule shows no double-header) and a re-load of those games are the owner's decision.

**Owner rulings of 2026-10-01.** The full 1,000-game baseline re-run and the calibration layer's refit on
the new odds move to the draw-weight fit (SIM-548), which measures its baseline on these rows anyway;
the SIM-555 definition of done no longer carries them. The live closing-price defect (nothing in
production marks a live game's closing prices, and the marker picks one row per game across every
market and book) joins the segment-markets surface ticket (SIM-546). The loader gains a retry option
(next paragraph).

**The loader's retry option (built 2026-10-01).** The re-load showed that one failed vendor read left a
silent, permanent gap: the provider logs the failure and returns an empty result, and the loader then
lists the game as done (game 717171 lost every row to one schedule time-out). Now:
- **The provider retries a passing failure.** Every vendor and MLB read goes through
  `_http_get_json`, which retries an HTTP 429 or 5xx, a time-out and a refused, dropped or unreachable
  connection; the wait doubles from `retry_wait_s` and stops at 60 s, and a 429 or 503 waits at least
  its `Retry-After`. Any other failure (another 4xx, a body that is not JSON) raises at once, as before.
  Each retry logs one INFO line with the host and path only, never the query or the key. The policy
  comes from `set_retry_policy` or the environment (`BETTINGPROS_MAX_RETRIES`, default 0, and
  `BETTINGPROS_RETRY_WAIT_S`, default 2.0); it must stay unset in the API container, because the live
  pipeline and the opening-line job call the provider on the app's event loop and a retry's wait would
  block it.
- **The provider counts every read it gives up on** (`read_failures`), at each of its eight places that
  catch a failed read and carry on, never for a real absence (no event, a matcher decline, empty offers,
  a player with no offer, a first-five exclusion). `forget_failed_lookups()` clears the remembered
  failed lookups, so a player whose lookup failed in one game is read again in the next.
- **The loader** takes `--retries` (default 3) and `--retry-wait` (default 5 s) and hands them to the
  provider. A game whose reads failed after the retries, or whose fetch or database write failed inside
  the loader, is INCOMPLETE: its rows so far stay, it stays off the done-list, a WARNING names it, and
  the run lists it at the end and exits 1. The crash-safe loop runs the loader again a minute later on
  exit 1, so only the incomplete games load again (at most 30 runs in all). A run with
  `--skip-loaded-since` warns that it skips an incomplete game that holds rows.
- **Built and checked** by a workflow: two builders in parallel, an integration pass, three adversarial
  reviewers (retry correctness, failure counting and the done-list, tests and docs; eight findings, all
  fixed), then the full unit lane: 4,981 passed, 1 skipped; ruff, ruff format and mypy clean. New tests:
  `tests/unit/test_sim555_provider_retry.py`, `tests/unit/test_sim555_loader_retry.py`.

**The matcher fixed, the games re-matched and re-loaded (2026-10-01; commit 9401438).** By owner
decision ("Fix the matcher"). The provider reads the PLAYED schedule entry; a suspended game keeps its
original first pitch and also searches the resume date's slate; the single-event rule takes the only
same-team event on a day without a double-header within 12 hours of the start; and game 2 of a straight
double-header with no listed start (MLB's placeholder sits 5 minutes after game 1's start) takes the later
of exactly two same-team events. That last rule came from the build's adversarial review: the old
matcher gave such a game 2 game 1's event. A new guard rule, `stamped_before_postponement`, refuses a price
stamped at or before a made-up game's original start (the price of the game that was not played; on game
716597 it refused 210 such rows). `forget_game()` lets the live pipeline read a postponement it learns of
later. Reviewed by three adversarial passes (eight findings, all fixed); unit lane 5,058 passed.
*The data run.* A read-only check compared the old and the new matcher's event on every postponed,
suspended or double-header game with `bp:` rows (340 games): 321 the same, 10 changed - each a
double-header game 2 that had game 1's prices (566734; 630973, 630984, 631152, 631426; 661233, 662199,
662459; 745310, 745659). Their `bp:` rows (511 game rows, 3,044 prop rows) moved to the archive
(`scripts/sim555_archive_wrong_dh_game2.sql`). Then every Final game with no `bp:` row, 839 in all, was
re-loaded through the fixed matcher, one crash-safe loader per season (with `--retries 3`): every season
exited 0; 2023's game 716597 had two failed reads, stayed off the done-list, and loaded on the wrapper's
second attempt, as the retry option intends. The loader logs show the made-up, suspended and game-2
matches by name. The census reads zero on every rule in all eight seasons; the coverage findings are the
three explained first-five moneyline ones. Nine of the ten re-matched game 2s now hold their own prices;
745659's vendor event (92774) carries no offer in any market, so that game has no odds.
*The retirement's dry run* (`--allow-missing 567323 745659`) passes every season: 359,524 game rows and
4,035,864 prop rows to archive and delete. 567323 has no close before first pitch; 745659's only old
rows are game 1's prices. One 2021 game (633417) has old closing props and no new ones; its old props go
to the archive like the rest. The real run waits for the owner: the session's permission check refused
the delete.

**A trap for step 8.** The retirement script refuses a season while any game with a `consensus`
closing moneyline lacks a `bp:` one. A game the event matcher now declines (746572 above: its
old row predates the two-hour limit) can never get a `bp:` row, so it blocks its season. Before
step 8, list those games from the loader logs ("no event for game_pk") and decide: retire with an
explicit list of unmatched games, or keep their old rows in the archive only.
