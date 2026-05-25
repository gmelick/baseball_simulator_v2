"""similarity/backtesting/recency_walk_forward.py — SIM-076 walk-forward harness.

Validates that the SIM-076 ``recency_weight`` column actually improves the
**out-of-sample** fit of similarity-based outcome prediction. The companion
materialized column is produced by
``pipeline.batch.player_profile_computor.recency_weight`` (2.0 for the most
recent two seasons, x0.75/season decay, floor 0.25, relative to the build's
reference season); this module does *not* recompute weights — it consumes
whatever ``recency_weight`` is already attached to each pooled row, exactly as
SIM-220's broader backtester will feed it real data.

Design goals
------------
* **In-memory only.** Inputs are a pandas ``DataFrame`` (or anything with the
  required columns) — never a live DB — so the harness is trivially
  unit-testable and reusable.
* **Composable.** Each piece (fold generation, single prediction, full eval)
  is a standalone, importable function with no hidden global state.
* **Simple, documented math.** The predictor is a deliberately plain k-NN over
  the training rows. The contribution of SIM-076 is the *recency weighting*,
  not a state-of-the-art model, so the comparison stays interpretable: the only
  thing that changes between the "weighted" and "baseline" arms is whether
  ``recency_weight`` participates in the neighbour averaging.

Required columns / fields per row
---------------------------------
``season``         int   — the season the row came from.
``feature``        array — the feature vector (1-D, fixed length per call).
``outcome``        float — the scalar outcome being predicted.
``recency_weight`` float — SIM-076 sampling weight (peak 2.0 ... floor 0.25).

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

# Column names the harness reads. Centralized so SIM-220 can adapt if the
# pooled-row schema is renamed without hunting through the body.
SEASON_COL = "season"
FEATURE_COL = "feature"
OUTCOME_COL = "outcome"
WEIGHT_COL = "recency_weight"

# Default neighbourhood size for the k-NN predictor. Small by design: the point
# is a transparent, weightable average, not a tuned model.
DEFAULT_K = 8


# ---------------------------------------------------------------------------
# 1. Fold generation
# ---------------------------------------------------------------------------
def walk_forward_folds(
    seasons: Iterable[int],
) -> list[tuple[tuple[int, ...], int]]:
    """Expanding-window walk-forward folds over distinct ``seasons``.

    For each season (after the first) we emit a fold whose **train** set is all
    *strictly prior* seasons and whose **test** is that single season. This is
    the classic time-series expanding window: no future information ever leaks
    into the training set, so it is a faithful proxy for "what could we have
    known when this season was the present?".

    Parameters
    ----------
    seasons:
        Any iterable of season ints. Duplicates are collapsed and the result is
        sorted ascending, so callers may pass a raw column.

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
# 2. Single prediction (k-NN, optionally recency-weighted)
# ---------------------------------------------------------------------------
def _stack_features(rows: Any) -> np.ndarray:
    """Return an ``(n, d)`` float array from a frame's / mapping's feature col."""
    feats = rows[FEATURE_COL]
    # Works for a pandas Series of array-likes, a list, or an ndarray of arrays.
    return np.asarray([np.asarray(f, dtype=float).ravel() for f in feats], dtype=float)


def recency_weighted_prediction(
    train_df: Any,
    query: Sequence[float] | np.ndarray,
    *,
    weighted: bool,
    k: int = DEFAULT_K,
) -> float:
    """Predict an outcome for ``query`` via a (recency-)weighted k-NN average.

    The predictor finds the ``k`` nearest training rows to ``query`` in
    Euclidean feature space and returns a weighted average of their outcomes.

    The weighting scheme is where SIM-076 enters:

    * **Similarity weight** — always applied. Each neighbour gets
      ``1 / (eps + distance)`` so closer comparables count more. This is the
      "k-NN" part and is identical across both arms of the comparison.
    * **Recency weight** — applied only when ``weighted=True``. The neighbour's
      similarity weight is *multiplied* by its SIM-076 ``recency_weight``, so
      recent comparables are up-weighted exactly as the materialized sampling
      column intends. When ``weighted=False`` the recency column is ignored
      entirely, giving the baseline (recency-agnostic) prediction.

    Holding the similarity term fixed and toggling only the recency term means
    any error difference between the two arms is attributable to recency
    weighting alone — which is precisely what the eval is trying to measure.

    Parameters
    ----------
    train_df:
        In-memory training rows exposing ``feature``, ``outcome`` and
        ``recency_weight`` columns/keys.
    query:
        The feature vector to predict an outcome for.
    weighted:
        If True, fold ``recency_weight`` into the neighbour weights; if False,
        ignore it (uniform-recency baseline).
    k:
        Neighbourhood size (capped at the number of training rows).

    Returns
    -------
    float — the predicted outcome. ``nan`` if there are no training rows.
    """
    feats = _stack_features(train_df)
    if feats.shape[0] == 0:
        return float("nan")

    outcomes = np.asarray(train_df[OUTCOME_COL], dtype=float).ravel()
    rec_w = np.asarray(train_df[WEIGHT_COL], dtype=float).ravel()
    q = np.asarray(query, dtype=float).ravel()

    # Euclidean distance from the query to every training row.
    dists = np.linalg.norm(feats - q[None, :], axis=1)

    # Nearest-k indices (argpartition is O(n); ties broken arbitrarily but
    # deterministically for a fixed input).
    kk = int(min(k, dists.shape[0]))
    nn = np.argpartition(dists, kk - 1)[:kk]

    eps = 1e-9
    sim_w = 1.0 / (eps + dists[nn])  # closer => larger weight
    if weighted:
        sim_w = sim_w * rec_w[nn]  # SIM-076 recency up-weighting

    total = sim_w.sum()
    if total <= 0 or not np.isfinite(total):
        # Degenerate weights -> fall back to a plain mean of the neighbours.
        return float(np.mean(outcomes[nn]))
    return float(np.dot(sim_w, outcomes[nn]) / total)


# ---------------------------------------------------------------------------
# 3. Metrics
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
# 4. Full walk-forward evaluation
# ---------------------------------------------------------------------------
def walk_forward_recency_eval(
    df: Any,
    *,
    metric: str = "mae",
    k: int = DEFAULT_K,
) -> dict:
    """Run the expanding-window folds and compare weighted vs unweighted error.

    For each fold (see :func:`walk_forward_folds`) the harness:

    1. Trains on every row from the strictly-prior seasons.
    2. For each test-season row, predicts its outcome twice — once with
       recency weighting on (``weighted=True``) and once off — using
       :func:`recency_weighted_prediction`.
    3. Scores both prediction vectors against the true test outcomes with the
       chosen ``metric``.

    Lower error is better, so the per-fold and aggregate ``improvement`` is
    ``unweighted_error - weighted_error`` (positive => recency helped).

    Parameters
    ----------
    df:
        In-memory pooled rows with ``season``, ``feature``, ``outcome`` and
        ``recency_weight`` columns.
    metric:
        ``"mae"`` (default) or ``"rmse"``.
    k:
        k-NN neighbourhood size, passed through to the predictor.

    Returns
    -------
    dict with keys:
        ``metric``     — the metric name used.
        ``folds``      — list of per-fold dicts: ``test_season``,
                         ``n_train``, ``n_test``, ``weighted_error``,
                         ``unweighted_error``, ``improvement``.
        ``aggregate``  — dict of pooled-across-all-test-rows
                         ``weighted_error``, ``unweighted_error``,
                         ``improvement`` plus ``n_test_total``.
        ``improvement``— convenience copy of the aggregate improvement.
    """
    if metric not in _METRICS:
        raise ValueError(f"unknown metric {metric!r}; choose from {sorted(_METRICS)}")
    score = _METRICS[metric]

    seasons_all = np.asarray(df[SEASON_COL], dtype=int).ravel()
    folds = walk_forward_folds(seasons_all)

    per_fold: list[dict] = []
    # Pooled (concatenated) predictions across folds for the aggregate metric so
    # every test row contributes equally regardless of season size.
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
    """Boolean-mask ``df`` into a column dict the predictor can consume.

    We return a plain dict-of-columns rather than depending on pandas indexing,
    so the harness works whether ``df`` is a DataFrame or a mapping of
    array-likes (keeping SIM-220's call site flexible).
    """
    out: dict[str, Any] = {}
    for col in (SEASON_COL, FEATURE_COL, OUTCOME_COL, WEIGHT_COL):
        values = df[col]
        seq = list(values)  # array-likes, Series, and lists all iterate fine
        out[col] = [seq[i] for i in range(len(seq)) if mask[i]]
    return out
