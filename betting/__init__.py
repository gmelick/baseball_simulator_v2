"""The betting layer: it compares the simulator's probabilities with the market's prices.

The simulator gives probabilities: a player's prop distribution (the
probability of each value of his stat), the win probability and the raw
per-game scores (``GameSimSummary``). The market gives prices in American odds:
``-110`` means stake 110 to win 100, ``+150`` means stake 100 to win 150. Each
price carries the vig, the book's margin, so the implied probabilities of a
market's sides add up to more than 1.

The package turns the two into numbers that compare: the implied probability,
the no-vig probability (the implied probability with the vig removed), the edge
(the simulator's probability minus the no-vig probability), the expected value
(EV) and the closing line value (CLV: how the price a bet took compares with
the market's last price before first pitch).

The arithmetic is deterministic: no random numbers, and the caller supplies the
odds. Only :func:`betting.line_movement.fetch_line_movement` reads the
database, through a connection the caller passes in. The package re-exports
the main names of :mod:`betting.clv_engine`.
"""

from __future__ import annotations

from betting.clv_engine import (
    CLV,
    EdgeReport,
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    american_to_decimal,
    american_to_implied_prob,
    clv_from_odds,
    clv_from_prob,
    decimal_to_american,
    devig_multiway,
    devig_two_way,
    edge,
    expected_value,
    fair_american_from_prob,
    implied_prob_from_american,
    moneyline_edge_report,
    prob_to_american,
    prop_edge_report,
    total_over_under_edge_report,
)

__all__ = [
    "CLV",
    "EdgeReport",
    "MarketSide",
    "OddsQuote",
    "TwoWayMarket",
    "american_to_decimal",
    "american_to_implied_prob",
    "clv_from_odds",
    "clv_from_prob",
    "decimal_to_american",
    "devig_multiway",
    "devig_two_way",
    "edge",
    "expected_value",
    "fair_american_from_prob",
    "implied_prob_from_american",
    "moneyline_edge_report",
    "prob_to_american",
    "prop_edge_report",
    "total_over_under_edge_report",
]
