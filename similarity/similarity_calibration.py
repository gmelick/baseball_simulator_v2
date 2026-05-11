"""
similarity_calibration.py
==========================
Empirical calibration of all tunable constants in the similarity engines.

Replaces hand-picked magic numbers with values derived from the actual
population of pitcher/batter profiles and (optionally) validated against
downstream outcome prediction.

Three calibration tiers:

  Tier 1 — Population statistics (no ground truth needed):
    - RBF sigma:         calibrated so the median pairwise score = target
    - ARSENAL_NORM_SCALE: same principle for W2 distances
    - EB_N_PRIOR:        estimated from variance decomposition
    - Reliability weights: derived from split-half correlation at sample size

  Tier 2 — Downstream validation (needs outcome data):
    - Sub-score weights: optimized so that similarity predicts outcome similarity
    - Platoon weight shifts: validated against platoon-specific outcomes

  Tier 3 — Design choices (reported, not optimized):
    - Bats penalty: measured from spray-angle distribution overlap
    - MIN_CLUSTER_SIZE: reported as component-size distribution percentiles

Usage:
    from similarity_calibration import SimilarityCalibrator

    cal = SimilarityCalibrator(duckdb_path="/data/baseball_sim.duckdb")

    # Tier 1: derive from population (fast, no outcome data needed)
    params = cal.calibrate_from_population(seasons=[2022, 2023, 2024])
    print(params)

    # Tier 2: optimize weights against outcomes (slower, needs outcome pool)
    weights = cal.optimize_subscores(seasons=[2022, 2023, 2024])
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from similarity.engines.fielder_similarity import IF_PIVOT_FEATURES

log = logging.getLogger("similarity_calibration")


# ============================================================================
# Tier 1: RBF Sigma Calibration
# ============================================================================


def calibrate_sigma(
    feature_matrix: NDArray[np.float64],
    weights: NDArray[np.float64] | None = None,
    target_median_score: float = 0.50,
    n_sample_pairs: int = 50_000,
    seed: int = 42,
) -> float:
    """
    Find the RBF sigma that produces a target median similarity score
    across random pairs from the population.

    The idea: sigma controls how "picky" the kernel is. We want the
    median pair of MLB players to score around 0.4-0.5 so that the
    useful discrimination range spans [0.1, 0.9]. If median is too
    high, everyone looks similar. If too low, only clones score above
    zero.

    Method: sample random pairs, compute their mean squared distances,
    find the median distance, then solve for the sigma that maps that
    median distance to the target score.

    Parameters
    ----------
    feature_matrix : (N, D) array
        Z-score normalized feature vectors for all profiles.
    weights : (D,) array or None
        Reliability weights (summing to 1.0). If None, uniform.
    target_median_score : float
        Desired median RBF score across all pairs.
    n_sample_pairs : int
        Number of random pairs to sample.
    seed : int
        Random seed for reproducibility.

    Returns
    -------
    Calibrated sigma value.
    """
    n, d = feature_matrix.shape
    if weights is None:
        weights = np.ones(d) / d

    rng = np.random.default_rng(seed)
    n_pairs = min(n_sample_pairs, n * (n - 1) // 2)

    # Sample random pairs
    idx_a = rng.integers(0, n, size=n_pairs)
    idx_b = rng.integers(0, n, size=n_pairs)
    # Avoid self-pairs
    mask = idx_a != idx_b
    idx_a, idx_b = idx_a[mask], idx_b[mask]

    # Compute weighted mean squared distances
    diff = feature_matrix[idx_a] - feature_matrix[idx_b]
    diff = np.nan_to_num(diff, nan=0.0)
    mean_dist_sq = np.sum(weights[np.newaxis, :] * diff**2, axis=1)

    median_dist_sq = np.median(mean_dist_sq)

    # Solve: target = exp(-gamma * median_dist_sq)
    #        gamma = -ln(target) / median_dist_sq
    #        sigma = sqrt(1 / (2 * gamma))
    if median_dist_sq <= 0 or target_median_score <= 0 or target_median_score >= 1:
        log.warning("Cannot calibrate sigma: invalid inputs. Returning default 1.0.")
        return 1.0

    gamma = -np.log(target_median_score) / median_dist_sq
    sigma = np.sqrt(1.0 / (2.0 * gamma))

    return float(sigma)


def calibrate_arsenal_gamma(
    w2_distances: NDArray[np.float64],
    target_median_score: float = 0.50,
) -> float:
    """
    Find the ARSENAL_GAMMA that produces a target median arsenal
    similarity score using the squared-exponential form:

        score = exp(-γ × W₂²)

    Parameters
    ----------
    w2_distances : 1-D array
        Pairwise W2 distances (finite values only).
        Obtain via: engine._arsenal_cache.finite_distances()
    target_median_score : float
        Desired median score.

    Returns
    -------
    Calibrated gamma value.

    Example
    -------
        engine.precompute_arsenal_cache()
        dists = engine._arsenal_cache.finite_distances()
        optimal_gamma = calibrate_arsenal_gamma(dists, target_median_score=0.50)

        import pitcher_similarity
        pitcher_similarity.ARSENAL_GAMMA = optimal_gamma
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


# Keep the old function name as an alias for backwards compatibility
def calibrate_arsenal_norm_scale(
    w2_distances: NDArray[np.float64],
    target_median_score: float = 0.50,
) -> float:
    """Deprecated — use calibrate_arsenal_gamma() instead."""
    gamma = calibrate_arsenal_gamma(w2_distances, target_median_score)
    # Convert gamma to the old scale form for any legacy callers:
    #   exp(-d/scale) ≈ exp(-γd²) when scale = 1/(γ * median_d)
    finite = w2_distances[np.isfinite(w2_distances)]
    median_dist = np.median(finite) if len(finite) > 0 else 4.0
    if gamma > 0 and median_dist > 0:
        return 1.0 / (gamma * median_dist)
    return 5.8


# ============================================================================
# Tier 1: Empirical Bayes N_PRIOR Calibration
# ============================================================================


def calibrate_eb_prior(
    feature_matrix: NDArray[np.float64],
    sample_sizes: NDArray[np.int64],
    min_sample: int = 100,
) -> float:
    """
    Estimate the optimal Empirical Bayes prior strength (N_PRIOR) from
    the population variance decomposition.

    The shrinkage formula is: alpha = N / (N + N_PRIOR)
    N_PRIOR represents "how many observations of prior data are worth
    as much as the population mean." It can be estimated as:

        N_PRIOR = within-player variance / between-player variance

    This is the James-Stein shrinkage intensity: when between-player
    variance is high (players are very different), N_PRIOR is small
    and we trust individual data quickly. When within-player variance
    is high (lots of noise), N_PRIOR is large and we lean on the
    population mean longer.

    For this to work properly, you need multi-season data for the same
    players so we can separate within-player (season-to-season) variance
    from between-player variance.

    Parameters
    ----------
    feature_matrix : (N, D) array
        Raw (not z-scored) feature vectors, one per player-season.
    sample_sizes : (N,) array
        PA or pitch count per player-season.
    min_sample : int
        Minimum sample size for inclusion.

    Returns
    -------
    Calibrated N_PRIOR value.

    Notes
    -----
    If multi-season data is not available (single season), falls back to
    a heuristic based on the coefficient of variation of sample sizes.
    """
    mask = sample_sizes >= min_sample
    X = feature_matrix[mask]

    if len(X) < 20:
        log.warning("Too few profiles for EB calibration. Returning default 50.")
        return 50.0

    # Total variance of each feature across population
    total_var = np.nanvar(X, axis=0)

    # Approximate within-player variance as the variance that would remain
    # if all players were identical — estimated from the sampling variance
    # of a binomial/normal at the population mean with the median sample size.
    pop_means = np.nanmean(X, axis=0)
    median_n = np.median(sample_sizes[mask])

    # For rate statistics: sampling variance ≈ p(1-p)/n
    # For continuous stats (exit velo etc): use population std / sqrt(n) as proxy
    within_var = np.zeros_like(total_var)
    for j in range(X.shape[1]):
        p = pop_means[j]
        if 0 < p < 1:
            # Rate statistic
            within_var[j] = p * (1 - p) / median_n
        else:
            # Continuous statistic — use the population variance / n as proxy
            within_var[j] = total_var[j] / median_n

    between_var = np.maximum(total_var - within_var, 1e-10)

    # N_PRIOR = mean(within_var / between_var) across features
    ratios = within_var / between_var
    # Trim outlier ratios (features where between-var is near zero)
    ratios = ratios[np.isfinite(ratios) & (ratios < 1000)]

    if len(ratios) == 0:
        return 50.0

    n_prior = float(np.median(ratios))
    # Clamp to reasonable range
    n_prior = max(5.0, min(n_prior, 200.0))

    return n_prior


# ============================================================================
# Tier 1: Reliability Weight Calibration
# ============================================================================


def calibrate_reliability_weights(
    raw_feature_matrix: NDArray[np.float64],
    player_ids: NDArray[np.int64],
    n_splits: int = 100,
    seed: int = 42,
) -> NDArray[np.float64]:
    """
    Compute per-feature reliability weights via split-half correlation.

    For each feature, we randomly split each player's pitch/PA data into
    two halves, compute the feature from each half, and correlate the
    halves across players. Features with high split-half correlation
    (fast stabilization) get higher weights.

    When raw pitch-level data isn't available (we only have aggregated
    profiles), we approximate by using season-to-season correlation for
    players with multiple seasons as a proxy for reliability.

    Parameters
    ----------
    raw_feature_matrix : (N, D) array
        Feature vectors, one per player-season.
    player_ids : (N,) array
        Player ID for each row (to identify multi-season players).
    n_splits : int
        Number of random split-half iterations.
    seed : int
        Random seed.

    Returns
    -------
    (D,) array of reliability weights in [0, 1], where higher = more reliable.
    """
    n, d = raw_feature_matrix.shape

    # Find players with 2+ seasons
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

    # For each multi-season player, pair consecutive seasons
    # and compute the correlation of each feature across pairs.
    from collections import defaultdict

    player_rows: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        pid = int(player_ids[i])
        if pid in multi_season:
            player_rows[pid].append(i)

    # Build paired feature matrices
    rows_a, rows_b = [], []
    for _pid, row_indices in player_rows.items():
        if len(row_indices) >= 2:
            # Use first two seasons as the pair
            rows_a.append(raw_feature_matrix[row_indices[0]])
            rows_b.append(raw_feature_matrix[row_indices[1]])

    if len(rows_a) < 5:
        return np.ones(d)

    A = np.array(rows_a)
    B = np.array(rows_b)

    # Per-feature Pearson correlation between season pairs
    weights = np.zeros(d)
    for j in range(d):
        col_a = A[:, j]
        col_b = B[:, j]
        # Remove NaN pairs
        valid = ~(np.isnan(col_a) | np.isnan(col_b))
        if valid.sum() < 5:
            weights[j] = 0.5  # default for insufficient data
            continue
        ca, cb = col_a[valid], col_b[valid]
        if np.std(ca) == 0 or np.std(cb) == 0:
            weights[j] = 0.5
            continue
        r = np.corrcoef(ca, cb)[0, 1]
        # Clamp to [0, 1] — negative correlations indicate pure noise
        weights[j] = max(0.0, min(float(r), 1.0))

    # Ensure no zero weights (would make feature invisible)
    weights = np.maximum(weights, 0.1)

    return weights


# ============================================================================
# Tier 2: Sub-Score Weight Optimization
# ============================================================================


def optimize_subscore_weights(
    sub_scores: NDArray[np.float64],
    outcome_similarities: NDArray[np.float64],
    n_subscores: int = 4,
) -> NDArray[np.float64]:
    """
    Find sub-score weights that maximize the correlation between the
    composite similarity and an external measure of outcome similarity.

    The "outcome similarity" could be computed as the RBF similarity
    between two batters' actual outcome distributions (wOBA, K%, BB%,
    HR rate) in a held-out season — i.e., "did batters the engine
    called similar actually produce similar results?"

    Method: constrained optimization (weights ≥ 0, sum to 1) of the
    Pearson correlation between the weighted composite and the outcome
    similarity vector.

    Parameters
    ----------
    sub_scores : (N_pairs, n_subscores) array
        Per-sub-score similarities for each pair.
    outcome_similarities : (N_pairs,) array
        External outcome similarity for each pair.
    n_subscores : int
        Number of sub-scores.

    Returns
    -------
    Optimal weight vector (sums to 1.0).
    """
    from scipy.optimize import minimize

    def neg_correlation(w):
        composite = sub_scores @ w
        if np.std(composite) == 0:
            return 0.0
        return -np.corrcoef(composite, outcome_similarities)[0, 1]

    # Initial guess: uniform
    w0 = np.ones(n_subscores) / n_subscores

    # Constraints: weights sum to 1
    constraints = {"type": "eq", "fun": lambda w: w.sum() - 1.0}
    # Bounds: each weight in [0.02, 0.80]
    bounds = [(0.02, 0.80)] * n_subscores

    result = minimize(
        neg_correlation,
        w0,
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
    )

    if result.success:
        return result.x / result.x.sum()  # re-normalize for safety
    else:
        log.warning("Weight optimization did not converge. Returning uniform.")
        return w0


# ============================================================================
# Tier 3: Bats Penalty Measurement
# ============================================================================


def measure_bats_penalty(
    spray_angles_l: NDArray[np.float64],
    spray_angles_r: NDArray[np.float64],
) -> float:
    """
    Measure the bats-mismatch penalty empirically from the overlap of
    spray-angle distributions between LHB and RHB.

    Uses the Bhattacharyya coefficient (histogram overlap) as the
    penalty. Two identical distributions → 1.0. No overlap → 0.0.
    In practice, LHB and RHB spray distributions are roughly mirrored,
    so the overlap is moderate (~0.7-0.85).

    Parameters
    ----------
    spray_angles_l : 1-D array
        Spray angles (degrees) for all LHB balls in play.
    spray_angles_r : 1-D array
        Spray angles (degrees) for all RHB balls in play.

    Returns
    -------
    Bhattacharyya coefficient in [0, 1].
    """
    bins = np.linspace(-45, 45, 61)
    hist_l, _ = np.histogram(spray_angles_l, bins=bins, density=True)
    hist_r, _ = np.histogram(spray_angles_r, bins=bins, density=True)

    # Normalize to probability distributions
    hist_l = hist_l / (hist_l.sum() + 1e-10)
    hist_r = hist_r / (hist_r.sum() + 1e-10)

    # Bhattacharyya coefficient
    bc = np.sum(np.sqrt(hist_l * hist_r))
    return float(bc)


# ============================================================================
# Calibration Report
# ============================================================================


@dataclass
class CalibrationReport:
    """Holds all calibrated parameters with their derivation context."""

    # Tier 1
    sigma_discipline: float = 0.0
    sigma_batted_ball: float = 0.0
    sigma_platoon: float = 0.0
    sigma_power: float = 0.0
    sigma_command: float = 0.0  # pitcher
    sigma_results: float = 0.0  # pitcher
    arsenal_gamma: float = 0.0
    eb_n_prior_batter: float = 0.0
    eb_n_prior_pitcher: float = 0.0
    reliability_weights_discipline: NDArray | None = None
    reliability_weights_batted_ball: NDArray | None = None
    reliability_weights_power: NDArray | None = None
    reliability_weights_platoon: NDArray | None = None
    sigma_if_range: float = 0.0
    sigma_if_dp: float = 0.0
    sigma_if_errors: float = 0.0
    sigma_if_specialty: float = 0.0
    sigma_of_range: float = 0.0
    sigma_of_arm: float = 0.0
    sigma_of_stars: float = 0.0
    sigma_of_errors: float = 0.0
    sigma_baserunner_speed: float = 0.0
    sigma_baserunner_aggression: float = 0.0
    sigma_baserunner_success: float = 0.0
    eb_n_prior_baserunner: float = 0.0
    reliability_weights_if_range: NDArray | None = None
    reliability_weights_if_dp: NDArray | None = None
    reliability_weights_if_pivot: NDArray | None = None
    reliability_weights_if_error: NDArray | None = None
    reliability_weights_if_specialty: NDArray | None = None
    reliability_weights_of_range: NDArray | None = None
    reliability_weights_of_arm: NDArray | None = None
    reliability_weights_of_star: NDArray | None = None
    reliability_weights_of_error: NDArray | None = None
    reliability_weights_baserunner_speed: NDArray | None = None
    reliability_weights_baserunner_aggression: NDArray | None = None
    reliability_weights_baserunner_success: NDArray | None = None

    # Tier 2
    batter_subscores: NDArray | None = None
    pitcher_subscores: NDArray | None = None

    # Tier 3
    bats_penalty_lr: float = 0.0

    # Metadata
    n_profiles_used: int = 0
    seasons_used: list = field(default_factory=list)
    target_median_score: float = 0.50
    calibration_time_sec: float = 0.0

    def summary(self) -> str:
        lines = [
            "=" * 70,
            "SIMILARITY ENGINE CALIBRATION REPORT",
            "=" * 70,
            f"Profiles used: {self.n_profiles_used}",
            f"Seasons: {self.seasons_used}",
            f"Target median score: {self.target_median_score}",
            f"Calibration time: {self.calibration_time_sec:.1f}s",
            "",
            "--- Tier 1: Population-Derived Parameters ---",
            "",
            "  RBF Sigma (batter):",
            f"    Discipline:  {self.sigma_discipline:.4f}",
            f"    Batted Ball: {self.sigma_batted_ball:.4f}",
            f"    Platoon:     {self.sigma_platoon:.4f}",
            f"    Power:       {self.sigma_power:.4f}",
            "",
            "  RBF Sigma (pitcher):",
            f"    Command:     {self.sigma_command:.4f}",
            f"    Results:     {self.sigma_results:.4f}",
            "",
            "  RBF Sigma (infield):",
            f"    Range:       {self.sigma_if_range}",
            f"    DP:          {self.sigma_if_dp}",
            f"    Errors:      {self.sigma_if_errors}",
            f"    Specialty:   {self.sigma_if_specialty}",
            "",
            "  RBF Sigma (outfield):",
            f"    Range:       {self.sigma_of_range}",
            f"    Arm:         {self.sigma_of_arm}",
            f"    Stars:       {self.sigma_of_stars}",
            f"    Errors:      {self.sigma_of_errors}",
            "",
            "  RBF Sigma (baserunner):",
            f"    Speed:       {self.sigma_baserunner_speed:.4f}",
            f"    Aggression:  {self.sigma_baserunner_aggression:.4f}",
            f"    Success:     {self.sigma_baserunner_success:.4f}",
            "",
            f"  Arsenal Gamma:           {self.arsenal_gamma:.4f}",
            f"  EB N_PRIOR (batter):     {self.eb_n_prior_batter:.1f}",
            f"  EB N_PRIOR (pitcher):    {self.eb_n_prior_pitcher:.1f}",
            f"  EB N_PRIOR (baserunner): {self.eb_n_prior_baserunner:.1f}",
            "",
        ]

        if self.reliability_weights_discipline is not None:
            lines.append("  Reliability Weights (discipline):")
            from similarity.engines.batter_similarity import DISCIPLINE_FEATURES

            for (name, _), w in zip(
                DISCIPLINE_FEATURES, self.reliability_weights_discipline, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_batted_ball is not None:
            lines.append("  Reliability Weights (batted ball):")
            from similarity.engines.batter_similarity import BATTED_BALL_FEATURES

            for (name, _), w in zip(
                BATTED_BALL_FEATURES, self.reliability_weights_batted_ball, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_power is not None:
            lines.append("  Reliability Weights (power):")
            from similarity.engines.batter_similarity import POWER_FEATURES

            for (name, _), w in zip(POWER_FEATURES, self.reliability_weights_power, strict=False):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_platoon is not None:
            lines.append("  Reliability Weights (platoon):")
            from similarity.engines.batter_similarity import PLATOON_FEATURES

            for (name, _), w in zip(
                PLATOON_FEATURES, self.reliability_weights_platoon, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_if_range is not None:
            lines.append("  Reliability Weights (IF Range):")
            from similarity.engines.fielder_similarity import IF_RANGE_FEATURES

            for (name, _), w in zip(
                IF_RANGE_FEATURES, self.reliability_weights_if_range, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_if_dp is not None:
            lines.append("  Reliability Weights (IF DP):")
            from similarity.engines.fielder_similarity import IF_DP_FEATURES

            for (name, _), w in zip(IF_DP_FEATURES, self.reliability_weights_if_dp, strict=False):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_if_pivot is not None:
            lines.append("  Reliability Weights (IF Pivot):")
            from similarity.engines.fielder_similarity import IF_PIVOT_FEATURES

            for (name, _), w in zip(
                IF_PIVOT_FEATURES, self.reliability_weights_if_pivot, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_if_error is not None:
            lines.append("  Reliability Weights (IF Error):")
            from similarity.engines.fielder_similarity import IF_ERROR_FEATURES

            for (name, _), w in zip(
                IF_ERROR_FEATURES, self.reliability_weights_if_error, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_if_specialty is not None:
            lines.append("  Reliability Weights (IF Specialty):")
            from similarity.engines.fielder_similarity import IF_SPECIALTY_FEATURES

            for (name, _), w in zip(
                IF_SPECIALTY_FEATURES, self.reliability_weights_if_specialty, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_of_range is not None:
            lines.append("  Reliability Weights (OF Range):")
            from similarity.engines.fielder_similarity import OF_RANGE_FEATURES

            for (name, _), w in zip(
                OF_RANGE_FEATURES, self.reliability_weights_of_range, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_of_arm is not None:
            lines.append("  Reliability Weights (OF Arm):")
            from similarity.engines.fielder_similarity import OF_ARM_FEATURES

            for (name, _), w in zip(OF_ARM_FEATURES, self.reliability_weights_of_arm, strict=False):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_of_star is not None:
            lines.append("  Reliability Weights (OF Star):")
            from similarity.engines.fielder_similarity import OF_STAR_FEATURES

            for (name, _), w in zip(
                OF_STAR_FEATURES, self.reliability_weights_of_star, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_of_error is not None:
            lines.append("  Reliability Weights (OF Error):")
            from similarity.engines.fielder_similarity import OF_ERROR_FEATURES

            for (name, _), w in zip(
                OF_ERROR_FEATURES, self.reliability_weights_of_error, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_baserunner_speed is not None:
            lines.append("  Reliability Weights (Baserunner Speed):")
            from similarity.engines.baserunner_similarity import SPEED_FEATURES as BR_SPEED_FEATURES

            for (name, _), w in zip(
                BR_SPEED_FEATURES, self.reliability_weights_baserunner_speed, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_baserunner_aggression is not None:
            lines.append("  Reliability Weights (Baserunner Aggression):")
            from similarity.engines.baserunner_similarity import (
                AGGRESSION_FEATURES as BR_AGG_FEATURES,
            )

            for (name, _), w in zip(
                BR_AGG_FEATURES, self.reliability_weights_baserunner_aggression, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.reliability_weights_baserunner_success is not None:
            lines.append("  Reliability Weights (Baserunner Success):")
            from similarity.engines.baserunner_similarity import SUCCESS_FEATURES as BR_SUC_FEATURES

            for (name, _), w in zip(
                BR_SUC_FEATURES, self.reliability_weights_baserunner_success, strict=False
            ):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.batter_subscores is not None:
            lines.extend(
                [
                    "--- Tier 2: Optimized Sub-Score Weights ---",
                    "",
                    "  Batter: [discipline, batted_ball, platoon, power]",
                    f"    {self.batter_subscores}",
                    "",
                ]
            )

        if self.bats_penalty_lr > 0:
            lines.extend(
                [
                    "--- Tier 3: Design Choice Measurements ---",
                    "",
                    f"  Bats penalty (L↔R): {self.bats_penalty_lr:.4f}",
                    "",
                ]
            )

        lines.append("=" * 70)
        return "\n".join(lines)

    def as_dict(self) -> dict:
        """Export as a flat dictionary for programmatic use."""
        d = {
            "sigma_discipline": self.sigma_discipline,
            "sigma_batted_ball": self.sigma_batted_ball,
            "sigma_platoon": self.sigma_platoon,
            "sigma_power": self.sigma_power,
            "sigma_command": self.sigma_command,
            "sigma_results": self.sigma_results,
            "arsenal_gamma": self.arsenal_gamma,
            "eb_n_prior_batter": self.eb_n_prior_batter,
            "eb_n_prior_pitcher": self.eb_n_prior_pitcher,
            "bats_penalty_lr": self.bats_penalty_lr,
            "target_median_score": self.target_median_score,
            "sigma_if_range": self.sigma_if_range,
            "sigma_if_dp": self.sigma_if_dp,
            "sigma_if_errors": self.sigma_if_errors,
            "sigma_if_specialty": self.sigma_if_specialty,
            "sigma_of_range": self.sigma_of_range,
            "sigma_of_arm": self.sigma_of_arm,
            "sigma_of_stars": self.sigma_of_stars,
            "sigma_of_errors": self.sigma_of_errors,
            "sigma_baserunner_speed": self.sigma_baserunner_speed,
            "sigma_baserunner_aggression": self.sigma_baserunner_aggression,
            "sigma_baserunner_success": self.sigma_baserunner_success,
            "eb_n_prior_baserunner": self.eb_n_prior_baserunner,
        }
        if self.reliability_weights_discipline is not None:
            d["reliability_weights_discipline"] = self.reliability_weights_discipline.tolist()
        if self.reliability_weights_batted_ball is not None:
            d["reliability_weights_batted_ball"] = self.reliability_weights_batted_ball.tolist()
        if self.reliability_weights_power is not None:
            d["reliability_weights_power"] = self.reliability_weights_power.tolist()
        if self.reliability_weights_platoon is not None:
            d["reliability_weights_platoon"] = self.reliability_weights_platoon.tolist()
        if self.reliability_weights_if_range is not None:
            d["reliability_weights_if_range"] = self.reliability_weights_if_range.tolist()
        if self.reliability_weights_if_dp is not None:
            d["reliability_weights_if_dp"] = self.reliability_weights_if_dp.tolist()
        if self.reliability_weights_if_pivot is not None:
            d["reliability_weights_if_pivot"] = self.reliability_weights_if_pivot.tolist()
        if self.reliability_weights_if_error is not None:
            d["reliability_weights_if_error"] = self.reliability_weights_if_error.tolist()
        if self.reliability_weights_if_specialty is not None:
            d["reliability_weights_if_specialty"] = self.reliability_weights_if_specialty.tolist()
        if self.reliability_weights_of_range is not None:
            d["reliability_weights_of_range"] = self.reliability_weights_of_range.tolist()
        if self.reliability_weights_of_arm is not None:
            d["reliability_weights_of_arm"] = self.reliability_weights_of_arm.tolist()
        if self.reliability_weights_of_star is not None:
            d["reliability_weights_of_star"] = self.reliability_weights_of_star.tolist()
        if self.reliability_weights_of_error is not None:
            d["reliability_weights_of_error"] = self.reliability_weights_of_error.tolist()
        if self.reliability_weights_baserunner_speed is not None:
            d["reliability_weights_baserunner_speed"] = (
                self.reliability_weights_baserunner_speed.tolist()
            )
        if self.reliability_weights_baserunner_aggression is not None:
            d["reliability_weights_baserunner_aggression"] = (
                self.reliability_weights_baserunner_aggression.tolist()
            )
        if self.reliability_weights_baserunner_success is not None:
            d["reliability_weights_baserunner_success"] = (
                self.reliability_weights_baserunner_success.tolist()
            )
        if self.batter_subscores is not None:
            d["batter_subscores"] = self.batter_subscores.tolist()
        if self.pitcher_subscores is not None:
            d["pitcher_subscores"] = self.pitcher_subscores.tolist()
        return d


# ============================================================================
# Main Calibrator
# ============================================================================


class SimilarityCalibrator:
    """
    Orchestrates calibration of all similarity engine constants.

    Usage:
        cal = SimilarityCalibrator(duckdb_path="/data/baseball_sim.duckdb")
        report = cal.calibrate_from_population(seasons=[2022, 2023, 2024])
        print(report.summary())

        # Apply to engines:
        batter_engine = BatterSimilarityEngine(...)
        # Override constants before calling build():
        batter_similarity.RBF_SIGMA_DISCIPLINE = report.sigma_discipline
        batter_similarity.RBF_SIGMA_BATTED_BALL = report.sigma_batted_ball
        ...
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path

    def calibrate_from_population(
        self,
        seasons: list[int],
        target_median_score: float = 0.50,
    ) -> CalibrationReport:
        """
        Tier 1 calibration: derive all parameters from the population
        of profiles. No outcome data needed.
        """
        import duckdb

        t0 = time.time()
        report = CalibrationReport(
            seasons_used=seasons,
            target_median_score=target_median_score,
        )

        conn = duckdb.connect(self._duckdb_path, read_only=True)
        try:
            report = self._calibrate_batter_params(conn, seasons, target_median_score, report)
            report = self._calibrate_pitcher_params(conn, seasons, target_median_score, report)
            report = self._calibrate_fielder_params(conn, seasons, target_median_score, report)
            report = self._calibrate_baserunner_params(conn, seasons, target_median_score, report)
        finally:
            conn.close()

        report.calibration_time_sec = time.time() - t0
        return report

    def _calibrate_batter_params(
        self,
        conn: Any,
        seasons: list[int],
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Load batter profiles and calibrate all batter constants."""
        from similarity.engines.batter_similarity import (
            BATTED_BALL_FEATURES,
            DISCIPLINE_FEATURES,
            PLATOON_FEATURES,
            POWER_FEATURES,
        )

        sl = ", ".join(str(s) for s in seasons)
        rows = conn.execute(f"""
            SELECT
                batter_id, season, sample_pa,
                -- Discipline (7)
                first_pitch_take_rate, o_swing_rate, z_swing_rate,
                contact_rate, whiff_rate, k_rate, walk_rate,
                -- Batted ball (8)
                gb_rate, fb_rate, pull_rate, oppo_rate,
                avg_exit_velo, avg_launch_angle, hard_hit_rate, barrel_rate,
                -- Power (4)
                hr_rate, xba, xslg, max_exit_velo,
                -- Platoon vs R (7) — larger split, better for calibration
                o_swing_rate_vs_r, z_swing_rate_vs_r, whiff_rate_vs_r,
                walk_rate_vs_r, k_rate_vs_r, gb_rate_vs_r, barrel_rate_vs_r,
                sample_pa_vs_r
            FROM derived.batter_season_metrics
            WHERE season IN ({sl}) AND below_minimum_sample = FALSE
        """).fetchall()

        if not rows:
            log.warning("No batter profiles found for calibration.")
            return report

        report.n_profiles_used = len(rows)

        # Parse into arrays
        ids = np.array([r[0] for r in rows])
        sample_pa = np.array([r[2] for r in rows])

        n_disc = len(DISCIPLINE_FEATURES)
        n_bb = len(BATTED_BALL_FEATURES)
        n_pow = len(POWER_FEATURES)

        n_plat = len(PLATOON_FEATURES)

        col = 3  # first feature column index
        disc_raw = np.array([[r[col + i] or 0.0 for i in range(n_disc)] for r in rows])
        col += n_disc
        bb_raw = np.array([[r[col + i] or 0.0 for i in range(n_bb)] for r in rows])
        col += n_bb
        pow_raw = np.array([[r[col + i] or 0.0 for i in range(n_pow)] for r in rows])
        col += n_pow
        plat_raw = np.array([[r[col + i] or 0.0 for i in range(n_plat)] for r in rows])
        col += n_plat
        sample_pa_vs_r = np.array([r[col] or 0 for r in rows])

        # Filter platoon data to profiles with sufficient split sample
        plat_mask = sample_pa_vs_r >= 30
        plat_filtered = plat_raw[plat_mask]
        plat_ids = ids[plat_mask]

        # Z-score normalize
        def _zscore(mat):
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return (mat - m) / s

        disc_z = _zscore(disc_raw)
        bb_z = _zscore(bb_raw)
        pow_z = _zscore(pow_raw)

        # Calibrate sigmas
        report.sigma_discipline = calibrate_sigma(disc_z, target_median_score=target)
        report.sigma_batted_ball = calibrate_sigma(bb_z, target_median_score=target)
        report.sigma_power = calibrate_sigma(pow_z, target_median_score=target)

        if len(plat_filtered) >= 20:
            plat_z = _zscore(plat_filtered)
            report.sigma_platoon = calibrate_sigma(plat_z, target_median_score=target)
        else:
            report.sigma_platoon = report.sigma_discipline  # reasonable fallback
            log.warning("Insufficient platoon data for sigma calibration; using discipline sigma.")

        # Calibrate EB prior
        all_raw = np.hstack([disc_raw, bb_raw, pow_raw])
        report.eb_n_prior_batter = calibrate_eb_prior(all_raw, sample_pa)

        # Calibrate reliability weights
        report.reliability_weights_discipline = calibrate_reliability_weights(
            disc_raw,
            ids,
        )
        report.reliability_weights_batted_ball = calibrate_reliability_weights(
            bb_raw,
            ids,
        )
        report.reliability_weights_power = calibrate_reliability_weights(
            pow_raw,
            ids,
        )
        if len(plat_filtered) >= 20:
            report.reliability_weights_platoon = calibrate_reliability_weights(
                plat_filtered,
                plat_ids,
            )
        else:
            log.warning("Insufficient platoon data for reliability calibration; using defaults.")

        log.info(
            "Batter calibration complete: %d profiles, sigma_disc=%.3f, "
            "sigma_bb=%.3f, sigma_plat=%.3f, sigma_pow=%.3f, eb_prior=%.1f",
            len(rows),
            report.sigma_discipline,
            report.sigma_batted_ball,
            report.sigma_platoon,
            report.sigma_power,
            report.eb_n_prior_batter,
        )
        return report

    def _calibrate_pitcher_params(
        self,
        conn: Any,
        seasons: list[int],
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Load pitcher profiles and calibrate pitcher constants."""
        from similarity.engines.pitcher_similarity import COMMAND_FEATURES, RESULT_FEATURES

        sl = ", ".join(str(s) for s in seasons)
        rows = conn.execute(f"""
            SELECT
                pitcher_id, season, sample_pitches,
                bb_rate, k_rate, csw_rate, zone_rate, chase_rate,
                ground_ball_rate, fly_ball_rate, line_drive_rate, whip, hr_per_9
            FROM derived.pitcher_season_metrics
            WHERE season IN ({sl}) AND below_minimum_sample = FALSE
        """).fetchall()

        if not rows:
            log.warning("No pitcher profiles found for calibration.")
            return report

        np.array([r[0] for r in rows])
        samples = np.array([r[2] for r in rows])

        n_cmd = len(COMMAND_FEATURES)
        n_res = len(RESULT_FEATURES)

        cmd_raw = np.array([[r[3 + i] or 0.0 for i in range(n_cmd)] for r in rows])
        res_raw = np.array([[r[3 + n_cmd + i] or 0.0 for i in range(n_res)] for r in rows])

        def _zscore(mat):
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return (mat - m) / s

        cmd_z = _zscore(cmd_raw)
        res_z = _zscore(res_raw)

        report.sigma_command = calibrate_sigma(cmd_z, target_median_score=target)
        report.sigma_results = calibrate_sigma(res_z, target_median_score=target)

        # EB prior
        all_raw = np.hstack([cmd_raw, res_raw])
        report.eb_n_prior_pitcher = calibrate_eb_prior(all_raw, samples, min_sample=200)

        log.info(
            "Pitcher calibration complete: %d profiles, sigma_cmd=%.3f, "
            "sigma_res=%.3f, eb_prior=%.1f",
            len(rows),
            report.sigma_command,
            report.sigma_results,
            report.eb_n_prior_pitcher,
        )
        return report

    def _calibrate_fielder_params(
        self,
        conn: Any,
        seasons: list[int],
        target_median_score: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """
        Tier 1 calibration for fielder similarity engine parameters.

        Loads fielder profiles from DuckDB, computes per-position z-score
        normalized feature matrices, and calibrates RBF sigma per sub-score
        per position group (IF vs OF).

        Returns a dict with calibrated sigma values keyed by sub-score name.
        """
        from similarity.engines.fielder_similarity import (
            IF_DP_FEATURES,
            IF_ERROR_FEATURES,
            IF_RANGE_FEATURES,
            IF_SPECIALTY_FEATURES,
            OF_ARM_FEATURES,
            OF_ERROR_FEATURES,
            OF_RANGE_FEATURES,
        )

        sl = ", ".join(str(s) for s in seasons)

        # Load infield profiles
        if_rows = conn.execute(f"""
            SELECT
                player_id, position, season, sample_batted_balls,
                oaa_glove_side, oaa_arm_side, oaa_charging, oaa_deep, catch_pct_added,
                fielding_error_rate, throwing_error_rate,
                dp_above_expected, dp_attempt_rate, dp_success_rate,
                dp_pivot_above_expected,
                bunt_fielding_rate, scoop_success_rate
            FROM derived.fielder_season_metrics
            WHERE season IN ({sl})
              AND position IN ('1B','2B','3B','SS')
              AND NOT below_minimum_sample
        """).fetchall()

        # Load outfield profiles
        of_rows = conn.execute(f"""
            SELECT
                player_id, position, season, sample_batted_balls,
                oaa_glove_side, oaa_arm_side, oaa_charging, oaa_deep, catch_pct_added,
                fielding_error_rate, throwing_error_rate,
                arm_hold_rate, arm_thrown_out_rate, arm_advancement_prevention, of_arm_runs,
                five_star_opps, five_star_catches, four_star_opps, four_star_catches,
                routine_opps, routine_catches
            FROM derived.fielder_season_metrics
            WHERE season IN ({sl})
              AND position IN ('LF','CF','RF')
              AND NOT below_minimum_sample
        """).fetchall()

        def _zscore(mat):
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return (mat - m) / s

        # Calibrate IF sigmas
        if if_rows:
            n_range = len(IF_RANGE_FEATURES)
            n_err = len(IF_ERROR_FEATURES)
            n_dp = len(IF_DP_FEATURES)
            n_pivot = len(IF_PIVOT_FEATURES)
            n_spec = len(IF_SPECIALTY_FEATURES)

            range_raw = np.array([[r[4 + i] or 0.0 for i in range(n_range)] for r in if_rows])
            err_raw = np.array([[r[4 + n_range + i] or 0.0 for i in range(n_err)] for r in if_rows])
            dp_raw = np.array(
                [[r[4 + n_range + n_err + i] or 0.0 for i in range(n_dp)] for r in if_rows]
            )
            pivot_raw = np.array(
                [
                    [r[4 + n_range + n_err + n_dp + i] or 0.0 for i in range(n_pivot)]
                    for r in if_rows
                ]
            )
            spec_raw = np.array(
                [
                    [r[4 + n_range + n_err + n_dp + n_pivot + i] or 0.0 for i in range(n_spec)]
                    for r in if_rows
                ]
            )

            ids = np.array([r[0] for r in if_rows])

            report.sigma_if_range = calibrate_sigma(
                _zscore(range_raw), target_median_score=target_median_score
            )
            report.sigma_if_dp = calibrate_sigma(
                _zscore(np.concatenate((dp_raw, pivot_raw), axis=1)),
                target_median_score=target_median_score,
            )
            report.sigma_if_errors = calibrate_sigma(
                _zscore(err_raw), target_median_score=target_median_score
            )
            report.sigma_if_specialty = calibrate_sigma(
                _zscore(spec_raw), target_median_score=target_median_score
            )

            report.reliability_weights_if_range = calibrate_reliability_weights(range_raw, ids)
            report.reliability_weights_if_dp = calibrate_reliability_weights(dp_raw, ids)
            report.reliability_weights_if_pivot = calibrate_reliability_weights(pivot_raw, ids)
            report.reliability_weights_if_error = calibrate_reliability_weights(err_raw, ids)
            report.reliability_weights_if_specialty = calibrate_reliability_weights(spec_raw, ids)

            log.info(
                "IF calibration: %d profiles, sigma_range=%.3f, sigma_dp=%.3f, "
                "sigma_err=%.3f, sigma_spec=%.3f",
                len(if_rows),
                report.sigma_if_range,
                report.sigma_if_dp,
                report.sigma_if_errors,
                report.sigma_if_specialty,
            )

        # Calibrate OF sigmas
        if of_rows:
            n_range = len(OF_RANGE_FEATURES)
            n_err = len(OF_ERROR_FEATURES)
            n_arm = len(OF_ARM_FEATURES)

            range_raw = np.array([[r[4 + i] or 0.0 for i in range(n_range)] for r in of_rows])
            err_raw = np.array([[r[4 + n_range + i] or 0.0 for i in range(n_err)] for r in of_rows])
            arm_raw = np.array(
                [[r[4 + n_range + n_err + i] or 0.0 for i in range(n_arm)] for r in of_rows]
            )

            # Star rates
            star_raw = []
            for r in of_rows:
                base = 4 + n_range + n_err + n_arm
                f5o, f5c = r[base], r[base + 1]
                f4o, f4c = r[base + 2], r[base + 3]
                ro, rc = r[base + 4], r[base + 5]
                star_raw.append(
                    [
                        (f5c / f5o) if f5o and f5o > 0 else 0.0,
                        (f4c / f4o) if f4o and f4o > 0 else 0.0,
                        (rc / ro) if ro and ro > 0 else 0.0,
                    ]
                )
            star_raw = np.array(star_raw)

            ids = np.array([r[0] for r in of_rows])

            report.sigma_of_range = calibrate_sigma(
                _zscore(range_raw), target_median_score=target_median_score
            )
            report.sigma_of_arm = calibrate_sigma(
                _zscore(arm_raw), target_median_score=target_median_score
            )
            report.sigma_of_stars = calibrate_sigma(
                _zscore(star_raw), target_median_score=target_median_score
            )
            report.sigma_of_errors = calibrate_sigma(
                _zscore(err_raw), target_median_score=target_median_score
            )

            report.reliability_weights_of_range = calibrate_reliability_weights(range_raw, ids)
            report.reliability_weights_of_arm = calibrate_reliability_weights(arm_raw, ids)
            report.reliability_weights_of_star = calibrate_reliability_weights(star_raw, ids)
            report.reliability_weights_of_error = calibrate_reliability_weights(err_raw, ids)

            log.info(
                "OF calibration: %d profiles, sigma_range=%.3f, sigma_arm=%.3f, "
                "sigma_stars=%.3f, sigma_err=%.3f",
                len(of_rows),
                report.sigma_of_range,
                report.sigma_of_arm,
                report.sigma_of_stars,
                report.sigma_of_errors,
            )

        return report

    def _calibrate_baserunner_params(
        self,
        conn: Any,
        seasons: list[int],
        target_median_score: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """
        Tier 1 calibration for baserunner extra-base similarity engine.

        Loads baserunner profiles from DuckDB, z-score normalizes per
        sub-score, and calibrates RBF sigma for speed, aggression, and
        success dimensions.
        """
        from similarity.engines.baserunner_similarity import (
            AGGRESSION_FEATURES,
            SPEED_FEATURES,
            SUCCESS_FEATURES,
        )

        sl = ", ".join(str(s) for s in seasons)
        rows = conn.execute(f"""
            SELECT
                player_id, season, sample_advancement_opps,
                -- Speed (1)
                sprint_speed,
                -- Aggression (6)
                extra_base_attempt_rate, first_to_third_attempt_rate,
                second_to_home_attempt_rate, first_to_home_attempt_rate,
                tag_up_attempt_rate, stop_rate,
                -- Success (5)
                extra_base_success_rate, first_to_third_success_rate,
                second_to_home_success_rate, first_to_home_success_rate,
                tag_up_success_rate
            FROM derived.baserunner_season_metrics
            WHERE season IN ({sl}) AND NOT below_minimum_sample
        """).fetchall()

        if not rows:
            log.warning("No baserunner profiles found for calibration.")
            return report

        ids = np.array([r[0] for r in rows])
        samples = np.array([r[2] for r in rows])

        n_speed = len(SPEED_FEATURES)
        n_agg = len(AGGRESSION_FEATURES)
        n_suc = len(SUCCESS_FEATURES)

        col = 3
        speed_raw = np.array([[r[col + i] or 0.0 for i in range(n_speed)] for r in rows])
        col += n_speed
        agg_raw = np.array([[r[col + i] or 0.0 for i in range(n_agg)] for r in rows])
        col += n_agg
        suc_raw = np.array([[r[col + i] or 0.0 for i in range(n_suc)] for r in rows])

        def _zscore(mat):
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return (mat - m) / s

        report.sigma_baserunner_speed = calibrate_sigma(
            _zscore(speed_raw),
            target_median_score=target_median_score,
        )
        report.sigma_baserunner_aggression = calibrate_sigma(
            _zscore(agg_raw),
            target_median_score=target_median_score,
        )
        report.sigma_baserunner_success = calibrate_sigma(
            _zscore(suc_raw),
            target_median_score=target_median_score,
        )

        # EB prior
        all_raw = np.hstack([speed_raw, agg_raw, suc_raw])
        report.eb_n_prior_baserunner = calibrate_eb_prior(all_raw, samples, min_sample=20)

        # Reliability weights
        report.reliability_weights_baserunner_speed = calibrate_reliability_weights(speed_raw, ids)
        report.reliability_weights_baserunner_aggression = calibrate_reliability_weights(
            agg_raw, ids
        )
        report.reliability_weights_baserunner_success = calibrate_reliability_weights(suc_raw, ids)

        log.info(
            "Baserunner calibration complete: %d profiles, sigma_speed=%.3f, "
            "sigma_agg=%.3f, sigma_suc=%.3f, eb_prior=%.1f",
            len(rows),
            report.sigma_baserunner_speed,
            report.sigma_baserunner_aggression,
            report.sigma_baserunner_success,
            report.eb_n_prior_baserunner,
        )
        return report


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    cal = SimilarityCalibrator(duckdb_path="../db/schemas/baseball_simulator.duckdb")
    report = cal.calibrate_from_population(
        seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017],
        target_median_score=0.5,
    )
    print(report.summary())
