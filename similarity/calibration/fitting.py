"""The population fits behind every calibrated engine constant.

Each function reads a feature matrix of profiles and returns one fitted value:

- ``calibrate_sigma``: the bandwidth of a Gaussian kernel (RBF, a score of
  ``exp(-distance² / (2·sigma²))``) that puts the median random pair at the
  target score.
- ``calibrate_arsenal_gamma``: the same anchor for the pitcher's arsenal
  distance (the Wasserstein-2 distance between two pitch mixtures, W₂).
- ``calibrate_eb_prior``: the strength of the empirical-Bayes prior, the
  number of league-average observations a profile is shrunk toward.
- ``calibrate_reliability_weights``: one weight per feature, the
  season-to-season correlation of that feature inside one player.

``keep_defaults_on_thin_entries`` keeps an engine's default weight for a
feature that too few season pairs measure.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import numpy as np
from numpy.typing import NDArray

log = logging.getLogger("similarity_calibration")

#: A fitted reliability weight needs this many consecutive-season pairs; under
#: it the feature keeps its engine default (the outfield arm block's rule,
#: SIM-532).
RELIABILITY_MIN_PAIRS = 20


def calibrate_sigma(
    feature_matrix: NDArray[np.float64],
    weights: NDArray[np.float64] | None = None,
    target_median_score: float = 0.50,
    n_sample_pairs: int = 50_000,
    seed: int = 42,
    degenerate_value: float = 1.0,
) -> float:
    """Find the RBF sigma that puts the median random pair at the target score.

    Sigma sets how strict the kernel is. A median pair near 0.5 spreads the
    useful range of scores over [0.1, 0.9]: with a higher median every player
    looks alike, with a lower one only near-clones score above zero.

    The fit samples random pairs, takes the median weighted squared distance,
    and solves ``target = exp(-gamma · median)`` with ``sigma = sqrt(1 / 2gamma)``.

    Parameters
    ----------
    feature_matrix : (N, D) array
        Z-scored feature vectors for all profiles.
    weights : (D,) array or None
        Reliability weights that sum to 1.0. None means uniform.
    target_median_score : float
        The median score the kernel must give a random pair.
    n_sample_pairs : int
        The number of random pairs to sample.
    seed : int
        The seed of the pair sample.
    degenerate_value : float
        The value returned when the matrix cannot give a real sigma: no spread
        between pairs (``median_dist_sq <= 0``) or a target outside (0, 1).
        A caller that treats sigma as an override passes ``0.0``, the engines'
        keep-the-default sentinel (see ``CalibratorBase._fit_sigma``).

    Returns
    -------
    The fitted sigma.
    """
    n, d = feature_matrix.shape
    if weights is None:
        weights = np.ones(d) / d

    rng = np.random.default_rng(seed)
    n_pairs = min(n_sample_pairs, n * (n - 1) // 2)

    idx_a = rng.integers(0, n, size=n_pairs)
    idx_b = rng.integers(0, n, size=n_pairs)
    # Drop the self-pairs.
    mask = idx_a != idx_b
    idx_a, idx_b = idx_a[mask], idx_b[mask]

    diff = feature_matrix[idx_a] - feature_matrix[idx_b]
    diff = np.nan_to_num(diff, nan=0.0)
    mean_dist_sq = np.sum(weights[np.newaxis, :] * diff**2, axis=1)

    median_dist_sq = np.median(mean_dist_sq)

    # Solve: target = exp(-gamma * median_dist_sq)
    #        gamma = -ln(target) / median_dist_sq
    #        sigma = sqrt(1 / (2 * gamma))
    if median_dist_sq <= 0 or target_median_score <= 0 or target_median_score >= 1:
        log.warning("Cannot calibrate sigma: invalid inputs. Returning %.4g.", degenerate_value)
        return degenerate_value

    gamma = -np.log(target_median_score) / median_dist_sq
    sigma = np.sqrt(1.0 / (2.0 * gamma))

    return float(sigma)


def calibrate_arsenal_gamma(
    w2_distances: NDArray[np.float64],
    target_median_score: float = 0.50,
) -> float:
    """Find the gamma of ``score = exp(-gamma · W₂²)`` that puts the median
    pitcher pair at the target score.

    The pitcher engine turns gamma and the median into its linear
    ``ARSENAL_SCALE`` (``PitcherSimilarityEngine.apply_calibration``).

    Parameters
    ----------
    w2_distances : 1-D array
        Pairwise W₂ distances; a non-finite value is ignored. The engine's
        ``_arsenal_cache.finite_distances()`` returns them.
    target_median_score : float
        The median score the curve must give a random pair.

    Returns
    -------
    The fitted gamma; 0.173 when no distance is finite or the median is zero.
    """
    finite = w2_distances[np.isfinite(w2_distances)]
    if len(finite) == 0:
        log.warning("No finite W2 distances. Returning default gamma 0.173.")
        return 0.173

    median_dist = np.median(finite)
    if median_dist <= 0:
        return 0.173

    # Solve: target = exp(-γ × median²)
    #        γ = -ln(target) / median²
    gamma = -np.log(target_median_score) / (median_dist**2)
    return float(gamma)


def calibrate_eb_prior(
    feature_matrix: NDArray[np.float64],
    sample_sizes: NDArray[np.int64],
    min_sample: int = 100,
) -> float:
    """Estimate the empirical-Bayes prior strength (N_PRIOR) from the spread
    of the population.

    The shrinkage is ``alpha = N / (N + N_PRIOR)``: N_PRIOR is the number of
    league-average observations a profile's own data is weighed against. The
    estimate is the James-Stein ratio, within-player variance over
    between-player variance. When players differ a lot, N_PRIOR is small and
    a profile's own data counts early; when the noise is large, N_PRIOR is
    large.

    The within-player variance is approximated at the median sample size: a
    rate feature (mean in (0, 1)) uses the binomial ``p(1-p)/n``; any other
    feature uses its population variance over n.

    Parameters
    ----------
    feature_matrix : (N, D) array
        Raw (not z-scored) feature vectors, one per player-season.
    sample_sizes : (N,) array
        The plate appearances or pitches of each player-season.
    min_sample : int
        A row with a smaller sample is left out.

    Returns
    -------
    The median ratio over the features, clamped to [5, 200]; 50 with fewer
    than 20 rows or no finite ratio.
    """
    mask = sample_sizes >= min_sample
    X = feature_matrix[mask]

    if len(X) < 20:
        log.warning("Too few profiles for EB calibration. Returning default 50.")
        return 50.0

    total_var = np.nanvar(X, axis=0)
    pop_means = np.nanmean(X, axis=0)
    median_n = np.median(sample_sizes[mask])

    within_var = np.zeros_like(total_var)
    for j in range(X.shape[1]):
        p = pop_means[j]
        if 0 < p < 1:
            within_var[j] = p * (1 - p) / median_n
        else:
            within_var[j] = total_var[j] / median_n

    between_var = np.maximum(total_var - within_var, 1e-10)

    ratios = within_var / between_var
    # Drop the outliers: a feature whose between-player variance is near zero.
    ratios = ratios[np.isfinite(ratios) & (ratios < 1000)]

    if len(ratios) == 0:
        return 50.0

    n_prior = float(np.median(ratios))
    n_prior = max(5.0, min(n_prior, 200.0))

    return n_prior


def calibrate_reliability_weights(
    raw_feature_matrix: NDArray[np.float64],
    player_ids: NDArray[np.int64],
    n_splits: int = 100,
    seed: int = 42,
    seasons: NDArray[np.int64] | None = None,
) -> NDArray[np.float64]:
    """Compute one reliability weight per feature: the correlation of the
    feature between two seasons of one player.

    A feature that repeats from season to season carries more true talent and
    gets a higher weight. The profiles hold season aggregates, not pitches,
    so the season-to-season correlation stands in for a split-half one.

    Parameters
    ----------
    raw_feature_matrix : (N, D) array
        Feature vectors, one per player-season.
    player_ids : (N,) array
        The id of each row. A caller whose rows are finer than one per
        player-season (the fielder profiles: one per player, position and
        season) passes a composite id, so a pair never crosses positions.
    n_splits, seed : int
        Unused; kept for the callers that pass them.
    seasons : (N,) array, optional
        The season of each row. When given, the pairs are EVERY pair of
        consecutive seasons inside one id (2023 with 2024, 2024 with 2025): a
        gap forms no pair, and a duplicate season keeps the last row. When
        absent, the pair is the first two rows of the id in scan order,
        whatever their seasons (the rule for callers with one row per
        player-season).

    Returns
    -------
    (D,) array of weights in [0.1, 1]. A feature with under five valid pairs
    or no spread gets 0.5; all ones when fewer than five ids, or fewer than
    five pairs, have two rows.
    """
    n, d = raw_feature_matrix.shape

    unique_ids, counts = np.unique(player_ids, return_counts=True)
    multi_season = set(unique_ids[counts >= 2])

    if len(multi_season) < 20:
        log.warning(
            "Only %d multi-season players found. Reliability weights "
            "may be noisy. Consider loading more seasons.",
            len(multi_season),
        )
        if len(multi_season) < 5:
            return np.ones(d)

    player_rows: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        pid = int(player_ids[i])
        if pid in multi_season:
            player_rows[pid].append(i)

    rows_a, rows_b = [], []
    for _pid, row_indices in player_rows.items():
        if len(row_indices) < 2:
            continue
        if seasons is None:
            rows_a.append(raw_feature_matrix[row_indices[0]])
            rows_b.append(raw_feature_matrix[row_indices[1]])
            continue
        by_season = {int(seasons[i]): i for i in row_indices}
        for season, row in sorted(by_season.items()):
            following = by_season.get(season + 1)
            if following is not None:
                rows_a.append(raw_feature_matrix[row])
                rows_b.append(raw_feature_matrix[following])

    if len(rows_a) < 5:
        return np.ones(d)

    A = np.array(rows_a)
    B = np.array(rows_b)

    weights = np.zeros(d)
    for j in range(d):
        col_a = A[:, j]
        col_b = B[:, j]
        valid = ~(np.isnan(col_a) | np.isnan(col_b))
        if valid.sum() < 5:
            weights[j] = 0.5
            continue
        ca, cb = col_a[valid], col_b[valid]
        if np.std(ca) == 0 or np.std(cb) == 0:
            weights[j] = 0.5
            continue
        r = np.corrcoef(ca, cb)[0, 1]
        # A negative correlation is noise: clamp to [0, 1].
        weights[j] = max(0.0, min(float(r), 1.0))

    # A zero weight would make the feature invisible.
    weights = np.maximum(weights, 0.1)

    return weights


def reliable_pair_count(
    values: NDArray[np.float64],
    ids: NDArray[np.int64],
    seasons: NDArray[np.int64],
    row_ok: NDArray[np.bool_],
) -> int:
    """Count the consecutive-season pairs that fit one feature's reliability
    weight.

    A pair is two rows of one id in seasons s and s + 1, the rule of
    ``calibrate_reliability_weights`` (a duplicate season keeps the last row).
    The pair counts when the value is finite on both rows and ``row_ok``
    holds on both rows.
    """
    by_id: dict[int, dict[int, int]] = defaultdict(dict)
    for i in range(len(ids)):
        by_id[int(ids[i])][int(seasons[i])] = i
    count = 0
    for by_season in by_id.values():
        for season, row in by_season.items():
            following = by_season.get(season + 1)
            if following is None:
                continue
            if not (row_ok[row] and row_ok[following]):
                continue
            if np.isfinite(values[row]) and np.isfinite(values[following]):
                count += 1
    return count


def keep_defaults_on_thin_entries(
    weights: NDArray[np.float64],
    features: list[tuple[str, float]],
    matrix: NDArray[np.float64],
    ids: NDArray[np.int64],
    seasons: NDArray[np.int64],
    entries: dict[int, NDArray[np.bool_]],
    group: str,
) -> NDArray[np.float64]:
    """Overwrite each thin entry of ``weights`` with its engine default.

    ``entries`` maps a column index to the rows that may form a pair for it.
    An entry with fewer than ``RELIABILITY_MIN_PAIRS`` reliable pairs (see
    ``reliable_pair_count``) takes the default from ``features`` and logs a
    warning that names the column and the count. Without this rule the report
    would carry ``calibrate_reliability_weights``' 0.5 as a fitted weight for
    a column with no data (the board not loaded, or one season only).
    """
    fitted = weights.copy()
    for j, row_ok in entries.items():
        pairs = reliable_pair_count(matrix[:, j], ids, seasons, row_ok)
        if pairs >= RELIABILITY_MIN_PAIRS:
            continue
        name, default = features[j]
        fitted[j] = default
        log.warning(
            "Only %d reliable season-to-season pairs carry %s in the %s range block "
            "(%d needed); keeping the engine's default weight %.3f for it.",
            pairs,
            name,
            group,
            RELIABILITY_MIN_PAIRS,
            default,
        )
    return fitted
