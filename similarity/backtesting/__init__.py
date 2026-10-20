"""Offline validation harnesses: walk-forward backtests on in-memory data.

:mod:`similarity.backtesting.recency_walk_forward` checks whether the recency
weight improves an out-of-sample k-nearest-neighbour prediction of a scalar
outcome. It is small and reads plain in-memory data.

:mod:`similarity.backtesting.backtester` scores probability distributions over
discrete outcomes: the expected calibration error (ECE, the gap between the
stated confidence and the hit rate), the Brier score (the mean squared error
of the probability vector), the log-loss and the reliability curve. It also
runs a walk-forward ablation against a league-average baseline, on the folds
of :func:`walk_forward_folds`. Neither module reads a database, so the unit
tests run both on synthetic data.
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
