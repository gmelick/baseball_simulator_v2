"""Score predicted outcome distributions: calibration, accuracy and a walk-forward ablation.

:mod:`similarity.backtesting.recency_walk_forward` scores scalar predictions
with the mean absolute error and the root-mean-square error. This module
scores a probability distribution over discrete outcomes, such as
``{"single": 0.31, "field_out": 0.55, ...}``, against the outcome that
happened.

Offline only
------------
No live code calls the functions here. Production imports only the two
constants ``DEFAULT_N_BINS`` and ``OUTCOME_PROB_EPS``, into
:mod:`simulation.prop_validation`. That module scores the binary events (a
home win, an over) on a binary path of its own, because the multi-class
confidence below does not score a binary event correctly. The functions here
are tested and correct; "offline" describes their use, not their state.

What it computes
----------------
The inputs are an ``(n, c)`` matrix of predicted class probabilities and the
``(n,)`` integer labels of the actual outcomes.

* **ECE** (expected calibration error). A row's confidence is the probability
  of its predicted (argmax) class. The rows go into confidence bins. In each
  bin, the gap between the mean confidence and the hit rate is weighted by the
  bin's share of the rows. A calibrated predictor scores about 0.
* **Brier score**: the mean squared error between the predicted probability
  vector and the one-hot actual outcome. Lower is better; the range is
  ``[0, 2]``.
* **log-loss**: the multi-class cross-entropy. The probabilities are clipped at
  ``eps``, so a confident wrong prediction (``p → 0`` on the true class) costs a
  large but finite penalty, not ``+inf``.
* **reliability curve**: the per-bin points behind the ECE (the mean
  confidence, the hit rate, the count), for a reliability diagram.

The ablation
------------
:func:`walk_forward_ablation` runs the expanding-window folds of
:func:`walk_forward_folds`. On each held-out season it scores the model's
distributions and a league-average baseline: the outcome frequencies of the
training seasons, the same for every row, with no features. The delta is
baseline − model on each lower-is-better metric, so a positive delta means the
model beats the baseline. The caller can add named predictors, for example the
model with one engine replaced by the league average. Each one is reported
against the same baseline.

Testing
-------
The metric functions are pure NumPy: no database, no engine. The harness takes
an injected prediction function ``predict(train, x) -> probability vector``, so
the unit tests run it end to end on synthetic data.

Public API
----------
``OUTCOME_PROB_EPS``                                   — the log-loss clip.
``DEFAULT_N_BINS``                                     — the default bin count.
``probs_from_dicts(dicts, classes)``                  -> (n, c) ndarray
``normalize_probs(probs)``                            -> (n, c) ndarray
``expected_calibration_error(probs, labels, n_bins)`` -> float
``brier_score(probs, labels)``                        -> float
``log_loss(probs, labels, eps)``                      -> float
``reliability_curve(probs, labels, n_bins)``          -> list[dict]
``evaluate_distributions(probs, labels, ...)``        -> dict (all metrics)
``league_average_distribution(train_labels, n_classes)`` -> (c,) ndarray
``walk_forward_ablation(...)``                         -> dict
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

# The column names and the expanding-window folds of the recency harness.
from similarity.backtesting.recency_walk_forward import (
    FEATURE_COL,
    OUTCOME_COL,
    SEASON_COL,
    walk_forward_folds,
)

__all__ = [
    "OUTCOME_PROB_EPS",
    "DEFAULT_N_BINS",
    "probs_from_dicts",
    "normalize_probs",
    "expected_calibration_error",
    "brier_score",
    "log_loss",
    "reliability_curve",
    "evaluate_distributions",
    "league_average_distribution",
    "walk_forward_ablation",
]

#: The log-loss clip: a probability of exactly 0 on the true class costs
#: ``-log(eps)``, about 34.5, not ``+inf``. sklearn's ``log_loss`` long used the
#: same value as its default. The league-average baseline also uses it as its
#: smoothing pseudo-count.
OUTCOME_PROB_EPS = 1e-15

#: The default number of equal-width confidence bins of the ECE and the
#: reliability curve.
DEFAULT_N_BINS = 10


# ---------------------------------------------------------------------------
# 1. Conversion helpers: predicted-distribution dicts into a probability matrix
# ---------------------------------------------------------------------------
def probs_from_dicts(
    dists: Sequence[Mapping[str, float]],
    classes: Sequence[str],
) -> np.ndarray:
    """Stack a sequence of outcome-probability dicts into an ``(n, c)`` matrix.

    Each dict is one prediction, ``{outcome: probability}``. ``classes`` fixes
    the column order (the closed outcome vocabulary). An outcome missing from a
    dict counts 0.0, and an outcome not in ``classes`` is ignored. Each row is
    then scaled to add up to 1 (:func:`normalize_probs`), so a dict that leaves
    out its zero-probability outcomes still gives a valid row.

    Parameters
    ----------
    dists:
        Sequence of ``{class_name: probability}`` mappings.
    classes:
        Ordered, unique class labels defining the columns.

    Returns
    -------
    ``(n, c)`` float array; row ``i`` is the probability vector for ``dists[i]``
    over ``classes``.
    """
    col = {c: j for j, c in enumerate(classes)}
    n, c = len(dists), len(classes)
    out = np.zeros((n, c), dtype=float)
    for i, d in enumerate(dists):
        for k, p in d.items():
            j = col.get(k)
            if j is not None:
                out[i, j] += float(p)
    return normalize_probs(out)


def normalize_probs(probs: np.ndarray) -> np.ndarray:
    """Scale each row of an ``(n, c)`` matrix into a valid probability vector.

    Negative entries become 0. A row whose total is 0 or not finite becomes
    uniform, so the metrics never see an all-zero row. A matrix that is not 2-D
    raises ``ValueError``.
    """
    p = np.asarray(probs, dtype=float)
    if p.ndim != 2:
        raise ValueError(f"probs must be 2-D (n, c); got shape {p.shape}")
    p = np.clip(p, 0.0, None)
    row_sums = p.sum(axis=1, keepdims=True)
    bad = ~np.isfinite(row_sums.ravel()) | (row_sums.ravel() <= 0.0)
    if bad.any():
        p[bad] = 1.0
        row_sums = p.sum(axis=1, keepdims=True)
    return p / row_sums


def _validate(probs: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Check the shapes and the label range for the metric functions."""
    p = np.asarray(probs, dtype=float)
    y = np.asarray(labels).ravel().astype(int)
    if p.ndim != 2:
        raise ValueError(f"probs must be 2-D (n, c); got shape {p.shape}")
    if p.shape[0] != y.shape[0]:
        raise ValueError(f"probs has {p.shape[0]} rows but {y.shape[0]} labels were given")
    if p.shape[0] == 0:
        return p, y
    if y.min() < 0 or y.max() >= p.shape[1]:
        raise ValueError(
            f"labels must be class indices in [0, {p.shape[1] - 1}]; "
            f"got range [{int(y.min())}, {int(y.max())}]"
        )
    return p, y


# ---------------------------------------------------------------------------
# 2. Probabilistic metrics (pure NumPy)
# ---------------------------------------------------------------------------
def _confidence_and_correct(probs: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per row: the confidence (the largest probability) and the hit (1 or 0)."""
    pred = np.argmax(probs, axis=1)
    confidence = probs[np.arange(probs.shape[0]), pred]
    correct = (pred == labels).astype(float)
    return confidence, correct


def _bin_edges(n_bins: int) -> np.ndarray:
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1; got {n_bins}")
    return np.linspace(0.0, 1.0, n_bins + 1)


def reliability_curve(
    probs: np.ndarray,
    labels: np.ndarray,
    n_bins: int = DEFAULT_N_BINS,
) -> list[dict]:
    """The points of a reliability diagram: the mean confidence against the hit rate.

    Each row's confidence goes into one of ``n_bins`` equal-width bins on
    ``[0, 1]``. Each NON-EMPTY bin gives
    ``{bin_lo, bin_hi, count, mean_confidence, accuracy, gap}``. ``accuracy`` is
    the share of the bin's rows whose argmax class was right, and
    ``gap = |mean_confidence - accuracy|``. A calibrated model lies on the
    diagonal (``mean_confidence ≈ accuracy``).
    :func:`expected_calibration_error` adds up these points.

    The bins are half-open ``[lo, hi)``, except the last, which includes 1.0.
    """
    p, y = _validate(probs, labels)
    edges = _bin_edges(n_bins)
    if p.shape[0] == 0:
        return []
    confidence, correct = _confidence_and_correct(p, y)
    # np.digitize over the interior edges gives a bin index in [0, n_bins - 1];
    # a confidence of exactly 1.0 lands in the last bin. The clip is a guard.
    idx = np.clip(np.digitize(confidence, edges[1:-1], right=False), 0, n_bins - 1)

    curve: list[dict] = []
    for b in range(n_bins):
        mask = idx == b
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        mean_conf = float(confidence[mask].mean())
        acc = float(correct[mask].mean())
        curve.append(
            {
                "bin_lo": float(edges[b]),
                "bin_hi": float(edges[b + 1]),
                "count": cnt,
                "mean_confidence": mean_conf,
                "accuracy": acc,
                "gap": abs(mean_conf - acc),
            }
        )
    return curve


def expected_calibration_error(
    probs: np.ndarray,
    labels: np.ndarray,
    n_bins: int = DEFAULT_N_BINS,
) -> float:
    """The expected calibration error (ECE), binned on the confidence.

        ECE = Σ_b (n_b / N) · |acc_b − conf_b|

    over ``n_bins`` equal-width confidence bins. ``conf_b`` is the mean
    confidence in bin ``b`` and ``acc_b`` its hit rate. A calibrated predictor
    (confidence = hit rate in every bin) scores about 0; an over- or
    under-confident one scores more. The range is ``[0, 1]``; no rows give NaN.
    """
    p, y = _validate(probs, labels)
    if p.shape[0] == 0:
        return float("nan")
    n = p.shape[0]
    curve = reliability_curve(p, y, n_bins=n_bins)
    return float(sum(pt["count"] / n * pt["gap"] for pt in curve))


def brier_score(probs: np.ndarray, labels: np.ndarray) -> float:
    """The multi-class Brier score: the mean squared error of the probability vector.

        Brier = (1/N) Σ_i Σ_k (p_ik − y_ik)²

    ``y_i`` is the one-hot encoding of the actual class. The sum runs over the
    whole probability vector, not only the true class, so the score rewards both
    calibration and sharpness. Lower is better; the range is ``[0, 2]``. A
    predictor that puts all its mass on the right class scores 0. No rows give
    NaN.
    """
    p, y = _validate(probs, labels)
    if p.shape[0] == 0:
        return float("nan")
    onehot = np.zeros_like(p)
    onehot[np.arange(p.shape[0]), y] = 1.0
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def log_loss(
    probs: np.ndarray,
    labels: np.ndarray,
    eps: float = OUTCOME_PROB_EPS,
) -> float:
    """The multi-class cross-entropy (log-loss), clipped at ``eps``.

        log_loss = −(1/N) Σ_i log( clip(p_i,true_class) )

    The probabilities are clipped to ``[eps, 1]`` before the log, so a confident
    wrong prediction (``p → 0`` on the true class) costs the finite penalty
    ``−log(eps)``, not ``+inf``. Lower is better; a predictor that puts all its
    mass on the right class scores about 0. No rows give NaN.
    """
    p, y = _validate(probs, labels)
    if p.shape[0] == 0:
        return float("nan")
    true_p = p[np.arange(p.shape[0]), y]
    true_p = np.clip(true_p, eps, 1.0)
    return float(-np.mean(np.log(true_p)))


def evaluate_distributions(
    probs: np.ndarray,
    labels: np.ndarray,
    *,
    n_bins: int = DEFAULT_N_BINS,
    eps: float = OUTCOME_PROB_EPS,
) -> dict:
    """Every metric of predicted distributions against the actual labels, at once.

    The inputs are an ``(n, c)`` predicted-probability matrix and the ``(n,)``
    integer labels of the actual outcomes. Returns a dict with ``ece``,
    ``brier``, ``log_loss``, ``accuracy`` (the argmax hit rate),
    ``reliability_curve`` (the per-bin points) and ``n``. No rows give NaN
    metrics and an empty curve.
    """
    p, y = _validate(probs, labels)
    if p.shape[0] == 0:
        return {
            "n": 0,
            "ece": float("nan"),
            "brier": float("nan"),
            "log_loss": float("nan"),
            "accuracy": float("nan"),
            "reliability_curve": [],
        }
    _, correct = _confidence_and_correct(p, y)
    return {
        "n": int(p.shape[0]),
        "ece": expected_calibration_error(p, y, n_bins=n_bins),
        "brier": brier_score(p, y),
        "log_loss": log_loss(p, y, eps=eps),
        "accuracy": float(correct.mean()),
        "reliability_curve": reliability_curve(p, y, n_bins=n_bins),
    }


# ---------------------------------------------------------------------------
# 3. The league-average baseline (the floor of the ablation)
# ---------------------------------------------------------------------------
def league_average_distribution(
    train_labels: Sequence[int],
    n_classes: int,
    *,
    eps: float = OUTCOME_PROB_EPS,
) -> np.ndarray:
    """The league-average predictor: the outcome frequencies of the training rows.

    It counts each class in ``train_labels`` and scales the counts to a
    probability vector. The baseline uses no features and no engine, so a model
    that beats it carries signal. Every class starts at a pseudo-count of
    ``eps`` (additive smoothing), so a class never seen in training gets a tiny
    mass and the log-loss stays finite when it appears in the held-out rows. A
    label outside ``[0, n_classes)`` is ignored.

    Returns a ``(n_classes,)`` probability vector.
    """
    counts = np.full(n_classes, eps, dtype=float)
    for c in train_labels:
        ci = int(c)
        if 0 <= ci < n_classes:
            counts[ci] += 1.0
    return counts / counts.sum()


# ---------------------------------------------------------------------------
# 4. The walk-forward ablation harness
# ---------------------------------------------------------------------------
def _column(df: Any, col: str) -> list:
    """A column of ``df`` as a list (``df`` is a DataFrame or a mapping)."""
    return list(df[col])


def _subset_indices(seasons: np.ndarray, wanted) -> np.ndarray:
    """The row indices whose season is in ``wanted``."""
    return np.where(np.isin(seasons, np.asarray(wanted)))[0]


def walk_forward_ablation(
    df: Any,
    predict_fn: Callable[[Any, Any], Sequence[float]],
    classes: Sequence[str],
    *,
    label_col: str = OUTCOME_COL,
    feature_col: str = FEATURE_COL,
    season_col: str = SEASON_COL,
    extra_predictors: Mapping[str, Callable[[Any, Any], Sequence[float]]] | None = None,
    n_bins: int = DEFAULT_N_BINS,
    eps: float = OUTCOME_PROB_EPS,
) -> dict:
    """A walk-forward backtest of predicted distributions, with a league-average ablation.

    The folds come from :func:`walk_forward_folds`: each fold trains on every
    earlier season and tests on the next season. On each held-out row the
    harness scores two or more predicted distributions against the actual
    outcome:

    * **model**: ``predict_fn(train_subset, feature)``, the model under test.
      Any callable that returns a probability vector over ``classes`` works.
    * **baseline**: the league-average predictor
      (:func:`league_average_distribution` over the training rows' labels).
    * each of **extra_predictors**: a named callable with the same signature,
      e.g. the model with one engine replaced by the league average. Each one is
      reported against the same baseline.

    ``train_subset`` is a dict: ``{feature_col: [the training features],
    "label": the training class indices (-1 for a label outside ``classes``),
    "classes": the vocabulary}``. A held-out row whose label is outside
    ``classes`` is skipped.

    :func:`evaluate_distributions` scores each predictor over the held-out rows
    of every fold pooled. The ablation deltas are ``baseline − predictor`` on the
    lower-is-better metrics (Brier, log-loss, ECE) and ``predictor − baseline``
    on the accuracy, so a positive delta means the predictor beats the baseline.

    Parameters
    ----------
    df:
        In-memory rows with ``season``, ``feature`` and ``outcome`` (a string
        outcome label) columns: a DataFrame or a mapping of array-likes.
    predict_fn:
        ``(train_subset, feature) -> probability vector``: the model.
    classes:
        The ordered, unique outcome vocabulary: the probability columns.
    extra_predictors:
        An optional ``{name: predict_fn}`` of more predictors to score.

    Returns
    -------
    dict with:
        ``classes``   — the outcome vocabulary.
        ``folds``     — one dict per fold: ``test_season``, ``n_train``,
                        ``n_test`` (``n_test`` counts the skipped rows too).
        ``metrics``   — ``{predictor_name: evaluate_distributions(...)}`` over
                        all the held-out rows (always with ``"model"`` and
                        ``"baseline"``).
        ``ablation``  — ``{predictor_name: {ece, brier, log_loss, accuracy}}``,
                        the deltas against the baseline (positive = beats the
                        baseline); ``"model"`` is always present.
    """
    classes = list(classes)
    cls_idx = {c: i for i, c in enumerate(classes)}
    n_classes = len(classes)
    extra_predictors = dict(extra_predictors or {})

    seasons = np.asarray(_column(df, season_col), dtype=int).ravel()
    features = _column(df, feature_col)
    raw_labels = _column(df, label_col)
    # Map the string outcome labels to class indices once (-1 = not in classes).
    labels = np.array([cls_idx.get(str(lbl), -1) for lbl in raw_labels], dtype=int)

    folds_spec = walk_forward_folds(seasons)

    # One probability list per predictor and one label list, pooled across the
    # folds, so every held-out row counts the same.
    arms = ["model", "baseline", *extra_predictors.keys()]
    pooled_probs: dict[str, list] = {a: [] for a in arms}
    pooled_labels: list[int] = []
    per_fold: list[dict] = []

    for train_seasons, test_season in folds_spec:
        tr = _subset_indices(seasons, train_seasons)
        te = _subset_indices(seasons, [test_season])
        if te.size == 0:
            continue

        train_labels = labels[tr]
        # Only the training rows with a known label feed the baseline frequencies.
        baseline_vec = league_average_distribution(
            [c for c in train_labels.tolist() if c >= 0],
            n_classes,
            eps=eps,
        )

        # The training subset the injected predictors read: the features, the
        # label indices and the vocabulary.
        train_subset = {
            feature_col: [features[i] for i in tr],
            "label": train_labels,
            "classes": classes,
        }

        for j in te:
            y = int(labels[j])
            if y < 0:
                continue  # the held-out outcome is outside the vocabulary: skip the row
            x = features[j]
            pooled_labels.append(y)

            pooled_probs["model"].append(np.asarray(predict_fn(train_subset, x), dtype=float))
            pooled_probs["baseline"].append(baseline_vec)
            for name, fn in extra_predictors.items():
                pooled_probs[name].append(np.asarray(fn(train_subset, x), dtype=float))

        per_fold.append(
            {
                "test_season": int(test_season),
                "n_train": int(tr.size),
                "n_test": int(te.size),
            }
        )

    y_all = np.asarray(pooled_labels, dtype=int)
    metrics: dict[str, dict] = {}
    for name in arms:
        if pooled_probs[name]:
            probs = normalize_probs(np.vstack(pooled_probs[name]))
        else:
            probs = np.zeros((0, n_classes), dtype=float)
        metrics[name] = evaluate_distributions(probs, y_all, n_bins=n_bins, eps=eps)

    base = metrics["baseline"]
    ablation: dict[str, dict] = {}
    for name in arms:
        if name == "baseline":
            continue
        m = metrics[name]
        ablation[name] = {
            # the lower-is-better metrics: baseline - model (positive = better)
            "ece": base["ece"] - m["ece"],
            "brier": base["brier"] - m["brier"],
            "log_loss": base["log_loss"] - m["log_loss"],
            # the higher-is-better metric: model - baseline (positive = better)
            "accuracy": m["accuracy"] - base["accuracy"],
        }

    return {
        "classes": classes,
        "folds": per_fold,
        "metrics": metrics,
        "ablation": ablation,
    }
