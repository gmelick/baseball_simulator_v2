"""The pitcher similarity engine: how alike two pitcher-seasons are.

The engine scores two pitcher-seasons of the same throwing hand from 0 to 1
(1 = identical). The score is a weighted sum of two sub-scores:

* The arsenal (weight 0.65): how alike the two pitch mixes are. Each
  pitcher-season is a GMM (a Gaussian mixture model: one Gaussian per pitch
  cluster, over velocity, movement, spin and the release point). The engine
  measures the W₂ distance (the Wasserstein-2 distance: the least cost to move
  one mixture onto the other) and scores it as exp(-W₂ / ARSENAL_SCALE).
* The command (weight 0.35): an RBF kernel (a Gaussian kernel,
  score = exp(-distance² / 2σ²)) over seven plate-discipline rates.

Why W₂. W₂ is a true metric: it is symmetric and obeys the triangle
inequality, and KL divergence is neither. It compares mixtures with different
numbers of components. Between two single Gaussians it has a closed form (the
Bures-Wasserstein distance) that reads the full covariance, so a pitch's
velocity-spin correlation counts. The POT library (Python Optimal Transport)
solves the transport between the components.

Why the linear form exp(-W₂ / ARSENAL_SCALE). The simulator weights every
pitch in the pool by similarity, not only the top matches. The squared form
exp(-γ·W₂²) crushes every far pitcher to about 0; the linear form keeps the
tail apart. The scale puts the median pair at 0.50: 2.84 / ln 2 = 4.10
(CLAUDE.md §10).

The handedness gate. A left-hander never matches a right-hander: the release
geometry and the batter's view differ. Each hand is its own partition. Two
seasons of one pitcher do match; only the query's own (pitcher_id, season)
is left out of the results.

The adjustments. EB (empirical-Bayes shrinkage) pulls each command vector
toward its season's league average, with weight α = N / (N + EB_N_PRIOR) on
the pitcher's own N pitches. The composite is multiplied by
√min(α_query, α_candidate), a confidence discount. A pair without both GMMs
moves the arsenal weight onto the command score.

The performance model. The command scores of a partition are one matrix
operation (under 1 ms for thousands of profiles). The W₂ distance needs two
8 × 8 matrix square roots per pair of components: about 0.5 ms a pitcher
pair, so a first query against 2,400 right-handers takes about 1.2 s.
``ArsenalCache`` (``similarity/engines/arsenal_cache.py``) keeps every
distance once. It fills lazily, or all at once through
``precompute_arsenal_cache``, and then serves a query as one row of a dense
matrix. The simulator does not query this engine: the nightly artifact build
scores every profile once and ships the scores as a matrix.

The data, read-only from DuckDB: ``derived.pitcher_season_metrics`` (the
command rates and the GMM JSON) and ``derived.league_averages`` (the
shrinkage targets).

``ARSENAL_SCALE`` is a module global on purpose. ``apply_calibration``
rewrites it, and ``ArsenalSimilarity.score``, ``HandednessPartition.score_all``
and ``PitcherSimilarityEngine._score_pair`` read it at call time, so the four
stay in this file.

Usage::

    engine = PitcherSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
    engine.build(seasons=[2023, 2024, 2025, 2026])
    every_match = engine.query(pitcher_id=543037, season=2025)
    top_20 = engine.query(pitcher_id=543037, season=2025, n=20)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import duckdb
import numpy as np
from numpy.typing import NDArray
from scipy.linalg import sqrtm

from db.introspect import duck_columns
from similarity.engines.arsenal_cache import ArsenalCache

if TYPE_CHECKING:
    # For annotations only: the engine does not load the calibration package
    # when it imports.
    from similarity.calibration.report import CalibrationReport

# POT (Python Optimal Transport) solves the transport behind the W₂ distance.
# requirements.txt installs it, but the import stays optional, so the module
# imports on a host without POT (the engine-build smoke tests mock DuckDB and
# never reach W₂). Without POT, ArsenalSimilarity.distance uses a greedy
# transport instead.
try:  # pragma: no cover
    import ot

    _POT_AVAILABLE = True
except ImportError as _pot_err:  # pragma: no cover
    ot = None  # type: ignore[assignment]
    _POT_AVAILABLE = False
    _POT_IMPORT_ERROR = _pot_err


def _require_pot() -> None:
    """Raise a RuntimeError when POT is not installed."""
    if not _POT_AVAILABLE:  # pragma: no cover
        raise RuntimeError(
            "POT (Python Optimal Transport) is required for W2 arsenal "
            "similarity but is not installed.  On Python 3.13 it currently "
            "has no published wheel -- install from source "
            "(`pip install POT`) or use Python 3.11 / 3.12.  "
            f"Original ImportError: {_POT_IMPORT_ERROR!r}"
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pitcher_similarity")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# The eight dimensions of the arsenal GMM. The list must match GMM_FEATURE_NAMES
# in player_profile_computor, which fits the GMMs.
# Savant's arm angle, active spin and spin-axis deviation are NOT features, by owner
# decision (the arm-angle and spin-shape study, SIM-533) on the measurements in
# docs/audit/2026-09-18-sim533-pitcher-arm-angle-spin-shape-plan.md.
#   * The release point is here already (release_x, release_z, release_ext, per
#     pitch cluster). The model reads it once; a second release sub-score would
#     count it twice.
#   * Arm angle = atan2(release z - shoulder z, |release x - shoulder x|) (r 1.000
#     on Savant's own columns). Our release point reproduces 73% of it, 81% with
#     height. The rest is the shoulder's posture, which the ball's flight does not
#     carry. On 67,941 same-hand pairs of 2024, that rest adds 0.0005 R² to the
#     score's fit of nine outcome rates.
#   * Active spin follows movement × velocity / spin rate (r 0.94). The measured
#     axis IS spin_axis (r 0.992). The inferred axis is a formula on ivb and hb
#     (r 0.987). Their difference rebuilds at r 0.80. None adds more than 0.009 R²
#     on outcomes the score does not read.
#   * One signal points the other way: given the score, a larger arm-angle gap
#     goes with a SMALLER outcome gap (coefficient -0.17). The W₂ may weight its
#     three release dimensions too heavily. A weight per dimension is a candidate
#     for the comprehensive sweep of the tunable parameters (SIM-548), not a feature.
#   * Savant's per-pitch arm angle is stored in raw.savant_pitch_tracking and
#     stays unread.
# tests/unit/test_arm_angle_decision.py guards the decision;
# scripts/probes/arm_angle_probe.py re-runs the numbers.
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
GMM_FEATURE_DIM = len(GMM_FEATURE_NAMES)

# The plate-discipline rates of the command sub-score, in vector order.
COMMAND_FEATURES = [
    "bb_rate",
    "k_rate",
    "csw_rate",
    "zone_take_rate",
    "chase_rate",
    "zone_rate",
    "whiff_rate",
]

# The sub-score weights. The project specification gives the arsenal the most.
WEIGHT_ARSENAL = 0.65
WEIGHT_COMMAND = 0.35
_TOTAL_WEIGHT = WEIGHT_ARSENAL + WEIGHT_COMMAND
assert abs(_TOTAL_WEIGHT - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# The bandwidth σ of the command kernel: gamma = 1 / (2σ²). The value puts the
# median pitcher pair's command score at about 0.4 to 0.6, where the score
# still tells pairs apart.
RBF_SIGMA_COMMAND = 1.0453

# The arsenal score: score = exp(-W₂ / ARSENAL_SCALE). The project locks this
# linear form (CLAUDE.md §10); the module docstring says why.
#
# The scale puts the median pair at a target score:
#   ARSENAL_SCALE = -median_W₂ / ln(target) = 2.84 / 0.693 = 4.10 for a 0.50 target.
# The calibrator fits the squared form's gamma on the same anchor.
# arsenal_scale_from_gamma converts that gamma into this scale, and
# PitcherSimilarityEngine.apply_calibration rewrites this global with it.
#
# To refit from real data (the calibrator fits the arsenal only when it gets
# the distances):
#   from similarity.similarity_calibration import SimilarityCalibrator
#   engine.precompute_arsenal_cache()
#   dists = engine._arsenal_cache.finite_distances()
#   report = SimilarityCalibrator(duckdb_path).calibrate_from_population(
#       seasons, arsenal_w2_distances=dists)
#   engine.apply_calibration(report)
ARSENAL_SCALE = 4.10

# The scale when no CalibrationReport applies. apply_calibration rewrites
# ARSENAL_SCALE; this constant keeps the 4.10 fallback (median pair at 0.50).
DEFAULT_ARSENAL_SCALE = 4.10

# The EB prior: the shrinkage weight is α = N / (N + EB_N_PRIOR) for N pitches.
# At N = EB_N_PRIOR, α = 0.5. At the 200-pitch minimum, α = 200 / 205 ≈ 0.98.
EB_N_PRIOR = 5

# A GMM component with fewer assigned pitches merges into its nearest other
# component (enforce_min_cluster_size).
MIN_CLUSTER_SIZE = 30

# The minimum sample of a profile, as in player_profile_computor.py. The engine
# loads only the profiles that the computor marks at or above it.
MIN_PITCHER_PITCHES = 200


def arsenal_scale_from_gamma(gamma: float, median_w2: float) -> float:
    """Convert the calibrator's squared-form ``arsenal_gamma`` into the
    engine's linear ``ARSENAL_SCALE``.

    The calibrator solves its anchor in the squared form::

        target = exp(-gamma * median_w2 ** 2)   =>   gamma = -ln(target)/median_w2²

    The engine uses the linear form::

        target = exp(-median_w2 / scale)        =>   scale  = -median_w2/ln(target)

    Both pin the SAME median W₂ to the SAME target score, so
    ``-ln(target) = gamma * median_w2² = median_w2 / scale``, and::

        scale = 1 / (gamma * median_w2)

    Parameters
    ----------
    gamma : float
        ``CalibrationReport.arsenal_gamma``, the squared form's coefficient.
    median_w2 : float
        The median finite W₂ distance of the calibration anchor.

    Returns
    -------
    The linear ``ARSENAL_SCALE``, or ``DEFAULT_ARSENAL_SCALE`` when an input is
    missing, not finite or not positive.
    """
    if (
        gamma is None
        or median_w2 is None
        or not np.isfinite(gamma)
        or not np.isfinite(median_w2)
        or gamma <= 0.0
        or median_w2 <= 0.0
    ):
        return DEFAULT_ARSENAL_SCALE
    return float(1.0 / (gamma * median_w2))


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(frozen=True, slots=True)
class GMMComponent:
    """One component of a pitcher's GMM: one pitch cluster."""

    component_id: int
    weight: float  # the mixing proportion; a GMM's weights sum to 1.0
    mean: NDArray[np.float64]  # shape (8,): original units, z-scores after standardize_gmm
    covariance: NDArray[np.float64]  # shape (8, 8): the full covariance, in the mean's units
    n_pitches: int


@dataclass(slots=True)
class GMMModel:
    """The GMM (Gaussian mixture model) of one pitcher-season."""

    n_components: int
    feature_names: list[str]
    # The standardization of the fit (the pitcher's own pitches) on load; the
    # population's after standardize_gmm.
    feature_means: NDArray[np.float64]
    feature_stds: NDArray[np.float64]
    components: list[GMMComponent]
    bic: float = 0.0

    @classmethod
    def from_json(cls, model_json: dict) -> GMMModel:
        """Build a GMMModel from the JSON in ``pitcher_season_metrics.gmm_model``.

        The profile computor (``player_profile_computor._fit_gmm_for_pitcher``)
        stores each component's ``mean`` in original feature units, but its
        ``covariance`` in the standardized space of the pitcher's own fit::

            cov_std = D⁻¹ · cov_orig · D⁻¹,   D = diag(feature_stds)

        This method restores the covariance to original units::

            cov_orig = D · cov_std · D

        The mean and the covariance then share one scale, and
        ``standardize_gmm`` applies one population standardization to both.
        Without the restore, the covariance is standardized twice and the mean
        once, and the covariance term of the W₂ distance is wrong.
        """
        feature_means = np.array(
            model_json.get("feature_means", [0.0] * GMM_FEATURE_DIM),
            dtype=np.float64,
        )
        feature_stds = np.array(
            model_json.get("feature_stds", [1.0] * GMM_FEATURE_DIM),
            dtype=np.float64,
        )
        d_feat = np.diag(feature_stds)

        components = []
        for c in model_json.get("components", []):
            cov_std = np.array(c["covariance"], dtype=np.float64)
            cov_orig = d_feat @ cov_std @ d_feat
            components.append(
                GMMComponent(
                    component_id=c["component_id"],
                    weight=c["weight"],
                    mean=np.array(c["mean"], dtype=np.float64),
                    covariance=cov_orig,
                    n_pitches=c.get("n_pitches", 0),
                )
            )
        diag = model_json.get("fit_diagnostics", {})
        return cls(
            n_components=model_json.get("n_components", 0),
            feature_names=model_json.get("feature_names", GMM_FEATURE_NAMES),
            feature_means=feature_means,
            feature_stds=feature_stds,
            components=components,
            bic=diag.get("bic", 0.0),
        )


@dataclass(slots=True)
class PitcherProfile:
    """One pitcher-season's profile, as the engine scores it."""

    pitcher_id: int
    season: int
    p_throws: str  # "L" or "R"
    sample_pitches: int

    # The arsenal; None when the pitcher-season has no fitted GMM.
    gmm: GMMModel | None

    # The command rates. build() shrinks them toward the league average, and
    # the FeatureNormalizer z-scores them when the engine scores.
    command_vec: NDArray[np.float64]  # shape (len(COMMAND_FEATURES),)

    # The EB shrinkage weight α (1.0 = all own data, 0.0 = all league average).
    eb_alpha: float = 1.0

    # True when the sample is under the minimum.
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One candidate pitcher-season in a query's results."""

    pitcher_id: int
    season: int
    p_throws: str
    score: float  # composite [0, 1], 1 = identical
    arsenal_score: float
    command_score: float
    sample_pitches: int


# ============================================================================
# The arsenal sub-score: the W₂ distance by optimal transport
# ============================================================================


class ArsenalSimilarity:
    """The arsenal sub-score: the W₂ distance between two pitchers' GMMs.

    The W₂ distance solves an optimal transport between the components of the
    two mixtures. The cost to move component i onto component j is their
    squared Bures-Wasserstein distance (the closed-form W₂² between two
    Gaussians), which reads the full covariance.

    Without the POT library, ``distance`` uses a greedy transport, which
    matches the cheapest component pairs first.
    """

    @staticmethod
    def bures_wasserstein_sq(
        m1: NDArray,
        C1: NDArray,
        m2: NDArray,
        C2: NDArray,
    ) -> float:
        """The squared Bures-Wasserstein distance between N(m1, C1) and N(m2, C2)::

            W₂² = ||m1 - m2||² + Tr(C1 + C2 - 2 (C1^½ C2 C1^½)^½)

        This is the closed-form W₂² between two multivariate Gaussians, with
        the matrix square root of ``scipy.linalg.sqrtm``. Rounding can give
        ``sqrtm`` a tiny imaginary part: the method keeps the real part, and
        clamps the result at 0.
        """
        mean_diff_sq = np.sum((m1 - m2) ** 2)

        C1_sqrt = sqrtm(C1)
        if np.iscomplexobj(C1_sqrt):
            C1_sqrt = C1_sqrt.real

        inner = C1_sqrt @ C2 @ C1_sqrt

        inner_sqrt = sqrtm(inner)
        if np.iscomplexobj(inner_sqrt):
            inner_sqrt = inner_sqrt.real

        trace_term = np.trace(C1) + np.trace(C2) - 2.0 * np.trace(inner_sqrt)

        # Rounding can push a near-zero distance below 0.
        return float(max(mean_diff_sq + trace_term, 0.0))

    @classmethod
    def _build_cost_matrix(
        cls,
        gmm_a: GMMModel,
        gmm_b: GMMModel,
    ) -> NDArray[np.float64]:
        """The |K_a| × |K_b| cost matrix: entry (i, j) is the squared
        Bures-Wasserstein distance between component i of gmm_a and
        component j of gmm_b."""
        n_a = len(gmm_a.components)
        n_b = len(gmm_b.components)
        cost = np.zeros((n_a, n_b), dtype=np.float64)

        for i, ca in enumerate(gmm_a.components):
            for j, cb in enumerate(gmm_b.components):
                cost[i, j] = cls.bures_wasserstein_sq(
                    ca.mean,
                    ca.covariance,
                    cb.mean,
                    cb.covariance,
                )
        return cost

    @classmethod
    def distance(cls, gmm_a: GMMModel, gmm_b: GMMModel) -> float:
        """The W₂ distance between two GMMs.

        The method solves the optimal transport problem::

            min_T Σ_ij T_ij · C_ij   subject to   T · 1 = weights_a,   Tᵀ · 1 = weights_b

        where C_ij is the squared Bures-Wasserstein distance between the
        components. It returns the square root of the least cost (W₂, not
        W₂²), or inf when either GMM has no component.
        """
        if not gmm_a.components or not gmm_b.components:
            return float("inf")

        cost = cls._build_cost_matrix(gmm_a, gmm_b)
        weights_a = np.array([c.weight for c in gmm_a.components], dtype=np.float64)
        weights_b = np.array([c.weight for c in gmm_b.components], dtype=np.float64)

        # The weights should sum to 1 already; normalize them to be safe.
        weights_a /= weights_a.sum()
        weights_b /= weights_b.sum()

        if ot is not None:
            # The exact transport: POT's linear-program solver.
            transport_cost = ot.emd2(weights_a, weights_b, cost)
        else:
            transport_cost = cls._greedy_transport(weights_a, weights_b, cost)

        return float(np.sqrt(max(transport_cost, 0.0)))

    @staticmethod
    def _greedy_transport(
        wa: NDArray,
        wb: NDArray,
        cost: NDArray,
    ) -> float:
        """A greedy transport, for a host without POT.

        The method takes the (i, j) pairs from the cheapest up, and moves as
        much mass as both sides still hold. The result is not optimal, but it
        is close for a few components.
        """
        wa = wa.copy()
        wb = wb.copy()
        total_cost = 0.0

        n_a, n_b = cost.shape
        entries = []
        for i in range(n_a):
            for j in range(n_b):
                entries.append((cost[i, j], i, j))
        entries.sort()

        for c, i, j in entries:
            if wa[i] <= 0 or wb[j] <= 0:
                continue
            transfer = min(wa[i], wb[j])
            total_cost += transfer * c
            wa[i] -= transfer
            wb[j] -= transfer

        return total_cost

    @classmethod
    def score(cls, gmm_a: GMMModel, gmm_b: GMMModel) -> float:
        """The arsenal score in [0, 1]: exp(-W₂ / ARSENAL_SCALE).

        A distance of 0 scores 1.0; an infinite distance scores 0.0. The
        method reads the module global ``ARSENAL_SCALE`` at call time.
        """
        d = cls.distance(gmm_a, gmm_b)
        if not np.isfinite(d):
            return 0.0
        return float(np.exp(-d / ARSENAL_SCALE))


# ============================================================================
# The command sub-score: the RBF kernel
# ============================================================================


class RBFSimilarity:
    """The RBF kernel (a Gaussian kernel) of the command sub-score.

    K(x, y) = exp(-γ · mean_i (x_i - y_i)²), with γ = 1 / (2σ²).

    The mean, not the sum, of the squared gaps keeps the score on one scale
    for any number of features. A NaN gap counts as 0. The caller z-scores the
    vectors first (``FeatureNormalizer``), so each feature counts about equally.
    """

    def __init__(self, sigma: float = 1.0) -> None:
        self.sigma = sigma
        self.gamma = 1.0 / (2.0 * sigma**2)

    def score(self, x: NDArray, y: NDArray) -> float:
        """The kernel score of two vectors, in [0, 1]."""
        diff = x - y
        diff = np.nan_to_num(diff, nan=0.0)
        mean_dist_sq = np.dot(diff, diff) / len(diff)
        return float(np.exp(-self.gamma * mean_dist_sq))

    def score_batch(
        self,
        query: NDArray,
        candidates: NDArray,
    ) -> NDArray[np.float64]:
        """The kernel scores of one query vector against each candidate row,
        shape (n_candidates,)."""
        diff = candidates - query[np.newaxis, :]
        diff = np.nan_to_num(diff, nan=0.0)
        n_features = diff.shape[1]
        mean_dist_sq = np.sum(diff**2, axis=1) / n_features
        return np.exp(-self.gamma * mean_dist_sq)


# ============================================================================
# EB shrinkage
# ============================================================================


class EmpiricalBayesShrinkage:
    """EB (empirical-Bayes) shrinkage: pull a vector toward the league average
    by its sample size.

    The shrinkage weight is α = N / (N + n_prior) for N pitches. A large N
    gives α near 1 (the pitcher's own rates); a small N gives α near 0 (the
    league average). With EB_N_PRIOR = 5, a pitcher at the 200-pitch minimum
    keeps about 98% of his own rates.

    The engine shrinks the command vector only. A GMM has no partial blend
    with another GMM, because their components differ, so it stays as fitted.
    """

    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n_samples: int) -> float:
        """The shrinkage weight α: the share of the pitcher's own data."""
        return n_samples / (n_samples + self.n_prior)

    def shrink(
        self,
        raw_vec: NDArray[np.float64],
        league_avg_vec: NDArray[np.float64],
        n_samples: int,
    ) -> NDArray[np.float64]:
        """Return α · raw_vec + (1 - α) · league_avg_vec. A NaN in raw_vec
        takes the league value."""
        a = self.alpha(n_samples)
        raw_clean = np.where(np.isnan(raw_vec), league_avg_vec, raw_vec)
        return a * raw_clean + (1.0 - a) * league_avg_vec


# ============================================================================
# GMM post-processing: the standardization and the minimum cluster size
# ============================================================================


def standardize_gmm(
    gmm: GMMModel,
    pop_mean: NDArray[np.float64],
    pop_std: NDArray[np.float64],
) -> GMMModel:
    """Move a GMM's component means and covariances from original units to
    z-scores (the gap from the population mean, in population standard
    deviations).

    W₂ reads raw Euclidean gaps between the component means. In original
    units, spin rate (a range of about 1,000 rpm) outweighs velocity (about
    15 mph). In z-scores, each feature counts on one scale.

        z_μ = (μ - pop_mean) / pop_std
        z_Σ = D⁻¹ Σ D⁻¹,   D = diag(pop_std)

    The covariance rule follows from Cov(AX) = A Cov(X) Aᵀ, with A = D⁻¹.

    Parameters
    ----------
    gmm : GMMModel
        A GMM with its means and covariances in original units.
    pop_mean : (D,) array
        The population mean of each feature: the weight-averaged component
        mean over every profile.
    pop_std : (D,) array
        The population standard deviation of each feature. A zero becomes 1.0.

    Returns
    -------
    A new GMMModel in z-scores. The weights and the components stay as they
    are.
    """
    safe_std = pop_std.copy()
    safe_std[safe_std == 0] = 1.0
    D_inv = np.diag(1.0 / safe_std)

    new_components = []
    for c in gmm.components:
        z_mean = (c.mean - pop_mean) / safe_std
        z_cov = D_inv @ c.covariance @ D_inv
        new_components.append(
            GMMComponent(
                component_id=c.component_id,
                weight=c.weight,
                mean=z_mean,
                covariance=z_cov,
                n_pitches=c.n_pitches,
            )
        )

    return GMMModel(
        n_components=gmm.n_components,
        feature_names=gmm.feature_names,
        feature_means=pop_mean,
        feature_stds=safe_std,
        components=new_components,
        bic=gmm.bic,
    )


def enforce_min_cluster_size(gmm: GMMModel) -> GMMModel:
    """
    Merge each GMM component with fewer than MIN_CLUSTER_SIZE pitches into
    its nearest other component.

    A tiny component (say, ten pitches of an experimental pitch) sits in an
    odd spot of the feature space and can dominate the W₂ distance. Each pass
    takes the smallest such component and merges it into the component with
    the nearest mean (the Euclidean distance, in the units the GMM holds;
    ``build`` runs this before the standardization, so original units). That
    component can be small too. The passes stop when no small component is
    left, or one component remains.

    A merge keeps the first two moments: the merged mean and covariance blend
    the two by weight, and the covariance adds the spread between the two
    means. At the end the weights are renormalized to sum to 1, and the
    components are renumbered from 0.

    Returns a new GMMModel.
    """
    if not gmm.components:
        return gmm

    components = list(gmm.components)

    changed = True
    while changed and len(components) > 1:
        changed = False

        small = [c for c in components if c.n_pitches < MIN_CLUSTER_SIZE]
        if not small:
            break

        small.sort(key=lambda c: c.n_pitches)

        for s in small:
            if s not in components:
                continue

            large = [c for c in components if c is not s]
            if not large:
                break

            dists = [np.linalg.norm(s.mean - c.mean) for c in large]
            nearest = large[int(np.argmin(dists))]

            total_w = s.weight + nearest.weight
            total_n = s.n_pitches + nearest.n_pitches
            frac_s = s.weight / total_w if total_w > 0 else 0.5
            frac_n = nearest.weight / total_w if total_w > 0 else 0.5

            merged_mean = frac_s * s.mean + frac_n * nearest.mean
            diff_s = (s.mean - merged_mean).reshape(-1, 1)
            diff_n = (nearest.mean - merged_mean).reshape(-1, 1)
            merged_cov = frac_s * (s.covariance + diff_s @ diff_s.T) + frac_n * (
                nearest.covariance + diff_n @ diff_n.T
            )

            merged = GMMComponent(
                component_id=nearest.component_id,
                weight=total_w,
                mean=merged_mean,
                covariance=merged_cov,
                n_pitches=total_n,
            )

            components = [c for c in components if c is not s and c is not nearest]
            components.append(merged)
            changed = True
            break

    total_w = sum(c.weight for c in components)
    if total_w > 0:
        components = [
            GMMComponent(
                component_id=i,
                weight=c.weight / total_w,
                mean=c.mean,
                covariance=c.covariance,
                n_pitches=c.n_pitches,
            )
            for i, c in enumerate(components)
        ]

    return GMMModel(
        n_components=len(components),
        feature_names=gmm.feature_names,
        feature_means=gmm.feature_means,
        feature_stds=gmm.feature_stds,
        components=components,
        bic=gmm.bic,
    )


# ============================================================================
# The command vector's z-score normalizer
# ============================================================================


@dataclass(slots=True)
class FeatureNormalizer:
    """The z-score normalizer of the command vectors, fit on the loaded profiles.

    It keeps each command feature's population mean and standard deviation. A
    NaN rate becomes the population mean (z-score 0), so it adds nothing to the
    distance. A feature with zero spread gets a standard deviation of 1.
    """

    command_mean: NDArray[np.float64] | None = None
    command_std: NDArray[np.float64] | None = None

    def fit(self, profiles: list[PitcherProfile]) -> None:
        """Fit the mean and the standard deviation on every profile."""
        if not profiles:
            return

        cmd = np.array([p.command_vec for p in profiles], dtype=np.float64)

        self.command_mean = np.nanmean(cmd, axis=0)
        self.command_std = np.nanstd(cmd, axis=0)
        self.command_std[self.command_std == 0] = 1.0

    def normalize_command(self, vec: NDArray) -> NDArray:
        """The z-scores of one command vector; the raw vector before ``fit``."""
        if self.command_mean is None:
            return vec
        normed = (vec - self.command_mean) / self.command_std
        return np.nan_to_num(normed, nan=0.0)


# ============================================================================
# The handedness partition: one throwing hand's profiles
# ============================================================================


class HandednessPartition:
    """Every profile of one throwing hand (L or R), with the z-scored command
    matrix built once.

    A query runs in three steps:
      1. The command scores: one matrix operation (under 1 ms for 3,000 profiles).
      2. The arsenal scores: one row of W₂ distances from the ``ArsenalCache``.
      3. The composite: the weighted sum, times the confidence discount.
    """

    def __init__(self, hand: str) -> None:
        self.hand = hand
        self.profiles: list[PitcherProfile] = []
        self.keys: list[tuple[int, int]] = []  # (pitcher_id, season), parallel to profiles

        # The z-scored command vectors, shape (N, len(COMMAND_FEATURES)).
        self._cmd_matrix: NDArray | None = None

        # The EB weight α of each profile, shape (N,).
        self._eb_alphas: NDArray | None = None

    def build(
        self,
        profiles: list[PitcherProfile],
        normalizer: FeatureNormalizer,
    ) -> None:
        """Fill the partition and build the command matrix and the α array."""
        self.profiles = profiles
        self.keys = [(p.pitcher_id, p.season) for p in profiles]

        if not profiles:
            return

        cmd_rows = []
        alphas = []

        for p in profiles:
            cmd_rows.append(normalizer.normalize_command(p.command_vec))
            alphas.append(p.eb_alpha)

        self._cmd_matrix = np.array(cmd_rows, dtype=np.float64)
        self._eb_alphas = np.array(alphas, dtype=np.float64)

    def score_all(
        self,
        query_profile: PitcherProfile,
        normalizer: FeatureNormalizer,
        arsenal_cache: ArsenalCache,
        command_rbf: RBFSimilarity,
    ) -> list[SimilarityResult]:
        """Score the query profile against EVERY profile in this partition.

        The result holds one SimilarityResult for each profile EXCEPT the
        query's own (pitcher_id, season). The query pitcher's other seasons
        ARE in it.

        The command scores come from one vectorized pass. The arsenal
        distances come from the cache, which computes a missing one.
        """
        if not self.profiles:
            return []

        n = len(self.profiles)
        query_key = (query_profile.pitcher_id, query_profile.season)

        # The command scores, in one pass.
        cmd_q = normalizer.normalize_command(query_profile.command_vec)

        command_scores = command_rbf.score_batch(cmd_q, self._cmd_matrix)

        # The arsenal scores. ``row_distances`` returns the query's whole row of
        # raw W₂ distances (0.0 for the query itself, inf for a pair without
        # both GMMs), and NumPy turns the finite ones into scores.
        query_has_gmm = query_profile.gmm is not None

        w2_row = arsenal_cache.row_distances(query_profile, self.profiles)
        finite_mask = np.isfinite(w2_row)
        arsenal_scores = np.zeros(n, dtype=np.float64)
        arsenal_scores[finite_mask] = np.exp(-w2_row[finite_mask] / ARSENAL_SCALE)
        # The query's own row leaves the results below; its arsenal score is 0.0.
        self_mask = np.array([k == query_key for k in self.keys], dtype=bool)
        arsenal_scores[self_mask] = 0.0

        # The composite scores.
        has_arsenal = np.array(
            [query_has_gmm and p.gmm is not None for p in self.profiles],
            dtype=bool,
        )

        composite = np.zeros(n, dtype=np.float64)

        # Both GMMs present: the full weighted sum.
        mask_full = has_arsenal
        composite[mask_full] = (
            WEIGHT_ARSENAL * arsenal_scores[mask_full] + WEIGHT_COMMAND * command_scores[mask_full]
        )

        # A GMM missing: the arsenal weight moves onto the command score, so the
        # weights still sum to 1. The command score is multiplied by
        # _TOTAL_WEIGHT / WEIGHT_COMMAND (1 / 0.35, about 2.86), and the composite
        # stays on the same [0, 1] scale as a full pair. Without the move, a
        # pair without a GMM tops out at 0.35 and never outranks a weak full match.
        mask_no_arsenal = ~has_arsenal
        command_redistrib = _TOTAL_WEIGHT / WEIGHT_COMMAND
        composite[mask_no_arsenal] = command_redistrib * command_scores[mask_no_arsenal]

        # The confidence discount: √min(α_query, α_candidate).
        query_alpha = query_profile.eb_alpha
        pair_confidence = np.minimum(query_alpha, self._eb_alphas)
        composite *= np.sqrt(pair_confidence)
        composite = np.clip(composite, 0.0, 1.0)

        # The results: every profile but the query's own.
        results = []
        for i in range(n):
            if self.keys[i] == query_key:
                continue
            cand = self.profiles[i]
            results.append(
                SimilarityResult(
                    pitcher_id=cand.pitcher_id,
                    season=cand.season,
                    p_throws=cand.p_throws,
                    score=float(composite[i]),
                    arsenal_score=float(arsenal_scores[i]),
                    command_score=float(command_scores[i]),
                    sample_pitches=cand.sample_pitches,
                )
            )

        return results


# ============================================================================
# The engine
# ============================================================================


class PitcherSimilarityEngine:
    """The pitcher similarity engine: the composite score in [0, 1] of two
    pitcher-seasons, from the arsenal (W₂) and the command (RBF) sub-scores.

    A query scores EVERY pitcher-season of the same hand. Two seasons of one
    pitcher (2025 Scherzer against 2024 Scherzer) match; only the query's own
    (pitcher_id, season) is left out. The module docstring holds the model.

    Usage::

        engine = PitcherSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2023, 2024, 2025, 2026])
        every_match = engine.query(pitcher_id=543037, season=2025)
        top_20 = engine.query(pitcher_id=543037, season=2025, n=20)
        engine.precompute_arsenal_cache(n_workers=4)  # optional: fill every W₂ now
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, int], PitcherProfile] = {}
        self._league_avg_command: dict[int, NDArray] = {}

        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._command_rbf = RBFSimilarity(sigma=RBF_SIGMA_COMMAND)

        # One partition per throwing hand.
        self._partition_l = HandednessPartition("L")
        self._partition_r = HandednessPartition("R")

        # The W₂ distances of both partitions.
        self._arsenal_cache = ArsenalCache()

        # The arsenal scale that apply_calibration last set; DEFAULT_ARSENAL_SCALE
        # until then. The scoring reads the module global ARSENAL_SCALE.
        self._arsenal_scale: float = DEFAULT_ARSENAL_SCALE

        # The cutoff every loaded profile declares (the point-in-time rule,
        # SIM-537). None when the table has no asof_date column (a DuckDB file
        # before migration 0027) or every row's value is NULL.
        self._asof_date = None

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def apply_calibration(
        self,
        report: CalibrationReport,
        median_w2: float | None = None,
    ) -> float:
        """Apply a ``CalibrationReport`` to the arsenal score.

        The ``SimilarityCalibrator`` fits ``report.arsenal_gamma`` (the
        coefficient of the squared form exp(-gamma · W₂²)) and records the
        median W₂ of its anchor. This method converts the two into the linear
        ``ARSENAL_SCALE`` (``arsenal_scale_from_gamma``) and rewrites the
        module global, so ``ArsenalSimilarity.score``,
        ``HandednessPartition.score_all`` and ``_score_pair`` read the new
        scale. The global is shared: the scale reaches every engine in the
        process. A report with no usable gamma sets ``DEFAULT_ARSENAL_SCALE``.

        The method reads only the arsenal fields of the report.

        Parameters
        ----------
        report : CalibrationReport
            Read for ``arsenal_gamma`` and ``arsenal_median_w2``.
        median_w2 : float, optional
            The median finite W₂ of the anchor. Without it, the method takes
            ``report.arsenal_median_w2``, then the median of this engine's
            cache (``finite_distances()``), then the project's 2.84.

        Returns
        -------
        The arsenal scale now in force.
        """
        global ARSENAL_SCALE

        gamma = float(getattr(report, "arsenal_gamma", 0.0) or 0.0)

        # A median of 0.0 (the report's default) means none was fitted: fall back
        # to this engine's cache, then to the project's median.
        if median_w2 is None or median_w2 <= 0.0:
            report_median = getattr(report, "arsenal_median_w2", 0.0) or 0.0
            median_w2 = float(report_median) if report_median > 0.0 else None
        if median_w2 is None or median_w2 <= 0.0:
            try:
                dists = self._arsenal_cache.finite_distances()
                if dists.size:
                    median_w2 = float(np.median(dists))
            except Exception:  # pragma: no cover - defensive
                median_w2 = None
        if median_w2 is None or median_w2 <= 0.0:
            median_w2 = 2.84  # the project's median W₂ (CLAUDE.md §10)

        scale = arsenal_scale_from_gamma(gamma, median_w2)
        self._arsenal_scale = scale
        # Every scoring path reads the module global at call time.
        ARSENAL_SCALE = scale
        log.info(
            "SIM-346: applied CalibrationReport — arsenal_gamma=%.5f, "
            "median_W2=%.4f → ARSENAL_SCALE=%.4f (was default %.4f).",
            gamma,
            median_w2,
            scale,
            DEFAULT_ARSENAL_SCALE,
        )
        return scale

    @property
    def arsenal_scale(self) -> float:
        """The arsenal scale that this engine's ``apply_calibration`` last set.

        The scoring reads the module global ``ARSENAL_SCALE``, which another
        engine's ``apply_calibration`` can change.
        """
        return self._arsenal_scale

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(
        self,
        seasons: list[int] | None = None,
        arsenal_cache_path: str | None = None,
    ) -> None:
        """Load the profiles from DuckDB and prepare them for scoring.

        The steps: load the league averages and the profiles, shrink each
        command vector, merge the small GMM components, standardize the GMMs,
        fit the command normalizer, and build the two hand partitions.

        Parameters
        ----------
        seasons : list[int]
            The seasons to load; None loads every season.
        arsenal_cache_path : str, optional
            A saved arsenal cache. When the file exists, the engine loads its
            W₂ distances and does not compute them again.
        """
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)

        try:
            self._load_league_averages(conn, seasons)
            self._load_profiles(conn, seasons)
        finally:
            conn.close()

        self._apply_shrinkage()

        # Merge the small GMM components (in original units).
        for key, profile in self._profiles.items():
            if profile.gmm is not None:
                self._profiles[key].gmm = enforce_min_cluster_size(profile.gmm)

        # Move every GMM into z-scores. Without it, spin rate (a range of about
        # 1,000) outweighs velocity (about 15) in W₂, and the arsenal scores
        # collapse to near 0.
        self._standardize_arsenals()

        # Fit the command normalizer on every profile.
        all_profiles = list(self._profiles.values())
        self._normalizer.fit(all_profiles)

        # Build one partition per throwing hand.
        profiles_l = [p for p in all_profiles if p.p_throws == "L"]
        profiles_r = [p for p in all_profiles if p.p_throws == "R"]
        self._partition_l.build(profiles_l, self._normalizer)
        self._partition_r.build(profiles_r, self._normalizer)

        if arsenal_cache_path:
            self._arsenal_cache.load(arsenal_cache_path)

        elapsed = time.time() - t0
        log.info(
            "PitcherSimilarityEngine built: %d profiles (%d LHP, %d RHP) in %.2fs.",
            len(self._profiles),
            len(profiles_l),
            len(profiles_r),
            elapsed,
        )

    def precompute_arsenal_cache(
        self,
        n_workers: int = 1,
        save_path: str | None = None,
    ) -> None:
        """Compute every same-hand W₂ distance now, so no query computes one.

        The cache then builds each hand's dense matrix, and a query reads one
        row of it.

        Parameters
        ----------
        n_workers : int
            The number of worker processes; 1 runs in this process.
        save_path : str, optional
            A file to save the cache to after the computation.
        """
        log.info("Precomputing arsenal cache (LHP) …")
        self._arsenal_cache.precompute(self._partition_l.profiles, n_workers)
        log.info("Precomputing arsenal cache (RHP) …")
        self._arsenal_cache.precompute(self._partition_r.profiles, n_workers)
        log.info(
            "Arsenal cache complete: %d total cached distances.",
            self._arsenal_cache.size,
        )

        if save_path:
            self._arsenal_cache.save(save_path)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_league_averages(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        """Load each season's league-average command vector, the shrinkage
        target, from ``derived.league_averages``. A missing table leaves the
        command vectors unshrunk."""
        try:
            season_filter = ""
            if seasons:
                season_list = ", ".join(str(s) for s in seasons)
                season_filter = f"AND season IN ({season_list})"

            rows = conn.execute(f"""
                SELECT season, profile_json
                FROM derived.league_averages
                WHERE entity_type = 'pitcher'
                  {season_filter}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found — no shrinkage fallbacks available.")
            return

        for season, profile_json_raw in rows:
            if isinstance(profile_json_raw, str):
                pj = json.loads(profile_json_raw)
            else:
                pj = profile_json_raw

            self._league_avg_command[season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f in COMMAND_FEATURES], dtype=np.float64
            )

        log.info("Loaded league averages for %d seasons.", len(rows))

    def _load_profiles(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        """Load the pitcher profiles at or above the minimum sample from
        ``derived.pitcher_season_metrics``."""
        season_filter = ""
        if seasons:
            season_list = ", ".join(str(s) for s in seasons)
            season_filter = f"AND psm.season IN ({season_list})"

        # A DuckDB file before migration 0027 has no asof_date column: the query
        # reads NULL for it, and the engine still builds.
        _present = duck_columns(conn, "derived", "pitcher_season_metrics")
        _asof_col = "psm.asof_date" if "asof_date" in _present else "NULL AS asof_date"

        rows = conn.execute(f"""
            SELECT
                psm.pitcher_id,
                psm.season,
                psm.p_throws,
                psm.sample_pitches,
                psm.gmm_model,
                psm.bb_rate,
                psm.k_rate,
                psm.csw_rate,
                psm.chase_rate,
                psm.zone_take_rate,
                psm.zone_rate,
                psm.whiff_rate,
                psm.ground_ball_rate,
                psm.fly_ball_rate,
                psm.line_drive_rate,
                psm.whip,
                psm.hr_per_9,
                psm.below_minimum_sample,
                {_asof_col}
            FROM derived.pitcher_season_metrics psm
            WHERE psm.below_minimum_sample = FALSE
              {season_filter}
        """).fetchall()

        log.info("Loading %d pitcher profiles from DuckDB …", len(rows))

        # Every profile must declare one cutoff (the point-in-time rule, SIM-537).
        asof_values: set = set()

        for row in rows:
            (
                pitcher_id,
                season,
                p_throws,
                sample_pitches,
                gmm_model_raw,
                bb_rate,
                k_rate,
                csw_rate,
                zone_take_rate,
                chase_rate,
                zone_rate,
                whiff_rate,
                gb_rate,
                fb_rate,
                ld_rate,
                whip,
                hr_per_9,
                below_min,
                asof_val,
            ) = row
            asof_values.add(asof_val)

            gmm = None
            if gmm_model_raw:
                if isinstance(gmm_model_raw, str):
                    gmm_dict = json.loads(gmm_model_raw)
                else:
                    gmm_dict = gmm_model_raw
                if gmm_dict.get("n_components", 0) > 0:
                    gmm = GMMModel.from_json(gmm_dict)

            command_vec = np.array(
                [
                    bb_rate or 0.0,
                    k_rate or 0.0,
                    csw_rate or 0.0,
                    zone_take_rate or 0.0,
                    chase_rate or 0.0,
                    zone_rate or 0.0,
                    whiff_rate or 0.0,
                ],
                dtype=np.float64,
            )

            self._profiles[(pitcher_id, season)] = PitcherProfile(
                pitcher_id=pitcher_id,
                season=season,
                p_throws=p_throws,
                sample_pitches=sample_pitches,
                gmm=gmm,
                command_vec=command_vec,
                eb_alpha=self._shrinkage.alpha(sample_pitches),
                below_minimum=bool(below_min),
            )

        # Refuse a mixed set: profiles built at two cutoffs (or one stamped and
        # one not) do not compare, and nothing downstream shows it.
        if len(asof_values) > 1:
            raise RuntimeError(
                "pitcher profiles were built at different cutoffs "
                f"({sorted(str(v) for v in asof_values)}). Rebuild them all at one "
                "date before scoring."
            )
        self._asof_date = next(iter(asof_values), None)
        if self._asof_date is not None:
            log.info("Pitcher profiles are as of %s.", self._asof_date)

    def _apply_shrinkage(self) -> None:
        """Shrink each profile's command vector toward its season's league
        average. A season with no league average leaves its vectors as they are."""
        for _key, profile in self._profiles.items():
            season = profile.season
            la_cmd = self._league_avg_command.get(season)

            if la_cmd is not None:
                profile.command_vec = self._shrinkage.shrink(
                    profile.command_vec,
                    la_cmd,
                    profile.sample_pitches,
                )

    def _standardize_arsenals(self) -> None:
        """Fit the population mean and standard deviation of the GMM component
        means, then move every GMM into z-scores (``standardize_gmm``).

        Each component of each profile counts by its mixing weight. W₂ reads
        raw Euclidean gaps: without this step, spin rate (a range of about
        1,000 rpm) outweighs velocity (about 15 mph).
        """
        all_means = []
        all_weights = []
        for profile in self._profiles.values():
            if profile.gmm is not None:
                for c in profile.gmm.components:
                    all_means.append(c.mean)
                    all_weights.append(c.weight)

        if not all_means:
            log.warning("No GMM components found — skipping arsenal standardization.")
            return

        means = np.array(all_means, dtype=np.float64)
        weights = np.array(all_weights, dtype=np.float64)
        weights /= weights.sum()

        pop_mean = np.average(means, axis=0, weights=weights)
        pop_var = np.average((means - pop_mean) ** 2, axis=0, weights=weights)
        pop_std = np.sqrt(pop_var)
        pop_std[pop_std == 0] = 1.0

        log.info(
            "  Arsenal standardization — pop_mean: [%s], pop_std: [%s]",
            ", ".join(f"{v:.1f}" for v in pop_mean),
            ", ".join(f"{v:.1f}" for v in pop_std),
        )

        for key, profile in self._profiles.items():
            if profile.gmm is not None:
                self._profiles[key].gmm = standardize_gmm(
                    profile.gmm,
                    pop_mean,
                    pop_std,
                )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        pitcher_id: int,
        season: int,
        n: int | None = None,
    ) -> list[SimilarityResult]:
        """Score the query pitcher-season against EVERY profile of the same hand.

        Parameters
        ----------
        pitcher_id : int
            The MLB player ID of the query pitcher.
        season : int
            The season of the query profile.
        n : int or None
            The number of top results to return; None returns all of them.

        Returns
        -------
        The SimilarityResults, highest composite score first. The query's own
        (pitcher_id, season) is left out; the pitcher's other seasons are in.
        An unknown pitcher-season returns an empty list.
        """
        query_profile = self._profiles.get((pitcher_id, season))
        if query_profile is None:
            log.warning(
                "Pitcher %d season %d not found in engine. "
                "Ensure build() was called with that season.",
                pitcher_id,
                season,
            )
            return []

        partition = self._partition_l if query_profile.p_throws == "L" else self._partition_r

        results = partition.score_all(
            query_profile=query_profile,
            normalizer=self._normalizer,
            arsenal_cache=self._arsenal_cache,
            command_rbf=self._command_rbf,
        )

        results.sort(key=lambda r: r.score, reverse=True)

        if n is not None:
            return results[:n]
        return results

    def query_pair(
        self,
        pitcher_a: tuple[int, int],
        pitcher_b: tuple[int, int],
    ) -> SimilarityResult | None:
        """Score two pitcher-seasons against each other.

        Parameters
        ----------
        pitcher_a : (pitcher_id, season)
            The query.
        pitcher_b : (pitcher_id, season)
            The candidate; the result names this one.

        Returns
        -------
        A SimilarityResult, or None when either pitcher-season is not loaded.
        """
        pa = self._profiles.get(pitcher_a)
        pb = self._profiles.get(pitcher_b)
        if pa is None or pb is None:
            return None

        composite, arsenal_s, command_s = self._score_pair(pa, pb)

        return SimilarityResult(
            pitcher_id=pb.pitcher_id,
            season=pb.season,
            p_throws=pb.p_throws,
            score=composite,
            arsenal_score=arsenal_s,
            command_score=command_s,
            sample_pitches=pb.sample_pitches,
        )

    def _score_pair(
        self,
        query: PitcherProfile,
        candidate: PitcherProfile,
    ) -> tuple[float, float, float]:
        """The two sub-scores and the composite of one pair, for ``query_pair``.

        For two different pitcher-seasons, the arithmetic matches
        ``HandednessPartition.score_all``.

        Returns
        -------
        (composite, arsenal, command), each in [0, 1]. The engine has no
        release sub-score: the GMM means carry the release point.
        ``TestScoreProperties.test_score_pair_returns_three_subscores`` in
        tests/unit/test_pitcher_similarity.py holds the shape.
        """
        key_q = (query.pitcher_id, query.season)
        key_c = (candidate.pitcher_id, candidate.season)
        w2_dist = self._arsenal_cache.get_or_compute(
            key_q,
            key_c,
            query.gmm,
            candidate.gmm,
        )

        arsenal_s = float(np.exp(-w2_dist / ARSENAL_SCALE)) if np.isfinite(w2_dist) else 0.0

        cmd_q = self._normalizer.normalize_command(query.command_vec)
        cmd_c = self._normalizer.normalize_command(candidate.command_vec)
        command_s = self._command_rbf.score(cmd_q, cmd_c)

        has_arsenal = query.gmm is not None and candidate.gmm is not None
        if has_arsenal:
            composite = WEIGHT_ARSENAL * arsenal_s + WEIGHT_COMMAND * command_s
        else:
            # A GMM missing: the arsenal weight moves onto the command score, so
            # the pair stays on the same [0, 1] scale as a full pair.
            composite = (_TOTAL_WEIGHT / WEIGHT_COMMAND) * command_s

        confidence = min(query.eb_alpha, candidate.eb_alpha)
        composite *= np.sqrt(confidence)

        return (
            float(np.clip(composite, 0.0, 1.0)),
            float(arsenal_s),
            float(command_s),
        )

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get_profile(self, pitcher_id: int, season: int) -> PitcherProfile | None:
        """The loaded profile of one pitcher-season, or None."""
        return self._profiles.get((pitcher_id, season))

    @property
    def profile_count(self) -> int:
        """The number of loaded profiles."""
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, int]]:
        """Every loaded (pitcher_id, season)."""
        return list(self._profiles.keys())

    @property
    def asof_date(self):
        """The cutoff every loaded profile declares, or ``None`` (the
        point-in-time rule, SIM-537)."""
        return self._asof_date

    @property
    def arsenal_cache_size(self) -> int:
        """The number of cached W₂ distances."""
        return self._arsenal_cache.size


# ============================================================================
# A manual build run
# ============================================================================
if __name__ == "__main__":
    engine = PitcherSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
