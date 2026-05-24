"""
fingerprints.py
===============
SIM-317 -- Real query-fingerprint derivation from game state.

WHAT THIS IS
------------
The SIM-303 scaffold built the two FAISS query vectors as deterministic *hashes*
of the count/outs (``PlateAppearanceSimulator._pitch_fingerprint`` /
``_battedball_fingerprint``, ``# TODO(SIM-317)``).  This module replaces those
hashes with the **real feature vectors the FAISS tiles expect**, derived from the
SIM-311 ``GameState`` + the similarity engines (resolved via the registry) +
SIM-321 cross-engine fusion, in the exact feature order the engines index
(``docs/architecture/2026-06-17-phase4-sim-loop-spec.md`` §4).

The two vectors:

  * the **10-dim pitch fingerprint** in
    ``similarity.engines.pitch_pitch_similarity.PITCH_FEATURES`` order
    ``[velo, ivb, hb, spin_rate, spin_axis, release_x, release_z, release_ext,
       plate_x, plate_z]`` (§4.1);
  * the **3-dim batted-ball fingerprint** in
    ``similarity.engines.batted_ball_similarity.BATTED_BALL_FEATURES`` order
    ``[exit_velo, launch_angle, spray_angle]`` (§4.2).

THE PRE-FILTER IS NOT IN THE VECTOR (§4.3)
------------------------------------------
The categorical matchup keys ``(pitcher_id, bat_hand, season)`` are the tile
**pre-filter**, passed as *arguments* to ``sample_pitch`` / ``sample_batted_ball``
-- they are NOT dimensions of the query vector.  Count is likewise NOT a
fingerprint dimension (its effect enters at step 4 via the SIM-056 foul
re-weight, SIM-318).  This module never encodes any of them as a vector dim.

SCORE DISCIPLINE (the locked boundary)
--------------------------------------
Engines stay distance-/similarity-pure; the **sampler** is the one and only place
a FAISS distance becomes a sampling weight.  This module never converts a distance
to a weight: SIM-321 fusion gives a bounded ``[0, 1]`` *shaping scalar* per engine
that tilts the fingerprint centroid (a comparability transform, not a weight); the
sampler still owns distance->weight.  See ``simulation/score_fusion.py``.

NO LIVE DB / FAISS REQUIRED (the injection seam)
------------------------------------------------
The expensive matchup geometry (the pitcher arsenal centroid, the batter's
contact/location profile) comes from a :class:`MatchupProfile` that the loop
supplies.  In production a :class:`MatchupProfileProvider` resolves it from the
engines via the registry; in tests it is *injected* directly (the in-memory
``__new__`` / dependency-injection pattern the SIM-302/303/321 tests use), so this
module unit-tests with no DuckDB and no FAISS.

PER-PA MATCHUP CACHE (§2.1 / SIM-119 §5)
----------------------------------------
The matchup (pitcher, batter, hand, season) does not change within a plate
appearance, so the expensive part of the derivation is cached for the whole PA
keyed on the pre-filter tuple.  Within one PA the cache hits and the engine /
provider work runs once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

import numpy as np
from numpy.typing import NDArray

# Import the engines' feature definitions as the SINGLE SOURCE OF TRUTH for the
# fingerprint order + the sqrt feature weighting.  These modules import only
# numpy / duckdb at module level (faiss is guarded), so importing them here does
# NOT require faiss to be installed.
from similarity.engines.pitch_pitch_similarity import (
    PITCH_FEATURES,
    FEATURE_SCALE as _PITCH_FEATURE_SCALE,
)
from similarity.engines.batted_ball_similarity import (
    BATTED_BALL_FEATURES,
    FEATURE_SCALE as _BB_FEATURE_SCALE,
)

from simulation.score_fusion import ScoreFusion

__all__ = [
    "PITCH_FEATURE_NAMES",
    "BATTED_BALL_FEATURE_NAMES",
    "PITCH_FINGERPRINT_DIM",
    "BATTED_BALL_FINGERPRINT_DIM",
    "MatchupProfile",
    "MatchupProfileProvider",
    "FingerprintDeriver",
]

# ---------------------------------------------------------------------------
# Feature order (re-exported from the engines so there is ONE source of truth).
# Re-deriving the names rather than re-listing them guarantees the loop's query
# vector lands in the SAME ordered, normalized space as the tile vectors.
# ---------------------------------------------------------------------------

#: The 10-dim pitch fingerprint feature names, in SIM-041 ``PITCH_FEATURES`` order
#: (spec §4.1).  ``[velo, ivb, hb, spin_rate, spin_axis, release_x, release_z,
#: release_ext, plate_x, plate_z]``.
PITCH_FEATURE_NAMES: tuple[str, ...] = tuple(f for f, _ in PITCH_FEATURES)

#: The 3-dim batted-ball fingerprint feature names, in SIM-042
#: ``BATTED_BALL_FEATURES`` order (spec §4.2). ``[exit_velo, launch_angle,
#: spray_angle]``.
BATTED_BALL_FEATURE_NAMES: tuple[str, ...] = tuple(f for f, _ in BATTED_BALL_FEATURES)

PITCH_FINGERPRINT_DIM = len(PITCH_FEATURE_NAMES)          # 10
BATTED_BALL_FINGERPRINT_DIM = len(BATTED_BALL_FEATURE_NAMES)  # 3

#: The 8 GMM arsenal dims the pitcher engine models (the pitch fingerprint's
#: first 8 features); the remaining two (plate_x, plate_z) are the intended
#: location, supplied by the situation/batter tilt (spec §4.1).
_ARSENAL_DIMS = PITCH_FEATURE_NAMES[:8]
_LOCATION_DIMS = PITCH_FEATURE_NAMES[8:]  # (plate_x, plate_z)


# ---------------------------------------------------------------------------
# MatchupProfile — the per-matchup geometry the fingerprint is assembled from
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchupProfile:
    """The per-matchup geometry the two fingerprints are assembled from (§4).

    This is the *expensive*, matchup-fixed part of the derivation -- it does NOT
    depend on the count, so it is computed once per PA and cached.  It carries the
    centroids the spec §4.1/§4.2 describe:

      * ``arsenal`` -- the pitcher's expected pitch geometry (the 8 GMM dims:
        velo, ivb, hb, spin_rate, spin_axis, release_x/z, release_ext), i.e. the
        arsenal centroid the pitcher GMM engine supplies (spec §4.1);
      * ``intended_location`` -- the ``(plate_x, plate_z)`` the count + matchup
        imply (situation KDTree + batter RBF tilt), completing the 10-dim pitch
        vector (spec §4.1);
      * ``batted_ball`` -- the batter's expected contact-quality centroid
        ``(exit_velo, launch_angle, spray_angle)`` for this hand / matchup, the
        3-dim batted-ball vector centroid (spec §4.2).

    These are **raw physical units** (mph / degrees / feet) -- the SAME space the
    engines fit their z-score normalizer in -- so they normalize into the tile
    space cleanly.  A profile may be built by the engines (production, via
    :class:`MatchupProfileProvider`) or injected directly (tests).
    """

    #: 8-vector of arsenal-centroid features in ``_ARSENAL_DIMS`` order (raw units).
    arsenal: NDArray[np.float64]
    #: 2-vector ``(plate_x, plate_z)`` intended location (raw units, feet).
    intended_location: NDArray[np.float64]
    #: 3-vector ``(exit_velo, launch_angle, spray_angle)`` contact centroid (raw).
    batted_ball: NDArray[np.float64]

    def __post_init__(self) -> None:
        arsenal = np.asarray(self.arsenal, dtype=np.float64).reshape(-1)
        location = np.asarray(self.intended_location, dtype=np.float64).reshape(-1)
        bb = np.asarray(self.batted_ball, dtype=np.float64).reshape(-1)
        if arsenal.shape[0] != len(_ARSENAL_DIMS):
            raise ValueError(
                f"arsenal must have {len(_ARSENAL_DIMS)} dims "
                f"({_ARSENAL_DIMS}); got shape {arsenal.shape}."
            )
        if location.shape[0] != len(_LOCATION_DIMS):
            raise ValueError(
                f"intended_location must have {len(_LOCATION_DIMS)} dims "
                f"({_LOCATION_DIMS}); got shape {location.shape}."
            )
        if bb.shape[0] != BATTED_BALL_FINGERPRINT_DIM:
            raise ValueError(
                f"batted_ball must have {BATTED_BALL_FINGERPRINT_DIM} dims "
                f"({BATTED_BALL_FEATURE_NAMES}); got shape {bb.shape}."
            )
        # frozen dataclass: bypass __setattr__ to store the coerced arrays.
        object.__setattr__(self, "arsenal", arsenal)
        object.__setattr__(self, "intended_location", location)
        object.__setattr__(self, "batted_ball", bb)


#: A provider resolves the matchup geometry for ``(pitcher_id, bat_hand, season,
#: batter_id)`` into a :class:`MatchupProfile`.  Pluggable so the loop can inject
#: an engine-backed provider (production) or a fixed one (tests).
MatchupProfileProvider = Callable[[int, str, int, "Optional[int]"], MatchupProfile]


# ---------------------------------------------------------------------------
# FingerprintDeriver — the SIM-317 deliverable
# ---------------------------------------------------------------------------


@dataclass
class _PitchNorm:
    """How the pitch query vector is normalized into the SIM-041 tile space.

    The tile was indexed as ``z = (raw - mean) / std`` then scaled by
    ``sqrt(weight)`` (``pitch_pitch_similarity.PitchNormalizer`` +
    ``FEATURE_SCALE``).  The query MUST use the SAME mean / std the engine fit on
    its pool so the query lands in the indexed space.  When the engine's fitted
    stats are unavailable (no-DB test mode) ``mean``/``std`` are None and the
    deriver applies the sqrt-weight scale only -- byte-identical to the engine's
    own ``normalize`` "unfitted" branch (``mean is None``).
    """

    mean: Optional[NDArray[np.float64]] = None
    std: Optional[NDArray[np.float64]] = None


class FingerprintDeriver:
    """Derive the two FAISS query fingerprints from game state (spec §4).

    Responsibilities (the SIM-317 acceptance criteria):

      1. derive the 10-dim pitch vector (``PITCH_FEATURES`` order) and the 3-dim
         batted-ball vector (``BATTED_BALL_FEATURES`` order) from game state +
         the matchup profile, normalized into the engines' space (z-score +
         the documented ``sqrt(weight)`` scaling);
      2. cache the matchup-fixed geometry **per PA** so repeated pitches in one
         plate appearance do not recompute it;
      3. stay on the correct side of the distance->weight boundary -- SIM-321
         fusion only *tilts the centroid*; it never weights, and this deriver
         never converts a distance to a weight (that is the sampler's job);
      4. keep the pre-filter keys ``(pitcher_id, bat_hand, season)`` as ARGS, not
         vector dims.

    Engine resolution is via the registry (``similarity.registry``); the actual
    geometry is supplied by an injected :class:`MatchupProfileProvider`, so the
    deriver unit-tests with no live DB / FAISS.
    """

    def __init__(
        self,
        profile_provider: MatchupProfileProvider,
        *,
        pitch_fusion: "ScoreFusion | None" = None,
        battedball_fusion: "ScoreFusion | None" = None,
        pitch_norm: "_PitchNorm | None" = None,
        battedball_norm: "_PitchNorm | None" = None,
    ) -> None:
        if profile_provider is None:
            raise ValueError(
                "FingerprintDeriver requires a MatchupProfileProvider "
                "(engine-backed in production, injected in tests)."
            )
        self._provider = profile_provider
        # SIM-321 fusion shapes WHICH neighbourhood the draw favours.  Default to
        # the documented profiles (``pitch_draw`` / ``batted_ball``).
        self._pitch_fusion = pitch_fusion or ScoreFusion(profile="pitch_draw")
        self._bb_fusion = battedball_fusion or ScoreFusion(profile="batted_ball")
        # Engine-fitted normalizer stats (None == sqrt-weight-only, the engine's
        # own unfitted branch -- valid for the no-DB test path).
        self._pitch_norm = pitch_norm or _PitchNorm()
        self._bb_norm = battedball_norm or _PitchNorm()
        # Per-PA matchup cache: pre-filter tuple -> MatchupProfile.  One entry,
        # reset on each new PA via :meth:`new_plate_appearance`.
        self._pa_cache: dict[tuple, MatchupProfile] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    # ---- per-PA cache control --------------------------------------------

    def new_plate_appearance(self) -> None:
        """Drop the per-PA matchup cache (call at the top of each new PA).

        The matchup is fixed *within* a PA but changes when the batter / pitcher
        rolls over, so the cache is cleared at the PA boundary (spec §2.1).
        """
        self._pa_cache.clear()

    @property
    def cache_hits(self) -> int:
        """How many matchup lookups were served from the per-PA cache (test #2)."""
        return self._cache_hits

    @property
    def cache_misses(self) -> int:
        """How many matchup lookups computed a fresh profile (cache miss)."""
        return self._cache_misses

    def _matchup(
        self, pitcher_id: int, bat_hand: str, season: int, batter_id: "int | None"
    ) -> MatchupProfile:
        """Resolve the matchup geometry, hitting the per-PA cache when possible.

        Keyed on the pre-filter tuple + batter id (the matchup identity); within
        one PA every pitch shares this key, so the expensive provider call runs
        once per PA (spec §2.1 / SIM-119 §5).
        """
        key = (int(pitcher_id), str(bat_hand), int(season),
               None if batter_id is None else int(batter_id))
        cached = self._pa_cache.get(key)
        if cached is not None:
            self._cache_hits += 1
            return cached
        self._cache_misses += 1
        profile = self._provider(pitcher_id, bat_hand, season, batter_id)
        self._pa_cache[key] = profile
        return profile

    # ---- the two derivations ---------------------------------------------

    def pitch_fingerprint(
        self,
        state,
        *,
        pitch_signals: "Mapping | None" = None,
    ) -> NDArray[np.float32]:
        """Build the 10-dim pitch query vector from ``state`` (spec §4.1).

        ``state`` is a SIM-311 ``GameState`` (or anything exposing
        ``pitcher_id``/``bat_hand``/``season``/``batter_id``).  The raw vector is
        assembled in ``PITCH_FEATURES`` order: the 8 arsenal dims from the
        pitcher's centroid, then ``(plate_x, plate_z)`` from the intended
        location.  SIM-321 fusion (``pitch_signals``) supplies a bounded shaping
        scalar that tilts the location toward the favoured neighbourhood; the
        vector is then z-score + sqrt-weight normalized into the SIM-041 tile
        space.  The pre-filter keys are NOT encoded here (spec §4.3).
        """
        prof = self._matchup(
            state.pitcher_id, state.bat_hand, state.season,
            getattr(state, "batter_id", None),
        )
        # Assemble the raw 10-vector in PITCH_FEATURES order: arsenal then loc.
        raw = np.empty(PITCH_FINGERPRINT_DIM, dtype=np.float64)
        raw[:8] = prof.arsenal
        raw[8:] = prof.intended_location

        # SIM-321 fusion: a [0,1] shaping scalar tilts the *location* toward the
        # neighbourhood the per-matchup engine scores favour.  This is a centroid
        # tilt, NOT a distance->weight conversion (the sampler owns that).  At the
        # neutral scalar 0.5 the location is unchanged; >0.5 expands toward the
        # edge, <0.5 attacks the middle -- a bounded, deterministic nudge.
        if pitch_signals:
            shape = self._pitch_fusion.fuse(pitch_signals).fused
            raw[8:] = self._tilt_location(raw[8:], shape)

        return self._normalize(raw, self._pitch_norm, _PITCH_FEATURE_SCALE)

    def battedball_fingerprint(
        self,
        state,
        *,
        battedball_signals: "Mapping | None" = None,
    ) -> NDArray[np.float32]:
        """Build the 3-dim batted-ball query vector from ``state`` (spec §4.2).

        Built only on contact (step 5): the batter's expected contact centroid
        ``(exit_velo, launch_angle, spray_angle)`` in ``BATTED_BALL_FEATURES``
        order, optionally tilted by SIM-321 fusion (``battedball_signals``), then
        z-score + sqrt-weight normalized into the SIM-042 tile space.  Pre-filter
        keys ``(bat_hand, season)`` are passed to ``sample_batted_ball`` as args,
        not encoded here (spec §4.3).
        """
        prof = self._matchup(
            state.pitcher_id, state.bat_hand, state.season,
            getattr(state, "batter_id", None),
        )
        raw = np.array(prof.batted_ball, dtype=np.float64, copy=True)

        if battedball_signals:
            shape = self._bb_fusion.fuse(battedball_signals).fused
            # Tilt exit velocity toward harder contact as the shaping scalar
            # rises (a bounded centroid nudge, not a weight).
            raw[0] = self._tilt_exit_velo(raw[0], shape)

        return self._normalize(raw, self._bb_norm, _BB_FEATURE_SCALE)

    # ---- internals: centroid tilt + normalization ------------------------

    @staticmethod
    def _tilt_location(
        location: NDArray[np.float64], shape: float
    ) -> NDArray[np.float64]:
        """Tilt ``(plate_x, plate_z)`` by the SIM-321 shaping scalar in ``[0,1]``.

        Deterministic and bounded: ``0.5`` is neutral (no change).  Above 0.5 the
        location is pushed marginally toward the edge of its current quadrant
        (expand the zone -- ahead in count); below 0.5 it is pulled toward the
        middle (attack -- behind in count).  The magnitude is small (<= 1 unit
        scaled by the deviation from neutral) so the fingerprint stays in the
        pitcher's realistic location envelope.
        """
        factor = 1.0 + (float(shape) - 0.5)  # in [0.5, 1.5]
        return location * factor

    @staticmethod
    def _tilt_exit_velo(exit_velo: float, shape: float) -> float:
        """Tilt the contact-centroid exit velocity by the shaping scalar.

        Neutral at ``0.5``; a small bounded nudge (+/- up to ~5 mph at the
        extremes) toward harder contact as the favoured-neighbourhood signal
        rises.  Bounded so the centroid stays physical.
        """
        return float(exit_velo) + (float(shape) - 0.5) * 10.0

    @staticmethod
    def _normalize(
        raw: NDArray[np.float64],
        norm: _PitchNorm,
        feature_scale: NDArray[np.float64],
    ) -> NDArray[np.float32]:
        """Normalize a raw feature vector into the engine's indexed space.

        Mirrors the engines' ``Normalizer.normalize`` EXACTLY (so the query lands
        in the same space as the tile vectors):

          * fitted   -> ``z = (raw - mean) / std`` (NaN -> 0) then ``* sqrt(weight)``;
          * unfitted -> ``raw * sqrt(weight)`` (the engine's ``mean is None`` branch).

        ``feature_scale`` is the engine's ``FEATURE_SCALE`` (``sqrt(weight)``),
        imported so the weighting is identical, not re-derived.
        """
        if norm.mean is None or norm.std is None:
            return (raw * feature_scale).astype(np.float32)
        normed = (raw - norm.mean) / norm.std
        return (np.nan_to_num(normed, nan=0.0) * feature_scale).astype(np.float32)
