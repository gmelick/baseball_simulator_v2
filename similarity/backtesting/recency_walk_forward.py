"""A walk-forward check of the recency weight on a k-nearest-neighbour prediction.

The harness tests whether the ``recency_weight`` column improves the
out-of-sample fit of a similarity-based prediction of a scalar outcome. The
pool builders write the column by the rule of
``pipeline.batch.profiles.pool_meta.recency_weight``: 2.0 for the two most
recent seasons, then ×0.75 for each older season, with a floor of 0.25,
relative to the build's reference season. This module does not compute the
weights; it reads the ``recency_weight`` already on each row.

Offline only
------------
No live code calls this module. The production calibration of the win
probability and the props runs on the binary path of
:mod:`simulation.prop_validation`. The functions here are tested and correct;
"offline" describes their use, not their state.

Design
------
* In memory only. The input is a pandas ``DataFrame``, or anything with the
  required columns, never a database.
* Each piece (the folds, one prediction, the full evaluation) is a standalone
  function with no global state.
* The predictor is a plain k-nearest-neighbour (k-NN) average over the
  training rows. The weighted arm and the baseline arm differ only in whether
  ``recency_weight`` joins the neighbour weights, so any difference in error
  comes from the recency weight.

Required columns per row
------------------------
``season``         int   — the season the row came from.
``feature``        array — the feature vector (1-D, one length per call).
``outcome``        float — the scalar outcome to predict.
``recency_weight`` float — the sampling weight (peak 2.0 ... floor 0.25).

Public API
----------
``walk_forward_folds(seasons)``                       -> list[(train, test)]
``recency_weighted_prediction(train_df, query, ...)`` -> float
``walk_forward_recency_eval(df, ...)``                -> dict
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

import numpy as np

__all__ = [
    "recency_weighted_prediction",
    "walk_forward_folds",
    "walk_forward_recency_eval",
]

# The column names the harness reads. The backtester imports them too.
SEASON_COL = "season"
FEATURE_COL = "feature"
OUTCOME_COL = "outcome"
WEIGHT_COL = "recency_weight"

# The default neighbourhood size of the k-NN predictor. It is small on purpose:
# the point is a plain average that a weight can change, not a tuned model.
DEFAULT_K = 8


# ---------------------------------------------------------------------------
# 1. The folds
# ---------------------------------------------------------------------------
def walk_forward_folds(
    seasons: Iterable[int],
) -> list[tuple[tuple[int, ...], int]]:
    """The expanding-window walk-forward folds over the distinct ``seasons``.

    Each season after the first gives one fold: the training set is every
    earlier season and the test is that one season. No later information
    reaches the training set, so each fold asks "what could we have known when
    this season was the present?".

    Parameters
    ----------
    seasons:
        Any iterable of season ints. The duplicates collapse and the result is
        sorted ascending, so a caller may pass a raw column.

    Returns
    -------
    list of ``(train_seasons, test_season)`` where ``train_seasons`` is a tuple
    of ints strictly less than ``test_season``. The earliest season yields no
    fold (nothing to train on), so ``len(folds) == n_distinct_seasons - 1``.

    Examples
    --------
    >>> walk_forward_folds([2021, 2022, 2023])
    [((2021,), 2022), ((2021, 2022), 2023)]
    """
    uniq = sorted({int(s) for s in seasons})
    folds: list[tuple[tuple[int, ...], int]] = []
    for i in range(1, len(uniq)):
        train = tuple(uniq[:i])
        test = uniq[i]
        folds.append((train, test))
    return folds


# ---------------------------------------------------------------------------
# 2. One prediction (k-NN, with or without the recency weight)
# ---------------------------------------------------------------------------
def _stack_features(rows: Any) -> np.ndarray:
    """The feature column of a frame or a mapping as an ``(n, d)`` float array."""
    feats = rows[FEATURE_COL]
    # A pandas Series of array-likes, a list and an ndarray of arrays all work.
    return np.asarray([np.asarray(f, dtype=float).ravel() for f in feats], dtype=float)


def recency_weighted_prediction(
    train_df: Any,
    query: Sequence[float] | np.ndarray,
    *,
    weighted: bool,
    k: int = DEFAULT_K,
) -> float:
    """Predict the outcome of ``query`` by a weighted k-NN average.

    The predictor finds the ``k`` training rows nearest to ``query`` (Euclidean
    distance on the features) and returns a weighted average of their outcomes.
    Two weights:

    * **The similarity weight**, always: each neighbour gets
      ``1 / (eps + distance)``, so a closer row counts more. Both arms of the
      comparison use it.
    * **The recency weight**, only when ``weighted=True``: the similarity weight
      is multiplied by the neighbour's ``recency_weight``, so a recent row counts
      more, as the pool's sampling column intends. With ``weighted=False`` the
      column is ignored: the baseline prediction.

    Only the recency term changes between the two arms, so any difference in
    error comes from the recency weight alone. That is what the evaluation
    measures.

    Parameters
    ----------
    train_df:
        In-memory training rows with ``feature``, ``outcome`` and
        ``recency_weight`` columns or keys.
    query:
        The feature vector to predict an outcome for.
    weighted:
        True multiplies the neighbour weights by ``recency_weight``; False
        ignores it (the baseline).
    k:
        The neighbourhood size (capped at the number of training rows).

    Returns
    -------
    float — the predicted outcome; ``nan`` when there are no training rows. When
    the weights add up to 0 or are not finite, the plain mean of the neighbours.
    """
    feats = _stack_features(train_df)
    if feats.shape[0] == 0:
        return float("nan")

    outcomes = np.asarray(train_df[OUTCOME_COL], dtype=float).ravel()
    rec_w = np.asarray(train_df[WEIGHT_COL], dtype=float).ravel()
    q = np.asarray(query, dtype=float).ravel()

    # The Euclidean distance from the query to every training row.
    dists = np.linalg.norm(feats - q[None, :], axis=1)

    # The k nearest rows. argpartition is O(n); it breaks a tie in a way that is
    # arbitrary but fixed for a given input.
    kk = int(min(k, dists.shape[0]))
    nn = np.argpartition(dists, kk - 1)[:kk]

    eps = 1e-9
    sim_w = 1.0 / (eps + dists[nn])  # closer => larger weight
    if weighted:
        sim_w = sim_w * rec_w[nn]  # the recency weight

    total = sim_w.sum()
    if total <= 0 or not np.isfinite(total):
        # The weights are unusable: fall back to the plain mean of the neighbours.
        return float(np.mean(outcomes[nn]))
    return float(np.dot(sim_w, outcomes[nn]) / total)


# ---------------------------------------------------------------------------
# 3. The metrics: the mean absolute error and the root-mean-square error
# ---------------------------------------------------------------------------
def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)))


def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


_METRICS: dict[str, Callable[[np.ndarray, np.ndarray], float]] = {
    "mae": _mae,
    "rmse": _rmse,
}


# ---------------------------------------------------------------------------
# 4. The full walk-forward evaluation
# ---------------------------------------------------------------------------
def walk_forward_recency_eval(
    df: Any,
    *,
    metric: str = "mae",
    k: int = DEFAULT_K,
) -> dict:
    """Run the expanding-window folds and compare the weighted with the unweighted error.

    For each fold (see :func:`walk_forward_folds`) the harness:

    1. trains on every row of the earlier seasons;
    2. predicts each test-season row twice with
       :func:`recency_weighted_prediction`: once with the recency weight
       (``weighted=True``) and once without;
    3. scores both predictions against the true test outcomes with ``metric``.

    Lower error is better, so the per-fold and the pooled ``improvement`` is
    ``unweighted_error - weighted_error``: positive means the recency weight
    helped.

    Parameters
    ----------
    df:
        In-memory pooled rows with ``season``, ``feature``, ``outcome`` and
        ``recency_weight`` columns.
    metric:
        ``"mae"`` (the mean absolute error, the default) or ``"rmse"`` (the
        root-mean-square error). Any other name raises ``ValueError``.
    k:
        The k-NN neighbourhood size, passed to the predictor.

    Returns
    -------
    dict with keys:
        ``metric``     — the metric name used.
        ``folds``      — list of per-fold dicts: ``test_season``,
                         ``n_train``, ``n_test``, ``weighted_error``,
                         ``unweighted_error``, ``improvement``.
        ``aggregate``  — over all the test rows pooled: ``weighted_error``,
                         ``unweighted_error``, ``improvement`` and
                         ``n_test_total``.
        ``improvement``— a copy of the pooled improvement.
    """
    if metric not in _METRICS:
        raise ValueError(f"unknown metric {metric!r}; choose from {sorted(_METRICS)}")
    score = _METRICS[metric]

    seasons_all = np.asarray(df[SEASON_COL], dtype=int).ravel()
    folds = walk_forward_folds(seasons_all)

    per_fold: list[dict] = []
    # The predictions of every fold, pooled for the aggregate metric, so every
    # test row counts the same whatever the size of its season.
    pooled_true: list[float] = []
    pooled_w_pred: list[float] = []
    pooled_u_pred: list[float] = []

    for train_seasons, test_season in folds:
        train_mask = np.isin(seasons_all, np.asarray(train_seasons))
        test_mask = seasons_all == test_season

        train_df = _subset(df, train_mask)
        test_df = _subset(df, test_mask)

        test_feats = _stack_features(test_df)
        test_y = np.asarray(test_df[OUTCOME_COL], dtype=float).ravel()

        w_pred = np.array(
            [recency_weighted_prediction(train_df, x, weighted=True, k=k) for x in test_feats]
        )
        u_pred = np.array(
            [recency_weighted_prediction(train_df, x, weighted=False, k=k) for x in test_feats]
        )

        per_fold.append(
            {
                "test_season": int(test_season),
                "n_train": int(train_mask.sum()),
                "n_test": int(test_mask.sum()),
                "weighted_error": score(test_y, w_pred),
                "unweighted_error": score(test_y, u_pred),
                "improvement": score(test_y, u_pred) - score(test_y, w_pred),
            }
        )

        pooled_true.extend(test_y.tolist())
        pooled_w_pred.extend(w_pred.tolist())
        pooled_u_pred.extend(u_pred.tolist())

    pt = np.asarray(pooled_true, dtype=float)
    pw = np.asarray(pooled_w_pred, dtype=float)
    pu = np.asarray(pooled_u_pred, dtype=float)

    if pt.size:
        agg_w = score(pt, pw)
        agg_u = score(pt, pu)
    else:
        agg_w = agg_u = float("nan")
    aggregate = {
        "n_test_total": int(pt.size),
        "weighted_error": agg_w,
        "unweighted_error": agg_u,
        "improvement": agg_u - agg_w,
    }

    return {
        "metric": metric,
        "folds": per_fold,
        "aggregate": aggregate,
        "improvement": aggregate["improvement"],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _subset(df: Any, mask: np.ndarray) -> dict:
    """The rows of ``df`` where ``mask`` is true, as a dict of column lists.

    A plain dict of columns, not pandas indexing, so the harness works whether
    ``df`` is a DataFrame or a mapping of array-likes.
    """
    out: dict[str, Any] = {}
    for col in (SEASON_COL, FEATURE_COL, OUTCOME_COL, WEIGHT_COL):
        values = df[col]
        seq = list(values)  # an array-like, a Series and a list all iterate
        out[col] = [seq[i] for i in range(len(seq)) if mask[i]]
    return out
