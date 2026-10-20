"""
simulation.validation
======================
The end-to-end check of the simulator's run distribution.

The package holds the historical-replay harness
(:mod:`simulation.validation.replay_chi_squared`). The harness replays a set of
games through ``simulate_game()``. A chi-squared goodness-of-fit test then
compares the simulated runs per team-game with an actual or reference
distribution; the gate is p > 0.05.

The harness drives the loop and changes nothing in it. With no database, it
runs the production machine over the synthetic league bundle.
"""

from __future__ import annotations

from simulation.validation.replay_chi_squared import (
    ChiSquaredResult,
    HistoricalGame,
    bin_run_totals,
    chi_squared_gof,
    pool_low_expected_bins,
    run_total_distribution,
    simulate_run_distribution,
)

__all__ = [
    "ChiSquaredResult",
    "HistoricalGame",
    "bin_run_totals",
    "chi_squared_gof",
    "pool_low_expected_bins",
    "run_total_distribution",
    "simulate_run_distribution",
]
