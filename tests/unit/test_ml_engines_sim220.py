"""tests/unit/test_ml_engines_sim220.py — SIM-220
====================================================
Unit tests for the probabilistic backtester in
``similarity.backtesting.backtester``.

These tests pin the calibration / accuracy metrics on KNOWN synthetic inputs
(no live DB, no engine, no FAISS):

  (a) a perfectly-calibrated predictor -> ~0 ECE, low Brier, low log-loss;
  (b) a deliberately mis-calibrated (over-confident) predictor -> strictly
      higher ECE / Brier / log-loss than the well-calibrated one;
  (c) log-loss stays finite when a predictor puts ~0 mass on the true class
      (eps clipping), and equals the analytic ``-log(eps)`` in the limit;
  (d) the reliability curve places rows in the correct confidence bins with the
      correct per-bin accuracy;
  (e) the multi-class Brier matches its closed form on a hand-checked case;
  (f) ``probs_from_dicts`` builds the right matrix from sampler-style dicts;
  (g) ``walk_forward_ablation`` runs the SIM-076 expanding folds and shows a
      signal-bearing model beating the league-average baseline (positive
      deltas), while a no-signal model does NOT beat the floor.

A fixed RNG seed keeps the stochastic assertions deterministic.

Run with:
    pytest tests/unit/test_ml_engines_sim220.py -v
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from similarity.backtesting.backtester import (
    OUTCOME_PROB_EPS,
    brier_score,
    evaluate_distributions,
    expected_calibration_error,
    league_average_distribution,
    log_loss,
    normalize_probs,
    probs_from_dicts,
    reliability_curve,
    walk_forward_ablation,
)


SEED = 20260610
CLASSES = ["single", "double", "field_out", "strikeout"]
N_CLASSES = len(CLASSES)


def _onehot(labels, n_classes):
    oh = np.zeros((len(labels), n_classes))
    oh[np.arange(len(labels)), labels] = 1.0
    return oh


def _well_calibrated_dataset(n=4000, n_classes=N_CLASSES, seed=SEED):
    """A genuinely calibrated predictor: draw a random p-vector per row, then
    sample the actual label *from that vector*. By construction the predicted
    probabilities match the data-generating process, so confidence ~ accuracy.
    """
    rng = np.random.default_rng(seed)
    alpha = np.full(n_classes, 0.6)
    probs = rng.dirichlet(alpha, size=n)
    labels = np.array([rng.choice(n_classes, p=probs[i]) for i in range(n)])
    return probs, labels


class TestMetricBasics(unittest.TestCase):
    def test_perfect_predictor_scores_zero(self):
        # All mass on the true class -> ECE/Brier/log_loss all ~0, acc==1.
        labels = np.array([0, 1, 2, 3, 0, 2])
        probs = _onehot(labels, N_CLASSES)
        self.assertAlmostEqual(brier_score(probs, labels), 0.0, places=9)
        self.assertAlmostEqual(expected_calibration_error(probs, labels), 0.0, places=9)
        self.assertAlmostEqual(log_loss(probs, labels), 0.0, places=9)
        res = evaluate_distributions(probs, labels)
        self.assertAlmostEqual(res["accuracy"], 1.0, places=9)

    def test_brier_closed_form(self):
        # Single row, 3 classes, p=[0.7,0.2,0.1], true class 0.
        # Brier = (0.7-1)^2 + 0.2^2 + 0.1^2 = 0.09 + 0.04 + 0.01 = 0.14
        probs = np.array([[0.7, 0.2, 0.1]])
        labels = np.array([0])
        self.assertAlmostEqual(brier_score(probs, labels), 0.14, places=12)

    def test_log_loss_closed_form(self):
        # p_true = 0.5 over two rows -> log_loss = -log(0.5).
        probs = np.array([[0.5, 0.5], [0.5, 0.5]])
        labels = np.array([0, 1])
        self.assertAlmostEqual(log_loss(probs, labels), -math.log(0.5), places=12)

    def test_log_loss_clips_zero_to_finite(self):
        # Zero mass on the true class would be +inf without eps clipping.
        probs = np.array([[1.0, 0.0], [1.0, 0.0]])
        labels = np.array([1, 1])  # true class has prob 0
        ll = log_loss(probs, labels)
        self.assertTrue(np.isfinite(ll))
        # Each row contributes -log(eps) after clipping.
        self.assertAlmostEqual(ll, -math.log(OUTCOME_PROB_EPS), places=6)


class TestCalibrationOrdering(unittest.TestCase):
    def test_calibrated_beats_miscalibrated(self):
        probs, labels = _well_calibrated_dataset()

        good = evaluate_distributions(probs, labels, n_bins=10)

        # Mis-calibrate by sharpening toward the argmax (over-confident): raise
        # to a power and renormalize. This pushes confidence above accuracy.
        sharp = probs**6
        sharp = sharp / sharp.sum(axis=1, keepdims=True)
        bad = evaluate_distributions(sharp, labels, n_bins=10)

        # Well-calibrated => small ECE; over-confident => materially larger.
        self.assertLess(good["ece"], 0.05)
        self.assertGreater(bad["ece"], good["ece"])
        # Over-confidence also hurts Brier and log-loss (proper scoring rules).
        self.assertGreater(bad["brier"], good["brier"])
        self.assertGreater(bad["log_loss"], good["log_loss"])

    def test_uniform_predictor_is_diffuse(self):
        # A uniform predictor has confidence == 1/C in every row; its accuracy
        # is the marginal of the argmax class. ECE is just |1/C - acc|.
        _, labels = _well_calibrated_dataset(n=2000)
        uniform = np.full((len(labels), N_CLASSES), 1.0 / N_CLASSES)
        res = evaluate_distributions(uniform, labels)
        # log-loss of a uniform predictor is exactly log(C).
        self.assertAlmostEqual(res["log_loss"], math.log(N_CLASSES), places=6)


class TestReliabilityCurve(unittest.TestCase):
    def test_bins_and_accuracy_are_correct(self):
        # Construct rows with controlled confidence + correctness:
        #  - 3 rows with confidence 0.95, all CORRECT   -> bin [0.9,1.0]
        #  - 2 rows with confidence 0.55, all WRONG     -> bin [0.5,0.6]
        rows = []
        labels = []
        # confident + correct (2-class): p=[0.95,0.05], true=0
        for _ in range(3):
            rows.append([0.95, 0.05])
            labels.append(0)
        # mid-confidence + wrong: p=[0.55,0.45], true=1 (argmax is class 0)
        for _ in range(2):
            rows.append([0.55, 0.45])
            labels.append(1)
        probs = np.array(rows)
        labels = np.array(labels)

        curve = reliability_curve(probs, labels, n_bins=10)
        by_bin = {round(pt["bin_lo"], 1): pt for pt in curve}

        self.assertIn(0.9, by_bin)
        self.assertIn(0.5, by_bin)
        self.assertEqual(by_bin[0.9]["count"], 3)
        self.assertAlmostEqual(by_bin[0.9]["accuracy"], 1.0, places=9)
        self.assertAlmostEqual(by_bin[0.9]["mean_confidence"], 0.95, places=9)
        self.assertEqual(by_bin[0.5]["count"], 2)
        self.assertAlmostEqual(by_bin[0.5]["accuracy"], 0.0, places=9)
        self.assertAlmostEqual(by_bin[0.5]["mean_confidence"], 0.55, places=9)

        # ECE = weighted mean gap = (3/5)|0.95-1.0| + (2/5)|0.55-0.0|
        expected_ece = (3 / 5) * 0.05 + (2 / 5) * 0.55
        self.assertAlmostEqual(
            expected_calibration_error(probs, labels, n_bins=10),
            expected_ece,
            places=9,
        )


class TestProbsFromDicts(unittest.TestCase):
    def test_builds_matrix_and_renormalizes(self):
        dists = [
            {"single": 0.3, "field_out": 0.5, "strikeout": 0.2},
            {"double": 1.0},
            {"single": 0.25, "double": 0.25, "field_out": 0.25, "strikeout": 0.25},
        ]
        mat = probs_from_dicts(dists, CLASSES)
        self.assertEqual(mat.shape, (3, N_CLASSES))
        np.testing.assert_allclose(mat.sum(axis=1), 1.0, atol=1e-9)
        # Row 0: 'double' absent -> 0 in that column.
        self.assertAlmostEqual(mat[0, CLASSES.index("double")], 0.0, places=12)
        self.assertAlmostEqual(mat[0, CLASSES.index("field_out")], 0.5, places=12)
        # Row 1: all mass on 'double'.
        self.assertAlmostEqual(mat[1, CLASSES.index("double")], 1.0, places=12)

    def test_degenerate_row_falls_back_to_uniform(self):
        mat = probs_from_dicts([{}], CLASSES)
        np.testing.assert_allclose(mat[0], 1.0 / N_CLASSES, atol=1e-12)


# ---------------------------------------------------------------------------
# Walk-forward ablation: a signal model beats the league-average floor.
# ---------------------------------------------------------------------------
SEASONS = [2019, 2020, 2021, 2022]
ROWS_PER_SEASON = 400


def _make_pa_dataset(seed=SEED):
    """Synthetic PA dataset: a 1-D feature deterministically biases the outcome.

    feature is a class index in [0, N_CLASSES); the actual outcome is that same
    class ~80% of the time and uniform noise otherwise. A feature-aware model
    can therefore beat the marginal (league-average) baseline; a feature-blind
    model cannot.
    """
    rng = np.random.default_rng(seed)
    seasons, features, outcomes = [], [], []
    for s in SEASONS:
        for _ in range(ROWS_PER_SEASON):
            f = int(rng.integers(0, N_CLASSES))
            if rng.random() < 0.8:
                y = f
            else:
                y = int(rng.integers(0, N_CLASSES))
            seasons.append(s)
            features.append([float(f)])
            outcomes.append(CLASSES[y])
    return {"season": seasons, "feature": features, "outcome": outcomes}


def _signal_predict(train_subset, x):
    """Feature-aware predictor: empirical outcome distribution among the train
    rows whose feature matches the query (a 1-feature k-NN / lookup). Falls back
    to the overall train marginal when the feature is unseen."""
    feats = train_subset["feature"]
    train_labels = np.asarray(train_subset["label"], dtype=int)
    n_classes = len(train_subset["classes"])
    qf = float(np.asarray(x).ravel()[0])

    counts = np.full(n_classes, OUTCOME_PROB_EPS, dtype=float)
    matched = 0
    for fv, lbl in zip(feats, train_labels):
        if lbl < 0:
            continue
        if abs(float(np.asarray(fv).ravel()[0]) - qf) < 1e-9:
            counts[lbl] += 1.0
            matched += 1
    if matched == 0:
        for lbl in train_labels:
            if lbl >= 0:
                counts[lbl] += 1.0
    return counts / counts.sum()


def _blind_predict(train_subset, x):
    """Feature-blind predictor: the overall training marginal — identical
    behaviour to the league-average baseline (a no-signal control arm)."""
    train_labels = np.asarray(train_subset["label"], dtype=int)
    n_classes = len(train_subset["classes"])
    counts = np.full(n_classes, OUTCOME_PROB_EPS, dtype=float)
    for lbl in train_labels:
        if lbl >= 0:
            counts[lbl] += 1.0
    return counts / counts.sum()


class TestWalkForwardAblation(unittest.TestCase):
    def test_signal_model_beats_league_average(self):
        df = _make_pa_dataset()
        res = walk_forward_ablation(
            df,
            predict_fn=_signal_predict,
            classes=CLASSES,
            extra_predictors={"blind": _blind_predict},
        )

        # Folds: SIM-076 expanding window -> n_distinct_seasons - 1 folds.
        self.assertEqual(len(res["folds"]), len(SEASONS) - 1)
        for fold in res["folds"]:
            self.assertEqual(fold["n_test"], ROWS_PER_SEASON)

        metrics = res["metrics"]
        for arm in ("model", "baseline", "blind"):
            self.assertIn(arm, metrics)
            self.assertGreater(metrics[arm]["n"], 0)
            # Every arm exposes the full metric set incl. a reliability curve.
            for key in ("ece", "brier", "log_loss", "accuracy", "reliability_curve"):
                self.assertIn(key, metrics[arm])

        abl = res["ablation"]
        # Signal model beats the floor on the proper scoring rules + accuracy.
        self.assertGreater(abl["model"]["brier"], 0.0)
        self.assertGreater(abl["model"]["log_loss"], 0.0)
        self.assertGreater(abl["model"]["accuracy"], 0.0)
        # The model is sharper/more-accurate than the league-average baseline.
        self.assertGreater(metrics["model"]["accuracy"], metrics["baseline"]["accuracy"])
        self.assertLess(metrics["model"]["brier"], metrics["baseline"]["brier"])

        # The feature-BLIND control is essentially the baseline -> ~no lift.
        self.assertAlmostEqual(abl["blind"]["brier"], 0.0, delta=0.02)
        self.assertAlmostEqual(abl["blind"]["log_loss"], 0.0, delta=0.05)

    def test_handles_label_outside_vocabulary(self):
        # A held-out outcome not in CLASSES is skipped, not crashed on.
        df = _make_pa_dataset()
        df["outcome"][0] = "triple"  # unknown event
        res = walk_forward_ablation(df, predict_fn=_signal_predict, classes=CLASSES)
        self.assertIn("model", res["metrics"])
        self.assertGreater(res["metrics"]["model"]["n"], 0)


class TestLeagueAverageDistribution(unittest.TestCase):
    def test_marginal_frequencies(self):
        labels = [0, 0, 0, 1, 2]  # 5 rows
        vec = league_average_distribution(labels, N_CLASSES)
        self.assertEqual(vec.shape, (N_CLASSES,))
        self.assertAlmostEqual(vec.sum(), 1.0, places=9)
        # Class 0 dominates (3/5); class 3 unseen -> ~0 (eps-smoothed).
        self.assertGreater(vec[0], vec[1])
        self.assertGreater(vec[0], vec[3])
        self.assertGreater(vec[3], 0.0)  # eps keeps it strictly positive

    def test_normalize_probs_handles_bad_rows(self):
        mat = np.array([[0.0, 0.0], [2.0, 2.0]])
        out = normalize_probs(mat)
        np.testing.assert_allclose(out[0], 0.5, atol=1e-12)  # all-zero -> uniform
        np.testing.assert_allclose(out[1], 0.5, atol=1e-12)  # renormalized


if __name__ == "__main__":
    unittest.main()
