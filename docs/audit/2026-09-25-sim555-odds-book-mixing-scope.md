# Scope — the odds provider mixes books within one row and never stores which book (SIM-555)

**Date:** 2026-09-25. **Status:** the evidence for this ticket. The checks fixed nothing and wrote to
no database. **Asked by:** the owner, after the diagnosis of the 2025 first-inning run lines that
store +1 for both teams ("file it as SIM-555").

## 1. The short version

The accuracy comparison, the calibration layer and the live line-movement and CLV pages read
stored odds that can mix books. (A book is a sportsbook. CLV, closing line value: whether the
opening price beat the close.) Every stored price comes from BettingPros, which lists up to 17
books and prediction markets in one market, beside its own consensus line. The provider
(`pipeline/bettingpros_odds_provider.py`, the code that reads BettingPros) picks each side's price
on its own. The sides are home, away, tie, over and under. The provider never stores the book:
every row says `book = 'consensus'`. The results:

- **The opening row and the closing row come from different books.** The sample shows this on 36
  of 39 moneyline games and 36 of 40 run-line and total games. The first-five run line shows it on
  35 of 40. The live pages therefore measure a change of book, not a move of the line.
- **One closing row can mix books.** In the full-game markets, the two sides come from two books
  on 4 to 9 of 40 sampled closing rows. On 3 of 40 total rows, the over and the under come from
  two different total lines.
- **The 2025 "+1 / +1" run lines are a second defect, not a mix of books.** Each row traced so
  far is one book's own entry. BettingPros' entry for FanDuel (book 10) carries the spread as 1 for
  both teams on some 2025 dates. The provider stores that entry without testing its shape. One
  book per row does not remove these rows; the load guard of §7 does.

Nobody can repair the stored rows in place, because the provider never kept the per-book data.
The fix is a provider change (one book per row, named on the row), a load guard and a re-load.

**Prior record.** The July bug register (`docs/audit/2026-07-23-MASTER-BUG-REGISTER.md`, rows 1.8,
SIM-bettingpros-1, SIM-oddsprovider-1 and B-N5) records the cross-book closing scan. The
unmerged `wave1-remediation` branch holds a partial fix: it pins one book when a caller asks,
drops lines updated after the scheduled start and returns the update stamp and book id. No
production caller asks for a book, so the fix never reached the stored rows. SIM-555 replaces that
design with a per-offer anchor (§7). Its first-pitch guard needs a decision first (§7, §8).

## 2. How the checks ran

Three read-only checks ran in parallel on 2026-09-25. They wrote no database row and changed no
repository file.

1. **A census of every stored row** of `raw.game_odds` (359,524 rows), in SQL inside
   `BEGIN READ ONLY … ROLLBACK`. Every row reads `source = 'bettingpros'`, `book = 'consensus'` and
   `is_mock = false`. The only line types are `opening` and `closing`. Every `raw.prop_odds` row
   (4,035,864) also says `consensus`.
2. **A sample of BettingPros' raw data.** The sample holds 40 games, 10 from each season 2023–2026,
   one from each tenth of the season. Each game gives five markets: the full-game moneyline, run
   line and total, the first-five run line and the first-inning run line. That is 200 payloads, at
   one request a second. The provider's own rules, replayed on today's payload, reproduce all 200
   opening rows and 199 of 200 closing rows. So the payloads show what the loader saw.
3. **A code audit** of every writer and reader of the stored odds. It ran a second probe of 48
   games (8 each from 2019, 2021 and 2023–2026) to measure book coverage.

The 40-game sample carries these ids (names from BettingPros' `/v3/books` list of 41): 0
BettingPros Consensus (a blend of other books' prices that nobody can bet), 10 FanDuel,
12 DraftKings, 13 Caesars, 14 Fanatics, 15 SugarHouse, 18 BetRivers, 19 BetMGM, 24 bet365,
27 PartyCasino, 33 theScore Bet, 38 ProphetX (an exchange), 39 Fliff, 49 Hard Rock, 60 Novig,
68 Kalshi, 73 Polymarket, 75 Polymarket US. Kalshi and the two Polymarkets are prediction markets.
The 48-game probe also shows 36 Underdog, 37 PrizePicks, 45 Betr and 63 Sleeper. Pinnacle (2)
appears in neither sample.

**The sample is 40 games a market.** Every rate from it carries a wide error. The census counts
cover every stored row.

## 3. The mechanism

`_pick_line` (`pipeline/bettingpros_odds_provider.py:506–566`) runs once for each side:

- **Opening:** the side's `opening_line` field, the entry of the book that BettingPros names as the
  opener. Both sides of every sampled opening row came from one book: FanDuel on 119 of 200 rows,
  then Fanatics 38, DraftKings 32, BetMGM 4, bet365 4 and Caesars 3.
- **Closing:** the line with the latest `updated` stamp across all books. A tie goes to the
  last-listed book (`>=`). The tie decided the pick on 34 to 40 of 40 rows a market. In 2025–26 the
  pick is the last-listed book on 13 to 18 of 20 rows a market: bet365 for the full-game markets,
  PartyCasino for the first-five run line. On 4 of 200 sampled closing rows, one side came from a
  prediction market (Polymarket once, Kalshi twice, Polymarket US once). ProphetX supplied one side
  of 1 more closing row and the whole of 2 first-inning closing rows.
- **Current (the live cycle):** the price BettingPros flags as 'best' for each side, so the two
  sides usually come from two books.
- No production caller sets `prefer_book_id`. Every stored row says `consensus`, because the
  historical loader writes that label.

## 4. What the census and the sample found

| Market | Opening and closing from the same book (sample) | A closing row mixing books (sample) |
|---|---|---|
| Full-game moneyline | 3 / 39 | 4 / 40 |
| Full-game run line | 4 / 40 | 9 / 40 |
| Full-game total | 4 / 40 | 8 / 40 (3 at two total lines) |
| First-five run line | 5 / 40 | 7 / 40 |
| First-inning run line | 27 / 40 (2–4 books an offer) | 2 / 40 |

**Run lines (census, every row).** The store holds 85,409 run-line rows. A pair is one bet with two
sides (home −1.5 against away +1.5). Pairs make up 98.4% of the full-game rows, 91.6% of the
first-inning rows and 78.4% of the first-five rows. Some closing rows hold spreads that no single
book posts together, for example home −1 against away +1.5. Among the latest closing rows there
are 243 such full-game rows, 558 first-five and 157 first-inning. Among the latest opening rows
there are 0, 66 and 0.

**The "+1 / +1" rows.** An implied probability is the chance a price stands for: 100 / (price + 100)
for a plus price, −price / (−price + 100) for a minus price. A book's margin (its built-in cut) is
how far the implied probabilities of its sides add above 1. A test of equal non-zero spreads whose
implied probabilities add to 1.00–1.15, a pair's margin, finds 1,204 stored rows. 1,186 of them are
+1 / +1: first-inning 2025 opening 577 and closing 191, and first-five 2025 opening 418. Their
price sums run 1.042–1.084. They fall into two price classes:

- **1,164 rows priced like a ±1.5 pair** (every opening row and 169 closing rows; the smaller
  implied probability averages 0.13–0.14, the sums run 1.042–1.059). The sample held five of them
  (three games). All five trace to BettingPros' entry for FanDuel, which reads 1 for both teams
  on 2025-04-05, 04-24 and 05-13, at +540 to +710 against −850 to −1,250. The prices match the
  stored ±1.5 first-inning pairs of 2022 and 2024. Nobody knows what the 1 stands for.
- **22 first-inning closing rows of 2025-08-28 to 2025-09-03 priced like the ±0.5 market** (the
  smaller implied probability averages 0.28, the sums run 1.067–1.084). A re-fetch of one of those
  games, 776465 (2025-09-03), shows Hard Rock (49) at 1 for both teams, −400 against +260, beside
  theScore's ±0.5 pair at −400 / +230 (appendix B). So the value 1 does not mean the same bet at
  every book. The book behind the other 21 rows is not traced.

Real two-bet listings (both teams at +x, or both at −x, as two separate bets) sit outside the
band. 2,544 "both +x" rows add above 1.15, and 3,626 "both −x" rows add below 1.00. The "both +x"
rows start at the band's top edge: 353 of them add to 1.15–1.20. One flagged row, a first-five 2021
row at +0.5 / +0.5 adding to 1.141, is probably two real bets. A 1.00–1.10 band, the one §7
recommends, catches the same 1,186 rows and 12 others, and spares it. Nobody has classified the
12 others yet; two of them are first-five 2022 opening rows at +0.5 / +0.5 adding to 1.084 and
1.088.

**Three-way moneylines** (the first-inning and first-five moneyline: home, away or a tie). Many rows
price home and away like a two-way bet, where a tie voids the bet, and add a tie price (the
`draw_ml` column) from a three-way bet. That is a mix of two bets, and probably of two books; the
census inferred it from the prices, not from a book trace. It finds about 2,262 such first-inning
and 7,813 first-five closing rows. The first-five count is approximate.

**Props (2024–2025; the latest row for each player, stat and line type).** Closing rows whose two
prices add outside 1.00–1.15: strikeouts 7.9% (751 of 9,527), total bases 5.7% (4,936 of 86,922),
outs recorded 15% (247 of 1,641). At opening, 0.09% across all prop markets (total bases 0.3%). The
other seasons were not measured. The provider stores the under's line, so the over's price can
belong to another book's line.

**The full-game moneyline and total are the cleanest.** Only 42 of 34,876 moneyline rows and 59 of
34,875 total rows add outside 1.00–1.15. Their defect is the change of book and the mixed rows
above, not wild prices.

## 5. Other defects found by the same scope

Check our own market ids and request parameters before blaming the vendor's data.

### 5a. Cause not yet known

- **First-five ±1.5 run-line pairs priced like the first inning** (the smaller implied probability
  below 0.20): 1,095 of 1,123 (97.5%) opening ±1.5 pairs in 2025, 1,294 of 1,419 (91.2%) in 2022,
  146 of 220 (66.4%) in 2026 and 336 of 592 (56.8%) in 2023. Two leads. BettingPros flags every
  sampled FanDuel first-five line as `is_off`, and the provider ignores that flag. DraftKings'
  first-five entries from 2025 on sit close to its first-inning entries (778445: first five
  −340 / −700, first inning −310 / −650). The price-sum guard cannot see this defect.
- **First-five tie prices priced like a first-inning tie** (implied above 0.35): 2,125 of 2,335
  closing and 2,303 of 2,334 opening rows in 2022, about 600 of each line type in 2023, 649
  opening rows in 2025.
- **Full-game team totals, 2022–2024:** 8,028 of the 27,745 rows of those seasons (29%) add
  outside 1.00–1.15; across every season, 8,184 rows (15%). About 90% of them sit at a line of 1
  with an over price that cannot belong to that line. About a third of all 2022–2024 team-total
  rows sit at a line of 1.

### 5b. Cause known, found by the code audit

- **About 600 games of 2024 hold two rows a market** (3,575 extra rows). The first backfill
  (2026-06-05/06) and the re-load (2026-09-12/13) both kept rows, because their prices differ. On
  18 to 20 games a market the latest row is an opening row, which the line-movement page reads as
  the close.
- **The live prop cycle.** With `ODDS_PROVIDER=bettingpros`, it would write one set of prices four
  times, under the names Pinnacle, Circa, DraftKings and FanDuel (`PROP_BOOKS`,
  `pipeline/live/live_ingestion_pipeline.py:155–160`). Pinnacle and Circa carry the sharp flag,
  which marks a book whose prices lead the market. The provider ignores the name when it picks.
  The default provider is the mock, and the live pipeline is off by default
  (`LIVE_PIPELINE_ENABLED`), so no stored row comes from it.
- **Two dormant defects.** `mark_closing_lines` relabels one row across ALL markets (`LIMIT 1`),
  although its docstring promises one row a market. `opening_line_job` inserts game rows without
  an `odds_hash`, and the database refuses them (the column is NOT NULL since migration 0012). It
  can also pass a NULL prop line into a NOT NULL column. Its prop rows carry no `odds_hash`, so they
  escape the dedup index. Neither function has a production caller today.

## 6. Who reads the wrong numbers

- **The accuracy comparison** (`scripts/clv_backtest.py`) reads only the closing row, the latest by
  fetch time with no book filter. The change of book does not touch it; the mixing inside a
  closing row does. For totals, team totals and props, the over and the under can come from two
  books at two lines. The comparison then de-vigs the two prices (removes the book's cut),
  although no book offers that bet. For the three-way moneylines, the comparison removes one
  margin from three prices of two bets. It scores a +1 / +1 row as two one-sided bets: separate
  +1 bets, each with no opposite side. Only the one-sided-line guard from the run-line scoring work
  (SIM-549) catches that, and it reads a whole season's market, not a single row. The strikeout
  market, the first job of the draw-weight fit (SIM-548), has 7.9% of its 2024–2025 closing rows
  out of band.
- **The live line-movement and CLV pages** (`betting/line_movement.py`, `/line-movement`, `/clv`)
  serve only the full-game moneyline, run line and total (`api/routes/betting.py:141`). So they
  never read the first-inning or first-five rows. They compare the first and last row by fetch
  time under one label, so they compare two books. They refuse a run-line CLV when the spread
  moved, but they compute a total's CLV across a moved total line; the full-game total line moves
  on 21–61% of games. Their reference margin cannot work while every row says `consensus`. (The
  reference margin borrows a book's cut from another market of the same game, and it looks for the
  same book first.) Their sharp-book flag stays empty even after the fix: no BettingPros MLB book
  is flagged sharp.
- **Three more readers take the closing rows through the accuracy reports:** the per-market skill
  table (`scripts/sim548_market_skill.py`), the calibration layer
  (`scripts/sim548_calibrate_markets.py`) and the paired accuracy reads
  (`scripts/sim518_pair_accuracy.py`). **Three scripts read the store directly**, the latest row by
  fetch time with no label filter: the run-line re-score (`scripts/sim549_rescore_runlines.py`),
  `scripts/sim427_manager_probe.py` and `scripts/sim518_fatigue_probe.py`.
- **Not affected:** `/edges` and `/signals`, which price mock or injected odds.

## 7. The fix

1. **One book per row, named on the row.** In the provider, choose one book for each market offer.
   Read every side and both line types from that book.
   - **The anchor** is the book that BettingPros names as the opener (the `opening_line` field) on
     every side of the row. The 48-game probe found an anchor in every two-way offer except 1 of
     48 moneylines and 3 of 86 strikeout props.
   - **Without an anchor,** write no opening row: the feed gives each side's opening price from one
     book only. For the closing and current rows, take the first book in a fixed list that quotes
     every side: DraftKings, FanDuel, BetMGM, Caesars, Fanatics, BetRivers, SugarHouse, bet365,
     theScore, Hard Rock. Never take book 0.
   - **Skip every line BettingPros flags `is_off` or inactive.** The skip alone does not keep
     first-inning prices out of first-five rows. Replayed on the 40-game sample without the skip,
     the anchor rule puts first-inning prices into 11 of 40 first-five closing rows (FanDuel 8,
     DraftKings 3). The skip removes FanDuel's lines. The anchor and the fixed list then take
     DraftKings' entries for 14 of the 20 sampled 2025–26 first-five closing rows, and those
     entries also carry first-inning prices. So explain the first-five entries (§5a), opening and
     closing, before the re-load, and keep first-inning prices out of first-five rows.
   - **The first-pitch guard needs a decision.** The branch keeps only lines stamped before MLB's
     scheduled start (`gameDate`). In the 2025–26 sample, the newest stamp of every offer sits
     0–3 minutes after the scheduled start. On 75 of those 100 offers, no book holds a line
     stamped before it. The branch's guard would leave those games with no closing row. Settle
     what `updated` means (§8) before adding the guard.
   - Delete the scan that takes the latest-updated price across all books. Delete the scan that
     takes each side's 'best' price.
   - Write `book = 'bp:<id>'`. No schema change: `book` is `VARCHAR(50)` with no check constraint.
     Map the id to the book's name where the game page shows it.
   - Fill only the requested market's own columns on every row. Drop the legacy full-game row
     that carries all three markets' fields; no reader takes one market's fields from another
     market's row.
2. **The load guard: refuse a row that cannot be one bet.** It refuses a run-line row whose equal
   non-zero spreads are priced like a pair (1.00–1.10 recommended: all 1,186 +1 / +1 rows and 12
   others on the store, the 12 still unclassified). It refuses a run-line row whose two spreads
   differ in size. It refuses a total, team-total or prop row whose over and under carry different
   lines. The guard logs and counts each refusal. Separately, a three-way row whose book lists no
   tie price keeps `draw_ml` empty.
3. **The live cycle.** Map the prop cycle's book names to BettingPros ids and pass the id. Drop the
   names BettingPros does not carry (Pinnacle, Circa).
4. **The readers.** Filter every reader to the new label until the old rows are gone. The
   line-movement series takes its start from the opening row and its end from the closing row,
   one book at a time. The live CLV refuses a total's CLV when the total line moved, as it does for
   the run line.
5. **The tests.** About 30 unit tests in six files pin today's per-side pick. For example,
   `tests/unit/test_sim435_historical_odds.py:100` pins a strikeout over closing at book 18 and its
   under at book 15. Rewrite them on purpose.
6. **The re-load.** Stored odds cover 16,845 games (2019 2,208; 2020 816; 2021 2,232; 2022 2,343;
   2023 2,424; 2024 2,439; 2025 2,440; 2026 1,943). The code audit's probe fetched the game markets
   at about 4.4 s a game, so the game markets take about 21 hours. Props add about 52 hours, an
   estimate, not a measurement: the documented re-load speed of about 18 s a game covers all 30
   markets. That leaves about 13.6 s a game for the props of the 13,722 games of 2021–2026 that
   carry them. Delete the 3,575 duplicate 2024 rows first. After a verification of the new rows,
   delete the old `consensus` rows. Then re-run the accuracy baselines and the calibration layer:
   the market probabilities change.

## 8. Risks and open questions

- **A stale close.** In the probe's seasons 2019, 2021, 2023 and 2024, the anchor's last update
  lagged the newest book's by a median of 20 to 180 minutes, by season. In 2025 and 2026 the lag is
  zero. In the 40-game sample, the newest stamp of each of the 100 offers of 2025–26 sits 0–3
  minutes after the scheduled start. On 43 of them, every book carries that one stamp. That
  pattern reads like a sighting at game time, not a last change. Nobody has confirmed what
  BettingPros' `updated` means.
- **The book behind "the line" varies by game** (FanDuel opens most early-season offers). The
  accuracy comparison then grades against books of different sharpness.
- **Three-way opening rows shrink:** all three opening prices shared one book on only 10 of 40
  first-inning and 22 of 40 first-five sample offers.
- **A re-fetch is not reproducible over time,** and nobody has checked BettingPros' rate limits.
- **Certify on hundreds of games, not 40–48** (CLAUDE.md §2b): a larger probe must confirm the
  anchor coverage before the re-load.

## Appendix A — the load guard's first rule as SQL

```sql
-- equal non-zero spreads whose two prices add like a pair (1.00-1.10 recommended)
WITH r AS (
  SELECT g.season, o.market_type, o.line_type, o.home_spread hs, o.away_spread asp,
    CASE WHEN o.home_spread_ml < 0 THEN -o.home_spread_ml::float8 / (-o.home_spread_ml + 100)
         ELSE 100.0 / (o.home_spread_ml + 100) END ph,
    CASE WHEN o.away_spread_ml < 0 THEN -o.away_spread_ml::float8 / (-o.away_spread_ml + 100)
         ELSE 100.0 / (o.away_spread_ml + 100) END pa
  FROM raw.game_odds o JOIN raw.games g USING (game_pk)
  WHERE o.market_type LIKE '%runline%'
    AND abs(o.home_spread_ml) >= 100 AND abs(o.away_spread_ml) >= 100
)
SELECT market_type, season, line_type, hs, asp, count(*)
FROM r
WHERE hs = asp AND hs <> 0 AND ph + pa BETWEEN 1.00 AND 1.10
GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3;
```

## Appendix B — game 776465's first-inning payload (re-fetched 2026-09-25)

BettingPros event 97809, San Diego (home) against Baltimore, market 282 (first-inning run line).
Every line is stamped 2025-09-03 20:11:43.

| Book | Baltimore | San Diego |
|---|---|---|
| Opening line (FanDuel, 10) | +1.5 at −1,250 | −1.5 at +710 |
| 0 BettingPros Consensus | +1.5 at −1,250 | −1.5 at +230 |
| 10 FanDuel | +1.5 at −1,250 | −1.5 at +710 |
| 33 theScore Bet | +0.5 at −400 | −0.5 at +230 |
| 49 Hard Rock | 1 at −400 | 1 at +260 |

The stored closing row reads home 1 at +260, away 1 at −400: Hard Rock, the last-listed book. The
stored opening row reads FanDuel's ±1.5 pair. The consensus line shows San Diego at −1.5,
FanDuel's line, at +230, the price theScore posts for its −0.5 bet. So the consensus appears to
pair one bet's line with another bet's price.
