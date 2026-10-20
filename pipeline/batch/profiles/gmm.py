"""The pitcher arsenal mixture fit: one Gaussian mixture per pitcher-season,
fitted in a process pool, and the writes of its results.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture

log = logging.getLogger("player_profile_computor")

# The mixture fit's settings.
GMM_MIN_K = 2
GMM_MAX_K = 7
GMM_MIN_PITCHES_PER_K = 50  # minimum pitches per component to attempt K
GMM_IVB_CAP_SD = 3.0  # cap IVB at ±N standard deviations before fitting
GMM_FEATURE_NAMES = [
    "velo",
    "ivb",
    "hb",
    "spin_rate",
    "spin_axis",
    "release_x",
    "release_z",
    "release_ext",
]


# ---------------------------------------------------------------------------
# The area under the curve, for the component labels
# ---------------------------------------------------------------------------


def _quick_auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Fast AUC approximation without importing sklearn.metrics."""
    try:
        from sklearn.metrics import roc_auc_score

        return roc_auc_score(y_true, y_score)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# One pitcher-season's mixture
# ---------------------------------------------------------------------------


def _fit_gmm_for_pitcher(
    pitches_df: pd.DataFrame,
    pitcher_id: int,
    season: int,
) -> tuple[dict | None, list[dict]]:
    """
    Fits a Gaussian Mixture Model to the pitch feature vectors for one
    pitcher/season.

    Returns
    -------
    model_json : dict or None
        Serializable GMM model dict matching the schema's gmm_model JSON
        structure. None if fitting failed or sample too small.
    components : list[dict]
        One dict per GMM component for derived.pitcher_gmm_components.
    """
    features = pitches_df[GMM_FEATURE_NAMES].dropna()

    if len(features) < GMM_MIN_PITCHES_PER_K * GMM_MIN_K:
        log.debug(
            "  pitcher %s season %s: only %d clean pitches — skipping GMM",
            pitcher_id,
            season,
            len(features),
        )
        return None, []

    X = features.values.astype(np.float64)

    # Cap IVB at ±GMM_IVB_CAP_SD standard deviations (index 1 in feature order)
    ivb_col = GMM_FEATURE_NAMES.index("ivb")
    ivb_mean = X[:, ivb_col].mean()
    ivb_std = X[:, ivb_col].std()
    if ivb_std > 0:
        cap = GMM_IVB_CAP_SD * ivb_std
        X[:, ivb_col] = np.clip(X[:, ivb_col], ivb_mean - cap, ivb_mean + cap)

    # Standardize (store global mean/std for de-standardization at query time)
    feature_means = X.mean(axis=0)
    feature_stds = X.std(axis=0)
    feature_stds[feature_stds == 0] = 1.0  # avoid divide-by-zero
    X_std = (X - feature_means) / feature_stds

    # BIC-select K in range [GMM_MIN_K, GMM_MAX_K]
    # Cap max K so each component has at least GMM_MIN_PITCHES_PER_K pitches
    max_k = min(GMM_MAX_K, len(features) // GMM_MIN_PITCHES_PER_K)
    max_k = max(max_k, GMM_MIN_K)

    best_gmm = None
    best_bic = np.inf
    best_k = GMM_MIN_K

    for k in range(GMM_MIN_K, max_k + 1):
        try:
            gmm = GaussianMixture(
                n_components=k,
                covariance_type="full",
                max_iter=200,
                n_init=3,
                random_state=42,
            )
            gmm.fit(X_std)
            bic = gmm.bic(X_std)
            if bic < best_bic:
                best_bic = bic
                best_gmm = gmm
                best_k = k
        except Exception as exc:
            log.warning(
                "  GMM fit failed: pitcher=%s season=%s k=%s: %s",
                pitcher_id,
                season,
                k,
                exc,
            )
            continue

    if best_gmm is None:
        return None, []

    # Soft-assign pitches to components for n_pitches_assigned
    responsibilities = best_gmm.predict_proba(X_std)  # shape (n_pitches, K)
    soft_counts = responsibilities.sum(axis=0)

    # Build model JSON (means stored in original feature units for readability;
    # covariance stored in STANDARDIZED space so the similarity engine operates
    # in a well-conditioned space where all features are O(1) variance.
    # Similarity engine contract:
    #   1. Standardize input: x_std = (x - feature_means) / feature_stds
    #   2. Use covariance (standardized) for Mahalanobis / Gaussian kernel
    # DO NOT de-standardize the covariance — spin_rate std ~118 RPM means
    # de-standardizing multiplies those entries by 118² ≈ 14,000, making
    # spin_rate dominate all similarity scores regardless of actual difference.
    components_json = []
    for c in range(best_k):
        mean_std = best_gmm.means_[c]  # standardized
        mean_orig = mean_std * feature_stds + feature_means  # original units (display only)
        cov_std = best_gmm.covariances_[c]  # standardized — stored as-is

        components_json.append(
            {
                "component_id": c,
                "weight": float(best_gmm.weights_[c]),
                "mean": mean_orig.tolist(),  # original units for display
                "mean_std": mean_std.tolist(),  # standardized — use for distance calc
                "covariance": cov_std.tolist(),  # standardized covariance
                "n_pitches": int(round(soft_counts[c])),
            }
        )

    model_json = {
        "n_components": best_k,
        "feature_names": GMM_FEATURE_NAMES,
        "feature_means": feature_means.tolist(),
        "feature_stds": feature_stds.tolist(),
        "components": components_json,
        "fit_diagnostics": {
            "bic": float(best_bic),
            "aic": float(best_gmm.aic(X_std)),
            "log_likelihood": float(best_gmm.lower_bound_),
            "n_iter": int(best_gmm.n_iter_),
            "converged": bool(best_gmm.converged_),
            "fit_date": date.today().isoformat(),
        },
    }

    # Build component rows for derived.pitcher_gmm_components.
    # mean_* columns store original-unit means (for display/interpretation).
    # var_* columns store STANDARDIZED variances (diagonal of standardized
    # covariance) so SQL-level similarity scoring stays well-conditioned.
    component_rows = []
    for c_json in components_json:
        mean_orig = c_json["mean"]
        cov_std = np.array(c_json["covariance"])
        variances_std = np.diag(cov_std).tolist()  # standardized variances — O(1) scale
        fi = {name: i for i, name in enumerate(GMM_FEATURE_NAMES)}

        component_rows.append(
            {
                "pitcher_id": pitcher_id,
                "season": season,
                "component_id": c_json["component_id"],
                "weight": c_json["weight"],
                "mean_velo": mean_orig[fi["velo"]],
                "mean_ivb": mean_orig[fi["ivb"]],
                "mean_hb": mean_orig[fi["hb"]],
                "mean_spin_rate": mean_orig[fi["spin_rate"]],
                "mean_spin_axis": mean_orig[fi["spin_axis"]],
                "mean_release_x": mean_orig[fi["release_x"]],
                "mean_release_z": mean_orig[fi["release_z"]],
                "mean_release_ext": mean_orig[fi["release_ext"]],
                # Standardized variances — safe for SQL distance scoring
                "var_velo": variances_std[fi["velo"]],
                "var_ivb": variances_std[fi["ivb"]],
                "var_hb": variances_std[fi["hb"]],
                "var_spin_rate": variances_std[fi["spin_rate"]],
                "var_spin_axis": variances_std[fi["spin_axis"]],
                "var_release_x": variances_std[fi["release_x"]],
                "var_release_z": variances_std[fi["release_z"]],
                "var_release_ext": variances_std[fi["release_ext"]],
                "n_pitches_assigned": c_json["n_pitches"],
                "display_label": _label_component(mean_orig, fi),
                "display_label_confidence": 0.7,  # heuristic labeler confidence
            }
        )

    return model_json, component_rows


# ---------------------------------------------------------------------------
# The batch fit: the worker count, the chunks and the bulk writes
# ---------------------------------------------------------------------------


def _resolve_gmm_workers(n_tasks: int) -> int:
    """Size the mixture-fit pool to the host: one worker per CPU core less one
    for the parent, never more than the number of tasks, and at least 1.
    """
    cpu = os.cpu_count() or 2
    return max(1, min(cpu - 1, n_tasks or 1))


def _chunk_tasks(tasks: list, n_chunks: int) -> list[list]:
    """Round-robin ``tasks`` into at most ``n_chunks`` non-empty lists.

    Round-robin keeps per-chunk wall-time balanced when fit cost varies by
    pitcher (high-volume starters vs. relievers).
    """
    if n_chunks <= 1:
        return [list(tasks)] if tasks else []
    buckets: list[list] = [[] for _ in range(n_chunks)]
    for i, t in enumerate(tasks):
        buckets[i % n_chunks].append(t)
    return [b for b in buckets if b]


def _fit_gmm_batch(
    task_chunk: list[tuple[np.ndarray, int, int]],
) -> list[tuple[int, int, dict | None, list[dict]]]:
    """Fit the mixtures of a *chunk* of pitchers in one worker process.

    One task per chunk, not one per pitcher, makes one round trip per worker.
    Each task carries a compact float32 feature array rather than a pandas
    DataFrame, which roughly halves the bytes pickled across the process
    boundary.
    """
    results: list[tuple[int, int, dict | None, list[dict]]] = []
    for arr, pitcher_id, season in task_chunk:
        df = pd.DataFrame(arr, columns=GMM_FEATURE_NAMES)
        model_json, component_rows = _fit_gmm_for_pitcher(df, pitcher_id, season)
        results.append((pitcher_id, season, model_json, component_rows))
    return results


def _flush_gmm_results(
    conn,
    fitted: list[tuple[int, int, dict, list[dict]]],
    fallbacks: list[tuple[int, int]],
) -> None:
    """Write every mixture result in three set-based statements, driven by
    registered frames, instead of one UPDATE or INSERT per pitcher (5,000 and
    more round trips on a full nine-season build).
    """
    if fallbacks:
        fb_df = pd.DataFrame(fallbacks, columns=["pitcher_id", "season"])
        conn.register("gmm_fallback_df", fb_df)
        try:
            conn.execute(
                """
                UPDATE derived.pitcher_season_metrics AS m
                SET below_minimum_sample = TRUE
                FROM gmm_fallback_df AS f
                WHERE m.pitcher_id = f.pitcher_id AND m.season = f.season
                """
            )
        finally:
            conn.unregister("gmm_fallback_df")

    if not fitted:
        return

    model_df = pd.DataFrame(
        [(pid, ssn, json.dumps(mj)) for pid, ssn, mj, _ in fitted],
        columns=["pitcher_id", "season", "gmm_model_json"],
    )
    conn.register("gmm_model_df", model_df)
    try:
        conn.execute(
            """
            UPDATE derived.pitcher_season_metrics AS m
            SET gmm_model = CAST(g.gmm_model_json AS JSON)
            FROM gmm_model_df AS g
            WHERE m.pitcher_id = g.pitcher_id AND m.season = g.season
            """
        )
    finally:
        conn.unregister("gmm_model_df")

    all_components = [row for _, _, _, rows in fitted for row in rows]
    if all_components:
        comp_df = pd.DataFrame(all_components)
        conn.register("comp_df", comp_df)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO derived.pitcher_gmm_components SELECT * FROM comp_df"
            )
        finally:
            conn.unregister("comp_df")


def _label_component(mean: list[float], fi: dict[str, int]) -> str | None:
    """
    Post-hoc human-readable label for a GMM component.
    Used for UI display only — never touches similarity calculations.
    Heuristic rules based on velocity and movement signature.
    """
    velo = mean[fi["velo"]]
    ivb = mean[fi["ivb"]]
    hb = mean[fi["hb"]]

    if velo is None or ivb is None or hb is None:
        return None

    # Fastball family
    if velo >= 93:
        if ivb >= 14 and abs(hb) <= 8:
            return "4-Seam Fastball"
        if ivb >= 8 and hb >= 10:
            return "2-Seam / Sinker"
        if ivb >= 8 and hb <= -10:
            return "2-Seam / Sinker (LHP)"
        if ivb <= 5 and abs(hb) >= 10:
            return "Cutter"
        return "Fastball"

    # Hard breaking balls
    if 82 <= velo < 93:
        if ivb <= -5 and abs(hb) <= 8:
            return "12-6 Curveball"
        if ivb <= 0 and abs(hb) >= 12:
            return "Sweeping Slider"
        if -5 <= ivb <= 8 and abs(hb) >= 8:
            return "Slider"
        return "Hard Breaking Ball"

    # Soft stuff
    if velo < 82:
        if ivb <= -5:
            return "Curveball"
        if ivb >= 5:
            return "Changeup / Splitter"
        return "Offspeed"

    return None
