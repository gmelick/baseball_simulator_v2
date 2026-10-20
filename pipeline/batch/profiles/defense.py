"""The defensive constants and the field-geometry helpers the fielder and
catcher metrics use.
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
