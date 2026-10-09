# Tech design — the twelve segment and team markets on the API and the game page, and every live game's closing prices (SIM-546)

> **STATUS 2026-10-08 — DESIGN, PROPOSED. Nothing is built.** Five decisions wait for the
> owner (§10). The build order and the run book are in §8. The ticket is P2 in `BACKLOG.xlsx`;
> the next free ID is SIM-560. The readable page is https://claude.ai/artifact/3192dt3Z27A8YHAZfp8MRL.

**Date:** 2026-10-08
**Ticket:** SIM-546 (P2). The row asks for two things: the twelve segment and team markets on
the edge endpoint and the game page, and a closing row for every live game.
**Evidence:** the code at commit 80e3f46 — the pricing module
(`simulation/game_market_distributions.py`), the edge route (`api/routes/betting.py`), the
report builders (`betting/clv_engine.py`), the runner and its cache
(`simulation/batch_runner.py`, `simulation/results.py`), the loop (`simulation/sim_loop.py`),
the live pipeline (`pipeline/live/live_ingestion_pipeline.py`), the load guard
(`pipeline/odds_row_guard.py`), the historical loader (`scripts/load_historical_odds.py`), the
accuracy comparison (`scripts/clv_backtest.py`), the betting card
(`frontend/src/components/games/BettingCard.tsx`) and the nightly chain
(`scripts/nightly_ingest.sh`, `deploy/ofelia/config.ini`). The one-book-per-row plan
(`docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md`) and the changelog entries of
2026-09-12 and 2026-10-01 (`CHANGES.md`).

---

## 0. The short version

**The problem.** The platform prices fifteen bets on a game. Three cover the whole game: who
wins, the margin of victory against a spread, and the total runs. Twelve cover a slice of it:
the first inning, the first five innings, one team's runs, or which team scores first. We store
every sportsbook's prices for all fifteen, and our offline accuracy study grades all fifteen.
But the live product shows three. When a user opens the betting card on a game page, the server
simulates the game 200 times and prices the bets from those 200 outcomes. It keeps only each
simulated game's final score. A final score answers "who won" and "how many runs in total"; it
cannot answer "how many runs in the first five innings". The runs per inning exist while each
game is simulated and are thrown away. The code that turns per-inning runs into prices for the
twelve markets exists and is tested; nothing on the live path gives it data.

**The second problem: closing prices.** A closing price is the last price a sportsbook posts
before first pitch. It is the reference every bet is judged against, and the live pages (the
edge report, line movement, closing-line value) need it. On a game day we store each book's
current price every few minutes. At first pitch, the last stored price for each market and each
book should be marked as that book's closing price. The code for this exists, but nothing calls
it, and it marks one row per game instead of one per market and book (about 180 rows). So a live
game has no closing prices. They appear only if someone runs the historical backfill the next
morning, and that is not scheduled either. One more wrinkle: each stored price carries a
fingerprint that stops duplicates, and the fingerprint includes whether the row is "current" or
"closing". Marking a row closing without recomputing its fingerprint would let the morning
backfill write the same price again as a second closing row.

**The fix for the markets.** Keep the runs per inning. The simulator already knows the score at
the end of every half-inning, so each simulated game records its runs per inning (about nine
numbers per team). Those numbers are stored next to the final scores in the 200-game summary the
server keeps for a minute, so a second request does not re-simulate. Nothing about how a game is
played changes. The betting endpoint then prices all fifteen markets with the existing pricing
code: each market's prices come from the stored sportsbook rows, or from the built-in mock
prices when none exist, as the three markets do today. The two markets that can end in a tie
(the first-inning and first-five moneylines) get one small new pricing helper, because a tie is
a third outcome. The betting card shows whatever markets the server returns, in a fixed order
with plain names, instead of knowing three markets by name.

**The fix for closing prices.** Rewrite the marking step. The moment our schedule check first
sees a game go live, it looks at each market and each book, finds the last price stored before
that moment, checks that the book's own timestamp is not after the scheduled start plus 15
minutes (the rule the backfill already uses), marks that row closing and recomputes its
fingerprint, so the morning backfill's identical row is recognised as a duplicate. The rule is
written so that running it a second time (after a restart) changes nothing. The existing
backfill is scheduled to run each morning over the previous day's games as a safety net; where a
book's own final snapshot differs from the last price we fetched, the backfill adds it, and
every reader uses the latest one. And in the last 15 minutes before a game we fetch prices every
minute instead of every ten, so the marked closing price is fresh.

**The definition of done.** The simulation cache carries each simulated game's runs by
inning. The edge endpoint returns an edge report for every one of the fifteen game markets.
The betting card on the game page shows them. The three full-game markets do not change.
Every live game's last price before first pitch becomes its closing row, for every market and
every book, and for every player prop and book. A unit test pins one closing row per
(market, book).

**Terms.** A *game market* is a bet on the game as a whole or on a slice of it. The *three
full-game markets* are the moneyline (who wins), the run line (the winner's margin against a
spread) and the total (the two teams' runs against a line). The *twelve segment and team
markets* settle on the first inning, the first five innings, one team's runs, or which team
scores first (the list is §1.1). A *three-way market* has three priced outcomes: home, away and
the tie; the first-inning and first-five moneylines are three-way. *De-vig* means removing the
book's margin from a set of prices so they sum to a probability of one. The *edge* of a bet is
the simulator's probability minus the de-vigged market probability. A *closing row* is the
last price a book posted before first pitch; the platform stores one per book and market. The
*simulation cache* is the Redis store (in-memory in the tests) that keeps one summary per
(game spec, seed, iteration count) for sixty seconds, so a second request for the same run
does not re-simulate.

**The two defects, in one line each.**

1. The cache stores three arrays per summary (home runs, away runs, total runs per iteration)
   and nothing per inning, so the edge route can price only the three full-game markets. The
   pricing code for the other twelve exists and is tested; it has no input on the request path.
2. The step that marks a game's closing rows has no caller in production, and it promotes ONE
   row per game across every market and book. So a live game never gets a closing row until the
   historical loader backfills it the next morning, and the loader is not scheduled either.

**The fix, in four parts.**

- **A. The inning grid on the result.** The loop writes each half-inning's runs onto the
  per-game result as it rolls the half. The summary keeps those cells per iteration (two short
  integer lists per game), so the cache carries them. No play stream is kept.
- **B. The twelve markets on the edge route.** The route builds the segment run totals from the
  cached grids once per request and prices each market through the existing pricing module.
  A two-way market goes through the existing report builder; the two three-way markets get a
  new builder with the three-way de-vig. Prices come from the stored rows (one book per row),
  else the mock, as the three full-game markets do today.
- **C. The betting card.** The card renders whatever markets the response carries, in the
  vocabulary's order, with display names the API supplies. No per-market branch in the card.
- **D. The closing rows.** A rewritten marker promotes, per (market, book), the last pre-pitch
  current row through the load guard's closing-stamp rule, and rewrites that row's dedup hash so
  the nightly loader's identical row deduplicates against it. The schedule poll calls it the
  first time it sees a game Live. A nightly closing pass of the historical loader over the
  previous day's games is the fallback and the reconciliation.

**What does not change.** The simulator's play, every draw, every weight. The three full-game
reports (their labels, sides, prices and the calibrated full-game win probability). The odds
tables' schema (no migration). The vocabulary of markets and books.

---

## 1. The two defects, with evidence

### 1.1 The markets

`pipeline/odds_provider.py` defines the fifteen `GAME_MARKET_TYPES`, each with a *kind*
(the column layout), a *segment* (the slice of the game) and a *side* (the team a team total
counts):

| Market type | Kind | Segment | Side | What settles it |
|---|---|---|---|---|
| `moneyline` | moneyline | game | — | the winner |
| `runline` | runline | game | — | the margin against a spread |
| `total` | total | game | — | the two teams' runs |
| `f1_moneyline` | three_way | f1 | — | who leads after one inning, or a tie |
| `f5_moneyline` | three_way | f5 | — | who leads after five innings, or a tie |
| `f1_total` | total | f1 | — | runs in the first inning |
| `f5_total` | total | f5 | — | runs in the first five innings |
| `f1_runline` | runline | f1 | — | the first-inning margin against a spread |
| `f5_runline` | runline | f5 | — | the first-five margin against a spread |
| `team_total_home` | total | game | home | the home team's runs |
| `team_total_away` | total | game | away | the away team's runs |
| `f5_team_total_home` | total | f5 | home | the home team's first-five runs |
| `f5_team_total_away` | total | f5 | away | the away team's first-five runs |
| `first_to_score` | moneyline | game | — | which team scores first |
| `first_inning_run` | yes_no | f1 | — | a run in the first inning (over / under 0.5) |

The odds tables hold every one of them (Alembic 0024). The historical loader loads all fifteen.
The live cycle persists all fifteen once a minute during a game and every ten minutes before it
(`_persist_game_odds_cycle`, `_persist_pregame_odds`). The mock provider serves all fifteen.
The accuracy comparison prices and grades all fifteen (`score_segment_market_accuracy`).

### 1.2 Defect 1: the cache has no innings

The runner (`BatchRunner.run`) fans N games out to workers. Each worker returns a
`GameSimResult`: the final score, the innings played, the terminal state, the box score. The
parent folds them into a `GameSimSummary` (`simulation/results.py`), which keeps three raw
per-iteration arrays: `home_scores`, `away_scores`, `total_scores`. The cache stores that
summary, pickled, for `SIM_RESULT_TTL_S` = 60 seconds. Nothing per inning survives the fold.

The edge route (`_build_edge_reports` in `api/routes/betting.py`) prices three markets from
those arrays: the moneyline from the calibrated win probability, the total from
`total_scores`, the run line from the margin `home_scores − away_scores`. The valid market
list is a module constant of three (`_VALID_MARKET_TYPES`), the stored-row SQL selects those
three and no `draw_ml` column, and the betting card lists three display names.

The pricing module (`simulation/game_market_distributions.py`) already answers every market
from a `SegmentRuns`: per iteration, each team's game runs, first-inning runs, first-five runs
and which team scored first. It has three constructors. `from_inning_grids` takes
`(home_cells, away_cells)` per iteration — one run count per inning, `None` for an unplayed
half — and its docstring names it "the light form a worker can pickle back to the parent".
The accuracy comparison builds exactly that form (`_replay_game`, `scripts/clv_backtest.py`):
it records each game's plays, derives the linescore, keeps the two cell lists and drops the
plays. The request path has no equivalent, because the runner never records plays.

### 1.3 Defect 2: the closing marker

`mark_closing_lines` (SIM-133) runs this statement:

```sql
WITH last_pre_pitch AS (
    SELECT id FROM raw.game_odds
    WHERE game_pk = $1 AND line_type = 'current' AND fetched_at <= $2
    ORDER BY fetched_at DESC
    LIMIT 1
)
UPDATE raw.game_odds SET line_type = 'closing' WHERE id IN (SELECT id FROM last_pre_pitch)
```

Three faults. (1) `LIMIT 1` promotes one row per game: one market, one book. Since the
one-row-per-book change (SIM-555, 2026-09-28) a game at first pitch holds about 180 current
game rows (fifteen markets, about twelve books). (2) No production code calls it: a repo-wide
search finds callers in two unit tests only (`tests/unit/test_live_pipeline_sim348.py`,
`tests/unit/test_data_engineer_sim340.py`). The technical reference records the gap
(`docs/technical/pipeline-betting-db.md`, the note under the live pipeline). (3) The promoted
row keeps its `odds_hash`, computed with `line_type = 'current'`. The hash is part of the
dedup index `(game_pk, source, odds_hash)`, and `line_type` is in the hash. When the
historical loader later writes the same book's closing row with the same prices, its hash
differs, so the table holds two closing rows for one (market, book). The prop marker
(`mark_closing_prop_lines`) promotes per (player, prop, book) with `DISTINCT ON`, so it has
faults (2) and (3) only.

What the readers see today. The edge route reads each book's closing row, and its current rows
while the game is still `Preview` (`_SQL_STORED_GAME_ODDS`, `_usable_line_types`). Once a game
turns Live it reads closing rows only. With no closing rows, every live game's edge page falls
to the mock until the loader runs. The line-movement and CLV pages read the closing row as the
end of a book's series; without it a live game's series has no end and no CLV.

The historical loader's closing pass (`scripts/load_historical_odds.py --line-types closing`)
writes correct rows, one per book, through the load guard. Nothing schedules it. The nightly
chain (`scripts/nightly_ingest.sh`, Ofelia at 07:00 UTC) refreshes games, rebuilds profiles
and rebuilds the artifact bundle; it has no odds step.

---

## 2. The mechanism today (what the fix plugs into)

**The request path.** `GET /api/betting/games/{pk}/edges` → `_summary_and_winprob` resolves
the game state, builds a `GameSpec`, runs `BatchRunner.run` (cache first) → `summary`,
`win_prob` → `_priced_edge_reports` reads the stored rows when a requested market has no
injected price → `_build_edge_reports` prices each requested market both sides →
`EdgesResponse`. `/signals` runs the same build and feeds every report to
`bet_signals_from_edges`, which ranks the positive-EV sides.

**The stored price source (SIM-555).** `_stored_rows_by_market` keeps, per market, each
sportsbook's latest usable row in the graded order (`GRADED_BOOK_PREFERENCE`: DraftKings,
BetMGM, FanDuel, ...). The first row is the *graded* row: its prices give the fair
probability and its line. Each side's *offered* price is the best sportsbook price at that
line. The response names both books. The three market tables that drive this are
`_STORED_SIDE_COLUMNS` (side → price column, line column), `_INJECTED_PARAMS` (market → its
query params) and the SQL's market list.

**The report builders (`betting/clv_engine.py`).** `_build_edge_report` assembles one report
from a label, a side, a line, the simulator's probability and a `TwoWayMarket` quote; it
de-vigs the two prices with `devig_two_way`. `total_over_under_edge_report` reads
`summary.total_scores`. `run_line_edge_report` and `run_line_bet_cover_prob` take a summary
OR a raw margin array (`_as_margin_array`), so a segment margin fits them unchanged. A run
line listed as two separate bets (home −1.5 and away −1.5, SIM-549) prices each bet from its
own price over a reference two-way margin (`devig_one_sided`, `one_sided_edge_report`).
`devig_multiway` de-vigs N prices by normalisation; the accuracy comparison already uses it
for the three-way markets. `MarketSide` has four values: home, away, over, under.

**The live pipeline.** `_sync_live_games` polls the schedule every 30 seconds
(`SCHEDULE_POLL_S`). A `Preview` game gets the pre-game odds cycle every ten minutes
(`PREGAME_ODDS_CADENCE_S` = 600). The first poll that sees a game `Live` logs "New live game
detected", starts its websocket watcher and refreshes its state; each refresh runs the game
and prop odds cycles once a minute (`PROP_FETCH_CADENCE_S` = 60). Every row written is
`line_type = 'current'`, one row per book, through `_guard_rows` (the load guard) and the
one INSERT with `ON CONFLICT (game_pk, source, odds_hash) DO NOTHING`.

**The loop.** `simulate_game` (`simulation/sim_loop.py`) drives `step_pitch` until the game
ends. It already detects a half-inning roll on every pitch (`rolled`), tracks the current
`(inning, half)` and the last played inning, and knows a walk-off. The runs live on
`state.home_score` and `state.away_score`.

---

## 3. Part A — the inning grid on the result and the summary

### A1. The loop writes the grid

`simulate_game` keeps two lists, `home_by_inning` and `away_by_inning`, and the score at the
start of the current half. On each roll it appends the batting side's runs in the half that
just ended (the score now minus the score at the half's start) to that side's list. When the
loop breaks on a walk-off, the bottom half in progress gets its runs too (the home team scored
in it). When the game ends after a top half (the home team led after the top of the ninth or
later), the home list is one cell short: the bottom half was never played. The lists are
padded to the same length with `None`, the scoreboard "x". A half that was played and scored
nothing is `0`, as `simulation/linescore.py` defines it.

The two lists go on the per-game result as two new optional fields:

```python
@dataclass
class GameSimResult:
    ...
    boxscore: BoxScore | None = None
    #: SIM-546: each team's runs per inning, None for an unplayed half (the
    #: scoreboard "x"). The segment markets are priced from these cells.
    home_by_inning: list[int | None] | None = None
    away_by_inning: list[int | None] | None = None
```

Why the loop and not a recorder wrapper. The play recorder (`simulation/play_recorder.py`)
keeps every `PlayResult` with a deep copy of its committed state — a few hundred copies per
game, built for the replay surface. The accuracy comparison pays that cost because it also
needs the box score of a recorded game; the request path runs the runner's `_run_one`, which
needs no plays. Reading two score fields on a roll the loop already detects costs nothing and
touches no draw. The outcomes stay byte-identical: the accumulator reads the state after
`step_pitch` and never calls the random stream (§7 pins this).

Why the score delta and not the plays' `runs_scored`. The linescore derivation attributes a
third-out play's runs to the half it ended (Rule 5.08 timing, SIM-512). The score delta over a
half gets the same answer without the play stream: the loop reads `home_score` and
`away_score` after the roll, and the roll happens after the runs are credited. §7's equality
test pins the two derivations against each other on recorded games.

### A2. The summary keeps the grids

`GameSimSummary.from_results` reads the two lists off each result into one new field, added
LAST (after the fields with defaults):

```python
@dataclass(slots=True)
class GameSimSummary:
    ...
    ci_method: str = "normal"
    #: SIM-546: (home_cells, away_cells) per iteration, in input order; the
    #: light form SegmentRuns.from_inning_grids reads. None when the results
    #: carried no grid (a result built by hand in a test).
    inning_grids: list[tuple[list[int | None], list[int | None]]] | None = None
```

The summary is pickled into Redis, so the grids ride in the cache for free. The cost: two
lists of nine to twelve small integers per iteration — about 40 bytes per game pickled, about
8 KB at n = 200, beside the three arrays of 1.6 KB each. Nothing is pickled that the worker
did not already return (the result crosses the process boundary with its terminal state and
box score, which dwarf the grid).

**Old cache entries.** A summary pickled before the change lacks the slot. A slotted dataclass
restores its state field by field with `zip`, so the new slot is unset and `summary.inning_grids`
raises `AttributeError`. Every reader uses `getattr(summary, "inning_grids", None)`. The cache
TTL is 60 seconds, so the window closes itself after the restart.

### A3. The stored summary and the lite projection

`_persist_replay_artifacts` (`api/routes/games.py`) stores `to_jsonable(batch.summary)` as a
JSONB summary per run, and `_sim_summary_lite_from_stored` rebuilds a `GameSimSummaryLite`
from it by dropping the three array keys. The lite model forbids unknown fields
(`_ApiModel`, `extra="forbid"`), so `inning_grids` joins the dropped keys. The wire models
(`GameSimSummaryModel`, `GameSimSummaryLite`) do not expose the grids: `/simulate` returns
what it returns today. A `/linescore` consumer that wants the simulated grid distribution is a
follow-on, not this ticket.

### A4. `SegmentRuns` from the summary

One helper, in `simulation/game_market_distributions.py`:

```python
def segment_runs_from_summary(summary: Any) -> SegmentRuns | None:
    """The SegmentRuns of a GameSimSummary, or None when it carries no grids."""
    grids = getattr(summary, "inning_grids", None)
    return None if not grids else SegmentRuns.from_inning_grids(grids)
```

The edge route calls it once per request when any segment market is requested. `SegmentRuns`
is pure numpy over N; building it at n = 200 is microseconds.

---

## 4. Part B — the twelve markets on the edge route

### B1. One market table, driven by the vocabulary

The three per-market tables in `api/routes/betting.py` become one, generated from
`GAME_MARKET_KIND`. The side columns per kind:

| Kind | Sides | Price columns | Line column |
|---|---|---|---|
| moneyline | home, away | `home_ml`, `away_ml` | — |
| three_way | home, away, draw | `home_ml`, `away_ml`, `draw_ml` | — |
| total, yes_no | over, under | `over_ml`, `under_ml` | `total_line` |
| runline | home, away | `home_spread_ml`, `away_spread_ml` | `home_spread`, `away_spread` |

`_VALID_MARKET_TYPES` becomes `GAME_MARKET_TYPES` for `/edges` and `/signals`. The
line-movement and CLV reads keep their own three-market vocabulary (`_MARKET_SIDES` in
`betting/line_movement.py`); widening them is a follow-on (§11). `_SQL_STORED_GAME_ODDS`
selects `draw_ml` and takes the full market list. `_stored_rows_by_market`'s usability rule
reads the kind's columns: every side's price present, the line present when the kind has one,
and for a three-way row the tie present. A three-way row with no tie is not usable (the
accuracy comparison applies the same rule: `INCOMPLETE_THREE_WAY_LAST_SQL`); the graded row
is the first book with a complete row, and with none the market falls to the mock.

`MarketSide` gains `DRAW = "draw"`. The signal builder reads `report.side` and never
enumerates the values, so a draw report flows through it. The API side field is already a
string.

### B2. The builders

Two existing builders take a raw array, so a segment market reuses them:

- a **run line** (`f1_runline`, `f5_runline`): `run_line_edge_report` and
  `run_line_bet_cover_prob` with `runs.segment_margin(segment)` in place of the summary. The
  pair / two-bets rule (`run_line_is_pair`) applies as on the full-game run line; a segment
  run line listed as two bets reads its reference margin from the graded book's own two-way
  markets in the order the accuracy comparison fixed (SIM-549): the segment's total, then the
  full-game total, then the moneyline, then the flat 1.05.
- a **total-kind market** (`f1_total`, `f5_total`, the four team totals, `first_inning_run`):
  `total_over_under_edge_report` gains a sibling that takes the samples array and a label —
  `samples_over_under_edge_report(samples, market, side=..., label=...)` — and the old
  function delegates to it with `summary.total_scores` and the label `"total"`. The samples
  are `runs.market_samples(market_type)`; `first_inning_run` is priced at line 0.5 whatever
  the row says (the pricing module already fixes that).
- a **two-way side market** (`first_to_score`): `side_probabilities` gives
  `(p_home, p_away, p_nobody)`; the report uses `p_home` or `p_away` with the two prices
  through `_build_edge_report`. The full-game moneyline keeps its calibrated
  `moneyline_edge_report`; the first-to-score probability is uncalibrated (no reliability map
  exists for it; §9).
- a **three-way market** (`f1_moneyline`, `f5_moneyline`): a new builder,

  ```python
  def three_way_edge_report(
      p_home: float, p_away: float, p_draw: float,
      *, label: str, side: MarketSide,
      home_ml: float, away_ml: float, draw_ml: float,
  ) -> EdgeReport
  ```

  The fair probability of the chosen side is its entry of
  `devig_multiway([implied(home_ml), implied(away_ml), implied(draw_ml)])`. The EV uses the
  side's own price (`expected_value`). The sim probability is `side_probabilities(runs, m)`
  for home, away or draw. No CLV (no closing quote on this path, as on every /edges report).
  A sim probability of exactly 0 or 1 raises `ValueError`, like every builder; the route's
  `_safe_report` skips that side and keeps the others.

### B3. Labels and keys

Every one of the twelve reports carries its market type as its label (`f5_total`,
`f1_moneyline`, ...), and `odds_source`, `fair_book`, `fair_book_name` key on the same string.
The three full-game markets keep their labels (`moneyline`, `total`, `run_line`) and the
run line's `runline` source key, so no existing consumer moves. The response gains one map:

```python
class EdgesResponse(BaseModel):
    ...
    #: SIM-546: label -> display name ("First five total"); the card reads it.
    market_names: dict[str, str] = Field(default_factory=dict)
```

The names come from one table in `pipeline/odds_provider.py` (`GAME_MARKET_NAMES`), beside
the vocabulary, so the loader's log lines and the API say the same words. `markets` on the
response lists the market types priced, in vocabulary order. The default request prices all
fifteen; `?markets=` still narrows.

### B4. Prices: stored, else the mock; no new injection

The route injects prices for the three full-game markets through named query params
(`home_ml`, `total_line`, ...). The twelve markets would need about forty more. The design
prices the twelve from the stored rows, else the mock, with `odds_source` reading `stored` or
`mock`; it adds no query params. The ticket's "when odds are supplied or found" is met by the
stored rows (found) and the mock (always); an injection seam for the twelve is decision 2
(§10). The mock already serves every market deterministically per game
(`MockOddsAPI.get_odds`: a tie price on a three-way market, an over / under pair at 0.5 on the
yes / no market, team-sized lines on the team totals).

### B5. A summary without grids

When a requested segment market has no `SegmentRuns` (a cache entry from before the deploy,
or a test summary built by hand), the route logs one line, prices the full-game markets, and
leaves the twelve out: `markets` lists what was priced and `odds_source` has no key for them.
No 500 and no partial report.

### B6. The signals endpoint

`/signals` changes only through the shared build: every report, including a draw side, goes to
`bet_signals_from_edges`. The Kelly fraction reads one side's probability and price, so it is
the same arithmetic on a three-way side. The ranked list can now name a first-five total or
a tie; the response's `market_names` lets the card label it.

---

## 5. Part C — the betting card

`BettingCard.tsx` already groups reports by label and renders unknown labels after the three
it knows. Three changes make it data-driven:

1. **Order and names from the response.** The card orders sections by `edges.markets` (the
   vocabulary order) and titles them from `edges.market_names`, with its old three names as
   the fallback. The `MARKET_ORDER` and `MARKET_LABELS` constants go.
2. **Side labels by kind.** `sideLabel` reads the side and the line as today, with three
   additions: `draw` shows "Tie"; the yes / no market's `over` / `under` at 0.5 show "Yes" /
   "No" (the label `first_inning_run` tells it); a team total's "Over 4.5" reads as today. The
   source badge and the fair-book line read the market's own key (the run line's `runline`
   alias stays in `SOURCE_KEY`).
3. **The three-way note.** A section whose reports carry a `draw` side shows one line: "A tie
   is a priced outcome; the three fair prices add to one." The two-bets note on the run line
   extends to the segment run lines through a new response map,
   `run_line_pricing_by_label: {label: RunLinePricingModel}`, which carries all three run
   lines; the existing `run_line_pricing` field keeps its shape for the full-game run line.
   The TypeScript types in `frontend/src/api/betting.ts` follow.

Fifteen sections are long. The card groups them under four sub-headings in order — Full game,
First five innings, First inning, Team totals — read from the segment and side of each label
through one small table in the card (`SEGMENT_OF`, generated from the same vocabulary the API
serves in `market_names`; the grouping is presentation, so it lives in the card). The CSS
module gains one rule for the sub-heading. The Playwright smoke (`frontend/e2e/smoke.spec.ts`)
gains one check: with the mock provider the card renders fifteen market sections and a "Tie"
side.

---

## 6. Part D — every live game's closing prices

### D1. The rule

For one game at first pitch, per (market type, book): take the latest row by `fetched_at`
among the rows with `line_type` in (`current`, `closing`) fetched at or before the first-pitch
instant. If that row is `current`, and the load guard's closing-stamp rule keeps it (its
`book_line_at` is no later than the scheduled start plus 15 minutes, `CLOSING_STAMP_GRACE`;
a row with no stamp passes), promote it: set `line_type = 'closing'` and set `odds_hash` to
the hash of the row with `line_type = 'closing'`. If a row with that hash already exists for
the (game, source), leave the row as `current`: the closing row is already there (the loader
got to it first). For props the key is (player, prop stat, book) and the prop hash.

Why "among current OR closing" and not "among current": idempotence. The first call promotes
the latest current row; a second call (the pipeline restarted mid-game and detected the game
Live again) must find that promoted row as the latest and do nothing. A rule that scans
current rows only would promote the next-latest current row on the second call and leave two
closing rows.

Why rewrite the hash: fault (3) of §1.3. The loader's closing row for the same book and prices
hashes with `line_type = 'closing'`; the promoted row must carry that hash so the loader's
INSERT deduplicates against it.

### D2. The code

A module-level async function beside the writers in `pipeline/live/live_ingestion_pipeline.py`,
with pure helpers a unit test runs without a database:

```python
def closing_candidates(rows, *, scheduled_start) -> list[tuple[int, str]]:
    """PURE: (row id, new odds_hash) of every row to promote. ``rows`` are the
    latest pre-pitch row per (market_type, book), current or closing; a row is
    picked when it is current and check_row(..line_type='closing'..) keeps it."""

async def promote_closing_game_rows(conn, game_pk, first_pitch_at, *, scheduled_start) -> int
async def promote_closing_prop_rows(conn, game_pk, first_pitch_at, *, scheduled_start) -> int
```

The game read:

```sql
SELECT DISTINCT ON (market_type, book)
       id, source, line_type, book, market_type, is_sharp_book, book_line_at,
       home_ml, away_ml, draw_ml, home_spread, home_spread_ml,
       away_spread, away_spread_ml, total_line, over_ml, under_ml
FROM raw.game_odds
WHERE game_pk = $1 AND line_type IN ('current', 'closing') AND fetched_at <= $2
ORDER BY market_type, book, fetched_at DESC
```

The helper runs `check_row` on each current row with `line_type` set to `closing` and the
scheduled start attached, computes the new hash with `LiveIngestionPipeline._odds_hash`
(`_prop_odds_hash` for props), drops any whose hash already exists (one `SELECT odds_hash ...
WHERE game_pk = $1 AND source = $2 AND odds_hash = ANY($3)`), and writes the rest in one
statement:

```sql
UPDATE raw.game_odds AS g
SET    line_type = 'closing', odds_hash = v.odds_hash
FROM   unnest($1::bigint[], $2::text[]) AS v(id, odds_hash)
WHERE  g.id = v.id AND g.line_type = 'current'
```

The `g.line_type = 'current'` guard makes a concurrent second call harmless. The method
returns the rows promoted. `mark_closing_lines` and `mark_closing_prop_lines` become thin
wrappers over the two functions (the tests that pin their SQL move to the new rule, §7), and
the old `LIMIT 1` statement is deleted. About 180 game rows and up to about 3,600 prop rows
per game pass through the helper: one read, one existence check and one update per table.

### D3. The trigger

In `_sync_live_games`, the branch that logs "New live game detected" awaits the promotion
before it starts the watcher:

```python
if status == "Live":
    current_live.add(game_pk)
    if game_pk not in self._ws_clients:
        log.info("New live game detected: %s", game_pk)
        await self._mark_closing_rows(game_pk, game)   # SIM-546
        await self._start_watching(game_pk)
        ...
```

`_mark_closing_rows` takes the first-pitch instant as the poll's own time (UTC now) and the
scheduled start from the schedule entry's `gameDate` (ISO 8601 UTC), calls the two
functions, logs the two counts and never raises (a failed promotion is logged; the nightly
pass repairs it). The poll runs every 30 seconds, so the instant is at most 30 seconds after
MLB flipped the status, and the live cycle's first `current` row is written after the
promotion by construction (the refresh task starts after `_start_watching`). A restart
mid-game fires the branch again; D1's rule makes that a no-op.

### D4. The stamp and a delayed start

The vendor stamps a snapshot at game time, 0 to 5 minutes after the scheduled start (the
SIM-555 record). A pre-pitch `current` row fetched at T−1 minute carries a stamp before the
start and passes. When a game starts late (weather), MLB flips the status at the real first
pitch; rows fetched during the delay are pre-pitch by `fetched_at`, but a row stamped more
than 15 minutes after the scheduled start fails the stamp rule, so the promoted row is the
latest one stamped inside the grace. The historical loader applies the same rule to the same
game, so the two paths agree. A delayed game's closing row can therefore be a line from
before the delay. That is the platform's standing definition of a closing line (the guard's,
2026-09-28), not a new rule; it is recorded in §9 as a known limit.

### D5. The pre-game cadence near first pitch

The pre-game cycle reads the vendor every ten minutes, so the promoted row can be up to ten
minutes old at first pitch. Closing lines move most in the last minutes. The design tightens
the pre-game cadence to 60 seconds (`PROP_FETCH_CADENCE_S`, the live cadence) once the
scheduled start is within 15 minutes: `_persist_pregame_odds` reads the entry's `gameDate` and
picks the cadence by the time to start. The extra vendor cost is bounded: 15 minutes × 15
market calls per minute per game, under the provider's offers cache — the same rate the live
cycle already runs for three hours per game. This is decision 3 (§10).

### D6. The nightly fallback

A second Ofelia job in `deploy/ofelia/config.ini`, after the ingest chain, runs the loader's
closing pass over yesterday's games:

```ini
[job-run "nightly-closing-lines"]
schedule = 0 30 9 * * *
image = baseball_simulator_v2-app
network = baseball_simulator_v2_baseball_net
environment = BASEBALL_DB_DSN=postgresql://baseball_user:baseball_pass@db:5432/baseball_sim
command = sh /app/scripts/nightly_closing_lines.sh
```

`scripts/nightly_closing_lines.sh` runs
`python scripts/load_historical_odds.py --seasons YEAR --line-types closing --game-dates D-1 D-2`
when `ODDS_PROVIDER` is `bettingpros`, and exits 0 with one log line otherwise (the mock
provider must never write mock closing rows into a real store). The loader gains
`--game-dates` (one or more `YYYY-MM-DD`; `_fetch_final_games` adds `AND game_date = ANY(...)`).
Two dates cover a late West-coast game that turns Final after midnight UTC. A separate job
rather than a step in `nightly_ingest.sh`: a vendor outage must not stop the profile and
artifact rebuild, and the chain's `set -e` would.

What the pass does to a game the live marker already handled: the loader's row for a book
whose last pre-pitch price matches the promoted row deduplicates (the hash rewrite, D1). A
book whose vendor closing snapshot differs from our last fetch gets a second closing row with
the later `fetched_at`; every reader takes the latest fetch per (market, book), so the
vendor's own close wins where the two differ. The pass is therefore both the fallback (a game
the live pipeline missed: a restart, a crash, a failed promotion) and the reconciliation.

### D7. The readers

No reader changes for the closing rows. The edge route sorts a closing row before a current
row per (market, book) and reads current rows only while the game is `Preview`; a live game
now has closing rows the instant it starts. The line-movement series ends at the closing
row and the CLV page prices it; the accuracy comparison's graded-row read is unchanged.

---

## 7. Tests

**Part A (`tests/unit/test_sim546_inning_grid.py`).**

1. `test_grid_matches_the_linescore_on_recorded_games`: ten synthetic-bundle games at fixed
   seeds through `record_game_plays`; each result's `home_by_inning` / `away_by_inning` equal
   `linescore_from_plays(plays).home_by_inning` / `away_by_inning` cell for cell, `None`
   included.
2. `test_grid_sums_to_the_final_score`: every game of (1); the padded lengths are equal.
3. `test_walk_off_and_unplayed_bottom`: a game ended by a walk-off has its last home cell
   set; a game won by the home team after the top of the ninth has `None` there.
4. `test_outcomes_unchanged`: the scores and seeds of the ten games equal those of the same
   seeds at commit 80e3f46 (ten literal pairs in the test; the accumulator touches no draw).
5. `test_summary_carries_grids_and_tolerates_their_absence`: `from_results` fills
   `inning_grids`; a result with `None` grids yields `None`; `segment_runs_from_summary` on a
   hand-built summary without the attribute returns `None`.
6. `test_lite_projection_drops_the_grids`: `_sim_summary_lite_from_stored` on a stored dict
   with `inning_grids` validates.

**Part B (`tests/unit/test_sim546_segment_edges.py`, the `_FakePool` idiom of
`test_api_betting_sim36x.py`).**

7. `test_edges_default_prices_fifteen_markets_from_the_mock`: fifteen keys in `odds_source`,
   two reports per two-way market, three per three-way, labels equal the market types,
   `market_names` has fifteen entries.
8. `test_three_way_fair_probabilities_sum_to_one`: the three `market_fair_prob` of
   `f5_moneyline` sum to one within 1e-9; each side's EV uses its own price.
9. `test_stored_rows_price_the_segment_markets`: canned one-book rows for a first-five total
   and a first-inning moneyline with a tie; `odds_source` reads `stored`, the fair book is the
   graded book, the offered price is the best book's.
10. `test_three_way_row_without_a_tie_is_not_usable`: a DraftKings row with no `draw_ml` and a
    BetMGM row with one → BetMGM is the fair book; with no complete row the market reads `mock`.
11. `test_segment_run_line_two_bets_reads_the_segment_total_margin`: a first-five run line
    listed as home −1.5 / away −1.5 with a stored first-five total → `run_line_pricing_by_label["f5_runline"]`
    has shape `two_bets` and source `f5_total`.
12. `test_summary_without_grids_skips_the_segment_markets`: a summary stub without
    `inning_grids` → three markets priced, `markets` lists three, no 500.
13. `test_signals_rank_a_draw_side`: a draw report with positive EV appears in `/signals` with
    `side == "draw"`.
14. `test_legacy_markets_unchanged`: the existing `test_api_betting_sim36x.py` cases pass
    untouched; `?markets=moneyline,total` returns exactly those.

**Part D (`tests/unit/test_sim546_closing_rows.py`, the AsyncMock idiom of
`test_live_pipeline_sim348.py`).**

15. `test_one_closing_row_per_market_and_book`: twelve books × fifteen markets of current
    rows at three fetch times → the candidates are the latest row per (market, book), 180 ids,
    each with a hash equal to `_odds_hash` of the row with `line_type='closing'`.
16. `test_second_call_promotes_nothing`: the rows of (15) with the latest row of each key
    already `closing` → zero candidates.
17. `test_late_stamp_is_refused`: a row stamped 20 minutes after the scheduled start is not
    promoted; its predecessor is not promoted either (only the latest row is a candidate).
18. `test_existing_loader_row_blocks_the_rewrite`: the existence check returns one of the
    new hashes → that id is left out of the update.
19. `test_props_promote_per_player_prop_and_book`: the prop analogue of (15) with
    `_prop_odds_hash`.
20. `test_sync_live_games_marks_on_the_preview_to_live_turn`: a schedule stub with one game
    `Live` and no watcher → the marker is awaited once, before `_start_watching`; the same
    poll again (the watcher exists) → not called.
21. `test_pregame_cadence_tightens_near_the_start` (decision 3): a game 10 minutes from its
    start reads the vendor at 60-second spacing; one 2 hours out at 600.
22. `test_nightly_script_skips_the_mock_provider` and `test_loader_game_dates_filter`: the
    loader's `_fetch_final_games` SQL carries the date filter; the shell script exits 0 and
    writes nothing when `ODDS_PROVIDER` is unset.

**The lanes.** The unit and regression lanes. No acceptance lane: no channel of the simulator
moves (test 4 is the proof). The ten-game smoke (`scripts/sim_stats.py`) is not needed for the
same reason; it runs anyway in the run book as the cheap gate the owner's rule names.

**The frontend.** `vitest` for the card: fifteen sections in order with the mock response, the
"Tie" side, the "Yes" / "No" labels, the three-way note. The Playwright smoke check of §5.

---

## 8. Build order and run book

Build in the order the dependencies run; each step leaves the suite green.

1. **Part A** — `sim_loop.py` (the accumulator, about 20 lines), `results.py`,
   `game_market_distributions.py` (the helper), `api/routes/games.py` (the lite projection),
   tests 1–6. Then `make test-unit`, `make test-regression`, `scripts/sim_stats.py` on ten
   games (the credited-outs gate it carries).
2. **Part B** — `clv_engine.py` (`MarketSide.DRAW`, the samples builder, the three-way
   builder), `odds_provider.py` (`GAME_MARKET_NAMES`), `api/routes/betting.py` (the one market
   table, the SQL, the builder dispatch, the response fields), tests 7–14.
3. **Part C** — the card, the client types, the vitest cases, the smoke check. `npm run
   build`, `npm test`, the e2e smoke.
4. **Part D** — the two functions and helpers, the wrappers, the trigger, the cadence
   (decision 3), the loader's `--game-dates`, the shell script, the Ofelia job, tests 15–22.
5. **Docs** — `docs/technical/api.md` (the betting route rows), `docs/technical/pipeline-betting-db.md`
   (the marker row and its gotcha note, now closed), `docs/technical/scripts-frontend.md`
   (the loader's option and the nightly script), `CHANGES.md`, this document's build record.

**Deploy (the host's gotchas, §2a of `CLAUDE.md`).** `betting/` and `scripts/` are not
bind-mounted and the app hot-reloads `api/`: rebuild the image before the `api/` edit lands in
production (`docker compose build app`, then `docker compose up -d app`), then
`docker compose restart app` for the mounted edits. The scheduler service runs under the
`scheduler` profile; the new job needs `docker compose --profile scheduler up -d scheduler`
after the config edit. The frontend is built into the image.

**First live check.** On the first game day after the deploy: `GET /api/betting/games/{pk}/edges`
on a `Preview` game reads fifteen markets with `stored` sources; the app log at first pitch
shows "closing rows promoted: game N, K game rows, M prop rows"; the same `/edges` call after
first pitch reads closing rows; the next morning's job log shows the loader's refusals by rule
and the rows deduplicated. One SQL check per game: `SELECT market_type, book, COUNT(*) FROM
raw.game_odds WHERE game_pk = N AND line_type = 'closing' GROUP BY 1, 2 HAVING COUNT(*) > 1`
lists the (market, book) pairs where the vendor's close differed from the promoted row; it
should be short and explainable (D6).

---

## 9. What could go wrong (ranked)

1. **A segment moneyline reads uncalibrated.** The full-game moneyline goes through the fitted
   reliability curve; the first-five and first-inning moneylines, the first-to-score market
   and every total read the raw simulated frequency. The accuracy comparison of 2026-09
   measured the simulator behind the closing line on every segment market (a run in the first
   inning +0.0076 Brier, the first-five moneyline +0.0073, the first-five total +0.0069). The
   card shows a `stored` badge and an edge figure on markets the model has not been shown to
   beat. **Mitigation:** none in this ticket; the per-market calibration layer is the
   accuracy fit's plan (SIM-548, §7 of plan v2). The card's note on a three-way section is a
   presentation fact, not a confidence claim. The owner should read these twelve edges as
   model output, not as bets (the bet-signal gate is unchanged and still fires on them: §10,
   decision 4).
2. **Two closing rows for one (market, book).** The vendor's game-time snapshot can differ
   from our last pre-pitch fetch; the nightly pass then adds a second closing row (D6). The
   readers take the latest fetch, so the result is consistent, but a count check reads > 1.
   **Mitigation:** the near-start cadence (D5) narrows the gap to a minute; the §8 SQL check
   reports the pairs; a reconciliation that archives the earlier of two closing rows is a
   follow-on if the count is material.
3. **A delayed game's closing row predates the delay** (D4). The guard's definition of a
   closing line binds both paths. **Mitigation:** recorded here; a per-game real first pitch
   (the feed's first play `startTime`) as the stamp reference is a follow-on for the guard.
4. **An old cache entry after the deploy.** For up to 60 seconds a request can hit a summary
   without grids; the route prices three markets and says so (B5). No action.
5. **The hash rewrite collides with a concurrent loader insert.** The existence check and the
   update are two statements. The loader runs at 09:30 UTC and the marker at first pitch
   (afternoon and evening UTC), so they never overlap on one game; a manual loader run during
   a slate could. **Mitigation:** the update's `WHERE line_type = 'current'` and the unique
   index turn a collision into one failed statement, logged, with the loader's row standing.
6. **The mock environment promotes `consensus` rows.** The mock provider labels its rows
   `consensus`; the marker promotes them like any book. The readers filter to `bp:` rows, so a
   mock closing row reaches no page. The nightly script refuses to run on the mock (D6).
7. **The edge page's cost.** Fifteen markets price in microseconds from arrays already in
   memory; the stored-row read widens from three market types to fifteen on an indexed
   `(game_pk, market_type, line_type, book)` read (migration 0028). No measurable change to a
   request that runs a 200-iteration simulation first.

---

## 10. Decisions for the owner

1. **The grid comes from the loop, not a recorder** (A1). Recommended: the loop. The
   alternative keeps `sim_loop.py` untouched and wraps each worker's machine in a lighter
   recorder; it adds an attribute-forwarding layer on every pitch for no gain.
2. **No injected prices for the twelve markets** (B4). Recommended: stored, else the mock.
   The alternative is one JSON query param (`segment_prices`) carrying a market → prices map,
   validated against the vocabulary. It is cheap to add later; nothing on the game page sends
   injected prices today.
3. **The pre-game cadence tightens to 60 seconds inside 15 minutes of the start** (D5).
   Recommended: yes. Without it the promoted closing row is up to ten minutes stale, and the
   nightly pass then writes a second closing row on most books (§9, item 2).
4. **The bet-signal gate fires on the twelve markets** (B6). Recommended: yes, as the ticket's
   definition of done implies ("an edge report for every one of the fifteen"), with the §9
   item 1 caveat shown to the owner here, not on the page. The alternative restricts
   `/signals` to the three full-game markets until the calibration layer lands.
5. **The nightly closing pass is its own scheduler job, not a step of the ingest chain** (D6).
   Recommended: its own job at 09:30 UTC. A vendor outage then costs one morning's closing
   rows, not the profile and artifact rebuild.

---

## 11. Out of scope, filed as follow-ons when the owner asks

- **The line-movement and CLV pages for the twelve markets.** `betting/line_movement.py`
  knows three markets. Widening its side table to the vocabulary is mechanical; the three-way
  CLV (a tie leg) needs a rule the engine does not have. The closing rows this ticket writes
  make that work possible.
- **The simulated inning grid on the wire.** A `/linescore` distribution per inning from the
  cached grids (the first-inning run rate, the first-five lead share) for the game page.
- **Reconciling two closing rows per (market, book)** when the count of §8 is material.
- **The real first pitch as the stamp reference** for a delayed game (§9, item 3), in the
  guard, so both the live marker and the loader move together.

---

## 12. Files touched

| File | Change |
|---|---|
| `simulation/sim_loop.py` | the per-half accumulator in `simulate_game`; two optional fields on `GameSimResult` |
| `simulation/results.py` | `GameSimSummary.inning_grids`, filled in `from_results` |
| `simulation/game_market_distributions.py` | `segment_runs_from_summary` |
| `betting/clv_engine.py` | `MarketSide.DRAW`; `samples_over_under_edge_report`; `three_way_edge_report` |
| `pipeline/odds_provider.py` | `GAME_MARKET_NAMES` |
| `api/routes/betting.py` | one vocabulary-driven market table; the SQL with `draw_ml` and all fifteen types; the builder dispatch; `market_names`; `run_line_pricing_by_label` |
| `api/routes/games.py` | the lite projection drops `inning_grids` |
| `api/schemas.py` | no change (the side is a string; the grids stay off the wire) |
| `pipeline/live/live_ingestion_pipeline.py` | `closing_candidates`, `promote_closing_game_rows`, `promote_closing_prop_rows`; the two markers as wrappers; the trigger in `_sync_live_games`; the near-start cadence |
| `scripts/load_historical_odds.py` | `--game-dates` |
| `scripts/nightly_closing_lines.sh` | new: the nightly closing pass, provider-gated |
| `deploy/ofelia/config.ini` | the second job |
| `frontend/src/components/games/BettingCard.tsx`, `BettingCard.module.css`, `frontend/src/api/betting.ts` | the data-driven card and its types |
| `frontend/e2e/smoke.spec.ts` | the fifteen-section check |
| `tests/unit/test_sim546_*.py` (three files) | tests 1–22 |
| `tests/unit/test_live_pipeline_sim348.py`, `tests/unit/test_data_engineer_sim340.py` | the marker cases move to the new rule |
| `docs/technical/api.md`, `pipeline-betting-db.md`, `scripts-frontend.md`, `CHANGES.md` | the records |
