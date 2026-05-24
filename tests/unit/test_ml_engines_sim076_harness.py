"""tests/unit/test_ml_engines_sim076_harness.py — SIM-076
============================================================
Unit tests for the recency walk-forward validation harness in
``similarity.backtesting.recency_walk_forward``.

The harness validates that the SIM-076 ``recency_weight`` column improves the
*out-of-sample* fit of similarity-based outcome prediction. These tests build
synthetic data two ways and assert the harness behaves correctly:

  (a) ``walk_forward_folds`` yields expanding windows with NO leakage — every
      train season is strictly less than its test season, and the window grows;
  (b) ``walk_forward_recency_eval`` returns well-formed per-fold and aggregate
      results;
  (c) on **drift** data — where the outcome's dependence on the feature changes
      each season so recent seasons resemble the test season more than old ones
      — the recency-weighted error is <= the unweighted (baseline) error;
  (d) on **stationary** data — same outcome rule every season — recency neither
      meaningfully helps nor hurts, so the two errors are approximately equal.

A fixed RNG seed keeps the assertions deterministic; tolerances are kept loose.

Run with:
    pytest tests/unit/test_ml_engines_sim076_harness.py -v
"""

from __future__ import annotations

import unittest

import numpy as np

from similarity.backtesting.recency_walk_forward import (
    recency_weighted_prediction,
    walk_forward_folds,
    walk_forward_recency_eval,
)
from pipeline.batch.player_profile_computor import recency_weight


SEASONS = [2018, 2019, 2020, 2021, 2022]
REF_SEASON = SEASONS[-1]
ROWS_PER_SEASON = 120
FEATURE_DIM = 3
SEED = 20240511


def _make_dataset(*, drift: bool) -> dict:
    """Build a synthetic pooled-row dataset.

    Each row has a ``feature`` vector, a scalar ``outcome``, the ``season`` it
    came from, and the SIM-076 ``recency_weight`` for that season relative to
    ``REF_SEASON`` (using the real reference implementation so the harness is
    exercised against production weights).

    The outcome is a linear function of the feature: ``outcome = w . feature``.

    * **drift=True** — the coefficient vector ``w`` rotates/grows monotonically
      with season, so the input/output relationship in 2022 (the final test
      season) is closest to 2021, then 2020, etc. Recent training seasons are
      therefore better comparables than old ones, which is exactly the regime in
      which up-weighting recent rows should reduce out-of-sample error.

    * **drift=False** — ``w`` is identical every season (stationary). All
      training seasons are equally good comparables, so recency weighting should
      neither help nor hurt beyond noise.
    """
    rng = np.random.default_rng(SEED)
    base_w = np.array([1.0, -0.5, 0.25])

    seasons: list[int] = []
    features: list[np.ndarray] = []
    outcomes: list[float] = []
    weights: list[float] = []

    for s in SEASONS:
        if drift:
            # Coefficients drift linearly with how many seasons after the start.
            step = s - SEASONS[0]
            coef = base_w + step * np.array([0.6, 0.6, 0.6])
        else:
            coef = base_w

        X = rng.normal(0.0, 1.0, size=(ROWS_PER_SEASON, FEATURE_DIM))
        noise = rng.normal(0.0, 0.05, size=ROWS_PER_SEASON)
        y = X @ coef + noise

        rw = recency_weight(s, REF_SEASON)
        for i in range(ROWS_PER_SEASON):
            seasons.append(s)
            features.append(X[i])
            outcomes.append(float(y[i]))
            weights.append(rw)

    return {
        "season": seasons,
        "feature": features,
        "outcome": outcomes,
        "recency_weight": weights,
    }


class TestWalkForwardFolds(unittest.TestCase):
    def test_expanding_window_no_leakage(self):
        folds = walk_forward_folds(SEASONS)
        # n_distinct - 1 folds (first season can't be a test season).
        self.assertEqual(len(folds), len(SEASONS) - 1)

        prev_train_len = 0
        for train, test in folds:
            # No leakage: every train season strictly precedes the test season.
            self.assertTrue(all(ts < test for ts in train))
            # Expanding: train window grows by exactly one season each fold.
            self.assertEqual(len(train), prev_train_len + 1)
            prev_train_len = len(train)
            # Train seasons are sorted ascending and unique.
            self.assertEqual(list(train), sorted(set(train)))

        # Spot-check exact contents of the final fold.
        last_train, last_test = folds[-1]
        self.assertEqual(last_test, SEASONS[-1])
        self.assertEqual(list(last_train), SEASONS[:-1])

    def test_handles_unsorted_and_duplicate_seasons(self):
        folds = walk_forward_folds([2020, 2018, 2020, 2019, 2018])
        self.assertEqual(folds, [((2018,), 2019), ((2018, 2019), 2020)])


class TestSinglePrediction(unittest.TestCase):
    def test_empty_train_returns_nan(self):
        empty = {"season": [], "feature": [], "outcome": [], "recency_weight": []}
        pred = recency_weighted_prediction(empty, [0.0, 0.0, 0.0], weighted=True)
        self.assertTrue(np.isnan(pred))

    def test_prediction_is_finite_and_in_outcome_range(self):
        data = _make_dataset(drift=False)
        # Use all rows as "train" for a smoke check.
        pred = recency_weighted_prediction(data, data["feature"][0], weighted=True, k=10)
        self.assertTrue(np.isfinite(pred))
        lo, hi = min(data["outcome"]), max(data["outcome"])
        self.assertGreaterEqual(pred, lo - 1.0)
        self.assertLessEqual(pred, hi + 1.0)


class TestWalkForwardRecencyEval(unittest.TestCase):
    def test_result_shape_is_well_formed(self):
        data = _make_dataset(drift=True)
        res = walk_forward_recency_eval(data, metric="mae")

        self.assertEqual(res["metric"], "mae")
        self.assertEqual(len(res["folds"]), len(SEASONS) - 1)

        for fold in res["folds"]:
            for key in (
                "test_season",
                "n_train",
                "n_test",
                "weighted_error",
                "unweighted_error",
                "improvement",
            ):
                self.assertIn(key, fold)
            self.assertEqual(fold["n_test"], ROWS_PER_SEASON)
            self.assertAlmostEqual(
                fold["improvement"],
                fold["unweighted_error"] - fold["weighted_error"],
                places=9,
            )

        agg = res["aggregate"]
        self.assertEqual(agg["n_test_total"], (len(SEASONS) - 1) * ROWS_PER_SEASON)
        self.assertAlmostEqual(
            agg["improvement"],
            agg["unweighted_error"] - agg["weighted_error"],
            places=9,
        )
        self.assertAlmostEqual(res["improvement"], agg["improvement"], places=9)

    def test_unknown_metric_raises(self):
        data = _make_dataset(drift=False)
        with self.assertRaises(ValueError):
            walk_forward_recency_eval(data, metric="bogus")

    def test_recency_helps_under_drift(self):
        data = _make_dataset(drift=True)
        res = walk_forward_recency_eval(data, metric="mae")
        agg = res["aggregate"]
        # Recency weighting should not be worse, and should be a real
        # improvement on aggregate when the relationship drifts.
        self.assertLessEqual(agg["weighted_error"], agg["unweighted_error"] + 1e-9)
        self.assertGreater(agg["improvement"], 0.0)

    def test_recency_neutral_under_stationarity(self):
        data = _make_dataset(drift=False)
        res = walk_forward_recency_eval(data, metric="mae")
        agg = res["aggregate"]
        # No drift => recency neither helps nor hurts much. The two errors
        # should be close in both absolute and relative terms.
        self.assertAlmostEqual(agg["weighted_error"], agg["unweighted_error"], delta=0.05)
        rel = abs(agg["improvement"]) / max(agg["unweighted_error"], 1e-9)
        self.assertLess(rel, 0.25)

    def test_drift_improvement_exceeds_stationary(self):
        drift_imp = walk_forward_recency_eval(_make_dataset(drift=True))["improvement"]
        stat_imp = walk_forward_recency_eval(_make_dataset(drift=False))["improvement"]
        # Recency buys materially more under drift than under stationarity.
        self.assertGreater(drift_imp, stat_imp)


if __name__ == "__main__":
    unittest.main()
