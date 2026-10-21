# Betting

The betting layer compares the simulator's probabilities with the market's prices. Its
input is a probability from the simulator (a win probability, a player's prop
distribution, or the per-game scores) and a price from a sportsbook. Its output is an
edge report per side of a market, a ranked list of bet signals, and the line-movement
series of a market from the opening price to the close. The API's betting routes and the
accuracy-comparison tool call it.

The package is plain arithmetic. It draws no random numbers and fetches no odds: the
caller passes the prices in. One function, `fetch_line_movement`, reads the database
through a connection the caller supplies.

## The terms

The whole package rests on six numbers. Each module uses them with these meanings.

| Term | Meaning |
|---|---|
| **American odds** | The book's price. `-110` means you stake 110 to win 100. `+150` means you stake 100 to win 150. |
| **Decimal odds** | The total return per unit staked. `+150` is 2.50; `-110` is about 1.909. |
| **Implied probability** | The probability a price stands for, with the book's margin still in it. `-110` implies 0.5238. `+150` implies 0.40. |
| **Vig (the margin)** | The book's cut. The implied probabilities of a market's sides add up to more than 1. The excess (about 5% on a typical two-way market) is the vig, also called the overround. |
| **No-vig (fair) probability** | The implied probability with the vig removed, so the sides add up to exactly 1. Removing it is the "de-vig". |
| **Edge** | The simulator's probability minus the no-vig probability. A positive edge means the simulator rates the side more likely than the market does. |
| **Expected value (EV)** | The profit per unit staked if the simulator's probability is true: `EV = p × (decimal − 1) − (1 − p)`, at the OFFERED price, vig included. |
| **Closing line value (CLV)** | How the price a bet took compares with the closing price, the market's last price before first pitch: `clv_prob = p_close_fair − p_entry_fair`. A positive CLV means the entry beat the close. |

The edge reads the no-vig probability. The EV reads the offered price, because that is
the price a bet takes. Mixing the two up counts the vig twice or not at all, and the
report still looks plausible.

## How it fits together

The request path for a game's edges and signals:

1. A user opens the game page's Betting card. The frontend calls
   `GET /api/betting/games/{game_pk}/edges` and `.../signals`
   ([betting.ts](../../frontend/src/api/betting.ts)).
2. The route (`api/routes/betting.py`) runs or reuses a Monte-Carlo simulation of the game
   and gets a `GameSimSummary`.
3. `api/routes/betting_pricing.py` finds each market's prices: injected query
   parameters first, then the stored lines (`api/routes/betting_stored_odds.py`), then the
   mock odds provider.
4. For each side of each market, the pricing code calls one report builder in
   `betting/clv_engine.py`: `moneyline_edge_report`, `total_over_under_edge_report`,
   `run_line_edge_report` or `one_sided_edge_report` for the three full-game markets;
   `samples_over_under_edge_report` and `three_way_edge_report` for the twelve segment
   and team markets.
5. Each builder de-vigs the market (`devig_two_way` or `devig_multiway`), then computes
   the edge, the EV and, when a closing quote is present, the CLV. It returns an
   `EdgeReport`.
6. `/edges` returns the reports. `/signals` passes them to
   `betting/bet_signal.py` `bet_signals_from_edges`, which gates, stakes and ranks them.

A second path reads the stored odds history:

1. `GET /api/betting/games/{game_pk}/line-movement` and `.../clv`
   (`api/routes/betting_lines.py`) call `betting/line_movement.py` `fetch_line_movement`.
2. `fetch_line_movement` reads `raw.game_odds` for one game and market, groups the rows
   per side and book, and calls the pure builder `line_movement_from_quotes`.
3. The builder orders the quotes in time and derives the series, the opening-to-closing
   summary and the CLV of a bet at the opening price.

A third path is offline: the accuracy comparison (`scripts/accuracy/`) grades the
simulator against the closing lines of past games through the same `clv_engine`
functions.

## The modules

### `betting/__init__.py`

[Source](../../betting/__init__.py) · 68 lines · Tests: [test_api_schemas.py](../../tests/unit/test_api_schemas.py)

The package docstring glosses the terms above. The package re-exports a subset of
`betting.clv_engine`: the conversions, the two de-vigs, `edge`, `expected_value`, the CLV
builders, the report types and the moneyline, prop and total builders. It does not
re-export the run-line, segment-market or one-sided functions, nor `bet_signal` or
`line_movement`.

| Name | What it does | Called from |
|---|---|---|
| `MarketSide`, `OddsQuote`, `TwoWayMarket`, `prop_edge_report` | Re-exported from `clv_engine`. | `api/routes/games.py` (the prop panel's over / under edge) |

**Watch out:** a new `clv_engine` function does not appear at package level until you add
it to this file's import list and `__all__`.

### `betting/clv_engine.py`

[Source](../../betting/clv_engine.py) · 881 lines · Tests: [test_clv_engine.py](../../tests/unit/test_clv_engine.py), [test_clv_engine_run_line.py](../../tests/unit/test_clv_engine_run_line.py), [test_segment_edges.py](../../tests/unit/test_segment_edges.py), [test_live_run_lines.py](../../tests/unit/test_live_run_lines.py)

The pricing arithmetic. It converts prices, removes the vig, computes the edge, the EV and
the CLV, and builds one `EdgeReport` per side of a market from a simulator output.

**How the vig comes off.** The engine uses the proportional method. A market's raw
implied probabilities `q_i` add up to `1 + overround`. Each no-vig probability is
`p_i = q_i / Σ q_j`, so each side gives up the margin in proportion to its own implied
probability. A two-way market (moneyline, total, a run-line pair, a prop's over / under)
and a three-way market (the first-inning and first-five moneylines, where a tie is the
third outcome) use the same rule.

**A run line is a pair, or two separate bets (the run lines scored by shape, SIM-549).**
`raw.game_odds` keeps each team's run line at its own spread. The two prices are one bet
with two sides only when the away spread is the negative of the home spread (home −1.5 /
away +1.5). Otherwise the book listed two separate bets: home −1.5 and away −1.5 both lose
on a one-run game. Two separate bets must never de-vig against each other. Each one
prices from its own implied probability over the book's margin on another two-way market
of the same game. The caller orders the reference markets: the segment's own total, then
the full-game total, then the moneyline. When none has a margin inside
`REFERENCE_MARGIN_BAND` (1.00 to 1.15), the flat `DEFAULT_ONE_SIDED_MARGIN` (1.05)
applies. A three-way market is never a reference, because its margin also prices the tie.

**The push.** Baseball scores are whole numbers. On a half-integer line (−1.5, 8.5) no
game lands on the line, so the two sides add up to 1. On a whole-number line a game on
the line is a push and counts for neither side. Every builder uses strict inequalities
for this reason.

| Function / class | What it does | Called from |
|---|---|---|
| `american_to_decimal`, `decimal_to_american`, `implied_prob_from_american` (alias `american_to_implied_prob`), `prob_to_american` (alias `fair_american_from_prob`) | Convert between American odds, decimal odds and probability. A price of 0 or a probability outside (0, 1) raises `ValueError`. | every builder; `api/routes/betting_stored_odds.py`; `scripts/accuracy/pricing.py`, `hypothetical_return.py` |
| `devig_multiway(implied_probs)` | The proportional de-vig of N ≥ 2 outcomes. Returns no-vig probabilities that add up to 1. | `devig_two_way`, `three_way_edge_report`, `scripts/accuracy/grading.py` |
| `devig_two_way(a_american, b_american)` | The de-vig of a two-way market from its two prices. | `_build_edge_report`, `clv_from_odds`, `scripts/accuracy/grading.py` |
| `edge(p_sim, p_fair)` | `p_sim − p_fair`. | every builder |
| `expected_value(p_sim, offered_american)` | The EV per unit staked at the offered price. | every builder; `scripts/accuracy/hypothetical_return.py` |
| `CLV`, `clv_from_prob`, `clv_from_odds` | The CLV of one side. `clv_from_odds` de-vigs the entry pair and the closing pair, then compares the two no-vig probabilities. `CLV.beat_close` is `clv_prob > 0`. | `_build_edge_report`; `betting/line_movement.py` |
| `MarketSide` | HOME, AWAY, OVER, UNDER, DRAW (the tie of a three-way market). | every builder; `api/routes/betting*.py`; `scripts/accuracy/` |
| `OddsQuote`, `TwoWayMarket` | One snapshot of a two-way market (`side`, `other`, `line`), and an entry quote with an optional closing quote. | `api/routes/betting_pricing.py`, `api/routes/games.py`, `scripts/accuracy/grading.py` |
| `EdgeReport` | One side of one market: the simulator's probability, the no-vig probability, the edge, the EV, the offered price, the simulator's fair price and the optional CLV. `positive_edge` is a strict `edge > 0`. | `betting/bet_signal.py`; the API's response models |
| `moneyline_edge_report(win_prob, market, side)` | The moneyline side, from a `WinProbability`. | `api/routes/betting_pricing.py`, `scripts/accuracy/grading.py` |
| `prop_edge_report(dist, market, side)` | A player prop's over or under, from a `PropDistribution` at the book's line. | `api/routes/games.py`, `scripts/accuracy/grading.py` |
| `total_over_under_edge_report(summary, market, side)` | The full-game total, from the summary's per-game total runs. It delegates to `samples_over_under_edge_report` with the label `total`. | `api/routes/betting_pricing.py` |
| `samples_over_under_edge_report(samples, market, side, label)` | Any over / under market from one number per simulated game: first-five runs, one team's runs, first-inning runs. | `api/routes/betting_pricing.py` (the segment totals, team totals and "a run in the first inning") |
| `three_way_edge_report(p_home, p_away, p_draw, ...)` | One side of a three-way moneyline. The three prices de-vig together. No CLV. | `api/routes/betting_pricing.py` |
| `spread_cover_prob(margins, line, side)` | The simulator's cover probability of a run-line side. `line` is always the HOME line; the away side covers when `home − away < −line`. | `run_line_edge_report`, `run_line_bet_cover_prob` |
| `run_line_edge_report(margins, market, side, line)` | A run-line side that is one half of a pair. | `api/routes/betting_pricing.py` |
| `run_line_is_pair(home_spread, away_spread)` | True when the away spread is the negative of the home spread (0 / 0 included). | `betting/line_movement.py`, `scripts/accuracy/grading.py`, `scripts/probes/rescore_runlines.py` |
| `reference_margin_from_prices(candidates)` | The first candidate market whose two implied probabilities add up to a margin inside the band; else `(1.05, "flat")`. | `betting/line_movement.py`, `api/routes/betting_pricing.py`, `api/routes/betting_stored_odds.py`, `scripts/accuracy/pricing.py` |
| `devig_one_sided(american, margin)` | One separate bet's fair probability: its implied probability over the margin, capped at 1. | `betting/line_movement.py`, `api/routes/betting_pricing.py`, `scripts/accuracy/grading.py` |
| `run_line_bet_cover_prob(margins, side, own_line)` | The cover probability of one bet at its OWN spread (an away bet at −1.5 covers when the away team wins by 2 or more). | `scripts/accuracy/grading.py` |
| `one_sided_edge_report(...)` | The report of one separate bet: the edge against `devig_one_sided`'s fair probability, the EV at the offered price, no CLV. | `api/routes/betting_pricing.py` |

**Watch out:** every builder raises `ValueError` on a simulator probability of exactly 0
or 1, because `prob_to_american` has no fair price for it. The simulation imports
(`PropDistribution`, `GameSimSummary`, `WinProbability`) serve the report builders only;
the core arithmetic takes plain floats, so the tests feed it synthetic numbers.

### `betting/bet_signal.py`

[Source](../../betting/bet_signal.py) · 268 lines · Tests: [test_bet_signal.py](../../tests/unit/test_bet_signal.py)

Turns a list of edge reports into a ranked list of bet signals in three steps.

1. **The gate.** A report passes when its edge is above zero and at least `min_edge`
   (default 0.02), and its EV is above `min_ev` (default 0.0, a strict `>`). A report at
   exactly the EV floor fails.
2. **The stake.** Each passing report gets a fraction of the bankroll by fractional
   Kelly. The Kelly criterion is the stake that maximizes the long-run growth of a
   bankroll: `f* = (b·p − q) / b`, where `b` is the decimal odds minus 1, `p` the
   simulator's probability and `q = 1 − p`. The stake is
   `clamp(kelly_fraction × max(0, f*), 0, cap)`, with quarter Kelly (0.25) and a 5% cap
   by default.
3. **The rank.** Signals sort by EV, then edge, then stake, each highest first, and carry
   a 0-based `rank`.

A signal says the simulator prices a side above the market. It is not evidence that the
bet makes money. The module has no timing rule: a signal holds at the price its report
captured, and the caller rebuilds the report when the price moves.

| Function / class | What it does | Called from |
|---|---|---|
| `DEFAULT_MIN_EDGE`, `DEFAULT_MIN_EV`, `DEFAULT_KELLY_FRACTION`, `DEFAULT_MAX_STAKE_FRACTION` | The four defaults, at module level so the API and the scripts read the same numbers. | `scripts/accuracy/confidence.py`, `scripts/accuracy/hypothetical_return.py` |
| `kelly_fraction_full(p_sim, offered_american)` | The full-Kelly fraction; negative for a negative-EV bet. | `stake_fraction` |
| `stake_fraction(report, kelly_fraction, cap)` | The recommended stake, floored at 0 and capped. | `bet_signals_from_edges` |
| `BetSignalConfig` | The gate and stake settings. A negative Kelly fraction or cap raises `ValueError`. | `api/routes/betting.py` (`/signals` builds it from query parameters) |
| `BetSignal` | One signal: the report's label, side, line, price, edge and EV, plus the stake, a `confidence` (a copy of the edge) and the rank. It keeps the source report. | `api/routes/betting.py` |
| `bet_signals_from_edges(reports, config)` | Gate, stake, rank. Returns `[]` when nothing passes. | `api/routes/betting.py` |

`/signals` fires on all fifteen game markets, the uncalibrated segment and team markets
included (an owner ruling on the segment markets, SIM-546; the comment sits in
`api/routes/betting.py`).

### `betting/line_movement.py`

[Source](../../betting/line_movement.py) · 803 lines · Tests: [test_line_movement.py](../../tests/unit/test_line_movement.py), [test_odds_readers.py](../../tests/unit/test_odds_readers.py)

Builds the line-movement series of one side of one market at one book: the ordered quotes
from the opening to the close. From the series it derives the raw implied-probability
path, the step changes, the opening-to-closing summary and its direction ("toward" when
the side's implied probability rose, "away" when it fell), the CLV of a bet at the opening
price, and a sharp-consensus flag. The module reads the three full-game markets only:
moneyline, run line and total.

The per-quote implied probability keeps the vig, because a de-vig needs both sides and a
series holds one side. The CLV de-vigs the opening pair and the closing pair. So the raw
delta and the CLV can disagree in sign. The CLV here describes the market; the model is
not judged by it (owner ruling 2026-09-28, in the module docstring).

On a run line the series records its shape: `pair`, `two_bets` or `mixed`
(`run_line_shape`). It has no CLV when the side's spread moved between the opening and the
close (a different bet; `clv_note` says why). When either end is two separate bets, both
ends are priced the same way, each price over the game's two-way margin at that time
(`clv_basis = "two_bets"`).

| Function / class | What it does | Called from |
|---|---|---|
| `LineQuote` | One stamped quote: this side's price, the other side's price, the line, the other side's line, the book and its display name. | `line_movement_from_quotes` |
| `LineMovement` | One series and its summary: opening and closing prices, the deltas, the direction, the CLV, `sharp_consensus`, the run-line shape, `clv_basis`, `clv_note`, `book_name`. | `api/schemas/markets.py` (`LineMovementModel.from_dataclass`) |
| `line_movement_from_quotes(rows, market_type, side, ...)` | The pure builder. It coerces the rows (a row with a NULL price for this side is dropped), sorts by `fetched_at`, derives the series and the summary, and prices the CLV. | `fetch_line_movement`, `_net_direction` |
| `fetch_line_movement(conn, *, game_pk, market_type, book=None)` | The one database read. It selects the one-book rows (`book LIKE 'bp:%'`, `STORED_BOOK_FILTER_SQL`) of the game and market in `fetched_at` order. On a run line with any two-bet row it also reads the game's total and moneyline rows for the margin. It returns one series per side and book. | `api/routes/betting_lines.py` |
| `_reference_margin_reader(rows)` | The margin for a quote: the reference row of the quote's line type nearest the quote in time, its own book first, any book second, the flat 1.05 last. | `fetch_line_movement` |
| `_net_direction(rows, ...)` | The direction of a side over all books pooled, or over the rows flagged `is_sharp_book`. | `fetch_line_movement` (the `sharp_consensus` flag) |

**Watch out:** the series orders on `raw.game_odds.fetched_at`, the time the writer stored
the row. The writer skips a row whose prices match an earlier row of the same game and
source (an odds hash); the live writer re-stamps that row's `last_seen_at` instead. So a stored row is
a new price set, not a new moment. No writer sets `is_sharp_book` to true today, so
`sharp_consensus` reads None in production.

## Who calls the package

| Caller | What it uses |
|---|---|
| [`api/routes/betting.py`](../../api/routes/betting.py) | `GET /api/betting/games/{pk}/edges` and `/signals`; `BetSignalConfig`, `bet_signals_from_edges` |
| [`api/routes/betting_pricing.py`](../../api/routes/betting_pricing.py) | Every report builder, the one-sided de-vig and the margin rule |
| [`api/routes/betting_stored_odds.py`](../../api/routes/betting_stored_odds.py) | `reference_margin_from_prices`, `american_to_decimal` for the stored lines |
| [`api/routes/betting_lines.py`](../../api/routes/betting_lines.py) | `GET .../line-movement` and `.../clv`; `fetch_line_movement` |
| [`api/routes/games.py`](../../api/routes/games.py) | `GET /api/games/{pk}/props/{player_id}/{prop}`; `prop_edge_report` |
| [`scripts/accuracy/`](../../scripts/accuracy/) | The accuracy comparison: grading, pricing, the confidence floor and the hypothetical return |
| `scripts/probes/book_sharpness.py`, `rescore_graded_book.py`, `rescore_runlines.py` | One-off studies of the stored odds |

No betting-value measurement runs until every band the acceptance lanes grade is green
(owner ruling 2026-09-08). The hypothetical-return report in
`scripts/accuracy/hypothetical_return.py` cites that ruling.

Where the odds come from — the providers, the stored tables and the one-book rule — is on
the [data ingestion page](pipeline-ingest.md). The tables are on the
[database page](database.md#odds).
