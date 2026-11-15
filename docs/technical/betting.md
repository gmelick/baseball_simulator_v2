# Betting

The betting layer compares the simulator's probabilities with the market's prices. It
takes a simulator output (a win probability, a player's prop distribution or the
per-game scores) and a book's prices. It gives one edge report per side of a market and
a ranked list of bet signals.

The package is one module, `betting/bet_signal.py`. It is plain arithmetic: no database,
no random numbers and no odds fetch. The caller passes the prices in.

## The terms

| Term | Meaning |
|---|---|
| **American odds** | The book's price. `-110` means stake 110 to win 100. `+150` means stake 100 to win 150. |
| **Decimal odds** | The total return per unit staked. `+150` is 2.50. |
| **Implied probability** | The probability a price stands for, with the book's margin still in it. `-110` implies 0.5238. |
| **Vig** | The book's margin. A market's implied probabilities add up to more than 1; the excess is the vig. |
| **No-vig probability** | The implied probabilities scaled so the sides add up to exactly 1. Removing the vig is the "de-vig". |
| **Edge** | The simulator's probability minus the no-vig probability. Positive: the simulator rates the side higher than the market. |
| **Expected value (EV)** | The profit per unit staked if the simulator is right: `EV = p × b − (1 − p)`, where `b` is the profit per unit on a win at the OFFERED price. |
| **Fair price** | The American price of a probability with no vig. The report shows the simulator's fair price next to the offered price. |

The edge reads the no-vig probability. The EV reads the offered price, because a bet
takes that price. Mixing the two up counts the vig twice or not at all.

The package computes no closing line value (CLV). The model is not judged by it (owner
ruling 2026-09-28).

## How it fits together

1. The game page's Betting card calls `GET /api/betting/games/{game_pk}/edges` and
   `.../signals` ([betting.ts](../../frontend/src/api/betting.ts)).
2. The route (`api/routes/betting.py`) runs or reuses a simulation of the game.
3. `api/routes/betting_pricing.py` finds each market's prices: the query parameters,
   then the stored lines (`api/routes/betting_stored_odds.py`), then the mock provider.
4. For each side, the pricing code calls one report builder. The builder turns the
   simulator output into a probability, de-vigs the prices and returns an `EdgeReport`.
5. `/edges` returns the reports. `/signals` passes them to `bet_signals_from_edges`.

The accuracy comparison (`scripts/accuracy/`) uses the same functions offline, against
the closing lines of past games.

## The modules

### `betting/__init__.py`

[Source](../../betting/__init__.py) · 7 lines

The package docstring only. Import every name from `betting.bet_signal`.

### `betting/bet_signal.py`

[Source](../../betting/bet_signal.py) · 611 lines · Tests: [test_bet_signal_pricing.py](../../tests/unit/test_bet_signal_pricing.py), [test_bet_signal_run_line.py](../../tests/unit/test_bet_signal_run_line.py), [test_bet_signal.py](../../tests/unit/test_bet_signal.py), [test_segment_edges.py](../../tests/unit/test_segment_edges.py), [test_live_run_lines.py](../../tests/unit/test_live_run_lines.py)

The module has seven sections, in this order.

**1. The conversions.**

| Name | What it does | Called from |
|---|---|---|
| `american_to_decimal` | American odds to decimal odds. | `expected_value`, `stake_fraction`; `betting_stored_odds.py`; `scripts/accuracy/pricing.py`, `hypothetical_return.py` |
| `implied_prob_from_american` | A price's implied probability, vig included. | `devig`, `devig_one_sided`, `reference_margin_from_prices`; `scripts/accuracy/pricing.py` |
| `prob_to_american` | A probability's fair price. Raises on 0 or 1. | `edge_report` (the report's `sim_fair_american`); `scripts/accuracy/grading.py` (to skip a 0 or 1 probability) |

**2. The de-vig.**

| Name | What it does | Called from |
|---|---|---|
| `devig(american_prices)` | The no-vig probabilities of two or more prices: `q_i / Σ q`. Two prices for a two-way market, three for a market with a tie. | every two-way builder, `three_way_edge_report`; `betting_pricing.py`; `scripts/accuracy/grading.py`; the probes |
| `devig_one_sided(american, margin)` | The no-vig probability of a bet with no opposite side: its implied probability over another market's margin, capped at 1. | `betting_pricing.py`, `scripts/accuracy/grading.py` |

**3. The expected value.** `expected_value(p_sim, offered_american)` is the EV per unit
staked. It is used by `edge_report`, `stake_fraction`, `betting_pricing.py` and
`scripts/accuracy/hypothetical_return.py`.

**4. The records.**

| Name | What it does |
|---|---|
| `MarketSide` | HOME, AWAY, OVER, UNDER, DRAW (the tie). |
| `OddsQuote` | A two-way market's prices: `side` (the side being priced), `other` and `line`. |
| `EdgeReport` | One side of one market: the simulator's probability, the no-vig probability, the edge, the EV, the offered price and the simulator's fair price. |
| `edge_report(...)` | Builds an `EdgeReport` from the simulator's probability, the no-vig probability and the offered price. Every builder ends here; `betting_pricing.py` calls it for a separate run-line bet and the first-to-score moneyline. |

**5. The report builders.** Each one reads a different simulator output.

| Name | Simulator output | Called from |
|---|---|---|
| `moneyline_edge_report` | the win probability | `betting_pricing.py`, `scripts/accuracy/grading.py` |
| `prop_edge_report` | a player's prop distribution | `api/routes/games.py`, `scripts/accuracy/grading.py` |
| `samples_over_under_edge_report` | one number per simulated game: the game total (label `total`), the first-five runs, a team's runs, the first-inning runs | `betting_pricing.py`, `scripts/accuracy/grading.py` |
| `three_way_edge_report` | the home, away and tie probabilities of the first-inning and first-five moneylines | `betting_pricing.py` |

**The push.** Scores are whole numbers. On a half-integer line no game lands on the line,
so the two sides add up to 1. On a whole-number line a game on the line is a push and
counts for neither side.

**6. The run line.** The book lists a run line in one of two ways.

* **A pair:** one bet with two sides (home −1.5 / away +1.5). The away spread is the
  negative of the home spread. The two prices de-vig against each other
  (`run_line_edge_report`).
* **Two separate bets:** the spreads share a sign (home −1.5 / away −1.5). Both can lose
  on a one-run game, so they must never de-vig against each other. Each bet is priced
  from its own price over the margin of another two-way market of the same game
  (`devig_one_sided`). The caller lists the reference markets in order: the segment's
  total, the full-game total, the moneyline. A margin outside `REFERENCE_MARGIN_BAND`
  (1.00 to 1.15) is skipped. With none usable, `DEFAULT_ONE_SIDED_MARGIN` (1.05) applies.
  A three-way market is never a reference, because its margin also covers the tie.

| Name | What it does | Called from |
|---|---|---|
| `spread_cover_prob(margins, line, side)` | The cover probability of a side. `line` is always the HOME line. | `run_line_edge_report`, `run_line_bet_cover_prob`; `simulation/game_market_distributions.py`; `scripts/accuracy/` |
| `run_line_bet_cover_prob(margins, side, own_line)` | The cover probability of one bet at its OWN spread. Kept apart from `spread_cover_prob` because the two line conventions are easy to mix up. | `betting_pricing.py`, `scripts/accuracy/grading.py` |
| `run_line_edge_report` | The report of one side of a pair. | `betting_pricing.py` |
| `run_line_is_pair` | True when away spread = −home spread. | `betting_pricing.py`, `scripts/accuracy/`, `scripts/probes/rescore_runlines.py` |
| `reference_margin_from_prices` | The margin of the first usable reference market. | `betting_pricing.py`, `betting_stored_odds.py`, `scripts/accuracy/pricing.py` |

**7. The signals.** `bet_signals_from_edges` has three steps.

1. **The gate.** A report passes when its edge is above zero and at least `min_edge`
   (0.02), and its EV is above `min_ev` (0.0).
2. **The stake.** The Kelly criterion gives the stake that grows a bankroll fastest:
   `f* = EV / b`. The stake is `min(cap, kelly_fraction × max(0, f*))`, with quarter
   Kelly (0.25) and a 5% cap by default.
3. **The rank.** Signals sort by EV, then edge, then stake, highest first.

| Name | What it does | Called from |
|---|---|---|
| `DEFAULT_MIN_EDGE` | The 0.02 edge floor. | `BetSignalConfig`; `scripts/accuracy/confidence.py`, `hypothetical_return.py` |
| `BetSignalConfig`, `DEFAULT_CONFIG` | The gate and stake settings, and their default instance. A negative Kelly fraction or cap raises `ValueError`. | `api/routes/betting.py` builds a `BetSignalConfig`; `DEFAULT_CONFIG` is the default argument inside the module |
| `stake_fraction(report, config)` | The recommended stake as a fraction of the bankroll. | `bet_signals_from_edges` |
| `BetSignal` | One signal: the report's label, side, line, price, edge and EV, plus the stake and the rank. It keeps the source report. | `api/schemas/markets.py` (`BetSignalModel`) |
| `bet_signals_from_edges(reports, config)` | Gate, stake and rank. | `api/routes/betting.py` |

A signal says the simulator prices a side above the market. It is not proof that the bet
makes money. A signal holds at the price its report captured; the caller rebuilds the
report when the price moves. `/signals` fires on all fifteen game markets, the
uncalibrated segment and team markets included (SIM-546).

**Watch out:** every builder raises `ValueError` on a simulator probability of exactly 0
or 1, because such a probability has no fair price. The pricing route and the accuracy
comparison skip that side.

## Who calls the package

| Caller | What it uses |
|---|---|
| [`api/routes/betting.py`](../../api/routes/betting.py) | `/edges` and `/signals`; `BetSignalConfig`, `bet_signals_from_edges` |
| [`api/routes/betting_pricing.py`](../../api/routes/betting_pricing.py) | Every report builder, the de-vigs and the margin rule |
| [`api/routes/betting_stored_odds.py`](../../api/routes/betting_stored_odds.py) | `reference_margin_from_prices`, `american_to_decimal` |
| [`api/routes/games.py`](../../api/routes/games.py) | `GET /api/games/{pk}/props/{player_id}/{prop}`; `prop_edge_report` |
| [`scripts/accuracy/`](../../scripts/accuracy/) | The accuracy comparison: grading, pricing, the confidence floor and the hypothetical return |
| `scripts/probes/book_sharpness.py`, `rescore_graded_book.py`, `rescore_runlines.py` | One-off studies of the stored odds |

No betting-value measurement runs until every band the acceptance lanes grade is green
(owner ruling 2026-09-08).

Where the odds come from is on the [data ingestion page](pipeline-ingest.md). The tables
are on the [database page](database.md#odds).
