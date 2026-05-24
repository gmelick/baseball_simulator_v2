"""
betting
=======
The betting / markets layer (Agent 8).  Turns simulation outputs (SIM-329 prop
PMFs, SIM-330 win probability, SIM-327 ``GameSimSummary`` raw score arrays) into
actionable betting edge: implied probability, de-vig / no-vig fair odds, edge
(sim vs market), expected value, and **Closing Line Value (CLV)** -- the
gold-standard validation metric for this platform.

The first inhabitant is :mod:`betting.clv_engine` (SIM-339).  Everything here is
deterministic, pure numpy / Python, RNG-free and DB-free; odds are INJECTED by
the caller (no live provider needed).
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
