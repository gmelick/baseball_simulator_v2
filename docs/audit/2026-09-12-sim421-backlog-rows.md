# BACKLOG.xlsx rows for the prop-market work — text to paste (2026-09-12)

> **Superseded in part (owner ruling 2026-09-12, night).** The subtitle row carries ONLY the
> updated date, the two doc pointers and the next free ticket ID. Every "subtitle note" this
> file staged is therefore retired; the closures they described are recorded in `CHANGES.md`
> (each has its own entry). The row texts below stand as the record of what was written into
> the ticket rows.

**APPLIED 2026-09-12.** `BACKLOG.xlsx` was open in Excel (locked) while the build ran, so
this file was written first as the paste-ready text. Excel released the file at the end of
the session and every block below was written into the `Open Tickets` tab with openpyxl:
the subtitle row (row 2), the SIM-421 row (row 6) and a new SIM-545 row (row 7, inserted
after SIM-421 with the same cell styles). Do not paste these blocks again. This file stays
as the record of what was written. The sheet keeps one tab, one row per open ticket, ranked
by priority, and the six columns Priority / Ticket ID / Title / Description / Definition of
Done / Proposed Solution.

The text reflects the state at the end of 2026-09-12. The two migrations are applied. The
box-score backfill and its audit ran on the full 2024 season, 2,472 games
(`docs/audit/2026-09-12-sim545-boxscore-audit.md`). The odds run for the eight new markets
is still pending.

---

## 1. The subtitle row (row 2, column A) — replace the opening and prepend two notes

The row's opening sentence and the NEXT FREE TICKET ID change. The two new notes go at the
FRONT of the existing parenthesised list; keep every existing note after them unchanged.

**New opening (replaces "Updated 2026-09-11. ... NEXT FREE TICKET ID: SIM-545  ("):**

> Updated 2026-09-12. Ranked by priority. The frozen closed-ticket history lives at docs/archive/BACKLOG-history.md.  NEXT FREE TICKET ID: SIM-546  (

**The two new notes (paste right after that opening parenthesis, before "SIM-544 CLOSED 2026-09-11 …"):**

> SIM-545 FILED AND CODE COMPLETE 2026-09-12 — the official box score is now the ground truth for grading player props. One row per player who appeared in a game, copied from MLB's box score. A player with no row did not play, so his bet is voided, the way a sportsbook does it. The historical loader writes it for every game it loads from now on. A backfill script covers the games already loaded. An audit script measures how far the platform's own per-pitch numbers sit from the official ones. Ran 2026-09-12: the two migrations are applied. The backfill loaded the full 2024 season, 2,472 games, 72,651 rows, zero failures. The audit ran on all 2,472 games. The platform's own numbers are exact on every hit-type stat and RBI. They are not exact on steals, runs, strikeouts, walks, outs and earned runs. That is the evidence for grading props on the official box score. Not closed: the other nine seasons' box scores still have to load, about 2-3 hours. SIM-421 CODE COMPLETE 2026-09-12 — the platform now prices fifteen player-prop types instead of seven. Singles, doubles, triples, runs, stolen bases, hits+runs+RBI, pitcher outs recorded and pitcher hits allowed joined the original seven. The odds loader asks a pitcher only for pitcher markets and a hitter only for batter markets. So "hits allowed" can never be confused with a batter's "hits". Two real defects in the odds provider were fixed on the way. It downloaded the same offers hundreds of times per game. It read only the first page of a market's offers. Not closed: the odds for the eight new markets still have to be loaded. That run must be coordinated with the owner's wrong-game odds re-load (SIM-536).

---

## 2. The SIM-421 row (row 6 today) — replace Description, Definition of Done, Proposed Solution

**Priority:** P1 (unchanged)
**Ticket ID:** SIM-421 (unchanged)
**Title:** Add the prop-bet types the market already offers but the platform does not price (unchanged)

**Description (replace the cell):**

> STATUS 2026-09-12: CODE COMPLETE. The odds data run for the eight new markets is PENDING. A live check of the platform's own odds source ran on 2026-09-11, over two days of real games. It found eight prop types posted on nearly every game that the platform did not price. They are singles, doubles, triples, runs scored, stolen bases, a combined hits+runs+RBI bet, pitcher outs recorded and pitcher hits allowed. Together they carried 5,613 real, gradable bets over the two days. The platform's own seven prop types carried 4,233. The code now prices all fifteen. The simulator emits a probability distribution for each new stat off the per-player game record it already keeps. The odds vocabulary lists all fifteen markets in one place. The database rule that limits the stored market names was widened (migration 0022, applied). The odds loader asks a pitcher for the five pitcher markets and a hitter for the ten batter markets. So pitcher "hits allowed" and batter "hits" can never cross. The accuracy comparison grades every one of the fifteen against the official box score (SIM-545). What is left is data, not code: load the eight new markets' odds for the seasons already loaded. That run must wait for, or be coordinated with, the owner's re-load of odds that matched the wrong game (SIM-536). The owner runs that one personally and asked that nobody else start it.

**Definition of Done (replace the cell):**

> The platform produces its own price projection for each of the eight new prop types (DONE 2026-09-12). The posted odds for each of the eight are loaded for at least the 2024 season (PENDING; coordinate with SIM-536). The accuracy comparison has been re-run. Its report says how many games it graded on the official box score. Lower-volume game-segment markets (first/fifth-inning splits, team totals, first team to score) are a follow-on, not part of this ticket.

**Proposed Solution (replace the cell):**

> The two migrations are already applied (2026-09-12). Remaining steps, in order. (1) Wait for the owner's wrong-game odds re-load (SIM-536) to finish. (2) Load only the eight new markets for the 2024 season: `ODDS_PROVIDER=bettingpros ODDS_API_KEY=... python scripts/load_historical_odds.py --seasons 2024 --no-game-odds --prop-stats singles doubles triples runs stolen_bases hits_runs_rbis outs_recorded hits_allowed`. The game lines are skipped. A re-run is safe to repeat. This first real run also checks the eight market ids taken from the owner's scraper. The build could not reach the live odds API. (3) Re-run the accuracy comparison (`scripts/clv_backtest.py`). Read its header line first. It says how many games were graded on the official box score and how many on the older per-pitch fallback. Build details: `CHANGES.md` (2026-09-12) and the build record at the end of `docs/audit/2026-09-11-sim421-prop-market-scrape.md`.

---

## 3. The NEW SIM-545 row — insert directly below SIM-421

**Priority:** P1
**Ticket ID:** SIM-545
**Title:** Store the official per-player box score as the ground truth for grading player props

**Description:**

> STATUS 2026-09-12: CODE COMPLETE, DATA LOADED FOR 2024. The migrations are applied. The backfill and the audit ran on the full 2024 season, 2,472 games. The other nine seasons are PENDING. A sportsbook settles a player prop against the official box score. Did this batter get two hits? Did this pitcher record sixteen outs? The platform had no copy of that record. It worked out each player's real totals from its own pitch-by-pitch data instead. That derivation covers only five stats: a batter's hits, home runs and total bases; a pitcher's strikeouts and walks. It misses what never appears on a pitch, such as an intentional walk or a runner picked off. Every one of the eight new prop types (SIM-421) needs a real total that this derivation cannot supply exactly. This ticket adds a new table, `raw.game_player_stats` (migration 0023). It holds the official box score line for every player who appeared in a game. The batting line holds plate appearances, at-bats, runs, hits, doubles, triples, home runs and RBI. It also holds steals, caught stealing, walks, strikeouts, hit by pitch, sacrifice flies and total bases. The pitching line holds outs, hits, runs, earned runs, walks, strikeouts, home runs, pitches, batters faced and a started flag. A player who did not play has no row. Every reader treats "no row" as "the book voids the bet": the player is skipped, never scored as zero. The game loader now writes this table for every game it loads. A backfill script fills it for the games already loaded. An audit script compares the platform's derived totals with the official ones, stat by stat. The accuracy comparison and the prop-validation job read the official record first. They fall back to the old derivation only for a game with no rows. Both report how many games went each way.

**Definition of Done:**

> The two migrations are applied (DONE 2026-09-12). The box-score backfill has run for every loaded season (2024 DONE, 2,472 games; nine seasons PENDING). The table holds a row for every player who appeared in every completed game (DONE for 2024). The audit has run over at least a few hundred games (DONE on all 2,472 games of 2024). Its per-stat exact-match rate and bias are recorded in docs/audit/2026-09-12-sim545-boxscore-audit.md (DONE). The OUTS and ER gaps are explained (DONE: a displaced-runner out; an inherited runner charged to the wrong pitcher). The K gap is explained too (a strikeout the feed anchors to a trailing no-pitch event). The R, SB and BB gaps have hypotheses only; none blocks grading. The event-label fallback counts an intentional walk as a walk (DONE). The accuracy comparison reports every game graded on the official box score, none on the fallback (PENDING). It waits on the odds run (SIM-421).

**Proposed Solution:**

> The code is built and unit-tested on a real captured box score (tests/fixtures/mlb/boxscore_746437.json). Done 2026-09-12: the two migrations are applied. The backfill loaded the full 2024 season: 2,472 games, 72,651 rows, zero failures. The audit ran on all 2,472 games; its table is in docs/audit/2026-09-12-sim545-boxscore-audit.md. The fallback now counts `intent_walk` as a walk. Run, in order. (1) The backfill for the other nine seasons: `python scripts/load_official_boxscores.py --seasons 2017 2018 2019 2020 2021 2022 2023 2025 2026`. One MLB request per game, a quarter-second apart, about 2-3 hours. A failed game is logged and counted. Five failures in a row stop the run with exit code 1 (`--max-consecutive-failures`). That pattern is an outage or a schema mismatch, not one bad game. A re-run skips games that already have rows. (2) Re-run `scripts/clv_backtest.py` and `scripts/validate_props.py` once the eight new markets' odds are loaded (SIM-421). A non-zero bias on a stat is a finding about the platform's pitch data, not about the box score. Two things are on purpose; do not "fix" them. The table's player id has no link to the players table. A box score can name a player the pitch data never recorded. A missing ground-truth row must never come from a foreign-key failure. A bench player gets no row rather than an all-zero one.

---

## 4. The SIM-484 row — replace Description, Definition of Done, Proposed Solution (owner request, 2026-09-12)

**APPLIED 2026-09-12** (the owner closed the file; the three cells, the title and a subtitle note were written with openpyxl). Original note: `BACKLOG.xlsx` was locked again (open in Excel) when the owner asked
for this. Paste these three cells into the SIM-484 row, or run the session's write step
once Excel releases the file.

**Priority:** P2 (unchanged)
**Ticket ID:** SIM-484 (unchanged)
**Title:** Fix the box-score credits on the dropped-third-strike play (replaces "Model the 'dropped third strike' play")

**Description (replace the cell):**

> The old text said the simulator does not model the dropped third strike. That is stale. The simulator models it today. On a swinging third strike that gets away, with first base open or two outs, the batter reaches first. Forced runners move up. The code is `_dropped_third_strike` and `_force_on_reach` in `simulation/sim_loop.py`. The got-away rate is a graded pool band. What is still wrong is the box-score bookkeeping on that play. The prop-market review found it on 2026-09-12. (1) The pitcher gets NO strikeout in the box. The play commits to the run ledger as a reach on an error, and the box reads that label. Official scoring credits the strikeout to the pitcher and charges it to the batter. The strikeout prop under-counts by one on every such play. (2) A run forced home by the reach pays the batter an RBI. The rules withhold the RBI when the run scores on a wild pitch or a passed ball. (3) On a pitch that ends the plate appearance, a steal resolves before the dropped-third-strike test runs. The "first base open" test then reads the bases after the steal, not at the pitch. A test in `tests/unit/test_sim421_runner_run_credit.py` pins item (1) so any change is seen. The runner's run credit on this play is already fixed (2026-09-12).

**Definition of Done (replace the cell):**

> The box credits the pitcher a strikeout on a dropped-third-strike reach. The batter's line records the strikeout too. The batter gets no RBI for a run forced home by that reach. The "first base open" test reads the bases as they were at the pitch. The team score, the outs and the runs allowed do not change. The run-credit test file covers all three cases.

**Proposed Solution (replace the cell):**

> Give the play its own label in the run ledger's commit: a strikeout on which the batter reached. Or carry a "strikeout on this play" flag on `PlayResult` that `_accumulate_pa` reads for the two strikeout credits. Withhold the RBI when the reach is the got-away kind. Snapshot the bases before the steal resolves, and pass that snapshot to the predicate. Three small edits to `simulation/sim_loop.py`. The run-credit harness already exists. Rare play, about a few hundred per season, so no band moves; the strikeout prop's actual and projection must agree on it.

---

## 5. SIM-545 CLOSED — remove its row, add this note to the subtitle (2026-09-12, afternoon)

**APPLIED 2026-09-12** (written with openpyxl once the owner closed the file; the SIM-545 row is gone and the note is in the subtitle). Original instruction: delete the SIM-545
row and paste this note at the front of the subtitle row's parenthesised list:

> SIM-545 CLOSED 2026-09-12 — the official box score is the prop ground truth. All ten seasons are loaded: 22,742 games, 669,131 player lines, zero failures; on every game the box runs equal the final score. The audit over the full 2024 season showed the old per-pitch derivation exact on hit types and RBI but not on steals, runs, strikeouts, walks, outs or earned runs, so every prop is graded on the box. The backtest's read path was proved on live 2024 rows. Open residue moved to SIM-484 (the dropped-third-strike box credits).

---

## 6. SIM-421 and SIM-536 CLOSED — applied 2026-09-12 (night)

Both rows were removed and their closure notes written into the subtitle row by openpyxl on
the owner's instruction. SIM-546 (the API / game-page surface for the twelve segment markets)
stays open at P2. Next free ticket ID: SIM-547.
