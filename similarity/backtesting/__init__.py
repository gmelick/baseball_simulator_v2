"""similarity.backtesting -- out-of-sample validation harnesses.

SIM-076 contributes the recency-specific walk-forward harness
(:mod:`similarity.backtesting.recency_walk_forward`). It is intentionally
small, dependency-light, and operates on plain in-memory data so the broader
backtesting framework (SIM-220) can compose / reuse it with real pooled rows.

SIM-220 adds the probabilistic backtester
(:mod:`similarity.backtesting.backtester`): ECE / Brier / log-loss + reliability
curves and a walk-forward ablation vs a league-average baseline. It reuses the
SIM-076 :func:`walk_forward_folds` splitter and is likewise DB-free / injectable
so it unit-tests on synthetic distributions.
"""

from __future__ import annotations

from similarity.backtesting.backtester import (
    brier_score,
    evaluate_distributions,
    expected_calibration_error,
    league_average_distribution,
    log_loss,
    probs_from_dicts,
    reliability_curve,
    walk_forward_ablation,
)
from similarity.backtesting.recency_walk_forward import (
    recency_weighted_prediction,
    walk_forward_folds,
    walk_forward_recency_eval,
)

__all__ = [
    "recency_weighted_prediction",
    "walk_forward_folds",
    "walk_forward_recency_eval",
    "brier_score",
    "evaluate_distributions",
    "expected_calibration_error",
    "league_average_distribution",
    "log_loss",
    "probs_from_dicts",
    "reliability_curve",
    "walk_forward_ablation",
]
