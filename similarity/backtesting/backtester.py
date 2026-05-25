"""similarity/backtesting/backtester.py — SIM-220 probabilistic backtester.

The validation spine's gold-standard metrics. Where the SIM-076 harness
(:mod:`similarity.backtesting.recency_walk_forward`) scores **scalar** outcome
predictions with MAE / RMSE, this module generalizes the same walk-forward idea
to **probabilistic** PA-outcome prediction: the thing being scored is a
*probability distribution over discrete outcomes* (e.g. the dict returned by
``PlayPoolSampler.sample_batted_ball(..., return_distribution=True)`` —
``{"single": 0.31, "field_out": 0.55, ...}``) against the held-out *actual*
outcome label.

What it computes
----------------
Given an ``(n, c)`` matrix of predicted class probabilities and the ``(n,)``
integer actual labels, it computes the three canonical calibration / accuracy
scores plus a plottable reliability curve:

* **ECE** — expected calibration error (binned). Bins the predicted
  *confidence* (the prob assigned to the predicted/argmax class) and measures
  the gap between mean confidence and empirical accuracy inside each bin,
  weighted by bin population. A perfectly-calibrated predictor → ~0.
* **Brier score** — the multi-class (vector) Brier: mean squared error between
  the predicted probability vector and the one-hot actual. Lower is better;
  range ``[0, 2]``.
* **log-loss** — multi-class cross-entropy with eps clipping so a confident
  wrong prediction (``p → 0`` on the true class) yields a large-but-finite
  penalty instead of ``+inf``.
* **reliability curve** — the per-bin ``(mean_confidence, empirical_accuracy,
  count)`` points underlying the ECE, suitable for a calibration / reliability
  diagram.

Ablation framework
------------------
The point of a backtester here is not just "how good is the full model" but
"how much does each engine's signal *contribute*". :func:`walk_forward_ablation`
runs the SIM-076 expanding-window folds (reusing :func:`walk_forward_folds`, no
new splitting logic) and, on each held-out window, scores the **full** model's
predicted distributions against a **league-average baseline** predictor — the
floor: predict the marginal outcome frequencies of the training window for
every test row, ignoring all features / engine signals. The metric *delta*
(baseline − model; positive = the model beats the floor) isolates the signal
contribution. Arbitrary additional named predictors (e.g. per-engine swaps:
"full model but with the pitcher engine replaced by the league average") can be
passed in the same call so each engine's marginal lift is reported side-by-side
against the same baseline.

Purity / testability
--------------------
The metric functions are pure NumPy over arrays — no DB, no engine, no FAISS.
The harness takes an **injectable prediction function** ``predict(train, x) ->
prob-vector`` (exactly mirroring the SIM-076 harness's injectable predictor),
so it is unit-tested end-to-end on synthetic distributions + labels.

Public API
----------
``OUTCOME_PROB_EPS``                                   — log-loss clip epsilon.
``probs_from_dicts(dicts, classes)``                  -> (n, c) ndarray
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

# Reuse the SIM-076 expanding-window splitter rather than reinventing it.
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

#: Clip epsilon for log-loss so a predicted probability of exactly 0 on the
#: true class yields ``-log(eps)`` (a large but finite penalty) instead of
#: ``+inf``. Mirrors sklearn's ``log_loss`` default magnitude.
OUTCOME_PROB_EPS = 1e-15

#: Default number of equal-width confidence bins for ECE / reliability curve.
DEFAULT_N_BINS = 10


# ---------------------------------------------------------------------------
# 1. Coercion helpers — turn predicted-distribution dicts into a prob matrix
# ---------------------------------------------------------------------------
def probs_from_dicts(
    dists: Sequence[Mapping[str, float]],
    classes: Sequence[str],
) -> np.ndarray:
    """Stack a sequence of outcome-probability dicts into an ``(n, c)`` matrix.

    Each dict is a sampler-style prediction (``{event: prob}``, e.g. the output
    of ``PlayPoolSampler.sample_batted_ball(..., return_distribution=True)``).
    ``classes`` fixes the column order (the closed outcome vocabulary). Events
    absent from a dict contribute ``0.0``; rows are renormalized to sum to 1 so
    a sampler dict that omits zero-prob events still yields a valid p-vector.

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
    """Row-normalize a ``(n, c)`` matrix to valid probability vectors.

    Rows whose mass is non-positive / non-finite fall back to a uniform
    distribution so downstream metrics never see a degenerate (all-zero) row.
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
    """Common shape / range validation for the metric functions."""
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
    """Per-row predicted-class confidence (max prob) and correctness (0/1)."""
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
    """Binned reliability-diagram points (mean confidence vs empirical accuracy).

    The predicted-class confidence of each row is dropped into one of
    ``n_bins`` equal-width bins on ``[0, 1]``. For each **non-empty** bin we
    emit ``{bin_lo, bin_hi, count, mean_confidence, accuracy, gap}`` where
    ``accuracy`` is the empirical fraction of correct argmax predictions in the
    bin and ``gap = |mean_confidence - accuracy|``. A well-calibrated model lies
    on the diagonal (``mean_confidence ≈ accuracy``); these points are exactly
    what a reliability diagram plots and what :func:`expected_calibration_error`
    aggregates.

    Bins are half-open ``[lo, hi)`` except the last, which includes 1.0.
    """
    p, y = _validate(probs, labels)
    edges = _bin_edges(n_bins)
    if p.shape[0] == 0:
        return []
    confidence, correct = _confidence_and_correct(p, y)
    # np.digitize: index in [1, n_bins]; clamp the right edge (==1.0) into the
    # last bin so a perfect-confidence prediction is counted.
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
    """Expected Calibration Error (binned, confidence-based).

        ECE = Σ_b (n_b / N) · |acc_b − conf_b|

    over ``n_bins`` equal-width confidence bins, where ``conf_b`` is the mean
    predicted-class confidence in bin ``b`` and ``acc_b`` its empirical
    accuracy. A perfectly-calibrated predictor (confidence == accuracy in every
    bin) → ~0; a systematically over/under-confident one → larger. Range
    ``[0, 1]``.
    """
    p, y = _validate(probs, labels)
    if p.shape[0] == 0:
        return float("nan")
    n = p.shape[0]
    curve = reliability_curve(p, y, n_bins=n_bins)
    return float(sum(pt["count"] / n * pt["gap"] for pt in curve))


def brier_score(probs: np.ndarray, labels: np.ndarray) -> float:
    """Multi-class (vector) Brier score.

        Brier = (1/N) Σ_i Σ_k (p_ik − y_ik)²

    where ``y_i`` is the one-hot encoding of the actual class. This is the sum
    of squared errors of the full probability vector (not just the true-class
    component), so it rewards both calibration and sharpness. Lower is better;
    range ``[0, 2]``. A predictor that always puts all mass on the correct class
    scores 0.
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
    """Multi-class cross-entropy (log-loss) with eps clipping.

        log_loss = −(1/N) Σ_i log( clip(p_i,true_class) )

    Probabilities are clipped to ``[eps, 1]`` *before* taking the log so a
    confident-wrong prediction (``p → 0`` on the true class) yields the finite
    penalty ``−log(eps)`` instead of ``+inf``. Lower is better; a predictor that
    puts all mass on the correct class scores ~0.
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
    """Score predicted distributions against actual labels — all metrics at once.

    Returns a dict with ``ece``, ``brier``, ``log_loss``, ``accuracy`` (argmax
    top-1), ``reliability_curve`` (the plottable per-bin points), and ``n``.
    The inputs are an ``(n, c)`` predicted-probability matrix and the ``(n,)``
    integer actual labels; nothing here touches a DB.
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
# 3. League-average baseline (the ablation floor)
# ---------------------------------------------------------------------------
def league_average_distribution(
    train_labels: Sequence[int],
    n_classes: int,
    *,
    eps: float = OUTCOME_PROB_EPS,
) -> np.ndarray:
    """The league-average predictor's distribution: training marginal frequencies.

    Counts each class in ``train_labels`` and normalizes to a probability
    vector — the empirical outcome mix of the *training* window. This is the
    ablation **floor**: it uses no features and no engine signals, so any model
    that beats it is demonstrating real signal. Laplace-smoothed by ``eps`` so a
    class never seen in training still gets a tiny non-zero mass (keeps log-loss
    finite if it appears in the held-out window).

    Returns a ``(n_classes,)`` probability vector.
    """
    counts = np.full(n_classes, eps, dtype=float)
    for c in train_labels:
        ci = int(c)
        if 0 <= ci < n_classes:
            counts[ci] += 1.0
    return counts / counts.sum()


# ---------------------------------------------------------------------------
# 4. Walk-forward ablation harness
# ---------------------------------------------------------------------------
def _column(df: Any, col: str) -> list:
    """Return a column of ``df`` as a list (DataFrame / mapping agnostic)."""
    return list(df[col])


def _subset_indices(seasons: np.ndarray, wanted) -> np.ndarray:
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
    """Walk-forward probabilistic backtest with a league-average ablation.

    Reuses the SIM-076 expanding-window splitter (:func:`walk_forward_folds`):
    each fold trains on all strictly-prior seasons and evaluates on the next
    held-out season. On every fold and held-out row we score three (or more)
    predicted distributions against the actual outcome:

    * **model** — ``predict_fn(train, feature)``: the full model under test
      (e.g. a sampler-backed k-NN distribution). The harness only requires a
      callable returning a probability vector over ``classes``, so it is
      DB-free and synthetic-data testable.
    * **baseline** — the league-average predictor
      (:func:`league_average_distribution` over the *training* window's labels),
      the floor.
    * any **extra_predictors** — named callables with the same signature, e.g.
      per-engine swaps ("model with engine X replaced by the league average"),
      so each engine's marginal contribution is reported against the same floor.

    For each predictor the pooled-across-folds metrics (ECE / Brier / log-loss /
    accuracy + reliability curve) are computed via :func:`evaluate_distributions`.
    The ablation ``deltas`` report ``baseline − predictor`` for the
    lower-is-better metrics (Brier, log-loss, ECE) and ``predictor − baseline``
    for accuracy, so a **positive delta means the predictor beats the floor**.

    Parameters
    ----------
    df:
        In-memory rows with ``season``, ``feature`` and a discrete ``outcome``
        (string event label) column. (DataFrame or mapping of array-likes.)
    predict_fn:
        ``(train_subset, feature) -> prob-vector`` for the full model.
    classes:
        Ordered, unique outcome vocabulary defining the probability columns.
    extra_predictors:
        Optional ``{name: predict_fn}`` for per-engine ablation arms.

    Returns
    -------
    dict with:
        ``classes``   — the outcome vocabulary used.
        ``folds``     — per-fold dict: ``test_season``, ``n_train``, ``n_test``.
        ``metrics``   — ``{predictor_name: evaluate_distributions(...)}`` pooled
                        over all held-out rows (always includes ``"model"`` and
                        ``"baseline"``).
        ``ablation``  — ``{predictor_name: {ece, brier, log_loss, accuracy}}``
                        deltas vs the baseline (positive == beats the floor);
                        ``"model"`` is always present.
    """
    classes = list(classes)
    cls_idx = {c: i for i, c in enumerate(classes)}
    n_classes = len(classes)
    extra_predictors = dict(extra_predictors or {})

    seasons = np.asarray(_column(df, season_col), dtype=int).ravel()
    features = _column(df, feature_col)
    raw_labels = _column(df, label_col)
    # Map string outcome labels to class indices once.
    labels = np.array([cls_idx.get(str(lbl), -1) for lbl in raw_labels], dtype=int)

    folds_spec = walk_forward_folds(seasons)

    # One probability list + label list accumulator per predictor, pooled across
    # folds so every held-out row contributes equally.
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
        # Only train rows with a recognized label feed the baseline marginal.
        baseline_vec = league_average_distribution(
            [c for c in train_labels.tolist() if c >= 0],
            n_classes,
            eps=eps,
        )

        # Build a lightweight train subset the injected predictors can consume:
        # a mapping of the columns they need (feature + outcome label index).
        train_subset = {
            feature_col: [features[i] for i in tr],
            "label": train_labels,
            "classes": classes,
        }

        for j in te:
            y = int(labels[j])
            if y < 0:
                continue  # held-out outcome outside the vocabulary; skip row
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
            # lower-is-better metrics: baseline - model  (positive == better)
            "ece": base["ece"] - m["ece"],
            "brier": base["brier"] - m["brier"],
            "log_loss": base["log_loss"] - m["log_loss"],
            # higher-is-better: model - baseline (positive == better)
            "accuracy": m["accuracy"] - base["accuracy"],
        }

    return {
        "classes": classes,
        "folds": per_fold,
        "metrics": metrics,
        "ablation": ablation,
    }
