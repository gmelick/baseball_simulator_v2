# What prop markets actually exist — a live scrape (SIM-421 scoping)

**Date:** 2026-09-11
**Method:** ran `refresh_odds` from the owner's external odds scraper
(`Odds_Functions/scrape_odds.py`, BettingPros — the same provider already wired into
`pipeline/bettingpros_odds_provider.py`) for 2026-09-07 through 2026-09-08: 23 real MLB
games, 10,577 odds rows, one snapshot per market per game/player. This is a market-menu
check, not a statistical-power sample — two days is enough to see what a book posts every
day, not enough to judge how stable any one number is.

## What's posted, by volume

| Market | Rows | Distinct players | Already modeled? | Already in the odds vocab? |
|---|---:|---:|---|---|
| Hits | 1025 | 344 | ✅ (`H`) | ✅ |
| Total Bases | 1017 | 344 | ✅ (`TB`) | ✅ |
| RBIs | 1027 | 340 | ✅ (`RBI`) | ✅ |
| Home Runs | 898 | 317 | ✅ (`HR`) | ✅ |
| **Singles** | 1021 | 334 | ✅ (computable: `h - b2 - b3 - hr`) | ❌ |
| **Runs** | 975 | 316 | ✅ (`PlayerStatLine.r`) | ❌ |
| **Doubles** | 972 | 316 | ✅ (`PlayerStatLine.b2`) | ❌ |
| **Triples** | 946 | 340 | ✅ (`PlayerStatLine.b3`) | ❌ |
| **Runs+Hits+RBIs** | 933 | 326 | ✅ (sum of `r`+`h`+`rbi`) | ❌ |
| **Steals** | 595 | 266 | ✅ (`PlayerStatLine.sb`) | ❌ |
| Strikeouts | 92 | 45 | ✅ (`K`) | ✅ |
| Earned Runs | 88 | 44 | ✅ (`ER`) | ✅ |
| Walks Allowed | 86 | 43 | ✅ (`BB`) | ✅ |
| **Hits Allowed** | 90 | 44 | ✅ (`H` on the pitching side — confirm the ingestion doesn't conflate this with batter `Hits`) | ❌ |
| **Outs Recorded** | 84 | 42 | ✅ (`OUTS` PMF already built, per `simulation/prop_distributions.py`) | ❌ |
| Fifth Inning Team Total, Team Total, First/Fifth Inning Moneyline/Total/Run Line, Run in First Inning, First Team to Score | 44–92 each | game-level | partial (needs per-segment scoring, not just full-game) | ❌ |

Bold rows are real, currently-posted markets the platform does not yet price. Every one of
the bold **batter** rows is a straight read of a field `PlayerStatLine` already carries on
every simulated game — none of them need new simulation logic, only a `PropDistribution`
wrapper, an odds-vocabulary entry, and the backtest plumbing already built for the other
seven props.

## What this means for the size of the opportunity

Summing the rows for props not currently priced (Singles, Runs, Doubles, Triples,
Runs+Hits+RBIs, Steals, Hits Allowed, Outs Recorded) against the props already priced (Hits,
Total Bases, RBIs, Home Runs, Strikeouts, Earned Runs, Walks Allowed): the currently-unpriced
set is **5,613 rows against 4,233 currently addressable, from just two days**. Expanding
batter-prop coverage roughly doubles how many real, graded bets the new accuracy comparison
(SIM-538/SIM-541) has to work with — this is not a minor completeness item, it is close to
the single biggest lever on that comparison's statistical power available right now.

## Recommended scope and order for SIM-421

1. **Doubles, Triples, Runs, Steals, Singles, Runs+Hits+RBIs** (batter) — highest volume,
   zero new simulation work, only new `PropDistribution` wrappers + odds-vocabulary entries.
2. **Outs Recorded** (pitcher) — the model output already exists (`OUTS`); this is purely an
   odds-ingestion and vocabulary gap.
3. **Hits Allowed** (pitcher) — before wiring it, confirm the odds ingestion path
   distinguishes it from the batter `Hits` market; BettingPros carries both as separate
   market IDs (287 vs. 404 in the scraper's `market_dict`) but it's worth a direct check
   that nothing downstream conflates them.
4. Game-level segment markets (first/fifth-inning splits, team totals, first team to score)
   — real and posted, but lower volume (44–92 rows/2 days) and need per-segment scoring
   (e.g. a first-inning-only run total) that the full-game props don't, so lower priority.

## Caveat

Two days of one provider is a menu check, not proof every market has been available all
season, and not a odds-matching guarantee — `SIM-536` (the wrong-game-odds fix) still applies
to whatever the platform ends up ingesting for these markets too.
