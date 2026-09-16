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

## Build record — 2026-09-12

**What shipped (code complete, unit-tested; the migrations applied and the box-score
backfill + audit run on the full 2024 season the same day).** Four engineers built the
recommended scope in parallel, one file set
each; `CHANGES.md` (2026-09-12) has the full entry and `docs/technical/` the per-module
reference. In short:

1. **The simulator prices all fifteen markets.** `simulation/prop_distributions.py` now
   emits `1B`, `2B`, `3B`, `R`, `SB`, `HRR` (hits + runs + RBI, summed per simulated game
   and then turned into a distribution — never a convolution of the three separate
   distributions) for batters and `H_ALLOWED` for pitchers, beside the original
   H/HR/RBI/TB and K/BB/ER/OUTS. The original four stay first in each tuple; the frontend
   renders chips in tuple order and needed no change.
2. **The odds vocabulary has one source.** `pipeline/odds_provider.py` exports the fifteen
   `prop_stat` strings, split into five pitcher markets and ten batter markets, plus the
   odds-to-model map. Alembic migration 0022 widens the CHECK constraint on
   `raw.prop_odds.prop_stat` to the same fifteen values. Its downgrade deletes the rows of
   the eight new markets before it restores the old constraint.
3. **Markets are requested by role**, so the "hits allowed" conflation this document
   flagged in step 3 cannot happen: a pitcher is asked for the five pitcher markets and a
   hitter for the ten batter markets (a two-way player, or a lineup entry with no
   position, is asked for everything). The BettingPros market ids are the ones in the
   scraper's `market_dict` (singles 295, doubles 291, triples 292, runs 288, stolen bases
   294, hits+runs+RBI 403, outs recorded 405, hits allowed 404). They were NOT verified
   against the live API in this build (no network call was allowed).
4. **Two BettingPros provider defects were fixed on the way:** the provider re-fetched the
   same offers response for every (player, market, book, line type) — it now caches one
   response per (event, market) with a time-to-live (`ODDS_OFFERS_CACHE_TTL_S`, default
   30 s; the offline loader uses 600 s) — and it read only page 1 of a paged offers
   response, so a batter market's later pages were silently dropped. It now follows the
   pagination.
5. **The official box score is the prop ground truth (the sub-ticket, SIM-545).** Alembic
   migration 0023 adds `raw.game_player_stats`: one row per (game, player) for every player
   who appeared, copied verbatim from the MLB box score. No row means "did not play", and
   every reader skips that player (the book voids his bet). The historical loader writes
   it from the feed it already holds; `pipeline/etl/boxscore_ingest.py` is the parser and
   upsert; `scripts/load_official_boxscores.py` backfills the games already loaded;
   `scripts/sim545_boxscore_audit.py` measures how far the platform's own per-pitch
   derivation sits from the official row, stat by stat, over hundreds of games.
6. **The accuracy comparison and the validation job grade every market on the box.**
   `scripts/clv_backtest.py` and `scripts/validate_props.py` pick the ground truth per
   game — the official box score when the game has rows (every market, RBI and ER
   included), else the event-label reader (H/HR/TB, K/BB only) — and both report how many
   games each source graded. The eight new model props carry an `unvalidated` trust label
   until the comparison has scored them.

**The data runs that ran on 2026-09-12.** The two migrations are applied on the live
Postgres (`alembic current` prints `0023`). The box-score backfill ran for the full 2024
season — 2,472 games, 72,651 `raw.game_player_stats` rows, zero failures — and the audit
ran on all 2,472 games: the study, with the full per-stat table and the causes, is
`docs/audit/2026-09-12-sim545-boxscore-audit.md`. In one line: the `raw.pitches`
derivation is exact on every hit-type prop and RBI, and it is not exact on SB, R, K, BB,
OUTS and ER — the evidence for grading every prop on the official box score.

**The data runs still to do, in this order.**

1. Backfill the official box scores for the other nine seasons (one MLB request per game,
   about 0.25 s apart, about 2–3 hours; the 2024 season is done):

       python scripts/load_official_boxscores.py --seasons 2017 2018 2019 2020 2021 2022 2023 2025 2026

2. (Done.) The audit ran on the full 2024 season; the OUTS, ER and K gaps are explained
   and the R, SB and BB gaps carry hypotheses (see the study). The BB pairs need no
   ruling: the sim's BB line counts an intentional walk (`_BB_CANONICAL` in
   `simulation/sim_loop.py`), the box's walks total counts it too, and the event-label
   fallback in `simulation/prop_validation.py` now lists `intent_walk` in `_WALK_EVENTS`.
3. Load the eight new markets' odds. This is the exact command for a season whose seven
   original markets are already loaded (the game lines are skipped; a re-run is
   idempotent through the existing dedup):

       ODDS_PROVIDER=bettingpros ODDS_API_KEY=... python scripts/load_historical_odds.py \
           --seasons 2024 --no-game-odds \
           --prop-stats singles doubles triples runs stolen_bases hits_runs_rbis outs_recorded hits_allowed

   Coordinate it with the owner's wrong-game odds re-load (SIM-536), which the owner runs
   personally when fewer processes compete for the database and the odds API. Do not
   start it on your own. The first real run is also the check on the eight market ids.
4. Re-run the accuracy comparison (`scripts/clv_backtest.py`) and read the new header
   line first: it says how many games were graded on the official box score and how many
   on the event-label fallback. A run graded mostly on the fallback is a five-prop report.

**Loose ends recorded, not fixed here.** The mock provider's line centres for the new
markets are placeholders. The frontend's generated OpenAPI mirror (`frontend/openapi.json`,
`frontend/src/api/schema.d.ts`) was regenerated and carries the fifteen-prop text; the
frontend type-check and lint pass. The game-level segment markets in step 4 of the recommended scope
(first/fifth inning splits, team totals, first team to score) remain a follow-on. The
dropped third strike with the bases loaded, the fourth per-runner run-credit path the
review found, is fixed (`_force_on_reach` records the forced runners). Still open, all
pre-existing: a dropped-third-strike reach credits the pitcher no strikeout in the box and
its forced run pays the batter an RBI the rules withhold — both now the scope of the
dropped-third-strike ticket (SIM-484, owner decision 2026-09-12); a pickoff error staged
from third base moves the runner off the bases with no run (`CHANGES.md`, 2026-09-12).
