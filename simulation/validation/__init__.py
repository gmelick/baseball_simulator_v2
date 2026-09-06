"""
simulation.validation
======================
SIM-325 -- the validation-spine **end-to-end** package.

Houses the historical-replay + chi-squared goodness-of-fit harness
(:mod:`simulation.validation.replay_chi_squared`): replay a set of games
through the SIM-320 ``simulate_game()`` loop and assert the simulated
per-team-game run distribution matches an actual / reference distribution via a
chi-squared GOF test (the spec's "p > 0.05" validation gate).

This is a NEW harness module; it does NOT modify ``simulation/sim_loop.py`` --
it only *drives* it (the SIM-316/320/324 dependency-injection pattern: an
the synthetic league bundle, no live DuckDB).
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
