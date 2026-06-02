"""
player_profile_computor.py
==========================
Step 1.4 — Player Profile & Defensive Metrics Pre-computation
MLB Baseball Simulation Platform

Reads from PostgreSQL raw.pitches (and raw.games / raw.managers for context)
and writes all derived metrics + simulation pools to DuckDB.

Architecture
------------
  PostgreSQL (raw.*)
       │
       │  DuckDB postgres extension attaches directly — no full data load into Python
       ▼
  DuckDB (in-memory aggregation engine)
       │
       ├── derived.pitcher_season_metrics   (GMM model fit in Python via sklearn)
       ├── derived.pitcher_gmm_components   (one row per GMM component)
       ├── derived.batter_season_metrics
       ├── derived.fielder_season_metrics
       ├── derived.baserunner_season_metrics
       ├── derived.catcher_season_metrics
       ├── derived.manager_season_metrics
       ├── derived.park_factors
       │
       ├── sim.pitch_pool                   (full rebuild — quality-filtered raw.pitches)
       ├── sim.outcome_pool                 (in-play subset with batted ball detail)
       └── sim.stolen_base_pool             (SB attempts, denormalized from derived.*)

Run order matters:
  1. Park factors           (no dependencies)
  2. Pitcher profiles       (GMM — most expensive step)
  3. Batter profiles
  4. Fielder profiles
  5. Baserunner profiles
  6. Catcher profiles
  7. Manager profiles
  8. sim.pitch_pool         (sim pools go last)
  9. sim.outcome_pool
  10. sim.stolen_base_pool  (denormalizes from derived.* — must be after step 7)

Nightly usage
-------------
  # Recompute current season only (fast — ~5 min)
  computor = PlayerProfileComputor(
      pg_dsn="postgresql://user:pass@localhost:5432/baseball",
      duckdb_path="/data/baseball_sim.duckdb",
  )
  computor.run(seasons=[2025])

  # Full historical rebuild (slow — ~60 min for 3 seasons)
  computor.run(seasons=[2022, 2023, 2024, 2025], full_rebuild=True)

Dependencies
------------
  pip install duckdb psycopg2-binary scikit-learn numpy pandas scipy
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

from simulation.constants import DEFENSIVE_RUN_VALUES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("player_profile_computor")

# GMM
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

# Minimum sample thresholds (from schema comments)
MIN_PITCHER_PITCHES = 200
MIN_BATTER_PA = 100
MIN_FIELDER_BIP = 50
MIN_RUNNER_ADV_OPPS = 20
MIN_RUNNER_SB_ATTEMPTS = 5
MIN_FIELDER_PLAYS = 50
MIN_CATCHER_SB_ATTEMPTS = 10
MIN_MANAGER_GAMES = 50

# FIP constant — approximation; update per-season if desired
FIP_CONSTANT = 3.17

# Park factor Bayesian shrinkage — weight of prior (1.0 = league average)
# At sample_pa = PARK_PRIOR_PA, posterior is 50/50 between prior and observed.
PARK_PRIOR_PA = 2000

# Leverage index simplified bins (used for manager profiles)
LI_LOW = 0.7
LI_HIGH = 1.5

# Barrel (Statcast sliding-scale definition).
# A batted ball is a *barrel* when exit velocity >= 98 mph AND launch angle falls
# inside a band that WIDENS as exit velocity increases.  At the 98 mph floor the
# band is launch_angle 26-30 deg; for every +1 mph above 98 the lower bound drops
# 1 deg and the upper bound rises 2 deg, expanding to roughly 8-50 deg at
# >= 116 mph (capped there).  Below 98 mph it is never a barrel.
#   lower_la = GREATEST(BARREL_LA_FLOOR,  BARREL_MIN_LA - (EV - BARREL_MIN_VELO) * BARREL_LOWER_SLOPE)
#   upper_la = LEAST   (BARREL_LA_CEILING, BARREL_MAX_LA + (EV - BARREL_MIN_VELO) * BARREL_UPPER_SLOPE)
# Hard hit: exit_velo >= 95 mph
BARREL_MIN_VELO = 98.0  # EV floor; below this never a barrel
BARREL_MIN_LA = 26.0  # lower LA bound at the 98 mph floor
BARREL_MAX_LA = 30.0  # upper LA bound at the 98 mph floor
BARREL_LOWER_SLOPE = 1.0  # deg the lower bound drops per +1 mph above floor
BARREL_UPPER_SLOPE = 2.0  # deg the upper bound rises per +1 mph above floor
BARREL_LA_FLOOR = 8.0  # lower LA bound never goes below this (>= ~116 mph)
BARREL_LA_CEILING = 50.0  # upper LA bound never goes above this (>= ~116 mph)
HARD_HIT_MIN_VELO = 95.0

# SIM-074: barrel uses the Statcast sliding-scale band built by _barrel_case_sql.


def _barrel_case_sql(prefix: str = "") -> str:
    """Return a DuckDB SQL boolean expression that is TRUE when the row is a
    Statcast *barrel*.

    The sliding-scale band widens with exit velocity (see module constants).
    NULL launch_speed / launch_angle yield NULL (counted as not-a-barrel by the
    enclosing SUM(CASE ...)).  ``prefix`` is prepended to the predicate so the
    platoon variants can gate on e.g. ``p_throws='L' AND``.
    """
    return (
        f"{prefix}launch_speed >= {BARREL_MIN_VELO} "
        f"AND launch_angle >= GREATEST({BARREL_LA_FLOOR}, "
        f"{BARREL_MIN_LA} - (launch_speed - {BARREL_MIN_VELO}) * {BARREL_LOWER_SLOPE}) "
        f"AND launch_angle <= LEAST({BARREL_LA_CEILING}, "
        f"{BARREL_MAX_LA} + (launch_speed - {BARREL_MIN_VELO}) * {BARREL_UPPER_SLOPE})"
    )


# ---------------------------------------------------------------------------
# Defensive Metrics Constants
# ---------------------------------------------------------------------------

HC_SCALE = 2.48  # hc units per foot
HOME_PLATE = (125.42, 199.27)
SECOND_BASE = (125.42, 108.27)
FIRST_BASE = (162.5, 153.5)
THIRD_BASE = (88.3, 153.5)

AVG_FIELDER_POS = {
    "SS": {"R": (112.0, 152.0), "L": (118.0, 149.0)},
    "2B": {"R": (141.0, 152.0), "L": (136.0, 149.0)},
    "3B": {"R": (96.0, 165.0), "L": (90.0, 168.0)},
    "1B": {"R": (157.0, 165.0), "L": (162.0, 162.0)},
    "LF": {"R": (80.0, 115.0), "L": (75.0, 118.0)},
    "CF": {"R": (125.0, 80.0), "L": (125.0, 82.0)},
    "RF": {"R": (170.0, 115.0), "L": (175.0, 118.0)},
}

AVG_THROW_VELO = {
    "SS": 85.0,
    "2B": 75.0,
    "3B": 83.0,
    "1B": 65.0,
    "LF": 82.0,
    "CF": 84.0,
    "RF": 86.0,
}

INFIELD_EXCHANGE_TIME = 0.80
OUTFIELD_EXCHANGE_TIME = 1.00
# Defensive run-value constants — sourced from simulation.constants (SIM-202).
# Values preserved exactly: infield 0.75, outfield 0.90, block 0.25, strike 0.125.
RUNS_PER_OAA_INFIELD = DEFENSIVE_RUN_VALUES["runs_per_oaa_infield"]
RUNS_PER_OAA_OUTFIELD = DEFENSIVE_RUN_VALUES["runs_per_oaa_outfield"]
RUNS_PER_BLOCK_SAVED = DEFENSIVE_RUN_VALUES["runs_per_block_saved"]
RUNS_PER_STRIKE_ABOVE_AVG = DEFENSIVE_RUN_VALUES["runs_per_strike_above_avg"]
FRAMING_SHADOW_INNER = 0.0
FRAMING_SHADOW_OUTER = 0.5
ZONE_HALF_WIDTH = 0.833
OF_GOING_BACK_PENALTY_FPS = 1.0
IF_AWAY_FROM_BAG_PENALTY_S = 0.20
GRAVITY_FPS2 = 32.174
DEFAULT_SPRINT_SPEED_FPS = 27.0
MIN_CATCHER_PITCHES = 500
MIN_DP_OPPORTUNITIES = 20

RE24_APPROX = {
    (0, 0b001): 0.83,
    (0, 0b011): 1.45,
    (0, 0b111): 2.20,
    (1, 0b001): 0.50,
    (1, 0b011): 0.90,
    (1, 0b111): 1.50,
    (2, 0b000): 0.10,
    (0, 0b000): 0.48,
    (1, 0b000): 0.26,
    (2, 0b001): 0.22,
}


def compute_leverage_index(
    inning: int,
    outs: int,
    runners_state: int,  # bitmask 0-7
    score_diff: int,  # bat_score - fld_score, capped
) -> float:
    """
    Simplified leverage index based on the Tango/Litchman framework.

    Full LI requires win-probability tables; this implementation uses a
    well-calibrated approximation that captures the key drivers:
      - Late innings increase leverage
      - Close score differential increases leverage
      - Runners on base increase leverage
      - Two outs increase leverage slightly

    Phase 5 can swap in a full win-probability table lookup if desired.
    The approximation is sufficient for manager tendency profiling.
    """
    # Base inning weight: increases in later innings
    if inning <= 3:
        inning_factor = 0.6
    elif inning <= 6:
        inning_factor = 0.9
    elif inning <= 8:
        inning_factor = 1.3
    else:
        inning_factor = 1.5  # 9th+

    # Score differential factor: close games have higher leverage
    abs_diff = min(abs(score_diff), 5)
    score_factor = max(0.3, 1.5 - abs_diff * 0.24)

    # Runners on base: more runners = more scoring potential
    runners_on = bin(runners_state).count("1")
    runner_factor = 1.0 + runners_on * 0.15

    # Outs factor: two outs increases pressure slightly
    outs_factor = 1.0 if outs < 2 else 1.1

    return inning_factor * score_factor * runner_factor * outs_factor


# ---------------------------------------------------------------------------
# Defensive utility functions
# ---------------------------------------------------------------------------


def _hc_to_feet(hc_dist: float) -> float:
    """Convert distance in hc_x/hc_y coordinate units to feet."""
    return hc_dist / HC_SCALE


def _euclidean_hc(x1: float, y1: float, x2: float, y2: float) -> float:
    """Euclidean distance in hc coordinate space."""
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def _throw_time(distance_ft: float, velo_mph: float) -> float:
    """Time for a throw to travel a given distance at a given velocity."""
    if velo_mph <= 0:
        return 999.0
    velo_fps = velo_mph * 1.467
    return distance_ft / velo_fps


def _estimate_hang_time(launch_speed: float, launch_angle: float) -> float | None:
    """
    Estimate batted ball hang time from launch conditions.
    Uses projectile motion with empirical drag correction calibrated
    against Alan Nathan's trajectory calculator for MLB conditions.

    Parameters
    ----------
    launch_speed : float  (mph)
    launch_angle : float  (degrees)

    Returns
    -------
    float : estimated hang time in seconds, or None if invalid
    """
    if launch_speed is None or launch_angle is None:
        return None
    if launch_speed <= 0 or launch_angle < -30 or launch_angle > 90:
        return None

    v0 = launch_speed * 1.467  # mph -> ft/s
    theta = math.radians(launch_angle)
    vz = v0 * math.sin(theta)

    # Contact height ~3 ft above ground
    h0 = 3.0
    # Catch height: ~5 ft for fly balls, ~0 for ground balls
    h_catch = 5.0 if launch_angle > 15 else 0.0

    # Drag correction: reduces hang time ~12% vs vacuum for fly balls,
    # less for line drives, more for high popups
    if launch_angle > 50:
        drag_factor = 0.82
    elif launch_angle > 25:
        drag_factor = 0.87
    elif launch_angle > 10:
        drag_factor = 0.92
    else:
        drag_factor = 0.95

    discriminant = vz**2 + 2 * GRAVITY_FPS2 * (h0 - h_catch)
    if discriminant < 0:
        return None

    t_vacuum = (vz + math.sqrt(discriminant)) / GRAVITY_FPS2
    return max(0.5, t_vacuum * drag_factor)


def _estimate_gb_travel_time(
    launch_speed: float, launch_angle: float, distance_ft: float
) -> float | None:
    """
    Estimate how long a ground ball takes to travel a given distance.
    Accounts for ground friction deceleration on grass infields.

    Returns None if the ball stops before reaching the distance.
    """
    if launch_speed is None or launch_speed <= 0 or distance_ft <= 0:
        return None

    v0 = launch_speed * 1.467  # mph -> ft/s
    v_ground = v0 * math.cos(math.radians(abs(launch_angle or 0)))

    # Friction deceleration on grass (~12 ft/s²)
    decel = 12.0
    a = 0.5 * decel
    b = -v_ground
    c = distance_ft

    disc = b**2 - 4 * a * c
    if disc < 0:
        return None  # ball stops before reaching fielder

    t = (-b - math.sqrt(disc)) / (2 * a)
    return max(0.1, t)


def _classify_direction_of(
    fielder_x: float,
    fielder_y: float,
    ball_x: float,
    ball_y: float,
    position: str,
) -> tuple[str, bool]:
    """
    Classify the direction a fielder must move relative to their position.

    Returns
    -------
    category : str
        One of 'glove_side', 'arm_side', 'charging', 'deep'
    going_back : bool
        True if the fielder is moving away from home plate (OF)
        or away from the bag they need to throw to (IF)
    """
    dx = ball_x - fielder_x
    dy = ball_y - fielder_y

    # Angle from fielder to ball: 0° = toward home plate, 180° = away
    math.degrees(math.atan2(dx, -dy))  # -dy because y decreases toward OF

    if position in ("LF", "CF", "RF"):
        # Outfield: going_back = ball is further from home plate than fielder
        going_back = ball_y < fielder_y  # smaller y = deeper outfield
        if going_back:
            category = "deep"
        elif ball_y > fielder_y + 10:
            category = "charging"
        elif (position == "LF" and dx > 0) or (position == "RF" and dx < 0):
            category = "glove_side"  # toward center field
        else:
            category = "arm_side"  # toward foul line
    else:
        # Infield
        if abs(dy) > abs(dx) and dy > 0:
            category = "charging"  # coming toward home plate
        elif abs(dy) > abs(dx) and dy < 0:
            category = "deep"  # ranging back into outfield grass
        else:
            # Lateral movement
            if position in ("SS", "3B"):
                category = "glove_side" if dx > 0 else "arm_side"
            else:  # 2B, 1B
                category = "glove_side" if dx < 0 else "arm_side"

        going_back = category == "deep"

    return category, going_back


def _sigmoid(x: np.ndarray, L: float, k: float, x0: float) -> np.ndarray:
    """Standard sigmoid function."""
    return L / (1.0 + np.exp(-k * (x - x0)))


def _asymmetric_sigmoid(
    x: np.ndarray,
    L_left: float,
    k_left: float,
    x0_left: float,
    L_right: float,
    k_right: float,
    x0_right: float,
    blend_k: float,
    blend_x0: float,
) -> np.ndarray:
    """
    Tango's two-sigmoid blend for asymmetric probability curves.
    Used for catch probability, infield out probability, and DP probability.
    """
    left = _sigmoid(x, L_left, k_left, x0_left)
    right = _sigmoid(x, L_right, k_right, x0_right)
    blend = _sigmoid(x, 1.0, blend_k, blend_x0)
    return blend * left + (1.0 - blend) * right


def _fit_logistic_model(
    X: np.ndarray, y: np.ndarray, max_iter: int = 1000
) -> LogisticRegression | None:
    """
    Fit a logistic regression with standard scaling.
    Returns None if fitting fails or sample is too small.
    """
    if len(X) < 100 or y.sum() < 5 or (1 - y).sum() < 5:
        log.warning("  Insufficient data for logistic fit: n=%d, pos=%d", len(X), int(y.sum()))
        return None

    try:
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        model = LogisticRegression(max_iter=max_iter, solver="lbfgs")
        model.fit(X_scaled, y)
        # Store scaler as attribute for later prediction
        model._scaler = scaler
        return model
    except Exception as e:
        log.warning("  Logistic fit failed: %s", e)
        return None


def _predict_proba(model: LogisticRegression, X: np.ndarray) -> np.ndarray:
    """Predict probability using a fitted logistic model with its stored scaler."""
    X_scaled = model._scaler.transform(X)
    return model.predict_proba(X_scaled)[:, 1]


# ---------------------------------------------------------------------------
# Run expectancy matrix builder
# ---------------------------------------------------------------------------


def build_run_expectancy_matrix(conn: duckdb.DuckDBPyConnection, seasons: list[int]) -> dict:
    """
    Build the 24-state run expectancy matrix from play-by-play data.

    Returns a dict mapping (outs, runners_state) -> expected_runs
    that can also be written to derived.run_expectancy_matrix.
    """
    season_list = ", ".join(str(s) for s in seasons)
    log.info("Building run expectancy matrix for seasons %s …", seasons)

    # Compute runs scored from each base-out state to end of inning
    df = conn.execute(f"""
        WITH plays AS (
            SELECT
                game_pk,
                inning,
                inning_topbot,
                at_bat_number,
                outs,
                -- Runners state bitmask: bit0=1B, bit1=2B, bit2=3B
                (CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)
                + (CASE WHEN on_2b IS NOT NULL THEN 2 ELSE 0 END)
                + (CASE WHEN on_3b IS NOT NULL THEN 4 ELSE 0 END) AS runners_state,
                home_score,
                away_score,
                CASE WHEN inning_topbot = 'Top' THEN away_score ELSE home_score END AS bat_score
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND inning <= 8            -- exclude 9th+ for unbiased estimates
              AND events IS NOT NULL     -- one row per PA
        ),
        -- Get the final score for each half-inning
        inning_final AS (
            SELECT
                game_pk, inning, inning_topbot,
                MAX(bat_score) AS final_bat_score
            FROM plays
            GROUP BY game_pk, inning, inning_topbot
        ),
        scored AS (
            SELECT
                p.outs,
                p.runners_state,
                (f.final_bat_score - p.bat_score) AS runs_to_end_of_inning
            FROM plays p
            JOIN inning_final f
              ON p.game_pk = f.game_pk
              AND p.inning = f.inning
              AND p.inning_topbot = f.inning_topbot
        )
        SELECT
            outs,
            runners_state,
            AVG(runs_to_end_of_inning) AS expected_runs,
            COUNT(*) AS sample_size
        FROM scored
        GROUP BY outs, runners_state
        ORDER BY outs, runners_state
    """).fetchdf()

    re_matrix = {}
    season_range = f"{min(seasons)}-{max(seasons)}"

    for _, row in df.iterrows():
        key = (int(row["outs"]), int(row["runners_state"]))
        re_matrix[key] = float(row["expected_runs"])

    # Write to DuckDB table
    conn.execute("DELETE FROM derived.run_expectancy_matrix WHERE season_range = ?", [season_range])
    for _, row in df.iterrows():
        conn.execute(
            """
            INSERT INTO derived.run_expectancy_matrix
            (season_range, outs, runners_state, expected_runs, sample_size)
            VALUES (?, ?, ?, ?, ?)
        """,
            [
                season_range,
                int(row["outs"]),
                int(row["runners_state"]),
                float(row["expected_runs"]),
                int(row["sample_size"]),
            ],
        )

    log.info("  RE matrix built: %d states", len(re_matrix))
    return re_matrix


# ---------------------------------------------------------------------------
# Quick AUC helper
# ---------------------------------------------------------------------------


def _quick_auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Fast AUC approximation without importing sklearn.metrics."""
    try:
        from sklearn.metrics import roc_auc_score

        return roc_auc_score(y_true, y_score)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# GMM helpers
# ---------------------------------------------------------------------------

# GMM helpers
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
# SIM-113: GMM batch-fit scaling, chunked IPC, and bulk result writes
# ---------------------------------------------------------------------------


def _resolve_gmm_workers(n_tasks: int) -> int:
    """Size the GMM fit pool to the host (SIM-113).

    Replaces the previously hardcoded ``n_workers = 8`` (which oversubscribed
    small CI boxes and underused large hosts). One worker per CPU core minus
    one for the parent, never more than the number of tasks, floored at 1.
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
    """Fit GMMs for a *chunk* of pitchers in one worker process (SIM-113).

    Submitting one task per chunk instead of one per pitcher collapses the
    per-pitcher submission/result IPC into a single round-trip per worker.
    Each task carries a compact float32 feature array rather than a pandas
    DataFrame, roughly halving the bytes pickled across the process boundary.
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
    """Write all GMM results in a few set-based statements (SIM-113).

    Replaces ~one UPDATE/INSERT per pitcher (5,000+ round-trips on a full
    9-season build) with three bulk operations driven by registered frames.
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


# ---------------------------------------------------------------------------
# SIM-076 / SIM-095: sim-pool recency weighting + incremental rebuild helpers
# ---------------------------------------------------------------------------

POOL_BUILDER_VERSION = "sim076.1"
RECENCY_RECENT_SEASONS = 2  # seasons (incl. ref) that get the full peak weight
RECENCY_DECAY = 0.75  # geometric decay per season beyond the recent window
RECENCY_FLOOR = 0.25
RECENCY_PEAK = 2.0


def recency_weight(season: int, ref_season: int) -> float:
    """Sampling recency weight for a row from ``season`` vs ``ref_season`` (SIM-076).

    2.0 for the most-recent two seasons, then x0.75 per additional season,
    floored at 0.25. This mirrors the SQL emitted into the pool builders so the
    Python reference and the materialized ``recency_weight`` column never diverge.
    """
    age = ref_season - season
    if age <= RECENCY_RECENT_SEASONS - 1:
        return RECENCY_PEAK
    return max(
        RECENCY_FLOOR, RECENCY_PEAK * (RECENCY_DECAY ** (age - (RECENCY_RECENT_SEASONS - 1)))
    )


def _recency_weight_sql(season_expr: str, ref_season: int) -> str:
    """SQL CASE expression mirroring ``recency_weight()`` for a season column."""
    age = f"({ref_season} - {season_expr})"
    return (
        f"CASE WHEN {age} <= {RECENCY_RECENT_SEASONS - 1} THEN {RECENCY_PEAK} "
        f"ELSE GREATEST({RECENCY_FLOOR}, {RECENCY_PEAK} * "
        f"POWER({RECENCY_DECAY}, {age} - {RECENCY_RECENT_SEASONS - 1})) END"
    )


def _canonical_ref_season(conn, seasons: list[int]) -> int:
    """The single recency reference season shared by ALL three sim pools (SIM-345).

    Previously each pool computed ``ref_season = max(seasons)`` from its own
    argument, so a partial/incremental call could give pools different
    references — and a row's ``recency_weight`` would then disagree between the
    pitch_pool and the outcome_pool built from it. This pins one canonical
    reference: the newest of the seasons requested in THIS run and every
    ``recency_ref_season`` already recorded in ``sim.pool_build_metadata``. Once
    a pool advances the reference, the others adopt the same value, so the
    weights stay consistent across pools and across incremental runs.
    """
    ref = max(seasons)
    try:
        prev = conn.execute(
            "SELECT MAX(recency_ref_season) FROM sim.pool_build_metadata"
        ).fetchone()[0]
    except Exception:
        prev = None
    if prev is not None:
        ref = max(ref, int(prev))
    return ref


def _source_state(conn, season: int):
    """``(MAX(game_date), COUNT(*))`` of usable source rows for ``season``.

    Single scan over ``pg.raw.pitches`` (quality rows only). The count is the
    SIM-345 row-count guard so a same-``game_date`` late/doubleheader row — which
    does NOT advance ``MAX(game_date)`` — is still detected. Returns
    ``(None, 0)`` when the source has no usable rows (or an empty result).
    """
    row = conn.execute(
        "SELECT MAX(game_date), COUNT(*) FROM pg.raw.pitches "
        "WHERE season = ? AND data_quality_flag = FALSE",
        [season],
    ).fetchone()
    if not row or len(row) < 2:
        return None, 0
    return row[0], row[1]


def _seasons_needing_rebuild(conn, pool_name: str, seasons: list[int]) -> list[int]:
    """Subset of ``seasons`` whose source changed since the last build (SIM-095).

    SIM-345 freshness contract — a season is fresh (skipped) only when BOTH:
      * the recorded watermark ``source_max_game_date`` is ``>=`` the current
        ``MAX(game_date)`` in ``pg.raw.pitches`` (date has not advanced), AND
      * the recorded ``source_row_count`` equals the current source row count.

    The previous strict ``>`` watermark silently skipped a season when a late or
    doubleheader row landed on an ALREADY-SEEN ``game_date`` (the max did not
    move). Switching the staleness test to ``src_max >= prev`` and adding the
    row-count guard catches those same-date additions without rebuilding an
    unchanged season every night. If the metadata table is absent or has no row
    for a season (or a legacy row predates ``source_row_count``), the season is
    (re)built.
    """
    try:
        meta = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT season, source_max_game_date, source_row_count "
                "FROM sim.pool_build_metadata WHERE pool_name = ?",
                [pool_name],
            ).fetchall()
        }
    except Exception:
        return list(seasons)
    stale: list[int] = []
    for s in seasons:
        src_max, src_cnt = _source_state(conn, s)
        prev = meta.get(s)
        if prev is None:
            stale.append(s)
            continue
        prev_max, prev_cnt = prev
        if src_max is None or prev_max is None:
            stale.append(s)
            continue
        # Fresh only when the date has not advanced AND the source row count is
        # unchanged. ``src_max >= prev_max`` is the freshness side; we rebuild
        # when the date moved forward OR the count differs (same-date late row).
        date_advanced = src_max > prev_max
        count_changed = prev_cnt is None or src_cnt != prev_cnt
        if date_advanced or count_changed:
            stale.append(s)
    return stale


def _record_pool_build(conn, pool_name: str, seasons: list[int], ref_season: int) -> None:
    """Upsert per-(pool, season) build watermark + recency reference (SIM-076/095).

    Records both the source watermark (``source_max_game_date``) and the source
    row count (``source_row_count``, SIM-345 guard) so the next incremental run
    can detect same-date late rows. ``row_count`` remains the materialized pool
    row count for observability.
    """
    for s in seasons:
        src_max, src_cnt = _source_state(conn, s)
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM sim.{pool_name} WHERE season = ?", [s]
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO sim.pool_build_metadata "
            "(pool_name, season, row_count, source_max_game_date, "
            " source_row_count, recency_ref_season, builder_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [pool_name, s, cnt, src_max, src_cnt, ref_season, POOL_BUILDER_VERSION],
        )


# ---------------------------------------------------------------------------
# Index-recreate hardening (SIM-419)
# ---------------------------------------------------------------------------


def _ensure_index_if_not_exists(create_index_sql: str) -> str:
    """Inject ``IF NOT EXISTS`` into a ``CREATE [UNIQUE] INDEX`` statement.

    Makes an index recreate idempotent so re-running a profile rebuild (or a
    partially-applied prior run) can't fail on an already-present index. Returns
    the statement unchanged if it already has the clause or isn't a recognised
    CREATE INDEX form (the caller still DROPs IF EXISTS first as a safety net).
    """
    stmt = create_index_sql.strip()
    low = stmt.lower()
    if "if not exists" in low:
        return stmt
    for prefix in ("create unique index ", "create index "):
        if low.startswith(prefix):
            return stmt[: len(prefix)] + "IF NOT EXISTS " + stmt[len(prefix) :]
    return stmt


# ---------------------------------------------------------------------------
# Main computor class
# ---------------------------------------------------------------------------


class PlayerProfileComputor:
    """
    Orchestrates the full Step 1.4 nightly batch job.

    All heavy aggregation runs in DuckDB via the postgres extension.
    GMM fitting for pitcher profiles runs in Python (sklearn).
    Defensive metrics (framing, blocking, OAA, DP, arm) are computed
    and aggregated into derived.fielder/catcher_season_metrics inline.
    All results are written to the DuckDB file at duckdb_path.
    """

    def __init__(self, pg_dsn: str, duckdb_path: str) -> None:
        """
        Parameters
        ----------
        pg_dsn : str
            PostgreSQL DSN, e.g. "postgresql://user:pass@localhost:5432/baseball"
        duckdb_path : str
            Path to the DuckDB file. Created if it does not exist.
        """
        self._pg_dsn = pg_dsn
        self._duckdb_path = duckdb_path
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._re_matrix: dict = {}  # populated before defensive steps
        # SIM-433: per-game bullpen workload (rest/recent-pitch-count) derived
        # from raw.pitches by _compute_bullpen_workload during run(); consumed by
        # the bullpen-availability ingest (which persists raw.game_bullpen_availability).
        self._bullpen_workload: pd.DataFrame | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        log.info("Opening DuckDB at %s …", self._duckdb_path)
        self._conn = duckdb.connect(self._duckdb_path)
        self._conn.execute("INSTALL postgres; LOAD postgres;")
        self._conn.execute(f"ATTACH '{self._pg_dsn}' AS pg (TYPE postgres, READ_ONLY);")
        log.info("PostgreSQL attached as 'pg'.")
        self._run_schema_ddl()

    def _close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _run_schema_ddl(self) -> None:
        """
        Creates all DuckDB tables via 02_duckdb_schema.sql (the single
        canonical schema that includes derived, sim, and per-play detail tables).
        Idempotent — uses CREATE TABLE IF NOT EXISTS throughout.
        """
        try:
            self._conn.execute("SELECT 1 FROM derived.pitcher_season_metrics LIMIT 1")
            log.info("DuckDB schema already present — skipping DDL.")
            return
        except Exception:
            pass

        schema_file = Path.cwd() / "db" / "schemas" / "02_duckdb_schema.sql"
        if schema_file.exists():
            ddl = schema_file.read_text()
            self._conn.execute(ddl)
            log.info("Schema DDL applied from 02_duckdb_schema.sql.")
        else:
            self._conn.execute("CREATE SCHEMA IF NOT EXISTS derived;")
            self._conn.execute("CREATE SCHEMA IF NOT EXISTS sim;")
            log.warning("02_duckdb_schema.sql not found. Tables must already exist in DuckDB.")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(
        self,
        seasons: list[int] | None = None,
        full_rebuild: bool = False,
    ) -> None:
        """
        Run the full nightly pre-computation job.

        Parameters
        ----------
        seasons : list[int]
            Seasons to (re)compute. Defaults to current year.
        full_rebuild : bool
            If True, deletes and rewrites all derived metrics for the seasons.
        """
        if seasons is None:
            seasons = [datetime.today().year]

        self._connect()
        try:
            t0 = time.time()
            log.info("=== Player Profile & Defensive Metrics Pre-computation ===")
            log.info("  Seasons: %s | full_rebuild=%s", seasons, full_rebuild)

            if full_rebuild:
                self._delete_seasons(seasons)

            # ── Non-defensive profiles (ordered for dependency satisfaction) ──
            self._compute_park_factors(seasons)  # 1. no dependencies
            self._compute_pitcher_profiles(seasons)  # 2. GMM — most expensive
            self._compute_batter_profiles(seasons)  # 3.
            self._compute_baserunner_profiles(seasons)  # 4. infield/DP need sprint speeds
            self._build_baserunner_steal_metrics(seasons)  # 4b. SIM-408 steal-engine table
            self._build_pitcher_steal_metrics(seasons)  # 4c. SIM-408 pitcher-steal table
            # 4d. SIM-433: derive per-game bullpen rest/workload from raw.pitches.
            # Returned (not persisted — Postgres is attached READ_ONLY here); the
            # bullpen-availability ingest merges it with the MLB-API roster/IL and
            # writes raw.game_bullpen_availability. Cached for the same-process
            # consumer / inspection.
            self._bullpen_workload = self._compute_bullpen_workload(seasons)
            self._compute_manager_profiles(seasons)  # 5.

            # ── Defensive metrics ────────────────────────────────────────────
            # RE matrix first — DP run value and OF arm runs depend on it
            self._re_matrix = build_run_expectancy_matrix(self._conn, seasons)

            # Catcher: framing → blocking → throwing (temp tables consumed in order)
            self._compute_catcher_framing(seasons)
            self._compute_catcher_blocking(seasons)
            self._compute_catcher_throwing(seasons)

            # Outfield
            self._compute_outfield_catch_probability(seasons)
            self._compute_outfield_arm_metrics(seasons)

            # Infield
            self._compute_infield_oaa(seasons)
            self._compute_dp_metrics(seasons)
            self._compute_bunt_defense(seasons)
            self._compute_first_base_scooping(seasons)
            self._compute_error_decomposition(seasons)

            # Season-level aggregation: temp tables → derived.fielder/catcher_season_metrics
            self._aggregate_fielder_season_metrics(seasons)
            self._aggregate_catcher_season_metrics(seasons)

            # ── Simulation pools (last — denormalize from derived.*) ──────────
            # SIM-095: incremental rebuild unless a full_rebuild was requested.
            incremental = not full_rebuild
            self._build_pitch_pool(seasons, incremental=incremental)
            self._build_outcome_pool(seasons, incremental=incremental)
            self._build_stolen_base_pool(seasons, incremental=incremental)

            # SIM-408: per-PA situation facts for the Step 2.9 situation KDTree
            # engine (idempotent INSERT OR REPLACE, so no incremental gating).
            self._build_at_bat_situations(seasons)

            elapsed = time.time() - t0
            log.info("=== Completed in %.1f seconds ===", elapsed)

        finally:
            self._close()

    # ------------------------------------------------------------------
    # Teardown helpers
    # ------------------------------------------------------------------

    def _delete_seasons(self, seasons: list[int]) -> None:
        """Remove existing rows for the given seasons from all derived + sim tables.

        SIM-091: Per-play detail tables (outfield/infield/dp) are explicitly
        enumerated below.  When you add a new derived.* table with a `season`
        column, also add it here OR add it to _EXCLUDED_FROM_DELETE_SEASONS in
        tests/unit/test_data_engineer_sim085_to_091.py with a comment — the
        SIM-091 regression test enforces this.

        Intentionally omitted:
          - derived.run_expectancy_matrix — keyed by season_range, not season.
          - sim.* pools — each has its own DELETE in its own build method.
        """
        season_list = ", ".join(str(s) for s in seasons)
        tables = [
            # FK-ordered: children before parents
            "derived.pitcher_gmm_components",
            "derived.pitcher_season_metrics",
            "derived.batter_season_metrics",
            "derived.fielder_season_metrics",
            "derived.baserunner_season_metrics",
            "derived.baserunner_steal_metrics",  # SIM-408 steal-engine table
            "derived.pitcher_steal_metrics",  # SIM-408 pitcher-steal-engine table
            "derived.catcher_season_metrics",
            "derived.manager_season_metrics",
            "derived.park_factors",
            # SIM-408: per-PA situation facts (own INSERT OR REPLACE build, but
            # season-keyed so full_rebuild must clear it — SIM-091 requires it here)
            "derived.at_bat_situations",
            # Per-play detail tables (SIM-091 explicit enumeration)
            "derived.outfield_play_detail",
            "derived.infield_play_detail",
            "derived.dp_play_detail",
        ]
        # SIM-419: drop secondary indexes before the bulk DELETE (a large DELETE
        # against indexed tables is slow), then recreate them afterwards.
        # Hardened so the tables are NEVER left de-indexed:
        #   * the DELETE + recreate run in try/finally — a DELETE error still
        #     triggers the recreate (previously an error skipped it entirely);
        #   * recreate is idempotent (DROP IF EXISTS + injected IF NOT EXISTS in
        #     _recreate_indexes), so a partially-applied prior run can't fail it;
        #   * recreate failures are collected + verified against duckdb_indexes()
        #     and escalated to ERROR (previously a silent WARNING), so a
        #     now-missing index is surfaced rather than swallowed.
        saved_indexes: list[tuple[str, str]] = []
        for tbl in tables:
            schema_name, table_name = tbl.split(".")
            try:
                rows = self._conn.execute(f"""
                    SELECT index_name, sql
                    FROM duckdb_indexes()
                    WHERE schema_name = '{schema_name}'
                      AND table_name  = '{table_name}'
                      AND is_primary  = false
                """).fetchall()
            except Exception:
                rows = []
            for idx_name, idx_sql in rows:
                saved_indexes.append((f"{schema_name}.{idx_name}", idx_sql))
                self._conn.execute(f"DROP INDEX IF EXISTS {schema_name}.{idx_name}")

        if saved_indexes:
            log.info("  Dropped %d secondary indexes before DELETE.", len(saved_indexes))

        try:
            for tbl in tables:
                self._conn.execute(f"DELETE FROM {tbl} WHERE season IN ({season_list})")
        finally:
            failed = self._recreate_indexes(saved_indexes)
            if saved_indexes:
                log.info(
                    "  Recreated %d/%d secondary indexes after DELETE.",
                    len(saved_indexes) - len(failed),
                    len(saved_indexes),
                )
            if failed:
                log.error(
                    "  %d secondary index(es) FAILED to recreate and are now MISSING: %s. "
                    "Queries still work but lose this index's speedup — investigate and "
                    "rebuild before relying on profile-read latency.",
                    len(failed),
                    ", ".join(failed),
                )

        log.info("Cleared existing rows for seasons %s.", seasons)

    def _recreate_indexes(self, saved_indexes: list[tuple[str, str]]) -> list[str]:
        """Idempotently recreate dropped secondary indexes; return names that FAILED.

        SIM-419: each recreate is ``DROP INDEX IF EXISTS`` + a ``CREATE INDEX IF
        NOT EXISTS`` (injected via :func:`_ensure_index_if_not_exists`) so it is
        safe even if a prior run left the index half-applied, then verified
        against ``duckdb_indexes()`` so a silently-missing index is reported.
        """
        failed: list[str] = []
        for idx_qualified_name, idx_sql in saved_indexes:
            schema_name, _, idx_name = idx_qualified_name.partition(".")
            stmt = _ensure_index_if_not_exists(idx_sql)
            try:
                self._conn.execute(f"DROP INDEX IF EXISTS {idx_qualified_name}")
                self._conn.execute(stmt)
            except Exception as exc:  # noqa: BLE001 — recorded + escalated by caller
                log.warning("  Could not recreate index %s: %s", idx_qualified_name, exc)
                failed.append(idx_qualified_name)
                continue
            # Verify the index is actually present now (don't trust a silent no-op).
            try:
                present = self._conn.execute(
                    "SELECT 1 FROM duckdb_indexes() WHERE schema_name = ? AND index_name = ?",
                    [schema_name, idx_name],
                ).fetchone()
            except Exception:  # noqa: BLE001 — can't verify → don't false-alarm
                present = (True,)
            if not present:
                failed.append(idx_qualified_name)
        return failed

    def _compute_park_factors(self, seasons: list[int]) -> None:
        """
        Computes park factors per venue per season per event type
        (HR, 1B, 2B, 3B, R, K, BB, GB, FB).

        Method: compare the rate of each event at a given park to the
        league-average rate.  Bayesian shrinkage toward 1.0 based on
        sample_pa (PARK_PRIOR_PA controls the shrinkage rate).

        Handedness splits (SIM-336)
        ---------------------------
        ``factor_vs_l`` / ``factor_vs_r`` are split on the **batter's**
        handedness (``stand``), the standard sabermetric convention — short
        porches and outfield dimensions interact with the batter's pull side,
        and ``stand`` is the same key the pitch/outcome read path pre-filters
        on (SIM-337 indexes, SIM-345 contract). Switch hitters (``stand='S'``)
        contribute only to ``factor_overall`` (the per-PA resolved batting side
        is carried by ``bat_hand``, which is not retained at venue grain here),
        so an 'S' PA is neutral w.r.t. the L/R splits but still counted overall.
        Each L/R split is the venue's vs-hand rate over its own vs-hand league
        average, so it is independently centered on 1.0.

        Pool-neutralization policy (SIM-336)
        ------------------------------------
        The sim pools (``sim.pitch_pool`` / ``sim.outcome_pool``) are drawn
        from *real* PAs that ALREADY occurred in some park, so their empirical
        rates are park-influenced. To apply a venue's park factor at sim time
        WITHOUT double-counting, the caller must first NEUTRALIZE each sampled
        comp by dividing out the park factor of the venue the comp was played
        in, then multiply by the target venue's factor:
            adjusted = base_rate * (target_factor / comp_origin_factor)
        Equivalently, sample from a park-neutral pool (each row pre-divided by
        its origin venue's ``regressed_factor``) and multiply by the target
        venue factor once. ``factor_overall == 1.0`` for a perfectly neutral
        park, so a league-average comp pool needs no adjustment. This policy is
        documented on ``derived.park_factors`` (schema) and enforced by the
        park-application engine, not by this builder (which only materializes
        the factors). Always use ``regressed_factor`` (Bayesian-shrunk), never
        the raw ``factor_overall``, in the sim path.
        """
        log.info("Computing park factors …")
        season_list = ", ".join(str(s) for s in seasons)

        # Build a per-venue per-season per-factor table in one DuckDB query.
        # Each PA is counted once from the pitcher's perspective (avoiding
        # double-counting home/away splits).
        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.park_factors

            WITH pa AS (
                -- One row per plate appearance (last pitch of each PA)
                SELECT
                    p.venue_id,
                    p.season,
                    p.p_throws,
                    p.stand,
                    p.events,
                    p.bb_type,
                    p.launch_speed,
                    p.at_bat_number,
                    p.game_pk,
                    -- Count each event type
                    CASE WHEN p.events = 'home_run'                              THEN 1 ELSE 0 END AS is_hr,
                    CASE WHEN p.events = 'single'                                THEN 1 ELSE 0 END AS is_1b,
                    CASE WHEN p.events = 'double'                                THEN 1 ELSE 0 END AS is_2b,
                    CASE WHEN p.events = 'triple'                                THEN 1 ELSE 0 END AS is_3b,
                    CASE WHEN p.events IN ('walk','intent_walk')                 THEN 1 ELSE 0 END AS is_bb,
                    CASE WHEN p.events IN ('strikeout','strikeout_double_play')  THEN 1 ELSE 0 END AS is_k,
                    CASE WHEN p.bb_type = 'ground_ball'                          THEN 1 ELSE 0 END AS is_gb,
                    CASE WHEN p.bb_type IN ('fly_ball','popup')                  THEN 1 ELSE 0 END AS is_fb,
                    -- Batter-handedness gates (switch hitters excluded from both splits)
                    CASE WHEN p.stand = 'L' THEN 1 ELSE 0 END                   AS is_vs_l,
                    CASE WHEN p.stand = 'R' THEN 1 ELSE 0 END                   AS is_vs_r,
                    p.runs_on_pitch
                FROM pg.raw.pitches p
                WHERE p.events IS NOT NULL
                  AND p.data_quality_flag = FALSE
                  AND p.season IN ({season_list})
            ),
            venue_rates AS (
                SELECT
                    venue_id,
                    season,
                    COUNT(*)                            AS sample_pa,
                    SUM(is_vs_l)                        AS sample_pa_l,
                    SUM(is_vs_r)                        AS sample_pa_r,
                    -- Overall numerators
                    SUM(is_hr)                          AS hr_total,
                    SUM(is_1b)                          AS singles,
                    SUM(is_2b)                          AS doubles,
                    SUM(is_3b)                          AS triples,
                    SUM(is_bb)                          AS walks,
                    SUM(is_k)                           AS strikeouts,
                    SUM(is_gb)                          AS groundballs,
                    SUM(is_fb)                          AS flyballs,
                    SUM(runs_on_pitch)                  AS runs,
                    -- vs-LHB numerators
                    SUM(is_hr * is_vs_l)                AS hr_l,
                    SUM(is_1b * is_vs_l)                AS singles_l,
                    SUM(is_2b * is_vs_l)                AS doubles_l,
                    SUM(is_3b * is_vs_l)                AS triples_l,
                    SUM(is_bb * is_vs_l)                AS walks_l,
                    SUM(is_k  * is_vs_l)                AS strikeouts_l,
                    SUM(is_gb * is_vs_l)                AS groundballs_l,
                    SUM(is_fb * is_vs_l)                AS flyballs_l,
                    SUM(runs_on_pitch * is_vs_l)        AS runs_l,
                    -- vs-RHB numerators
                    SUM(is_hr * is_vs_r)                AS hr_r,
                    SUM(is_1b * is_vs_r)                AS singles_r,
                    SUM(is_2b * is_vs_r)                AS doubles_r,
                    SUM(is_3b * is_vs_r)                AS triples_r,
                    SUM(is_bb * is_vs_r)                AS walks_r,
                    SUM(is_k  * is_vs_r)                AS strikeouts_r,
                    SUM(is_gb * is_vs_r)                AS groundballs_r,
                    SUM(is_fb * is_vs_r)                AS flyballs_r,
                    SUM(runs_on_pitch * is_vs_r)        AS runs_r
                FROM pa
                GROUP BY venue_id, season
            ),
            league_rates AS (
                SELECT
                    season,
                    SUM(hr_total) * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_hr_rate,
                    SUM(singles)  * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_1b_rate,
                    SUM(doubles)  * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_2b_rate,
                    SUM(triples)  * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_3b_rate,
                    SUM(walks)    * 1.0 / NULLIF(SUM(sample_pa), 0)     AS lg_bb_rate,
                    SUM(strikeouts) * 1.0 / NULLIF(SUM(sample_pa), 0)   AS lg_k_rate,
                    SUM(groundballs) * 1.0 / NULLIF(SUM(sample_pa), 0)  AS lg_gb_rate,
                    SUM(flyballs)    * 1.0 / NULLIF(SUM(sample_pa), 0)  AS lg_fb_rate,
                    SUM(runs)        * 1.0 / NULLIF(SUM(sample_pa), 0)  AS lg_r_rate,
                    -- vs-LHB league rates
                    SUM(hr_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0)       AS lg_hr_rate_l,
                    SUM(singles_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0)  AS lg_1b_rate_l,
                    SUM(doubles_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0)  AS lg_2b_rate_l,
                    SUM(triples_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0)  AS lg_3b_rate_l,
                    SUM(walks_l)   * 1.0 / NULLIF(SUM(sample_pa_l), 0)  AS lg_bb_rate_l,
                    SUM(strikeouts_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0) AS lg_k_rate_l,
                    SUM(groundballs_l) * 1.0 / NULLIF(SUM(sample_pa_l), 0) AS lg_gb_rate_l,
                    SUM(flyballs_l)    * 1.0 / NULLIF(SUM(sample_pa_l), 0) AS lg_fb_rate_l,
                    SUM(runs_l)        * 1.0 / NULLIF(SUM(sample_pa_l), 0) AS lg_r_rate_l,
                    -- vs-RHB league rates
                    SUM(hr_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0)       AS lg_hr_rate_r,
                    SUM(singles_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0)  AS lg_1b_rate_r,
                    SUM(doubles_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0)  AS lg_2b_rate_r,
                    SUM(triples_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0)  AS lg_3b_rate_r,
                    SUM(walks_r)   * 1.0 / NULLIF(SUM(sample_pa_r), 0)  AS lg_bb_rate_r,
                    SUM(strikeouts_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0) AS lg_k_rate_r,
                    SUM(groundballs_r) * 1.0 / NULLIF(SUM(sample_pa_r), 0) AS lg_gb_rate_r,
                    SUM(flyballs_r)    * 1.0 / NULLIF(SUM(sample_pa_r), 0) AS lg_fb_rate_r,
                    SUM(runs_r)        * 1.0 / NULLIF(SUM(sample_pa_r), 0) AS lg_r_rate_r
                FROM venue_rates
                GROUP BY season
            ),
            factors AS (
                SELECT
                    vr.venue_id,
                    vr.season,
                    vr.sample_pa,
                    -- Overall factor = venue rate / league rate
                    (vr.hr_total * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_hr_rate, 0)  AS factor_hr,
                    (vr.singles  * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_1b_rate, 0)  AS factor_1b,
                    (vr.doubles  * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_2b_rate, 0)  AS factor_2b,
                    (vr.triples  * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_3b_rate, 0)  AS factor_3b,
                    (vr.walks    * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_bb_rate, 0)  AS factor_bb,
                    (vr.strikeouts * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_k_rate, 0) AS factor_k,
                    (vr.groundballs * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_gb_rate, 0) AS factor_gb,
                    (vr.flyballs * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_fb_rate, 0)  AS factor_fb,
                    (vr.runs     * 1.0 / NULLIF(vr.sample_pa,0)) / NULLIF(lr.lg_r_rate, 0)   AS factor_r,
                    -- vs-LHB factor = venue vs-L rate / league vs-L rate
                    (vr.hr_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_hr_rate_l, 0)  AS factor_hr_l,
                    (vr.singles_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_1b_rate_l, 0) AS factor_1b_l,
                    (vr.doubles_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_2b_rate_l, 0) AS factor_2b_l,
                    (vr.triples_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_3b_rate_l, 0) AS factor_3b_l,
                    (vr.walks_l   * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_bb_rate_l, 0) AS factor_bb_l,
                    (vr.strikeouts_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_k_rate_l, 0) AS factor_k_l,
                    (vr.groundballs_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_gb_rate_l, 0) AS factor_gb_l,
                    (vr.flyballs_l * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_fb_rate_l, 0) AS factor_fb_l,
                    (vr.runs_l     * 1.0 / NULLIF(vr.sample_pa_l,0)) / NULLIF(lr.lg_r_rate_l, 0)  AS factor_r_l,
                    -- vs-RHB factor = venue vs-R rate / league vs-R rate
                    (vr.hr_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_hr_rate_r, 0)  AS factor_hr_r,
                    (vr.singles_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_1b_rate_r, 0) AS factor_1b_r,
                    (vr.doubles_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_2b_rate_r, 0) AS factor_2b_r,
                    (vr.triples_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_3b_rate_r, 0) AS factor_3b_r,
                    (vr.walks_r   * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_bb_rate_r, 0) AS factor_bb_r,
                    (vr.strikeouts_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_k_rate_r, 0) AS factor_k_r,
                    (vr.groundballs_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_gb_rate_r, 0) AS factor_gb_r,
                    (vr.flyballs_r * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_fb_rate_r, 0) AS factor_fb_r,
                    (vr.runs_r     * 1.0 / NULLIF(vr.sample_pa_r,0)) / NULLIF(lr.lg_r_rate_r, 0)  AS factor_r_r
                FROM venue_rates vr
                JOIN league_rates lr ON vr.season = lr.season
            ),
            -- Melt the overall / vs-L / vs-R factor groups together so each
            -- factor_type yields ONE row carrying overall + both splits.
            melted AS (
                SELECT
                    venue_id, season, sample_pa,
                    factor_type, factor_overall, factor_vs_l, factor_vs_r
                FROM factors
                -- INCLUDE NULLS so every factor_type always yields a row even
                -- when a venue/league rate is undefined (0 events). The outer
                -- COALESCE then makes factor_overall the neutral 1.0, honoring
                -- the NOT NULL constraint on derived.park_factors.factor_overall.
                UNPIVOT INCLUDE NULLS (
                    (factor_overall, factor_vs_l, factor_vs_r)
                    FOR factor_type IN (
                        (factor_hr, factor_hr_l, factor_hr_r) AS 'HR',
                        (factor_1b, factor_1b_l, factor_1b_r) AS '1B',
                        (factor_2b, factor_2b_l, factor_2b_r) AS '2B',
                        (factor_3b, factor_3b_l, factor_3b_r) AS '3B',
                        (factor_bb, factor_bb_l, factor_bb_r) AS 'BB',
                        (factor_k,  factor_k_l,  factor_k_r)  AS 'K',
                        (factor_gb, factor_gb_l, factor_gb_r) AS 'GB',
                        (factor_fb, factor_fb_l, factor_fb_r) AS 'FB',
                        (factor_r,  factor_r_l,  factor_r_r)  AS 'R'
                    )
                )
            )
            -- One row per (venue, season, factor_type) with real L/R splits.
            SELECT
                venue_id,
                season,
                factor_type,
                COALESCE(factor_overall, 1.0) AS factor_overall,
                factor_vs_l,
                factor_vs_r,
                sample_pa,
                -- Bayesian shrinkage: posterior = (observed * N + 1.0 * prior_weight) / (N + prior_weight)
                -- where prior_weight = PARK_PRIOR_PA. Applied to the overall factor;
                -- the L/R splits are stored raw (callers shrink with their own
                -- vs-hand sample if desired).
                (COALESCE(factor_overall, 1.0) * sample_pa + 1.0 * {PARK_PRIOR_PA})
                    / (sample_pa + {PARK_PRIOR_PA}) AS regressed_factor
            FROM melted
        """)
        log.info("  Park factors done.")

    def _compute_pitcher_profiles(self, seasons: list[int]) -> None:
        """
        Two-pass approach:
          Pass 1 (SQL): compute all command metrics and result stats in DuckDB.
          Pass 2 (Python): fit GMM for each pitcher/season, write model JSON
                           and component rows.
        """
        log.info("Computing pitcher profiles …")
        season_list = ", ".join(str(s) for s in seasons)

        # Delete child rows first so INSERT OR REPLACE on pitcher_season_metrics
        # can implicitly remove the old parent row without a FK violation.
        self._conn.execute(
            f"DELETE FROM derived.pitcher_gmm_components WHERE season IN ({season_list})"
        )

        # --- Pass 1: command + result metrics ----------------------------
        self._conn.execute(f"""
                INSERT OR REPLACE INTO derived.pitcher_season_metrics (
                    pitcher_id, season, p_throws, sample_pitches,
                    gmm_model,
                    bb_rate, k_rate, csw_rate, zone_take_rate, chase_rate, zone_rate, whiff_rate,
                    era, fip, xfip, ground_ball_rate, fly_ball_rate,
                    line_drive_rate, hr_per_9, whip,
                    below_minimum_sample
                )
                WITH pitch_stats AS (
                    SELECT
                        pitcher                         AS pitcher_id,
                        season,
                        MAX(p_throws)                   AS p_throws,
                        COUNT(*)                        AS sample_pitches,

                        -- Command metrics
                        -- O-swing: swung at pitches outside zone / pitches outside zone
                        SUM(CASE WHEN zone NOT BETWEEN 1 AND 9 AND type IN ('D','E','F','L','M','O','S','T','W','X')
                            THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN zone NOT BETWEEN 1 AND 9 THEN 1 ELSE 0 END),0)
                            AS chase_rate,

                        -- Z-take: pitches taken inside the zone / pitches inside the zone
                        SUM(CASE WHEN zone BETWEEN 1 AND 9 AND type = 'C' THEN 1.0 ELSE 0 END) /
                            NULLIF(SUM(CASE WHEN zone BETWEEN 1 AND 9 THEN 1 ELSE 0 END),0) AS zone_take_rate,

                        -- Whiff Rate
                        SUM(CASE WHEN type = 'C' THEN 1.0 ELSE 0 END) / COUNT(*) AS whiff_rate,

                        -- Zone rate: pitches in zone / total
                        SUM(CASE WHEN zone BETWEEN 1 AND 9 THEN 1.0 ELSE 0 END) / COUNT(*) AS zone_rate,

                        -- Called Strike + Whiff Rate
                        SUM(CASE WHEN type IN ('C','M','O','S','T','W') THEN 1.0 ELSE 0 END) / COUNT(*) AS csw_rate,

                        -- Count PA-level outcomes (use last pitch of each PA)
                        COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk||'-'||at_bat_number END) AS total_pa,

                        SUM(CASE WHEN events IN ('walk','intent_walk') THEN 1 ELSE 0 END) AS walks,
                        SUM(CASE WHEN events IN ('strikeout','strikeout_double_play') THEN 1 ELSE 0 END) AS strikeouts,
                        SUM(CASE WHEN events = 'home_run' THEN 1 ELSE 0 END) AS home_runs,
                        SUM(CASE WHEN events IN ('single','double','triple','home_run') THEN 1 ELSE 0 END) AS hits,
                        SUM(CASE WHEN events = 'hit_by_pitch' THEN 1 ELSE 0 END) AS hbp,

                        SUM(earned_runs_on_pitch)       AS earned_runs,
                        SUM(outs_on_pitch)              AS outs_recorded,

                        -- Batted ball types (of balls in play only)
                        SUM(CASE WHEN bb_type = 'ground_ball' THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN type='X'
                            THEN 1 ELSE 0 END),0) AS ground_ball_rate,
                        SUM(CASE WHEN bb_type IN ('fly_ball','popup') THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN
                            type='X' THEN 1 ELSE 0 END),0) AS fly_ball_rate,
                        SUM(CASE WHEN bb_type = 'line_drive' THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN type='X'
                            THEN 1 ELSE 0 END),0) AS line_drive_rate,
                        SUM(CASE WHEN bb_type IN ('fly_ball','popup') THEN 1 ELSE 0 END) AS fly_balls_total

                    FROM pg.raw.pitches
                    WHERE data_quality_flag = FALSE
                      AND season IN ({season_list})
                    GROUP BY pitcher, season
                )
                SELECT
                    pitcher_id,
                    season,
                    p_throws,
                    sample_pitches,

                    -- GMM model filled in Pass 2; placeholder until then
                    '{{"n_components": 0, "note": "pending_gmm_fit"}}'::JSON AS gmm_model,

                    walks * 1.0 / NULLIF(total_pa, 0)      AS bb_rate,
                    strikeouts * 1.0 / NULLIF(total_pa, 0) AS k_rate,
                    csw_rate,
                    zone_take_rate,
                    chase_rate,
                    zone_rate,
                    whiff_rate,

                    -- ERA: (earned_runs / outs_recorded) * 27
                    (earned_runs * 27.0) / NULLIF(outs_recorded, 0)  AS era,

                    -- FIP = (13*HR + 3*(BB+HBP) - 2*K) / IP + FIP_CONSTANT
                    -- IP = outs_recorded / 3
                    ({FIP_CONSTANT} + (13.0 * home_runs + 3.0 * (walks + hbp) - 2.0 * strikeouts) /
                        NULLIF(outs_recorded / 3.0, 0)) AS fip,

                    -- xFIP: substitute expected HR (league HR/FB * actual FB) for actual HR
                    -- Uses a fixed league HR/FB rate of ~0.105 (MLB average)
                    ({FIP_CONSTANT} + (13.0 * (fly_balls_total * 0.105) + 3.0 * (walks + hbp) - 2.0 * strikeouts)
                        / NULLIF(outs_recorded / 3.0, 0)) AS xfip,

                    ground_ball_rate,
                    fly_ball_rate,
                    line_drive_rate,

                    -- HR/9 = home_runs / IP * 9
                    (home_runs * 9.0) / NULLIF(outs_recorded / 3.0, 0) AS hr_per_9,

                    -- WHIP = (walks + hits) / IP
                    ((walks + hits) * 3.0) / NULLIF(outs_recorded, 0)   AS whip,

                    (sample_pitches < {MIN_PITCHER_PITCHES})             AS below_minimum_sample,
                    CURRENT_TIMESTAMP                                    AS updated_at

                FROM pitch_stats
            """)
        log.info("  Pitcher command metrics inserted (GMM pending).")

        # --- Pass 2: GMM fitting -----------------------------------------
        # Performance design:
        #   BEFORE: one pg.raw.pitches query per pitcher/season (~5,600 round-trips
        #           for 700 pitchers × 8 seasons), all fits sequential.
        #   AFTER:  one query per season fetches all pitch vectors at once,
        #           split by pitcher in pandas. Fitting parallelised across
        #           CPU cores with ProcessPoolExecutor.
        #
        # Fetch qualifying (pitcher_id, season) pairs
        qualifying = self._conn.execute(
            f"""
                SELECT DISTINCT pitcher_id, season
                FROM derived.pitcher_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                ORDER BY season, pitcher_id
                """
        ).fetchall()

        log.info("  Fitting GMMs for %d pitcher/season pairs …", len(qualifying))

        # Group by season so we issue one bulk fetch per season rather than
        # one per pitcher. A single season is ~700K rows — well within memory.
        from collections import defaultdict

        by_season: dict[int, list[int]] = defaultdict(list)
        for pitcher_id, season in qualifying:
            by_season[season].append(pitcher_id)

        # Collect all (pitcher_id, season, pitches_df) tuples for parallel fitting
        fit_tasks: list[tuple[int, int, pd.DataFrame]] = []

        for season, pitcher_ids in sorted(by_season.items()):
            id_list = ", ".join(str(p) for p in pitcher_ids)
            log.info(
                "  Fetching pitch vectors for season %s (%d pitchers) …", season, len(pitcher_ids)
            )

            season_df = self._conn.execute(
                f"""
                    SELECT
                        pitcher                     AS pitcher_id,
                        release_speed               AS velo,
                        break_vertical_induced      AS ivb,
                        break_horizontal            AS hb,
                        release_spin_rate           AS spin_rate,
                        spin_axis,
                        release_pos_x               AS release_x,
                        release_pos_z               AS release_z,
                        release_extension           AS release_ext
                    FROM pg.raw.pitches
                    WHERE pitcher IN ({id_list})
                      AND season  = {season}
                      AND data_quality_flag = FALSE
                      AND release_speed IS NOT NULL
                    """
            ).df()

            for pitcher_id in pitcher_ids:
                pitcher_df = season_df[season_df["pitcher_id"] == pitcher_id].drop(
                    columns=["pitcher_id"]
                )
                fit_tasks.append((pitcher_id, season, pitcher_df))

        # Parallel GMM fitting — chunked across CPU cores (SIM-113).
        from concurrent.futures import ProcessPoolExecutor, as_completed

        # Compact float32 feature arrays (not DataFrames) keep per-task IPC small.
        array_tasks = [
            (df[GMM_FEATURE_NAMES].to_numpy(dtype=np.float32), pid, ssn)
            for pid, ssn, df in fit_tasks
        ]
        n_workers = _resolve_gmm_workers(len(array_tasks))
        chunks = _chunk_tasks(array_tasks, n_workers)
        log.info(
            "  Fitting GMMs: %d pitchers across %d workers (%d chunks) …",
            len(array_tasks),
            n_workers,
            len(chunks),
        )

        fitted: list[tuple[int, int, dict, list[dict]]] = []
        fallbacks: list[tuple[int, int]] = []

        # _fit_gmm_batch is a module-level function so it pickles cleanly.
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            future_to_chunk = {pool.submit(_fit_gmm_batch, chunk): chunk for chunk in chunks}
            for future in as_completed(future_to_chunk):
                try:
                    chunk_results = future.result()
                except Exception as exc:
                    # Whole-chunk crash: flag every pitcher in it as fallback,
                    # preserving the prior per-pitcher crash semantics.
                    log.error("GMM worker chunk crashed: %s", exc)
                    fallbacks.extend((pid, ssn) for _arr, pid, ssn in future_to_chunk[future])
                    continue
                for pitcher_id, season, model_json, component_rows in chunk_results:
                    if model_json is None:
                        fallbacks.append((pitcher_id, season))
                    else:
                        fitted.append((pitcher_id, season, model_json, component_rows))

        # Batched DuckDB writes (SIM-113): a few set-based statements instead
        # of one UPDATE/INSERT per pitcher.
        _flush_gmm_results(self._conn, fitted, fallbacks)

        gmm_success = len(fitted)
        gmm_fallback = len(fallbacks)

        log.info(
            "  GMM fitting complete: %d fitted, %d fell back to minimum-sample flag.",
            gmm_success,
            gmm_fallback,
        )

    def _compute_batter_profiles(self, seasons: list[int]) -> None:
        log.info("Computing batter profiles …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.batter_season_metrics

            WITH batter_pitches AS (
                SELECT
                    batter                              AS batter_id,
                    season,
                    MAX(bat_hand)                       AS bats,
                    COUNT(*)                            AS sample_pitches,

                    -- PA count (events is non-null on the last pitch of each PA)
                    COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk || '-' || at_bat_number END) AS sample_pa,

                    -- Plate discipline
                    -- First-pitch take: did NOT swing on pitch_number=1
                    SUM(CASE WHEN pitch_number = 1 AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1.0 ELSE 0 END) /
                        NULLIF(COUNT(CASE WHEN pitch_number = 1 THEN 1 END), 0) AS first_pitch_take_rate,

                    -- O-swing
                    SUM(CASE WHEN zone NOT BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1.0 ELSE 0
                        END) / NULLIF(SUM(CASE WHEN zone NOT BETWEEN 1 AND 9 THEN 1 ELSE 0 END), 0) AS o_swing_rate,

                    -- Z-swing
                    SUM(CASE WHEN zone BETWEEN 1 AND 9 AND type IN ('B', 'C', 'H', 'P', '*B') THEN 1.0 ELSE 0 END)
                        * 1.0 / NULLIF(SUM(CASE WHEN zone BETWEEN 1 AND 9 THEN 1 ELSE 0 END), 0) AS z_swing_rate,

                    -- Whiff: swinging strikes / swings
                    SUM(CASE WHEN type IN ('M', 'O', 'S', 'T') THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN type NOT IN
                        ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END), 0) AS whiff_rate,

                    -- Contact: in-play / swings
                    SUM(CASE WHEN type IN ('D', 'E', 'F', 'X') THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN type NOT IN
                        ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END), 0) AS contact_rate,

                    -- Walk / K rates (per PA)
                    SUM(CASE WHEN events IN ('walk','intent_walk') THEN 1.0 ELSE 0 END) /
                        NULLIF(COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk || '-' || at_bat_number END), 0)
                        AS walk_rate,

                    SUM(CASE WHEN events IN ('strikeout','strikeout_double_play') THEN 1.0 ELSE 0 END) /
                        NULLIF(COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk || '-' || at_bat_number END), 0)
                        AS k_rate,

                    -- Batted ball profile (of BIP)
                    SUM(CASE WHEN bb_type = 'ground_ball' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS gb_rate,

                    SUM(CASE WHEN bb_type = 'fly_ball' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS fb_rate,

                    -- Spray direction (hc_x relative to center of field = 125.42)
                    SUM(CASE WHEN spray_angle < -15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS pull_rate,

                    SUM(CASE WHEN spray_angle > 15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS oppo_rate,

                    -- Exit velocity & launch angle (on contact only)
                    AVG(CASE WHEN launch_speed IS NOT NULL THEN launch_speed END) AS avg_exit_velo,
                    MAX(launch_speed)                   AS max_exit_velo,
                    AVG(CASE WHEN launch_angle IS NOT NULL THEN launch_angle END) AS avg_launch_angle,

                    -- Hard hit rate (>= 95 mph exit velo)
                    SUM(CASE WHEN launch_speed >= {HARD_HIT_MIN_VELO} THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS hard_hit_rate,

                    -- Barrel rate (Statcast sliding-scale: EV>=98 & LA band widening with EV)
                    SUM(CASE WHEN {_barrel_case_sql()} THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS barrel_rate,

                    -- HR rate per PA
                    SUM(CASE WHEN events = 'home_run' THEN 1.0 ELSE 0 END) /
                        NULLIF(COUNT(DISTINCT CASE WHEN events IS NOT NULL THEN game_pk || '-' || at_bat_number END), 0)
                        AS hr_rate,

                    -- Platoon split sample sizes
                    COUNT(DISTINCT CASE WHEN p_throws = 'L' AND events IS NOT NULL THEN game_pk || '-' || at_bat_number
                        END) AS sample_pa_vs_l,
                    COUNT(DISTINCT CASE WHEN p_throws = 'R' AND events IS NOT NULL THEN game_pk || '-' || at_bat_number
                        END) AS sample_pa_vs_r,

                    -- Platoon splits — vs LHP
                    SUM(CASE WHEN p_throws='L' AND zone NOT BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B')
                        THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN p_throws='L' AND zone NOT BETWEEN 1 AND 9 THEN 1
                        ELSE 0 END), 0) AS o_swing_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND zone BETWEEN 1 AND 9 AND type IN ('B', 'C', 'H', 'P', '*B') THEN 1.0
                        ELSE 0 END) / NULLIF(SUM(CASE WHEN p_throws='L' AND zone BETWEEN 1 AND 9 THEN 1 ELSE 0 END), 0)
                        AS z_swing_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND type IN ('M', 'O', 'S', 'T') THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE
                        WHEN p_throws='L' AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END), 0)
                        AS whiff_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND type IN ('D', 'E', 'F', 'X') THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END)
                        , 0) AS contact_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND events IN ('walk','intent_walk') THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND events IS NOT NULL THEN 1 ELSE 0 END), 0)
                        AS walk_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND events IN ('strikeout','strikeout_double_play') THEN 1.0 ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN p_throws='L' AND events IS NOT NULL THEN 1 ELSE 0 END), 0) AS k_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND bb_type='ground_ball' THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN
                        p_throws='L' AND type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS gb_rate_vs_l,
                    SUM(CASE WHEN p_throws='L' AND bb_type = 'fly_ball' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS fb_rate_vs_l,
                    SUM(CASE WHEN spray_angle < -15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type='X' THEN 1 ELSE 0 END), 0) AS pull_rate_vs_l,
                    SUM(CASE WHEN spray_angle > 15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type='X' THEN 1 ELSE 0 END), 0) AS oppo_rate_vs_l,
                    AVG(CASE WHEN p_throws='L' AND launch_speed IS NOT NULL THEN launch_speed END) AS avg_exit_velo_vs_l,
                    SUM(CASE WHEN {_barrel_case_sql("p_throws='L' AND ")} THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='L' AND type='X' THEN 1 ELSE 0 END), 0) AS barrel_rate_vs_l,

                    -- Platoon splits — vs RHP
                    SUM(CASE WHEN p_throws='R' AND zone NOT BETWEEN 1 AND 9 AND type NOT IN ('B', 'C', 'H', 'P', '*B')
                        THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN p_throws='R' AND zone NOT BETWEEN 1 AND 9 THEN 1
                        ELSE 0 END), 0) AS o_swing_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND zone BETWEEN 1 AND 9 AND type IN ('B', 'C', 'H', 'P', '*B') THEN 1.0
                        ELSE 0 END) / NULLIF(SUM(CASE WHEN p_throws='R' AND zone BETWEEN 1 AND 9 THEN 1 ELSE 0 END), 0)
                        AS z_swing_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND type IN ('M', 'O', 'S', 'T') THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE
                        WHEN p_throws='R' AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END), 0)
                        AS whiff_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND type IN ('D', 'E', 'F', 'X') THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND type NOT IN ('B', 'C', 'H', 'P', '*B') THEN 1 ELSE 0 END)
                        , 0) AS contact_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND events IN ('walk','intent_walk') THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND events IS NOT NULL THEN 1 ELSE 0 END), 0)
                        AS walk_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND events IN ('strikeout','strikeout_double_play') THEN 1.0 ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN p_throws='R' AND events IS NOT NULL THEN 1 ELSE 0 END), 0) AS k_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND bb_type='ground_ball' THEN 1.0 ELSE 0 END) / NULLIF(SUM(CASE WHEN
                        p_throws='R' AND type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS gb_rate_vs_r,
                    SUM(CASE WHEN p_throws='R' AND bb_type = 'fly_ball' THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND type IN ('D', 'E', 'X') THEN 1 ELSE 0 END), 0) AS fb_rate_vs_r,
                    SUM(CASE WHEN spray_angle < -15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type='X' THEN 1 ELSE 0 END), 0) AS pull_rate_vs_r,
                    SUM(CASE WHEN spray_angle > 15 THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN type='X' THEN 1 ELSE 0 END), 0) AS oppo_rate_vs_r,
                    AVG(CASE WHEN p_throws='R' AND launch_speed IS NOT NULL THEN launch_speed END) AS avg_exit_velo_vs_r,
                    SUM(CASE WHEN {_barrel_case_sql("p_throws='R' AND ")} THEN 1.0 ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN p_throws='R' AND type='X' THEN 1 ELSE 0 END), 0) AS barrel_rate_vs_r

                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                GROUP BY batter, season
            )
            SELECT
                batter_id,
                season,
                sample_pitches,
                sample_pa,
                bats,
                first_pitch_take_rate,
                o_swing_rate,
                z_swing_rate,
                whiff_rate,
                contact_rate,
                walk_rate,
                k_rate,
                gb_rate,
                fb_rate,
                pull_rate,
                oppo_rate,
                avg_exit_velo,
                max_exit_velo,
                avg_launch_angle,
                hard_hit_rate,
                barrel_rate,
                hr_rate,
                -- vs LHP
                o_swing_rate_vs_l,
                z_swing_rate_vs_l,
                whiff_rate_vs_l,
                contact_rate_vs_l,
                walk_rate_vs_l,
                k_rate_vs_l,
                gb_rate_vs_l,
                fb_rate_vs_l,
                pull_rate_vs_l,
                oppo_rate_vs_l,
                avg_exit_velo_vs_l,
                barrel_rate_vs_l,
                sample_pa_vs_l,
                -- vs RHP
                o_swing_rate_vs_r,
                z_swing_rate_vs_r,
                whiff_rate_vs_r,
                contact_rate_vs_r,
                walk_rate_vs_r,
                k_rate_vs_r,
                gb_rate_vs_r,
                fb_rate_vs_r,
                pull_rate_vs_r,
                oppo_rate_vs_r,
                avg_exit_velo_vs_r,
                barrel_rate_vs_r,
                sample_pa_vs_r,
                (sample_pa < {MIN_BATTER_PA}) AS below_minimum_sample,
                CURRENT_TIMESTAMP                                        AS updated_at
            FROM batter_pitches
        """)
        log.info("  Batter profiles done.")

    def _compute_baserunner_profiles(self, seasons: list[int]) -> None:
        """
        Computes extra-base advancement rates (attempt/success split) and
        stolen base rates from post-play runner columns, runner-out-advancing
        flags, and SB columns in raw.pitches.

        Attempt = runner tried to advance (succeeded OR thrown out).
        Success = runner reached the next base safely.
        Stop rate = 1 - attempt_rate (held up on advancement opportunity).

        Sprint speed: not directly available in raw.pitches.  Derived as a
        proxy from advancement aggressiveness (extra_base_attempt_rate weighted
        by situation).  Phase 2 can inject Statcast sprint speed data if
        a separate fetch is added to the ETL.
        """
        log.info("Computing baserunner profiles …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.baserunner_season_metrics

            WITH runner_events AS (
                SELECT
                    season,
                    -- Runners on each base pre-pitch
                    on_1b, on_2b, on_3b,
                    -- Post-pitch runner positions
                    post_on_1b, post_on_2b, post_on_3b,
                    -- Runner scoring flags
                    runner_1b_scored, runner_2b_scored, runner_3b_scored,
                    -- Runner thrown out advancing
                    runner_1b_out_advancing, runner_2b_out_advancing, runner_3b_out_advancing,
                    -- Hit type for advancement context
                    events, bb_type,
                    -- SB data
                    sb_attempt_2b, sb_attempt_3b, sb_attempt_home,
                    sb_success_2b, sb_success_3b, sb_success_home
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                  AND (
                      on_1b IS NOT NULL OR on_2b IS NOT NULL OR on_3b IS NOT NULL
                      OR sb_attempt_2b = TRUE OR sb_attempt_3b = TRUE OR sb_attempt_home = TRUE
                  )
            ),
            -- First-to-third on singles (runner on 1B, no one on 2B)
            first_to_third AS (
                SELECT season, on_1b AS player_id,
                    COUNT(*) AS opps,
                    SUM(CASE WHEN runner_1b_scored = TRUE
                             OR post_on_3b = on_1b
                             OR runner_1b_out_advancing = TRUE THEN 1 ELSE 0 END) AS attempts,
                    SUM(CASE WHEN runner_1b_scored = TRUE
                             OR post_on_3b = on_1b THEN 1 ELSE 0 END) AS success
                FROM runner_events
                WHERE events = 'single' AND on_1b IS NOT NULL AND on_2b IS NULL
                GROUP BY season, on_1b
            ),
            -- Second-to-home on singles
            second_to_home AS (
                SELECT season, on_2b AS player_id,
                    COUNT(*) AS opps,
                    SUM(CASE WHEN runner_2b_scored = TRUE
                             OR runner_2b_out_advancing = TRUE THEN 1 ELSE 0 END) AS attempts,
                    SUM(CASE WHEN runner_2b_scored = TRUE THEN 1 ELSE 0 END) AS success
                FROM runner_events
                WHERE events = 'single' AND on_2b IS NOT NULL
                GROUP BY season, on_2b
            ),
            -- First-to-home on doubles
            first_to_home AS (
                SELECT season, on_1b AS player_id,
                    COUNT(*) AS opps,
                    SUM(CASE WHEN runner_1b_scored = TRUE
                             OR runner_1b_out_advancing = TRUE THEN 1 ELSE 0 END) AS attempts,
                    SUM(CASE WHEN runner_1b_scored = TRUE THEN 1 ELSE 0 END) AS success
                FROM runner_events
                WHERE events = 'double' AND on_1b IS NOT NULL
                GROUP BY season, on_1b
            ),
            -- Tag-up on fly balls (third-to-home)
            tag_up AS (
                SELECT season, on_3b AS player_id,
                    COUNT(*) AS opps,
                    SUM(CASE WHEN runner_3b_scored = TRUE
                             OR runner_3b_out_advancing = TRUE THEN 1 ELSE 0 END) AS attempts,
                    SUM(CASE WHEN runner_3b_scored = TRUE THEN 1 ELSE 0 END) AS success
                FROM runner_events
                WHERE bb_type IN ('fly_ball','popup') AND on_3b IS NOT NULL
                GROUP BY season, on_3b
            ),
            -- Overall extra-base (aggregate all advancement opportunities)
            extra_base AS (
                SELECT season, player_id,
                    SUM(opps) AS total_opps,
                    SUM(attempts) AS total_attempts,
                    SUM(success) AS total_success
                FROM (
                    SELECT season, player_id, opps, attempts, success FROM first_to_third
                    UNION ALL
                    SELECT season, player_id, opps, attempts, success FROM second_to_home
                    UNION ALL
                    SELECT season, player_id, opps, attempts, success FROM first_to_home
                    UNION ALL
                    SELECT season, player_id, opps, attempts, success FROM tag_up
                ) combined
                GROUP BY season, player_id
            ),
            -- Stolen base stats
            sb_stats AS (
                SELECT season,
                    -- Player attempting the steal is the runner on the lead base
                    CASE
                        WHEN sb_attempt_home = TRUE AND on_3b IS NOT NULL THEN on_3b
                        WHEN sb_attempt_3b  = TRUE AND on_2b IS NOT NULL THEN on_2b
                        WHEN sb_attempt_2b  = TRUE AND on_1b IS NOT NULL THEN on_1b
                    END AS player_id,
                    CASE
                        WHEN sb_attempt_2b  = TRUE THEN sb_success_2b
                        WHEN sb_attempt_3b  = TRUE THEN sb_success_3b
                        WHEN sb_attempt_home = TRUE THEN sb_success_home
                    END AS sb_success
                FROM runner_events
                WHERE sb_attempt_2b = TRUE OR sb_attempt_3b = TRUE OR sb_attempt_home = TRUE
            ),
            sb_agg AS (
                SELECT season, player_id,
                    COUNT(*) AS sb_attempts,
                    SUM(CASE WHEN sb_success THEN 1 ELSE 0 END) AS sb_successes
                FROM sb_stats
                WHERE player_id IS NOT NULL
                GROUP BY season, player_id
            ),
            all_players AS (
                SELECT DISTINCT season, player_id FROM extra_base
                UNION
                SELECT DISTINCT season, player_id FROM sb_agg
            )
            SELECT
                ap.player_id,
                ap.season,
                -- Sprint speed proxy: scale from extra-base aggression
                -- (higher attempt rate + success rate → faster implied sprint speed)
                -- Bounded to reasonable ft/s range: 24-31 ft/s
                ss.sprint_speed                                   AS sprint_speed,

                -- Overall extra-base attempt / success
                eb.total_attempts * 1.0 / NULLIF(eb.total_opps, 0)
                                                                AS extra_base_attempt_rate,
                eb.total_success * 1.0 / NULLIF(eb.total_attempts, 0)
                                                                AS extra_base_success_rate,

                -- Per-situation attempt / success rates
                ftt.attempts * 1.0 / NULLIF(ftt.opps, 0)       AS first_to_third_attempt_rate,
                ftt.success  * 1.0 / NULLIF(ftt.attempts, 0)   AS first_to_third_success_rate,

                sth.attempts * 1.0 / NULLIF(sth.opps, 0)       AS second_to_home_attempt_rate,
                sth.success  * 1.0 / NULLIF(sth.attempts, 0)   AS second_to_home_success_rate,

                fth.attempts * 1.0 / NULLIF(fth.opps, 0)       AS first_to_home_attempt_rate,
                fth.success  * 1.0 / NULLIF(fth.attempts, 0)   AS first_to_home_success_rate,

                tu.attempts  * 1.0 / NULLIF(tu.opps, 0)        AS tag_up_attempt_rate,
                tu.success   * 1.0 / NULLIF(tu.attempts, 0)    AS tag_up_success_rate,

                -- Stop rate = 1 - overall attempt rate
                1.0 - COALESCE(eb.total_attempts * 1.0 / NULLIF(eb.total_opps, 0), 0)
                                                                AS stop_rate,

                -- SB rates
                sba.sb_attempts * 1.0 / NULLIF(eb.total_opps, 0)    AS sb_attempt_rate,
                sba.sb_successes * 1.0 / NULLIF(sba.sb_attempts, 0) AS sb_success_rate,
                (sba.sb_attempts - sba.sb_successes) * 1.0
                    / NULLIF(sba.sb_attempts, 0)                AS cs_rate,

                -- Per-situation sample sizes
                COALESCE(eb.total_opps, 0)                      AS sample_advancement_opps,
                COALESCE(ftt.opps, 0)                           AS sample_first_to_third_opps,
                COALESCE(sth.opps, 0)                           AS sample_second_to_home_opps,
                COALESCE(fth.opps, 0)                           AS sample_first_to_home_opps,
                COALESCE(tu.opps, 0)                            AS sample_tag_up_opps,
                COALESCE(sba.sb_attempts, 0)                    AS sample_sb_attempts,

                (COALESCE(eb.total_opps, 0) < {MIN_RUNNER_ADV_OPPS}
                 OR COALESCE(sba.sb_attempts, 0) < {MIN_RUNNER_SB_ATTEMPTS})
                                                                AS below_minimum_sample,
                CURRENT_TIMESTAMP                               AS updated_at
            FROM all_players ap
            LEFT JOIN extra_base eb ON ap.player_id = eb.player_id AND ap.season = eb.season
            LEFT JOIN first_to_third ftt ON ap.player_id = ftt.player_id AND ap.season = ftt.season
            LEFT JOIN second_to_home sth ON ap.player_id = sth.player_id AND ap.season = sth.season
            LEFT JOIN first_to_home fth  ON ap.player_id = fth.player_id AND ap.season = fth.season
            LEFT JOIN tag_up tu          ON ap.player_id = tu.player_id  AND ap.season = tu.season
            LEFT JOIN sb_agg sba         ON ap.player_id = sba.player_id AND ap.season = sba.season
            LEFT JOIN pg.raw.sprint_speed ss ON ap.player_id = ss.player_id AND ap.season = ss.season
        """)
        log.info("  Baserunner profiles done.")

    def _build_baserunner_steal_metrics(self, seasons: list[int]) -> None:
        """
        SIM-408: build derived.baserunner_steal_metrics for the Step 2.5
        baserunner-steal similarity engine, which SELECTed this table but the
        computor never produced it.

        Per runner-season, from raw.pitches SB flags:
          * sample_steal_attempts  — SB attempts as the lead runner (2B/3B/home)
          * sample_first_base_opps — PAs begun on 1B (the steal-2B opportunity)
          * steal_attempt_rate     — attempts / first_base_opps
          * steal_attempt_rate_2b  — 2B→3B attempts / PAs begun on 2B
          * steal_success_rate / _2b — success fractions

        Biomech jump features (reaction_time / burst_distance / break_angle) are
        NOT computed — Statcast doesn't publish them and the engine's JUMP
        sub-score was removed (see baserunner_steal_similarity.py). Idempotent
        via INSERT OR REPLACE on (player_id, season).
        """
        log.info("Building derived.baserunner_steal_metrics …")
        season_list = ", ".join(str(s) for s in seasons)
        # Matches the engine's MIN_STEAL_ATTEMPTS gate (rows flagged below this
        # are filtered out by the engine's `WHERE NOT below_minimum_sample`).
        min_attempts = 10

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.baserunner_steal_metrics (
                player_id, season, sample_steal_attempts, sample_first_base_opps,
                steal_attempt_rate, steal_attempt_rate_2b,
                steal_success_rate, steal_success_rate_2b, below_minimum_sample
            )
            WITH clean AS (
                SELECT
                    game_pk, at_bat_number, pitch_number, season,
                    on_1b, on_2b, on_3b,
                    sb_attempt_2b, sb_attempt_3b, sb_attempt_home,
                    sb_success_2b, sb_success_3b, sb_success_home
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
            ),
            -- One SB-attempt event per qualifying pitch, attributed to the lead runner
            attempts AS (
                SELECT
                    CASE
                        WHEN sb_attempt_2b   THEN on_1b
                        WHEN sb_attempt_3b   THEN on_2b
                        WHEN sb_attempt_home THEN on_3b
                    END                                       AS runner_id,
                    season,
                    sb_attempt_3b                             AS is_2b_steal,  -- runner from 2B → 3B
                    CASE
                        WHEN sb_attempt_2b   THEN sb_success_2b
                        WHEN sb_attempt_3b   THEN sb_success_3b
                        WHEN sb_attempt_home THEN sb_success_home
                    END                                       AS success
                FROM clean
                WHERE sb_attempt_2b OR sb_attempt_3b OR sb_attempt_home
            ),
            attempt_agg AS (
                SELECT
                    runner_id AS player_id,
                    season,
                    COUNT(*)                                        AS n_attempts,
                    SUM(CASE WHEN success THEN 1 ELSE 0 END)        AS n_success,
                    SUM(CASE WHEN is_2b_steal THEN 1 ELSE 0 END)    AS n_attempts_2b,
                    SUM(CASE WHEN is_2b_steal AND success THEN 1 ELSE 0 END) AS n_success_2b
                FROM attempts
                WHERE runner_id IS NOT NULL
                GROUP BY runner_id, season
            ),
            -- Opportunities = distinct PAs begun on the base (first pitch of each PA)
            pa_state AS (
                SELECT game_pk, at_bat_number, season, on_1b, on_2b
                FROM clean
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                ) = 1
            ),
            opp_1b AS (
                SELECT on_1b AS player_id, season, COUNT(*) AS opps_1b
                FROM pa_state WHERE on_1b IS NOT NULL GROUP BY on_1b, season
            ),
            opp_2b AS (
                SELECT on_2b AS player_id, season, COUNT(*) AS opps_2b
                FROM pa_state WHERE on_2b IS NOT NULL GROUP BY on_2b, season
            )
            SELECT
                a.player_id,
                a.season,
                a.n_attempts                                          AS sample_steal_attempts,
                COALESCE(o1.opps_1b, 0)                               AS sample_first_base_opps,
                a.n_attempts    * 1.0 / NULLIF(o1.opps_1b, 0)         AS steal_attempt_rate,
                a.n_attempts_2b * 1.0 / NULLIF(o2.opps_2b, 0)         AS steal_attempt_rate_2b,
                a.n_success     * 1.0 / NULLIF(a.n_attempts, 0)       AS steal_success_rate,
                a.n_success_2b  * 1.0 / NULLIF(a.n_attempts_2b, 0)    AS steal_success_rate_2b,
                (a.n_attempts < {min_attempts})                       AS below_minimum_sample
            FROM attempt_agg a
            LEFT JOIN opp_1b o1 ON o1.player_id = a.player_id AND o1.season = a.season
            LEFT JOIN opp_2b o2 ON o2.player_id = a.player_id AND o2.season = a.season
        """)
        log.info("  derived.baserunner_steal_metrics done.")

    def _build_pitcher_steal_metrics(self, seasons: list[int]) -> None:
        """
        SIM-408: build derived.pitcher_steal_metrics for the Step 2.7
        pitcher-steal (hold-runner) engine, which SELECTed this table but the
        computor never produced it.

        OUTCOME-only, per pitcher-season, from raw.pitches:
          * sample_baserunner_events       — PAs pitched with a runner on base
          * sample_steal_attempts_against  — SB attempts while this pitcher threw
          * sb_against_per_9               — SB allowed × 27 / outs recorded
          * cs_rate_forced                 — (attempts − successes) / attempts
          * steal_attempt_rate_allowed     — attempts / baserunner_events

        Delivery (biomech timings) + Pickoff (no pickoff/disengagement columns
        exist in raw.pitches) are NOT computed — the engine's Delivery and
        Pickoff sub-scores were removed (see pitcher_steal_similarity.py).
        Idempotent via INSERT OR REPLACE on (pitcher_id, season).
        """
        log.info("Building derived.pitcher_steal_metrics …")
        season_list = ", ".join(str(s) for s in seasons)
        min_events = 30  # matches the engine's MIN_BASERUNNER_EVENTS gate

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.pitcher_steal_metrics (
                pitcher_id, season, throws,
                sample_baserunner_events, sample_steal_attempts_against,
                sb_against_per_9, cs_rate_forced, steal_attempt_rate_allowed,
                below_minimum_sample
            )
            WITH clean AS (
                SELECT
                    pitcher, season, p_throws,
                    game_pk, at_bat_number, pitch_number,
                    on_1b, on_2b, on_3b, outs_on_pitch,
                    sb_attempt_2b, sb_attempt_3b, sb_attempt_home,
                    sb_success_2b, sb_success_3b, sb_success_home
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
            ),
            -- Outs recorded (for IP) + SB attempts/successes against, per pitcher
            pitch_agg AS (
                SELECT
                    pitcher AS pitcher_id,
                    season,
                    MIN(p_throws)                                            AS throws,
                    SUM(outs_on_pitch)                                       AS outs_recorded,
                    SUM(CASE WHEN sb_attempt_2b OR sb_attempt_3b OR sb_attempt_home
                             THEN 1 ELSE 0 END)                              AS sb_attempts_against,
                    SUM(CASE WHEN sb_success_2b OR sb_success_3b OR sb_success_home
                             THEN 1 ELSE 0 END)                              AS sb_allowed
                FROM clean
                GROUP BY pitcher, season
            ),
            -- Baserunner events = distinct PAs pitched with a runner on base
            br_events AS (
                SELECT pitcher AS pitcher_id, season, COUNT(*) AS n_br_events
                FROM (
                    SELECT
                        pitcher, season,
                        (on_1b IS NOT NULL OR on_2b IS NOT NULL OR on_3b IS NOT NULL) AS has_runner
                    FROM clean
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                    ) = 1
                ) pa
                WHERE has_runner
                GROUP BY pitcher, season
            )
            SELECT
                p.pitcher_id,
                p.season,
                p.throws,
                COALESCE(b.n_br_events, 0)                                   AS sample_baserunner_events,
                p.sb_attempts_against                                        AS sample_steal_attempts_against,
                p.sb_allowed * 27.0 / NULLIF(p.outs_recorded, 0)             AS sb_against_per_9,
                (p.sb_attempts_against - p.sb_allowed) * 1.0
                    / NULLIF(p.sb_attempts_against, 0)                       AS cs_rate_forced,
                p.sb_attempts_against * 1.0 / NULLIF(b.n_br_events, 0)       AS steal_attempt_rate_allowed,
                (COALESCE(b.n_br_events, 0) < {min_events})                  AS below_minimum_sample
            FROM pitch_agg p
            LEFT JOIN br_events b ON b.pitcher_id = p.pitcher_id AND b.season = p.season
        """)
        log.info("  derived.pitcher_steal_metrics done.")

    def _compute_bullpen_workload(self, seasons: list[int]) -> pd.DataFrame:
        """
        SIM-433: derive the per-game rest / recent-workload signal that the
        availability table (``raw.game_bullpen_availability``) needs, straight
        from ``raw.pitches`` (no network).

        ``raw.game_lineups`` records who *played*; the manager-profile USAGE
        sub-score (SIM-427/434) also needs the available-but-unused signal, which
        starts with workload: an arm that threw 45 pitches yesterday is
        rest-unavailable today regardless of whether the manager "chose" not to
        use it. This method builds, per (game_pk, team_id, pitcher_id) where the
        pitcher actually appeared:

          * ``last_game_date``   — this pitcher's appearance date (the game's date)
          * ``days_rest``        — days since the pitcher's PREVIOUS appearance
                                   (NULL for the first appearance in the window)
          * ``pitches_last_3d``  — pitches thrown in the 3 calendar days BEFORE
                                   this game's date (excludes this game)
          * ``back_to_back``     — pitched on the immediately preceding calendar
                                   day

        ``team_id`` is the FIELDING team for the pitcher in each appearance: the
        home team when the half-inning is the top (``inning_topbot='Top'``), else
        the away team. (A pitcher only ever fields for one team in a game, so the
        per-(game, pitcher) MIN over the half-inning derivation is unambiguous.)

        This is the raw.pitches-derived HALF of SIM-433. It does NOT persist —
        the computor attaches PostgreSQL READ_ONLY, so it returns the workload
        rows as a DataFrame for
        :class:`pipeline.live.bullpen_availability_ingest.BullpenAvailabilityIngest`
        to merge with the MLB-API active-roster / IL fetch and persist to
        ``raw.game_bullpen_availability``. Returns an EMPTY DataFrame (with the
        documented columns) when there are no qualifying rows.
        """
        log.info("Deriving bullpen workload (SIM-433) …")
        season_list = ", ".join(str(s) for s in seasons)

        # One row per (game_pk, pitcher) appearance with that game's date, the
        # pitcher's fielding team, and pitches thrown; then a self-referential
        # window over the pitcher's appearance timeline derives rest/workload.
        df = self._conn.execute(f"""
            WITH appearances AS (
                SELECT
                    game_pk,
                    pitcher                                                  AS pitcher_id,
                    season,
                    MIN(game_date)                                           AS game_date,
                    -- Fielding team: home fields the top half, away the bottom.
                    MIN(CASE WHEN inning_topbot = 'Top' THEN home_id ELSE away_id END)
                                                                             AS team_id,
                    COUNT(*)                                                 AS pitches_in_game
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                GROUP BY game_pk, pitcher, season
            ),
            with_prev AS (
                SELECT
                    a.*,
                    LAG(a.game_date) OVER (
                        PARTITION BY a.pitcher_id ORDER BY a.game_date, a.game_pk
                    )                                                        AS prev_game_date
                FROM appearances a
            )
            SELECT
                w.game_pk,
                w.team_id,
                w.pitcher_id,
                w.season,
                w.game_date,
                w.pitches_in_game,
                CASE WHEN w.prev_game_date IS NOT NULL
                     THEN DATE_DIFF('day', w.prev_game_date, w.game_date)
                END                                                          AS days_rest,
                CASE WHEN w.prev_game_date IS NOT NULL
                       AND DATE_DIFF('day', w.prev_game_date, w.game_date) = 1
                     THEN TRUE ELSE FALSE END                                AS back_to_back,
                -- Pitches thrown by this arm in the 3 calendar days BEFORE this
                -- game (a correlated sum over the same appearances set).
                COALESCE((
                    SELECT SUM(p.pitches_in_game)
                    FROM appearances p
                    WHERE p.pitcher_id = w.pitcher_id
                      AND p.game_date < w.game_date
                      AND p.game_date >= w.game_date - INTERVAL 3 DAY
                ), 0)                                                        AS pitches_last_3d
            FROM with_prev w
            ORDER BY w.pitcher_id, w.game_date, w.game_pk
        """).fetchdf()

        if df.empty:
            log.info("  bullpen workload: no qualifying appearances.")
            return df
        log.info(
            "  bullpen workload: %d appearance rows (%d pitchers).",
            len(df),
            df["pitcher_id"].nunique(),
        )
        return df

    def _compute_manager_profiles(self, seasons: list[int]) -> None:
        """
        SIM-408: compute manager tendency profiles in the manager engine's
        vocabulary (usage / aggression / platoon).

        Offensive decisions (pinch-hit, steal order, sac bunt, squeeze, platoon
        exploitation) are attributed to the BATTING manager; defensive ones
        (defensive sub) to the FIELDING manager.  The USAGE sub-score
        (starter-pull timing, closer/reliever roles) is GATED on the SIM-427
        bullpen-roster build and emitted NULL until that lands; two further
        tendencies are not flagged in Statcast (hit-and-run, double-switch) and
        are likewise NULL.  Idempotent via INSERT OR REPLACE on
        (manager_id, season).
        """
        log.info("Computing manager profiles …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.manager_season_metrics (
                manager_id, season, sample_games, sample_starter_decisions,
                starter_avg_pitch_count, starter_pull_pct_before_100,
                closer_entry_leverage_index, high_leverage_reliever_rate,
                opener_usage_rate, bulk_innings_rate,
                steal_order_rate_per_1b_opp, hit_and_run_rate_per_opportunity,
                sac_bunt_rate_high_leverage, sac_bunt_rate_low_leverage,
                squeeze_play_rate_per_3b_opp,
                pinch_hit_rate_vs_same_hand, pinch_hit_rate_high_leverage,
                defensive_sub_rate_late_innings, double_switch_rate_per_reliever_change,
                platoon_advantage_exploitation_rate, below_minimum_sample
            )
            WITH p AS (
                SELECT
                    game_pk, at_bat_number, pitch_number, season,
                    inning, inning_topbot, on_1b, on_2b, on_3b,
                    stand, p_throws, pinch_hitter, defensive_sub,
                    sb_attempt_2b, sb_attempt_3b, sb_attempt_home,
                    -- batting manager makes offensive calls; fielding manager defensive
                    CASE WHEN inning_topbot = 'Top' THEN away_manager_id
                         ELSE home_manager_id END AS bat_mgr,
                    CASE WHEN inning_topbot = 'Top' THEN home_manager_id
                         ELSE away_manager_id END AS fld_mgr,
                    -- simplified leverage bucket (matches the situation-engine LI drivers)
                    CASE
                        WHEN ABS(bat_score - fld_score) <= 1 AND inning >= 7 THEN 'high'
                        WHEN ABS(bat_score - fld_score) >= 4 OR inning <= 5  THEN 'low'
                        ELSE 'mid'
                    END AS li_bucket,
                    -- the PA's terminal event is on the last pitch; broadcast it
                    MAX(events) OVER (PARTITION BY game_pk, at_bat_number) AS pa_event
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                  AND home_manager_id IS NOT NULL
            ),
            -- one row per PA (first pitch) for per-PA offensive rates
            pa AS (
                SELECT * FROM p
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                ) = 1
            ),
            -- offensive (batting-manager) aggregates, per PA
            bat_agg AS (
                SELECT
                    bat_mgr AS manager_id, season,
                    COUNT(*)                                                       AS n_pa,
                    SUM(CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)             AS opp_1b,
                    SUM(CASE WHEN on_3b IS NOT NULL THEN 1 ELSE 0 END)             AS opp_3b,
                    SUM(CASE WHEN li_bucket = 'high' THEN 1 ELSE 0 END)            AS n_pa_high,
                    SUM(CASE WHEN li_bucket = 'low'  THEN 1 ELSE 0 END)            AS n_pa_low,
                    SUM(CASE WHEN pinch_hitter THEN 1 ELSE 0 END)                  AS n_ph,
                    SUM(CASE WHEN pinch_hitter AND stand = p_throws THEN 1 ELSE 0 END) AS n_ph_same_hand,
                    SUM(CASE WHEN pinch_hitter AND li_bucket = 'high' THEN 1 ELSE 0 END) AS n_ph_high,
                    SUM(CASE WHEN pa_event = 'sac_bunt' AND li_bucket = 'high' THEN 1 ELSE 0 END) AS n_bunt_high,
                    SUM(CASE WHEN pa_event = 'sac_bunt' AND li_bucket = 'low'  THEN 1 ELSE 0 END) AS n_bunt_low,
                    SUM(CASE WHEN pa_event = 'sac_bunt' AND on_3b IS NOT NULL THEN 1 ELSE 0 END) AS n_squeeze,
                    SUM(CASE WHEN stand <> p_throws THEN 1 ELSE 0 END)             AS n_platoon_adv
                FROM pa
                WHERE bat_mgr IS NOT NULL
                GROUP BY bat_mgr, season
            ),
            -- steal attempts (per-pitch events, batting side)
            steal_agg AS (
                SELECT bat_mgr AS manager_id, season,
                    SUM(CASE WHEN sb_attempt_2b OR sb_attempt_3b OR sb_attempt_home
                             THEN 1 ELSE 0 END) AS n_steal_orders
                FROM p
                WHERE bat_mgr IS NOT NULL
                GROUP BY bat_mgr, season
            ),
            -- defensive (fielding-manager) aggregates
            fld_agg AS (
                SELECT fld_mgr AS manager_id, season,
                    COUNT(DISTINCT game_pk)                                        AS n_fld_games,
                    SUM(CASE WHEN defensive_sub AND inning >= 7 THEN 1 ELSE 0 END) AS n_def_sub_late
                FROM p
                WHERE fld_mgr IS NOT NULL
                GROUP BY fld_mgr, season
            ),
            -- games per manager (either dugout)
            games AS (
                SELECT manager_id, season, COUNT(DISTINCT game_pk) AS sample_games
                FROM (
                    SELECT bat_mgr AS manager_id, season, game_pk FROM p WHERE bat_mgr IS NOT NULL
                    UNION
                    SELECT fld_mgr AS manager_id, season, game_pk FROM p WHERE fld_mgr IS NOT NULL
                )
                GROUP BY manager_id, season
            )
            SELECT
                g.manager_id,
                g.season,
                g.sample_games,
                g.sample_games                                                AS sample_starter_decisions,  -- proxy (~1 SP pull/game); SIM-427 refines
                -- Usage (6) — GATED on the SIM-427 bullpen-roster build
                NULL::FLOAT AS starter_avg_pitch_count,
                NULL::FLOAT AS starter_pull_pct_before_100,
                NULL::FLOAT AS closer_entry_leverage_index,
                NULL::FLOAT AS high_leverage_reliever_rate,
                NULL::FLOAT AS opener_usage_rate,
                NULL::FLOAT AS bulk_innings_rate,
                -- Aggression (5)
                s.n_steal_orders * 1.0 / NULLIF(b.opp_1b, 0)                  AS steal_order_rate_per_1b_opp,
                NULL::FLOAT                                                   AS hit_and_run_rate_per_opportunity,  -- not flagged in Statcast
                b.n_bunt_high * 1.0 / NULLIF(b.n_pa_high, 0)                  AS sac_bunt_rate_high_leverage,
                b.n_bunt_low  * 1.0 / NULLIF(b.n_pa_low, 0)                   AS sac_bunt_rate_low_leverage,
                b.n_squeeze   * 1.0 / NULLIF(b.opp_3b, 0)                     AS squeeze_play_rate_per_3b_opp,
                -- Platoon (5)
                b.n_ph_same_hand * 1.0 / NULLIF(b.n_ph, 0)                    AS pinch_hit_rate_vs_same_hand,
                b.n_ph_high * 1.0 / NULLIF(b.n_pa_high, 0)                    AS pinch_hit_rate_high_leverage,
                f.n_def_sub_late * 1.0 / NULLIF(f.n_fld_games, 0)            AS defensive_sub_rate_late_innings,
                NULL::FLOAT                                                   AS double_switch_rate_per_reliever_change,  -- not detectable
                b.n_platoon_adv * 1.0 / NULLIF(b.n_pa, 0)                     AS platoon_advantage_exploitation_rate,
                (g.sample_games < {MIN_MANAGER_GAMES})                        AS below_minimum_sample
            FROM games g
            LEFT JOIN bat_agg b   ON b.manager_id = g.manager_id AND b.season = g.season
            LEFT JOIN steal_agg s ON s.manager_id = g.manager_id AND s.season = g.season
            LEFT JOIN fld_agg f   ON f.manager_id = g.manager_id AND f.season = g.season
        """)
        log.info("  Manager profiles done.")

    # ==================================================================
    # Defensive Metrics Methods
    # (Catcher framing/blocking/throwing, OF/IF OAA, DP, errors, aggregation)
    # ==================================================================

    def _compute_catcher_framing(self, seasons: list[int]) -> None:
        """
        Build a called-strike probability model and compute framing runs.

        Model inputs: pitch location relative to zone edges, count,
        pitcher/batter handedness.

        The 'shadow zone' (~3-6 inches outside the zone boundary) is
        where framing skill matters. Pitches well inside the zone are
        always strikes; pitches well outside are never strikes.
        """
        log.info("Computing catcher framing …")
        season_list = ", ".join(str(s) for s in seasons)

        # Extract all taken pitches (not swung at)
        df = self._conn.execute(f"""
            SELECT
                game_pk,
                at_bat_number,
                pitch_number,
                season,
                fielder_2          AS catcher_id,
                plate_x,
                plate_z,
                sz_top,
                sz_bot,
                p_throws,
                stand,
                balls,
                strikes,
                -- Called strike = 1, called ball = 0
                CASE WHEN type = 'C' THEN 1 ELSE 0 END AS is_strike,
                CASE WHEN type IN ('B', 'C') THEN 1 ELSE 0 END AS is_taken
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND type IN ('B', 'C')
              AND plate_x IS NOT NULL
              AND plate_z IS NOT NULL
              AND sz_top IS NOT NULL
              AND sz_bot IS NOT NULL
        """).fetchdf()

        if len(df) < 1000:
            log.warning("  Too few taken pitches (%d) — skipping framing", len(df))
            return

        log.info("  Loaded %d taken pitches for framing model", len(df))

        # Compute distance from zone edges (the key framing features)
        df["zone_dist_x"] = (df["plate_x"].abs() - ZONE_HALF_WIDTH).clip(lower=0)
        df["zone_dist_z_top"] = (df["plate_z"] - df["sz_top"]).clip(lower=0)
        df["zone_dist_z_bot"] = (df["sz_bot"] - df["plate_z"]).clip(lower=0)
        df["zone_dist_z"] = df[["zone_dist_z_top", "zone_dist_z_bot"]].max(axis=1)
        df["zone_distance"] = np.sqrt(df["zone_dist_x"] ** 2 + df["zone_dist_z"] ** 2)

        # Additional features
        df["in_zone"] = (
            (df["plate_x"].abs() <= ZONE_HALF_WIDTH)
            & (df["plate_z"] >= df["sz_bot"])
            & (df["plate_z"] <= df["sz_top"])
        ).astype(int)
        df["count_state"] = df["balls"] * 3 + df["strikes"]  # 0-11 encoding
        df["p_throws_enc"] = (df["p_throws"] == "R").astype(int)
        df["stand_enc"] = (df["stand"] == "R").astype(int)
        df["pitch_side"] = df["plate_x"] * np.where(df["stand"] == "R", 1, -1)

        # Zone location categories for breakdown
        df["zone_low"] = (df["plate_z"] < df["sz_bot"] + 0.25).astype(int)
        df["zone_high"] = (df["plate_z"] > df["sz_top"] - 0.25).astype(int)
        df["zone_inside"] = (df["pitch_side"] > 0.4).astype(int)
        df["zone_outside"] = (df["pitch_side"] < -0.4).astype(int)

        # SIM-408: heart (clearly in-zone) vs shadow (just off the edge, where
        # framing skill lives) buckets for the catcher engine's zone-framing
        # features (shadow_zone_strike_rate / heart_zone_strike_rate).
        df["zone_heart"] = df["in_zone"]
        df["zone_shadow"] = ((df["in_zone"] == 0) & (df["zone_distance"] <= 0.6)).astype(int)

        # Fit the logistic model: P(called_strike) = f(location, count, handedness)
        feature_cols = [
            "plate_x",
            "plate_z",
            "zone_distance",
            "in_zone",
            "count_state",
            "p_throws_enc",
            "stand_enc",
        ]
        X = df[feature_cols].values
        y = df["is_strike"].values

        model = _fit_logistic_model(X, y)
        if model is None:
            log.error("  Framing model fit failed — skipping")
            return

        # Predict expected strike probability for each pitch
        df["expected_strike"] = _predict_proba(model, X)

        # Per-catcher aggregation
        framing_agg = (
            df.groupby(["catcher_id", "season"])
            .agg(
                called_pitches=("is_strike", "count"),
                called_strikes=("is_strike", "sum"),
                expected_called_strikes=("expected_strike", "sum"),
                # Zone breakdowns
                strikes_low=("is_strike", lambda x: x[df.loc[x.index, "zone_low"] == 1].sum()),
                expected_low=(
                    "expected_strike",
                    lambda x: x[df.loc[x.index, "zone_low"] == 1].sum(),
                ),
                strikes_high=("is_strike", lambda x: x[df.loc[x.index, "zone_high"] == 1].sum()),
                expected_high=(
                    "expected_strike",
                    lambda x: x[df.loc[x.index, "zone_high"] == 1].sum(),
                ),
                strikes_inside=(
                    "is_strike",
                    lambda x: x[df.loc[x.index, "zone_inside"] == 1].sum(),
                ),
                expected_inside=(
                    "expected_strike",
                    lambda x: x[df.loc[x.index, "zone_inside"] == 1].sum(),
                ),
                strikes_outside=(
                    "is_strike",
                    lambda x: x[df.loc[x.index, "zone_outside"] == 1].sum(),
                ),
                expected_outside=(
                    "expected_strike",
                    lambda x: x[df.loc[x.index, "zone_outside"] == 1].sum(),
                ),
                # SIM-408: heart/shadow called-strike rates (counts here; rate below)
                strikes_heart=("is_strike", lambda x: x[df.loc[x.index, "zone_heart"] == 1].sum()),
                pitches_heart=(
                    "is_strike",
                    lambda x: int((df.loc[x.index, "zone_heart"] == 1).sum()),
                ),
                strikes_shadow=(
                    "is_strike",
                    lambda x: x[df.loc[x.index, "zone_shadow"] == 1].sum(),
                ),
                pitches_shadow=(
                    "is_strike",
                    lambda x: int((df.loc[x.index, "zone_shadow"] == 1).sum()),
                ),
            )
            .reset_index()
        )

        framing_agg["strikes_above_average"] = (
            framing_agg["called_strikes"] - framing_agg["expected_called_strikes"]
        )
        framing_agg["framing_runs"] = (
            framing_agg["strikes_above_average"] * RUNS_PER_STRIKE_ABOVE_AVG
        )
        framing_agg["framing_low"] = framing_agg["strikes_low"] - framing_agg["expected_low"]
        framing_agg["framing_high"] = framing_agg["strikes_high"] - framing_agg["expected_high"]
        framing_agg["framing_inside"] = (
            framing_agg["strikes_inside"] - framing_agg["expected_inside"]
        )
        framing_agg["framing_outside"] = (
            framing_agg["strikes_outside"] - framing_agg["expected_outside"]
        )
        # SIM-408: zone-framing strike rates (NaN when the bucket is empty;
        # the engine COALESCEs NULL → 0 / falls back to league average).
        framing_agg["heart_zone_strike_rate"] = framing_agg["strikes_heart"] / framing_agg[
            "pitches_heart"
        ].replace(0, np.nan)
        framing_agg["shadow_zone_strike_rate"] = framing_agg["strikes_shadow"] / framing_agg[
            "pitches_shadow"
        ].replace(0, np.nan)

        # Store framing results in a temp table for later aggregation
        self._conn.execute("DROP TABLE IF EXISTS _tmp_framing")
        self._conn.execute(
            "CREATE TABLE _tmp_framing AS SELECT * FROM framing_agg",
        )
        self._conn.register("framing_agg", framing_agg)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_framing;
            CREATE TABLE _tmp_framing AS SELECT * FROM framing_agg;
        """)
        self._conn.unregister("framing_agg")

        log.info("  Framing computed for %d catcher-seasons", len(framing_agg))

    # ------------------------------------------------------------------
    # CATCHER BLOCKING
    # ------------------------------------------------------------------

    def _compute_catcher_blocking(self, seasons: list[int]) -> None:
        """
        Build a PB/WP probability model and compute blocks above average.

        Core model inputs (from Tango's post):
          - Pitch height at catcher position (primary sigmoid axis)
          - Whether the pitch bounced before reaching catcher
          - Horizontal distance from catcher's center
          - Pitch speed and movement
          - Pitcher/batter handedness
        """
        log.info("Computing catcher blocking …")
        season_list = ", ".join(str(s) for s in seasons)

        df = self._conn.execute(f"""
            SELECT
                game_pk,
                at_bat_number,
                pitch_number,
                season,
                fielder_2           AS catcher_id,
                plate_x,
                plate_z,
                release_speed,
                end_speed,
                vx0, vy0, vz0,
                ax, ay, az,
                pfx_x,
                break_vertical,
                break_horizontal,
                p_throws,
                stand,
                passed_ball_wild_pitch,
                on_1b, on_2b, on_3b
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND plate_x IS NOT NULL
              AND plate_z IS NOT NULL
              AND end_speed IS NOT NULL
        """).fetchdf()

        if len(df) < 1000:
            log.warning("  Too few pitches for blocking model (%d)", len(df))
            return

        log.info("  Loaded %d pitches for blocking model", len(df))

        # Estimate height at catcher position (~3.9 ft behind front of plate)
        # Using the simple drop approximation from end_speed
        extra_time = 3.9 / (df["end_speed"].clip(lower=50) * 1.467)
        gravity_drop = 0.5 * GRAVITY_FPS2 * extra_time**2
        # Additional drop from downward pitch velocity (vz0 at 50ft plane is often negative)
        pitch_drop = df["vz0"].clip(upper=0).abs() * extra_time * 0.3  # partial effect
        df["height_at_catcher"] = df["plate_z"] - gravity_drop - pitch_drop

        # Classify: bounced vs reached
        df["bounced"] = (df["height_at_catcher"] <= 0.3).astype(int)

        # Catcher's effective "radius" — distance from zone center
        df["dist_from_center"] = np.sqrt(df["plate_x"] ** 2 + (df["plate_z"] - 2.0) ** 2)

        # Lateral distance from center
        df["lateral_dist"] = df["plate_x"].abs()

        # Target variable
        df["pbwp"] = df["passed_ball_wild_pitch"].astype(int)

        # Features for the blocking model
        feature_cols = [
            "height_at_catcher",
            "bounced",
            "dist_from_center",
            "lateral_dist",
            "end_speed",
            "break_horizontal",
            "break_vertical",
        ]
        df_model = df.dropna(subset=feature_cols + ["pbwp"])
        X = df_model[feature_cols].fillna(0).values
        y = df_model["pbwp"].values

        model = _fit_logistic_model(X, y)
        if model is None:
            log.error("  Blocking model fit failed — skipping")
            return

        df_model["expected_pbwp"] = _predict_proba(model, X)

        # Per-catcher aggregation
        blocking_agg = (
            df_model.groupby(["catcher_id", "season"])
            .agg(
                pitches_received=("pbwp", "count"),
                expected_pbwp=("expected_pbwp", "sum"),
                actual_pbwp=("pbwp", "sum"),
            )
            .reset_index()
        )

        blocking_agg["blocks_above_average"] = (
            blocking_agg["expected_pbwp"] - blocking_agg["actual_pbwp"]
        )
        blocking_agg["blocking_runs"] = blocking_agg["blocks_above_average"] * RUNS_PER_BLOCK_SAVED

        # Category breakdowns
        for cat_name, _cat_mask_col in [
            ("bounced", "bounced"),
            ("high", "height_at_catcher"),
            ("lateral", "lateral_dist"),
        ]:
            if cat_name == "bounced":
                mask = df_model["bounced"] == 1
            elif cat_name == "high":
                mask = df_model["height_at_catcher"] > 4.0
            else:
                mask = df_model["lateral_dist"] > 1.5

            cat_agg = (
                df_model[mask]
                .groupby(["catcher_id", "season"])
                .agg(
                    cat_expected=("expected_pbwp", "sum"),
                    cat_actual=("pbwp", "sum"),
                )
                .reset_index()
            )
            cat_agg[f"blocks_aa_{cat_name}"] = cat_agg["cat_expected"] - cat_agg["cat_actual"]
            blocking_agg = blocking_agg.merge(
                cat_agg[["catcher_id", "season", f"blocks_aa_{cat_name}"]],
                on=["catcher_id", "season"],
                how="left",
            )

        self._conn.register("blocking_agg", blocking_agg)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_blocking;
            CREATE TABLE _tmp_blocking AS SELECT * FROM blocking_agg;
        """)
        self._conn.unregister("blocking_agg")

        log.info("  Blocking computed for %d catcher-seasons", len(blocking_agg))

    # ------------------------------------------------------------------
    # CATCHER THROWING (BY BASE)
    # ------------------------------------------------------------------

    def _compute_catcher_throwing(self, seasons: list[int]) -> None:
        """Compute SB prevention metrics broken down by base attempted.

        Includes the SIM-073 PA-level deterrence rate
        (steal_attempt_rate_against) used by the catcher similarity engine v2
        as a Deterrence sub-score (SIM-072).
        """
        log.info("Computing catcher throwing metrics …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_catcher_throwing;
            CREATE TABLE _tmp_catcher_throwing AS

            WITH sb_events AS (
                SELECT
                    fielder_2 AS catcher_id,
                    season,
                    sb_attempt_2b, sb_success_2b,
                    sb_attempt_3b, sb_success_3b,
                    sb_attempt_home, sb_success_home
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  AND (sb_attempt_2b = TRUE OR sb_attempt_3b = TRUE OR sb_attempt_home = TRUE)
            ),
            sb_aggregated AS (
                SELECT
                    catcher_id,
                    season,
                    COUNT(*) AS sb_attempts_faced,

                    -- Overall CS
                    SUM(CASE WHEN (sb_attempt_2b AND NOT sb_success_2b)
                               OR (sb_attempt_3b AND NOT sb_success_3b)
                               OR (sb_attempt_home AND NOT sb_success_home)
                             THEN 1 ELSE 0 END) AS cs_total,

                    -- 2B attempts
                    SUM(CASE WHEN sb_attempt_2b THEN 1 ELSE 0 END) AS sb_attempts_2b,
                    SUM(CASE WHEN sb_attempt_2b AND NOT sb_success_2b THEN 1 ELSE 0 END)
                        * 1.0 / NULLIF(SUM(CASE WHEN sb_attempt_2b THEN 1 ELSE 0 END), 0)
                        AS cs_rate_2b,

                    -- 3B attempts
                    SUM(CASE WHEN sb_attempt_3b THEN 1 ELSE 0 END) AS sb_attempts_3b,
                    SUM(CASE WHEN sb_attempt_3b AND NOT sb_success_3b THEN 1 ELSE 0 END)
                        * 1.0 / NULLIF(SUM(CASE WHEN sb_attempt_3b THEN 1 ELSE 0 END), 0)
                        AS cs_rate_3b,

                    -- Overall rates
                    SUM(CASE WHEN (sb_attempt_2b AND NOT sb_success_2b)
                               OR (sb_attempt_3b AND NOT sb_success_3b)
                               OR (sb_attempt_home AND NOT sb_success_home)
                             THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) AS cs_rate,

                    1.0 - (SUM(CASE WHEN (sb_attempt_2b AND NOT sb_success_2b)
                                      OR (sb_attempt_3b AND NOT sb_success_3b)
                                      OR (sb_attempt_home AND NOT sb_success_home)
                                    THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0))
                        AS sb_allowed_rate,

                    -- SIM-073 numerator: total steal *attempts* against this catcher
                    -- across 2B + 3B (home steal attempts excluded — they're a
                    -- different baseball action, not a catcher-arm decision).
                    SUM(CASE WHEN sb_attempt_2b OR sb_attempt_3b THEN 1 ELSE 0 END)
                        AS steal_attempts_2b_or_3b
                FROM sb_events
                GROUP BY catcher_id, season
            ),
            -- SIM-073: PA-level opportunity counter.  One row per
            -- (catcher, game, at-bat) regardless of pitch count, so a
            -- 7-pitch PA contributes exactly 1 opportunity.
            catcher_pa_state AS (
                SELECT DISTINCT
                    fielder_2 AS catcher_id,
                    season,
                    game_pk,
                    at_bat_number,
                    on_1b,
                    on_2b,
                    on_3b
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  AND fielder_2 IS NOT NULL
            ),
            catcher_opportunities AS (
                SELECT
                    catcher_id,
                    season,
                    -- 1B opportunity: runner on 1B with 2B empty.  Excludes
                    -- forced-advance scenarios (2B occupied) where the runner
                    -- has no stealing lane.
                    SUM(CASE WHEN on_1b IS NOT NULL AND on_2b IS NULL
                             THEN 1 ELSE 0 END) AS opp_1b_pa,
                    -- 2B opportunity: runner on 2B with 3B empty.
                    SUM(CASE WHEN on_2b IS NOT NULL AND on_3b IS NULL
                             THEN 1 ELSE 0 END) AS opp_2b_pa
                FROM catcher_pa_state
                GROUP BY catcher_id, season
            )
            SELECT
                COALESCE(sa.catcher_id, co.catcher_id) AS catcher_id,
                COALESCE(sa.season, co.season)         AS season,
                COALESCE(sa.sb_attempts_faced, 0)      AS sb_attempts_faced,
                COALESCE(sa.cs_total, 0)               AS cs_total,
                sa.sb_attempts_2b,
                sa.cs_rate_2b,
                sa.sb_attempts_3b,
                sa.cs_rate_3b,
                sa.cs_rate,
                sa.sb_allowed_rate,

                -- SIM-073 deterrence rate
                COALESCE(co.opp_1b_pa, 0) + COALESCE(co.opp_2b_pa, 0)
                    AS sb_opportunities_pa,
                CASE WHEN COALESCE(co.opp_1b_pa, 0) + COALESCE(co.opp_2b_pa, 0) >= 100
                     THEN COALESCE(sa.steal_attempts_2b_or_3b, 0) * 1.0
                          / NULLIF(COALESCE(co.opp_1b_pa, 0) + COALESCE(co.opp_2b_pa, 0), 0)
                     ELSE NULL
                END AS steal_attempt_rate_against
            FROM sb_aggregated sa
            FULL OUTER JOIN catcher_opportunities co
                ON sa.catcher_id = co.catcher_id
               AND sa.season     = co.season
        """)

        log.info("  Catcher throwing metrics computed.")

    # ------------------------------------------------------------------
    # OUTFIELD CATCH PROBABILITY + OAA
    # ------------------------------------------------------------------

    def _compute_outfield_catch_probability(self, seasons: list[int]) -> None:
        """
        Build the outfield catch probability model and compute per-play OAA.

        Follows the Statcast methodology:
          1. Estimate hang time from launch conditions
          2. Estimate distance needed from fielder start position to ball landing
          3. Compute speed needed = distance / (opportunity_time - buffer)
          4. Adjust for direction (going back penalty: 1 ft/s per Tango)
          5. Fit sigmoid: P(catch) = f(speed_needed)
          6. OAA = sum of (1-P) for catches + (-P) for misses
        """
        log.info("Computing outfield catch probability …")
        season_list = ", ".join(str(s) for s in seasons)

        df = self._conn.execute(f"""
            SELECT
                game_pk || '-' || at_bat_number || '-' || pitch_number AS pitch_id_str,
                game_pk,
                season,
                stand,
                fielder_7, fielder_8, fielder_9,
                fielded_by,
                hc_x, hc_y,
                launch_speed, launch_angle, spray_angle,
                hit_distance_sc,
                bb_type,
                events,
                outs_on_pitch,
                fielding_error
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND type = 'X'
              AND hc_x IS NOT NULL AND hc_y IS NOT NULL
              AND launch_speed IS NOT NULL AND launch_angle IS NOT NULL
              -- Outfield plays: fly balls, line drives, and popups
              -- Exclude ground balls (handled by infield model)
              AND bb_type IN ('fly_ball', 'line_drive', 'popup')
              AND launch_angle > 10
              -- Ball reached outfield (hc_y < ~150 in hc coordinates)
              AND hc_y < 160
        """).fetchdf()

        if len(df) < 500:
            log.warning("  Too few outfield plays (%d) — skipping", len(df))
            return

        log.info("  Loaded %d outfield plays", len(df))

        # Determine which outfielder is responsible for each play
        rows = []
        for _, r in df.iterrows():
            # Assign to closest OF position based on ball landing location
            stand = r["stand"] if r["stand"] in ("L", "R") else "R"
            best_pos = None
            best_dist = float("inf")

            for pos in ("LF", "CF", "RF"):
                fx, fy = AVG_FIELDER_POS[pos].get(stand, AVG_FIELDER_POS[pos]["R"])
                d = _euclidean_hc(r["hc_x"], r["hc_y"], fx, fy)
                if d < best_dist:
                    best_dist = d
                    best_pos = pos

            # Map position to fielder ID
            pos_to_col = {"LF": "fielder_7", "CF": "fielder_8", "RF": "fielder_9"}
            fielder_id = r[pos_to_col[best_pos]]
            if pd.isna(fielder_id):
                continue

            fx, fy = AVG_FIELDER_POS[best_pos].get(stand, AVG_FIELDER_POS[best_pos]["R"])
            distance_hc = _euclidean_hc(r["hc_x"], r["hc_y"], fx, fy)
            distance_ft = _hc_to_feet(distance_hc)

            hang_time = _estimate_hang_time(r["launch_speed"], r["launch_angle"])
            if hang_time is None or hang_time < 1.0:
                continue

            # Opportunity time ≈ hang time + ~0.4s (pitch release to contact)
            opportunity_time = hang_time + 0.4

            # Speed needed to cover the distance in the available time
            if opportunity_time <= 0.5:
                continue
            speed_needed = distance_ft / opportunity_time

            # Direction adjustment
            direction_cat, going_back = _classify_direction_of(
                fx, fy, r["hc_x"], r["hc_y"], best_pos
            )
            if going_back:
                speed_needed += OF_GOING_BACK_PENALTY_FPS

            # Outcome
            caught = r["outs_on_pitch"] > 0 and pd.isna(r.get("fielding_error"))

            rows.append(
                {
                    "pitch_id_str": r["pitch_id_str"],
                    "game_pk": r["game_pk"],
                    "season": r["season"],
                    "fielder_id": int(fielder_id),
                    "position": best_pos,
                    "hang_time": hang_time,
                    "distance_needed": distance_ft,
                    "speed_needed": speed_needed,
                    "direction_back": going_back,
                    "direction_cat": direction_cat,
                    "caught": caught,
                }
            )

        plays = pd.DataFrame(rows)
        if len(plays) < 200:
            log.warning("  Too few valid OF plays (%d) after processing", len(plays))
            return

        # Fit the catch probability model: P(catch) = sigmoid(speed_needed)
        X = plays[["speed_needed"]].values
        y = plays["caught"].astype(int).values

        model = _fit_logistic_model(X, y)
        if model is None:
            log.error("  OF catch probability model fit failed")
            return

        plays["catch_probability"] = _predict_proba(model, X)

        # Clamp to [0.01, 0.99] to avoid extreme credits
        plays["catch_probability"] = plays["catch_probability"].clip(0.01, 0.99)

        # Star ratings
        plays["star_rating"] = pd.cut(
            plays["catch_probability"],
            bins=[0, 0.25, 0.50, 0.75, 0.90, 1.01],
            labels=[5, 4, 3, 2, 1],
        ).astype(int)

        # OAA credit per play
        plays["oaa_credit"] = np.where(
            plays["caught"],
            1.0 - plays["catch_probability"],
            -plays["catch_probability"],
        )

        # Write per-play detail table
        self._conn.register("of_plays", plays)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_of_plays;
            CREATE TABLE _tmp_of_plays AS SELECT * FROM of_plays;
        """)
        self._conn.unregister("of_plays")

        log.info(
            "  OF catch probability computed: %d plays, model AUC ~%.3f",
            len(plays),
            _quick_auc(y, plays["catch_probability"].values),
        )

    # ------------------------------------------------------------------
    # OUTFIELD ARM METRICS
    # ------------------------------------------------------------------

    def _compute_outfield_arm_metrics(self, seasons: list[int]) -> None:
        """
        Compute OF arm value: hold rate, thrown-out rate, arm runs.
        Uses RE24 deltas to value advancement prevention.
        """
        log.info("Computing outfield arm metrics …")
        season_list = ", ".join(str(s) for s in seasons)

        # Extract all BIP with runners on base where an OF fielded the ball
        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_of_arm;
            CREATE TABLE _tmp_of_arm AS

            WITH of_bip AS (
                SELECT
                    game_pk, at_bat_number, season,
                    fielded_by,
                    fielder_7, fielder_8, fielder_9,
                    on_1b, on_2b, on_3b,
                    post_on_1b, post_on_2b, post_on_3b,
                    runner_1b_scored, runner_2b_scored, runner_3b_scored,
                    outs_on_pitch, of_assist,
                    outs,
                    -- runners state bitmask
                    (CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)
                    + (CASE WHEN on_2b IS NOT NULL THEN 2 ELSE 0 END)
                    + (CASE WHEN on_3b IS NOT NULL THEN 4 ELSE 0 END) AS runners_state
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  AND type = 'X'
                  AND events IS NOT NULL
                  AND (on_1b IS NOT NULL OR on_2b IS NOT NULL OR on_3b IS NOT NULL)
                  AND (fielded_by = fielder_7 OR fielded_by = fielder_8 OR fielded_by = fielder_9)
            )
            SELECT
                *,
                CASE
                    WHEN fielded_by = fielder_7 THEN 'LF'
                    WHEN fielded_by = fielder_8 THEN 'CF'
                    WHEN fielded_by = fielder_9 THEN 'RF'
                END AS fielder_position,
                fielded_by AS fielder_id,
                -- Runner advancement outcomes
                CASE WHEN of_assist IS NOT NULL AND of_assist > 0 THEN TRUE ELSE FALSE END
                    AS threw_out_runner
            FROM of_bip
        """)

        log.info("  OF arm metrics extracted.")

    # ------------------------------------------------------------------
    # INFIELD OAA
    # ------------------------------------------------------------------

    def _compute_infield_oaa(self, seasons: list[int]) -> None:
        """
        Compute infield OAA using the intercept/time-margin model.

        Model inputs (from the Statcast infield OAA article):
          1. Distance fielder must travel to reach ball
          2. Time to get there (from ball speed)
          3. Throw distance to the base
          4. Batter-runner speed
          + Direction adjustments (lateral vs charging)
        """
        log.info("Computing infield OAA …")
        season_list = ", ".join(str(s) for s in seasons)

        df = self._conn.execute(f"""
            SELECT
                game_pk || '-' || at_bat_number || '-' || pitch_number AS pitch_id_str,
                game_pk,
                season,
                stand,
                batter,
                fielder_3, fielder_4, fielder_5, fielder_6,
                fielded_by,
                hc_x, hc_y,
                launch_speed, launch_angle, spray_angle,
                bb_type,
                events,
                outs_on_pitch,
                fielding_error,
                throwing_error_1, throwing_error_2
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND type = 'X'
              AND events IS NOT NULL
              AND hc_x IS NOT NULL AND hc_y IS NOT NULL
              AND launch_speed IS NOT NULL
              -- Infield plays: ground balls and some line drives
              AND (bb_type = 'ground_ball' OR (bb_type = 'line_drive' AND hc_y > 150))
        """).fetchdf()

        if len(df) < 500:
            log.warning("  Too few infield plays (%d)", len(df))
            return

        log.info("  Loaded %d infield plays", len(df))

        # Look up batter sprint speeds
        batter_speeds = self._conn.execute(f"""
            SELECT player_id, season, sprint_speed
            FROM pg.raw.sprint_speed
            WHERE season IN ({", ".join(str(s) for s in seasons)})
        """).fetchdf()
        speed_map = {}
        for _, r in batter_speeds.iterrows():
            speed_map[(int(r["player_id"]), int(r["season"]))] = (
                r["sprint_speed"] if pd.notna(r["sprint_speed"]) else DEFAULT_SPRINT_SPEED_FPS
            )

        rows = []
        for _, r in df.iterrows():
            stand = r["stand"] if r["stand"] in ("L", "R") else "R"

            # Determine which infielder is responsible
            best_pos = None
            best_dist = float("inf")
            for pos in ("1B", "2B", "3B", "SS"):
                fx, fy = AVG_FIELDER_POS[pos].get(stand, AVG_FIELDER_POS[pos]["R"])
                d = _euclidean_hc(r["hc_x"], r["hc_y"], fx, fy)
                if d < best_dist:
                    best_dist = d
                    best_pos = pos

            # Map position to fielder column
            pos_to_col = {
                "1B": "fielder_3",
                "2B": "fielder_4",
                "3B": "fielder_5",
                "SS": "fielder_6",
            }
            fielder_id = r[pos_to_col[best_pos]]
            if pd.isna(fielder_id):
                continue

            fx, fy = AVG_FIELDER_POS[best_pos].get(stand, AVG_FIELDER_POS[best_pos]["R"])
            fielder_dist_hc = _euclidean_hc(r["hc_x"], r["hc_y"], fx, fy)
            fielder_dist_ft = _hc_to_feet(fielder_dist_hc)

            # Throw distance to 1B
            throw_dist_hc = _euclidean_hc(r["hc_x"], r["hc_y"], FIRST_BASE[0], FIRST_BASE[1])
            throw_dist_ft = _hc_to_feet(throw_dist_hc)

            # Time for ball to reach fielder
            ball_travel_time = _estimate_gb_travel_time(
                r["launch_speed"], r["launch_angle"], fielder_dist_ft
            )
            if ball_travel_time is None:
                continue

            # Defense time: ball travel + exchange + throw
            throw_velo = AVG_THROW_VELO.get(best_pos, 80.0)
            throw_time = _throw_time(throw_dist_ft, throw_velo)
            defense_time = ball_travel_time + INFIELD_EXCHANGE_TIME + throw_time

            # Batter-runner time to 1B
            batter_speed = speed_map.get(
                (int(r["batter"]), int(r["season"])), DEFAULT_SPRINT_SPEED_FPS
            )
            runner_time = 90.0 / batter_speed  # seconds from home to first

            # Time margin (positive = defense has time to spare)
            time_margin = runner_time - defense_time

            # Direction classification
            direction_cat, going_back = _classify_direction_of(
                fx, fy, r["hc_x"], r["hc_y"], best_pos
            )
            if going_back:
                time_margin -= IF_AWAY_FROM_BAG_PENALTY_S

            # Lateral movement: moving away from throw target also costs time
            fx_to_ball = (r["hc_x"] - fx, r["hc_y"] - fy)
            fx_to_base = (FIRST_BASE[0] - fx, FIRST_BASE[1] - fy)
            dot = fx_to_ball[0] * fx_to_base[0] + fx_to_ball[1] * fx_to_base[1]
            mag1 = math.sqrt(fx_to_ball[0] ** 2 + fx_to_ball[1] ** 2)
            mag2 = math.sqrt(fx_to_base[0] ** 2 + fx_to_base[1] ** 2)
            if mag1 > 0 and mag2 > 0:
                cos_angle = dot / (mag1 * mag2)
                if cos_angle < -0.3:  # moving substantially away from 1B
                    time_margin -= 0.10

            # Outcome
            out_recorded = r["outs_on_pitch"] > 0
            is_error = pd.notna(r.get("fielding_error")) or pd.notna(r.get("throwing_error_1"))
            error_type = None
            if pd.notna(r.get("throwing_error_1")) or pd.notna(r.get("throwing_error_2")):
                error_type = "throwing"
            elif pd.notna(r.get("fielding_error")):
                error_type = "fielding"

            rows.append(
                {
                    "pitch_id_str": r["pitch_id_str"],
                    "game_pk": r["game_pk"],
                    "season": r["season"],
                    "fielder_id": int(fielder_id),
                    "position": best_pos,
                    "fielder_distance": fielder_dist_ft,
                    "throw_distance": throw_dist_ft,
                    "time_margin": time_margin,
                    "runner_speed": batter_speed,
                    "direction_category": direction_cat,
                    "out_recorded": out_recorded,
                    "is_error": is_error,
                    "error_type": error_type,
                }
            )

        plays = pd.DataFrame(rows)
        if len(plays) < 200:
            log.warning("  Too few valid infield plays (%d) after processing", len(plays))
            return

        # Fit the out probability model: P(out) = sigmoid(time_margin)
        X = plays[["time_margin"]].values
        y = plays["out_recorded"].astype(int).values

        model = _fit_logistic_model(X, y)
        if model is None:
            log.error("  Infield OAA model fit failed")
            return

        plays["out_probability"] = _predict_proba(model, X)
        plays["out_probability"] = plays["out_probability"].clip(0.01, 0.99)

        plays["oaa_credit"] = np.where(
            plays["out_recorded"],
            1.0 - plays["out_probability"],
            -plays["out_probability"],
        )

        self._conn.register("if_plays", plays)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_if_plays;
            CREATE TABLE _tmp_if_plays AS SELECT * FROM if_plays;
        """)
        self._conn.unregister("if_plays")

        log.info(
            "  Infield OAA computed: %d plays, model AUC ~%.3f",
            len(plays),
            _quick_auc(y, plays["out_probability"].values),
        )

    # ------------------------------------------------------------------
    # DOUBLE PLAY METRICS
    # ------------------------------------------------------------------

    def _compute_dp_metrics(self, seasons: list[int]) -> None:
        """
        Compute DP metrics using Tango's intercept/sigmoid approach.

        Scoped to: runner on 1B only, <2 outs, ground ball.
        Time-based model: time margin between defense completion and runner arrival.
        Split credit: initiator (fields ball) vs pivot (turns relay).
        """
        log.info("Computing double play metrics …")
        season_list = ", ".join(str(s) for s in seasons)

        df = self._conn.execute(f"""
            SELECT
                game_pk || '-' || at_bat_number || '-' || pitch_number AS pitch_id_str,
                game_pk,
                at_bat_number,
                season,
                stand,
                batter,
                on_1b,
                outs,
                fielder_3, fielder_4, fielder_5, fielder_6,
                fielded_by,
                hc_x, hc_y,
                launch_speed, launch_angle,
                bb_type,
                events,
                outs_on_pitch,
                field_assist_1, field_assist_2,
                field_putout_1, field_putout_2,
                home_score, away_score, inning, inning_topbot,
                -- runners state for RE24
                (CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)
                + (CASE WHEN on_2b IS NOT NULL THEN 2 ELSE 0 END)
                + (CASE WHEN on_3b IS NOT NULL THEN 4 ELSE 0 END) AS runners_state
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              AND on_1b IS NOT NULL
              AND on_2b IS NULL        -- runner on 1B only (Tango scoping)
              AND on_3b IS NULL
              AND outs < 2
              AND bb_type = 'ground_ball'
              AND hc_x IS NOT NULL AND hc_y IS NOT NULL
              AND launch_speed IS NOT NULL
        """).fetchdf()

        if len(df) < 200:
            log.warning("  Too few DP opportunities (%d)", len(df))
            return

        log.info("  Loaded %d DP opportunities", len(df))

        # Batter sprint speed lookup
        batter_speeds = self._conn.execute(f"""
            SELECT bsm.player_id, bsm.season, COALESCE(ss.sprint_speed, 27.0) AS sprint_speed
            FROM derived.baserunner_season_metrics bsm
            LEFT JOIN pg.raw.sprint_speed ss
                ON bsm.player_id = ss.player_id
                AND bsm.season = ss.season
            WHERE bsm.season IN ({", ".join(str(s) for s in seasons)})
        """).fetchdf()
        speed_map = {}
        for _, r in batter_speeds.iterrows():
            speed_map[(int(r["player_id"]), int(r["season"]))] = (
                r["sprint_speed"] if pd.notna(r["sprint_speed"]) else DEFAULT_SPRINT_SPEED_FPS
            )

        # Runner on 1B speed lookup
        speed_map.copy()

        rows = []
        for _, r in df.iterrows():
            stand = r["stand"] if r["stand"] in ("L", "R") else "R"

            # Identify initiator (who fielded the ball)
            fielded_by = r["fielded_by"]
            if pd.isna(fielded_by):
                continue

            pos_to_col = {
                "1B": "fielder_3",
                "2B": "fielder_4",
                "3B": "fielder_5",
                "SS": "fielder_6",
            }
            initiator_pos = None
            for pos, col in pos_to_col.items():
                if r[col] == fielded_by:
                    initiator_pos = pos
                    break
            if initiator_pos is None:
                continue

            # Fielder distance to ball
            fx, fy = AVG_FIELDER_POS.get(initiator_pos, {}).get(stand, (125, 155))
            fielder_dist_ft = _hc_to_feet(_euclidean_hc(r["hc_x"], r["hc_y"], fx, fy))

            # Distance from ball to 2B bag
            dist_to_2b_ft = _hc_to_feet(
                _euclidean_hc(r["hc_x"], r["hc_y"], SECOND_BASE[0], SECOND_BASE[1])
            )

            # Distance from 2B bag to 1B
            dist_2b_to_1b_ft = _hc_to_feet(
                _euclidean_hc(SECOND_BASE[0], SECOND_BASE[1], FIRST_BASE[0], FIRST_BASE[1])
            )

            # Ball travel time to fielder
            ball_time = _estimate_gb_travel_time(
                r["launch_speed"], r["launch_angle"], fielder_dist_ft
            )
            if ball_time is None:
                continue

            # Defense time: field + throw to 2B + pivot exchange + throw to 1B
            throw_velo_to_2b = AVG_THROW_VELO.get(initiator_pos, 80.0)
            time_to_2b = (
                ball_time + INFIELD_EXCHANGE_TIME + _throw_time(dist_to_2b_ft, throw_velo_to_2b)
            )

            pivot_exchange = 0.60  # pivot man faster exchange than initial fielder
            throw_to_1b_time = _throw_time(dist_2b_to_1b_ft, 78.0)  # pivot throw ~78mph
            total_defense_time = time_to_2b + pivot_exchange + throw_to_1b_time

            # Runner on 1B time to 2B
            runner_1b_id = int(r["on_1b"]) if pd.notna(r["on_1b"]) else 0
            runner_speed = speed_map.get((runner_1b_id, int(r["season"])), DEFAULT_SPRINT_SPEED_FPS)
            runner_time_to_2b = 90.0 / runner_speed

            # Batter time to 1B
            batter_speed = speed_map.get(
                (int(r["batter"]), int(r["season"])), DEFAULT_SPRINT_SPEED_FPS
            )
            batter_time_to_1b = 90.0 / batter_speed

            # Combined time margin: both runner must be beaten at 2B AND batter at 1B
            # The constraint is the tighter of the two
            time_margin_2b = runner_time_to_2b - time_to_2b
            time_margin_1b = batter_time_to_1b - total_defense_time
            time_margin = min(time_margin_2b, time_margin_1b)

            # Direction adjustment: moving toward or away from 2B bag
            toward_2b = r["hc_y"] < fy  # ball is closer to 2B than fielder start
            if not toward_2b and initiator_pos in ("3B", "1B"):
                time_margin -= 0.15

            # Outcome — Statcast never sets outs_on_pitch=2 for DPs; the relay
            # out is recorded via the events field only.
            dp_turned = r["events"] in ("grounded_into_double_play", "double_play")

            # Identify pivot man
            pivot_id = None
            pivot_pos = None
            if pd.notna(r.get("field_assist_2")):
                for pos, col in pos_to_col.items():
                    if r[col] == r["field_assist_2"]:
                        pivot_id = int(r["field_assist_2"])
                        pivot_pos = pos
                        break

            # RE24 run value
            pre_outs = int(r["outs"])
            pre_runners = int(r["runners_state"])
            re_start = self._re_matrix.get(
                (pre_outs, pre_runners), RE24_APPROX.get((pre_outs, pre_runners), 0.5)
            )

            if dp_turned:
                post_outs = pre_outs + 2
                post_runners = 0  # DP clears runners
                re_end = self._re_matrix.get((min(post_outs, 2), post_runners), 0.10)
            else:
                post_outs = pre_outs + (1 if r["outs_on_pitch"] > 0 else 0)
                # Approximate: force at 2B successful, runner safe at 1B
                post_runners = 0b001 if r["outs_on_pitch"] == 1 else pre_runners
                re_end = self._re_matrix.get((min(post_outs, 2), post_runners), 0.30)

            dp_run_value = re_start - re_end  # positive = runs prevented

            rows.append(
                {
                    "pitch_id_str": r["pitch_id_str"],
                    "game_pk": r["game_pk"],
                    "season": r["season"],
                    "initiator_id": int(fielded_by),
                    "initiator_position": initiator_pos,
                    "pivot_id": pivot_id,
                    "pivot_position": pivot_pos,
                    "time_margin": time_margin,
                    "runner_speed": runner_speed,
                    "batter_speed": batter_speed,
                    "fielder_toward_2b": toward_2b,
                    "dp_turned": dp_turned,
                    "outs_recorded": int(r["outs_on_pitch"]),
                    "re24_start": re_start,
                    "re24_end": re_end,
                    "dp_run_value": dp_run_value,
                }
            )

        dp_plays = pd.DataFrame(rows)
        if len(dp_plays) < 100:
            log.warning("  Too few valid DP plays (%d) after processing", len(dp_plays))
            return

        # Fit DP probability model: P(DP) = sigmoid(time_margin)
        X = dp_plays[["time_margin"]].values
        y = dp_plays["dp_turned"].astype(int).values

        model = _fit_logistic_model(X, y)
        if model is not None:
            dp_plays["dp_probability"] = _predict_proba(model, X)
            dp_plays["dp_probability"] = dp_plays["dp_probability"].clip(0.01, 0.99)
        else:
            # Fallback: use league average DP rate
            league_dp_rate = y.mean()
            dp_plays["dp_probability"] = league_dp_rate

        self._conn.register("dp_plays", dp_plays)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_dp_plays;
            CREATE TABLE _tmp_dp_plays AS SELECT * FROM dp_plays;
        """)
        self._conn.unregister("dp_plays")

        log.info(
            "  DP metrics computed: %d opportunities, DP rate %.1f%%",
            len(dp_plays),
            dp_plays["dp_turned"].mean() * 100,
        )

    # ------------------------------------------------------------------
    # BUNT DEFENSE
    # ------------------------------------------------------------------

    def _compute_bunt_defense(self, seasons: list[int]) -> None:
        """Compute bunt fielding rates for corner infielders."""
        log.info("Computing bunt defense metrics …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_bunt_defense;
            CREATE TABLE _tmp_bunt_defense AS

            WITH bunt_plays AS (
                SELECT
                    season,
                    fielded_by,
                    fielder_3, fielder_5,
                    outs_on_pitch,
                    events,
                    CASE
                        WHEN fielded_by = fielder_3 THEN '1B'
                        WHEN fielded_by = fielder_5 THEN '3B'
                        ELSE 'other'
                    END AS fielder_position
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  AND type = 'X'
                  AND events IS NOT NULL
                  -- Detect bunts: look for 'bunt' in description/events or very low EV + LA
                  AND (
                      events LIKE '%bunt%'
                      OR (launch_speed IS NOT NULL AND launch_speed < 50
                          AND launch_angle IS NOT NULL AND launch_angle BETWEEN -10 AND 30
                          AND bb_type = 'ground_ball'
                          AND hit_distance_sc IS NOT NULL AND hit_distance_sc < 60)
                  )
            )
            SELECT
                fielded_by AS fielder_id,
                fielder_position AS position,
                season,
                COUNT(*) AS bunt_opportunities,
                SUM(CASE WHEN outs_on_pitch > 0 THEN 1 ELSE 0 END) AS bunt_outs_recorded,
                SUM(CASE WHEN outs_on_pitch > 0 THEN 1 ELSE 0 END) * 1.0
                    / NULLIF(COUNT(*), 0) AS bunt_fielding_rate
            FROM bunt_plays
            WHERE fielder_position IN ('1B', '3B')
              AND fielded_by IS NOT NULL
            GROUP BY fielded_by, fielder_position, season
        """)

        log.info("  Bunt defense metrics computed.")

    # ------------------------------------------------------------------
    # FIRST BASE SCOOPING
    # ------------------------------------------------------------------

    def _compute_first_base_scooping(self, seasons: list[int]) -> None:
        """Compute 1B receiving metrics on difficult throws."""
        log.info("Computing 1B scooping metrics …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_1b_scoop;
            CREATE TABLE _tmp_1b_scoop AS

            WITH scoop_plays AS (
                SELECT
                    season,
                    fielder_3 AS first_baseman_id,
                    field_putout_1,
                    field_putout_2,
                    field_assist_1,
                    fielded_by,
                    outs_on_pitch,
                    throwing_error_1,
                    -- A scoop opportunity: throw from deep infield to 1B
                    -- Identified by: putout at 1B on a throw from SS/3B (longer throws)
                    CASE WHEN (fielded_by = fielder_5 OR fielded_by = fielder_6)
                              AND hc_x IS NOT NULL
                              AND bb_type = 'ground_ball'
                         THEN TRUE ELSE FALSE END AS is_scoop_candidate
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  AND type = 'X'
                  AND events IS NOT NULL
                  AND fielder_3 IS NOT NULL
            )
            SELECT
                first_baseman_id AS fielder_id,
                '1B' AS position,
                season,
                SUM(CASE WHEN is_scoop_candidate THEN 1 ELSE 0 END) AS scoop_opportunities,
                SUM(CASE WHEN is_scoop_candidate AND outs_on_pitch > 0 THEN 1 ELSE 0 END)
                    * 1.0 / NULLIF(SUM(CASE WHEN is_scoop_candidate THEN 1 ELSE 0 END), 0)
                    AS scoop_success_rate
            FROM scoop_plays
            WHERE first_baseman_id IS NOT NULL
            GROUP BY first_baseman_id, season
        """)

        log.info("  1B scooping metrics computed.")

    # ------------------------------------------------------------------
    # ERROR DECOMPOSITION
    # ------------------------------------------------------------------

    def _compute_error_decomposition(self, seasons: list[int]) -> None:
        """Compute fielding error vs throwing error rates by fielder."""
        log.info("Computing error decomposition …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_errors;
            CREATE TABLE _tmp_errors AS

            WITH fielder_errors AS (
                SELECT
                    season,
                    fielded_by AS fielder_id,
                    CASE
                        WHEN fielded_by = fielder_3 THEN '1B'
                        WHEN fielded_by = fielder_4 THEN '2B'
                        WHEN fielded_by = fielder_5 THEN '3B'
                        WHEN fielded_by = fielder_6 THEN 'SS'
                        WHEN fielded_by = fielder_7 THEN 'LF'
                        WHEN fielded_by = fielder_8 THEN 'CF'
                        WHEN fielded_by = fielder_9 THEN 'RF'
                        ELSE 'unknown'
                    END AS position,
                    -- Total plays fielded
                    1 AS play,
                    -- Fielding error: misplay on the ball itself
                    CASE WHEN fielding_error = fielded_by THEN 1 ELSE 0 END AS fielding_err,
                    -- Throwing error: clean field but bad throw
                    CASE WHEN throwing_error_1 = fielded_by
                           OR throwing_error_2 = fielded_by THEN 1 ELSE 0 END AS throwing_err
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  AND type = 'X'
                  AND events IS NOT NULL
                  AND fielded_by IS NOT NULL
            )
            SELECT
                fielder_id,
                position,
                season,
                SUM(play)           AS total_plays_fielded,
                SUM(fielding_err)   AS fielding_error_count,
                SUM(throwing_err)   AS throwing_error_count,
                SUM(fielding_err) * 1.0 / NULLIF(SUM(play), 0) AS fielding_error_rate,
                SUM(throwing_err) * 1.0 / NULLIF(SUM(play), 0) AS throwing_error_rate,
                (SUM(fielding_err) + SUM(throwing_err)) * 1.0 / NULLIF(SUM(play), 0) AS error_rate
            FROM fielder_errors
            WHERE position != 'unknown'
            GROUP BY fielder_id, position, season
        """)

        log.info("  Error decomposition computed.")

    # ------------------------------------------------------------------
    # AGGREGATE SEASON-LEVEL METRICS
    # ------------------------------------------------------------------

    def _ensure_fielder_temp_tables_exist(self) -> None:
        """Create empty `_tmp_*` placeholders for any temp table the
        fielder aggregator JOINs against that hasn't been created yet
        (e.g. because the upstream compute step returned early on a
        sample-size guard).

        Columns match what the aggregator's CTEs SELECT — the goal is
        to make the JOIN a no-op when the corresponding compute step
        was skipped, NOT to substitute real data.
        """
        # Each entry: (table_name, CREATE TABLE DDL with the columns
        # the aggregator references).  We use CREATE TABLE IF NOT EXISTS
        # so this is idempotent: if the compute step DID create the
        # table, this no-ops and the real data is preserved.
        placeholders = {
            # Outfield catch probability — _compute_outfield_catch_probability
            "_tmp_of_plays": """
                CREATE TABLE IF NOT EXISTS _tmp_of_plays (
                    fielder_id INTEGER, position VARCHAR, season SMALLINT,
                    catch_probability FLOAT, caught BOOLEAN,
                    oaa_credit FLOAT, direction_cat VARCHAR, star_rating INTEGER
                )
            """,
            # Infield OAA — _compute_infield_oaa
            "_tmp_if_plays": """
                CREATE TABLE IF NOT EXISTS _tmp_if_plays (
                    fielder_id INTEGER, position VARCHAR, season SMALLINT,
                    out_probability FLOAT, out_recorded BOOLEAN,
                    oaa_credit FLOAT, direction_category VARCHAR
                )
            """,
            # Double-play metrics — _compute_dp_metrics
            "_tmp_dp_plays": """
                CREATE TABLE IF NOT EXISTS _tmp_dp_plays (
                    initiator_id INTEGER, initiator_position VARCHAR,
                    pivot_id INTEGER, pivot_position VARCHAR,
                    season SMALLINT,
                    dp_probability FLOAT, dp_turned BOOLEAN,
                    dp_run_value FLOAT
                )
            """,
            # Errors — _compute_error_decomposition
            # Schema follows the SELECT shape inside the aggregator's `err` CTE
            # (SELECT * FROM _tmp_errors).  Keep the column set minimal but
            # include the foreign-key columns the outer SELECT joins on.
            "_tmp_errors": """
                CREATE TABLE IF NOT EXISTS _tmp_errors (
                    season SMALLINT, fielder_id INTEGER, position VARCHAR,
                    total_plays INTEGER, fielding_errors INTEGER,
                    throwing_errors INTEGER, error_rate FLOAT
                )
            """,
            # Bunt defense — _compute_bunt_defense
            "_tmp_bunt_defense": """
                CREATE TABLE IF NOT EXISTS _tmp_bunt_defense (
                    season SMALLINT, fielder_id INTEGER, position VARCHAR,
                    bunt_opportunities INTEGER, bunt_outs INTEGER,
                    bunt_success_rate FLOAT
                )
            """,
            # 1B scooping — _compute_first_base_scooping
            "_tmp_1b_scoop": """
                CREATE TABLE IF NOT EXISTS _tmp_1b_scoop (
                    season SMALLINT, fielder_id INTEGER, position VARCHAR,
                    scoop_opportunities INTEGER, scoop_successes INTEGER,
                    scoop_success_rate FLOAT
                )
            """,
            # OF arm — _compute_outfield_arm_metrics (not joined directly by
            # the aggregator today, but referenced by the cleanup loop, so
            # we ensure it's safe to DROP).
            "_tmp_of_arm": """
                CREATE TABLE IF NOT EXISTS _tmp_of_arm (
                    season SMALLINT, fielder_id INTEGER, position VARCHAR,
                    arm_opportunities INTEGER, arm_runs FLOAT
                )
            """,
        }
        for table, ddl in placeholders.items():
            try:
                self._conn.execute(ddl)
            except Exception as exc:  # noqa: BLE001  - defensive log + continue
                log.warning("  Could not ensure placeholder %s exists: %s", table, exc)

    def _aggregate_fielder_season_metrics(self, seasons: list[int]) -> None:
        """
        Combine all per-play detail tables into the final
        derived.fielder_season_metrics rows.
        """
        log.info("Aggregating fielder season metrics …")
        season_list = ", ".join(str(s) for s in seasons)

        # ----------------------------------------------------------------
        # Defensive: ensure every _tmp_* table the aggregator JOINs against
        # exists, even if the upstream _compute_* step returned early due to
        # the sample-size guard (e.g. _compute_outfield_catch_probability
        # bails out when len(df) < 500).  Without this step the aggregator
        # crashes with "Catalog Error: Table with name _tmp_of_plays does
        # not exist!" on small / partial data loads.  Creating an empty
        # placeholder with the right columns is a no-op JOIN target and
        # preserves the aggregator's shape.
        # ----------------------------------------------------------------
        self._ensure_fielder_temp_tables_exist()

        # Delete existing rows for these seasons (full rebuild pattern)
        self._conn.execute(f"""
            DELETE FROM derived.fielder_season_metrics
            WHERE season IN ({season_list})
        """)

        self._conn.execute(f"""
            INSERT INTO derived.fielder_season_metrics

            WITH
            -- Outfield OAA aggregation
            of_agg AS (
                SELECT
                    fielder_id AS player_id,
                    position,
                    season,
                    COUNT(*)                        AS opportunities,
                    SUM(catch_probability)           AS expected_outs,
                    SUM(CASE WHEN caught THEN 1 ELSE 0 END) AS actual_outs,
                    SUM(oaa_credit)                  AS outs_above_average,
                    AVG(catch_probability)            AS expected_catch_pct,
                    SUM(CASE WHEN caught THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS actual_catch_pct,

                    -- Directional breakdown
                    SUM(CASE WHEN direction_cat = 'glove_side' THEN oaa_credit ELSE 0 END) AS oaa_glove_side,
                    SUM(CASE WHEN direction_cat = 'arm_side' THEN oaa_credit ELSE 0 END) AS oaa_arm_side,
                    SUM(CASE WHEN direction_cat = 'charging' THEN oaa_credit ELSE 0 END) AS oaa_charging,
                    SUM(CASE WHEN direction_cat = 'deep' THEN oaa_credit ELSE 0 END) AS oaa_deep,

                    -- Star ratings
                    SUM(CASE WHEN star_rating = 5 THEN 1 ELSE 0 END) AS five_star_opps,
                    SUM(CASE WHEN star_rating = 5 AND caught THEN 1 ELSE 0 END) AS five_star_catches,
                    SUM(CASE WHEN star_rating = 4 THEN 1 ELSE 0 END) AS four_star_opps,
                    SUM(CASE WHEN star_rating = 4 AND caught THEN 1 ELSE 0 END) AS four_star_catches,
                    SUM(CASE WHEN star_rating IN (1, 2) THEN 1 ELSE 0 END) AS routine_opps,
                    SUM(CASE WHEN star_rating IN (1, 2) AND caught THEN 1 ELSE 0 END) AS routine_catches

                FROM _tmp_of_plays
                GROUP BY fielder_id, position, season
            ),
            -- Infield OAA aggregation
            if_agg AS (
                SELECT
                    fielder_id AS player_id,
                    position,
                    season,
                    COUNT(*)                        AS opportunities,
                    SUM(out_probability)             AS expected_outs,
                    SUM(CASE WHEN out_recorded THEN 1 ELSE 0 END) AS actual_outs,
                    SUM(oaa_credit)                  AS outs_above_average,
                    AVG(out_probability)              AS expected_catch_pct,
                    SUM(CASE WHEN out_recorded THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS actual_catch_pct,

                    SUM(CASE WHEN direction_category = 'glove_side' THEN oaa_credit ELSE 0 END) AS oaa_glove_side,
                    SUM(CASE WHEN direction_category = 'arm_side' THEN oaa_credit ELSE 0 END) AS oaa_arm_side,
                    SUM(CASE WHEN direction_category = 'charging' THEN oaa_credit ELSE 0 END) AS oaa_charging,
                    SUM(CASE WHEN direction_category = 'deep' THEN oaa_credit ELSE 0 END) AS oaa_deep

                FROM _tmp_if_plays
                GROUP BY fielder_id, position, season
            ),
            -- Combined OAA (union OF and IF)
            combined_oaa AS (
                SELECT * FROM of_agg
                UNION ALL
                SELECT player_id, position, season,
                       opportunities, expected_outs, actual_outs, outs_above_average,
                       expected_catch_pct, actual_catch_pct,
                       oaa_glove_side, oaa_arm_side, oaa_charging, oaa_deep,
                       NULL, NULL, NULL, NULL, NULL, NULL  -- star ratings NULL for IF
                FROM if_agg
            ),
            -- DP aggregation by initiator
            dp_init_agg AS (
                SELECT
                    initiator_id AS player_id,
                    initiator_position AS position,
                    season,
                    COUNT(*)                    AS dp_opportunities,
                    SUM(dp_probability)          AS dp_expected,
                    SUM(CASE WHEN dp_turned THEN 1 ELSE 0 END) AS dp_turned,
                    SUM(CASE WHEN dp_turned THEN 1 ELSE 0 END) - SUM(dp_probability) AS dp_above_expected,
                    SUM(dp_run_value)            AS dp_run_value
                FROM _tmp_dp_plays
                GROUP BY initiator_id, initiator_position, season
            ),
            -- DP pivot aggregation
            dp_pivot_agg AS (
                SELECT
                    pivot_id AS player_id,
                    pivot_position AS position,
                    season,
                    COUNT(*) AS dp_pivot_opportunities,
                    SUM(CASE WHEN dp_turned THEN 1 ELSE 0 END) AS dp_pivot_completions,
                    SUM(dp_probability) AS dp_pivot_expected,
                    SUM(CASE WHEN dp_turned THEN 1 ELSE 0 END) - SUM(dp_probability)
                        AS dp_pivot_above_expected
                FROM _tmp_dp_plays
                WHERE pivot_id IS NOT NULL
                GROUP BY pivot_id, pivot_position, season
            ),
            -- Errors
            err AS (
                SELECT * FROM _tmp_errors
            ),
            -- Bunt defense
            bunt AS (
                SELECT * FROM _tmp_bunt_defense
            ),
            -- 1B scooping
            scoop AS (
                SELECT * FROM _tmp_1b_scoop
            )

            SELECT
                c.player_id,
                c.position,
                c.season,
                0.0                         AS innings_played,
                c.opportunities             AS sample_batted_balls,

                -- Range / OAA
                c.opportunities,
                c.expected_outs,
                c.actual_outs,
                c.outs_above_average,
                c.expected_catch_pct,
                c.actual_catch_pct,
                c.actual_catch_pct - c.expected_catch_pct AS catch_pct_added,

                -- Range runs
                CASE WHEN c.position IN ('LF','CF','RF')
                     THEN c.outs_above_average * {RUNS_PER_OAA_OUTFIELD}
                     ELSE c.outs_above_average * {RUNS_PER_OAA_INFIELD}
                END AS range_runs,

                -- Directional
                c.oaa_glove_side,
                c.oaa_arm_side,
                c.oaa_charging,
                c.oaa_deep,

                -- Star ratings (OF only)
                c.five_star_opps,
                c.five_star_catches,
                c.four_star_opps,
                c.four_star_catches,
                c.routine_opps,
                c.routine_catches,

                -- Errors
                COALESCE(e.error_rate, 0)           AS error_rate,
                COALESCE(e.fielding_error_count, 0) AS fielding_error_count,
                COALESCE(e.throwing_error_count, 0) AS throwing_error_count,
                COALESCE(e.fielding_error_rate, 0)  AS fielding_error_rate,
                COALESCE(e.throwing_error_rate, 0)  AS throwing_error_rate,
                NULL::FLOAT                          AS error_oaa_cost,

                -- OF Arm (NULL for infielders)
                NULL::FLOAT AS arm_strength,
                NULL::INTEGER AS arm_opportunities,
                NULL::INTEGER AS arm_holds,
                NULL::FLOAT AS arm_hold_rate,
                NULL::INTEGER AS arm_assists,
                NULL::FLOAT AS arm_thrown_out_rate,
                NULL::FLOAT AS arm_advancement_prevention,
                NULL::FLOAT AS of_arm_runs,

                -- DP (NULL for outfielders)
                dp.dp_opportunities,
                NULL::INTEGER AS dp_attempts,
                dp.dp_turned,
                dp.dp_expected,
                dp.dp_above_expected,
                CASE WHEN dp.dp_opportunities > 0
                     THEN dp.dp_turned * 1.0 / dp.dp_opportunities END AS dp_attempt_rate,
                NULL::FLOAT AS dp_success_rate,
                dp.dp_run_value,

                piv.dp_pivot_opportunities,
                piv.dp_pivot_completions,
                piv.dp_pivot_expected,
                piv.dp_pivot_above_expected,
                NULL::FLOAT AS dp_pivot_run_value,

                -- Bunt defense
                b.bunt_opportunities,
                b.bunt_outs_recorded,
                b.bunt_fielding_rate,

                -- 1B scooping
                s.scoop_opportunities,
                s.scoop_success_rate,

                -- Meta
                (c.opportunities < {MIN_FIELDER_PLAYS}) AS below_minimum_sample,
                CURRENT_TIMESTAMP AS updated_at

            FROM combined_oaa c
            LEFT JOIN dp_init_agg dp
                ON c.player_id = dp.player_id AND c.position = dp.position AND c.season = dp.season
            LEFT JOIN dp_pivot_agg piv
                ON c.player_id = piv.player_id AND c.position = piv.position AND c.season = piv.season
            LEFT JOIN err e
                ON c.player_id = e.fielder_id AND c.position = e.position AND c.season = e.season
            LEFT JOIN bunt b
                ON c.player_id = b.fielder_id AND c.position = b.position AND c.season = b.season
            LEFT JOIN scoop s
                ON c.player_id = s.fielder_id AND c.position = s.position AND c.season = s.season
        """)

        log.info("  Fielder season metrics aggregated.")

    def _ensure_catcher_temp_tables_exist(self) -> None:
        """Defensive — same pattern as _ensure_fielder_temp_tables_exist.

        The catcher aggregator FULL-OUTER-JOINs three temp tables:
          _tmp_framing            ← created by _compute_catcher_framing
          _tmp_blocking           ← created by _compute_catcher_blocking
          _tmp_catcher_throwing   ← created by _compute_catcher_throwing

        Each compute step may early-`return` when the per-season sample
        size falls below its guard (typical on partial / small data
        loads).  Empty placeholders preserve the aggregator's join
        shape without injecting fake rows.
        """
        placeholders = {
            "_tmp_framing": """
                CREATE TABLE IF NOT EXISTS _tmp_framing (
                    catcher_id INTEGER, season SMALLINT,
                    called_pitches INTEGER, called_strikes INTEGER,
                    expected_called_strikes FLOAT, strikes_above_average FLOAT,
                    framing_runs FLOAT,
                    framing_low FLOAT, framing_high FLOAT,
                    framing_inside FLOAT, framing_outside FLOAT,
                    shadow_zone_strike_rate FLOAT, heart_zone_strike_rate FLOAT
                )
            """,
            "_tmp_blocking": """
                CREATE TABLE IF NOT EXISTS _tmp_blocking (
                    catcher_id INTEGER, season SMALLINT,
                    expected_pbwp FLOAT, actual_pbwp INTEGER,
                    blocks_above_average FLOAT, blocking_runs FLOAT,
                    blocks_aa_bounced FLOAT, blocks_aa_high FLOAT,
                    blocks_aa_lateral FLOAT
                )
            """,
            "_tmp_catcher_throwing": """
                CREATE TABLE IF NOT EXISTS _tmp_catcher_throwing (
                    catcher_id INTEGER, season SMALLINT,
                    sb_attempts_faced INTEGER, cs_total INTEGER,
                    sb_attempts_2b INTEGER, cs_rate_2b FLOAT,
                    sb_attempts_3b INTEGER, cs_rate_3b FLOAT,
                    cs_rate FLOAT, sb_allowed_rate FLOAT,
                    sb_opportunities_pa INTEGER,
                    steal_attempt_rate_against FLOAT
                )
            """,
        }
        for table, ddl in placeholders.items():
            try:
                self._conn.execute(ddl)
            except Exception as exc:  # noqa: BLE001
                log.warning("  Could not ensure placeholder %s exists: %s", table, exc)

    def _aggregate_catcher_season_metrics(self, seasons: list[int]) -> None:
        """Combine framing, blocking, and throwing into catcher season metrics."""
        log.info("Aggregating catcher season metrics …")
        season_list = ", ".join(str(s) for s in seasons)

        # SIM-2026-05 hardening: see _ensure_fielder_temp_tables_exist for
        # the rationale.  When a per-season sample is too thin, the upstream
        # _compute_catcher_{framing,blocking,throwing} method returns early
        # without creating its _tmp_* table.  Guarantee the aggregator's
        # FULL OUTER JOIN inputs all exist (possibly empty) before running.
        self._ensure_catcher_temp_tables_exist()

        self._conn.execute(f"""
            DELETE FROM derived.catcher_season_metrics
            WHERE season IN ({season_list})
        """)

        self._conn.execute(f"""
            INSERT INTO derived.catcher_season_metrics (
                player_id, season,
                pitches_received_total, called_pitches, called_strikes,
                expected_called_strikes, strikes_above_average, framing_runs,
                framing_low, framing_high, framing_inside, framing_outside,
                shadow_zone_strike_rate, heart_zone_strike_rate,
                expected_pbwp, actual_pbwp, blocks_above_average, blocking_runs,
                blocks_aa_bounced, blocks_aa_high, blocks_aa_lateral,
                pop_time_mean, pop_time_std, arm_strength_mean,
                sb_attempts_faced, cs_total, cs_rate, sb_allowed_rate,
                sb_attempts_2b, cs_rate_2b, sb_attempts_3b, cs_rate_3b,
                steal_attempt_rate_against,
                pickoff_attempts, pickoff_successes, pickoff_rate,
                below_minimum_sample, updated_at
            )

            SELECT
                COALESCE(f.catcher_id, b.catcher_id, t.catcher_id) AS player_id,
                COALESCE(f.season, b.season, t.season) AS season,

                -- Framing
                f.called_pitches            AS pitches_received_total,
                f.called_pitches,
                f.called_strikes,
                f.expected_called_strikes,
                f.strikes_above_average,
                f.framing_runs,
                f.framing_low,
                f.framing_high,
                f.framing_inside,
                f.framing_outside,
                -- SIM-408: zone-framing strike rates (catcher engine)
                f.shadow_zone_strike_rate,
                f.heart_zone_strike_rate,

                -- Blocking
                b.expected_pbwp,
                CAST(b.actual_pbwp AS INTEGER) AS actual_pbwp,
                b.blocks_above_average,
                b.blocking_runs,
                b.blocks_aa_bounced,
                b.blocks_aa_high,
                b.blocks_aa_lateral,

                -- Throwing
                -- Pop time proxy from cs_rate
                CASE WHEN t.cs_rate IS NOT NULL
                     THEN 2.0 + (0.25 - t.cs_rate) * 2.0
                     ELSE NULL END              AS pop_time_mean,
                0.08                            AS pop_time_std,
                NULL::FLOAT                     AS arm_strength_mean,
                CAST(t.sb_attempts_faced AS INTEGER),
                CAST(t.cs_total AS INTEGER),
                t.cs_rate,
                t.sb_allowed_rate,
                CAST(t.sb_attempts_2b AS INTEGER),
                t.cs_rate_2b,
                CAST(t.sb_attempts_3b AS INTEGER),
                t.cs_rate_3b,

                -- SIM-073 deterrence rate (PA-level steal-attempt rate).
                -- Min-sample guard already enforced inside _tmp_catcher_throwing
                -- (NULL when sb_opportunities_pa < 100).
                t.steal_attempt_rate_against,

                -- Pickoffs (not yet computed — placeholder)
                NULL::INTEGER AS pickoff_attempts,
                NULL::INTEGER AS pickoff_successes,
                NULL::FLOAT   AS pickoff_rate,

                -- Meta
                (COALESCE(CAST(t.sb_attempts_faced AS INTEGER), 0) < {MIN_CATCHER_SB_ATTEMPTS})
                    AS below_minimum_sample,
                CURRENT_TIMESTAMP AS updated_at

            FROM _tmp_framing f
            FULL OUTER JOIN _tmp_blocking b
                ON f.catcher_id = b.catcher_id AND f.season = b.season
            FULL OUTER JOIN _tmp_catcher_throwing t
                ON COALESCE(f.catcher_id, b.catcher_id) = t.catcher_id
                AND COALESCE(f.season, b.season) = t.season
        """)

        # Clean up temp tables
        for t in [
            "_tmp_framing",
            "_tmp_blocking",
            "_tmp_catcher_throwing",
            "_tmp_of_plays",
            "_tmp_if_plays",
            "_tmp_dp_plays",
            "_tmp_of_arm",
            "_tmp_bunt_defense",
            "_tmp_1b_scoop",
            "_tmp_errors",
        ]:
            self._conn.execute(f"DROP TABLE IF EXISTS {t}")

        log.info("  Catcher season metrics aggregated.")

    def _build_pitch_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """
        Rebuild sim.pitch_pool from raw.pitches.
        Excludes quality-flagged rows.  Adds:
          - runners_state bitmask
          - score_diff
          - outcome_type classification
          - prev_pitch_velo / ivb / hb / outcome (LAG window)
          - pitch_id surrogate key (row_number over natural sort)
          - recency_weight (SIM-076)
        When ``incremental`` (SIM-095), only seasons whose source changed rebuild.
        """
        ref_season = _canonical_ref_season(self._conn, seasons)
        if incremental:
            seasons = _seasons_needing_rebuild(self._conn, "pitch_pool", seasons)
            if not seasons:
                log.info("Building sim.pitch_pool … all seasons fresh; skipped.")
                return
        log.info("Building sim.pitch_pool … (seasons=%s)", seasons)
        season_list = ", ".join(str(s) for s in seasons)
        recency_expr = _recency_weight_sql(
            "CAST(EXTRACT(year FROM game_date) AS SMALLINT)", ref_season
        )

        # Delete existing rows for these seasons, then insert
        self._conn.execute(f"DELETE FROM sim.pitch_pool WHERE season IN ({season_list})")

        self._conn.execute(f"""
            INSERT INTO sim.pitch_pool

            WITH ordered AS (
                SELECT
                    (game_pk || LPAD(at_bat_number::VARCHAR, 3, '0') || LPAD(pitch_number::VARCHAR, 3, '0'))::BIGINT AS pitch_id,
                    game_pk,
                    at_bat_number,
                    pitch_number,
                    game_date,
                    CAST(EXTRACT(year FROM game_date) AS SMALLINT) AS season,
                    pitcher                             AS pitcher_id,
                    p_throws,
                    batter                              AS batter_id,
                    -- SIM-345 pool `stand` contract: the pool's `stand` column is
                    -- the RESOLVED batting side for the PA, so it agrees with the
                    -- `bat_hand`-keyed spray angle / FAISS tile key and with the
                    -- read-path pre-filter (which scopes on the batter's actual
                    -- side, never 'S'). For a switch hitter (`stand`='S'),
                    -- `bat_hand` carries the side they batted from that PA; we
                    -- adopt it. When `bat_hand` is itself unresolved ('S' or
                    -- NULL — gate-capped ≤1 %/season by SIM-160) we fall back to
                    -- the declared `stand`. Result is always 'L' or 'R' except
                    -- for the residual unresolved-switch tail, keeping `stand`
                    -- and `bat_hand` consistent for every fully-resolved row.
                    CASE
                        WHEN bat_hand IN ('L', 'R') THEN bat_hand
                        ELSE stand
                    END                                 AS stand,
                    release_speed                       AS velo,
                    break_vertical_induced              AS ivb,
                    break_horizontal                    AS hb,
                    release_spin_rate                   AS spin_rate,
                    spin_axis,
                    release_pos_x                       AS release_x,
                    release_pos_z                       AS release_z,
                    release_extension                   AS release_ext,
                    plate_x,
                    plate_z,
                    zone,
                    balls                               AS count_balls,
                    strikes                             AS count_strikes,
                    outs,
                    -- Runners state bitmask: bit0=1B, bit1=2B, bit2=3B
                    (CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END
                     + CASE WHEN on_2b IS NOT NULL THEN 2 ELSE 0 END
                     + CASE WHEN on_3b IS NOT NULL THEN 4 ELSE 0 END)::SMALLINT
                                                        AS runners_state,
                    inning,
                    -- Score diff capped at ±5 for similarity scoring
                    GREATEST(-5, LEAST(5, bat_score - fld_score))::SMALLINT
                                                        AS score_diff,

                    -- Prev pitch context via LAG window over (game, at_bat, pitch_number)
                    LAG(release_speed,          1) OVER w AS prev_pitch_velo,
                    LAG(break_vertical_induced, 1) OVER w AS prev_pitch_ivb,
                    LAG(break_horizontal,       1) OVER w AS prev_pitch_hb,
                    LAG(type,                   1) OVER w AS prev_pitch_outcome,

                    -- Outcome type classification from the MLB Gameday pitch
                    -- `type` code (SIM-421 fix).  The in-play code splits THREE
                    -- ways -- X=out, D=hit (no out), E=hit with run(s) -- so the
                    -- old `type='X'`-only mapping silently dropped ~all hits +
                    -- home runs into 'ball' (the no-balls-in-play defect).  TRIM
                    -- guards the space-padded Gameday codes.
                    CASE
                        WHEN TRIM(type) IN ('B', '*B') THEN 'ball'
                        WHEN TRIM(type) = 'C' THEN 'called_strike'
                        WHEN TRIM(type) IN ('S', 'W', 'M') THEN 'swinging_strike'
                        WHEN TRIM(type) IN ('F', 'T', 'L') THEN 'foul'
                        WHEN TRIM(type) IN ('X', 'D', 'E') THEN 'in_play'
                        ELSE 'ball'
                    END                                 AS outcome_type,
                    events,
                    {recency_expr} AS recency_weight,

                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
                WINDOW w AS (
                    PARTITION BY game_pk, at_bat_number
                    ORDER BY pitch_number
                    ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
                )
            )
            SELECT * FROM ordered
        """)
        log.info("  sim.pitch_pool done.")
        _record_pool_build(self._conn, "pitch_pool", seasons, ref_season)

    # ------------------------------------------------------------------
    # 9. sim.outcome_pool
    # ------------------------------------------------------------------

    def _build_outcome_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """
        Rebuilds sim.outcome_pool as the in-play subset of sim.pitch_pool
        with full batted ball detail and fielding outcome columns added.
        Adds recency_weight (SIM-076). When ``incremental`` (SIM-095), only
        seasons whose source changed rebuild.

        Fielder position is looked up from the fielder_N columns so the
        simulation engine knows which positional number fielded the ball.
        """
        ref_season = _canonical_ref_season(self._conn, seasons)
        if incremental:
            seasons = _seasons_needing_rebuild(self._conn, "outcome_pool", seasons)
            if not seasons:
                log.info("Building sim.outcome_pool … all seasons fresh; skipped.")
                return
        log.info("Building sim.outcome_pool … (seasons=%s)", seasons)
        season_list = ", ".join(str(s) for s in seasons)
        recency_expr = _recency_weight_sql("pp.season", ref_season)

        self._conn.execute(f"DELETE FROM sim.outcome_pool WHERE season IN ({season_list})")

        self._conn.execute(f"""
            INSERT INTO sim.outcome_pool

            WITH bip AS (
                SELECT
                    pp.pitch_id,
                    pp.game_pk, pp.at_bat_number, pp.pitch_number,
                    pp.game_date, pp.season,
                    pp.pitcher_id, pp.p_throws,
                    pp.batter_id, pp.stand,
                    pp.velo, pp.ivb, pp.hb, pp.spin_rate, pp.spin_axis,
                    pp.release_x, pp.release_z, pp.release_ext,
                    pp.plate_x, pp.plate_z, pp.zone,
                    pp.count_balls, pp.count_strikes, pp.outs,
                    pp.runners_state, pp.inning, pp.score_diff,
                    pp.events,
                    -- Batted ball detail from raw.pitches
                    rp.launch_speed                 AS exit_velo,
                    rp.launch_angle,
                    rp.spray_angle,
                    -- SIM-051: handedness-corrected pull-relative spray angle.
                    -- Sign convention: positive = pull side (LF for RHB, RF for LHB).
                    -- `bat_hand` is the per-pitch resolved handedness; switch
                    -- hitters with bat_hand='S' get NULL (gate-controlled to
                    -- ≤ 1 % per season by SIM-160).
                    CASE
                        WHEN rp.bat_hand = 'R' THEN  rp.spray_angle
                        WHEN rp.bat_hand = 'L' THEN -rp.spray_angle
                        ELSE NULL
                    END                              AS pull_relative_spray_angle,
                    rp.bb_type,
                    rp.hit_distance_sc              AS hit_distance,
                    rp.hc_x,
                    rp.hc_y,
                    -- Fielder who fielded the ball — translate player_id to position number
                    CASE
                        WHEN rp.fielded_by = rp.fielder_2 THEN 2
                        WHEN rp.fielded_by = rp.fielder_3 THEN 3
                        WHEN rp.fielded_by = rp.fielder_4 THEN 4
                        WHEN rp.fielded_by = rp.fielder_5 THEN 5
                        WHEN rp.fielded_by = rp.fielder_6 THEN 6
                        WHEN rp.fielded_by = rp.fielder_7 THEN 7
                        WHEN rp.fielded_by = rp.fielder_8 THEN 8
                        WHEN rp.fielded_by = rp.fielder_9 THEN 9
                        ELSE NULL
                    END::SMALLINT                   AS fielded_by_position,
                    CASE
                        WHEN rp.fielding_error = rp.fielder_2 THEN 2
                        WHEN rp.fielding_error = rp.fielder_3 THEN 3
                        WHEN rp.fielding_error = rp.fielder_4 THEN 4
                        WHEN rp.fielding_error = rp.fielder_5 THEN 5
                        WHEN rp.fielding_error = rp.fielder_6 THEN 6
                        WHEN rp.fielding_error = rp.fielder_7 THEN 7
                        WHEN rp.fielding_error = rp.fielder_8 THEN 8
                        WHEN rp.fielding_error = rp.fielder_9 THEN 9
                        ELSE NULL
                    END::SMALLINT                   AS fielding_error_position,
                    -- Result encoding
                    CASE rp.events
                        WHEN 'single'    THEN 1
                        WHEN 'double'    THEN 2
                        WHEN 'triple'    THEN 3
                        WHEN 'home_run'  THEN 4
                        ELSE 0
                    END::SMALLINT                   AS result_hits,
                    rp.outs_on_pitch::SMALLINT      AS result_outs,
                    rp.runs_on_pitch::SMALLINT      AS result_runs,
                    {recency_expr} AS recency_weight
                FROM sim.pitch_pool pp
                JOIN pg.raw.pitches rp
                    ON rp.game_pk       = pp.game_pk
                   AND rp.at_bat_number = pp.at_bat_number
                   AND rp.pitch_number  = pp.pitch_number
                WHERE pp.outcome_type = 'in_play'
                  AND pp.season IN ({season_list})
            )
            SELECT * FROM bip
        """)
        log.info("  sim.outcome_pool done.")
        _record_pool_build(self._conn, "outcome_pool", seasons, ref_season)

    # ------------------------------------------------------------------
    # 10. sim.stolen_base_pool
    # ------------------------------------------------------------------

    def _build_stolen_base_pool(self, seasons: list[int], incremental: bool = False) -> None:
        """
        Builds sim.stolen_base_pool from all SB attempts in raw.pitches.
        Denormalizes runner/catcher/pitcher metrics from the derived tables
        so the simulation engine can score similarity without joins at runtime.
        Adds recency_weight (SIM-076). When ``incremental`` (SIM-095), only
        seasons whose source changed rebuild.
        """
        ref_season = _canonical_ref_season(self._conn, seasons)
        if incremental:
            seasons = _seasons_needing_rebuild(self._conn, "stolen_base_pool", seasons)
            if not seasons:
                log.info("Building sim.stolen_base_pool … all seasons fresh; skipped.")
                return
        log.info("Building sim.stolen_base_pool … (seasons=%s)", seasons)
        season_list = ", ".join(str(s) for s in seasons)
        recency_expr = _recency_weight_sql("s.season", ref_season)

        self._conn.execute(f"DELETE FROM sim.stolen_base_pool WHERE season IN ({season_list})")

        self._conn.execute(f"""
            INSERT INTO sim.stolen_base_pool

            WITH sb_pitches AS (
                SELECT
                    -- Use a surrogate pitch_id matching sim.pitch_pool
                    pp.pitch_id,
                    rp.game_pk,
                    rp.at_bat_number,
                    rp.pitch_number,
                    rp.game_date,
                    rp.season,
                    rp.fielder_2                AS catcher_id,
                    rp.pitcher,
                    rp.inning,
                    rp.outs,
                    (CASE WHEN rp.on_1b IS NOT NULL THEN 1 ELSE 0 END
                     + CASE WHEN rp.on_2b IS NOT NULL THEN 2 ELSE 0 END
                     + CASE WHEN rp.on_3b IS NOT NULL THEN 4 ELSE 0 END)::SMALLINT
                                                AS runners_state,
                    GREATEST(-5, LEAST(5, rp.bat_score - rp.fld_score))::SMALLINT
                                                AS score_diff,
                    rp.balls                    AS count_balls,
                    rp.strikes                  AS count_strikes,
                    rp.release_speed            AS velo,
                    rp.break_vertical_induced   AS ivb,
                    -- Determine which base was attempted and the runner ID
                    CASE
                        WHEN rp.sb_attempt_home = TRUE AND rp.on_3b IS NOT NULL THEN rp.on_3b
                        WHEN rp.sb_attempt_3b   = TRUE AND rp.on_2b IS NOT NULL THEN rp.on_2b
                        WHEN rp.sb_attempt_2b   = TRUE AND rp.on_1b IS NOT NULL THEN rp.on_1b
                    END                         AS runner_id,
                    CASE
                        WHEN rp.sb_attempt_home = TRUE THEN 'home'
                        WHEN rp.sb_attempt_3b   = TRUE THEN '3B'
                        WHEN rp.sb_attempt_2b   = TRUE THEN '2B'
                    END                         AS base_attempted,
                    CASE
                        WHEN rp.sb_attempt_home = TRUE THEN rp.sb_success_home
                        WHEN rp.sb_attempt_3b   = TRUE THEN rp.sb_success_3b
                        WHEN rp.sb_attempt_2b   = TRUE THEN rp.sb_success_2b
                    END                         AS success
                FROM pg.raw.pitches rp
                JOIN sim.pitch_pool pp
                    ON pp.game_pk = rp.game_pk
                   AND pp.at_bat_number = rp.at_bat_number
                   AND pp.pitch_number  = rp.pitch_number
                WHERE rp.data_quality_flag = FALSE
                  AND rp.season IN ({season_list})
                  AND (rp.sb_attempt_2b = TRUE OR rp.sb_attempt_3b = TRUE
                       OR rp.sb_attempt_home = TRUE)
            )
            SELECT
                s.pitch_id,
                s.game_pk,
                s.at_bat_number,
                s.pitch_number,
                s.game_date,
                s.season,
                s.runner_id,
                s.pitcher,
                s.catcher_id,
                s.base_attempted,
                s.inning,
                s.outs,
                s.runners_state,
                s.score_diff,
                s.count_balls,
                s.count_strikes,
                s.velo,
                s.ivb,
                -- Denormalized runner metrics (most recent season available)
                ss.sprint_speed             AS runner_sprint_speed,
                brm.sb_success_rate         AS runner_sb_success_rate,
                -- Denormalized catcher metrics
                csm.pop_time_mean           AS catcher_pop_time_mean,
                csm.arm_strength_mean       AS catcher_arm_strength,
                -- Denormalized pitcher-allowing-SB metric
                -- Proxy: SB allowed rate from catcher_season_metrics on pitcher
                -- (pitcher's own cs_rate as opposition catcher doesn't apply here;
                -- real pitcher-SB-allowed rate requires Phase 2 Step 2.7)
                NULL::FLOAT                 AS pitcher_sb_allowed_rate,
                s.success,
                {recency_expr} AS recency_weight
            FROM sb_pitches s
            LEFT JOIN derived.baserunner_season_metrics brm
                ON brm.player_id = s.runner_id AND brm.season = s.season
            LEFT JOIN derived.catcher_season_metrics csm
                ON csm.player_id = s.catcher_id AND csm.season = s.season
            LEFT JOIN pg.raw.sprint_speed ss
                ON ss.player_id = s.runner_id AND ss.season = s.season
            WHERE s.runner_id IS NOT NULL
              AND s.base_attempted IS NOT NULL
        """)
        log.info("  sim.stolen_base_pool done.")
        _record_pool_build(self._conn, "stolen_base_pool", seasons, ref_season)

    def _build_at_bat_situations(self, seasons: list[int]) -> None:
        """
        SIM-408: materialize derived.at_bat_situations — one row per plate
        appearance capturing the pre-PA game state the Step 2.9 Situation KDTree
        engine indexes (similarity/engines/situation_similarity.py).  The engine
        SELECTed this table but the computor never built it, so the situation
        engine indexed 0 rows and normalized a degenerate empty matrix; this
        closes that gap.

        One row per (game_pk, at_bat_number), taken at the FIRST pitch of the PA
        so outs / base-state / score reflect the situation the batter came up to.
        ``leverage_index`` replicates :func:`compute_leverage_index` as a SQL
        expression (inning × score-diff × runners × outs factors).  Idempotent
        via INSERT OR REPLACE on the (game_pk, at_bat_number) PK.
        """
        log.info("Building derived.at_bat_situations …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.at_bat_situations (
                play_id, game_pk, season, venue_id, at_bat_number,
                inning, top_or_bottom, outs_when_up,
                on_1b, on_2b, on_3b, home_score, away_score,
                leverage_index, pitcher_pitch_count, batter_pa_count
            )
            WITH clean AS (
                SELECT
                    game_pk, at_bat_number, pitch_number, season, venue_id,
                    inning, inning_topbot, outs,
                    on_1b, on_2b, on_3b,
                    home_score, away_score, bat_score, fld_score,
                    batter,
                    -- pitches thrown by this pitcher in this game up to & incl. this pitch
                    COUNT(*) OVER (
                        PARTITION BY game_pk, pitcher
                        ORDER BY at_bat_number, pitch_number
                        ROWS UNBOUNDED PRECEDING
                    ) AS pitcher_pitches_thru
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
            ),
            pa AS (
                SELECT
                    *,
                    -- batter's PA sequence number in the game (ties share a rank,
                    -- so every pitch of the same PA gets the same value)
                    DENSE_RANK() OVER (
                        PARTITION BY game_pk, batter ORDER BY at_bat_number
                    ) AS batter_pa_seq
                FROM clean
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                ) = 1
            )
            SELECT
                CAST(game_pk AS VARCHAR) || '-' || CAST(at_bat_number AS VARCHAR) AS play_id,
                game_pk,
                season,
                venue_id,
                at_bat_number,
                inning,
                CASE WHEN inning_topbot = 'Bot' THEN 1 ELSE 0 END                 AS top_or_bottom,
                outs                                                              AS outs_when_up,
                CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END                     AS on_1b,
                CASE WHEN on_2b IS NOT NULL THEN 1 ELSE 0 END                     AS on_2b,
                CASE WHEN on_3b IS NOT NULL THEN 1 ELSE 0 END                     AS on_3b,
                home_score,
                away_score,
                -- compute_leverage_index(inning, outs, runners_state, bat-fld) in SQL
                (CASE WHEN inning <= 3 THEN 0.6 WHEN inning <= 6 THEN 0.9
                      WHEN inning <= 8 THEN 1.3 ELSE 1.5 END)
                  * GREATEST(0.3, 1.5 - LEAST(ABS(bat_score - fld_score), 5) * 0.24)
                  * (1.0 + ((CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)
                          + (CASE WHEN on_2b IS NOT NULL THEN 1 ELSE 0 END)
                          + (CASE WHEN on_3b IS NOT NULL THEN 1 ELSE 0 END)) * 0.15)
                  * (CASE WHEN outs < 2 THEN 1.0 ELSE 1.1 END)                    AS leverage_index,
                GREATEST(pitcher_pitches_thru - 1, 0)                             AS pitcher_pitch_count,
                batter_pa_seq                                                     AS batter_pa_count
            FROM pa
        """)
        log.info("  derived.at_bat_situations done.")


class LeagueAverageProfiles:
    """
    Computes and caches league-average profiles for each entity type.
    Used by the similarity engines as the fallback profile for players
    with below_minimum_sample=TRUE.

    Call compute(seasons) once after PlayerProfileComputor.run() completes.
    Results are stored in derived.league_averages (created here if absent).
    """

    LEAGUE_AVG_DDL = """
        CREATE TABLE IF NOT EXISTS derived.league_averages (
            entity_type  VARCHAR(20)  NOT NULL,  -- 'pitcher', 'batter', 'fielder_IF', etc.
            season       SMALLINT     NOT NULL,
            profile_json JSON         NOT NULL,
            updated_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (entity_type, season)
        );
        COMMENT ON TABLE derived.league_averages IS
            'League-average profiles per entity type per season. '
            'Similarity engines fall back to these when below_minimum_sample=TRUE.';
    """

    def __init__(self, duckdb_path: str) -> None:
        self._path = duckdb_path

    def compute(self, seasons: list[int]) -> None:
        conn = duckdb.connect(self._path)
        try:
            conn.execute(self.LEAGUE_AVG_DDL)
            season_list = ", ".join(str(s) for s in seasons)

            # Pitcher average (used for GMM fallback)
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'pitcher' AS entity_type, season,
                    JSON_OBJECT(
                        'bb_rate',          AVG(bb_rate),
                        'k_rate',           AVG(k_rate),
                        'csw_rate',         AVG(csw_rate),
                        'zone_rate',        AVG(zone_rate),
                        'chase_rate',       AVG(chase_rate),
                        'era',              AVG(era),
                        'fip',              AVG(fip),
                        'ground_ball_rate', AVG(ground_ball_rate),
                        'fly_ball_rate',    AVG(fly_ball_rate)
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.pitcher_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            # Batter average
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'batter' AS entity_type, season,
                    JSON_OBJECT(
                        'first_pitch_take_rate', AVG(first_pitch_take_rate),
                        'o_swing_rate',          AVG(o_swing_rate),
                        'z_swing_rate',          AVG(z_swing_rate),
                        'whiff_rate',            AVG(whiff_rate),
                        'contact_rate',          AVG(contact_rate),
                        'walk_rate',             AVG(walk_rate),
                        'k_rate',                AVG(k_rate),
                        'avg_exit_velo',         AVG(avg_exit_velo),
                        'hard_hit_rate',         AVG(hard_hit_rate)
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.batter_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            # Fielder average — one row per position so shrinkage falls
            # back to position-specific league averages (a 1B should not
            # be compared to an OF league average).
            for position in ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"):
                conn.execute(f"""
                    INSERT OR REPLACE INTO derived.league_averages
                    SELECT
                        'fielder_{position}' AS entity_type, season,
                        JSON_OBJECT(
                            'outs_above_average', AVG(outs_above_average),
                            'error_rate',         AVG(error_rate),
                            'arm_hold_rate',      AVG(arm_hold_rate),
                            'dp_run_value',       AVG(dp_run_value)
                        ) AS profile_json,
                        CURRENT_TIMESTAMP AS updated_at
                    FROM derived.fielder_season_metrics
                    WHERE position = '{position}'
                      AND season IN ({season_list})
                      AND below_minimum_sample = FALSE
                    GROUP BY season
                """)

            # Baserunner average
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'baserunner' AS entity_type, season,
                    JSON_OBJECT(
                        'sprint_speed',            AVG(sprint_speed),
                        'extra_base_attempt_rate', AVG(extra_base_attempt_rate),
                        'extra_base_success_rate', AVG(extra_base_success_rate),
                        'sb_success_rate',         AVG(sb_success_rate)
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.baserunner_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            # Catcher average
            conn.execute(f"""
                INSERT OR REPLACE INTO derived.league_averages
                SELECT
                    'catcher' AS entity_type, season,
                    JSON_OBJECT(
                        'framing_runs',  AVG(framing_runs),
                        'blocking_runs', AVG(blocking_runs),
                        'cs_rate',       AVG(cs_rate)
                    ) AS profile_json,
                    CURRENT_TIMESTAMP AS updated_at
                FROM derived.catcher_season_metrics
                WHERE season IN ({season_list})
                  AND below_minimum_sample = FALSE
                GROUP BY season
            """)

            log.info("League averages computed for seasons: %s", seasons)
        finally:
            conn.close()


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    import argparse
    import os
    import sys
    from datetime import datetime

    parser = argparse.ArgumentParser(
        description=(
            "Compute player profile + defensive metrics from Postgres and "
            "write them into the DuckDB analytical store that the similarity "
            "engines read from."
        ),
    )
    parser.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=None,
        help="One or more seasons to (re)compute. Defaults to current year.",
    )
    parser.add_argument(
        "--full-rebuild",
        action="store_true",
        help="Delete and rewrite all derived metrics for the requested seasons.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("BASEBALL_DB_DSN"),
        help="Postgres DSN. Defaults to BASEBALL_DB_DSN env var.",
    )
    parser.add_argument(
        "--duckdb-path",
        default=os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb"),
        help="Path to the DuckDB output file.",
    )
    parser.add_argument(
        "--skip-league-averages",
        action="store_true",
        help="Skip the LeagueAverageProfiles.compute() pass.",
    )
    args = parser.parse_args()

    if not args.dsn:
        sys.stderr.write("ERROR: no Postgres DSN. Set BASEBALL_DB_DSN or pass --dsn.\n")
        sys.exit(2)

    log.info(
        "Starting player profile compute - dsn=%s duckdb=%s seasons=%s full_rebuild=%s",
        args.dsn.split("@", 1)[-1] if "@" in args.dsn else args.dsn,
        args.duckdb_path,
        args.seasons,
        args.full_rebuild,
    )

    computor = PlayerProfileComputor(
        pg_dsn=args.dsn,
        duckdb_path=args.duckdb_path,
    )
    computor.run(seasons=args.seasons, full_rebuild=args.full_rebuild)
    log.info("Player profile compute complete.")

    if not args.skip_league_averages:
        log.info("Computing league averages ...")
        LeagueAverageProfiles(args.duckdb_path).compute(args.seasons or [datetime.today().year])
        log.info("League averages complete.")
