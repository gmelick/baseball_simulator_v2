"""
simulation/matchup_provider.py
==============================
Production MatchupProfileProvider + the tile-space normalization artifacts.

WHY THIS EXISTS
---------------
The FAISS play-pool tiles and the loop's query fingerprint MUST live in the SAME
vector space or the k-NN is meaningless (a raw-space tile + a normalized query =
distance dominated by ``spin_rate`` magnitude, so every query collapses to the
lowest-norm vectors -- the SIM-421 "no balls in play / walk-fest" defect).

The fix is a single shared space ``z = (raw - mean) / std * sqrt(weight)``:

  * ``pipeline/batch/play_pool_cache.py`` normalizes the tile vectors with that
    transform at build time AND writes the fitted ``mean``/``std`` (this module's
    :func:`write_norm`) plus the per-matchup RAW centroids (:func:`write_centroids`);
  * this module reads them back at sim time to build the
    :class:`~simulation.fingerprints.FingerprintDeriver`'s normalizers (so the
    query is normalized identically) and to supply :class:`MatchupProfile`
    geometry (the per-matchup raw centroid the fingerprint is assembled from).

``sqrt(weight)`` is the engine's ``FEATURE_SCALE`` constant (imported on both
sides), so only the fit-dependent ``mean``/``std`` need to cross via disk.

The provider is built from on-disk artifacts, so every ProcessPool worker can
rebuild it from the factory regardless of the start method (fork OR spawn) -- no
live object crosses the process boundary.
"""

from __future__ import annotations

import json
import os

import numpy as np
from numpy.typing import NDArray

from simulation.fingerprints import (
    BATTED_BALL_FINGERPRINT_DIM,
    PITCH_FINGERPRINT_DIM,
    MatchupProfile,
    _PitchNorm,
)

__all__ = [
    "PITCH_NORM_FILE",
    "BATTEDBALL_NORM_FILE",
    "PITCH_CENTROIDS_FILE",
    "BATTEDBALL_CENTROIDS_FILE",
    "write_norm",
    "read_norm",
    "write_centroids",
    "read_centroids",
    "PrecomputedMatchupProvider",
    "load_provider",
    "load_pitch_norm",
    "load_battedball_norm",
]

#: Artifact filenames, written into the play-pool root by play_pool_cache.
PITCH_NORM_FILE = "pitch_norm.json"
BATTEDBALL_NORM_FILE = "battedball_norm.json"
PITCH_CENTROIDS_FILE = "pitch_centroids.json"
BATTEDBALL_CENTROIDS_FILE = "battedball_centroids.json"

#: The 8 arsenal dims lead the 10-dim pitch fingerprint; the last 2 are location.
_ARSENAL = 8

#: Provider lookup key for the league-average fall-back pitch tile (pitcher_id=0).
_FALLBACK_PITCHER_ID = 0


# ---------------------------------------------------------------------------
# Norm-stats artifact (the fitted mean/std of the global pool)
# ---------------------------------------------------------------------------


def write_norm(pool_dir: str, fname: str, mean: NDArray, std: NDArray) -> str:
    """Persist a fitted normalizer's ``mean``/``std`` as JSON in ``pool_dir``."""
    path = os.path.join(pool_dir, fname)
    payload = {
        "mean": [float(x) for x in np.asarray(mean).reshape(-1)],
        "std": [float(x) for x in np.asarray(std).reshape(-1)],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    return path


def read_norm(pool_dir: str, fname: str) -> tuple[NDArray, NDArray] | None:
    """Load ``(mean, std)`` written by :func:`write_norm`, or None if absent."""
    path = os.path.join(pool_dir, fname)
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    mean = np.asarray(payload["mean"], dtype=np.float64)
    std = np.asarray(payload["std"], dtype=np.float64)
    std = np.where(std == 0.0, 1.0, std)
    return mean, std


# ---------------------------------------------------------------------------
# Centroid artifact (per-matchup RAW feature means)
# ---------------------------------------------------------------------------


def write_centroids(pool_dir: str, fname: str, mapping: dict[str, list[float]]) -> str:
    """Persist a ``{key: [raw means]}`` centroid map as JSON in ``pool_dir``."""
    path = os.path.join(pool_dir, fname)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, sort_keys=True)
    return path


def read_centroids(pool_dir: str, fname: str) -> dict[str, NDArray]:
    """Load a centroid map written by :func:`write_centroids` (empty on miss)."""
    path = os.path.join(pool_dir, fname)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: np.asarray(v, dtype=np.float64) for k, v in raw.items()}


def pitch_key(season: int, pitcher_id: int, bat_hand: str) -> str:
    return f"{int(season)}/{int(pitcher_id)}/{bat_hand}"


def battedball_key(season: int, bat_hand: str) -> str:
    return f"{int(season)}/{bat_hand}"


# ---------------------------------------------------------------------------
# The production provider
# ---------------------------------------------------------------------------


class PrecomputedMatchupProvider:
    """Resolve a :class:`MatchupProfile` from precomputed per-matchup centroids.

    The arsenal (8) + intended location (2) come from the pitch centroid keyed
    ``"<season>/<pitcher_id>/<bat_hand>"`` -- i.e. the RAW centroid of the exact
    tile the sampler will query, so the normalized query lands at the centre of
    that tile's neighbourhood (a dense, representative draw rather than the
    degenerate lowest-norm collapse).  The batted-ball centroid (3) comes from
    the ``"<season>/<bat_hand>"`` key (the batted-ball tile's grain).

    Misses fall forward: an unknown pitcher -> the season/hand fall-back (pitcher
    0) centroid -> the global pitch mean; an unknown season/hand batted-ball key
    -> the global batted-ball mean.  This mirrors the sampler's own tile
    fall-forward so the query always lands in a populated region.
    """

    def __init__(
        self,
        pitch_centroids: dict[str, NDArray],
        battedball_centroids: dict[str, NDArray],
        pitch_global: NDArray,
        battedball_global: NDArray,
    ) -> None:
        self._pitch = pitch_centroids
        self._bb = battedball_centroids
        self._pitch_global = np.asarray(pitch_global, dtype=np.float64).reshape(-1)
        self._bb_global = np.asarray(battedball_global, dtype=np.float64).reshape(-1)

    def _pitch_centroid(self, pitcher_id: int, bat_hand: str, season: int) -> NDArray:
        for key in (
            pitch_key(season, pitcher_id, bat_hand),
            pitch_key(season, _FALLBACK_PITCHER_ID, bat_hand),
        ):
            vec = self._pitch.get(key)
            if vec is not None:
                return vec
        return self._pitch_global

    def _battedball_centroid(self, bat_hand: str, season: int) -> NDArray:
        vec = self._bb.get(battedball_key(season, bat_hand))
        return vec if vec is not None else self._bb_global

    def __call__(
        self, pitcher_id: int, bat_hand: str, season: int, batter_id: int | None
    ) -> MatchupProfile:
        pitch = self._pitch_centroid(pitcher_id, bat_hand, season)
        bb = self._battedball_centroid(bat_hand, season)
        return MatchupProfile(
            arsenal=pitch[:_ARSENAL].copy(),
            intended_location=pitch[_ARSENAL:].copy(),
            batted_ball=bb.copy(),
        )


# ---------------------------------------------------------------------------
# Loaders (used by production_factory's deriver builder)
# ---------------------------------------------------------------------------


def load_pitch_norm(pool_dir: str) -> _PitchNorm | None:
    """Build the deriver's pitch normalizer from the persisted stats (None on miss)."""
    stats = read_norm(pool_dir, PITCH_NORM_FILE)
    if stats is None:
        return None
    mean, std = stats
    if mean.shape[0] != PITCH_FINGERPRINT_DIM:
        return None
    return _PitchNorm(mean=mean, std=std)


def load_battedball_norm(pool_dir: str) -> _PitchNorm | None:
    """Build the deriver's batted-ball normalizer from the persisted stats."""
    stats = read_norm(pool_dir, BATTEDBALL_NORM_FILE)
    if stats is None:
        return None
    mean, std = stats
    if mean.shape[0] != BATTED_BALL_FINGERPRINT_DIM:
        return None
    return _PitchNorm(mean=mean, std=std)


def load_provider(pool_dir: str) -> PrecomputedMatchupProvider | None:
    """Build a :class:`PrecomputedMatchupProvider` from on-disk centroids.

    Returns None when the centroid artifacts are absent (e.g. tiles built before
    this fix, or a no-artifact test env) so the caller falls back to the stub
    fingerprint -- the deriver is only wired when the data to feed it exists.
    """
    pitch = read_centroids(pool_dir, PITCH_CENTROIDS_FILE)
    bb = read_centroids(pool_dir, BATTEDBALL_CENTROIDS_FILE)
    if not pitch or not bb:
        return None
    pitch_global = np.mean(np.stack(list(pitch.values())), axis=0)
    bb_global = np.mean(np.stack(list(bb.values())), axis=0)
    return PrecomputedMatchupProvider(pitch, bb, pitch_global, bb_global)
