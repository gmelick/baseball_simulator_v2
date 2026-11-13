# Betting

The betting layer compares the simulator's probabilities with the market's prices. Its
input is a probability from the simulator (a win probability, a player's prop
distribution, or the per-game scores) and a price from a sportsbook. Its output is an
edge report per side of a market and a ranked list of bet signals. The API's betting
routes and the accuracy-comparison tool call it.

The package is one module, `betting/bet_signal.py`. It is plain arithmetic: it reads no
database, draws no random numbers and fetches no odds. The caller passes the prices in.

## The terms

The whole package rests on seven terms. The module uses them with these meanings.

| Term | Meaning |
|---|---|
| **American odds** | The book's price. `-110` means you stake 110 to win 100. `+150` means you stake 100 to win 150. |
| **Decimal odds** | The total return per unit staked. `+150` is 2.50; `-110` is about 1.909. |
| **Implied probability** | The probability a price stands for, with the book's margin still in it. `-110` implies 0.5238. `+150` implies 0.40. |
| **Vig (the margin)** | The book's cut. The implied probabilities of a market's sides add up to more than 1. The excess (about 5% on a typical two-way market) is the vig, also called the overround. |
| **No-vig (fair) probability** | The implied probability with the vig removed, so the sides add up to exactly 1. Removing it is the "de-vig". |
| **Edge** | The simulator's probability minus the no-vig probability. A positive edge means the simulator rates the side more likely than the market does. |
| **Expected value (EV)** | The profit per unit staked if the simulator's probability is true: `EV = p × (decimal − 1) − (1 − p)`, at the OFFERED price, vig included. |

The edge reads the no-vig probability. The EV reads the offered price, because that is
the price a bet takes. Mixing the two up counts the vig twice or not at all, and the
report still looks plausible.

The package computes no closing line value (CLV: how a bet's price compares with the
market's last price before first pitch). The model is not judged by CLV (owner ruling
2026-09-28).

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
4. For each side of each market, the pricing code calls one report builder:
   `moneyline_edge_report`, `total_over_under_edge_report`, `run_line_edge_report` or
   `one_sided_edge_report` for the three full-game markets;
   `samples_over_under_edge_report` and `three_way_edge_report` for the twelve segment
   and team markets.
5. Each builder de-vigs the market (`devig_two_way` or `devig_multiway`), then computes
   the edge and the EV. It returns an `EdgeReport`.
6. `/edges` returns the reports. `/signals` passes them to `bet_signals_from_edges`,
   which gates, stakes and ranks them.

A second path is offline: the accuracy comparison (`scripts/accuracy/`) grades the
simulator against the closing lines of past games through the same pricing functions.

## The modules

### `betting/__init__.py`

[Source](../../betting/__init__.py) · 7 lines

The package docstring only. It re-exports nothing: import every name from
`betting.bet_signal`.

### `betting/bet_signal.py`

[Source](../../betting/bet_signal.py) · 1016 lines · Tests: [test_bet_signal_pricing.py](../../tests/unit/test_bet_signal_pricing.py), [test_bet_signal_run_line.py](../../tests/unit/test_bet_signal_run_line.py), [test_bet_signal.py](../../tests/unit/test_bet_signal.py), [test_segment_edges.py](../../tests/unit/test_segment_edges.py), [test_live_run_lines.py](../../tests/unit/test_live_run_lines.py)

The module works in two stages. The pricing stage converts prices, removes the vig, and
builds one `EdgeReport` per side of a market from a simulator output. The signal stage
gates, stakes and ranks those reports.

**How the vig comes off.** The module uses the proportional method. A market's raw
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

**The pricing stage.**

| Function / class | What it does | Called from |
|---|---|---|
| `american_to_decimal`, `decimal_to_american`, `implied_prob_from_american` (alias `american_to_implied_prob`), `prob_to_american` (alias `fair_american_from_prob`) | Convert between American odds, decimal odds and probability. A price of 0 or a probability outside (0, 1) raises `ValueError`. | every builder; `api/routes/betting_stored_odds.py`; `scripts/accuracy/pricing.py`, `hypothetical_return.py` |
| `devig_multiway(implied_probs)` | The proportional de-vig of N ≥ 2 outcomes. Returns no-vig probabilities that add up to 1. | `devig_two_way`, `three_way_edge_report`, `scripts/accuracy/grading.py` |
| `devig_two_way(a_american, b_american)` | The de-vig of a two-way market from its two prices. | `_build_edge_report`, `scripts/accuracy/grading.py` |
| `edge(p_sim, p_fair)` | `p_sim − p_fair`. | every builder |
| `expected_value(p_sim, offered_american)` | The EV per unit staked at the offered price. | every builder; `scripts/accuracy/hypothetical_return.py` |
| `MarketSide` | HOME, AWAY, OVER, UNDER, DRAW (the tie of a three-way market). | every builder; `api/routes/betting*.py`; `scripts/accuracy/` |
| `OddsQuote`, `TwoWayMarket` | One snapshot of a two-way market (`side`, `other`, `line`), and the market's entry quote. | `api/routes/betting_pricing.py`, `api/routes/games.py`, `scripts/accuracy/grading.py` |
| `EdgeReport` | One side of one market: the simulator's probability, the no-vig probability, the edge, the EV, the offered price and the simulator's fair price. `positive_edge` is a strict `edge > 0`. | the signal stage; the API's response models |
| `moneyline_edge_report(win_prob, market, side)` | The moneyline side, from a `WinProbability`. | `api/routes/betting_pricing.py`, `scripts/accuracy/grading.py` |
| `prop_edge_report(dist, market, side)` | A player prop's over or under, from a `PropDistribution` at the book's line. | `api/routes/games.py`, `scripts/accuracy/grading.py` |
| `total_over_under_edge_report(summary, market, side)` | The full-game total, from the summary's per-game total runs. It delegates to `samples_over_under_edge_report` with the label `total`. | `api/routes/betting_pricing.py` |
| `samples_over_under_edge_report(samples, market, side, label)` | Any over / under market from one number per simulated game: first-five runs, one team's runs, first-inning runs. | `api/routes/betting_pricing.py` (the segment totals, team totals and "a run in the first inning") |
| `three_way_edge_report(p_home, p_away, p_draw, ...)` | One side of a three-way moneyline. The three prices de-vig together. | `api/routes/betting_pricing.py` |
| `spread_cover_prob(margins, line, side)` | The simulator's cover probability of a run-line side. `line` is always the HOME line; the away side covers when `home − away < −line`. | `run_line_edge_report`, `run_line_bet_cover_prob` |
| `run_line_edge_report(margins, market, side, line)` | A run-line side that is one half of a pair. | `api/routes/betting_pricing.py` |
| `run_line_is_pair(home_spread, away_spread)` | True when the away spread is the negative of the home spread (0 / 0 included). | `scripts/accuracy/grading.py`, `scripts/probes/rescore_runlines.py` |
| `reference_margin_from_prices(candidates)` | The first candidate market whose two implied probabilities add up to a margin inside the band; else `(1.05, "flat")`. | `api/routes/betting_pricing.py`, `api/routes/betting_stored_odds.py`, `scripts/accuracy/pricing.py` |
| `devig_one_sided(american, margin)` | One separate bet's fair probability: its implied probability over the margin, capped at 1. | `api/routes/betting_pricing.py`, `scripts/accuracy/grading.py` |
| `run_line_bet_cover_prob(margins, side, own_line)` | The cover probability of one bet at its OWN spread (an away bet at −1.5 covers when the away team wins by 2 or more). | `scripts/accuracy/grading.py` |
| `one_sided_edge_report(...)` | The report of one separate bet: the edge against `devig_one_sided`'s fair probability, and the EV at the offered price. | `api/routes/betting_pricing.py` |

**Watch out:** every builder raises `ValueError` on a simulator probability of exactly 0
or 1, because `prob_to_american` has no fair price for it. The simulation imports
(`PropDistribution`, `GameSimSummary`, `WinProbability`) serve the report builders only;
the core arithmetic takes plain floats, so the tests feed it synthetic numbers.

**The signal stage.** `bet_signals_from_edges` turns a list of edge reports into a ranked
list of bet signals in three steps.

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

## Who calls the package

| Caller | What it uses |
|---|---|
| [`api/routes/betting.py`](../../api/routes/betting.py) | `GET /api/betting/games/{pk}/edges` and `/signals`; `BetSignalConfig`, `bet_signals_from_edges` |
| [`api/routes/betting_pricing.py`](../../api/routes/betting_pricing.py) | Every report builder, the one-sided de-vig and the margin rule |
| [`api/routes/betting_stored_odds.py`](../../api/routes/betting_stored_odds.py) | `reference_margin_from_prices`, `american_to_decimal` for the stored lines |
| [`api/routes/games.py`](../../api/routes/games.py) | `GET /api/games/{pk}/props/{player_id}/{prop}`; `prop_edge_report` |
| [`scripts/accuracy/`](../../scripts/accuracy/) | The accuracy comparison: grading, pricing, the confidence floor and the hypothetical return |
| `scripts/probes/book_sharpness.py`, `rescore_graded_book.py`, `rescore_runlines.py` | One-off studies of the stored odds |

No betting-value measurement runs until every band the acceptance lanes grade is green
(owner ruling 2026-09-08). The hypothetical-return report in
`scripts/accuracy/hypothetical_return.py` cites that ruling.

Where the odds come from — the providers, the stored tables and the one-book rule — is on
the [data ingestion page](pipeline-ingest.md). The tables are on the
[database page](database.md#odds).
