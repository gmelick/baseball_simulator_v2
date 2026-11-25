"""The defensive constants and the field-geometry helpers that the fielder and catcher metrics use.

Field positions are in Statcast's hit coordinates (``hc_x``, ``hc_y``; ``hc_y`` grows toward
home plate). ``HC_SCALE`` converts them to feet.
"""

from __future__ import annotations

import logging
import math

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from simulation.constants import DEFENSIVE_RUN_VALUES

log = logging.getLogger("player_profile_computor")


# ---------------------------------------------------------------------------
# The defensive constants
# ---------------------------------------------------------------------------

HC_SCALE = 2.48  # hc units per foot
SECOND_BASE = (125.42, 108.27)
FIRST_BASE = (162.5, 153.5)

# The average fielder spot per position, keyed by the batter's side (``stand``).
AVG_FIELDER_POS = {
    "SS": {"R": (112.0, 152.0), "L": (118.0, 149.0)},
    "2B": {"R": (141.0, 152.0), "L": (136.0, 149.0)},
    "3B": {"R": (96.0, 165.0), "L": (90.0, 168.0)},
    "1B": {"R": (157.0, 165.0), "L": (162.0, 162.0)},
    "LF": {"R": (80.0, 115.0), "L": (75.0, 118.0)},
    "CF": {"R": (125.0, 80.0), "L": (125.0, 82.0)},
    "RF": {"R": (170.0, 115.0), "L": (175.0, 118.0)},
}

# The average throw speed per position, in mph.
AVG_THROW_VELO = {
    "SS": 85.0,
    "2B": 75.0,
    "3B": 83.0,
    "1B": 65.0,
    "LF": 82.0,
    "CF": 84.0,
    "RF": 86.0,
}

INFIELD_EXCHANGE_TIME = 0.80  # seconds from the catch to the throw
# The defensive run values, from simulation.constants: infield 0.75,
# outfield 0.90, block 0.25, strike 0.125.
RUNS_PER_OAA_INFIELD = DEFENSIVE_RUN_VALUES["runs_per_oaa_infield"]
RUNS_PER_OAA_OUTFIELD = DEFENSIVE_RUN_VALUES["runs_per_oaa_outfield"]
RUNS_PER_BLOCK_SAVED = DEFENSIVE_RUN_VALUES["runs_per_block_saved"]
RUNS_PER_STRIKE_ABOVE_AVG = DEFENSIVE_RUN_VALUES["runs_per_strike_above_avg"]
ZONE_HALF_WIDTH = 0.833  # feet, from the middle of the plate (plate_x units)
OF_GOING_BACK_PENALTY_FPS = 1.0
IF_AWAY_FROM_BAG_PENALTY_S = 0.20
GRAVITY_FPS2 = 32.174
DEFAULT_SPRINT_SPEED_FPS = 27.0


# ---------------------------------------------------------------------------
# The field-geometry helpers
# ---------------------------------------------------------------------------


def _hc_to_feet(hc_dist: float) -> float:
    """Convert a distance in hit-coordinate units to feet."""
    return hc_dist / HC_SCALE


def _euclidean_hc(x1: float, y1: float, x2: float, y2: float) -> float:
    """Return the straight-line distance between two points, in hit-coordinate units."""
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def _throw_time(distance_ft: float, velo_mph: float) -> float:
    """Return the seconds a throw takes over ``distance_ft``; 999.0 for a speed of 0 or less."""
    if velo_mph <= 0:
        return 999.0
    velo_fps = velo_mph * 1.467
    return distance_ft / velo_fps


def _estimate_hang_time(launch_speed: float, launch_angle: float) -> float | None:
    """Estimate a batted ball's hang time in seconds, at least 0.5; None for an invalid input.

    ``launch_speed`` is in mph, ``launch_angle`` in degrees. The model is
    projectile motion with an empirical drag correction, calibrated against
    Alan Nathan's trajectory calculator for MLB conditions.
    """
    if launch_speed is None or launch_angle is None:
        return None
    if launch_speed <= 0 or launch_angle < -30 or launch_angle > 90:
        return None

    v0 = launch_speed * 1.467  # mph -> ft/s
    theta = math.radians(launch_angle)
    vz = v0 * math.sin(theta)

    # The contact height, in feet.
    h0 = 3.0
    # The catch height: about 5 ft for a fly ball, 0 for a ground ball.
    h_catch = 5.0 if launch_angle > 15 else 0.0

    # Drag cuts a fly ball's hang time about 12% against a vacuum, less for a
    # line drive and more for a high popup.
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
    """Estimate the seconds a ground ball takes to travel ``distance_ft``, at least 0.1.

    The model slows the ball by the friction of an infield's grass. Return
    None when the ball stops short of the distance or the input is invalid.
    """
    if launch_speed is None or launch_speed <= 0 or distance_ft <= 0:
        return None

    v0 = launch_speed * 1.467  # mph -> ft/s
    v_ground = v0 * math.cos(math.radians(abs(launch_angle or 0)))

    decel = 12.0  # ft/s², the friction of the grass
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
    """Classify the direction a fielder moves from his spot to the ball.

    Return ``(category, going_back)``. ``category`` is one of 'glove_side',
    'arm_side', 'charging' or 'deep'. ``going_back`` is True when an
    outfielder moves away from home plate, or an infielder ranges deep.
    """
    dx = ball_x - fielder_x
    dy = ball_y - fielder_y

    if position in ("LF", "CF", "RF"):
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


def _fit_logistic_model(X: np.ndarray, y: np.ndarray) -> LogisticRegression | None:
    """Fit a logistic regression on standardized features; return the model, or None.

    None means the fit failed or the sample is too small: under 100 rows, or
    under 5 rows of either class. The fitted scaler rides on the model as
    ``_scaler``, for ``_predict_proba``.
    """
    if len(X) < 100 or y.sum() < 5 or (1 - y).sum() < 5:
        log.warning("  Insufficient data for logistic fit: n=%d, pos=%d", len(X), int(y.sum()))
        return None

    try:
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        model = LogisticRegression(max_iter=1000, solver="lbfgs")
        model.fit(X_scaled, y)
        model._scaler = scaler
        return model
    except Exception as e:
        log.warning("  Logistic fit failed: %s", e)
        return None


def _predict_proba(model: LogisticRegression, X: np.ndarray) -> np.ndarray:
    """Return the positive-class probabilities of a model from ``_fit_logistic_model``."""
    X_scaled = model._scaler.transform(X)
    return model.predict_proba(X_scaled)[:, 1]


def _quick_auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Return the AUC (area under the ROC curve) of ``y_score``, or 0.0 when it fails.

    The AUC fails, for example, when ``y_true`` holds one class only.
    """
    try:
        from sklearn.metrics import roc_auc_score

        return roc_auc_score(y_true, y_score)
    except Exception:
        return 0.0
